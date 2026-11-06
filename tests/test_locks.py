"""The library lock primitive and its D-1 classification (B7 Phase 2, S3-B2c-b, milestone b1b).

``locks`` holds the folder locks that later, separately authorized stages build on (design: frozen
revision 5 sections 2.2 and 3, revision 3 sections 2.2 to 2.4, plan revision 4.7 state machine
section 3.2). It has no caller yet, so no application behaviour changes. These tests pin:

* the lock objects: an ``flock`` on the sets folder's own descriptor (L-A, no lock file) and on the
  migration folder M (``.format1-backup``); M is opened relative to the sets folder, never through
  a link, and is **never created**;
* exclusive operations: the sets lock first, then the M lock whenever M exists (found and opened
  here unless the caller passes the descriptor it opened), both exclusive and non-blocking; normal
  writers: both shared and non-blocking, M only when it exists, and a re-check for an M that
  appeared later;
* every request is non-blocking (D-2.1) and made once: no retry, no waiting;
* the path identity checks after locking: ``stat(sets_path)`` against ``fstat(sets_fd)`` and
  ``fstatat(sets_fd, M, AT_SYMLINK_NOFOLLOW)`` against ``fstat(M_fd)``;
* the D-1 classification: only ``EWOULDBLOCK``/``EAGAIN`` is contention (``refused_busy``); every
  other lock error, an unverifiable lock state and an identity mismatch are
  ``refused_unverifiable``; the two are never confused. A platform without the primitives is
  ``refused_platform`` before any call (fail closed, D-2.5 (ii));
* a refusal leaves no lock held by the caller and nothing written; a killed holder leaves nothing
  behind.

Real I/O and real holder processes on both supported file systems: ext4 (``/tmp``) and tmpfs
(``/dev/shm``). Lock errors are injected through the module's narrow helpers. Synthetic folders
only.
"""

import ast
import contextlib
import errno
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX primitives; Windows is refused")

try:
    import fcntl
except ImportError:  # Windows: the module is skipped, but must still import
    fcntl = None

from lpbfstudio import locks  # noqa: E402

ROOTS = {"ext4": "/tmp", "tmpfs": "/dev/shm"}
SRC = str(Path(locks.__file__).resolve().parents[1])
M = ".format1-backup"
CONTENTION = (errno.EWOULDBLOCK, errno.EAGAIN)
# looked up by name so that the module still imports where an errno is missing (it is skipped there)
LOCK_ERRORS = tuple(getattr(errno, n) for n in (
    "ENOLCK", "EOPNOTSUPP", "EINVAL", "EIO", "EBADF", "EACCES", "ENOENT", "EINTR", "ENOMEM",
    "EINPROGRESS", "EALREADY") if hasattr(errno, n))


# --------------------------------------------------------------------------- helpers


@pytest.fixture(params=sorted(ROOTS))
def sets(request):
    """A new sets folder ``<tmp>/lib/sets`` on a real ext4 or tmpfs file system (never skipped)."""
    root = Path(tempfile.mkdtemp(prefix="locks-", dir=ROOTS[request.param]))
    path = root / "lib" / "sets"
    path.mkdir(parents=True)
    (path / "A.json").write_bytes(b'{"name": "A"}\n')
    try:
        yield path
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def with_m(sets):
    (sets / M).mkdir()
    (sets / M / "records").mkdir()
    return sets


@contextlib.contextmanager
def opened(path, flags=None):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY if flags is None else flags)
    try:
        yield fd
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def snap(root: Path) -> dict:
    """Every entry under ``root``'s parent: type, inode, size and bytes (or link target)."""
    out = {}
    base = root.parent
    for dirpath, dirs, files in os.walk(base):
        for name in dirs + files:
            p = Path(dirpath) / name
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                body = os.readlink(p)
            elif stat.S_ISREG(st.st_mode):
                body = p.read_bytes()
            else:
                body = None
            out[str(p.relative_to(base))] = (stat.S_IFMT(st.st_mode), st.st_ino, st.st_size, body)
    return out


