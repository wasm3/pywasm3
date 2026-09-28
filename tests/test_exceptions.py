"""Exception handling across the host boundary: wasm3.Tag, wasm3.WasmException,
Runtime.new_tag(), Module.get_tag() and Module.link_tag()."""

import gc
import sys

import pytest
from helpers import wat2wasm

import wasm3

EH_WASM = wat2wasm("""
(module
  (type $pay (func (result i32 i64)))
  (import "env" "host" (func $host (param i32)))
  (import "env" "err" (tag $err (param i32)))
  (tag $e (export "e") (param i32 i64))
  (tag $f (export "f") (param f32 f64))
  (tag $none (export "none"))

  (func (export "throw") (param i32 i64) (throw $e (local.get 0) (local.get 1)))
  (func (export "call_host") (param i32) (call $host (local.get 0)))
  (func (export "throw_floats") (throw $f (f32.const 1.5) (f64.const -2.25)))

  ;; a guest-only round trip, nothing crossing to the host
  (func (export "catch_own") (param i32) (result i32)
    (block $h
      (try_table (catch $none $h) (throw $none))
      (return (i32.const -1)))
    (local.get 0))

  ;; what the host throws: the payload of $e, 0 if it returned, -1 for anything else
  (func (export "catch_e") (param i32) (result i64) (local i64)
    (block $other
      (block $h (type $pay)
        (try_table (catch $e $h) (catch_all $other) (call $host (local.get 0)))
        (return (i64.const 0)))
      (local.set 1)
      (i64.extend_i32_s)
      (i64.add (local.get 1))
      (return))
    (i64.const -1))

  ;; the payload of the linked "err", or -1 if something else was thrown
  (func (export "catch_err") (param i32) (result i32)
    (block $other
      (block $h (result i32)
        (try_table (catch $err $h) (catch_all $other) (call $host (local.get 0)))
        (return (i32.const 0)))
      (return))
    (i32.const -1))

  ;; 1 if anything at all was thrown
  (func (export "catch_all") (param i32) (result i32)
    (block $h
      (try_table (catch_all $h) (call $host (local.get 0)))
      (return (i32.const 0)))
    (i32.const 1))

  ;; catches anything, and throws it on as it was
  (func (export "rethrow") (param i32)
    (block $h (result exnref)
      (try_table (catch_all_ref $h) (call $host (local.get 0)))
      (return))
    (throw_ref))
)
""")


class Instance:
    """EH_WASM, loaded, with its "host" import calling whatever `host` is set to."""

    def __init__(self, link_err=True, env=None):
        self.env = env or wasm3.Environment()
        self.rt = self.env.new_runtime(64 * 1024)
        self.mod = self.env.parse_module(EH_WASM)
        self.rt.load(self.mod)
        self.host = lambda x: None
        self.mod.link_function("env", "host", "v(i)", lambda x: self.host(x))
        self.err = self.rt.new_tag("v(i)")
        if link_err:
            self.mod.link_tag("env", "err", self.err)

    def __getattr__(self, name):
        return self.rt.find_function(name)


def raiser(exc):
    def host(x):
        raise exc

    return host


def test_guest_catches_its_own():
    assert Instance().catch_own(5) == 5


def test_uncaught_carries_tag_and_payload():
    inst = Instance()
    with pytest.raises(wasm3.WasmException) as info:
        inst.throw(-7, 1 << 40)
    assert info.value.tag == inst.mod.get_tag("e")
    assert info.value.payload == (-7, 1 << 40)
    assert info.value.args == (info.value.tag, -7, 1 << 40)

    with pytest.raises(wasm3.WasmException) as info:
        inst.throw_floats()
    assert info.value.tag == inst.mod.get_tag("f")
    assert info.value.payload == (1.5, -2.25)


def test_uncaught_is_still_a_runtime_error():
    with pytest.raises(RuntimeError):
        Instance().throw(1, 2)


def test_tags():
    inst = Instance()
    e = inst.mod.get_tag("e")
    assert e.num_args == 2
    assert e.arg_types == (1, 2)  # i32, i64
    assert inst.mod.get_tag("f").arg_types == (3, 4)
    assert inst.mod.get_tag("none").num_args == 0
    assert repr(e) == "<wasm3.Tag (i32, i64)>"

    assert e == inst.mod.get_tag("e")
    assert hash(e) == hash(inst.mod.get_tag("e"))
    assert e != inst.mod.get_tag("f")
    assert e != inst.err
    assert inst.rt.new_tag("v(iI)") != inst.rt.new_tag("v(iI)")
    assert len({e, inst.mod.get_tag("e"), inst.err}) == 2

    with pytest.raises(TypeError):
        wasm3.Tag()


def test_get_tag_errors():
    inst = Instance()
    with pytest.raises(RuntimeError, match="tag lookup failed"):
        inst.mod.get_tag("throw")  # a function
    with pytest.raises(RuntimeError, match="tag lookup failed"):
        inst.mod.get_tag("missing")

    mod = inst.env.parse_module(EH_WASM)
    with pytest.raises(RuntimeError, match="not loaded"):
        mod.get_tag("e")


def test_new_tag_and_link_tag_errors():
    inst = Instance(link_err=False)
    with pytest.raises(ValueError, match="unknown argument type char"):
        inst.rt.new_tag("v(x)")
    with pytest.raises(RuntimeError, match="incompatible import type"):
        inst.mod.link_tag("env", "err", inst.rt.new_tag("v(f)"))
    with pytest.raises(RuntimeError, match="tag lookup failed"):
        inst.mod.link_tag("env", "host", inst.err)  # a function
    with pytest.raises(RuntimeError, match="tag lookup failed"):
        inst.mod.link_tag("other", "err", inst.err)
    with pytest.raises(TypeError):
        inst.mod.link_tag("env", "err", "not a tag")  # pyright: ignore[reportArgumentType]
    inst.mod.link_tag("*", "err", inst.err)


