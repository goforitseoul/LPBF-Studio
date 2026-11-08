"""Restore at and after the exchange: R10 to R12 (B7 Phase 2, S3-B2c-b, b3).

These tests pin, against the accepted plan revision 4.7 (state machine "SM" sections 3.11 to 3.13
and 4; failure matrix "FM" W-11 to W-13):

* an exchange error is decided by errno class first (OD-21, OD-25): class A (``ENOSYS``, ``EXDEV``,
  ``EBADF``, ...) may give ``failed`` only with complete, matching position evidence read **after**
  the error (E-5, T3-4, T3-16); class C (``ENOENT``, E-3, T3-1) and class B (``EIO``, ``EINTR``,
  ``EINVAL``, every ``EROFS``, ...; E-4, T3-2 to T3-4d, T3-14) never do, even when the exchange did
  not happen, and never enter W-12 or the post-exchange checks; the call is never retried;
* W-12 (OD-29, T3-27): both ``fsync`` calls are made once each, in order, whatever the first
  returned; each result is recorded separately; any failure gives ``exchange_durable = no``,
  ``uncertain`` and ``confirmed: false``; no ``restored`` record, no second exchange, no W-14/W-15,
  and no W-16 even when W-13 fails (RC-1);
* PX-Ω (T3-20): every check runs; a G6-type failure means no record write (D3); any other failure
  means W-13 ``uncertain`` with the displaced facts (D1, D2);
* W-13 failures by Q-Ω (F0 to F5, T3-5, T3-6, T3-21): exact bytes and identity only.

Injected aborts stand for process termination inside the process (class U); real kills are in
``test_restore_kill_points.py``. None of this is power-loss evidence.
"""

import errno
import os

import pytest

from lpbfstudio import restore as rs
from lpbfstudio import restore_record as rr
from lpbfstudio.fsprim import Incomplete

from _restore_support import (  # noqa: F401 (fixtures)
    F, ORIGINAL, Res, Trace, exchange_raising, fail_at, fs_root, only_attempt, oserror, posix, record_of, report,
    replace_same_bytes, restore, sets, upgraded_bytes,
)

pytestmark = posix

CLASS_A = [errno.EXDEV, errno.ENOSYS, errno.EBADF, errno.ENOTDIR, errno.ENAMETOOLONG, errno.EFAULT]
CLASS_B = [errno.EIO, errno.EINTR, errno.EINVAL, errno.EROFS, errno.ENOSPC, errno.EPERM, errno.EBUSY, errno.EMLINK]


def tmp_files(a) -> list:
    return [n for n in os.listdir(a) if n.startswith(f".{rr.RECORD_NAME}.")]


def evidence_untouched(trace: Trace) -> None:
    """No unlink, and no rename other than a record update's own T(x) -> restore.json."""
    assert not trace.has("unlink")
    assert all(w.endswith(".u7") for w in trace.ops("rename")), trace.ops("rename")


# ================================================================ exchange errors (SM 3.11, E-1 to E-6)


@pytest.mark.parametrize("code", CLASS_A)
def test_class_a_with_matching_position_evidence_is_failed(sets, monkeypatch, code):
    trace = Trace(monkeypatch)
    upgraded = upgraded_bytes(sets)
    calls = exchange_raising(monkeypatch, code)
    out = restore(sets)
    assert out.outcome is Res.FAILED and out.failed_kind == "a" and out.confirmed, out.reason
    assert out.exchange_called.startswith("error:") and out.exchange_durable == "n/a"
    assert calls == [F] and not any(w.startswith("W-12") for w in trace.ops("fsync"))
    assert any(o.label == "inferred" for o in out.observations)
    assert (sets / F).read_bytes() == upgraded
    assert record_of(only_attempt(sets)).status is rr.RestoreStatus.FAILED
    assert report(sets).rule == "P24"


