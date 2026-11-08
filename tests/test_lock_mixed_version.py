"""Lock tests 10 to 12: the new build against the frozen old build (revision 5 sections 2.4, 2.5, 5).

Stage B2a of B7 Phase 2 (S3-B2c-b). The old build is the fixture in ``tests/legacy_build``, a copy of
commit ``7516fca`` (OD-8, B2a decision 4), pinned by ``SHA256SUMS``. It runs only in subprocesses with
its own ``PYTHONPATH``, and it is paused by test-only patching inside the subprocess. The fixture's
bytes are never changed.

Tests 13 and 14 need a real restore and belong to stage b3.
"""

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

try:
    import fcntl
except ImportError:  # Windows: only the fixture pins run there
    fcntl = None

from lpbfstudio import library as libmod  # noqa: E402
from lpbfstudio import upgrade as up  # noqa: E402
from lpbfstudio.defaults import default_sets  # noqa: E402
from lpbfstudio.library import Library, LibraryError  # noqa: E402

posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock behaviour; not executed on Windows")

FIXTURE = Path(__file__).resolve().parent / "legacy_build"
SOURCE_COMMIT = "7516fca75cfc5f58b06b0b8ed1c2b1b39c31a649"
NEW = str(Path(libmod.__file__).resolve().parents[1])
OLD = str(FIXTURE)
ROOTS = {"ext4": "/tmp", "tmpfs": "/dev/shm"}
M = up.MIGRATION_DIR
DEFAULT = default_sets()[0].name
BUSY_TEXT = "an upgrade, report, apply or restore is in progress for this library; nothing was saved"
ABANDONED_TEXT = "an upgrade started while saving; nothing was saved"

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

