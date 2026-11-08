"""Lock tests 1, 2, 4 to 9 at the caller level: ``Library`` writers and the upgrade lock entry.

Stage B2a of B7 Phase 2 (S3-B2c-b). Requirements: revision 5 sections 2.2, 3 and 5, revision 3
sections 2.2 to 2.4 and 2.6, plan revision 4.7 sections 1.1 and 1.2 (OD-1, OD-2, OD-11), and the B2a
decision record. The primitives are tested in ``test_locks.py``; these tests go through the callers.

Tests 3, 13, 14 and 15 need a real restore and belong to stage b3.

The writers are exactly ``save``, ``copy``, ``rename``, ``delete``, ``import_file`` and
``ensure_defaults``. Every POSIX test uses real holder processes on ext4 (``/tmp``) and tmpfs
(``/dev/shm``). On a platform with no ``fcntl.flock`` the writers stay unlocked by explicit rule
(B2a decision 6): those tests run everywhere and claim nothing about mutual exclusion.
"""

import ast
import contextlib
import errno
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner

try:
    import fcntl
except ImportError:  # Windows: the POSIX tests are skipped, the platform-rule tests still run
    fcntl = None

from lpbfstudio import library as libmod  # noqa: E402
from lpbfstudio import locks  # noqa: E402
from lpbfstudio import upgrade as up  # noqa: E402
from lpbfstudio.cli import main as cli  # noqa: E402
from lpbfstudio.defaults import default_sets  # noqa: E402
from lpbfstudio.library import Library, LibraryError  # noqa: E402
from lpbfstudio.migration import FileResult  # noqa: E402

posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock behaviour; not executed on Windows")

ROOTS = {"ext4": "/tmp", "tmpfs": "/dev/shm"}
M = up.MIGRATION_DIR
SRC = str(Path(libmod.__file__).resolve().parents[1])
BUSY_TEXT = "an upgrade, report, apply or restore is in progress for this library; nothing was saved"
CANNOT_LOCK_TEXT = "this library's file system cannot lock; nothing was saved"
ABANDONED_TEXT = "an upgrade started while saving; nothing was saved"
CHANGED_TEXT = "the library folder changed while it was being locked"
DEFAULT = default_sets()[0].name
ERRORS = {"ENOLCK": errno.ENOLCK, "EOPNOTSUPP": errno.EOPNOTSUPP, "EINVAL": errno.EINVAL, "EIO": errno.EIO,
          "generic": None}


# --------------------------------------------------------------------------- helpers


@pytest.fixture(params=sorted(ROOTS))
def lib(request):
    """A seeded library with an unlocked format 1 set ``P1``, on a real ext4 or tmpfs file system."""
    root = Path(tempfile.mkdtemp(prefix="liblocks-", dir=ROOTS[request.param]))
    library = Library(root / "lib")
    library.copy(DEFAULT, "P1")
    try:
        yield library
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def with_m(lib):
    (lib.sets_dir / M).mkdir()
    (lib.sets_dir / M / up.RECORDS_DIR).mkdir()
    return lib


def snap(root: Path) -> dict:
    """Every entry under ``root``: type, inode, and size and bytes (or link target). A folder's own
    size is left out: it depends on the file system (tmpfs counts entries), and its entries are here."""
    out = {}
    for dirpath, dirs, files in os.walk(root):
        for name in dirs + files:
            p = Path(dirpath) / name
            st = os.lstat(p)
            size = None if stat.S_ISDIR(st.st_mode) else st.st_size
            if stat.S_ISLNK(st.st_mode):
                body = os.readlink(p)
            elif stat.S_ISREG(st.st_mode):
                body = p.read_bytes()
            else:
                body = None
            out[str(p.relative_to(root))] = (stat.S_IFMT(st.st_mode), st.st_ino, size, body)
    return out


def ident(path) -> tuple:
    st = os.stat(path)
    return st.st_dev, st.st_ino


