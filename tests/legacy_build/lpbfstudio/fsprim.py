"""Strict file-system primitives for Restore (B7 Phase 2, S3-B2c-b, milestone b1a).

Low-level helpers that a later, separately authorized Restore stage builds on (design: plan
revision 4.7, state machine sections 0.4, 0.5, 0.7, 1 and 3.11). **Nothing in the application
calls this module yet**, so it changes no existing behaviour. It imports no application module.

What it provides:

* ``identify_fs``: the file-system and mount identity of a folder descriptor. ext4 and tmpfs only,
  by ``fstatfs`` magic, plus the mount ID from ``/proc/self/fdinfo`` and a strict parse of
  ``/proc/self/mountinfo`` (OD-19). ``fstatfs`` is called through ctypes with the x86_64 glibc
  ``struct statfs`` layout; every other architecture is refused before any call (no guessed ABI,
  no fallback).
* ``strict_fsync``: ``fsync`` whose every error propagates (``upgrade._fsync_fd`` swallows them).
* ``complete_listing`` and ``complete_read``: a listing or read is either complete or
  ``Incomplete``; an error or an observed change is never a partial pass. The listing's
  before/after ``fstat`` comparison is the *operational stability check* (AL-1): a bounded
  operational check, **not** an atomic snapshot.
* ``write_in_full``: continues after a short write; a zero return or an error stops at once
  (``WriteIncomplete``, with the count of bytes confirmed written). Never retried.
* ``exchange``: exactly one ``renameat2(..., RENAME_EXCHANGE)`` call; no fallback, no retry; the
  errno is raised as an ``OSError`` and is not classified here.

Errors are kept apart:

* ``UnsupportedArchitecture`` (a kind of ``UnsupportedPlatform``): the process is not x86_64 with
  64-bit ``long``; raised before any library call.
* ``UnsupportedPlatform``: not Linux, or a needed C library symbol is missing.
* ``OSError``: a system call failed (``fstatfs``, a ``/proc`` read, ``fsync``, ``renameat2``),
  passed through unwrapped with its errno.
* ``UnsupportedFileSystem``: a valid answer naming a file system other than ext4 or tmpfs.
* ``Incomplete``: missing, malformed, duplicated, inconsistent or changed data, or an error inside
  a complete listing or read (``errno`` keeps the system error, if any). Never a pass.

No claim is made about durability after power loss: a returned ``fsync`` says only what the
kernel reported.

**"One call" means one Python-level invocation (state machine section 1, Python boundary note;
section 3.11 step 3).** This module never repeats a call itself: ``strict_fsync`` invokes
``os.fsync`` once, ``write_in_full`` invokes ``os.write`` once per continuation step, and
``complete_read`` invokes ``os.close`` once. Python itself may restart an interrupted system call
inside one ``os`` invocation when a signal handler returns normally (PEP 475), so one invocation
can be more than one kernel call: observed with strace on this system for ``os.write``
(``ERESTARTSYS``, then a second ``write``); not established for ``os.fsync``. The design places
that runtime behaviour outside its retry rules and judges only the final return or exception.
``exchange`` calls ``renameat2`` through ctypes, where PEP 475 does not apply: one C library call,
and no retry after ``EINTR`` (that glibc issues exactly one kernel call for it is inferred from
its documented thin wrapper; observed once with strace, not proven).
"""

from __future__ import annotations

import ctypes
import os
import platform
import re
import stat
import sys
from dataclasses import dataclass

EXT4_MAGIC = 0xEF53
TMPFS_MAGIC = 0x01021994
SUPPORTED = {EXT4_MAGIC: "ext4", TMPFS_MAGIC: "tmpfs"}
NAMELEN_MIN, NAMELEN_MAX = 1, 4096
RENAME_EXCHANGE = 2  # linux/fs.h
CHUNK = 1 << 20

_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DECIMAL = re.compile(r"[0-9]+")
_MAJMIN = re.compile(r"([0-9]+):([0-9]+)")


# --------------------------------------------------------------------------- errors


class FsprimError(Exception):
    """Base of this module's own refusals (system call failures stay ``OSError``)."""


class UnsupportedPlatform(FsprimError):
    """The platform lacks something these primitives need (not Linux, or a missing symbol)."""


class UnsupportedArchitecture(UnsupportedPlatform):
    """Not x86_64 with a 64-bit ``long``: the ``struct statfs`` layout is not known, so no call is made."""


class UnsupportedFileSystem(FsprimError):
    """The file system answered validly, but it is not ext4 or tmpfs."""


