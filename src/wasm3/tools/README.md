# Bundled WABT tools

`wat2wasm.wasm` and `wasm2wat.wasm` are [WABT](https://github.com/WebAssembly/wabt)
1.0.41, built for `wasm32-wasip1`, also shipped in wasm3's own test suite.

`wasm3.wabt` runs them on wasm3 itself, over an in-memory filesystem (`wasm3._wasi`), so
the text format works without a toolchain installed. Refresh them from the submodule when
it updates - they are plain WASI command-line programs, and nothing here depends on
anything but their argv, stdin/stdout and the files they open.

WABT is released under the Apache License 2.0, a copy of which is in `LICENSE.wabt`.
