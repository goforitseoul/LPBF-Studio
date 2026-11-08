"""Lock tests 3, 13, 14 and 15 with a real restore (revision 5 sections 2.4, 4 and 5; B2a decision 2
deferred them to b3).

* **Test 3:** while a restore holds its exclusive locks (paused at R6, R8, R9b, R10 and R11), a
  normal save is refused with nothing written; the restore's outcome is unaffected.
* **Test 13:** a new-build restore and an old-build upgrade exclude each other through M, in both
  directions, with nothing written by the one refused.
* **Test 14:** the same for an old-build report or apply; after a completed restore the old report
  is safe and the old apply writes nothing (revision 5 row 6, pinned as observed).
* **Test 15:** an early refusal after R2a leaves only an empty ``M/restores``; new-build and
  old-build reports classify every file exactly as before it existed.

The old build is the frozen fixture ``tests/legacy_build`` (commit 7516fca, pinned by
``SHA256SUMS``), run in subprocesses with its own ``PYTHONPATH``. Synthetic data only.
"""

import contextlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from lpbfstudio import restore as rs
from lpbfstudio.library import Library, LibraryError

from _restore_support import F, M, RS, fs_root, make_library, posix, report, snap  # noqa: F401

pytestmark = posix

NEW = str(Path(rs.__file__).resolve().parents[1])
OLD = str(Path(__file__).resolve().parent / "legacy_build")
BUSY_TEXT = "an upgrade, report, apply or restore is in progress for this library; nothing was saved"

RESTORE = """
import sys
from lpbfstudio import restore as rs
op, what = sys.argv[3], sys.argv[4]
def trace(o, w=""):
    if (o, w) == (op, what):
        print("paused", flush=True)
        sys.stdin.readline()
rs._trace = trace
out = rs.restore_file(sys.argv[1], sys.argv[2])
print("result|%s|%s" % (out.outcome.value, out.reason), flush=True)
"""

UPGRADE = """
import sys
from lpbfstudio import upgrade as up
if sys.argv[3] == "pause":
    real = up._replace
    def paused(src, dst):
        print("paused", flush=True)
        sys.stdin.readline()
        return real(src, dst)
    up._replace = paused
out = up.upgrade_file(sys.argv[1], sys.argv[2])
print("result|%s|%s|%s" % (out.result.value, out.busy, out.reason), flush=True)
"""

REPORT = """
import sys
from lpbfstudio import reconcile as rc, reconcile_apply as ra, upgrade as up
if sys.argv[3] == "pause":
    real = up._flock
    def paused(fd, op):
        real(fd, op)
        if not op & up.fcntl.LOCK_UN:
            print("paused", flush=True)
            sys.stdin.readline()
    up._flock = paused
if sys.argv[4] == "report":
    out = rc.reconcile_file(sys.argv[1], sys.argv[2])
    print("result|%s|%s" % (out.outcome.value, out.rule), flush=True)
else:
    out = ra.apply_file(sys.argv[1], sys.argv[2])
    print("result|%s|%s|%s" % (out.result.value, out.report.outcome.value, out.report.rule), flush=True)
"""

PAUSES = {"R3": ("survey", "R3"), "R6": ("mkdir", "W-02"), "R8": ("open", "W-08 W/f"),
          "R9b": ("read", "W-10.u1"), "R10": ("exchange", F), "R11": ("stat", "PX sets path")}


def spawn(path: str, code: str, *args: str) -> subprocess.Popen:
    env = {**os.environ, "PYTHONPATH": path, "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.Popen([sys.executable, "-c", code, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            text=True, env=env, cwd=tempfile.gettempdir())


def run(path: str, code: str, *args: str) -> str:
    p = spawn(path, code, *args)
    out, _ = p.communicate(timeout=120)
    assert p.returncode == 0, out
    return out.strip().splitlines()[-1]


@contextlib.contextmanager
def paused(path: str, code: str, *args: str):
    p = spawn(path, code, *args)

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
        p.wait(30)
        p.stdout.close()


def tree(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): (p.read_bytes() if p.is_file() else None, p.lstat().st_ino)
            for p in sorted(root.rglob("*"))}


@pytest.fixture
def sets(fs_root):
    return make_library(fs_root[1])


# ================================================================ test 3


