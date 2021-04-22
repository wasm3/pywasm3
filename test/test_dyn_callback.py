import wasm3 as m3
import pytest

from helpers import wat2wasm

DYN_CALLBACK_WASM = wat2wasm("""
(module
  (type $t0 (func (param i32 i32) (result i32)))
  (type $t1 (func))
  (type $t2 (func (param i32)))
  (type $t3 (func (param i32 i32 i32) (result i32)))
  (import "env" "pass_fptr" (func $env.pass_fptr (type $t2)))
  (import "env" "__table_base" (global $env.__table_base i32))
  (func $run_test (export "run_test") (type $t1)
    global.get $env.__table_base
    call $env.pass_fptr
    global.get $env.__table_base
    i32.const 1
    i32.add
    call $env.pass_fptr)
  (func $f2 (type $t0) (param $p0 i32) (param $p1 i32) (result i32)
    local.get $p0
    local.get $p1
    i32.add)
  (func $f3 (type $t0) (param $p0 i32) (param $p1 i32) (result i32)
    local.get $p0
    local.get $p1
    i32.mul)
  (func $test (export "call_pass_fptr") (type $t2) (param $p0 i32)
    local.get $p0
    call $env.pass_fptr
  )
  (func $dynCall_iii (export "dynCall_iii") (type $t3) (param $p0 i32) (param $p1 i32) (param $p2 i32) (result i32)
    local.get $p1
    local.get $p2
    local.get $p0
    call_indirect $table (type $t0))
  (table $table (export "table") 2 funcref)
  (elem (global.get $env.__table_base) func $f2 $f3))
""")

def test_dynamic_callback():
    env = m3.Environment()
    rt = env.new_runtime(1024)
    mod = env.parse_module(DYN_CALLBACK_WASM)
    rt.load(mod)
    dynCall_iii = rt.find_function("dynCall_iii")

    def pass_fptr(fptr):
        if fptr == 0:
            assert dynCall_iii(fptr, 12, 34) == 46
        elif fptr == 1:
            # TODO: call by table index directly here
            assert dynCall_iii(fptr, 12, 34) == 408
        else:
            raise Exception(f"Strange function ptr: {fptr}")

    mod.link_function("env", "pass_fptr", "v(i)", pass_fptr)

    # Indirect calls
    assert dynCall_iii(0, 12, 34) == 46
    assert dynCall_iii(1, 12, 34) == 408

    # Recursive exported function call (single calls)
    call_pass_fptr = rt.find_function("call_pass_fptr")
    base = 0
    call_pass_fptr(base+0)
    call_pass_fptr(base+1)

    # Recursive exported function call (multiple calls)
    rt.find_function("run_test")()
