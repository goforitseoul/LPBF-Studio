"""ABP-0.3: the set writers through ``Library``: phases P, H, C and V (DC-001 v0.6 §7), outcome delivery by the
caller-scoped collector (§7.11), and the promise that history never changes a canonical result (D-7).

Real libraries, on ext4 (``/tmp``) and tmpfs (``/dev/shm``) on Linux and on the local NTFS on Windows. Injected step
failures use ``history._fault``; the B2a re-check is observed through the real ``_final_check`` on Linux and through the
same context variable on Windows, where no lock sets it.
"""

import ast
import errno
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

from _history_support import seq, sha, tree
from lpbfstudio import doe as doemod
from lpbfstudio import history as h
from lpbfstudio import library as libmod
from lpbfstudio import locks
from lpbfstudio import outcomes as oc
from lpbfstudio import upgrade as up
from lpbfstudio.defaults import default_sets
from lpbfstudio.history import ObjectState as O
from lpbfstudio.history import ObjectType as T
from lpbfstudio.history import PathState as S
from lpbfstudio.library import Library, LibraryError, LibraryLocked
from lpbfstudio.outcomes import AdoptionStatus as A
from lpbfstudio.outcomes import CanonicalReason as CR
from lpbfstudio.outcomes import CanonicalStatus as C
from lpbfstudio.outcomes import HistoryReason as HR
from lpbfstudio.outcomes import HistoryStatus as H
from lpbfstudio.outcomes import Readback as R

try:
    import fcntl
except ImportError:
    fcntl = None

posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock behaviour; not executed on Windows")
ROOTS = {"ext4": "/tmp", "tmpfs": "/dev/shm"} if sys.platform != "win32" else {"ntfs": None}
DEFAULT = default_sets()[0].name
ABANDONED = "an upgrade started while saving; nothing was saved"
M = up.MIGRATION_DIR
SRC = str(Path(libmod.__file__).resolve().parents[1])


@pytest.fixture(params=sorted(ROOTS))
def root(request):
    d = Path(tempfile.mkdtemp(prefix="histlib-", dir=ROOTS[request.param]))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def lib(root):
    """A seeded library with an unlocked, tracked set ``P1``."""
    library = Library(root / "lib")
    library.copy(DEFAULT, "P1")
    return library


def new_set(name, **kw):
    es = default_sets()[0].model_copy(deep=True)
    es.name, es.locked, es.description = name, False, "test set"
    for k, v in kw.items():
        setattr(es, k, v)
    return es


def resolved(lib, name):
    return h.resolve(lib.root, T.EXPOSURE_SET, f"sets/{name}.json")


def objects(lib):
    return {o.object_id: o for o in h.scan_objects(lib.root, T.EXPOSURE_SET)}


def head_of(lib, name):
    r = resolved(lib, name)
    assert r.claimant, r
    return objects(lib)[r.claimant].head


def fail_at(monkeypatch, step, exc=None):
    def fault(s):
        if s == step:
            raise (exc or OSError(errno.EIO, "injected"))

    monkeypatch.setattr(h, "_fault", fault)


def hist(lib):
    d = lib.root / ".history"
    return tree(d) if d.exists() else {}


# --------------------------------------------------------------------------- each writer


def test_save_of_a_new_set(lib):
    with lib.collect_outcomes() as sink:
        assert lib.save(new_set("N1")) is None
    o = sink.last
    assert len(sink) == 1 and o.complete and (o.operation, o.path, o.seq) == ("save", "sets/N1.json", seq(1))
    assert head_of(lib, "N1").operation == "created"


def test_first_save_of_a_placed_file_adopts_its_exact_bytes(lib):
    f = lib.sets_dir / "U1.json"
    placed = json.dumps(json.loads(new_set("U1").model_dump_json()), indent=5).encode()
    f.write_bytes(placed)
    with lib.collect_outcomes() as sink:
        lib.save(new_set("U1", description="edited"))
    o = sink.last
    assert o.complete and (o.adoption, o.adopted_seq, o.seq) == (A.COMMITTED, seq(1), seq(2))
    blob = lib.root / ".history/objects/exposure_set" / o.object_id / "revisions" / seq(1) / "content"
    assert blob.read_bytes() == placed


def test_save_of_a_tracked_set_appends(lib):
    before = resolved(lib, "P1")
    es = lib.get("P1")
    es.description = "changed"
    with lib.collect_outcomes() as sink:
        lib.save(es)
    assert sink.last.complete and sink.last.object_id == before.object_id and sink.last.seq == seq(int(before.seq) + 1)
    assert head_of(lib, "P1").source == {"object_id": before.object_id, "seq": before.seq}


def test_a_new_locked_set_records_the_lock_change(lib):
    lib.save(new_set("L1", locked=True))
    assert head_of(lib, "L1").lock_change == "locked"


