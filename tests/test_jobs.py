"""ABP-0.5: Build Job Manifest v0 (DC-001 v0.6 §9): drafts, finalization, pinning, validation, and every §9.3 reason."""

import errno
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from _history_support import seq, sha, tree
from lpbfstudio import history as h
from lpbfstudio import jobs as j
from lpbfstudio.defaults import default_sets
from lpbfstudio.history import ObjectType as T
from lpbfstudio.history import PathState as S
from lpbfstudio.jobs import JobStatus as St
from lpbfstudio.jobs import ManifestResult as MR
from lpbfstudio.library import Library
from lpbfstudio.models import MachineProfile, ProcessSettings

MACHINE = MachineProfile().name
AHEAD = "a higher committed revision exists for this object that does not match the current file; it was not pinned"
SRC = str(Path(j.__file__).resolve().parents[1])


@pytest.fixture
def lib(tmp_path):
    library = Library(tmp_path / "lib")
    library.copy(default_sets()[0].name, "P1")
    library.save_process(ProcessSettings())
    return library


def draft(lib, job_id=None, sets=None, machine=MACHINE, material="Al"):
    return j.save_draft(lib, job_id, sets if sets is not None else [{"name": "P1", "role": "part", "z_range": None}], machine, material)


def ready(lib):
    out = draft(lib)
    assert out.status is St.DRAFT_SAVED
    return out.build_job_id


def job_dir(lib, job_id):
    return lib.root / ".history/jobs" / job_id


def manifest(lib, job_id):
    return json.loads((job_dir(lib, job_id) / "final/manifest.json").read_text(encoding="utf-8"))


def fail_at(monkeypatch, step, exc=None):
    def fault(s):
        if s == step:
            raise (exc or OSError(errno.EIO, "injected"))

    monkeypatch.setattr(h, "_fault", fault)


def edit(lib, text="changed"):
    es = lib.get("P1")
    es.description = text
    lib.save(es)


# --------------------------------------------------------------------------- drafts


def test_draft_creates_a_job_and_is_last_writer_wins(lib):
    jid = ready(lib)
    assert h._ID.match(jid)
    assert draft(lib, jid, material="Ti").status is St.DRAFT_SAVED
    doc = json.loads((job_dir(lib, jid) / "draft.json").read_text(encoding="utf-8"))
    assert doc["material"] == "Ti" and doc["build_job_id"] == jid


@pytest.mark.parametrize("sets", [[], [{"name": "P1", "role": "other", "z_range": None}], [{"name": "P1", "role": "part", "z_range": [2, 1]}],
                                  [{"name": "P1", "role": "part", "z_range": [1]}], [{"role": "part", "z_range": None}]])
def test_invalid_drafts_are_refused(lib, sets):
    out = draft(lib, sets=sets)
    assert (out.status, out.reason) == (St.DRAFT_NOT_SAVED, "draft_invalid") and not (lib.root / ".history/jobs").exists()


def test_draft_for_an_unknown_job_is_refused(lib):
    assert draft(lib, "9" * 32).reason == "job_not_found"


@pytest.mark.parametrize("step,reason", [("J.mkdir", "job_reservation_failed"), ("J1.temp", "draft_temp_write_failed"),
                                         ("J1.fsync", "draft_temp_write_failed"), ("J1.replace", "draft_commit_failed")])
def test_draft_failures(lib, monkeypatch, step, reason):
    fail_at(monkeypatch, step)
    out = draft(lib)
    assert (out.status, out.reason, out.errno) == (St.DRAFT_NOT_SAVED, reason, errno.EIO)


def test_job_reservation_collisions(lib, monkeypatch):
    fail_at(monkeypatch, "J.mkdir", FileExistsError(errno.EEXIST, "x"))
    out = draft(lib)
    assert (out.status, out.reason, out.errno) == (St.DRAFT_NOT_SAVED, "job_reservation_collisions_exhausted", None)


def test_a_draft_after_final_exists_is_refused(lib):
    jid = ready(lib)
    (job_dir(lib, jid) / "final").mkdir()
    before = (job_dir(lib, jid) / "draft.json").read_bytes()
    assert (draft(lib, jid).reason, draft(lib, jid).status) == ("job_finalized", St.DRAFT_NOT_SAVED)
    assert (job_dir(lib, jid) / "draft.json").read_bytes() == before


# --------------------------------------------------------------------------- finalization


