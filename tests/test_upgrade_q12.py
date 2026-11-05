"""Q-12: the upgrade reports its record's state as the disk holds it (B7 Phase 2, S3-B2b).

When updating the migration record fails after the record's rename may already have happened
(the read-back fails or shows other bytes: failure point F8c / W6), ``upgrade_file`` used to say
"it stays backed_up" from its in-memory record, although the disk could already hold the final
record. The outcome must instead come from the record as read back once from disk, without
writing anything: the confirmed state when the record reads back and parses, and an explicit
"unknown" when it does not. The result itself (uncertain / failed) is unchanged, nothing is
retried or overwritten, and no extra file is created or removed.

Synthetic data in temporary folders only.
"""

import errno
import sys
from pathlib import Path

import pytest

from lpbfstudio import migration as mig
from lpbfstudio import reconcile as rc
from lpbfstudio import reconcile_apply as ra
from lpbfstudio import upgrade as up
from lpbfstudio.library import load_json
from lpbfstudio.migration import FileResult, RecordStatus
from test_upgrade_file import ORIGINAL, lib, only_record, sets  # noqa: F401 (fixtures)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX primitives; Windows is refused (see upgrade.py)")

M, R = up.MIGRATION_DIR, up.RECORDS_DIR


def fail_read_back(monkeypatch, nth: int, *, times: int | None, garble: bool = False):
    """After the ``nth`` record rename (1 = backed_up, 2 = final) has really happened, make reading
    that record fail ``times`` times (None: always), or append garbage to it (``garble``)."""
    real_rename, real_read = up._rename, up._read_file
    state = {"n": 0, "path": None, "left": times}

    def rename(src, dst):
        real_rename(src, dst)
        if Path(src).parent.name == R:
            state["n"] += 1
            if state["n"] == nth:
                state["path"] = Path(dst)
                if garble:
                    Path(dst).write_bytes(Path(dst).read_bytes() + b"garbage")

    def read(path):
        if not garble and state["path"] is not None and Path(path) == state["path"]:
            if state["left"] is None or state["left"] > 0:
                if state["left"] is not None:
                    state["left"] -= 1
                raise OSError(errno.EIO, "Input/output error")
        return real_read(path)
    monkeypatch.setattr(up, "_rename", rename)
    monkeypatch.setattr(up, "_read_file", read)


def leftovers(sets: Path) -> list[str]:
    hidden = [p.name for p in sets.iterdir() if p.name.startswith(".") and p.name != M]
    return hidden + [p.name for p in (sets / M / R).iterdir() if p.name.startswith(".")]


def test_final_record_renamed_but_read_back_failed_reports_the_record_as_read_from_disk(sets, monkeypatch):
    fail_read_back(monkeypatch, 2, times=1)  # the read-back fails once; the record is on disk
    out = up.upgrade_file(sets, "P1.json")
    monkeypatch.undo()
    assert out.result is FileResult.UNCERTAIN  # unchanged: the run could not verify its record
    assert "stays backed_up" not in out.reason and "backed_up" not in out.reason, out.reason
    assert "reads back as upgraded" in out.reason, out.reason
    disk = only_record(sets)
    assert disk.status is RecordStatus.UPGRADED
    assert out.record == disk  # the record state confirmed on disk, never the stale one
    assert load_json(sets / "P1.json").schema_version == 2
    assert leftovers(sets) == []


def test_final_record_that_cannot_be_read_back_at_all_is_reported_unknown(sets, monkeypatch):
    fail_read_back(monkeypatch, 2, times=None)
    out = up.upgrade_file(sets, "P1.json")
    monkeypatch.undo()
    assert out.result is FileResult.UNCERTAIN
    assert "backed_up" not in out.reason, out.reason
    assert "cannot be read back" in out.reason and "unknown" in out.reason, out.reason
    assert out.record is None  # no state is claimed that the disk did not confirm
    assert only_record(sets).status is RecordStatus.UPGRADED  # what really happened
    assert leftovers(sets) == []
    # the disk decides afterwards: a verified completed upgrade, and nothing to apply
    report = rc.reconcile_file(sets, "P1.json")
    assert (report.outcome, report.rule) == (rc.Outcome.VERIFIED_UPGRADED, "P12")
    assert ra.apply_file(sets, "P1.json").result is ra.ApplyResult.NO_WRITE


