"""Restore: the complete run, its refusals and its eligibility rules (B7 Phase 2, S3-B2c-b, b3).

``restore.restore_file`` implements the frozen design revision 5 and the accepted plan revision 4.7
(state machine "SM", failure matrix "FM"). These tests pin:

* the complete run on ext4 and tmpfs: the set file holds the original bytes at the staged inode,
  the displaced file (the upgraded bytes, at the set file's old inode) and the preserved copy are
  kept, the record is ``restored``, the outcome is confirmed, the next report is P20R and a second
  call is ``already_restored`` with nothing written (SM 3.13, 4.10 of revision 1);
* R0 to R5 refusals, each with nothing written (apart from an empty ``M/restores`` created at R2a,
  F-3), and the D-1/D-2 classification (T3-17, T3-18, T3-28);
* eligibility (OD-10): P12 or P24 ``verified_upgraded`` only, H1 to H9, and L0 = L1; P13, P14, P15
  and every other rule refuse; P20R is ``already_restored``;
* retry: after ``failed`` a new attempt with a new folder; after ``uncertain`` refused;
* file metadata (PL 6.10): the restored file's mode is the requested ``0o644`` less the umask; the
  displaced file keeps its own metadata.

Synthetic data in temporary folders only.
"""

import errno
import json
import os
import stat
import sys
import uuid

import pytest

from lpbfstudio import migration as mig
from lpbfstudio import locks
from lpbfstudio import reconcile as rc
from lpbfstudio import reconcile_apply as ra
from lpbfstudio import restore as rs
from lpbfstudio import restore_record as rr
from lpbfstudio import upgrade as up
from lpbfstudio.fsprim import Incomplete, UnsupportedArchitecture, UnsupportedFileSystem

from _restore_support import (  # noqa: F401 (fixtures)
    F, M, NOW, ORIGINAL, RS, O, R, Res, Trace, fail_at, fs_root, lock_free, make_library, names,
    nothing_removed, only_attempt, oserror, posix, record_of, report, restore, sets, sha, snap,
    upgraded_bytes,
)


# ================================================================ the complete run


@posix
def test_a_restore_puts_the_original_bytes_back_and_keeps_everything(sets, monkeypatch):
    trace = Trace(monkeypatch)
    upgraded = upgraded_bytes(sets)
    s_ino = (sets / F).stat().st_ino
    before = names(sets.parent)
    out = restore(sets)
    assert out.outcome is Res.RESTORED and out.confirmed, out.reason
    assert (out.exchange_called, out.exchange_durable, out.record_observed, out.record_durable) == \
        ("ok", "yes", "restored", "yes")
    assert out.last_observed_op == out.last_durable_op == "W-13"
    assert out.predicted_report == "P20R"
    a = only_attempt(sets)
    assert a.name == out.restore_id
    assert (sets / F).read_bytes() == ORIGINAL
    assert (a / rr.SWAP_DIR / F).read_bytes() == upgraded
    assert (a / rr.SWAP_DIR / F).stat().st_ino == s_ino  # the displaced file is the old set file's inode
    assert (a / rr.PRESERVED_DIR / F).read_bytes() == upgraded
    assert (sets / M / F).read_bytes() == ORIGINAL  # the backup is untouched
    rec = record_of(a)
    assert rec.status is rr.RestoreStatus.RESTORED and rec.exchange_attempted
    assert rec.displaced_matches_upgraded and rec.displaced_observed_at_restore.sha256 == sha(upgraded)
    assert rec.source_identity == [os.stat(a / rr.SWAP_DIR / F).st_dev, s_ino]
    assert rec.staged_identity == [os.stat(sets / F).st_dev, os.stat(sets / F).st_ino]
    assert rec.created_at == rec.completed_at == NOW
    assert sorted(os.listdir(a)) == sorted([rr.RECORD_NAME, rr.PRESERVED_DIR, rr.SWAP_DIR])  # no temporary file left
    nothing_removed(before, names(sets.parent))
    # both W-12 fsync calls, once each, in order (OD-29)
    w12 = [w for o, w in trace.events if o == "fsync" and w.startswith("W-12")]
    assert w12 == ["W-12 sets", "W-12 W"]
    assert out.facts("W-12 fsync(sets_fd)") == ["success"] and out.facts("W-12 fsync(W_fd)") == ["success"]
    assert trace.ops("exchange") == [F]
    assert not trace.has("unlink")
    out_r = report(sets)
    assert (out_r.outcome, out_r.rule) == (O.VERIFIED_RESTORED, "P20R"), out_r.reason
    assert lock_free(sets)