def test_copy_is_a_new_object_naming_its_tracked_source(lib):
    src = resolved(lib, "P1")
    with lib.collect_outcomes() as sink:
        es = lib.copy("P1", "C1")
    assert es.name == "C1" and sink.last.complete and sink.last.operation == "copy" and sink.last.object_id != src.object_id
    head = head_of(lib, "C1")
    assert head.operation == "copied" and head.source == {"object_id": src.object_id, "seq": src.seq}


def test_copy_never_adopts_an_untracked_source(lib):
    placed = new_set("U2").model_dump_json(indent=3).encode()
    (lib.sets_dir / "U2.json").write_bytes(placed)
    lib.copy("U2", "C2")
    assert resolved(lib, "U2").state is S.UNTRACKED
    assert head_of(lib, "C2").source == {"path": "sets/U2.json", "content_hash": {"algorithm": "sha256", "value": sha(placed)},
                                         "state": "untracked"}


def test_rename_keeps_the_object(lib):
    before = resolved(lib, "P1")
    with lib.collect_outcomes() as sink:
        es = lib.rename("P1", "R1")
    o = sink.last
    assert es.name == "R1" and o.complete and (o.operation, o.path, o.previous_path) == ("rename", "sets/R1.json", "sets/P1.json")
    assert o.previous_path_readback is R.ABSENT and o.object_id == before.object_id
    head = head_of(lib, "R1")
    assert (head.operation, head.previous_path) == ("renamed", "sets/P1.json")
    assert resolved(lib, "P1").state is S.NONE


def test_rename_within_one_file_records_the_real_file_name(lib):
    lib.save(new_set("A B"))
    lib.rename("A B", "A_B")  # both names use the file A_B.json
    head = head_of(lib, "A_B")
    assert (head.path, head.previous_path) == ("sets/A_B.json", "sets/A_B.json")


@pytest.mark.skipif(sys.platform != "win32", reason="a case-only rename reaches one file only on a case-insensitive file system")
def test_case_only_rename_records_the_file_as_it_is_on_disk(lib):
    lib.save(new_set("casey"))
    with lib.collect_outcomes() as sink:
        lib.rename("casey", "CASEY")
    assert sorted(p.name for p in lib.sets_dir.glob("[cC]*.json")) == ["CASEY.json"]  # the replace gave it the new spelling
    assert sink.last.complete and sink.last.path == "sets/CASEY.json"
    assert h.resolve(lib.root, T.EXPOSURE_SET, "sets/CASEY.json").state is S.TRACKED


def test_delete_writes_a_tombstone(lib):
    before = resolved(lib, "P1")
    with lib.collect_outcomes() as sink:
        assert lib.delete("P1") is None
    o = sink.last
    assert o.complete and o.operation == "delete" and o.readback is R.ABSENT
    assert objects(lib)[before.object_id].state is O.D4


def test_import_is_a_new_object_naming_the_external_file(lib, tmp_path):
    f = tmp_path / "in.json"
    raw = new_set("I1").model_dump_json(indent=2).encode()
    f.write_bytes(raw)
    with lib.collect_outcomes() as sink:
        es = lib.import_file(f)
    assert es.name == "I1" and len(sink) == 1 and sink.last.complete and sink.last.operation == "import"
    head = head_of(lib, "I1")
    assert head.operation == "imported"
    assert head.source == {"external_path": str(f), "content_hash": {"algorithm": "sha256", "value": sha(raw)}}


def test_import_overwrite_of_an_existing_set_is_not_recorded(lib, tmp_path):
    f = tmp_path / "in.json"
    f.write_text(new_set("P1").model_dump_json(indent=2), encoding="utf-8")
    before = hist(lib)
    with lib.collect_outcomes() as sink:
        lib.import_file(f, overwrite=True)
    o = sink.last
    assert (o.canonical, o.history, o.history_reason) == (C.DONE, H.NOT_RECORDED, HR.IMPORT_OVERWRITE_UNDEFINED)
    assert hist(lib) == before


def test_import_overwrite_with_no_existing_set_is_a_new_object(lib, tmp_path):
    f = tmp_path / "in.json"
    f.write_text(new_set("I2").model_dump_json(indent=2), encoding="utf-8")
    with lib.collect_outcomes() as sink:
        lib.import_file(f, overwrite=True)
    assert sink.last.complete


def test_import_of_a_bad_file_is_refused_with_one_outcome(lib, tmp_path):
    f = tmp_path / "bad.json"
    f.write_text("not json", encoding="utf-8")
    before = hist(lib)
    with lib.collect_outcomes() as sink:
        with pytest.raises(ValueError):
            lib.import_file(f)
    assert len(sink) == 1 and (sink.last.canonical, sink.last.history) == (C.NOT_DONE, H.NOT_APPLICABLE)
    assert sink.last.operation == "import" and hist(lib) == before


