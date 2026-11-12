"""R7-B O-1: a copy and a finalization examine history once, and nothing else about them changes.

Every comparison runs the operation twice on byte-identical libraries: once as shipped, and once with the operation's
memory switched off (``plainly``), which is the behaviour of the baseline ``ce62165``. Object ids and times are made
repeatable, so the two libraries must end byte for byte the same.
"""

import contextlib
import itertools
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from _history_support import seq, tree
from _r7b_support import age, plain
from lpbfstudio import history as h
from lpbfstudio import jobs as j
from lpbfstudio import library as library_module
from lpbfstudio.defaults import default_sets
from lpbfstudio.history import ObjectType as T
from lpbfstudio.history import PathState as S
from lpbfstudio.jobs import JobStatus as St
from lpbfstudio.library import Library, LibraryError
from lpbfstudio.models import MachineProfile, ProcessSettings

DEFAULT = default_sets()[0].name
MACHINE = MachineProfile().name
SRC = str(Path(h.__file__).resolve().parents[1])
SETS = ".history/objects/exposure_set"


# --------------------------------------------------------------------------- helpers


class Repeatable:
    """Stands in for ``secrets`` inside history, so that two runs choose the same ids and temporary names."""

    def __init__(self) -> None:
        self.n = itertools.count(1)

    def token_hex(self, size: int) -> str:
        return f"{next(self.n):0{2 * size}x}"


@pytest.fixture(autouse=True)
def fixed_time(monkeypatch):
    monkeypatch.setattr(h, "_now", lambda: "2026-01-01T00:00:00Z")


def repeatable(monkeypatch) -> None:
    monkeypatch.setattr(h, "secrets", Repeatable())


@contextlib.contextmanager
def plainly(monkeypatch):
    """Inside the block the operations keep no memory: every resolution examines everything, as at the baseline."""
    with monkeypatch.context() as m:
        m.setattr(h, "read_cache", lambda settle_ns=0: contextlib.nullcontext())
        yield


def reads(monkeypatch) -> list[str]:
    """Every file read under ``.history`` from now on, as a path relative to the objects folder."""
    seen: list[str] = []
    real = h._read_entry

    def read(p):
        seen.append(Path(p).as_posix().split("/.history/objects/")[-1])
        return real(p)

    monkeypatch.setattr(h, "_read_entry", read)
    return seen


def edit(lib, name="P1", text="changed"):
    es = lib.get(name)
    es.description = text
    lib.save(es)


def untracked(lib, name="U1", like="P1"):
    es = lib.get(like).model_copy(deep=True)
    es.name = name
    lib._file(name).write_text(es.model_dump_json(indent=2), encoding="utf-8")


def base(root: Path) -> Library:
    lib = Library(root)
    lib.copy(DEFAULT, "P1")
    lib.copy(DEFAULT, "P2")
    lib.copy(DEFAULT, "P3")
    edit(lib, "P2", "second")
    lib.save_process(ProcessSettings())
    return lib


def object_of(lib, name):
    return h.resolve(lib.root, T.EXPOSURE_SET, lib._rel(lib._file(name)))


def twin(tmp_path, build) -> tuple[Library, Library]:
    """Two byte-identical libraries at rest."""
    a = tmp_path / "a" / "lib"
    lib = base(a)
    build(lib)
    age(a)
    shutil.copytree(a, tmp_path / "b" / "lib")
    age(tmp_path / "b" / "lib")
    assert tree(a) == tree(tmp_path / "b" / "lib")
    return Library(a, seed=False), Library(tmp_path / "b" / "lib", seed=False)


def run(lib, operation):
    with lib.collect_outcomes() as sink:
        try:
            result = operation(lib)
        except LibraryError as exc:
            result = ("refused", str(exc))
    return plain(result), plain(list(sink))


def both(tmp_path, monkeypatch, build, operation):
    """The operation as shipped on one library and with no memory on its twin: (shipped, plain), and the twin trees."""
    a, b = twin(tmp_path, build)
    repeatable(monkeypatch)
    shipped = run(a, operation)
    repeatable(monkeypatch)
    with plainly(monkeypatch):
        plain_run = run(b, operation)
    return shipped, plain_run, tree(a.root), tree(b.root)