@posix
def test_a_second_restore_is_already_restored_and_writes_nothing(sets):
    assert restore(sets).outcome is Res.RESTORED
    before = snap(sets.parent)
    again = restore(sets)
    assert again.outcome is Res.ALREADY_RESTORED and again.confirmed, again.reason
    assert snap(sets.parent) == before
    assert again.predicted_report == "P20R"


@posix
def test_apply_writes_nothing_after_a_restore(sets):
    assert restore(sets).outcome is Res.RESTORED
    before = snap(sets.parent)
    res = ra.apply_file(sets, F, now="2099-01-01T00:00:00Z")
    assert res.result is ra.ApplyResult.NO_WRITE and snap(sets.parent) == before


@posix
def test_the_restored_file_can_be_upgraded_again_only_by_an_explicit_new_upgrade(sets):
    # a restored file is format 1 again, but its upgrade record still says upgraded: an upgrade refuses
    assert restore(sets).outcome is Res.RESTORED
    out = up.upgrade_file(sets, F)
    assert out.result.value != "upgraded"


@posix
def test_other_set_files_and_their_records_are_untouched(sets):
    q1 = (sets / "Q1.json").read_bytes()
    records = {p.name: p.read_bytes() for p in (sets / M / R).iterdir()}
    assert restore(sets).outcome is Res.RESTORED
    assert (sets / "Q1.json").read_bytes() == q1
    assert {p.name: p.read_bytes() for p in (sets / M / R).iterdir()} == records  # no upgrade record written


@posix
def test_metadata_is_not_restored_the_restored_file_gets_the_requested_mode(sets):
    os.chmod(sets / F, 0o600)
    umask = os.umask(0)
    os.umask(umask)
    out = restore(sets)
    assert out.outcome is Res.RESTORED
    a = only_attempt(sets)
    assert stat.S_IMODE((sets / F).stat().st_mode) == 0o644 & ~umask  # W has no default ACL here
    assert stat.S_IMODE((a / rr.SWAP_DIR / F).stat().st_mode) == 0o600  # the displaced file keeps its own
    assert (sets / F).stat().st_uid == os.geteuid()


# ================================================================ R0, R1: arguments, platform, identity


@pytest.mark.parametrize("name", ["", "../x.json", "a/b.json", ".hidden.json", "x.txt", None, 3, "x\x00.json"])
def test_an_unsafe_name_is_refused_before_anything_is_opened(tmp_path, name):
    out = rs.restore_file(tmp_path, name)
    assert out.outcome is Res.REFUSED_INVALID and out.confirmed
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("now", ["yesterday", "2026-11-07T12:00:00+02:00", "2026-11-07T12:00:00", 5])
def test_an_invalid_now_is_refused_before_anything_is_opened(tmp_path, now):
    out = rs.restore_file(tmp_path, F, now=now)
    assert out.outcome is Res.REFUSED_INVALID and out.confirmed


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows refusal itself")
def test_windows_is_refused_before_anything_is_read(tmp_path):
    (tmp_path / F).write_bytes(b"{}")
    before = sorted(tmp_path.rglob("*"))
    out = rs.restore_file(tmp_path, F)
    assert out.outcome is Res.REFUSED_PLATFORM and out.confirmed
    assert sorted(tmp_path.rglob("*")) == before


@posix
def test_an_unsupported_architecture_is_refused_before_any_write(sets, monkeypatch):
    def refuse():
        raise UnsupportedArchitecture("not x86_64")
    monkeypatch.setattr(rs, "check_architecture", refuse)
    before = snap(sets.parent)
    out = restore(sets)
    assert out.outcome is Res.REFUSED_PLATFORM and snap(sets.parent) == before


@posix
@pytest.mark.parametrize("exc", [UnsupportedFileSystem("FUSE"), Incomplete("mountinfo truncated"),
                                 oserror(errno.EIO)])
