"""B2b: the three items held out of B2a (authorized 2026-11-07).

* B2b-1: rev 5 section 2.2 step 3a, a supplement to step 3: ``delete`` re-checks the migration folder
  M (and both folder identities) immediately before its unlink, and refuses if it may not go on.
* B2b-2: ``rename``'s unlink of the old name keeps no re-check and no rollback. Pinned, not changed.
* B2b-3: a close that fails while the locks are released after a successful write reaches the caller
  as that ``OSError``; it is not a refusal, and the write stays done. A failed write keeps its own
  error, with the close failure recorded on it as ``close_errno``. Pinned, not changed.

Caller-level, through ``Library``. Helpers and fixtures come from the B2a suite, unchanged.
"""

import contextlib
import errno
import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from lpbfstudio import library as libmod
from lpbfstudio import locks
from lpbfstudio.cli import main as cli
from lpbfstudio.library import Library, LibraryError

from test_library_locks import (  # noqa: F401  (lib and with_m are fixtures)
    ABANDONED_TEXT, CHANGED_TEXT, DEFAULT, M, FailLock, fcntl, free, holder, ident, lib, posix, refused,
    snap, temps, with_m,
)


DELETE_READS = 2  # ``delete`` reads the set twice under its locks: ``_locate``, then the ``locked`` flag


def after_the_last_read(monkeypatch, action, last=DELETE_READS):
    """Run ``action`` once, right after read number ``last`` of a set file: for ``delete`` its last
    read, after the locks are taken and before the re-check and the unlink."""
    real = Library._read
    state = {"reads": 0}

    def read(f):
        es = real(f)
        state["reads"] += 1
        if state["reads"] == last:
            action()
        return es
    monkeypatch.setattr(Library, "_read", staticmethod(read))


def count_checks(monkeypatch) -> list:
    """Every call of the writer's final check, in order. The wrapper calls the real check."""
    calls = []
    real = libmod._final_check

    def check(held):
        calls.append("check")
        return real(held)
    monkeypatch.setattr(libmod, "_final_check", check)
    return calls


# ================================================================ B2b-1: delete, step 3a


@posix
def test_b2b1_delete_is_refused_when_m_appears_held_before_the_unlink(lib, monkeypatch):
    before = snap(lib.root)
    with contextlib.ExitStack() as stack:
        def appear():
            (lib.sets_dir / M).mkdir()
            stack.enter_context(holder((lib.sets_dir / M, "ex")))
        after_the_last_read(monkeypatch, appear)
        r = refused(lambda: lib.delete("P1"))
        assert str(r) == ABANDONED_TEXT and r.refusal is locks.Refusal.BUSY and r.result == "refused_busy"
    after = snap(lib.root)
    assert after.pop(f"sets/{M}") is not None
    assert after == before  # P1.json is still there: same inode, same bytes; nothing else changed
    assert lib.exists("P1") and free(lib.sets_dir)


@posix
def test_b2b1_the_check_runs_immediately_before_the_unlink(lib, monkeypatch):
    events = count_checks(monkeypatch)
    real_read, real_unlink = Library._read, Path.unlink
    monkeypatch.setattr(Library, "_read", staticmethod(lambda f: (events.append("read"), real_read(f))[1]))

    def unlink(path, *a, **k):
        events.append(("unlink", Path(path).name))
        return real_unlink(path, *a, **k)
    monkeypatch.setattr(Path, "unlink", unlink)
    lib.delete("P1")
    # the check comes after delete's last read, and the unlink comes straight after the check
    assert events == ["read"] * DELETE_READS + ["check", ("unlink", "P1.json")]
    assert not lib.exists("P1")


@posix
def test_b2b1_delete_locks_an_m_that_appeared_free_and_goes_on(lib, monkeypatch):
    after_the_last_read(monkeypatch, lambda: (lib.sets_dir / M).mkdir())
    real = Path.unlink
    held = []

    def unlink(path, *a, **k):
        if Path(path).name == "P1.json":  # M is held shared by this delete when it unlinks
            held.append(free(lib.sets_dir / M, fcntl.LOCK_SH) and not free(lib.sets_dir / M))
        return real(path, *a, **k)
    monkeypatch.setattr(Path, "unlink", unlink)
    lib.delete("P1")
    assert held == [True] and not lib.exists("P1")
    assert free(lib.sets_dir / M) and free(lib.sets_dir)
    assert sorted(os.listdir(lib.sets_dir / M)) == []  # the re-check never writes into M


@posix
@pytest.mark.parametrize("kind", ["link", "file"])
def test_b2b1_delete_is_refused_when_m_appears_as_something_else(lib, monkeypatch, kind):
    before = (lib.sets_dir / "P1.json").read_bytes()

    def appear():
        if kind == "link":
            (lib.sets_dir.parent / "elsewhere").mkdir()
            (lib.sets_dir / M).symlink_to(lib.sets_dir.parent / "elsewhere", target_is_directory=True)
        else:
            (lib.sets_dir / M).write_bytes(b"x")
    after_the_last_read(monkeypatch, appear)
    r = refused(lambda: lib.delete("P1"))
    assert r.result == "refused_unverifiable" and str(r) == ABANDONED_TEXT
    assert (lib.sets_dir / "P1.json").read_bytes() == before and free(lib.sets_dir)


