"""Read-only reconciliation reports (B7 Phase 2, S3-B2a).

``reconcile.reconcile_file(sets_dir, file_name)`` and ``reconcile.reconcile_library(sets_dir)``
read the disk under the library upgrade lock and classify each set file, from the evidence alone,
as one of: ``verified_upgraded``, ``verified_original``, ``format_2_no_record``, ``recoverable``,
``conflict``, ``unverifiable`` or ``busy``. They follow the decision table of the S3-B2 design
report (rules G1-G5, then P1-P17, first match wins) and name the rule that decided.

S3-B2a is report only: nothing is created, written, moved or deleted, and ``apply=True`` is
refused before anything is read. ``recoverable`` names the record state that a later, separately
approved stage (S3-B2b) would write (``planned_status``); here nothing is written.

Hard-linked, linked and non-regular files are refused; unexpected entries are reported, and an
unexpected entry in the records folder keeps an interrupted run from being called recoverable.
Restore, applying, quarantine, the CLI and the GUI are not part of this stage.

Synthetic data in temporary folders only.
"""

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from lpbfstudio import migration as mig
from lpbfstudio import upgrade as up
from lpbfstudio.library import _parse_set
from lpbfstudio.migration import DiskState, FileResult, RecordStatus
from lpbfstudio import reconcile as rc
from test_upgrade_file import ORIGINAL, lib, set_data, sets  # noqa: F401 (fixtures)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX primitives; Windows is refused (see upgrade.py)")

SRC = str(Path(up.__file__).resolve().parents[1])
O = rc.Outcome
T0, T1 = "2026-10-27T08:00:00Z", "2026-10-27T08:00:05Z"
M, R = up.MIGRATION_DIR, up.RECORDS_DIR


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _upgraded_of(original: bytes) -> tuple[bytes, tuple[str, ...]]:
    target, ids = up._build_target(_parse_set(original.decode("utf-8")))
    return target.model_dump_json(indent=2).encode("utf-8"), ids


UPGRADED, IDS = _upgraded_of(ORIGINAL)
OTHER_V1 = json.dumps({**set_data("P1"), "description": "edited after a failed attempt"}, indent=2).encode()
OTHER_V2 = json.dumps({**json.loads(UPGRADED), "description": "edited after the upgrade"}, indent=2).encode()
# the format 2 file of a different original: same pass layout, other values
FOREIGN_UPGRADED, _ = _upgraded_of(json.dumps({**set_data("P1"), "description": "another set"}, indent=2).encode())


# ================================================================ helpers

def snap(root: Path) -> dict:
    """Every entry under ``root`` (and ``root``), never following links: type, mode, size, inode,
    link count, mtime and, for regular files, the SHA-256. Access times are left out (reads)."""
    out = {}
    paths = [root] + sorted(root.rglob("*"))
    for p in paths:
        st = os.lstat(p)
        digest = sha(p.read_bytes()) if stat.S_ISREG(st.st_mode) else None
        target = os.readlink(p) if stat.S_ISLNK(st.st_mode) else None
        out[p.relative_to(root).as_posix() or "."] = (stat.S_IFMT(st.st_mode), st.st_mode, st.st_size,
                                                        st.st_ino, st.st_nlink, st.st_mtime_ns, digest, target)
    return out


def rdir(sets: Path) -> Path:
    return sets / M / R


def put_record(sets: Path, status: RecordStatus, *, source: bytes = ORIGINAL, upgraded: bytes = UPGRADED,
               error: str = "stopped", name: str = "P1.json", backup_path: str | None = None) -> mig.MigrationRecord:
    rec = mig.new_record(name, backup_path or f"{M}/{name}", source, now=T0)
    if status is not RecordStatus.PREPARING:
        if status in (RecordStatus.BACKED_UP, RecordStatus.UPGRADED):
            rec = mig.advance(rec, RecordStatus.BACKED_UP, upgraded=upgraded)
        if status is RecordStatus.UPGRADED:
            rec = mig.confirm_replace(rec, DiskState.UPGRADED, now=T1)
        elif status in (RecordStatus.FAILED, RecordStatus.UNCERTAIN):
            rec = mig.advance(rec, status, error=error, now=T1)
    rdir(sets).mkdir(parents=True, exist_ok=True)
    (rdir(sets) / f"{rec.record_id}.json").write_text(mig.dump_record(rec), encoding="utf-8")
    return rec


def put_source(sets: Path, data: bytes | None, name: str = "P1.json") -> None:
    p = sets / name
    if data is None:
        p.unlink(missing_ok=True)
    else:
        p.write_bytes(data)


def put_backup(sets: Path, data: bytes | None, name: str = "P1.json") -> Path:
    (sets / M).mkdir(exist_ok=True)
    p = sets / M / name
    if data is not None:
        p.write_bytes(data)
    return p


def staged(sets: Path, data: bytes, name: str = "P1.json", token: str = "a1b2c3d4e5f6") -> Path:
    p = sets / f".{name}.{token}{up.TMP_SUFFIX}"
    p.write_bytes(data)
    return p


