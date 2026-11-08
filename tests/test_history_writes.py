"""DC-001 §7.4 to §7.10: the history write steps. Every §7.10 row by deterministic injection, on both platforms; real
process kills at every step boundary. The canonical step is played by the real ``_atomic_write``; nothing in the product
calls these functions yet (ABP-0.2)."""

import errno
import hashlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from _history_support import ID_A, add_revision, canonical, make_identity, make_object, objdir, seq, sha, tree
from lpbfstudio import history as h
from lpbfstudio import library as libmod
from lpbfstudio import locks
from lpbfstudio.history import ObjectState as O
from lpbfstudio.history import ObjectType as T
from lpbfstudio.history import PathState as S
from lpbfstudio.outcomes import AdoptionStatus as A
from lpbfstudio.outcomes import CanonicalReason as CR
from lpbfstudio.outcomes import CanonicalStatus as C
from lpbfstudio.outcomes import HistoryReason as HR
from lpbfstudio.outcomes import HistoryStatus as H
from lpbfstudio.outcomes import Readback as R

P = "sets/a.json"
Q = "sets/b.json"
TEXT = 'first\nsecond\n'
ST = T.EXPOSURE_SET


@pytest.fixture
def root(tmp_path):
    (tmp_path / "sets").mkdir()
    return tmp_path


def fail_at(monkeypatch, *steps, exc=None, times=None):
    """Raise at the named steps; ``times`` limits how often."""
    n = {"left": times}

    def fault(step):
        if step in steps and (n["left"] is None or n["left"] > 0):
            if n["left"] is not None:
                n["left"] -= 1
            raise (exc or OSError(errno.EIO, "injected"))

    monkeypatch.setattr(h, "_fault", fault)


def on_step(monkeypatch, step, action):
    def fault(s):
        if s == step:
            action()

    monkeypatch.setattr(h, "_fault", fault)


def canon_write(root, path, text):
    libmod._atomic_write(root / path, text)


def bytes_of(text):
    """The bytes the routine writes here (the test's own prediction, for hand-built prior states only)."""
    return text.replace("\n", os.linesep).encode()


def save_flow(root, text=TEXT, path=P, fail_canon=None, **kw):
    hr = h.phase_h_write(root, ST, path, text, **kw)
    exc = None
    if fail_canon is None:
        canon_write(root, path, text)
    else:
        exc = fail_canon()
    return hr, h.conclude(hr, "save", exc)


def state(root, path=P):
    return h.resolve(root, ST, path)


def obj_state(root, oid):
    return {o.object_id: o.state for o in h.scan_objects(root, ST)}[oid]


def only_object(root):
    objs = h.scan_objects(root, ST)
    assert len(objs) == 1
    return objs[0]


def tracked_at_1(root, data=b"old"):
    make_object(root, [data])
    canonical(root, data)


# --------------------------------------------------------------------------- normal paths


def test_new_object_is_created_at_one(root):
    hr, out = save_flow(root)
    assert out.complete and out.adoption is A.NOT_NEEDED and out.seq == seq(1)
    o = only_object(root)
    assert o.state is O.D3 and o.head.operation == "created" and o.head.source is None
    r = state(root)
    assert (r.state, r.object_id, r.seq) == (S.TRACKED, out.object_id, seq(1))


def test_first_save_of_an_untracked_file_adopts_then_saves(root):
    canonical(root, b"on disk\n")
    hr, out = save_flow(root)
    assert out.complete and out.adoption is A.COMMITTED and out.adopted_seq == seq(1) and out.seq == seq(2)
    rev = objdir(root, out.object_id) / "revisions"
    assert (rev / seq(1) / "content").read_bytes() == b"on disk\n"
    assert json.loads((rev / seq(1) / "record.json").read_text())["operation"] == "adopted"
    r2 = json.loads((rev / seq(2) / "record.json").read_text())
    assert r2["operation"] == "saved" and r2["source"] == {"object_id": out.object_id, "seq": seq(1)}


def test_save_of_tracked_appends(root):
    tracked_at_1(root)
    hr, out = save_flow(root)
    assert out.complete and out.seq == seq(2) and out.object_id == ID_A
    head = only_object(root).head
    assert head.source == {"object_id": ID_A, "seq": seq(1)}


def test_save_from_history_ahead_names_the_matching_revision(root):
    make_object(root, [b"1", b"2"])
    canonical(root, b"1")
    _, out = save_flow(root)
    assert out.seq == seq(3) and only_object(root).head.source == {"object_id": ID_A, "seq": seq(1)}


def test_blob_is_the_routines_actual_output_not_the_text(root):
    _, out = save_flow(root)
    blob = (objdir(root, out.object_id) / "revisions" / seq(1) / "content").read_bytes()
    assert blob == (root / P).read_bytes()
    if os.name == "nt":
        assert blob != TEXT.encode() and b"\r\n" in blob
    else:
        assert blob == TEXT.encode()


def test_staging_file_is_removed_afterwards(root):
    save_flow(root)
    assert os.listdir(root / ".history" / "staging") == []


