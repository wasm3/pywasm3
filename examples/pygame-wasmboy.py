#!/usr/bin/env python3

import os, sys, struct, time
import wasm3
import numpy
import urllib.request
from enum import Enum

os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = "true"

import pygame

if len(sys.argv) >= 2:
    rom_fn = sys.argv[1]
else:
    rom_fn = ""

print("WasmBoy by Aaron Turner (torch2424)")
print()

if rom_fn:
    rom_f = open(rom_fn, "rb")
    rom_size = os.path.getsize(rom_fn)
else:
    print('Downloading "Back to Color" demo by Antonio Niño Díaz...')
    try:
        rom_f = urllib.request.urlopen('https://github.com/AntonioND/back-to-color/raw/master/demo.gbc')
        rom_size = int(rom_f.headers['content-length'])
    except Exception:
        print('Download failed. Please specify GB ROM file.')
        sys.exit(1)

scriptpath = os.path.dirname(os.path.realpath(__file__))
wasm_fn = os.path.join(scriptpath, f"./wasm/wasmerboy.wasm")

# Prepare Wasm3 engine

img_size = (160, 144)
(img_w, img_h) = img_size
scr_size = (img_w*4, img_h*4)
pygame.init()
surface = pygame.display.set_mode(scr_size)
pygame.display.set_caption("Wasm3 WasmBoy")
clock = pygame.time.Clock()

def virtual_rom_read(size):
    data = rom_f.read(size)
    return data

def virtual_size_write(data):
    #print(data.decode(), end='')
    # Always 160x144
    pass

def virtual_draw_write(data):
    global img
    img_scaled = pygame.transform.scale(img, scr_size)
    surface.blit(img_scaled, (0, 0))
    pygame.display.flip()

def virtual_fb_write(data):
    global img
    # Convert BGRX to RGBX
    arr = numpy.frombuffer(data, dtype=numpy.uint8)
    arr = arr.reshape((img_w*img_h, 4))[..., [2,1,0,3]]
    data = arr.astype(numpy.uint8).tobytes()
    img = pygame.image.frombuffer(data, img_size, "RGBX")

def virtual_input_read(size):
    # Process input
    inputs = b''
    for event in pygame.event.get():
        if (event.type == pygame.QUIT or
            (event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE)):
            pygame.quit()
            sys.exit()
        elif event.type == pygame.KEYDOWN or event.type == pygame.KEYUP:
            key = 0
            if event.key == pygame.K_UP:
                key = 38
            elif event.key == pygame.K_DOWN:
                key = 40
            elif event.key == pygame.K_LEFT:
                key = 37
            elif event.key == pygame.K_RIGHT:
                key = 39
            elif event.key == pygame.K_RETURN:
                key = 13
            elif event.key == pygame.K_BACKSPACE:
                key = 8

            if event.type == pygame.KEYDOWN:
                inputs += struct.pack("<BB", 1, key)
            else:
                inputs += struct.pack("<BB", 3, key)
    return inputs

class FileType(Enum):
    DIR = 3
    REG = 4

class FilePos(Enum):
    CUR = 0
    END = 1
    SET = 2

class WasiErrno(Enum):
    SUCCESS = 0
    ACCES   = 2
    AGAIN   = 6
    ALREADY = 7
    BADF    = 8
    BUSY    = 10
    INVAL   = 28

vfs = {
    "rom": {
        "fd":   1000,
        "type": FileType.REG,
        "size": rom_size,
        "read": virtual_rom_read,
    },
    "_wasmer/dev/fb0/virtual_size": {
        "fd":   1001,
        "type": FileType.REG,
        "write": virtual_size_write,
    },
    "_wasmer/dev/fb0/input": {
        "fd":   1002,
        "type": FileType.REG,
        "read": virtual_input_read,
    },
    "_wasmer/dev/fb0/draw": {
        "fd":   1003,
        "type": FileType.REG,
        "write": virtual_draw_write,
    },
    "_wasmer/dev/fb0/fb": {
        "fd":   1004,
        "type": FileType.REG,
        "write": virtual_fb_write,
    },
}
vfs_fds = { v["fd"] : v for (k,v) in vfs.items() }

env = wasm3.Environment()
rt = env.new_runtime(16*1024)
with open(wasm_fn, "rb") as f:
    mod = env.parse_module(f.read())
    rt.load(mod)

def args_sizes_get(argc, buf_sz):
    mem = rt.get_memory(0)
    struct.pack_into("<I", mem, argc,   2)
    struct.pack_into("<I", mem, buf_sz, 32)
    return WasiErrno.SUCCESS.value

