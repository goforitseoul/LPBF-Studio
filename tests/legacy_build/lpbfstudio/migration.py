"""Format 1 -> format 2 upgrade: state model and migration record contract (B7 Phase 2, S3-A).

This module only defines and checks states; it writes nothing. Backups, the upgrade write,
recovery and the ``lpbf format`` commands are later stages (S3-B onwards) and must follow it.

Three notions are kept apart:

- ``RecordStatus``: what a migration record claims about one file's upgrade.
- ``FileResult``: what a run reports for one file.
- ``DiskState``: what the file's bytes show, compared with the digests in a record.

A file is reported upgraded only when its record says so *and* its bytes match the recorded
upgraded digest and size (``result_for``). A record is never trusted on its own, and anything
that cannot be read or verified is reported, not guessed.

Records carry their own ``record_schema_version``, unrelated to a set's ``schema_version``.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from .library import _parse_set
from .models import PASS_ID_VERSION, SCHEMA_VERSION

RECORD_SCHEMA_VERSION = 1
SUPPORTED_RECORD_VERSIONS = (1,)
SOURCE_SCHEMA_VERSION = SCHEMA_VERSION  # the only version that is upgraded
TARGET_SCHEMA_VERSION = PASS_ID_VERSION

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RECORD_ID = re.compile(r"^[0-9a-f]{32}$")


class RecordError(ValueError):
    """A migration record that cannot be read, is not valid, or cannot make the requested move."""


class RecordStatus(str, Enum):
    PREPARING = "preparing"    # record written, source digest taken; backup not yet verified
    BACKED_UP = "backed_up"    # backup verified byte-identical; upgraded bytes known; not replaced yet
    UPGRADED = "upgraded"      # source replaced and the bytes on disk matched the upgraded digest
    FAILED = "failed"          # stopped; the source was verified unchanged (or never touched)
    UNCERTAIN = "uncertain"    # the state of the source could not be confirmed


IN_PROGRESS = {RecordStatus.PREPARING, RecordStatus.BACKED_UP}
FINAL = {RecordStatus.UPGRADED, RecordStatus.FAILED, RecordStatus.UNCERTAIN}
TRANSITIONS: dict[RecordStatus, set[RecordStatus]] = {
    RecordStatus.PREPARING: {RecordStatus.BACKED_UP, RecordStatus.FAILED, RecordStatus.UNCERTAIN},
    RecordStatus.BACKED_UP: {RecordStatus.UPGRADED, RecordStatus.FAILED, RecordStatus.UNCERTAIN},
    RecordStatus.UPGRADED: set(),
    RecordStatus.FAILED: set(),
    RecordStatus.UNCERTAIN: set(),
}


class FileResult(str, Enum):
    UPGRADED = "upgraded"
    SKIPPED_LOCKED = "skipped_locked"
    SKIPPED_V2 = "skipped_v2"
    # this build cannot safely process the file: reading, parsing, the schema_version check or
    # structural validation failed (not necessarily physically unreadable). Always has a reason;
    # no write or replace of the source was attempted. Distinct from failed and uncertain.
    SKIPPED_UNREADABLE = "skipped_unreadable"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    NOT_ATTEMPTED = "not_attempted"


# results that leave a batch incomplete, and that therefore need a reason (except not_attempted)
PROBLEMS = {FileResult.FAILED, FileResult.UNCERTAIN, FileResult.NOT_ATTEMPTED, FileResult.SKIPPED_UNREADABLE}


class DiskState(str, Enum):
    SOURCE = "matches_source"      # size and SHA-256 equal the record's source
    UPGRADED = "matches_upgraded"  # size and SHA-256 equal the record's upgraded bytes
    OTHER = "differs"              # readable, but neither
    MISSING = "missing"
    UNREADABLE = "unreadable"


# --------------------------------------------------------------------------- record


def _utc(value: str) -> datetime:
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{value!r} is not an ISO 8601 time") from None
    if when.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{value!r} must be in UTC (ending in Z or +00:00)")
    return when


def _relative(value: str, what: str) -> str:
    p = PurePosixPath(value)
    if not value or "\\" in value or p.is_absolute() or ".." in p.parts or p.suffix != ".json":
        raise ValueError(f"{what} {value!r} must be a relative .json path inside the sets folder")
    return value


def _a(word: str) -> str:
    return f"{'an' if word[0] in 'aeiou' else 'a'} {word}"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class MigrationRecord(BaseModel):
    """One file's upgrade, as written next to its backup. Every key is required (null where the
    status does not allow a value); unknown keys are refused. Paths are relative to the sets folder."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    record_schema_version: int
    record_id: str
    source_path: str
    backup_path: str
    source_schema_version: int
    target_schema_version: int
    source_sha256: str
    source_size_bytes: int
    upgraded_sha256: str | None
    upgraded_size_bytes: int | None
    status: RecordStatus
    created_at: str
    completed_at: str | None
    error: str | None

    @field_validator("status", mode="before")
    @classmethod
    def _status(cls, value: Any) -> Any:
        return RecordStatus(value) if isinstance(value, str) else value  # strict mode: accept the text

    @model_validator(mode="after")
    def _check(self) -> "MigrationRecord":
        if self.record_schema_version not in SUPPORTED_RECORD_VERSIONS:
            raise ValueError(f"record_schema_version {self.record_schema_version} is not supported")
        if not _RECORD_ID.match(self.record_id):
            raise ValueError("record_id must be 32 lowercase hex characters")
        _relative(self.source_path, "source_path")
        _relative(self.backup_path, "backup_path")
        if PurePosixPath(self.backup_path) == PurePosixPath(self.source_path):
            raise ValueError("backup_path must differ from source_path")
        if self.source_schema_version != SOURCE_SCHEMA_VERSION:
            raise ValueError(f"source_schema_version must be {SOURCE_SCHEMA_VERSION}")
        if self.target_schema_version != TARGET_SCHEMA_VERSION:
            raise ValueError(f"target_schema_version must be {TARGET_SCHEMA_VERSION}")
        if not _SHA256.match(self.source_sha256):
            raise ValueError("source_sha256 must be 64 lowercase hex characters")
        if self.source_size_bytes < 0:
            raise ValueError("source_size_bytes must not be negative")
        if (self.upgraded_sha256 is None) != (self.upgraded_size_bytes is None):
            raise ValueError("upgraded_sha256 and upgraded_size_bytes are set together")
        if self.upgraded_sha256 is not None:
            if not _SHA256.match(self.upgraded_sha256):
                raise ValueError("upgraded_sha256 must be 64 lowercase hex characters")
            if self.upgraded_size_bytes < 0:
                raise ValueError("upgraded_size_bytes must not be negative")
            if self.upgraded_sha256 == self.source_sha256:
                raise ValueError("the upgraded bytes cannot equal the source bytes")
        created = _utc(self.created_at)
        if self.completed_at is not None and _utc(self.completed_at) < created:
            raise ValueError("completed_at is before created_at")

        s = self.status
        if s is RecordStatus.PREPARING and self.upgraded_sha256 is not None:
            raise ValueError("a preparing record has no upgraded digest yet")
        if s in (RecordStatus.BACKED_UP, RecordStatus.UPGRADED) and self.upgraded_sha256 is None:
            raise ValueError(f"{_a(s.value)} record needs upgraded_sha256 and upgraded_size_bytes")
        if (s in FINAL) != (self.completed_at is not None):
            raise ValueError(f"completed_at is {'required' if s in FINAL else 'not allowed'} when {s.value}")
        needs_error = s in (RecordStatus.FAILED, RecordStatus.UNCERTAIN)
        if needs_error and not (self.error or "").strip():
            raise ValueError(f"{_a(s.value)} record needs an error")
        if not needs_error and self.error is not None:
            raise ValueError(f"error is not allowed when {s.value}")
        return self