def test_record_fields(root):
    _, out = save_flow(root)
    doc = json.loads((objdir(root, out.object_id) / "revisions" / seq(1) / "record.json").read_text())
    blob = (root / P).read_bytes()
    assert doc["record_version"] == 1 and doc["object_type"] == "exposure_set" and doc["kind"] == "content"
    assert doc["path"] == P and doc["previous_path"] is None and doc["lock_change"] is None
    assert doc["content_hash"] == {"algorithm": "sha256", "value": sha(blob)} and doc["byte_length"] == len(blob)
    assert set(doc["software"]) == {"version", "git_commit"} and set(doc["platform"]) == {"os", "architecture", "python_version"}
    assert doc["created_at"]
    assert not list((objdir(root, out.object_id) / "revisions" / seq(1)).glob(".*"))


def test_lock_change_is_recorded(root):
    tracked_at_1(root)
    save_flow(root, lock_change="locked")
    assert only_object(root).head.lock_change == "locked"


def test_identity_is_write_once_with_three_fields(root):
    _, out = save_flow(root)
    doc = json.loads((objdir(root, out.object_id) / "identity.json").read_text())
    assert set(doc) == {"object_type", "object_id", "created_at"}


@pytest.mark.parametrize("op,src", [("copied", {"object_id": "b" * 32, "seq": seq(1)}), ("doe", {"object_id": "b" * 32, "seq": seq(1)}),
                                    ("imported", {"external_path": "x.json", "content_hash": {"algorithm": "sha256", "value": "0" * 64}})])
def test_new_objects_carry_operation_and_source(root, op, src):
    _, out = save_flow(root, new_operation=op, new_source=src)
    head = only_object(root).head
    assert out.complete and head.operation == op and head.source == src


def test_copy_source_helper_tracked_and_untracked(root):
    make_object(root, [b"1"], "sets/s.json")
    canonical(root, b"1", "sets/s.json")
    assert h.copy_source(root, ST, "sets/s.json") == {"object_id": ID_A, "seq": seq(1)}
    canonical(root, b"u", "sets/u.json")
    before = tree(root)
    assert h.copy_source(root, ST, "sets/u.json") == {
        "path": "sets/u.json", "content_hash": {"algorithm": "sha256", "value": sha(b"u")}, "state": "untracked"}
    assert tree(root) == before  # a copy source is never adopted


def test_import_overwrite_is_not_recorded(root):
    canonical(root, b"old")
    before = {k: v for k, v in tree(root).items()}
    hr, out = save_flow(root, overwrite_undefined=True)
    assert out.history is H.NOT_RECORDED and out.history_reason is HR.IMPORT_OVERWRITE_UNDEFINED and out.seq is None
    assert ".history" not in {k.split("/")[0] for k in tree(root)} and before


def test_rename_keeps_the_object_and_records_both_paths(root):
    tracked_at_1(root)
    hr = h.phase_h_rename(root, ST, P, Q, TEXT)
    canon_write(root, Q, TEXT)
    (root / P).unlink()
    out = h.conclude(hr, "rename", None)
    assert out.complete and out.path == Q and out.previous_path == P and out.previous_path_readback is R.ABSENT
    head = only_object(root).head
    assert (head.operation, head.path, head.previous_path, head.source) == ("renamed", Q, P, {"object_id": ID_A, "seq": seq(1)})
    assert state(root, Q).state is S.TRACKED


def test_rename_adopts_an_untracked_source(root):
    canonical(root, b"old")
    hr = h.phase_h_rename(root, ST, P, Q, TEXT)
    canon_write(root, Q, TEXT)
    (root / P).unlink()
    out = h.conclude(hr, "rename", None)
    assert out.complete and out.adoption is A.COMMITTED and out.seq == seq(2)


def delete_flow(root, path=P, fail=None):
    hr = h.phase_h_delete(root, ST, path)
    exc = None
    if fail is None:
        (root / path).unlink()
    else:
        exc = fail()
    return hr, h.conclude(hr, "delete", exc)


def test_delete_writes_a_tombstone(root):
    tracked_at_1(root)
    _, out = delete_flow(root)
    assert out.complete and out.readback is R.ABSENT and out.seq == seq(2)
    o = only_object(root)
    assert o.state is O.D4 and o.head.kind == "deleted" and o.head.source == {"object_id": ID_A, "seq": seq(1)}
    assert not (objdir(root) / "revisions" / seq(2) / "content").exists()


def test_delete_of_untracked_adopts_first_so_the_content_is_kept(root):
    canonical(root, b"keep me")
    _, out = delete_flow(root)
    assert out.complete and out.adoption is A.COMMITTED and out.seq == seq(2)
    assert (objdir(root, out.object_id) / "revisions" / seq(1) / "content").read_bytes() == b"keep me"
    assert state(root).state is S.NONE


def test_a_later_file_is_never_reattached_to_a_tombstoned_object(root):
    tracked_at_1(root)
    delete_flow(root)
    _, out = save_flow(root)
    assert out.complete and out.object_id != ID_A


# --------------------------------------------------------------------------- anomalies


def anomaly_changed(root):
    make_object(root, [b"1"])
    canonical(root, b"other")


def anomaly_unknown(root):
    make_object(root, [b"1"])
    canonical(root, b"1")
    (objdir(root) / "junk").write_bytes(b"")


