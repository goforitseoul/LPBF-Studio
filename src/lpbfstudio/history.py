"""Object identity and immutable revisions beside the library's canonical files (DC-001 v0.6).

Layout, under ``<library>/.history/``::

    objects/<object_type>/<object_id>/identity.json
                                      revisions/<seq>/content, record.json
    staging/                          temporary candidates; never evidence

The resolver reads only and takes no lock. It fails closed: any structure that no protocol step or interruption can
produce is damage, and damage makes every path of that type ``unknown``. Nothing here deletes, repairs or rewrites
anything under ``.history/``, and there is no mutable index: every answer comes from the immutable revisions. Inside one
batch or one operation (``read_cache``) an object already verified in full is not read again while its folders are
unchanged; that memory ends with the batch or operation and nothing of it is ever written.

Entry types come from an examination that does not follow links. On Windows a junction reports as a plain directory, so
the reparse-point attribute decides there, not ``S_ISLNK`` or ``isjunction`` (ABP-0.1, EG-5).

The write steps follow DC-001 §7. An ``OSError`` inside phase H is reported as a history reason and never reaches the
canonical operation (D-7). Anything else is not caught, as for the rest of the library (§7.1). Staging uses the unchanged
canonical write routine with the B2a re-check cleared for that one call, because the re-check belongs to phase C.
"""

from __future__ import annotations

import contextlib
import contextvars
import errno as errno_codes
import hashlib
import json
import os
import platform
import re
import secrets
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator

from . import __version__
from .outcomes import (
    AdoptionStatus, CanonicalReason, CanonicalStatus, HistoryReason, HistoryStatus, Readback, WriteOutcome,
)

HISTORY_DIR = ".history"
OBJECTS_DIR = "objects"
JOBS_DIR = "jobs"
STAGING_DIR = "staging"
IDENTITY = "identity.json"
REVISIONS = "revisions"
CONTENT = "content"
RECORD = "record.json"
RECORD_VERSION = 1
_MAX_SEQ = 99_999_999
_ATTEMPTS = 100
_REPARSE_ATTRIBUTE = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT

_ID = re.compile(r"\A[0-9a-f]{32}\Z")
_SEQ = re.compile(r"\A[0-9]{8}\Z")
_SHA = re.compile(r"\A[0-9a-f]{64}\Z")
_IDENTITY_TMP = re.compile(r"\A\.identity\.json\.[0-9A-Za-z_-]+\.tmp\Z")
_RECORD_TMP = re.compile(r"\A\.record\.json\.[0-9A-Za-z_-]+\.tmp\Z")
_FILE_PART = r"[^/\\]+\.json"
_SET_PATH = re.compile(rf"\Asets/{_FILE_PART}\Z")
_MACHINE_PATH = re.compile(rf"\Amachines/{_FILE_PART}\Z")

# These keep history independent of anything patching or wrapping the ``os`` functions of the canonical path.
_lstat = os.lstat
_mkdir = os.mkdir
_replace = os.replace
_fsync = os.fsync
_os_open = os.open
_unlink = os.unlink

_fault: Callable[[str], None] | None = None  # test hook: called with the name of each step, just before it


def _at(step: str) -> None:
    if _fault is not None:
        _fault(step)


class ObjectType(str, Enum):
    EXPOSURE_SET = "exposure_set"
    MACHINE_PROFILE = "machine_profile"
    PROCESS_SETTINGS = "process_settings"


class ObjectState(str, Enum):
    D1 = "uncommitted_reservation"
    D2 = "empty_object"
    D3 = "live_object"
    D4 = "tombstoned_object"
    D5 = "damaged"


class PathState(str, Enum):
    UNVERIFIABLE = "unverifiable"
    UNKNOWN = "unknown"
    MISSING = "missing"
    TRACKED = "tracked"
    CHANGED_OUTSIDE = "changed_outside_history"
    TOMBSTONED_PRESENT = "tombstoned_file_present"
    UNTRACKED = "untracked"
    NONE = "none"


_ANOMALY_REASON = {
    PathState.UNVERIFIABLE: HistoryReason.STATE_UNVERIFIABLE,
    PathState.UNKNOWN: HistoryReason.STATE_UNKNOWN,
    PathState.MISSING: HistoryReason.STATE_MISSING,
    PathState.CHANGED_OUTSIDE: HistoryReason.STATE_CHANGED_OUTSIDE_HISTORY,
    PathState.TOMBSTONED_PRESENT: HistoryReason.STATE_TOMBSTONED_FILE_PRESENT,
}


class _Invalid(ValueError):
    """A record or identity document the contract does not allow."""


class Unverifiable(Exception):
    """The history could not be examined: §6.2 row 0."""

    def __init__(self, detail: str, errno: int | None = None) -> None:
        super().__init__(detail)
        self.errno = errno


class _Damaged(Exception):
    """A structure no protocol step can produce: D5."""


class StepFailed(Exception):
    """A history step failed; the reason code and errno of §7.3."""

    def __init__(self, reason: HistoryReason, errno: int | None = None) -> None:
        super().__init__(reason.value)
        self.reason = reason
        self.errno = errno


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def seq_text(n: int) -> str:
    return f"{n:08d}"


def _hash_doc(data: bytes) -> dict[str, str]:
    return {"algorithm": "sha256", "value": sha256(data)}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_reparse(st: Any) -> bool:
    return bool(getattr(st, "st_file_attributes", 0) & _REPARSE_ATTRIBUTE)


def _is_real_dir(st: Any) -> bool:
    return stat.S_ISDIR(st.st_mode) and not _is_reparse(st)


def _is_regular(st: Any) -> bool:
    return stat.S_ISREG(st.st_mode) and not _is_reparse(st)


def _read_file(path: Path | str) -> bytes:
    """All the bytes of ``path``, following links; used for canonical files only."""
    with open(path, "rb") as fh:
        return fh.read()


