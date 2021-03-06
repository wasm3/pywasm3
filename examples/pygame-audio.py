#!/usr/bin/env python3

import os, struct, time
import multiprocessing as mp
import wasm3
import numpy

os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = "true"

sample_rate = 22050     # or 44100

prebuffer = 1024

def draw(c):
    print(c, end='', flush=True)

def player(q):
    import pygame
    pygame.mixer.pre_init(frequency=sample_rate, size=-16, channels=2)
    pygame.init()

    channel = pygame.mixer.Channel(0)
    try:
        while True:
            chunk = pygame.mixer.Sound(buffer=q.get())

            draw("|" if channel.get_queue() else ".")

            while channel.get_queue() is not None:
                time.sleep(0.01)

            channel.queue(chunk)
    except (TypeError, BrokenPipeError, KeyboardInterrupt, SystemExit):
        pygame.quit()


if __name__ == '__main__':

    print("Hondarribia by Peter Salomonsen - intro song for WebAssembly Summit 2020")
    print("Source:      https://petersalomonsen.com/webassemblymusic/livecodev2/?gist=5b795090ead4f192e7f5ee5dcdd17392")
    print("Synthesized: https://soundcloud.com/psalomo/hondarribia")
    print()

    q = mp.Queue()
    p = mp.Process(target=player, args=(q,))
    p.start()

    scriptpath = os.path.dirname(os.path.realpath(__file__))
    wasm_fn = os.path.join(scriptpath, f"./wasm/hondarribia-{sample_rate}.wasm")

    # Prepare Wasm3 engine

    env = wasm3.Environment()
    rt = env.new_runtime(2048)
    with open(wasm_fn, "rb") as f:
        mod = env.parse_module(f.read())
        rt.load(mod)

    buff = b''
    buff_sz = prebuffer

    def fd_write(fd, iovs, iovs_len, nwritten):
        global buff, buff_sz
        mem = rt.get_memory(0)

        # get data
        (off, size) = struct.unpack("<II", mem[iovs:iovs+8])
        data = mem[off:off+size]

        # decode
        arr = numpy.frombuffer(data, dtype=numpy.float32) 
        data = (arr.clip(-1,1) * 32767).astype(numpy.int16).tobytes()

        # buffer
        buff += data

        if buff_sz == prebuffer:
            progress = int(100*len(buff)/(prebuffer*1024))
            if not progress % 5:
                draw(f"\rPre-buffering... {progress}%")

            if len(buff) >= prebuffer*1024:
                buff_sz = 64
                draw("\n")

        if len(buff) >= buff_sz*1024:
            #draw("+")
            q.put(buff)
            buff = b''
            time.sleep(0.01)

        return 0

    for modname in ["wasi_unstable", "wasi_snapshot_preview1"]:
        mod.link_function(modname, "fd_write", "i(i*i*)", fd_write)

    wasm_start = rt.find_function("_start")
    try:
        wasm_start()
        q.put(buff)         # play the leftover
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        q.put(None)
        q.close()
        p.join()

    print()
    print("Finished")