def anomaly_two(root):
    make_object(root, [b"1"])
    make_object(root, [b"1"], oid="b" * 32)
    canonical(root, b"1")


def anomaly_unverifiable(root):
    (root / ".history").write_bytes(b"")
    canonical(root, b"1")


def anomaly_tombstoned(root):
    make_identity(root)
    add_revision(root, 1, b"x")
    add_revision(root, 2, None, kind="deleted")
    canonical(root, b"x")


def anomaly_missing(root):
    make_object(root, [b"1"])


ANOMALIES = [(anomaly_changed, HR.STATE_CHANGED_OUTSIDE_HISTORY), (anomaly_unknown, HR.STATE_UNKNOWN),
             (anomaly_two, HR.STATE_UNKNOWN), (anomaly_unverifiable, HR.STATE_UNVERIFIABLE),
             (anomaly_tombstoned, HR.STATE_TOMBSTONED_FILE_PRESENT), (anomaly_missing, HR.STATE_MISSING)]


@pytest.mark.parametrize("setup,reason", ANOMALIES, ids=lambda x: getattr(x, "__name__", ""))
def test_anomalies_are_never_adopted_and_the_save_goes_on(root, setup, reason):
    setup(root)
    hist = root / ".history"
    before = tree(hist) if hist.is_dir() else None
    _, out = save_flow(root)
    assert out.canonical is C.DONE and out.history is H.NOT_RECORDED and out.history_reason is reason
    assert out.adoption is A.NOT_NEEDED and out.seq is None and not out.complete
    assert (root / P).read_bytes() == bytes_of(TEXT)
    if before is not None:
        assert tree(hist) == before  # nothing was attached, written or repaired


@pytest.mark.parametrize("setup,reason", [a for a in ANOMALIES if a[0] is not anomaly_missing], ids=lambda x: getattr(x, "__name__", ""))
def test_anomalies_on_delete_are_not_recorded(root, setup, reason):
    setup(root)
    _, out = delete_flow(root)
    assert out.canonical is C.DONE and out.history is H.NOT_RECORDED and out.history_reason is reason


# --------------------------------------------------------------------------- §7.10 adoption rows, by injection


ADOPTION_ROWS = [
    ("A1.read", A.NOT_RECORDED, HR.SOURCE_READ_FAILED, None, S.UNTRACKED),
    ("A2.mkdir", A.NOT_RECORDED, HR.OBJECT_RESERVATION_FAILED, None, S.UNTRACKED),
    ("A3.temp", A.INCOMPLETE, HR.IDENTITY_TEMP_WRITE_FAILED, O.D1, S.UNTRACKED),
    ("A3.fsync", A.INCOMPLETE, HR.IDENTITY_TEMP_WRITE_FAILED, O.D1, S.UNTRACKED),
    ("A3.replace", A.INCOMPLETE, HR.IDENTITY_COMMIT_FAILED, O.D1, S.UNTRACKED),
    ("A3.dirfsync", A.INCOMPLETE, HR.IDENTITY_COMMIT_FAILED, O.D2, S.UNTRACKED),
    ("A4.mkdir_revisions", A.INCOMPLETE, HR.REVISION_RESERVATION_FAILED, O.D2, S.UNTRACKED),
    ("A4.mkdir", A.INCOMPLETE, HR.REVISION_RESERVATION_FAILED, O.D2, S.UNTRACKED),
    ("A5.write", A.INCOMPLETE, HR.BLOB_WRITE_FAILED, O.D2, S.UNTRACKED),
    ("A5.fsync", A.INCOMPLETE, HR.BLOB_WRITE_FAILED, O.D2, S.UNTRACKED),
    ("A5.verify", A.INCOMPLETE, HR.BLOB_VERIFY_FAILED, O.D2, S.UNTRACKED),
    ("A6.temp", A.INCOMPLETE, HR.RECORD_TEMP_WRITE_FAILED, O.D2, S.UNTRACKED),
    ("A6.fsync", A.INCOMPLETE, HR.RECORD_TEMP_WRITE_FAILED, O.D2, S.UNTRACKED),
    ("A6.replace", A.INCOMPLETE, HR.RECORD_COMMIT_FAILED, O.D2, S.UNTRACKED),
]


@pytest.mark.parametrize("step,adoption,reason,obj,pstate", ADOPTION_ROWS, ids=[r[0] for r in ADOPTION_ROWS])
def test_adoption_failure_rows(root, monkeypatch, step, adoption, reason, obj, pstate):
    canonical(root, b"old")
    fail_at(monkeypatch, step)
    hr, out = save_flow(root)
    assert out.canonical is C.DONE and (root / P).read_bytes() == bytes_of(TEXT)
    assert (out.adoption, out.history, out.history_reason) == (adoption, H.NOT_RECORDED, reason)
    assert out.adopted_seq is None and out.seq is None
    objs = h.scan_objects(root, ST)
    if obj is None:
        assert objs == []
    else:
        assert [o.state for o in objs] == [obj]
        assert h.resolve(root, ST, P).state is pstate