def test_seeding_records_each_file_in_order(root):
    lib = Library(root / "lib", seed=False)
    with lib.collect_outcomes() as sink:
        lib.ensure_defaults()
    names = [es.name for es in default_sets()]
    assert [o.path for o in sink] == [f"sets/{n}.json" for n in names]
    assert all(o.complete and o.operation == "create" for o in sink)
    for es in default_sets():
        head = head_of(lib, es.name)
        assert head.operation == "created" and head.lock_change == ("locked" if es.locked else None)


def test_seeding_with_nothing_to_do_writes_nothing(lib):
    before = hist(lib)
    with lib.collect_outcomes() as sink:
        lib.ensure_defaults()
    assert len(sink) == 0 and hist(lib) == before


def check_raising_on(monkeypatch, call_number, exc):
    """Make the B2a re-check raise on its n-th call, on every platform. Linux: the hook the lock sets; Windows: the variable."""
    n = {"calls": 0}

    def run():
        n["calls"] += 1
        if n["calls"] == call_number:
            raise exc

    monkeypatch.setattr(libmod, "_final_check", lambda held: run())
    return run, n


def test_seeding_stopped_by_the_re_check_after_one_file(root, monkeypatch):
    lib = Library(root / "lib", seed=False)
    run, n = check_raising_on(monkeypatch, 3, LibraryLocked(ABANDONED, locks.Refusal.BUSY))  # staged, first replace, second
    token = libmod._FINAL_CHECK.set(run)
    try:
        with lib.collect_outcomes() as sink:
            with pytest.raises(LibraryLocked):
                lib.ensure_defaults()
    finally:
        libmod._FINAL_CHECK.reset(token)
    first, *rest = list(sink)
    assert (first.canonical, first.readback, first.history, first.history_reason) == (
        C.UNCERTAIN, R.READ, H.RECORDED, HR.CANONICAL_STEP_FAILED)
    assert rest and all((o.canonical, o.readback, o.history) == (C.NOT_DONE, R.ABSENT, H.RECORDED_NOT_CURRENT) for o in rest)


def variants(base, count):
    return [new_set(f"{base}_V{i}") for i in range(1, count + 1)]


def test_doe_batch_records_each_variant_with_its_source(lib):
    src = resolved(lib, "P1")
    with lib.collect_outcomes() as sink:
        with doemod.batch_source("P1"):
            result = doemod.save_batch(lib, variants("P1", 3))
    assert result.complete and [o.path for o in sink] == [f"sets/P1_V{i}.json" for i in (1, 2, 3)]
    assert all(o.complete and o.operation == "doe" for o in sink)
    for i in (1, 2, 3):
        head = head_of(lib, f"P1_V{i}")
        assert head.operation == "doe" and head.source == {"object_id": src.object_id, "seq": src.seq}


def test_doe_batch_without_a_named_base_is_recorded_as_created(lib):
    with lib.collect_outcomes() as sink:
        doemod.save_batch(lib, variants("X", 1))
    assert sink.last.operation == "doe" and head_of(lib, "X_V1").operation == "created"


def test_doe_is_non_transactional_and_each_variant_has_its_outcome(lib):
    lib.save(new_set("P1_V2", description="already here"))  # taken: the batch stops there, keeping what it saved
    with lib.collect_outcomes() as sink:
        with doemod.batch_source("P1"):
            result = doemod.save_batch(lib, variants("P1", 3))
    assert not result.complete and result.saved == ["P1_V1"] and result.not_attempted == ["P1_V3"]
    assert [o.canonical for o in sink] == [C.DONE, C.NOT_DONE] and sink.last.history is H.NOT_APPLICABLE


def test_a_doe_context_does_not_leak_into_later_saves(lib):
    with doemod.batch_source("P1"):
        doemod.save_batch(lib, variants("P1", 1))
    with lib.collect_outcomes() as sink:
        lib.save(new_set("after"))
    assert sink.last.operation == "save" and head_of(lib, "after").operation == "created"


def test_a_doe_batch_reads_each_verified_object_once(lib, monkeypatch):
    """The budget for §7 finding P-5: linear in the objects, not quadratic. Counted, so it does not depend on the machine."""
    reads = []
    real = h._read_entry
    monkeypatch.setattr(h, "_read_entry", lambda p: (reads.append(p), real(p))[1])
    objects_before = len(objects(lib))
    reads.clear()
    count = 25
    with doemod.batch_source("P1"):
        assert doemod.save_batch(lib, variants("P1", count)).complete
    assert len(reads) <= 3 * (objects_before + count) + count  # identity, record and blob of each object, once


def test_outside_a_batch_every_resolution_reads_everything(lib, monkeypatch):
    reads = []
    real = h._read_entry
    monkeypatch.setattr(h, "_read_entry", lambda p: (reads.append(p), real(p))[1])
    resolved(lib, "P1")
    first = len(reads)
    resolved(lib, "P1")
    assert first > 0 and len(reads) == 2 * first


def test_inside_a_batch_a_new_revision_is_seen(lib):
    with h.read_cache():
        assert resolved(lib, "P1").seq == seq(1)
        es = lib.get("P1")
        es.description = "second"
        lib.save(es)
        r = resolved(lib, "P1")
        assert (r.state, r.seq) == (S.TRACKED, seq(2))


