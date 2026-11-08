"""ABP-0.4: machine profiles and process settings through history (DC-001 v0.6 §7.9 and §8).

The same steps and the same outcome model as sets, but uncoordinated on every platform: B2a keeps these writers outside
its locks and that list is not widened. Exclusive ``mkdir`` and exclusive create still apply, and the canonical file is
last-writer-wins.
"""

import errno
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
from lpbfstudio import history as h
from lpbfstudio import library as libmod
from lpbfstudio.history import ObjectState as O
from lpbfstudio.history import ObjectType as T
from lpbfstudio.history import PathState as S
from lpbfstudio.library import Library, LibraryError
from lpbfstudio.models import MachineProfile, ProcessSettings
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
SRC = str(Path(libmod.__file__).resolve().parents[1])
MA, PR = T.MACHINE_PROFILE, T.PROCESS_SETTINGS
PROCESS = "process.json"


@pytest.fixture(params=sorted(ROOTS))
def lib(request):
    d = Path(tempfile.mkdtemp(prefix="histprof-", dir=ROOTS[request.param]))
    try:
        yield Library(d / "lib", seed=False)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def machine(name="M1", **kw):
    return MachineProfile(name=name, **kw)


def process(**kw):
    return ProcessSettings(**kw)


def objects(lib, otype):
    return {o.object_id: o for o in h.scan_objects(lib.root, otype)}


def resolved(lib, otype, path):
    return h.resolve(lib.root, otype, path)


def head_of(lib, otype, path):
    r = resolved(lib, otype, path)
    assert r.claimant, r
    return objects(lib, otype)[r.claimant].head


def plain_bytes(tmp, text):
    f = tmp / "control.json"
    libmod._atomic_write(f, text)
    return f.read_bytes()


def fail_at(monkeypatch, step, exc=None):
    def fault(s):
        if s == step:
            raise (exc or OSError(errno.EIO, "injected"))

    monkeypatch.setattr(h, "_fault", fault)


def hist(lib):
    d = lib.root / ".history"
    return tree(d) if d.exists() else {}


# --------------------------------------------------------------------------- process settings


def test_first_process_save_creates_the_one_object(lib):
    with lib.collect_outcomes() as sink:
        assert lib.save_process(process()) is None
    o = sink.last
    assert len(sink) == 1 and o.complete and (o.operation, o.path, o.seq) == ("save", PROCESS, seq(1))
    assert head_of(lib, PR, PROCESS).operation == "created"
    assert len(objects(lib, PR)) == 1


def test_a_placed_process_file_is_adopted_with_its_exact_bytes(lib):
    placed = json.dumps(json.loads(process().model_dump_json()), indent=7).encode()
    (lib.root / PROCESS).write_bytes(placed)
    with lib.collect_outcomes() as sink:
        lib.save_process(process())
    o = sink.last
    assert o.complete and (o.adoption, o.adopted_seq, o.seq) == (A.COMMITTED, seq(1), seq(2))
    blob = lib.root / ".history/objects/process_settings" / o.object_id / "revisions" / seq(1) / "content"
    assert blob.read_bytes() == placed


def test_process_saves_append_to_one_object(lib):
    lib.save_process(process())
    oid = resolved(lib, PR, PROCESS).object_id
    with lib.collect_outcomes() as sink:
        lib.save_process(process(**{next(iter(_a_float_field(ProcessSettings))): 1.5}))
    assert sink.last.complete and sink.last.object_id == oid and sink.last.seq == seq(2)
    assert head_of(lib, PR, PROCESS).source == {"object_id": oid, "seq": seq(1)}


def _a_float_field(model):
    return {name: f for name, f in model.model_fields.items() if f.annotation is float}


def test_process_bytes_are_unchanged_by_history(lib, tmp_path):
    ps = process()
    lib.save_process(ps)
    assert (lib.root / PROCESS).read_bytes() == plain_bytes(tmp_path, ps.model_dump_json(indent=2))


# --------------------------------------------------------------------------- machine profiles


