"""Restore failures before the exchange: R6 to R9b (B7 Phase 2, S3-B2c-b, b3).

Each failure is injected at one system call through ``restore``'s narrow helpers, on real ext4 and
tmpfs. The tests pin, against the accepted plan revision 4.7 (state machine "SM", failure matrix
"FM"):

* R6: R6-N (``failed`` (b) only when all six R6N-Ω checks pass, whatever the errno), R6-N′, R6-PD,
  R6-P (including the R6C-Ω capture, DG-1), R6-O, R6-W, R6-D, R6-D′ and R6-R (T3-10, T3-15, T3-22,
  T3-24, T3-7, T3-12);
* R7, R8: a data-file failure goes to FE-Ω and then W-14 (``failed``), W-15 (``uncertain``) or no
  write; a foreign identity fails FE-6 (OD-20);
* the U-update u1 to u9 at W-07, W-09 and W-10, branch by branch (T3-19, T3-25, T3-26): W-16 runs
  only after u3, u6 or u7, never after u1, u2, u4 or u5 (OD-24); Q-Ω decides ``failed`` (c) or
  ``uncertain``; ``confirmed`` follows K-4;
* R9-Ω: a K-G failure or incomplete check writes nothing; K-F/K-L go through FE-Ω;
* in every case: nothing is removed except the run's own T(x) by W-16, the exchange is never
  called, and the next report is the one the plan predicts.

Synthetic data in temporary folders only. Process kills are in ``test_restore_kill_points.py``.
"""

import errno
import os

import pytest

from lpbfstudio import restore as rs
from lpbfstudio import restore_record as rr
from lpbfstudio.fsprim import Incomplete

from _restore_support import (  # noqa: F401 (fixtures)
    F, M, O, RS, Res, Trace, fail_at, fs_root, names, nothing_removed, only_attempt, oserror, posix, record_of,
    replace_same_bytes, report, restore, sets,
)

pytestmark = posix


def nth(label: str, n: int):
    """A ``what`` predicate that matches the ``n``-th call (1-based) with ``label``."""
    seen = {"k": 0}

    def test(what):
        if what != label:
            return False
        seen["k"] += 1
        return seen["k"] == n
    return test


def no_exchange(trace: Trace) -> None:
    assert not trace.has("exchange"), "the exchange must never be called on this path"


def tmp_files(a) -> list:
    return [n for n in os.listdir(a) if n.startswith(f".{rr.RECORD_NAME}.")]


# ================================================================ R6-N, R6-N′ (T3-15)


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.ENOENT])
def test_r6n_a_mkdir_error_with_all_six_checks_passing_is_failed_b(sets, monkeypatch, code):
    trace = Trace(monkeypatch)
    fail_at(monkeypatch, "_mkdirat", "W-02", oserror(code))
    out = restore(sets)
    assert out.outcome is Res.FAILED and out.failed_kind == "b" and out.confirmed, out.reason
    assert out.record_durable == "n/a" and out.record_observed == "none"
    assert list((sets / M / RS).iterdir()) == []
    assert not trace.has("open", "W-05 restore.json")
    # no write between the mkdirat error and the end of R6N-Ω
    after = trace.events[trace.index("mkdir", "W-02") + 1:]
    assert not [e for e in after if e[0] in ("mkdir", "open", "write", "rename", "unlink")]
    assert report(sets).rule == "P12"


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.ENOENT])
@pytest.mark.parametrize("variant", ["foreign-entry", "entry-at-rid", "listing-error", "rid-lstat-error",
                                     "rs-replaced"])
def test_r6n_prime_any_failing_check_gives_uncertain_with_no_record(sets, monkeypatch, code, variant):
    rsdir = sets / M / RS
    if variant == "foreign-entry":
        fail_at(monkeypatch, "_mkdirat", "W-02", oserror(code), before=lambda *a: (rsdir / "x").mkdir())
    elif variant == "entry-at-rid":
        monkeypatch.setattr(rs, "_mkdirat", _after_real(rs._mkdirat, "W-02", oserror(code)))
    elif variant == "rs-replaced":
        _replace_rs_after_error(monkeypatch, sets, code)
    else:
        fail_at(monkeypatch, "_mkdirat", "W-02", oserror(code))
        if variant == "listing-error":
            fail_at(monkeypatch, "_listing", nth("RS", 3), Incomplete("listing raised"))
        else:
            fail_at(monkeypatch, "_lstat_at", "rid", oserror(errno.EIO))
    out = restore(sets)
    assert out.outcome is Res.UNCERTAIN and not out.confirmed, out.reason
    assert out.record_observed == "none"
    if variant == "entry-at-rid":
        assert out.predicted_report == "G6"
        assert report(sets).rule == "G6"


