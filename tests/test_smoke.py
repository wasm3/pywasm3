"""Smoke tests on a pre-assembled module - the only ones that run without wabt."""

import base64
import gc
import struct

import pytest

import wasm3

# (module (func $fib (export "fib") (param i64) (result i64) ...))
FIB_WASM = base64.b64decode("AGFzbQEAAAABBgFgAX4BfgMCAQAHBwEDZmliAAAKHwEdACAAQgJUBEAgAA8LIABCAn0QACAAQgF9EAB8Dws=")


def test_version_is_reported():
    assert isinstance(wasm3.M3_VERSION, str)
    assert wasm3.M3_VERSION


def test_public_api_is_reexported():
    assert wasm3.Environment.__module__ == "wasm3"
    for name in wasm3.__all__:
        assert hasattr(wasm3, name)


def test_call_precompiled_module():
    env = wasm3.Environment()
    rt = env.new_runtime(2048)
    mod = env.parse_module(FIB_WASM)
    rt.load(mod)
    assert rt.find_function("fib")(24) == 46368


def test_function_introspection():
    env = wasm3.Environment()
    rt = env.new_runtime(2048)
    rt.load(env.parse_module(FIB_WASM))
    fib = rt.find_function("fib")
    assert fib.name == "fib"
    assert fib.num_args == 1
    assert fib.num_rets == 1
    assert len(fib.arg_types) == 1
    assert len(fib.ret_types) == 1


def _fib_runtime(gas_limit=None):
    env = wasm3.Environment()
    rt = env.new_runtime(2048)
    rt.load(env.parse_module(FIB_WASM))
    # Before find_function: bodies compiled earlier are not metered.
    if gas_limit is not None:
        rt.gas_limit = gas_limit
    return rt, rt.find_function("fib")


def test_gas_is_off_by_default():
    rt, fib = _fib_runtime()
    assert fib(24) == 46368
    assert rt.gas_limit == 0
    assert rt.gas_used == 0


def test_gas_is_metered():
    rt, fib = _fib_runtime(1_000_000)
    assert rt.gas_limit == 1_000_000
    assert fib(10) == 55
    used = rt.gas_used
    assert 0 < used < 1_000_000
    # Charging is cumulative until the limit is set again.
    fib(10)
    assert rt.gas_used == pytest.approx(2 * used)


def test_out_of_gas_traps_and_rearms():
    rt, fib = _fib_runtime(1)
    with pytest.raises(RuntimeError, match="out of gas"):
        fib(24)
    assert rt.gas_used >= 1
    # A new limit re-arms the runtime with a full budget.
    rt.gas_limit = 1_000_000
    assert rt.gas_used == 0
    assert fib(10) == 55


def test_gas_limit_rejects_non_numbers():
    rt, _ = _fib_runtime()
    with pytest.raises(TypeError):
        rt.gas_limit = "lots"  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(AttributeError):
        del rt.gas_limit


# (module
#   (memory (export "memory") 1)
#   (func (export "grow") (param i32) (result i32) (memory.grow (local.get 0)))
#   (func (export "store") (param i32 i32) (i32.store8 (local.get 0) (local.get 1)))
#   (func (export "load") (param i32) (result i32) (i32.load8_u (local.get 0))))
MEM_WASM = base64.b64decode(
    "AGFzbQEAAAABCwJgAX8Bf2ACf38AAwQDAAEABQMBAAEHIAQGbWVtb3J5AgAEZ3JvdwAABXN0b3JlAAEEbG9hZAACChoD"
    "BgAgAEAACwkAIAAgAToAAAsHACAALQAACw=="
)
PAGE = 65536


def _mem_module():
    env = wasm3.Environment()
    rt = env.new_runtime(2048)
    mod = env.parse_module(MEM_WASM)
    rt.load(mod)
    return rt, mod


def test_get_memory_by_index_or_export_name():
    _, mod = _mem_module()
    assert isinstance(mod.get_memory(), wasm3.Memory)
    assert len(mod.get_memory(0)) == len(mod.get_memory("memory")) == PAGE
    for key in (1, -1, "nope"):
        with pytest.raises(RuntimeError, match="unknown memory"):
            mod.get_memory(key)


def test_get_memory_without_memory_or_before_load():
    env = wasm3.Environment()
    rt = env.new_runtime(2048)
    mod = env.parse_module(FIB_WASM)
    with pytest.raises(RuntimeError, match="not loaded"):
        mod.get_memory(0)
    rt.load(mod)
    with pytest.raises(RuntimeError, match="unknown memory"):
        mod.get_memory()


def test_memory_is_shared_with_wasm():
    rt, mod = _mem_module()
    mem = mod.get_memory()
    mem[10] = 42
    assert rt.find_function("load")(10) == 42
    rt.find_function("store")(11, 7)
    assert mem[11] == 7
    mem[20:23] = b"abc"
    assert mem[20:23] == b"abc"
    assert mem[-1] == 0
    with pytest.raises(IndexError):
        mem[PAGE]


def test_memory_slices_are_copies():
    _, mod = _mem_module()
    mem = mod.get_memory()
    snapshot = mem[0:4]
    assert isinstance(snapshot, bytes)
    mem[0] = 1
    assert snapshot == b"\0\0\0\0"


def test_memory_buffer_protocol():
    _, mod = _mem_module()
    mem = mod.get_memory()
    struct.pack_into("<I", mem, 100, 0xDEADBEEF)
    assert struct.unpack_from("<I", mem, 100) == (0xDEADBEEF,)
    with memoryview(mem) as view:
        assert not view.readonly
        assert len(view) == PAGE


def test_memory_survives_grow():
    rt, mod = _mem_module()
    mem = mod.get_memory()
    mem[10] = 42
    assert rt.find_function("grow")(2) == 1
    assert len(mem) == 3 * PAGE
    assert mem[10] == 42
    mem[-1] = 9
    assert rt.find_function("load")(3 * PAGE - 1) == 9


def test_memory_keeps_its_module_alive():
    _, mod = _mem_module()
    mem = mod.get_memory()
    del mod
    gc.collect()
    mem[0] = 5
    assert mem[0] == 5


def test_memory_cannot_be_created_directly():
    with pytest.raises(TypeError):
        wasm3.Memory()
