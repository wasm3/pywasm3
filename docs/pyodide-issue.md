# Pyodide (wasm32) support: not currently possible

pywasm3 does not build or publish a Pyodide (`wasm32-emscripten`) wheel. This was tried
and reverted; this document is the record of what was found, in case someone picks it up
later. There is no `pyodide` entry in `[tool.cibuildwheel]`, no Emscripten-specific code
in `setup.py`, and no in-browser demo - all of that was removed when this was shelved.

## Symptom

The extension builds and links against Emscripten (via `pyodide-build`/cibuildwheel's
`pyodide` platform) without errors and packages into a valid wheel. But the first call
into the compiled module - as little as constructing a `wasm3.Environment()` - crashes
the whole Pyodide runtime, not just the Python call:

```
Pyodide has suffered a fatal error. Please report this to the Pyodide maintainers.
The cause of the fatal error was:
RuntimeError: null function or function signature mismatch
    at wasm://wasm/...:wasm-function[2673]:0x1dad60
    ...
  pyodide_fatal_error: true
```

This is a WebAssembly `call_indirect` trap (wrong or missing function type in the
indirect-call table), not a Python exception - it cannot be caught, and it takes down the
entire Pyodide session.

## What it isn't

wasm3's interpreter dispatch is implemented as "threaded code": `m3_compile.c` contains
large tables of function pointers (the various `op_*` handlers) computed at compile time,
which `m3_exec.c`'s bytecode loop later reads back and calls indirectly. The initial
suspicion was wasm3's `musttail`-attributed dispatch, which (as of upstream commit
`3211777`) compiles unconditionally to a WebAssembly `return_call` - a different
indirect-call-adjacent instruction that needs the `tail-call` target feature explicitly
enabled on this target, or clang refuses to emit it at all
(`WebAssembly 'tail-call' feature not enabled`).

That turned out to be a red herring. Both of these were tried and produced the *exact
same crash*, at the same wasm-function offsets:

- `-mtail-call` (enables the target feature, lets clang emit real tail calls): builds,
  crashes identically.
- `-DM3_HAS_TAIL_CALL=0` (wasm3's own documented fallback - plain, non-tail-called
  dispatch, no `return_call` anywhere): also builds, also crashes identically.

Since disabling `musttail` entirely didn't change the crash at all, tail-call handling
was never the cause.

## Bisection

Reproduced locally (not just from CI logs) using `pyodide-build` + `pyodide venv`
(a real, node-backed Pyodide environment) and narrowed it down with four minimal
extensions, each built and run the same way:

| Extension | Result |
|---|---|
| `PyType_FromSpec` + a `Py_tp_new` slot, no wasm3 | works |
| A plain function calling `m3_NewEnvironment()`/`m3_FreeEnvironment()`, no `Py_tp_new` | works |
| `PyType_FromSpec` + `Py_tp_new` whose body calls `m3_NewEnvironment()` (mirrors `_wasm3.c`'s real `newEnvironment()`, hand-written minimal version) | works |
| The real `_wasm3.c`, copied verbatim, compiled with the full `external/wasm3/source/*.c` and built as its own standalone module (not as part of the `wasm3` package) | **crashes identically** |

So it is not:
- tail-call handling (ruled out above),
- a general incompatibility between `PyType_FromSpec` heap types and Pyodide's
  dlopen-based dynamic linking (the minimal repro using the same pattern works fine),
- "a wasm3 call made from inside a `Py_tp_new` slot" as a category (also works fine in
  isolation).

Only the real extension - four heap types, the `CallImport` trampoline, the full op
dispatch tables, everything - reproduces it, and does so standalone, without needing the
rest of the `wasm3` Python package around it.

## Working theory

This points to a size/shape-dependent bug in Emscripten's dynamic linking: when a dlopen
side module takes the address of enough functions and stores them as data (exactly
wasm3's `op_*` dispatch table pattern), the linker's indirect-function-table relocation
can compute a wrong index, so a later indirect call lands on the wrong (or no) table
entry. This is a known class of upstream issue, not unique to wasm3:

- <https://github.com/emscripten-core/emscripten/issues/13026> - "Random function
  signature mismatch with dynamic libraries"
- <https://github.com/emscripten-core/emscripten/issues/13241> - similar, with a minimal
  repro involving function pointers across a dynamically-linked side module
- <https://github.com/emscripten-core/emscripten/issues/9901> - the general
  "function pointer identity/table index" problem under dynamic linking

None of the fixes suggested in those threads (e.g. `-sMAIN_MODULE=2`) apply here: they
address the *main* module's link command, which for a Pyodide wheel is CPython's own
build - not something this project controls from `setup.py`.

## Environment notes for anyone reproducing this

Reproducing needs `pyodide-build` (`pip install pyodide-build`, requires Python >=3.12)
plus an installed xbuildenv and Emscripten SDK matching the target CPython build (this
was done against `pyodide-build==0.39.0`, xbuildenv `0.29.4`, Emscripten `4.0.9` - the
same versions cibuildwheel's `pyodide` platform picked in CI at the time):

```sh
pip install "pyodide-build==0.39.0"
pyodide xbuildenv install --path <cache> 0.29.4
pyodide xbuildenv install-emscripten --version 4.0.9 --path <cache>
pyodide build .                          # builds the wheel
pyodide venv <venv-dir>                  # a node-backed Pyodide virtualenv
<venv-dir>/bin/pip install dist/*.whl
<venv-dir>/bin/python -c "import wasm3; wasm3.Environment()"   # reproduces the crash
```

`pyodide.github.io` (used by `pyodide xbuildenv install`'s metadata lookup) and
`github.com` (release asset downloads) both need to be reachable.

## If picking this up again

Worth trying, roughly in order of how likely they seem to actually help:

1. File an issue upstream (wasm3 and/or emscripten-core) with the minimal repro above -
   nothing here points to a fix controllable from a `setup.py` alone.
2. Try a newer Emscripten/`pyodide-build` release in case the underlying linker bug has
   since been fixed - this was last tried against Emscripten 4.0.9.
3. Investigate whether restructuring wasm3's dispatch to avoid large function-pointer
   tables as static data (a real architecture change, not a flag) sidesteps the pattern
   that triggers it - this would be a significant, wasm3-upstream-level change, not
   something to attempt only in this bindings package.