# --------------------------------------------------------------------------- copy: the same result


def nothing(lib):
    pass


def changed_outside(lib):
    f = lib._file("P1")
    f.write_text(f.read_text(encoding="utf-8").replace('"description": "', '"description": "edited by hand '), encoding="utf-8")


def damaged(lib):
    (lib.root / SETS / object_of(lib, "P3").object_id / "junk").write_bytes(b"")


def tombstoned_target(lib):
    lib.copy("P1", "C1")
    lib.delete("C1")


def target_untracked_in_history_only(lib):
    """The file of an earlier C1 was removed by hand: its object's head is content, the path is absent."""
    lib.copy("P1", "C1")
    lib._file("C1").unlink()


def history_is_a_file(lib):
    shutil.rmtree(lib.root / ".history")
    (lib.root / ".history").write_bytes(b"")


def no_history(lib):
    shutil.rmtree(lib.root / ".history")


def source_history_ahead(lib):
    f = lib._file("P2")
    first = (lib.root / SETS / object_of(lib, "P2").object_id / "revisions" / seq(1) / "content").read_bytes()
    f.write_bytes(first)


def misnamed_source(lib):
    """P1 is held by a file whose name says something else."""
    os.replace(lib._file("P1"), lib.sets_dir / "elsewhere.json")


COPIES = [
    ("tracked", nothing, lambda lib: lib.copy("P1", "C1")),
    ("edited source", nothing, lambda lib: lib.copy("P2", "C1")),
    ("locked default", nothing, lambda lib: lib.copy(DEFAULT, "C1")),
    ("untracked source", untracked, lambda lib: lib.copy("U1", "C1")),
    ("source changed outside", changed_outside, lambda lib: lib.copy("P1", "C1")),
    ("source history ahead", source_history_ahead, lambda lib: lib.copy("P2", "C1")),
    ("misnamed source", misnamed_source, lambda lib: lib.copy("P1", "C1")),
    ("damaged object", damaged, lambda lib: lib.copy("P1", "C1")),
    ("tombstoned target", tombstoned_target, lambda lib: lib.copy("P1", "C1")),
    ("target missing but claimed", target_untracked_in_history_only, lambda lib: lib.copy("P1", "C1")),
    ("history is a file", history_is_a_file, lambda lib: lib.copy("P1", "C1")),
    ("no history yet", no_history, lambda lib: lib.copy("P1", "C1")),
    ("target exists", nothing, lambda lib: lib.copy("P1", "P2")),
    ("no source", nothing, lambda lib: lib.copy("none", "C1")),
    ("two copies", nothing, lambda lib: (lib.copy("P1", "C1"), lib.copy("C1", "C2"))[1]),
]


@pytest.mark.parametrize("build, operation", [c[1:] for c in COPIES], ids=[c[0] for c in COPIES])
def test_a_copy_ends_the_same_with_and_without_its_memory(tmp_path, monkeypatch, build, operation):
    shipped, plain_run, tree_a, tree_b = both(tmp_path, monkeypatch, build, operation)
    assert shipped == plain_run  # the returned set and every outcome
    assert tree_a == tree_b  # every byte of the library: canonical files, records, blobs, identities


def test_the_copies_compared_above_are_real_copies(tmp_path, monkeypatch):
    shipped, _, tree_a, _ = both(tmp_path, monkeypatch, nothing, lambda lib: lib.copy("P1", "C1"))
    outcome = shipped[1][0]
    assert (outcome["operation"], outcome["canonical"], outcome["history"]) == ("copy", "done", "recorded")
    record = [v for k, v in tree_a.items() if k.endswith("record.json") and b'"sets/C1.json"' in v and b'"copied"' in v]
    assert len(record) == 1 and b'"seq":"00000001"' in record[0].replace(b" ", b"")


# --------------------------------------------------------------------------- copy: one scan