def test_a1b_mkdir_collisions_are_counted_and_not_an_io_error(root, monkeypatch):
    canonical(root, b"old")
    calls = []

    def fault(step):
        if step == "A2.mkdir":
            calls.append(step)
            raise FileExistsError(errno.EEXIST, "exists")

    monkeypatch.setattr(h, "_fault", fault)
    _, out = save_flow(root)
    assert out.history_reason is HR.OBJECT_RESERVATION_COLLISIONS_EXHAUSTED and out.history_errno is None
    assert len(calls) == 100


def test_blob_verify_mismatch_has_no_errno(root, monkeypatch):
    canonical(root, b"old")

    def corrupt():
        next((root / ".history/objects/exposure_set").glob("*/revisions/00000001/content")).write_bytes(b"bad")

    on_step(monkeypatch, "A5.verify", corrupt)
    _, out = save_flow(root)
    assert out.history_reason is HR.BLOB_VERIFY_FAILED and out.history_errno is None


def test_a7_committed(root):
    canonical(root, b"old")
    _, out = save_flow(root)
    assert out.adoption is A.COMMITTED


def test_a8_source_changed_stops_h_and_c_still_runs(root, monkeypatch):
    canonical(root, b"old")
    on_step(monkeypatch, "A7.read", lambda: (root / P).write_bytes(b"changed"))
    _, out = save_flow(root)
    assert (out.adoption, out.adopted_seq, out.history, out.history_reason) == (
        A.COMMITTED_SOURCE_CHANGED, seq(1), H.NOT_RECORDED, HR.ADOPTION_SOURCE_CHANGED)
    assert out.canonical is C.DONE and out.seq is None and not out.complete
    assert only_object(root).head.seq == seq(1)  # no target
    assert state(root).state is S.CHANGED_OUTSIDE


def test_a9_recheck_failed(root, monkeypatch):
    canonical(root, b"old")
    fail_at(monkeypatch, "A7.read")
    _, out = save_flow(root)
    assert (out.adoption, out.history, out.history_reason, out.history_errno) == (
        A.COMMITTED_UNVERIFIED, H.NOT_RECORDED, HR.ADOPTION_RECHECK_FAILED, errno.EIO)
    assert out.adopted_seq == seq(1)


def test_adoption_never_writes_the_canonical_file(root):
    canonical(root, b"old")
    h.phase_h_write(root, ST, P, TEXT)
    assert (root / P).read_bytes() == b"old"


# --------------------------------------------------------------------------- §7.10 new-object rows


@pytest.mark.parametrize("step,reason,hist,obj", [
    ("A2.mkdir", HR.OBJECT_RESERVATION_FAILED, H.NOT_RECORDED, None),
    ("A3.temp", HR.IDENTITY_TEMP_WRITE_FAILED, H.INCOMPLETE, O.D1),
    ("A3.replace", HR.IDENTITY_COMMIT_FAILED, H.INCOMPLETE, O.D1),
    ("S3.write", HR.STAGING_WRITE_FAILED, H.INCOMPLETE, O.D2),
    ("S4.mkdir", HR.REVISION_RESERVATION_FAILED, H.INCOMPLETE, O.D2),
    ("S5.write", HR.BLOB_WRITE_FAILED, H.INCOMPLETE, O.D2),
    ("S6.temp", HR.RECORD_TEMP_WRITE_FAILED, H.INCOMPLETE, O.D2),
])
def test_new_object_failure_rows(root, monkeypatch, step, reason, hist, obj):
    fail_at(monkeypatch, step)
    _, out = save_flow(root)
    assert out.canonical is C.DONE and (out.adoption, out.history, out.history_reason) == (A.NOT_NEEDED, hist, reason)
    assert [o.state for o in h.scan_objects(root, ST)] == ([obj] if obj else [])
    assert state(root).state is S.UNTRACKED


# --------------------------------------------------------------------------- §7.10 save rows


def test_s1_staging_failures(root, monkeypatch):
    tracked_at_1(root)
    fail_at(monkeypatch, "S3.write")
    _, out = save_flow(root)
    assert (out.history, out.history_reason, out.history_errno) == (H.NOT_RECORDED, HR.STAGING_WRITE_FAILED, errno.EIO)
    assert out.canonical is C.DONE


def test_s1_staging_readback_failure(root, monkeypatch):
    tracked_at_1(root)
    fail_at(monkeypatch, "S3.read")
    _, out = save_flow(root)
    assert (out.history, out.history_reason) == (H.NOT_RECORDED, HR.STAGING_READBACK_FAILED)
    assert len(os.listdir(root / ".history/staging")) == 1  # "staging file remains"


def test_s2_reservation_failure_on_a_tracked_object_has_no_artefact(root, monkeypatch):
    tracked_at_1(root)
    fail_at(monkeypatch, "S4.mkdir")
    _, out = save_flow(root)
    assert (out.history, out.history_reason, out.seq) == (H.NOT_RECORDED, HR.REVISION_RESERVATION_FAILED, None)


def test_s2_reservation_collisions(root, monkeypatch):
    tracked_at_1(root)
    calls = []

    def fault(step):
        if step == "S4.mkdir":
            calls.append(step)
            raise FileExistsError(errno.EEXIST, "exists")

    monkeypatch.setattr(h, "_fault", fault)
    _, out = save_flow(root)
    assert out.history_reason is HR.REVISION_RESERVATION_COLLISIONS_EXHAUSTED and out.history_errno is None and len(calls) == 100


