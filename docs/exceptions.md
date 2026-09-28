# Exceptions

Modules can use the exception handling proposal - `tag`, `throw`, `throw_ref`,
`try_table` - and exceptions cross between the guest and Python in both directions.

## Out of the guest

An exception nothing in the guest catches raises `wasm3.WasmException`, which carries the
tag it was thrown with and its payload:

```py
mod = env.parse_module("""
(module
  (tag $overflow (export "overflow") (param i32 i64))
  (func (export "run") (param i32)
    (throw $overflow (local.get 0) (i64.const 1000))))
""")
rt.load(mod)

try:
    rt.find_function("run")(7)
except wasm3.WasmException as e:
    assert e.tag == mod.get_tag("overflow")
    print(e.payload)                    # (7, 1000)
```

`WasmException` is a `RuntimeError`, which is what an uncaught exception raised before.

## Into the guest

An import throws into the guest by raising `WasmException(tag, *payload)`, and a
`catch` naming the tag gets the payload. The tag can be one the module exports
(`mod.get_tag(name)`), or one of Python's own, made with `rt.new_tag(signature)` - the
payload typed as in `link_function` signatures - and linked to a tag the module imports:

```py
mod = env.parse_module("""
(module
  (import "env" "parse" (func $parse (param i32) (result i32)))
  (import "env" "bad_input" (tag $bad (param i32)))
  (func (export "safe_parse") (param i32) (result i32)
    (block $h (result i32)
      (try_table (result i32) (catch $bad $h)
        (call $parse (local.get 0)))
      (return))
    (i32.mul (i32.const -1))))              ;; what it was handed, negated
""")
rt.load(mod)
bad_input = rt.new_tag("v(i)")
mod.link_tag("env", "bad_input", bad_input)     # before find_function()

def parse(value):
    if value > 100:
        raise wasm3.WasmException(bad_input, value)
    return value

mod.link_function("env", "parse", "i(i)", parse)
rt.find_function("safe_parse")(5)       # 5
rt.find_function("safe_parse")(500)     # -500: the guest caught it
```

Code refers to a tag as whatever it was linked to when it compiled, so link tags
before `find_function()`, like `gas_limit`. Tags are compared by identity: two `Tag`
objects are equal when they name the same tag.

## Python exceptions

Any other `Exception` an import raises crosses the guest as a Wasm exception too, with a
tag of the runtime's own that no module can name. `catch_all` and `catch_all_ref`
catch it, `throw_ref` sends it on, and if it leaves the guest it is raised again as the
very same object, traceback and all. The same goes for an import whose results can't be
converted, which raises `TypeError`.

A `BaseException` that isn't an `Exception` - `KeyboardInterrupt`, `SystemExit` - is a
trap instead, and goes straight through: no `catch_all` gets to hold on to it.

## Limits

- An `exnref` can't cross into Python: a function or import that takes or returns one
  raises `TypeError`.
- `ref.null exn` doesn't compile yet: wasm3 understands the `exnref` shorthand, but not
  the `exn` heap type that instruction names.
- An exception is released when the outermost call ends, as wasm3 does with every
  exception; an `exnref` parked in a global or a table past that point dangles.
- A snapshot refuses a live exception whose tag the module can't name, such as one an
  import raised.
