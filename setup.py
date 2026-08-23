#!/usr/bin/env python3
"""Builds the wasm3._wasm3 extension.

Metadata lives in pyproject.toml; what stays here can't be declarative: the source list,
per-compiler flags and the abi3 wheel tag. wasm3 comes from the external/wasm3 submodule.

abi3 floor is 3.11 because Runtime.get_memory() needs PyBuffer_FillInfo(), which entered
the limited API there. Free-threaded builds (incompatible with Py_LIMITED_API, PEP 703)
and Android (must link libpythonX.Y.so explicitly) get a normal build instead.
Keep in sync with [tool.cibuildwheel] in pyproject.toml.
"""

import os
import sys
import sysconfig
from glob import glob

from setuptools import Extension, setup

# m3_api_* are wasm3's own libc/WASI/tracer bindings: never linked here (WASI is
# implemented Python-side), and m3_api_uvwasi.c wouldn't compile without uvwasi headers.
SOURCES = [s for s in sorted(glob("external/wasm3/source/*.c")) if "m3_api_" not in s] + ["src/wasm3/_wasm3.c"]

if not any("m3_core.c" in s for s in SOURCES):
    raise SystemExit("external/wasm3/source is empty - run: git submodule update --init --recursive")

# platform.system() reports the host under cross-compilation; sysconfig reports the target.
_PLATFORM = sysconfig.get_platform()
IS_ANDROID = "android" in _PLATFORM
IS_MSVC = _PLATFORM.startswith("win")

ABI3_FLOOR = (3, 11)
# Android must link libpythonX.Y.so explicitly, pinning one CPython point release
# regardless of the wheel tag, so abi3 buys nothing there.
USE_LIMITED_API = sys.version_info >= ABI3_FLOOR and not sysconfig.get_config_var("Py_GIL_DISABLED") and not IS_ANDROID

# PYWASM3_COVERAGE=1: gcov instrumentation for .github/workflows/coverage.yml.
COVERAGE = os.environ.get("PYWASM3_COVERAGE") == "1"

# The old flag list was GCC/Clang-only yet passed unconditionally, leaving MSVC unoptimized.
if COVERAGE:
    EXTRA_COMPILE_ARGS = ["-O0", "-g", "--coverage"]
    EXTRA_LINK_ARGS = ["--coverage"]
elif IS_MSVC:
    EXTRA_COMPILE_ARGS = ["/O2"]
    EXTRA_LINK_ARGS = []
else:
    EXTRA_COMPILE_ARGS = ["-g0", "-O3", "-fomit-frame-pointer", "-fno-stack-check", "-fno-stack-protector"]
    EXTRA_LINK_ARGS = []

DEFINE_MACROS = [
    ("DEBUG", None),
    ("NASSERTS", None),
    ("d_m3MaxFunctionStackHeight", "16384"),
    ("d_m3HasTypedRefs", "1"),
]
if USE_LIMITED_API:
    DEFINE_MACROS.append(("Py_LIMITED_API", f"0x{ABI3_FLOOR[0]:02X}{ABI3_FLOOR[1]:02X}0000"))

cmdclass = {}
if USE_LIMITED_API:
    from setuptools.command.bdist_wheel import bdist_wheel

    # py_limited_api on the Extension builds against the limited API; this tags the wheel.
    class _bdist_wheel_abi3(bdist_wheel):
        def finalize_options(self):
            super().finalize_options()
            self.py_limited_api = f"cp{ABI3_FLOOR[0]}{ABI3_FLOOR[1]}"

    cmdclass["bdist_wheel"] = _bdist_wheel_abi3

setup(
    ext_modules=[
        Extension(
            "wasm3._wasm3",
            sources=SOURCES,
            include_dirs=["external/wasm3/source"],
            define_macros=DEFINE_MACROS,
            extra_compile_args=EXTRA_COMPILE_ARGS,
            extra_link_args=EXTRA_LINK_ARGS,
            py_limited_api=USE_LIMITED_API,
        )
    ],
    cmdclass=cmdclass,
)