def test_a_copy_examines_history_once(tmp_path, monkeypatch):
    a, b = twin(tmp_path, nothing)
    seen = reads(monkeypatch)
    object_of(a, "P1")
    one_scan = len(seen)
    assert one_scan >= 3 * 4  # identity, record and blob of each of the four objects, and the second revision of P2
    del seen[:]
    a.copy("P1", "C1")
    assert len(seen) == one_scan
    del seen[:]
    with plainly(monkeypatch):
        b.copy("P1", "C1")
    assert len(seen) == 2 * one_scan  # the baseline: the target, then the source


def test_a_copy_of_an_untracked_source_examines_history_once(tmp_path, monkeypatch):
    a, _ = twin(tmp_path, untracked)
    seen = reads(monkeypatch)
    object_of(a, "P1")
    one_scan = len(seen)
    del seen[:]
    a.copy("U1", "C1")
    assert len(seen) == one_scan


def test_a_copy_in_a_library_just_written_still_reads_what_has_not_settled(tmp_path, monkeypatch):
    """Without rest nothing is remembered: the copy behaves exactly as at the baseline."""
    lib = base(tmp_path / "lib")
    seen = reads(monkeypatch)
    object_of(lib, "P1")
    one_scan = len(seen)
    del seen[:]
    lib.copy("P1", "C1")
    assert len(seen) == 2 * one_scan


# --------------------------------------------------------------------------- copy: the memory ends with phase H


def test_the_memory_of_a_copy_is_gone_before_the_canonical_write(tmp_path, monkeypatch):
    lib, _ = twin(tmp_path, nothing)
    at_write = []
    real = library_module._atomic_write

    def write(f, text):
        at_write.append(h._READ_CACHE.get())
        return real(f, text)

    monkeypatch.setattr(library_module, "_atomic_write", write)
    lib.copy("P1", "C1")
    assert len(at_write) == 2  # the staged candidate inside phase H, then the canonical file in phase C
    assert at_write[0] is not None and at_write[1] is None
    assert h._READ_CACHE.get() is None and h._READ_SETTLE.get() == 0


def test_the_memory_of_a_copy_is_open_only_for_phase_h(tmp_path, monkeypatch):
    lib, _ = twin(tmp_path, nothing)
    during: dict[str, bool] = {}
    monkeypatch.setattr(h, "_fault", lambda step: during.setdefault(step, h._READ_CACHE.get() is not None))
    lib.copy("P1", "C1")
    assert during["scan.read"] and during["S6.replace"]  # phase H
    assert not during["V.read"] and not during["S9.unlink"]  # phase V


@pytest.mark.parametrize("operation", [lambda lib: lib.copy("P1", "P2"), lambda lib: lib.copy("none", "C1"),
                                       lambda lib: lib.copy("P1", "C1")], ids=["refused", "no source", "write fails"])
def test_the_memory_of_a_copy_never_survives_it(tmp_path, monkeypatch, operation):
    lib, _ = twin(tmp_path, nothing)

    def fail(f, text):
        raise OSError(28, "injected")

    monkeypatch.setattr(library_module, "_atomic_write", fail)
    with pytest.raises((LibraryError, OSError)):
        operation(lib)
    assert h._READ_CACHE.get() is None and h._READ_SETTLE.get() == 0


def test_a_step_interrupted_inside_a_copy_leaves_no_memory(tmp_path, monkeypatch):
    class Stop(BaseException):
        pass

    for step in ("scan.read", "A2.mkdir", "S6.replace", "V.read"):
        lib = base(tmp_path / step)
        age(lib.root)

        def fault(s, step=step):
            if s == step:
                raise Stop

        monkeypatch.setattr(h, "_fault", fault)
        with pytest.raises(Stop):
            lib.copy("P1", "C1")
        monkeypatch.setattr(h, "_fault", None)
        assert h._READ_CACHE.get() is None and h._READ_SETTLE.get() == 0