def test_inside_a_batch_a_new_entry_in_an_object_is_damage(lib):
    with h.read_cache():
        before = resolved(lib, "P1")
        assert before.state is S.TRACKED
        (lib.root / ".history/objects/exposure_set" / before.object_id / "junk").write_bytes(b"")
        assert resolved(lib, "P1").state is S.UNKNOWN


def test_outside_a_batch_a_changed_blob_is_damage_at_once(lib):
    before = resolved(lib, "P1")
    with h.read_cache():
        resolved(lib, "P1")
    blob = lib.root / ".history/objects/exposure_set" / before.object_id / "revisions" / seq(1) / "content"
    blob.write_bytes(b"tampered")
    assert resolved(lib, "P1").state is S.UNKNOWN


def test_a_batch_cache_does_not_outlive_its_batch(lib):
    with h.read_cache():
        resolved(lib, "P1")
    assert h._READ_CACHE.get() is None


# --------------------------------------------------------------------------- phase P refusals


def refused(lib, call, exc_type=LibraryError):
    before = hist(lib)
    with lib.collect_outcomes() as sink:
        with pytest.raises(exc_type) as e:
            call()
    assert len(sink) == 1
    o = sink.last
    assert (o.canonical, o.canonical_reason, o.readback, o.history, o.adoption) == (
        C.NOT_DONE, CR.PRECONDITION_REFUSED, R.NOT_PERFORMED, H.NOT_APPLICABLE, A.NOT_NEEDED)
    assert o.canonical_error == str(e.value) and o.seq is None and o.object_id is None
    assert hist(lib) == before  # §7.1: a refusal in P happens before any history artefact exists
    return e.value


def test_refusals_in_phase_p(lib):
    es = new_set(DEFAULT)
    assert "locked" in str(refused(lib, lambda: lib.save(es)))
    assert "already exists" in str(refused(lib, lambda: lib.save(new_set("P1"), overwrite=False)))
    assert "no exposure set" in str(refused(lib, lambda: lib.delete("nope")))
    assert "no exposure set" in str(refused(lib, lambda: lib.rename("nope", "x")))
    assert "already exists" in str(refused(lib, lambda: lib.copy("P1", DEFAULT)))
    refused(lib, lambda: lib.save(new_set("???")), LibraryError)  # no usable file name


def test_a_model_that_would_not_read_back_is_refused_before_any_history(lib):
    bad = new_set("Z1")
    bad.__dict__["schema_version"] = 99  # a validation bypass; _text refuses to write it
    before = hist(lib)
    with lib.collect_outcomes() as sink:
        with pytest.raises(LibraryError):
            lib.save(bad)
    assert sink.last.history is H.NOT_APPLICABLE and hist(lib) == before


# --------------------------------------------------------------------------- compatibility


SIGNATURES = {
    "save": "(self, es: 'ExposureSet', overwrite: 'bool' = True) -> 'None'",
    "copy": "(self, source: 'str', new_name: 'str') -> 'ExposureSet'",
    "rename": "(self, old: 'str', new: 'str') -> 'ExposureSet'",
    "delete": "(self, name: 'str') -> 'None'",
    "import_file": "(self, path: 'Path | str', overwrite: 'bool' = False) -> 'ExposureSet'",
    "ensure_defaults": "(self) -> 'None'",
    "save_machine": "(self, mp: 'MachineProfile', overwrite: 'bool' = True) -> 'None'",
    "import_machine": "(self, path: 'Path | str', overwrite: 'bool' = False) -> 'MachineProfile'",
    "save_process": "(self, ps: 'ProcessSettings') -> 'None'",
}


@pytest.mark.parametrize("name", sorted(SIGNATURES))
def test_writer_signatures_are_unchanged(name):
    assert str(inspect.signature(getattr(Library, name))) == SIGNATURES[name]


def test_save_batch_signature_is_unchanged():
    assert str(inspect.signature(doemod.save_batch)) == (
        "(lib: 'Library', variants: 'Sequence[ExposureSet]', on_saved: 'Callable[[str], None] | None' = None) -> 'BatchSave'")


def plain_bytes(tmp, es):
    """What the unchanged routine writes for this model, with history nowhere near it."""
    f = tmp / "control.json"
    libmod._atomic_write(f, es.model_dump_json(indent=2))
    return f.read_bytes()


def test_canonical_bytes_are_unchanged_by_history(lib, tmp_path):
    es = new_set("B1")
    lib.save(es)
    assert (lib.sets_dir / "B1.json").read_bytes() == plain_bytes(tmp_path, es)
    es2 = lib.copy("B1", "B2")
    assert (lib.sets_dir / "B2.json").read_bytes() == plain_bytes(tmp_path, es2)
    es3 = lib.rename("B2", "B3")
    assert (lib.sets_dir / "B3.json").read_bytes() == plain_bytes(tmp_path, es3)


