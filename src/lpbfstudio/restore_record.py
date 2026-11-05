"""The restore record contract (B7 Phase 2, S3-B2c-a).

A Restore (S3-B2c-b, **not implemented in this build**) would put a format 2 set back to the
original format 1 bytes kept in its backup. Each attempt keeps its own folder:

    <sets>/.format1-backup/restores/<restore_id>/
        restore.json            this record
        preserved/<file>        a copy of the upgraded bytes, made before the exchange
        swap/<file>             the staged original before the exchange; the displaced file after it

This module only defines and checks the record; it has no file access of any kind. The
reconciliation report (``reconcile``) reads restore evidence with it. Design: the frozen
revision 5 (``docs/audit/b7p2_s3b2c/design/report_rev5.md``); fields from revision 2 §6, with
revision 3 §5.1 splitting the displaced-file observation: the record holds only
``displaced_observed_at_restore`` (set once, in a final record, never changed); the report's
``displaced_observed_at_report`` is never stored anywhere.

Every key is required (null where the status allows no value) and unknown keys are refused.
``restore_record_schema_version`` is checked before anything else; only version 1 is read.
A restore record never proves a restore on its own: the report judges it against the files.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

RESTORE_RECORD_SCHEMA_VERSION = 1
SUPPORTED_RESTORE_RECORD_VERSIONS = (1,)

BACKUP_DIR = ".format1-backup"   # the migration folder M (upgrade.MIGRATION_DIR)
RESTORES_DIR = "restores"        # M/restores/
RECORD_NAME = "restore.json"     # A/restore.json
PRESERVED_DIR = "preserved"      # A/preserved/<file>
SWAP_DIR = "swap"                # A/swap/<file>

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[0-9a-f]{32}$")
_NAME = re.compile(r"^[^./\\\x00][^/\\\x00]*\.json$")  # the same plain set file name as upgrade._NAME

KINDS = ("regular", "missing", "symlink", "directory", "fifo", "socket", "device", "other", "unreadable")


class RestoreRecordError(ValueError):
    """A restore record that cannot be read or is not valid."""


class RestoreStatus(str, Enum):
    PREPARING = "preparing"      # the attempt folder and record exist; the preserved copy is not yet verified
    PRESERVED = "preserved"      # the preserved copy equals the upgraded bytes; nothing exchanged
    EXCHANGING = "exchanging"    # written just before the exchange call; whether it happened is decided by the bytes
    RESTORED = "restored"        # exchanged, and every post-exchange check held
    FAILED = "failed"            # stopped; the set file was verified unchanged (the upgraded bytes)
    UNCERTAIN = "uncertain"      # the outcome could not be confirmed


IN_PROGRESS = {RestoreStatus.PREPARING, RestoreStatus.PRESERVED, RestoreStatus.EXCHANGING}
FINAL = {RestoreStatus.RESTORED, RestoreStatus.FAILED, RestoreStatus.UNCERTAIN}
TRANSITIONS: dict[RestoreStatus, set[RestoreStatus]] = {
    RestoreStatus.PREPARING: {RestoreStatus.PRESERVED, RestoreStatus.FAILED, RestoreStatus.UNCERTAIN},
    RestoreStatus.PRESERVED: {RestoreStatus.EXCHANGING, RestoreStatus.FAILED, RestoreStatus.UNCERTAIN},
    RestoreStatus.EXCHANGING: {RestoreStatus.RESTORED, RestoreStatus.FAILED, RestoreStatus.UNCERTAIN},
    RestoreStatus.RESTORED: set(),
    RestoreStatus.FAILED: set(),
    RestoreStatus.UNCERTAIN: set(),
}


def _utc(value: str) -> datetime:
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{value!r} is not an ISO 8601 time") from None
    if when.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{value!r} must be in UTC (ending in Z or +00:00)")
    return when


def _identity(value: list[int] | None, what: str) -> None:
    if value is not None and (len(value) != 2 or any(v < 0 for v in value)):
        raise ValueError(f"{what} must be [device, inode], two non-negative integers")


class DisplacedObservation(BaseModel):
    """What was found at ``swap/<file>``: its kind, and for a regular file its size, SHA-256 and
    link count; device and inode when known. Used for the record's observation at restore and
    for the report's own observation, which is never stored."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    kind: str
    size: int | None
    sha256: str | None
    dev: int | None
    ino: int | None
    nlink: int | None

    @model_validator(mode="after")
    def _check(self) -> "DisplacedObservation":
        if self.kind not in KINDS:
            raise ValueError(f"kind {self.kind!r} is not one of {', '.join(KINDS)}")
        if self.kind == "missing" and any(v is not None for v in (self.size, self.sha256, self.dev, self.ino,
                                                                   self.nlink)):
            raise ValueError("a missing displaced file has no size, digest or identity")
        if self.kind == "regular" and (self.size is None or self.sha256 is None):
            raise ValueError("a regular displaced file needs its size and sha256")
        if self.kind not in ("regular", "unreadable") and self.sha256 is not None:
            raise ValueError(f"a {self.kind} has no sha256")
        if self.sha256 is not None and not _SHA256.match(self.sha256):
            raise ValueError("sha256 must be 64 lowercase hex characters")
        for name in ("size", "dev", "ino", "nlink"):
            v = getattr(self, name)
            if v is not None and v < 0:
                raise ValueError(f"{name} must not be negative")
        return self