def test_saves_renames_and_deletes_keep_no_memory(tmp_path, monkeypatch):
    lib, _ = twin(tmp_path, untracked)
    seen: list[bool] = []
    monkeypatch.setattr(h, "_fault", lambda step: seen.append(h._READ_CACHE.get() is not None))
    edit(lib, "P1")
    lib.rename("P3", "R3")
    lib.delete("P2")
    edit(lib, "U1")
    assert seen and not any(seen)


# --------------------------------------------------------------------------- copy: another writer in the middle

OTHER = textwrap.dedent("""
    import sys
    from lpbfstudio.library import Library
    lib = Library(sys.argv[1], seed=False)
    es = lib.get(sys.argv[2])
    es.description = sys.argv[3]
    lib.save(es)
""")


def other_process_saves(lib, name, text="by another process"):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([SRC, os.environ.get("PYTHONPATH", "")]))
    p = subprocess.run([sys.executable, "-c", OTHER, str(lib.root), name, text], env=env, capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr


def after_first_resolve(monkeypatch, action):
    """Run ``action`` once, between the first and the second resolution of the operation that follows."""
    real = h.resolve
    calls = []

    def resolve(root, object_type, path):
        if len(calls) == 1:
            action()
        calls.append(path)
        return real(root, object_type, path)

    monkeypatch.setattr(h, "resolve", resolve)
    return calls


def test_a_revision_added_by_another_process_during_a_copy_is_seen(tmp_path, monkeypatch):
    lib, _ = twin(tmp_path, nothing)
    source = object_of(lib, "P1")
    assert source.seq == seq(1)
    calls = after_first_resolve(monkeypatch, lambda: other_process_saves(lib, "P1"))
    lib.copy("P1", "C1")
    assert calls == ["sets/C1.json", "sets/P1.json"]
    copy = object_of(lib, "C1")
    assert copy.head.source == {"object_id": source.object_id, "seq": seq(2)}  # not the remembered first revision


def test_that_revision_is_seen_even_if_the_folder_times_are_put_back(tmp_path, monkeypatch):
    """A file system with a coarse clock can show the same times after a change. In a library that has not been at
    rest nothing was remembered, so the change is found by reading."""
    lib = base(tmp_path / "lib")
    source = object_of(lib, "P1")
    folder = lib.root / SETS / source.object_id

    def change():
        kept = [(p, os.stat(p)) for p in (folder, folder / "revisions")]
        other_process_saves(lib, "P1")
        for p, st in kept:
            os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))

    after_first_resolve(monkeypatch, change)
    lib.copy("P1", "C1")
    assert object_of(lib, "C1").head.source == {"object_id": source.object_id, "seq": seq(2)}


def test_a_source_damaged_by_a_new_entry_during_a_copy_is_seen(tmp_path, monkeypatch):
    lib, _ = twin(tmp_path, nothing)
    folder = lib.root / SETS / object_of(lib, "P3").object_id
    after_first_resolve(monkeypatch, lambda: (folder / "junk").write_bytes(b""))
    lib.copy("P1", "C1")
    assert object_of(lib, "P1").state is S.UNKNOWN
    record = [v for k, v in tree(lib.root).items() if k.endswith("record.json") and b'"sets/C1.json"' in v]
    # as at the baseline: the second resolution finds the damage, so the source is named by its path, not as an object
    assert len(record) == 1 and b'"untracked"' in record[0]


# --------------------------------------------------------------------------- the settling rule


def objects(lib) -> int:
    return len(os.listdir(lib.root / SETS))


def cached(root) -> int:
    with h.read_cache(h.OPERATION_SETTLE_NS):
        h.scan_objects(root, T.EXPOSURE_SET)
        return len(h._READ_CACHE.get())


def test_only_objects_at_rest_are_remembered_by_an_operation(tmp_path):
    lib = base(tmp_path / "lib")
    assert cached(lib.root) == 0  # just written
    age(lib.root, 1.0)
    assert cached(lib.root) == 0  # one second is inside the settling time
    age(lib.root, 30.0)
    assert cached(lib.root) == objects(lib) >= 4
    edit(lib, "P1", "again")
    assert cached(lib.root) == objects(lib) - 1  # P1 has just changed


