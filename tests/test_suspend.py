"""Suspendable execution and snapshots: Runtime.suspendable, request_suspend(), resume(),
save_snapshot() and load_snapshot()."""

import pytest
from helpers import wat2wasm

import wasm3

# Keeps its loop counter in a global, so a test can see how far a paused call got, and
# its running total in a local, which only a snapshot that restores the frame gets back.
SUM_WASM = wat2wasm("""
(module
  (import "env" "tick" (func $tick))
  (memory (export "memory") 1)
  (global $i (export "i") (mut i32) (i32.const 0))

  ;; 0 + 1 + ... + (n - 1), with the last value added stored at address 0
  (func (export "sum") (param $n i32) (result i64)
    (local $acc i64)
    (block $done
      (loop $next
        (br_if $done (i32.ge_u (global.get $i) (local.get $n)))
        (local.set $acc (i64.add (local.get $acc) (i64.extend_i32_u (global.get $i))))
        (i32.store (i32.const 0) (global.get $i))
        (global.set $i (i32.add (global.get $i) (i32.const 1)))
        (br $next)))
    (local.get $acc))

  ;; Calls the host once per iteration
  (func (export "ticks") (param $n i32) (result i32)
    (loop $next
      (call $tick)
      (global.set $i (i32.add (global.get $i) (i32.const 1)))
      (br_if $next (i32.lt_u (global.get $i) (local.get $n))))
    (global.get $i))

  (func (export "peek") (result i32) (global.get $i))
)
""")

N = 100_000
TOTAL = N * (N - 1) // 2
SLICE = 500  # gas: a run of sum(N) takes about 30 of these


def _instance(gas_limit=None, tick=lambda: None, suspendable=True):
    env = wasm3.Environment()
    rt = env.new_runtime(64 * 1024)
    rt.suspendable = suspendable
    # Both are compiled into the code, so they go in before anything compiles
    if gas_limit is not None:
        rt.gas_limit = gas_limit
    mod = env.parse_module(SUM_WASM)
    rt.load(mod)
    mod.link_function("env", "tick", "v()", tick)
    return rt, mod


def _run_in_slices(rt, gas, first_call):
    """Runs a call to the end in gas-sized slices; returns (result, slices)."""
    result = first_call()
    slices = 1
    while rt.suspended:
        rt.gas_limit = gas
        result = rt.resume()
        slices += 1
    return result, slices


def test_suspendable_is_off_by_default():
    rt, mod = _instance(suspendable=False)
    assert rt.suspendable is False
    assert rt.suspended is False
    # A pause asked of a runtime that cannot pause is never answered
    rt.request_suspend()
    assert rt.find_function("sum")(10) == 45
    assert rt.suspended is False


def test_suspendable_attribute():
    rt, _ = _instance()
    assert rt.suspendable is True
    rt.suspendable = False
    assert rt.suspendable is False
    with pytest.raises(AttributeError):
        del rt.suspendable


def test_request_suspend_pauses_at_entry():
    rt, mod = _instance()
    sum_ = rt.find_function("sum")
    rt.request_suspend()
    assert sum_(N) is None
    assert rt.suspended
    assert mod.get_global("i") == 0
    # Nothing asks again, so it runs to the end
    assert rt.resume() == TOTAL
    assert not rt.suspended
    assert mod.get_global("i") == N


def test_step_one_pause_point_at_a_time():
    rt, mod = _instance()
    rt.request_suspend()
    assert rt.find_function("sum")(N) is None
    seen = []
    for _ in range(5):
        rt.request_suspend()
        assert rt.resume() is None
        seen.append(mod.get_global("i"))
    # Each resume goes past the point it paused at and stops at the next back edge
    assert seen == [seen[0] + k for k in range(5)]
    assert rt.resume() == TOTAL


def test_resume_with_nothing_paused_raises():
    rt, _ = _instance()
    with pytest.raises(RuntimeError, match="nothing to resume"):
        rt.resume()
    assert rt.find_function("sum")(10) == 45
    with pytest.raises(RuntimeError, match="nothing to resume"):
        rt.resume()