def _after_real(real, label, exc):
    def mkdirat(dir_fd, name, what):
        if what == label:
            real(dir_fd, name, what)
            raise exc
        return real(dir_fd, name, what)
    return mkdirat


def _replace_rs_after_error(monkeypatch, sets, code):
    real = rs._mkdirat

    def mkdirat(dir_fd, name, what):
        if what == "W-02":
            os.rename(sets / M / RS, sets / M / "rs-moved")
            (sets / M / RS).mkdir()
            raise oserror(code)
        return real(dir_fd, name, what)
    monkeypatch.setattr(rs, "_mkdirat", mkdirat)


# ================================================================ R6-PD (T3-10), R6-P, R6C-Ω (T3-22)


def test_r6pd_the_attempt_folder_is_kept_and_nothing_else_is_written(sets, monkeypatch):
    trace = Trace(monkeypatch)
    fail_at(monkeypatch, "_fsync", "RS")
    out = restore(sets)
    assert out.outcome is Res.UNCERTAIN and not out.confirmed and out.attempt_dir == "created-not-durable"
    assert not trace.has("mkdir", "W-03") and not trace.has("mkdir", "W-04")
    a = only_attempt(sets)
    assert list(a.iterdir()) == []
    assert report(sets).rule == "G6"


def test_r6pd_variant_b_the_folder_gone_before_the_observation(sets, monkeypatch):
    def remove(*a):
        for p in (sets / M / RS).iterdir():
            p.rmdir()
    fail_at(monkeypatch, "_fsync", "RS", before=remove)
    out = restore(sets)
    assert out.outcome is Res.UNCERTAIN and out.facts("attempt folder") == ["absent"]
    assert out.predicted_report == "unchanged" and report(sets).rule == "P12"


@pytest.mark.parametrize("helper,label", [("_mkdirat", "W-03"), ("_mkdirat", "W-04"), ("_fsync", "A"),
                                          ("_openat", "P"), ("_mount_id", "W")])
def test_r6p_a_partly_created_attempt_is_kept_and_reported_g6(sets, monkeypatch, helper, label):
    trace = Trace(monkeypatch)
    fail_at(monkeypatch, helper, label)
    before = names(sets.parent)
    out = restore(sets)
    assert out.outcome is Res.UNCERTAIN and not out.confirmed, out.reason
    assert not trace.has("open", "W-05 restore.json")
    nothing_removed(before, names(sets.parent))
    assert not (only_attempt(sets) / rr.RECORD_NAME).exists()
    assert report(sets).rule == "G6"


@pytest.mark.parametrize("variant", ["extra-file", "extra-folder", "listing-incomplete", "fstat-error"])
def test_r6c_a_capture_precondition_failure_is_r6p(sets, monkeypatch, variant):
    trace = Trace(monkeypatch)
    real = rs._listing
    if variant.startswith("extra"):
        def listing(fd, what):
            if what == "A" and "made" not in state:
                state["made"] = True
                target = only_attempt(sets) / "extra"
                target.mkdir() if variant == "extra-folder" else target.write_bytes(b"x")
            return real(fd, what)
        state = {}
        monkeypatch.setattr(rs, "_listing", listing)
    elif variant == "listing-incomplete":
        fail_at(monkeypatch, "_listing", "A", Incomplete("changed while listed"))
    else:
        fail_at(monkeypatch, "_fstat", nth("A", 2), oserror(errno.EIO))
    out = restore(sets)
    assert out.outcome is Res.UNCERTAIN and "R6C-Ω" in " ".join(out.facts("R6-P")), out.reason
    assert not trace.has("open", "W-05 restore.json")
    a = only_attempt(sets)
    assert (a / rr.PRESERVED_DIR).is_dir() and (a / rr.SWAP_DIR).is_dir()
    # no write between the W-04 fsync(A) and the end of R6C-Ω
    i = trace.index("fsync", "A")
    assert not [e for e in trace.events[i + 1:] if e[0] in ("mkdir", "open", "write", "rename", "unlink")]