def test_either_folder_changed_lately_keeps_an_object_out_of_the_memory(tmp_path):
    lib = base(tmp_path / "lib")
    age(lib.root, 30.0)
    folder = lib.root / SETS / object_of(lib, "P1").object_id
    old = os.stat(folder).st_mtime_ns
    os.utime(folder / "revisions")
    assert cached(lib.root) == objects(lib) - 1
    os.utime(folder / "revisions", ns=(old, old))
    os.utime(folder)
    assert cached(lib.root) == objects(lib) - 1
    os.utime(folder, ns=(old, old))
    assert cached(lib.root) == objects(lib)


def test_a_folder_time_in_the_future_is_never_remembered(tmp_path):
    lib = base(tmp_path / "lib")
    age(lib.root, -3600.0)
    assert cached(lib.root) == 0


def test_the_batch_memory_keeps_its_own_rule(tmp_path):
    lib = base(tmp_path / "lib")
    with h.read_cache():
        assert h._READ_SETTLE.get() == 0
        h.scan_objects(lib.root, T.EXPOSURE_SET)
        assert len(h._READ_CACHE.get()) == objects(lib)  # as before R7-B: a batch remembers what it has just verified
    assert h._READ_CACHE.get() is None


def test_an_operation_inside_a_batch_starts_its_own_memory_and_gives_the_batch_back(tmp_path):
    lib = base(tmp_path / "lib")
    with h.read_cache():
        h.scan_objects(lib.root, T.EXPOSURE_SET)
        outer = h._READ_CACHE.get()
        with h.read_cache(h.OPERATION_SETTLE_NS):
            assert h._READ_CACHE.get() == {} and h._READ_SETTLE.get() == h.OPERATION_SETTLE_NS
        assert h._READ_CACHE.get() is outer and h._READ_SETTLE.get() == 0


@pytest.mark.parametrize("settle", [0, h.OPERATION_SETTLE_NS], ids=["batch", "operation"])
def test_an_object_with_an_incomplete_revision_is_never_remembered(tmp_path, settle):
    """A writer completes a reserved revision by adding files inside its folder: neither the object folder nor
    ``revisions`` changes. Such an object must be read again every time."""
    import json

    from _history_support import ID_A, canonical, make_object, objdir, record_doc
    root = tmp_path / "lib"
    make_object(root, (b"1",))
    rev = objdir(root) / "revisions" / seq(2)
    rev.mkdir()
    canonical(root, b"22")
    age(root)
    with h.read_cache(settle):
        first = h.resolve(root, T.EXPOSURE_SET, "sets/a.json")
        assert (first.state, first.claimant, first.head_seq) == (S.CHANGED_OUTSIDE, ID_A, seq(1))
        assert h._READ_CACHE.get() == {}
        (rev / "content").write_bytes(b"22")
        (rev / "record.json").write_text(json.dumps(record_doc(2, data=b"22")), encoding="utf-8")
        second = h.resolve(root, T.EXPOSURE_SET, "sets/a.json")
        assert (second.state, second.seq) == (S.TRACKED, seq(2))


@pytest.mark.parametrize("settle", [0, h.OPERATION_SETTLE_NS], ids=["batch", "operation"])
def test_a_damaged_object_is_never_remembered(tmp_path, settle):
    from _history_support import canonical, make_object, objdir
    root = tmp_path / "lib"
    make_object(root, (b"1",))
    canonical(root, b"1")
    blob = objdir(root) / "revisions" / seq(1) / "content"
    blob.write_bytes(b"2")
    age(root)
    with h.read_cache(settle):
        assert h.resolve(root, T.EXPOSURE_SET, "sets/a.json").state is S.UNKNOWN
        assert h._READ_CACHE.get() == {}
        blob.write_bytes(b"1")  # put right by hand, in place: seen at once, because damage is never remembered
        assert h.resolve(root, T.EXPOSURE_SET, "sets/a.json").state is S.TRACKED


