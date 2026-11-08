"""HX-1, the explicit rebaseline (DC-001 v0.6 §7.12).

Two layers. The evidence and failure rules run on every platform, with the frozen report and the B7 records replaced by
recorded values. Real Upgrade and Restore flows run on Linux, on ext4 and tmpfs. There is no ``external`` mode, and no CLI
or GUI entry point (Q4-L2).
"""

import ast
import errno
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from _history_support import ID_A, make_object, seq, sha, tree
from lpbfstudio import history as h
from lpbfstudio import reconcile as rc
from lpbfstudio import rebaseline as rb
from lpbfstudio import restore as rs
from lpbfstudio import upgrade as up
from lpbfstudio.defaults import default_sets
from lpbfstudio.history import ObjectType as T
from lpbfstudio.history import PathState as S
from lpbfstudio.library import Library
from lpbfstudio.migration import RecordStatus
from lpbfstudio.outcomes import InvalidOutcome
from lpbfstudio.outcomes import RebaselineStatus as St
from lpbfstudio.restore_record import RestoreStatus

posix = pytest.mark.skipif(sys.platform == "win32", reason="the frozen report, upgrade and restore are refused on Windows")
ROOTS = {"ext4": "/tmp", "tmpfs": "/dev/shm"} if sys.platform != "win32" else {"ntfs": None}
RID, RSID = "1" * 32, "2" * 32
F = "F1.json"
P = f"sets/{F}"
SRC = str(Path(rb.__file__).resolve().parents[1])
M = up.MIGRATION_DIR


def set_text(name="F1", version=1, description="d", indent=2):
    data = json.loads(default_sets()[0].model_dump_json())
    data.update(name=name, locked=False, schema_version=version, description=description)
    return json.dumps(data, indent=indent)


