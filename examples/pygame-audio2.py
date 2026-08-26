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
import time

import numpy

import wasm3

os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "true"

# Set to 44100 for better quality, or 11025 for faster computation
sample_rate = 22050

buffersize = 128 * 4

prebuffer = 256


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
    print("WebAssembly Music by Peter Salomonsen - from the executable music competition at Revision demoparty 2021")
    print("Source:      https://petersalomonsen.com/webassemblymusic/livecodev2/?gist=d71387112368a2692dc1d84c0ab5b1d2")
    print(
        "Synthesized: https://soundcloud.com/psalomo/webassembly-music-entry-for-the-revision-2021-executable-music-competition"
    )
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
    wasm_fn = os.path.join(scriptpath, "./wasm/music.wasm")

    # Prepare Wasm3 engine

    env = wasm3.Environment()
    rt = env.new_runtime(2 * 1024)
    with open(wasm_fn, "rb") as f:
        mod = env.parse_module(f.read())
        mod.link_global("environment", "SAMPLERATE", sample_rate)
        rt.load(mod)

    wasm_play = rt.find_function("playEventsAndFillSampleBuffer")

    duration = 164000

    def fetch_data():
        wasm_play()

        # music.wasm allocates its sample buffer during the first render call, so the
        # pointer has to be read afterwards - before that the global still reads as 0
        samplebufferL = mod.get_global("samplebuffer")
        samplebufferR = samplebufferL + buffersize

        # get data
        mem = rt.get_memory(0)
        data_l = mem[samplebufferL : samplebufferL + buffersize]
        data_r = mem[samplebufferR : samplebufferR + buffersize]

        # decode
        data_l = numpy.frombuffer(data_l, dtype=numpy.float32)
        data_r = numpy.frombuffer(data_r, dtype=numpy.float32)
        data = numpy.dstack((data_l, data_r))

        return (data.clip(-1, 1) * 32767).astype(numpy.int16).tobytes()

    stop_now = False
    try:
        buff = b""
        progress = 0
        while progress < 100:
            buff += fetch_data()
            progress = int(100 * len(buff) / (prebuffer * 1024))
            if not progress % 5:
                draw(f"\rPre-buffering... {progress}%")

        send(buff)
        draw("\n")

        buff = b""
        t = 0
        while t < duration:
            t = mod.get_global("currentTimeMillis")
            # draw(f"\rT: {t/1000:.3f}s")

            buff += fetch_data()

            if len(buff) >= 64 * 1024:
                # draw("+")
                send(buff)
                buff = b""
                time.sleep(0.01)

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
