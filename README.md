[![SWUbanner](https://raw.githubusercontent.com/vshymanskyy/StandWithUkraine/main/banner-direct.svg)](https://github.com/vshymanskyy/StandWithUkraine/blob/main/docs/README.md)

# pywasm3

Python bindings for Wasm3, a fast WebAssembly interpreter and the most universal WASM runtime.

Main repository: [**Wasm3 project**](https://github.com/wasm3/wasm3)

## Install

```sh
# Latest release:
pip3 install pywasm3

# Bleeding edge version:
pip3 install https://github.com/wasm3/pywasm3/archive/main.tar.gz

# Or, if you have a local copy:
pip3 install .
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

### License
This project is released under The MIT License (MIT)
