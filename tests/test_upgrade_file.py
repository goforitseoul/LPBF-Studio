"""Upgrading one format 1 set file to format 2: the safe file-operation core (B7 Phase 2, S3-B1).

``upgrade.upgrade_file(sets_dir, file_name)`` re-reads and re-validates the source, builds the
format 2 set through the model, writes a migration record (exclusively) before anything else,
writes and verifies a byte-identical backup (exclusively), stages and verifies the target in a
temporary file, re-checks the source just before ``os.replace``, then reads the result back.
It reports ``upgraded`` only after that read-back; it reports ``failed`` only when the source is
verified unchanged; anything else is ``uncertain``.

Restore, reconciliation of interrupted runs, batches and the CLI/GUI are later stages. Failure
windows are reached by deterministic fault injection (monkeypatching the module's I/O helpers).
Synthetic data in temporary folders only.
"""

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from lpbfstudio import migration as mig
from lpbfstudio import upgrade as up
from lpbfstudio.defaults import default_sets
from lpbfstudio.library import Library, load_json
from lpbfstudio.migration import FileResult, RecordStatus
from lpbfstudio.models import ExposureSet, is_valid_pass_id

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX primitives; Windows is refused (see upgrade.py)")

A, B = "3f9c2e1a", "b7d10c44"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def set_data(name: str, version=1, locked=False, ids=(A, B)) -> dict:
    data = json.loads(default_sets()[0].model_dump_json())
    data.update(name=name, locked=locked, schema_version=version)
    if version == 2:
        for et in data["exposure_types"]:
            if et["kind"] == "contour":
                et["passes"] = [{"id": i, **p} for i, p in zip(ids, et["passes"])]
    return data


def tree(root: Path) -> dict:
    """Every file and symlink under ``root`` with its bytes (or link target) and mtime."""
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_symlink():
            out[rel] = ("link", os.readlink(p))
        elif p.is_file():
            out[rel] = (p.read_bytes(), p.stat().st_mtime_ns)
    return out


# A hand-formatted format 1 file (CRLF line endings, odd spacing): the backup must keep these bytes.
ORIGINAL = json.dumps(set_data("P1"), indent=3).replace("\n", "\r\n").encode() + b"\r\n"


@pytest.fixture
def lib(tmp_path):
    lib = Library(tmp_path / "lib")  # seeds the locked default sets (format 1)
    (lib.sets_dir / "P1.json").write_bytes(ORIGINAL)
    (lib.sets_dir / "V2.json").write_text(json.dumps(set_data("V2", 2), indent=2), encoding="utf-8")
    return lib


@pytest.fixture
def sets(lib):
    return lib.sets_dir


def records_in(sets: Path) -> list[Path]:
    d = sets / up.MIGRATION_DIR / up.RECORDS_DIR
    return sorted(d.glob("*.json")) if d.exists() else []


def only_record(sets: Path) -> mig.MigrationRecord:
    files = records_in(sets)
    assert len(files) == 1, files
    return mig.parse_record(files[0].read_text(encoding="utf-8"))


def passes_of(path: Path):
    return load_json(path).get("contour").passes


def without_format(es: ExposureSet) -> dict:
    data = es.model_dump()
    data.pop("schema_version")
    for et in data["exposure_types"]:
        for p in et.get("passes", ()):
            p.pop("id", None)
    return data


# ================================================================ the normal upgrade

def test_valid_format_1_file_upgrades_to_valid_format_2(sets):
    out = up.upgrade_file(sets, "P1.json")
    assert out.result is FileResult.UPGRADED, out.reason
    es = load_json(sets / "P1.json")
    assert es.schema_version == 2
    assert json.loads((sets / "P1.json").read_text())["schema_version"] == 2
    assert out.pass_ids == tuple(p.id for p in es.get("contour").passes)


def test_generated_ids_are_valid_unique_and_committed(sets):
    out = up.upgrade_file(sets, "P1.json")
    ids = [p.id for p in passes_of(sets / "P1.json")]
    assert len(ids) == 2 and len(set(ids)) == 2 and all(is_valid_pass_id(i) for i in ids)
    assert all(len(i) == 8 and int(i, 16) >= 0 for i in ids)
    assert list(out.pass_ids) == ids


