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
    # TODO
    pass
