# Suspend, resume and snapshots

A suspendable runtime can pause a call part way, hand control back to Python, and
continue it later - in the same runtime, or from a snapshot in a new one, even in
another process:

```py
rt = env.new_runtime(64 * 1024)
rt.suspendable = True               # before find_function(): pause points are compiled in
rt.gas_limit = 1000                 # running out of gas now pauses instead of trapping
rt.load(mod)

result = rt.find_function("run")()  # None: it paused
while rt.suspended:
    snapshot = rt.save_snapshot()   # bytes: the paused call, memories, globals, tables
    rt.gas_limit = 1000             # refill
    result = rt.resume()

# Elsewhere: a fresh runtime with the same module loaded and linked
rt2.load_snapshot(mod2, snapshot)
result = rt2.resume()
```

`rt.request_suspend()` asks for a pause at the next loop back edge or function entry,
and can be called from an import.
[`examples/04-suspend-resume.py`](../examples/04-suspend-resume.py) runs a long search in
slices with a progress bar; Ctrl+C saves it to a file, and running the script again picks
it up where it left off.
