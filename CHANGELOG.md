# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Changed

- wasm3 is now the `external/wasm3` git submodule instead of a vendored copy of its
  sources.
- `src/` layout: the extension is `wasm3._wasm3`, re-exported by the `wasm3` package,
  which now ships `py.typed` and type stubs.
- Build moved to `pyproject.toml` + `uv`, with the version derived from git tags by
  setuptools-scm.
- Wheels are built against the stable ABI (`cp311-abi3`), so one wheel per platform
  covers CPython 3.11 and newer. Minimum supported Python is now 3.11.
- Compiler flags are selected per compiler; MSVC builds previously received none.
- CI builds wheels for Linux (x86_64/i686/aarch64/armv7l), Windows (x64/x86/ARM64),
  macOS (arm64/x86_64) and Android via cibuildwheel.
- Every script in `examples/` declares its dependencies inline (PEP 723), so
  `uv run examples/<name>.py` runs it without any manual setup.
- The audio examples depend on `pygame-ce` instead of `pygame`: it ships wheels with
  `pygame.mixer` for current CPython versions, while a source build of `pygame` silently
  drops the mixer when SDL2_mixer is missing.

### Fixed

- `Runtime.get_memory()` leaked a `Py_buffer` on every call.
- Argument/result scratch buffers were `static`, making calls non-reentrant and racy on
  free-threaded builds.
- A `memset()` cleared a pointer's worth of a 32-slot argument array.
- The audio examples hung instead of exiting when their player subprocess could not
  start (no mixer support or no audio device): nothing drained the queue, so the feeder
  thread blocked at exit and even Ctrl+C could not end the process. They now report why
  audio is unavailable and stop, and Ctrl+C no longer waits for buffered audio.
- `examples/pygame-audio2.py` played silence: `music.wasm` allocates its sample buffer
  during the first render call, so the `samplebuffer` pointer was read while it was
  still 0 and every chunk came from address 0. It is now read after rendering.

[Unreleased]: https://github.com/wasm3/pywasm3/commits/main