def test_no_memory_outside_any_block(tmp_path, monkeypatch):
    lib, _ = twin(tmp_path, nothing)
    seen = reads(monkeypatch)
    object_of(lib, "P1")
    first = len(seen)
    object_of(lib, "P1")
    assert len(seen) == 2 * first and h._READ_CACHE.get() is None


# --------------------------------------------------------------------------- finalization

THREE = [{"name": "P1", "role": "part", "z_range": None}, {"name": "P2", "role": "support", "z_range": [0, 5]},
         {"name": "P3", "role": "part", "z_range": [5, 9]}]


def drafted(lib, sets=None):
    out = j.save_draft(lib, None, sets if sets is not None else THREE, MACHINE, "Al")
    assert out.status is St.DRAFT_SAVED
    return out.build_job_id


def finalizing(sets=None):
    def operation(lib):
        jid = drafted(lib, sets)
        out = j.finalize(lib, jid)
        return out, j.validate(lib, jid), j.job_state(lib, jid)
    return operation


def machine_tracked(lib):
    lib.load_machines()
    assert h.resolve(lib.root, T.MACHINE_PROFILE, lib._rel(lib._machine_source[MACHINE])).state is S.TRACKED


def machine_untracked(lib):
    lib.load_machines()
    shutil.rmtree(lib.root / ".history/objects/machine_profile")


def one_untracked(lib):
    machine_tracked(lib)
    untracked(lib, "U1")


def one_ahead(lib):
    machine_tracked(lib)
    source_history_ahead(lib)


def one_damaged(lib):
    machine_tracked(lib)
    damaged(lib)


def one_changed(lib):
    machine_tracked(lib)
    changed_outside(lib)


def process_missing(lib):
    machine_tracked(lib)
    (lib.root / "process.json").unlink()


SAME_TWICE = [{"name": "P1", "role": "part", "z_range": [0, 5]}, {"name": "P1", "role": "support", "z_range": None}]
WITH_UNTRACKED = THREE + [{"name": "U1", "role": "part", "z_range": None}]
WITH_ABSENT = THREE + [{"name": "none", "role": "part", "z_range": None}]

FINALIZATIONS = [
    ("three sets", machine_tracked, finalizing()),
    ("one set", machine_tracked, finalizing(THREE[:1])),
    ("the same set twice", machine_tracked, finalizing(SAME_TWICE)),
    ("machine untracked", machine_untracked, finalizing()),
    ("a set untracked", one_untracked, finalizing(WITH_UNTRACKED)),
    ("a set absent", machine_tracked, finalizing(WITH_ABSENT)),
    ("history ahead", one_ahead, finalizing()),
    ("a set changed outside", one_changed, finalizing()),
    ("a damaged object", one_damaged, finalizing()),
    ("no process settings", process_missing, finalizing()),
]


@pytest.mark.parametrize("build, operation", [c[1:] for c in FINALIZATIONS], ids=[c[0] for c in FINALIZATIONS])
def test_a_finalization_ends_the_same_with_and_without_its_memory(tmp_path, monkeypatch, build, operation):
    shipped, plain_run, tree_a, tree_b = both(tmp_path, monkeypatch, build, operation)
    assert shipped == plain_run  # status, reason, refused inputs, warnings, validation report, job state
    assert tree_a == tree_b  # and the manifest, byte for byte


def test_the_finalizations_compared_above_pin_and_refuse(tmp_path, monkeypatch):
    shipped, _, tree_a, _ = both(tmp_path, monkeypatch, machine_tracked, finalizing())
    assert shipped[0][0]["status"] == "finalized" and shipped[0][1]["result"] == "valid"
    manifest = [v for k, v in tree_a.items() if k.endswith("final/manifest.json")]
    assert len(manifest) == 1 and manifest[0].count(b'"object_id"') == 5
    for build, sets in ((one_untracked, WITH_UNTRACKED), (one_damaged, THREE)):
        d = tmp_path / build.__name__
        d.mkdir()
        refused, _, _, _ = both(d, monkeypatch, build, finalizing(sets))
        assert refused[0][0]["reason"] == "input_not_pinnable"