def test_finalize_pins_every_input(lib):
    jid = ready(lib)
    out = j.finalize(lib, jid)
    assert out.status is St.FINALIZED and out.reason is None and out.warnings == () and out.refused_inputs == ()
    doc = manifest(lib, jid)
    assert doc["manifest_version"] == 1 and doc["build_job_id"] == jid
    assert doc["material"] == {"value": "Al", "pinned": False} and set(doc["software"]) == {"version", "git_commit"}
    assert set(doc["platform"]) == {"os", "architecture", "python_version"}
    kinds = [i["object_type"] for i in doc["inputs"]]
    assert kinds == ["exposure_set", "machine_profile", "process_settings"]
    es = doc["inputs"][0]
    r = h.resolve(lib.root, T.EXPOSURE_SET, "sets/P1.json")
    raw = (lib.sets_dir / "P1.json").read_bytes()
    assert (es["object_id"], es["seq"], es["content_hash"], es["byte_length"], es["role"], es["z_range"]) == (
        r.object_id, r.seq, {"algorithm": "sha256", "value": sha(raw)}, len(raw), "part", None)
    assert doc["inputs"][1]["resolved_from_machine_name"] == MACHINE


def test_pinned_revisions_ignore_later_saves(lib):
    jid = ready(lib)
    j.finalize(lib, jid)
    edit(lib)
    assert j.validate(lib, jid).result is MR.VALID


def test_history_ahead_pins_the_matching_revision_with_the_exact_warning(lib):
    before = (lib.sets_dir / "P1.json").read_bytes()
    edit(lib)
    (lib.sets_dir / "P1.json").write_bytes(before)
    jid = ready(lib)
    out = j.finalize(lib, jid)
    assert out.status is St.FINALIZED and out.warnings == (AHEAD,)
    assert manifest(lib, jid)["inputs"][0]["seq"] == seq(1)


def refused_state(lib, state):
    jid = ready(lib)
    before = tree(job_dir(lib, jid))
    out = j.finalize(lib, jid)
    assert (out.status, out.reason) == (St.NOT_FINALIZED, "input_not_pinnable")
    assert any(i["state"] == state for i in out.refused_inputs), out.refused_inputs
    assert tree(job_dir(lib, jid)) == before  # no final/


def test_untracked_is_refused(lib):
    lib.sets_dir.joinpath("U.json").write_text(lib.get("P1").model_copy(update={"name": "U"}).model_dump_json(indent=3), encoding="utf-8")
    jid = draft(lib, sets=[{"name": "U", "role": "part", "z_range": None}]).build_job_id
    out = j.finalize(lib, jid)
    assert out.reason == "input_not_pinnable" and out.refused_inputs[0]["state"] == "untracked"


def test_changed_outside_history_is_refused(lib):
    (lib.sets_dir / "P1.json").write_bytes((lib.sets_dir / "P1.json").read_bytes() + b" ")
    refused_state(lib, "changed_outside_history")


def test_damage_is_refused(lib):
    r = h.resolve(lib.root, T.EXPOSURE_SET, "sets/P1.json")
    (lib.root / ".history/objects/exposure_set" / r.object_id / "junk").write_bytes(b"")
    refused_state(lib, "unknown")


def test_unknown_set_name_is_refused(lib):
    jid = draft(lib, sets=[{"name": "nope", "role": "part", "z_range": None}]).build_job_id
    assert j.finalize(lib, jid).refused_inputs[0]["state"] == "none"


def test_unknown_machine_is_refused(lib):
    jid = draft(lib, machine="no such machine").build_job_id
    assert j.finalize(lib, jid).refused_inputs[0]["state"] == "none"


def test_untracked_process_is_refused(lib):
    (lib.root / "process.json").write_bytes(b"{}")
    refused_state(lib, "changed_outside_history")


def test_every_refused_input_is_listed(lib):
    (lib.sets_dir / "P1.json").write_bytes((lib.sets_dir / "P1.json").read_bytes() + b" ")
    (lib.root / "process.json").write_bytes(b"{}")
    out = j.finalize(lib, ready(lib))
    assert len(out.refused_inputs) == 2


def test_a_blob_that_fails_its_re_verification_refuses(lib, monkeypatch):
    jid = ready(lib)
    monkeypatch.setattr(j, "_read_blob", lambda *a: b"other")
    assert j.finalize(lib, jid).refused_inputs[0]["state"] == "unverifiable"


def test_final_exists_is_single_winner(lib):
    jid = ready(lib)
    assert j.finalize(lib, jid).status is St.FINALIZED
    before = tree(job_dir(lib, jid))
    out = j.finalize(lib, jid)
    assert (out.status, out.reason) == (St.NOT_FINALIZED, "final_exists") and tree(job_dir(lib, jid)) == before


def test_final_found_at_mkdir_loses_the_race_and_changes_nothing(lib, monkeypatch):
    jid = ready(lib)

    def other_writer(s):
        if s == "J3.mkdir":
            (job_dir(lib, jid) / "final").mkdir()
            (job_dir(lib, jid) / "final/manifest.json").write_bytes(b"the winner")

    monkeypatch.setattr(h, "_fault", other_writer)
    assert j.finalize(lib, jid).reason == "final_exists"
    assert (job_dir(lib, jid) / "final/manifest.json").read_bytes() == b"the winner"