def test_r6c_the_captured_nlink_is_recorded_once_and_used(sets, monkeypatch):
    out = restore(sets)
    assert out.outcome is Res.RESTORED
    assert len(out.facts("I_A_nlink_after_W04")) == 1  # an observation; no numeric constant is asserted


def test_a_later_nlink_mismatch_of_the_attempt_folder_fails_k_g(sets, monkeypatch):
    real = rs._listing

    def listing(fd, what):
        got = real(fd, what)
        if what == "R9 RS":
            (sets / M / RS / only_attempt(sets).name / "sub").mkdir()
            got = real(fd, what)
        return got
    monkeypatch.setattr(rs, "_listing", listing)
    trace = Trace(monkeypatch)
    out = restore(sets)
    assert out.outcome is Res.UNCERTAIN and out.predicted_report == "G6", out.reason
    no_exchange(trace)


# ================================================================ W-05 (R6-O, R6-W, R6-D, R6-D′, R6-R; T3-24)


@pytest.mark.parametrize("leave_file", [False, True])
def test_r6o_the_record_file_cannot_be_created(sets, monkeypatch, leave_file):
    def leave(dir_fd, name, flags, mode, what):
        if leave_file:
            os.close(os.open(name, os.O_CREAT | os.O_WRONLY, 0o644, dir_fd=dir_fd))
    trace = Trace(monkeypatch)
    fail_at(monkeypatch, "_openat", "W-05 restore.json", oserror(errno.ENOSPC), before=leave)
    out = restore(sets)
    assert out.outcome is Res.UNCERTAIN and out.last_observed_op == "W-04", out.reason
    assert "attempted" in out.facts("W-05")
    assert not trace.has("fsync", "W-05 restore.json") and not trace.has("unlink")
    assert report(sets).rule == "G6"


def test_r6w_fstat_after_open_fails(sets, monkeypatch):
    fail_at(monkeypatch, "_fstat", "W-05 restore.json")
    out = restore(sets)
    assert out.outcome is Res.UNCERTAIN and "identity not established" in out.reason
    assert (only_attempt(sets) / rr.RECORD_NAME).read_bytes() == b""  # kept: no unlink
    assert report(sets).rule == "G6"


@pytest.mark.parametrize("written", [0, 10])
def test_r6w_a_write_error_never_assumes_zero_bytes(sets, monkeypatch, written):
    def part(fd, data, what):
        os.write(fd, data[:written])
    fail_at(monkeypatch, "_write", "W-05 restore.json", before=part)
    out = restore(sets)
    assert out.outcome is Res.UNCERTAIN and "partly written" in out.reason
    assert len((only_attempt(sets) / rr.RECORD_NAME).read_bytes()) == written
    assert report(sets).rule == "G6"


@pytest.mark.parametrize("label", ["W-05 restore.json", "A"])
def test_r6d_a_record_fsync_failure_with_an_exact_read_back_is_failed_c(sets, monkeypatch, label):
    trace = Trace(monkeypatch)
    fail_at(monkeypatch, "_fsync", nth(label, 1 if label != "A" else 2))
    out = restore(sets)
    assert out.outcome is Res.FAILED and out.failed_kind == "c" and not out.confirmed, out.reason
    assert (out.record_observed, out.record_durable) == ("preparing", "no")
    assert not trace.has("open", "W-06 P/f")
    assert report(sets).rule == "P18"


def test_r6d_prime_fsync_failure_and_an_incomplete_read_back(sets, monkeypatch):
    fail_at(monkeypatch, "_fsync", "W-05 restore.json")
    fail_at(monkeypatch, "_read", "restore.json", Incomplete("read raised"))
    out = restore(sets)
    assert out.outcome is Res.UNCERTAIN and out.record_observed == "unknown"


def test_r6r_the_read_back_fails_after_every_syscall_succeeded(sets, monkeypatch):
    fail_at(monkeypatch, "_read", "restore.json", Incomplete("read raised"))
    out = restore(sets)
    assert out.outcome is Res.UNCERTAIN and out.record_durable == "yes" and out.record_observed == "unknown"
    assert report(sets).rule == "P18"


# ================================================================ R7, R8: FE-Ω, W-14, W-15


