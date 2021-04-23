#!/usr/bin/env python3

import os, struct, time
import multiprocessing as mp
import wasm3
import numpy

os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = "true"

# Set to 44100 for better quality, or 11025 for faster computation
sample_rate = 22050

buffersize = 128*4

prebuffer = 256

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

    print("WebAssembly Summit 2021 theme - by Peter Salomonsen")
    print("Source:      https://petersalomonsen.com/webassemblymusic/livecodev2/?gitrepo=wasmsummit2")
    print()

    q = mp.Queue()
    p = mp.Process(target=player, args=(q,))
    p.start()

    scriptpath = os.path.dirname(os.path.realpath(__file__))
    wasm_fn = os.path.join(scriptpath, f"./wasm/wasmsummit2.wasm")

    # Prepare Wasm3 engine

    env = wasm3.Environment()
    rt = env.new_runtime(2*1024)
    with open(wasm_fn, "rb") as f:
        mod = env.parse_module(f.read())
        rt.load(mod)

    mod.set_global("SAMPLERATE", sample_rate)

    wasm_play = rt.find_function("playEventsAndFillSampleBuffer")

    duration = rt.find_function("getDuration")()

    samplebufferL = mod.get_global("samplebuffer")
    samplebufferR = samplebufferL + buffersize

    def fetch_data():
        global buff
        wasm_play()

        # get data
        mem = rt.get_memory(0)
        data_l = mem[samplebufferL : samplebufferL + buffersize]
        data_r = mem[samplebufferR : samplebufferR + buffersize]

        # decode
        data_l = numpy.frombuffer(data_l, dtype=numpy.float32)
        data_r = numpy.frombuffer(data_r, dtype=numpy.float32)
        data = numpy.dstack((data_l, data_r))

        return (data.clip(-1,1) * 32767).astype(numpy.int16).tobytes()

    try:
        
        buff = b''
        progress = 0
        while progress < 100:
            buff += fetch_data()
            progress = int(100*len(buff)/(prebuffer*1024))
            if not progress % 5:
                draw(f"\rPre-buffering... {progress}%")

        q.put(buff)
        draw("\n")

        buff = b''
        t = 0
        while t < duration:
            t = mod.get_global("currentTimeMillis")
            #draw(f"\rT: {t/1000:.3f}s")

            buff += fetch_data()

            if len(buff) >= 64*1024:
                #draw("+")
                q.put(buff)
                buff = b''
                time.sleep(0.01)

        q.put(buff)         # play the leftover
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        q.put(None)
        q.close()
        p.join()

    print()
    print("Finished")