class Incomplete(FsprimError):
    """An observation that is missing, malformed, duplicated, inconsistent, changed, or interrupted
    by an error. ``errno`` is the system error behind it, or None. ``close_errno`` is set when
    closing a descriptor failed: alone it is also ``errno``; after an earlier failure, ``errno``
    and ``reason`` keep that earlier failure and ``close_errno`` adds the close failure."""

    def __init__(self, reason: str, errno: int | None = None, close_errno: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.errno = errno
        self.close_errno = close_errno


class WriteIncomplete(FsprimError):
    """A write stopped before all bytes were written. ``written`` counts the bytes the calls
    confirmed; bytes of the failing call may still have reached the file. ``zero_return`` is True
    when a call returned 0 (no error); otherwise ``errno`` is the failing call's errno (None only
    if the ``OSError`` carried none)."""

    def __init__(self, written: int, total: int, errno: int | None, zero_return: bool = False) -> None:
        how = "a write call returned 0" if zero_return else f"a write call failed (errno {errno})"
        super().__init__(f"{how} after {written} of {total} bytes were confirmed written; this does not "
                         "mean that no further byte reached the file")
        self.written = written
        self.total = total
        self.errno = errno
        self.zero_return = zero_return


# --------------------------------------------------------------------------- narrow helpers
# Replaced one at a time by the tests to reach each failure.


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


def _machine() -> str:
    return platform.machine()


def _long_size() -> int:
    return ctypes.sizeof(ctypes.c_long)


_LIBC = None


def _libc():
    global _LIBC
    if _LIBC is None:
        try:
            _LIBC = ctypes.CDLL(None, use_errno=True)
        except OSError as exc:
            raise UnsupportedPlatform(f"the C library cannot be loaded: {exc}") from None
    return _LIBC


def _fstat(fd: int):
    return os.fstat(fd)


def _listdir(fd: int) -> list[str]:
    return os.listdir(fd)


def _lstat_at(dir_fd: int, name: str):
    return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)


def _open_at(dir_fd: int, name: str, flags: int) -> int:
    return os.open(name, flags, dir_fd=dir_fd)


def _read(fd: int, n: int) -> bytes:
    return os.read(fd, n)


def _write(fd: int, view) -> int:
    return os.write(fd, view)


def _fsync(fd: int) -> None:
    os.fsync(fd)


def _close(fd: int) -> None:
    os.close(fd)


def _read_proc(path: str) -> str:
    """The complete text of a /proc file, read to end of file. Errors propagate."""
    fd = os.open(path, os.O_RDONLY | _CLOEXEC)
    try:
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks).decode("utf-8", "surrogateescape")


# --------------------------------------------------------------------------- platform


def _require_linux() -> None:
    if not _is_linux():
        raise UnsupportedPlatform(f"these primitives need Linux; this is {sys.platform!r}")


def check_architecture() -> None:
    """Refuse unless this is a Linux x86_64 process with a 64-bit ``long`` (b1a support only)."""
    _require_linux()
    machine = _machine()
    if machine != "x86_64":
        raise UnsupportedArchitecture(f"fstatfs is supported on x86_64 only; this machine is {machine!r}")
    if _long_size() != 8:
        raise UnsupportedArchitecture("fstatfs is supported for 64-bit processes only; this process "
                                      f"has a {_long_size() * 8}-bit long")


def _symbol(name: str):
    try:
        return getattr(_libc(), name)
    except AttributeError:
        raise UnsupportedPlatform(f"the C library has no {name}") from None


class _StatfsX86_64(ctypes.Structure):
    """glibc ``struct statfs`` on x86_64 (bits/statfs.h, without _FILE_OFFSET_BITS=64 differences:
    every field is 64 bits wide there either way)."""

    _fields_ = [("f_type", ctypes.c_long), ("f_bsize", ctypes.c_long),
                ("f_blocks", ctypes.c_ulong), ("f_bfree", ctypes.c_ulong), ("f_bavail", ctypes.c_ulong),
                ("f_files", ctypes.c_ulong), ("f_ffree", ctypes.c_ulong),
                ("f_fsid", ctypes.c_int * 2), ("f_namelen", ctypes.c_long), ("f_frsize", ctypes.c_long),
                ("f_flags", ctypes.c_long), ("f_spare", ctypes.c_long * 4)]


assert ctypes.sizeof(_StatfsX86_64) == 120 or ctypes.sizeof(ctypes.c_long) != 8


