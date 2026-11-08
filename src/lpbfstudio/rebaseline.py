"""Explicit rebaseline of one tracked set after a proven Upgrade or Restore (DC-001 v0.6 §7.12, HX-1).

Upgrade and Restore never write history, so afterwards a tracked set no longer matches any revision of its object: it
resolves "changed outside history", and that state is never adopted. This operation gives the object one more revision
holding the file's exact bytes, but only on evidence read in this run: the frozen report, the B7 record it names, the
bytes of the file and the object's head must all agree. Anything missing or different refuses, and nothing is written.

It is explicit, for one file, and never runs by itself. It writes only under ``.history/`` and never touches the canonical
file or any B7 record. There is no mode that rests on a user's assertion. It runs under the B2a writer locks and is
refused where the frozen report is refused (Windows). No CLI or GUI entry point exists (Q4-L2).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from . import history as h
from . import reconcile as rc
from . import upgrade as up
from .library import Library, LibraryLocked
from .migration import MigrationRecord, RecordStatus, parse_record
from .outcomes import REBASELINE_MODES, RebaselineOutcome, RebaselineStatus, Readback
from .restore_record import RECORD_NAME, RESTORES_DIR, RestoreRecord, RestoreStatus, parse_restore_record

_SETS = h.ObjectType.EXPOSURE_SET


class _Refused(Exception):
    def __init__(self, reason: str, detail: str, errno: int | None = None) -> None:
        super().__init__(detail)
        self.reason, self.detail, self.errno = reason, detail, errno


@dataclass(frozen=True)
class _Evidence:
    want_bytes: tuple[str, int]  # what the file must hold
    want_head: tuple[str, int]   # what the object's head must hold
    source: dict                 # the record's source, without prior_head_seq


def _read_upgrade_record(sets_dir: Path, record_id: str) -> MigrationRecord:
    return parse_record((Path(sets_dir) / up.MIGRATION_DIR / up.RECORDS_DIR / f"{record_id}.json").read_text(encoding="utf-8"))


def _read_restore_record(sets_dir: Path, restore_id: str) -> RestoreRecord:
    path = Path(sets_dir) / up.MIGRATION_DIR / RESTORES_DIR / restore_id / RECORD_NAME
    return parse_restore_record(path.read_text(encoding="utf-8"))


def _record(read, sets_dir: Path, identifier: str):
    try:
        return read(sets_dir, identifier)
    except OSError as exc:
        raise _Refused("rebaseline_record", f"the record {identifier} cannot be read: {exc}", exc.errno) from exc
    except ValueError as exc:
        raise _Refused("rebaseline_record", f"the record {identifier} is not valid: {exc}") from exc


def _evidence(sets_dir: Path, file_name: str, mode: str) -> _Evidence:
    """B2: the frozen report and the record it names. The report takes its own lock, so this runs before the writer locks."""
    report = rc.reconcile_file(sets_dir, file_name)
    said = f"the report gives {report.outcome.value}, rule {report.rule}: {report.reason}"
    if mode == "upgrade":
        if report.outcome is not rc.Outcome.VERIFIED_UPGRADED or report.rule not in ("P12", "P24"):
            raise _Refused("rebaseline_report", said)
        if report.record_id is None:
            raise _Refused("rebaseline_record", "the report names no upgrade record")
        rec = _record(_read_upgrade_record, sets_dir, report.record_id)
        if rec.status is not RecordStatus.UPGRADED or rec.source_path != file_name or rec.upgraded_sha256 is None:
            raise _Refused("rebaseline_record", f"the upgrade record {report.record_id} does not describe this upgrade")
        evidence = _Evidence((rec.upgraded_sha256, rec.upgraded_size_bytes), (rec.source_sha256, rec.source_size_bytes),
                             {"kind": "upgrade", "record_id": rec.record_id, "restore_id": None})
    else:
        if report.outcome is not rc.Outcome.VERIFIED_RESTORED or report.rule != "P20R":
            raise _Refused("rebaseline_report", said)
        if report.record_id is None or report.restore_id is None:
            raise _Refused("rebaseline_record", "the report names no restore record")
        rec = _record(_read_restore_record, sets_dir, report.restore_id)
        if rec.status is not RestoreStatus.RESTORED or rec.source_path != file_name or rec.upgrade_record_id != report.record_id:
            raise _Refused("rebaseline_record", f"the restore record {report.restore_id} does not describe this restore")
        evidence = _Evidence((rec.restored_sha256, rec.restored_size_bytes), (rec.current_sha256, rec.current_size_bytes),
                             {"kind": "restore", "record_id": rec.upgrade_record_id, "restore_id": rec.restore_id})
    observed = report.source
    if observed is None or (observed.sha256, observed.size) != evidence.want_bytes:
        raise _Refused("rebaseline_bytes", "the file the report observed is not the file its record describes")
    return evidence


def _under_lock(lib: Library, path: str, mode: str, object_id: str, expected_sha256: str, evidence: _Evidence) -> RebaselineOutcome:
    """B4 to B9, inside the writer locks."""
    res = h.resolve(lib.root, _SETS, path)
    if res.state is not h.PathState.CHANGED_OUTSIDE or res.claimant != object_id:
        raise _Refused("rebaseline_state", f"the file resolves {res.state.value} (object {res.claimant}); only a file "
                                           f"changed outside the history of object {object_id} can be rebaselined", res.errno)
    data = res.data  # B5: read once
    digest = h.sha256(data)
    if digest != expected_sha256:
        raise _Refused("rebaseline_expected_hash", f"the file holds {digest}, not the expected {expected_sha256}")
    if (digest, len(data)) != evidence.want_bytes:
        raise _Refused("rebaseline_bytes", "the file does not hold the bytes its record describes")
    if res.head is None or res.head.kind != "content" or (res.head.hash, res.head.byte_length) != evidence.want_head:
        raise _Refused("rebaseline_head", "the object's head does not hold the bytes the record replaced, so the chain "
                                          "from the head to this file is not proven")
    source = {**evidence.source, "prior_head_seq": res.head_seq}
    written = h.commit_rebaseline(lib.root, _SETS, object_id, path, data, source)  # B6 to B8
    reason = written.reason.value if written.reason is not None else None
    if not written.committed:
        status = RebaselineStatus.INCOMPLETE if written.seq is not None else RebaselineStatus.NOT_RECORDED
        return RebaselineOutcome(mode, path, object_id, written.seq, status, reason, written.errno, "nothing was committed")
    readback, seen, read_errno = h._read_back(lib.root / path, "B9.read")
    if readback is Readback.FAILED:
        status, errno, detail = RebaselineStatus.RECORDED_UNVERIFIED, read_errno, "the file could not be read afterwards"
    elif readback is Readback.READ and seen == digest:
        status, errno, detail = RebaselineStatus.RECORDED, written.errno, ""
    else:
        status, errno, detail = RebaselineStatus.RECORDED_NOT_CURRENT, written.errno, "the file changed afterwards"
    return RebaselineOutcome(mode, path, object_id, written.seq, status, reason, errno, detail)


def rebaseline_set(lib: Library, file_name: str, mode: str, object_id: str, expected_sha256: str) -> RebaselineOutcome:
    """Rebaseline ``sets/<file_name>``, whose object is ``object_id``, after a proven ``"upgrade"`` or ``"restore"``.

    ``expected_sha256`` is the digest of the file the caller read the evidence for. A refusal is an outcome and writes
    nothing. An unknown mode raises ``ValueError``."""
    if mode not in REBASELINE_MODES:
        raise ValueError(f"unknown rebaseline mode {mode!r}")
    path = f"sets/{file_name}"

    def refused(reason: str, detail: str, errno: int | None = None) -> RebaselineOutcome:
        return RebaselineOutcome(mode, path, None, None, RebaselineStatus.REFUSED, reason, errno, detail)

    if rc._platform_block() is not None:  # B0
        return refused("rebaseline_platform", "the frozen report is refused on this platform")
    if (not isinstance(file_name, str) or not up._NAME.match(file_name) or not isinstance(object_id, str)
            or not h._ID.match(object_id) or not isinstance(expected_sha256, str) or not h._SHA.match(expected_sha256)):
        return refused("rebaseline_arguments", "the file name, object id or expected hash is not valid")  # B1
    try:
        evidence = _evidence(lib.sets_dir, file_name, mode)
    except _Refused as exc:
        return refused(exc.reason, exc.detail, exc.errno)
    outcome: RebaselineOutcome | None = None
    try:
        with lib._writer_locks():  # B3
            outcome = _under_lock(lib, path, mode, object_id, expected_sha256, evidence)
    except _Refused as exc:
        return refused(exc.reason, exc.detail, exc.errno)
    except LibraryLocked as exc:
        return refused("rebaseline_locked", str(exc))
    except OSError as exc:
        if outcome is None:
            return refused("rebaseline_locked", f"the locks failed: {exc}", exc.errno)
        return replace(outcome, detail=f"{outcome.detail}; releasing the locks failed: {exc}".lstrip("; "))
    return outcome