def record_tmp(sets: Path, rec_id: str, data: bytes, token: str = "0123456789ab") -> Path:
    p = rdir(sets) / f".{rec_id}.{token}.tmp"
    p.write_bytes(data)
    return p


def report(sets: Path, name: str = "P1.json") -> "rc.ReconcileOutcome":
    """The report for one file; asserts that it changed nothing on disk and is repeatable."""
    before = snap(sets.parent)
    out = rc.reconcile_file(sets, name)
    assert snap(sets.parent) == before, "a report must not change anything"
    again = rc.reconcile_file(sets, name)
    assert (again.outcome, again.rule, again.reason) == (out.outcome, out.rule, out.reason)
    assert out.wrote is None and again.wrote is None
    assert out.reason.strip()
    return out


def check(out, outcome, rule, *words, planned=None):
    assert (out.outcome, out.rule) == (outcome, rule), (out.outcome, out.rule, out.reason)
    for w in words:
        assert w in out.reason, (w, out.reason)
    assert out.planned_status is planned, out.planned_status


def lock_is_free(sets: Path) -> bool:
    try:
        with up.library_lock(sets):
            return True
    except up.LibraryBusy:
        return False


# ================================================================ G1: platform

NO_IO = ("open", "stat", "lstat", "listdir", "scandir", "mkdir", "rename", "replace", "unlink", "remove", "rmdir")