def free(path: Path, op=None) -> bool:
    op = fcntl.LOCK_EX if op is None else op
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            fcntl.flock(fd, op | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


def temps(lib: Library) -> list:
    return sorted(p.name for p in lib.sets_dir.iterdir() if p.name.endswith(".tmp"))


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

SAVER = """
import os, sys
from lpbfstudio import library as L
lib = L.Library(sys.argv[1], seed=False)
es = lib.get(sys.argv[2])
es.description = "from the paused saver"
real = os.replace
def paused(src, dst):
    print("paused", flush=True)
    sys.stdin.readline()
    return real(src, dst)
os.replace = paused
lib.save(es)
print("saved", flush=True)
"""

UPGRADER = """
import sys
from lpbfstudio import upgrade as up
real = up._replace
def paused(src, dst):
    print("paused", flush=True)
    sys.stdin.readline()
    return real(src, dst)
up._replace = paused
out = up.upgrade_file(sys.argv[1], sys.argv[2])
print("result", out.result.value, out.busy, flush=True)
"""


def spawn(code: str, *args: str) -> subprocess.Popen:
    env = {**os.environ, "PYTHONPATH": SRC, "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.Popen([sys.executable, "-c", code, *args], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True, env=env)


@contextlib.contextmanager
def holder(*specs):
    p = spawn(HOLDER, *[f"{path}={mode}" for path, mode in specs])
    try:
        assert p.stdout.readline().strip() == "held"
        yield p
    finally:
        with contextlib.suppress(OSError, ValueError):
            p.stdin.close()
        p.wait(10)
        p.stdout.close()


@contextlib.contextmanager
def paused(code: str, *args: str):
    """A subprocess stopped at its pause point. ``resume`` lets it finish and returns its last line."""
    p = spawn(code, *args)

    def resume() -> str:
        p.stdin.write("\n")
        p.stdin.flush()
        return p.stdout.readline().strip()
    try:
        assert p.stdout.readline().strip() == "paused"
        yield resume
    finally:
        with contextlib.suppress(OSError, ValueError):
            p.stdin.close()
        p.wait(10)
        p.stdout.close()


def writers(lib: Library, tmp: Path) -> dict:
    """One call of each of the six writers, on a library that holds ``P1`` and lacks one default."""
    src = tmp / "imported.json"
    data = json.loads(lib.get("P1").model_dump_json())
    data["name"] = "Imported"
    src.write_text(json.dumps(data), encoding="utf-8")
    es = lib.get("P1")
    es.description = "changed"
    return {"save": lambda: lib.save(es), "copy": lambda: lib.copy("P1", "C1"),
            "rename": lambda: lib.rename("P1", "R1"), "delete": lambda: lib.delete("P1"),
            "import_file": lambda: lib.import_file(src), "ensure_defaults": lib.ensure_defaults}


WRITERS = ["save", "copy", "rename", "delete", "import_file", "ensure_defaults"]


def prepare(lib: Library, tmp: Path, name: str):
    """The call for writer ``name``. ``ensure_defaults`` needs a missing default before the snapshot."""
    calls = writers(lib, tmp)
    if name == "ensure_defaults":
        (lib.sets_dir / f"{DEFAULT}.json").unlink()
    return calls[name]


def refused(fn):
    with pytest.raises(LibraryError) as info:
        fn()
    assert type(info.value).__name__ == "LibraryLocked", info.value
    return info.value


class FailLock:
    """Makes ``flock`` fail on the descriptor of one folder; every other call is real."""

    def __init__(self, monkeypatch, module, target: Path, err):
        self.real, self.target, self.err, self.calls = module._flock, ident(target), err, []
        monkeypatch.setattr(module, "_flock", self)

    def __call__(self, fd, op):
        st = os.fstat(fd)
        self.calls.append(((st.st_dev, st.st_ino), op))
        if (st.st_dev, st.st_ino) == self.target and not op & fcntl.LOCK_UN:
            if self.err is None:
                raise OSError("generic lock failure")
            raise OSError(self.err, os.strerror(self.err))
        return self.real(fd, op)


# ================================================================ test 1 (rev5 §5 test 1; OD-1)


@posix
def test_1_upgrade_is_busy_against_a_paused_save_with_m_absent(lib):
    sets = lib.sets_dir
    with paused(SAVER, str(lib.root), "P1") as resume:
        before = (sets / "P1.json").read_bytes()
        out = up.upgrade_file(sets, "P1.json")
        assert out.busy and out.result is FileResult.FAILED and "nothing was written" in out.reason
        assert not (sets / M).exists()  # the sets lock comes before M is created
        assert (sets / "P1.json").read_bytes() == before
        assert resume() == "saved"
    assert b"from the paused saver" in (sets / "P1.json").read_bytes()
    assert up.upgrade_file(sets, "P1.json").result is FileResult.UPGRADED


# ================================================================ test 2 (rev5 §5 test 2)


@posix
def test_2_save_is_refused_while_an_upgrade_is_running(lib):
    lib.copy(DEFAULT, "Other")
    es = lib.get("Other")
    es.description = "unsaved edit"
    expected = es.model_dump()
    with paused(UPGRADER, str(lib.sets_dir), "P1.json") as resume:
        before = snap(lib.sets_dir)
        r = refused(lambda: lib.save(es))
        assert str(r) == BUSY_TEXT
        assert snap(lib.sets_dir) == before
        assert es.model_dump() == expected  # the in-memory set is unchanged
        assert resume().startswith("result upgraded")
    lib.save(es)
    assert lib.get("Other").description == "unsaved edit"


# ================================================================ test 4 (rev5 §5 test 4)


@pytest.fixture
def window(lib, monkeypatch):
    if os.environ.get("LPBF_REQUIRE_GUI_TESTS"):
        from PySide6 import QtWidgets  # a missing Qt runtime must fail, not skip
    else:
        QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
    from lpbfstudio.gui import app as appmod
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    shown = []
    monkeypatch.setattr(appmod.QMessageBox, "warning", staticmethod(lambda _w, _t, text: shown.append(text)))
    monkeypatch.setattr(appmod.QMessageBox, "question",
                        staticmethod(lambda *a, **k: appmod.QMessageBox.StandardButton.Yes))
    w = appmod.MainWindow(lib)
    w.refresh_library(select="P1")
    w.load_set("P1")
    w.current.description = "unsaved edit"
    w.mark_dirty()
    return w, shown


@posix
def test_4_gui_save_during_an_exclusive_operation_keeps_unsaved_changes(window, lib):
    w, shown = window
    before = snap(lib.sets_dir)
    with holder((lib.sets_dir, "ex")):
        assert w.save_current() is False
    assert snap(lib.sets_dir) == before
    assert w.dirty and w.current.description == "unsaved edit"
    assert len(shown) == 1 and BUSY_TEXT in shown[0]


@posix
def test_4_gui_delete_during_an_exclusive_operation_shows_the_refusal(window, lib):
    w, shown = window
    before = snap(lib.sets_dir)
    with holder((lib.sets_dir, "ex")):
        w.delete_set()
    assert snap(lib.sets_dir) == before
    assert w.current is not None and w.current.name == "P1" and w.dirty
    assert len(shown) == 1 and BUSY_TEXT in shown[0]


# ================================================================ test 5a: writers (rev5 §3, §5 test 5)


@posix
@pytest.mark.parametrize("writer", WRITERS)
@pytest.mark.parametrize("err", ERRORS.values(), ids=ERRORS.keys())
@pytest.mark.parametrize("where", ["sets", "m"])
def test_5a_every_writer_refuses_every_lock_error(with_m, tmp_path, monkeypatch, writer, err, where):
    lib = with_m
    call = prepare(lib, tmp_path, writer)
    before = snap(lib.root)
    FailLock(monkeypatch, locks, lib.sets_dir if where == "sets" else lib.sets_dir / M, err)
    r = refused(call)
    assert r.result == "refused_unverifiable" and r.refusal is locks.Refusal.UNVERIFIABLE
    if err in (errno.ENOLCK, errno.EOPNOTSUPP, errno.EINVAL):
        assert str(r) == CANNOT_LOCK_TEXT
    else:
        assert "nothing was saved" in str(r)
    assert snap(lib.root) == before  # no temporary file; S byte- and inode-identical
    assert free(lib.sets_dir) and free(lib.sets_dir / M)


@posix
@pytest.mark.parametrize("writer", WRITERS)
@pytest.mark.parametrize("where", ["sets", "m"])
def test_5a_every_writer_is_busy_against_an_exclusive_holder(with_m, tmp_path, writer, where):
    lib = with_m
    call = prepare(lib, tmp_path, writer)
    before = snap(lib.root)
    with holder((lib.sets_dir if where == "sets" else lib.sets_dir / M, "ex")):
        r = refused(call)
    assert r.result == "refused_busy" and str(r) == BUSY_TEXT
    assert snap(lib.root) == before


@posix
def test_writers_share_with_each_other(with_m, tmp_path):
    lib = with_m
    with holder((lib.sets_dir, "sh"), (lib.sets_dir / M, "sh")):
        for name in WRITERS:
            prepare(lib, tmp_path, name)()
            if name in ("rename", "delete"):
                lib.copy(DEFAULT, "P1")


@posix
@pytest.mark.parametrize("command,args", [(["new", "N1"], []), (["copy", "P1", "C1"], []),
                                          (["rename", "P1", "R1"], []), (["delete", "P1"], []),
                                          (["set", "P1", "description=x"], []), (["import"], ["file"])],
                         ids=["new", "copy", "rename", "delete", "set", "import"])
def test_5a_cli_reports_the_refusal_and_exits_non_zero(lib, tmp_path, command, args):
    argv = list(command)
    if args:
        src = tmp_path / "in.json"
        data = json.loads(lib.get("P1").model_dump_json())
        data["name"] = "Imported"
        src.write_text(json.dumps(data), encoding="utf-8")
        argv.append(str(src))
    before = snap(lib.root)
    with holder((lib.sets_dir, "ex")):
        result = CliRunner().invoke(cli, ["--library", str(lib.root), *argv])
    assert result.exit_code == 1 and isinstance(result.exception, SystemExit)  # no traceback
    assert BUSY_TEXT in result.output
    assert snap(lib.root) == before


@posix
@pytest.mark.parametrize("err", ERRORS.values(), ids=ERRORS.keys())
def test_5a_gui_keeps_its_state_on_a_lock_error(window, lib, monkeypatch, err):
    w, shown = window
    before = snap(lib.root)
    FailLock(monkeypatch, locks, lib.sets_dir, err)
    assert w.save_current() is False
    assert w.dirty and w.current.description == "unsaved edit" and len(shown) == 1
    assert snap(lib.root) == before


# ================================================================ test 5b: upgrade entry (OD-1)


@posix
@pytest.mark.parametrize("err", ERRORS.values(), ids=ERRORS.keys())
@pytest.mark.parametrize("m_exists", [False, True], ids=["m-absent", "m-present"])
def test_5b_upgrade_refuses_a_sets_lock_error_and_creates_nothing(lib, monkeypatch, err, m_exists):
    if m_exists:
        (lib.sets_dir / M).mkdir()
    before = snap(lib.root)
    FailLock(monkeypatch, up, lib.sets_dir, err)
    out = up.upgrade_file(lib.sets_dir, "P1.json")
    assert out.result is FileResult.FAILED and not out.busy and "nothing was written" in out.reason
    assert snap(lib.root) == before
    assert (lib.sets_dir / M).exists() is m_exists


@posix
@pytest.mark.parametrize("err", ERRORS.values(), ids=ERRORS.keys())
def test_5b_upgrade_refuses_an_m_lock_error_and_keeps_no_lock(with_m, monkeypatch, err):
    lib = with_m
    before = snap(lib.root)
    FailLock(monkeypatch, up, lib.sets_dir / M, err)
    out = up.upgrade_file(lib.sets_dir, "P1.json")
    assert out.result is FileResult.FAILED and not out.busy
    assert snap(lib.root) == before
    assert free(lib.sets_dir) and free(lib.sets_dir / M)


@posix
def test_5b_upgrade_takes_the_sets_lock_before_it_creates_or_locks_m(lib, monkeypatch):
    calls = []
    real_lock = up._flock

    def spy_lock(fd, op):
        st = os.fstat(fd)
        calls.append(((st.st_dev, st.st_ino), op, (lib.sets_dir / M).exists()))
        return real_lock(fd, op)
    monkeypatch.setattr(up, "_flock", spy_lock)
    assert up.upgrade_file(lib.sets_dir, "P1.json").result is FileResult.UPGRADED
    ex = fcntl.LOCK_EX | fcntl.LOCK_NB
    assert calls[0] == (ident(lib.sets_dir), ex, False)  # M does not exist yet
    assert calls[1] == (ident(lib.sets_dir / M), ex, True)


@posix
def test_5b_upgrade_refuses_a_swapped_sets_folder(lib, monkeypatch):
    sets, moved = lib.sets_dir, lib.sets_dir.parent / "moved"
    real = up._flock
    state = {"done": False}

    def swap(fd, op):
        real(fd, op)
        if not op & fcntl.LOCK_UN and not state["done"]:
            state["done"] = True
            os.rename(sets, moved)
            sets.mkdir()
    monkeypatch.setattr(up, "_flock", swap)
    before = (sets / "P1.json").read_bytes()
    out = up.upgrade_file(sets, "P1.json")
    assert out.result is FileResult.FAILED and not out.busy and CHANGED_TEXT in out.reason
    assert os.listdir(sets) == [] and not (moved / M).exists()
    assert (moved / "P1.json").read_bytes() == before


@posix
def test_5b_upgrade_holds_the_sets_lock_until_it_has_finished(lib, monkeypatch):
    real = up._replace
    held = []

    def spy(src, dst):
        held.append(not free(lib.sets_dir, fcntl.LOCK_SH))
        return real(src, dst)
    monkeypatch.setattr(up, "_replace", spy)
    assert up.upgrade_file(lib.sets_dir, "P1.json").result is FileResult.UPGRADED
    assert held == [True]
    assert free(lib.sets_dir) and free(lib.sets_dir / M)


# ================================================================ test 6 (rev5 §5 test 6, through Library)


@posix
@pytest.mark.parametrize("mode", ["sh", "ex"])
def test_6_a_killed_holder_does_not_block_the_next_save(with_m, mode):
    lib = with_m
    es = lib.get("P1")
    es.description = "after the kill"
    before = snap(lib.root)
    with holder((lib.sets_dir, mode), (lib.sets_dir / M, mode)) as p:
        p.stdin.write("die\n")
        p.stdin.flush()
        assert p.wait(10) == 9
    assert snap(lib.root) == before  # the dead holder left nothing behind
    lib.save(es)
    assert lib.get("P1").description == "after the kill"
    after = snap(lib.root)
    assert set(after) == set(before) and temps(lib) == []


# ================================================================ test 7 (rev5 §5 test 7, through Library)


@posix
def test_7_a_swapped_sets_folder_refuses_the_save_and_leaves_both_folders_alone(lib, monkeypatch):
    sets, moved = lib.sets_dir, lib.sets_dir.parent / "moved"
    es = lib.get("P1")
    es.description = "must not land anywhere"
    real = locks._flock
    state = {"done": False}

    def swap(fd, op):
        real(fd, op)
        if not op & fcntl.LOCK_UN and not state["done"]:
            state["done"] = True
            os.rename(sets, moved)
            sets.mkdir()
            (sets / "B.json").write_bytes(b"other folder")
    monkeypatch.setattr(locks, "_flock", swap)
    r = refused(lambda: lib.save(es))
    assert r.refusal is locks.Refusal.UNVERIFIABLE and CHANGED_TEXT in str(r)
    assert sorted(os.listdir(sets)) == ["B.json"] and (sets / "B.json").read_bytes() == b"other folder"
    assert b"must not land anywhere" not in (moved / "P1.json").read_bytes()
    assert free(moved) and free(sets)


# ================================================================ test 8 (rev5 §5 test 8, through Library)


@posix
def test_8_a_save_locks_the_sets_folder_then_m_shared_and_non_blocking(with_m, monkeypatch):
    lib = with_m
    calls = []
    real = locks._flock

    def spy(fd, op):
        st = os.fstat(fd)
        calls.append(((st.st_dev, st.st_ino), op))
        return real(fd, op)
    monkeypatch.setattr(locks, "_flock", spy)
    lib.save(lib.get("P1"))
    taken = [c for c in calls if not c[1] & fcntl.LOCK_UN]
    assert taken == [(ident(lib.sets_dir), fcntl.LOCK_SH | fcntl.LOCK_NB),
                     (ident(lib.sets_dir / M), fcntl.LOCK_SH | fcntl.LOCK_NB)]


@posix
def test_8_an_operation_entering_through_m_only_is_busy_against_a_save(with_m):
    lib = with_m
    with paused(SAVER, str(lib.root), "P1") as resume:
        fd = os.open(lib.sets_dir / M, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        assert resume() == "saved"


# ================================================================ test 9 (rev5 §5 test 9)


def forbid_locks(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a read-only path used a lock primitive")
    monkeypatch.setattr(locks, "_flock", boom)
    for name in ("open_sets", "acquire_writer"):
        monkeypatch.setattr(libmod, name, boom)


def read_everything(root: Path) -> None:
    lib = Library(root)
    assert DEFAULT in lib.names()
    es = lib.get(DEFAULT)
    lib.validate(es)
    assert lib.exists(DEFAULT) and lib.load_all()
    for argv in (["list"], ["show", DEFAULT], ["validate", DEFAULT]):
        result = CliRunner().invoke(cli, ["--library", str(root), *argv])
        assert result.exit_code == 0, result.output


@posix
def test_9_read_only_paths_take_no_lock_while_an_exclusive_operation_runs(with_m, monkeypatch):
    lib = with_m
    before = snap(lib.root)
    with holder((lib.sets_dir, "ex"), (lib.sets_dir / M, "ex")):
        forbid_locks(monkeypatch)
        read_everything(lib.root)
    assert snap(lib.root) == before


@posix
def test_9_gui_open_is_not_refused_while_an_exclusive_operation_runs(window, lib):
    w, shown = window
    w.dirty = False
    with holder((lib.sets_dir, "ex")):
        w.refresh_library(select=DEFAULT)
        w.load_set(DEFAULT)
    assert w.current.name == DEFAULT and shown == []


def test_9_read_only_paths_take_no_lock_on_any_platform(tmp_path, monkeypatch):
    # prepared by an ordinary seed: unlocked by rule where flock is missing, locked and free elsewhere
    root = tmp_path / "lib"
    Library(root)
    forbid_locks(monkeypatch)
    read_everything(root)


@posix
def test_9_a_library_that_needs_no_seeding_takes_no_lock(lib, monkeypatch):
    forbid_locks(monkeypatch)
    lib.ensure_defaults()
    Library(lib.root)


@posix
def test_9_contention_skips_seeding_and_never_refuses_a_reader(lib):
    # B2a follow-up decision 2: the constructor skips seeding; a direct ensure_defaults still refuses
    (lib.sets_dir / f"{DEFAULT}.json").unlink()
    before = snap(lib.root)
    with holder((lib.sets_dir, "ex")):
        reader = Library(lib.root)
        assert "P1" in reader.names()
        assert refused(reader.ensure_defaults).result == "refused_busy"
    assert snap(lib.root) == before
    Library(lib.root)  # the lock is free: the next start seeds what is missing
    assert lib.exists(DEFAULT)


@posix
@pytest.mark.parametrize("err", ERRORS.values(), ids=ERRORS.keys())
def test_9_a_lock_failure_that_is_not_contention_is_not_swallowed(lib, monkeypatch, err):
    (lib.sets_dir / f"{DEFAULT}.json").unlink()
    before = snap(lib.root)
    FailLock(monkeypatch, locks, lib.sets_dir, err)
    assert refused(lambda: Library(lib.root)).result == "refused_unverifiable"
    assert snap(lib.root) == before


@posix
def test_9_a_write_error_while_seeding_propagates(lib, monkeypatch):
    (lib.sets_dir / f"{DEFAULT}.json").unlink()

    def denied(*a, **k):
        raise PermissionError(errno.EACCES, "denied")
    monkeypatch.setattr(libmod.tempfile, "mkstemp", denied)
    with pytest.raises(PermissionError):
        Library(lib.root)


@posix
def test_9_a_serialization_error_while_seeding_propagates(lib, monkeypatch):
    (lib.sets_dir / f"{DEFAULT}.json").unlink()
    monkeypatch.setattr(libmod, "identity_problem", lambda es: "read-only field changed")
    with pytest.raises(LibraryError) as info:
        Library(lib.root)
    assert type(info.value) is LibraryError


@posix
def test_contention_never_creates_some_of_the_missing_defaults(lib):
    names = [es.name for es in default_sets()]
    assert len(names) >= 2
    for name in names:
        (lib.sets_dir / f"{name}.json").unlink()
    before = snap(lib.root)
    with holder((lib.sets_dir, "ex")):
        refused(lib.ensure_defaults)
    assert snap(lib.root) == before


@posix
def test_an_m_appearing_while_seeding_creates_none_of_the_missing_defaults(lib, monkeypatch):
    names = [es.name for es in default_sets()]
    for name in names:
        (lib.sets_dir / f"{name}.json").unlink()
    before = snap(lib.root)
    with contextlib.ExitStack() as stack:
        def appear():
            (lib.sets_dir / M).mkdir()
            stack.enter_context(holder((lib.sets_dir / M, "ex")))
        when_the_temporary_file_exists(monkeypatch, appear)
        assert str(refused(lib.ensure_defaults)) == ABANDONED_TEXT
    after = snap(lib.root)
    after.pop(f"sets/{M}")
    assert after == before and temps(lib) == []


@posix
def test_an_m_appearing_between_two_seeding_replaces_stops_the_batch(lib, monkeypatch):
    # B2a follow-up decision of 2026-11-07: a re-check before every replace; a refusal after the
    # first replace keeps the earlier complete files, removes only this batch's own temporary files,
    # and the next ensure_defaults completes the rest
    names = [es.name for es in default_sets()]
    assert len(names) >= 2
    for name in names:
        (lib.sets_dir / f"{name}.json").unlink()
    real = os.replace
    replaced = []
    with contextlib.ExitStack() as stack:
        def replace(src, dst):
            real(src, dst)
            replaced.append(Path(dst).name)
            if len(replaced) == 1:  # after the first replace, before the second
                (lib.sets_dir / M).mkdir()
                stack.enter_context(holder((lib.sets_dir / M, "ex")))
        monkeypatch.setattr(os, "replace", replace)
        r = refused(lib.ensure_defaults)
        monkeypatch.undo()
        assert r.result == "refused_busy" and str(r).startswith(ABANDONED_TEXT)
        # exact counts from this setup: one replaced before the refusal, out of every default
        assert f"1 of {len(names)} files were already complete and stay in place" in str(r)
        assert replaced == [f"{names[0]}.json"]  # the second replace never happened
        first = lib.sets_dir / f"{names[0]}.json"
        assert json.loads(first.read_text(encoding="utf-8"))["name"] == names[0]  # complete
        assert all(not (lib.sets_dir / f"{n}.json").exists() for n in names[1:])
        assert temps(lib) == []
    lib.ensure_defaults()  # the contention is gone
    assert all(lib.exists(name) for name in names) and temps(lib) == []


@posix
def test_seeding_leaves_a_temporary_file_that_is_no_longer_its_own(lib, monkeypatch):
    names = [es.name for es in default_sets()]
    for name in names:
        (lib.sets_dir / f"{name}.json").unlink()
    real = os.replace
    swapped = []
    with contextlib.ExitStack() as stack:
        def replace(src, dst):
            real(src, dst)
            if not swapped:
                others = sorted(p for p in lib.sets_dir.iterdir() if p.name.endswith(".tmp"))
                os.rename(others[0], str(others[0]) + ".moved")
                others[0].write_bytes(b"not this batch's file")
                swapped.append(others[0])
                (lib.sets_dir / M).mkdir()
                stack.enter_context(holder((lib.sets_dir / M, "ex")))
        monkeypatch.setattr(os, "replace", replace)
        r = refused(lib.ensure_defaults)
        monkeypatch.undo()
    assert swapped[0].read_bytes() == b"not this batch's file" and swapped[0].name in str(r)


@posix
def test_ensure_defaults_writes_every_missing_default_under_the_locks(lib):
    names = [es.name for es in default_sets()]
    for name in names:
        (lib.sets_dir / f"{name}.json").unlink()
    lib.ensure_defaults()
    assert all(lib.exists(name) for name in names) and temps(lib) == []


# ================================================================ pre-replace re-check (rev5 §2.2 step 3, §3)


def when_the_temporary_file_exists(monkeypatch, action) -> None:
    """Run ``action`` once, after the writer took its locks and created its temporary file."""
    real = libmod.tempfile.mkstemp
    state = {"done": False}

    def mkstemp(*a, **k):
        out = real(*a, **k)
        if not state["done"]:
            state["done"] = True
            action()
        return out
    monkeypatch.setattr(libmod.tempfile, "mkstemp", mkstemp)


@posix
@pytest.mark.parametrize("writer", ["save", "copy", "rename", "import_file", "ensure_defaults"])
def test_recheck_abandons_when_m_appears_held(lib, tmp_path, monkeypatch, writer):
    call = prepare(lib, tmp_path, writer)
    before = snap(lib.root)
    with contextlib.ExitStack() as stack:
        def appear():
            (lib.sets_dir / M).mkdir()
            stack.enter_context(holder((lib.sets_dir / M, "ex")))
        when_the_temporary_file_exists(monkeypatch, appear)
        r = refused(call)
        assert str(r) == ABANDONED_TEXT and r.result == "refused_busy"
    after = snap(lib.root)
    assert after.pop(f"sets/{M}") is not None
    assert after == before and temps(lib) == []  # S untouched; only its own temporary file removed
    assert free(lib.sets_dir)


@posix
def test_recheck_locks_an_m_that_appeared_free_and_the_save_goes_on(lib, monkeypatch):
    es = lib.get("P1")
    es.description = "saved"
    when_the_temporary_file_exists(monkeypatch, lambda: (lib.sets_dir / M).mkdir())
    real = os.replace
    held = []

    def spy(src, dst):
        held.append(free(lib.sets_dir / M, fcntl.LOCK_SH) and not free(lib.sets_dir / M))
        return real(src, dst)
    monkeypatch.setattr(os, "replace", spy)
    lib.save(es)
    assert held == [True] and lib.get("P1").description == "saved"
    assert free(lib.sets_dir / M)


@posix
@pytest.mark.parametrize("kind", ["link", "file"])
def test_recheck_abandons_when_m_appears_as_something_else(lib, monkeypatch, kind):
    es = lib.get("P1")
    es.description = "must not be saved"
    before = (lib.sets_dir / "P1.json").read_bytes()

    def appear():
        if kind == "link":
            (lib.sets_dir.parent / "elsewhere").mkdir()
            (lib.sets_dir / M).symlink_to(lib.sets_dir.parent / "elsewhere", target_is_directory=True)
        else:
            (lib.sets_dir / M).write_bytes(b"x")
    when_the_temporary_file_exists(monkeypatch, appear)
    r = refused(lambda: lib.save(es))
    assert r.result == "refused_unverifiable" and str(r) == ABANDONED_TEXT
    assert (lib.sets_dir / "P1.json").read_bytes() == before and temps(lib) == []


@posix
def test_recheck_abandons_when_a_held_m_was_replaced(with_m, monkeypatch):
    lib = with_m
    es = lib.get("P1")
    es.description = "must not be saved"
    before = (lib.sets_dir / "P1.json").read_bytes()

    def swap():
        os.rename(lib.sets_dir / M, lib.sets_dir / "old-m")
        (lib.sets_dir / M).mkdir()
    when_the_temporary_file_exists(monkeypatch, swap)
    r = refused(lambda: lib.save(es))
    assert r.result == "refused_unverifiable"
    assert (lib.sets_dir / "P1.json").read_bytes() == before and temps(lib) == []


@posix
def test_abandoning_leaves_a_temporary_file_that_is_no_longer_its_own(lib, monkeypatch):
    es = lib.get("P1")
    made = []
    real = libmod.tempfile.mkstemp

    def mkstemp(*a, **k):
        fd, path = real(*a, **k)
        made.append(path)
        os.rename(path, path + ".moved")
        Path(path).write_bytes(b"not the save's file")
        (lib.sets_dir / M).mkdir()
        stack.enter_context(holder((lib.sets_dir / M, "ex")))
        return fd, path
    monkeypatch.setattr(libmod.tempfile, "mkstemp", mkstemp)
    with contextlib.ExitStack() as stack:
        r = refused(lambda: lib.save(es))
        assert "nothing was saved" in str(r)
    assert Path(made[0]).read_bytes() == b"not the save's file"  # identified by inode, not by name
    assert str(made[0]) in str(r) or Path(made[0]).name in str(r)  # what is on disk is reported


@posix
def test_abandoning_reports_a_temporary_file_it_could_not_remove(lib, monkeypatch):
    es = lib.get("P1")
    made = []
    real_mk, real_unlink = libmod.tempfile.mkstemp, os.unlink

    def mkstemp(*a, **k):
        out = real_mk(*a, **k)
        made.append(out[1])
        (lib.sets_dir / M).mkdir()
        stack.enter_context(holder((lib.sets_dir / M, "ex")))
        return out

    def unlink(path, *a, **k):
        if str(path) == made[0]:
            raise OSError(errno.EIO, "unlink failed")
        return real_unlink(path, *a, **k)
    monkeypatch.setattr(libmod.tempfile, "mkstemp", mkstemp)
    monkeypatch.setattr(os, "unlink", unlink)
    with contextlib.ExitStack() as stack:
        r = refused(lambda: lib.save(es))
    assert Path(made[0]).exists() and Path(made[0]).name in str(r)
    monkeypatch.undo()


@posix
def test_a_refused_write_never_counts_as_a_save_in_a_later_read(lib, monkeypatch):
    es = lib.get("P1")
    es.description = "never saved"
    with holder((lib.sets_dir, "ex")):
        refused(lambda: lib.save(es))
    assert lib.get("P1").description != "never saved"


# ================================================================ the platform rule (B2a decision 6)


def no_flock(monkeypatch, how: str) -> None:
    fake = None if how == "no-fcntl" else type("NoFlock", (), {"LOCK_SH": 1, "LOCK_EX": 2, "LOCK_NB": 4, "LOCK_UN": 8})
    monkeypatch.setattr(locks, "fcntl", fake)
    monkeypatch.setattr(libmod, "fcntl", fake)


@pytest.mark.parametrize("how", ["no-fcntl", "no-flock"])
@pytest.mark.parametrize("writer", WRITERS)
def test_a_platform_without_flock_writes_unlocked_by_explicit_rule(tmp_path, monkeypatch, how, writer):
    lib = Library(tmp_path / "lib")
    lib.copy(DEFAULT, "P1")
    no_flock(monkeypatch, how)
    assert "flock" in locks.platform_problem()
    call = prepare(lib, tmp_path, writer)

    def boom(*a, **k):
        raise AssertionError("no lock primitive is called where flock does not exist")
    for name in ("open_sets", "acquire_writer"):
        monkeypatch.setattr(libmod, name, boom)
    call()
    assert temps(lib) == []
    assert {"save": lambda: lib.get("P1").description == "changed",
            "copy": lambda: lib.exists("C1"),
            "rename": lambda: lib.exists("R1") and not lib.exists("P1"),
            "delete": lambda: not lib.exists("P1"),
            "import_file": lambda: lib.exists("Imported"),
            "ensure_defaults": lambda: lib.exists(DEFAULT)}[writer]()


def test_a_new_library_is_seeded_where_flock_does_not_exist(tmp_path, monkeypatch):
    no_flock(monkeypatch, "no-fcntl")
    assert DEFAULT in Library(tmp_path / "lib").names()


def test_the_primitive_still_refuses_where_flock_does_not_exist(tmp_path, monkeypatch):
    # the rule is the caller's; the B1b contract is unchanged
    no_flock(monkeypatch, "no-fcntl")
    with pytest.raises(locks.LockRefused) as info:
        locks.open_sets(tmp_path)
    assert info.value.result == "refused_platform"


@posix
@pytest.mark.parametrize("flag", ["O_DIRECTORY", "O_NOFOLLOW"])
def test_a_platform_with_flock_but_missing_primitives_fails_closed(lib, monkeypatch, flag):
    # only a missing flock selects the rule: anything else unsupported still refuses
    before = snap(lib.root)
    es = lib.get("P1")
    monkeypatch.delattr(locks.os, flag)
    r = refused(lambda: lib.save(es))
    assert r.result == "refused_platform"
    monkeypatch.undo()
    assert snap(lib.root) == before


@posix
@pytest.mark.parametrize("err", ERRORS.values(), ids=ERRORS.keys())
def test_a_real_flock_failure_never_falls_back_to_an_unlocked_save(lib, monkeypatch, err):
    es = lib.get("P1")
    es.description = "must not be saved"
    before = snap(lib.root)
    FailLock(monkeypatch, locks, lib.sets_dir, err)
    refused(lambda: lib.save(es))
    assert snap(lib.root) == before


# ================================================================ scope


def test_process_and_machine_files_stay_outside_the_writer_locks(tmp_path, monkeypatch):
    lib = Library(tmp_path / "lib")

    def boom(*a, **k):
        raise AssertionError("not a set writer")
    for name in ("open_sets", "acquire_writer"):
        monkeypatch.setattr(libmod, name, boom)
    lib.save_process(lib.load_process())
    profile = next(iter(lib.load_machines().values()))
    lib.save_machine(profile)


# ================================================================ architecture: the B2a dependency boundary
#
# test_locks.py::test_the_module_imports_no_application_module_and_has_no_caller still passes, but it
# no longer proves its name: its text checks do not recognise ``from .locks import ...``, and ``locks``
# now has a caller. These tests state the boundary B2a actually has, by parsing imports and calls.


PACKAGE = Path(libmod.__file__).parent
ALLOWED_FROM_LOCKS = {"RESULT", "Held", "LockRefused", "Refusal", "Step", "acquire_writer", "open_sets"}


def modules():
    return sorted(p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts)


def imports_of(path: Path, target: str) -> set:
    """The names ``path`` imports from module ``target`` of this package, however it is written.
    A whole-module import counts as the name ``*``."""
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {"*" for a in node.names if a.name == f"lpbfstudio.{target}"}
        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").removeprefix("lpbfstudio").lstrip(".")
            if module == target:
                found |= {a.name for a in node.names}
            elif module == "" and (node.level or node.module == "lpbfstudio"):
                found |= {"*" for a in node.names if a.name == target}
    return found


# B3 (restore, 2026-11-07): the boundary gains exactly one caller, restore.py, which plan revision 4.7
# section 1.1 requires to use locks.py (R2) and fsprim.py (every strict primitive). Its imports are pinned
# as exactly as library.py's; library.py's own are unchanged.
ALLOWED_FOR_RESTORE_FROM_LOCKS = {"LockRefused", "Refusal", "acquire_exclusive", "platform_problem"}
ALLOWED_FOR_RESTORE_FROM_FSPRIM = {
    "FsprimError", "Incomplete", "UnsupportedPlatform", "WriteIncomplete", "check_architecture", "complete_listing",
    "complete_read", "exchange", "fd_mount_id", "identify_fs", "strict_fsync", "write_in_full", "_renameat2",
    "_symbol",
}


def test_library_py_and_restore_py_are_the_only_application_modules_that_import_locks():
    callers = {p.relative_to(PACKAGE).as_posix() for p in modules()
               if p.name != "locks.py" and imports_of(p, "locks")}
    assert callers == {"library.py", "restore.py"}


def test_restore_py_imports_only_the_exclusive_side_of_locks():
    assert imports_of(PACKAGE / "restore.py", "locks") == ALLOWED_FOR_RESTORE_FROM_LOCKS


def test_library_py_imports_only_the_writer_side_of_locks():
    assert imports_of(PACKAGE / "library.py", "locks") == ALLOWED_FROM_LOCKS


def test_no_application_module_reaches_locks_or_fsprim_dynamically():
    for p in modules():
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                name = ast.unparse(node.func)
                assert name not in ("__import__", "importlib.import_module", "import_module"), (p, name)


def test_restore_py_is_the_only_application_module_that_imports_fsprim():
    assert [p.name for p in modules() if p.name != "fsprim.py" and imports_of(p, "fsprim")] == ["restore.py"]
    assert imports_of(PACKAGE / "restore.py", "fsprim") == ALLOWED_FOR_RESTORE_FROM_FSPRIM


def test_locks_imports_no_application_module():
    tree = ast.parse((PACKAGE / "locks.py").read_text(encoding="utf-8"))
    relative = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level]
    assert relative == []


def calls_to(name: str) -> list:
    """(file, enclosing function) of every call of ``name`` in the package."""
    found = []
    for p in modules():
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef):
                for node in ast.walk(fn):
                    if isinstance(node, ast.Call) and ast.unparse(node.func).split(".")[-1] == name:
                        found.append((p.name, fn.name))
    return found


def test_upgrade_file_is_the_only_production_caller_of_library_lock():
    # the OD-1 order holds for upgrades because this is the only way in: see the decision record
    assert calls_to("library_lock") == [("upgrade.py", "upgrade_file")]
    assert calls_to("_lock_sets_folder") == [("upgrade.py", "upgrade_file")]


@posix
def test_library_lock_called_directly_takes_m_only_and_can_create_it(lib):
    # the contract as it stands: not equivalent to the upgrade entry, and pinned so nobody assumes it is
    with up.library_lock(lib.sets_dir):
        assert (lib.sets_dir / M).is_dir()
        assert free(lib.sets_dir) and not free(lib.sets_dir / M)
