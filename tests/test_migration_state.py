"""Format 1 -> format 2 upgrade: state model and migration record contract (B7 Phase 2, S3-A).

S3-A defines the states and the record only. Backups, the upgrade write, recovery and the CLI
are later stages, so nothing here writes a set file, a backup or a record; these tests check the
contract that those stages must follow:

* three separate notions: the record's ``status`` (what the record claims), the per-file
  ``FileResult`` (what a run reports) and the ``DiskState`` (what the file's bytes show);
* a file is reported upgraded only when its bytes match the digest recorded for the upgrade;
* records have their own ``record_schema_version``, unrelated to a set's ``schema_version``;
* anything unreadable, unsupported or unresolved is refused, never guessed.

Synthetic data in temporary folders only.
"""

import hashlib
import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from lpbfstudio import migration as mig
from lpbfstudio.defaults import default_sets
from lpbfstudio.library import Library, LibraryError, load_json
from lpbfstudio.migration import DiskState, FileResult, RecordError, RecordStatus
from lpbfstudio.models import SCHEMA_VERSION, SUPPORTED_SCHEMA_VERSIONS, ExposureSet

A, B = "3f9c2e1a", "b7d10c44"
T0, T1 = "2026-10-26T13:00:00Z", "2026-10-26T13:00:05Z"
RID = "0123456789abcdef0123456789abcdef"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def set_data(name: str, version=1, ids=(A, B), locked=False, drop_version=False) -> dict:
    data = json.loads(default_sets()[0].model_dump_json())
    data.update(name=name, locked=locked, schema_version=version)
    if drop_version:
        del data["schema_version"]
    if version == 2:
        for et in data["exposure_types"]:
            if et["kind"] == "contour":
                et["passes"] = [{"id": i, **p} for i, p in zip(ids, et["passes"])]
    return data


SRC = json.dumps(set_data("P1"), indent=2).encode()
UPG = json.dumps(set_data("P1", 2), indent=2).encode()


def record_data(status="preparing", **over) -> dict:
    """A valid record (as JSON data) in ``status``, with the fields that status requires."""
    data = {
        "record_schema_version": 1,
        "record_id": RID,
        "source_path": "P1.json",
        "backup_path": ".format1-backup/P1.json",
        "source_schema_version": 1,
        "target_schema_version": 2,
        "source_sha256": sha(SRC),
        "source_size_bytes": len(SRC),
        "upgraded_sha256": None,
        "upgraded_size_bytes": None,
        "status": status,
        "created_at": T0,
        "completed_at": None,
        "error": None,
    }
    if status in ("backed_up", "upgraded"):
        data.update(upgraded_sha256=sha(UPG), upgraded_size_bytes=len(UPG))
    if status in ("upgraded", "failed", "uncertain"):
        data["completed_at"] = T1
    if status in ("failed", "uncertain"):
        data["error"] = "simulated"
    data.update(over)
    return data


def record(status="preparing", **over) -> mig.MigrationRecord:
    return mig.parse_record(json.dumps(record_data(status, **over)))


def snapshot(folder: Path):
    return {p.relative_to(folder).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in sorted(folder.rglob("*")) if p.is_file()}


@pytest.fixture
def sets(tmp_path):
    """A folder of synthetic set files covering every classification."""
    d = tmp_path / "sets"
    d.mkdir()
    files = {
        "P1.json": SRC,
        "Legacy.json": json.dumps(set_data("Legacy", drop_version=True), indent=2).encode(),
        "Locked1.json": json.dumps(set_data("Locked1", locked=True), indent=2).encode(),
        "V2.json": UPG.replace(b'"P1"', b'"V2"'),
        "LockedV2.json": json.dumps(set_data("LockedV2", 2, locked=True), indent=2).encode(),
        "V3.json": json.dumps(set_data("V3", 3), indent=2).encode(),
        "VStr.json": json.dumps(set_data("VStr", "1"), indent=2).encode(),
        "VNull.json": json.dumps(set_data("VNull", None), indent=2).encode(),
        "VFloat.json": json.dumps(set_data("VFloat", 1.0), indent=2).encode(),
        "DupIds.json": json.dumps(set_data("DupIds", 2, ids=(A, A)), indent=2).encode(),
        "IdsInV1.json": json.dumps({**set_data("IdsInV1", 2), "schema_version": 1}, indent=2).encode(),
        "Broken.json": b'{"schema_version": 1, "name": ',
        "NotUtf8.json": b"\xff\xfe\x00",
    }
    for name, data in files.items():
        (d / name).write_bytes(data)
    return d