def test_enoent_is_never_failed_even_with_every_fe_item_passing(sets, monkeypatch):
    trace = Trace(monkeypatch)
    calls = exchange_raising(monkeypatch, errno.ENOENT)
    out = restore(sets)
    assert out.outcome is Res.UNCERTAIN and not out.confirmed, out.reason
    assert out.exchange_called == "error:ENOENT" and out.exchange_durable == "unknown"
    assert calls == [F] and not any(o.label == "inferred" for o in out.observations)  # E-5 not taken
    rec = record_of(only_attempt(sets))
    assert rec.status is rr.RestoreStatus.UNCERTAIN and "ENOENT" in rec.error
    assert not any(w.startswith("W-12") for w in trace.ops("fsync"))
    assert report(sets).rule == "P21"


def test_enoent_with_a_changed_record_writes_nothing(sets, monkeypatch):
    def swap(*a):
        replace_same_bytes(only_attempt(sets) / rr.RECORD_NAME)
    exchange_raising(monkeypatch, errno.ENOENT, before_return=swap)
    out = restore(sets)
    assert out.outcome is Res.UNCERTAIN and out.predicted_report == "unknown"
    assert record_of(only_attempt(sets)).status is rr.RestoreStatus.EXCHANGING


@pytest.mark.parametrize("code", CLASS_B)
def test_class_b_is_always_uncertain_and_never_retried(sets, monkeypatch, code):
    trace = Trace(monkeypatch)
    calls = exchange_raising(monkeypatch, code)
    out = restore(sets)
    assert out.outcome is Res.UNCERTAIN and not out.confirmed, out.reason
    assert out.exchange_durable == "unknown" and calls == [F]
    assert not any(w.startswith("W-12") for w in trace.ops("fsync"))
    assert not trace.has("lstat", "PX M")  # the post-exchange checks are not run as if it had succeeded
    assert record_of(only_attempt(sets)).status is rr.RestoreStatus.UNCERTAIN
    if code == errno.EINTR:
        assert "interrupted exchange" in out.reason
    assert report(sets).rule == "P21"


@pytest.mark.parametrize("code", [errno.EIO, errno.EINVAL])
def test_class_b_although_the_exchange_took_place(sets, monkeypatch, code):
    trace = Trace(monkeypatch)
    exchange_raising(monkeypatch, code, effect=True)
    out = restore(sets)
    assert out.outcome is Res.UNCERTAIN and not out.confirmed and out.exchange_durable == "unknown"
    assert not any(w.startswith("W-12") for w in trace.ops("fsync"))
    assert (sets / F).read_bytes() == ORIGINAL  # the exchange happened; it is recorded, not interpreted
    assert any("uninterpreted" in o.value for o in out.observations)
    evidence_untouched(trace)


@pytest.mark.parametrize("variant", ["W/f replaced", "S replaced", "W/f read incomplete"])
def test_class_a_without_matching_post_error_evidence_is_uncertain(sets, monkeypatch, variant):
    def change(w_fd, sets_fd, name):
        target = (only_attempt(sets) / rr.SWAP_DIR / F) if variant == "W/f replaced" else (sets / F)
        if variant != "W/f read incomplete":
            replace_same_bytes(target)  # equal bytes, another inode: only a fresh read can see it
    exchange_raising(monkeypatch, errno.EXDEV, before_return=change)
    if variant == "W/f read incomplete":
        fail_at(monkeypatch, "_read", "FE W/f", Incomplete("read raised"))
    out = restore(sets)
    assert out.outcome is Res.UNCERTAIN and out.exchange_durable == "unknown", out.reason
    assert record_of(only_attempt(sets)).status is rr.RestoreStatus.UNCERTAIN


def test_class_a_with_a_foreign_entry_in_a_is_g6_with_no_write(sets, monkeypatch):
    exchange_raising(monkeypatch, errno.EXDEV,
                     before_return=lambda *a: (only_attempt(sets) / "stray").write_bytes(b"x"))
    out = restore(sets)
    assert out.outcome is Res.UNCERTAIN and out.predicted_report == "G6"
    assert record_of(only_attempt(sets)).status is rr.RestoreStatus.EXCHANGING