def parse_record(text: str) -> MigrationRecord:
    """Read a record, checking ``record_schema_version`` before anything else. Raises
    RecordError; a missing or unsupported version is refused, never assumed."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise RecordError(f"the record is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise RecordError("the record is not a JSON object")
    if "record_schema_version" not in data:
        raise RecordError("record_schema_version is missing; this is not a migration record of a known version")
    version = data["record_schema_version"]
    if type(version) is not int or version not in SUPPORTED_RECORD_VERSIONS:
        supported = ", ".join(map(str, SUPPORTED_RECORD_VERSIONS))
        raise RecordError(f"record_schema_version {version!r} is not supported (supported: {supported})")
    return _validated(data)


def dump_record(record: MigrationRecord) -> str:
    return record.model_dump_json(indent=2)


def _validated(data: dict) -> MigrationRecord:
    try:
        return MigrationRecord.model_validate(data)
    except ValidationError as exc:
        msg = exc.errors()[0]["msg"]
        raise RecordError(f"invalid migration record: {msg.removeprefix('Value error, ')}") from None


def new_record(source_path: str, backup_path: str, source: bytes, now: str | None = None) -> MigrationRecord:
    """The first record of an upgrade: status preparing, from the source bytes as read."""
    return _validated({
        "record_schema_version": RECORD_SCHEMA_VERSION, "record_id": uuid.uuid4().hex,
        "source_path": source_path, "backup_path": backup_path,
        "source_schema_version": SOURCE_SCHEMA_VERSION, "target_schema_version": TARGET_SCHEMA_VERSION,
        "source_sha256": hashlib.sha256(source).hexdigest(), "source_size_bytes": len(source),
        "upgraded_sha256": None, "upgraded_size_bytes": None, "status": RecordStatus.PREPARING,
        "created_at": now or _now(), "completed_at": None, "error": None,
    })


def advance(record: MigrationRecord, status: RecordStatus, *, upgraded: bytes | None = None,
            error: str | None = None, now: str | None = None) -> MigrationRecord:
    """A new record moved to ``status``; ``record`` is unchanged. Moving to backed_up needs the
    upgraded bytes; failed and uncertain need an error. Success is recorded only by
    ``confirm_replace``, from the bytes on disk."""
    if status not in TRANSITIONS[record.status] or status is RecordStatus.UPGRADED:
        how = " (only confirm_replace records an upgrade)" if status is RecordStatus.UPGRADED else ""
        raise RecordError(f"cannot move {_a(record.status.value)} record to {status.value}{how}")
    data = record.model_dump()
    data["status"] = status
    if status is RecordStatus.BACKED_UP:
        if upgraded is None:
            raise RecordError("backed_up needs the upgraded bytes")
        data.update(upgraded_sha256=hashlib.sha256(upgraded).hexdigest(), upgraded_size_bytes=len(upgraded))
    else:
        data.update(completed_at=now or _now(), error=error)
    return _validated(data)


def _confirmed(record: MigrationRecord, status: RecordStatus, error: str | None, now: str | None) -> MigrationRecord:
    data = record.model_dump()
    data.update(status=status, completed_at=now or _now(), error=error)
    return _validated(data)


def confirm_replace(record: MigrationRecord, state: DiskState, now: str | None = None) -> MigrationRecord:
    """The final record after the replace of a backed_up file, from what the disk shows: upgraded
    only if the bytes match the upgraded digest, failed if they still match the source, otherwise
    uncertain (never retried blindly)."""
    if record.status is not RecordStatus.BACKED_UP:
        raise RecordError(f"cannot move {_a(record.status.value)} record to a replace result; it must be backed_up")
    if state is DiskState.UPGRADED:
        return _confirmed(record, RecordStatus.UPGRADED, None, now)
    if state is DiskState.SOURCE:
        return _confirmed(record, RecordStatus.FAILED, "the file was not replaced; it is unchanged", now)
    return _confirmed(record, RecordStatus.UNCERTAIN, f"after the replace the file is {state.value}", now)


# --------------------------------------------------------------------------- disk


@dataclass(frozen=True)
class Observation:
    """What reading a file showed. Reading never writes."""
    exists: bool
    size: int | None = None
    sha256: str | None = None
    error: str | None = None


def observe(path: Path | str) -> Observation:
    path = Path(path)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return Observation(exists=False)
    except OSError as exc:
        return Observation(exists=True, error=exc.strerror or str(exc))
    return Observation(exists=True, size=len(data), sha256=hashlib.sha256(data).hexdigest())


def disk_state(obs: Observation, record: MigrationRecord) -> DiskState:
    if not obs.exists:
        return DiskState.MISSING
    if obs.error is not None:
        return DiskState.UNREADABLE
    if (obs.sha256, obs.size) == (record.source_sha256, record.source_size_bytes):
        return DiskState.SOURCE
    if record.upgraded_sha256 is not None and (obs.sha256, obs.size) == (record.upgraded_sha256,
                                                                          record.upgraded_size_bytes):
        return DiskState.UPGRADED
    return DiskState.OTHER


def result_for(record: MigrationRecord, state: DiskState) -> FileResult:
    """The result to report for a file with this record, judged by its bytes: upgraded only for an
    upgraded record whose file matches the upgraded digest; failed only when the file still
    matches the source and the record never claimed success; uncertain otherwise. An in-progress
    record (an interrupted run) is never reported as upgraded."""
    if record.status is RecordStatus.UPGRADED:
        return FileResult.UPGRADED if state is DiskState.UPGRADED else FileResult.UNCERTAIN
    if record.status is RecordStatus.UNCERTAIN:
        return FileResult.UNCERTAIN
    return FileResult.FAILED if state is DiskState.SOURCE else FileResult.UNCERTAIN


# --------------------------------------------------------------------------- planning


@dataclass(frozen=True)
class Classification:
    """Whether a set file would be upgraded. ``result`` is None for an eligible file, otherwise
    the result to report without touching it."""
    upgrade: bool
    result: FileResult | None
    reason: str
    source_sha256: str | None = None
    source_size_bytes: int | None = None


def classify(path: Path | str, *, include_locked: bool = False,
             records: Sequence[MigrationRecord | RecordError] = ()) -> Classification:
    """Decide, by reading only, what an upgrade would do with one set file. ``records`` are the
    existing migration records for this file (or the errors met reading them). Refused files are
    skipped, not treated as format 1; format 2 files are skipped with their ids; locked sets are
    skipped unless included; an unreadable or unresolved record blocks the file."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        return Classification(False, FileResult.SKIPPED_UNREADABLE, f"cannot be read: {exc.strerror or exc}")
    return classify_bytes(data, include_locked=include_locked, records=records)