def _forbid_io(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("file I/O on a refused platform")
    for fn in NO_IO:
        monkeypatch.setattr(os, fn, boom)


def test_g1_platform_without_the_primitives_is_refused_before_any_io(sets, monkeypatch):
    before = snap(sets.parent)
    monkeypatch.setattr(up, "_platform_problem", lambda: "needs os.open with dir_fd")
    _forbid_io(monkeypatch)
    out = rc.reconcile_file(sets, "P1.json")
    lib_out = rc.reconcile_library(sets)
    monkeypatch.undo()
    check(out, O.UNVERIFIABLE, "G1", "not supported on this platform", "nothing was read")
    assert lib_out.blocked is O.UNVERIFIABLE and lib_out.rule == "G1" and lib_out.outcomes == ()
    assert snap(sets.parent) == before


def test_g1_windows_like_platform_without_flock_is_refused(sets, monkeypatch):
    monkeypatch.setattr(up, "fcntl", None)
    _forbid_io(monkeypatch)
    out = rc.reconcile_file(sets, "P1.json")
    monkeypatch.undo()
    check(out, O.UNVERIFIABLE, "G1", "fcntl.flock")


# ================================================================ G2: no migration folder

def test_g2_no_migration_folder_reports_without_locking_or_creating_it(sets, monkeypatch):
    monkeypatch.setattr(up, "_flock", lambda fd, op: (_ for _ in ()).throw(AssertionError("locked")))
    out = report(sets)
    check(out, O.VERIFIED_ORIGINAL, "G2", "no migration")
    check(report(sets, "V2.json"), O.FORMAT_2_NO_RECORD, "G2")
    assert not (sets / M).exists()
    lib_out = rc.reconcile_library(sets)
    assert lib_out.blocked is None and not (sets / M).exists()
    assert lib_out.get("P1.json").outcome is O.VERIFIED_ORIGINAL
    assert lib_out.get("V2.json").outcome is O.FORMAT_2_NO_RECORD
    assert [o.file_name for o in lib_out.outcomes] == sorted(p.name for p in sets.glob("*.json"))


def test_g2_still_refuses_a_linked_source(sets, tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_bytes(ORIGINAL)
    (sets / "P1.json").unlink()
    (sets / "P1.json").symlink_to(target)
    check(report(sets), O.UNVERIFIABLE, "P1", "link")


# ================================================================ G3: migration folders

@pytest.mark.parametrize("which", ["migration", "records"])
@pytest.mark.parametrize("how", ["link", "file"])
def test_g3_migration_folder_that_is_a_link_or_a_file_blocks_everything(sets, tmp_path, which, how):
    put_record(sets, RecordStatus.UPGRADED)
    put_source(sets, UPGRADED)
    put_backup(sets, ORIGINAL)
    victim = sets / M if which == "migration" else rdir(sets)
    moved = tmp_path / "moved"
    victim.rename(moved)
    if how == "link":
        victim.symlink_to(moved, target_is_directory=True)
    else:
        victim.write_bytes(b"not a folder")
    check(report(sets), O.UNVERIFIABLE, "G3")
    lib_out = rc.reconcile_library(sets)
    assert lib_out.blocked is O.UNVERIFIABLE and lib_out.rule == "G3"


def test_g3_missing_sets_folder_is_unverifiable(tmp_path):
    out = rc.reconcile_file(tmp_path / "nowhere", "P1.json")
    check(out, O.UNVERIFIABLE, "G3", "sets folder")
    assert not (tmp_path / "nowhere").exists()


def test_records_folder_missing_under_an_existing_migration_folder_is_no_record(sets):
    (sets / M).mkdir()
    check(report(sets), O.VERIFIED_ORIGINAL, "P2")
    assert not rdir(sets).exists()


# ================================================================ G4: the lock

def _holder(sets: Path) -> subprocess.Popen:
    code = ("import sys\nfrom lpbfstudio import upgrade as up\n"
            "lock = up.library_lock(sys.argv[1]); lock.__enter__()\n"
            "print('locked', flush=True); sys.stdin.read()\n")
    env = {**os.environ, "PYTHONPATH": SRC, "PYTHONDONTWRITEBYTECODE": "1"}
    p = subprocess.Popen([sys.executable, "-c", code, str(sets)], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, env=env)
    assert p.stdout.readline().strip() == b"locked"
    return p


def test_g4_lock_held_by_another_process_gives_busy_and_reads_no_records(sets, monkeypatch):
    put_record(sets, RecordStatus.PREPARING)
    p = _holder(sets)
    try:
        seen = []
        real = up._read_file
        monkeypatch.setattr(up, "_read_file", lambda path: (seen.append(path), real(path))[1])
        out = report(sets)
        check(out, O.BUSY, "G4", "another")
        assert not [x for x in seen if x.parent.name == R], "records read without the lock"
        assert rc.reconcile_library(sets).blocked is O.BUSY
    finally:
        p.stdin.close()
        p.wait(10)
        p.stdout.close()
    assert report(sets).outcome is O.RECOVERABLE  # same evidence once the lock is free


def test_g4_lock_held_in_this_process_gives_busy(sets):
    (sets / M).mkdir()
    with up.library_lock(sets):
        check(rc.reconcile_file(sets, "P1.json"), O.BUSY, "G4")


def test_g4_unlockable_file_system_is_unverifiable(sets, monkeypatch):
    (sets / M).mkdir()

    def no_locks(fd, op):
        raise OSError(37, "No locks available")
    monkeypatch.setattr(up, "_flock", no_locks)
    check(report(sets), O.UNVERIFIABLE, "G4", "lock")


def test_the_report_holds_the_lock_while_reading_and_releases_it(sets, monkeypatch):
    put_record(sets, RecordStatus.UPGRADED)
    put_source(sets, UPGRADED)
    put_backup(sets, ORIGINAL)
    held = []
    real = up._read_file

    def spy(path):
        held.append(not lock_is_free(sets))
        return real(path)
    monkeypatch.setattr(up, "_read_file", spy)
    rc.reconcile_file(sets, "P1.json")
    assert held and all(held)
    monkeypatch.undo()
    assert lock_is_free(sets)


def test_the_lock_is_released_when_the_report_raises(sets, monkeypatch):
    put_record(sets, RecordStatus.PREPARING)
    monkeypatch.setattr(up, "_read_file", lambda path: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        rc.reconcile_file(sets, "P1.json")
    monkeypatch.undo()
    assert lock_is_free(sets)


# ================================================================ G5: records that cannot be read

def _bad_records():
    rid = "0" * 32
    return {
        "empty": (f"{rid}.json", b""),
        "partial": (f"{rid}.json", mig.dump_record(mig.new_record("P1.json", f"{M}/P1.json", ORIGINAL, now=T0)).encode()[:60]),
        "not_json": (f"{rid}.json", b"{not json"),
        "invalid": (f"{rid}.json", json.dumps({"record_schema_version": 1, "status": "upgraded"}).encode()),
        "future_version": (f"{rid}.json", json.dumps({"record_schema_version": 2}).encode()),
        "misnamed": ("notarecord.json", mig.dump_record(mig.new_record("P1.json", f"{M}/P1.json", ORIGINAL, now=T0)).encode()),
    }


@pytest.mark.parametrize("case", sorted(_bad_records()))
def test_g5_any_unreadable_record_blocks_every_file(sets, case):
    name, data = _bad_records()[case]
    rdir(sets).mkdir(parents=True)
    (rdir(sets) / name).write_bytes(data)
    check(report(sets), O.UNVERIFIABLE, "G5", name)
    check(report(sets, "V2.json"), O.UNVERIFIABLE, "G5")  # another file: it cannot be told whose
    lib_out = rc.reconcile_library(sets)
    assert lib_out.blocked is O.UNVERIFIABLE and lib_out.rule == "G5" and name in lib_out.reason


def test_g5_record_that_is_a_link_blocks(sets, tmp_path):
    rec = put_record(sets, RecordStatus.UPGRADED)
    f = rdir(sets) / f"{rec.record_id}.json"
    real = tmp_path / "real.json"
    f.rename(real)
    f.symlink_to(real)
    check(report(sets), O.UNVERIFIABLE, "G5", "link")


def test_g5_record_that_is_hard_linked_blocks(sets, tmp_path):
    rec = put_record(sets, RecordStatus.UPGRADED)
    os.link(rdir(sets) / f"{rec.record_id}.json", tmp_path / "second-name.json")
    check(report(sets), O.UNVERIFIABLE, "G5", "hard link")


def test_g5_record_that_is_a_fifo_blocks_without_hanging(sets):
    rdir(sets).mkdir(parents=True)
    os.mkfifo(rdir(sets) / f"{'1' * 32}.json")
    check(report(sets), O.UNVERIFIABLE, "G5", "not a regular file")


def test_g5_record_that_is_a_folder_blocks(sets):
    (rdir(sets) / f"{'2' * 32}.json").mkdir(parents=True)
    check(report(sets), O.UNVERIFIABLE, "G5")


# ================================================================ P1: unsafe file types

def test_p1_linked_source_is_unverifiable(sets, tmp_path):
    put_record(sets, RecordStatus.UPGRADED)
    put_backup(sets, ORIGINAL)
    target = tmp_path / "t.json"
    target.write_bytes(UPGRADED)
    (sets / "P1.json").unlink()
    (sets / "P1.json").symlink_to(target)
    check(report(sets), O.UNVERIFIABLE, "P1", "link")


def test_p1_fifo_source_is_unverifiable_without_hanging(sets):
    put_record(sets, RecordStatus.PREPARING)
    (sets / "P1.json").unlink()
    os.mkfifo(sets / "P1.json")
    check(report(sets), O.UNVERIFIABLE, "P1", "not a regular file")


def test_p1_folder_in_place_of_the_source_is_unverifiable(sets):
    (sets / "P1.json").unlink()
    (sets / "P1.json").mkdir()
    check(report(sets), O.UNVERIFIABLE, "P1", "not a regular file")


def test_p1_hard_linked_source_is_unverifiable(sets, tmp_path):
    put_record(sets, RecordStatus.UPGRADED)
    put_source(sets, UPGRADED)
    put_backup(sets, ORIGINAL)
    os.link(sets / "P1.json", tmp_path / "other-name.json")
    check(report(sets), O.UNVERIFIABLE, "P1", "hard link")


def test_p1_source_that_cannot_be_read_is_unverifiable(sets, monkeypatch):
    real = up._read_file

    def denied(path):
        if path.name == "P1.json" and path.parent.name != M:
            raise PermissionError(13, "Permission denied")
        return real(path)
    monkeypatch.setattr(up, "_read_file", denied)
    put_record(sets, RecordStatus.PREPARING)
    check(report(sets), O.UNVERIFIABLE, "P1", "Permission denied")


def test_p1_source_swapped_between_lstat_and_read_is_unverifiable(sets, monkeypatch):
    put_record(sets, RecordStatus.PREPARING)
    real = up._read_file

    def swap_then_read(path):
        if path.name == "P1.json" and path.parent.name != M:
            tmp = path.with_name("swap.part")
            tmp.write_bytes(ORIGINAL)
            os.replace(tmp, path)  # same bytes, another file
        return real(path)
    monkeypatch.setattr(up, "_read_file", swap_then_read)
    check(rc.reconcile_file(sets, "P1.json"), O.UNVERIFIABLE, "P1", "changed while")


@pytest.mark.parametrize("how", ["link", "fifo", "hardlink", "folder"])
def test_p1_unsafe_backup_is_unverifiable(sets, tmp_path, how):
    put_record(sets, RecordStatus.UPGRADED)
    put_source(sets, UPGRADED)
    b = put_backup(sets, None)
    if how == "link":
        (tmp_path / "b.json").write_bytes(ORIGINAL)
        b.symlink_to(tmp_path / "b.json")
    elif how == "fifo":
        os.mkfifo(b)
    elif how == "hardlink":
        b.write_bytes(ORIGINAL)
        os.link(b, tmp_path / "b2.json")
    else:
        b.mkdir()
    out = report(sets)
    check(out, O.UNVERIFIABLE, "P1", "backup")


def test_p1_unsafe_backup_without_a_record_is_unverifiable(sets, tmp_path):
    b = put_backup(sets, None)
    (tmp_path / "b.json").write_bytes(ORIGINAL)
    b.symlink_to(tmp_path / "b.json")
    check(report(sets), O.UNVERIFIABLE, "P1", "backup")


def test_p1_record_naming_a_backup_path_this_build_never_writes_is_unverifiable(sets):
    put_record(sets, RecordStatus.PREPARING, backup_path=f"{M}/elsewhere.json")
    check(report(sets), O.UNVERIFIABLE, "P1", "backup path")


def test_p1_record_for_a_path_outside_the_sets_folder_top_level(sets):
    put_record(sets, RecordStatus.PREPARING, name="sub/P9.json")
    out = rc.reconcile_library(sets)
    assert out.blocked is None
    check(out.get("sub/P9.json"), O.UNVERIFIABLE, "P1", "not a plain set file name")
    assert out.get("P1.json").outcome is O.VERIFIED_ORIGINAL


@pytest.mark.parametrize("name", ["../P1.json", ".hidden.json", "a/b.json", "P1.txt", "", ".format1-backup"])
def test_unsafe_file_names_are_refused_without_reading(sets, name):
    out = report(sets, name)
    check(out, O.UNVERIFIABLE, "P1", "not a plain set file name")


# ================================================================ P2, P3: no record

def test_p2_format_1_without_record_or_backup_is_a_verified_original(sets):
    (sets / M).mkdir()
    out = report(sets)
    check(out, O.VERIFIED_ORIGINAL, "P2")
    assert out.source.kind == "regular" and out.source.sha256 == sha(ORIGINAL) and out.backup.kind == "missing"
    assert out.records == ()


def test_p2_format_2_without_record_is_reported_as_such(sets):
    (sets / M).mkdir()
    check(report(sets, "V2.json"), O.FORMAT_2_NO_RECORD, "P2")


def test_p2_regular_file_that_does_not_parse_is_unverifiable(sets):
    (sets / M).mkdir()
    put_source(sets, b"{broken")
    check(report(sets), O.UNVERIFIABLE, "P2", "cannot be read")


def test_p2_missing_file_without_any_evidence_is_unverifiable(sets):
    (sets / M).mkdir()
    check(report(sets, "Nothing.json"), O.UNVERIFIABLE, "P2", "no such set file")


def test_p3_backup_without_a_record_is_a_conflict(sets):
    put_backup(sets, ORIGINAL)
    check(report(sets), O.CONFLICT, "P3", "without a migration record")


def test_p3_backup_without_record_or_set_appears_in_the_library_report(sets):
    put_backup(sets, ORIGINAL, name="Gone.json")
    out = rc.reconcile_library(sets)
    check(out.get("Gone.json"), O.CONFLICT, "P3")


# ================================================================ P4: several records

def test_p4_two_unresolved_records_are_a_conflict(sets):
    put_record(sets, RecordStatus.PREPARING)
    put_record(sets, RecordStatus.BACKED_UP)
    out = report(sets)
    check(out, O.CONFLICT, "P4")
    assert len(out.records) == 2


def test_p4_in_progress_record_next_to_a_failed_one_for_other_bytes_is_a_conflict(sets):
    put_record(sets, RecordStatus.FAILED, source=OTHER_V1)
    put_record(sets, RecordStatus.PREPARING)
    check(report(sets), O.CONFLICT, "P4", "different bytes")


def test_p4_upgraded_and_uncertain_records_are_a_conflict(sets):
    put_record(sets, RecordStatus.UPGRADED)
    put_record(sets, RecordStatus.UNCERTAIN)
    put_source(sets, UPGRADED)
    put_backup(sets, ORIGINAL)
    check(report(sets), O.CONFLICT, "P4")


def test_in_progress_record_with_failed_records_for_the_same_bytes_uses_its_own_rule(sets):
    put_record(sets, RecordStatus.FAILED)
    rec = put_record(sets, RecordStatus.PREPARING)
    out = report(sets)
    check(out, O.RECOVERABLE, "P5", planned=RecordStatus.FAILED)
    assert out.record_id == rec.record_id and len(out.records) == 2


# ================================================================ P5, P6: preparing

def test_p5_preparing_with_the_original_and_no_backup_is_recoverable_as_failed(sets):
    rec = put_record(sets, RecordStatus.PREPARING)
    out = report(sets)
    check(out, O.RECOVERABLE, "P5", "unchanged", planned=RecordStatus.FAILED)
    assert out.record_id == rec.record_id and out.record_status is RecordStatus.PREPARING
    assert out.backup.kind == "missing"


@pytest.mark.parametrize("backup", [b"", ORIGINAL[:40], ORIGINAL])
def test_p5_preparing_with_any_backup_state_is_recoverable_and_reports_the_backup(sets, backup):
    put_record(sets, RecordStatus.PREPARING)
    put_backup(sets, backup)
    out = report(sets)
    check(out, O.RECOVERABLE, "P5", planned=RecordStatus.FAILED)
    assert (out.backup.size, out.backup.sha256) == (len(backup), sha(backup))


def test_p5_record_temporary_file_is_reported_but_not_trusted(sets):
    rec = put_record(sets, RecordStatus.PREPARING)
    newer = mig.advance(rec, RecordStatus.BACKED_UP, upgraded=UPGRADED)
    put_backup(sets, ORIGINAL)
    t = record_tmp(sets, rec.record_id, mig.dump_record(newer).encode())
    out = report(sets)
    check(out, O.RECOVERABLE, "P5", planned=RecordStatus.FAILED)
    assert out.record_status is RecordStatus.PREPARING
    assert [e.path for e in out.temps] == [f"{M}/{R}/{t.name}"]


def test_p5_unexpected_entry_in_the_records_folder_makes_it_a_conflict(sets):
    put_record(sets, RecordStatus.PREPARING)
    (rdir(sets) / "notes.txt").write_text("hello")
    out = report(sets)
    check(out, O.CONFLICT, "P5", "unexpected")
    assert f"{M}/{R}/notes.txt" in [e.path for e in out.unexpected]


def test_p5_record_temporary_file_of_an_unknown_record_is_unexpected(sets):
    put_record(sets, RecordStatus.PREPARING)
    t = record_tmp(sets, "f" * 32, b"{}")
    out = report(sets)
    check(out, O.CONFLICT, "P5", "unexpected")
    assert f"{M}/{R}/{t.name}" in [e.path for e in out.unexpected]


def test_p5_subfolder_in_the_records_folder_is_unexpected(sets):
    put_record(sets, RecordStatus.PREPARING)
    (rdir(sets) / "old").mkdir()
    check(report(sets), O.CONFLICT, "P5", "unexpected")


@pytest.mark.parametrize("source", [OTHER_V1, None, UPGRADED])
def test_p6_preparing_with_anything_but_the_original_is_a_conflict(sets, source):
    put_record(sets, RecordStatus.PREPARING)
    put_source(sets, source)
    check(report(sets), O.CONFLICT, "P6")


# ================================================================ P7-P10: backed_up

def test_p7_backed_up_with_the_original_is_recoverable_as_failed(sets):
    put_record(sets, RecordStatus.BACKED_UP)
    put_backup(sets, ORIGINAL)
    t = staged(sets, UPGRADED)
    out = report(sets)
    check(out, O.RECOVERABLE, "P7", "not replaced", planned=RecordStatus.FAILED)
    assert [(e.path, e.sha256) for e in out.temps] == [(t.name, sha(UPGRADED))]
    assert t.exists()  # reported, never deleted


def test_p7_partial_staged_file_is_reported(sets):
    put_record(sets, RecordStatus.BACKED_UP)
    put_backup(sets, ORIGINAL)
    t = staged(sets, UPGRADED[:30])
    out = report(sets)
    check(out, O.RECOVERABLE, "P7", planned=RecordStatus.FAILED)
    assert [e.size for e in out.temps] == [30] and t.exists()


@pytest.mark.parametrize("how", ["link", "fifo"])
def test_p7_staged_file_that_is_not_a_regular_file_makes_it_a_conflict(sets, tmp_path, how):
    put_record(sets, RecordStatus.BACKED_UP)
    put_backup(sets, ORIGINAL)
    t = sets / f".P1.json.a1b2c3d4e5f6{up.TMP_SUFFIX}"
    if how == "link":
        (tmp_path / "x").write_bytes(UPGRADED)
        t.symlink_to(tmp_path / "x")
    else:
        os.mkfifo(t)
    check(report(sets), O.CONFLICT, "P7", "unexpected")


def test_p8_backed_up_with_the_verified_upgraded_file_is_recoverable_as_upgraded(sets):
    put_record(sets, RecordStatus.BACKED_UP)
    put_source(sets, UPGRADED)
    put_backup(sets, ORIGINAL)
    out = report(sets)
    check(out, O.RECOVERABLE, "P8", planned=RecordStatus.UPGRADED)
    assert out.source.sha256 == sha(UPGRADED) and out.backup.sha256 == sha(ORIGINAL)


def test_p8_holds_with_a_record_temporary_file_holding_the_final_state(sets):
    rec = put_record(sets, RecordStatus.BACKED_UP)
    put_source(sets, UPGRADED)
    put_backup(sets, ORIGINAL)
    record_tmp(sets, rec.record_id, mig.dump_record(mig.confirm_replace(rec, DiskState.UPGRADED, now=T1)).encode())
    check(report(sets), O.RECOVERABLE, "P8", planned=RecordStatus.UPGRADED)


@pytest.mark.parametrize("backup", [None, b"", ORIGINAL[:50], OTHER_V1])
def test_p9_upgraded_bytes_with_a_missing_or_damaged_backup_is_a_conflict(sets, backup):
    put_record(sets, RecordStatus.BACKED_UP)
    put_source(sets, UPGRADED)
    put_backup(sets, backup)
    check(report(sets), O.CONFLICT, "P9", "backup")


def test_p9_upgraded_digest_of_another_original_does_not_verify(sets):
    # the record's upgraded digest matches the file, but the file is not the upgrade of the backup
    put_record(sets, RecordStatus.BACKED_UP, upgraded=FOREIGN_UPGRADED)
    put_source(sets, FOREIGN_UPGRADED)
    put_backup(sets, ORIGINAL)
    check(report(sets), O.CONFLICT, "P9", "values")


def test_p9_upgraded_digest_that_does_not_parse_as_format_2(sets):
    junk = b'{"schema_version": 2, "not": "a set"}'
    put_record(sets, RecordStatus.BACKED_UP, upgraded=junk)
    put_source(sets, junk)
    put_backup(sets, ORIGINAL)
    check(report(sets), O.CONFLICT, "P9")


@pytest.mark.parametrize("source", [OTHER_V1, OTHER_V2, None])
def test_p10_backed_up_with_anything_else_is_a_conflict(sets, source):
    put_record(sets, RecordStatus.BACKED_UP)
    put_source(sets, source)
    put_backup(sets, ORIGINAL)
    staged(sets, UPGRADED)
    out = report(sets)
    check(out, O.CONFLICT, "P10")
    assert out.temps  # the staged file is reported as evidence


# ================================================================ P11: uncertain

@pytest.mark.parametrize("source", [UPGRADED, ORIGINAL, OTHER_V2])
def test_p11_uncertain_is_always_a_conflict_whatever_the_evidence(sets, source):
    put_record(sets, RecordStatus.UNCERTAIN, error="upgraded fine, nothing to worry about")
    put_source(sets, source)
    put_backup(sets, ORIGINAL)
    check(report(sets), O.CONFLICT, "P11")


# ================================================================ P12-P15: upgraded

def test_p12_real_upgrade_is_a_verified_completed_upgrade(sets):
    assert up.upgrade_file(sets, "P1.json").result is FileResult.UPGRADED
    out = report(sets)
    check(out, O.VERIFIED_UPGRADED, "P12")
    assert out.record_status is RecordStatus.UPGRADED


def test_p12_constructed_state(sets):
    put_record(sets, RecordStatus.UPGRADED)
    put_source(sets, UPGRADED)
    put_backup(sets, ORIGINAL)
    check(report(sets), O.VERIFIED_UPGRADED, "P12")


@pytest.mark.parametrize("backup", [None, ORIGINAL[:50], OTHER_V1])
def test_p13_completed_upgrade_with_a_missing_or_damaged_backup_is_a_conflict(sets, backup):
    put_record(sets, RecordStatus.UPGRADED)
    put_source(sets, UPGRADED)
    put_backup(sets, backup)
    check(report(sets), O.CONFLICT, "P13", "backup")


def test_p14_upgraded_then_edited_is_a_completed_upgrade_changed_since(lib, sets):
    up.upgrade_file(sets, "P1.json")
    es = lib.get("P1")
    es.description = "edited after the upgrade"
    lib.save(es)
    check(report(sets), O.VERIFIED_UPGRADED, "P14", "changed since")


@pytest.mark.parametrize("source", [ORIGINAL, OTHER_V1, b"{broken", None])
def test_p15_upgraded_record_with_a_non_format_2_or_missing_file_is_a_conflict(sets, source):
    put_record(sets, RecordStatus.UPGRADED)
    put_source(sets, source)
    put_backup(sets, ORIGINAL)
    check(report(sets), O.CONFLICT, "P15")


# ================================================================ P16, P17: failed only

@pytest.mark.parametrize("backup", [None, ORIGINAL])
def test_p16_failed_with_the_original_is_a_verified_original(sets, backup):
    put_record(sets, RecordStatus.FAILED)
    put_record(sets, RecordStatus.FAILED)
    put_backup(sets, backup)
    check(report(sets), O.VERIFIED_ORIGINAL, "P16")


def test_p16_failed_with_a_blocked_backup_path_says_so(sets):
    put_record(sets, RecordStatus.FAILED)
    put_backup(sets, ORIGINAL[:40])
    check(report(sets), O.VERIFIED_ORIGINAL, "P16", "backup", "holds other bytes")


def test_p16_failed_with_an_identical_backup_says_it_matches(sets):
    put_record(sets, RecordStatus.FAILED)
    put_backup(sets, ORIGINAL)
    check(report(sets), O.VERIFIED_ORIGINAL, "P16", "the backup matches the original")


@pytest.mark.parametrize("source", [OTHER_V1, UPGRADED, None])
def test_p17_failed_with_anything_but_the_original_is_a_conflict(sets, source):
    put_record(sets, RecordStatus.FAILED)
    put_source(sets, source)
    check(report(sets), O.CONFLICT, "P17")


# ================================================================ evidence, not outcome text

def test_the_disk_decides_when_the_upgrade_outcome_text_was_wrong(sets, monkeypatch):
    # the final record update succeeded but its read-back failed. S3-B1 said "stays backed_up"
    # here while the disk held the final record; since the Q-12 fix (S3-B2b) the outcome reports
    # the record as read back from disk
    real = up._update_record

    def flaky(path, old, new):
        data = real(path, old, new)
        if new.status is not RecordStatus.BACKED_UP:
            raise up._RecordChanged("did not read back as written")
        return data
    monkeypatch.setattr(up, "_update_record", flaky)
    out = up.upgrade_file(sets, "P1.json")
    assert out.result is FileResult.UNCERTAIN and "stays backed_up" not in out.reason
    assert "reads back as upgraded" in out.reason and out.record.status is RecordStatus.UPGRADED
    monkeypatch.undo()
    check(report(sets), O.VERIFIED_UPGRADED, "P12")


def test_a_record_rewritten_on_disk_is_judged_as_it_now_reads(sets):
    up.upgrade_file(sets, "P1.json")
    f = next(rdir(sets).glob("*.json"))
    rec = mig.parse_record(f.read_text())
    fake = mig.advance(mig.new_record("P1.json", f"{M}/P1.json", ORIGINAL, now=T0), RecordStatus.FAILED,
                       error="x", now=T1).model_dump()
    fake["record_id"] = rec.record_id
    f.write_text(json.dumps(fake), encoding="utf-8")
    check(report(sets), O.CONFLICT, "P17")


def test_error_text_in_records_never_changes_a_decision(sets):
    put_record(sets, RecordStatus.FAILED, error="upgraded to format 2")
    check(report(sets), O.VERIFIED_ORIGINAL, "P16")


# ================================================================ the library report

def test_library_report_covers_sets_backups_and_records_once_each_in_order(sets):
    put_record(sets, RecordStatus.PREPARING)
    put_record(sets, RecordStatus.FAILED, name="Gone.json")
    put_backup(sets, ORIGINAL, name="Orphan.json")
    before = snap(sets.parent)
    out = rc.reconcile_library(sets)
    assert snap(sets.parent) == before
    names = [o.file_name for o in out.outcomes]
    assert names == sorted(set(names))
    assert {"P1.json", "V2.json", "Gone.json", "Orphan.json"} <= set(names)
    assert out.get("P1.json").rule == "P5" and out.get("Gone.json").rule == "P17"
    assert out.get("Orphan.json").rule == "P3"
    again = rc.reconcile_library(sets)
    assert [(o.file_name, o.outcome, o.rule, o.reason) for o in again.outcomes] == \
        [(o.file_name, o.outcome, o.rule, o.reason) for o in out.outcomes]


def test_unexpected_entries_are_listed_and_never_touched(sets):
    put_record(sets, RecordStatus.UPGRADED)
    put_source(sets, UPGRADED)
    put_backup(sets, ORIGINAL)
    (sets / M / "notes.txt").write_text("hi")
    (sets / ".P1.json.abc123.tmp").write_text("library residue")
    (sets / "README").write_text("hi")
    out = rc.reconcile_library(sets)
    paths = [e.path for e in out.unexpected]
    assert f"{M}/notes.txt" in paths and ".P1.json.abc123.tmp" in paths and "README" in paths
    assert out.get("P1.json").rule == "P12"  # entries outside the records folder are report only
    assert (sets / M / "notes.txt").exists() and (sets / "README").exists()


def test_a_messy_library_report_calls_no_writing_function(sets, monkeypatch, tmp_path):
    put_record(sets, RecordStatus.BACKED_UP)
    put_backup(sets, ORIGINAL)
    staged(sets, UPGRADED)
    put_record(sets, RecordStatus.UNCERTAIN, name="V2.json")
    os.mkfifo(sets / "Fifo.json")
    (sets / "Link.json").symlink_to(tmp_path / "nowhere.json")

    def boom(*a, **k):
        raise AssertionError("a report must not write")
    # replacing os functions removes them from os.supports_dir_fd; the platform was checked above
    assert up._platform_problem() is None
    monkeypatch.setattr(up, "_platform_problem", lambda: None)
    for name in ("_write_new", "_rename", "_replace", "_unlink", "_update_record", "_stage"):
        monkeypatch.setattr(up, name, boom)
    for name in ("mkdir", "makedirs", "rename", "replace", "unlink", "remove", "rmdir", "write", "link",
                 "symlink", "truncate", "ftruncate", "chmod", "utime"):
        monkeypatch.setattr(os, name, boom)
    out = rc.reconcile_library(sets)
    monkeypatch.undo()
    assert out.get("P1.json").rule == "P7" and out.get("Fifo.json").rule == "P1"
    assert out.get("Link.json").rule == "P1" and out.get("V2.json").rule == "P11"


def test_apply_is_refused_before_anything_is_read(sets, monkeypatch):
    put_record(sets, RecordStatus.PREPARING)
    before = snap(sets.parent)
    _forbid_io(monkeypatch)
    with pytest.raises(ValueError, match="not available"):
        rc.reconcile_file(sets, "P1.json", apply=True)
    with pytest.raises(ValueError, match="not available"):
        rc.reconcile_library(sets, apply=True)
    monkeypatch.undo()
    assert snap(sets.parent) == before


def test_upgrade_does_not_run_reconciliation(sets, monkeypatch):
    for name in ("reconcile_file", "reconcile_library"):
        monkeypatch.setattr(rc, name, lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
    put_record(sets, RecordStatus.PREPARING)
    assert up.upgrade_file(sets, "P1.json").result is FileResult.FAILED  # still blocked, as in S3-B1


def test_the_report_leaves_the_upgrade_refusal_unchanged(sets):
    put_record(sets, RecordStatus.PREPARING)
    assert report(sets).outcome is O.RECOVERABLE
    out = up.upgrade_file(sets, "P1.json")
    assert out.result is FileResult.FAILED and "resolve it first" in out.reason


def test_reconcile_module_never_writes_or_generates_ids():
    text = Path(rc.__file__).read_text(encoding="utf-8")
    for name in ("_write_new", "_update_record", "_rename", "_replace", "_unlink", "_stage", "new_pass_id",
                 "_build_target", "library_lock", "os.mkdir", "os.rename", "os.replace", "os.unlink",
                 "os.remove", "os.write", "advance(", "confirm_replace(", "dump_record"):
        assert name not in text, name
