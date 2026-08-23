import shutil
import subprocess
import tempfile

import pytest


def wat2wasm(wat: str) -> bytes:
    """Assemble WAT with wabt's wat2wasm, skipping the whole module when it isn't installed.

    These calls happen at import time, hence allow_module_level - wheel-test runs
    (cibuildwheel) have no wabt, and tests/test_smoke.py covers them instead.
    """
    if shutil.which("wat2wasm") is None:
        pytest.skip("wabt's wat2wasm is not installed", allow_module_level=True)

    with tempfile.TemporaryDirectory() as d:
        fn_in = d + "/input.wat"
        fn_out = d + "/output.wasm"
        with open(fn_in, "wb") as f:
            f.write(wat.encode("utf8"))
        subprocess.run(["wat2wasm", "--enable-tail-call", "-o", fn_out, fn_in], check=True)
        with open(fn_out, "rb") as f:
            return f.read()