def _fstatfs_raw(fd: int) -> tuple[int, int]:
    check_architecture()
    fn = _symbol("fstatfs")
    fn.argtypes = [ctypes.c_int, ctypes.POINTER(_StatfsX86_64)]
    fn.restype = ctypes.c_int
    buf = _StatfsX86_64()
    ctypes.set_errno(0)
    if fn(fd, ctypes.byref(buf)) != 0:
        err = ctypes.get_errno()
        if not err:
            raise Incomplete("fstatfs failed without an errno")
        raise OSError(err, os.strerror(err))
    return buf.f_type & 0xFFFFFFFF, buf.f_namelen


@dataclass(frozen=True)
class StatFs:
    f_type: int
    f_namelen: int


def fstatfs(fd: int) -> StatFs:
    """The file-system magic and maximum name length of ``fd``. Refuses an unsupported
    architecture before any call; a failing call raises ``OSError``."""
    f_type, f_namelen = _fstatfs_raw(fd)
    return StatFs(f_type, f_namelen)


# --------------------------------------------------------------------------- mount identity


@dataclass(frozen=True)
class MountLine:
    mount_id: int
    parent_id: int
    major: int
    minor: int
    mount_point: str
    fs_type: str


def fd_mount_id(fd: int) -> int:
    """The ``mnt_id`` of ``fd`` from ``/proc/self/fdinfo``: exactly one line with a decimal value."""
    text = _read_proc(f"/proc/self/fdinfo/{fd}")
    if not text.endswith("\n"):
        raise Incomplete("fdinfo is empty or does not end with a newline")
    values = [ln.split(":", 1)[1].strip(" \t") for ln in text.split("\n") if ln.startswith("mnt_id:")]
    if len(values) != 1:
        raise Incomplete(f"fdinfo holds {len(values)} mnt_id lines, not exactly one")
    if not _DECIMAL.fullmatch(values[0]):
        raise Incomplete(f"fdinfo mnt_id {values[0]!r} is not a decimal number")
    return int(values[0])


def parse_mountinfo(text: str) -> dict[int, MountLine]:
    """Every line of ``/proc/self/mountinfo`` parsed strictly, by mount ID. Any line that does
    not parse, a duplicate mount ID, an empty text or a missing final newline is ``Incomplete``."""
    if not text or not text.endswith("\n"):
        raise Incomplete("mountinfo is empty or does not end with a newline")
    lines: dict[int, MountLine] = {}
    for number, raw in enumerate(text[:-1].split("\n"), 1):
        fields = raw.split(" ")
        if len(fields) < 10:
            raise Incomplete(f"mountinfo line {number} has {len(fields)} fields, fewer than 10")
        seps = [i for i, f in enumerate(fields) if f == "-"]
        if len(seps) != 1 or seps[0] < 6 or len(fields) - seps[0] - 1 < 3:
            raise Incomplete(f"mountinfo line {number} has no single, well-placed '-' separator")
        if not (_DECIMAL.fullmatch(fields[0]) and _DECIMAL.fullmatch(fields[1])):
            raise Incomplete(f"mountinfo line {number} has a non-numeric mount or parent ID")
        mm = _MAJMIN.fullmatch(fields[2])
        if not mm:
            raise Incomplete(f"mountinfo line {number} has a malformed major:minor {fields[2]!r}")
        mid = int(fields[0])
        if mid in lines:
            raise Incomplete(f"mountinfo has mount ID {mid} twice")
        lines[mid] = MountLine(mid, int(fields[1]), int(mm.group(1)), int(mm.group(2)), fields[4],
                               fields[seps[0] + 1])
    return lines


@dataclass(frozen=True)
class FsIdentity:
    magic: int
    fs_type: str
    f_namelen: int
    mnt_id: int
    st_dev: int


