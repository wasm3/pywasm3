"""A minimal WASI preview1 host, enough to run the single-shot tools in wasm3/tools.

Deliberately small: argv, an in-memory filesystem behind one preopened directory ("."),
stdin/stdout/stderr and the handful of calls wasi-libc makes on the way there. There are
no clocks, no sockets, no access to real files, and anything else a guest imports stays
unlinked - wasm3 traps on the first call to it. That is fine for `wat2wasm` and friends,
and not a sandbox for arbitrary WASI programs.

Struct layouts below are wasi-libc's, on a 32-bit guest.
"""

import struct
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wasm3._wasm3 import Memory, Module

# __wasi_errno_t
ESUCCESS = 0
EBADF = 8
EINVAL = 28
ENOENT = 44

# __wasi_filetype_t
FILETYPE_CHARACTER_DEVICE = 2
FILETYPE_DIRECTORY = 3
FILETYPE_REGULAR_FILE = 4

# __wasi_oflags_t
OFLAG_CREAT = 1 << 0
OFLAG_TRUNC = 1 << 3

# __wasi_rights_t: the tools only ever get handed descriptors we made, so there is
# nothing to be gained from handing out anything less than everything.
RIGHTS_ALL = (1 << 64) - 1

STDIN_FD = 0
STDOUT_FD = 1
STDERR_FD = 2
# Every relative path the guest opens is resolved against a preopened directory; the
# first descriptor after the standard streams is where wasi-libc starts looking for one.
PREOPEN_FD = 3
PREOPEN_DIR = "."

# The WASI functions this host implements, linked from both namespaces a module may
# import them from. wasm3 ignores an import a module does not have, so linking the
# whole set is harmless.
_EXPORTS = (
    "args_get",
    "args_sizes_get",
    "environ_get",
    "environ_sizes_get",
    "fd_close",
    "fd_fdstat_get",
    "fd_fdstat_set_flags",
    "fd_filestat_get",
    "fd_prestat_get",
    "fd_prestat_dir_name",
    "fd_read",
    "fd_seek",
    "fd_write",
    "path_filestat_get",
    "path_open",
    "proc_exit",
)
_NAMESPACES = ("wasi_unstable", "wasi_snapshot_preview1")


class ProcExit(Exception):
    """Raised out of proc_exit() to unwind the guest, the way exiting a process would."""

    def __init__(self, code: int):
        super().__init__(f"exited with code {code}")
        self.code = code


class _Handle:
    """An open descriptor: a byte buffer and a position in it.

    The buffer is shared with whoever owns the bytes - Wasi.files, Wasi.stdout - so guest
    writes show up there as they happen. Directories carry no buffer.
    """

    __slots__ = ("data", "filetype", "pos")

    def __init__(self, data: bytearray | None, filetype: int):
        self.data = data
        self.filetype = filetype
        self.pos = 0