def test_two_upgrades_of_identical_files_get_different_ids(sets):
    (sets / "P2.json").write_bytes(ORIGINAL.replace(b'"P1"', b'"P2"'))
    up.upgrade_file(sets, "P1.json")
    up.upgrade_file(sets, "P2.json")
    assert {p.id for p in passes_of(sets / "P1.json")}.isdisjoint(p.id for p in passes_of(sets / "P2.json"))


def test_non_format_fields_and_values_are_unchanged(sets):
    before = load_json(sets / "P1.json")
    up.upgrade_file(sets, "P1.json")
    after = load_json(sets / "P1.json")
    assert without_format(after) == without_format(before)
    assert [p.name for p in after.get("contour").passes] == [p.name for p in before.get("contour").passes]


def test_locked_set_stays_locked_when_included(sets):
    (sets / "L1.json").write_text(json.dumps(set_data("L1", locked=True)), encoding="utf-8")
    assert up.upgrade_file(sets, "L1.json").result is FileResult.SKIPPED_LOCKED
    assert up.upgrade_file(sets, "L1.json", include_locked=True).result is FileResult.UPGRADED
    es = load_json(sets / "L1.json")
    assert es.locked and es.schema_version == 2


def test_upgraded_file_is_written_like_the_library_writes_it(lib, sets):
    up.upgrade_file(sets, "P1.json")
    text = (sets / "P1.json").read_text(encoding="utf-8")
    assert text == load_json(sets / "P1.json").model_dump_json(indent=2)
    es = lib.get("P1")
    assert es.schema_version == 2
    lib.save(es)  # an ordinary save keeps format 2 and the ids
    assert (sets / "P1.json").read_text(encoding="utf-8") == text


# ================================================================ backup

def test_backup_is_byte_identical_to_the_original(sets):
    out = up.upgrade_file(sets, "P1.json")
    backup = sets / up.MIGRATION_DIR / "P1.json"
    assert out.backup_path == backup
    assert backup.read_bytes() == ORIGINAL  # CRLF and spacing kept


def test_record_holds_verified_lengths_and_digests(sets):
    out = up.upgrade_file(sets, "P1.json")
    rec = only_record(sets)
    assert rec == out.record and rec.status is RecordStatus.UPGRADED
    assert (rec.source_sha256, rec.source_size_bytes) == (sha(ORIGINAL), len(ORIGINAL))
    new = (sets / "P1.json").read_bytes()
    assert (rec.upgraded_sha256, rec.upgraded_size_bytes) == (sha(new), len(new))
    assert rec.source_path == "P1.json" and rec.backup_path == f"{up.MIGRATION_DIR}/P1.json"
    assert rec.record_schema_version == 1 and rec.source_schema_version == 1 and rec.target_schema_version == 2
    assert out.record_path == records_in(sets)[0] and out.record_path.name == f"{rec.record_id}.json"


def test_backup_read_back_mismatch_fails_before_any_replace(sets, monkeypatch):
    real = up._read_file

    def corrupt_backup(path):
        data = real(path)
        return data + b" " if Path(path).parent.name == up.MIGRATION_DIR else data

    monkeypatch.setattr(up, "_read_file", corrupt_backup)
    out = up.upgrade_file(sets, "P1.json")
    assert out.result is FileResult.FAILED and "backup" in out.reason
    assert (sets / "P1.json").read_bytes() == ORIGINAL
    assert only_record(sets).status is RecordStatus.FAILED


def test_backup_write_failure_fails_and_keeps_the_source(sets, monkeypatch):
    real = up._write_new

    def no_backup(path, data):
        if Path(path).parent.name == up.MIGRATION_DIR:
            raise OSError(28, "No space left on device")
        return real(path, data)

    monkeypatch.setattr(up, "_write_new", no_backup)
    out = up.upgrade_file(sets, "P1.json")
    assert out.result is FileResult.FAILED and "No space left" in out.reason
    assert (sets / "P1.json").read_bytes() == ORIGINAL
    assert only_record(sets).status is RecordStatus.FAILED


def test_existing_different_backup_is_never_overwritten(sets):
    mdir = sets / up.MIGRATION_DIR
    mdir.mkdir()
    (mdir / "P1.json").write_bytes(b"something else")
    before = tree(sets)
    out = up.upgrade_file(sets, "P1.json")
    assert out.result is FileResult.FAILED and "backup" in out.reason
    assert (mdir / "P1.json").read_bytes() == b"something else"
    assert (sets / "P1.json").read_bytes() == ORIGINAL
    changed = {k for k in tree(sets) if tree(sets).get(k) != before.get(k)}
    assert all(k.startswith(f"{up.MIGRATION_DIR}/{up.RECORDS_DIR}/") for k in changed)  # only its record


