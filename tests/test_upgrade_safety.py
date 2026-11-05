"""S3-B1 pre-commit safety review: races, the library upgrade lock and platform requirements.

* Files are opened relative to already-opened folder descriptors, never following a link, and the
  opened descriptor is checked with ``fstat`` before its bytes are used.
* One upgrade at a time per library: ``upgrade.library_lock`` is an OS-level lock (``flock``) on
  the migration folder, taken before any write and held until the outcome is recorded. A second
  upgrade, in this process or another, gets an explicit busy result and writes nothing. The OS
  releases the lock when its holder exits, so there is no stale lock file to delete.
* Before the replace, the source and the record must be the same file (device and inode) as well
  as the same bytes.
* Platforms without these primitives (``dir_fd`` support, ``O_NOFOLLOW``, ``O_DIRECTORY``, ``flock``)
  are refused before anything is read or written.

Synthetic data in temporary folders only.
"""

try:
    import fcntl
except ImportError:  # Windows: the module is skipped, collection must still succeed
    fcntl = None  # type: ignore[assignment]
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from lpbfstudio import migration as mig
from lpbfstudio import upgrade as up
from lpbfstudio.library import load_json
from lpbfstudio.migration import FileResult, RecordStatus
from test_upgrade_file import ORIGINAL, lib, only_record, records_in, sets, tree  # noqa: F401 (fixtures)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX primitives; Windows is refused (see upgrade.py)")
SRC = str(Path(up.__file__).resolve().parents[1])


def lock_is_free(sets: Path) -> bool:
    try:
        with up.library_lock(sets):
            return True
    except up.LibraryBusy:
        return False


# ================================================================ platform requirements

def test_this_platform_has_the_required_primitives():
    assert up._platform_problem() is None


def test_a_platform_without_the_primitives_is_refused_before_any_io(sets, monkeypatch):
    monkeypatch.setattr(up, "_platform_problem", lambda: "os.open has no dir_fd support here")
    before = tree(sets)
    out = up.upgrade_file(sets, "P1.json")
    assert out.result is FileResult.FAILED and "dir_fd" in out.reason and "not supported" in out.reason
    assert "nothing was read" in out.reason  # refused by the first gate, before opening anything
    assert tree(sets) == before and not (sets / up.MIGRATION_DIR).exists()


@pytest.mark.parametrize("missing", ["O_NOFOLLOW", "O_DIRECTORY"])
def test_platform_problem_names_a_missing_flag(monkeypatch, missing):
    monkeypatch.delattr(os, missing)
    assert missing in up._platform_problem()


def test_platform_problem_without_flock(monkeypatch):
    monkeypatch.setattr(up, "fcntl", None)
    assert "flock" in up._platform_problem()


# ================================================================ reading through a verified descriptor

def test_read_file_refuses_a_symlink_a_folder_and_a_fifo(tmp_path):
    real = tmp_path / "real.json"
    real.write_bytes(b"{}")
    (tmp_path / "link.json").symlink_to(real)
    (tmp_path / "dir.json").mkdir()
    os.mkfifo(tmp_path / "fifo.json")  # a FIFO would block a plain open; it must be refused at once
    assert up._read_file(real) == b"{}"
    for name in ("link.json", "dir.json", "fifo.json"):
        with pytest.raises(OSError):
            up._read_file(tmp_path / name)


def test_a_fifo_in_place_of_a_set_is_refused_without_blocking(sets):
    os.mkfifo(sets / "F.json")
    done = {}
    t = threading.Thread(target=lambda: done.setdefault("out", up.upgrade_file(sets, "F.json")), daemon=True)
    t.start()
    t.join(10)
    assert not t.is_alive(), "upgrade_file blocked on a FIFO"
    assert done["out"].result is FileResult.SKIPPED_UNREADABLE


def test_source_swapped_for_a_link_after_the_first_check_is_refused(sets, tmp_path, monkeypatch):
    outside = tmp_path / "outside.json"
    outside.write_bytes(ORIGINAL)
    real = up.library_lock

    def lock_then_swap(*args, **kwargs):
        cm = real(*args, **kwargs)
        (sets / "P1.json").unlink()
        (sets / "P1.json").symlink_to(outside)  # same bytes, reached through a link
        return cm

    monkeypatch.setattr(up, "library_lock", lock_then_swap)
    out = up.upgrade_file(sets, "P1.json")
    assert out.result is FileResult.SKIPPED_UNREADABLE and "link" in out.reason
    assert outside.read_bytes() == ORIGINAL and (sets / "P1.json").is_symlink()
    assert records_in(sets) == []


