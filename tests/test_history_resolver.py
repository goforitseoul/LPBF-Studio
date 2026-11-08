"""DC-001 §4.2, §5.2, §6: the resolver, against on-disk states built by hand. Reads only."""

import json
import os
import shutil
import sys

import pytest

from _history_support import (
    ID_A, ID_B, SETS, add_revision, canonical, can_symlink, identity_doc, junction, make_identity, make_object, objdir,
    record_doc, seq, sha, tree,
)
from lpbfstudio import history as h
from lpbfstudio.history import ObjectState as O
from lpbfstudio.history import ObjectType as T
from lpbfstudio.history import PathState as S

P = "sets/a.json"


def resolve(root, path=P, otype=T.EXPOSURE_SET):
    return h.resolve(root, otype, path)


def state_of(root, oid=ID_A):
    return {o.object_id: o for o in h.scan_objects(root, T.EXPOSURE_SET)}[oid].state


# --------------------------------------------------------------------------- §6.2 rows


def test_row0_history_is_a_file(tmp_path):
    (tmp_path / ".history").write_bytes(b"")
    canonical(tmp_path)
    assert resolve(tmp_path).state is S.UNVERIFIABLE


def test_row0_objects_is_a_file(tmp_path):
    (tmp_path / ".history").mkdir()
    (tmp_path / ".history/objects").write_bytes(b"")
    assert resolve(tmp_path).state is S.UNVERIFIABLE


def test_row0_type_folder_is_a_file(tmp_path):
    (tmp_path / ".history/objects").mkdir(parents=True)
    (tmp_path / ".history/objects" / SETS).write_bytes(b"")
    assert resolve(tmp_path).state is S.UNVERIFIABLE


def test_row0_unknown_entries_in_the_fixed_folders(tmp_path):
    (tmp_path / ".history/objects" / SETS).mkdir(parents=True)
    (tmp_path / ".history/stray").write_bytes(b"")
    assert resolve(tmp_path).state is S.UNVERIFIABLE
    (tmp_path / ".history/stray").unlink()
    (tmp_path / ".history/objects/stray").mkdir()
    assert resolve(tmp_path).state is S.UNVERIFIABLE
    (tmp_path / ".history/objects/stray").rmdir()
    (tmp_path / ".history/jobs").write_bytes(b"")  # jobs must be a folder if it exists
    assert resolve(tmp_path).state is S.UNVERIFIABLE


def test_row0_p_exists_but_cannot_be_read(tmp_path):
    (tmp_path / P).mkdir(parents=True)  # a folder at P: exists, cannot be read as a file
    assert resolve(tmp_path).state is S.UNVERIFIABLE


def test_row0_scan_read_error_is_unverifiable_not_damage(tmp_path, monkeypatch):
    make_object(tmp_path)
    canonical(tmp_path)

    def fail(step):
        if step == "scan.read":
            raise PermissionError(13, "denied")

    monkeypatch.setattr(h, "_fault", fail)
    r = resolve(tmp_path)
    assert r.state is S.UNVERIFIABLE and r.errno == 13


def test_row1_any_d5_makes_every_path_of_the_type_unknown(tmp_path):
    make_object(tmp_path, [b"x"], "sets/a.json", oid=ID_A)
    canonical(tmp_path, b"x")
    (objdir(tmp_path, ID_B)).mkdir(parents=True)
    (objdir(tmp_path, ID_B) / "junk").write_bytes(b"")
    r = resolve(tmp_path)
    assert r.state is S.UNKNOWN and any(ID_B in d for d in r.damaged)
    assert r.object_id is None


def test_row1_is_limited_to_the_affected_type(tmp_path):
    make_object(tmp_path, [b"x"], "sets/a.json", oid=ID_A)
    canonical(tmp_path, b"x")
    d = tmp_path / ".history/objects/machine_profile" / ID_B
    d.mkdir(parents=True)
    (d / "junk").write_bytes(b"")
    assert resolve(tmp_path).state is S.TRACKED


def test_row2_two_claims(tmp_path):
    make_object(tmp_path, [b"x"], oid=ID_A)
    make_object(tmp_path, [b"x"], oid=ID_B)
    canonical(tmp_path, b"x")
    r = resolve(tmp_path)
    assert r.state is S.UNKNOWN and r.object_id is None