def test_new_profile_is_created(lib):
    with lib.collect_outcomes() as sink:
        assert lib.save_machine(machine()) is None
    o = sink.last
    assert o.complete and (o.operation, o.path, o.seq) == ("save", "machines/M1.json", seq(1))
    assert head_of(lib, MA, "machines/M1.json").operation == "created"


def test_placed_profile_is_adopted(lib):
    lib.machines_dir.mkdir()
    placed = machine().model_dump_json(indent=5).encode()
    (lib.machines_dir / "M1.json").write_bytes(placed)
    with lib.collect_outcomes() as sink:
        lib.save_machine(machine())
    assert sink.last.complete and sink.last.adoption is A.COMMITTED and sink.last.seq == seq(2)


def test_saving_again_appends(lib):
    lib.save_machine(machine())
    with lib.collect_outcomes() as sink:
        lib.save_machine(machine(max_power_w=350.0))
    assert sink.last.complete and sink.last.seq == seq(2)


def test_legacy_machine_file_keeps_its_path(lib):
    (lib.root / "machine.json").write_text(machine("Old").model_dump_json(indent=2), encoding="utf-8")
    with lib.collect_outcomes() as sink:
        lib.save_machine(machine("Old", max_power_w=300.0))
    assert sink.last.complete and sink.last.path == "machine.json"
    assert resolved(lib, MA, "machine.json").state is S.TRACKED
    assert not lib.machines_dir.exists()  # the writer leaves the legacy file in place, as it always has


def test_profile_bytes_are_unchanged_by_history(lib, tmp_path):
    mp = machine()
    lib.save_machine(mp)
    assert (lib.machines_dir / "M1.json").read_bytes() == plain_bytes(tmp_path, mp.model_dump_json(indent=2))


def test_seeded_default_profile_is_created_as_one_object(tmp_path):
    lib = Library(tmp_path / "lib", seed=False)
    with lib.collect_outcomes() as sink:
        lib.ensure_default_machine()
    assert len(sink) == 1 and sink.last.complete and sink.last.operation == "create"
    assert head_of(lib, MA, sink.last.path).operation == "created"


def test_seeding_when_a_profile_exists_writes_nothing(lib):
    lib.save_machine(machine())
    before = hist(lib)
    with lib.collect_outcomes() as sink:
        lib.ensure_default_machine()
    assert len(sink) == 0 and hist(lib) == before


def test_import_is_a_new_object_naming_the_external_file(lib, tmp_path):
    f = tmp_path / "m.json"
    raw = machine("I1").model_dump_json(indent=2).encode()
    f.write_bytes(raw)
    with lib.collect_outcomes() as sink:
        mp = lib.import_machine(f)
    assert mp.name == "I1" and len(sink) == 1 and sink.last.complete and sink.last.operation == "import"
    head = head_of(lib, MA, "machines/I1.json")
    assert head.operation == "imported"
    assert head.source == {"external_path": str(f), "content_hash": {"algorithm": "sha256", "value": sha(raw)}}


def test_import_overwrite_of_an_existing_profile_is_not_recorded(lib, tmp_path):
    lib.save_machine(machine("I2"))
    before = hist(lib)
    f = tmp_path / "m.json"
    f.write_text(machine("I2", max_power_w=321.0).model_dump_json(indent=2), encoding="utf-8")
    with lib.collect_outcomes() as sink:
        lib.import_machine(f, overwrite=True)
    o = sink.last
    assert (o.canonical, o.history, o.history_reason) == (C.DONE, H.NOT_RECORDED, HR.IMPORT_OVERWRITE_UNDEFINED)
    assert hist(lib) == before and lib.get_machine("I2").max_power_w == 321.0


def test_import_of_a_bad_file_is_refused_with_one_outcome(lib, tmp_path):
    f = tmp_path / "m.json"
    f.write_text("{", encoding="utf-8")
    with lib.collect_outcomes() as sink:
        with pytest.raises(ValueError):
            lib.import_machine(f)
    assert len(sink) == 1 and (sink.last.canonical, sink.last.history, sink.last.operation) == (C.NOT_DONE, H.NOT_APPLICABLE, "import")