def test_final_record_that_reads_back_as_other_bytes_is_reported_unknown(sets, monkeypatch):
    fail_read_back(monkeypatch, 2, times=None, garble=True)
    out = up.upgrade_file(sets, "P1.json")
    monkeypatch.undo()
    assert out.result is FileResult.UNCERTAIN
    assert "backed_up" not in out.reason and "unknown" in out.reason, out.reason
    assert out.record is None


def test_final_record_write_that_never_happened_reports_backed_up_as_confirmed(sets, monkeypatch):
    real = up._update_record

    def fail_final(path, old, new):
        if new.status is RecordStatus.UPGRADED:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(path, old, new)
    monkeypatch.setattr(up, "_update_record", fail_final)
    out = up.upgrade_file(sets, "P1.json")
    monkeypatch.undo()
    assert out.result is FileResult.UNCERTAIN
    assert "reads back as backed_up" in out.reason, out.reason
    assert "stays" not in out.reason  # a confirmed reading, not an assumption
    assert out.record == only_record(sets) and out.record.status is RecordStatus.BACKED_UP


def test_backed_up_record_renamed_but_read_back_failed_is_reported_as_read_from_disk(sets, monkeypatch):
    # the backed_up update's read-back fails once: the run stops before any replace; the stop's own
    # record update must not overwrite the backed_up record it did not confirm, and the outcome
    # names the state the disk holds
    fail_read_back(monkeypatch, 1, times=1)
    out = up.upgrade_file(sets, "P1.json")
    monkeypatch.undo()
    assert out.result is FileResult.FAILED  # unchanged: the file is verified unchanged
    assert (sets / "P1.json").read_bytes() == ORIGINAL
    disk = only_record(sets)
    assert disk.status is RecordStatus.BACKED_UP  # not overwritten by the stop
    assert "reads back as backed_up" in out.reason, out.reason
    assert out.record == disk
    assert leftovers(sets) == []
    # the disk decides afterwards: P7, which apply may finalise
    assert rc.reconcile_file(sets, "P1.json").rule == "P7"


def test_stop_record_update_failure_with_an_unreadable_record_is_reported_unknown(sets, monkeypatch):
    fail_read_back(monkeypatch, 1, times=None)
    out = up.upgrade_file(sets, "P1.json")
    monkeypatch.undo()
    assert out.result is FileResult.FAILED
    assert "unknown" in out.reason and out.record is None, out.reason
    assert only_record(sets).status is RecordStatus.BACKED_UP


def test_successful_upgrade_is_unchanged(sets):
    out = up.upgrade_file(sets, "P1.json")
    assert out.result is FileResult.UPGRADED and out.reason == "upgraded to format 2"
    assert out.record == only_record(sets) and out.record.status is RecordStatus.UPGRADED


def test_reading_the_record_back_writes_nothing(sets, monkeypatch):
    # counting every write helper: the corrected reporting adds reads only
    fail_read_back(monkeypatch, 2, times=None)
    calls = []
    for name in ("_write_new", "_rename", "_unlink", "_replace"):
        real = getattr(up, name)

        def spy(*a, _real=real, _name=name, **k):
            calls.append(_name)
            return _real(*a, **k)
        monkeypatch.setattr(up, name, spy)
    up.upgrade_file(sets, "P1.json")
    monkeypatch.undo()
    # record, backup, record tmp x2, staged file; renames: record x2 (+ replace); no unlink
    assert calls.count("_write_new") == 5 and calls.count("_unlink") == 0, calls
    assert calls.count("_replace") == 1


def test_outcome_record_is_never_a_state_the_disk_does_not_hold(sets, monkeypatch):
    fail_read_back(monkeypatch, 2, times=1)
    out = up.upgrade_file(sets, "P1.json")
    monkeypatch.undo()
    assert out.record is None or out.record == mig.parse_record(
        (sets / M / R / f"{out.record.record_id}.json").read_text(encoding="utf-8"))


def test_final_record_replaced_by_a_different_record_is_reported_unknown(sets, monkeypatch):
    # after the rename, a lock-ignoring writer puts a valid but different record under the same name
    import json
    real = up._rename
    n = [0]

    def rename(src, dst):
        real(src, dst)
        if Path(src).parent.name == R:
            n[0] += 1
            if n[0] == 2:
                data = json.loads(Path(dst).read_text(encoding="utf-8"))
                data["created_at"] = "2020-01-01T00:00:00Z"
                Path(dst).write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(up, "_rename", rename)
    out = up.upgrade_file(sets, "P1.json")
    monkeypatch.undo()
    assert out.result is FileResult.UNCERTAIN
    assert "another record" in out.reason and "unknown" in out.reason, out.reason
    assert out.record is None
