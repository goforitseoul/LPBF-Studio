"""Applying reconciliation: record-only finalisations of interrupted upgrades (B7 Phase 2, S3-B2b).

``apply_file(sets_dir, file_name)`` is the only writing step of reconciliation. It takes the
library upgrade lock and builds exactly the read-only report of ``reconcile.reconcile_file``
(S3-B2a: the same survey, the same decision table G1-G5 and P1-P17, first match wins). It writes
only when that report is ``recoverable`` under one of the three rules the approved S3-B2 design
lets write (``WRITES``):

- **P5**: a ``preparing`` record, and the file holds the original bytes it recorded. The record
  becomes ``failed`` (``migration.advance``, error ``PREPARING_ERROR``).
- **P7**: a ``backed_up`` record, and the file holds the original bytes. The record becomes
  ``failed`` through ``migration.confirm_replace(record, DiskState.SOURCE)``.
- **P8**: a ``backed_up`` record, the file holds the upgraded bytes, parses as format 2 and
  verifies against the backup, which holds the original bytes. The record becomes ``upgraded``
  through ``migration.confirm_replace(record, DiskState.UPGRADED)`` (the project owner's decision Q-1).

Every other rule writes nothing: conflicts, unverifiable and busy libraries, ``uncertain`` and
stale ``failed`` records (Q-2), completed upgrades (P12-P15, P14 included, whatever the file now
holds), and every ``recoverable`` report that an unexpected entry turned into a conflict.

**The one write.** The record file of the decided record is replaced through
``upgrade._update_record``: the record is read again and must be the same file (device and
inode) with the same bytes as when it was surveyed; the new state goes to an exclusively created
temporary file in the records folder, which is renamed over the record and read back. The only
thing ever removed is that temporary file, by ``upgrade._update_record`` itself, when its own
rename fails. Set files, backups, staged files, other records, record temporary files left by
earlier runs and unexpected entries are never written, moved, renamed or deleted. No pass id is
generated: P8 writes the record the interrupted run would have written, from its own digests.

**Before the write** the file (and, for P8, the backup) is read again under the lock and must be
the same file with the same bytes that decided the rule; a change, even a re-save with the same
bytes, refuses the write. This narrows, but cannot close, the window in which a program that
ignores the lock could change a file between the check and the record's rename.

**Results.** ``finalised``: the record was written and read back as written. ``no_write``: the
report's rule permits no write (the report is attached). ``refused``: a P5/P7/P8 decision whose
re-check failed, or whose write failed with the record read again and found unchanged.
``unverified``: the write failed in a way that leaves the record's state unknown to this run
(for example, the read-back after the rename failed); the record is never assumed written or
unwritten, and the next report shows what the disk holds.

**Idempotent.** After a write the file matches a rule that writes nothing (P5 and P7 give P16, P8
gives P12), so a second run on an unchanged disk writes nothing. A run killed before the rename
leaves the old record and its temporary file; the next run re-derives the same rule and writes
the record again (finalising a record, never re-running an upgrade). A run killed after the
rename leaves the final record.

**Not here:** a library-wide or batch apply, restore, quarantine, clean-up of temporary files,
resolution of conflicts, a CLI or GUI, and Windows (refused, G1). The upgrade never runs this.
The read-only report keeps refusing ``apply=True``; it stays a separate, writing-free capability.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from . import reconcile as rc
from . import upgrade as up
from .migration import (
    DiskState, MigrationRecord, RecordError, RecordStatus, advance, confirm_replace, dump_record, parse_record,
)

M, R = up.MIGRATION_DIR, up.RECORDS_DIR

PREPARING_ERROR = ("the upgrade was interrupted before its backup was verified; the file is unchanged "
                   "(finalised by reconciliation)")

# the only rules that write, and the record status each writes
WRITES = {"P5": RecordStatus.FAILED, "P7": RecordStatus.FAILED, "P8": RecordStatus.UPGRADED}


class ApplyResult(str, Enum):
    FINALISED = "finalised"    # the record was moved to its final status and read back as written
    NO_WRITE = "no_write"      # the report's rule permits no write; nothing was written
    REFUSED = "refused"        # a permitted rule, but a re-check or the write failed; the record is unchanged
    UNVERIFIED = "unverified"  # the write failed and the record's state is unknown; run the report again


@dataclass(frozen=True)
class ApplyOutcome:
    """The result for one set file. ``report`` is the read-only report that decided; ``wrote``
    is the record written (only when finalised), and ``status_after`` its new status."""
    file_name: str
    result: ApplyResult
    reason: str
    report: rc.ReconcileOutcome
    record_id: str | None = None
    status_before: RecordStatus | None = None
    status_after: RecordStatus | None = None
    wrote: Path | None = None


def _no_write(report: rc.ReconcileOutcome) -> ApplyOutcome:
    return ApplyOutcome(report.file_name, ApplyResult.NO_WRITE,
                        f"{report.reason}; nothing was written (rule {report.rule} never writes)", report,
                        report.record_id, report.record_status)


def _final(rule: str, record: MigrationRecord, now: str | None) -> MigrationRecord:
    if rule == "P5":
        return advance(record, RecordStatus.FAILED, error=PREPARING_ERROR, now=now)
    if rule == "P7":
        return confirm_replace(record, DiskState.SOURCE, now=now)
    return confirm_replace(record, DiskState.UPGRADED, now=now)


def _still(ctx, path: Path, decided: rc.FileEvidence, what: str) -> str | None:
    """Why ``path`` is no longer the file that decided the rule; None if it is the same file
    (device and inode) with the same length and SHA-256."""
    ident = ctx.ident.get(path)
    now, _ = rc._inspect(decided.path, path)
    if now.kind == "missing":
        return f"{what} was removed after the decision"
    if not now.safe or ident is None or ctx.ident.get(path) != ident:
        return f"{what} was replaced or changed after the decision"
    if (now.sha256, now.size) != (decided.sha256, decided.size):
        return f"{what} changed after the decision"
    return None


def _finalise(ctx, sv, report: rc.ReconcileOutcome, now: str | None) -> ApplyOutcome:
    record = next(r for r in sv.records[report.file_name] if r.record_id == report.record_id)
    rpath = sv.base / M / R / f"{record.record_id}.json"
    new = _final(report.rule, record, now)
    if new.status is not WRITES[report.rule] or new.status is not report.planned_status:
        raise RuntimeError(f"rule {report.rule} would write {new.status.value}")  # never reached

    def refused(why: str) -> ApplyOutcome:
        return ApplyOutcome(report.file_name, ApplyResult.REFUSED,
                            f"{why}; nothing was written; the record stays {record.status.value}", report,
                            record.record_id, record.status)

    def unverified(why: str) -> ApplyOutcome:
        return ApplyOutcome(report.file_name, ApplyResult.UNVERIFIED,
                            f"{why}; it is not known whether the record was finalised; nothing else was touched; "
                            "run the report again to see what the disk holds", report, record.record_id, record.status)

    # the evidence that decided the rule must still be in place, read again under the lock
    problem = _still(ctx, sv.base / report.file_name, report.source, "the file")
    if problem is None and report.rule == "P8":
        problem = _still(ctx, sv.base / M / report.file_name, report.backup, f"the backup {report.backup.path}")
    if problem:
        return refused(problem)
    ident = ctx.ident.get(rpath)
    try:
        old = up._read_file(rpath)
    except OSError as exc:
        return refused(f"the migration record {rpath.name} cannot be read again ({up._text(exc)})")
    try:
        same = ident is not None and ctx.ident.get(rpath) == ident and parse_record(old.decode("utf-8")) == record
    except (UnicodeDecodeError, RecordError):
        same = False
    if not same:
        return refused(f"the migration record {rpath.name} changed after it was read")

    ctx.owned[rpath] = ident
    data = dump_record(new).encode("utf-8")
    try:
        up._update_record(rpath, old, new)
    except (OSError, up._RecordChanged) as exc:
        error = up._text(exc)
        try:
            current = up._read_file(rpath)
        except OSError as again:
            return unverified(f"updating the migration record {rpath.name} failed ({error}), and it cannot be "
                              f"read again ({up._text(again)})")
        if current == old and ctx.ident.get(rpath) == ident:
            return refused(f"the migration record {rpath.name} could not be updated ({error}); it was read again "
                           "and is unchanged")
        what = "the new state" if current == data else "other bytes"
        return unverified(f"updating the migration record {rpath.name} failed ({error}); it now reads as {what}")
    return ApplyOutcome(report.file_name, ApplyResult.FINALISED,
                        f"{report.reason}; the record {rpath.name} was moved from {record.status.value} to "
                        f"{new.status.value} and read back as written; no other file was touched", report,
                        record.record_id, record.status, new.status, rpath)


def apply_file(sets_dir: Path | str, file_name: str, *, now: str | None = None) -> ApplyOutcome:
    """Finalise the interrupted upgrade record of ``<sets_dir>/<file_name>`` when the disk proves
    its state (rules P5, P7 and P8 only); otherwise write nothing and return the report."""
    block = rc._platform_block()
    if block:
        return _no_write(rc._blocked_outcome(str(file_name), block))
    if not isinstance(file_name, str) or not up._NAME.match(file_name):
        return _no_write(rc.ReconcileOutcome(str(file_name), rc.Outcome.UNVERIFIABLE, "P1",
                                             f"{file_name!r} is not a plain set file name in the sets folder; "
                                             "nothing was read"))
    with up._context(fresh=True) as ctx:
        sv = rc._survey(ctx, sets_dir)
        if isinstance(sv, rc._Block):
            return _no_write(rc._blocked_outcome(file_name, sv))
        report = rc._decide(sv, file_name)
        if report.outcome is not rc.Outcome.RECOVERABLE or report.rule not in WRITES:
            return _no_write(report)
        return _finalise(ctx, sv, report, now)
