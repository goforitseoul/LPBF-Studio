"""Restore's own lock release and descriptor closing, fault-injected (B3 acceptance review, 2026-11-08).

Restore takes its locks through ``locks.acquire_exclusive`` on descriptors it owns (the sets folder
and M, held in the operation's context), so under the accepted B1b contract ``Held.release`` only
unlocks them: a failed ``LOCK_UN`` is kept in ``Held.unlock_error`` and the lock lasts until the
descriptor is closed. Restore then closes every descriptor it opened, once. These tests inject a
failure at the narrowest boundaries, ``locks._flock`` for ``LOCK_UN`` and ``restore._close``, and pin:

* the outcome decided before the release is kept: its result, ``confirmed`` and every record and
  exchange field (cleanup never replaces the outcome); the release facts are appended to the reason
  and the observations, never lost;
* before the exchange, after the exchange, and on top of an earlier failure;
* an interrupt that is already propagating is not replaced by a release failure;
* every descriptor is closed exactly once and the locks are free afterwards, whatever failed.

Synthetic data in temporary folders only.
"""

import errno
import os

import pytest

from lpbfstudio import locks
from lpbfstudio import restore as rs
from lpbfstudio import restore_record as rr

from _restore_support import (  # noqa: F401 (fixtures)
    F, M, ORIGINAL, Res, exchange_raising, fail_at, fs_root, lock_free, only_attempt, oserror, posix, record_of,
    report, restore, sets,
)

pytestmark = posix


def fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


def unlock_fails(monkeypatch, which=("sets", "m")):
    """``LOCK_UN`` on restore's sets and/or M descriptor raises EIO (locks.py's narrow helper)."""
    real = locks._flock
    seen = {"locks": []}

    def flock(fd, op):
        if op & locks.fcntl.LOCK_UN:
            target = "m" if os.path.basename(os.readlink(f"/proc/self/fd/{fd}")) == M else "sets"
            seen["locks"].append(target)
            if target in which:
                raise oserror(errno.EIO)
        return real(fd, op)
    monkeypatch.setattr(locks, "_flock", flock)
    return seen


def closes_fail(monkeypatch, what: str):
    """``restore._close`` closes for real, then reports EIO, for descriptors labelled ``what``."""
    real = rs._close
    closed = []

    def close(fd, label):
        real(fd, label)
        if label == what:
            closed.append(fd)
            raise oserror(errno.EIO)
    monkeypatch.setattr(rs, "_close", close)
    return closed


def release_facts(out) -> list[str]:
    return out.facts("lock release")


# ================================================================ before the exchange


@pytest.mark.parametrize("which", [("sets",), ("m",), ("sets", "m")])
def test_an_unlock_failure_after_a_refusal_keeps_the_refusal(sets, monkeypatch, which):
    (sets / F).write_bytes((sets / F).read_bytes().replace(b'"description"', b'"description" ', 1))  # P14
    seen = unlock_fails(monkeypatch, which)
    start = fd_count()
    out = restore(sets)
    assert out.outcome is Res.REFUSED_NOT_RESTORABLE and out.confirmed, out.reason
    assert any("unlocking failed" in t for t in release_facts(out)) and "releasing the locks" in out.reason
    assert out.reason.startswith("not restorable")  # the original reason comes first, unchanged
    assert sorted(set(seen["locks"])) == ["m", "sets"]  # both unlocks were attempted, M first
    assert seen["locks"][0] == "m"
    assert fd_count() == start and lock_free(sets)


def test_an_unlock_failure_after_a_failure_before_the_exchange(sets, monkeypatch):
    fail_at(monkeypatch, "_fsync", "W-06 P/f")
    unlock_fails(monkeypatch)
    out = restore(sets)
    assert out.outcome is Res.FAILED and out.failed_kind == "a" and out.confirmed, out.reason
    assert record_of(only_attempt(sets)).status is rr.RestoreStatus.FAILED
    assert release_facts(out) and lock_free(sets)
    assert report(sets).rule == "P24"


def test_a_close_failure_of_the_attempt_folders_is_reported_and_nothing_leaks(sets, monkeypatch):
    fail_at(monkeypatch, "_fsync", "W-06 P/f")
    closed = closes_fail(monkeypatch, "attempt folder")
    start = fd_count()
    out = restore(sets)
    assert out.outcome is Res.FAILED and out.confirmed
    assert len(closed) == 3 and len(release_facts(out)) == 3  # A, P and W, each once
    assert fd_count() == start and lock_free(sets)


# ================================================================ after the exchange


