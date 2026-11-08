"""Build Job Manifest v0 (DC-001 v0.6 §9): a draft of what a build will use, and a finalized manifest pinning exact revisions.

Under ``<library>/.history/jobs/<build_job_id>/``. A draft is ``draft.json``, last-writer-wins. Finalization resolves every
input, pins its current matching revision, and writes ``final/manifest.json``. It is single-winner through ``mkdir final``
and takes no lock on any platform. An input that is not tracked refuses it. ``final/`` without a valid manifest is reported
and never completed.

These are new operations with no canonical file, so D-7 does not apply and they refuse normally. Job states never affect
the resolution of objects. Validation re-checks every pinned blob and never substitutes another revision. A manifest says
which bytes a build used; it proves nothing about qualification or executability.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from . import history as h
from .library import Library
from .outcomes import HistoryReason as HR

MANIFEST_VERSION = 1
DRAFT_VERSION = 1
DRAFT = "draft.json"
FINAL = "final"
MANIFEST = "manifest.json"
AHEAD_WARNING = "a higher committed revision exists for this object that does not match the current file; it was not pinned"
_ROLES = ("part", "support")
_DRAFT_KEYS = {"draft_version", "build_job_id", "exposure_sets", "machine_name", "material"}
_MANIFEST_KEYS = {"manifest_version", "build_job_id", "finalized_at", "software", "platform", "material", "inputs"}


class JobStatus(str, Enum):
    DRAFT_SAVED = "draft_saved"
    DRAFT_NOT_SAVED = "draft_not_saved"
    FINALIZED = "finalized"
    NOT_FINALIZED = "not_finalized"
    FINALIZATION_INCOMPLETE = "finalization_incomplete"
    FINALIZED_UNVERIFIED = "finalized_unverified"


@dataclass(frozen=True)
class JobOutcome:
    build_job_id: str | None
    status: JobStatus
    reason: str | None = None
    errno: int | None = None
    refused_inputs: tuple[dict, ...] = ()
    warnings: tuple[str, ...] = ()


class ManifestResult(str, Enum):
    VALID = "valid"
    NOT_FINALIZED = "not_finalized"
    MISSING_REVISION = "missing_revision"
    HASH_OR_LENGTH_MISMATCH = "hash_or_length_mismatch"
    DAMAGED_OBJECT = "damaged_object"
    UNSUPPORTED_VERSION = "unsupported_version"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class InputCheck:
    path: str
    object_id: str
    seq: str
    result: ManifestResult


@dataclass(frozen=True)
class ManifestReport:
    build_job_id: str
    result: ManifestResult
    inputs: tuple[InputCheck, ...] = ()
    detail: str = ""


def _folder(lib: Library, job_id: str) -> Path:
    return lib.root / h.HISTORY_DIR / h.JOBS_DIR / job_id


def _state(folder: Path) -> str:
    """draft, finalized, reservation (J-1), finalization_incomplete (J-2), damaged or absent."""
    try:
        if not h._is_real_dir(h._lstat(folder)):
            return "damaged"
    except FileNotFoundError:
        return "absent"
    temp = f".{DRAFT}."
    names = h._names(folder)
    for name in names:
        kind = h._lstat(folder / name)
        if name == FINAL:
            if not h._is_real_dir(kind):
                return "damaged"
        elif not (name == DRAFT or (name.startswith(temp) and name.endswith(".tmp"))) or not h._is_regular(kind):
            return "damaged"
    if FINAL in names:
        final = folder / FINAL
        inner = h._names(final)
        temp = f".{MANIFEST}."
        for name in inner:
            if not (name == MANIFEST or (name.startswith(temp) and name.endswith(".tmp"))) or not h._is_regular(h._lstat(final / name)):
                return "damaged"
        return "finalized" if MANIFEST in inner and _manifest_is_valid(final / MANIFEST) else "finalization_incomplete"
    return "draft" if DRAFT in names else "reservation"


def _manifest_is_valid(path: Path) -> bool:
    try:
        _parse_manifest(path.read_bytes())
    except (OSError, ValueError):
        return False
    return True


def _parse_manifest(raw: bytes) -> dict:
    doc = h._load_object(raw)
    if set(doc) != _MANIFEST_KEYS or not isinstance(doc["inputs"], list):
        raise ValueError("not a manifest")
    return doc


def job_state(lib: Library, job_id: str) -> str:
    """The persistent state of a job (§9.3)."""
    try:
        return _state(_folder(lib, job_id))
    except OSError:
        return "damaged"


def _valid_sets(sets: Any) -> bool:
    if not isinstance(sets, list) or not sets:
        return False
    for item in sets:
        if not isinstance(item, dict) or set(item) != {"name", "role", "z_range"} or not isinstance(item["name"], str) or item["role"] not in _ROLES:
            return False
        z = item["z_range"]
        if z is not None and (not isinstance(z, (list, tuple)) or len(z) != 2 or not all(isinstance(v, (int, float)) for v in z) or not z[0] < z[1]):
            return False
    return True


def _commit(folder: Path, target: str, doc: dict, label: str) -> None:
    h._commit_json(folder, target, doc, label, HR.RECORD_TEMP_WRITE_FAILED, HR.RECORD_COMMIT_FAILED)


def _reserve_job(lib: Library) -> str:
    """A new job folder, by exclusive ``mkdir``. ``FileExistsError`` when every attempt collided."""
    jobs = lib.root / h.HISTORY_DIR / h.JOBS_DIR
    for _ in range(h._ATTEMPTS):
        job_id = h.secrets.token_hex(16)
        try:
            h._at("J.mkdir")
            h._ensure_real_dir(lib.root / h.HISTORY_DIR)
            h._ensure_real_dir(jobs)
            h._mkdir(jobs / job_id, 0o755)
        except FileExistsError:
            continue
        return job_id
    raise FileExistsError


def save_draft(lib: Library, job_id: str | None, exposure_sets: list[dict], machine_name: str, material: str) -> JobOutcome:
    """Write the draft of job ``job_id``, or of a new job when it is None. Last writer wins until ``final/`` exists."""
    def refused(reason: str, errno: int | None = None) -> JobOutcome:
        return JobOutcome(job_id, JobStatus.DRAFT_NOT_SAVED, reason, errno)

    if not _valid_sets(exposure_sets) or not isinstance(machine_name, str) or not isinstance(material, str):
        return refused("draft_invalid")
    if job_id is None:
        try:
            job_id = _reserve_job(lib)
        except FileExistsError:
            return refused("job_reservation_collisions_exhausted")
        except OSError as exc:
            return refused("job_reservation_failed", exc.errno)
    else:
        state = job_state(lib, job_id) if h._ID.match(job_id) else "absent"
        if state == "absent":
            return refused("job_not_found")
        if state == "damaged":
            return refused("job_damaged")
        if state in ("finalized", "finalization_incomplete"):
            return refused("job_finalized")
    doc = {"draft_version": DRAFT_VERSION, "build_job_id": job_id, "exposure_sets": [dict(s, z_range=list(s["z_range"]) if s["z_range"] else None)
                                                                                     for s in exposure_sets],
           "machine_name": machine_name, "material": material}
    folder = _folder(lib, job_id)
    try:
        _commit(folder, DRAFT, doc, "J1")
    except h.StepFailed as failure:
        reason = "draft_temp_write_failed" if failure.reason is HR.RECORD_TEMP_WRITE_FAILED else "draft_commit_failed"
        return JobOutcome(job_id, JobStatus.DRAFT_NOT_SAVED, reason, failure.errno)
    warnings: tuple[str, ...] = ()
    try:
        h._fsync_dir(folder, "J1.dirfsync")
    except OSError:
        warnings = ("the draft was written; its durability was not established",)
    return JobOutcome(job_id, JobStatus.DRAFT_SAVED, warnings=warnings)


def _read_blob(root: Path, object_type: h.ObjectType, object_id: str, seq: str) -> bytes:
    return h._read_entry(root / h.HISTORY_DIR / h.OBJECTS_DIR / object_type.value / object_id / h.REVISIONS / seq / h.CONTENT)


def _pin(lib: Library, object_type: h.ObjectType, path: str | None, name: str, extra: dict, pins: list, refused: list, warnings: list) -> None:
    """§9.2 step 2 for one input."""
    if path is None:
        refused.append({"object": name, "state": h.PathState.NONE.value})
        return
    res = h.resolve(lib.root, object_type, path)
    if res.state is not h.PathState.TRACKED:
        refused.append({"object": path, "state": res.state.value})
        return
    try:
        blob = _read_blob(lib.root, object_type, res.object_id, res.seq)
    except OSError:
        blob = None
    if blob != res.data:
        refused.append({"object": path, "state": h.PathState.UNVERIFIABLE.value})
        return
    if res.history_ahead:
        warnings.append(AHEAD_WARNING)
    pins.append({"object_type": object_type.value, "path": path, "object_id": res.object_id, "seq": res.seq,
                 "content_hash": {"algorithm": "sha256", "value": h.sha256(blob)}, "byte_length": len(blob), **extra})


def finalize(lib: Library, job_id: str) -> JobOutcome:
    """§9.2. Reads history at one point in time; pinned revisions are immutable, so a later save changes nothing pinned."""
    def stop(reason: str, errno: int | None = None, refused_inputs: tuple = ()) -> JobOutcome:
        return JobOutcome(job_id, JobStatus.NOT_FINALIZED, reason, errno, refused_inputs)

    state = job_state(lib, job_id) if isinstance(job_id, str) and h._ID.match(job_id) else "absent"
    if state == "absent":
        return stop("job_not_found")
    if state == "damaged":
        return stop("job_damaged")
    if state in ("finalized", "finalization_incomplete"):
        return stop("final_exists")
    folder = _folder(lib, job_id)
    try:  # step 1
        draft = h._load_object((folder / DRAFT).read_bytes())
        if set(draft) != _DRAFT_KEYS or draft["draft_version"] != DRAFT_VERSION or not _valid_sets(draft["exposure_sets"]):
            raise ValueError("not a draft")
    except (OSError, ValueError):
        return stop("draft_unreadable")

    pins: list[dict] = []
    refused: list[dict] = []
    warnings: list[str] = []
    for item in draft["exposure_sets"]:  # step 2
        f = lib._locate(item["name"])
        _pin(lib, h.ObjectType.EXPOSURE_SET, lib._rel(f) if f is not None else None, item["name"],
             {"role": item["role"], "z_range": item["z_range"]}, pins, refused, warnings)
    lib.load_machines()
    machine = lib._machine_source.get(draft["machine_name"])
    _pin(lib, h.ObjectType.MACHINE_PROFILE, lib._rel(machine) if machine is not None else None, draft["machine_name"],
         {"resolved_from_machine_name": draft["machine_name"]}, pins, refused, warnings)
    _pin(lib, h.ObjectType.PROCESS_SETTINGS, "process.json", "process.json", {}, pins, refused, warnings)
    if refused:
        return stop("input_not_pinnable", refused_inputs=tuple(refused))

    final = folder / FINAL
    try:  # step 3: the single winner
        h._at("J3.mkdir")
        h._mkdir(final, 0o755)
    except FileExistsError:
        return stop("final_exists")
    except OSError as exc:
        return stop("final_reservation_failed", exc.errno)

    doc = {"manifest_version": MANIFEST_VERSION, "build_job_id": job_id, "finalized_at": h._now(), "software": h._software(),
           "platform": h._platform(), "material": {"value": draft["material"], "pinned": False}, "inputs": pins}
    try:  # step 4
        _commit(final, MANIFEST, doc, "J4")
    except h.StepFailed as failure:
        reason = "final_temp_write_failed" if failure.reason is HR.RECORD_TEMP_WRITE_FAILED else "final_commit_failed"
        return JobOutcome(job_id, JobStatus.FINALIZATION_INCOMPLETE, reason, failure.errno)
    done = tuple(warnings)
    try:
        h._fsync_dir(final, "J4.dirfsync")
    except OSError as exc:  # the manifest is present, but not known to be durable
        return JobOutcome(job_id, JobStatus.FINALIZED_UNVERIFIED, "final_durability_failed", exc.errno, warnings=done)
    try:  # step 5
        h._at("J5.read")
        back = _parse_manifest((final / MANIFEST).read_bytes())
    except OSError as exc:
        return JobOutcome(job_id, JobStatus.FINALIZED_UNVERIFIED, "final_readback_failed", exc.errno, warnings=done)
    except ValueError:
        return JobOutcome(job_id, JobStatus.FINALIZED_UNVERIFIED, "final_readback_failed", warnings=done)
    if back != json.loads(json.dumps(doc)):
        return JobOutcome(job_id, JobStatus.FINALIZED_UNVERIFIED, "final_readback_failed", warnings=done)
    return JobOutcome(job_id, JobStatus.FINALIZED, warnings=done)


_ORDER = (ManifestResult.UNVERIFIABLE, ManifestResult.DAMAGED_OBJECT, ManifestResult.MISSING_REVISION, ManifestResult.HASH_OR_LENGTH_MISMATCH)


def _check_input(lib: Library, item: dict, views: dict) -> InputCheck:
    object_type = h.ObjectType(item["object_type"])
    object_id, seq = item["object_id"], item["seq"]
    path = item["path"]
    if not h._ID.match(object_id) or not h._SEQ.match(seq):
        return InputCheck(path, str(object_id), str(seq), ManifestResult.UNVERIFIABLE)

    def done(result: ManifestResult) -> InputCheck:
        return InputCheck(path, object_id, seq, result)

    try:
        blob = _read_blob(lib.root, object_type, object_id, seq)
    except FileNotFoundError:
        return done(ManifestResult.MISSING_REVISION)
    except OSError:
        return done(ManifestResult.UNVERIFIABLE)
    want = item["content_hash"]["value"]
    if len(blob) != item["byte_length"] or h.sha256(blob) != want:
        return done(ManifestResult.HASH_OR_LENGTH_MISMATCH)
    if object_type not in views:
        try:
            views[object_type] = {v.object_id: v for v in h.scan_objects(lib.root, object_type)}
        except h.Unverifiable:
            return done(ManifestResult.UNVERIFIABLE)
    view = views[object_type].get(object_id)
    if view is None:
        return done(ManifestResult.MISSING_REVISION)
    if view.state is h.ObjectState.D5:
        return done(ManifestResult.DAMAGED_OBJECT)
    if not any(r.seq == seq and r.kind == "content" for r in view.committed):
        return done(ManifestResult.MISSING_REVISION)
    return done(ManifestResult.VALID)


def validate(lib: Library, job_id: str) -> ManifestReport:
    """§9.2: re-check every pinned blob of a finalized manifest. Reads only; never substitutes another revision."""
    state = job_state(lib, job_id) if isinstance(job_id, str) and h._ID.match(job_id) else "absent"
    final = _folder(lib, job_id) / FINAL / MANIFEST
    try:
        raw = final.read_bytes()
    except FileNotFoundError:
        return ManifestReport(job_id, ManifestResult.NOT_FINALIZED, detail=state)
    except OSError as exc:
        return ManifestReport(job_id, ManifestResult.UNVERIFIABLE, detail=str(exc))
    try:
        doc = _parse_manifest(raw)
    except ValueError as exc:
        return ManifestReport(job_id, ManifestResult.UNVERIFIABLE, detail=str(exc))
    if doc["manifest_version"] != MANIFEST_VERSION or isinstance(doc["manifest_version"], bool):
        return ManifestReport(job_id, ManifestResult.UNSUPPORTED_VERSION)
    views: dict = {}
    try:
        checks = tuple(_check_input(lib, item, views) for item in doc["inputs"])
    except (KeyError, TypeError, ValueError) as exc:
        return ManifestReport(job_id, ManifestResult.UNVERIFIABLE, detail=str(exc))
    found = {c.result for c in checks}
    overall = next((r for r in _ORDER if r in found), ManifestResult.VALID)
    return ManifestReport(job_id, overall, checks)