def identify_fs(fd: int) -> FsIdentity:
    """The checked file-system and mount identity of folder descriptor ``fd`` (state machine 0.7).
    ext4 or tmpfs by magic, ``1 <= f_namelen <= 4096``, one fdinfo ``mnt_id``, a completely
    parsed mountinfo with exactly one line for it, whose device equals ``st_dev(fd)`` and whose
    type agrees with the magic. Anything else refuses; nothing is inferred from paths."""
    st = fstatfs(fd)
    fs_type = SUPPORTED.get(st.f_type)
    if fs_type is None:
        raise UnsupportedFileSystem(f"file-system magic {st.f_type:#x} is not ext4 or tmpfs")
    if not NAMELEN_MIN <= st.f_namelen <= NAMELEN_MAX:
        raise Incomplete(f"fstatfs returned an invalid maximum name length {st.f_namelen}")
    mnt_id = fd_mount_id(fd)
    lines = parse_mountinfo(_read_proc("/proc/self/mountinfo"))
    found = lines.get(mnt_id)
    if found is None:
        raise Incomplete(f"mountinfo has no line for mount ID {mnt_id}")
    dev = _fstat(fd).st_dev
    if (found.major, found.minor) != (os.major(dev), os.minor(dev)):
        raise Incomplete(f"mount {mnt_id} is device {found.major}:{found.minor}, but the folder is on "
                         f"{os.major(dev)}:{os.minor(dev)}")
    if found.fs_type != fs_type:
        raise Incomplete(f"mount {mnt_id} has type {found.fs_type!r}, but the magic says {fs_type}")
    return FsIdentity(st.f_type, fs_type, st.f_namelen, mnt_id, dev)


# --------------------------------------------------------------------------- fsync


def strict_fsync(fd: int) -> None:
    """``fsync(fd)``: one ``os.fsync`` invocation, never repeated by this module (Python's own
    PEP 475 handling inside that invocation is outside this module; see the module notes). Every
    error propagates unchanged. A return says only what the kernel reported; it is not a
    power-loss guarantee."""
    _fsync(fd)


# --------------------------------------------------------------------------- complete listing