def test_source_swapped_for_a_link_before_the_replace_is_not_replaced(sets, tmp_path, monkeypatch):
    outside = tmp_path / "outside.json"
    outside.write_bytes(ORIGINAL)
    real = up._stage

    def stage_then_swap(*args, **kwargs):
        staged = real(*args, **kwargs)
        (sets / "P1.json").unlink()
        (sets / "P1.json").symlink_to(outside)
        return staged

    monkeypatch.setattr(up, "_stage", stage_then_swap)
    out = up.upgrade_file(sets, "P1.json")
    assert out.result is FileResult.UNCERTAIN and "changed" in out.reason
    assert (sets / "P1.json").is_symlink() and outside.read_bytes() == ORIGINAL
    assert only_record(sets).status is RecordStatus.UNCERTAIN


def test_source_replaced_by_another_file_with_the_same_bytes_is_not_replaced(sets, monkeypatch):
    real = up._stage
    seen = {}

    def stage_then_resave(*args, **kwargs):
        staged = real(*args, **kwargs)
        other = sets / "resaved.tmp"
        other.write_bytes(ORIGINAL)
        os.replace(other, sets / "P1.json")  # an editor saving identical bytes as a new file
        seen["ino"] = os.stat(sets / "P1.json").st_ino
        return staged

    monkeypatch.setattr(up, "_stage", stage_then_resave)
    out = up.upgrade_file(sets, "P1.json")
    assert out.result is FileResult.FAILED and "changed" in out.reason
    assert (sets / "P1.json").read_bytes() == ORIGINAL and os.stat(sets / "P1.json").st_ino == seen["ino"]
    assert only_record(sets).status is RecordStatus.FAILED