class RestoreRecord(BaseModel):
    """One restore attempt of one set file. Paths: ``source_path`` and ``backup_path`` are
    relative to the sets folder; ``preserved_path`` and ``swap_path`` to the migration folder."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    restore_record_schema_version: int
    restore_id: str
    upgrade_record_id: str
    source_path: str
    backup_path: str
    current_sha256: str          # U: the upgraded bytes the set file held when the restore began
    current_size_bytes: int
    restored_sha256: str         # O: the original bytes the restore puts back
    restored_size_bytes: int
    source_identity: list[int] | None
    staged_identity: list[int] | None
    preserved_path: str
    swap_path: str
    exchange_attempted: bool
    displaced_observed_at_restore: DisplacedObservation | None
    displaced_matches_upgraded: bool | None
    status: RestoreStatus
    created_at: str
    completed_at: str | None
    error: str | None

    @field_validator("status", mode="before")
    @classmethod
    def _status(cls, value: Any) -> Any:
        return RestoreStatus(value) if isinstance(value, str) else value  # strict mode: accept the text

    @field_validator("displaced_observed_at_restore", mode="before")
    @classmethod
    def _observation(cls, value: Any) -> Any:
        return DisplacedObservation.model_validate(value) if isinstance(value, dict) else value

    @model_validator(mode="after")
    def _check(self) -> "RestoreRecord":
        if self.restore_record_schema_version not in SUPPORTED_RESTORE_RECORD_VERSIONS:
            raise ValueError(f"restore_record_schema_version {self.restore_record_schema_version} is not supported")
        for name in ("restore_id", "upgrade_record_id"):
            if not _ID.match(getattr(self, name)):
                raise ValueError(f"{name} must be 32 lowercase hex characters")
        if not _NAME.match(self.source_path):
            raise ValueError(f"source_path {self.source_path!r} must be a plain set file name in the sets folder")
        if self.backup_path != f"{BACKUP_DIR}/{self.source_path}":
            raise ValueError(f"backup_path must be {BACKUP_DIR}/{self.source_path}")
        folder = f"{RESTORES_DIR}/{self.restore_id}"
        if self.preserved_path != f"{folder}/{PRESERVED_DIR}/{self.source_path}":
            raise ValueError(f"preserved_path must be {folder}/{PRESERVED_DIR}/{self.source_path}")
        if self.swap_path != f"{folder}/{SWAP_DIR}/{self.source_path}":
            raise ValueError(f"swap_path must be {folder}/{SWAP_DIR}/{self.source_path}")
        for name in ("current_sha256", "restored_sha256"):
            if not _SHA256.match(getattr(self, name)):
                raise ValueError(f"{name} must be 64 lowercase hex characters")
        for name in ("current_size_bytes", "restored_size_bytes"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must not be negative")
        if self.current_sha256 == self.restored_sha256:
            raise ValueError("the current (upgraded) and restored (original) bytes must differ")
        _identity(self.source_identity, "source_identity")
        _identity(self.staged_identity, "staged_identity")
        created = _utc(self.created_at)
        if self.completed_at is not None and _utc(self.completed_at) < created:
            raise ValueError("completed_at is before created_at")

        obs = self.displaced_observed_at_restore
        if (obs is None) != (self.displaced_matches_upgraded is None):
            raise ValueError("displaced_observed_at_restore and displaced_matches_upgraded are set together")
        if obs is not None:
            matches = obs.kind == "regular" and (obs.sha256, obs.size) == (self.current_sha256,
                                                                          self.current_size_bytes)
            if self.displaced_matches_upgraded is not matches:
                raise ValueError("displaced_matches_upgraded must say whether the observation equals the upgraded "
                                 "bytes")

        s = self.status
        if (s in FINAL) != (self.completed_at is not None):
            raise ValueError(f"completed_at is {'required' if s in FINAL else 'not allowed'} when {s.value}")
        needs_error = s in (RestoreStatus.FAILED, RestoreStatus.UNCERTAIN)
        if needs_error and not (self.error or "").strip():
            raise ValueError(f"a {s.value} record needs an error")
        if not needs_error and self.error is not None:
            raise ValueError(f"error is not allowed when {s.value}")
        if s in IN_PROGRESS and obs is not None:
            raise ValueError(f"a {s.value} record has no displaced observation")
        if s is RestoreStatus.PREPARING and self.staged_identity is not None:
            raise ValueError("a preparing record has staged nothing")
        if s in (RestoreStatus.PREPARING, RestoreStatus.PRESERVED) and self.exchange_attempted:
            raise ValueError(f"a {s.value} record has not attempted the exchange")
        if s in (RestoreStatus.EXCHANGING, RestoreStatus.RESTORED):
            if not self.exchange_attempted:
                raise ValueError(f"{'an' if s is RestoreStatus.EXCHANGING else 'a'} {s.value} record must have "
                                 "attempted the exchange")
            if self.source_identity is None or self.staged_identity is None:
                raise ValueError(f"{s.value} needs source_identity and staged_identity")
        if s is RestoreStatus.RESTORED and (obs is None or not self.displaced_matches_upgraded):
            raise ValueError("a restored record needs a displaced observation equal to the upgraded bytes")
        if s is RestoreStatus.FAILED and obs is not None:
            raise ValueError("a failed record has no displaced observation")
        return self


def parse_restore_record(text: str) -> RestoreRecord:
    """Read a restore record, checking ``restore_record_schema_version`` before anything else.
    Raises RestoreRecordError; a missing or unsupported version is refused, never assumed."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise RestoreRecordError(f"the restore record is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise RestoreRecordError("the restore record is not a JSON object")
    if "restore_record_schema_version" not in data:
        raise RestoreRecordError("restore_record_schema_version is missing; this is not a restore record of a "
                                 "known version")
    version = data["restore_record_schema_version"]
    if type(version) is not int or version not in SUPPORTED_RESTORE_RECORD_VERSIONS:
        supported = ", ".join(map(str, SUPPORTED_RESTORE_RECORD_VERSIONS))
        raise RestoreRecordError(f"restore_record_schema_version {version!r} is not supported (supported: "
                                 f"{supported})")
    try:
        return RestoreRecord.model_validate(data)
    except ValidationError as exc:
        msg = exc.errors()[0]["msg"]
        raise RestoreRecordError(f"invalid restore record: {msg.removeprefix('Value error, ')}") from None


def dump_restore_record(record: RestoreRecord) -> str:
    return record.model_dump_json(indent=2)