def test_the_blob_equals_the_canonical_file(lib):
    with lib.collect_outcomes() as sink:
        lib.save(new_set("B4"))
    blob = lib.root / ".history/objects/exposure_set" / sink.last.object_id / "revisions" / seq(1) / "content"
    assert blob.read_bytes() == (lib.sets_dir / "B4.json").read_bytes()


def test_the_model_is_serialized_once(lib, monkeypatch):
    calls = []
    real = Library._text
    monkeypatch.setattr(Library, "_text", staticmethod(lambda es: (calls.append(es.name), real(es))[1]))
    lib.save(new_set("S1"))
    assert calls == ["S1"]
    calls.clear()
    lib.copy("S1", "S2")
    assert calls == ["S2"]
    calls.clear()
    lib.rename("S2", "S3")
    assert calls == ["S3"]


def test_history_is_invisible_to_the_library_reads(lib):
    lib.save(new_set("V1"))
    assert (lib.root / ".history").is_dir()
    assert not lib.load_errors and "V1" in lib.names() and not list(lib.sets_dir.glob(".history*"))


def test_collector_is_optional(lib):
    lib.save(new_set("O1"))
    assert resolved(lib, "O1").state is S.TRACKED


# --------------------------------------------------------------------------- D-7


STEPS_UNTRACKED = ["root.mkdir", "A1.read", "A2.mkdir", "A3.temp", "A3.fsync", "A3.replace", "A3.dirfsync", "A4.mkdir_revisions",
                   "A4.mkdir", "A5.write", "A5.fsync", "A5.verify", "A6.temp", "A6.fsync", "A6.replace", "A6.dirfsync", "A7.read",
                   "S3.write", "S3.read", "S4.mkdir", "S5.write", "S5.fsync", "S5.verify", "S6.temp", "S6.fsync", "S6.replace",
                   "S6.dirfsync", "S9.unlink"]


@pytest.mark.parametrize("step", STEPS_UNTRACKED)
def test_no_history_failure_changes_a_save(lib, tmp_path, monkeypatch, step):
    (lib.sets_dir / "U3.json").write_text(new_set("U3").model_dump_json(indent=4), encoding="utf-8")
    es = new_set("U3", description="saved over it")
    fail_at(monkeypatch, step)
    with lib.collect_outcomes() as sink:
        assert lib.save(es) is None
    assert sink.last.canonical is C.DONE
    assert (lib.sets_dir / "U3.json").read_bytes() == plain_bytes(tmp_path, es)
    assert lib.get("U3") == es


@pytest.mark.parametrize("step", ["S3.write", "S4.mkdir", "S5.write", "S6.temp", "S6.replace"])
@pytest.mark.parametrize("call", ["copy", "rename"])
def test_no_history_failure_changes_copy_or_rename(lib, monkeypatch, step, call):
    fail_at(monkeypatch, step)
    es = getattr(lib, call)("P1", "T1")
    assert es.name == "T1" and lib.get("T1") == es


@pytest.mark.parametrize("step", ["D4.mkdir", "D6.temp", "D6.replace", "D6.dirfsync"])
def test_no_history_failure_changes_a_delete(lib, monkeypatch, step):
    fail_at(monkeypatch, step)
    assert lib.delete("P1") is None and not lib.exists("P1")


def test_damaged_history_never_stops_a_save(lib, tmp_path):
    d = next((lib.root / ".history/objects/exposure_set").iterdir())
    (d / "junk").write_bytes(b"")
    es = new_set("D1")
    with lib.collect_outcomes() as sink:
        lib.save(es)
    assert (sink.last.canonical, sink.last.history, sink.last.history_reason) == (C.DONE, H.NOT_RECORDED, HR.STATE_UNKNOWN)
    assert (lib.sets_dir / "D1.json").read_bytes() == plain_bytes(tmp_path, es)


def test_a_history_folder_that_is_a_file_never_stops_a_save(root):
    lib = Library(root / "lib", seed=False)
    (lib.root / ".history").write_bytes(b"")
    with lib.collect_outcomes() as sink:
        lib.save(new_set("D2"))
    assert (sink.last.canonical, sink.last.history_reason) == (C.DONE, HR.STATE_UNVERIFIABLE)


def test_an_invalid_outcome_never_changes_the_canonical_result(lib, monkeypatch):
    def broken(*a, **k):
        raise oc.InvalidOutcome("defect")

    monkeypatch.setattr(h, "conclude", broken)
    assert lib.save(new_set("G1")) is None and lib.exists("G1")


