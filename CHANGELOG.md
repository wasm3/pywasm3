# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Added

- The text format works out of the box: `wasm3.wat2wasm()`, `wasm3.wasm2wat()`, and
  `Environment.parse_module()` accepting WAT directly. The package bundles wabt's
  `wat2wasm`/`wasm2wat` as wasm and runs them on wasm3 itself, so there is no toolchain
  to install and no subprocess to spawn - and the tests need neither.
- Suspendable execution and snapshots (wasm3/wasm3#268): `Runtime.suspendable`,
  `request_suspend()`, `suspended`, `resume()`, `save_snapshot()` and `load_snapshot()`.
  A call can pause at a loop back edge or function entry - on request, or when its gas
  runs out - return to Python, and continue later, in the same runtime or from a
  snapshot in a new one. See `examples/04-suspend-resume.py`.

### Changed

- wasm3 is now the `external/wasm3` submodule instead of a vendored copy.
- The package is `src/wasm3/`, ships `py.typed` and type stubs, and builds with
  `pyproject.toml` + `uv`, versioned from git tags.
- Wheels use the stable ABI (`cp311-abi3`): one wheel per platform covers CPython 3.11
  and newer. Minimum supported Python is now 3.11. CI builds for Linux, Windows, macOS
  and Android.
- Every script in `examples/` declares its dependencies inline (PEP 723), so
  `uv run examples/<name>.py` just works. The three `pygame-audio*` scripts are now one
  script with a playlist.

### Fixed

- Python callables linked as imports were leaked, and could be freed while the module
  could still call them.
- Every call into a Python import leaked its argument tuple and its return value.
- An exception raised by an import surfaced later, at some unrelated call.
- A parse error on a module raised from a NULL message instead of reporting the error.
- `Runtime.get_memory()` leaked a `Py_buffer` on every call.
- Argument/result buffers were `static`, making calls non-reentrant and racy on
  free-threaded builds.
- A `memset()` cleared a pointer's worth of a 32-slot argument array.
- `Environment` can now be subclassed from Python.
- The free-threaded (`cp314t`) wheels turned the GIL back on at import (wasm3/wasm3#585).
  The extension now declares `Py_mod_gil`, and locks for itself instead: runtimes in
  separate `Environment`s run in parallel, while those sharing one take turns, since
  compiling - which a call does too - writes to the environment. `Runtime.request_suspend()`
  takes no lock, so it can still interrupt a call running in another thread. Loading a
  module into a runtime of another `Environment` now raises `RuntimeError`.

[Unreleased]: https://github.com/wasm3/pywasm3/commits/main