def test_collision_takes_the_next_number(root, monkeypatch):
    tracked_at_1(root)
    (objdir(root) / "revisions" / seq(2)).parent.mkdir(exist_ok=True)

    def fault(step):
        if step == "S4.mkdir" and not (objdir(root) / "revisions" / seq(2)).exists():
            (objdir(root) / "revisions" / seq(2)).mkdir()  # another writer took it

    monkeypatch.setattr(h, "_fault", fault)
    _, out = save_flow(root)
    assert out.seq == seq(3) and out.complete


def test_sequence_exhausted(root):
    make_identity(root)
    add_revision(root, 1, b"old")
    rev = objdir(root) / "revisions"
    for n in range(2, 4):
        (rev / seq(n)).mkdir()
    canonical(root, b"old")
    # the contiguity rule forbids a real 99999999 folder here; the limit is tested on the allocator
    with pytest.raises(h.StepFailed) as e:
        h._next_number(99999999)
    assert e.value.reason is HR.SEQUENCE_EXHAUSTED


@pytest.mark.parametrize("step,reason", [("S5.write", HR.BLOB_WRITE_FAILED), ("S5.fsync", HR.BLOB_WRITE_FAILED),
                                         ("S5.verify", HR.BLOB_VERIFY_FAILED)])
def test_s2_blob_failures_leave_incomplete(root, monkeypatch, step, reason):
    tracked_at_1(root)
    fail_at(monkeypatch, step)
    _, out = save_flow(root)
    assert (out.history, out.history_reason, out.seq) == (H.INCOMPLETE, reason, seq(2))
    assert out.canonical is C.DONE
    assert only_object(root).state is O.D3 and only_object(root).head.seq == seq(1)
    assert state(root).state is S.CHANGED_OUTSIDE  # S-7/S-2: the file moved on, history did not


@pytest.mark.parametrize("step,reason", [("S6.temp", HR.RECORD_TEMP_WRITE_FAILED), ("S6.fsync", HR.RECORD_TEMP_WRITE_FAILED),
                                         ("S6.replace", HR.RECORD_COMMIT_FAILED)])
def test_s3_record_failures_leave_incomplete(root, monkeypatch, step, reason):
    tracked_at_1(root)
    fail_at(monkeypatch, step)
    _, out = save_flow(root)
    assert (out.history, out.history_reason, out.seq) == (H.INCOMPLETE, reason, seq(2))
    assert only_object(root).head.seq == seq(1)


def test_record_durability_failure_is_committed_and_never_complete(root, monkeypatch):
    tracked_at_1(root)
    fail_at(monkeypatch, "S6.dirfsync")
    _, out = save_flow(root)
    assert (out.history, out.history_reason, out.history_errno, out.seq) == (H.RECORDED, HR.RECORD_DURABILITY_FAILED, errno.EIO, seq(2))
    assert not out.complete and out.history_committed
    assert state(root).seq == seq(2)


def raises_after(root, write=None):
    def run():
        if write is not None:
            (root / P).write_bytes(write)
        return OSError(errno.EIO, "disk")

    return run


def test_c1_canonical_failed_prior_bytes(root):
    tracked_at_1(root)
    hr, out = save_flow(root, fail_canon=raises_after(root))
    assert (out.canonical, out.canonical_reason, out.readback, out.history, out.history_reason) == (
        C.NOT_DONE, CR.CANONICAL_STEP_FAILED, R.READ, H.RECORDED_NOT_CURRENT, HR.CANONICAL_STEP_FAILED)
    assert out.canonical_error == "[Errno 5] disk" or "disk" in out.canonical_error
    r = state(root)
    assert (r.state, r.seq, r.history_ahead) == (S.TRACKED, seq(1), True)


def test_c1_for_a_new_file_prior_absence(root):
    hr, out = save_flow(root, fail_canon=raises_after(root))
    assert (out.canonical, out.readback, out.history) == (C.NOT_DONE, R.ABSENT, H.RECORDED_NOT_CURRENT)


def test_c1_real_b2a_abandon_keeps_the_committed_record(root):
    tracked_at_1(root)
    refusal = libmod.LibraryLocked("an upgrade started while saving; nothing was saved", locks.Refusal.BUSY)

    def refuse():
        raise refusal

    hr = h.phase_h_write(root, ST, P, TEXT)
    token = libmod._FINAL_CHECK.set(refuse)
    try:
        with pytest.raises(libmod.LibraryLocked) as e:
            canon_write(root, P, TEXT)
    finally:
        libmod._FINAL_CHECK.reset(token)
    out = h.conclude(hr, "save", e.value)
    assert out.canonical is C.NOT_DONE and out.history is H.RECORDED_NOT_CURRENT
    assert out.canonical_error == str(refusal)


def test_c2a_raised_and_readback_failed(root, monkeypatch):
    tracked_at_1(root)
    hr = h.phase_h_write(root, ST, P, TEXT)
    fail_at(monkeypatch, "V.read")
    out = h.conclude(hr, "save", OSError(errno.EIO, "disk"))
    assert (out.canonical, out.readback, out.readback_errno, out.history, out.history_reason, out.history_errno) == (
        C.UNCERTAIN, R.FAILED, errno.EIO, H.RECORDED_UNVERIFIED, HR.CANONICAL_READBACK_FAILED, errno.EIO)


