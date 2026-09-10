"""Python bindings for Wasm3, a fast WebAssembly interpreter.

Re-exports the compiled wasm3._wasm3 extension so the package can ship py.typed stubs.
"""

from wasm3._wasm3 import (
    M3_VERSION,
    Environment,
    Function,
    Memory,
    Module,
    Runtime,
)

__all__ = [
    "M3_VERSION",
    "Environment",
    "Function",
    "Memory",
    "Module",
    "Runtime",
]
