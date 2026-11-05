"""Applying reconciliation: record-only finalisations of interrupted upgrades (B7 Phase 2, S3-B2b).

``reconcile_apply.apply_file(sets_dir, file_name)`` takes the library upgrade lock, builds the
same read-only report as ``reconcile.reconcile_file`` (S3-B2a), and writes **only** when that
report is ``recoverable`` under one of the three rules the approved design allows to write:

- P5: a ``preparing`` record whose file holds the original bytes -> ``failed``;
- P7: a ``backed_up`` record whose file holds the original bytes -> ``failed``
  (``confirm_replace(SOURCE)``);
- P8: a ``backed_up`` record whose file is the verified upgrade of an intact backup ->
  ``upgraded`` (``confirm_replace(UPGRADED)``).

The only write is that one record, replaced through ``upgrade._update_record`` (exclusive
temporary file, identity re-check, rename, read-back). Set files, backups, staged files, record
temporary files and every other entry are never written, moved, renamed or deleted, and no pass
id is generated. Every other rule (G1-G5, P1-P4, P6, P9-P17) writes nothing. Before writing, the
record, the file and (P8) the backup are read again and must be the same files with the same
bytes that decided the rule. A second run on an unchanged disk writes nothing.

The read-only report (``reconcile_file`` / ``reconcile_library``, ``apply=True`` still refused)
is unchanged. There is no library-wide apply, CLI, GUI, restore, quarantine or clean-up.

Synthetic data in temporary folders only.
"""

import errno
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from lpbfstudio import migration as mig
from lpbfstudio import reconcile as rc
from lpbfstudio import upgrade as up
from lpbfstudio.library import load_json
from lpbfstudio.migration import DiskState, FileResult, RecordStatus
from lpbfstudio.models import is_valid_pass_id
from test_reconcile_kill_points import kill_at
from test_reconcile_report import (  # noqa: F401
    FOREIGN_UPGRADED, IDS, OTHER_V1, OTHER_V2, T0, T1, UPGRADED, lock_is_free, put_backup, put_record,
    put_source, rdir, record_tmp, sha, snap, staged,
)
from test_upgrade_file import ORIGINAL, lib, sets  # noqa: F401 (fixtures)

try:
    from lpbfstudio import reconcile_apply as ra
except ImportError:  # recorded before the implementation existed: every test then fails, none is skipped
    ra = None

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX primitives; Windows is refused (see upgrade.py)")

SRC = str(Path(up.__file__).resolve().parents[1])
O, A = rc.Outcome, getattr(ra, "ApplyResult", None)
M, R = up.MIGRATION_DIR, up.RECORDS_DIR
T2 = "2099-01-01T00:00:00Z"  # after every created_at, also of records written by real upgrades during the test


# ================================================================ helpers

def rpath(sets: Path, rec: mig.MigrationRecord) -> Path:
    return rdir(sets) / f"{rec.record_id}.json"


def on_disk(sets: Path, rec: mig.MigrationRecord) -> mig.MigrationRecord:
    return mig.parse_record(rpath(sets, rec).read_text(encoding="utf-8"))


def changed(before: dict, after: dict) -> set[str]:
    return {k for k in set(before) | set(after) if before.get(k) != after.get(k)}


def finalised(sets: Path, rec: mig.MigrationRecord, expected: mig.MigrationRecord, rule: str):
    """Apply once; assert that exactly this record changed, to ``expected``, and nothing else."""
    before = snap(sets.parent)
    out = ra.apply_file(sets, "P1.json", now=T2)
    after = snap(sets.parent)
    assert out.result is A.FINALISED, (out.result, out.reason)
    assert out.report.rule == rule and out.report.outcome is O.RECOVERABLE
    assert out.record_id == rec.record_id and out.status_before is rec.status
    assert out.status_after is expected.status
    assert out.wrote == rpath(sets, rec)
    assert on_disk(sets, rec) == expected
    rel = rpath(sets, rec).relative_to(sets.parent).as_posix()
    folder = rdir(sets).relative_to(sets.parent).as_posix()
    # only the record (new inode, new bytes) and its folder's own entry (mtime) changed
    assert changed(before, after) <= {rel, folder}, changed(before, after)
    assert rel in changed(before, after)  # no entry was added or removed anywhere (no temporary file left)
    assert lock_is_free(sets)
    return out