def classify_bytes(data: bytes, *, include_locked: bool = False,
                   records: Sequence[MigrationRecord | RecordError] = ()) -> Classification:
    """``classify`` for bytes already read, so that a caller can check and then use the same
    bytes."""
    digest = {"source_sha256": hashlib.sha256(data).hexdigest(), "source_size_bytes": len(data)}

    def skip(result: FileResult, reason: str) -> Classification:
        return Classification(False, result, reason, **digest)

    try:
        es = _parse_set(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return skip(FileResult.SKIPPED_UNREADABLE, f"cannot be read: {str(exc).splitlines()[0]}")
    if es.schema_version == TARGET_SCHEMA_VERSION:
        return skip(FileResult.SKIPPED_V2, "already format 2; its pass ids are kept")
    if es.schema_version != SOURCE_SCHEMA_VERSION:  # unreachable while (1, 2) are the supported versions
        return skip(FileResult.SKIPPED_UNREADABLE, f"format {es.schema_version} cannot be upgraded")
    if es.locked and not include_locked:
        return skip(FileResult.SKIPPED_LOCKED, "locked")
    for rec in records:
        if isinstance(rec, RecordError):
            return skip(FileResult.FAILED, f"its migration record cannot be read ({rec}); resolve it first")
        if rec.status is not RecordStatus.FAILED:
            return skip(FileResult.FAILED, f"an earlier upgrade record is {rec.status.value}; resolve it first")
        if (rec.source_sha256, rec.source_size_bytes) != (digest["source_sha256"], digest["source_size_bytes"]):
            return skip(FileResult.FAILED, "an earlier failed upgrade record is for different bytes; resolve it first")
    return Classification(True, None, "format 1", **digest)


# --------------------------------------------------------------------------- batch


@dataclass
class BatchReport:
    """Per-file results of a run, kept independently: a later failure never removes or changes
    an earlier result, and a file has one result."""
    results: dict[str, tuple[FileResult, str]] = field(default_factory=dict)

    def add(self, name: str, result: FileResult, reason: str = "") -> None:
        if name in self.results:
            raise ValueError(f"{name} already has a result")
        if result in PROBLEMS and result is not FileResult.NOT_ATTEMPTED and not reason.strip():
            raise ValueError(f"a {result.value} result needs a reason")
        self.results[name] = (result, reason)

    def count(self, result: FileResult) -> int:
        return sum(1 for r, _ in self.results.values() if r is result)

    @property
    def problems(self) -> list[str]:
        return [name for name, (r, _) in self.results.items() if r in PROBLEMS]

    @property
    def complete(self) -> bool:
        """True when every file was upgraded or skipped as locked or already format 2."""
        return not self.problems
