#!/usr/bin/env python3

from setuptools import setup
from distutils.core import Extension
from glob import glob

SOURCES = glob('wasm3/*.c') + ['m3module.c']

setup(
    name         = "pywasm3",
    version      = "0.5.0",
    description  = "The fastest WebAssembly interpreter",
    platforms    = "any",
    url          = "https://github.com/wasm3/pywasm3",
    license      = "MIT",
    author       = "Volodymyr Shymanskyy",
    author_email = "vshymanskyi@gmail.com",
    
    long_description                = open("README.md").read(),
    long_description_content_type   = "text/markdown",

    ext_modules=[
        Extension('wasm3', sources=SOURCES, include_dirs=['wasm3'],
        extra_compile_args=['-g0', '-O3',
                            '-fomit-frame-pointer', '-fno-stack-check', '-fno-stack-protector',
                            '-DDEBUG', '-DNASSERTS', '-Dd_m3RecordBacktraces=1'])
    ],

    classifiers  = [
        "Topic :: Software Development :: Libraries :: Python Modules",
        "Development Status :: 4 - Beta",
        "License :: OSI Approved :: MIT License",
        "Operating System :: POSIX :: Linux",
        "Operating System :: Microsoft :: Windows",
        "Operating System :: MacOS :: MacOS X"
    ]
)