def test_a_finalization_examines_each_type_once_and_reads_every_pinned_blob_again(tmp_path, monkeypatch):
    a, b = twin(tmp_path, machine_tracked)
    jid_a, jid_b = drafted(a), drafted(b)
    seen = reads(monkeypatch)
    for t, path in ((T.EXPOSURE_SET, "sets/none.json"), (T.MACHINE_PROFILE, "machines/none.json"), (T.PROCESS_SETTINGS, "process.json")):
        h.resolve(a.root, t, path)
    one_scan_of_each = list(seen)
    del seen[:]
    assert j.finalize(a, jid_a).status is St.FINALIZED
    inputs = len(THREE) + 2
    assert len(seen) == len(one_scan_of_each) + inputs
    pinned = [f"{i['object_type']}/{i['object_id']}/revisions/{i['seq']}/content" for i in manifest_of(a, jid_a)["inputs"]]
    assert len(pinned) == inputs
    for blob in pinned:  # examined by the scan, then read again for the pin
        assert seen.count(blob) == 2
    assert sorted(set(seen)) == sorted(set(one_scan_of_each))  # nothing else, and nothing left out
    del seen[:]
    with plainly(monkeypatch):
        assert j.finalize(b, jid_b).status is St.FINALIZED
    per_type = {t: sum(1 for r in one_scan_of_each if r.startswith(t)) for t in ("exposure_set", "machine_profile", "process_settings")}
    assert len(seen) == 3 * per_type["exposure_set"] + per_type["machine_profile"] + per_type["process_settings"] + inputs


def manifest_of(lib, jid):
    import json
    return json.loads((lib.root / ".history/jobs" / jid / "final/manifest.json").read_text(encoding="utf-8"))


def test_the_pin_reads_the_blob_itself_and_refuses_one_changed_after_the_scan(tmp_path, monkeypatch):
    """The second read of a pinned blob is a read of the file, never the remembered bytes."""
    lib, _ = twin(tmp_path, machine_tracked)
    jid = drafted(lib)
    target = object_of(lib, "P2")
    blob = lib.root / SETS / target.object_id / "revisions" / target.seq / "content"
    real = j._read_blob

    def read_blob(root, object_type, object_id, seq_):
        if object_id == target.object_id:
            data = blob.read_bytes()
            blob.write_bytes(data.replace(b"second", b"SECOND"))  # same length, in place: the folder times do not change
        return real(root, object_type, object_id, seq_)

    monkeypatch.setattr(j, "_read_blob", read_blob)
    out = j.finalize(lib, jid)
    assert (out.status, out.reason) == (St.NOT_FINALIZED, "input_not_pinnable")
    assert out.refused_inputs == ({"object": "sets/P2.json", "state": "unverifiable"},)
    assert j.job_state(lib, jid) == "draft"


def test_a_pinned_blob_that_cannot_be_read_again_is_refused(tmp_path, monkeypatch):
    lib, _ = twin(tmp_path, machine_tracked)
    jid = drafted(lib)

    def read_blob(*args):
        raise OSError(5, "injected")

    monkeypatch.setattr(j, "_read_blob", read_blob)
    out = j.finalize(lib, jid)
    assert out.reason == "input_not_pinnable" and len(out.refused_inputs) == 5


def after_first_pin(monkeypatch, action):
    real = j._pin
    calls = []

    def pin(*args):
        if len(calls) == 1:
            action()
        calls.append(args[2])
        return real(*args)

    monkeypatch.setattr(j, "_pin", pin)
    return calls


