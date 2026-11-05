"""Strict file-system primitives for Restore (B7 Phase 2, S3-B2c-b, milestone b1a).

``fsprim`` holds the low-level helpers that a later Restore stage will build on (design: plan
revision 4.7, state machine sections 0.4, 0.5, 0.7, 1 and 3.11). It has no caller yet, so no
application behaviour changes. These tests pin:

* file-system and mount identity: ``fstatfs`` through ctypes on x86_64 only (other architectures
  are refused before any call), ext4 and tmpfs only, the mount ID from ``/proc/self/fdinfo`` and a
  strict parse of ``/proc/self/mountinfo``; anything missing, duplicated, malformed or
  inconsistent fails closed, and an unsupported architecture, a failing system call and invalid
  returned data are three different errors;
* ``strict_fsync``: every error propagates, never swallowed (unlike ``upgrade._fsync_fd``);
* the complete listing (with the operational stability check, AL-1) and the complete read: an
  error or a change is ``Incomplete``, never a partial pass;
* write in full: a short write is continued, a zero return or an error stops at once, and the
  failing call is never retried;
* ``exchange``: exactly one ``renameat2(RENAME_EXCHANGE)`` call, no fallback, no retry; the errno
  is passed through unclassified.

Real I/O runs on both supported file systems: ext4 (``/tmp``) and tmpfs (``/dev/shm``). Syscall
errors and incomplete observations are injected through the module's narrow helpers. Nothing here
is a power-loss or crash-durability test. Synthetic data in temporary folders only.
"""

import ctypes
import errno
import os
import re
import shutil
import stat
import sys
import tempfile
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX primitives; Windows is refused")

from lpbfstudio import fsprim  # noqa: E402

ROOTS = {"ext4": "/tmp", "tmpfs": "/dev/shm"}
FUSE_MAGIC, OVERLAY_MAGIC = 0x65735546, 0x794C7630


# --------------------------------------------------------------------------- helpers


@pytest.fixture(params=sorted(ROOTS))
def fsdir(request):
    """A new empty folder on a real ext4 or tmpfs file system (never skipped: both must exist)."""
    root = ROOTS[request.param]
    path = Path(tempfile.mkdtemp(prefix="fsprim-", dir=root))
    try:
        yield request.param, path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def dfd(fsdir):
    _, path = fsdir
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield fd
    finally:
        os.close(fd)


def _path_of(fsdir) -> Path:
    return fsdir[1]