def test_row3_missing(tmp_path):
    make_object(tmp_path)
    r = resolve(tmp_path)
    assert r.state is S.MISSING and r.object_id is None and r.claimant == ID_A


def test_row4_tracked_at_head(tmp_path):
    make_object(tmp_path, [b"1", b"2"])
    canonical(tmp_path, b"2")
    r = resolve(tmp_path)
    assert (r.state, r.object_id, r.seq, r.head_seq, r.history_ahead) == (S.TRACKED, ID_A, seq(2), seq(2), False)


def test_row4_history_ahead(tmp_path):
    make_object(tmp_path, [b"1", b"2"])
    canonical(tmp_path, b"1")
    r = resolve(tmp_path)
    assert (r.state, r.object_id, r.seq, r.head_seq, r.history_ahead) == (S.TRACKED, ID_A, seq(1), seq(2), True)


def test_row4_highest_matching_revision_wins(tmp_path):
    make_object(tmp_path, [b"1", b"2", b"1"])
    canonical(tmp_path, b"1")
    r = resolve(tmp_path)
    assert (r.seq, r.history_ahead) == (seq(3), False)


def test_row5_changed_outside_history(tmp_path):
    make_object(tmp_path, [b"1"])
    canonical(tmp_path, b"other")
    r = resolve(tmp_path)
    assert r.state is S.CHANGED_OUTSIDE and r.object_id is None and r.claimant == ID_A and r.head_seq == seq(1)


def test_tombstone_is_never_the_matching_revision(tmp_path):
    make_identity(tmp_path)
    add_revision(tmp_path, 1, b"x")
    add_revision(tmp_path, 2, None, kind="deleted")
    add_revision(tmp_path, 3, b"y")  # content after a tombstone (the Windows race)
    canonical(tmp_path, b"y")
    r = resolve(tmp_path)
    assert (r.state, r.seq) == (S.TRACKED, seq(3))


def test_race_a_tombstone_then_content_resolves_by_rows_3_to_5(tmp_path):
    make_identity(tmp_path)
    add_revision(tmp_path, 1, b"x")
    add_revision(tmp_path, 2, None, kind="deleted")
    add_revision(tmp_path, 3, b"y")
    assert resolve(tmp_path).state is S.MISSING
    canonical(tmp_path, b"z")
    assert resolve(tmp_path).state is S.CHANGED_OUTSIDE


def test_row6_tombstoned_file_present(tmp_path):
    make_identity(tmp_path)
    add_revision(tmp_path, 1, b"x")
    add_revision(tmp_path, 2, None, kind="deleted")
    canonical(tmp_path, b"x")
    r = resolve(tmp_path)
    assert r.state is S.TOMBSTONED_PRESENT and r.object_id is None


def test_row7_untracked(tmp_path):
    canonical(tmp_path, b"x")
    r = resolve(tmp_path)
    assert r.state is S.UNTRACKED and r.data == b"x" and r.object_id is None


def test_row8_no_state(tmp_path):
    assert resolve(tmp_path).state is S.NONE


def test_row8_after_tombstone_when_the_file_is_absent(tmp_path):
    make_identity(tmp_path)
    add_revision(tmp_path, 1, b"x")
    add_revision(tmp_path, 2, None, kind="deleted")
    assert resolve(tmp_path).state is S.NONE


def test_no_name_or_hash_attachment(tmp_path):
    """A file with bytes equal to another object's revision is not attached to it."""
    make_object(tmp_path, [b"same"], "sets/other.json")
    canonical(tmp_path, b"same", "sets/a.json")
    assert resolve(tmp_path).state is S.UNTRACKED


def test_resolve_reads_only(tmp_path):
    make_object(tmp_path, [b"1", b"2"])
    canonical(tmp_path, b"1")
    before = tree(tmp_path)
    resolve(tmp_path)
    assert tree(tmp_path) == before
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    canonical(fresh, b"x")
    before = tree(fresh)
    resolve(fresh)
    assert tree(fresh) == before  # no .history is created by reading