# ================================================================ separation of the two versions

def test_record_version_is_separate_from_set_version():
    assert mig.RECORD_SCHEMA_VERSION == 1 and mig.SUPPORTED_RECORD_VERSIONS == (1,)
    assert mig.SOURCE_SCHEMA_VERSION == SCHEMA_VERSION == 1 and mig.TARGET_SCHEMA_VERSION == 2
    assert mig.TARGET_SCHEMA_VERSION in SUPPORTED_SCHEMA_VERSIONS
    fields = set(mig.MigrationRecord.model_fields)
    assert "record_schema_version" in fields and "schema_version" not in fields
    assert {"source_schema_version", "target_schema_version"} <= fields


def test_record_version_2_is_refused_although_set_version_2_is_supported():
    with pytest.raises(RecordError, match="record_schema_version 2 is not supported"):
        record(record_schema_version=2)


def test_a_set_file_is_not_a_record_and_a_record_is_not_a_set(tmp_path):
    with pytest.raises(RecordError, match="record_schema_version"):
        mig.parse_record(SRC.decode())
    f = tmp_path / "rec.json"
    f.write_text(mig.dump_record(record()), encoding="utf-8")
    with pytest.raises(ValueError):
        load_json(f)


def test_record_schema_version_is_not_taken_from_schema_version():
    data = record_data()
    data["schema_version"] = data.pop("record_schema_version")
    with pytest.raises(RecordError, match="record_schema_version"):
        mig.parse_record(json.dumps(data))


# ================================================================ record contract: valid records

@pytest.mark.parametrize("status", ["preparing", "backed_up", "upgraded", "failed", "uncertain"])
def test_valid_records_round_trip(status):
    r = record(status)
    assert r.status is RecordStatus(status)
    assert mig.parse_record(mig.dump_record(r)) == r
    assert json.loads(mig.dump_record(r)) == record_data(status)


def test_dump_writes_record_schema_version_first():
    assert next(iter(json.loads(mig.dump_record(record())))) == "record_schema_version"


def test_new_record_starts_preparing_from_the_source_bytes():
    r = mig.new_record("P1.json", ".format1-backup/P1.json", SRC, now=T0)
    assert r.status is RecordStatus.PREPARING and r.record_schema_version == 1
    assert (r.source_sha256, r.source_size_bytes) == (sha(SRC), len(SRC))
    assert (r.source_schema_version, r.target_schema_version) == (1, 2)
    assert r.upgraded_sha256 is None and r.completed_at is None and r.error is None
    assert len(r.record_id) == 32 and r.record_id != mig.new_record("P1.json", ".format1-backup/P1.json", SRC).record_id


def test_records_are_immutable():
    r = record()
    with pytest.raises(ValidationError):
        r.status = RecordStatus.UPGRADED
    with pytest.raises(ValidationError):
        r.source_sha256 = "0" * 64


# ================================================================ record contract: refused records

@pytest.mark.parametrize("text", ["", "not json", "[]", "null", "1", '"x"'], ids=repr)
def test_unreadable_records_are_refused(text):
    with pytest.raises(RecordError):
        mig.parse_record(text)


@pytest.mark.parametrize("value", [None, 0, 2, 99, "1", 1.0, True], ids=repr)
def test_unsupported_record_versions_are_refused(value):
    with pytest.raises(RecordError, match="record_schema_version"):
        record(record_schema_version=value)


def test_missing_record_version_is_refused_not_assumed():
    data = record_data()
    del data["record_schema_version"]
    with pytest.raises(RecordError, match="record_schema_version is missing"):
        mig.parse_record(json.dumps(data))


@pytest.mark.parametrize("field", [f for f in record_data("upgraded") if f != "record_schema_version"])
def test_every_field_is_required(field):
    data = record_data("upgraded")
    del data[field]
    with pytest.raises(RecordError):
        mig.parse_record(json.dumps(data))