def test_an_unidentified_or_unsupported_file_system_is_refused_before_any_write(sets, monkeypatch, exc):
    def identify(fd):
        raise exc
    monkeypatch.setattr(rs, "_identify", identify)
    before = snap(sets.parent)
    out = restore(sets)
    assert out.outcome is Res.REFUSED_PLATFORM and out.confirmed
    assert snap(sets.parent) == before and not (sets / M / RS).exists()


@posix
def test_m_on_another_mount_is_refused(sets, monkeypatch):
    real = rs._mount_id
    monkeypatch.setattr(rs, "_mount_id", lambda fd, what: real(fd, what) + (1 if what == "M" else 0))
    before = snap(sets.parent)
    assert restore(sets).outcome is Res.REFUSED_PLATFORM
    assert snap(sets.parent) == before


@posix
def test_a_library_that_was_never_upgraded_is_not_restorable_and_gains_nothing(fs_root):
    sets = make_library(fs_root[1], upgrade=False)
    before = snap(sets.parent)
    out = restore(sets)
    assert out.outcome is Res.REFUSED_NOT_RESTORABLE and out.confirmed
    assert snap(sets.parent) == before and not (sets / M).exists()


@posix
def test_a_migration_folder_that_is_a_link_is_refused(fs_root):
    sets = make_library(fs_root[1], upgrade=False)
    (fs_root[1] / "elsewhere").mkdir()
    (sets / M).symlink_to(fs_root[1] / "elsewhere", target_is_directory=True)
    out = restore(sets)
    assert out.outcome is Res.REFUSED_UNVERIFIABLE
    assert list((fs_root[1] / "elsewhere").iterdir()) == []


# ================================================================ R2: locks (D-1, T3-17)


