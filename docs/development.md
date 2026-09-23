# Development

## Building from source

Wasm3 is the `external/wasm3` submodule, so a plain clone has nothing to compile:

```sh
git clone --recurse-submodules https://github.com/wasm3/pywasm3
git submodule update --init --recursive   # if already cloned without it
```

Then `pip install .` or `uv build`. An sdist ships wasm3's sources, so
`pip install pywasm3 --no-binary pywasm3` needs no submodule handling.

## Tests and checks

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

## Releases

Release wheels are built by `.github/workflows/publish.yml` with
[cibuildwheel](https://cibuildwheel.pypa.io/) for Linux (x86_64/i686/aarch64/armv7l),
Windows (x64/x86/ARM64), macOS (arm64/x86_64) and Android.

There is no Pyodide (`wasm32-emscripten`) wheel: [pyodide-issue.md](pyodide-issue.md)
records why.
