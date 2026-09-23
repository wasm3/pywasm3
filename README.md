[![SWUbanner](https://raw.githubusercontent.com/vshymanskyy/StandWithUkraine/main/banner-direct.svg)](https://github.com/vshymanskyy/StandWithUkraine/blob/main/docs/README.md)

# pywasm3

[![license]][license-url]
[![pypi version]][PyPiUrl]
[![python versions]][PyPiUrl]
[![Pre-commit]][pre-commit-workflow]
[![Publish]][publish-workflow]
[![coverage]][CodecovUrl]

Python bindings for Wasm3, a fast WebAssembly interpreter and the most universal WASM runtime.

Main repository: [**Wasm3 project**](https://github.com/wasm3/wasm3)

Requires CPython 3.11+. Wheels are `cp311-abi3`, so one wheel per platform covers 3.11 and newer.

## Install

```sh
pip install pywasm3
pip install "pywasm3 @ git+https://github.com/wasm3/pywasm3"   # bleeding edge
pip install .                                                  # local copy
```

With [`uv`](https://docs.astral.sh/uv/):

```sh
uv add pywasm3
uv add "pywasm3 @ git+https://github.com/wasm3/pywasm3"        # bleeding edge
uv run --with pywasm3 python my_script.py                      # without a project
```

## Usage example

```py
import wasm3

WAT = """
(module
  (func $fib (export "fib") (param $n i64) (result i64)
    (if (i64.lt_u (local.get $n) (i64.const 2))
      (then (return (local.get $n))))
    (return (i64.add (call $fib (i64.sub (local.get $n) (i64.const 2)))
                     (call $fib (i64.sub (local.get $n) (i64.const 1))))))
)
"""

env = wasm3.Environment()
rt  = env.new_runtime(2048)
mod = env.parse_module(WAT)          # or a binary module, as bytes
rt.load(mod)
wasm_fib = rt.find_function("fib")
result = wasm_fib(24)
print(result)                       # 46368
```

## Examples

Every script in [`examples/`](examples) carries its dependencies inline (PEP 723), so
`uv run` sets up an environment for it on the fly — nothing to install first:

```sh
uv run examples/00-fibonacci.py     # wasm3 vs. pure Python fib(24)
uv run examples/01-coremark.py      # CoreMark benchmark
uv run examples/02-metered.py       # gas metering
uv run examples/03-asyncified.py    # asyncified module driven by asyncio
uv run examples/04-suspend-resume.py # pause, snapshot, Ctrl+C, resume in a new process
uv run examples/pygame-doomfire.py  # one of the pygame demos
```

The scripts resolve `pywasm3` from this checkout, so they build the extension you have
locally. Add `--no-sources` to run them against the released package instead:

```sh
uv run --no-sources examples/00-fibonacci.py
```

## Documentation

- [Text format](https://github.com/wasm3/pywasm3/blob/main/docs/text-format.md) - the bundled `wat2wasm` / `wasm2wat`
- [Suspend, resume and snapshots](https://github.com/wasm3/pywasm3/blob/main/docs/suspend-resume.md) - pause a call, save it, pick it up in another process
- [Development](https://github.com/wasm3/pywasm3/blob/main/docs/development.md) - building from source, tests, releases

### License
This project is released under The MIT License (MIT)

<!-- REUSABLE LINKS -->

[license]:
https://img.shields.io/github/license/wasm3/pywasm3

[license-url]:
https://opensource.org/licenses/MIT

[pypi version]:
https://img.shields.io/pypi/v/pywasm3?logo=pypi

[python versions]:
https://img.shields.io/pypi/pyversions/pywasm3?logo=python

[PyPiUrl]:
https://pypi.org/project/pywasm3/

[Pre-commit]:
https://github.com/wasm3/pywasm3/actions/workflows/pre-commit.yml/badge.svg

[pre-commit-workflow]:
https://github.com/wasm3/pywasm3/actions/workflows/pre-commit.yml

[Publish]:
https://github.com/wasm3/pywasm3/actions/workflows/publish.yml/badge.svg

[publish-workflow]:
https://github.com/wasm3/pywasm3/actions/workflows/publish.yml

[coverage]:
https://codecov.io/gh/wasm3/pywasm3/graph/badge.svg

[CodecovUrl]:
https://codecov.io/gh/wasm3/pywasm3