def args_get(argv, buf):
    mem = rt.get_memory(0)
    struct.pack_into("<II", mem, argv, buf, buf+8)
    struct.pack_into("8s4s", mem, buf, b"wasmboy\0", b"rom\0")
    return WasiErrno.SUCCESS.value

def path_filestat_get(fd, flags, path, path_len, buff):
    mem = rt.get_memory(0)
    path = mem[path:path+path_len].tobytes().decode()
    #print("path_filestat_get:", path)
    f = vfs[path]
    struct.pack_into("<QQBxxxIQQQQ", mem, buff, 1, 1, f["type"].value, 1, f["size"], 0, 0 , 0)

    return WasiErrno.SUCCESS.value

def path_open(dirfd, dirflags, path, path_len, oflags, fs_rights_base, fs_rights_inheriting, fs_flags, fd):
    mem = rt.get_memory(0)
    path = mem[path:path+path_len].tobytes().decode()

    fd_val = vfs[path]["fd"]
    struct.pack_into("<I", mem, fd, fd_val)

    #print("path_open:", f"{dirfd}:{path} => {fd_val}")
    return WasiErrno.SUCCESS.value

def fd_seek(fd, offset, whence, result):
    mem = rt.get_memory(0)
    #print("fd_seek:", f"{fd} {FilePos(whence)}:{offset}")
    struct.pack_into("<Q", mem, result, 0)
    return WasiErrno.SUCCESS.value

def fd_read(fd, iovs, iovs_len, nread):
    mem = rt.get_memory(0)

    data_sz = 0
    for i in range(iovs_len):
        iov = iovs+8*i
        (off, size) = struct.unpack("<II", mem[iov:iov+8])
        data_sz += size

    if fd in vfs_fds and vfs_fds[fd]["read"]:
        data = vfs_fds[fd]["read"](data_sz)

        data_off = 0
        for i in range(iovs_len):
            iov = iovs+8*i
            (off, size) = struct.unpack("<II", mem[iov:iov+8])
            d = data[data_off:data_off+size]
            #print(f"Read {i}: {off}, {len(d)}")
            mem[off:off+len(d)] = d
            data_off += len(d)

        struct.pack_into("<I", mem, nread, data_off)
    else:
        print(f"Cannot read fd: {fd}")
        return WasiErrno.BADF.value

    return WasiErrno.SUCCESS.value

def fd_write(fd, iovs, iovs_len, nwritten):
    mem = rt.get_memory(0)

    # get data
    data = b''
    for i in range(iovs_len):
        iov = iovs+8*i
        (off, size) = struct.unpack("<II", mem[iov:iov+8])
        data += mem[off:off+size].tobytes()

    if fd == 1 or fd == 2:     # stdout, stderr
        print(data.decode(), end='')
    elif fd in vfs_fds and vfs_fds[fd]["write"]:
        vfs_fds[fd]["write"](data)
    else:
        print(f"Cannot write fd: {fd}")
        return WasiErrno.BADF.value

    return WasiErrno.SUCCESS.value

def clock_time_get(clk_id, precision, result):
    mem = rt.get_memory(0)
    struct.pack_into("<Q", mem, result, 0)
    return WasiErrno.SUCCESS.value

def poll_oneoff(ev_in, ev_out, subs, evts):
    mem = rt.get_memory(0)
    clock.tick(60)
    return WasiErrno.SUCCESS.value

for modname in ["wasi_unstable", "wasi_snapshot_preview1"]:
    mod.link_function(modname, "args_get",              "i(**)",        args_get)
    mod.link_function(modname, "args_sizes_get",        "i(**)",        args_sizes_get)
    mod.link_function(modname, "path_filestat_get",     "i(ii*i*)",     path_filestat_get)
    mod.link_function(modname, "path_open",             "i(ii*iiIIi*)", path_open)
    mod.link_function(modname, "fd_seek",               "i(iIi*)",      fd_seek)
    mod.link_function(modname, "fd_write",              "i(i*i*)",      fd_write)
    mod.link_function(modname, "fd_read",               "i(i*i*)",      fd_read)
    mod.link_function(modname, "clock_time_get",        "i(iI*)",       clock_time_get)
    mod.link_function(modname, "poll_oneoff",           "i(**i*)",      poll_oneoff)

wasm_start = rt.find_function("_start")
try:
    wasm_start()
except (KeyboardInterrupt, SystemExit):
    pass