def test_running_out_of_gas_pauses():
    rt, mod = _instance(gas_limit=SLICE)
    sum_ = rt.find_function("sum")
    result, slices = _run_in_slices(rt, SLICE, lambda: sum_(N))
    assert result == TOTAL
    assert slices > 10


def test_out_of_gas_still_traps_when_not_suspendable():
    rt, _ = _instance(gas_limit=SLICE, suspendable=False)
    with pytest.raises(RuntimeError, match="out of gas"):
        rt.find_function("sum")(N)


def test_host_calls_while_paused():
    rt, mod = _instance()
    rt.request_suspend()
    rt.find_function("sum")(N)
    rt.request_suspend()
    rt.resume()
    where = mod.get_global("i")
    # A call made while another is paused runs on its own, and leaves it be
    assert rt.find_function("peek")() == where
    assert rt.suspended
    assert rt.resume() == TOTAL


def test_import_can_request_a_pause():
    ticks = 0

    def tick():
        nonlocal ticks
        ticks += 1
        rt.request_suspend()

    rt, mod = _instance(tick=tick)
    result = rt.find_function("ticks")(5)
    pauses = 0
    while rt.suspended:
        pauses += 1
        assert mod.get_global("i") == pauses
        result = rt.resume()
    assert result == 5
    assert ticks == 5
    # The last tick's request finds no back edge left to pause at...
    assert pauses == 4
    # ...so it is still pending, and the next call answers it at its entry
    assert rt.find_function("peek")() is None
    assert rt.suspended


def test_snapshot_restores_into_a_new_runtime():
    rt, mod = _instance(gas_limit=SLICE)
    assert rt.find_function("sum")(N) is None
    rt.gas_limit = SLICE
    rt.resume()
    snapshot = rt.save_snapshot()
    assert isinstance(snapshot, bytes)
    where = mod.get_global("i")
    assert 0 < where < N

    rt2, mod2 = _instance(gas_limit=SLICE)
    rt2.load_snapshot(mod2, snapshot)
    assert rt2.suspended
    assert mod2.get_global("i") == where
    assert mod2.get_memory()[0:4] == mod.get_memory()[0:4]
    # The half-built total in the paused frame's local comes back too
    result, _ = _run_in_slices(rt2, SLICE, rt2.resume)
    assert result == TOTAL


def test_snapshot_forks():
    rt, _ = _instance(gas_limit=SLICE)
    assert rt.find_function("sum")(N) is None
    snapshot = rt.save_snapshot()

    forks = [_instance(gas_limit=SLICE) for _ in range(2)]
    for fork_rt, fork_mod in forks:
        fork_rt.load_snapshot(fork_mod, bytearray(snapshot))  # any bytes-like object

    # Running one to the end leaves the other where the snapshot was taken
    (rt_a, mod_a), (rt_b, mod_b) = forks
    assert _run_in_slices(rt_a, SLICE, rt_a.resume)[0] == TOTAL
    assert rt_b.suspended
    assert mod_b.get_global("i") < mod_a.get_global("i") == N
    assert _run_in_slices(rt_b, SLICE, rt_b.resume)[0] == TOTAL
    # And the original never moved
    assert rt.suspended
    assert _run_in_slices(rt, SLICE, rt.resume)[0] == TOTAL


def test_load_snapshot_checks_its_module():
    rt, _ = _instance()
    rt.request_suspend()
    rt.find_function("sum")(N)
    snapshot = rt.save_snapshot()

    rt2, _ = _instance()
    _, other_mod = _instance()
    with pytest.raises(RuntimeError, match="not loaded into this runtime"):
        rt2.load_snapshot(other_mod, snapshot)
    with pytest.raises(TypeError):
        rt2.load_snapshot("module", snapshot)  # pyright: ignore[reportArgumentType]


def test_load_snapshot_rejects_garbage():
    rt, mod = _instance()
    with pytest.raises(RuntimeError):
        rt.load_snapshot(mod, b"not a snapshot")
    assert not rt.suspended


def test_save_snapshot_with_nothing_run_raises():
    rt, _ = _instance()
    with pytest.raises(RuntimeError):
        rt.save_snapshot()