def test_refusals_in_phase_p(lib):
    lib.save_machine(machine())
    before = hist(lib)
    with lib.collect_outcomes() as sink:
        with pytest.raises(LibraryError) as e:
            lib.save_machine(machine(), overwrite=False)
    o = sink.last
    assert (o.canonical, o.canonical_reason, o.readback, o.history) == (C.NOT_DONE, CR.PRECONDITION_REFUSED, R.NOT_PERFORMED, H.NOT_APPLICABLE)
    assert o.canonical_error == str(e.value) and hist(lib) == before


def test_a_name_held_by_another_profile_is_refused_in_phase_p(lib):
    lib.machines_dir.mkdir()
    (lib.machines_dir / "M_1.json").write_text(machine("other").model_dump_json(indent=2), encoding="utf-8")
    with lib.collect_outcomes() as sink:
        with pytest.raises(LibraryError):
            lib.save_machine(machine("M 1"))
    assert sink.last.history is H.NOT_APPLICABLE


# --------------------------------------------------------------------------- D-7, on both writers


STEPS = ["root.mkdir", "A1.read", "A2.mkdir", "A3.temp", "A3.fsync", "A3.replace", "A3.dirfsync", "A4.mkdir_revisions", "A4.mkdir", "A5.write",
         "A5.fsync", "A5.verify", "A6.temp", "A6.fsync", "A6.replace", "A6.dirfsync", "A7.read", "S3.write", "S3.read", "S4.mkdir", "S5.write",
         "S5.fsync", "S5.verify", "S6.temp", "S6.fsync", "S6.replace", "S6.dirfsync", "S9.unlink"]


@pytest.mark.parametrize("step", STEPS)
def test_no_history_failure_changes_a_process_save(lib, tmp_path, monkeypatch, step):
    (lib.root / PROCESS).write_text(process().model_dump_json(indent=4), encoding="utf-8")
    ps = process()
    fail_at(monkeypatch, step)
    with lib.collect_outcomes() as sink:
        assert lib.save_process(ps) is None
    assert sink.last.canonical is C.DONE
    assert (lib.root / PROCESS).read_bytes() == plain_bytes(tmp_path, ps.model_dump_json(indent=2))


@pytest.mark.parametrize("step", STEPS)
def test_no_history_failure_changes_a_profile_save(lib, tmp_path, monkeypatch, step):
    lib.machines_dir.mkdir()
    (lib.machines_dir / "M1.json").write_text(machine().model_dump_json(indent=4), encoding="utf-8")
    mp = machine(max_power_w=222.0)
    fail_at(monkeypatch, step)
    with lib.collect_outcomes() as sink:
        assert lib.save_machine(mp) is None
    assert sink.last.canonical is C.DONE
    assert (lib.machines_dir / "M1.json").read_bytes() == plain_bytes(tmp_path, mp.model_dump_json(indent=2))


def test_canonical_failure_keeps_its_exception(lib, monkeypatch):
    real = libmod._atomic_write
    boom = OSError(errno.ENOSPC, "No space left on device")

    def fails(path, text):
        if Path(path).name == PROCESS:
            raise boom
        real(path, text)

    monkeypatch.setattr(libmod, "_atomic_write", fails)
    with lib.collect_outcomes() as sink:
        with pytest.raises(OSError) as e:
            lib.save_process(process())
    assert e.value is boom and (sink.last.canonical, sink.last.readback, sink.last.history) == (C.NOT_DONE, R.ABSENT, H.RECORDED_NOT_CURRENT)


def test_damaged_machine_history_does_not_stop_a_process_save(lib):
    d = lib.root / ".history/objects/machine_profile" / ("a" * 32)
    d.mkdir(parents=True)
    (d / "junk").write_bytes(b"")
    with lib.collect_outcomes() as sink:
        lib.save_process(process())
    assert sink.last.complete