def test_final_reservation_failure(lib, monkeypatch):
    jid = ready(lib)
    fail_at(monkeypatch, "J3.mkdir")
    assert (j.finalize(lib, jid).reason, j.finalize.__name__) == ("final_reservation_failed", "finalize")


@pytest.mark.parametrize("step,reason", [("J4.temp", "final_temp_write_failed"), ("J4.fsync", "final_temp_write_failed"), ("J4.replace", "final_commit_failed")])
def test_incomplete_finalization_is_never_completed(lib, monkeypatch, step, reason):
    jid = ready(lib)
    fail_at(monkeypatch, step)
    out = j.finalize(lib, jid)
    assert (out.status, out.reason) == (St.FINALIZATION_INCOMPLETE, reason)
    monkeypatch.setattr(h, "_fault", None)
    assert j.job_state(lib, jid) == "finalization_incomplete"
    assert j.finalize(lib, jid).reason == "final_exists"  # J-2 stays as it is (MAN-2)
    assert j.validate(lib, jid).result is MR.NOT_FINALIZED


def test_read_back_failure(lib, monkeypatch):
    jid = ready(lib)
    fail_at(monkeypatch, "J5.read")
    out = j.finalize(lib, jid)
    assert (out.status, out.reason) == (St.FINALIZED_UNVERIFIED, "final_readback_failed")
    assert (job_dir(lib, jid) / "final/manifest.json").exists()


def test_read_back_mismatch(lib, monkeypatch):
    jid = ready(lib)

    def corrupt(s):
        if s == "J5.read":
            (job_dir(lib, jid) / "final/manifest.json").write_bytes(b"{}")

    monkeypatch.setattr(h, "_fault", corrupt)
    assert j.finalize(lib, jid).status is St.FINALIZED_UNVERIFIED


def test_read_back_of_another_valid_manifest_is_unverified(lib, monkeypatch):
    jid = ready(lib)

    def swap(s):
        if s == "J5.read":
            p = job_dir(lib, jid) / "final/manifest.json"
            doc = json.loads(p.read_text(encoding="utf-8"))
            doc["inputs"] = []
            p.write_text(json.dumps(doc), encoding="utf-8")

    monkeypatch.setattr(h, "_fault", swap)
    assert j.finalize(lib, jid).status is St.FINALIZED_UNVERIFIED


def test_durability_failure_is_not_called_finalized(lib, monkeypatch):
    jid = ready(lib)
    fail_at(monkeypatch, "J4.dirfsync")
    out = j.finalize(lib, jid)
    assert out.status is St.FINALIZED_UNVERIFIED


def test_finalize_of_a_missing_job(lib):
    assert (j.finalize(lib, "9" * 32).status, j.finalize(lib, "9" * 32).reason) == (St.NOT_FINALIZED, "job_not_found")


def test_finalize_refuses_a_bad_id(lib):
    assert j.finalize(lib, "../x").reason == "job_not_found"


def test_finalize_with_no_draft_is_refused(lib):
    jid = ready(lib)
    os.unlink(job_dir(lib, jid) / "draft.json")
    assert j.job_state(lib, jid) == "reservation"
    assert j.finalize(lib, jid).reason == "draft_unreadable"


def test_finalize_with_a_corrupt_draft_is_refused(lib):
    jid = ready(lib)
    (job_dir(lib, jid) / "draft.json").write_bytes(b"{")
    assert j.finalize(lib, jid).reason == "draft_unreadable"


# --------------------------------------------------------------------------- job states


def test_states(lib):
    jid = ready(lib)
    assert j.job_state(lib, jid) == "draft"
    j.finalize(lib, jid)
    assert j.job_state(lib, jid) == "finalized"


def test_reservation_with_a_temporary_file(lib, monkeypatch):
    fail_at(monkeypatch, "J1.replace")
    jid = draft(lib).build_job_id
    assert j.job_state(lib, jid) == "reservation"


@pytest.mark.parametrize("name", ["stray", "draft.json.bak"])
def test_unknown_entry_is_damage_and_refuses(lib, name):
    jid = ready(lib)
    (job_dir(lib, jid) / name).write_bytes(b"")
    assert j.job_state(lib, jid) == "damaged"
    assert (j.finalize(lib, jid).status, j.finalize(lib, jid).reason) == (St.NOT_FINALIZED, "job_damaged")
    assert draft(lib, jid).reason == "job_damaged"


