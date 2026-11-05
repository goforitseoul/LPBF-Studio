"""Regression tests for the S3-B2a review findings (read-only reconciliation reports).

* H-1: every staged upgrade file (``.<set>.<12 hex>.upgrade-tmp``) is reported, even when its set
  has no file, record or backup.
* H-2: upgrade residue without a migration record (a staged file, or a record temporary file
  whose record is missing) is never reported as a verified original or as format 2 without a
  record: a record must have existed, so the state is a conflict, never recoverable.
* L-1: reasons keep what was observed apart from what is inferred.
* L-2: stray entries are inspected without reading them; temporary files are hashed by streaming.

Nothing is ever written: every report is checked against a full snapshot of the library.
The M-1 classification (P14) is intentionally unchanged (see the remediation report).

Synthetic data in temporary folders only.
"""

import os
import sys
import tracemalloc
from pathlib import Path

import pytest

from lpbfstudio import reconcile as rc
from lpbfstudio import upgrade as up
from lpbfstudio.migration import RecordStatus
from test_reconcile_report import (  # noqa: F401 (fixtures and helpers)
    ORIGINAL, OTHER_V1, UPGRADED, lib, put_backup, put_record, put_source, record_tmp, rdir, sets, sha, snap,
    staged,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX primitives; Windows is refused (see upgrade.py)")

O = rc.Outcome
M, R = up.MIGRATION_DIR, up.RECORDS_DIR
NO_RECORD_CLASSES = {O.VERIFIED_ORIGINAL, O.FORMAT_2_NO_RECORD}


def library(sets: Path) -> "rc.LibraryReport":
    before = snap(sets.parent)
    out = rc.reconcile_library(sets)
    assert snap(sets.parent) == before, "a report must not change anything"
    again = rc.reconcile_library(sets)
    assert [(o.file_name, o.outcome, o.rule, o.reason) for o in again.outcomes] == \
        [(o.file_name, o.outcome, o.rule, o.reason) for o in out.outcomes]
    return out


def one(sets: Path, name: str = "P1.json") -> "rc.ReconcileOutcome":
    before = snap(sets.parent)
    out = rc.reconcile_file(sets, name)
    assert snap(sets.parent) == before, "a report must not change anything"
    return out


def blocked_residue(out, rule: str) -> None:
    assert out.outcome is O.CONFLICT, (out.outcome, out.rule, out.reason)
    assert out.rule == rule, out.rule
    assert out.planned_status is None
    assert "observed" in out.reason and "inferred" in out.reason, out.reason
    assert "no upgrade has started" not in out.reason


def all_paths(report) -> set[str]:
    paths = {e.path for e in report.unexpected}
    for o in report.outcomes:
        paths |= {e.path for e in o.temps}
    return paths


# ================================================================ H-1: orphan staged files are reported

def test_h1_orphan_staged_file_without_migration_folder_is_reported(sets):
    t = staged(sets, UPGRADED, name="Gone.json")
    out = library(sets)
    assert t.name in all_paths(out)
    gone = out.get("Gone.json")
    assert [e.path for e in gone.temps] == [t.name]
    assert gone.temps[0].sha256 == sha(UPGRADED) and gone.source.kind == "missing"
    blocked_residue(gone, "G2")
    assert t.read_bytes() == UPGRADED  # reported, never touched


def test_h1_orphan_staged_file_with_an_empty_records_folder_is_reported(sets):
    rdir(sets).mkdir(parents=True)
    t = staged(sets, UPGRADED[:20], name="Gone.json")
    out = library(sets)
    gone = out.get("Gone.json")
    assert [(e.path, e.size) for e in gone.temps] == [(t.name, 20)]
    blocked_residue(gone, "P2")


def test_h1_single_file_report_for_an_orphan_staged_file(sets):
    t = staged(sets, b"{}", name="Gone.json")
    out = one(sets, "Gone.json")
    assert [e.path for e in out.temps] == [t.name]
    blocked_residue(out, "G2")


@pytest.mark.parametrize("how", ["fifo", "link"])
def test_h1_orphan_staged_entry_that_is_not_a_regular_file_is_reported(sets, tmp_path, how):
    t = sets / f".Gone.json.a1b2c3d4e5f6{up.TMP_SUFFIX}"
    if how == "fifo":
        os.mkfifo(t)
    else:
        t.symlink_to(tmp_path / "nowhere")
    gone = library(sets).get("Gone.json")
    assert [(e.path, e.kind) for e in gone.temps] == [(t.name, "fifo" if how == "fifo" else "symlink")]
    blocked_residue(gone, "G2")


def test_h1_staged_file_for_a_name_that_is_not_a_plain_set_name_is_still_listed(sets):
    t = sets / f"..json.a1b2c3d4e5f6{up.TMP_SUFFIX}"
    t.write_bytes(b"x")
    assert t.name in all_paths(library(sets))


def test_h1_several_orphan_staged_files_are_all_reported(sets):
    a = staged(sets, b"1", name="Gone.json", token="a1b2c3d4e5f6")
    b = staged(sets, b"2", name="Gone.json", token="0123456789ab")
    c = staged(sets, b"3", name="Other.json")
    out = library(sets)
    assert sorted(e.path for e in out.get("Gone.json").temps) == sorted([a.name, b.name])
    assert [e.path for e in out.get("Other.json").temps] == [c.name]


# ================================================================ H-2: residue without a record

def test_h2_staged_file_next_to_a_format_1_set_without_migration_folder(sets):
    staged(sets, UPGRADED)
    out = one(sets)
    blocked_residue(out, "G2")
    assert out.temps and out.source.sha256 == sha(ORIGINAL)


def test_h2_staged_file_next_to_a_format_1_set_with_an_empty_records_folder(sets):
    rdir(sets).mkdir(parents=True)
    staged(sets, UPGRADED)
    blocked_residue(one(sets), "P2")


def test_h2_staged_file_next_to_a_format_2_set_without_a_record(sets):
    staged(sets, b"{}", name="V2.json")
    out = one(sets, "V2.json")
    assert out.outcome not in NO_RECORD_CLASSES
    blocked_residue(out, "G2")


def test_h2_staged_file_next_to_an_unparsable_file_is_still_a_conflict(sets):
    put_source(sets, b"{broken")
    staged(sets, UPGRADED)
    blocked_residue(one(sets), "G2")


def test_h2_orphan_record_temporary_file_blocks_every_file_without_a_record(sets):
    rdir(sets).mkdir(parents=True)
    t = record_tmp(sets, "a" * 32, b"{}")
    out = library(sets)
    no_record = [o for o in out.outcomes if not o.records]
    assert no_record, "the fixture library has sets without records"
    for o in no_record:
        assert o.outcome not in NO_RECORD_CLASSES, (o.file_name, o.outcome)
        blocked_residue(o, "P2")
        assert f"{M}/{R}/{t.name}" in o.reason


def test_h2_orphan_record_temporary_file_single_file_report(sets):
    rdir(sets).mkdir(parents=True)
    record_tmp(sets, "b" * 32, b"")
    blocked_residue(one(sets), "P2")


def test_h2_orphan_record_temporary_file_does_not_change_files_with_records(sets):
    # a file with its own (upgraded, verified) record is judged by that record
    put_record(sets, RecordStatus.UPGRADED)
    put_source(sets, UPGRADED)
    put_backup(sets, ORIGINAL)
    record_tmp(sets, "c" * 32, b"{}")
    out = one(sets)
    assert (out.outcome, out.rule) == (O.VERIFIED_UPGRADED, "P12")


def test_h2_residue_is_never_recoverable_even_beside_a_failed_attempt_elsewhere(sets):
    put_record(sets, RecordStatus.FAILED, name="Other.json", source=OTHER_V1)
    staged(sets, UPGRADED)
    out = one(sets)
    assert out.outcome is O.CONFLICT and out.planned_status is None


def test_h2_without_residue_the_no_record_rules_are_unchanged(sets):
    out = one(sets)
    assert (out.outcome, out.rule) == (O.VERIFIED_ORIGINAL, "G2")
    rdir(sets).mkdir(parents=True)
    out = one(sets)
    assert (out.outcome, out.rule) == (O.VERIFIED_ORIGINAL, "P2")
    assert (one(sets, "V2.json").outcome, one(sets, "V2.json").rule) == (O.FORMAT_2_NO_RECORD, "P2")


# ================================================================ L-1: observed versus inferred

def test_l1_g2_reason_states_the_observation_not_a_conclusion(sets):
    out = one(sets)
    assert out.outcome is O.VERIFIED_ORIGINAL
    assert "no upgrade has started" not in out.reason
    assert "observed" in out.reason and "no migration folder" in out.reason


@pytest.mark.parametrize("status, source, rule", [
    (RecordStatus.PREPARING, OTHER_V1, "P6"),
    (RecordStatus.BACKED_UP, ORIGINAL, "P7"),
    (RecordStatus.BACKED_UP, UPGRADED, "P8"),
    (RecordStatus.BACKED_UP, OTHER_V1, "P10"),
    (RecordStatus.UNCERTAIN, UPGRADED, "P11"),
], ids=["P6", "P7", "P8", "P10", "P11"])
def test_l1_reasons_that_rest_on_an_inference_say_which_part_is_inferred(sets, status, source, rule):
    put_record(sets, status)
    put_source(sets, source)
    put_backup(sets, ORIGINAL)
    out = one(sets)
    assert out.rule == rule
    assert "observed:" in out.reason and "inferred:" in out.reason, out.reason
    assert "something else changed it" not in out.reason


def test_l1_p14_reason_marks_changed_since_as_an_inference(lib, sets):
    up.upgrade_file(sets, "P1.json")
    es = lib.get("P1")
    es.description = "edited"
    lib.save(es)
    out = one(sets)
    assert (out.outcome, out.rule) == (O.VERIFIED_UPGRADED, "P14")  # M-1: classification unchanged
    assert "observed:" in out.reason and "inferred:" in out.reason


# ================================================================ L-2: bounded inspection

BIG = 32 << 20


def _peak(fn) -> int:
    tracemalloc.start()
    try:
        fn()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_l2_large_stray_files_are_listed_without_being_read(sets, monkeypatch):
    (sets / "huge.bin").write_bytes(b"\0" * BIG)
    rdir(sets).mkdir(parents=True)
    (sets / M / "huge.bin").write_bytes(b"\0" * BIG)
    read = []
    real = up._read_file
    monkeypatch.setattr(up, "_read_file", lambda p: (read.append(p.name), real(p))[1])
    peak = _peak(lambda: rc.reconcile_library(sets))
    out = rc.reconcile_library(sets)
    assert "huge.bin" not in read
    sizes = {e.path: e.size for e in out.unexpected}
    assert sizes["huge.bin"] == BIG and sizes[f"{M}/huge.bin"] == BIG
    assert peak < 8 << 20, peak


def test_l2_large_staged_file_is_hashed_by_streaming(sets):
    data = b"\1" * BIG
    t = staged(sets, data)
    peak = _peak(lambda: rc.reconcile_library(sets))
    gone = rc.reconcile_library(sets).get("P1.json")
    assert [(e.path, e.size, e.sha256) for e in gone.temps] == [(t.name, BIG, sha(data))]
    assert peak < 8 << 20, peak


def test_l2_streamed_temporary_file_swapped_during_the_read_is_not_trusted(sets, monkeypatch):
    rdir(sets).mkdir(parents=True)
    t = staged(sets, UPGRADED)
    real_lstat = up._lstat

    def lstat_then_swap(path):
        st = real_lstat(path)
        if path.name == t.name:
            tmp = path.with_name("swap.part")
            tmp.write_bytes(UPGRADED)
            os.replace(tmp, path)  # same bytes, another file
        return st
    monkeypatch.setattr(up, "_lstat", lstat_then_swap)
    gone = rc.reconcile_library(sets).get("P1.json")
    assert gone.temps[0].kind == "unreadable" and "changed" in gone.temps[0].error


def test_l2_stray_entries_still_report_kind_and_links(sets, tmp_path):
    os.mkfifo(sets / "pipe")
    (sets / "stray.txt").write_text("x")
    os.link(sets / "stray.txt", tmp_path / "second")
    by_path = {e.path: e for e in library(sets).unexpected}
    assert by_path["pipe"].kind == "fifo"
    assert (by_path["stray.txt"].kind, by_path["stray.txt"].links, by_path["stray.txt"].size) == ("regular", 2, 1)


# ================================================================ uncertain after a replace stays blocked
# (added after the mutation check: every other uncertain record in the tests has no upgraded digest)

@pytest.mark.parametrize("source", [UPGRADED, ORIGINAL], ids=["upgraded_bytes", "original_bytes"])
def test_uncertain_record_with_an_upgraded_digest_stays_a_conflict(sets, source):
    from lpbfstudio import migration as mig
    rec = mig.new_record("P1.json", f"{M}/P1.json", ORIGINAL, now="2026-10-27T08:00:00Z")
    rec = mig.advance(rec, RecordStatus.BACKED_UP, upgraded=UPGRADED)
    rec = mig.advance(rec, RecordStatus.UNCERTAIN, error="after the replace the file is unreadable",
                      now="2026-10-27T08:00:05Z")
    rdir(sets).mkdir(parents=True)
    (rdir(sets) / f"{rec.record_id}.json").write_text(mig.dump_record(rec), encoding="utf-8")
    put_source(sets, source)
    put_backup(sets, ORIGINAL)
    out = one(sets)
    assert (out.outcome, out.rule, out.planned_status) == (O.CONFLICT, "P11", None)
