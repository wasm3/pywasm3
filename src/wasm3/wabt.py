"""WABT's `wat2wasm` and `wasm2wat`, bundled as wasm and run on wasm3 itself.

`wasm3/tools/*.wasm` are the `wasm32-wasip1` builds of the
[WebAssembly Binary Toolkit](https://github.com/WebAssembly/wabt) that wasm3 ships with
its test suite, so text-format modules work out of the box - no toolchain to install, no
subprocess to spawn, and the same behaviour on every platform pywasm3 has a wheel for:

    import wasm3

    wasm = wasm3.wat2wasm('(module (func (export "f") (result i32) i32.const 42))')
    print(wasm3.wasm2wat(wasm))

`wasm3.Environment.parse_module()` calls `wat2wasm()` for you when it is handed text, so
a module can be assembled and loaded in one step.

Each call runs the tool in a fresh runtime with an in-memory filesystem (see
`wasm3._wasi`); a conversion takes single-digit milliseconds.
"""

from collections.abc import Mapping, Sequence
from typing import NamedTuple

from wasm3 import _wasi
from wasm3._wasm3 import Environment

__all__ = ["FEATURE_ARGS", "TOOLS", "ToolResult", "WabtError", "run", "tool_bytes", "wasm2wat", "wat2wasm"]

#: The bundled tools, by the name `run()` takes.
TOOLS = ("wat2wasm", "wasm2wat")

#: Prepended to every tool's arguments: turn on the features wabt leaves off by default
#: (`--enable-all` would include one this build trips over), and leave it to wasm3 to
#: reject a module using something it cannot run. Later flags win, so
#: `args=["--disable-simd"]` still narrows it.
FEATURE_ARGS = (
    "--enable-exceptions",
    "--enable-threads",
    "--enable-function-references",
    "--enable-tail-call",
    "--enable-memory64",
    "--enable-multi-memory",
    "--enable-extended-const",
    "--enable-custom-page-sizes",
    # "--enable-compact-imports",  # broken in wabt 1.0.41
)

# Room for the guest's call frames. The tools recurse while parsing, and run out of
# their own 64 KB shadow stack long before they get through this.
_STACK_SIZE = 1024 * 1024

_TOOL_CACHE: dict[str, bytes] = {}


class WabtError(RuntimeError):
    """A bundled tool rejected its input. `str()` is what the tool reported.

    `exit_code` is what the tool exited with, or -1 when it trapped instead of exiting.
    """

    def __init__(self, tool: str, exit_code: int, message: str):
        super().__init__(message.strip() or f"{tool} exited with code {exit_code}")
        self.tool = tool
        self.exit_code = exit_code


class ToolResult(NamedTuple):
    """What one `run()` of a tool produced.

    A `dataclass` would read better here, but importing `dataclasses` costs more than
    everything else in this package put together.
    """

    exit_code: int
    stdout: bytes
    stderr: bytes
    #: The in-memory filesystem the tool left behind: its input files, plus whatever it
    #: wrote to a `-o <name>` output.
    files: dict[str, bytes]


def tool_bytes(tool: str) -> bytes:
    """The wasm binary for one bundled tool, read once and kept for later calls."""
    if tool not in TOOLS:
        raise ValueError(f"unknown tool {tool!r}, expected one of {', '.join(TOOLS)}")
    if tool not in _TOOL_CACHE:
        # Imported here, not at the top: it pulls in pathlib, tempfile and inspect,
        # which would triple the cost of `import wasm3` for everyone.
        import importlib.resources

        _TOOL_CACHE[tool] = (importlib.resources.files("wasm3") / "tools" / f"{tool}.wasm").read_bytes()
    return _TOOL_CACHE[tool]


def run(
    tool: str,
    args: Sequence[str] = (),
    *,
    files: Mapping[str, bytes] | None = None,
    stdin: bytes = b"",
) -> ToolResult:
    """Runs a bundled tool over an in-memory filesystem, as a command line would.

    `args` are the tool's arguments without its name (`["in.wat", "-o", "-"]`), `files`
    the flat set of files it can open, and the returned `ToolResult` carries its output.
    A tool that fails reports why on stderr and exits non-zero - unlike `wat2wasm()` and
    `wasm2wat()`, this does not turn that into an exception.
    """
    wasi = _wasi.Wasi([tool, *args], files=files, stdin=stdin)
    env = Environment()
    runtime = env.new_runtime(_STACK_SIZE)
    module = env.parse_module(tool_bytes(tool))
    runtime.load(module)
    wasi.link(module)

    exit_code = 0
    try:
        try:
            runtime.find_function("_start")()
        except _wasi.ProcExit as exited:
            exit_code = exited.code
        return ToolResult(
            exit_code=exit_code,
            stdout=bytes(wasi.stdout),
            stderr=bytes(wasi.stderr),
            files={name: bytes(data) for name, data in wasi.files.items()},
        )
    finally:
        wasi.close()


def wat2wasm(source: str | bytes, *, filename: str = "input.wat", args: Sequence[str] = ()) -> bytes:
    """Assembles a module from the text format, raising `WabtError` if it does not parse.

    `filename` is the name the tool reports errors against; `args` are extra `wat2wasm`
    flags, appended after `FEATURE_ARGS` (`["--debug-names"]` to keep the name section,
    for instance).
    """
    filename = _check_filename(filename)
    if isinstance(source, str):
        source = source.encode("utf8")
    result = _run_checked(
        "wat2wasm",
        [filename, "-o", "-", *FEATURE_ARGS, *args],
        {filename: bytes(source)},
    )
    return result.stdout


def wasm2wat(module: bytes, *, filename: str = "input.wasm", args: Sequence[str] = ()) -> str:
    """Disassembles a binary module to the text format, raising `WabtError` if invalid.

    `filename` is the name the tool reports errors against; `args` are extra `wasm2wat`
    flags, appended after `FEATURE_ARGS` (`["--fold-exprs"]`, say).
    """
    filename = _check_filename(filename)
    result = _run_checked("wasm2wat", [filename, *FEATURE_ARGS, *args], {filename: bytes(module)})
    return result.stdout.decode("utf8")


def _check_filename(filename: str) -> str:
    """The in-memory filesystem is flat, so a name is all a tool can be given."""
    if not filename or "/" in filename or "\\" in filename:
        raise ValueError(f"filename must be a plain file name, not {filename!r}")
    return filename


def _run_checked(tool: str, args: Sequence[str], files: Mapping[str, bytes]) -> ToolResult:
    """Runs a tool, turning both a non-zero exit and a trap into a `WabtError`."""
    try:
        result = run(tool, args, files=files)
    except RuntimeError as trap:
        # The tools get a 64 KB stack from their linker, which input nesting a few
        # hundred levels deep overruns - reported as a trap, with nothing on stderr.
        raise WabtError(tool, -1, f"{tool} trapped ({trap}); input may be nested too deeply") from trap
    if result.exit_code:
        raise WabtError(tool, result.exit_code, (result.stderr or result.stdout).decode("utf8", "replace"))
    return result