def test_works_for_each_object_type(tmp_path):
    make_object(tmp_path, [b"p"], "process.json", otype="process_settings")
    canonical(tmp_path, b"p", "process.json")
    assert resolve(tmp_path, "process.json", T.PROCESS_SETTINGS).state is S.TRACKED
    make_object(tmp_path, [b"m"], "machine.json", otype="machine_profile")
    canonical(tmp_path, b"m", "machine.json")
    assert resolve(tmp_path, "machine.json", T.MACHINE_PROFILE).state is S.TRACKED


# --------------------------------------------------------------------------- §6.1.2 object states


def test_d1_reservation(tmp_path):
    objdir(tmp_path).mkdir(parents=True)
    assert state_of(tmp_path) is O.D1
    (objdir(tmp_path) / ".identity.json.abc123.tmp").write_bytes(b"partial")
    assert state_of(tmp_path) is O.D1


def test_d1_is_ignored_for_claims_and_reported(tmp_path):
    objdir(tmp_path).mkdir(parents=True)
    canonical(tmp_path)
    r = resolve(tmp_path)
    assert r.state is S.UNTRACKED and ID_A in " ".join(r.observations)


def test_d2_empty_object_forms(tmp_path):
    make_identity(tmp_path)
    assert state_of(tmp_path) is O.D2
    (objdir(tmp_path) / "revisions").mkdir()
    assert state_of(tmp_path) is O.D2
    (objdir(tmp_path) / "revisions" / seq(1)).mkdir()
    assert state_of(tmp_path) is O.D2


def test_d3_head_is_the_highest_committed_with_incomplete_between(tmp_path):
    make_object(tmp_path, [b"1"])
    (objdir(tmp_path) / "revisions" / seq(2)).mkdir()
    add_revision(tmp_path, 3, b"3")
    (objdir(tmp_path) / "revisions" / seq(4)).mkdir()
    canonical(tmp_path, b"3")
    assert state_of(tmp_path) is O.D3
    r = resolve(tmp_path)
    assert (r.seq, r.head_seq) == (seq(3), seq(3))


def test_incomplete_is_never_the_head_or_current(tmp_path):
    make_object(tmp_path, [b"1"])
    rev = objdir(tmp_path) / "revisions" / seq(2)
    rev.mkdir()
    (rev / "content").write_bytes(b"2")
    canonical(tmp_path, b"2")
    assert resolve(tmp_path).state is S.CHANGED_OUTSIDE


def test_d4_tombstoned(tmp_path):
    make_identity(tmp_path)
    add_revision(tmp_path, 1, b"x")
    add_revision(tmp_path, 2, None, kind="deleted")
    assert state_of(tmp_path) is O.D4


@pytest.mark.parametrize("name", ["short", "A" * 32, "g" * 32, "a" * 33])
def test_d5_bad_folder_name(tmp_path, name):
    (tmp_path / ".history/objects" / SETS / name).mkdir(parents=True)
    r = resolve(tmp_path)
    assert r.state is S.UNKNOWN


def test_d5_file_where_a_folder_belongs(tmp_path):
    (tmp_path / ".history/objects" / SETS).mkdir(parents=True)
    (tmp_path / ".history/objects" / SETS / ID_A).write_bytes(b"")
    assert resolve(tmp_path).state is S.UNKNOWN


@pytest.mark.parametrize("setup", ["identity_less_extra", "two_temps", "temp_next_to_identity", "revisions_no_identity",
                                   "unknown_entry", "revisions_is_file", "identity_is_folder"])
def test_d5_structures(tmp_path, setup):
    d = objdir(tmp_path)
    d.mkdir(parents=True)
    if setup == "identity_less_extra":
        (d / "other").write_bytes(b"")
    elif setup == "two_temps":
        (d / ".identity.json.a1.tmp").write_bytes(b"")
        (d / ".identity.json.a2.tmp").write_bytes(b"")
    elif setup == "temp_next_to_identity":
        (d / "identity.json").write_bytes(identity_doc())
        (d / ".identity.json.a1.tmp").write_bytes(b"")
    elif setup == "revisions_no_identity":
        (d / "revisions").mkdir()
    elif setup == "unknown_entry":
        (d / "identity.json").write_bytes(identity_doc())
        (d / "notes.txt").write_bytes(b"")
    elif setup == "revisions_is_file":
        (d / "identity.json").write_bytes(identity_doc())
        (d / "revisions").write_bytes(b"")
    elif setup == "identity_is_folder":
        (d / "identity.json").mkdir()
    assert state_of(tmp_path) is O.D5