def test_erofs_is_class_b_whatever_mountinfo_says(sets, monkeypatch):
    exchange_raising(monkeypatch, errno.EROFS)
    out = restore(sets)
    assert out.outcome is Res.UNCERTAIN and out.facts("errno class") == ["B"]


def test_the_post_error_reads_come_after_the_exchange_returned(sets, monkeypatch):
    trace = Trace(monkeypatch)
    exchange_raising(monkeypatch, errno.EXDEV)
    assert restore(sets).outcome is Res.FAILED
    i = trace.index("exchange", F)
    for what in ("FE S", "FE W/f", "FE restore.json"):
        assert trace.index("read", what) > i
    assert trace.index("list", "FE A") > i and trace.index("list", "FE RS") > i


# ================================================================ W-12 (OD-29, T3-27)


@pytest.mark.parametrize("fail", [("sets",), ("W",), ("sets", "W")])
def test_w12_both_calls_are_always_made_and_any_failure_is_uncertain(sets, monkeypatch, fail):
    trace = Trace(monkeypatch)
    codes = {"sets": errno.EIO, "W": errno.ENOSPC}
    for label in fail:
        fail_at(monkeypatch, "_fsync", f"W-12 {label}", oserror(codes[label]))
    out = restore(sets)
    assert [w for w in trace.ops("fsync") if w.startswith("W-12")] == ["W-12 sets", "W-12 W"]
    assert out.outcome is Res.UNCERTAIN and not out.confirmed and out.exchange_durable == "no", out.reason
    for label, fd in (("sets", "sets"), ("W", "W")):
        want = f"error:{errno.errorcode[codes[label]]}" if label in fail else "success"
        assert out.facts(f"W-12 fsync({fd}_fd)") == [want]
    rec = record_of(only_attempt(sets))
    assert rec.status is rr.RestoreStatus.UNCERTAIN
    for label in ("sets", "W"):
        want = f"error:{errno.errorcode[codes[label]]}" if label in fail else "success"
        assert f"fsync({label}_fd) {want}" in rec.error
    assert trace.ops("exchange") == [F]
    evidence_untouched(trace)
    assert report(sets).rule == "P21"


def test_w12_failure_and_a_g6_type_px_failure_writes_no_record(sets, monkeypatch):
    fail_at(monkeypatch, "_fsync", "W-12 sets")
    real = rs._lstat_at

    def lstat(dir_fd, name, what):
        st = real(dir_fd, name, what)
        if what == "PX preserved":
            return os.stat(sets)  # another identity
        return st
    monkeypatch.setattr(rs, "_lstat_at", lstat)
    trace = Trace(monkeypatch)
    out = restore(sets)
    assert out.outcome is Res.UNCERTAIN and not trace.has("open", "W-13.u2 T(x)")
    assert record_of(only_attempt(sets)).status is rr.RestoreStatus.EXCHANGING
    assert out.facts("W-12 fsync(sets_fd)") == ["error:EIO"] and out.facts("W-12 fsync(W_fd)") == ["success"]


@pytest.mark.parametrize("step,helper,suffix", [("u3", "_write", "u3 T(x)"), ("u6", "_lstat_at", "u6"),
                                                ("u7", "_renameat", "u7")])
def test_after_a_w12_failure_w16_never_runs_whichever_step_of_w13_fails(sets, monkeypatch, step, helper, suffix):
    trace = Trace(monkeypatch)
    fail_at(monkeypatch, "_fsync", "W-12 W")
    fail_at(monkeypatch, helper, f"W-13.{suffix}")
    out = restore(sets)
    assert not trace.has("unlink") and not trace.has("fsync", "W-13.u10 A")
    assert out.outcome is Res.UNCERTAIN and not out.confirmed
    assert len(tmp_files(only_attempt(sets))) == 1  # T(x) kept at its identity
    assert out.predicted_report == "G6" and report(sets).rule == "G6"