def _kind(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "regular"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        return "device"
    return "other"


@dataclass(frozen=True)
class Entry:
    kind: str
    nlink: int
    dev: int
    ino: int


@dataclass(frozen=True)
class Listing:
    folder: tuple[int, int, int, int, int]  # (st_dev, st_ino, st_nlink, st_mtime_ns, st_ctime_ns)
    entries: dict[str, Entry]


def _stability(st) -> tuple[int, int, int, int, int]:
    return st.st_dev, st.st_ino, st.st_nlink, st.st_mtime_ns, st.st_ctime_ns


def complete_listing(dir_fd: int) -> Listing:
    """``fstat``, ``listdir``, ``fstat``, then an ``lstat`` of every name (state machine 0.4).
    Complete only if nothing raised and the five folder fields are equal before and after
    (the operational stability check, AL-1: not an atomic snapshot). Otherwise ``Incomplete``."""
    try:
        before = _stability(_fstat(dir_fd))
        names = _listdir(dir_fd)
        after = _stability(_fstat(dir_fd))
    except OSError as exc:
        raise Incomplete(f"the folder could not be listed completely: {exc}", exc.errno) from exc
    if before != after:
        raise Incomplete("the folder changed while it was listed (operational stability check failed)")
    entries: dict[str, Entry] = {}
    for name in names:
        try:
            st = _lstat_at(dir_fd, name)
        except OSError as exc:
            raise Incomplete(f"the entry {name!r} could not be lstat-ed: {exc}", exc.errno) from exc
        entries[name] = Entry(_kind(st.st_mode), st.st_nlink, st.st_dev, st.st_ino)
    return Listing(before, entries)


# --------------------------------------------------------------------------- complete read


def _check_name(name) -> str:
    """``name`` if it is one plain path component, else ``ValueError`` (a parameter error, raised
    before any file-system operation; never an ``OSError`` or ``Incomplete``)."""
    if not isinstance(name, str) or name in ("", ".", "..") or "/" in name or "\x00" in name:
        raise ValueError(f"{name!r} is not one plain path component")
    return name


@dataclass(frozen=True)
class FileRead:
    data: bytes
    dev: int
    ino: int
    size: int
    mtime_ns: int
    ctime_ns: int


def _read_facts(st) -> tuple[int, int, int, int, int]:
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def complete_read(dir_fd: int, name: str) -> FileRead:
    """The six-step complete read of ``name`` in ``dir_fd`` (state machine 0.4): a regular file
    with one link by ``lstat``, opened ``O_NOFOLLOW|O_NONBLOCK``, the same identity by ``fstat``,
    read in chunks to end of file, ``fstat`` unchanged after the read, and a final ``lstat`` with
    the same identity. Anything else, or any error, is ``Incomplete``. Never writes.

    ``name`` must be one plain path component (a non-empty ``str`` without ``/`` or NUL, not
    ``.`` or ``..``); anything else is a ``ValueError`` before any file-system operation.
    The descriptor is closed exactly once (never retried). A close failure is ``Incomplete``
    with ``close_errno``, never a successful read, and never hides an earlier failure."""
    _check_name(name)
    try:
        first = _lstat_at(dir_fd, name)
    except OSError as exc:
        raise Incomplete(f"{name!r} could not be lstat-ed: {exc}", exc.errno) from exc
    if not stat.S_ISREG(first.st_mode):
        raise Incomplete(f"{name!r} is a {_kind(first.st_mode)}, not a regular file")
    if first.st_nlink != 1:
        raise Incomplete(f"{name!r} has {first.st_nlink} hard links, not one")
    try:
        fd = _open_at(dir_fd, name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _CLOEXEC)
    except OSError as exc:
        raise Incomplete(f"{name!r} could not be opened: {exc}", exc.errno) from exc
    failure: Incomplete | None = None
    try:
        opened = _fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (first.st_dev, first.st_ino):
            raise Incomplete(f"{name!r} is not the file that was lstat-ed")
        chunks = []
        while True:
            chunk = _read(fd, CHUNK)
            if not chunk:
                break
            chunks.append(chunk)
        closing = _fstat(fd)
    except Incomplete as exc:
        failure = exc
    except OSError as exc:
        failure = Incomplete(f"{name!r} could not be read completely: {exc}", exc.errno)
        failure.__cause__ = exc
    except BaseException as exc:  # an interrupt: close once, then let it propagate unchanged
        try:
            _close(fd)
        except OSError as close_exc:
            exc.fsprim_close_error = close_exc  # kept on the propagating exception, not discarded
        raise
    try:
        _close(fd)  # exactly once, whatever happened above
    except OSError as close_exc:
        if failure is None:
            raise Incomplete(f"{name!r} was read, but closing its descriptor failed: {close_exc}; "
                             "the read is not accepted", close_exc.errno, close_exc.errno) from close_exc
        combined = Incomplete(f"{failure.reason}; closing the descriptor then also failed: {close_exc}",
                              failure.errno, close_exc.errno)
        raise combined from failure
    if failure is not None:
        raise failure
    data = b"".join(chunks)
    if _read_facts(closing) != _read_facts(opened) or len(data) != opened.st_size:
        raise Incomplete(f"{name!r} changed while it was read")
    try:
        last = _lstat_at(dir_fd, name)
    except OSError as exc:
        raise Incomplete(f"{name!r} could not be lstat-ed after the read: {exc}", exc.errno) from exc
    if (last.st_dev, last.st_ino) != (first.st_dev, first.st_ino):
        raise Incomplete(f"{name!r} names another file after the read")
    return FileRead(data, *_read_facts(opened))


# --------------------------------------------------------------------------- write in full


def write_in_full(fd: int, data: bytes) -> None:
    """Write all of ``data``: after a short write, the next call asks for the rest. A call that
    returns 0 (or less) or raises stops at once with ``WriteIncomplete``. This continuation is
    the only loop; a failing call is never repeated. Each step is one ``os.write`` invocation;
    Python may restart an interrupted kernel ``write`` inside it (PEP 475; see the module notes)."""
    view = memoryview(data)
    written = 0
    while written < len(view):
        try:
            n = _write(fd, view[written:])
        except OSError as exc:
            raise WriteIncomplete(written, len(view), exc.errno) from exc
        if n <= 0:
            raise WriteIncomplete(written, len(view), None, zero_return=True)
        written += n


# --------------------------------------------------------------------------- exchange


def _plain_name(name) -> bytes:
    return os.fsencode(_check_name(name))


def _renameat2():
    _require_linux()
    fn = _symbol("renameat2")
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    fn.restype = ctypes.c_int
    return fn


def exchange(dir_a: int, name_a: str, dir_b: int, name_b: str) -> None:
    """Atomically exchange ``dir_a/name_a`` and ``dir_b/name_b`` with exactly one
    ``renameat2(..., RENAME_EXCHANGE)`` call. No fallback and no retry. A failure raises
    ``OSError`` with the kernel's errno, unclassified; a failure without an errno is
    ``Incomplete``. Names must be single plain components (checked before the call). The call goes
    through ctypes, so Python's PEP 475 restart does not apply: an ``EINTR`` is raised, not retried."""
    a, b = _plain_name(name_a), _plain_name(name_b)
    fn = _renameat2()
    ctypes.set_errno(0)
    result = fn(dir_a, a, dir_b, b, RENAME_EXCHANGE)
    if result == 0:
        return
    err = ctypes.get_errno()
    if not err:
        raise Incomplete(f"renameat2 returned {result} without an errno")
    raise OSError(err, os.strerror(err))
