"""Smoke tests on a pre-assembled module - the only ones that run without wabt."""

import base64

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
