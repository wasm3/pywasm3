# Text format

pywasm3 bundles `wat2wasm` and `wasm2wat` tools from [wabt](https://github.com/WebAssembly/wabt):

```py
wasm = wasm3.wat2wasm('(module (func (export "f") (result i32) i32.const 42))')
print(wasm3.wasm2wat(wasm))         # back to text
```

`Environment.parse_module()` takes the text format directly too, as a `str` or as the
bytes of a `.wat` file.

The optional wasm features wabt leaves off are enabled by default
(`wasm3.wabt.FEATURE_ARGS`); pass extra flags with `args=[...]`, or use
`wasm3.wabt.run()` to drive a bundled tool exactly as a command line would.