class Wasi:
    """WASI host state for one run of one guest.

    Create it with the argv and input files the guest should see, `link()` it into a
    loaded module, call the module's `_start`, then read `stdout`, `stderr` and `files`.
    A host is single-use: the guest it ran has exited and its memory is spent.
    """

    def __init__(self, args: Sequence[str], files: Mapping[str, bytes] | None = None, stdin: bytes = b""):
        self.args = list(args)
        self.files: dict[str, bytearray] = {name: bytearray(data) for name, data in (files or {}).items()}
        self.stdout = bytearray()
        self.stderr = bytearray()
        self._mem: "Memory | None" = None
        self._fds = {
            STDIN_FD: _Handle(bytearray(stdin), FILETYPE_CHARACTER_DEVICE),
            STDOUT_FD: _Handle(self.stdout, FILETYPE_CHARACTER_DEVICE),
            STDERR_FD: _Handle(self.stderr, FILETYPE_CHARACTER_DEVICE),
            PREOPEN_FD: _Handle(None, FILETYPE_DIRECTORY),
        }
        self._next_fd = PREOPEN_FD + 1

    def link(self, module: "Module") -> None:
        """Links this host into a module. It has to be loaded already, for its memory."""
        self._mem = module.get_memory(0)
        for name in _EXPORTS:
            handler: Callable[..., int] = getattr(self, name)
            for namespace in _NAMESPACES:
                module.link_function(namespace, name, handler)

    def close(self) -> None:
        """Drops this host's references to the guest, keeping what it produced reachable.

        `Module.link_function()` keeps the callable it is handed alive for good, so the
        bound methods linked above outlive the run. Without this, every run would also
        retain its module, that module's memory and every file it touched.
        """
        self._mem = None
        self._fds = {}
        self.files = {}
        self.stdout = bytearray()
        self.stderr = bytearray()

    @property
    def mem(self) -> "Memory":
        if self._mem is None:
            raise RuntimeError("this WASI host is not linked to a module")
        return self._mem

    def _str(self, ptr: int, size: int) -> str:
        return bytes(self.mem[ptr : ptr + size]).decode("utf8", "surrogateescape")

    def _iovecs(self, iovs: int, iovs_len: int) -> Iterator[tuple[int, int]]:
        """The (buffer, length) pairs of an __wasi_ciovec_t array."""
        for i in range(iovs_len):
            yield struct.unpack_from("<II", self.mem, iovs + 8 * i)

    # --- argv and environment ------------------------------------------------------

    def args_sizes_get(self, argc_ptr: int, argv_size_ptr: int) -> int:
        size = sum(len(arg.encode("utf8")) + 1 for arg in self.args)
        struct.pack_into("<I", self.mem, argc_ptr, len(self.args))
        struct.pack_into("<I", self.mem, argv_size_ptr, size)
        return ESUCCESS

    def args_get(self, argv_ptr: int, argv_buf: int) -> int:
        offset = argv_buf
        for i, arg in enumerate(self.args):
            struct.pack_into("<I", self.mem, argv_ptr + 4 * i, offset)
            encoded = arg.encode("utf8") + b"\0"
            self.mem[offset : offset + len(encoded)] = encoded
            offset += len(encoded)
        return ESUCCESS

    def environ_sizes_get(self, count_ptr: int, size_ptr: int) -> int:
        struct.pack_into("<I", self.mem, count_ptr, 0)
        struct.pack_into("<I", self.mem, size_ptr, 0)
        return ESUCCESS

    def environ_get(self, environ_ptr: int, environ_buf: int) -> int:
        return ESUCCESS

    # --- descriptors ---------------------------------------------------------------

    def fd_prestat_get(self, fd: int, buf: int) -> int:
        """__wasi_prestat_t { u8 tag; u32 pr_name_len; }, padded to the u32's alignment."""
        if fd != PREOPEN_FD:
            return EBADF
        struct.pack_into("<BxxxI", self.mem, buf, 0, len(PREOPEN_DIR))  # tag 0: a directory
        return ESUCCESS

    def fd_prestat_dir_name(self, fd: int, path_ptr: int, path_len: int) -> int:
        if fd != PREOPEN_FD:
            return EBADF
        name = PREOPEN_DIR.encode()[:path_len]
        self.mem[path_ptr : path_ptr + len(name)] = name
        return ESUCCESS

    def fd_fdstat_get(self, fd: int, buf: int) -> int:
        """__wasi_fdstat_t { u8 fs_filetype; u16 fs_flags; u64 base, inheriting; }."""
        handle = self._fds.get(fd)
        if handle is None:
            return EBADF
        struct.pack_into("<BxHxxxxQQ", self.mem, buf, handle.filetype, 0, RIGHTS_ALL, RIGHTS_ALL)
        return ESUCCESS

    def fd_fdstat_set_flags(self, fd: int, flags: int) -> int:
        # Only ever used to set O_APPEND / O_NONBLOCK, neither of which means anything
        # for in-memory buffers written through a single-threaded guest.
        return ESUCCESS if fd in self._fds else EBADF

    def fd_filestat_get(self, fd: int, buf: int) -> int:
        handle = self._fds.get(fd)
        if handle is None:
            return EBADF
        self._pack_filestat(buf, handle.filetype, len(handle.data) if handle.data is not None else 0)
        return ESUCCESS

    def fd_close(self, fd: int) -> int:
        if fd not in self._fds:
            return EBADF
        # The standard streams and the preopened directory stay open: a guest closing
        # them still gets SUCCESS, and anything it writes afterwards is still captured.
        if fd > PREOPEN_FD:
            del self._fds[fd]
        return ESUCCESS

    def fd_seek(self, fd: int, offset: int, whence: int, new_offset_ptr: int) -> int:
        handle = self._fds.get(fd)
        if handle is None or handle.data is None:
            return EBADF
        if whence == 0:  # SET
            pos = offset
        elif whence == 1:  # CUR
            pos = handle.pos + offset
        elif whence == 2:  # END
            pos = len(handle.data) + offset
        else:
            return EINVAL
        if pos < 0:
            return EINVAL
        handle.pos = pos
        struct.pack_into("<Q", self.mem, new_offset_ptr, pos)
        return ESUCCESS

    def fd_read(self, fd: int, iovs: int, iovs_len: int, nread_ptr: int) -> int:
        handle = self._fds.get(fd)
        if handle is None or handle.data is None:
            return EBADF
        nread = 0
        for buf, size in self._iovecs(iovs, iovs_len):
            if size == 0:
                continue
            chunk = handle.data[handle.pos : handle.pos + size]
            if not chunk:
                break  # end of file: a short read, then zero, is how the guest sees it
            self.mem[buf : buf + len(chunk)] = chunk
            handle.pos += len(chunk)
            nread += len(chunk)
        struct.pack_into("<I", self.mem, nread_ptr, nread)
        return ESUCCESS

    def fd_write(self, fd: int, iovs: int, iovs_len: int, nwritten_ptr: int) -> int:
        handle = self._fds.get(fd)
        if handle is None or handle.data is None:
            return EBADF
        nwritten = 0
        for buf, size in self._iovecs(iovs, iovs_len):
            if handle.pos > len(handle.data):  # seeked past the end: the gap reads as zeros
                handle.data += bytes(handle.pos - len(handle.data))
            handle.data[handle.pos : handle.pos + size] = self.mem[buf : buf + size]
            handle.pos += size
            nwritten += size
        struct.pack_into("<I", self.mem, nwritten_ptr, nwritten)
        return ESUCCESS

    # --- paths ---------------------------------------------------------------------

    def path_open(
        self,
        dir_fd: int,
        dir_flags: int,
        path_ptr: int,
        path_len: int,
        oflags: int,
        rights: int,
        rights_inheriting: int,
        fd_flags: int,
        opened_fd_ptr: int,
    ) -> int:
        if dir_fd != PREOPEN_FD:
            return EBADF
        name = self._str(path_ptr, path_len)
        data = self.files.get(name)
        if data is None:
            if not oflags & OFLAG_CREAT:
                return ENOENT
            data = self.files[name] = bytearray()
        elif oflags & OFLAG_TRUNC:
            del data[:]  # in place, so a caller holding the buffer sees the new contents
        fd = self._next_fd
        self._next_fd += 1
        self._fds[fd] = _Handle(data, FILETYPE_REGULAR_FILE)
        struct.pack_into("<I", self.mem, opened_fd_ptr, fd)
        return ESUCCESS

    def path_filestat_get(self, dir_fd: int, flags: int, path_ptr: int, path_len: int, buf: int) -> int:
        if dir_fd != PREOPEN_FD:
            return EBADF
        data = self.files.get(self._str(path_ptr, path_len))
        if data is None:
            return ENOENT
        self._pack_filestat(buf, FILETYPE_REGULAR_FILE, len(data))
        return ESUCCESS

    def _pack_filestat(self, buf: int, filetype: int, size: int) -> None:
        """__wasi_filestat_t { u64 dev, ino; u8 filetype; u64 nlink, size, atim, mtim, ctim; }."""
        struct.pack_into("<QQB7xQQQQQ", self.mem, buf, 0, 0, filetype, 1, size, 0, 0, 0)

    # --- process -------------------------------------------------------------------

    def proc_exit(self, code: int) -> int:
        raise ProcExit(code)