@pytest.mark.parametrize("doc", [b"not json", b"[]", json.dumps({"object_type": SETS, "object_id": ID_A}).encode(),
                                 json.dumps({"object_type": "machine_profile", "object_id": ID_A, "created_at": "t"}).encode(),
                                 json.dumps({"object_type": SETS, "object_id": ID_B, "created_at": "t"}).encode(),
                                 json.dumps({"object_type": SETS, "object_id": ID_A, "created_at": "t", "x": 1}).encode(),
                                 b"\xff\xfe"])
def test_d5_bad_identity(tmp_path, doc):
    d = objdir(tmp_path)
    d.mkdir(parents=True)
    (d / "identity.json").write_bytes(doc)
    assert state_of(tmp_path) is O.D5


def test_d5_gap_in_folder_numbers(tmp_path):
    make_object(tmp_path, [b"1"])
    add_revision(tmp_path, 3, b"3")
    assert state_of(tmp_path) is O.D5


def test_d5_does_not_start_at_one(tmp_path):
    make_identity(tmp_path)
    add_revision(tmp_path, 2, b"3")
    assert state_of(tmp_path) is O.D5


@pytest.mark.parametrize("name", ["1", "0000001", "000000012", "abcdefgh", "00000000"])
def test_d5_bad_revision_name(tmp_path, name):
    make_object(tmp_path, [b"1"])
    (objdir(tmp_path) / "revisions" / name).mkdir()
    assert state_of(tmp_path) is O.D5


def test_d5_file_in_revisions(tmp_path):
    make_object(tmp_path, [b"1"])
    (objdir(tmp_path) / "revisions" / "stray").write_bytes(b"")
    assert state_of(tmp_path) is O.D5


# --------------------------------------------------------------------------- §5.2 revision folders


def rev(tmp_path, n=2):
    make_object(tmp_path, [b"1"])
    r = objdir(tmp_path) / "revisions" / seq(n)
    r.mkdir()
    return r


@pytest.mark.parametrize("files", [[], ["content"], ["content", ".record.json.a1.tmp"], [".record.json.a1.tmp"]])
def test_incomplete_forms(tmp_path, files):
    r = rev(tmp_path)
    for f in files:
        (r / f).write_bytes(b"partial")
    assert state_of(tmp_path) is O.D3


@pytest.mark.parametrize("files", [["other"], ["content", "other"], [".record.json.a.tmp", ".record.json.b.tmp"],
                                   ["content", ".record.json.a.tmp", ".record.json.b.tmp"], [".record.json.tmp"]])
def test_integrity_failure_names(tmp_path, files):
    r = rev(tmp_path)
    for f in files:
        (r / f).write_bytes(b"x")
    assert state_of(tmp_path) is O.D5


def write_record(r, doc):
    (r / "record.json").write_text(json.dumps(doc), encoding="utf-8")


def test_committed_with_blob_mismatch_is_integrity_failure(tmp_path):
    r = rev(tmp_path)
    (r / "content").write_bytes(b"different")
    write_record(r, record_doc(2, data=b"x2"))
    assert state_of(tmp_path) is O.D5


def test_committed_with_length_mismatch_is_integrity_failure(tmp_path):
    r = rev(tmp_path)
    (r / "content").write_bytes(b"x2")
    write_record(r, record_doc(2, data=b"x2", byte_length=3))
    assert state_of(tmp_path) is O.D5


def test_content_record_without_blob_is_integrity_failure(tmp_path):
    r = rev(tmp_path)
    write_record(r, record_doc(2, data=b"x2"))
    assert state_of(tmp_path) is O.D5


def test_tombstone_with_a_blob_is_integrity_failure(tmp_path):
    r = rev(tmp_path)
    (r / "content").write_bytes(b"x")
    write_record(r, record_doc(2, kind="deleted", data=None))
    assert state_of(tmp_path) is O.D5