def test_canonical_failure_keeps_its_own_exception_and_message(lib, monkeypatch):
    real = libmod._atomic_write
    boom = OSError(errno.ENOSPC, "No space left on device")

    def canonical_fails(path, text):
        if Path(path).parent == lib.sets_dir:
            raise boom
        real(path, text)

    monkeypatch.setattr(libmod, "_atomic_write", canonical_fails)
    before = (lib.sets_dir / "P1.json").read_bytes()
    es = lib.get("P1")
    es.description = "will fail"
    with lib.collect_outcomes() as sink:
        with pytest.raises(OSError) as e:
            lib.save(es)
    assert e.value is boom and (lib.sets_dir / "P1.json").read_bytes() == before
    o = sink.last
    assert (o.canonical, o.readback, o.history, o.history_reason) == (C.NOT_DONE, R.READ, H.RECORDED_NOT_CURRENT, HR.CANONICAL_STEP_FAILED)
    assert o.canonical_error == str(boom)


def test_saving_identical_bytes_then_failing_is_uncertain_not_not_done(lib, monkeypatch):
    """The file shows both the prior and the target bytes: only C-2c is a valid report."""
    real = libmod._atomic_write
    boom = OSError(errno.EIO, "x")

    def canonical_fails(path, text):
        if Path(path).parent == lib.sets_dir:
            raise boom
        real(path, text)

    monkeypatch.setattr(libmod, "_atomic_write", canonical_fails)
    with lib.collect_outcomes() as sink:
        with pytest.raises(OSError):
            lib.save(lib.get("P1"))
    assert (sink.last.canonical, sink.last.readback, sink.last.history) == (C.UNCERTAIN, R.READ, H.RECORDED)


def test_the_outcome_is_in_the_sink_before_the_exception_leaves(lib, monkeypatch):
    real = libmod._atomic_write
    seen = []

    def canonical_fails(path, text):
        if Path(path).parent == lib.sets_dir:
            raise OSError(errno.EIO, "x")
        real(path, text)

    monkeypatch.setattr(libmod, "_atomic_write", canonical_fails)
    with lib.collect_outcomes() as sink:
        try:
            lib.save(new_set("E1"))
        except OSError:
            seen.append(len(sink))
    assert seen == [1]


def test_nested_import_gives_one_outcome(lib, tmp_path):
    f = tmp_path / "in.json"
    f.write_text(new_set("I3").model_dump_json(indent=2), encoding="utf-8")
    with lib.collect_outcomes() as sink:
        lib.import_file(f)
    assert len(sink) == 1


def test_an_interrupt_in_phase_h_leaves_as_it_does_today_and_gives_no_outcome(lib, monkeypatch):
    fail_at(monkeypatch, "S5.write", KeyboardInterrupt())
    with lib.collect_outcomes() as sink:
        with pytest.raises(KeyboardInterrupt):
            lib.save(new_set("K1"))
    assert len(sink) == 0


# --------------------------------------------------------------------------- phase order and the re-check


def trace_check(monkeypatch, events):
    def run():
        events.append("final_check")

    monkeypatch.setattr(libmod, "_final_check", lambda held: run())
    monkeypatch.setattr(h, "_fault", events.append)
    return run


def steps_of(events):
    return [e for e in events if e.startswith(("S", "final"))]


def test_the_re_check_runs_once_after_the_record_committed(lib, monkeypatch):
    events = []
    run = trace_check(monkeypatch, events)
    es = lib.get("P1")
    es.description = "x"
    token = libmod._FINAL_CHECK.set(run)  # Windows has no lock to set it
    try:
        lib.save(es)
    finally:
        libmod._FINAL_CHECK.reset(token)
    assert events.count("final_check") == 1
    assert events.index("S6.replace") < events.index("final_check") < events.index("V.read")


def test_delete_re_checks_after_the_tombstone_committed(lib, monkeypatch):
    events = []
    run = trace_check(monkeypatch, events)
    token = libmod._FINAL_CHECK.set(run)
    try:
        lib.delete("P1")
    finally:
        libmod._FINAL_CHECK.reset(token)
    assert events.count("final_check") == 1 and events.index("D6.replace") < events.index("final_check")


def abandon_at_c(lib, monkeypatch, call):
    run, _ = check_raising_on(monkeypatch, 1, LibraryLocked(ABANDONED, locks.Refusal.BUSY))
    token = libmod._FINAL_CHECK.set(run)
    try:
        with lib.collect_outcomes() as sink:
            with pytest.raises(LibraryLocked) as e:
                call()
    finally:
        libmod._FINAL_CHECK.reset(token)
    assert str(e.value) == ABANDONED
    return sink.last


def test_abandon_at_c_on_save_keeps_the_committed_record_and_the_prior_file(lib, monkeypatch):
    es = lib.get("P1")
    es.description = "x"
    before = (lib.sets_dir / "P1.json").read_bytes()
    o = abandon_at_c(lib, monkeypatch, lambda: lib.save(es))
    assert (o.canonical, o.readback, o.history, o.history_reason) == (C.NOT_DONE, R.READ, H.RECORDED_NOT_CURRENT, HR.CANONICAL_STEP_FAILED)
    assert (lib.sets_dir / "P1.json").read_bytes() == before
    assert resolved(lib, "P1").history_ahead


