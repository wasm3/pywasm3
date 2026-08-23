import gc
import sys

from helpers import wat2wasm

import wasm3 as m3

WASM = wat2wasm("""
(module
  (func $fib2 (param $n i64) (param $a i64) (param $b i64) (result i64)
    (if (result i64)
        (i64.eqz (local.get $n))
        (then (local.get $a))
        (else (return_call $fib2 (i64.sub (local.get $n)
                                   (i64.const 1))
                          (local.get $b)
                          (i64.add (local.get $a)
                                   (local.get $b))))))

  (func $fib (export "fib") (param i64) (result i64)
    (return_call $fib2 (local.get 0)
                (i64.const 0)
                (i64.const 1)))
)
""")


def collect():
    gc.collect()
    gc.collect()


def test_runtime_releases_environment_reference():
    env = m3.Environment()
    before = sys.getrefcount(env)

    rt = env.new_runtime(2048)
    assert sys.getrefcount(env) == before + 1

    del rt
    collect()
    assert sys.getrefcount(env) == before


def test_unloaded_module_releases_environment_and_wasm_bytes():
    env = m3.Environment()
    env_refs = sys.getrefcount(env)
    bytes_refs = sys.getrefcount(WASM)

    mod = env.parse_module(WASM)
    assert sys.getrefcount(env) == env_refs + 1
    assert sys.getrefcount(WASM) == bytes_refs + 1

    del mod
    collect()
    assert sys.getrefcount(env) == env_refs
    assert sys.getrefcount(WASM) == bytes_refs


def test_loaded_module_bytes_live_until_runtime_is_released():
    env = m3.Environment()
    rt = env.new_runtime(2048)
    mod = env.parse_module(WASM)
    bytes_refs = sys.getrefcount(WASM)

    rt.load(mod)
    assert sys.getrefcount(WASM) == bytes_refs + 1

    del mod
    collect()
    assert sys.getrefcount(WASM) == bytes_refs

    del rt
    collect()
    assert sys.getrefcount(WASM) == bytes_refs - 1


def test_function_releases_runtime_reference():
    env = m3.Environment()
    rt = env.new_runtime(2048)
    mod = env.parse_module(WASM)
    rt.load(mod)
    runtime_refs = sys.getrefcount(rt)

    fib = rt.find_function("fib")
    assert fib(10) == 55
    assert sys.getrefcount(rt) == runtime_refs + 1

    del fib
    collect()
    assert sys.getrefcount(rt) == runtime_refs