def test_existing_identical_backup_without_a_record_is_refused(sets):
    mdir = sets / up.MIGRATION_DIR
    mdir.mkdir()
    (mdir / "P1.json").write_bytes(ORIGINAL)
    out = up.upgrade_file(sets, "P1.json")
    assert out.result is FileResult.FAILED and "backup" in out.reason
    assert (sets / "P1.json").read_bytes() == ORIGINAL and (mdir / "P1.json").read_bytes() == ORIGINAL


def test_identical_backup_is_reused_after_a_verified_failed_attempt(sets, monkeypatch):
    real = up._replace
    monkeypatch.setattr(up, "_replace", lambda src, dst: (_ for _ in ()).throw(OSError(13, "Permission denied")))
    first = up.upgrade_file(sets, "P1.json")
    assert first.result is FileResult.FAILED and (sets / "P1.json").read_bytes() == ORIGINAL
    monkeypatch.setattr(up, "_replace", real)
    second = up.upgrade_file(sets, "P1.json")
    assert second.result is FileResult.UPGRADED, second.reason
    assert (sets / up.MIGRATION_DIR / "P1.json").read_bytes() == ORIGINAL
    assert len(records_in(sets)) == 2


# ================================================================ records

def test_existing_unresolved_record_blocks_the_upgrade(sets, monkeypatch):
    monkeypatch.setattr(up, "_replace", lambda src, dst: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        up.upgrade_file(sets, "P1.json")
    monkeypatch.undo()
    rec_file = records_in(sets)[0]
    rec_before = rec_file.read_bytes()
    assert mig.parse_record(rec_before.decode()).status is RecordStatus.BACKED_UP  # the interrupted run
    before = tree(sets)
    out = up.upgrade_file(sets, "P1.json")
    assert out.result is FileResult.FAILED and "resolve it first" in out.reason
    assert tree(sets) == before and rec_file.read_bytes() == rec_before


def test_record_id_collision_never_overwrites_the_other_record(sets, monkeypatch):
    rid = "ab" * 16
    rdir = sets / up.MIGRATION_DIR / up.RECORDS_DIR
    rdir.mkdir(parents=True)
    other = rdir / f"{rid}.json"
    other.write_bytes(b"not mine")  # a file already holds the path this run would use
    monkeypatch.setattr(mig.uuid, "uuid4", lambda: type("U", (), {"hex": rid})())
    out = up.upgrade_file(sets, "P1.json")
    assert out.result is FileResult.FAILED and "record" in out.reason
    assert other.read_bytes() == b"not mine"
    assert (sets / "P1.json").read_bytes() == ORIGINAL
    assert not (sets / up.MIGRATION_DIR / "P1.json").exists()  # nothing written before the record


def test_unreadable_record_blocks_all_upgrades(sets):
    rdir = sets / up.MIGRATION_DIR / up.RECORDS_DIR
    rdir.mkdir(parents=True)
    (rdir / ("cd" * 16 + ".json")).write_text("{broken", encoding="utf-8")
    before = tree(sets)
    out = up.upgrade_file(sets, "P1.json")
    assert out.result is FileResult.FAILED and "record" in out.reason
    assert tree(sets) == before


def test_record_with_a_traversal_path_is_refused_and_blocks(sets):
    rdir = sets / up.MIGRATION_DIR / up.RECORDS_DIR
    rdir.mkdir(parents=True)
    rec = mig.new_record("P1.json", f"{up.MIGRATION_DIR}/P1.json", ORIGINAL, now="2026-10-26T13:00:00Z")
    data = json.loads(mig.dump_record(rec))
    data["source_path"] = "../outside/P1.json"
    (rdir / f"{rec.record_id}.json").write_text(json.dumps(data), encoding="utf-8")
    assert isinstance(up.find_records(sets, "P1.json")[0], mig.RecordError)
    before = tree(sets)
    assert up.upgrade_file(sets, "P1.json").result is FileResult.FAILED
    assert tree(sets) == before


def test_record_is_written_before_the_backup(sets, monkeypatch):
    order = []
    real = up._write_new

    def spy(path, data):
        order.append(Path(path).parent.name)
        return real(path, data)

    monkeypatch.setattr(up, "_write_new", spy)
    up.upgrade_file(sets, "P1.json")
    assert order[:2] == [up.RECORDS_DIR, up.MIGRATION_DIR]


def test_record_is_backed_up_before_the_replace(sets, monkeypatch):
    seen = []
    real = up._replace

    def spy(src, dst):
        if Path(dst) == sets / "P1.json":
            seen.append(only_record(sets).status)
        return real(src, dst)

    monkeypatch.setattr(up, "_replace", spy)
    assert up.upgrade_file(sets, "P1.json").result is FileResult.UPGRADED
    assert seen == [RecordStatus.BACKED_UP]


# ================================================================ refused sources (nothing written)

@pytest.mark.parametrize("content, words", [
    (b'{"schema_version": 1, "name": ', "cannot be read"),
    (json.dumps(set_data("X", 3)).encode(), "version"),
    (json.dumps(set_data("X", "1")).encode(), "version"),
    (json.dumps({**set_data("X"), "layer_thickness_mm": 5}).encode(), "cannot be read"),
    (json.dumps({**set_data("X"), "bogus": 1}).encode(), "cannot be read"),
    (json.dumps({**set_data("X", 2, ids=(A, A))}).encode(), "cannot be read"),
    (b"\xff\xfe", "cannot be read"),
], ids=["bad-json", "version-3", "version-str", "out-of-range", "unknown-field", "dup-ids", "not-utf8"])
def test_refused_sources_are_never_modified(sets, content, words):
    (sets / "X.json").write_bytes(content)
    before = tree(sets)
    out = up.upgrade_file(sets, "X.json")
    assert out.result is FileResult.SKIPPED_UNREADABLE and words in out.reason, out.reason
    assert tree(sets) == before and not (sets / up.MIGRATION_DIR).exists()


def test_format_2_and_locked_files_are_skipped_without_writes(sets):
    before = tree(sets)
    assert up.upgrade_file(sets, "V2.json").result is FileResult.SKIPPED_V2
    assert up.upgrade_file(sets, "Default_Part_40um.json").result is FileResult.SKIPPED_LOCKED
    assert tree(sets) == before


def test_missing_source_is_refused(sets):
    before = tree(sets)
    assert up.upgrade_file(sets, "Nope.json").result is FileResult.SKIPPED_UNREADABLE
    assert tree(sets) == before


@pytest.mark.parametrize("name", ["../P1.json", "sub/P1.json", "..\\P1.json", "/etc/passwd", "", ".json",
                                  "P1.txt", ".hidden.json", "P1.json\x00"], ids=repr)
def test_unsafe_file_names_are_refused(sets, name):
    before = tree(sets.parent)
    out = up.upgrade_file(sets, name)
    assert out.result is FileResult.SKIPPED_UNREADABLE and "name" in out.reason
    assert tree(sets.parent) == before


def test_source_is_revalidated_at_run_time_not_trusted_from_classify(sets):
    assert mig.classify(sets / "P1.json").upgrade
    (sets / "P1.json").write_text(json.dumps(set_data("P1", 3)), encoding="utf-8")  # changed after the check
    before = tree(sets)
    assert up.upgrade_file(sets, "P1.json").result is FileResult.SKIPPED_UNREADABLE
    assert tree(sets) == before


# ================================================================ symlinks and path escape (POSIX)

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows; "
                                "the checks use lstat and O_NOFOLLOW where available (see upgrade.py)")