def nothing_written(sets: Path, name: str = "P1.json"):
    """Apply; assert that nothing at all changed and that the outcome is the report's."""
    before = snap(sets.parent)
    expected = rc.reconcile_file(sets, name)
    out = ra.apply_file(sets, name, now=T2)
    assert snap(sets.parent) == before, changed(before, snap(sets.parent))
    assert out.result is A.NO_WRITE, (out.result, out.reason)
    assert out.wrote is None and out.status_after is None
    assert (out.report.outcome, out.report.rule, out.report.reason) == (expected.outcome, expected.rule,
                                                                       expected.reason)
    assert out.reason.strip()
    return out


def forbid_record_writes(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a record write was attempted")
    for name in ("_update_record", "_write_new", "_rename", "_replace", "_unlink", "_stage"):
        monkeypatch.setattr(up, name, boom)


# ================================================================ P5, P7, P8: the only writes

@pytest.mark.parametrize("backup", [None, b"", ORIGINAL[:40], ORIGINAL])
def test_p5_preparing_with_the_original_is_finalised_as_failed(sets, backup):
    rec = put_record(sets, RecordStatus.PREPARING)
    if backup is not None:
        put_backup(sets, backup)
    expected = mig.advance(rec, RecordStatus.FAILED, error=ra.PREPARING_ERROR, now=T2)
    out = finalised(sets, rec, expected, "P5")
    assert "unchanged" in out.reason
    assert (sets / "P1.json").read_bytes() == ORIGINAL
    if backup is not None:
        assert (sets / M / "P1.json").read_bytes() == backup  # never touched, even when partial
    after = rc.reconcile_file(sets, "P1.json")
    assert (after.outcome, after.rule) == (O.VERIFIED_ORIGINAL, "P16")


def test_p5_error_text_says_what_happened():
    assert "unchanged" in ra.PREPARING_ERROR and "backup" in ra.PREPARING_ERROR


def test_p7_backed_up_with_the_original_is_finalised_as_failed(sets):
    rec = put_record(sets, RecordStatus.BACKED_UP)
    put_backup(sets, ORIGINAL)
    t = staged(sets, UPGRADED)
    expected = mig.confirm_replace(rec, DiskState.SOURCE, now=T2)
    finalised(sets, rec, expected, "P7")
    assert on_disk(sets, rec).error == "the file was not replaced; it is unchanged"
    assert t.read_bytes() == UPGRADED  # the staged file is reported, never removed or used
    after = rc.reconcile_file(sets, "P1.json")
    assert (after.outcome, after.rule) == (O.VERIFIED_ORIGINAL, "P16")
    assert [e.path for e in after.temps] == [t.name]


def test_p8_backed_up_with_the_verified_upgrade_is_finalised_as_upgraded(sets):
    rec = put_record(sets, RecordStatus.BACKED_UP)
    put_source(sets, UPGRADED)
    put_backup(sets, ORIGINAL)
    expected = mig.confirm_replace(rec, DiskState.UPGRADED, now=T2)
    finalised(sets, rec, expected, "P8")
    assert on_disk(sets, rec).error is None and on_disk(sets, rec).upgraded_sha256 == sha(UPGRADED)
    assert (sets / "P1.json").read_bytes() == UPGRADED and (sets / M / "P1.json").read_bytes() == ORIGINAL
    after = rc.reconcile_file(sets, "P1.json")
    assert (after.outcome, after.rule) == (O.VERIFIED_UPGRADED, "P12")


def test_p8_keeps_a_record_temporary_file_holding_the_final_state(sets):
    rec = put_record(sets, RecordStatus.BACKED_UP)
    put_source(sets, UPGRADED)
    put_backup(sets, ORIGINAL)
    rt = record_tmp(sets, rec.record_id, mig.dump_record(mig.confirm_replace(rec, DiskState.UPGRADED, now=T1)).encode())
    data = rt.read_bytes()
    finalised(sets, rec, mig.confirm_replace(rec, DiskState.UPGRADED, now=T2), "P8")
    assert rt.read_bytes() == data  # not authoritative, not used, not removed


def test_the_written_record_keeps_every_other_field(sets):
    rec = put_record(sets, RecordStatus.BACKED_UP)
    put_backup(sets, ORIGINAL)
    ra.apply_file(sets, "P1.json", now=T2)
    new = on_disk(sets, rec).model_dump()
    old = rec.model_dump()
    for key in ("status", "completed_at", "error"):
        new.pop(key), old.pop(key)
    assert new == old


def test_now_defaults_to_the_current_time(sets):
    rec = put_record(sets, RecordStatus.PREPARING)
    assert ra.apply_file(sets, "P1.json").result is A.FINALISED
    got = on_disk(sets, rec)
    assert got.status is RecordStatus.FAILED and got.completed_at >= rec.created_at


def test_the_lock_is_held_while_the_record_is_written(sets, monkeypatch):
    put_record(sets, RecordStatus.PREPARING)
    held, real = [], up._update_record

    def spy(*a, **k):
        held.append(not lock_is_free(sets))
        return real(*a, **k)
    monkeypatch.setattr(up, "_update_record", spy)
    assert ra.apply_file(sets, "P1.json").result is A.FINALISED
    assert held == [True]
    monkeypatch.undo()
    assert lock_is_free(sets)


def test_only_the_named_file_is_finalised(sets):
    a = put_record(sets, RecordStatus.PREPARING)
    (sets / "P2.json").write_bytes(ORIGINAL)
    b = put_record(sets, RecordStatus.PREPARING, name="P2.json")
    assert ra.apply_file(sets, "P1.json", now=T2).result is A.FINALISED
    assert on_disk(sets, a).status is RecordStatus.FAILED
    assert on_disk(sets, b) == b  # the other file's record is untouched


def test_no_library_wide_apply_is_offered():
    assert not hasattr(ra, "apply_library")
    with pytest.raises(ValueError, match="not available"):
        rc.reconcile_library(Path("."), apply=True)


# ================================================================ after a real interrupted upgrade

@pytest.mark.parametrize("point, rule, status", [
    ("F2", "P5", RecordStatus.FAILED), ("F3a", "P5", RecordStatus.FAILED), ("F4b", "P5", RecordStatus.FAILED),
    ("F5a", "P7", RecordStatus.FAILED), ("F6a-before", "P7", RecordStatus.FAILED),
    ("F7a", "P8", RecordStatus.UPGRADED), ("F8a", "P8", RecordStatus.UPGRADED),
])
def test_a_killed_upgrade_is_finalised_and_a_second_apply_writes_nothing(sets, point, rule, status):
    kill_at(sets, point)
    file_before = (sets / "P1.json").read_bytes()
    out = ra.apply_file(sets, "P1.json", now=T2)
    assert (out.result, out.report.rule, out.status_after) == (A.FINALISED, rule, status), out.reason
    assert (sets / "P1.json").read_bytes() == file_before
    again = nothing_written(sets)
    assert again.report.rule == ("P16" if status is RecordStatus.FAILED else "P12")


@pytest.mark.parametrize("point", ["F2", "F5a", "F6a-before"])
def test_after_finalising_as_failed_the_file_can_be_upgraded_again_with_new_ids(sets, point):
    kill_at(sets, point)
    staged_ids = set()
    for t in sets.glob(f".P1.json.*{up.TMP_SUFFIX}"):
        try:
            staged_ids |= {p.id for p in load_json(t).get("contour").passes}
        except Exception:  # a partial staged file
            pass
    assert ra.apply_file(sets, "P1.json").result is A.FINALISED
    out = up.upgrade_file(sets, "P1.json")
    assert out.result is FileResult.UPGRADED, out.reason
    ids = [p.id for p in load_json(sets / "P1.json").get("contour").passes]
    assert all(is_valid_pass_id(i) for i in ids) and not set(ids) & staged_ids  # never the uncommitted ids
    assert (sets / M / "P1.json").read_bytes() == ORIGINAL


def test_after_finalising_as_upgraded_the_upgrade_skips_the_file(sets):
    kill_at(sets, "F8a")
    data = (sets / "P1.json").read_bytes()
    assert ra.apply_file(sets, "P1.json").result is A.FINALISED
    assert up.upgrade_file(sets, "P1.json").result is FileResult.SKIPPED_V2
    assert (sets / "P1.json").read_bytes() == data


# ================================================================ every other rule writes nothing

def _p1_link(sets, tmp_path):
    put_record(sets, RecordStatus.PREPARING)
    (tmp_path / "t.json").write_bytes(ORIGINAL)
    (sets / "P1.json").unlink()
    (sets / "P1.json").symlink_to(tmp_path / "t.json")


def _p1_hardlink(sets, tmp_path):
    put_record(sets, RecordStatus.PREPARING)
    os.link(sets / "P1.json", tmp_path / "second")


def _p1_unsafe_backup(sets, tmp_path):
    put_record(sets, RecordStatus.BACKED_UP)
    put_source(sets, UPGRADED)
    put_backup(sets, None)
    (tmp_path / "b").write_bytes(ORIGINAL)
    os.link(tmp_path / "b", sets / M / "P1.json")


def _p1_foreign_backup_path(sets, tmp_path):
    put_record(sets, RecordStatus.PREPARING, backup_path=f"{M}/elsewhere.json")


def _p3(sets, tmp_path):
    put_backup(sets, ORIGINAL)


def _p2_staged_without_record(sets, tmp_path):
    (sets / M).mkdir()
    staged(sets, UPGRADED)


def _orphan_record_tmp(sets, tmp_path):
    put_record(sets, RecordStatus.PREPARING)
    record_tmp(sets, "f" * 32, b"{}")


def _unexpected_in_records(sets, tmp_path):
    put_record(sets, RecordStatus.PREPARING)
    (rdir(sets) / "notes.txt").write_text("x")


def _p4(sets, tmp_path):
    put_record(sets, RecordStatus.PREPARING)
    put_record(sets, RecordStatus.BACKED_UP)


def _p4_failed_other_bytes(sets, tmp_path):
    put_record(sets, RecordStatus.FAILED, source=OTHER_V1)
    put_record(sets, RecordStatus.PREPARING)


def _p6(source):
    def build(sets, tmp_path):
        put_record(sets, RecordStatus.PREPARING)
        put_source(sets, source)
    return build


def _p7_unsafe_staged(sets, tmp_path):
    put_record(sets, RecordStatus.BACKED_UP)
    put_backup(sets, ORIGINAL)
    os.mkfifo(sets / f".P1.json.a1b2c3d4e5f6{up.TMP_SUFFIX}")


def _p9(backup):
    def build(sets, tmp_path):
        put_record(sets, RecordStatus.BACKED_UP)
        put_source(sets, UPGRADED)
        put_backup(sets, backup)
    return build


def _p9_foreign(sets, tmp_path):
    put_record(sets, RecordStatus.BACKED_UP, upgraded=FOREIGN_UPGRADED)
    put_source(sets, FOREIGN_UPGRADED)
    put_backup(sets, ORIGINAL)


def _p10(source):
    def build(sets, tmp_path):
        put_record(sets, RecordStatus.BACKED_UP)
        put_backup(sets, ORIGINAL)
        put_source(sets, source)
    return build


def _p11(source):
    def build(sets, tmp_path):
        put_record(sets, RecordStatus.UNCERTAIN, upgraded=UPGRADED)
        put_backup(sets, ORIGINAL)
        put_source(sets, source)
    return build


def _upgraded(source, backup=ORIGINAL):
    def build(sets, tmp_path):
        put_record(sets, RecordStatus.UPGRADED)
        put_backup(sets, backup)
        put_source(sets, source)
    return build


def _failed(source, backup=None):
    def build(sets, tmp_path):
        put_record(sets, RecordStatus.FAILED)
        put_backup(sets, backup)
        put_source(sets, source)
    return build


NO_WRITE_STATES = {
    "G2 no migration folder": (lambda sets, tmp_path: None, "G2"),
    "P1 linked file": (_p1_link, "P1"),
    "P1 hard-linked file": (_p1_hardlink, "P1"),
    "P1 hard-linked backup": (_p1_unsafe_backup, "P1"),
    "P1 foreign backup path": (_p1_foreign_backup_path, "P1"),
    "P2 records folder without a record": (lambda sets, tmp_path: rdir(sets).mkdir(parents=True), "P2"),
    "P2 staged file without a record (H-2)": (_p2_staged_without_record, "P2"),
    "P3 backup without a record": (_p3, "P3"),
    "P4 two unresolved records": (_p4, "P4"),
    "P4 failed record for other bytes": (_p4_failed_other_bytes, "P4"),
    "P5 with an orphan record temporary file": (_orphan_record_tmp, "P5"),
    "P5 with an unexpected entry in records": (_unexpected_in_records, "P5"),
    "P6 edited": (_p6(OTHER_V1), "P6"),
    "P6 missing": (_p6(None), "P6"),
    "P6 upgraded bytes": (_p6(UPGRADED), "P6"),
    "P7 with an unsafe staged file": (_p7_unsafe_staged, "P7"),
    "P9 backup missing": (_p9(None), "P9"),
    "P9 backup empty": (_p9(b""), "P9"),
    "P9 backup partial": (_p9(ORIGINAL[:50]), "P9"),
    "P9 backup different": (_p9(OTHER_V1), "P9"),
    "P9 upgrade of another original": (_p9_foreign, "P9"),
    "P10 edited format 1": (_p10(OTHER_V1), "P10"),
    "P10 edited format 2": (_p10(OTHER_V2), "P10"),
    "P10 missing": (_p10(None), "P10"),
    "P11 uncertain, file looks upgraded": (_p11(UPGRADED), "P11"),
    "P11 uncertain, file is the original": (_p11(ORIGINAL), "P11"),
    "P11 uncertain, file edited": (_p11(OTHER_V2), "P11"),
    "P12 verified upgrade": (_upgraded(UPGRADED), "P12"),
    "P13 upgraded, backup missing": (_upgraded(UPGRADED, None), "P13"),
    "P14 upgraded, changed since": (_upgraded(OTHER_V2), "P14"),
    "P14 upgraded, unrelated format 2, no backup": (_upgraded(FOREIGN_UPGRADED, None), "P14"),
    "P15 upgraded record, original back": (_upgraded(ORIGINAL), "P15"),
    "P15 upgraded record, missing file": (_upgraded(None), "P15"),
    "P16 failed, unchanged": (_failed(ORIGINAL), "P16"),
    "P16 failed, blocked backup path": (_failed(ORIGINAL, ORIGINAL[:20]), "P16"),
    "P17 stale failed, edited": (_failed(OTHER_V1), "P17"),
    "P17 stale failed, missing": (_failed(None), "P17"),
}


@pytest.mark.parametrize("case", sorted(NO_WRITE_STATES))
def test_every_other_rule_writes_nothing(sets, tmp_path, monkeypatch, case):
    build, rule = NO_WRITE_STATES[case]
    build(sets, tmp_path)
    forbid_record_writes(monkeypatch)
    out = nothing_written(sets)
    assert out.report.rule == rule, (out.report.rule, out.report.reason)
    assert out.report.outcome is not O.RECOVERABLE
    assert out.record_id == out.report.record_id


@pytest.mark.parametrize("case", sorted(NO_WRITE_STATES))
def test_every_other_rule_still_writes_nothing_on_a_second_run(sets, tmp_path, case):
    NO_WRITE_STATES[case][0](sets, tmp_path)
    first = nothing_written(sets)
    second = nothing_written(sets)
    assert (first.report.rule, first.report.reason) == (second.report.rule, second.report.reason)


def test_g2_apply_does_not_create_the_migration_folder(sets):
    nothing_written(sets)
    nothing_written(sets, "V2.json")
    assert not (sets / M).exists()


def test_g1_refused_platform_writes_and_reads_nothing(sets, monkeypatch):
    put_record(sets, RecordStatus.PREPARING)
    before = snap(sets.parent)
    monkeypatch.setattr(up, "_platform_problem", lambda: "needs os.open with dir_fd")
    for fn in ("open", "stat", "lstat", "listdir", "scandir", "mkdir", "rename", "replace", "unlink"):
        monkeypatch.setattr(os, fn, lambda *a, **k: (_ for _ in ()).throw(AssertionError("I/O")))
    try:
        out = ra.apply_file(sets, "P1.json")
    finally:
        monkeypatch.undo()  # pytest itself needs os.stat to report a failure
    assert (out.result, out.report.rule) == (A.NO_WRITE, "G1")
    assert snap(sets.parent) == before


@pytest.mark.parametrize("which", ["migration", "records"])
def test_g3_linked_migration_folders_write_nothing(sets, tmp_path, which):
    put_record(sets, RecordStatus.PREPARING)
    target = sets / M if which == "migration" else rdir(sets)
    moved = tmp_path / "moved"
    target.rename(moved)
    target.symlink_to(moved)
    assert nothing_written(sets).report.rule == "G3"


def test_g4_busy_library_writes_nothing(sets):
    rec = put_record(sets, RecordStatus.PREPARING)
    with up.library_lock(sets):
        out = ra.apply_file(sets, "P1.json")
    assert (out.result, out.report.outcome, out.report.rule) == (A.NO_WRITE, O.BUSY, "G4")
    assert on_disk(sets, rec) == rec


def test_g4_busy_library_held_by_another_process_writes_nothing(sets):
    rec = put_record(sets, RecordStatus.PREPARING)
    code = ("import sys\nfrom lpbfstudio import upgrade as up\n"
            "with up.library_lock(sys.argv[1]):\n    print('held', flush=True)\n    sys.stdin.readline()\n")
    env = {**os.environ, "PYTHONPATH": SRC, "PYTHONDONTWRITEBYTECODE": "1"}
    p = subprocess.Popen([sys.executable, "-c", code, str(sets)], env=env, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "held"
        out = ra.apply_file(sets, "P1.json")
        assert out.result is A.NO_WRITE and out.report.outcome is O.BUSY
    finally:
        p.stdin.close()
        p.wait(10)
        p.stdout.close()
    assert on_disk(sets, rec) == rec
    assert ra.apply_file(sets, "P1.json").result is A.FINALISED  # once the lock is free


def test_g5_unreadable_record_blocks_every_write(sets):
    rec = put_record(sets, RecordStatus.PREPARING)
    (rdir(sets) / f"{'e' * 32}.json").write_text("{not json")
    out = nothing_written(sets)
    assert out.report.rule == "G5"
    assert on_disk(sets, rec) == rec


@pytest.mark.parametrize("name", ["../P1.json", ".hidden.json", "a/b.json", "P1.txt", "", M])
def test_unsafe_file_names_write_nothing(sets, name):
    put_record(sets, RecordStatus.PREPARING)
    before = snap(sets.parent)
    out = ra.apply_file(sets, name)
    assert (out.result, out.report.outcome, out.report.rule) == (A.NO_WRITE, O.UNVERIFIABLE, "P1")
    assert snap(sets.parent) == before


# ================================================================ re-checks just before the write

def _after_decision(monkeypatch, action):
    """Run ``action`` once, right after the decision and before any write (a lock-ignoring writer)."""
    real = rc._decide

    def decide(sv, name):
        out = real(sv, name)
        action()
        return out
    monkeypatch.setattr(rc, "_decide", decide)


def _resave(path: Path, data: bytes) -> None:
    tmp = path.with_name(".resave")
    tmp.write_bytes(data)
    os.replace(tmp, path)  # a new inode, as Library.save makes


@pytest.mark.parametrize("how", ["same bytes, new file", "other valid record"])
def test_record_changed_after_the_decision_is_not_overwritten(sets, monkeypatch, how):
    rec = put_record(sets, RecordStatus.PREPARING)
    p = rpath(sets, rec)
    data = p.read_bytes() if how.startswith("same") else \
        mig.dump_record(mig.advance(rec, RecordStatus.UNCERTAIN, error="someone else", now=T1)).encode()
    _after_decision(monkeypatch, lambda: _resave(p, data))
    out = ra.apply_file(sets, "P1.json", now=T2)
    assert out.result is A.REFUSED and out.wrote is None, (out.result, out.reason)
    assert "record" in out.reason and "nothing was written" in out.reason
    assert p.read_bytes() == data
    assert sorted(x.name for x in rdir(sets).iterdir()) == [p.name]  # no temporary file left


@pytest.mark.parametrize("how", ["edited", "same bytes, new file", "removed"])
def test_file_changed_after_the_decision_is_refused(sets, monkeypatch, how):
    rec = put_record(sets, RecordStatus.BACKED_UP)
    put_backup(sets, ORIGINAL)
    src = sets / "P1.json"
    actions = {"edited": lambda: src.write_bytes(OTHER_V1), "same bytes, new file": lambda: _resave(src, ORIGINAL),
               "removed": lambda: src.unlink()}
    _after_decision(monkeypatch, actions[how])
    out = ra.apply_file(sets, "P1.json", now=T2)
    assert out.result is A.REFUSED and out.wrote is None, (out.result, out.reason)
    assert "nothing was written" in out.reason
    assert on_disk(sets, rec) == rec


@pytest.mark.parametrize("how", ["damaged", "same bytes, new file", "removed"])
def test_p8_backup_changed_after_the_decision_is_refused(sets, monkeypatch, how):
    rec = put_record(sets, RecordStatus.BACKED_UP)
    put_source(sets, UPGRADED)
    b = put_backup(sets, ORIGINAL)
    actions = {"damaged": lambda: b.write_bytes(ORIGINAL[:10]), "same bytes, new file": lambda: _resave(b, ORIGINAL),
               "removed": lambda: b.unlink()}
    _after_decision(monkeypatch, actions[how])
    out = ra.apply_file(sets, "P1.json", now=T2)
    assert out.result is A.REFUSED, (out.result, out.reason)
    assert on_disk(sets, rec) == rec


# ================================================================ failures while writing

def test_temporary_record_file_cannot_be_created(sets, monkeypatch):
    rec = put_record(sets, RecordStatus.PREPARING)
    before = snap(sets.parent)

    def full(path, data):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(up, "_write_new", full)
    out = ra.apply_file(sets, "P1.json", now=T2)
    assert out.result is A.REFUSED and out.wrote is None, (out.result, out.reason)
    assert "No space left" in out.reason and "unchanged" in out.reason
    assert snap(sets.parent) == before
    assert on_disk(sets, rec) == rec
    assert lock_is_free(sets)


def test_rename_failure_leaves_the_record_unchanged_and_no_temporary_file(sets, monkeypatch):
    rec = put_record(sets, RecordStatus.BACKED_UP)
    put_backup(sets, ORIGINAL)

    def broken(src, dst):
        raise OSError(errno.EIO, "Input/output error")
    monkeypatch.setattr(up, "_rename", broken)
    out = ra.apply_file(sets, "P1.json", now=T2)
    assert out.result is A.REFUSED, (out.result, out.reason)
    assert "Input/output error" in out.reason
    assert on_disk(sets, rec) == rec
    assert sorted(x.name for x in rdir(sets).iterdir()) == [rpath(sets, rec).name]  # its own temp removed
    monkeypatch.undo()
    assert ra.apply_file(sets, "P1.json", now=T2).result is A.FINALISED


def test_read_back_failure_after_the_rename_is_unverified_and_the_disk_decides(sets, monkeypatch):
    rec = put_record(sets, RecordStatus.PREPARING)
    renamed, real_rename, real_read = [], up._rename, up._read_file

    def rename(src, dst):
        real_rename(src, dst)
        renamed.append(dst)

    def read(path):
        if renamed and path == renamed[0]:
            raise OSError(errno.EIO, "Input/output error")
        return real_read(path)
    monkeypatch.setattr(up, "_rename", rename)
    monkeypatch.setattr(up, "_read_file", read)
    out = ra.apply_file(sets, "P1.json", now=T2)
    monkeypatch.undo()
    assert out.result is A.UNVERIFIED and out.wrote is None, (out.result, out.reason)
    assert "report" in out.reason  # tells the caller to look at the disk again
    assert on_disk(sets, rec).status is RecordStatus.FAILED  # it did happen; the next report shows it
    assert nothing_written(sets).report.rule == "P16"


def test_read_back_showing_other_bytes_is_unverified(sets, monkeypatch):
    rec = put_record(sets, RecordStatus.PREPARING)
    real = up._rename

    def rename(src, dst):
        real(src, dst)
        dst.write_bytes(dst.read_bytes() + b" ")  # a lock-ignoring writer right after the rename
    monkeypatch.setattr(up, "_rename", rename)
    out = ra.apply_file(sets, "P1.json", now=T2)
    assert out.result is A.UNVERIFIED and out.wrote is None, (out.result, out.reason)
    assert rpath(sets, rec).exists()


def test_keyboard_interrupt_before_the_rename_leaves_the_record_and_releases_the_lock(sets, monkeypatch):
    rec = put_record(sets, RecordStatus.PREPARING)
    monkeypatch.setattr(up, "_rename", lambda src, dst: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        ra.apply_file(sets, "P1.json", now=T2)
    monkeypatch.undo()
    assert on_disk(sets, rec) == rec
    assert sorted(x.name for x in rdir(sets).iterdir()) == [rpath(sets, rec).name]
    assert lock_is_free(sets)
    assert ra.apply_file(sets, "P1.json", now=T2).result is A.FINALISED


# ================================================================ apply killed while writing

APPLY_KILLS = {
    # the new record state written to its temporary file, killed before the rename
    "before rename": ("real = up._rename\n"
                      "def r(src, dst):\n"
                      "    os._exit(9)\n"
                      "up._rename = r\n"),
    # killed right after the rename, before the read-back
    "after rename": ("real = up._rename\n"
                     "def r(src, dst):\n"
                     "    real(src, dst)\n"
                     "    os._exit(9)\n"
                     "up._rename = r\n"),
}


def kill_apply(sets: Path, point: str) -> None:
    code = ("import os, sys\nfrom lpbfstudio import upgrade as up\nfrom lpbfstudio import reconcile_apply as ra\n" +
            APPLY_KILLS[point] + f"ra.apply_file(sys.argv[1], 'P1.json', now={T2!r})\nos._exit(3)\n")
    env = {**os.environ, "PYTHONPATH": SRC, "PYTHONDONTWRITEBYTECODE": "1"}
    assert subprocess.run([sys.executable, "-c", code, str(sets)], env=env, timeout=60).returncode == 9
    assert lock_is_free(sets)


def _p5(sets):
    return put_record(sets, RecordStatus.PREPARING), RecordStatus.FAILED, "P16"


def _p7(sets):
    rec = put_record(sets, RecordStatus.BACKED_UP)
    put_backup(sets, ORIGINAL)
    return rec, RecordStatus.FAILED, "P16"


def _p8(sets):
    rec = put_record(sets, RecordStatus.BACKED_UP)
    put_source(sets, UPGRADED)
    put_backup(sets, ORIGINAL)
    return rec, RecordStatus.UPGRADED, "P12"


@pytest.mark.parametrize("state", [_p5, _p7, _p8], ids=["P5", "P7", "P8"])
def test_apply_killed_before_the_rename_is_finished_by_the_next_run(sets, state):
    rec, status, final_rule = state(sets)
    kill_apply(sets, "before rename")
    assert on_disk(sets, rec) == rec  # still in progress
    leftovers = [x for x in rdir(sets).iterdir() if x.name.startswith(".")]
    assert len(leftovers) == 1  # its record temporary file, reported and never removed
    out = ra.apply_file(sets, "P1.json", now=T2)
    assert (out.result, out.status_after) == (A.FINALISED, status), out.reason
    assert leftovers[0].exists()
    assert nothing_written(sets).report.rule == final_rule


@pytest.mark.parametrize("state", [_p5, _p7, _p8], ids=["P5", "P7", "P8"])
def test_apply_killed_after_the_rename_leaves_a_final_record_and_nothing_to_do(sets, state):
    rec, status, final_rule = state(sets)
    kill_apply(sets, "after rename")
    assert on_disk(sets, rec).status is status
    assert nothing_written(sets).report.rule == final_rule


# ================================================================ boundaries

def test_the_report_is_unchanged_and_still_refuses_apply(sets):
    put_record(sets, RecordStatus.PREPARING)
    before = snap(sets.parent)
    out = rc.reconcile_file(sets, "P1.json")
    assert (out.outcome, out.rule, out.wrote) == (O.RECOVERABLE, "P5", None)
    with pytest.raises(ValueError, match="not available"):
        rc.reconcile_file(sets, "P1.json", apply=True)
    assert snap(sets.parent) == before


def test_upgrade_does_not_run_apply(sets, monkeypatch):
    monkeypatch.setattr(ra, "apply_file", lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
    rec = put_record(sets, RecordStatus.PREPARING)
    assert up.upgrade_file(sets, "P1.json").result is FileResult.FAILED  # still blocked, as in S3-B1
    assert on_disk(sets, rec) == rec


def test_apply_module_writes_only_through_update_record_and_generates_no_ids():
    text = Path(ra.__file__).read_text(encoding="utf-8")
    for name in ("_write_new", "_rename", "_replace", "_unlink", "_stage", "new_pass_id", "_build_target",
                 "library_lock", "upgrade_file", "os.mkdir", "os.makedirs", "os.rename", "os.replace",
                 "os.unlink", "os.remove", "os.rmdir", "os.write", "shutil", "open("):
        assert not re.search(r"(?<!\w)" + re.escape(name), text), name  # confirm_replace( is allowed
    assert text.count("_update_record(") == 1


def test_apply_writes_only_the_three_permitted_transitions():
    assert ra.WRITES == {"P5": RecordStatus.FAILED, "P7": RecordStatus.FAILED, "P8": RecordStatus.UPGRADED}


@pytest.mark.parametrize("rule", ["P4", "P6", "P9", "P10", "P11", "P14", "P16", "P17"])
def test_a_recoverable_report_under_any_other_rule_still_writes_nothing(sets, monkeypatch, rule):
    # defence in depth: only P5, P7 and P8 can be recoverable today; even if the table were changed
    # to call another rule recoverable, applying it must not write
    rec = put_record(sets, RecordStatus.PREPARING)
    real = rc._decide

    def decide(sv, name):
        out = real(sv, name)
        return rc.ReconcileOutcome(**{**out.__dict__, "rule": rule})
    monkeypatch.setattr(rc, "_decide", decide)
    forbid_record_writes(monkeypatch)
    before = snap(sets.parent)
    out = ra.apply_file(sets, "P1.json", now=T2)
    assert (out.result, out.report.outcome, out.report.rule) == (A.NO_WRITE, O.RECOVERABLE, rule)
    assert snap(sets.parent) == before and on_disk(sets, rec) == rec