# --------------------------------------------------------------------------- uncoordinated, as §7.9 defines


@posix
@pytest.mark.parametrize("call", ["process", "machine"])
def test_these_writers_take_no_lock(lib, monkeypatch, call):
    """B2a's writer list is not widened: an exclusive lock on the sets folder stays free throughout phase H."""
    (lib.sets_dir / ".format1-backup").mkdir()
    free = []

    def probe(step):
        if step in ("S3.write", "S6.replace"):
            fd = os.open(lib.sets_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
                free.append(True)
            except BlockingIOError:
                free.append(False)
            finally:
                os.close(fd)

    monkeypatch.setattr(h, "_fault", probe)
    lib.save_process(process()) if call == "process" else lib.save_machine(machine())
    assert free == [True, True]


def test_two_racing_adoptions_make_two_claims_and_resolve_unknown(lib, monkeypatch):
    """§12.4 and §7.9: uncoordinated writers can both adopt; the result is a defined anomaly, never a merge."""
    (lib.root / PROCESS).write_text(process().model_dump_json(indent=4), encoding="utf-8")
    ran = []

    def other(step):
        if step == "A6.temp" and not ran:
            ran.append(True)
            monkeypatch.setattr(h, "_fault", None)
            lib.save_process(process())
            monkeypatch.setattr(h, "_fault", other)

    monkeypatch.setattr(h, "_fault", other)
    with lib.collect_outcomes() as sink:
        lib.save_process(process())
    assert ran and sink.outcomes[0].canonical is C.DONE
    assert resolved(lib, PR, PROCESS).state is S.UNKNOWN
    assert all(o.state is O.D3 for o in objects(lib, PR).values())


def test_racing_saves_take_distinct_numbers_and_the_canonical_file_is_last_writer_wins(lib, monkeypatch):
    lib.save_machine(machine())
    ran = []
    second = machine(max_power_w=111.0)

    def other(step):
        if step == "S4.mkdir" and not ran:
            ran.append(True)
            monkeypatch.setattr(h, "_fault", None)
            lib.save_machine(second)
            monkeypatch.setattr(h, "_fault", other)

    monkeypatch.setattr(h, "_fault", other)
    first = machine(max_power_w=222.0)
    with lib.collect_outcomes() as sink:
        lib.save_machine(first)
    outcomes = list(sink)
    assert sorted(o.seq for o in outcomes) == [seq(2), seq(3)]
    assert lib.get_machine("M1").max_power_w == 222.0  # the outer save replaced the file last
    assert all(o.complete for o in outcomes[-1:])


# --------------------------------------------------------------------------- kills


KILL = textwrap.dedent("""
    import os, sys
    from lpbfstudio import history as h, library as L
    from lpbfstudio.models import ProcessSettings
    root, step = sys.argv[1], sys.argv[2]
    lib = L.Library(root, seed=False)
    def fault(s):
        if s == step:
            os._exit(9)
    h._fault = fault
    lib.save_process(ProcessSettings())
    os._exit(0)
""")


@pytest.mark.parametrize("step", ["A3.temp", "A5.write", "A6.replace", "A7.read", "S3.write", "S5.write", "S6.replace", "S6.dirfsync", "V.read", "S9.unlink"])
def test_a_kill_during_a_process_save_leaves_a_defined_state(lib, step):
    placed = process().model_dump_json(indent=7).encode()
    (lib.root / PROCESS).write_bytes(placed)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([SRC, os.environ.get("PYTHONPATH", "")]))
    p = subprocess.run([sys.executable, "-c", KILL, str(lib.root), step], env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode == 9, p.stderr
    assert all(o.state is not O.D5 for o in h.scan_objects(lib.root, PR))
    assert resolved(lib, PR, PROCESS).state in (S.UNTRACKED, S.TRACKED)
    with lib.collect_outcomes() as sink:
        lib.save_process(process())
    assert sink.last.canonical is C.DONE