def test_after_a_w12_failure_a_w13_read_back_failure_is_still_not_confirmed(sets, monkeypatch):
    fail_at(monkeypatch, "_fsync", "W-12 sets")
    fail_at(monkeypatch, "_read", "W-13.u9", Incomplete("read raised"))
    out = restore(sets)
    assert out.outcome is Res.UNCERTAIN and not out.confirmed and out.record_observed == "uncertain"
    assert out.record_durable == "yes" and out.exchange_durable == "no"


class Abort(BaseException):
    """An injected termination of the run inside the process (class U)."""


@pytest.mark.parametrize("where", ["before PX", "after W-13 u3", "after W-13 u7"])
def test_an_abort_after_a_w12_failure_leaves_only_what_disk_shows(sets, monkeypatch, where):
    fail_at(monkeypatch, "_fsync", "W-12 sets")
    helper, label = {"before PX": ("_stat_path", "PX sets path"), "after W-13 u3": ("_fsync", "W-13.u4 T(x)"),
                     "after W-13 u7": ("_fsync", "W-13.u8 A")}[where]
    fail_at(monkeypatch, helper, label, Abort())
    with pytest.raises(Abort):
        rs.restore_file(sets, F)
    # (T) only what disk shows is asserted: the record never claims restored
    rec = record_of(only_attempt(sets))
    assert rec.status in (rr.RestoreStatus.EXCHANGING, rr.RestoreStatus.UNCERTAIN)
    assert (sets / F).read_bytes() == ORIGINAL


def test_an_abort_between_the_two_w12_calls_means_the_second_was_never_made(sets, monkeypatch):
    trace = Trace(monkeypatch)
    fail_at(monkeypatch, "_fsync", "W-12 sets", Abort(), after=lambda *a: None)
    with pytest.raises(Abort):
        rs.restore_file(sets, F)
    assert not trace.has("fsync", "W-12 W")


# ================================================================ PX-Ω and W-13


def test_a_displaced_file_with_other_bytes_is_uncertain_with_the_displaced_facts(sets, monkeypatch):
    def exch(w_fd, sets_fd, name):
        rs._trace("exchange", name)
        rs.exchange(w_fd, name, sets_fd, name)
        with open(only_attempt(sets) / rr.SWAP_DIR / F, "ab") as fh:  # a late write through an old handle
            fh.write(b" ")
    monkeypatch.setattr(rs, "_exchange", exch)
    out = restore(sets)
    assert out.outcome is Res.UNCERTAIN, out.reason
    rec = record_of(only_attempt(sets))
    assert rec.status is rr.RestoreStatus.UNCERTAIN and rec.displaced_matches_upgraded is False
    assert "Nothing has been merged or restored automatically" in rec.error
    listed = " ".join(out.facts("PX-Ω"))
    assert "PX-16: fail" in listed and "PX-15: pass" in listed  # the bytes check itself, not the record contract
    assert (sets / F).read_bytes() == ORIGINAL and report(sets).rule == "P21"


def test_another_post_exchange_check_failing_is_uncertain(sets, monkeypatch):
    def exch(w_fd, sets_fd, name):
        rs._trace("exchange", name)
        rs.exchange(w_fd, name, sets_fd, name)
        (sets / "new.json").write_bytes(b"{}")
    monkeypatch.setattr(rs, "_exchange", exch)
    out = restore(sets)
    assert out.outcome is Res.UNCERTAIN and "PX-21" in out.reason
    assert report(sets).rule == "P21"


def test_every_px_check_is_run_and_listed_even_after_a_failure(sets, monkeypatch):
    def exch(w_fd, sets_fd, name):
        rs._trace("exchange", name)
        rs.exchange(w_fd, name, sets_fd, name)
        (sets / "new.json").write_bytes(b"{}")
    monkeypatch.setattr(rs, "_exchange", exch)
    out = restore(sets)
    listed = " ".join(out.facts("PX-Ω"))
    for n in range(1, 28):
        assert f"PX-{n:02d}:" in listed