@posix
def test_b2b1_delete_is_refused_when_a_held_m_was_replaced(with_m, monkeypatch):
    lib = with_m
    before = (lib.sets_dir / "P1.json").read_bytes()

    def swap():
        os.rename(lib.sets_dir / M, lib.sets_dir / "old-m")
        (lib.sets_dir / M).mkdir()
    after_the_last_read(monkeypatch, swap)
    r = refused(lambda: lib.delete("P1"))
    assert r.result == "refused_unverifiable"
    assert (lib.sets_dir / "P1.json").read_bytes() == before
    assert free(lib.sets_dir) and free(lib.sets_dir / M) and free(lib.sets_dir / "old-m")


@posix
def test_b2b1_delete_is_refused_when_the_sets_folder_was_swapped_before_the_unlink(lib, monkeypatch):
    sets, moved = lib.sets_dir, lib.sets_dir.parent / "moved"
    before = (sets / "P1.json").read_bytes()

    def swap():
        os.rename(sets, moved)
        sets.mkdir()
        (sets / "P1.json").write_bytes(b"other folder")
    after_the_last_read(monkeypatch, swap)
    r = refused(lambda: lib.delete("P1"))
    assert r.result == "refused_unverifiable" and r.refusal is locks.Refusal.UNVERIFIABLE
    assert (moved / "P1.json").read_bytes() == before  # the locked folder's file is not removed
    assert (sets / "P1.json").read_bytes() == b"other folder"  # nor the one now at the path
    assert free(moved) and free(sets)


@posix
def test_b2b1_delete_is_refused_when_m_cannot_be_examined(lib, monkeypatch):
    before = (lib.sets_dir / "P1.json").read_bytes()
    real = locks._lstat_at

    def broken(dir_fd, name):
        if name == M:
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        return real(dir_fd, name)
    after_the_last_read(monkeypatch, lambda: monkeypatch.setattr(locks, "_lstat_at", broken))
    r = refused(lambda: lib.delete("P1"))
    assert r.result == "refused_unverifiable"
    assert (lib.sets_dir / "P1.json").read_bytes() == before and free(lib.sets_dir)


@posix
@pytest.mark.parametrize("err", [errno.EIO, errno.ENOLCK])
def test_b2b1_delete_is_refused_when_an_appeared_m_cannot_be_locked(lib, monkeypatch, err):
    before = (lib.sets_dir / "P1.json").read_bytes()

    def appear():
        (lib.sets_dir / M).mkdir()
        FailLock(monkeypatch, locks, lib.sets_dir / M, err)
    after_the_last_read(monkeypatch, appear)
    r = refused(lambda: lib.delete("P1"))
    assert r.result == "refused_unverifiable" and str(r) == ABANDONED_TEXT
    assert (lib.sets_dir / "P1.json").read_bytes() == before
    assert free(lib.sets_dir) and free(lib.sets_dir / M)


@posix
@pytest.mark.parametrize("case", ["missing", "locked"])
def test_b2b1_earlier_delete_refusals_are_unchanged_and_never_reach_the_check(lib, monkeypatch, case):
    calls = count_checks(monkeypatch)
    if case == "locked":  # written directly: no writer may set the flag, and none is under test here
        f = lib.sets_dir / "P1.json"
        data = json.loads(f.read_text(encoding="utf-8"))
        data["locked"] = True
        f.write_text(json.dumps(data, indent=2), encoding="utf-8")
    before = snap(lib.root)
    with pytest.raises(LibraryError) as info:
        lib.delete("absent" if case == "missing" else "P1")
    assert type(info.value) is LibraryError and calls == []
    assert snap(lib.root) == before and free(lib.sets_dir)


@posix
def test_b2b1_the_cli_reports_a_delete_refused_at_the_recheck_and_exits_1(lib, monkeypatch):
    before = snap(lib.root)
    with contextlib.ExitStack() as stack:
        def appear():
            (lib.sets_dir / M).mkdir()
            stack.enter_context(holder((lib.sets_dir / M, "ex")))
        after_the_last_read(monkeypatch, appear)
        result = CliRunner().invoke(cli, ["--library", str(lib.root), "delete", "P1"])
    assert result.exit_code == 1 and isinstance(result.exception, SystemExit)  # no traceback
    assert ABANDONED_TEXT in result.output
    after = snap(lib.root)
    assert after.pop(f"sets/{M}") is not None and after == before


def test_b2b1_without_flock_delete_has_no_check_and_still_deletes(tmp_path, monkeypatch):
    """The B2a platform rule (decision 6) is unchanged: no locks, so there is nothing to re-check."""
    monkeypatch.setattr(libmod, "_flock_exists", lambda: False)
    lib = Library(tmp_path / "lib")
    lib.copy(DEFAULT, "P1")
    seen = []
    real = Path.unlink

    def unlink(path, *a, **k):
        seen.append(libmod._FINAL_CHECK.get())
        return real(path, *a, **k)
    monkeypatch.setattr(Path, "unlink", unlink)
    lib.delete("P1")
    assert seen == [None] and not lib.exists("P1")