def test_record_with_temporary_file_is_integrity_failure(tmp_path):
    r = rev(tmp_path)
    (r / "content").write_bytes(b"x2")
    write_record(r, record_doc(2, data=b"x2"))
    (r / ".record.json.ab.tmp").write_bytes(b"")
    assert state_of(tmp_path) is O.D5


BAD_FIELDS = [
    {"record_version": 2}, {"record_version": "1"}, {"object_type": "machine_profile"}, {"object_id": ID_B}, {"seq": seq(9)},
    {"kind": "other"}, {"operation": "nonsense"}, {"path": "../a.json"}, {"path": "sets\\a.json"}, {"path": "/sets/a.json"},
    {"path": "machines/a.json"}, {"path": "sets/a.txt"}, {"path": "sets/x/a.json"}, {"path": 5}, {"path": "sets/a.json\n"},
    {"previous_path": "sets/b.json"}, {"content_hash": None}, {"content_hash": {"algorithm": "md5", "value": "0" * 64}},
    {"content_hash": {"algorithm": "sha256", "value": "Z" * 64}}, {"content_hash": "x"}, {"byte_length": None},
    {"byte_length": -1}, {"byte_length": True}, {"byte_length": "2"}, {"lock_change": "unlocked"}, {"created_at": 5},
    {"created_at": ""}, {"software": {}}, {"software": {"version": "1"}}, {"software": {"version": 1, "git_commit": "x"}},
    {"platform": {}}, {"platform": None}, {"extra": 1},
]


@pytest.mark.parametrize("over", BAD_FIELDS, ids=lambda o: "-".join(f"{k}={v}"[:24] for k, v in o.items()))
def test_invalid_record_fields_are_integrity_failures(tmp_path, over):
    r = rev(tmp_path)
    (r / "content").write_bytes(b"x2")
    write_record(r, record_doc(2, data=b"x2", **over))
    assert state_of(tmp_path) is O.D5


@pytest.mark.parametrize("key", ["record_version", "kind", "operation", "path", "previous_path", "content_hash",
                                 "byte_length", "lock_change", "source", "created_at", "software", "platform"])
def test_a_missing_key_is_an_integrity_failure(tmp_path, key):
    r = rev(tmp_path)
    (r / "content").write_bytes(b"x2")
    doc = record_doc(2, data=b"x2")
    del doc[key]
    write_record(r, doc)
    assert state_of(tmp_path) is O.D5


@pytest.mark.parametrize("raw", [b"", b"{", b"[1]", b"\xff\xfe", b"null"])
def test_unparsable_records_are_integrity_failures(tmp_path, raw):
    r = rev(tmp_path)
    (r / "content").write_bytes(b"x2")
    (r / "record.json").write_bytes(raw)
    assert state_of(tmp_path) is O.D5


GOOD_SRC = {"object_id": ID_B, "seq": seq(1)}
UNTRACKED = {"path": "sets/o.json", "content_hash": {"algorithm": "sha256", "value": "0" * 64}, "state": "untracked"}
EXTERNAL = {"external_path": "C:/x.json", "content_hash": {"algorithm": "sha256", "value": "0" * 64}}
REB_U = {"kind": "upgrade", "record_id": "1" * 32, "restore_id": None, "prior_head_seq": seq(1)}
REB_R = {"kind": "restore", "record_id": "1" * 32, "restore_id": "2" * 32, "prior_head_seq": seq(1)}
OWN = {"object_id": ID_A, "seq": seq(1)}


@pytest.mark.parametrize("op,src", [("adopted", None), ("created", None), ("saved", OWN), ("copied", GOOD_SRC),
                                    ("copied", UNTRACKED), ("doe", GOOD_SRC), ("doe", UNTRACKED), ("imported", EXTERNAL),
                                    ("rebaselined", REB_U), ("rebaselined", REB_R)])
def test_valid_source_forms(tmp_path, op, src):
    r = rev(tmp_path)
    (r / "content").write_bytes(b"x2")
    write_record(r, record_doc(2, data=b"x2", operation=op, source=src))
    assert state_of(tmp_path) is O.D3


