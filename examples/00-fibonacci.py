#!/usr/bin/env python3

import wasm3
import base64, time, timeit

"""
Input module:
  (module
    (type (;0;) (func (param i64) (result i64)))
    (func (;0;) (type 0) (param i64) (result i64)
      local.get 0
      i64.const 2
      i64.lt_u
      if  ;; label = @1
        local.get 0
        return
      end
      local.get 0
      i64.const 2
      i64.sub
      call 0
      local.get 0
      i64.const 1
      i64.sub
      call 0
      i64.add
      return)
    (export "fib" (func 0)))
"""

# WebAssembly binary
WASM = base64.b64decode("""
    AGFzbQEAAAABBgFgAX4BfgMCAQAHBwEDZmliAAAKHwEdACAAQgJUBEAgAA8LIABCAn0QACAAQgF9
    EAB8Dws=
""")

(N, RES, CYCLES) = (24, 46368, 1000)

# Note: this is cold-start
def run_wasm():
    env = wasm3.Environment()
    rt  = env.new_runtime(4096)
    mod = env.parse_module(WASM)
    rt.load(mod)
    wasm_fib = rt.find_function("fib")
    assert wasm_fib(N) == RES

def fib(n: int) -> int:
    if n < 2:
        return n
    return fib(n-1) + fib(n-2)

def run_py():
    assert fib(N) == RES

t1 = timeit.timeit(run_wasm, number=CYCLES)
print(f"Wasm3:  {t1:.4f} seconds")
print("Cooling down... ", end="", flush=True)
time.sleep(10)
print("ok")
t2 = timeit.timeit(run_py, number=CYCLES)
if t2 > t1:
    ratio = f"{(t2/t1):.1f}x slower"
else:
    retio = f"{(t1/t2):.1f}x faster"
print(f"Python: {t2:.4f} seconds, {ratio}")