def test_migration_folder_swapped_after_the_check_is_not_used(sets, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (sets / up.MIGRATION_DIR).mkdir()
    real = up.library_lock

    def lock_then_swap(*args, **kwargs):
        cm = real(*args, **kwargs)
        os.rename(sets / up.MIGRATION_DIR, tmp_path / "moved")
        (sets / up.MIGRATION_DIR).symlink_to(elsewhere, target_is_directory=True)
        return cm

    monkeypatch.setattr(up, "library_lock", lock_then_swap)
    up.upgrade_file(sets, "P1.json")
    assert list(elsewhere.iterdir()) == []  # nothing was written through the link


# ================================================================ the library upgrade lock

def test_a_held_lock_gives_a_busy_result_and_writes_nothing(sets):
    with up.library_lock(sets):
        before = tree(sets)
        out = up.upgrade_file(sets, "P1.json")
        assert out.busy and out.result is FileResult.FAILED and "another upgrade" in out.reason
        assert tree(sets) == before
    assert up.upgrade_file(sets, "P1.json").result is FileResult.UPGRADED


def test_the_lock_is_an_os_lock_on_the_migration_folder(sets):
    with up.library_lock(sets):
        fd = os.open(sets / up.MIGRATION_DIR, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
    assert lock_is_free(sets)


def test_the_lock_is_held_during_the_replace(sets, monkeypatch):
    real = up._replace
    held = []

    def spy(src, dst):
        held.append(not lock_is_free(sets))
        return real(src, dst)

    monkeypatch.setattr(up, "_replace", spy)
    assert up.upgrade_file(sets, "P1.json").result is FileResult.UPGRADED
    assert held == [True] and lock_is_free(sets)


def test_the_lock_is_released_after_success_failure_and_interruption(sets, monkeypatch):
    assert up.upgrade_file(sets, "P1.json").result is FileResult.UPGRADED
    assert lock_is_free(sets)
    (sets / "P2.json").write_bytes(ORIGINAL.replace(b'"P1"', b'"P2"'))
    monkeypatch.setattr(up, "_replace", lambda s, d: (_ for _ in ()).throw(OSError(16, "Device busy")))
    assert up.upgrade_file(sets, "P2.json").result is FileResult.FAILED
    assert lock_is_free(sets)
    (sets / "P3.json").write_bytes(ORIGINAL.replace(b'"P1"', b'"P3"'))
    monkeypatch.setattr(up, "_replace", lambda s, d: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        up.upgrade_file(sets, "P3.json")
    assert lock_is_free(sets)


def test_an_unlockable_file_system_is_refused(sets, monkeypatch):
    def no_locks(fd, op):
        raise OSError(37, "No locks available")

    monkeypatch.setattr(up, "_flock", no_locks)
    before = tree(sets)
    out = up.upgrade_file(sets, "P1.json")
    assert out.result is FileResult.FAILED and not out.busy and "lock" in out.reason
    assert {k for k in tree(sets)} == set(before)  # no record, no backup


def _holder(sets: Path) -> subprocess.Popen:
    code = ("import sys\nfrom lpbfstudio import upgrade as up\n"
            "lock = up.library_lock(sys.argv[1]); lock.__enter__()\n"
            "print('locked', flush=True); sys.stdin.read()\n")
    env = {**os.environ, "PYTHONPATH": SRC, "PYTHONDONTWRITEBYTECODE": "1"}
    p = subprocess.Popen([sys.executable, "-c", code, str(sets)], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, env=env)
    assert p.stdout.readline().strip() == b"locked"
    return p


def test_another_process_holding_the_lock_makes_the_upgrade_busy(sets):
    p = _holder(sets)
    try:
        before = tree(sets)
        out = up.upgrade_file(sets, "P1.json")
        assert out.busy and tree(sets) == before
    finally:
        p.stdin.close()
        p.wait(10)
    assert up.upgrade_file(sets, "P1.json").result is FileResult.UPGRADED


def test_a_killed_holder_leaves_no_stale_lock(sets):
    p = _holder(sets)
    assert not lock_is_free(sets)
    p.kill()
    p.wait(10)
    p.stdin.close()
    p.stdout.close()
    assert lock_is_free(sets)  # released by the OS; no lock file to delete
    assert up.upgrade_file(sets, "P1.json").result is FileResult.UPGRADED
    assert sorted(x.name for x in (sets / up.MIGRATION_DIR).iterdir()) == ["P1.json", up.RECORDS_DIR]


def test_an_interrupted_run_under_another_process_is_refused_not_repeated(sets):
    # a process killed after writing its record leaves it in progress: the next run refuses the file
    code = ("import sys, os\nfrom lpbfstudio import upgrade as up\n"
            "real = up._replace\n"
            "def die(s, d):\n    os._exit(9)\n"
            "up._replace = die\nup.upgrade_file(sys.argv[1], 'P1.json')\n")
    env = {**os.environ, "PYTHONPATH": SRC, "PYTHONDONTWRITEBYTECODE": "1"}
    assert subprocess.run([sys.executable, "-c", code, str(sets)], env=env, timeout=30).returncode == 9
    assert lock_is_free(sets)
    rec = only_record(sets)
    assert rec.status is RecordStatus.BACKED_UP
    out = up.upgrade_file(sets, "P1.json")
    assert out.result is FileResult.FAILED and "resolve it first" in out.reason
    assert (sets / "P1.json").read_bytes() == ORIGINAL and len(records_in(sets)) == 1


# ================================================================ record identity before an update

def test_record_replaced_by_another_file_with_the_same_bytes_is_not_overwritten(sets, monkeypatch):
    real = up._stage
    seen = {}

    def stage_then_resave(*args, **kwargs):
        staged = real(*args, **kwargs)
        rec = records_in(sets)[0]
        copy = rec.with_name("copy.part")
        copy.write_bytes(rec.read_bytes())
        os.replace(copy, rec)  # same bytes, another file
        seen["ino"], seen["bytes"] = os.stat(rec).st_ino, rec.read_bytes()
        return staged

    monkeypatch.setattr(up, "_stage", stage_then_resave)
    out = up.upgrade_file(sets, "P1.json")
    assert out.result is FileResult.FAILED and "record" in out.reason
    rec = records_in(sets)[0]
    assert os.stat(rec).st_ino == seen["ino"] and rec.read_bytes() == seen["bytes"]
    assert (sets / "P1.json").read_bytes() == ORIGINAL


def test_find_records_reads_through_descriptors(sets, tmp_path):
    assert up.upgrade_file(sets, "P1.json").result is FileResult.UPGRADED
    rdir = sets / up.MIGRATION_DIR / up.RECORDS_DIR
    outside = tmp_path / "outside.json"
    outside.write_text(mig.dump_record(only_record(sets)), encoding="utf-8")
    (rdir / ("ef" * 16 + ".json")).symlink_to(outside)  # a link among the records
    found = up.find_records(sets, "P1.json")
    assert any(isinstance(r, mig.RecordError) and "link" in str(r) for r in found)
    assert load_json(sets / "P1.json").schema_version == 2
