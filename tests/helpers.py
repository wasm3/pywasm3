"""Shared test helpers.

Modules are assembled from inline WAT by the wabt build bundled in the package, so the
whole suite runs anywhere pywasm3 is installed - including wheel tests, which have no
toolchain - and always against the same wabt version. `tests/test_wabt.py` covers the
bundled tools themselves.
"""

from wasm3 import wat2wasm

__all__ = ["wat2wasm"]