@pytest.mark.parametrize("helper,label", [("_fsync", "W-06 P/f"), ("_fsync", "P"), ("_openat", "W-06 P/f"),
                                          ("_write", "W-06 P/f")])
def test_r7_a_preserved_copy_failure_with_every_fe_item_passing_is_failed(sets, monkeypatch, helper, label):
    trace = Trace(monkeypatch)
    before = names(sets.parent)
    fail_at(monkeypatch, helper, label)
    out = restore(sets)
    assert out.outcome is Res.FAILED and out.failed_kind == "a" and out.confirmed, out.reason
    assert record_of(only_attempt(sets)).status is rr.RestoreStatus.FAILED
    no_exchange(trace)
    nothing_removed(before, names(sets.parent))
    assert (report(sets).outcome, report(sets).rule) == (O.VERIFIED_UPGRADED, "P24")


def test_r7b_a_foreign_identity_fails_fe6_and_gives_uncertain(sets, monkeypatch):
    real = rs._close

    def close(fd, what):
        real(fd, what)
        if what == "W-06 P/f":
            replace_same_bytes(only_attempt(sets) / rr.PRESERVED_DIR / F)  # equal bytes, another inode
    monkeypatch.setattr(rs, "_close", close)
    trace = Trace(monkeypatch)
    out = restore(sets)
    assert out.outcome is Res.UNCERTAIN, out.reason
    # R7b itself stops the run: no W-07, so the later re-checks are not what catches it
    assert "is another file than the one written" in out.reason
    assert not trace.has("read", "W-07.u1")
    assert record_of(only_attempt(sets)).status is rr.RestoreStatus.UNCERTAIN
    assert report(sets).rule == "P21"


def test_r7_a_g6_condition_seen_by_fe3_writes_nothing(sets, monkeypatch):
    def extra(*a):
        (only_attempt(sets) / "stray").write_bytes(b"x")
    fail_at(monkeypatch, "_fsync", "W-06 P/f", before=extra)
    out = restore(sets)
    assert out.outcome is Res.UNCERTAIN and out.predicted_report == "G6"
    assert record_of(only_attempt(sets)).status is rr.RestoreStatus.PREPARING
    assert report(sets).rule == "G6"


def test_r7_fe4_a_replaced_record_writes_nothing(sets, monkeypatch):
    def swap(*a):
        replace_same_bytes(only_attempt(sets) / rr.RECORD_NAME)
    fail_at(monkeypatch, "_fsync", "W-06 P/f", before=swap)
    out = restore(sets)
    assert out.outcome is Res.UNCERTAIN and out.predicted_report == "unknown"


def test_r8_a_staged_original_failure_is_failed(sets, monkeypatch):
    trace = Trace(monkeypatch)
    fail_at(monkeypatch, "_fsync", "W-08 W/f")
    out = restore(sets)
    assert out.outcome is Res.FAILED and out.failed_kind == "a", out.reason
    assert (only_attempt(sets) / rr.SWAP_DIR / F).exists()  # kept
    no_exchange(trace)
    assert report(sets).rule == "P24"


# ================================================================ the U-update at W-07, W-09, W-10 (T3-19, T3-25, T3-26)

BRANCHES = {
    # step: (helper, label suffix, expected outcome, failed kind, confirmed, W-16 runs)
    "u3": ("_write", "u3 T(x)", Res.FAILED, "c", True, True),
    "u4": ("_fsync", "u4 T(x)", Res.UNCERTAIN, None, False, False),
    "u5": ("_fsync", "u5 A", Res.UNCERTAIN, None, False, False),
    "u6": ("_lstat_at", "u6", Res.FAILED, "c", True, True),
    "u7": ("_renameat", "u7", Res.FAILED, "c", True, True),
    "u8": ("_fsync", "u8 A", Res.FAILED, "c", False, False),
    "u9": ("_read", "u9", Res.FAILED, "c", True, False),
}
REPORT_OLD = {"W-07": "P18", "W-09": "P19", "W-10": "P19"}
REPORT_NEW = {"W-07": "P19", "W-09": "P19", "W-10": "P19"}