def _read_entry(path: Path) -> bytes:
    """All the bytes of a file under ``.history``, already examined as regular; a link there is never followed."""
    _at("scan.read")
    fd = _os_open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    with os.fdopen(fd, "rb") as fh:
        return fh.read()


# --------------------------------------------------------------------------- records


@dataclass(frozen=True)
class Record:
    object_type: str
    object_id: str
    seq: str
    kind: str
    operation: str
    path: str
    previous_path: str | None
    hash: str | None
    byte_length: int | None
    lock_change: str | None
    source: dict | None


_RECORD_KEYS = {"record_version", "object_type", "object_id", "seq", "kind", "operation", "path", "previous_path",
                "content_hash", "byte_length", "lock_change", "source", "created_at", "software", "platform"}
_CONTENT_OPERATIONS = {"adopted", "created", "copied", "doe", "imported", "saved", "renamed", "rebaselined"}


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise _Invalid(f"duplicate key {key!r}")
        out[key] = value
    return out


def _load_object(raw: bytes) -> dict:
    try:
        doc = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (ValueError, RecursionError) as exc:
        raise _Invalid(f"not valid JSON: {exc}") from None
    if not isinstance(doc, dict):
        raise _Invalid("not a JSON object")
    return doc


def _text_value(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise _Invalid(f"{what} must be non-empty text")
    return value


def _exact_keys(doc: Any, keys: set[str], what: str) -> None:
    if not isinstance(doc, dict) or set(doc) != keys:
        raise _Invalid(f"{what} must have exactly the keys {sorted(keys)}")


def _valid_hash(value: Any, what: str) -> str:
    _exact_keys(value, {"algorithm", "value"}, what)
    if value["algorithm"] != "sha256" or not isinstance(value["value"], str) or not _SHA.match(value["value"]):
        raise _Invalid(f"{what} is not a SHA-256 value")
    return value["value"]


def _valid_seq(value: Any, what: str) -> int:
    if not isinstance(value, str) or not _SEQ.match(value) or value == "00000000":
        raise _Invalid(f"{what} is not a revision number")
    return int(value)


def _valid_id(value: Any, what: str) -> None:
    if not isinstance(value, str) or not _ID.match(value):
        raise _Invalid(f"{what} is not an id")


def _valid_path(value: Any, object_type: str, what: str) -> None:
    if not isinstance(value, str) or "\\" in value or value.startswith("/") or ".." in value.split("/"):
        raise _Invalid(f"{what} is not a relative POSIX path")
    if object_type == ObjectType.EXPOSURE_SET.value:
        good = bool(_SET_PATH.match(value))
    elif object_type == ObjectType.MACHINE_PROFILE.value:
        good = bool(_MACHINE_PATH.match(value)) or value == "machine.json"
    else:
        good = value == "process.json"
    if not good:
        raise _Invalid(f"{what} {value!r} is not a path of a {object_type}")


def _valid_source(doc: dict, seq: int, object_id: str) -> None:
    """§5.1 field rules: the source form each operation allows."""
    op, src = doc["operation"], doc["source"]
    if op in ("adopted", "created"):
        if src is not None:
            raise _Invalid(f"{op} has no source")
    elif op in ("saved", "renamed", "deleted"):
        _exact_keys(src, {"object_id", "seq"}, "source")
        if src["object_id"] != object_id or _valid_seq(src["seq"], "source seq") >= seq:
            raise _Invalid("source must be an earlier revision of this object")
    elif op in ("copied", "doe"):
        if isinstance(src, dict) and "object_id" in src:
            _exact_keys(src, {"object_id", "seq"}, "source")
            _valid_id(src["object_id"], "source object_id")
            _valid_seq(src["seq"], "source seq")
        else:
            _exact_keys(src, {"path", "content_hash", "state"}, "source")
            _text_value(src["path"], "source path")
            _valid_hash(src["content_hash"], "source hash")
            if src["state"] != "untracked":
                raise _Invalid("source state must be untracked")
    elif op == "imported":
        _exact_keys(src, {"external_path", "content_hash"}, "source")
        _text_value(src["external_path"], "source path")
        _valid_hash(src["content_hash"], "source hash")
    elif op == "rebaselined":
        _exact_keys(src, {"kind", "record_id", "restore_id", "prior_head_seq"}, "source")
        if src["kind"] not in ("upgrade", "restore"):
            raise _Invalid("rebaseline source kind must be upgrade or restore")
        _valid_id(src["record_id"], "source record_id")
        if src["kind"] == "restore":
            _valid_id(src["restore_id"], "source restore_id")
        elif src["restore_id"] is not None:
            raise _Invalid("an upgrade rebaseline has no restore_id")
        if _valid_seq(src["prior_head_seq"], "prior_head_seq") >= seq:
            raise _Invalid("prior_head_seq must be lower than seq")


def parse_record(raw: bytes, object_type: str, object_id: str, seq: str) -> Record:
    """A valid record for this folder, or ``_Invalid``. Unknown or missing keys are never accepted."""
    doc = _load_object(raw)
    if set(doc) != _RECORD_KEYS:
        raise _Invalid("the record's keys are not exactly those of record version 1")
    if doc["record_version"] != RECORD_VERSION or isinstance(doc["record_version"], bool):
        raise _Invalid("unsupported record_version")
    if doc["object_type"] != object_type or doc["object_id"] != object_id or doc["seq"] != seq:
        raise _Invalid("the record does not match its folder")
    kind, op = doc["kind"], doc["operation"]
    if not isinstance(op, str) or (kind == "deleted") != (op == "deleted") or (kind != "deleted" and op not in _CONTENT_OPERATIONS):
        raise _Invalid("kind and operation do not agree")
    if kind not in ("content", "deleted"):
        raise _Invalid("unknown kind")
    _valid_path(doc["path"], object_type, "path")
    if op == "renamed":
        _valid_path(doc["previous_path"], object_type, "previous_path")
    elif doc["previous_path"] is not None:
        raise _Invalid("previous_path belongs to a rename")
    digest: str | None = None
    if kind == "content":
        digest = _valid_hash(doc["content_hash"], "content_hash")
        length = doc["byte_length"]
        if not isinstance(length, int) or isinstance(length, bool) or length < 0:
            raise _Invalid("byte_length must be a count")
    elif doc["content_hash"] is not None or doc["byte_length"] is not None:
        raise _Invalid("a tombstone has no content hash")
    if doc["lock_change"] not in ("locked", None):
        raise _Invalid("unknown lock_change")
    _text_value(doc["created_at"], "created_at")
    _exact_keys(doc["software"], {"version", "git_commit"}, "software")
    for key in ("version", "git_commit"):
        _text_value(doc["software"][key], f"software {key}")
    _exact_keys(doc["platform"], {"os", "architecture", "python_version"}, "platform")
    for key in ("os", "architecture", "python_version"):
        _text_value(doc["platform"][key], f"platform {key}")
    _valid_source(doc, int(seq), object_id)
    return Record(object_type, object_id, seq, kind, op, doc["path"], doc["previous_path"], digest, doc["byte_length"],
                  doc["lock_change"], doc["source"])


def _software() -> dict[str, str]:
    return {"version": __version__, "git_commit": "unknown"}


def _platform() -> dict[str, str]:
    return {"os": platform.system() or "unknown", "architecture": platform.machine() or "unknown",
            "python_version": platform.python_version()}


def build_record(object_type: str, object_id: str, seq: str, operation: str, path: str, *, previous_path: str | None = None,
                 data: bytes | None = None, lock_change: str | None = None, source: dict | None = None) -> dict:
    deleted = operation == "deleted"
    return {"record_version": RECORD_VERSION, "object_type": object_type, "object_id": object_id, "seq": seq,
            "kind": "deleted" if deleted else "content", "operation": operation, "path": path, "previous_path": previous_path,
            "content_hash": None if deleted else _hash_doc(data), "byte_length": None if deleted else len(data),
            "lock_change": lock_change, "source": source, "created_at": _now(), "software": _software(), "platform": _platform()}


# --------------------------------------------------------------------------- discovery


@dataclass(frozen=True)
class ObjectView:
    object_id: str
    state: ObjectState
    head: Record | None = None
    committed: tuple[Record, ...] = ()  # highest first
    incomplete: tuple[str, ...] = ()
    detail: str = ""
    a_max: int = 0


def _names(path: Path) -> list[str]:
    return sorted(os.listdir(path))


def _classify_revision(rev: Path, object_type: str, object_id: str, seq: str) -> Record | None:
    """The committed record of a revision folder; None if incomplete; ``_Damaged`` if neither (§5.2)."""
    names = _names(rev)
    for name in names:
        if not _is_regular(_lstat(rev / name)):
            raise _Damaged(f"{rev.name}/{name} is not a regular file")
    temps = [n for n in names if _RECORD_TMP.match(n)]
    other = set(names) - {CONTENT, RECORD} - set(temps)
    if other:
        raise _Damaged(f"{rev.name} has the unexpected entries {sorted(other)}")
    if RECORD not in names:
        if len(temps) > 1:
            raise _Damaged(f"{rev.name} has more than one record temporary file")
        return None
    if temps:
        raise _Damaged(f"{rev.name} has a record and a record temporary file")
    try:
        record = parse_record(_read_entry(rev / RECORD), object_type, object_id, seq)
    except _Invalid as exc:
        raise _Damaged(f"{rev.name}/{RECORD}: {exc}") from None
    if record.kind == "deleted":
        if CONTENT in names:
            raise _Damaged(f"{rev.name} is a tombstone with a blob")
        return record
    if CONTENT not in names:
        raise _Damaged(f"{rev.name} has a content record and no blob")
    blob = _read_entry(rev / CONTENT)
    if len(blob) != record.byte_length or sha256(blob) != record.hash:
        raise _Damaged(f"{rev.name}: the blob does not match its record")
    return record


def _valid_identity(raw: bytes, object_type: str, object_id: str) -> None:
    doc = _load_object(raw)
    _exact_keys(doc, {"object_type", "object_id", "created_at"}, IDENTITY)
    if doc["object_type"] != object_type or doc["object_id"] != object_id:
        raise _Invalid("the identity does not match its folder")
    _text_value(doc["created_at"], "created_at")


_READ_CACHE: contextvars.ContextVar[dict | None] = contextvars.ContextVar("lpbf_history_read_cache", default=None)
_READ_SETTLE: contextvars.ContextVar[int] = contextvars.ContextVar("lpbf_history_read_settle", default=0)

# For the memory of one operation (a copy, a finalization): an object is remembered only if its folders had been left
# unchanged for this long when they were examined. A change made after that then always shows in their modification
# times, whatever the resolution of the file system's clock (FAT keeps two seconds).
OPERATION_SETTLE_NS = 2_000_000_000


@contextlib.contextmanager
def read_cache(settle_ns: int = 0) -> Iterator[None]:
    """Inside a batch of writes, an object verified in full with no incomplete folder is not read again while its object
    and ``revisions`` folders are unchanged: a new revision or entry changes them. Memory only, for this block only.
    With ``settle_ns`` an object is remembered only if both folders were last changed at least that long before."""
    token, settle = _READ_CACHE.set({}), _READ_SETTLE.set(settle_ns)
    try:
        yield
    finally:
        _READ_SETTLE.reset(settle)
        _READ_CACHE.reset(token)


def _signature(folder: Path) -> tuple | None:
    try:
        a, b = _lstat(folder), _lstat(folder / REVISIONS)
    except OSError:
        return None
    return a.st_ino, a.st_mtime_ns, b.st_ino, b.st_mtime_ns


def _classify_object(folder: Path, object_type: str, name: str) -> ObjectView:
    """§6.1.2. Raises ``OSError`` when something cannot be examined; that is row 0, not damage."""
    cache = _READ_CACHE.get()
    before = time.time_ns() if cache is not None else 0
    signature = _signature(folder) if cache is not None else None  # taken first, so a change during the scan is not cached
    if signature is not None:
        known = cache.get(folder)
        if known is not None and known[0] == signature:
            return known[1]
    try:
        view = _classify_inner(folder, object_type, name)
    except _Damaged as exc:
        return ObjectView(name, ObjectState.D5, detail=str(exc))
    if signature is not None and view.state in (ObjectState.D3, ObjectState.D4) and not view.incomplete:
        settle = _READ_SETTLE.get()
        if settle == 0 or max(signature[1], signature[3]) <= before - settle:
            cache[folder] = (signature, view)
    return view


def _classify_inner(folder: Path, object_type: str, name: str) -> ObjectView:
    if not _ID.match(name):
        raise _Damaged(f"{name!r} is not an object id")
    if not _is_real_dir(_lstat(folder)):
        raise _Damaged(f"{name} is not a real folder")
    names = _names(folder)
    temps = [n for n in names if _IDENTITY_TMP.match(n)]
    other = set(names) - {IDENTITY, REVISIONS} - set(temps)
    if other:
        raise _Damaged(f"{name} has the unexpected entries {sorted(other)}")
    for t in temps:
        if not _is_regular(_lstat(folder / t)):
            raise _Damaged(f"{t} is not a regular file")
    if IDENTITY not in names:
        if REVISIONS in names or len(temps) > 1:
            raise _Damaged(f"{name} has no identity but holds more than one reservation entry")
        return ObjectView(name, ObjectState.D1)
    if temps:
        raise _Damaged(f"{name} has an identity and an identity temporary file")
    if not _is_regular(_lstat(folder / IDENTITY)):
        raise _Damaged(f"{IDENTITY} is not a regular file")
    try:
        _valid_identity(_read_entry(folder / IDENTITY), object_type, name)
    except _Invalid as exc:
        raise _Damaged(f"{IDENTITY}: {exc}") from None
    if REVISIONS not in names:
        return ObjectView(name, ObjectState.D2)
    revisions = folder / REVISIONS
    if not _is_real_dir(_lstat(revisions)):
        raise _Damaged(f"{REVISIONS} is not a real folder")
    seqs = _names(revisions)
    for s in seqs:
        if not _SEQ.match(s) or s == "00000000":
            raise _Damaged(f"{REVISIONS}/{s} is not a revision number")
        if not _is_real_dir(_lstat(revisions / s)):
            raise _Damaged(f"{REVISIONS}/{s} is not a real folder")
    if [int(s) for s in seqs] != list(range(1, len(seqs) + 1)):
        raise _Damaged(f"{name} has a gap in its revision numbers")
    found = [(s, _classify_revision(revisions / s, object_type, name, s)) for s in seqs]
    committed = [r for _, r in found if r is not None]
    incomplete = tuple(s for s, r in found if r is None)
    if not committed:
        return ObjectView(name, ObjectState.D2, incomplete=incomplete, a_max=len(seqs))
    head = committed[-1]
    state = ObjectState.D4 if head.kind == "deleted" else ObjectState.D3
    return ObjectView(name, state, head=head, committed=tuple(reversed(committed)), incomplete=incomplete, a_max=len(seqs))


def _fixed_folder(path: Path) -> bool:
    """Whether a fixed folder exists; ``Unverifiable`` unless it is absent or a real folder (§6.1.1)."""
    try:
        st = _lstat(path)
    except FileNotFoundError:
        return False
    if not _is_real_dir(st):
        raise Unverifiable(f"{path.name} is not a real folder")
    return True


def scan_objects(root: Path | str, object_type: ObjectType) -> list[ObjectView]:
    """Every object of a type, examined in full. ``Unverifiable`` for §6.2 row 0."""
    root = Path(root)
    try:
        history = root / HISTORY_DIR
        if not _fixed_folder(history):
            return []
        for name in _names(history):
            if name not in (OBJECTS_DIR, JOBS_DIR, STAGING_DIR):
                raise Unverifiable(f"{HISTORY_DIR}/{name} is not expected")
            _fixed_folder(history / name)
        objects = history / OBJECTS_DIR
        if not _fixed_folder(objects):
            return []
        known = {t.value for t in ObjectType}
        for name in _names(objects):
            if name not in known:
                raise Unverifiable(f"{OBJECTS_DIR}/{name} is not expected")
            _fixed_folder(objects / name)
        folder = objects / object_type.value
        if not _fixed_folder(folder):
            return []
        return [_classify_object(folder / name, object_type.value, name) for name in _names(folder)]
    except OSError as exc:
        raise Unverifiable(str(exc), exc.errno) from exc


@dataclass(frozen=True)
class Resolution:
    state: PathState
    object_id: str | None = None           # attached object (row 4 only)
    claimant: str | None = None            # the one object claiming the path, attached or not (rows 3, 4, 5)
    seq: str | None = None                 # the current matching revision
    head_seq: str | None = None
    head: Record | None = None             # the object head, when one object claims the path
    history_ahead: bool = False
    data: bytes | None = None              # the exact bytes of the path, when it was read
    present: bool | None = None            # None when the path could not be examined
    damaged: tuple[str, ...] = ()
    observations: tuple[str, ...] = ()
    errno: int | None = None


def _staging_observations(root: Path) -> tuple[str, ...]:
    """Staging files are reported and never interpreted (§6.4, §7.10)."""
    try:
        return tuple(f"staging file {name}" for name in _names(root / HISTORY_DIR / STAGING_DIR))
    except OSError:
        return ()


def resolve(root: Path | str, object_type: ObjectType, path: str) -> Resolution:
    """§6.2. Reads only; takes no lock; not an atomic snapshot."""
    root = Path(root)
    try:
        data = _read_file(root / path)
        present = True
    except FileNotFoundError:
        data, present = None, False
    except OSError as exc:
        return Resolution(PathState.UNVERIFIABLE, errno=exc.errno)
    try:
        views = scan_objects(root, object_type)
    except Unverifiable as exc:
        return Resolution(PathState.UNVERIFIABLE, data=data, present=present, errno=exc.errno)
    observations = tuple(f"{v.object_id}: {v.state.value}" for v in views if v.state in (ObjectState.D1, ObjectState.D2))
    observations += tuple(f"{v.object_id}: incomplete revision {s}" for v in views for s in v.incomplete)
    observations += _staging_observations(root)
    base = {"data": data, "present": present, "observations": observations}
    damaged = tuple(f"{v.object_id}: {v.detail}" for v in views if v.state is ObjectState.D5)
    if damaged:
        return Resolution(PathState.UNKNOWN, damaged=damaged, **base)
    claims = [v for v in views if v.state is ObjectState.D3 and v.head.path == path]
    if len(claims) >= 2:
        return Resolution(PathState.UNKNOWN, damaged=tuple(f"{v.object_id}: claims {path}" for v in claims), **base)
    if len(claims) == 1:
        x = claims[0]
        if not present:
            return Resolution(PathState.MISSING, claimant=x.object_id, head_seq=x.head.seq, head=x.head, **base)
        digest = sha256(data)
        for record in x.committed:  # highest first
            if record.kind == "content" and record.byte_length == len(data) and record.hash == digest:
                return Resolution(PathState.TRACKED, object_id=x.object_id, claimant=x.object_id, seq=record.seq,
                                  head_seq=x.head.seq, head=x.head, history_ahead=record.seq != x.head.seq, **base)
        return Resolution(PathState.CHANGED_OUTSIDE, claimant=x.object_id, head_seq=x.head.seq, head=x.head, **base)
    if present and any(v.state is ObjectState.D4 and v.head.path == path for v in views):
        return Resolution(PathState.TOMBSTONED_PRESENT, **base)
    return Resolution(PathState.UNTRACKED if present else PathState.NONE, **base)


# --------------------------------------------------------------------------- write primitives


def _fsync_dir(path: Path, step: str) -> None:
    """Folder ``fsync``; not available on Windows (§7.10)."""
    _at(step)
    if not hasattr(os, "O_DIRECTORY"):
        return
    fd = _os_open(path, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
    try:
        _fsync(fd)
    finally:
        os.close(fd)


def _sync_parent(path: Path) -> None:
    """Best effort for a folder just created; the contract's guarantees rest on the record's own folder ``fsync``."""
    if hasattr(os, "O_DIRECTORY"):
        try:
            fd = _os_open(path, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
        except OSError:
            return
        try:
            _fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)


def _exclusive_fd(path: Path) -> int:
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_CLOEXEC", 0))
    return _os_open(path, flags, 0o644)


def _ensure_real_dir(path: Path) -> None:
    """Make one fixed folder, without following links, or fail with ``OSError``."""
    try:
        _mkdir(path, 0o755)
    except FileExistsError:
        if not _is_real_dir(_lstat(path)):
            raise NotADirectoryError(errno_codes.ENOTDIR, "not a real folder", str(path)) from None


def ensure_object_root(root: Path, object_type: ObjectType) -> Path:
    """The object folder of a type, created if needed. ``history_root_unavailable`` on any failure."""
    try:
        _at("root.mkdir")
        history = root / HISTORY_DIR
        _ensure_real_dir(history)
        objects = history / OBJECTS_DIR
        _ensure_real_dir(objects)
        folder = objects / object_type.value
        _ensure_real_dir(folder)
        return folder
    except OSError as exc:
        raise StepFailed(HistoryReason.HISTORY_ROOT_UNAVAILABLE, exc.errno) from exc


def _reserve_object(type_folder: Path) -> tuple[str, Path]:
    """A2: a new object folder (D1)."""
    for _ in range(_ATTEMPTS):
        object_id = secrets.token_hex(16)
        folder = type_folder / object_id
        try:
            _at("A2.mkdir")
            _mkdir(folder, 0o755)
        except FileExistsError:
            continue
        except OSError as exc:
            raise StepFailed(HistoryReason.OBJECT_RESERVATION_FAILED, exc.errno) from exc
        _sync_parent(type_folder)
        return object_id, folder
    raise StepFailed(HistoryReason.OBJECT_RESERVATION_COLLISIONS_EXHAUSTED)


def _commit_json(folder: Path, target: str, doc: dict, label: str, temp_reason: HistoryReason,
                 commit_reason: HistoryReason) -> None:
    """Temporary file, ``fsync``, replace. Failures up to and including the replace raise ``StepFailed``."""
    tmp = folder / f".{target}.{secrets.token_hex(6)}.tmp"
    payload = json.dumps(doc, indent=1, sort_keys=True).encode("utf-8")
    try:
        _at(f"{label}.temp")
        fd = _exclusive_fd(tmp)
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            _at(f"{label}.fsync")
            _fsync(fh.fileno())
    except OSError as exc:
        raise StepFailed(temp_reason, exc.errno) from exc
    try:
        _at(f"{label}.replace")
        _replace(tmp, folder / target)
    except OSError as exc:
        raise StepFailed(commit_reason, exc.errno) from exc


def _commit_identity(folder: Path, object_type: ObjectType, object_id: str) -> None:
    """A3: write-once identity (D2)."""
    doc = {"object_type": object_type.value, "object_id": object_id, "created_at": _now()}
    _commit_json(folder, IDENTITY, doc, "A3", HistoryReason.IDENTITY_TEMP_WRITE_FAILED, HistoryReason.IDENTITY_COMMIT_FAILED)
    try:
        _fsync_dir(folder, "A3.dirfsync")
    except OSError as exc:
        raise StepFailed(HistoryReason.IDENTITY_COMMIT_FAILED, exc.errno) from exc


def _next_number(highest: int) -> int:
    if highest >= _MAX_SEQ:
        raise StepFailed(HistoryReason.SEQUENCE_EXHAUSTED)
    return highest + 1


def _allocate(folder: Path, label: str) -> tuple[str, Path]:
    """§4.2: ``A_max + 1`` by exclusive ``mkdir``; a number is never reused."""
    revisions = folder / REVISIONS
    try:
        if not os.path.lexists(revisions):
            _at(f"{label}.mkdir_revisions")
            _mkdir(revisions, 0o755)
            _sync_parent(folder)
        numbers = [int(n) for n in os.listdir(revisions) if _SEQ.match(n)]
    except OSError as exc:
        raise StepFailed(HistoryReason.REVISION_RESERVATION_FAILED, exc.errno) from exc
    highest = max(numbers, default=0)
    for _ in range(_ATTEMPTS):
        highest = _next_number(highest)
        seq = seq_text(highest)
        try:
            _at(f"{label}.mkdir")
            _mkdir(revisions / seq, 0o755)
        except FileExistsError:
            continue
        except OSError as exc:
            raise StepFailed(HistoryReason.REVISION_RESERVATION_FAILED, exc.errno) from exc
        _sync_parent(revisions)
        return seq, revisions / seq
    raise StepFailed(HistoryReason.REVISION_RESERVATION_COLLISIONS_EXHAUSTED)


def _write_blob(rev: Path, data: bytes, label: str) -> None:
    """Exclusive create, binary, ``fsync``; then read back and verify."""
    path = rev / CONTENT
    try:
        _at(f"{label}.write")
        fd = _exclusive_fd(path)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            _at(f"{label}.fsync")
            _fsync(fh.fileno())
    except OSError as exc:
        raise StepFailed(HistoryReason.BLOB_WRITE_FAILED, exc.errno) from exc
    try:
        _at(f"{label}.verify")
        back = _read_file(path)
    except OSError as exc:
        raise StepFailed(HistoryReason.BLOB_VERIFY_FAILED, exc.errno) from exc
    if len(back) != len(data) or sha256(back) != sha256(data):
        raise StepFailed(HistoryReason.BLOB_VERIFY_FAILED)


def _commit_record(rev: Path, doc: dict, label: str) -> int | None:
    """Commit a record. The errno of a failed folder ``fsync`` is returned: the record is committed, not known durable."""
    _commit_json(rev, RECORD, doc, label, HistoryReason.RECORD_TEMP_WRITE_FAILED, HistoryReason.RECORD_COMMIT_FAILED)
    try:
        _fsync_dir(rev, f"{label}.dirfsync")
    except OSError as exc:
        return exc.errno if exc.errno is not None else 0
    return None


def _canonical_routine():
    from . import library
    return library._atomic_write


def _staging_write(path: Path, text: str) -> None:
    """The unchanged canonical routine, with the B2a re-check cleared for this one call: it belongs to phase C (§7.1)."""
    from . import library
    token = library._FINAL_CHECK.set(None)
    try:
        library._atomic_write(path, text)
    finally:
        library._FINAL_CHECK.reset(token)


@dataclass(frozen=True)
class Staged:
    path: Path
    data: bytes


def stage_candidate(root: Path, text: str) -> Staged:
    """S3. The candidate's bytes are what the routine actually wrote, read back; never predicted from the text."""
    staging = root / HISTORY_DIR / STAGING_DIR
    path = staging / f"{secrets.token_hex(16)}.json"
    try:
        _at("S3.write")
        _ensure_real_dir(root / HISTORY_DIR)
        _ensure_real_dir(staging)
        _staging_write(path, text)
    except OSError as exc:
        raise StepFailed(HistoryReason.STAGING_WRITE_FAILED, exc.errno) from exc
    try:
        _at("S3.read")
        return Staged(path, _read_file(path))
    except OSError as exc:
        raise StepFailed(HistoryReason.STAGING_READBACK_FAILED, exc.errno) from exc


# --------------------------------------------------------------------------- phase H


@dataclass
class _Prior:
    known: bool
    present: bool = False
    data: bytes | None = None

    def shows(self, readback: Readback, digest: str | None) -> bool:
        if not self.known:
            return False
        if not self.present:
            return readback is Readback.ABSENT
        return readback is Readback.READ and digest == sha256(self.data)


@dataclass
class HResult:
    """What phase H did, carried to phase V."""
    root: Path
    object_type: ObjectType
    path: str
    previous_path: str | None = None
    prior: _Prior = field(default_factory=lambda: _Prior(False))
    prior_previous: _Prior | None = None
    object_id: str | None = None
    seq: str | None = None
    committed: bool = False
    artefacts: bool = False                # artefacts for the target exist
    target_hash: str | None = None         # None for a tombstone
    target_length: int | None = None
    reason: HistoryReason | None = None    # the first failure in phase H
    errno: int | None = None
    adoption: AdoptionStatus = AdoptionStatus.NOT_NEEDED
    adopted_seq: str | None = None
    staged: Staged | None = None
    observations: list[str] = field(default_factory=list)

    @property
    def previous_or_path(self) -> str:
        """The path the existing file is at: the source of a rename, otherwise the target."""
        return self.previous_path if self.previous_path is not None else self.path

    def fail(self, reason: HistoryReason, errno: int | None = None) -> None:
        if self.reason is None:
            self.reason, self.errno = reason, errno


def _prior_of(res: Resolution) -> _Prior:
    return _Prior(False) if res.present is None else _Prior(True, res.present, res.data)


def _observe(root: Path, path: str) -> _Prior:
    try:
        return _Prior(True, True, _read_file(root / path))
    except FileNotFoundError:
        return _Prior(True, False)
    except OSError:
        return _Prior(False)


def _new_object(hr: HResult) -> Path:
    """A2 and A3 for a new object, leaving D2."""
    type_folder = ensure_object_root(hr.root, hr.object_type)
    hr.object_id, folder = _reserve_object(type_folder)
    return folder


def _adopt(hr: HResult) -> str | None:
    """§7.4, A1 to A7. The seq of a committed adoption whose re-check matched; otherwise None, with ``hr`` filled in."""
    try:
        try:
            _at("A1.read")
            source = _read_file(hr.root / hr.previous_or_path)
        except OSError as exc:
            raise StepFailed(HistoryReason.SOURCE_READ_FAILED, exc.errno) from exc
        folder = _new_object(hr)
        hr.adoption = AdoptionStatus.INCOMPLETE  # from here on an artefact exists
        _commit_identity(folder, hr.object_type, hr.object_id)
        seq, rev = _allocate(folder, "A4")
        _write_blob(rev, source, "A5")
        doc = build_record(hr.object_type.value, hr.object_id, seq, "adopted", hr.previous_or_path, data=source)
        durability = _commit_record(rev, doc, "A6")
    except StepFailed as failure:
        if hr.adoption is not AdoptionStatus.INCOMPLETE:
            hr.adoption = AdoptionStatus.NOT_RECORDED
        hr.fail(failure.reason, failure.errno)
        return None
    hr.adopted_seq = seq
    if durability is not None:
        hr.fail(HistoryReason.RECORD_DURABILITY_FAILED, durability)
    try:
        _at("A7.read")
        again = _read_file(hr.root / hr.previous_or_path)
    except FileNotFoundError:
        again = None
    except OSError as exc:
        hr.adoption = AdoptionStatus.COMMITTED_UNVERIFIED
        hr.reason, hr.errno = HistoryReason.ADOPTION_RECHECK_FAILED, exc.errno
        return None
    if again != source:
        hr.adoption = AdoptionStatus.COMMITTED_SOURCE_CHANGED
        hr.reason, hr.errno = HistoryReason.ADOPTION_SOURCE_CHANGED, None
        return None
    hr.adoption = AdoptionStatus.COMMITTED
    return seq


def _object_folder(hr: HResult) -> Path:
    return hr.root / HISTORY_DIR / OBJECTS_DIR / hr.object_type.value / hr.object_id


def _content_revision(hr: HResult, text: str, operation: str, source: dict | None, lock_change: str | None) -> None:
    """S3 to S6."""
    folder = _object_folder(hr)
    hr.staged = stage_candidate(hr.root, text)
    data = hr.staged.data
    seq, rev = _allocate(folder, "S4")
    hr.seq, hr.artefacts = seq, True
    _write_blob(rev, data, "S5")
    doc = build_record(hr.object_type.value, hr.object_id, seq, operation, hr.path, previous_path=hr.previous_path, data=data,
                       lock_change=lock_change, source=source)
    durability = _commit_record(rev, doc, "S6")
    hr.committed, hr.target_hash, hr.target_length = True, sha256(data), len(data)
    if durability is not None:
        hr.fail(HistoryReason.RECORD_DURABILITY_FAILED, durability)


def _attach(hr: HResult, res: Resolution) -> dict | None:
    """Tracked or untracked: the source naming the object's current revision. None when phase H stops here."""
    if res.state is PathState.TRACKED:
        hr.object_id = res.object_id
        return {"object_id": res.object_id, "seq": res.seq}
    adopted = _adopt(hr)
    return None if adopted is None else {"object_id": hr.object_id, "seq": adopted}


def phase_h_write(root: Path | str, object_type: ObjectType, path: str, text: str, *, new_operation: str = "created",
                  new_source: dict | Callable[[], dict] | None = None, overwrite_undefined: bool = False,
                  lock_change: str | None = None) -> HResult:
    """Save, copy, DOE, import and create: §7.5 and §7.9. ``text`` has been serialized once by the caller. ``new_source``
    is the record's source for a new object, or a callable giving it, which may raise ``StepFailed``."""
    hr = HResult(Path(root), object_type, path)
    res = resolve(hr.root, object_type, path)
    hr.prior = _prior_of(res)
    if overwrite_undefined and res.present:
        hr.fail(HistoryReason.IMPORT_OVERWRITE_UNDEFINED)
        return hr
    if res.state in _ANOMALY_REASON:
        hr.fail(_ANOMALY_REASON[res.state], res.errno)
        return hr
    try:
        if res.state is PathState.NONE:
            operation, source = new_operation, new_source() if callable(new_source) else new_source
            type_folder = ensure_object_root(hr.root, object_type)
            hr.object_id, folder = _reserve_object(type_folder)
            hr.artefacts = True
            _commit_identity(folder, object_type, hr.object_id)
        else:
            source = _attach(hr, res)
            if source is None:
                return hr
            operation = "saved"
        _content_revision(hr, text, operation, source, lock_change)
    except StepFailed as failure:
        hr.fail(failure.reason, failure.errno)
    return hr


def phase_h_rename(root: Path | str, object_type: ObjectType, source_path: str, target_path: str, text: str) -> HResult:
    """§7.6."""
    hr = HResult(Path(root), object_type, target_path, previous_path=source_path)
    res = resolve(hr.root, object_type, source_path)
    hr.prior_previous = _prior_of(res)
    hr.prior = hr.prior_previous if target_path == source_path else _observe(hr.root, target_path)
    if res.state in _ANOMALY_REASON or res.state is PathState.NONE:
        hr.fail(_ANOMALY_REASON.get(res.state, HistoryReason.STATE_MISSING), res.errno)
        return hr
    try:
        source = _attach(hr, res)
        if source is not None:
            _content_revision(hr, text, "renamed", source, None)
    except StepFailed as failure:
        hr.fail(failure.reason, failure.errno)
    return hr


def phase_h_delete(root: Path | str, object_type: ObjectType, path: str) -> HResult:
    """§7.8: adopt first if untracked, so the deleted content is kept; then a tombstone."""
    hr = HResult(Path(root), object_type, path)
    res = resolve(hr.root, object_type, path)
    hr.prior = _prior_of(res)
    if res.state in _ANOMALY_REASON or res.state is PathState.NONE:
        hr.fail(_ANOMALY_REASON.get(res.state, HistoryReason.STATE_MISSING), res.errno)
        return hr
    try:
        source = _attach(hr, res)
        if source is None:
            return hr
        seq, rev = _allocate(_object_folder(hr), "D4")
        hr.seq, hr.artefacts = seq, True
        doc = build_record(object_type.value, hr.object_id, seq, "deleted", path, source=source)
        durability = _commit_record(rev, doc, "D6")
        hr.committed = True
        if durability is not None:
            hr.fail(HistoryReason.RECORD_DURABILITY_FAILED, durability)
    except StepFailed as failure:
        hr.fail(failure.reason, failure.errno)
    return hr


def copy_source(root: Path | str, object_type: ObjectType, path: str) -> dict:
    """§7.9: the record's source for a copy. The source is never adopted. ``StepFailed`` when it cannot be read."""
    res = resolve(root, object_type, path)
    if res.state is PathState.TRACKED:
        return {"object_id": res.object_id, "seq": res.seq}
    if res.data is None:
        raise StepFailed(HistoryReason.SOURCE_READ_FAILED, res.errno)
    return {"path": path, "content_hash": _hash_doc(res.data), "state": "untracked"}


def external_source(path: Path) -> dict:
    """§7.9: the record's source for an import. ``StepFailed`` when the file cannot be read."""
    try:
        return {"external_path": str(path), "content_hash": _hash_doc(_read_file(path))}
    except OSError as exc:
        raise StepFailed(HistoryReason.SOURCE_READ_FAILED, exc.errno) from exc


@dataclass(frozen=True)
class RebaselineWrite:
    seq: str | None
    committed: bool
    reason: HistoryReason | None
    errno: int | None


def commit_rebaseline(root: Path, object_type: ObjectType, object_id: str, path: str, data: bytes, source: dict) -> RebaselineWrite:
    """§7.12, B6 to B8: one more content revision of ``object_id`` holding exactly ``data``. Writes only under ``.history``."""
    folder = root / HISTORY_DIR / OBJECTS_DIR / object_type.value / object_id
    seq = None
    try:
        seq, rev = _allocate(folder, "B6")
        _write_blob(rev, data, "B7")
        doc = build_record(object_type.value, object_id, seq, "rebaselined", path, data=data, source=source)
        durability = _commit_record(rev, doc, "B8")
    except StepFailed as failure:
        return RebaselineWrite(seq, False, failure.reason, failure.errno)
    if durability is not None:
        return RebaselineWrite(seq, True, HistoryReason.RECORD_DURABILITY_FAILED, durability)
    return RebaselineWrite(seq, True, None, None)


# --------------------------------------------------------------------------- phase V


def _read_back(path: Path, step: str) -> tuple[Readback, str | None, int | None]:
    try:
        _at(step)
        return Readback.READ, sha256(_read_file(path)), None
    except FileNotFoundError:
        return Readback.ABSENT, None, None
    except OSError as exc:
        return Readback.FAILED, None, exc.errno if exc.errno is not None else 0


def refused_outcome(operation: str, path: str, exc: BaseException) -> WriteOutcome:
    """Phase P refused: nothing was started."""
    return WriteOutcome(operation, path, None, CanonicalStatus.NOT_DONE, CanonicalReason.PRECONDITION_REFUSED, str(exc),
                        Readback.NOT_PERFORMED, None, None, None, HistoryStatus.NOT_APPLICABLE, None, None, None, None,
                        AdoptionStatus.NOT_NEEDED, None)


def conclude(hr: HResult, operation: str, exc: BaseException | None) -> WriteOutcome:
    """S8 and S9: read back, decide the statuses of §7.2, remove the staging file. Never raises ``OSError``."""
    readback, digest, read_errno = _read_back(hr.root / hr.path, "V.read")
    previous: Readback | None = None
    previous_digest: str | None = None
    if hr.previous_path is not None:
        previous, previous_digest, _ = _read_back(hr.root / hr.previous_path, "V.read_previous")

    reason, reason_errno = hr.reason, hr.errno
    if hr.committed:
        if readback is Readback.FAILED:
            history = HistoryStatus.RECORDED_UNVERIFIED
        elif hr.target_hash is None:
            history = HistoryStatus.RECORDED if readback is Readback.ABSENT else HistoryStatus.RECORDED_NOT_CURRENT
        else:
            match = readback is Readback.READ and digest == hr.target_hash
            history = HistoryStatus.RECORDED if match else HistoryStatus.RECORDED_NOT_CURRENT
        if reason is None:  # phase H succeeded: the phase C or V reason governs
            if readback is Readback.FAILED:
                reason, reason_errno = HistoryReason.CANONICAL_READBACK_FAILED, read_errno
            elif exc is not None:
                reason = HistoryReason.CANONICAL_STEP_FAILED
            elif history is HistoryStatus.RECORDED_NOT_CURRENT:
                reason = HistoryReason.CANONICAL_MISMATCH
    else:
        history = HistoryStatus.INCOMPLETE if hr.artefacts else HistoryStatus.NOT_RECORDED

    if exc is None:
        canonical, canonical_reason, canonical_error = CanonicalStatus.DONE, None, None
    else:
        shows_prior = hr.prior.shows(readback, digest) and (
            hr.prior_previous is None or hr.prior_previous.shows(previous, previous_digest))
        if history is HistoryStatus.RECORDED:  # the file shows the target, so "the prior state" proves nothing (C-2c)
            shows_prior = False
        canonical = CanonicalStatus.NOT_DONE if shows_prior else CanonicalStatus.UNCERTAIN
        canonical_reason, canonical_error = CanonicalReason.CANONICAL_STEP_FAILED, str(exc)

    if hr.staged is not None:
        try:
            _at("S9.unlink")
            _unlink(hr.staged.path)
        except OSError as failure:
            hr.observations.append(f"the staging file {hr.staged.path.name} could not be removed: {failure}")
    outcome_object = hr.object_id if (hr.artefacts or hr.committed or hr.adoption is not AdoptionStatus.NOT_NEEDED) else None
    if hr.adoption is AdoptionStatus.NOT_RECORDED:
        outcome_object = None
    return WriteOutcome(operation, hr.path, hr.previous_path, canonical, canonical_reason, canonical_error, readback, read_errno,
                        digest, previous, history, reason, reason_errno, outcome_object,
                        hr.seq if history is not HistoryStatus.NOT_RECORDED else None, hr.adoption, hr.adopted_seq)
