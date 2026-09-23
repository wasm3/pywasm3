#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pywasm3",
# ]
#
# [tool.uv.sources]
# pywasm3 = { path = "../" }
# ///

"""
Pause a long computation, save it to a file, and pick it up again in another process.

The Wasm module below looks for the number under ten million with the longest Collatz
chain (halve it if even, 3n+1 if odd, until it reaches 1). That keeps wasm3 busy for a
while, yet control comes back to Python several times a second:

  - The runtime is suspendable and has a small gas budget. Running out of gas pauses
    the call instead of trapping, and the call returns with `rt.suspended` set.
  - While it is paused, Python reads the module's globals to draw a progress bar,
    refills the budget, and calls `rt.resume()` to continue from where it stopped.
  - Press Ctrl+C and the paused call goes into a snapshot file. Run the script again:
    a new runtime loads the snapshot and resumes the search from the same point, down
    to the half-walked chain it was in the middle of and the locals holding it.

Loading one snapshot into two runtimes gives two independent copies of the same
paused call, which is how you would fork one.
"""

import os
import signal
import sys
import tempfile

import wasm3

WAT = """
(module
  ;; The search keeps its progress in exported globals, so the host can read them
  ;; while the call is paused
  (global $n        (export "n")        (mut i32) (i32.const 1))
  (global $best_n   (export "best_n")   (mut i32) (i32.const 1))
  (global $best_len (export "best_len") (mut i32) (i32.const 0))

  ;; The number of steps it takes $x to reach 1
  (func $chain_length (param $x i64) (result i32)
    (local $len i32)
    (block $done
      (loop $step
        (br_if $done (i64.eq (local.get $x) (i64.const 1)))
        (local.set $x
          (if (result i64) (i64.eqz (i64.and (local.get $x) (i64.const 1)))
            (then (i64.shr_u (local.get $x) (i64.const 1)))
            (else (i64.add (i64.mul (local.get $x) (i64.const 3)) (i64.const 1)))))
        (local.set $len (i32.add (local.get $len) (i32.const 1)))
        (br $step)))
    (local.get $len))

  ;; The number below $limit with the longest chain
  (func (export "search") (param $limit i32) (result i32)
    (local $len i32)
    (loop $next
      (local.set $len (call $chain_length (i64.extend_i32_u (global.get $n))))
      (if (i32.gt_u (local.get $len) (global.get $best_len))
        (then
          (global.set $best_len (local.get $len))
          (global.set $best_n (global.get $n))))
      (global.set $n (i32.add (global.get $n) (i32.const 1)))
      (br_if $next (i32.lt_u (global.get $n) (local.get $limit))))
    (global.get $best_n))
)
"""

LIMIT = 10_000_000
GAS_PER_SLICE = 2_000_000  # roughly a tenth of a second
SNAPSHOT = os.path.join(tempfile.gettempdir(), "pywasm3-collatz.dmp")


def instantiate():
    env = wasm3.Environment()
    rt = env.new_runtime(64 * 1024)
    # wasm3 compiles the pause points and the gas checks into the code, so both are
    # switched on before anything compiles
    rt.suspendable = True
    rt.gas_limit = GAS_PER_SLICE
    mod = env.parse_module(WAT)
    rt.load(mod)
    return rt, mod


def show_progress(mod):
    n = mod.get_global("n")
    done = n / LIMIT
    bar = "#" * int(done * 30)
    print(
        f"\r[{bar:.<30}] {done:4.0%}  n = {n:,}  longest so far: "
        f"{mod.get_global('best_n'):,} ({mod.get_global('best_len')} steps)",
        end="",
        flush=True,
    )


def on_ctrl_c(signum, frame):
    global quitting
    quitting = True


# Ctrl+C only sets a flag: the slice running now finishes, and the loop saves the
# search it paused
quitting = False
signal.signal(signal.SIGINT, on_ctrl_c)

rt, mod = instantiate()

if os.path.exists(SNAPSHOT):
    with open(SNAPSHOT, "rb") as f:
        rt.load_snapshot(mod, f.read())
    print(f"Resuming from {SNAPSHOT}")
    # The snapshot knows which call was paused, and with what arguments
    result = rt.resume()
else:
    print(f"Searching below {LIMIT:,}. Press Ctrl+C to save and quit.")
    result = rt.find_function("search")(LIMIT)

# A call that pauses returns None: rt.suspended is what says it has more to do
while rt.suspended:
    show_progress(mod)
    if quitting:
        snapshot = rt.save_snapshot()
        with open(SNAPSHOT, "wb") as f:
            f.write(snapshot)
        print(f"\nSaved {len(snapshot)} bytes to {SNAPSHOT}. Run again to continue.")
        sys.exit(0)
    rt.gas_limit = GAS_PER_SLICE  # a fresh budget for the next slice
    result = rt.resume()

show_progress(mod)
print(f"\nThe longest chain below {LIMIT:,} starts at {result:,}: {mod.get_global('best_len')} steps")

if os.path.exists(SNAPSHOT):
    os.remove(SNAPSHOT)
