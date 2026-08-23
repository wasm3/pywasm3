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
import wasm3, base64

# WebAssembly binary
WASM = base64.b64decode("AGFzbQEAAAABBgFgAX4"
    "BfgMCAQAHBwEDZmliAAAKHwEdACAAQgJUBEAgAA"
    "8LIABCAn0QACAAQgF9EAB8Dws=")

env = wasm3.Environment()
rt  = env.new_runtime(2048)
mod = env.parse_module(WASM)
rt.load(mod)
wasm_fib = rt.find_function("fib")
result = wasm_fib(24)
print(result)                       # 46368
```

## Building from source

Wasm3 is the `external/wasm3` submodule, so a plain clone has nothing to compile:

```sh
git clone --recurse-submodules https://github.com/wasm3/pywasm3
git submodule update --init --recursive   # if already cloned without it
```

Then `pip install .` or `uv build`. An sdist ships wasm3's sources, so
`pip install pywasm3 --no-binary pywasm3` needs no submodule handling.

## Development

```sh
uv sync                  # .venv with the project and dev tools
uv run pytest
uv run ruff check
uv run ruff format
uv run pyright
uv run --reinstall pytest                   # after editing src/wasm3/_wasm3.c
```

The same tools run as `pre-commit` hooks, which is what CI checks:

```sh
uv tool install pre-commit
pre-commit run --all-files
```

Most tests assemble their modules from inline WAT and need
[wabt](https://github.com/WebAssembly/wabt)'s `wat2wasm` (`apt install wabt`,
`brew install wabt`); without it they skip and only `tests/test_smoke.py` runs.

Release wheels are built by `.github/workflows/publish.yml` with
[cibuildwheel](https://cibuildwheel.pypa.io/) for Linux (x86_64/i686/aarch64/armv7l),
Windows (x64/x86/ARM64), macOS (arm64/x86_64) and Android.

### License
This project is released under The MIT License (MIT)

<!-- REUSABLE LINKS -->

[license]:
https://img.shields.io/github/license/o-murphy/pywasm3

[license-url]:
https://opensource.org/licenses/MIT

[pypi version]:
https://img.shields.io/pypi/v/pywasm3?logo=pypi

[python versions]:
https://img.shields.io/pypi/pyversions/pywasm3?logo=python

[PyPiUrl]:
https://pypi.org/project/pywasm3/

[Pre-commit]:
https://github.com/o-murphy/pywasm3/actions/workflows/pre-commit.yml/badge.svg

[pre-commit-workflow]:
https://github.com/o-murphy/pywasm3/actions/workflows/pre-commit.yml

[Publish]:
https://github.com/o-murphy/pywasm3/actions/workflows/publish.yml/badge.svg

[publish-workflow]:
https://github.com/o-murphy/pywasm3/actions/workflows/publish.yml

[coverage]:
https://codecov.io/gh/o-murphy/pywasm3/graph/badge.svg

[CodecovUrl]:
https://codecov.io/gh/o-murphy/pywasm3