@pytest.mark.parametrize("over", [
    {"status": "done"}, {"status": "UPGRADED"}, {"status": None},
    {"status": "skipped_v2"}, {"status": "not_attempted"},          # results, not record states
    {"record_id": "xyz"}, {"record_id": RID.upper()}, {"record_id": 5},
    {"source_path": "/abs/P1.json"}, {"source_path": "../P1.json"}, {"source_path": "a\\P1.json"},
    {"source_path": "P1.txt"}, {"source_path": ""},
    {"backup_path": "P1.json"}, {"backup_path": "../x/P1.json"}, {"backup_path": None},
    {"source_schema_version": 2}, {"source_schema_version": "1"},
    {"target_schema_version": 1}, {"target_schema_version": 3},
    {"source_sha256": "abc"}, {"source_sha256": sha(SRC).upper()},
    {"source_size_bytes": -1}, {"source_size_bytes": "10"}, {"source_size_bytes": True},
    {"created_at": "yesterday"}, {"created_at": "2026-10-26T13:00:00"},  # no time zone
    {"error": "set too early"}, {"completed_at": T1},                  # not allowed while preparing
    {"upgraded_sha256": sha(UPG), "upgraded_size_bytes": len(UPG)},    # not known while preparing
    {"extra_field": 1},
], ids=lambda o: ",".join(f"{k}={v!r}"[:30] for k, v in o.items()))
def test_invalid_fields_are_refused(over):
    data = {**record_data("preparing"), **over}
    with pytest.raises(RecordError):
        mig.parse_record(json.dumps(data))


@pytest.mark.parametrize("status, over", [
    ("backed_up", {"upgraded_sha256": None, "upgraded_size_bytes": None}),
    ("backed_up", {"upgraded_size_bytes": None}),                       # digest without size
    ("backed_up", {"upgraded_sha256": sha(SRC), "upgraded_size_bytes": len(SRC)}),  # same as source
    ("backed_up", {"completed_at": T1}),
    ("upgraded", {"upgraded_sha256": None, "upgraded_size_bytes": None}),
    ("upgraded", {"completed_at": None}),
    ("upgraded", {"error": "but it failed"}),
    ("upgraded", {"completed_at": "2026-10-26T12:59:59Z"}),             # before created_at
    ("failed", {"error": None}), ("failed", {"error": " "}), ("failed", {"completed_at": None}),
    ("uncertain", {"error": None}), ("uncertain", {"completed_at": None}),
])
def test_per_status_requirements_are_enforced(status, over):
    with pytest.raises(RecordError):
        record(status, **over)


def test_failed_and_uncertain_may_carry_the_upgraded_digest():
    for status in ("failed", "uncertain"):
        assert record(status, upgraded_sha256=sha(UPG), upgraded_size_bytes=len(UPG)).upgraded_sha256 == sha(UPG)


# ================================================================ record state machine

def test_allowed_transitions():
    S = RecordStatus
    assert mig.TRANSITIONS == {
        S.PREPARING: {S.BACKED_UP, S.FAILED, S.UNCERTAIN},
        S.BACKED_UP: {S.UPGRADED, S.FAILED, S.UNCERTAIN},
        S.UPGRADED: set(), S.FAILED: set(), S.UNCERTAIN: set(),
    }
    assert mig.IN_PROGRESS == {S.PREPARING, S.BACKED_UP}
    assert mig.FINAL == {S.UPGRADED, S.FAILED, S.UNCERTAIN}


def test_normal_upgrade_sequence():
    r = mig.new_record("P1.json", ".format1-backup/P1.json", SRC, now=T0)
    r = mig.advance(r, RecordStatus.BACKED_UP, upgraded=UPG)
    assert (r.upgraded_sha256, r.upgraded_size_bytes, r.completed_at) == (sha(UPG), len(UPG), None)
    r = mig.confirm_replace(r, DiskState.UPGRADED, now=T1)
    assert r.status is RecordStatus.UPGRADED and r.completed_at == T1 and r.error is None
    assert mig.result_for(r, DiskState.UPGRADED) is FileResult.UPGRADED