def test_valid_rename_record_needs_previous_path(tmp_path):
    r = rev(tmp_path)
    (r / "content").write_bytes(b"x2")
    write_record(r, record_doc(2, data=b"x2", operation="renamed", previous_path="sets/o.json", source=OWN))
    assert state_of(tmp_path) is O.D3


def test_rename_record_without_previous_path_is_invalid(tmp_path):
    r = rev(tmp_path)
    (r / "content").write_bytes(b"x2")
    write_record(r, record_doc(2, data=b"x2", operation="renamed", source=OWN))
    assert state_of(tmp_path) is O.D5


BAD_SOURCES = [
    ("saved", GOOD_SRC), ("saved", None), ("saved", UNTRACKED), ("saved", {"object_id": ID_A, "seq": seq(2)}),
    ("saved", {"object_id": ID_A, "seq": 1}), ("adopted", OWN), ("created", OWN), ("imported", None), ("imported", OWN),
    ("copied", EXTERNAL), ("copied", None), ("doe", None), ("rebaselined", None), ("rebaselined", OWN),
    ("copied", {**GOOD_SRC, "x": 1}), ("copied", {**UNTRACKED, "state": "other"}), ("imported", {"external_path": "x"}),
    ("copied", "text"),
    ("rebaselined", {**REB_U, "kind": "external"}), ("rebaselined", {**REB_U, "kind": None}),
    ("rebaselined", {**REB_U, "record_id": "short"}), ("rebaselined", {**REB_U, "restore_id": "2" * 32}),
    ("rebaselined", {**REB_R, "restore_id": None}), ("rebaselined", {**REB_R, "restore_id": "Z" * 32}),
    ("rebaselined", {**REB_U, "prior_head_seq": seq(2)}), ("rebaselined", {**REB_U, "prior_head_seq": seq(3)}),
    ("rebaselined", {**REB_U, "prior_head_seq": 1}), ("rebaselined", {**REB_U, "extra": 1}),
    ("rebaselined", {k: v for k, v in REB_U.items() if k != "restore_id"}),
]


@pytest.mark.parametrize("op,src", BAD_SOURCES, ids=[f"{i}-{op}" for i, (op, _) in enumerate(BAD_SOURCES)])
def test_invalid_sources_fail_closed(tmp_path, op, src):
    r = rev(tmp_path)
    (r / "content").write_bytes(b"x2")
    doc = record_doc(2, data=b"x2", operation=op)
    doc["source"] = src
    write_record(r, doc)
    assert state_of(tmp_path) is O.D5
    canonical(tmp_path, b"x2")
    assert resolve(tmp_path).state is S.UNKNOWN


def test_kind_and_operation_must_agree(tmp_path):
    r = rev(tmp_path)
    write_record(r, record_doc(2, kind="deleted", data=None, operation="saved", source=OWN))
    assert state_of(tmp_path) is O.D5


def test_rebaselined_revision_is_ordinary_content(tmp_path):
    make_object(tmp_path, [b"1"])
    add_revision(tmp_path, 2, b"2", operation="rebaselined", source=REB_U)
    canonical(tmp_path, b"2")
    r = resolve(tmp_path)
    assert (r.state, r.seq) == (S.TRACKED, seq(2))


# --------------------------------------------------------------------------- §6.1.1 entry types


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junctions exist only on Windows")
class TestWindowsReparsePoints:
    def test_junction_for_the_history_folder_is_unverifiable(self, tmp_path):
        target = tmp_path / "elsewhere"
        target.mkdir()
        (tmp_path / "lib").mkdir()
        junction(tmp_path / "lib/.history", target)
        assert h.resolve(tmp_path / "lib", T.EXPOSURE_SET, P).state is S.UNVERIFIABLE

    def test_junction_as_a_type_folder_is_unverifiable(self, tmp_path):
        (tmp_path / ".history/objects").mkdir(parents=True)
        target = tmp_path / "t"
        target.mkdir()
        junction(tmp_path / ".history/objects" / SETS, target)
        assert resolve(tmp_path).state is S.UNVERIFIABLE

    def test_junction_as_an_object_folder_is_d5(self, tmp_path):
        (tmp_path / ".history/objects" / SETS).mkdir(parents=True)
        target = tmp_path / "t"
        target.mkdir()
        junction(objdir(tmp_path), target)
        assert resolve(tmp_path).state is S.UNKNOWN

    def test_junction_as_revisions_is_d5(self, tmp_path):
        make_identity(tmp_path)
        target = tmp_path / "t"
        target.mkdir()
        junction(objdir(tmp_path) / "revisions", target)
        assert state_of(tmp_path) is O.D5

    def test_junction_as_a_revision_folder_is_d5(self, tmp_path):
        make_object(tmp_path, [b"1"])
        target = tmp_path / "t"
        target.mkdir()
        junction(objdir(tmp_path) / "revisions" / seq(2), target)
        assert state_of(tmp_path) is O.D5