SAVE = """
import os, sys
from lpbfstudio import library as L
lib = L.Library(sys.argv[1], seed=False)
es = lib.get(sys.argv[2])
es.description = "from the paused save"
real = os.replace
def paused(src, dst):
    if os.path.basename(os.path.dirname(dst)) != "sets":  # history's staging write comes first (DC-001 S3)
        return real(src, dst)
    print("paused", flush=True)
    sys.stdin.readline()
    return real(src, dst)
os.replace = paused
lib.save(es)
print("saved", flush=True)
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


def spawn(path: str, code: str, *args: str) -> subprocess.Popen:
    env = {**os.environ, "PYTHONPATH": path, "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.Popen([sys.executable, "-c", code, *args], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True, env=env, cwd=tempfile.gettempdir())


def run(path: str, code: str, *args: str) -> str:
    p = spawn(path, code, *args)
    out, _ = p.communicate(timeout=60)
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
        p.wait(10)
        p.stdout.close()


@pytest.fixture(params=sorted(ROOTS))
def lib(request):
    root = Path(tempfile.mkdtemp(prefix="mixed-", dir=ROOTS[request.param]))
    library = Library(root / "lib")
    library.copy(DEFAULT, "P1")
    library.copy(DEFAULT, "Q1")
    try:
        yield library
    finally:
        shutil.rmtree(root, ignore_errors=True)


def tree(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): (p.read_bytes() if p.is_file() else None, p.lstat().st_ino)
            for p in sorted(root.rglob("*"))}


def records(lib: Library) -> list:
    d = lib.sets_dir / M / up.RECORDS_DIR
    return sorted(d.glob("*.json")) if d.exists() else []


def make_m(lib: Library) -> None:
    (lib.sets_dir / M).mkdir()
    (lib.sets_dir / M / up.RECORDS_DIR).mkdir()


def edited(lib: Library, name: str):
    es = lib.get(name)
    es.description = "from the new save"
    return es


def refused(fn):
    with pytest.raises(LibraryError) as info:
        fn()
    assert type(info.value).__name__ == "LibraryLocked", info.value
    return info.value


# ================================================================ the fixture (F-4, OD-8, decision 4)


def test_the_fixture_bytes_match_their_pins():
    pins = dict(reversed(line.split("  ", 1)) for line in
                (FIXTURE / "SHA256SUMS").read_text(encoding="utf-8").splitlines())
    found = {p.relative_to(FIXTURE).as_posix() for p in (FIXTURE / "lpbfstudio").glob("*.py")}
    assert set(pins) == found and len(found) == 18
    for name, digest in pins.items():
        assert hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest() == digest, name


def test_the_fixture_names_its_source_commit():
    assert SOURCE_COMMIT in (FIXTURE / "README.md").read_text(encoding="utf-8")


def test_the_fixture_takes_no_writer_lock_and_no_sets_lock():
    # the old-build behaviour revision 5 describes: writers lock nothing, upgrade locks M only
    library = (FIXTURE / "lpbfstudio" / "library.py").read_text(encoding="utf-8")
    assert "flock" not in library and "locks" not in library.replace("locked", "")
    upgrade = (FIXTURE / "lpbfstudio" / "upgrade.py").read_text(encoding="utf-8")
    assert upgrade.count("_flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)") == 1


@posix
def test_the_subprocess_really_runs_the_fixture():
    code = "import lpbfstudio; print(lpbfstudio.__file__)"
    assert Path(run(OLD, code)).resolve().parent == FIXTURE / "lpbfstudio"


# ================================================================ test 10a (rows 2a)


@posix
def test_10a_a_paused_old_upgrade_refuses_the_new_save(lib):
    make_m(lib)
    es = edited(lib, "Q1")
    with paused(OLD, UPGRADE, str(lib.sets_dir), "P1.json", "pause") as resume:
        before = tree(lib.sets_dir)
        r = refused(lambda: lib.save(es))
        assert r.result == "refused_busy" and str(r) == BUSY_TEXT
        assert tree(lib.sets_dir) == before
        assert resume().startswith("result|upgraded|")
    lib.save(es)
    assert lib.get("Q1").description == "from the new save"


@posix
def test_10a_a_paused_new_save_makes_the_old_upgrade_busy_with_no_record(lib):
    make_m(lib)
    with paused(NEW, SAVE, str(lib.root), "Q1") as resume:
        out = run(OLD, UPGRADE, str(lib.sets_dir), "P1.json", "go")
        assert out.split("|")[1:3] == ["failed", "True"]
        assert records(lib) == []
        assert resume() == "saved"
    assert lib.get("Q1").description == "from the paused save"


# ================================================================ test 10b (row 2b, the part the re-check catches)


@posix
def test_10b_the_save_abandons_when_an_old_upgrade_creates_m_before_the_recheck(lib, monkeypatch):
    es = edited(lib, "Q1")
    before = (lib.sets_dir / "Q1.json").read_bytes()
    inode = (lib.sets_dir / "Q1.json").stat().st_ino
    real = libmod.tempfile.mkstemp
    with contextlib.ExitStack() as stack:
        state = {}

        def mkstemp(*a, **k):
            out = real(*a, **k)
            if "resume" not in state:
                state["path"] = out[1]
                state["resume"] = stack.enter_context(paused(OLD, UPGRADE, str(lib.sets_dir), "P1.json", "pause"))
            return out
        monkeypatch.setattr(libmod.tempfile, "mkstemp", mkstemp)
        r = refused(lambda: lib.save(es))
        monkeypatch.undo()
        assert str(r) == ABANDONED_TEXT
        assert (lib.sets_dir / "Q1.json").read_bytes() == before
        assert (lib.sets_dir / "Q1.json").stat().st_ino == inode
        assert not Path(state["path"]).exists()  # its own temporary file, and nothing else, is gone
        assert (lib.sets_dir / M).is_dir()
        assert state["resume"]().startswith("result|upgraded|")
    assert len(records(lib)) == 1


# ================================================================ test 10c (row 2b, observational)


# Written once from the first real run, and labelled as observed behaviour. Not a guarantee.
# First run: Linux, Python 3.13.18, the same on ext4 and tmpfs. The upgrade reported success and the
# save's bytes are nowhere: this interleaving lost the save. Row 2b is unsupported (revision 5 §2.5).
OBSERVED_10C = {"backups": 1, "records": ["upgraded"], "s_format": 2, "save_bytes_in_a_backup": False,
                "save_bytes_in_s": False, "save_result": "saved", "upgrade_reason": "upgraded to format 2",
                "upgrade_result": ["upgraded", "False"]}


@posix
def test_10c_observed_row_2b_interleaving(lib):
    """Observational only. §2.4 row 2b is unsupported. This records one orchestrated interleaving and guarantees nothing about row 2b."""
    sets = lib.sets_dir
    with paused(NEW, SAVE, str(lib.root), "P1") as finish_save:
        with paused(OLD, UPGRADE, str(sets), "P1.json", "pause") as finish_upgrade:
            save_result = finish_save()
            upgrade_result = finish_upgrade()
    s = (sets / "P1.json").read_bytes()
    backups = sorted(p for p in (sets / M).rglob("*") if p.is_file() and up.RECORDS_DIR not in p.parts)
    recs = [json.loads(p.read_text(encoding="utf-8")) for p in records(lib)]
    marker = b"from the paused save"
    raw = {"size": len(s), "sha256": hashlib.sha256(s).hexdigest(), "inode": (sets / "P1.json").stat().st_ino}
    recording = {
        "save_result": save_result,
        "upgrade_result": upgrade_result.split("|")[1:3],
        "upgrade_reason": upgrade_result.split("|")[3],
        "s_format": json.loads(s)["schema_version"],
        "save_bytes_in_s": marker in s,
        "save_bytes_in_a_backup": any(marker in p.read_bytes() for p in backups),
        "backups": len(backups),
        "records": [r.get("status") for r in recs],
    }
    assert all(raw.values())  # the recording is complete
    print("10c raw", raw)
    print("10c recording", json.dumps(recording, sort_keys=True))
    assert recording == OBSERVED_10C


# ================================================================ test 11 (rows 3a, 3b)


@posix
@pytest.mark.parametrize("kind", ["report", "apply"])
def test_11a_a_paused_old_report_or_apply_refuses_the_new_save(lib, kind):
    make_m(lib)
    es = edited(lib, "Q1")
    with paused(OLD, REPORT, str(lib.sets_dir), "P1.json", "pause", kind) as resume:
        before = tree(lib.sets_dir)
        assert str(refused(lambda: lib.save(es))) == BUSY_TEXT
        assert tree(lib.sets_dir) == before
        assert resume().startswith("result|")
    lib.save(es)


@posix
@pytest.mark.parametrize("kind", ["report", "apply"])
def test_11a_a_paused_new_save_makes_the_old_report_or_apply_busy(lib, kind):
    make_m(lib)
    with paused(NEW, SAVE, str(lib.root), "Q1") as resume:
        before = tree(lib.sets_dir / M)
        out = run(OLD, REPORT, str(lib.sets_dir), "P1.json", "go", kind).split("|")
        assert out[-1] == "G4" and "busy" in out
        assert tree(lib.sets_dir / M) == before
        assert resume() == "saved"


@posix
def test_11b_with_m_absent_the_old_report_takes_no_lock_and_writes_nothing(lib):
    with paused(NEW, SAVE, str(lib.root), "Q1") as resume:
        before = tree(lib.sets_dir)
        assert run(OLD, REPORT, str(lib.sets_dir), "P1.json", "go", "report").split("|")[-1] == "G2"
        assert run(OLD, REPORT, str(lib.sets_dir), "P1.json", "go", "apply").split("|")[-1] == "G2"
        assert tree(lib.sets_dir) == before and not (lib.sets_dir / M).exists()
        assert resume() == "saved"
    assert lib.get("Q1").description == "from the paused save"


# ================================================================ test 12 (row 1)


@posix
@pytest.mark.parametrize("m_present", [False, True], ids=["m-absent", "m-present"])
@pytest.mark.parametrize("first", ["new", "old"])
def test_12_new_and_old_upgrade_exclude_each_other(lib, first, m_present):
    if m_present:
        make_m(lib)
    one, other = (NEW, OLD) if first == "new" else (OLD, NEW)
    sets = str(lib.sets_dir)
    with paused(one, UPGRADE, sets, "P1.json", "pause") as resume:
        inode = (lib.sets_dir / M).stat().st_ino
        before = tree(lib.sets_dir)
        out = run(other, UPGRADE, sets, "P1.json", "go")
        assert out.split("|")[1:3] == ["failed", "True"]
        assert tree(lib.sets_dir) == before
        assert resume().startswith("result|upgraded|")
    assert len(records(lib)) == 1
    assert (lib.sets_dir / M).stat().st_ino == inode
    assert [p.name for p in lib.sets_dir.iterdir() if p.name == M] == [M]


@posix
def test_12_each_build_can_lock_after_the_other_has_finished(lib):
    sets = str(lib.sets_dir)
    assert run(OLD, UPGRADE, sets, "P1.json", "go").startswith("result|upgraded|")
    assert run(NEW, UPGRADE, sets, "Q1.json", "go").startswith("result|upgraded|")
    assert len(records(lib)) == 2
    assert fcntl is not None