def test_an_unlock_failure_after_a_restore_keeps_the_confirmed_restore(sets, monkeypatch):
    unlock_fails(monkeypatch)
    out = restore(sets)
    assert out.outcome is Res.RESTORED and out.confirmed, out.reason
    assert (out.record_durable, out.exchange_durable, out.last_durable_op) == ("yes", "yes", "W-13")
    assert (sets / F).read_bytes() == ORIGINAL and record_of(only_attempt(sets)).status is rr.RestoreStatus.RESTORED
    assert release_facts(out) and lock_free(sets)
    assert report(sets).rule == "P20R"


def test_a_library_folder_close_failure_after_a_restore_is_reported(sets, monkeypatch):
    closed = closes_fail(monkeypatch, "library folder")
    start = fd_count()
    out = restore(sets)
    assert out.outcome is Res.RESTORED and out.confirmed
    assert closed and len(release_facts(out)) == len(closed)
    assert fd_count() == start and lock_free(sets)


# ================================================================ on top of an earlier failure


def test_an_unlock_failure_after_a_w12_failure_keeps_the_uncertain_outcome(sets, monkeypatch):
    fail_at(monkeypatch, "_fsync", "W-12 sets")
    unlock_fails(monkeypatch)
    out = restore(sets)
    assert out.outcome is Res.UNCERTAIN and not out.confirmed and out.exchange_durable == "no", out.reason
    assert out.facts("W-12 fsync(sets_fd)") == ["error:EIO"] and out.facts("W-12 fsync(W_fd)") == ["success"]
    assert out.reason.startswith("exchanged; durability of the exchange not confirmed")
    assert release_facts(out) and lock_free(sets)


class Abort(BaseException):
    pass


def test_an_interrupt_is_not_replaced_by_a_release_failure(sets, monkeypatch):
    fail_at(monkeypatch, "_fsync", "W-12 W", Abort())
    unlock_fails(monkeypatch)
    closes_fail(monkeypatch, "library folder")
    start = fd_count()
    with pytest.raises(Abort):
        rs.restore_file(sets, F)
    assert fd_count() == start and lock_free(sets)
    assert record_of(only_attempt(sets)).status is rr.RestoreStatus.EXCHANGING
    assert report(sets).rule == "P20"


def test_every_descriptor_is_closed_exactly_once(sets, monkeypatch):
    real = rs._close
    closed = []

    def close(fd, label):
        if label in ("attempt folder", "library folder"):
            closed.append(fd)  # folder descriptors are all open together, so their numbers are distinct
        real(fd, label)
    monkeypatch.setattr(rs, "_close", close)
    start = fd_count()
    assert restore(sets).outcome is Res.RESTORED
    assert fd_count() == start
    assert len(closed) == len(set(closed)) >= 6  # sets, M, records, restores, and A, P, W of the attempt


# ================================================================ the two other review fixes (2026-11-08)


def test_a_record_refused_by_the_contract_stops_the_run_with_nothing_written(sets, monkeypatch):
    # fix B: a contract refusal of the next record (never expected) is a fail-closed outcome, not an exception
    real = rs._validated

    def validated(data):
        if isinstance(data, dict) and data.get("status") == rr.RestoreStatus.PRESERVED.value:
            raise rs.RestoreRecordError("refused (injected)")
        return real(data)
    monkeypatch.setattr(rs, "_validated", validated)
    out = restore(sets)
    assert out.outcome is Res.UNCERTAIN and not out.confirmed and "W-07" in out.reason, out.reason
    a = only_attempt(sets)
    assert record_of(a).status is rr.RestoreStatus.PREPARING
    assert [n for n in os.listdir(a) if n.startswith(".")] == []  # no T(x): nothing was written
    assert lock_free(sets)


def test_an_unexpected_programming_error_is_not_turned_into_an_outcome(sets, monkeypatch):
    def validated(data):
        raise TypeError("a programming error (injected)")
    monkeypatch.setattr(rs, "_validated", validated)
    with pytest.raises(TypeError):
        rs.restore_file(sets, F)
    assert lock_free(sets)


def test_a_class_a_failed_record_does_not_say_it_stopped_before_the_exchange(sets, monkeypatch):
    # fix C: the exchange was called; the failed record says so; the result stays failed (E-5)
    exchange_raising(monkeypatch, errno.EXDEV)
    out = restore(sets)
    assert out.outcome is Res.FAILED and out.exchange_called == "error:EXDEV", out.reason
    rec = record_of(only_attempt(sets))
    assert rec.status is rr.RestoreStatus.FAILED and rec.exchange_attempted
    assert "stopped before the exchange" not in rec.error and "the exchange returned EXDEV" in rec.error


def test_a_pre_exchange_failed_record_still_says_it_stopped_before_the_exchange(sets, monkeypatch):
    fail_at(monkeypatch, "_fsync", "W-06 P/f")
    assert restore(sets).outcome is Res.FAILED
    assert record_of(only_attempt(sets)).error.startswith("the restore stopped before the exchange")