# ================================================================ B2b-2: rename's old name, unchanged


@posix
def test_b2b2_rename_does_not_recheck_before_removing_the_old_name(lib, monkeypatch):
    """The documented window: an M that appears, held, after the new name is in place does not stop
    the old name's removal, and nothing is rolled back. One check only, before the new name's replace."""
    calls = count_checks(monkeypatch)
    old = (lib.sets_dir / "P1.json").read_bytes()
    real = os.replace
    with contextlib.ExitStack() as stack:
        def replace(src, dst):
            out = real(src, dst)
            (lib.sets_dir / M).mkdir()
            stack.enter_context(holder((lib.sets_dir / M, "ex")))
            return out
        monkeypatch.setattr(os, "replace", replace)
        es = lib.rename("P1", "R1")
    assert es.name == "R1" and calls == ["check"]
    assert not (lib.sets_dir / "P1.json").exists()
    assert (lib.sets_dir / "R1.json").read_bytes() != old and lib.get("R1").name == "R1"
    assert temps(lib) == [] and free(lib.sets_dir)


@posix
def test_b2b2_rename_still_rechecks_before_the_new_name(lib, monkeypatch):
    """B2a behaviour, kept: the new name's replace is re-checked; both names stay as they were."""
    before = snap(lib.root)
    with contextlib.ExitStack() as stack:
        real = libmod.tempfile.mkstemp

        def mkstemp(*a, **k):
            out = real(*a, **k)
            (lib.sets_dir / M).mkdir()
            stack.enter_context(holder((lib.sets_dir / M, "ex")))
            return out
        monkeypatch.setattr(libmod.tempfile, "mkstemp", mkstemp)
        r = refused(lambda: lib.rename("P1", "R1"))
        assert str(r) == ABANDONED_TEXT
    after = snap(lib.root)
    assert after.pop(f"sets/{M}") is not None and after == before


# ================================================================ B2b-3: close failure on release


class FailClose:
    """Makes the locks module's close of M's descriptor fail once; the descriptor stays open and is
    closed by the test afterwards. Every other close is real."""

    def __init__(self, monkeypatch, m: Path):
        self.real, self.m, self.failed = locks._close, ident(m), []
        monkeypatch.setattr(locks, "_close", self)

    def __call__(self, fd):
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) == self.m and not self.failed:
            self.failed.append(fd)
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        return self.real(fd)

    def cleanup(self):
        for fd in self.failed:
            os.close(fd)


@posix
def test_b2b3_a_close_failure_after_a_successful_save_reaches_the_caller_as_oserror(with_m, monkeypatch):
    lib = with_m
    es = lib.get("P1")
    es.description = "written before the release"
    fail = FailClose(monkeypatch, lib.sets_dir / M)
    try:
        with pytest.raises(OSError) as info:
            lib.save(es)
        assert not isinstance(info.value, LibraryError)  # not a refusal
        assert info.value.errno == errno.EIO and len(fail.failed) == 1
        assert not hasattr(info.value, "close_errno")  # it is the release's own error
        # the write is done and complete, and both locks were released before the error was raised
        assert lib.get("P1").description == "written before the release" and temps(lib) == []
        assert free(lib.sets_dir) and free(lib.sets_dir / M)
    finally:
        fail.cleanup()


@posix
def test_b2b3_a_close_failure_after_a_successful_delete_reaches_the_caller_as_oserror(with_m, monkeypatch):
    lib = with_m
    fail = FailClose(monkeypatch, lib.sets_dir / M)
    try:
        with pytest.raises(OSError) as info:
            lib.delete("P1")
        assert not isinstance(info.value, LibraryError) and info.value.errno == errno.EIO
        assert not lib.exists("P1")  # the delete happened
        assert free(lib.sets_dir) and free(lib.sets_dir / M)
    finally:
        fail.cleanup()


@posix
def test_b2b3_a_failed_write_keeps_its_own_error_and_records_the_close_failure(with_m, monkeypatch):
    lib = with_m
    es = lib.get("P1")
    es.description = "must not be saved"
    before = (lib.sets_dir / "P1.json").read_bytes()
    fail = FailClose(monkeypatch, lib.sets_dir / M)

    def replace(src, dst):
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
    monkeypatch.setattr(os, "replace", replace)
    try:
        with pytest.raises(OSError) as info:
            lib.save(es)
        assert info.value.errno == errno.ENOSPC  # the write's error stays the primary result
        assert getattr(info.value, "close_errno", None) == errno.EIO and len(fail.failed) == 1
        assert (lib.sets_dir / "P1.json").read_bytes() == before and temps(lib) == []
        assert free(lib.sets_dir) and free(lib.sets_dir / M)
    finally:
        fail.cleanup()


@posix
def test_b2b3_a_clean_release_raises_nothing(with_m):
    """Control: with no close failure, the same save returns normally."""
    lib = with_m
    es = lib.get("P1")
    es.description = "plain"
    lib.save(es)
    assert lib.get("P1").description == "plain" and free(lib.sets_dir) and free(lib.sets_dir / M)