def test_reparse_attribute_alone_is_enough(tmp_path, monkeypatch):
    """The bit decides, even if nothing else reports a link."""
    make_object(tmp_path, [b"1"])
    canonical(tmp_path, b"1")
    real = os.lstat

    class Fake:
        def __init__(self, st):
            self._st = st

        def __getattr__(self, n):
            return 0x400 if n == "st_file_attributes" else getattr(self._st, n)

    monkeypatch.setattr(h, "_lstat", lambda p: Fake(real(p)) if str(p).endswith("record.json") else real(p))
    assert resolve(tmp_path).state is S.UNKNOWN


def links_or_skip(tmp_path):
    if not can_symlink(tmp_path):
        pytest.skip("this account cannot create symbolic links")


def test_symlink_as_history_folder(tmp_path):
    links_or_skip(tmp_path)
    (tmp_path / "t").mkdir()
    (tmp_path / "lib").mkdir()
    os.symlink(tmp_path / "t", tmp_path / "lib/.history", target_is_directory=True)
    assert h.resolve(tmp_path / "lib", T.EXPOSURE_SET, P).state is S.UNVERIFIABLE


def test_symlink_as_object_folder(tmp_path):
    links_or_skip(tmp_path)
    (tmp_path / ".history/objects" / SETS).mkdir(parents=True)
    (tmp_path / "t").mkdir()
    os.symlink(tmp_path / "t", objdir(tmp_path), target_is_directory=True)
    assert resolve(tmp_path).state is S.UNKNOWN


def test_symlink_as_identity(tmp_path):
    links_or_skip(tmp_path)
    objdir(tmp_path).mkdir(parents=True)
    (tmp_path / "i.json").write_bytes(identity_doc())
    os.symlink(tmp_path / "i.json", objdir(tmp_path) / "identity.json")
    assert state_of(tmp_path) is O.D5


def test_symlink_as_blob_or_record(tmp_path):
    links_or_skip(tmp_path)
    r = rev(tmp_path)
    (tmp_path / "blob").write_bytes(b"x2")
    os.symlink(tmp_path / "blob", r / "content")
    write_record(r, record_doc(2, data=b"x2"))
    assert state_of(tmp_path) is O.D5


@pytest.mark.skipif(sys.platform == "win32", reason="a file name cannot hold a newline on Windows")
def test_a_trailing_newline_never_passes_as_an_id_or_a_number(tmp_path):
    (tmp_path / ".history/objects" / SETS / (ID_A + "\n")).mkdir(parents=True)
    assert resolve(tmp_path).state is S.UNKNOWN
    shutil.rmtree(tmp_path / ".history")
    make_object(tmp_path, [b"1"])
    (objdir(tmp_path) / "revisions" / (seq(2) + "\n")).mkdir()
    assert state_of(tmp_path) is O.D5


def test_id_and_number_patterns_reject_a_trailing_newline():
    assert not h._ID.match(ID_A + "\n") and not h._SEQ.match(seq(1) + "\n") and not h._SHA.match("0" * 64 + "\n")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
def test_fifo_in_a_revision_is_d5_and_is_not_opened(tmp_path):
    r = rev(tmp_path)
    os.mkfifo(r / "content")
    write_record(r, record_doc(2, data=b"x2"))
    assert state_of(tmp_path) is O.D5  # finishes: the FIFO is never opened
