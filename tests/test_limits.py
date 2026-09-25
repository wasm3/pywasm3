"""Resource caps: Runtime.memory_limit, table_limit and continuation_limit, and the
usage counters beside them."""

import gc

import pytest
from helpers import wat2wasm

import wasm3

PAGE = 65536

GROW_WASM = wat2wasm("""
(module
  (memory (export "memory") 1)
  (table 1 funcref)
  (func (export "grow") (param i32) (result i32) (memory.grow (local.get 0)))
  (func (export "grow_table") (param i32) (result i32) (table.grow (ref.null func) (local.get 0)))
  (func (export "spin") (loop $again (br $again)))
)
""")

BIG_WASM = wat2wasm("(module (memory 3))")

# The bundled wabt predates stack switching, so these two are bytes, taken from wasm3's
# test/regression/limit-continuation-{loop,tree}.wast.
#
# (func $child)
# (func (export "loop") (result i32) (local $n i32)
#   i32.const 1000 local.set $n
#   loop $again
#     ref.func $child cont.new $c resume $c
#     local.get $n i32.const 1 i32.sub local.tee $n br_if $again
#   end i32.const 1)
CONT_LOOP_WASM = (
    b"\x00\x61\x73\x6d\x01\x00\x00\x00\x01\x0a\x03\x60\x00\x00\x5d\x00\x60\x00\x01\x7f"
    b"\x03\x03\x02\x00\x02\x07\x08\x01\x04loop\x00\x01\x09\x05\x01\x03\x00\x01\x00"
    b"\x0a\x23\x02\x02\x00\x0b\x1e\x01\x01\x7f\x41\xe8\x07\x21\x00\x03\x40"
    b"\xd2\x00\xe0\x01\xe3\x01\x00\x20\x00\x41\x01\x6b\x22\x00\x0d\x00\x0b\x41\x01\x0b"
)
# Each continuation starts another before it finishes, without end.
# (func $child ref.func $child cont.new $c resume $c)
# (func (export "tree") ref.func $child cont.new $c resume $c)
CONT_TREE_WASM = (
    b"\x00\x61\x73\x6d\x01\x00\x00\x00\x01\x06\x02\x60\x00\x00\x5d\x00"
    b"\x03\x03\x02\x00\x00\x07\x08\x01\x04tree\x00\x01\x09\x05\x01\x03\x00\x01\x00"
    b"\x0a\x15\x02\x09\x00\xd2\x00\xe0\x01\xe3\x01\x00\x0b"
    b"\x09\x00\xd2\x00\xe0\x01\xe3\x01\x00\x0b"
)

LIMITS = ("memory_limit", "table_limit", "continuation_limit")


def _runtime(suspendable=False, **limits):
    env = wasm3.Environment()
    rt = env.new_runtime(64 * 1024)
    rt.suspendable = suspendable
    for name, value in limits.items():
        setattr(rt, name, value)
    return env, rt


def _grow_instance(suspendable=False, **limits):
    env, rt = _runtime(suspendable, **limits)
    mod = env.parse_module(GROW_WASM)
    rt.load(mod)
    return rt, mod


def test_limits_are_off_by_default_and_usage_is_tracked():
    env, rt = _runtime()
    for name in LIMITS:
        assert getattr(rt, name) == 0
    assert rt.memory_used == rt.table_used == rt.continuation_used == 0

    rt.load(env.parse_module(GROW_WASM))
    assert rt.memory_used == PAGE
    assert rt.table_used == 1
    assert rt.find_function("grow")(4) == 1
    assert rt.memory_used == 5 * PAGE


def test_limits_round_trip():
    _, rt = _runtime()
    for name in LIMITS:
        setattr(rt, name, 1234)
        assert getattr(rt, name) == 1234
        setattr(rt, name, 0)
        assert getattr(rt, name) == 0


def test_memory_grow_stops_at_the_limit():
    rt, _ = _grow_instance(memory_limit=3 * PAGE)
    grow = rt.find_function("grow")
    assert grow(2) == 1
    assert grow(1) == -1  # over budget: fails without changing the memory
    assert rt.memory_used == 3 * PAGE
    # Lifting the cap lets it grow again
    rt.memory_limit = 0
    assert grow(1) == 3


