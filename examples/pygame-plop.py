#!/usr/bin/env python3

import wasm3
import os, time, random, math
import struct
import pygame
import zlib

print("WebAssembly file by Caltrop")
print("Sources: https://github.com/Caltrop256/plop")

scriptpath = os.path.dirname(os.path.realpath(__file__))
wasm_fn = os.path.join(scriptpath, "./wasm/plop-sim.wasm")

# Prepare Wasm3 engine

env = wasm3.Environment()
rt = env.new_runtime(32*1024)
with open(wasm_fn, "rb") as f:
    mod = env.parse_module(f.read())
    rt.load(mod)
    mod.link_function("env", "sin", "F(F)", math.sin)
    mod.link_function("env", "cos", "F(F)", math.cos)
    mod.link_function("env", "atan2", "F(FF)", math.atan2)

wasm_seed = rt.find_function("seed")
wasm_draw = rt.find_function("draw")
wasm_tick = rt.find_function("tick")
wasm_malloc = rt.find_function("malloc")
wasm_free = rt.find_function("free")
wasm_importData = rt.find_function("importData")


wasm_seed(random.getrandbits(32),
        random.getrandbits(32),
        random.getrandbits(32),
        random.getrandbits(32),
        random.getrandbits(32),
        random.getrandbits(32))

# Load PLOP state

state_fn = os.path.join(scriptpath, "./wasm/plop-state.plop")

with open(state_fn, 'rb') as compressed:
    plop_data = zlib.decompress(compressed.read())
plop_len = len(plop_data)
ptr = wasm_malloc(plop_len)

mem = rt.get_memory(0)
mem[ptr : ptr+plop_len] = plop_data

res = wasm_importData(ptr)
wasm_free(ptr)


if res != 1:
    print("Invalid PLOP file")
    quit()

# Map memory region to an RGBA image

area_size = plop_data[8]
img_size = (area_size * 75, area_size * 75)
(img_w, img_h) = img_size

# Prepare PyGame

scr_size = (img_w*2, img_h*2)
pygame.init()
surface = pygame.display.set_mode(scr_size)
pygame.display.set_caption("Wasm3 plop")
white = (255, 255, 255)

mem = rt.get_memory(0)
img_ptr = mod.get_global("imageData")
(img_base,) = struct.unpack("<I", mem[img_ptr : img_ptr+4])
region = mem[img_base : img_base + (img_w * img_h * 4)]
img = pygame.image.frombuffer(region, img_size, "RGBA")

clock = pygame.time.Clock()

while True:

    # Process input
    for event in pygame.event.get():
        if (event.type == pygame.QUIT or
            (event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE)):
            pygame.quit()
            quit()

    # Render a frame
    wasm_draw()
    wasm_tick()

    # Image output
    img_scaled = pygame.transform.scale(img, scr_size)
    surface.blit(img_scaled, (0, 0))
    pygame.display.flip()

    # Stabilize FPS
    clock.tick(60)
    #print(int(clock.get_fps()))
