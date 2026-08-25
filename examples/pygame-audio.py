#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "numpy>=2.4.6",
#     "pygame-ce>=2.5.5",
#     "pywasm3",
# ]
#
# [tool.uv.sources]
# pywasm3 = { path = "../" }
# ///

import multiprocessing as mp
import os
import queue
import struct
import time

import numpy

import wasm3

os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "true"

sample_rate = 22050  # or 44100

prebuffer = 1024


class PlayerGone(Exception):
    """The player subprocess died, so there is no point in rendering more audio."""


def draw(c):
    print(c, end="", flush=True)


def player(q):
    try:
        from pygame import mixer

        mixer.pre_init(frequency=sample_rate, size=-16, channels=2)
        mixer.init()
    except Exception as e:  # no mixer support in this pygame build, or no audio device
        print(f"\nCannot play audio: {e}", flush=True)
        return

    channel = mixer.Channel(0)
    try:
        while True:
            chunk = mixer.Sound(buffer=q.get())

            draw("|" if channel.get_queue() else ".")

            while channel.get_queue() is not None:
                time.sleep(0.01)

            channel.queue(chunk)
    except (TypeError, BrokenPipeError, KeyboardInterrupt, SystemExit):
        mixer.quit()


if __name__ == "__main__":
    print("Hondarribia by Peter Salomonsen - intro song for WebAssembly Summit 2020")
    print("Source:      https://petersalomonsen.com/webassemblymusic/livecodev2/?gist=5b795090ead4f192e7f5ee5dcdd17392")
    print("Synthesized: https://soundcloud.com/psalomo/hondarribia")
    print()

    q = mp.Queue(maxsize=8)
    p = mp.Process(target=player, args=(q,))
    p.start()

    def send(data):
        """Hand a chunk over to the player, giving up if it is gone."""
        while p.is_alive():
            try:
                q.put(data, timeout=0.1)
                return
            except queue.Full:
                pass
        raise PlayerGone

    scriptpath = os.path.dirname(os.path.realpath(__file__))
    wasm_fn = os.path.join(scriptpath, f"./wasm/hondarribia-{sample_rate}.wasm")

    # Prepare Wasm3 engine

    env = wasm3.Environment()
    rt = env.new_runtime(2 * 1024)
    with open(wasm_fn, "rb") as f:
        mod = env.parse_module(f.read())
        rt.load(mod)

    buff = b""
    buff_sz = prebuffer

    def fd_write(fd, iovs, iovs_len, nwritten):
        global buff, buff_sz
        mem = rt.get_memory(0)

        # get data
        (off, size) = struct.unpack("<II", mem[iovs : iovs + 8])
        data = mem[off : off + size]

        # decode
        arr = numpy.frombuffer(data, dtype=numpy.float32)
        data = (arr.clip(-1, 1) * 32767).astype(numpy.int16).tobytes()

        # buffer
        buff += data

        if buff_sz == prebuffer:
            progress = int(100 * len(buff) / (prebuffer * 1024))
            if not progress % 5:
                draw(f"\rPre-buffering... {progress}%")

            if len(buff) >= prebuffer * 1024:
                buff_sz = 64
                draw("\n")

        if len(buff) >= buff_sz * 1024:
            # draw("+")
            send(buff)
            buff = b""
            time.sleep(0.01)

        return 0

    for modname in ["wasi_unstable", "wasi_snapshot_preview1"]:
        mod.link_function(modname, "fd_write", "i(i*i*)", fd_write)

    wasm_start = rt.find_function("_start")
    stop_now = False
    try:
        wasm_start()
        send(buff)  # play the leftover
        draw("!")
    except (KeyboardInterrupt, SystemExit):
        print("\nInterrupted by user")
        stop_now = True
    except PlayerGone:
        print("\nPlayer process is gone, stopping")
        stop_now = True
    finally:
        if stop_now:
            # Stop right away instead of playing out what is buffered, and drop the
            # queued chunks: otherwise the feeder thread would block forever at exit.
            p.terminate()
            q.cancel_join_thread()
        else:
            try:
                send(None)  # let the player stop once it has drained the queue
            except PlayerGone:
                q.cancel_join_thread()
        q.close()
        p.join()