def test_table_grow_stops_at_the_limit():
    rt, _ = _grow_instance(table_limit=4)
    grow_table = rt.find_function("grow_table")
    assert grow_table(3) == 1
    assert grow_table(1) == -1
    assert rt.table_used == 4


def test_load_over_the_limit_raises():
    env, rt = _runtime(memory_limit=2 * PAGE)
    mod = env.parse_module(BIG_WASM)
    with pytest.raises(RuntimeError, match="runtime memory limit exceeded"):
        rt.load(mod)
    # The runtime keeps the module anyway: it goes when the runtime does, not before
    with pytest.raises(RuntimeError, match="multiple runtimes"):
        rt.load(mod)
    del mod
    gc.collect()
    assert rt.memory_used == 0


def _cont_function(wasm, name, **limits):
    env, rt = _runtime(**limits)
    rt.load(env.parse_module(wasm))
    return rt, rt.find_function(name)


def test_finished_continuations_give_their_stack_back():
    rt, loop = _cont_function(CONT_LOOP_WASM, "loop", continuation_limit=1)
    assert loop() == 1
    assert rt.continuation_used == 0


def test_continuation_limit_traps_cont_new():
    rt, tree = _cont_function(CONT_TREE_WASM, "tree", continuation_limit=4)
    with pytest.raises(RuntimeError, match="continuation limit exceeded"):
        tree()
    # A trap recycles the stacks too
    assert rt.continuation_used == 0


def test_limits_count_across_modules():
    env, rt = _runtime(memory_limit=4 * PAGE)
    rt.load(env.parse_module(BIG_WASM))
    mod = env.parse_module(GROW_WASM)
    rt.load(mod)
    assert rt.memory_used == 4 * PAGE
    assert rt.find_function("grow")(1) == -1


def test_limit_below_usage_is_refused():
    rt, _ = _grow_instance()
    assert rt.find_function("grow_table")(2) == 1
    with pytest.raises(ValueError, match="below current usage"):
        rt.memory_limit = PAGE - 1
    with pytest.raises(ValueError, match="below current usage"):
        rt.table_limit = 2
    assert rt.memory_limit == rt.table_limit == 0
    # Exactly what is in use is fine
    rt.memory_limit = PAGE
    rt.table_limit = 3


def test_limits_reject_bad_values():
    _, rt = _runtime()
    for name in LIMITS:
        with pytest.raises(TypeError):
            setattr(rt, name, 1.5)
        with pytest.raises(TypeError):
            setattr(rt, name, "lots")
        with pytest.raises(OverflowError):
            setattr(rt, name, -1)
        with pytest.raises(AttributeError):
            delattr(rt, name)
        assert getattr(rt, name) == 0
    # Anything with __index__ goes
    rt.memory_limit = True
    assert rt.memory_limit == 1


def test_huge_limits_saturate():
    _, rt = _runtime()
    rt.memory_limit = 2**64 - 1
    assert rt.memory_limit == 2**64 - 1
    rt.continuation_limit = 2**64 - 1
    assert rt.continuation_limit == 2**32 - 1
    rt.gas_limit = 1e300
    assert rt.gas_limit == pytest.approx((2**63 - 1) / 10000)


def test_usage_is_read_only():
    _, rt = _runtime()
    for name in ("memory_used", "table_used", "continuation_used"):
        with pytest.raises(AttributeError):
            setattr(rt, name, 0)


def test_snapshot_restore_checks_the_limits():
    rt, _ = _grow_instance(suspendable=True)
    assert rt.find_function("grow")(2) == 1
    rt.request_suspend()
    assert rt.find_function("spin")() is None
    snapshot = rt.save_snapshot()

    # The caps are the host's, not the snapshot's: too low a one refuses the restore...
    rt2, mod2 = _grow_instance(suspendable=True, memory_limit=2 * PAGE)
    with pytest.raises(RuntimeError, match="runtime memory limit exceeded"):
        rt2.load_snapshot(mod2, snapshot)
    assert not rt2.suspended
    # ...and leaves the module as it was, to try again with a higher one
    rt2.memory_limit = 3 * PAGE
    rt2.load_snapshot(mod2, snapshot)
    assert rt2.suspended
    assert rt2.memory_used == 3 * PAGE