def test_abandon_at_c_on_delete_leaves_tombstone_and_file(lib, monkeypatch):
    o = abandon_at_c(lib, monkeypatch, lambda: lib.delete("P1"))
    assert (o.canonical, o.history) == (C.NOT_DONE, H.RECORDED_NOT_CURRENT)
    assert lib.exists("P1") and resolved(lib, "P1").state is S.TOMBSTONED_PRESENT


@posix
def test_phase_h_runs_inside_the_lock_hold(lib, monkeypatch):
    held = []

    def probe(step):
        if step in ("S3.write", "S5.write", "S6.replace"):
            fd = os.open(lib.sets_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held.append(False)
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except BlockingIOError:
                    held.append(True)
            finally:
                os.close(fd)

    monkeypatch.setattr(h, "_fault", probe)
    lib.save(new_set("H1"))
    assert held == [True, True, True]


@posix
def test_a_migration_folder_appearing_during_h_abandons_at_c(lib, monkeypatch):
    es = lib.get("P1")
    es.description = "x"
    before = (lib.sets_dir / "P1.json").read_bytes()

    def appear(step):
        if step == "S6.replace":
            (lib.sets_dir / M).mkdir()

    monkeypatch.setattr(h, "_fault", appear)
    with lib.collect_outcomes() as sink:
        with pytest.raises(LibraryLocked) as e:
            lib.save(es)
    assert str(e.value) == ABANDONED and (lib.sets_dir / "P1.json").read_bytes() == before
    assert (sink.last.canonical, sink.last.history) == (C.NOT_DONE, H.RECORDED_NOT_CURRENT)
    assert resolved(lib, "P1").history_ahead  # DC-001 row C-1


@posix
def test_a_lock_refusal_is_in_phase_p_with_no_history(lib):
    fd = os.open(lib.sets_dir, os.O_RDONLY | os.O_DIRECTORY)
    pid = os.fork()
    if pid == 0:  # another process, so that the lock really conflicts
        fcntl.flock(os.open(lib.sets_dir, os.O_RDONLY | os.O_DIRECTORY), fcntl.LOCK_EX)
        os.write(fd if False else 1, b"")
        import time
        time.sleep(60)
        os._exit(0)
    try:
        import time
        for _ in range(500):
            probe = os.open(lib.sets_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
                fcntl.flock(probe, fcntl.LOCK_UN)
            except BlockingIOError:
                break
            finally:
                os.close(probe)
            time.sleep(0.01)
        before = hist(lib)
        with lib.collect_outcomes() as sink:
            with pytest.raises(LibraryLocked):
                lib.save(new_set("H2"))
        assert (sink.last.history, sink.last.canonical_reason) == (H.NOT_APPLICABLE, CR.PRECONDITION_REFUSED)
        assert hist(lib) == before
    finally:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
        os.close(fd)


@posix
def test_a_release_error_arrives_after_the_outcome(lib, monkeypatch):
    """B2b-3: the write is done and complete; the release's OSError reaches the caller unchanged."""
    real_close = os.close
    sets_ident = (os.stat(lib.sets_dir).st_dev, os.stat(lib.sets_dir).st_ino)
    armed = {"on": False}

    def close(fd):
        if armed["on"]:
            st = os.fstat(fd)
            if (st.st_dev, st.st_ino) == sets_ident:
                armed["on"] = False
                real_close(fd)
                raise OSError(errno.EIO, "close failed")
        real_close(fd)

    monkeypatch.setattr(locks.os, "close", close)
    armed["on"] = True
    es = new_set("H3")
    with lib.collect_outcomes() as sink:
        try:
            lib.save(es)
        except OSError:
            pass
    armed["on"] = False
    assert sink.last.complete and lib.get("H3") == es


# --------------------------------------------------------------------------- B7 is untouched


@posix
def test_upgrade_reconcile_and_restore_ignore_history_and_write_none(lib):
    from lpbfstudio import reconcile as rc
    from lpbfstudio import restore as rs

    data = json.loads(new_set("F1").model_dump_json())
    data["schema_version"] = 1
    (lib.sets_dir / "F1.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    es = lib.get("F1")
    es.description = "tracked before the upgrade"
    lib.save(es)  # adopted, then saved: a tracked format 1 set
    before = hist(lib)
    assert up.upgrade_file(lib.sets_dir, "F1.json").result.value == "upgraded"
    report = rc.reconcile_file(lib.sets_dir, "F1.json")
    assert report.rule == "P12" and not report.unexpected
    assert hist(lib) == before
    assert resolved(lib, "F1").state is S.CHANGED_OUTSIDE
    assert rs.restore_file(lib.sets_dir, "F1.json").result.value == "restored"
    assert hist(lib) == before
    assert resolved(lib, "F1").state is S.TRACKED  # the restored bytes are the tracked format 1 revision


# --------------------------------------------------------------------------- real kills through Library.save


KILL = textwrap.dedent("""
    import os, sys
    from lpbfstudio import history as h, library as L
    from lpbfstudio.defaults import default_sets
    root, step = sys.argv[1], sys.argv[2]
    lib = L.Library(root, seed=False)
    es = lib.get("P1")
    es.description = "the new bytes"
    if step == "C.before_replace":
        real = os.replace
        def kill(a, b):
            if str(b).endswith("P1.json"):
                os._exit(9)
            return real(a, b)
        L.os.replace = kill
    elif step == "C.after_replace":
        real = os.replace
        def kill(a, b):
            real(a, b)
            if str(b).endswith("P1.json"):
                os._exit(9)
        L.os.replace = kill
    else:
        def fault(s):
            if s == step:
                os._exit(9)
        h._fault = fault
    lib.save(es)
    os._exit(0)
""")


@pytest.mark.parametrize("step", ["S3.write", "S3.read", "S4.mkdir", "S5.write", "S5.fsync", "S5.verify", "S6.temp", "S6.fsync",
                                  "S6.replace", "S6.dirfsync", "C.before_replace", "C.after_replace", "V.read", "S9.unlink"])
def test_a_kill_during_a_save_leaves_a_whole_file_and_a_defined_history(lib, step):
    old = (lib.sets_dir / "P1.json").read_bytes()
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([SRC, os.environ.get("PYTHONPATH", "")]))
    p = subprocess.run([sys.executable, "-c", KILL, str(lib.root), step], env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode == 9, p.stderr
    now = (lib.sets_dir / "P1.json").read_bytes()
    assert lib.get("P1").description in ("the new bytes", json.loads(old)["description"])
    assert all(o.state is not O.D5 for o in h.scan_objects(lib.root, T.EXPOSURE_SET))
    r = resolved(lib, "P1")
    if step in ("C.after_replace", "V.read", "S9.unlink"):
        assert now != old and r.state is S.TRACKED and not r.history_ahead  # S-9 / S-5: tracked at n
    elif step in ("S6.dirfsync", "C.before_replace"):
        assert now == old and r.state is S.TRACKED and r.history_ahead  # S-4: history ahead
    else:
        assert now == old and r.state is S.TRACKED and not r.history_ahead  # S-1 to S-3: unchanged
    # the library still works afterwards
    with lib.collect_outcomes() as sink:
        es = lib.get("P1")
        es.description = "after the kill"
        lib.save(es)
    assert sink.last.complete


# --------------------------------------------------------------------------- static coverage of the callers


WRITERS = {"save", "copy", "rename", "delete", "import_file", "ensure_defaults", "save_machine", "import_machine", "save_process"}

# Callers that do not open a collector yet. Showing the status is package 0.6, under product authorization Q4-L2. Until
# then each is listed here by file, enclosing function and method: a new caller, or a stale entry, fails the test below.
PENDING_Q4L2 = {
    ("cli.py", "new", "save"), ("cli.py", "copy", "copy"), ("cli.py", "rename", "rename"), ("cli.py", "delete", "delete"),
    ("cli.py", "set_values", "save"), ("cli.py", "machine_import", "import_machine"), ("cli.py", "import_cmd", "import_file"),
    ("doe.py", "save_batch", "save"),
    ("gui/app.py", "save_current", "save"), ("gui/app.py", "copy_set", "copy"), ("gui/app.py", "rename_set", "rename"),
    ("gui/app.py", "delete_set", "delete"), ("gui/app.py", "new_set", "save"), ("gui/app.py", "edit_process", "save_process"),
    ("gui/app.py", "edit_machine", "save_machine"), ("gui/app.py", "import_set", "import_file"),
}
# Writers called by other writers or by the constructor: the outer call's collector covers them.
INTERNAL = {("library.py", "import_file", "save"), ("library.py", "import_machine", "save_machine"),
            ("library.py", "__init__", "ensure_defaults")}


def production_sites():
    root = Path(SRC) / "lpbfstudio"
    found, covered = set(), set()
    for f in sorted(root.rglob("*.py")):
        rel = f.relative_to(root).as_posix()
        tree_ = ast.parse(f.read_text(encoding="utf-8"))

        def walk(node, function, collecting):
            for child in ast.iter_child_nodes(node):
                fn, col = function, collecting
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    fn = child.name
                if isinstance(child, ast.With) and any("collect_outcomes" in ast.unparse(i.context_expr) for i in child.items):
                    col = True
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and child.func.attr in WRITERS:
                    receiver = ast.unparse(child.func.value)
                    if "lib" in receiver.lower() or (rel == "library.py" and receiver == "self"):
                        (covered if collecting else found).add((rel, function, child.func.attr))
                walk(child, fn, col)

        walk(tree_, "<module>", False)
    return found, covered


def test_every_production_writer_call_is_covered_or_listed_as_pending():
    found, covered = production_sites()
    assert found - INTERNAL == PENDING_Q4L2 - covered
    assert INTERNAL <= found
