"""Using wasm3 from several threads at once, which free-threaded builds (PEP 703) do for
real: the extension declares it runs without the GIL, and takes each Environment's lock
for anything that reaches into wasm3 instead."""

import os
import subprocess
import sys
import sysconfig
import threading

import pytest
from helpers import wat2wasm

import wasm3

FREE_THREADED = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))
THREADS = 8

# Several functions of several types, so that the calls compile as they go and write to
# the environment's type table and code pages while other threads do the same
WORK_WASM = wat2wasm("""
(module
  (import "env" "check" (func $check (param i32 i32)))
  (memory (export "memory") 1)

  (func $fib (param $n i32) (result i32)
    (if (result i32) (i32.lt_u (local.get $n) (i32.const 2))
      (then (local.get $n))
      (else (i32.add (call $fib (i32.sub (local.get $n) (i32.const 1)))
                     (call $fib (i32.sub (local.get $n) (i32.const 2)))))))

  (func $wide (param $a i64) (param $b f64) (result i64)
    (i64.add (local.get $a) (i64.trunc_f64_s (local.get $b))))

  ;; Leaves fib(n) in memory at `slot`, has the host check it there, and returns it
  (func (export "work") (param $slot i32) (param $n i32) (result i32)
    (local $r i32)
    (local.set $r (i32.wrap_i64 (call $wide (i64.extend_i32_u (call $fib (local.get $n))) (f64.const 0))))
    (i32.store (i32.mul (local.get $slot) (i32.const 4)) (local.get $r))
    (call $check (local.get $slot) (local.get $r))
    (local.get $r))
)
""")

FIB = [0, 1]
while len(FIB) < 30:
    FIB.append(FIB[-1] + FIB[-2])


def _instance(env):
    rt = env.new_runtime(64 * 1024)
    mod = env.parse_module(WORK_WASM)
    rt.load(mod)
    mem = mod.get_memory()
    errors = []

    # Runs inside a call, on the thread that holds the lock: reading the guest's memory
    # from here has to take it again rather than wait for it
    def check(slot, value):
        got = int.from_bytes(mem[slot * 4 : slot * 4 + 4], "little")
        if got != value:
            errors.append((slot, got, value))

    mod.link_function("env", "check", "v(ii)", check)
    return rt.find_function("work"), errors


def _run_threads(target):
    failures = []
    barrier = threading.Barrier(THREADS)

    def run(i):
        try:
            barrier.wait()
            target(i)
        except BaseException as e:  # reported below, from the test's own thread
            failures.append(e)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if failures:
        raise failures[0]


@pytest.mark.skipif(not FREE_THREADED, reason="only a free-threaded build can run without the GIL")
def test_import_keeps_the_gil_disabled():
    env = {k: v for k, v in os.environ.items() if k != "PYTHON_GIL"}
    out = subprocess.run(
        [sys.executable, "-W", "error::RuntimeWarning", "-c", "import sys, wasm3; print(sys._is_gil_enabled())"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert out.stdout.strip() == "False"


def test_separate_environments():
    def target(i):
        work, errors = _instance(wasm3.Environment())
        for n in range(20):
            assert work(n, n) == FIB[n]
        assert errors == []

    _run_threads(target)


def test_shared_environment():
    env = wasm3.Environment()

    # Parses, loads and compiles into the same environment from every thread at once
    def target(i):
        work, errors = _instance(env)
        for n in range(20):
            assert work(n, n) == FIB[n]
        assert errors == []

    _run_threads(target)


def test_shared_runtime():
    work, errors = _instance(wasm3.Environment())

    # Each thread keeps to slots of its own, so any mix-up shows in check()
    def target(i):
        for n in range(20):
            assert work(i * 20 + n, n) == FIB[n]

    _run_threads(target)
    assert errors == []


def test_request_suspend_from_another_thread():
    env = wasm3.Environment()
    rt = env.new_runtime(64 * 1024)
    rt.suspendable = True
    mod = env.parse_module("""
    (module
      (import "env" "tick" (func $tick))
      (func (export "forever")
        (loop $next (call $tick) (br $next))))
    """)
    rt.load(mod)
    ticking = threading.Event()
    mod.link_function("env", "tick", "v()", ticking.set)
    forever = rt.find_function("forever")

    results = []
    # A daemon: should the request go unanswered, the call never ends
    worker = threading.Thread(target=lambda: results.append(forever()), daemon=True)
    worker.start()
    assert ticking.wait(10)
    # Made while the call holds the environment's lock, so it must not wait for it
    rt.request_suspend()
    worker.join(10)
    assert not worker.is_alive()
    assert results == [None]
    assert rt.suspended


def test_load_into_another_environments_runtime():
    rt = wasm3.Environment().new_runtime(64 * 1024)
    mod = wasm3.Environment().parse_module(WORK_WASM)
    with pytest.raises(RuntimeError, match="different environment"):
        rt.load(mod)
