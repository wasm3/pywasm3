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

import argparse
import multiprocessing as mp
import os
import queue
import struct
import time

import numpy

import wasm3

os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "true"

# The songs, in playlist order. "rate" is the rate the module renders at: the WASI ones
# have it baked in, the samplebuffer ones take it via the SAMPLERATE import, so there it
# can be lowered (22050, 11025) to trade quality for speed on slower machines.
# "duration" (ms) is only needed by samplebuffer songs that don't export getDuration().
SONGS = {
    "hondarribia": {
        "title": "Hondarribia - intro song for WebAssembly Summit 2020",
        "source": "https://webassemblymusic.pages.dev/?gist=5b795090ead4f192e7f5ee5dcdd17392",
        "synthesized": "https://soundcloud.com/psalomo/hondarribia",
        "rate": 44100,
    },
    "music": {
        "title": "Executable music competition at Revision demoparty 2021",
        "source": "https://webassemblymusic.pages.dev/?gist=d71387112368a2692dc1d84c0ab5b1d2",
        "synthesized": "https://soundcloud.com/psalomo/webassembly-music-entry-for-the-revision-2021-executable-music-competition",
        "rate": 44100,
        "duration": 164000,
    },
    "wasmsummit2": {
        "title": "WebAssembly Summit 2021 theme",
        "source": "https://webassemblymusic.pages.dev/?gitrepo=wasmsummit2",
        "rate": 22050,  # 44100 for better quality, 11025 for faster computation
    },
    "shuffle-chill": {
        "title": "Shuffle Chill",
        "rate": 44100,
        "source": "https://webassemblymusic.pages.dev/?gist=0dd3fdd6cbc2ea6433c9e80635a68967",
        "synthesized": "https://soundcloud.com/psalomo/shuffle-chill",
    },
    "wasm-song": {
        "title": "Wasm Song",
        "rate": 44100,
        "source": "https://webassemblymusic.pages.dev/?gist=a74d2d036b3ecaa01af4e0f6d03ae7c4",
        "synthesized": "https://soundcloud.com/psalomo/wasm-song",
    },
    "webchip-music": {
        "title": "WebChip Music",
        "rate": 44100,
        "source": "https://webassemblymusic.pages.dev/?gist=ea73551e352440d5f470c6af89d7fe7c",
        "synthesized": "https://soundcloud.com/psalomo/webchip-music",
    },
}

scriptpath = os.path.dirname(os.path.realpath(__file__))
synthpath = os.path.join(scriptpath, "wasm", "synth")


class PlayerGone(Exception):
    """The player subprocess died, so there is no point in rendering more audio."""


def draw(c):
    print(c, end="", flush=True)


def player(q, sample_rate):
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


class Feeder:
    """Collects rendered PCM and hands it to the player in chunks.

    The first chunk is a big one: the synths render faster than realtime, but not by a
    wide margin, so a healthy head start is what keeps playback from stuttering later.
    """

    def __init__(self, send, prebuffer_kb):
        self.send = send
        self.limit = prebuffer_kb * 1024
        self.prebuffering = True
        self.buff = b""

    def feed(self, pcm):
        self.buff += pcm

        if self.prebuffering:
            progress = int(100 * len(self.buff) / self.limit)
            if not progress % 5:
                draw(f"\rPre-buffering... {progress}%")

        if len(self.buff) >= self.limit:
            self.flush()
            if self.prebuffering:
                self.prebuffering = False
                self.limit = 64 * 1024
                draw("\r" + " " * 30 + "\r")
            else:
                time.sleep(0.01)

    def flush(self):
        self.send(self.buff)
        self.buff = b""


def to_pcm(data, right=None):
    """Interleaved, or separate left/right, float32 samples -> signed 16-bit stereo."""
    if right is not None:
        data = numpy.dstack((data, right))
    return (data.clip(-1, 1) * 32767).astype(numpy.int16).tobytes()


def play_wasi(rt, mod, song, feeder):
    """Songs that render from _start() and write float32 frames out through fd_write."""
    mem = mod.get_memory(0)

    def fd_write(fd, iovs, iovs_len, nwritten):
        (off, size) = struct.unpack("<II", mem[iovs : iovs + 8])
        feeder.feed(to_pcm(numpy.frombuffer(mem[off : off + size], dtype=numpy.float32)))
        return 0

    for modname in ["wasi_unstable", "wasi_snapshot_preview1"]:
        mod.link_function(modname, "fd_write", "i(i*i*)", fd_write)

    rt.find_function("_start")()