@pytest.mark.parametrize("start, target", [
    ("preparing", "upgraded"),        # skipping the verified backup
    ("upgraded", "failed"), ("upgraded", "uncertain"), ("upgraded", "preparing"),
    ("failed", "upgraded"), ("failed", "backed_up"),
    ("uncertain", "upgraded"), ("uncertain", "failed"),
    ("backed_up", "preparing"), ("backed_up", "backed_up"),
])
def test_forbidden_transitions_are_refused(start, target):
    r = record(start)
    with pytest.raises(RecordError, match="cannot move"):
        mig.advance(r, RecordStatus(target), upgraded=UPG, error="x")
    assert r == record(start)


def test_advance_checks_what_each_status_needs():
    r = record("preparing")
    with pytest.raises(RecordError):
        mig.advance(r, RecordStatus.BACKED_UP)                  # no upgraded bytes
    with pytest.raises(RecordError):
        mig.advance(r, RecordStatus.BACKED_UP, upgraded=SRC)   # "upgraded" bytes equal to the source
    with pytest.raises(RecordError):
        mig.advance(r, RecordStatus.FAILED)                     # no error text
    with pytest.raises(RecordError):
        mig.advance(r, RecordStatus.UNCERTAIN, error="")


def test_upgraded_is_reachable_only_through_a_matching_disk_state():
    r = record("backed_up")
    with pytest.raises(RecordError):
        mig.advance(r, RecordStatus.UPGRADED)  # only confirm_replace may record success
    assert mig.confirm_replace(r, DiskState.UPGRADED, now=T1).status is RecordStatus.UPGRADED
    failed = mig.confirm_replace(r, DiskState.SOURCE, now=T1)
    assert failed.status is RecordStatus.FAILED and "unchanged" in failed.error
    for state in (DiskState.OTHER, DiskState.MISSING, DiskState.UNREADABLE):
        out = mig.confirm_replace(r, state, now=T1)
        assert out.status is RecordStatus.UNCERTAIN and out.error, state


@pytest.mark.parametrize("status", ["preparing", "upgraded", "failed", "uncertain"])
def test_confirm_replace_needs_a_backed_up_record(status):
    with pytest.raises(RecordError, match="cannot move|backed_up"):
        mig.confirm_replace(record(status), DiskState.UPGRADED, now=T1)


# ================================================================ disk state and reported results

def test_observe_and_disk_state(tmp_path):
    r = record("backed_up")
    f = tmp_path / "P1.json"
    assert mig.disk_state(mig.observe(f), r) is DiskState.MISSING
    f.write_bytes(SRC)
    assert mig.disk_state(mig.observe(f), r) is DiskState.SOURCE
    f.write_bytes(UPG)
    assert mig.disk_state(mig.observe(f), r) is DiskState.UPGRADED
    f.write_bytes(UPG + b" ")
    assert mig.disk_state(mig.observe(f), r) is DiskState.OTHER
    f.unlink()
    f.mkdir()  # a folder in its place cannot be read as a file
    assert mig.disk_state(mig.observe(f), r) is DiskState.UNREADABLE


def test_same_digest_with_a_different_size_is_not_a_match(tmp_path):
    f = tmp_path / "P1.json"
    f.write_bytes(UPG)
    r = record("backed_up", upgraded_size_bytes=len(UPG) + 1)
    assert mig.disk_state(mig.observe(f), r) is DiskState.OTHER


def test_disk_state_without_an_upgraded_digest_never_reports_upgraded(tmp_path):
    f = tmp_path / "P1.json"
    f.write_bytes(UPG)
    assert mig.disk_state(mig.observe(f), record("preparing")) is DiskState.OTHER


