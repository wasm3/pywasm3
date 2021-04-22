import tempfile, subprocess

def wat2wasm(wat):
    with tempfile.TemporaryDirectory() as d:
        fn_in = d + "/input.wat"
        fn_out = d + "/output.wasm"
        with open(fn_in, "wb") as f:
            f.write(wat.encode('utf8'))
        subprocess.run(["wat2wasm", "--enable-all", "-o", fn_out, fn_in], check=True)
        with open(fn_out, "rb") as f:
            return f.read()