@pytest.mark.parametrize("op", ["W-07", "W-09", "W-10"])
@pytest.mark.parametrize("step", sorted(BRANCHES))
def test_a_u_update_failure_branch_by_branch(sets, monkeypatch, op, step):
    helper, suffix, want, kind, confirmed, w16 = BRANCHES[step]
    exc = Incomplete("read-back raised") if helper == "_read" else oserror(errno.EIO)
    trace = Trace(monkeypatch)
    fail_at(monkeypatch, helper, f"{op}.{suffix}", exc)
    before = names(sets.parent)
    out = restore(sets)
    assert out.outcome is want and out.failed_kind == kind and out.confirmed is confirmed, out.reason
    no_exchange(trace)
    a = only_attempt(sets)
    assert trace.has("unlink") is w16
    if step in ("u4", "u5"):  # OD-24: strict D-STOP, T(x) left, no u10
        assert len(tmp_files(a)) == 1 and not trace.has("fsync", f"{op}.u10 A")
        assert report(sets).rule == "G6"
        nothing_removed(before, names(sets.parent))
    else:
        assert tmp_files(a) == []
        rule = REPORT_NEW[op] if step in ("u8", "u9") else REPORT_OLD[op]
        assert report(sets).rule == rule
    if step == "u8":
        assert out.record_durable == "no"


@pytest.mark.parametrize("op", ["W-07", "W-10"])
def test_u10_failure_after_w16_is_tmp_removal_not_durable(sets, monkeypatch, op):
    fail_at(monkeypatch, "_write", f"{op}.u3 T(x)")
    fail_at(monkeypatch, "_fsync", f"{op}.u10 A")
    out = restore(sets)
    assert out.outcome is Res.FAILED and not out.confirmed
    assert out.record_durable == "tmp-removal-not-durable"


@pytest.mark.parametrize("op", ["W-07", "W-10"])
def test_u1_failure_reads_only_and_runs_no_q_omega(sets, monkeypatch, op):
    trace = Trace(monkeypatch)
    fail_at(monkeypatch, "_read", f"{op}.u1", Incomplete("read raised"))
    out = restore(sets)
    assert out.outcome is Res.UNCERTAIN and not out.confirmed and out.predicted_report == "unknown"
    assert not trace.has("list", "Q-Ω A") and not trace.has("unlink") and not trace.has("open", f"{op}.u2 T(x)")
    no_exchange(trace)


@pytest.mark.parametrize("op", ["W-07", "W-10"])
@pytest.mark.parametrize("variant", ["openat-no-file", "openat-left-file", "fstat-error"])
def test_u2_failure_never_runs_w16(sets, monkeypatch, op, variant):
    trace = Trace(monkeypatch)
    if variant == "fstat-error":
        fail_at(monkeypatch, "_fstat", f"{op}.u2 T(x)")
    else:
        def leave(dir_fd, name, flags, mode, what):
            if variant == "openat-left-file":
                os.close(os.open(name, os.O_CREAT | os.O_WRONLY, 0o644, dir_fd=dir_fd))
        fail_at(monkeypatch, "_openat", f"{op}.u2 T(x)", before=leave)
    out = restore(sets)
    assert not trace.has("unlink") and not trace.has("fsync", f"{op}.u10 A")
    no_exchange(trace)
    if variant == "openat-no-file":
        assert out.outcome is Res.FAILED and out.failed_kind == "c" and out.confirmed
        assert report(sets).rule == REPORT_OLD[op]
    else:
        assert out.outcome is Res.UNCERTAIN and out.predicted_report == "G6"
        assert len(tmp_files(only_attempt(sets))) == 1 and report(sets).rule == "G6"


def test_w16_unlink_failure_leaves_tmp_and_gives_uncertain(sets, monkeypatch):
    fail_at(monkeypatch, "_write", "W-07.u3 T(x)")
    fail_at(monkeypatch, "_unlinkat", "W-07.W-16")
    out = restore(sets)
    assert out.outcome is Res.UNCERTAIN and out.predicted_report == "G6"
    assert report(sets).rule == "G6"


def test_q_omega_incomplete_gives_uncertain(sets, monkeypatch):
    fail_at(monkeypatch, "_write", "W-07.u3 T(x)")
    fail_at(monkeypatch, "_listing", "Q-Ω A", Incomplete("listing raised"))
    out = restore(sets)
    assert out.outcome is Res.UNCERTAIN and out.predicted_report == "unknown"