def test_import_throws_a_module_tag():
    inst = Instance()

    def host(x):
        raise wasm3.WasmException(inst.mod.get_tag("e"), x, 100)

    inst.host = host
    assert inst.catch_e(3) == 103
    inst.host = lambda x: None
    assert inst.catch_e(3) == 0


def test_import_throws_a_linked_host_tag():
    inst = Instance()
    inst.host = raiser(wasm3.WasmException(inst.err, 42))
    assert inst.catch_err(0) == 42


def test_unlinked_host_tag_is_caught_by_catch_all_only():
    inst = Instance(link_err=False)
    hidden = inst.rt.new_tag("v(i)")
    inst.host = raiser(wasm3.WasmException(hidden, 5))
    assert inst.catch_e(0) == -1
    assert inst.catch_all(0) == 1
    with pytest.raises(wasm3.WasmException) as info:
        inst.rethrow(0)
    assert info.value.tag == hidden
    assert info.value.payload == (5,)


def test_python_exception_is_caught_by_catch_all():
    inst = Instance()
    inst.host = raiser(ValueError("boom"))
    assert inst.catch_all(0) == 1
    assert inst.catch_e(0) == -1
    assert inst.catch_err(0) == -1


@pytest.mark.parametrize("name", ["call_host", "rethrow"])
def test_python_exception_comes_out_as_itself(name):
    # through a guest that doesn't catch it, and one that catches it and throws it on
    inst = Instance()
    err = ValueError("boom")
    inst.host = raiser(err)
    with pytest.raises(ValueError) as info:
        inst.rt.find_function(name)(0)
    assert info.value is err


def test_python_exception_uncaught_keeps_traceback():
    inst = Instance()

    def failing(x):
        raise KeyError(x)

    inst.host = failing
    with pytest.raises(KeyError) as info:
        inst.rethrow(9)
    assert info.value.args == (9,)
    assert any(entry.name == "failing" for entry in info.traceback)


def test_base_exception_is_not_catchable():
    inst = Instance()
    inst.host = raiser(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        inst.catch_all(0)


def test_bad_wasm_exception_raises_type_error():
    inst = Instance()
    inst.host = raiser(wasm3.WasmException(inst.mod.get_tag("e"), 1))  # one value short
    with pytest.raises(TypeError, match="carries 2 values") as info:
        inst.rethrow(0)
    assert isinstance(info.value.__context__, wasm3.WasmException)

    inst.host = raiser(wasm3.WasmException("not a tag"))  # pyright: ignore[reportArgumentType]
    assert inst.catch_all(0) == 1  # a TypeError is an Exception like any other


def test_exception_from_a_nested_call():
    # The import calls back into the guest, which throws; the WasmException that comes
    # out goes back in through the import as itself, and the outer catch gets it.
    inst = Instance()
    inst.host = lambda x: inst.throw(x, 1000)
    assert inst.catch_e(7) == 1007


def test_start_function_throws():
    # wasm3 runs the start function ahead of the first call, which is what raises
    env = wasm3.Environment()
    rt = env.new_runtime(64 * 1024)
    mod = env.parse_module("""
    (module
      (tag $e (export "e") (param i32))
      (func $start (throw $e (i32.const 11)))
      (func (export "f"))
      (start $start))
    """)
    rt.load(mod)
    with pytest.raises(wasm3.WasmException) as info:
        rt.find_function("f")()
    assert info.value.tag == mod.get_tag("e")
    assert info.value.payload == (11,)


def test_caught_python_exceptions_are_released():
    inst = Instance()
    err = ValueError("kept only while in flight")
    inst.host = raiser(err)
    before = sys.getrefcount(err)
    for _ in range(100):
        assert inst.catch_all(0) == 1
    gc.collect()
    assert sys.getrefcount(err) == before


def test_tag_of_another_runtime():
    # A host tag linked into a module of another runtime, whose own runtime object is
    # gone by the time anything throws it
    env = wasm3.Environment()
    other = env.new_runtime(64 * 1024)
    tag = other.new_tag("v(i)")
    inst = Instance(link_err=False, env=env)
    inst.mod.link_tag("env", "err", tag)
    inst.host = raiser(wasm3.WasmException(tag, 77))
    del other, tag
    gc.collect()
    assert inst.catch_err(0) == 77
    with pytest.raises(wasm3.WasmException) as info:
        inst.rethrow(0)
    assert info.value.payload == (77,)
    assert info.value.tag.arg_types == (1,)


def test_exnref_does_not_cross():
    env = wasm3.Environment()
    rt = env.new_runtime(64 * 1024)
    mod = env.parse_module("""
    (module
      (import "env" "take" (func $take (param exnref)))
      (tag $e)
      (func $caught (export "caught") (result exnref)
        (block $h (result exnref) (try_table (catch_all_ref $h) (throw $e)) (unreachable)))
      (func (export "pass") (call $take (call $caught))))
    """)
    rt.load(mod)
    mod.link_function("env", "take", lambda exn: None)
    with pytest.raises(TypeError, match="exnref"):
        rt.find_function("caught")()
    with pytest.raises(TypeError, match="exnref"):
        rt.find_function("pass")()