def test_c2b_raised_and_other_bytes(root):
    tracked_at_1(root)
    _, out = save_flow(root, fail_canon=raises_after(root, b"someone else"))
    assert (out.canonical, out.readback, out.history, out.history_reason) == (
        C.UNCERTAIN, R.READ, H.RECORDED_NOT_CURRENT, HR.CANONICAL_STEP_FAILED)


def test_c2c_raised_and_target_bytes(root):
    tracked_at_1(root)

    def run():
        canon_write(root, P, TEXT)
        return OSError(errno.EIO, "after")

    _, out = save_flow(root, fail_canon=run)
    assert (out.canonical, out.readback, out.history, out.history_reason) == (
        C.UNCERTAIN, R.READ, H.RECORDED, HR.CANONICAL_STEP_FAILED)
    assert not out.complete


def test_uncertain_absent_after_raise_on_a_prior_file(root):
    tracked_at_1(root)

    def run():
        (root / P).unlink()
        return OSError(errno.EIO, "gone")

    _, out = save_flow(root, fail_canon=run)
    assert (out.canonical, out.readback, out.history) == (C.UNCERTAIN, R.ABSENT, H.RECORDED_NOT_CURRENT)


def test_s6_returned_and_differs(root):
    tracked_at_1(root)
    hr = h.phase_h_write(root, ST, P, TEXT)
    canon_write(root, P, TEXT)
    (root / P).write_bytes(b"concurrent writer")
    out = h.conclude(hr, "save", None)
    assert (out.canonical, out.history, out.history_reason) == (C.DONE, H.RECORDED_NOT_CURRENT, HR.CANONICAL_MISMATCH)


def test_s7_h_failed_save_done(root, monkeypatch):
    tracked_at_1(root)
    fail_at(monkeypatch, "S3.write")
    save_flow(root)
    assert state(root).state is S.CHANGED_OUTSIDE


def test_s8_returned_and_readback_failed(root, monkeypatch):
    tracked_at_1(root)
    hr = h.phase_h_write(root, ST, P, TEXT)
    canon_write(root, P, TEXT)
    fail_at(monkeypatch, "V.read")
    out = h.conclude(hr, "save", None)
    assert (out.canonical, out.readback, out.history, out.history_reason) == (
        C.DONE, R.FAILED, H.RECORDED_UNVERIFIED, HR.CANONICAL_READBACK_FAILED)


def test_s9_staging_unlink_failure_is_an_observation_only(root, monkeypatch):
    tracked_at_1(root)
    hr = h.phase_h_write(root, ST, P, TEXT)
    canon_write(root, P, TEXT)
    fail_at(monkeypatch, "S9.unlink")
    out = h.conclude(hr, "save", None)
    assert out.complete and hr.observations


def test_h_failure_precedence_is_the_first_failure(root, monkeypatch):
    tracked_at_1(root)
    fail_at(monkeypatch, "S5.write")
    _, out = save_flow(root, fail_canon=raises_after(root))
    assert (out.canonical, out.history, out.history_reason) == (C.NOT_DONE, H.INCOMPLETE, HR.BLOB_WRITE_FAILED)


