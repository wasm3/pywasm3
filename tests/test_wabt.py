"""The bundled wabt tools, and the text format they give Environment.parse_module()."""

import pytest

import wasm3
from wasm3 import wabt

ADD_WAT = """
(module
  (func (export "add") (param i32 i32) (result i32)
    local.get 0
    local.get 1
    i32.add)
)
"""

SIMD_WAT = """
(module
  (func (export "splat") (result i32)
    (i32x4.extract_lane 0 (i32x4.splat (i32.const 3))))
)
"""

TAIL_CALL_WAT = """
(module
  (func $f (param i32) (result i32) (local.get 0))
  (func (export "g") (param i32) (result i32) (return_call $f (local.get 0)))
)
"""


def call_add(module_source):
    env = wasm3.Environment()
    rt = env.new_runtime(2048)
    rt.load(env.parse_module(module_source))
    return rt.find_function("add")(2, 3)


def test_wat2wasm_assembles_a_runnable_module():
    wasm = wasm3.wat2wasm(ADD_WAT)
    assert wasm.startswith(b"\0asm")
    assert call_add(wasm) == 5


def test_wat2wasm_accepts_text_as_bytes():
    assert wasm3.wat2wasm(ADD_WAT.encode()) == wasm3.wat2wasm(ADD_WAT)


def test_wat2wasm_reports_syntax_errors():
    with pytest.raises(wasm3.WabtError) as excinfo:
        wasm3.wat2wasm("(module (func nonsense))", filename="broken.wat")
    error = excinfo.value
    assert "broken.wat:1:15: error: unexpected token nonsense" in str(error)
    assert error.tool == "wat2wasm"
    assert error.exit_code == 1
    # Callers that only catch what parse_module() used to raise still see it.
    assert isinstance(error, RuntimeError)


def test_wat2wasm_takes_extra_arguments():
    assert b"nonsense" not in wasm3.wat2wasm(ADD_WAT)
    # --debug-names keeps the name section, which carries the exported name.
    assert b"add" in wasm3.wat2wasm(ADD_WAT, args=["--debug-names"])


def test_optional_features_are_enabled_and_can_be_narrowed():
    # return_call needs --enable-tail-call, which FEATURE_ARGS passes.
    assert wasm3.wat2wasm(TAIL_CALL_WAT)
    assert wasm3.wat2wasm(SIMD_WAT)
    # Extra flags land after FEATURE_ARGS, so they can still turn a feature back off.
    with pytest.raises(wasm3.WabtError, match="opcode not allowed: i32x4"):
        wasm3.wat2wasm(SIMD_WAT, args=["--disable-simd"])


def test_a_trapping_tool_is_reported_as_an_error(monkeypatch):
    """A tool that traps instead of exiting has to come out as a `WabtError` as well.

    Input nesting deeper than the 64 KB stack the tools were linked with does that for
    real, by reading outside the guest's memory - which is left to the hardware where
    wasm3 has guarded memories, so provoking it here would mean a test whose outcome
    depends on how the platform and CPython's faulthandler treat that fault.
    """

    def trap(*args, **kwargs):
        raise RuntimeError("[trap] out of bounds memory access")

    monkeypatch.setattr(wabt, "run", trap)
    with pytest.raises(wasm3.WabtError, match="nested too deeply") as excinfo:
        wasm3.wat2wasm(ADD_WAT)
    assert excinfo.value.exit_code == -1
    assert "[trap] out of bounds memory access" in str(excinfo.value)


def test_wasm2wat_disassembles():
    wat = wasm3.wasm2wat(wasm3.wat2wasm(ADD_WAT))
    assert "i32.add" in wat
    # Round trip: the text it prints assembles back to the same module.
    assert wasm3.wat2wasm(wat) == wasm3.wat2wasm(ADD_WAT)


def test_wasm2wat_rejects_a_broken_module():
    with pytest.raises(wasm3.WabtError, match="error"):
        wasm3.wasm2wat(b"\0asm\x01\0\0\0\xff\xff")


def test_parse_module_accepts_the_text_format():
    assert call_add(ADD_WAT) == 5
    assert call_add(ADD_WAT.encode()) == 5
    assert call_add(bytearray(wasm3.wat2wasm(ADD_WAT))) == 5
    assert call_add(memoryview(wasm3.wat2wasm(ADD_WAT))) == 5


def test_parse_module_reports_text_errors():
    env = wasm3.Environment()
    with pytest.raises(wasm3.WabtError, match="error: unexpected token"):
        env.parse_module("(module (func nonsense))")


def test_parse_module_still_rejects_broken_binaries():
    env = wasm3.Environment()
    with pytest.raises(RuntimeError):
        env.parse_module(b"\0asm\x01\0\0\0\xff\xff")


def test_environment_extends_the_extension_type():
    from wasm3 import _wasm3

    env = wasm3.Environment()
    assert isinstance(env, _wasm3.Environment)
    assert type(env) is wasm3.Environment

    class Subclass(wasm3.Environment):
        pass

    assert type(Subclass()) is Subclass


def test_run_reports_what_a_tool_produced():
    result = wabt.run("wat2wasm", ["--version"])
    assert result.exit_code == 0
    assert result.stdout.startswith(b"1.0.")

    result = wabt.run("wat2wasm", ["in.wat"], files={"in.wat": b"(module)"})
    assert result.exit_code == 0
    # No -o, so the tool named the output after its input, in its own filesystem.
    assert result.files["in.wasm"].startswith(b"\0asm")
    assert result.files["in.wat"] == b"(module)"


def test_run_does_not_raise_on_failure():
    result = wabt.run("wat2wasm", ["in.wat"], files={"in.wat": b"(module (func nonsense))"})
    assert result.exit_code == 1
    assert b"error: unexpected token" in result.stderr
    assert "in.wasm" not in result.files


def test_run_passes_stdin():
    result = wabt.run("wat2wasm", ["-", "-o", "-"], stdin=ADD_WAT.encode())
    assert result.exit_code == 0
    assert result.stdout == wasm3.wat2wasm(ADD_WAT)


def test_run_rejects_an_unbundled_tool():
    with pytest.raises(ValueError, match="unknown tool 'wasm-objdump'"):
        wabt.run("wasm-objdump", ["in.wasm"])


def test_filenames_are_plain_names():
    for filename in ("", "dir/in.wat", "dir\\in.wat"):
        with pytest.raises(ValueError, match="plain file name"):
            wasm3.wat2wasm(ADD_WAT, filename=filename)


def test_tool_bytes_are_read_once():
    tool = wabt.tool_bytes("wat2wasm")
    assert tool.startswith(b"\0asm")
    assert wabt.tool_bytes("wat2wasm") is tool