def test_a_revision_added_by_another_process_during_a_finalization_is_seen(tmp_path, monkeypatch):
    lib, _ = twin(tmp_path, machine_tracked)
    jid = drafted(lib)
    assert object_of(lib, "P3").seq == seq(1)
    after_first_pin(monkeypatch, lambda: other_process_saves(lib, "P3"))
    assert j.finalize(lib, jid).status is St.FINALIZED
    pins = {i["path"]: i for i in manifest_of(lib, jid)["inputs"]}
    now = object_of(lib, "P3")
    assert now.seq == seq(2) and pins["sets/P3.json"]["seq"] == seq(2)
    assert pins["sets/P3.json"]["content_hash"]["value"] == h.sha256(lib._file("P3").read_bytes())
    assert j.validate(lib, jid).result is j.ManifestResult.VALID


def test_that_revision_is_seen_by_a_finalization_even_if_the_folder_times_are_put_back(tmp_path, monkeypatch):
    lib = base(tmp_path / "lib")
    jid = drafted(lib)
    folder = lib.root / SETS / object_of(lib, "P3").object_id

    def change():
        kept = [(p, os.stat(p)) for p in (folder, folder / "revisions")]
        other_process_saves(lib, "P3")
        for p, st in kept:
            os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))

    after_first_pin(monkeypatch, change)
    assert j.finalize(lib, jid).status is St.FINALIZED
    pins = {i["path"]: i for i in manifest_of(lib, jid)["inputs"]}
    assert pins["sets/P3.json"]["seq"] == seq(2)


def test_a_finalization_in_a_library_just_written_reads_everything_each_time(tmp_path, monkeypatch):
    lib = base(tmp_path / "lib")
    jid = drafted(lib)
    seen = reads(monkeypatch)
    object_of(lib, "P1")
    one_scan = len(seen)
    del seen[:]
    assert j.finalize(lib, jid).status is St.FINALIZED
    assert len([r for r in seen if r.startswith("exposure_set")]) == 3 * one_scan + 3


def test_damage_added_by_another_process_during_a_finalization_refuses_the_rest(tmp_path, monkeypatch):
    lib, _ = twin(tmp_path, machine_tracked)
    jid = drafted(lib)
    folder = lib.root / SETS / object_of(lib, "P1").object_id
    after_first_pin(monkeypatch, lambda: (folder / "junk").write_bytes(b""))
    out = j.finalize(lib, jid)
    assert out.reason == "input_not_pinnable"
    assert out.refused_inputs == ({"object": "sets/P2.json", "state": "unknown"}, {"object": "sets/P3.json", "state": "unknown"})


def test_the_memory_of_a_finalization_ends_before_the_manifest_is_reserved(tmp_path, monkeypatch):
    lib, _ = twin(tmp_path, machine_tracked)
    jid = drafted(lib)
    during: dict[str, bool] = {}
    monkeypatch.setattr(h, "_fault", lambda step: during.setdefault(step, h._READ_CACHE.get() is not None))
    assert j.finalize(lib, jid).status is St.FINALIZED
    assert during["scan.read"]
    for step in ("J3.mkdir", "J4.temp", "J4.replace", "J4.dirfsync", "J5.read"):
        assert during[step] is False, step
    assert h._READ_CACHE.get() is None and h._READ_SETTLE.get() == 0


@pytest.mark.parametrize("build, sets", [(machine_untracked, THREE), (one_untracked, WITH_UNTRACKED), (one_damaged, THREE)],
                         ids=["machine untracked", "set untracked", "damaged"])
def test_the_memory_of_a_refused_finalization_does_not_survive(tmp_path, build, sets):
    lib, _ = twin(tmp_path, build)
    assert j.finalize(lib, drafted(lib, sets)).status is St.NOT_FINALIZED
    assert h._READ_CACHE.get() is None and h._READ_SETTLE.get() == 0


def test_validation_keeps_no_memory_and_reads_every_pinned_blob(tmp_path, monkeypatch):
    lib, _ = twin(tmp_path, machine_tracked)
    jid = drafted(lib)
    assert j.finalize(lib, jid).status is St.FINALIZED
    during: list[bool] = []
    monkeypatch.setattr(h, "_fault", lambda step: during.append(h._READ_CACHE.get() is not None))
    assert j.validate(lib, jid).result is j.ManifestResult.VALID
    assert during and not any(during)