@posix_only
def test_symlinked_source_is_refused(sets, tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_bytes(ORIGINAL)
    (sets / "Link.json").symlink_to(outside)
    before, outside_before = tree(sets), outside.read_bytes()
    out = up.upgrade_file(sets, "Link.json")
    assert out.result is FileResult.SKIPPED_UNREADABLE and "link" in out.reason
    assert tree(sets) == before and outside.read_bytes() == outside_before


@posix_only
@pytest.mark.parametrize("which", ["migration", "records"])
def test_symlinked_migration_folders_are_refused(sets, tmp_path, which):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    if which == "migration":
        (sets / up.MIGRATION_DIR).symlink_to(elsewhere, target_is_directory=True)
    else:
        (sets / up.MIGRATION_DIR).mkdir()
        (sets / up.MIGRATION_DIR / up.RECORDS_DIR).symlink_to(elsewhere, target_is_directory=True)
    before = tree(sets)
    out = up.upgrade_file(sets, "P1.json")
    assert out.result is FileResult.FAILED and "link" in out.reason
    assert tree(sets) == before and list(elsewhere.iterdir()) == []


@posix_only
def test_symlink_at_the_backup_path_is_not_followed(sets, tmp_path):
    target = tmp_path / "victim.json"
    (sets / up.MIGRATION_DIR).mkdir()
    (sets / up.MIGRATION_DIR / "P1.json").symlink_to(target)  # dangling: O_EXCL must refuse, not create it
    out = up.upgrade_file(sets, "P1.json")
    assert out.result is FileResult.FAILED and "backup" in out.reason
    assert not target.exists() and (sets / "P1.json").read_bytes() == ORIGINAL


# ================================================================ failure windows around the replace

def test_source_changed_before_the_replace_is_not_replaced(sets, monkeypatch):
    real = up._stage

    def stage_then_edit(*args, **kwargs):
        staged = real(*args, **kwargs)
        (sets / "P1.json").write_bytes(ORIGINAL + b" ")  # another program saves meanwhile
        return staged

    monkeypatch.setattr(up, "_stage", stage_then_edit)
    out = up.upgrade_file(sets, "P1.json")
    # not replaced; but the source is no longer the verified original, so not "failed"
    assert out.result is FileResult.UNCERTAIN and "changed" in out.reason
    assert (sets / "P1.json").read_bytes() == ORIGINAL + b" "
    assert only_record(sets).status is RecordStatus.UNCERTAIN
    assert not [p for p in sets.iterdir() if p.name.endswith(up.TMP_SUFFIX)]


def test_staged_target_that_does_not_verify_leaves_the_source(sets, monkeypatch):
    real = up._write_new

    def corrupt_stage(path, data):
        return real(path, data[:-5] if Path(path).name.endswith(up.TMP_SUFFIX) else data)

    monkeypatch.setattr(up, "_write_new", corrupt_stage)
    out = up.upgrade_file(sets, "P1.json")
    assert out.result is FileResult.FAILED and "staged" in out.reason
    assert (sets / "P1.json").read_bytes() == ORIGINAL
    assert only_record(sets).status is RecordStatus.FAILED
    assert not [p for p in sets.iterdir() if p.name.endswith(up.TMP_SUFFIX)]


def test_replace_error_with_the_source_intact_is_failed(sets, monkeypatch):
    monkeypatch.setattr(up, "_replace", lambda src, dst: (_ for _ in ()).throw(OSError(16, "Device busy")))
    out = up.upgrade_file(sets, "P1.json")
    assert out.result is FileResult.FAILED and "Device busy" in out.reason
    assert (sets / "P1.json").read_bytes() == ORIGINAL
    assert only_record(sets).status is RecordStatus.FAILED


def test_replace_that_reports_an_error_but_happened_is_verified_from_disk(sets, monkeypatch):
    real = up._replace

    def replaced_then_error(src, dst):
        real(src, dst)
        raise OSError(5, "I/O error")

    monkeypatch.setattr(up, "_replace", replaced_then_error)
    out = up.upgrade_file(sets, "P1.json")
    assert out.result is FileResult.UPGRADED  # the bytes on disk are verified, not the call's outcome
    assert only_record(sets).status is RecordStatus.UPGRADED


def test_read_back_error_after_the_replace_is_uncertain(sets, monkeypatch):
    real_replace, real_read = up._replace, up._read_file
    state = {"replaced": False}

    def replace(src, dst):
        real_replace(src, dst)
        state["replaced"] = True

    def read(path):
        if state["replaced"] and Path(path) == sets / "P1.json":
            raise OSError(5, "I/O error")
        return real_read(path)

    monkeypatch.setattr(up, "_replace", replace)
    monkeypatch.setattr(up, "_read_file", read)
    out = up.upgrade_file(sets, "P1.json")
    assert out.result is FileResult.UNCERTAIN, out.reason
    rec = only_record(sets)
    assert rec.status is RecordStatus.UNCERTAIN and rec.error


def test_replaced_bytes_that_do_not_match_are_uncertain(sets, monkeypatch):
    real = up._replace

    def replace_with_other(src, dst):
        real(src, dst)
        Path(dst).write_bytes(Path(dst).read_bytes() + b"\n")

    monkeypatch.setattr(up, "_replace", replace_with_other)
    out = up.upgrade_file(sets, "P1.json")
    assert out.result is FileResult.UNCERTAIN
    assert only_record(sets).status is RecordStatus.UNCERTAIN


def test_final_record_write_failure_is_uncertain_not_upgraded(sets, monkeypatch):
    real = up._update_record

    def fail_final(path, old, new):
        if new.status is RecordStatus.UPGRADED:
            raise OSError(28, "No space left on device")
        return real(path, old, new)

    monkeypatch.setattr(up, "_update_record", fail_final)
    out = up.upgrade_file(sets, "P1.json")
    assert out.result is FileResult.UNCERTAIN and "record" in out.reason
    assert load_json(sets / "P1.json").schema_version == 2  # the file was upgraded and verified
    rec = only_record(sets)
    assert rec.status is RecordStatus.BACKED_UP  # evidence for reconciliation: never reported success
    assert mig.result_for(rec, mig.disk_state(mig.observe(sets / "P1.json"), rec)) is FileResult.UNCERTAIN


def test_record_changed_on_disk_stops_before_the_replace_and_is_not_overwritten(sets, monkeypatch):
    real = up._stage
    tampered = {}

    def stage_then_tamper(*args, **kwargs):
        staged = real(*args, **kwargs)
        rec = records_in(sets)[0]
        rec.write_bytes(rec.read_bytes() + b"\n")  # someone edits the record meanwhile
        tampered["bytes"] = rec.read_bytes()
        return staged

    monkeypatch.setattr(up, "_stage", stage_then_tamper)
    out = up.upgrade_file(sets, "P1.json")
    assert out.result is FileResult.FAILED and "record" in out.reason, out.reason
    assert records_in(sets)[0].read_bytes() == tampered["bytes"]  # not overwritten
    assert (sets / "P1.json").read_bytes() == ORIGINAL  # never replaced without its record
    assert not [p for p in sets.iterdir() if p.name.endswith(up.TMP_SUFFIX)]


def test_no_new_ids_after_a_replace_may_have_happened(sets, monkeypatch):
    real_replace, real_read = up._replace, up._read_file
    state = {"replaced": False}

    def replace(src, dst):
        real_replace(src, dst)
        state["replaced"] = True

    def read(path):
        if state["replaced"] and Path(path) == sets / "P1.json":
            raise OSError(5, "I/O error")
        return real_read(path)

    monkeypatch.setattr(up, "_replace", replace)
    monkeypatch.setattr(up, "_read_file", read)
    first = up.upgrade_file(sets, "P1.json")
    assert first.result is FileResult.UNCERTAIN
    monkeypatch.undo()
    committed = (sets / "P1.json").read_bytes()
    second = up.upgrade_file(sets, "P1.json")
    assert second.result in (FileResult.SKIPPED_V2, FileResult.FAILED)  # never a second upgrade
    assert (sets / "P1.json").read_bytes() == committed
    assert [p.id for p in passes_of(sets / "P1.json")] == list(first.pass_ids)
    assert len(records_in(sets)) == 1


# ================================================================ everything else is untouched

def test_only_the_source_backup_and_record_change(lib, sets):
    before = tree(lib.root)
    out = up.upgrade_file(sets, "P1.json")
    after = tree(lib.root)
    added = set(after) - set(before)
    changed = {k for k in set(before) & set(after) if before[k] != after[k]}
    assert set(before) <= set(after)  # nothing deleted
    assert changed == {"sets/P1.json"}
    rec_rel = out.record_path.relative_to(lib.root).as_posix()
    assert added == {f"sets/{up.MIGRATION_DIR}/P1.json", rec_rel}


def test_library_ignores_the_migration_folder(lib, sets):
    names_before = sorted(lib.load_all())
    up.upgrade_file(sets, "P1.json")
    fresh = Library(lib.root)
    assert sorted(fresh.load_all()) == names_before and fresh.load_errors == []
    assert fresh.get("P1").schema_version == 2


def test_classify_bytes_matches_classify(sets):
    for f in sorted(sets.glob("*.json")):
        data = f.read_bytes()
        assert mig.classify_bytes(data) == mig.classify(f)