def test_interrupts_are_not_caught(root, monkeypatch):
    tracked_at_1(root)
    fail_at(monkeypatch, "S5.write", exc=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        h.phase_h_write(root, ST, P, TEXT)


def test_history_root_unavailable_is_reported_and_the_save_goes_on(root, monkeypatch):
    fail_at(monkeypatch, "root.mkdir")
    hr, out = save_flow(root)
    assert (out.canonical, out.history, out.history_reason) == (C.DONE, H.NOT_RECORDED, HR.HISTORY_ROOT_UNAVAILABLE)


def test_refused_in_p_outcome_shape():
    out = h.refused_outcome("save", P, Exception("nope"))
    assert (out.canonical, out.canonical_reason, out.readback, out.history, out.adoption) == (
        C.NOT_DONE, CR.PRECONDITION_REFUSED, R.NOT_PERFORMED, H.NOT_APPLICABLE, A.NOT_NEEDED)
    assert out.canonical_error == "nope" and out.seq is None and out.object_id is None


# --------------------------------------------------------------------------- rename and delete rows


def rename_flow(root, act):
    tracked_at_1(root)
    hr = h.phase_h_rename(root, ST, P, Q, TEXT)
    exc = act()
    return h.conclude(hr, "rename", exc)


def test_r1_rename_failed_before_q(root):
    out = rename_flow(root, lambda: OSError(errno.EIO, "x"))
    assert (out.canonical, out.readback, out.previous_path_readback, out.history) == (
        C.NOT_DONE, R.ABSENT, R.READ, H.RECORDED_NOT_CURRENT)
    assert state(root, Q).state is S.MISSING and state(root, P).state is S.UNTRACKED


def test_r3_unlink_failed_and_q_removed(root):
    def act():
        canon_write(root, Q, TEXT)
        (root / Q).unlink()
        return OSError(errno.EPERM, "unlink")

    out = rename_flow(root, act)
    assert out.canonical is C.NOT_DONE and out.history is H.RECORDED_NOT_CURRENT


def test_r4_unlink_failed_and_q_stays(root):
    def act():
        canon_write(root, Q, TEXT)
        return OSError(errno.EPERM, "unlink")

    out = rename_flow(root, act)
    assert (out.canonical, out.readback, out.history, out.history_reason) == (
        C.UNCERTAIN, R.READ, H.RECORDED, HR.CANONICAL_STEP_FAILED)


def test_r5_returned(root):
    def act():
        canon_write(root, Q, TEXT)
        (root / P).unlink()

    out = rename_flow(root, act)
    assert out.complete and state(root, Q).seq == seq(2)


def test_rename_to_the_same_file(root):
    tracked_at_1(root)
    hr = h.phase_h_rename(root, ST, P, P, TEXT)
    canon_write(root, P, TEXT)
    out = h.conclude(hr, "rename", None)
    assert out.complete and out.previous_path_readback is R.READ


def test_d1_delete_unlink_raised_file_present(root):
    tracked_at_1(root)
    _, out = delete_flow(root, fail=lambda: OSError(errno.EPERM, "x"))
    assert (out.canonical, out.readback, out.history, out.history_reason) == (
        C.NOT_DONE, R.READ, H.RECORDED_NOT_CURRENT, HR.CANONICAL_STEP_FAILED)
    assert state(root).state is S.TOMBSTONED_PRESENT


def test_d3_delete_unlink_raised_and_readback_failed(root, monkeypatch):
    tracked_at_1(root)
    hr = h.phase_h_delete(root, ST, P)
    fail_at(monkeypatch, "V.read")
    out = h.conclude(hr, "delete", OSError(errno.EIO, "x"))
    assert (out.canonical, out.readback, out.history, out.history_reason) == (
        C.UNCERTAIN, R.FAILED, H.RECORDED_UNVERIFIED, HR.CANONICAL_READBACK_FAILED)


def test_delete_raised_but_file_is_gone(root):
    tracked_at_1(root)

    def act():
        (root / P).unlink()
        return OSError(errno.EIO, "x")

    _, out = delete_flow(root, fail=act)
    assert (out.canonical, out.readback, out.history) == (C.UNCERTAIN, R.ABSENT, H.RECORDED)


@pytest.mark.parametrize("step,reason", [("D6.temp", HR.RECORD_TEMP_WRITE_FAILED), ("D6.replace", HR.RECORD_COMMIT_FAILED)])
def test_delete_record_failures(root, monkeypatch, step, reason):
    tracked_at_1(root)
    fail_at(monkeypatch, step)
    _, out = delete_flow(root)
    assert (out.canonical, out.history, out.history_reason) == (C.DONE, H.INCOMPLETE, reason)


# --------------------------------------------------------------------------- binding requirements 1 to 4


def test_staging_clears_the_final_check_and_restores_it(root):
    canonical(root, b"old")
    calls = []

    def refuse():
        calls.append("called")
        raise libmod.LibraryLocked("refused", locks.Refusal.BUSY)

    token = libmod._FINAL_CHECK.set(refuse)
    try:
        hr = h.phase_h_write(root, ST, P, TEXT)  # would raise LibraryLocked if staging ran the check
        assert libmod._FINAL_CHECK.get() is refuse
    finally:
        libmod._FINAL_CHECK.reset(token)
    assert calls == [] and hr.committed and hr.reason is None


def test_final_check_is_restored_when_staging_raises(root, monkeypatch):
    tracked_at_1(root)
    sentinel = lambda: None  # noqa: E731
    token = libmod._FINAL_CHECK.set(sentinel)
    seen = []

    def refuses(path, text):
        seen.append(libmod._FINAL_CHECK.get())
        raise OSError(errno.EIO, "x")

    monkeypatch.setattr(libmod, "_atomic_write", refuses)
    try:
        hr = h.phase_h_write(root, ST, P, TEXT)
        assert hr.reason is HR.STAGING_WRITE_FAILED
        assert seen == [None]  # cleared for the routine
        assert libmod._FINAL_CHECK.get() is sentinel  # and restored although it raised
    finally:
        libmod._FINAL_CHECK.reset(token)


def test_the_check_is_not_run_at_any_history_step(root):
    make_object(root, [b"old"])
    canonical(root, b"old")
    seen = []
    token = libmod._FINAL_CHECK.set(lambda: seen.append(1))
    try:
        h.phase_h_write(root, ST, P, TEXT)
        assert seen == []
        canon_write(root, P, TEXT)
        assert seen == [1]  # exactly once, in the canonical step
    finally:
        libmod._FINAL_CHECK.reset(token)


def test_staged_bytes_are_read_from_the_file(root, monkeypatch):
    """Point the routine at a writer that produces other bytes: the blob follows the file, not the text."""
    tracked_at_1(root)

    def writer(path, text):
        Path(path).write_bytes(b"what the file really holds")

    monkeypatch.setattr(h, "_staging_write", writer)
    hr = h.phase_h_write(root, ST, P, TEXT)
    assert hr.committed
    assert (objdir(root) / "revisions" / seq(2) / "content").read_bytes() == b"what the file really holds"


def test_staging_uses_the_unchanged_canonical_routine():
    assert h._canonical_routine() is libmod._atomic_write


def test_atomic_write_is_byte_identical_to_the_accepted_source():
    import ast
    tree_ = ast.parse(Path(libmod.__file__).read_text(encoding="utf-8"))
    fn = next(n for n in tree_.body if isinstance(n, ast.FunctionDef) and n.name == "_atomic_write")
    src = "\n".join(Path(libmod.__file__).read_text(encoding="utf-8").splitlines()[fn.lineno - 1:fn.end_lineno])
    assert hashlib.sha256(src.encode()).hexdigest() == ATOMIC_WRITE_SHA


ATOMIC_WRITE_SHA = "8aede53d1e714fb1921c5c02880048c535eb0c0504923750d3a1fba3e59853ad"


def test_blob_record_and_identity_are_fsynced(root, monkeypatch):
    events = []
    real = h._fsync

    def traced(fd):
        events.append("fsync")
        real(fd)

    monkeypatch.setattr(h, "_fsync", traced)
    monkeypatch.setattr(h, "_fault", events.append)
    save_flow(root)
    for step in ("A3.fsync", "S5.fsync", "S6.fsync"):
        assert events[events.index(step) + 1] == "fsync", step


def test_blob_is_created_exclusively(root):
    rev = root / "rev"
    rev.mkdir()
    (rev / "content").write_bytes(b"first")
    with pytest.raises(h.StepFailed) as e:
        h._write_blob(rev, b"second", "S5")
    assert e.value.reason is HR.BLOB_WRITE_FAILED and e.value.errno == errno.EEXIST
    assert (rev / "content").read_bytes() == b"first"


def test_resolver_reports_incomplete_folders_and_staging_files(root):
    make_object(root, [b"1"])
    (objdir(root) / "revisions" / seq(2)).mkdir()
    (root / ".history/staging").mkdir()
    (root / ".history/staging/left.json").write_bytes(b"x")
    canonical(root, b"1")
    r = state(root)
    assert r.state is S.TRACKED and r.seq == seq(1)
    text = " ".join(r.observations)
    assert "incomplete revision 00000002" in text and "left.json" in text


# --------------------------------------------------------------------------- no reuse and no rewrite


def test_committed_revisions_are_never_rewritten(root):
    tracked_at_1(root)
    before = tree(objdir(root) / "revisions" / seq(1))
    identity = (objdir(root) / "identity.json").read_bytes()
    for t in ("a\n", "b\n", "c\n"):
        save_flow(root, t)
    assert tree(objdir(root) / "revisions" / seq(1)) == before
    assert (objdir(root) / "identity.json").read_bytes() == identity
    assert only_object(root).head.seq == seq(4)


def test_incomplete_numbers_are_never_reused(root, monkeypatch):
    tracked_at_1(root)
    fail_at(monkeypatch, "S5.write", times=1)
    _, first = save_flow(root)
    assert first.seq == seq(2) and first.history is H.INCOMPLETE
    monkeypatch.setattr(h, "_fault", None)
    canonical(root, b"old")  # back to a tracked state
    _, second = save_flow(root)
    assert second.seq == seq(3) and second.complete


def test_no_mutable_index_exists(root):
    tracked_at_1(root)
    save_flow(root)
    names = {p.name for p in (root / ".history").iterdir()}
    assert names <= {"objects", "jobs", "staging"}


# --------------------------------------------------------------------------- real process kills


KILL_STEPS = [
    "A2.mkdir", "A3.temp", "A3.fsync", "A3.replace", "A3.dirfsync", "A4.mkdir_revisions", "A4.mkdir", "A5.write", "A5.fsync",
    "A5.verify", "A6.temp", "A6.fsync", "A6.replace", "A6.dirfsync", "A7.read", "S3.write", "S3.read", "S4.mkdir",
    "S5.write", "S5.fsync", "S5.verify", "S6.temp", "S6.fsync", "S6.replace", "S6.dirfsync",
]

KILL_SCRIPT = textwrap.dedent("""
    import os, sys
    from lpbfstudio import history as h
    root, step = sys.argv[1], sys.argv[2]
    def fault(s):
        if s == step:
            os._exit(9)
    h._fault = fault
    from pathlib import Path
    h.phase_h_write(Path(root), h.ObjectType.EXPOSURE_SET, "sets/a.json", "new\\n")
    os._exit(0)
""")


@pytest.mark.parametrize("step", KILL_STEPS)
def test_a_kill_at_every_step_leaves_a_defined_state(root, step):
    canonical(root, b"old")
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(Path(libmod.__file__).parents[1]), os.environ.get("PYTHONPATH", "")]))
    p = subprocess.run([sys.executable, "-c", KILL_SCRIPT, str(root), step], env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode == 9, p.stderr
    for o in h.scan_objects(root, ST):
        assert o.state is not O.D5, o.detail  # §7.10: no row produces a D5
    r = state(root)
    assert r.state in (S.UNTRACKED, S.TRACKED)
    assert (root / P).read_bytes() == b"old"
    # and a later write still works
    _, out = save_flow(root)
    assert out.canonical is C.DONE and out.history in (H.RECORDED,)