def test_a_g6_type_px_failure_writes_no_record(sets, monkeypatch):
    def exch(w_fd, sets_fd, name):
        rs._trace("exchange", name)
        rs.exchange(w_fd, name, sets_fd, name)
        (only_attempt(sets) / "stray").write_bytes(b"x")
    monkeypatch.setattr(rs, "_exchange", exch)
    out = restore(sets)
    assert out.outcome is Res.UNCERTAIN and out.predicted_report == "G6"
    assert record_of(only_attempt(sets)).status is rr.RestoreStatus.EXCHANGING
    assert report(sets).rule == "G6"


W13 = {
    # failing step: (helper, label, outcome, confirmed, record_durable, next report)
    "u9": ("_read", "W-13.u9", Res.RESTORED, True, "yes", "P20R"),
    "u8": ("_fsync", "W-13.u8 A", Res.RESTORED, False, "no", "P20R"),
    "u3": ("_write", "W-13.u3 T(x)", Res.UNCERTAIN, True, "yes", "P20"),
    "u4": ("_fsync", "W-13.u4 T(x)", Res.UNCERTAIN, False, "unknown", "G6"),
    "u5": ("_fsync", "W-13.u5 A", Res.UNCERTAIN, False, "unknown", "G6"),
    "u7": ("_renameat", "W-13.u7", Res.UNCERTAIN, True, "yes", "P20"),
}


@pytest.mark.parametrize("step", sorted(W13))
def test_a_w13_failure_is_decided_by_q_omega(sets, monkeypatch, step):
    helper, label, want, confirmed, durable, rule = W13[step]
    trace = Trace(monkeypatch)
    fail_at(monkeypatch, helper, label, Incomplete("read raised") if helper == "_read" else oserror(errno.EIO))
    out = restore(sets)
    assert out.outcome is want and out.confirmed is confirmed and out.record_durable == durable, out.reason
    assert out.exchange_durable == "yes"
    if step == "u8":
        assert "observable, not durable" in out.reason and "durable persistence" not in out.reason
    if step in ("u4", "u5"):
        assert not trace.has("unlink") and len(tmp_files(only_attempt(sets))) == 1
    assert report(sets).rule == rule


def test_a_w13_u10_failure_is_tmp_removal_not_durable(sets, monkeypatch):
    fail_at(monkeypatch, "_write", "W-13.u3 T(x)")
    fail_at(monkeypatch, "_fsync", "W-13.u10 A")
    out = restore(sets)
    assert out.outcome is Res.UNCERTAIN and not out.confirmed
    assert out.record_durable == "tmp-removal-not-durable" and report(sets).rule == "P20"


def test_a_w13_u1_failure_writes_nothing_and_runs_no_q_omega(sets, monkeypatch):
    trace = Trace(monkeypatch)
    fail_at(monkeypatch, "_read", "W-13.u1", Incomplete("read raised"))
    out = restore(sets)
    assert out.outcome is Res.UNCERTAIN and not out.confirmed and out.predicted_report == "unknown"
    assert not trace.has("list", "Q-Ω A")
    assert record_of(only_attempt(sets)).status is rr.RestoreStatus.EXCHANGING


def test_the_expected_listings_are_built_before_the_exchange(sets, monkeypatch):
    # OD-26: the expected identities after W-11 come from R8b and R4, never from a post-exchange read
    trace = Trace(monkeypatch)
    out = restore(sets)
    assert out.outcome is Res.RESTORED
    i = trace.index("exchange", F)
    assert trace.index("read", "W/f") < i  # R8b, the I_t read-back
    assert not [w for o, w in trace.events[i + 1:] if o == "read" and w in ("W/f", "S", "P/f")]