@pytest.mark.parametrize("step", ["R6", "R8", "R9b", "R10", "R11"])
def test_3_a_save_is_refused_while_restore_holds_the_locks(sets, step):
    library = Library(sets.parent, seed=False)
    es = library.get("Q1")
    es.description = "edited during the restore"
    with paused(NEW, RESTORE, str(sets), F, *PAUSES[step]) as resume:
        before = tree(sets)
        with pytest.raises(LibraryError) as info:
            library.save(es)
        assert type(info.value).__name__ == "LibraryLocked" and str(info.value) == BUSY_TEXT
        assert tree(sets) == before
        assert resume().startswith("result|restored|")
    library.save(es)
    assert Library(sets.parent, seed=False).get("Q1").description == "edited during the restore"


# ================================================================ test 13


@pytest.mark.parametrize("step", ["R3", "R6", "R8", "R9b", "R11"])
def test_13_a_paused_new_restore_makes_an_old_upgrade_busy(sets, step):
    with paused(NEW, RESTORE, str(sets), F, *PAUSES[step]) as resume:
        before = tree(sets / M)
        out = run(OLD, UPGRADE, str(sets), "Q1.json", "go")
        assert out.split("|")[1:3] == ["failed", "True"], out
        assert tree(sets / M) == before
        assert resume().startswith("result|restored|")


@pytest.mark.parametrize("restores_exists", [False, True])
def test_13_a_paused_old_upgrade_makes_the_new_restore_busy_with_nothing_written(sets, restores_exists):
    if restores_exists:
        (sets / M / RS).mkdir()
    with paused(OLD, UPGRADE, str(sets), "Q1.json", "pause") as resume:
        before = tree(sets / M)
        out = rs.restore_file(sets, F)
        assert out.outcome is rs.Result.REFUSED_BUSY, out.reason
        assert tree(sets / M) == before and (sets / M / RS).exists() is restores_exists
        assert resume().startswith("result|upgraded|")


# ================================================================ test 14


@pytest.mark.parametrize("kind", ["report", "apply"])
def test_14_a_paused_new_restore_makes_an_old_report_or_apply_busy(sets, kind):
    with paused(NEW, RESTORE, str(sets), F, *PAUSES["R8"]) as resume:
        before = tree(sets / M)
        out = run(OLD, REPORT, str(sets), F, "go", kind).split("|")
        assert out[-1] == "G4" and "busy" in out, out
        assert tree(sets / M) == before
        assert resume().startswith("result|restored|")


@pytest.mark.parametrize("kind", ["report", "apply"])
def test_14_a_paused_old_report_or_apply_makes_the_new_restore_busy(sets, kind):
    with paused(OLD, REPORT, str(sets), F, "pause", kind) as resume:
        before = tree(sets / M)
        assert rs.restore_file(sets, F).outcome is rs.Result.REFUSED_BUSY
        assert tree(sets / M) == before
        assert resume().startswith("result|")


# Recorded from the first real run and labelled as observed behaviour of the frozen old build.
OBSERVED_14_AFTER = {"report": ["verified_restored", "P20R"], "apply": ["no_write", "verified_restored", "P20R"]}


def test_14_after_a_restore_the_old_report_is_safe_and_the_old_apply_writes_nothing(sets):
    assert rs.restore_file(sets, F).outcome is rs.Result.RESTORED
    before = snap(sets.parent)
    got = {kind: run(OLD, REPORT, str(sets), F, "go", kind).split("|")[1:] for kind in ("report", "apply")}
    assert snap(sets.parent) == before
    print("14 observed", json.dumps(got, sort_keys=True))
    assert got == OBSERVED_14_AFTER


# ================================================================ test 15


def test_15_an_empty_restores_left_by_an_early_refusal_changes_no_classification(sets):
    data = json.loads((sets / F).read_text(encoding="utf-8"))
    data["description"] = "edited after the upgrade"  # P14: refused at R3, after R2a
    (sets / F).write_text(json.dumps(data, indent=2), encoding="utf-8")
    files = sorted(p.name for p in sets.glob("*.json"))
    new_before = {n: (report(sets, n).outcome, report(sets, n).rule) for n in files}
    old_before = {n: run(OLD, REPORT, str(sets), n, "go", "report") for n in files}
    out = rs.restore_file(sets, F)
    assert out.outcome is rs.Result.REFUSED_NOT_RESTORABLE and out.restores_dir == "created-durable"
    assert (sets / M / RS).is_dir() and list((sets / M / RS).iterdir()) == []
    assert {n: (report(sets, n).outcome, report(sets, n).rule) for n in files} == new_before
    assert {n: run(OLD, REPORT, str(sets), n, "go", "report") for n in files} == old_before