R, D = RecordStatus, DiskState
RESULTS = {
    # (record status, disk state): result. "upgraded" needs both an upgraded record and matching bytes.
    (R.UPGRADED, D.UPGRADED): FileResult.UPGRADED,
    (R.UPGRADED, D.SOURCE): FileResult.UNCERTAIN,
    (R.UPGRADED, D.OTHER): FileResult.UNCERTAIN,
    (R.UPGRADED, D.MISSING): FileResult.UNCERTAIN,
    (R.UPGRADED, D.UNREADABLE): FileResult.UNCERTAIN,
    (R.BACKED_UP, D.UPGRADED): FileResult.UNCERTAIN,   # replaced, but never confirmed: do not guess
    (R.BACKED_UP, D.SOURCE): FileResult.FAILED,        # interrupted before the replace; file unchanged
    (R.BACKED_UP, D.OTHER): FileResult.UNCERTAIN,
    (R.BACKED_UP, D.MISSING): FileResult.UNCERTAIN,
    (R.PREPARING, D.SOURCE): FileResult.FAILED,
    (R.PREPARING, D.OTHER): FileResult.UNCERTAIN,
    (R.PREPARING, D.MISSING): FileResult.UNCERTAIN,
    (R.FAILED, D.SOURCE): FileResult.FAILED,
    (R.FAILED, D.OTHER): FileResult.UNCERTAIN,
    (R.FAILED, D.UPGRADED): FileResult.UNCERTAIN,
    (R.UNCERTAIN, D.UPGRADED): FileResult.UNCERTAIN,   # an uncertain record is never turned into success
    (R.UNCERTAIN, D.SOURCE): FileResult.UNCERTAIN,
}


@pytest.mark.parametrize("status, state", list(RESULTS), ids=lambda v: v.value)
def test_result_for_record_and_disk_state(status, state):
    assert mig.result_for(record(status.value), state) is RESULTS[(status, state)]


def test_upgraded_result_requires_matching_bytes_whatever_the_record_says(tmp_path):
    f = tmp_path / "P1.json"
    f.write_bytes(SRC)  # the record claims success, the disk shows the original
    r = record("upgraded")
    assert mig.result_for(r, mig.disk_state(mig.observe(f), r)) is FileResult.UNCERTAIN


# ================================================================ classifying source files

EXPECTED = {
    "P1.json": None,                                  # eligible: format 1, unlocked
    "Legacy.json": None,                              # no key: version 1 by the documented legacy policy
    "Locked1.json": FileResult.SKIPPED_LOCKED,
    "V2.json": FileResult.SKIPPED_V2,
    "LockedV2.json": FileResult.SKIPPED_V2,
    "V3.json": FileResult.SKIPPED_UNREADABLE,
    "VStr.json": FileResult.SKIPPED_UNREADABLE,
    "VNull.json": FileResult.SKIPPED_UNREADABLE,
    "VFloat.json": FileResult.SKIPPED_UNREADABLE,
    "DupIds.json": FileResult.SKIPPED_UNREADABLE,
    "IdsInV1.json": FileResult.SKIPPED_UNREADABLE,
    "Broken.json": FileResult.SKIPPED_UNREADABLE,
    "NotUtf8.json": FileResult.SKIPPED_UNREADABLE,
}


@pytest.mark.parametrize("name", list(EXPECTED))
def test_classify(sets, name):
    c = mig.classify(sets / name)
    assert c.result is EXPECTED[name], c.reason
    assert c.upgrade is (EXPECTED[name] is None)
    if c.result is not None:
        assert c.reason
    data = (sets / name).read_bytes()
    assert (c.source_sha256, c.source_size_bytes) == (sha(data), len(data))


def test_unsupported_versions_are_never_treated_as_version_1(sets):
    for name in ("V3.json", "VStr.json", "VNull.json", "VFloat.json"):
        c = mig.classify(sets / name)
        assert not c.upgrade and c.result is FileResult.SKIPPED_UNREADABLE
        assert "version" in c.reason, c.reason


def test_locked_sets_are_upgraded_only_when_included(sets):
    assert mig.classify(sets / "Locked1.json", include_locked=True).upgrade
    assert mig.classify(sets / "LockedV2.json", include_locked=True).result is FileResult.SKIPPED_V2


def test_format_2_files_are_skipped_and_their_ids_kept(sets):
    before = (sets / "V2.json").read_bytes()
    c = mig.classify(sets / "V2.json", include_locked=True)
    assert c.result is FileResult.SKIPPED_V2 and "already format 2" in c.reason
    assert (sets / "V2.json").read_bytes() == before
    assert [p.id for p in load_json(sets / "V2.json").get("contour").passes] == [A, B]


def test_missing_source_is_not_eligible(sets):
    c = mig.classify(sets / "Nope.json")
    assert not c.upgrade and c.result is FileResult.SKIPPED_UNREADABLE