@posix
@pytest.mark.parametrize("which", ["sets", "m"])
def test_lock_contention_is_refused_busy(sets, which):
    import fcntl
    fd = os.open(sets if which == "sets" else sets / M, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        before = snap(sets.parent)
        out = restore(sets)
        assert out.outcome is Res.REFUSED_BUSY and out.confirmed, out.reason
        assert snap(sets.parent) == before
    finally:
        os.close(fd)


@posix
@pytest.mark.parametrize("code", [errno.ENOLCK, errno.EIO, errno.EOPNOTSUPP, errno.EINVAL, errno.EBADF])
@pytest.mark.parametrize("target", ["sets", "m"])
def test_a_lock_error_is_refused_unverifiable_never_busy(sets, monkeypatch, code, target):
    real = locks._flock
    calls = []

    def flock(fd, op):
        calls.append(fd)
        if not op & locks.fcntl.LOCK_UN and len(calls) == (1 if target == "sets" else 2):
            raise oserror(code)
        return real(fd, op)
    monkeypatch.setattr(locks, "_flock", flock)
    before = snap(sets.parent)
    out = restore(sets)
    assert out.outcome is Res.REFUSED_UNVERIFIABLE, out.reason
    assert snap(sets.parent) == before
    assert lock_free(sets)


@posix
def test_a_sets_folder_identity_mismatch_after_locking_is_unverifiable(sets, monkeypatch):
    monkeypatch.setattr(locks, "_stat_path", lambda p: os.stat(sets / M))
    before = snap(sets.parent)
    assert restore(sets).outcome is Res.REFUSED_UNVERIFIABLE
    assert snap(sets.parent) == before


@posix
def test_an_old_style_holder_of_m_alone_makes_restore_busy(sets):
    # an older build's upgrade, report or apply locks M only (revision 5 row 1)
    import fcntl
    fd = os.open(sets / M, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert restore(sets).outcome is Res.REFUSED_BUSY
    finally:
        os.close(fd)


# ================================================================ R2a: M/restores (W-01, T3-11, T3-18, test 15)


@posix
def test_r2a_creates_restores_and_an_early_refusal_leaves_only_that_empty_folder(sets):
    (sets / F).write_bytes((sets / F).read_bytes().replace(b'"description"', b'"description" ', 1))  # P14 now
    reports_before = {n: (report(sets, n).outcome, report(sets, n).rule) for n in (F, "Q1.json")}
    before = names(sets.parent)
    out = restore(sets)
    assert out.outcome is Res.REFUSED_NOT_RESTORABLE and out.restores_dir == "created-durable", out.reason
    assert names(sets.parent) - before == {f"{sets.name}/{M}/{RS}"}
    assert list((sets / M / RS).iterdir()) == []
    assert "left in place" in out.reason
    assert {n: (report(sets, n).outcome, report(sets, n).rule) for n in (F, "Q1.json")} == reports_before


@posix
def test_a_restores_fsync_failure_stops_before_the_baseline(sets, monkeypatch):
    trace = Trace(monkeypatch)
    fail_at(monkeypatch, "_fsync", "M")
    out = restore(sets)
    assert out.outcome is Res.REFUSED_UNVERIFIABLE and not out.confirmed
    assert out.restores_dir == "created-not-durable"
    assert not trace.has("survey") and not trace.has("list")
    assert (sets / M / RS).is_dir() and list((sets / M / RS).iterdir()) == []  # kept, never removed
    assert out.facts(f"{M}/{RS}") == ["present (folder)"]


@posix
def test_a_restores_mkdir_error_refuses(sets, monkeypatch):
    fail_at(monkeypatch, "_mkdirat", "W-01", oserror(errno.EACCES))
    out = restore(sets)
    assert out.outcome is Res.REFUSED_UNVERIFIABLE and not (sets / M / RS).exists()


@posix
def test_a_restores_folder_on_another_mount_is_refused_platform(sets, monkeypatch):
    trace = Trace(monkeypatch)
    (sets / M / RS).mkdir()
    real = rs._mount_id
    monkeypatch.setattr(rs, "_mount_id", lambda fd, what: real(fd, what) + (7 if what == f"{M}/{RS}" else 0))
    out = restore(sets)
    assert out.outcome is Res.REFUSED_PLATFORM, out.reason
    assert not trace.has("survey") and list((sets / M / RS).iterdir()) == []


@posix
def test_a_restores_link_refuses_with_nothing_else_written(sets, fs_root):
    (fs_root[1] / "other").mkdir()
    (sets / M / RS).symlink_to(fs_root[1] / "other", target_is_directory=True)
    before = snap(sets.parent)
    assert restore(sets).outcome is Res.REFUSED_UNVERIFIABLE
    assert snap(sets.parent) == before and list((fs_root[1] / "other").iterdir()) == []


@posix
def test_a_restores_folder_replaced_after_creation_is_refused(sets, monkeypatch):
    def swap(*a):
        os.rename(sets / M / RS, sets / M / "moved")
        (sets / M / RS).mkdir()
    real = rs._open_dir

    def open_dir(ctx, path, what):
        fd = real(ctx, path, what)
        if what == "RS":
            swap()
        return fd
    monkeypatch.setattr(rs, "_open_dir", open_dir)
    assert restore(sets).outcome is Res.REFUSED_UNVERIFIABLE


# ================================================================ R3: R3-Ω (OD-10, OD-12, T3-28)


@posix
def test_the_survey_reuses_the_runs_own_descriptors(sets, monkeypatch):
    seen = {}
    real = rc._survey

    def survey(ctx, base):
        seen["m"] = ctx.dirs.get(base / M)
        return real(ctx, base)
    monkeypatch.setattr(rc, "_survey", survey)
    trace = Trace(monkeypatch)
    assert restore(sets).outcome is Res.RESTORED
    assert seen["m"] is not None and trace.ops("survey") == ["R3"]


@posix
def test_a_survey_busy_on_another_descriptor_is_unverifiable_never_busy(sets, monkeypatch):
    real_open = up._open_sub

    def open_sub(ctx, path, create=False):
        if path.name == M and path in ctx.dirs:
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)  # a second open file description
            ctx.fds.append(fd)
            return fd
        return real_open(ctx, path, create)
    real_survey = rs._survey

    def survey(ctx, base):
        monkeypatch.setattr(up, "_open_sub", open_sub)
        try:
            return real_survey(ctx, base)
        finally:
            monkeypatch.setattr(up, "_open_sub", real_open)
    monkeypatch.setattr(rs, "_survey", survey)
    out = restore(sets)
    assert out.outcome is Res.REFUSED_UNVERIFIABLE, out.reason
    assert "G4" in " ".join(out.facts("survey"))


@posix
@pytest.mark.parametrize("exc", [BlockingIOError(errno.EWOULDBLOCK, "busy"), oserror(errno.ENOLCK)])
def test_a_survey_lock_error_is_unverifiable(sets, monkeypatch, exc):
    real_survey, real_flock = rs._survey, up._flock

    def survey(ctx, base):
        def flock(fd, op):
            raise exc
        monkeypatch.setattr(up, "_flock", flock)
        try:
            return real_survey(ctx, base)
        finally:
            monkeypatch.setattr(up, "_flock", real_flock)
    monkeypatch.setattr(rs, "_survey", survey)
    assert restore(sets).outcome is Res.REFUSED_UNVERIFIABLE


@posix
def test_a_change_during_the_baseline_round_refuses(sets, monkeypatch):
    real = rs._survey

    def survey(ctx, base):
        (sets / "new.json").write_bytes(b"{}")
        return real(ctx, base)
    monkeypatch.setattr(rs, "_survey", survey)
    out = restore(sets)
    assert out.outcome is Res.REFUSED_UNVERIFIABLE and "baseline" in out.reason
    assert list((sets / M / RS).iterdir()) == []


@posix
def test_an_incomplete_baseline_listing_refuses(sets, monkeypatch):
    fail_at(monkeypatch, "_listing", "R", Incomplete("changed while listed"))
    assert restore(sets).outcome is Res.REFUSED_UNVERIFIABLE


@posix
def test_p14_an_edited_format_2_file_is_never_restorable(sets):
    data = json.loads(upgraded_bytes(sets))
    data["description"] = "edited after the upgrade"
    (sets / F).write_text(json.dumps(data, indent=2), encoding="utf-8")
    out = restore(sets)
    assert out.outcome is Res.REFUSED_NOT_RESTORABLE and "P14" in out.reason


@posix
def test_p13_a_missing_backup_is_not_restorable(sets):
    (sets / M / F).unlink()
    assert restore(sets).outcome is Res.REFUSED_NOT_RESTORABLE


@posix
def test_p15_a_record_without_a_format_2_file_is_not_restorable(sets):
    (sets / F).write_bytes(ORIGINAL)
    out = restore(sets)
    assert out.outcome is Res.REFUSED_NOT_RESTORABLE and "P15" in out.reason


@posix
def test_a_file_that_was_never_upgraded_in_an_upgraded_library_is_not_restorable(sets):
    out = restore(sets, "Q1.json")
    assert out.outcome is Res.REFUSED_NOT_RESTORABLE


@posix
def test_g6_restore_evidence_that_cannot_be_attributed_refuses(sets):
    (sets / M / RS).mkdir()
    (sets / M / RS / "junk").write_bytes(b"x")
    out = restore(sets)
    assert out.outcome is Res.REFUSED_UNVERIFIABLE and "G6" in out.reason


def _failed_upgrade_record(sets, **over) -> mig.MigrationRecord:
    rec = mig.new_record(F, f"{M}/{F}", ORIGINAL, now="2026-11-07T09:00:00Z")
    rec = mig.advance(rec, mig.RecordStatus.FAILED, error="stopped", now="2026-11-07T09:00:01Z")
    data = json.loads(mig.dump_record(rec))
    data.update(over)
    rec = mig.parse_record(json.dumps(data))
    (sets / M / R / f"{rec.record_id}.json").write_text(mig.dump_record(rec), encoding="utf-8")
    return rec


@posix
def test_h_rules_an_earlier_failed_upgrade_of_the_same_bytes_is_allowed(sets):
    _failed_upgrade_record(sets)
    assert restore(sets).outcome is Res.RESTORED


@posix
def test_h3_a_failed_record_for_other_bytes_refuses(sets):
    _failed_upgrade_record(sets, source_sha256=sha(b"other"), source_size_bytes=5)
    assert restore(sets).outcome is Res.REFUSED_NOT_RESTORABLE


@posix
def test_h8_a_failed_record_created_after_the_upgrade_refuses(sets):
    _failed_upgrade_record(sets, created_at="2099-01-01T00:00:00Z", completed_at="2099-01-01T00:00:01Z")
    out = restore(sets)
    assert out.outcome is Res.REFUSED_NOT_RESTORABLE and "H8" in out.reason


@posix
def test_h5_a_failed_record_claiming_the_same_upgraded_bytes_refuses(sets):
    urec = next(mig.parse_record(p.read_text(encoding="utf-8")) for p in (sets / M / R).iterdir())
    rec = mig.new_record(F, f"{M}/{F}", ORIGINAL, now="2026-11-07T09:00:00Z")
    rec = mig.advance(rec, mig.RecordStatus.BACKED_UP, upgraded=upgraded_bytes(sets))
    rec = mig.advance(rec, mig.RecordStatus.FAILED, error="stopped", now="2026-11-07T09:00:01Z")
    assert rec.upgraded_sha256 == urec.upgraded_sha256
    (sets / M / R / f"{rec.record_id}.json").write_text(mig.dump_record(rec), encoding="utf-8")
    out = restore(sets)
    assert out.outcome is Res.REFUSED_NOT_RESTORABLE and "H5" in out.reason


@posix
def test_h2_another_files_record_naming_this_backup_refuses(sets):
    rec = mig.new_record("Q1.json", f"{M}/{F}", ORIGINAL, now="2026-11-07T09:00:00Z")
    rec = mig.advance(rec, mig.RecordStatus.FAILED, error="stopped", now="2026-11-07T09:00:01Z")
    (sets / M / R / f"{rec.record_id}.json").write_text(mig.dump_record(rec), encoding="utf-8")
    out = restore(sets)
    assert out.outcome is Res.REFUSED_NOT_RESTORABLE


@posix
def test_h9_an_orphan_record_temporary_file_refuses(sets):
    (sets / M / R / f".{uuid.uuid4().hex}.0123456789ab.tmp").write_bytes(b"x")
    out = restore(sets)
    assert out.outcome is Res.REFUSED_NOT_RESTORABLE and "H9" in out.reason


@posix
def test_upgrade_residue_for_the_file_refuses(sets):
    (sets / f".{F}.a1b2c3d4e5f6{up.TMP_SUFFIX}").write_bytes(b"x")
    assert restore(sets).outcome is Res.REFUSED_NOT_RESTORABLE


@posix
def test_an_in_progress_or_uncertain_restore_blocks_a_new_one(sets, monkeypatch):
    fail_at(monkeypatch, "_exchange", lambda w: True, oserror(errno.EIO), before=None)
    assert restore(sets).outcome is Res.UNCERTAIN
    monkeypatch.undo()
    before = snap(sets.parent)
    out = restore(sets)
    assert out.outcome is Res.REFUSED_NOT_RESTORABLE and "P21" in out.reason
    assert snap(sets.parent) == before


# ================================================================ R4, R5


@posix
def test_r4_an_in_place_change_after_the_decision_refuses(sets, monkeypatch):
    # the change lands after the frozen decision has read the file (P12), so R4 is the first to see it
    real = rs._read_urec

    def read_urec(run, sv, decision):
        assert decision.rule == "P12"
        with open(sets / F, "r+b") as fh:  # same inode, same size, other bytes: the listings stay equal
            data = fh.read()
            fh.seek(0)
            fh.write(data.replace(b"{", b" ", 1))
        return real(run, sv, decision)
    monkeypatch.setattr(rs, "_read_urec", read_urec)
    out = restore(sets)
    assert out.outcome is Res.REFUSED_NOT_RESTORABLE and "upgraded bytes" in out.reason, out.reason
    assert list((sets / M / RS).iterdir()) == []


@posix
@pytest.mark.parametrize("what", ["S", "B", "U-rec"])
def test_an_incomplete_read_of_s_b_or_the_record_refuses(sets, monkeypatch, what):
    fail_at(monkeypatch, "_read", what, Incomplete("changed while it was read"))
    out = restore(sets)
    assert out.outcome is Res.REFUSED_UNVERIFIABLE, out.reason
    assert list((sets / M / RS).iterdir()) == []


# ================================================================ retry (SM 3.13 retry table, OD-10)


@posix
def test_after_a_failed_attempt_a_new_attempt_is_allowed_and_the_old_one_is_kept(sets, monkeypatch):
    fail_at(monkeypatch, "_fsync", "W-06 P/f")
    first = restore(sets)
    assert first.outcome is Res.FAILED and first.failed_kind == "a", first.reason
    assert report(sets).rule == "P24"
    monkeypatch.undo()
    old = only_attempt(sets)
    old_snap = snap(old)
    second = restore(sets)
    assert second.outcome is Res.RESTORED and second.restore_id != first.restore_id
    assert snap(old) == old_snap  # never touched, never reused
    assert report(sets).rule == "P20R"