def free(path: Path, op=None) -> bool:
    """True when a new open file description (another holder) can take ``op`` on ``path`` now."""
    op = fcntl.LOCK_EX if op is None else op
    with opened(path) as fd:
        try:
            fcntl.flock(fd, op | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True


HOLDER = """
import fcntl, os, sys
fds = []
for spec in sys.argv[1:]:
    path, mode = spec.rsplit("=", 1)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    fcntl.flock(fd, {"sh": fcntl.LOCK_SH, "ex": fcntl.LOCK_EX}[mode] | fcntl.LOCK_NB)
    fds.append(fd)
print("held", flush=True)
cmd = sys.stdin.readline().strip()
if cmd == "die":
    os._exit(9)
"""


@contextlib.contextmanager
def holder(*specs):
    """Another process holding ``flock`` locks: specs are ``(path, "sh"|"ex")``."""
    args = [f"{p}={m}" for p, m in specs]
    p = subprocess.Popen([sys.executable, "-c", HOLDER, *args], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    try:
        assert p.stdout.readline().strip() == b"held"
        yield p
    finally:
        with contextlib.suppress(OSError, ValueError):
            p.stdin.close()
        p.wait(10)
        p.stdout.close()


def refused(fn, *args, **kw) -> locks.LockRefused:
    with pytest.raises(locks.LockRefused) as info:
        fn(*args, **kw)
    return info.value


class Recorder:
    """Wraps ``locks._flock``: records (fd, op) and can raise an errno on a chosen descriptor."""

    def __init__(self, monkeypatch, fail_fd=None, err=None, exc=None):
        self.calls = []
        self.real = locks._flock
        self.fail_fd, self.err, self.exc = fail_fd, err, exc
        monkeypatch.setattr(locks, "_flock", self)

    def __call__(self, fd, op):
        self.calls.append((fd, op))
        if fd == self.fail_fd and not op & fcntl.LOCK_UN:
            if self.exc is not None:
                raise self.exc
            raise OSError(self.err, os.strerror(self.err))
        return self.real(fd, op)

    def locks_taken(self):
        return [(fd, op) for fd, op in self.calls if not op & fcntl.LOCK_UN]


def error_of(err):
    """The exception the OS gives for ``err`` (Python maps some errnos to subclasses)."""
    return OSError(err, os.strerror(err))


# ================================================================ module boundaries


def test_the_module_imports_no_application_module_and_has_no_caller():
    tree = ast.parse(Path(locks.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(("." * node.level) + (node.module or ""))
    assert not any(n.startswith(".") or n.startswith("lpbfstudio") for n in imported), imported
    pkg = Path(locks.__file__).parent
    for py in pkg.rglob("*.py"):
        if py.name == "locks.py":
            continue
        text = py.read_text(encoding="utf-8")
        assert "locks" not in {a.name for n in ast.walk(ast.parse(text))
                               if isinstance(n, ast.ImportFrom) for a in n.names}, py
        assert "lpbfstudio.locks" not in text and "import locks" not in text, py


def test_the_m_name_is_the_one_upgrade_and_reconcile_lock():
    from lpbfstudio import upgrade as up
    assert locks.M_NAME == up.MIGRATION_DIR == M


# ================================================================ D-1 classification


@pytest.mark.parametrize("err", CONTENTION)
def test_contention_is_busy_and_never_unverifiable(err):
    exc = error_of(err)
    assert isinstance(exc, BlockingIOError)
    assert locks.classify(exc) is locks.Refusal.BUSY
    assert locks.RESULT[locks.Refusal.BUSY] == "refused_busy"


@pytest.mark.parametrize("err", LOCK_ERRORS)
def test_every_other_lock_error_is_unverifiable_and_never_busy(err):
    # EINPROGRESS and EALREADY are BlockingIOError too: the errno decides, not the exception type
    assert locks.classify(error_of(err)) is locks.Refusal.UNVERIFIABLE
    assert locks.RESULT[locks.Refusal.UNVERIFIABLE] == "refused_unverifiable"


def test_an_oserror_without_an_errno_is_unverifiable():
    assert locks.classify(OSError("no errno")) is locks.Refusal.UNVERIFIABLE
    assert locks.classify(BlockingIOError()) is locks.Refusal.UNVERIFIABLE


def test_the_result_names_are_exactly_the_d1_vocabulary():
    assert locks.RESULT == {locks.Refusal.BUSY: "refused_busy",
                            locks.Refusal.UNVERIFIABLE: "refused_unverifiable",
                            locks.Refusal.UNSUPPORTED: "refused_platform"}
    r = locks.LockRefused(locks.Refusal.BUSY, locks.Step.SETS_LOCK, errno.EWOULDBLOCK, "held")
    assert r.result == "refused_busy" and r.busy and r.errno == errno.EWOULDBLOCK
    r = locks.LockRefused(locks.Refusal.UNVERIFIABLE, locks.Step.M_IDENTITY, None, "changed")
    assert r.result == "refused_unverifiable" and not r.busy and r.errno is None
    assert "changed" in str(r)


# ================================================================ platform (fail closed)


def test_this_platform_has_the_primitives():
    assert locks.platform_problem() is None


@pytest.mark.parametrize("attr", ["fcntl", "flock"])
def test_a_platform_without_flock_is_refused_before_any_call(sets, monkeypatch, attr):
    if attr == "fcntl":
        monkeypatch.setattr(locks, "fcntl", None)
    else:
        monkeypatch.setattr(locks, "fcntl", type("NoFlock", (), {"LOCK_SH": 1, "LOCK_EX": 2,
                                                                 "LOCK_NB": 4, "LOCK_UN": 8}))
    calls = []
    for name in ("_flock", "_fstat", "_stat_path", "_lstat_at", "_open_at"):
        monkeypatch.setattr(locks, name, lambda *a, _n=name: calls.append(_n))
    assert "flock" in locks.platform_problem()
    with opened(sets) as fd:
        for fn in (lambda: locks.acquire_exclusive(sets, fd),
                   lambda: locks.acquire_writer(sets, fd),
                   lambda: locks.open_sets(sets)):
            r = refused(fn)
            assert r.refusal is locks.Refusal.UNSUPPORTED and r.result == "refused_platform"
            assert r.step is locks.Step.PLATFORM
    assert calls == []


@pytest.mark.parametrize("flag", ["O_DIRECTORY", "O_NOFOLLOW"])
def test_a_platform_without_safe_open_flags_is_refused(sets, monkeypatch, flag):
    monkeypatch.delattr(locks.os, flag)
    assert flag in locks.platform_problem()
    with opened(sets, os.O_RDONLY) as fd:
        assert refused(locks.acquire_writer, sets, fd).result == "refused_platform"


def test_a_platform_without_descriptor_relative_stat_is_refused(sets, monkeypatch):
    monkeypatch.setattr(locks.os, "supports_dir_fd", set())
    assert "dir_fd" in locks.platform_problem()
    with opened(sets) as fd:
        assert refused(locks.acquire_exclusive, sets, fd).result == "refused_platform"


# ================================================================ open_sets


def test_open_sets_returns_a_folder_descriptor_of_the_path(sets):
    fd = locks.open_sets(sets)
    try:
        st = os.fstat(fd)
        assert stat.S_ISDIR(st.st_mode)
        assert (st.st_dev, st.st_ino) == (os.stat(sets).st_dev, os.stat(sets).st_ino)
        assert os.get_inheritable(fd) is False
    finally:
        os.close(fd)


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_open_sets_refuses_what_is_not_a_folder(sets, kind):
    target = sets / "nope"
    if kind == "file":
        target.write_bytes(b"x")
    before = snap(sets)
    r = refused(locks.open_sets, target)
    assert r.refusal is locks.Refusal.UNVERIFIABLE and r.step is locks.Step.SETS_FOLDER
    assert r.errno in (errno.ENOENT, errno.ENOTDIR)
    assert snap(sets) == before


# ================================================================ exclusive operations


def test_exclusive_takes_the_sets_lock_then_the_m_lock_nonblocking(with_m, monkeypatch):
    rec = Recorder(monkeypatch)
    with opened(with_m) as sfd, opened(with_m / M) as mfd:
        held = locks.acquire_exclusive(with_m, sfd, mfd)
        try:
            assert rec.locks_taken() == [(sfd, fcntl.LOCK_EX | fcntl.LOCK_NB),
                                         (mfd, fcntl.LOCK_EX | fcntl.LOCK_NB)]
            assert held.mode is locks.Mode.EXCLUSIVE and held.sets_fd == sfd and held.m_fd == mfd
            assert not free(with_m, fcntl.LOCK_SH) and not free(with_m / M, fcntl.LOCK_SH)
        finally:
            held.release()
        assert free(with_m) and free(with_m / M)
        os.fstat(sfd), os.fstat(mfd)  # the caller's descriptors stay open


def test_exclusive_without_m_locks_only_the_sets_folder_and_creates_nothing(sets, monkeypatch):
    rec = Recorder(monkeypatch)
    before = snap(sets)
    with opened(sets) as sfd:
        with locks.acquire_exclusive(sets, sfd) as held:
            assert held.m_fd is None
            assert rec.locks_taken() == [(sfd, fcntl.LOCK_EX | fcntl.LOCK_NB)]
            assert not free(sets, fcntl.LOCK_SH)
        assert free(sets)
    assert snap(sets) == before and not (sets / M).exists()


def test_m_can_be_locked_later_under_the_held_sets_lock(sets, monkeypatch):
    # an upgrade creates M only while it already holds the exclusive sets lock (revision 3 §2.2)
    with opened(sets) as sfd:
        with locks.acquire_exclusive(sets, sfd) as held:
            (sets / M).mkdir()
            with opened(sets / M) as mfd:
                rec = Recorder(monkeypatch)
                held.lock_m(mfd)
                assert rec.locks_taken() == [(mfd, fcntl.LOCK_EX | fcntl.LOCK_NB)]
                assert held.m_fd == mfd and not free(sets / M, fcntl.LOCK_SH)
                held.release()
                assert free(sets / M) and free(sets)


@pytest.mark.parametrize("mode", ["sh", "ex"])
def test_exclusive_is_busy_when_another_process_holds_the_sets_folder(with_m, mode):
    before = snap(with_m)
    with holder((with_m, mode)), opened(with_m) as sfd, opened(with_m / M) as mfd:
        r = refused(locks.acquire_exclusive, with_m, sfd, mfd)
        assert r.refusal is locks.Refusal.BUSY and r.result == "refused_busy"
        assert r.step is locks.Step.SETS_LOCK and r.errno in CONTENTION
        assert free(with_m / M)  # M was never locked
    assert snap(with_m) == before


@pytest.mark.parametrize("mode", ["sh", "ex"])
def test_exclusive_is_busy_at_m_and_releases_the_sets_lock(with_m, mode):
    before = snap(with_m)
    with holder((with_m / M, mode)), opened(with_m) as sfd, opened(with_m / M) as mfd:
        r = refused(locks.acquire_exclusive, with_m, sfd, mfd)
        assert r.refusal is locks.Refusal.BUSY and r.step is locks.Step.M_LOCK
        assert free(with_m)  # the sets lock taken by this call was released
    assert snap(with_m) == before


def test_an_old_build_upgrade_holding_only_m_makes_exclusive_busy(with_m):
    # revision 3 §2.6 test 8: an operation entering through M only (upgrade.library_lock) excludes us
    code = ("import sys\nfrom lpbfstudio import upgrade as up\n"
            "lock = up.library_lock(sys.argv[1]); lock.__enter__()\n"
            "print('held', flush=True); sys.stdin.read()\n")
    env = {**os.environ, "PYTHONPATH": SRC, "PYTHONDONTWRITEBYTECODE": "1"}
    p = subprocess.Popen([sys.executable, "-c", code, str(with_m)], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, env=env)
    try:
        assert p.stdout.readline().strip() == b"held"
        with opened(with_m) as sfd, opened(with_m / M) as mfd:
            r = refused(locks.acquire_exclusive, with_m, sfd, mfd)
            assert r.busy and r.step is locks.Step.M_LOCK
            w = refused(locks.acquire_writer, with_m, sfd)
            assert w.busy and w.step is locks.Step.M_LOCK
            assert free(with_m)
    finally:
        p.stdin.close()
        p.wait(10)
        p.stdout.close()


def test_upgrade_library_lock_is_busy_while_an_exclusive_or_writer_holds_m(with_m):
    from lpbfstudio import upgrade as up
    with opened(with_m) as sfd, opened(with_m / M) as mfd:
        for acquire in (lambda: locks.acquire_exclusive(with_m, sfd, mfd),
                        lambda: locks.acquire_writer(with_m, sfd)):
            with acquire():
                with pytest.raises(up.LibraryBusy):
                    with up.library_lock(with_m):
                        pass
            with up.library_lock(with_m):
                pass


@pytest.mark.parametrize("err", CONTENTION + LOCK_ERRORS)
@pytest.mark.parametrize("where", ["sets", "m"])
def test_exclusive_lock_errors_follow_d1_and_leave_nothing_held(with_m, monkeypatch, err, where):
    before = snap(with_m)
    with opened(with_m) as sfd, opened(with_m / M) as mfd:
        rec = Recorder(monkeypatch, fail_fd=sfd if where == "sets" else mfd, err=err)
        r = refused(locks.acquire_exclusive, with_m, sfd, mfd)
        busy = err in CONTENTION
        assert r.busy is busy
        assert r.result == ("refused_busy" if busy else "refused_unverifiable")
        assert r.step is (locks.Step.SETS_LOCK if where == "sets" else locks.Step.M_LOCK)
        assert r.errno == err
        taken = rec.locks_taken()
        # each lock is requested once: no retry, no fallback to a blocking request
        assert len(taken) == (1 if where == "sets" else 2)
        assert all(op & fcntl.LOCK_NB for _, op in taken)
        assert free(with_m) and free(with_m / M)
        os.fstat(sfd), os.fstat(mfd)
    assert snap(with_m) == before


@pytest.mark.parametrize("where", ["sets", "m"])
def test_an_interrupt_while_locking_releases_and_propagates(with_m, monkeypatch, where):
    with opened(with_m) as sfd, opened(with_m / M) as mfd:
        Recorder(monkeypatch, fail_fd=sfd if where == "sets" else mfd, exc=KeyboardInterrupt())
        with pytest.raises(KeyboardInterrupt):
            locks.acquire_exclusive(with_m, sfd, mfd)
        assert free(with_m) and free(with_m / M)


def test_a_sets_descriptor_that_is_not_a_folder_is_unverifiable(sets, monkeypatch):
    rec = Recorder(monkeypatch)
    with opened(sets / "A.json", os.O_RDONLY) as fd:
        for fn in (locks.acquire_exclusive, locks.acquire_writer):
            r = refused(fn, sets, fd)
            assert r.refusal is locks.Refusal.UNVERIFIABLE and r.step is locks.Step.SETS_FOLDER
    assert rec.calls == []


def test_an_fstat_error_on_the_sets_descriptor_is_unverifiable(sets, monkeypatch):
    def bad(fd):
        raise OSError(errno.EIO, "fstat failed")
    monkeypatch.setattr(locks, "_fstat", bad)
    with opened(sets) as fd:
        r = refused(locks.acquire_exclusive, sets, fd)
        assert r.result == "refused_unverifiable" and r.errno == errno.EIO
        assert free(sets)


# ================================================================ path identity


def test_a_renamed_and_replaced_sets_folder_is_unverifiable_and_untouched(sets, monkeypatch):
    # revision 3 §2.6 test 7: the lock follows the inode, so the identity check must catch a swap
    other = sets.parent / "other"
    real = locks._flock

    def swap(fd, op):
        real(fd, op)
        if not op & fcntl.LOCK_UN and not other.exists():
            os.rename(sets, sets.parent / "moved")
            sets.mkdir()
            (sets / "B.json").write_bytes(b"new")
            other.mkdir()
    monkeypatch.setattr(locks, "_flock", swap)
    with opened(sets) as sfd:
        r = refused(locks.acquire_exclusive, sets, sfd)
        assert r.refusal is locks.Refusal.UNVERIFIABLE and r.step is locks.Step.SETS_IDENTITY
        assert free(sets.parent / "moved")
    assert sorted(os.listdir(sets)) == ["B.json"] and (sets / "B.json").read_bytes() == b"new"
    assert free(sets)


@pytest.mark.parametrize("kind", ["missing", "error", "different"])
def test_a_sets_path_that_no_longer_names_the_folder_is_unverifiable(sets, monkeypatch, kind):
    real = os.stat(sets)

    def fake(path):
        if kind == "missing":
            raise FileNotFoundError(errno.ENOENT, "gone")
        if kind == "error":
            raise OSError(errno.EIO, "stat failed")
        return os.stat(sets / "A.json")  # same device, another inode
    monkeypatch.setattr(locks, "_stat_path", fake)
    for fn in (locks.acquire_exclusive, locks.acquire_writer):
        with opened(sets) as sfd:
            r = refused(fn, sets, sfd)
            assert r.result == "refused_unverifiable" and r.step is locks.Step.SETS_IDENTITY
            assert free(sets)
    assert os.stat(sets).st_ino == real.st_ino


def test_the_identity_compares_device_and_inode(sets, monkeypatch):
    real = os.stat(sets)

    class Other:
        st_dev, st_ino, st_mode = real.st_dev + 1, real.st_ino, real.st_mode
    monkeypatch.setattr(locks, "_stat_path", lambda p: Other)
    with opened(sets) as sfd:
        assert refused(locks.acquire_exclusive, sets, sfd).step is locks.Step.SETS_IDENTITY


def test_the_sets_path_is_followed_once_like_the_user_gave_it(sets):
    link = sets.parent / "link-to-sets"
    link.symlink_to(sets, target_is_directory=True)
    with opened(link) as sfd:
        with locks.acquire_exclusive(link, sfd):
            pass


def test_a_replaced_m_is_unverifiable_and_nothing_is_held(with_m, monkeypatch):
    real = locks._flock

    def swap(fd, op):
        real(fd, op)
        if fd == state["m"] and not op & fcntl.LOCK_UN and not state["done"]:
            os.rename(with_m / M, with_m / "old-m")
            (with_m / M).mkdir()
            state["done"] = True
    state = {"done": False}
    monkeypatch.setattr(locks, "_flock", swap)
    with opened(with_m) as sfd, opened(with_m / M) as mfd:
        state["m"] = mfd
        r = refused(locks.acquire_exclusive, with_m, sfd, mfd)
        assert r.refusal is locks.Refusal.UNVERIFIABLE and r.step is locks.Step.M_IDENTITY
        assert free(with_m) and free(with_m / "old-m") and free(with_m / M)
    assert os.listdir(with_m / M) == []


@pytest.mark.parametrize("kind", ["missing", "link", "error"])
def test_an_m_path_that_no_longer_names_m_is_unverifiable(with_m, monkeypatch, kind):
    if kind == "link":
        os.rename(with_m / M, with_m / "real-m")
        (with_m / M).symlink_to("real-m")
        mpath = with_m / "real-m"
    else:
        mpath = with_m / M
    if kind == "missing":
        monkeypatch.setattr(locks, "_lstat_at", lambda d, n: (_ for _ in ()).throw(
            FileNotFoundError(errno.ENOENT, "gone")))
    if kind == "error":
        monkeypatch.setattr(locks, "_lstat_at", lambda d, n: (_ for _ in ()).throw(
            OSError(errno.EIO, "fstatat failed")))
    with opened(with_m) as sfd, opened(mpath) as mfd:
        r = refused(locks.acquire_exclusive, with_m, sfd, mfd)
        assert r.result == "refused_unverifiable" and r.step is locks.Step.M_IDENTITY
        assert free(with_m) and free(mpath)


def test_m_identity_is_checked_relative_to_the_sets_descriptor_without_following(with_m, monkeypatch):
    seen = []
    real = locks._lstat_at

    def spy(dir_fd, name):
        seen.append((dir_fd, name))
        return real(dir_fd, name)
    monkeypatch.setattr(locks, "_lstat_at", spy)
    with opened(with_m) as sfd, opened(with_m / M) as mfd:
        with locks.acquire_exclusive(with_m, sfd, mfd):
            pass
    assert seen == [(sfd, M)]


def test_verify_repeats_both_identity_checks(with_m):
    # exclusive operations repeat the check at their final re-check (revision 3 §2.3; R9)
    with opened(with_m) as sfd, opened(with_m / M) as mfd:
        with locks.acquire_exclusive(with_m, sfd, mfd) as held:
            held.verify()
            os.rename(with_m / M, with_m / "old-m")
            (with_m / M).mkdir()
            r = refused(held.verify)
            assert r.step is locks.Step.M_IDENTITY and r.result == "refused_unverifiable"
            os.rmdir(with_m / M)
            os.rename(with_m / "old-m", with_m / M)
            held.verify()
            os.rename(with_m, with_m.parent / "moved")
            try:
                r = refused(held.verify)
                assert r.step is locks.Step.SETS_IDENTITY
            finally:
                os.rename(with_m.parent / "moved", with_m)
        assert free(with_m) and free(with_m / M)


# ================================================================ normal writers


def test_a_writer_takes_shared_locks_on_sets_then_m(with_m, monkeypatch):
    rec = Recorder(monkeypatch)
    before = snap(with_m)
    with opened(with_m) as sfd:
        with locks.acquire_writer(with_m, sfd) as held:
            assert held.mode is locks.Mode.SHARED and held.m_fd is not None
            taken = rec.locks_taken()
            assert taken == [(sfd, fcntl.LOCK_SH | fcntl.LOCK_NB),
                             (held.m_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)]
            st = os.fstat(held.m_fd)
            assert (st.st_dev, st.st_ino) == (os.lstat(with_m / M).st_dev, os.lstat(with_m / M).st_ino)
            # another writer is admitted; an exclusive operation is not
            assert free(with_m, fcntl.LOCK_SH) and free(with_m / M, fcntl.LOCK_SH)
            assert not free(with_m) and not free(with_m / M)
            mfd = held.m_fd
        with pytest.raises(OSError):
            os.fstat(mfd)  # the writer's own M descriptor is closed on release
        assert free(with_m) and free(with_m / M)
    assert snap(with_m) == before


def test_two_writers_never_exclude_each_other(with_m):
    with holder((with_m, "sh"), (with_m / M, "sh")), opened(with_m) as sfd:
        with locks.acquire_writer(with_m, sfd) as held:
            assert held.m_fd is not None


def test_a_writer_with_m_absent_locks_only_sets_and_never_creates_m(sets, monkeypatch):
    rec = Recorder(monkeypatch)
    before = snap(sets)
    with opened(sets) as sfd:
        with locks.acquire_writer(sets, sfd) as held:
            assert held.m_fd is None
            assert rec.locks_taken() == [(sfd, fcntl.LOCK_SH | fcntl.LOCK_NB)]
            assert held.recheck_m() is False
    assert snap(sets) == before and not (sets / M).exists()


@pytest.mark.parametrize("held_by", ["sets", "m"])
def test_a_writer_is_busy_while_an_exclusive_operation_holds_either_lock(with_m, held_by):
    before = snap(with_m)
    target = with_m if held_by == "sets" else with_m / M
    with holder((target, "ex")), opened(with_m) as sfd:
        r = refused(locks.acquire_writer, with_m, sfd)
        assert r.busy and r.result == "refused_busy"
        assert r.step is (locks.Step.SETS_LOCK if held_by == "sets" else locks.Step.M_LOCK)
        assert free(with_m) if held_by == "m" else free(with_m / M)
    assert snap(with_m) == before


@pytest.mark.parametrize("err", CONTENTION + LOCK_ERRORS)
@pytest.mark.parametrize("where", ["sets", "m"])
def test_writer_lock_errors_fail_closed_by_d1(with_m, monkeypatch, err, where):
    # D-2.5 (ii): a writer that cannot lock refuses; it never proceeds unlocked
    before = snap(with_m)
    opened_m = []
    real_open = locks._open_at

    def spy_open(dir_fd, name, flags):
        fd = real_open(dir_fd, name, flags)
        opened_m.append(fd)
        if where == "m":
            rec.fail_fd = fd
        return fd
    monkeypatch.setattr(locks, "_open_at", spy_open)
    with opened(with_m) as sfd:
        rec = Recorder(monkeypatch, fail_fd=sfd if where == "sets" else None, err=err)
        r = refused(locks.acquire_writer, with_m, sfd)
        assert r.busy is (err in CONTENTION) and r.errno == err
        assert r.step is (locks.Step.SETS_LOCK if where == "sets" else locks.Step.M_LOCK)
        assert len(rec.locks_taken()) == (1 if where == "sets" else 2)
        assert free(with_m) and free(with_m / M)
        os.fstat(sfd)
    for fd in opened_m:
        with pytest.raises(OSError):
            os.fstat(fd)  # the M descriptor opened by the writer was closed
    assert snap(with_m) == before


@pytest.mark.parametrize("kind", ["link", "file", "link-to-nothing"])
def test_a_writer_refuses_an_m_that_is_not_a_plain_folder(sets, kind):
    if kind == "link":
        (sets.parent / "elsewhere").mkdir()
        (sets / M).symlink_to(sets.parent / "elsewhere", target_is_directory=True)
    elif kind == "file":
        (sets / M).write_bytes(b"not a folder")
    else:
        (sets / M).symlink_to(sets.parent / "nothing")
    before = snap(sets)
    with opened(sets) as sfd:
        r = refused(locks.acquire_writer, sets, sfd)
        assert r.refusal is locks.Refusal.UNVERIFIABLE and r.step is locks.Step.M_OPEN
        assert r.errno in (errno.ELOOP, errno.ENOTDIR)
        assert free(sets)
    assert snap(sets) == before
    if kind == "link":
        assert free(sets.parent / "elsewhere")


def test_a_writer_m_open_error_other_than_absent_is_unverifiable(with_m, monkeypatch):
    def bad(dir_fd, name, flags):
        raise PermissionError(errno.EACCES, "denied")
    monkeypatch.setattr(locks, "_open_at", bad)
    with opened(with_m) as sfd:
        r = refused(locks.acquire_writer, with_m, sfd)
        assert r.result == "refused_unverifiable" and r.step is locks.Step.M_OPEN
        assert r.errno == errno.EACCES and free(with_m)


def test_the_writer_opens_m_relative_to_sets_never_through_a_link(with_m, monkeypatch):
    seen = []
    real = locks._open_at

    def spy(dir_fd, name, flags):
        seen.append((dir_fd, name, flags))
        return real(dir_fd, name, flags)
    monkeypatch.setattr(locks, "_open_at", spy)
    with opened(with_m) as sfd:
        with locks.acquire_writer(with_m, sfd):
            pass
    assert len(seen) == 1
    dir_fd, name, flags = seen[0]
    assert dir_fd == sfd and name == M
    assert flags & os.O_DIRECTORY and flags & os.O_NOFOLLOW and not flags & os.O_CREAT
    assert flags & getattr(os, "O_CLOEXEC", 0) == getattr(os, "O_CLOEXEC", 0)
    assert flags & os.O_ACCMODE == os.O_RDONLY


def test_a_writer_m_identity_mismatch_is_unverifiable(with_m, monkeypatch):
    monkeypatch.setattr(locks, "_lstat_at", lambda d, n: os.stat(with_m / "A.json"))
    with opened(with_m) as sfd:
        r = refused(locks.acquire_writer, with_m, sfd)
        assert r.step is locks.Step.M_IDENTITY and r.result == "refused_unverifiable"
        assert free(with_m) and free(with_m / M)


# ---------------------------------------------------------------- pre-replace re-check (rev5 §2.2 step 3)


def test_recheck_with_m_still_absent_writes_nothing(sets):
    before = snap(sets)
    with opened(sets) as sfd, locks.acquire_writer(sets, sfd) as held:
        assert held.recheck_m() is False and held.recheck_m() is False
        assert held.m_fd is None
    assert snap(sets) == before


def test_recheck_locks_an_m_that_appeared_and_is_free(sets, monkeypatch):
    with opened(sets) as sfd, locks.acquire_writer(sets, sfd) as held:
        (sets / M).mkdir()
        rec = Recorder(monkeypatch)
        assert held.recheck_m() is True
        assert rec.locks_taken() == [(held.m_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)]
        assert not free(sets / M) and free(sets / M, fcntl.LOCK_SH)
        assert held.recheck_m() is True and len(rec.locks_taken()) == 1  # already held
    assert free(sets / M)


def test_recheck_is_busy_when_an_upgrade_created_and_holds_m(sets):
    # rev5 §2.2 step 3 / test 10 (b): the caller abandons its save
    with opened(sets) as sfd, locks.acquire_writer(sets, sfd) as held:
        (sets / M).mkdir()
        with holder((sets / M, "ex")):
            r = refused(held.recheck_m)
            assert r.busy and r.step is locks.Step.M_LOCK
            assert held.m_fd is None
            assert not free(sets)  # the writer still holds its sets lock until it releases
        assert held.recheck_m() is True  # a later re-check may succeed; the caller decides
    assert free(sets) and free(sets / M)


@pytest.mark.parametrize("kind", ["link", "file", "lstat-error", "open-error", "lock-error"])
def test_recheck_failures_are_unverifiable(sets, monkeypatch, kind):
    with opened(sets) as sfd, locks.acquire_writer(sets, sfd) as held:
        if kind == "link":
            (sets.parent / "elsewhere").mkdir()
            (sets / M).symlink_to(sets.parent / "elsewhere", target_is_directory=True)
        elif kind == "file":
            (sets / M).write_bytes(b"x")
        else:
            (sets / M).mkdir()
        if kind == "lstat-error":
            monkeypatch.setattr(locks, "_lstat_at", lambda d, n: (_ for _ in ()).throw(
                OSError(errno.EIO, "fstatat failed")))
        if kind == "open-error":
            monkeypatch.setattr(locks, "_open_at", lambda d, n, f: (_ for _ in ()).throw(
                OSError(errno.EIO, "open failed")))
        if kind == "lock-error":
            real_open = locks._open_at

            def spy(d, n, f):
                fd = real_open(d, n, f)
                rec.fail_fd = fd
                return fd
            rec = Recorder(monkeypatch, err=errno.ENOLCK)
            monkeypatch.setattr(locks, "_open_at", spy)
        r = refused(held.recheck_m)
        assert r.refusal is locks.Refusal.UNVERIFIABLE and r.result == "refused_unverifiable"
        assert held.m_fd is None
    assert free(sets)
    if kind == "link":
        assert free(sets.parent / "elsewhere")


def test_recheck_is_only_for_writers(with_m):
    with opened(with_m) as sfd, locks.acquire_exclusive(with_m, sfd) as held:
        with pytest.raises(ValueError):
            held.recheck_m()


# ================================================================ release and lifetime


def test_release_is_idempotent_and_in_reverse_order(with_m, monkeypatch):
    rec = Recorder(monkeypatch)
    with opened(with_m) as sfd, opened(with_m / M) as mfd:
        held = locks.acquire_exclusive(with_m, sfd, mfd)
        held.release()
        held.release()
        unlocks = [(fd, op) for fd, op in rec.calls if op & fcntl.LOCK_UN]
        assert unlocks == [(mfd, fcntl.LOCK_UN), (sfd, fcntl.LOCK_UN)]
        assert held.m_fd is None and held.released


def test_the_context_manager_releases_on_an_exception(with_m):
    with opened(with_m) as sfd, opened(with_m / M) as mfd:
        with pytest.raises(RuntimeError):
            with locks.acquire_exclusive(with_m, sfd, mfd):
                raise RuntimeError("boom")
        assert free(with_m) and free(with_m / M)


def test_an_unlock_error_does_not_mask_and_the_owned_descriptor_is_still_closed(with_m, monkeypatch):
    with opened(with_m) as sfd:
        held = locks.acquire_writer(with_m, sfd)
        mfd = held.m_fd
        real = locks._flock

        def bad(fd, op):
            if op & fcntl.LOCK_UN:
                raise OSError(errno.EIO, "unlock failed")
            return real(fd, op)
        monkeypatch.setattr(locks, "_flock", bad)
        held.release()
        with pytest.raises(OSError):
            os.fstat(mfd)
        assert free(with_m / M)  # closing the owned descriptor released the M lock
        assert held.released


def test_lock_m_refuses_on_a_released_or_shared_holder(with_m):
    with opened(with_m) as sfd, opened(with_m / M) as mfd:
        held = locks.acquire_exclusive(with_m, sfd)
        held.release()
        with pytest.raises(ValueError):
            held.lock_m(mfd)
        with locks.acquire_writer(with_m, sfd) as w:
            with pytest.raises(ValueError):
                w.lock_m(mfd)
        with locks.acquire_exclusive(with_m, sfd, mfd) as both:
            with pytest.raises(ValueError):
                both.lock_m(mfd)  # M is already held


@pytest.mark.parametrize("mode", ["sh", "ex"])
def test_a_killed_holder_leaves_no_stale_lock_and_no_file(with_m, mode):
    # revision 3 §2.6 test 6: flock keeps no on-disk state
    before = snap(with_m)
    with holder((with_m, mode), (with_m / M, mode)) as p:
        with opened(with_m) as sfd, opened(with_m / M) as mfd:
            assert refused(locks.acquire_exclusive, with_m, sfd, mfd).busy
        p.stdin.write(b"die\n")
        p.stdin.flush()
        assert p.wait(10) == 9
    with opened(with_m) as sfd, opened(with_m / M) as mfd:
        with locks.acquire_exclusive(with_m, sfd, mfd):
            pass
        with locks.acquire_writer(with_m, sfd):
            pass
    assert snap(with_m) == before


def test_locks_taken_here_are_visible_to_another_process(with_m):
    probe = ("import fcntl, os, sys\n"
             "fd = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY)\n"
             "try:\n    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)\nexcept BlockingIOError:\n"
             "    sys.exit(3)\nsys.exit(0)\n")

    def other_can_share(path):
        return subprocess.run([sys.executable, "-c", probe, str(path)], timeout=30).returncode == 0
    with opened(with_m) as sfd, opened(with_m / M) as mfd:
        with locks.acquire_exclusive(with_m, sfd, mfd):
            assert not other_can_share(with_m) and not other_can_share(with_m / M)
        with locks.acquire_writer(with_m, sfd):
            assert other_can_share(with_m) and other_can_share(with_m / M)
        assert other_can_share(with_m) and other_can_share(with_m / M)


def test_no_descriptor_leaks_after_refusals(with_m, monkeypatch):
    def count():
        return len(os.listdir("/proc/self/fd"))
    with opened(with_m) as sfd, opened(with_m / M) as mfd:
        with holder((with_m / M, "ex")):
            inside = count()
            for _ in range(5):
                refused(locks.acquire_writer, with_m, sfd)
                refused(locks.acquire_exclusive, with_m, sfd, mfd)
            assert count() == inside
        start = count()
        with locks.acquire_writer(with_m, sfd):
            assert count() == start + 1
        assert count() == start


# ================================================================ correction round: M found by acquire_exclusive


class Spy:
    """Records every descriptor ``locks`` opens relative to the sets folder."""

    def __init__(self, monkeypatch):
        self.fds = []
        self.real = locks._open_at
        monkeypatch.setattr(locks, "_open_at", self)

    def __call__(self, dir_fd, name, flags):
        fd = self.real(dir_fd, name, flags)
        self.fds.append(fd)
        return fd


def is_closed(fd) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return True
    return False


def test_exclusive_with_no_descriptor_still_locks_an_existing_m(with_m, monkeypatch):
    rec = Recorder(monkeypatch)
    with opened(with_m) as sfd:
        with locks.acquire_exclusive(with_m, sfd) as held:
            assert held.m_fd is not None and held.m_fd != sfd
            assert rec.locks_taken() == [(sfd, fcntl.LOCK_EX | fcntl.LOCK_NB),
                                         (held.m_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)]
            st = os.fstat(held.m_fd)
            assert (st.st_dev, st.st_ino) == (os.lstat(with_m / M).st_dev, os.lstat(with_m / M).st_ino)
            assert not free(with_m, fcntl.LOCK_SH) and not free(with_m / M, fcntl.LOCK_SH)


def test_exclusive_opens_m_relative_to_sets_never_through_a_link_and_after_the_sets_lock(with_m, monkeypatch):
    order = []
    real_open, real_lock = locks._open_at, locks._flock

    def spy_open(dir_fd, name, flags):
        order.append(("open", dir_fd, name, flags))
        return real_open(dir_fd, name, flags)

    def spy_lock(fd, op):
        order.append(("lock", fd, op))
        return real_lock(fd, op)
    monkeypatch.setattr(locks, "_open_at", spy_open)
    monkeypatch.setattr(locks, "_flock", spy_lock)
    with opened(with_m) as sfd:
        with locks.acquire_exclusive(with_m, sfd) as held:
            mfd = held.m_fd
            assert order[0] == ("lock", sfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            kind, dir_fd, name, flags = order[1]
            assert (kind, dir_fd, name) == ("open", sfd, M)
            assert flags & os.O_DIRECTORY and flags & os.O_NOFOLLOW and not flags & os.O_CREAT
            assert order[2] == ("lock", mfd, fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.parametrize("kind", ["link", "file", "link-to-nothing"])
def test_exclusive_refuses_an_m_that_is_not_a_plain_folder(sets, kind):
    if kind == "link":
        (sets.parent / "elsewhere").mkdir()
        (sets / M).symlink_to(sets.parent / "elsewhere", target_is_directory=True)
    elif kind == "file":
        (sets / M).write_bytes(b"not a folder")
    else:
        (sets / M).symlink_to(sets.parent / "nothing")
    before = snap(sets)
    with opened(sets) as sfd:
        r = refused(locks.acquire_exclusive, sets, sfd)
        assert r.refusal is locks.Refusal.UNVERIFIABLE and r.step is locks.Step.M_OPEN
        assert r.errno in (errno.ELOOP, errno.ENOTDIR)
        assert free(sets)  # never continues with the sets lock alone
    assert snap(sets) == before
    if kind == "link":
        assert free(sets.parent / "elsewhere")


def test_exclusive_m_open_error_other_than_absent_is_unverifiable(with_m, monkeypatch):
    def bad(dir_fd, name, flags):
        raise PermissionError(errno.EACCES, "denied")
    monkeypatch.setattr(locks, "_open_at", bad)
    with opened(with_m) as sfd:
        r = refused(locks.acquire_exclusive, with_m, sfd)
        assert r.result == "refused_unverifiable" and r.step is locks.Step.M_OPEN
        assert r.errno == errno.EACCES and free(with_m)


@pytest.mark.parametrize("mode", ["sh", "ex"])
def test_exclusive_with_no_descriptor_is_busy_at_m_and_releases_everything(with_m, monkeypatch, mode):
    before = snap(with_m)
    spy = Spy(monkeypatch)
    with holder((with_m / M, mode)), opened(with_m) as sfd:
        r = refused(locks.acquire_exclusive, with_m, sfd)
        assert r.refusal is locks.Refusal.BUSY and r.step is locks.Step.M_LOCK
        assert r.errno in CONTENTION
        assert free(with_m)
    assert len(spy.fds) == 1 and is_closed(spy.fds[0])
    assert snap(with_m) == before


def test_exclusive_with_no_descriptor_refuses_a_replaced_m(with_m, monkeypatch):
    spy = Spy(monkeypatch)
    real = locks._flock
    state = {"done": False}

    def swap(fd, op):
        real(fd, op)
        if spy.fds and fd == spy.fds[0] and not op & fcntl.LOCK_UN and not state["done"]:
            os.rename(with_m / M, with_m / "old-m")
            (with_m / M).mkdir()
            state["done"] = True
    monkeypatch.setattr(locks, "_flock", swap)
    with opened(with_m) as sfd:
        r = refused(locks.acquire_exclusive, with_m, sfd)
        assert r.refusal is locks.Refusal.UNVERIFIABLE and r.step is locks.Step.M_IDENTITY
        assert free(with_m) and free(with_m / "old-m") and free(with_m / M)
    assert is_closed(spy.fds[0])


@pytest.mark.parametrize("kind", ["different", "error"])
def test_exclusive_with_no_descriptor_refuses_an_unverifiable_m_identity(with_m, monkeypatch, kind):
    def fake(dir_fd, name):
        if kind == "error":
            raise OSError(errno.EIO, "fstatat failed")
        return os.stat(with_m / "A.json")
    monkeypatch.setattr(locks, "_lstat_at", fake)
    spy = Spy(monkeypatch)
    with opened(with_m) as sfd:
        r = refused(locks.acquire_exclusive, with_m, sfd)
        assert r.result == "refused_unverifiable" and r.step is locks.Step.M_IDENTITY
        assert free(with_m) and free(with_m / M)
    assert is_closed(spy.fds[0])


def test_the_m_lock_lasts_for_the_whole_held_lifetime_and_release_frees_both(with_m):
    with opened(with_m) as sfd:
        held = locks.acquire_exclusive(with_m, sfd)
        mfd = held.m_fd
        with holder():  # time passes in another process; the lock does not depend on this call
            assert not free(with_m / M, fcntl.LOCK_SH)
        held.verify()
        assert held.m_fd == mfd and not free(with_m / M, fcntl.LOCK_SH)
        held.release()
        assert is_closed(mfd) and held.m_fd is None
        assert free(with_m) and free(with_m / M)
        os.fstat(sfd)  # the caller's sets descriptor stays open


def test_exclusive_and_writers_still_exclude_each_other_with_m_found_here(with_m):
    from lpbfstudio import upgrade as up
    with opened(with_m) as sfd, opened(with_m) as other:
        with locks.acquire_exclusive(with_m, sfd):
            r = refused(locks.acquire_writer, with_m, other)
            assert r.busy and r.step is locks.Step.SETS_LOCK
            r = refused(locks.acquire_exclusive, with_m, other)
            assert r.busy and r.step is locks.Step.SETS_LOCK
            with pytest.raises(up.LibraryBusy):
                with up.library_lock(with_m):
                    pass
        with locks.acquire_writer(with_m, sfd):
            assert refused(locks.acquire_exclusive, with_m, other).busy
        with locks.acquire_exclusive(with_m, sfd):
            pass


@pytest.mark.parametrize("err", CONTENTION + LOCK_ERRORS)
def test_a_lock_failure_on_a_found_m_follows_d1_and_releases_the_sets_lock(with_m, monkeypatch, err):
    real_open = locks._open_at

    def spy(d, n, f):
        fd = real_open(d, n, f)
        rec.fail_fd = fd
        return fd
    rec = Recorder(monkeypatch, err=err)
    monkeypatch.setattr(locks, "_open_at", spy)
    with opened(with_m) as sfd:
        r = refused(locks.acquire_exclusive, with_m, sfd)
        assert r.busy is (err in CONTENTION) and r.step is locks.Step.M_LOCK and r.errno == err
        assert len(rec.locks_taken()) == 2 and free(with_m) and free(with_m / M)
        assert is_closed(rec.fail_fd)


def test_a_descriptor_the_caller_passes_is_the_one_locked_and_is_never_closed(with_m, monkeypatch):
    # plan revision 4.7 state machine sections 3.1 to 3.3: restore locks its own M_fd, and the frozen
    # survey then repeats the flock on that same open file description
    spy = Spy(monkeypatch)
    with opened(with_m) as sfd, opened(with_m / M) as mfd:
        with locks.acquire_exclusive(with_m, sfd, mfd) as held:
            assert held.m_fd == mfd and spy.fds == []
            fcntl.flock(mfd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # same description: succeeds
            assert not free(with_m / M)  # a new description is refused: why this path stays
        os.fstat(mfd)


def test_no_descriptor_leaks_when_exclusive_finds_m(with_m):
    def count():
        return len(os.listdir("/proc/self/fd"))
    with opened(with_m) as sfd:
        start = count()
        with locks.acquire_exclusive(with_m, sfd):
            assert count() == start + 1
        assert count() == start
        with holder((with_m / M, "ex")):
            inside = count()
            for _ in range(5):
                refused(locks.acquire_exclusive, with_m, sfd)
            assert count() == inside


# ================================================================ correction round: close failures


class BadClose:
    """Closes for real, so nothing leaks, then reports the failure a close can report."""

    def __init__(self, monkeypatch, err=errno.EIO):
        self.err, self.real, self.calls = err, locks._close, []
        monkeypatch.setattr(locks, "_close", self)

    def __call__(self, fd):
        self.calls.append(fd)
        self.real(fd)
        raise OSError(self.err, "close failed")


def test_open_m_keeps_its_refusal_when_closing_its_descriptor_fails(with_m, monkeypatch):
    spy = Spy(monkeypatch)
    real_fstat = locks._fstat

    def fstat(fd):
        if fd in spy.fds:
            raise OSError(errno.EIO, "fstat failed")
        return real_fstat(fd)
    monkeypatch.setattr(locks, "_fstat", fstat)
    bad = BadClose(monkeypatch, errno.EBADF)
    with opened(with_m) as sfd:
        for fn in (locks.acquire_writer, locks.acquire_exclusive):
            r = refused(fn, with_m, sfd)
            assert r.refusal is locks.Refusal.UNVERIFIABLE and r.step is locks.Step.M_OPEN
            assert r.errno == errno.EIO  # the primary error, not the close error
            assert r.close_errno == errno.EBADF
            assert free(with_m)
    assert bad.calls == spy.fds


@pytest.mark.parametrize("fn", ["acquire_writer", "acquire_exclusive"])
def test_take_m_keeps_busy_when_closing_its_descriptor_fails(with_m, monkeypatch, fn):
    bad = BadClose(monkeypatch)
    with holder((with_m / M, "ex")), opened(with_m) as sfd:
        r = refused(getattr(locks, fn), with_m, sfd)
        assert r.refusal is locks.Refusal.BUSY and r.result == "refused_busy"
        assert r.step is locks.Step.M_LOCK and r.errno in CONTENTION
        assert r.close_errno == errno.EIO
        assert free(with_m) and len(bad.calls) == 1


def test_take_m_keeps_an_identity_refusal_when_closing_its_descriptor_fails(with_m, monkeypatch):
    monkeypatch.setattr(locks, "_lstat_at", lambda d, n: os.stat(with_m / "A.json"))
    BadClose(monkeypatch)
    with opened(with_m) as sfd:
        r = refused(locks.acquire_exclusive, with_m, sfd)
        assert r.refusal is locks.Refusal.UNVERIFIABLE and r.step is locks.Step.M_IDENTITY
        assert r.errno is None and r.close_errno == errno.EIO
        assert free(with_m) and free(with_m / M)


def test_recheck_keeps_its_refusal_when_closing_its_descriptor_fails(sets, monkeypatch):
    with opened(sets) as sfd, locks.acquire_writer(sets, sfd) as held:
        (sets / M).mkdir()
        BadClose(monkeypatch)
        with holder((sets / M, "ex")):
            r = refused(held.recheck_m)
            assert r.busy and r.close_errno == errno.EIO and held.m_fd is None
        monkeypatch.undo()


def test_an_interrupt_stays_primary_when_closing_fails(with_m, monkeypatch):
    real_open = locks._open_at

    def spy(d, n, f):
        fd = real_open(d, n, f)
        rec.fail_fd = fd
        return fd
    rec = Recorder(monkeypatch, exc=KeyboardInterrupt())
    monkeypatch.setattr(locks, "_open_at", spy)
    BadClose(monkeypatch)
    with opened(with_m) as sfd:
        with pytest.raises(KeyboardInterrupt) as info:
            locks.acquire_exclusive(with_m, sfd)
        assert info.value.close_errno == errno.EIO
        assert free(with_m) and free(with_m / M)


def test_a_refusal_with_a_clean_close_has_no_close_error(with_m):
    with holder((with_m / M, "ex")), opened(with_m) as sfd:
        r = refused(locks.acquire_exclusive, with_m, sfd)
        assert r.busy and r.close_errno is None


def test_a_close_failure_on_release_is_reported_and_is_not_a_refusal(with_m, monkeypatch):
    with opened(with_m) as sfd:
        held = locks.acquire_exclusive(with_m, sfd)
        BadClose(monkeypatch)
        with pytest.raises(OSError) as info:
            held.release()
        assert not isinstance(info.value, locks.LockRefused) and info.value.errno == errno.EIO
        assert held.released and held.m_fd is None
        assert free(with_m) and free(with_m / M)  # the sets lock was released all the same
        held.release()  # idempotent: reported once


def test_a_close_failure_leaving_a_clean_with_block_is_raised(with_m, monkeypatch):
    with opened(with_m) as sfd:
        with pytest.raises(OSError) as info:
            with locks.acquire_exclusive(with_m, sfd):
                BadClose(monkeypatch)
        assert info.value.errno == errno.EIO and free(with_m)


def test_a_close_failure_never_replaces_the_exception_leaving_a_with_block(with_m, monkeypatch):
    with opened(with_m) as sfd:
        with pytest.raises(RuntimeError) as info:
            with locks.acquire_exclusive(with_m, sfd):
                BadClose(monkeypatch)
                raise RuntimeError("boom")
        assert info.value.close_errno == errno.EIO and free(with_m)


# ================================================================ correction round: lifecycle


def test_an_unlock_failure_on_the_callers_descriptor_is_reported_and_the_lock_stays(sets, monkeypatch):
    # the module cannot close a caller's descriptor: the lock lasts until the caller closes it
    sfd = os.open(sets, os.O_RDONLY | os.O_DIRECTORY)
    try:
        held = locks.acquire_exclusive(sets, sfd)
        real = locks._flock

        def bad(fd, op):
            if op & fcntl.LOCK_UN:
                raise OSError(errno.EIO, "unlock failed")
            return real(fd, op)
        monkeypatch.setattr(locks, "_flock", bad)
        held.release()
        assert held.released and held.unlock_error.errno == errno.EIO
        assert not free(sets)
    finally:
        os.close(sfd)
    assert free(sets)


def test_a_clean_release_reports_no_unlock_error(with_m):
    with opened(with_m) as sfd:
        held = locks.acquire_exclusive(with_m, sfd)
        held.release()
        assert held.unlock_error is None


@pytest.mark.parametrize("where", ["sets", "m"])
@pytest.mark.parametrize("own", [True, False])
def test_an_interrupt_right_after_a_granted_lock_leaves_nothing_held(with_m, monkeypatch, where, own):
    # the real lock IS granted, and only then the interrupt arrives
    real = locks._flock
    state = {}

    def granted_then_interrupted(fd, op):
        real(fd, op)
        if not op & fcntl.LOCK_UN and fd == state.get("target"):
            state["target"] = None
            raise KeyboardInterrupt()
    monkeypatch.setattr(locks, "_flock", granted_then_interrupted)
    real_open = locks._open_at

    def spy(d, n, f):
        fd = real_open(d, n, f)
        if where == "m":
            state["target"] = fd
        return fd
    monkeypatch.setattr(locks, "_open_at", spy)
    with opened(with_m) as sfd, opened(with_m / M) as mfd:
        if where == "sets":
            state["target"] = sfd
        elif not own:
            state["target"] = mfd
        with pytest.raises(KeyboardInterrupt):
            locks.acquire_exclusive(with_m, sfd, None if own else mfd)
        assert state["target"] is None  # the interrupt really fired after the grant
        assert free(with_m) and free(with_m / M)


def test_a_refused_sets_request_does_not_unlock_the_callers_descriptor(sets, monkeypatch):
    rec = Recorder(monkeypatch)
    with holder((sets, "ex")), opened(sets) as sfd:
        assert refused(locks.acquire_exclusive, sets, sfd).busy
    assert [c for c in rec.calls if c[1] & fcntl.LOCK_UN] == []
