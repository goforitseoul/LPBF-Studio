"""Shared fixtures and helpers of the Restore tests (B7 Phase 2, S3-B2c-b, stage b3).

Libraries are real: a ``Library`` on ext4 (``/tmp``) or tmpfs (``/dev/shm``), with ``P1.json``
upgraded by the real ``upgrade.upgrade_file``. Failures are injected through ``restore``'s narrow
helpers, one call at a time, chosen by the helper's ``what`` label; ``Trace`` records the order of
every system call ``restore`` makes. Synthetic data in temporary folders only. Nothing here is a
power-loss test.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

import pytest

from lpbfstudio import reconcile as rc
from lpbfstudio import restore as rs
from lpbfstudio import restore_record as rr
from lpbfstudio import upgrade as up
from lpbfstudio.defaults import default_sets
from lpbfstudio.library import Library

posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX restore primitives; Windows is refused "
                                                           "(refused_platform, tested separately)")

ROOTS = {"ext4": "/tmp", "tmpfs": "/dev/shm"}
M, R, RS = up.MIGRATION_DIR, up.RECORDS_DIR, rr.RESTORES_DIR
F = "P1.json"
NOW = "2026-11-07T12:00:00Z"
Res = rs.Result
O = rc.Outcome


def set_data(name: str, version: int = 1) -> dict:
    data = json.loads(default_sets()[0].model_dump_json())
    data.update(name=name, locked=False, schema_version=version)
    return data


ORIGINAL = json.dumps(set_data("P1"), indent=3).encode() + b"\n"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture(params=sorted(ROOTS))
def fs_root(request):
    root = Path(tempfile.mkdtemp(prefix="restore-", dir=ROOTS[request.param]))
    try:
        yield request.param, root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def make_library(root: Path, *, upgrade: bool = True) -> Path:
    lib = Library(root / "lib")
    sets = lib.sets_dir
    (sets / F).write_bytes(ORIGINAL)
    (sets / "Q1.json").write_bytes(json.dumps(set_data("Q1"), indent=2).encode())
    if upgrade:
        out = up.upgrade_file(sets, F, now="2026-11-07T10:00:00Z")
        assert out.result.value == "upgraded", out.reason
    return sets


@pytest.fixture
def sets(fs_root):
    """A library whose ``P1.json`` is upgraded (P12), on ext4 or tmpfs."""
    return make_library(fs_root[1])


def upgraded_bytes(sets: Path) -> bytes:
    return (sets / F).read_bytes()


def snap(root: Path) -> dict:
    """Every entry under ``root`` (never following links): type, mode, size, inode, links, mtime and
    the SHA-256 of regular files."""
    out = {}
    for p in [root] + sorted(root.rglob("*")):
        st = os.lstat(p)
        digest = sha(p.read_bytes()) if stat.S_ISREG(st.st_mode) else None
        out[p.relative_to(root).as_posix() or "."] = (stat.S_IFMT(st.st_mode), st.st_size, st.st_ino, st.st_nlink,
                                                        st.st_mtime_ns, digest)
    return out


def names(root: Path) -> set:
    return {p.relative_to(root).as_posix() for p in root.rglob("*")}


def report(sets: Path, name: str = F):
    """The frozen report, twice: it must write nothing and give the same answer."""
    before = snap(sets.parent)
    out = rc.reconcile_file(sets, name)
    again = rc.reconcile_file(sets, name)
    assert snap(sets.parent) == before
    assert (out.outcome, out.rule) == (again.outcome, again.rule)
    return out


def attempts(sets: Path) -> list[Path]:
    d = sets / M / RS
    return sorted(p for p in d.iterdir()) if d.exists() else []


def only_attempt(sets: Path) -> Path:
    found = attempts(sets)
    assert len(found) == 1, found
    return found[0]


def record_of(a: Path) -> rr.RestoreRecord:
    return rr.parse_restore_record((a / rr.RECORD_NAME).read_text(encoding="utf-8"))


def restore(sets: Path, name: str = F, **kw) -> rs.RestoreOutcome:
    out = rs.restore_file(sets, name, now=NOW, **kw)
    assert out.reason.strip()
    return out


def lock_free(sets: Path) -> bool:
    import fcntl
    for path in (sets, sets / M):
        if not path.exists():
            continue
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        finally:
            os.close(fd)
    return True


def nothing_removed(before: set, after: set, allowed: set = frozenset()) -> None:
    """Restore never removes anything but its own record temporary file (W-16)."""
    gone = before - after - set(allowed)
    assert not gone, f"removed: {sorted(gone)}"


# --------------------------------------------------------------------------- trace and injection


class Trace:
    """Records every ``restore._trace`` event (operation, label) in order."""

    def __init__(self, monkeypatch) -> None:
        self.events: list[tuple[str, str]] = []
        monkeypatch.setattr(rs, "_trace", lambda op, what="": self.events.append((op, what)))

    def ops(self, op: str) -> list[str]:
        return [w for o, w in self.events if o == op]

    def index(self, op: str, what: str) -> int:
        return self.events.index((op, what))

    def has(self, op: str, what: str | None = None) -> bool:
        return any(o == op and (what is None or w == what) for o, w in self.events)


def oserror(code: int) -> OSError:
    return OSError(code, os.strerror(code))


def replace_same_bytes(path: Path) -> None:
    """Put a new file with the same bytes at ``path``: created beside it first and renamed over it, so
    the new inode is guaranteed to differ (an unlink then a create may reuse the inode number on
    ext4, which no identity check can see; that is the unsupported O2 case, not what is tested)."""
    tmp = path.with_name(path.name + ".replacement")
    tmp.write_bytes(path.read_bytes())
    os.replace(tmp, path)


# the trace operation each injectable helper reports (an injected failure is traced like a real call)
_OPS = {"_mkdirat": "mkdir", "_openat": "open", "_open_dir": "opendir", "_fstat": "fstat", "_lstat_at": "lstat",
        "_stat_path": "stat", "_write": "write", "_fsync": "fsync", "_close": "close", "_renameat": "rename",
        "_unlinkat": "unlink", "_listing": "list", "_read": "read", "_mount_id": "mnt_id", "_exchange": "exchange"}


def fail_at(monkeypatch, helper: str, match, exc=None, *, times: int = 1, after=None, before=None):
    """Make ``restore.<helper>`` raise ``exc`` (an EIO OSError by default) on the calls whose ``what``
    label satisfies ``match`` (a string or a predicate), ``times`` times. ``before`` runs just before
    raising, ``after`` lets the real call run first and then raises."""
    real = getattr(rs, helper)
    state = {"left": times, "calls": 0}
    test = (lambda w: w == match) if isinstance(match, str) else match

    def wrapper(*args):
        what = args[-1]
        if state["left"] and test(what):
            state["left"] -= 1
            state["calls"] += 1
            if after is not None:
                real(*args)
                after(*args)
            else:
                rs._trace(_OPS[helper], what)
            if before is not None:
                before(*args)
            raise exc if exc is not None else oserror(errno.EIO)
        return real(*args)
    monkeypatch.setattr(rs, helper, wrapper)
    return state


def exchange_raising(monkeypatch, code: int, *, effect: bool = False, before_return=None):
    """``_exchange`` that returns -1 with ``code``; with ``effect`` it performs the real exchange first."""
    calls = []

    def fake(w_fd, sets_fd, name):
        calls.append(name)
        rs._trace("exchange", name)
        if effect:
            rs.exchange(w_fd, name, sets_fd, name)
        if before_return is not None:
            before_return(w_fd, sets_fd, name)
        raise oserror(code)
    monkeypatch.setattr(rs, "_exchange", fake)
    return calls


@contextlib.contextmanager
def chdir(path: Path):
    old = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)