def test_a_folder_named_draft_is_damage(lib):
    jid = ready(lib)
    os.unlink(job_dir(lib, jid) / "draft.json")
    (job_dir(lib, jid) / "draft.json").mkdir()
    assert j.job_state(lib, jid) == "damaged"


def test_unknown_entry_in_final_is_damage(lib):
    jid = ready(lib)
    j.finalize(lib, jid)
    (job_dir(lib, jid) / "final/x").write_bytes(b"")
    assert j.job_state(lib, jid) == "damaged"


def test_job_states_never_affect_resolution(lib):
    jid = ready(lib)
    (job_dir(lib, jid) / "stray").write_bytes(b"")
    assert h.resolve(lib.root, T.EXPOSURE_SET, "sets/P1.json").state is S.TRACKED


def test_job_operations_never_touch_objects_or_canonical_files(lib):
    before_objects = tree(lib.root / ".history/objects")
    before_sets = tree(lib.sets_dir)
    j.finalize(lib, ready(lib))
    assert tree(lib.root / ".history/objects") == before_objects and tree(lib.sets_dir) == before_sets


# --------------------------------------------------------------------------- validation


def finalized(lib):
    jid = ready(lib)
    assert j.finalize(lib, jid).status is St.FINALIZED
    return jid


def revision(lib, doc_input):
    return lib.root / ".history/objects" / doc_input["object_type"] / doc_input["object_id"] / "revisions" / doc_input["seq"]


def test_valid(lib):
    report = j.validate(lib, finalized(lib))
    assert report.result is MR.VALID and all(i.result is MR.VALID for i in report.inputs)


def test_missing_revision(lib):
    jid = finalized(lib)
    i = manifest(lib, jid)["inputs"][0]
    (revision(lib, i) / "content").unlink()
    assert j.validate(lib, jid).result is MR.MISSING_REVISION


def test_hash_or_length_mismatch(lib):
    jid = finalized(lib)
    i = manifest(lib, jid)["inputs"][0]
    (revision(lib, i) / "content").write_bytes(b"tampered")
    assert j.validate(lib, jid).result is MR.HASH_OR_LENGTH_MISMATCH


def test_damaged_object(lib):
    jid = finalized(lib)
    i = manifest(lib, jid)["inputs"][0]
    (revision(lib, i).parents[1] / "junk").write_bytes(b"")
    assert j.validate(lib, jid).result is MR.DAMAGED_OBJECT


def test_unsupported_version(lib):
    jid = finalized(lib)
    p = job_dir(lib, jid) / "final/manifest.json"
    doc = json.loads(p.read_text(encoding="utf-8"))
    doc["manifest_version"] = 2
    p.write_text(json.dumps(doc), encoding="utf-8")
    assert j.validate(lib, jid).result is MR.UNSUPPORTED_VERSION


def test_unverifiable(lib, monkeypatch):
    jid = finalized(lib)

    def boom(*a):
        raise h.Unverifiable("x", errno.EIO)

    monkeypatch.setattr(h, "scan_objects", boom)
    assert j.validate(lib, jid).result is MR.UNVERIFIABLE


def test_a_malformed_manifest_is_unverifiable(lib):
    jid = finalized(lib)
    (job_dir(lib, jid) / "final/manifest.json").write_bytes(b"{")
    assert j.validate(lib, jid).result is MR.UNVERIFIABLE


def test_validation_never_substitutes_another_revision(lib):
    jid = finalized(lib)
    edit(lib)
    i = manifest(lib, jid)["inputs"][0]
    (revision(lib, i) / "content").unlink()  # a later revision exists; it must not stand in
    assert j.validate(lib, jid).result is MR.MISSING_REVISION


def test_validation_reads_only(lib):
    jid = finalized(lib)
    before = tree(lib.root)
    j.validate(lib, jid)
    assert tree(lib.root) == before


# --------------------------------------------------------------------------- kills


KILL = textwrap.dedent("""
    import os, sys
    from lpbfstudio import history as h, jobs as j, library as L
    lib = L.Library(sys.argv[1], seed=False)
    def fault(s):
        if s == sys.argv[3]:
            os._exit(9)
    h._fault = fault
    j.finalize(lib, sys.argv[2])
    os._exit(0)
""")


@pytest.mark.parametrize("step,state", [("J3.mkdir", "draft"), ("J4.temp", "finalization_incomplete"), ("J4.fsync", "finalization_incomplete"),
                                        ("J4.replace", "finalization_incomplete"), ("J5.read", "finalized")])
def test_a_kill_during_finalization_leaves_a_defined_state(lib, step, state):
    jid = ready(lib)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([SRC, os.environ.get("PYTHONPATH", "")]))
    p = subprocess.run([sys.executable, "-c", KILL, str(lib.root), jid, step], env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode == 9, p.stderr
    assert j.job_state(lib, jid) == state