def play_samplebuffer(rt, mod, song, feeder):
    """Songs that fill a pair of float32 channel buffers, one block per call."""
    buffersize = 128 * 4
    mem = mod.get_memory(0)
    wasm_play = rt.find_function("playEventsAndFillSampleBuffer")

    try:
        duration = rt.find_function("getDuration")()
    except RuntimeError:
        # 0 means "unknown": play until the song loops around or the user stops it
        duration = song.get("duration", 0)

    def render():
        wasm_play()

        # Read the pointer after rendering: music.wasm allocates its sample buffer during
        # the first render call, before that the global still reads as 0
        samplebufferL = mod.get_global("samplebuffer")
        samplebufferR = samplebufferL + buffersize

        data_l = numpy.frombuffer(mem[samplebufferL : samplebufferL + buffersize], dtype=numpy.float32)
        data_r = numpy.frombuffer(mem[samplebufferR : samplebufferR + buffersize], dtype=numpy.float32)
        return to_pcm(data_l, data_r)

    t = 0
    while not duration or t < duration:
        t = mod.get_global("currentTimeMillis")
        # draw(f"\rT: {t/1000:.3f}s")
        feeder.feed(render())


def play_song(name, rate=None):
    if name in SONGS:
        song = SONGS[name]
        wasm_fn = os.path.join(synthpath, f"{name}.wasm")
    else:  # some other synth module
        song = {"title": os.path.basename(name), "rate": 44100}
        wasm_fn = name

    print("===", song["title"], "===")
    if song.get("source"):
        print(f"Source:      {song['source']}")
    if song.get("synthesized"):
        print(f"Synthesized: {song['synthesized']}")

    # Prepare Wasm3 engine

    env = wasm3.Environment()
    rt = env.new_runtime(2 * 1024)
    with open(wasm_fn, "rb") as f:
        mod = env.parse_module(f.read())
    rt.load(mod)

    try:
        rt.find_function("playEventsAndFillSampleBuffer")
    except RuntimeError:  # a WASI song, rendering at the rate baked into it
        play, prebuffer_kb = play_wasi, 1024
        rate = song["rate"]
    else:
        play, prebuffer_kb = play_samplebuffer, 256
        rate = rate or song["rate"]
        # SAMPLERATE is only read while rendering, so linking it after load is fine
        mod.link_global("environment", "SAMPLERATE", rate)

    # Every song gets its own player: the mixer frequency is fixed at init time, and the
    # songs do not all render at the same rate
    q = mp.Queue(maxsize=64)
    p = mp.Process(target=player, args=(q, rate))
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

    feeder = Feeder(send, prebuffer_kb)
    try:
        play(rt, mod, song, feeder)
        feeder.flush()  # play the leftover
        draw("!")
        try:
            send(None)  # let the player stop once it has drained the queue
        except PlayerGone:
            q.cancel_join_thread()
    except BaseException:
        # Stop right away instead of playing out what is buffered, and drop the queued
        # chunks: otherwise the feeder thread would block forever at exit.
        p.terminate()
        q.cancel_join_thread()
        raise
    finally:
        q.close()
        p.join()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Play the WebAssembly synth modules in wasm/synth.",
        epilog="songs:\n" + "".join(f"  {n:<14} {s['title']}\n" for n, s in SONGS.items()),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "songs",
        nargs="*",
        metavar="SONG",
        help="song name, or path to another synth module (default: play the whole playlist)",
    )
    parser.add_argument(
        "--rate",
        type=int,
        metavar="HZ",
        help="sample rate to render at, e.g. 22050 or 11025 on a slower machine",
    )
    args = parser.parse_args()

    playlist = args.songs
    if not playlist:
        playlist = list(SONGS)
        print("WebAssembly Music by Peter Salomonsen: https://webassemblymusic.pages.dev")
        print("Playing everything in wasm/synth. Use --help to see available options.\n")

    try:
        for i, name in enumerate(playlist):
            if i:
                print()
            play_song(name, args.rate)
    except (KeyboardInterrupt, SystemExit):
        print("\nInterrupted by user")
    except PlayerGone:
        print("\nPlayer process is gone, stopping")