def fake_stat(st, **changes):
    fields = ("st_mode", "st_ino", "st_dev", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
    data = {f: getattr(st, f) for f in fields}
    data.update(changes)
    return types.SimpleNamespace(**data)


def snap(path: Path) -> dict:
    out = {}
    for p in sorted(path.rglob("*")):
        st = p.lstat()
        data = p.read_bytes() if stat.S_ISREG(st.st_mode) else None
        out[str(p.relative_to(path))] = (st.st_mode, st.st_ino, st.st_nlink, st.st_size, st.st_mtime_ns, data)
    st = path.stat()
    out["."] = (st.st_ino, st.st_nlink, st.st_mtime_ns)
    return out


def line(mid, parent, majmin, mount_point, fstype, source="src", extra=""):
    opt = f" {extra}" if extra else ""
    return f"{mid} {parent} {majmin} / {mount_point} rw,relatime{opt} - {fstype} {source} rw\n"


def dev_of(fd) -> str:
    st = os.fstat(fd)
    return f"{os.major(st.st_dev)}:{os.minor(st.st_dev)}"


def proc(monkeypatch, fdinfo: str | None, mountinfo: str | None):
    """Serve synthetic /proc texts; None makes that read fail with EIO."""
    def read(path):
        text = fdinfo if "/fdinfo/" in path else mountinfo
        if text is None:
            raise OSError(errno.EIO, "injected")
        return text
    monkeypatch.setattr(fsprim, "_read_proc", read)


def statfs(monkeypatch, magic, namelen=255):
    monkeypatch.setattr(fsprim, "_fstatfs_raw", lambda fd: (magic, namelen))


# --------------------------------------------------------------------------- isolation


def test_no_module_of_the_application_uses_fsprim_yet():
    src = Path(fsprim.__file__).parent
    users = [p.name for p in src.rglob("*.py") if p.name != "fsprim.py"
             and re.search(r"\bfsprim\b", p.read_text(encoding="utf-8"))]
    assert users == []


def test_fsprim_imports_no_application_module():
    text = Path(fsprim.__file__).read_text(encoding="utf-8")
    assert not re.search(r"^\s*(from\s+\.|import\s+lpbfstudio|from\s+lpbfstudio)", text, re.M)


# --------------------------------------------------------------------------- architecture and fstatfs


def test_fstatfs_reports_the_real_magic_of_each_supported_file_system(fsdir, dfd):
    kind, _ = fsdir
    st = fsprim.fstatfs(dfd)
    assert st.f_type == {"ext4": fsprim.EXT4_MAGIC, "tmpfs": fsprim.TMPFS_MAGIC}[kind]
    assert 1 <= st.f_namelen <= 4096


@pytest.mark.parametrize("machine", ["aarch64", "arm64", "i686", "armv7l", "riscv64", "ppc64le", "", "X86_64"])
def test_unsupported_architecture_is_refused_before_any_call(monkeypatch, dfd, machine):
    monkeypatch.setattr(fsprim, "_machine", lambda: machine)
    monkeypatch.setattr(fsprim, "_libc", lambda: pytest.fail("libc must not be touched"))
    with pytest.raises(fsprim.UnsupportedArchitecture):
        fsprim.fstatfs(dfd)
    with pytest.raises(fsprim.UnsupportedArchitecture):
        fsprim.identify_fs(dfd)


def test_a_32_bit_process_on_x86_64_is_refused(monkeypatch, dfd):
    monkeypatch.setattr(fsprim, "_long_size", lambda: 4)
    monkeypatch.setattr(fsprim, "_libc", lambda: pytest.fail("libc must not be touched"))
    with pytest.raises(fsprim.UnsupportedArchitecture):
        fsprim.fstatfs(dfd)


def test_a_non_linux_platform_is_refused(monkeypatch, dfd):
    monkeypatch.setattr(fsprim, "_is_linux", lambda: False)
    monkeypatch.setattr(fsprim, "_libc", lambda: pytest.fail("libc must not be touched"))
    with pytest.raises(fsprim.UnsupportedPlatform):
        fsprim.fstatfs(dfd)
    with pytest.raises(fsprim.UnsupportedPlatform):
        fsprim.exchange(dfd, "a", dfd, "b")


def test_a_missing_fstatfs_symbol_is_an_unsupported_platform_not_an_architecture(monkeypatch, dfd):
    monkeypatch.setattr(fsprim, "_libc", lambda: types.SimpleNamespace())
    with pytest.raises(fsprim.UnsupportedPlatform) as exc:
        fsprim.fstatfs(dfd)
    assert not isinstance(exc.value, fsprim.UnsupportedArchitecture)


def test_a_failing_fstatfs_call_is_an_os_error_with_its_errno(fsdir):
    fd = os.open(_path_of(fsdir), os.O_RDONLY | os.O_DIRECTORY)
    os.close(fd)
    with pytest.raises(OSError) as exc:
        fsprim.fstatfs(fd)
    assert exc.value.errno == errno.EBADF
    assert not isinstance(exc.value, fsprim.FsprimError)


def test_the_three_error_kinds_are_distinct_types():
    assert issubclass(fsprim.UnsupportedArchitecture, fsprim.UnsupportedPlatform)
    assert not issubclass(fsprim.Incomplete, fsprim.UnsupportedPlatform)
    assert not issubclass(fsprim.UnsupportedPlatform, fsprim.Incomplete)
    assert not issubclass(fsprim.FsprimError, OSError)


# --------------------------------------------------------------------------- identify_fs (real)


def test_identify_fs_accepts_real_ext4_and_tmpfs(fsdir, dfd):
    kind, _ = fsdir
    ident = fsprim.identify_fs(dfd)
    assert ident.fs_type == kind
    assert ident.mnt_id == fsprim.fd_mount_id(dfd)
    assert ident.st_dev == os.fstat(dfd).st_dev


def test_identify_fs_writes_nothing(fsdir, dfd):
    (_path_of(fsdir) / "x.json").write_bytes(b"{}")
    before = snap(_path_of(fsdir))
    fsprim.identify_fs(dfd)
    assert snap(_path_of(fsdir)) == before


# --------------------------------------------------------------------------- identify_fs (injected)


@pytest.mark.parametrize("magic", [FUSE_MAGIC, OVERLAY_MAGIC, 0x9123683E, 0x6969, 0])
def test_identify_fs_refuses_every_other_file_system(monkeypatch, dfd, magic):
    statfs(monkeypatch, magic)
    monkeypatch.setattr(fsprim, "_read_proc", lambda p: pytest.fail("no /proc read for a refused type"))
    with pytest.raises(fsprim.UnsupportedFileSystem):
        fsprim.identify_fs(dfd)


@pytest.mark.parametrize("namelen", [0, -1, 4097, 1 << 40])
def test_an_out_of_range_name_length_is_invalid_returned_data(monkeypatch, dfd, namelen):
    statfs(monkeypatch, fsprim.EXT4_MAGIC, namelen)
    with pytest.raises(fsprim.Incomplete):
        fsprim.identify_fs(dfd)


@pytest.mark.parametrize("namelen", [1, 4096])
def test_name_length_limits_are_inclusive(monkeypatch, dfd, namelen):
    statfs(monkeypatch, fsprim.EXT4_MAGIC, namelen)
    proc(monkeypatch, "pos:\t0\nmnt_id:\t7\n", line(7, 1, dev_of(dfd), "/x", "ext4"))
    assert fsprim.identify_fs(dfd).f_namelen == namelen


@pytest.mark.parametrize("magic,fstype", [(fsprim.EXT4_MAGIC, "ext3"), (fsprim.EXT4_MAGIC, "ext2"),
                                          (fsprim.EXT4_MAGIC, "tmpfs"), (fsprim.TMPFS_MAGIC, "ext4"),
                                          (fsprim.TMPFS_MAGIC, "fuse.rclone")])
def test_a_mountinfo_type_that_disagrees_with_the_magic_is_refused(monkeypatch, dfd, magic, fstype):
    statfs(monkeypatch, magic)
    proc(monkeypatch, "mnt_id:\t7\n", line(7, 1, dev_of(dfd), "/x", fstype))
    with pytest.raises(fsprim.Incomplete):
        fsprim.identify_fs(dfd)


def test_a_device_number_that_disagrees_with_st_dev_is_refused(monkeypatch, dfd):
    statfs(monkeypatch, fsprim.EXT4_MAGIC)
    proc(monkeypatch, "mnt_id:\t7\n", line(7, 1, "4095:4095", "/x", "ext4"))
    with pytest.raises(fsprim.Incomplete):
        fsprim.identify_fs(dfd)


def test_no_mountinfo_line_for_the_mount_id_is_refused(monkeypatch, dfd):
    statfs(monkeypatch, fsprim.EXT4_MAGIC)
    proc(monkeypatch, "mnt_id:\t8\n", line(7, 1, dev_of(dfd), "/x", "ext4"))
    with pytest.raises(fsprim.Incomplete):
        fsprim.identify_fs(dfd)


def test_stacked_mounts_on_one_mount_point_resolve_by_mount_id_only(monkeypatch, dfd):
    statfs(monkeypatch, fsprim.TMPFS_MAGIC)
    text = line(26, 25, "0:24", "/dev/shm", "tmpfs") + line(36, 26, dev_of(dfd), "/dev/shm", "tmpfs")
    proc(monkeypatch, "mnt_id:\t36\n", text)
    assert fsprim.identify_fs(dfd).mnt_id == 36
    proc(monkeypatch, "mnt_id:\t26\n", text)  # the lower mount has another device: refused, never guessed
    with pytest.raises(fsprim.Incomplete):
        fsprim.identify_fs(dfd)


@pytest.mark.parametrize("which", ["fdinfo", "mountinfo"])
def test_a_failing_proc_read_fails_closed(monkeypatch, dfd, which):
    statfs(monkeypatch, fsprim.EXT4_MAGIC)
    proc(monkeypatch, None if which == "fdinfo" else "mnt_id:\t7\n",
         None if which == "mountinfo" else line(7, 1, dev_of(dfd), "/x", "ext4"))
    with pytest.raises(OSError):
        fsprim.identify_fs(dfd)


def test_the_real_proc_reader_reads_to_the_end(dfd):
    text = fsprim._read_proc("/proc/self/mountinfo")
    assert text.endswith("\n") and len(text.splitlines()) >= 1
    assert fsprim._read_proc(f"/proc/self/fdinfo/{dfd}").count("mnt_id:") == 1


# --------------------------------------------------------------------------- fdinfo


@pytest.mark.parametrize("text,expected", [("pos:\t0\nflags:\t02300000\nmnt_id:\t28\nino:\t20\n", 28),
                                           ("mnt_id:\t0\n", 0), ("mnt_id: 12345\n", 12345)])
def test_fdinfo_with_one_decimal_mount_id(monkeypatch, text, expected):
    proc(monkeypatch, text, None)
    assert fsprim.fd_mount_id(3) == expected


@pytest.mark.parametrize("text", ["", "pos:\t0\n", "mnt_id:\t28\nmnt_id:\t28\n", "mnt_id:\t28\nmnt_id:\t29\n",
                                  "mnt_id:\tabc\n", "mnt_id:\t-1\n", "mnt_id:\t\n", "mnt_id:\t1 2\n",
                                  "mnt_id:\t0x1c\n", "mnt_id:\t28", "mnt_id:\t٢٨\n", "pos:\t0\nmnt_id 28\n"])
def test_fdinfo_without_exactly_one_valid_mount_id_is_incomplete(monkeypatch, text):
    proc(monkeypatch, text, None)
    with pytest.raises(fsprim.Incomplete):
        fsprim.fd_mount_id(3)


# --------------------------------------------------------------------------- mountinfo


def test_mountinfo_parses_the_kernel_format_with_optional_fields():
    text = (line(28, 1, "254:0", "/", "ext4", "/dev/vda", "shared:1")
            + line(36, 26, "0:28", "/dev/shm", "tmpfs", "tmpfs", "shared:2 master:1")
            + "49 28 0:41 / /mnt/a\\040b rw - fuse.rclone remote:x rw,user_id=0\n")
    lines = fsprim.parse_mountinfo(text)
    assert set(lines) == {28, 36, 49}
    assert (lines[36].major, lines[36].minor, lines[36].fs_type) == (0, 28, "tmpfs")
    assert lines[28].parent_id == 1 and lines[49].fs_type == "fuse.rclone"


def test_the_real_mountinfo_parses_completely():
    assert fsprim.parse_mountinfo(fsprim._read_proc("/proc/self/mountinfo"))


GOOD = line(28, 1, "254:0", "/", "ext4")


@pytest.mark.parametrize("bad", [
    "28 1 254:0 / / rw - ext4\n",                          # fewer than 10 fields
    "28 1 254:0 / / rw ext4 src rw x\n",                    # no separator
    "28 1 254:0 / / rw - ext4 - rw\n",                      # two separators
    "x 1 254:0 / / rw - ext4 src rw\n",                     # non-numeric mount ID
    "28 y 254:0 / / rw - ext4 src rw\n",                    # non-numeric parent ID
    "28 1 254 / / rw - ext4 src rw\n",                      # malformed major:minor
    "28 1 a:b / / rw - ext4 src rw\n",
    "-28 1 254:0 / / rw - ext4 src rw\n",
    "28 1 254:0 / / - ext4 src rw x\n",                     # separator before the options field
    "\n",                                                   # blank line
])
def test_any_unparseable_line_makes_the_whole_mountinfo_incomplete(bad):
    with pytest.raises(fsprim.Incomplete):
        fsprim.parse_mountinfo(GOOD + bad)


@pytest.mark.parametrize("text", ["", GOOD.rstrip("\n"), GOOD + line(28, 1, "0:5", "/b", "tmpfs")])
def test_empty_truncated_or_duplicate_mountinfo_is_incomplete(text):
    with pytest.raises(fsprim.Incomplete):
        fsprim.parse_mountinfo(text)


# --------------------------------------------------------------------------- strict fsync


def test_strict_fsync_succeeds_on_a_real_file(fsdir):
    fd = os.open(_path_of(fsdir) / "f", os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        os.write(fd, b"x")
        fsprim.strict_fsync(fd)
    finally:
        os.close(fd)


@pytest.mark.parametrize("code", [errno.EIO, errno.ENOSPC, errno.EINVAL, errno.EROFS, errno.EBADF, errno.EINTR])
def test_strict_fsync_propagates_every_error_unchanged(monkeypatch, code):
    err = OSError(code, os.strerror(code))
    calls = []

    def boom(fd):
        calls.append(fd)
        raise err
    monkeypatch.setattr(fsprim, "_fsync", boom)
    with pytest.raises(OSError) as exc:
        fsprim.strict_fsync(5)
    assert exc.value is err and calls == [5]  # one call, no retry, not wrapped


def test_strict_fsync_of_a_closed_descriptor_raises_for_real(fsdir):
    fd = os.open(_path_of(fsdir), os.O_RDONLY | os.O_DIRECTORY)
    os.close(fd)
    with pytest.raises(OSError) as exc:
        fsprim.strict_fsync(fd)
    assert exc.value.errno == errno.EBADF


# --------------------------------------------------------------------------- complete listing


def _populate(path: Path) -> None:
    (path / "a.json").write_bytes(b"A")
    (path / "sub").mkdir()
    os.symlink("a.json", path / "link")
    os.mkfifo(path / "fifo")
    os.link(path / "a.json", path / "hard")


def test_complete_listing_lists_every_entry_with_lstat_facts(fsdir, dfd):
    path = _path_of(fsdir)
    _populate(path)
    before = snap(path)
    listing = fsprim.complete_listing(dfd)
    assert snap(path) == before
    assert sorted(listing.entries) == ["a.json", "fifo", "hard", "link", "sub"]
    kinds = {n: e.kind for n, e in listing.entries.items()}
    assert kinds == {"a.json": "regular", "fifo": "fifo", "hard": "regular", "link": "symlink", "sub": "directory"}
    assert listing.entries["a.json"].nlink == 2
    assert listing.entries["a.json"].ino == listing.entries["hard"].ino == (path / "a.json").lstat().st_ino
    st = os.fstat(dfd)
    assert listing.folder == (st.st_dev, st.st_ino, st.st_nlink, st.st_mtime_ns, st.st_ctime_ns)


def test_complete_listing_of_an_empty_folder(dfd):
    assert fsprim.complete_listing(dfd).entries == {}


@pytest.mark.parametrize("field", ["st_dev", "st_ino", "st_nlink", "st_mtime_ns", "st_ctime_ns"])
def test_a_change_of_any_stability_field_makes_the_listing_incomplete(monkeypatch, dfd, field):
    real = os.fstat
    seen = []

    def fstat(fd):
        st = real(fd)
        seen.append(fd)
        return st if len(seen) == 1 else fake_stat(st, **{field: getattr(st, field) + 1})
    monkeypatch.setattr(fsprim, "_fstat", fstat)
    with pytest.raises(fsprim.Incomplete):
        fsprim.complete_listing(dfd)


def test_a_real_entry_added_during_the_listing_is_detected(monkeypatch, fsdir, dfd):
    path = _path_of(fsdir)
    (path / "a.json").write_bytes(b"A")
    os.utime(path, ns=(10**9, 10**9))  # an old folder time, so the new entry changes it
    real = fsprim._listdir

    def listdir(fd):
        names = real(fd)
        (path / "late.json").write_bytes(b"L")
        return names
    monkeypatch.setattr(fsprim, "_listdir", listdir)
    with pytest.raises(fsprim.Incomplete):
        fsprim.complete_listing(dfd)


@pytest.mark.parametrize("where", ["fstat_before", "listdir", "fstat_after", "lstat"])
@pytest.mark.parametrize("code", [errno.EIO, errno.ENOENT, errno.EACCES])
def test_any_error_during_the_listing_is_incomplete_with_its_errno(monkeypatch, fsdir, dfd, where, code):
    (_path_of(fsdir) / "a.json").write_bytes(b"A")
    count = {"fstat": 0}
    real_fstat, real_listdir, real_lstat = fsprim._fstat, fsprim._listdir, fsprim._lstat_at

    def fstat(fd):
        count["fstat"] += 1
        if (where == "fstat_before" and count["fstat"] == 1) or (where == "fstat_after" and count["fstat"] == 2):
            raise OSError(code, "injected")
        return real_fstat(fd)

    def listdir(fd):
        if where == "listdir":
            raise OSError(code, "injected")
        return real_listdir(fd)

    def lstat(fd, name):
        if where == "lstat":
            raise OSError(code, "injected")
        return real_lstat(fd, name)
    monkeypatch.setattr(fsprim, "_fstat", fstat)
    monkeypatch.setattr(fsprim, "_listdir", listdir)
    monkeypatch.setattr(fsprim, "_lstat_at", lstat)
    with pytest.raises(fsprim.Incomplete) as exc:
        fsprim.complete_listing(dfd)
    assert exc.value.errno == code


def test_an_entry_that_vanishes_before_its_lstat_is_incomplete(monkeypatch, fsdir, dfd):
    path = _path_of(fsdir)
    (path / "a.json").write_bytes(b"A")
    real = fsprim._lstat_at

    def lstat(fd, name):
        os.unlink(path / name)
        return real(fd, name)
    monkeypatch.setattr(fsprim, "_lstat_at", lstat)
    with pytest.raises(fsprim.Incomplete) as exc:
        fsprim.complete_listing(dfd)
    assert exc.value.errno == errno.ENOENT


# --------------------------------------------------------------------------- complete read


def test_complete_read_returns_bytes_and_identity(fsdir, dfd):
    path = _path_of(fsdir)
    data = os.urandom(3 * (1 << 20) + 17)  # several chunks
    (path / "f.json").write_bytes(data)
    before = snap(path)
    got = fsprim.complete_read(dfd, "f.json")
    assert snap(path) == before
    st = (path / "f.json").lstat()
    assert got.data == data
    assert (got.dev, got.ino, got.size, got.mtime_ns, got.ctime_ns) == \
        (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def test_complete_read_of_an_empty_file(fsdir, dfd):
    (_path_of(fsdir) / "e").write_bytes(b"")
    assert fsprim.complete_read(dfd, "e").data == b""


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo", "hardlink", "missing", "socket"])
def test_complete_read_refuses_anything_but_a_single_link_regular_file(fsdir, dfd, kind):
    path = _path_of(fsdir)
    (path / "real").write_bytes(b"R")
    target = path / "x"
    if kind == "symlink":
        os.symlink("real", target)
    elif kind == "directory":
        target.mkdir()
    elif kind == "fifo":
        os.mkfifo(target)
    elif kind == "hardlink":
        os.link(path / "real", target)
    elif kind == "socket":
        import socket
        s = socket.socket(socket.AF_UNIX)
        try:
            s.bind(str(target))
        finally:
            s.close()
    with pytest.raises(fsprim.Incomplete) as exc:
        fsprim.complete_read(dfd, "x")
    if kind == "missing":
        assert exc.value.errno == errno.ENOENT


def test_a_fifo_swapped_in_after_the_lstat_never_blocks_and_is_incomplete(monkeypatch, fsdir, dfd):
    path = _path_of(fsdir)
    os.mkfifo(path / "x")
    real = fsprim._lstat_at
    regular = stat.S_IFREG | 0o644

    def lstat(fd, name):
        return fake_stat(real(fd, name), st_mode=regular, st_nlink=1)
    monkeypatch.setattr(fsprim, "_lstat_at", lstat)
    with pytest.raises(fsprim.Incomplete):
        fsprim.complete_read(dfd, "x")


def test_a_file_that_grows_during_the_read_is_incomplete(monkeypatch, fsdir, dfd):
    path = _path_of(fsdir)
    (path / "f").write_bytes(b"A" * 100)
    real = fsprim._read
    done = []

    def read(fd, n):
        chunk = real(fd, n)
        if not done:
            done.append(1)
            with open(path / "f", "ab") as fh:
                fh.write(b"B")
        return chunk
    monkeypatch.setattr(fsprim, "_read", read)
    with pytest.raises(fsprim.Incomplete):
        fsprim.complete_read(dfd, "f")


def test_a_file_replaced_during_the_read_is_incomplete(monkeypatch, fsdir, dfd):
    path = _path_of(fsdir)
    (path / "f").write_bytes(b"A" * 100)
    (path / "g").write_bytes(b"A" * 100)
    real = fsprim._read
    done = []

    def read(fd, n):
        if not done:
            done.append(1)
            os.rename(path / "g", path / "f")  # the name now holds another inode
        return real(fd, n)
    monkeypatch.setattr(fsprim, "_read", read)
    with pytest.raises(fsprim.Incomplete):
        fsprim.complete_read(dfd, "f")


@pytest.mark.parametrize("field", ["st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns"])
def test_any_change_of_the_open_file_during_the_read_is_incomplete(monkeypatch, fsdir, dfd, field):
    (_path_of(fsdir) / "f").write_bytes(b"A" * 10)
    real = fsprim._fstat
    seen = []

    def fstat(fd):
        st = real(fd)
        seen.append(1)
        return st if len(seen) == 1 else fake_stat(st, **{field: getattr(st, field) + 1})
    monkeypatch.setattr(fsprim, "_fstat", fstat)
    with pytest.raises(fsprim.Incomplete):
        fsprim.complete_read(dfd, "f")


def test_an_open_identity_that_differs_from_the_lstat_is_incomplete(monkeypatch, fsdir, dfd):
    (_path_of(fsdir) / "f").write_bytes(b"A")
    real = fsprim._lstat_at
    seen = []

    def lstat(fd, name):
        st = real(fd, name)
        seen.append(1)
        return fake_stat(st, st_ino=st.st_ino + 1) if len(seen) == 1 else st
    monkeypatch.setattr(fsprim, "_lstat_at", lstat)
    with pytest.raises(fsprim.Incomplete):
        fsprim.complete_read(dfd, "f")


def test_a_final_lstat_naming_another_file_is_incomplete(monkeypatch, fsdir, dfd):
    (_path_of(fsdir) / "f").write_bytes(b"A")
    real = fsprim._lstat_at
    seen = []

    def lstat(fd, name):
        st = real(fd, name)
        seen.append(1)
        return st if len(seen) == 1 else fake_stat(st, st_ino=st.st_ino + 1)
    monkeypatch.setattr(fsprim, "_lstat_at", lstat)
    with pytest.raises(fsprim.Incomplete):
        fsprim.complete_read(dfd, "f")
    assert len(seen) == 2


@pytest.mark.parametrize("where", ["lstat", "open", "fstat", "read", "final_lstat"])
def test_any_error_during_the_read_is_incomplete_and_closes_the_descriptor(monkeypatch, fsdir, dfd, where):
    (_path_of(fsdir) / "f").write_bytes(b"A" * 10)
    real = {n: getattr(fsprim, n) for n in ("_lstat_at", "_open_at", "_fstat", "_read")}
    opened, closed, lstats = [], [], []

    def lstat(fd, name):
        lstats.append(1)
        if (where == "lstat" and len(lstats) == 1) or (where == "final_lstat" and len(lstats) == 2):
            raise OSError(errno.EIO, "injected")
        return real["_lstat_at"](fd, name)

    def open_at(fd, name, flags):
        if where == "open":
            raise OSError(errno.EIO, "injected")
        got = real["_open_at"](fd, name, flags)
        opened.append(got)
        return got

    def fstat(fd):
        if where == "fstat":
            raise OSError(errno.EIO, "injected")
        return real["_fstat"](fd)

    def read(fd, n):
        if where == "read":
            raise OSError(errno.EIO, "injected")
        return real["_read"](fd, n)
    real_close = fsprim._close

    def close(fd):
        closed.append(fd)
        real_close(fd)
    for n, f in (("_lstat_at", lstat), ("_open_at", open_at), ("_fstat", fstat), ("_read", read), ("_close", close)):
        monkeypatch.setattr(fsprim, n, f)
    with pytest.raises(fsprim.Incomplete) as exc:
        fsprim.complete_read(dfd, "f")
    assert exc.value.errno == errno.EIO
    assert opened == closed


def test_complete_read_opens_without_following_links_and_without_blocking(monkeypatch, fsdir, dfd):
    (_path_of(fsdir) / "f").write_bytes(b"A")
    flags = []
    real = fsprim._open_at
    monkeypatch.setattr(fsprim, "_open_at", lambda fd, n, f: (flags.append(f), real(fd, n, f))[1])
    fsprim.complete_read(dfd, "f")
    assert flags and flags[0] & os.O_NOFOLLOW and flags[0] & os.O_NONBLOCK
    assert flags[0] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC) == 0


@pytest.mark.parametrize("name", ["../outside.json", "subdir/file.json", "/etc/passwd", "", ".", "..",
                                  "a\x00b.json", "./f.json", "f.json/", None, b"f.json", 3, Path("f.json")])
def test_complete_read_refuses_a_name_that_is_not_one_plain_component_before_any_operation(monkeypatch, dfd, name):
    for helper in ("_lstat_at", "_open_at", "_fstat", "_read", "_close"):
        monkeypatch.setattr(fsprim, helper, lambda *a, _h=helper: pytest.fail(f"{_h} called for an invalid name"))
    with pytest.raises(ValueError):
        fsprim.complete_read(dfd, name)


def test_complete_read_name_errors_are_parameter_errors_not_syscall_failures(dfd):
    with pytest.raises(ValueError) as exc:
        fsprim.complete_read(dfd, "../outside.json")
    assert not isinstance(exc.value, (OSError, fsprim.FsprimError))


def _closing(monkeypatch, close_errno, **fail):
    """Patch close to really close once, then raise ``close_errno``; ``fail`` makes the named
    helper raise EIO. Returns the list of close attempts."""
    closes = []

    def close(fd):
        closes.append(fd)
        os.close(fd)
        raise OSError(close_errno, "injected close failure")
    monkeypatch.setattr(fsprim, "_close", close)
    for helper in fail:
        monkeypatch.setattr(fsprim, helper, lambda *a: (_ for _ in ()).throw(OSError(errno.EIO, "injected")))
    return closes


def test_a_close_failure_after_a_successful_read_is_incomplete_with_its_errno(monkeypatch, fsdir, dfd):
    (_path_of(fsdir) / "f").write_bytes(b"A" * 10)
    closes = _closing(monkeypatch, errno.EIO)
    with pytest.raises(fsprim.Incomplete) as exc:
        fsprim.complete_read(dfd, "f")
    assert exc.value.errno == errno.EIO and exc.value.close_errno == errno.EIO
    assert len(closes) == 1


def test_a_read_error_and_a_close_failure_are_both_kept(monkeypatch, fsdir, dfd):
    (_path_of(fsdir) / "f").write_bytes(b"A" * 10)
    closes = _closing(monkeypatch, errno.EBADF, _read=True)
    with pytest.raises(fsprim.Incomplete) as exc:
        fsprim.complete_read(dfd, "f")
    assert exc.value.errno == errno.EIO and exc.value.close_errno == errno.EBADF
    assert "read" in exc.value.reason and "clos" in exc.value.reason
    assert len(closes) == 1


def test_an_fstat_error_and_a_close_failure_are_both_kept(monkeypatch, fsdir, dfd):
    (_path_of(fsdir) / "f").write_bytes(b"A" * 10)
    closes = _closing(monkeypatch, errno.EINTR, _fstat=True)
    with pytest.raises(fsprim.Incomplete) as exc:
        fsprim.complete_read(dfd, "f")
    assert exc.value.errno == errno.EIO and exc.value.close_errno == errno.EINTR
    assert len(closes) == 1


def test_an_identity_mismatch_and_a_close_failure_are_both_kept(monkeypatch, fsdir, dfd):
    (_path_of(fsdir) / "f").write_bytes(b"A")
    real = fsprim._lstat_at
    monkeypatch.setattr(fsprim, "_lstat_at", lambda fd, n: fake_stat(real(fd, n), st_ino=real(fd, n).st_ino + 1))
    closes = _closing(monkeypatch, errno.EIO)
    with pytest.raises(fsprim.Incomplete) as exc:
        fsprim.complete_read(dfd, "f")
    assert exc.value.errno is None and exc.value.close_errno == errno.EIO
    assert "lstat" in exc.value.reason and "clos" in exc.value.reason
    assert len(closes) == 1


@pytest.mark.parametrize("code", [errno.EIO, errno.EINTR, errno.EBADF, errno.ENOSPC])
def test_no_close_failure_ever_returns_a_file_read_and_close_is_never_retried(monkeypatch, fsdir, dfd, code):
    (_path_of(fsdir) / "f").write_bytes(b"A" * 10)
    closes = _closing(monkeypatch, code)
    with pytest.raises(fsprim.Incomplete) as exc:
        fsprim.complete_read(dfd, "f")
    assert exc.value.close_errno == code and len(closes) == 1


def test_a_successful_read_closes_exactly_once(monkeypatch, fsdir, dfd):
    (_path_of(fsdir) / "f").write_bytes(b"A")
    closes = []
    real = fsprim._close
    monkeypatch.setattr(fsprim, "_close", lambda fd: (closes.append(fd), real(fd))[1])
    assert fsprim.complete_read(dfd, "f").data == b"A"
    assert len(closes) == 1


def test_an_interrupt_during_the_read_still_closes_once_and_propagates(monkeypatch, fsdir, dfd):
    (_path_of(fsdir) / "f").write_bytes(b"A")
    closes = []
    real = fsprim._close
    monkeypatch.setattr(fsprim, "_close", lambda fd: (closes.append(fd), real(fd))[1])
    monkeypatch.setattr(fsprim, "_read", lambda fd, n: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        fsprim.complete_read(dfd, "f")
    assert len(closes) == 1


def test_an_interrupt_and_a_close_failure_keep_both(monkeypatch, fsdir, dfd):
    (_path_of(fsdir) / "f").write_bytes(b"A")
    closes = _closing(monkeypatch, errno.EIO)
    monkeypatch.setattr(fsprim, "_read", lambda fd, n: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt) as exc:
        fsprim.complete_read(dfd, "f")
    assert exc.value.fsprim_close_error.errno == errno.EIO and len(closes) == 1


# --------------------------------------------------------------------------- write in full


def test_write_in_full_writes_everything_on_a_real_file(fsdir):
    data = os.urandom(5 * (1 << 20) + 3)
    fd = os.open(_path_of(fsdir) / "w", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        fsprim.write_in_full(fd, data)
    finally:
        os.close(fd)
    assert (_path_of(fsdir) / "w").read_bytes() == data


def test_short_writes_are_continued_not_retried(monkeypatch, fsdir):
    fd = os.open(_path_of(fsdir) / "w", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    sizes = iter([3, 2, 1, 10**9])
    calls = []

    def write(f, view):
        calls.append(bytes(view))
        return os.write(f, bytes(view)[:next(sizes)])
    monkeypatch.setattr(fsprim, "_write", write)
    try:
        fsprim.write_in_full(fd, b"abcdefghij")
    finally:
        os.close(fd)
    assert (_path_of(fsdir) / "w").read_bytes() == b"abcdefghij"
    assert calls == [b"abcdefghij", b"defghij", b"fghij", b"ghij"]  # each call asks only for the rest


def test_a_zero_return_stops_with_the_count_written(monkeypatch):
    returns = iter([4, 0])
    calls = []
    monkeypatch.setattr(fsprim, "_write", lambda fd, view: (calls.append(1), next(returns))[1])
    with pytest.raises(fsprim.WriteIncomplete) as exc:
        fsprim.write_in_full(9, b"abcdefgh")
    assert exc.value.written == 4 and exc.value.zero_return and exc.value.errno is None and len(calls) == 2


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EIO, errno.EINTR, errno.EDQUOT])
def test_an_error_after_partial_progress_stops_at_once_and_never_claims_zero_bytes(monkeypatch, code):
    calls = []

    def write(fd, view):
        calls.append(1)
        if len(calls) == 1:
            return 3
        raise OSError(code, "injected")
    monkeypatch.setattr(fsprim, "_write", write)
    with pytest.raises(fsprim.WriteIncomplete) as exc:
        fsprim.write_in_full(9, b"abcdefgh")
    assert exc.value.written == 3 and exc.value.errno == code and not exc.value.zero_return and len(calls) == 2


def test_an_error_on_the_first_call_reports_zero_confirmed_bytes_not_zero_reached(monkeypatch):
    monkeypatch.setattr(fsprim, "_write", lambda fd, view: (_ for _ in ()).throw(OSError(errno.EIO, "x")))
    with pytest.raises(fsprim.WriteIncomplete) as exc:
        fsprim.write_in_full(9, b"ab")
    assert exc.value.written == 0 and "not" in str(exc.value)  # the text never says nothing reached the file


def test_writing_nothing_makes_no_call(monkeypatch):
    monkeypatch.setattr(fsprim, "_write", lambda fd, view: pytest.fail("no write for empty data"))
    fsprim.write_in_full(9, b"")


# --------------------------------------------------------------------------- exchange


def _pair(path: Path):
    (path / "d1").mkdir()
    (path / "d2").mkdir()
    (path / "d1" / "f.json").write_bytes(b"ONE")
    (path / "d2" / "f.json").write_bytes(b"TWO")
    fds = [os.open(path / d, os.O_RDONLY | os.O_DIRECTORY) for d in ("d1", "d2")]
    return fds


def test_exchange_swaps_two_names_atomically_on_a_real_file_system(fsdir):
    path = _path_of(fsdir)
    a, b = _pair(path)
    try:
        ia, ib = (path / "d1" / "f.json").lstat().st_ino, (path / "d2" / "f.json").lstat().st_ino
        fsprim.exchange(a, "f.json", b, "f.json")
    finally:
        os.close(a)
        os.close(b)
    assert (path / "d1" / "f.json").read_bytes() == b"TWO"
    assert (path / "d2" / "f.json").read_bytes() == b"ONE"
    assert (path / "d1" / "f.json").lstat().st_ino == ib and (path / "d2" / "f.json").lstat().st_ino == ia


def test_exchange_within_one_folder(fsdir, dfd):
    path = _path_of(fsdir)
    (path / "a").write_bytes(b"A")
    (path / "b").write_bytes(b"B")
    fsprim.exchange(dfd, "a", dfd, "b")
    assert (path / "a").read_bytes() == b"B" and (path / "b").read_bytes() == b"A"


def test_exchange_with_a_missing_name_raises_enoent_and_changes_nothing(fsdir):
    path = _path_of(fsdir)
    a, b = _pair(path)
    os.unlink(path / "d2" / "f.json")
    before = snap(path)
    try:
        with pytest.raises(OSError) as exc:
            fsprim.exchange(a, "f.json", b, "f.json")
    finally:
        os.close(a)
        os.close(b)
    assert exc.value.errno == errno.ENOENT
    assert snap(path) == before


def test_exchange_with_a_too_long_name_raises_enametoolong(fsdir, dfd):
    (_path_of(fsdir) / "a").write_bytes(b"A")
    with pytest.raises(OSError) as exc:
        fsprim.exchange(dfd, "a", dfd, "n" * 300)
    assert exc.value.errno == errno.ENAMETOOLONG


def test_exchange_with_a_bad_descriptor_raises_ebadf(fsdir, dfd):
    (_path_of(fsdir) / "a").write_bytes(b"A")
    with pytest.raises(OSError) as exc:
        fsprim.exchange(-1, "a", dfd, "a")
    assert exc.value.errno == errno.EBADF


def test_exchange_across_file_systems_raises_exdev_and_changes_nothing():
    dirs = [Path(tempfile.mkdtemp(prefix="fsprim-", dir=r)) for r in (ROOTS["ext4"], ROOTS["tmpfs"])]
    fds = []
    try:
        for i, d in enumerate(dirs):
            (d / "f").write_bytes(b"%d" % i)
            fds.append(os.open(d, os.O_RDONLY | os.O_DIRECTORY))
        with pytest.raises(OSError) as exc:
            fsprim.exchange(fds[0], "f", fds[1], "f")
        assert exc.value.errno == errno.EXDEV
        assert [(d / "f").read_bytes() for d in dirs] == [b"0", b"1"]
    finally:
        for fd in fds:
            os.close(fd)
        for d in dirs:
            shutil.rmtree(d, ignore_errors=True)


class _SpyLibc:
    def __init__(self, result, err=0):
        self.calls, self.result, self.err = [], result, err

    def renameat2(self, *args):
        self.calls.append(args)
        ctypes.set_errno(self.err)
        return self.result


def test_exchange_makes_exactly_one_call_with_the_exchange_flag(monkeypatch):
    spy = _SpyLibc(0)
    monkeypatch.setattr(fsprim, "_renameat2", lambda: spy.renameat2)
    fsprim.exchange(3, "a.json", 4, "b.json")
    assert len(spy.calls) == 1
    args = spy.calls[0]
    assert (args[0], args[1], args[2], args[3], args[4]) == (3, b"a.json", 4, b"b.json", fsprim.RENAME_EXCHANGE)
    assert fsprim.RENAME_EXCHANGE == 2


@pytest.mark.parametrize("code", [errno.EIO, errno.EINTR, errno.ENOSYS, errno.EINVAL, errno.EROFS])
def test_an_exchange_error_is_raised_once_unclassified_and_never_retried(monkeypatch, code):
    spy = _SpyLibc(-1, code)
    monkeypatch.setattr(fsprim, "_renameat2", lambda: spy.renameat2)
    monkeypatch.setattr(os, "rename", lambda *a, **k: pytest.fail("no fallback"))
    monkeypatch.setattr(os, "replace", lambda *a, **k: pytest.fail("no fallback"))
    with pytest.raises(OSError) as exc:
        fsprim.exchange(3, "a", 4, "b")
    assert exc.value.errno == code and len(spy.calls) == 1
    assert not isinstance(exc.value, fsprim.FsprimError)


def test_a_failure_without_an_errno_is_incomplete_not_success(monkeypatch):
    spy = _SpyLibc(-1, 0)
    monkeypatch.setattr(fsprim, "_renameat2", lambda: spy.renameat2)
    with pytest.raises(fsprim.Incomplete):
        fsprim.exchange(3, "a", 4, "b")
    assert len(spy.calls) == 1


def test_a_missing_renameat2_symbol_is_an_unsupported_platform_and_no_fallback(monkeypatch, fsdir, dfd):
    path = _path_of(fsdir)
    (path / "a").write_bytes(b"A")
    (path / "b").write_bytes(b"B")
    monkeypatch.setattr(fsprim, "_libc", lambda: types.SimpleNamespace())
    with pytest.raises(fsprim.UnsupportedPlatform):
        fsprim.exchange(dfd, "a", dfd, "b")
    assert (path / "a").read_bytes() == b"A" and (path / "b").read_bytes() == b"B"


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "/a", "a\x00b", b"a", None, "a/"])
def test_exchange_refuses_a_name_that_is_not_one_plain_component_before_any_call(monkeypatch, name):
    spy = _SpyLibc(0)
    monkeypatch.setattr(fsprim, "_renameat2", lambda: spy.renameat2)
    with pytest.raises(ValueError):
        fsprim.exchange(3, name, 4, "ok")
    with pytest.raises(ValueError):
        fsprim.exchange(3, "ok", 4, name)
    assert spy.calls == []