def test_a_status_only_match_is_never_accepted_by_q_omega(sets, monkeypatch):
    # after a u7 failure the record is replaced by an equal-status record with other bytes
    def replace(*a):
        p = only_attempt(sets) / rr.RECORD_NAME
        p.write_bytes(p.read_bytes().replace(b'"preparing"', b'"preparing" '))
    fail_at(monkeypatch, "_renameat", "W-07.u7", before=replace)
    out = restore(sets)
    assert out.outcome is Res.UNCERTAIN and out.record_observed == "unknown"


# ================================================================ R9-Ω


def _before_r9(monkeypatch, action):
    real = rs._r9

    def r9(run):
        action()
        return real(run)
    monkeypatch.setattr(rs, "_r9", r9)


def test_r9_a_g6_condition_writes_nothing(sets, monkeypatch):
    trace = Trace(monkeypatch)
    _before_r9(monkeypatch, lambda: (only_attempt(sets) / "stray").write_bytes(b"x"))
    out = restore(sets)
    assert out.outcome is Res.UNCERTAIN and out.predicted_report == "G6"
    rec = record_of(only_attempt(sets))
    assert rec.status is rr.RestoreStatus.PRESERVED and rec.staged_identity is not None
    no_exchange(trace)
    assert report(sets).rule == "G6"


def test_r9_a_changed_earlier_attempt_is_a_g6_condition(sets, monkeypatch):
    # P24: an earlier failed attempt is part of the baseline; an entry added to it is never ours
    fail_at(monkeypatch, "_fsync", "W-06 P/f")
    assert restore(sets).outcome is Res.FAILED
    monkeypatch.undo()
    old = only_attempt(sets)
    trace = Trace(monkeypatch)
    _before_r9(monkeypatch, lambda: (old / f".{rr.RECORD_NAME}.0123456789ab.tmp").write_bytes(b"x"))
    out = restore(sets)
    assert out.outcome is Res.UNCERTAIN and out.predicted_report == "G6", out.reason
    no_exchange(trace)


def test_r9_an_incomplete_g6_check_writes_nothing(sets, monkeypatch):
    fail_at(monkeypatch, "_listing", "R9 RS", Incomplete("changed while listed"))
    out = restore(sets)
    assert out.outcome is Res.UNCERTAIN and out.predicted_report == "unknown"
    assert record_of(only_attempt(sets)).status is rr.RestoreStatus.PRESERVED


def test_r9_a_changed_backup_is_failed_and_reported_p24_conflict(sets, monkeypatch):
    def edit_backup():
        p = sets / M / F
        with open(p, "r+b") as fh:
            data = fh.read()
            fh.seek(0)
            fh.write(data.replace(b"{", b" ", 1))
    trace = Trace(monkeypatch)
    _before_r9(monkeypatch, edit_backup)
    out = restore(sets)
    assert out.outcome is Res.FAILED and out.failed_kind == "a", out.reason
    no_exchange(trace)
    got = report(sets)
    assert (got.outcome, got.rule) == (O.CONFLICT, "P24")


def test_r9_a_new_entry_in_the_sets_folder_is_failed(sets, monkeypatch):
    _before_r9(monkeypatch, lambda: (sets / "new.json").write_bytes(b"{}"))
    out = restore(sets)
    assert out.outcome is Res.FAILED and "K-L" in out.reason


def test_r9_an_incomplete_file_check_goes_to_w15(sets, monkeypatch):
    fail_at(monkeypatch, "_read", "R9 B", Incomplete("read raised"))
    out = restore(sets)
    assert out.outcome is Res.UNCERTAIN, out.reason
    assert record_of(only_attempt(sets)).status is rr.RestoreStatus.UNCERTAIN
    assert report(sets).rule == "P21"


def test_a_normal_save_during_the_run_is_refused_and_the_restore_is_unaffected(sets, monkeypatch):
    # lock test 3 inside one process: the exclusive locks are held from R2 to the end
    from lpbfstudio.library import Library, LibraryError
    library = Library(sets.parent, seed=False)
    assert library.sets_dir == sets
    es = library.get("Q1")
    es.description = "edited during the restore"
    refusals = []
    real = rs._r9

    def r9(run):
        with pytest.raises(LibraryError) as info:
            library.save(es)
        refusals.append(type(info.value).__name__)
        return real(run)
    monkeypatch.setattr(rs, "_r9", r9)
    assert restore(sets).outcome is Res.RESTORED
    assert refusals == ["LibraryLocked"]
    library.save(es)