# --------------------------------------------------------------------------- evidence rules, every platform


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A library whose tracked set has been replaced, with a report and record saying an upgrade did it."""
    lib = Library(tmp_path / "lib", seed=False)
    old, new = b'{"old": 1}', b'{"new": 2}'
    f = lib.sets_dir / F
    hr = h.phase_h_write(lib.root, T.EXPOSURE_SET, P, "x", new_operation="created")  # a tracked object at this path
    oid = hr.object_id
    folder = h._object_folder(hr)
    # its head holds the bytes the record calls the source
    rev2 = folder / "revisions" / seq(2)
    rev2.mkdir()
    (rev2 / "content").write_bytes(old)
    (rev2 / "record.json").write_text(json.dumps(h.build_record("exposure_set", oid, seq(2), "saved", P, data=old,
                                                                source={"object_id": oid, "seq": seq(1)})), encoding="utf-8")
    f.write_bytes(new)
    w = SimpleNamespace(lib=lib, oid=oid, old=old, new=new, f=f)
    w.report = rc.ReconcileOutcome(F, rc.Outcome.VERIFIED_UPGRADED, "P12", "ok", record_id=RID,
                                   source=rc.FileEvidence(F, "regular", len(new), sha(new), 1))
    w.record = SimpleNamespace(record_id=RID, status=RecordStatus.UPGRADED, source_path=F, source_sha256=sha(old), source_size_bytes=len(old),
                               upgraded_sha256=sha(new), upgraded_size_bytes=len(new))
    w.restore = SimpleNamespace(restore_id=RSID, upgrade_record_id=RID, status=RestoreStatus.RESTORED, source_path=F, current_sha256=sha(old),
                                current_size_bytes=len(old), restored_sha256=sha(new), restored_size_bytes=len(new))
    monkeypatch.setattr(rc, "_platform_block", lambda: None)
    monkeypatch.setattr(rc, "reconcile_file", lambda sets, name, **k: w.report)
    monkeypatch.setattr(rb, "_read_upgrade_record", lambda sets, rid: w.record)
    monkeypatch.setattr(rb, "_read_restore_record", lambda sets, rid: w.restore)
    return w


def run(w, mode="upgrade", name=F, oid=None, expected=None):
    return rb.rebaseline_set(w.lib, name, mode, oid or w.oid, expected or sha(w.new))


def restore_world(w):
    w.report = rc.ReconcileOutcome(F, rc.Outcome.VERIFIED_RESTORED, "P20R", "ok", record_id=RID, restore_id=RSID,
                                   source=rc.FileEvidence(F, "regular", len(w.new), sha(w.new), 1))


def test_upgrade_mode_commits_a_revision_of_the_exact_bytes(world):
    before = world.f.read_bytes()
    out = run(world)
    assert out.complete and (out.status, out.seq, out.object_id, out.path) == (St.RECORDED, seq(3), world.oid, P)
    assert world.f.read_bytes() == before
    r = h.resolve(world.lib.root, T.EXPOSURE_SET, P)
    assert (r.state, r.seq, r.history_ahead) == (S.TRACKED, seq(3), False)
    head = {o.object_id: o for o in h.scan_objects(world.lib.root, T.EXPOSURE_SET)}[world.oid].head
    assert head.operation == "rebaselined" and head.kind == "content"
    assert head.source == {"kind": "upgrade", "record_id": RID, "restore_id": None, "prior_head_seq": seq(2)}
    blob = h._object_folder(SimpleNamespace(root=world.lib.root, object_type=T.EXPOSURE_SET, object_id=world.oid)) / "revisions" / seq(3) / "content"
    assert blob.read_bytes() == world.new


def test_restore_mode(world):
    restore_world(world)
    out = run(world, "restore")
    assert out.complete
    head = {o.object_id: o for o in h.scan_objects(world.lib.root, T.EXPOSURE_SET)}[world.oid].head
    assert head.source == {"kind": "restore", "record_id": RID, "restore_id": RSID, "prior_head_seq": seq(2)}


def refused(world, reason, **kw):
    before = tree(world.lib.root)
    out = run(world, **kw)
    assert (out.status, out.reason, out.seq) == (St.REFUSED, reason, None), out
    assert tree(world.lib.root) == before  # nothing is written
    return out


@pytest.mark.parametrize("mode", ["external", "assert", "", None])
def test_there_is_no_other_mode(world, mode):
    before = tree(world.lib.root)
    with pytest.raises(ValueError):
        rb.rebaseline_set(world.lib, F, mode, world.oid, sha(world.new))
    assert tree(world.lib.root) == before


def test_platform_gate(world, monkeypatch):
    monkeypatch.setattr(rc, "_platform_block", lambda: object())
    refused(world, "rebaseline_platform")


@pytest.mark.parametrize("kw", [{"name": "../x.json"}, {"oid": "short"}, {"expected": "abc"}, {"oid": "A" * 32}])
def test_bad_arguments_refuse(world, kw):
    refused(world, "rebaseline_arguments", **kw)


@pytest.mark.parametrize("outcome,rule", [
    (rc.Outcome.VERIFIED_UPGRADED, "P14"), (rc.Outcome.VERIFIED_RESTORED, "P20R"), (rc.Outcome.CONFLICT, "P24"), (rc.Outcome.CONFLICT, "P12"),
    (rc.Outcome.RECOVERABLE, "P18"), (rc.Outcome.UNVERIFIABLE, "G6"), (rc.Outcome.BUSY, "G4"), (rc.Outcome.VERIFIED_ORIGINAL, "P1")])
def test_upgrade_mode_refuses_any_other_report(world, outcome, rule):
    world.report = rc.ReconcileOutcome(F, outcome, rule, "no", record_id=RID, source=world.report.source)
    refused(world, "rebaseline_report")


@pytest.mark.parametrize("outcome,rule", [
    (rc.Outcome.VERIFIED_RESTORED, "P22"), (rc.Outcome.VERIFIED_UPGRADED, "P12"), (rc.Outcome.RECOVERABLE, "P20"), (rc.Outcome.UNVERIFIABLE, "P21"),
    (rc.Outcome.UNVERIFIABLE, "G6")])
def test_restore_mode_refuses_any_other_report(world, outcome, rule):
    world.report = rc.ReconcileOutcome(F, outcome, rule, "no", record_id=RID, restore_id=RSID, source=world.report.source)
    refused(world, "rebaseline_report", mode="restore")


def test_p24_is_accepted_for_upgrade(world):
    world.report = rc.ReconcileOutcome(F, rc.Outcome.VERIFIED_UPGRADED, "P24", "ok", record_id=RID, source=world.report.source)
    assert run(world).complete


def test_report_without_a_record_id_refuses(world):
    world.report = rc.ReconcileOutcome(F, rc.Outcome.VERIFIED_UPGRADED, "P12", "ok", source=world.report.source)
    refused(world, "rebaseline_record")


def test_report_without_a_restore_id_refuses(world):
    restore_world(world)
    world.report = rc.ReconcileOutcome(F, rc.Outcome.VERIFIED_RESTORED, "P20R", "ok", record_id=RID, source=world.report.source)
    refused(world, "rebaseline_record", mode="restore")


def test_unreadable_record_refuses(world, monkeypatch):
    def boom(*a):
        raise OSError(errno.EIO, "x")

    monkeypatch.setattr(rb, "_read_upgrade_record", boom)
    out = refused(world, "rebaseline_record")
    assert out.errno == errno.EIO


def test_malformed_record_refuses(world, monkeypatch):
    def bad(*a):
        raise ValueError("bad record")

    monkeypatch.setattr(rb, "_read_upgrade_record", bad)
    refused(world, "rebaseline_record")


@pytest.mark.parametrize("field,value", [("status", RecordStatus.FAILED), ("source_path", "other.json"), ("upgraded_sha256", None)])
def test_upgrade_record_rules(world, field, value):
    setattr(world.record, field, value)
    refused(world, "rebaseline_record")


@pytest.mark.parametrize("field,value", [("status", RestoreStatus.UNCERTAIN), ("source_path", "other.json"), ("upgrade_record_id", "9" * 32)])
def test_restore_record_rules(world, field, value):
    restore_world(world)
    setattr(world.restore, field, value)
    refused(world, "rebaseline_record", mode="restore")


def test_report_observed_other_bytes_refuses(world):
    world.report = rc.ReconcileOutcome(F, rc.Outcome.VERIFIED_UPGRADED, "P12", "ok", record_id=RID,
                                       source=rc.FileEvidence(F, "regular", 1, sha(b"z"), 1))
    refused(world, "rebaseline_bytes")


def test_expected_hash_must_match(world):
    refused(world, "rebaseline_expected_hash", expected=sha(b"something else"))


def test_upgraded_bytes_must_match_the_record(world):
    world.record.upgraded_sha256 = sha(b"zzz")
    refused(world, "rebaseline_bytes")


def test_upgraded_length_must_match_the_record(world):
    world.record.upgraded_size_bytes = 1
    refused(world, "rebaseline_bytes")


def test_bytes_changed_after_the_report_refuse(world):
    """The report ran before the writer locks; under them the file is compared again."""
    world.f.write_bytes(b"replaced after the report")
    refused(world, "rebaseline_bytes", expected=sha(b"replaced after the report"))


def test_prior_head_hash_must_match_the_source(world):
    world.record.source_sha256 = sha(b"not the head")
    refused(world, "rebaseline_head")


def test_prior_head_length_must_match_the_source(world):
    world.record.source_size_bytes = 99
    refused(world, "rebaseline_head")


def test_restore_prior_head_must_match(world):
    restore_world(world)
    world.restore.current_sha256 = sha(b"not the head")
    refused(world, "rebaseline_head", mode="restore")


def test_restored_bytes_must_match(world):
    restore_world(world)
    world.restore.restored_sha256 = sha(b"zzz")
    refused(world, "rebaseline_bytes", mode="restore")


def test_object_id_must_be_the_claimant(world):
    refused(world, "rebaseline_state", oid="9" * 32)


def test_only_row_5_is_accepted(world):
    world.f.write_bytes(world.old)  # now row 4, tracked
    world.record.upgraded_sha256, world.record.upgraded_size_bytes = sha(world.old), len(world.old)
    world.report = rc.ReconcileOutcome(F, rc.Outcome.VERIFIED_UPGRADED, "P12", "ok", record_id=RID,
                                       source=rc.FileEvidence(F, "regular", len(world.old), sha(world.old), 1))
    refused(world, "rebaseline_state", expected=sha(world.old))


def test_absent_file_refuses(world):
    world.f.unlink()
    refused(world, "rebaseline_state")


def test_damaged_history_refuses(world):
    (h._object_folder(SimpleNamespace(root=world.lib.root, object_type=T.EXPOSURE_SET, object_id=world.oid)) / "junk").write_bytes(b"")
    refused(world, "rebaseline_state")


def test_a_lock_refusal_refuses(world, monkeypatch):
    from lpbfstudio import library as libmod
    from lpbfstudio import locks

    def locked(self):
        raise libmod.LibraryLocked("busy", locks.Refusal.BUSY)

    class Ctx:
        def __enter__(self):
            locked(None)

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(Library, "_writer_locks", lambda self: Ctx())
    refused(world, "rebaseline_locked")


@pytest.mark.parametrize("step,status,reason", [
    ("B6.mkdir", St.NOT_RECORDED, "revision_reservation_failed"),
    ("B7.write", St.INCOMPLETE, "blob_write_failed"), ("B7.fsync", St.INCOMPLETE, "blob_write_failed"),
    ("B7.verify", St.INCOMPLETE, "blob_verify_failed"),
    ("B8.temp", St.INCOMPLETE, "record_temp_write_failed"), ("B8.fsync", St.INCOMPLETE, "record_temp_write_failed"),
    ("B8.replace", St.INCOMPLETE, "record_commit_failed")])
def test_write_failures_keep_row_5_and_a_later_attempt_takes_the_next_number(world, monkeypatch, step, status, reason):
    def fault(s):
        if s == step:
            raise OSError(errno.EIO, "injected")

    monkeypatch.setattr(h, "_fault", fault)
    out = run(world)
    assert (out.status, out.reason) == (status, reason) and not out.complete
    assert h.resolve(world.lib.root, T.EXPOSURE_SET, P).state is S.CHANGED_OUTSIDE
    monkeypatch.setattr(h, "_fault", None)
    again = run(world)
    assert again.complete and again.seq == seq(4 if status is St.INCOMPLETE else 3)


def test_durability_failure_is_committed_and_not_complete(world, monkeypatch):
    def fault(s):
        if s == "B8.dirfsync":
            raise OSError(errno.EIO, "injected")

    monkeypatch.setattr(h, "_fault", fault)
    out = run(world)
    assert (out.status, out.reason, out.errno) == (St.RECORDED, "record_durability_failed", errno.EIO) and not out.complete


def test_post_commit_change_is_reported(world, monkeypatch):
    def fault(s):
        if s == "B9.read":
            world.f.write_bytes(b"changed afterwards")

    monkeypatch.setattr(h, "_fault", fault)
    out = run(world)
    assert out.status is St.RECORDED_NOT_CURRENT and out.seq == seq(3) and not out.complete


def test_post_commit_absence_is_reported(world, monkeypatch):
    def fault(s):
        if s == "B9.read":
            world.f.unlink()

    monkeypatch.setattr(h, "_fault", fault)
    assert run(world).status is St.RECORDED_NOT_CURRENT


def test_post_commit_read_failure_is_reported(world, monkeypatch):
    def fault(s):
        if s == "B9.read":
            raise OSError(errno.EIO, "injected")

    monkeypatch.setattr(h, "_fault", fault)
    out = run(world)
    assert out.status is St.RECORDED_UNVERIFIED and not out.complete


def test_the_canonical_file_is_never_opened_for_writing(world, monkeypatch):
    calls = []
    from lpbfstudio import library as libmod
    monkeypatch.setattr(libmod, "_atomic_write", lambda *a: calls.append(a))
    assert run(world).complete and calls == []


def test_outcome_external_cannot_be_built():
    from lpbfstudio.outcomes import RebaselineOutcome
    with pytest.raises(InvalidOutcome):
        RebaselineOutcome("external", P, None, None, St.REFUSED, "x", None, "")


def test_no_user_facing_entry_point():
    root = Path(SRC) / "lpbfstudio"
    for f in ("cli.py", "gui/app.py", "gui/dialogs.py", "gui/forms.py", "library.py", "doe.py"):
        names = {n.name for node in ast.walk(ast.parse((root / f).read_text(encoding="utf-8")))
                 if isinstance(node, (ast.Import, ast.ImportFrom)) for n in node.names}
        assert "rebaseline" not in names and not any("rebaseline" in n for n in names), f


def test_windows_is_refused_with_the_real_gate(tmp_path):
    if sys.platform != "win32":
        pytest.skip("Windows only")
    lib = Library(tmp_path / "lib", seed=False)
    (lib.sets_dir / F).write_text(set_text(), encoding="utf-8")
    before = tree(lib.root)
    out = rb.rebaseline_set(lib, F, "upgrade", "a" * 32, "0" * 64)
    assert (out.status, out.reason) == (St.REFUSED, "rebaseline_platform") and tree(lib.root) == before


# --------------------------------------------------------------------------- real flows, Linux


@pytest.fixture(params=sorted(ROOTS))
def lib(request):
    d = Path(tempfile.mkdtemp(prefix="hx1-", dir=ROOTS[request.param]))
    try:
        yield Library(d / "lib")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def resolved(lib, name=P):
    return h.resolve(lib.root, T.EXPOSURE_SET, name)


def migration_tree(lib):
    d = lib.sets_dir / M
    return tree(d) if d.exists() else {}


def tracked_format1(lib):
    (lib.sets_dir / F).write_text(set_text(), encoding="utf-8")
    es = lib.get("F1")
    es.description = "tracked before the upgrade"
    lib.save(es)
    return resolved(lib)


@posix
def test_real_upgrade_then_rebaseline(lib):
    before = tracked_format1(lib)
    assert up.upgrade_file(lib.sets_dir, F).result.value == "upgraded"
    assert resolved(lib).state is S.CHANGED_OUTSIDE
    data = (lib.sets_dir / F).read_bytes()
    st = os.stat(lib.sets_dir / F)
    evidence = migration_tree(lib)
    out = rb.rebaseline_set(lib, F, "upgrade", before.object_id, sha(data))
    assert out.complete, out
    r = resolved(lib)
    assert (r.state, r.object_id, r.seq) == (S.TRACKED, before.object_id, seq(int(before.seq) + 1))
    assert (lib.sets_dir / F).read_bytes() == data and os.stat(lib.sets_dir / F).st_ino == st.st_ino
    assert migration_tree(lib) == evidence  # B7 evidence is only read
    # and a later save gains history again
    es = lib.get("F1")
    es.description = "after"
    with lib.collect_outcomes() as sink:
        lib.save(es)
    assert sink.last.complete


@posix
def test_real_restore_of_a_set_tracked_only_at_its_upgraded_bytes(lib):
    (lib.sets_dir / F).write_text(set_text(), encoding="utf-8")
    original = (lib.sets_dir / F).read_bytes()
    assert up.upgrade_file(lib.sets_dir, F).result.value == "upgraded"
    make_object(lib.root, [(lib.sets_dir / F).read_bytes()], P, oid=ID_A)  # as if first adopted while upgraded
    assert resolved(lib).state is S.TRACKED
    assert rs.restore_file(lib.sets_dir, F).result.value == "restored"
    assert (lib.sets_dir / F).read_bytes() == original
    assert resolved(lib).state is S.CHANGED_OUTSIDE  # no revision holds the original bytes
    report = rc.reconcile_file(lib.sets_dir, F)
    assert (report.outcome, report.rule) == (rc.Outcome.VERIFIED_RESTORED, "P20R")
    evidence = migration_tree(lib)
    out = rb.rebaseline_set(lib, F, "restore", ID_A, sha(original))
    assert out.complete, out
    r = resolved(lib)
    assert (r.state, r.object_id, r.seq) == (S.TRACKED, ID_A, seq(2))
    assert migration_tree(lib) == evidence


@posix
def test_a_restored_set_after_an_upgrade_rebaseline_is_row_4_and_needs_nothing(lib):
    before = tracked_format1(lib)
    up.upgrade_file(lib.sets_dir, F)
    data = (lib.sets_dir / F).read_bytes()
    assert rb.rebaseline_set(lib, F, "upgrade", before.object_id, sha(data)).complete
    assert rs.restore_file(lib.sets_dir, F).result.value == "restored"
    r = resolved(lib)
    assert (r.state, r.history_ahead) == (S.TRACKED, True)
    out = rb.rebaseline_set(lib, F, "restore", before.object_id, sha((lib.sets_dir / F).read_bytes()))
    assert (out.status, out.reason) == (St.REFUSED, "rebaseline_state")


@posix
def test_real_edit_before_the_upgrade_is_refused_on_the_head(lib):
    before = tracked_format1(lib)
    data = json.loads((lib.sets_dir / F).read_text(encoding="utf-8"))
    data["description"] = "edited outside the library"
    (lib.sets_dir / F).write_text(json.dumps(data, indent=2), encoding="utf-8")
    assert up.upgrade_file(lib.sets_dir, F).result.value == "upgraded"
    snapshot = tree(lib.root)
    out = rb.rebaseline_set(lib, F, "upgrade", before.object_id, sha((lib.sets_dir / F).read_bytes()))
    assert (out.status, out.reason) == (St.REFUSED, "rebaseline_head") and tree(lib.root) == snapshot


@posix
def test_real_edit_after_the_upgrade_is_refused_by_the_report(lib):
    before = tracked_format1(lib)
    up.upgrade_file(lib.sets_dir, F)
    data = json.loads((lib.sets_dir / F).read_text(encoding="utf-8"))
    data["description"] = "edited after"
    (lib.sets_dir / F).write_text(json.dumps(data, indent=2), encoding="utf-8")
    assert rc.reconcile_file(lib.sets_dir, F).rule == "P14"
    snapshot = tree(lib.root)
    out = rb.rebaseline_set(lib, F, "upgrade", before.object_id, sha((lib.sets_dir / F).read_bytes()))
    assert (out.status, out.reason) == (St.REFUSED, "rebaseline_report") and tree(lib.root) == snapshot


@posix
def test_real_wrong_mode_is_refused(lib):
    before = tracked_format1(lib)
    up.upgrade_file(lib.sets_dir, F)
    out = rb.rebaseline_set(lib, F, "restore", before.object_id, sha((lib.sets_dir / F).read_bytes()))
    assert (out.status, out.reason) == (St.REFUSED, "rebaseline_report")


@posix
def test_real_untracked_set_is_refused(lib):
    (lib.sets_dir / F).write_text(set_text(), encoding="utf-8")
    up.upgrade_file(lib.sets_dir, F)
    out = rb.rebaseline_set(lib, F, "upgrade", "a" * 32, sha((lib.sets_dir / F).read_bytes()))
    assert (out.status, out.reason) == (St.REFUSED, "rebaseline_state")


@posix
def test_real_contention_is_refused_and_writes_nothing(lib):
    before = tracked_format1(lib)
    up.upgrade_file(lib.sets_dir, F)
    code = ("import fcntl, os, sys, time\n"
            "fd = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\nprint('held', flush=True)\ntime.sleep(60)\n")
    holder = subprocess.Popen([sys.executable, "-c", code, str(lib.sets_dir / M)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        snapshot = tree(lib.root)
        out = rb.rebaseline_set(lib, F, "upgrade", before.object_id, sha((lib.sets_dir / F).read_bytes()))
        assert out.status is St.REFUSED and out.reason == "rebaseline_report" and tree(lib.root) == snapshot
    finally:
        holder.kill()
        holder.wait()


@posix
def test_real_writer_locks_are_held_during_the_write(lib, monkeypatch):
    import fcntl
    before = tracked_format1(lib)
    up.upgrade_file(lib.sets_dir, F)
    held = []

    def probe(step):
        if step == "B7.write":
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
    assert rb.rebaseline_set(lib, F, "upgrade", before.object_id, sha((lib.sets_dir / F).read_bytes())).complete
    assert held == [True]


@posix
@pytest.mark.parametrize("step", ["B6.mkdir", "B7.write", "B7.fsync", "B7.verify", "B8.temp", "B8.fsync", "B8.replace", "B8.dirfsync"])
def test_a_kill_at_every_step_leaves_a_defined_state(lib, step):
    before = tracked_format1(lib)
    up.upgrade_file(lib.sets_dir, F)
    data = (lib.sets_dir / F).read_bytes()
    script = ("import os, sys\n"
              "from lpbfstudio import history as h, library as L, rebaseline as rb\n"
              "lib = L.Library(sys.argv[1], seed=False)\n"
              "def fault(s):\n    if s == sys.argv[2]:\n        os._exit(9)\n"
              "h._fault = fault\n"
              "rb.rebaseline_set(lib, sys.argv[3], 'upgrade', sys.argv[4], sys.argv[5])\nos._exit(0)\n")
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([SRC, os.environ.get("PYTHONPATH", "")]))
    p = subprocess.run([sys.executable, "-c", script, str(lib.root), step, F, before.object_id, sha(data)], env=env,
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 9, p.stderr
    assert (lib.sets_dir / F).read_bytes() == data
    r = resolved(lib)
    assert r.state is (S.TRACKED if step == "B8.dirfsync" else S.CHANGED_OUTSIDE)
    if r.state is S.CHANGED_OUTSIDE:
        assert rb.rebaseline_set(lib, F, "upgrade", before.object_id, sha(data)).complete