def test_an_unresolved_or_unreadable_record_blocks_the_file(sets):
    for existing in (record("preparing"), record("backed_up"), record("uncertain"), record("upgraded"),
                     RecordError("record_schema_version 9 is not supported")):
        c = mig.classify(sets / "P1.json", records=[existing])
        assert not c.upgrade and c.result is FileResult.FAILED, existing
        assert "record" in c.reason


def test_a_failed_record_with_the_source_unchanged_allows_a_new_attempt(sets):
    assert mig.classify(sets / "P1.json", records=[record("failed")]).upgrade


def test_a_failed_record_for_other_bytes_blocks_the_file(sets):
    other = record("failed", source_sha256="0" * 64)
    c = mig.classify(sets / "P1.json", records=[other])
    assert not c.upgrade and c.result is FileResult.FAILED


def test_records_do_not_matter_for_files_that_are_skipped(sets):
    c = mig.classify(sets / "V2.json", records=[RecordError("corrupt")])
    assert c.result is FileResult.SKIPPED_V2


# ================================================================ batch report

def test_batch_report_keeps_each_file_independently():
    rep = mig.BatchReport()
    rep.add("a.json", FileResult.UPGRADED)
    rep.add("b.json", FileResult.FAILED, "disk full")
    rep.add("c.json", FileResult.NOT_ATTEMPTED)
    assert rep.results["a.json"] == (FileResult.UPGRADED, "")  # a later failure does not erase it
    assert rep.count(FileResult.UPGRADED) == 1 and rep.count(FileResult.FAILED) == 1
    assert not rep.complete
    with pytest.raises(ValueError):
        rep.add("a.json", FileResult.FAILED, "again")  # a result is never replaced
    assert rep.results["a.json"] == (FileResult.UPGRADED, "")


@pytest.mark.parametrize("result", [FileResult.FAILED, FileResult.UNCERTAIN, FileResult.NOT_ATTEMPTED,
                                    FileResult.SKIPPED_UNREADABLE])
def test_batch_with_a_problem_is_not_complete(result):
    rep = mig.BatchReport()
    rep.add("a.json", FileResult.UPGRADED)
    rep.add("b.json", result, "" if result is FileResult.NOT_ATTEMPTED else "why")
    assert not rep.complete and rep.problems == ["b.json"]


def test_batch_of_upgrades_and_skips_is_complete():
    rep = mig.BatchReport()
    for name, r in (("a", FileResult.UPGRADED), ("b", FileResult.SKIPPED_V2), ("c", FileResult.SKIPPED_LOCKED)):
        rep.add(name, r)
    assert rep.complete and rep.problems == []


def test_problem_results_need_a_reason():
    rep = mig.BatchReport()
    for r in (FileResult.FAILED, FileResult.UNCERTAIN, FileResult.SKIPPED_UNREADABLE):
        with pytest.raises(ValueError):
            rep.add("x.json", r)


def test_results_and_record_statuses_are_different_sets():
    assert {r.value for r in FileResult} == {"upgraded", "skipped_locked", "skipped_v2", "skipped_unreadable",
                                              "failed", "uncertain", "not_attempted"}
    assert {s.value for s in RecordStatus} == {"preparing", "backed_up", "upgraded", "failed", "uncertain"}


# ================================================================ nothing is written

def test_nothing_in_s3a_writes_files(sets, tmp_path):
    before = snapshot(tmp_path)
    for f in sorted(sets.iterdir()):
        c = mig.classify(f, include_locked=True, records=[record("failed")])
        mig.disk_state(mig.observe(f), record("backed_up"))
    mig.observe(tmp_path / "missing.json")
    assert snapshot(tmp_path) == before


def test_library_protections_still_hold(sets, tmp_path):
    lib = Library(tmp_path / "lib")
    (lib.sets_dir / "P1.json").write_bytes(SRC)
    es = lib.get("P1")
    with pytest.raises(ValidationError, match="frozen"):
        es.schema_version = 2
    before = (lib.sets_dir / "P1.json").read_bytes()
    with pytest.raises(LibraryError, match="format"):
        lib.save(es.model_copy(update={"schema_version": 2}))
    assert (lib.sets_dir / "P1.json").read_bytes() == before
    assert mig.classify(lib.sets_dir / "P1.json").upgrade  # still format 1 and eligible
