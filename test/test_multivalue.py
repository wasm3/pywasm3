import wasm3 as m3
import pytest

from helpers import wat2wasm

MV_SWAP_WASM = wat2wasm("""
(module
  (func (export "swap") (param i32 i32) (result i32 i32)
    (get_local 1)
    (get_local 0)
  )
)
""")

MV_IMPORT_WASM = wat2wasm("""
(module
  (type $t0 (func (param i32 i64) (result i64 i32)))
  (import "env" "swap" (func $env.swap (type $t0)))
  (func (export "swap") (type $t0)
    (get_local 0)
    (get_local 1)
    (call $env.swap)
  )
)
""")

def test_multivalue_swap():
    env = m3.Environment()
    rt = env.new_runtime(64)
    mod = env.parse_module(MV_SWAP_WASM)
    rt.load(mod)
    swap = rt.find_function('swap')
    assert swap(1, 2) == (2, 1)
    assert swap(2, 1) == (1, 2)
    assert swap.call_argv('1', '2') == (2, 1)
    assert swap.call_argv('2', '1') == (1, 2)

def test_multivalue_imported():
    env = m3.Environment()
    rt = env.new_runtime(64)
    mod = env.parse_module(MV_IMPORT_WASM)
    rt.load(mod)
    mod.link_function("env", "swap", "Ii(iI)", lambda a,b: (b,a))
    swap = rt.find_function('swap')
    assert swap(1, 2) == (2, 1)
    assert swap(2, 1) == (1, 2)
