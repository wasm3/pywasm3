"""Python bindings for Wasm3, a fast WebAssembly interpreter.

Re-exports the compiled wasm3._wasm3 extension so the package can ship py.typed stubs,
and extends its Environment with the text format, assembled by the WABT builds bundled
in wasm3/tools (see wasm3.wabt).
"""

from typing import TYPE_CHECKING

from wasm3._wasm3 import (
    M3_VERSION,
    Function,
    Memory,
    Module,
    Runtime,
)
from wasm3._wasm3 import (
    Environment as _Environment,
)
from wasm3.wabt import WabtError, wasm2wat, wat2wasm

if TYPE_CHECKING:
    from typing_extensions import Buffer  # collections.abc.Buffer is 3.12+

# WebAssembly's magic number. Anything else is taken to be the text format.
_WASM_MAGIC = b"\0asm"


class Environment(_Environment):
    """A wasm3 environment: the owner of runtimes and of the modules parsed here."""

    def parse_module(self, data: "str | Buffer", /) -> Module:
        """Parses a module, in either the binary or the text format.

        Text is assembled with the bundled `wat2wasm` first, so `str` - and bytes that
        are not a binary module, such as the contents of a `.wat` file - go through
        `wasm3.wat2wasm()`, and a syntax error raises `wasm3.WabtError`.
        """
        if isinstance(data, str):
            data = wat2wasm(data)
        else:
            if not isinstance(data, bytes):
                data = bytes(data)  # the extension parses out of bytes only
            if not data.startswith(_WASM_MAGIC):
                data = wat2wasm(data)
        return super().parse_module(data)


__all__ = [
    "M3_VERSION",
    "Environment",
    "Function",
    "Memory",
    "Module",
    "Runtime",
    "WabtError",
    "wasm2wat",
    "wat2wasm",
]
