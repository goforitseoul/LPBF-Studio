"""What one library write did, as DC-001 v0.6 §7.2 defines it, and the caller-scoped collector of §7.11.

The model keeps four facts apart: how the existing operation ended, what the read-back of the canonical file saw, what
history artefacts the operation produced, and whether the target revision verifiably matches the canonical state. A
combination the contract does not allow cannot be built.

``collect_outcomes`` is how a caller receives them. Existing methods keep their signatures, return values and exceptions;
an outcome reaches every collector open in the calling thread before an exception leaves the writer. A collector belongs
to the thread that opened it: another thread never reaches it, whatever context it runs in.
"""

from __future__ import annotations

import contextlib
import contextvars
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum


class InvalidOutcome(ValueError):
    """A combination of facts that the contract does not allow."""


class CanonicalStatus(str, Enum):
    DONE = "done"            # the existing operation returned normally (its existing success semantics)
    NOT_DONE = "not_done"    # it refused or raised, and the read-back established the prior state
    UNCERTAIN = "uncertain"  # it raised, and the read-back did not establish the prior state


class Readback(str, Enum):
    READ = "read"                    # target path read in full; observed_hash set
    ABSENT = "absent"                # target path observed absent
    FAILED = "failed"                # target path could not be examined; readback_errno set
    NOT_PERFORMED = "not_performed"  # phase P refused; nothing to compare


class HistoryStatus(str, Enum):      # about the operation's TARGET revision (save/rename content, delete tombstone)
    RECORDED = "recorded"                          # target committed; read-back verified it matches
    RECORDED_NOT_CURRENT = "recorded_not_current"  # target committed; read-back verified it does NOT match
    RECORDED_UNVERIFIED = "recorded_unverified"    # target committed; read-back FAILED, relationship unknown
    INCOMPLETE = "incomplete"                      # this op created history artefacts but committed no target
    NOT_RECORDED = "not_recorded"                  # this op created no history artefact for the target
    NOT_APPLICABLE = "not_applicable"              # phase P refused; no history step started


class AdoptionStatus(str, Enum):     # about the adoption revision, when the target path was untracked
    NOT_NEEDED = "not_needed"                              # path was tracked, new, or not adoptable (anomaly state)
    COMMITTED = "committed"                                # adoption record committed; re-check A7 matched
    COMMITTED_SOURCE_CHANGED = "committed_source_changed"  # committed; A7 read P and found different bytes
    COMMITTED_UNVERIFIED = "committed_unverified"          # committed; A7 could not read P
    INCOMPLETE = "incomplete"                              # adoption artefacts exist, no adoption record
    NOT_RECORDED = "not_recorded"                          # adoption failed before creating any artefact


class HistoryReason(str, Enum):
    HISTORY_ROOT_UNAVAILABLE = "history_root_unavailable"
    STATE_UNVERIFIABLE = "state_unverifiable"
    STATE_UNKNOWN = "state_unknown"
    STATE_CHANGED_OUTSIDE_HISTORY = "state_changed_outside_history"
    STATE_MISSING = "state_missing"
    STATE_TOMBSTONED_FILE_PRESENT = "state_tombstoned_file_present"
    IMPORT_OVERWRITE_UNDEFINED = "import_overwrite_undefined"
    SOURCE_READ_FAILED = "source_read_failed"
    OBJECT_RESERVATION_COLLISIONS_EXHAUSTED = "object_reservation_collisions_exhausted"
    OBJECT_RESERVATION_FAILED = "object_reservation_failed"
    IDENTITY_TEMP_WRITE_FAILED = "identity_temp_write_failed"
    IDENTITY_COMMIT_FAILED = "identity_commit_failed"
    REVISION_RESERVATION_COLLISIONS_EXHAUSTED = "revision_reservation_collisions_exhausted"
    REVISION_RESERVATION_FAILED = "revision_reservation_failed"
    SEQUENCE_EXHAUSTED = "sequence_exhausted"
    STAGING_WRITE_FAILED = "staging_write_failed"
    STAGING_READBACK_FAILED = "staging_readback_failed"
    BLOB_WRITE_FAILED = "blob_write_failed"
    BLOB_VERIFY_FAILED = "blob_verify_failed"
    RECORD_TEMP_WRITE_FAILED = "record_temp_write_failed"
    RECORD_COMMIT_FAILED = "record_commit_failed"
    RECORD_DURABILITY_FAILED = "record_durability_failed"
    ADOPTION_SOURCE_CHANGED = "adoption_source_changed"
    ADOPTION_RECHECK_FAILED = "adoption_recheck_failed"
    CANONICAL_STEP_FAILED = "canonical_step_failed"
    CANONICAL_READBACK_FAILED = "canonical_readback_failed"
    CANONICAL_MISMATCH = "canonical_mismatch"


class CanonicalReason(str, Enum):
    PRECONDITION_REFUSED = "precondition_refused"  # phase P refusal, including B2a lock refusal
    CANONICAL_STEP_FAILED = "canonical_step_failed"  # phase C raised


_C, _R, _H, _A = CanonicalStatus, Readback, HistoryStatus, AdoptionStatus
OPERATIONS = frozenset({"save", "rename", "delete", "copy", "doe", "import", "create"})
_RECORDED = frozenset({_H.RECORDED, _H.RECORDED_NOT_CURRENT, _H.RECORDED_UNVERIFIED})
_ADOPTED = frozenset({_A.COMMITTED, _A.COMMITTED_SOURCE_CHANGED, _A.COMMITTED_UNVERIFIED})
_EXAMINED = (_R.READ, _R.ABSENT)
_ANY = (_R.READ, _R.ABSENT, _R.FAILED)

# §7.2 "Every valid combination". The uncertain rows admit ABSENT as well as READ: a raised operation can leave the target
# absent, and §7.2's own rules then give "match" for a tombstone target and "not match" for a content target.
_VALID: frozenset[tuple[CanonicalStatus, Readback, HistoryStatus]] = frozenset({
    *((_C.DONE, r, _H.RECORDED) for r in _EXAMINED),
    *((_C.DONE, r, _H.RECORDED_NOT_CURRENT) for r in _EXAMINED),
    (_C.DONE, _R.FAILED, _H.RECORDED_UNVERIFIED),
    *((_C.DONE, r, h) for r in _ANY for h in (_H.INCOMPLETE, _H.NOT_RECORDED)),
    *((_C.NOT_DONE, r, h) for r in _EXAMINED for h in (_H.RECORDED_NOT_CURRENT, _H.INCOMPLETE, _H.NOT_RECORDED)),
    (_C.NOT_DONE, _R.NOT_PERFORMED, _H.NOT_APPLICABLE),
    *((_C.UNCERTAIN, r, h) for r in _EXAMINED for h in (_H.RECORDED, _H.RECORDED_NOT_CURRENT)),
    (_C.UNCERTAIN, _R.FAILED, _H.RECORDED_UNVERIFIED),
    *((_C.UNCERTAIN, r, h) for r in _ANY for h in (_H.INCOMPLETE, _H.NOT_RECORDED)),
})


@dataclass(frozen=True)
class WriteOutcome:
    operation: str                      # "save" | "rename" | "delete" | "copy" | "doe" | "import" | "create"
    path: str                           # target canonical path (Q for rename)
    previous_path: str | None           # P for rename
    canonical: CanonicalStatus
    canonical_reason: CanonicalReason | None
    canonical_error: str | None         # existing message, unchanged
    readback: Readback
    readback_errno: int | None
    observed_hash: str | None           # only when readback == READ
    previous_path_readback: Readback | None   # rename only (P after the operation)
    history: HistoryStatus
    history_reason: HistoryReason | None
    history_errno: int | None
    object_id: str | None               # set whenever this op committed or reserved artefacts for an object
    seq: str | None                     # target revision if allocated
    adoption: AdoptionStatus
    adopted_seq: str | None             # "00000001" whenever adoption COMMITTED*

    def __post_init__(self) -> None:
        problem = _problem(self)
        if problem:
            raise InvalidOutcome(problem)

    @property
    def complete(self) -> bool:
        return (self.canonical is CanonicalStatus.DONE
                and self.history is HistoryStatus.RECORDED
                and self.history_reason is None          # excludes record_durability_failed
                and self.adoption in (AdoptionStatus.NOT_NEEDED, AdoptionStatus.COMMITTED))

    @property
    def history_committed(self) -> bool:   # any committed artefact from this op
        return self.history in _RECORDED or self.adopted_seq is not None


def _problem(o: WriteOutcome) -> str | None:
    if o.operation not in OPERATIONS:
        return f"unknown operation {o.operation!r}"
    if (o.canonical, o.readback, o.history) not in _VALID:
        return f"{o.canonical.value}/{o.readback.value}/{o.history.value} is not a valid combination"
    if (o.canonical_reason is None) != (o.canonical is _C.DONE):
        return "a canonical reason exists exactly when the canonical operation did not end normally"
    if (o.observed_hash is not None) != (o.readback is _R.READ):
        return "observed_hash exists exactly when the read-back read the file"
    if (o.readback_errno is not None) != (o.readback is _R.FAILED):
        return "readback_errno exists exactly when the read-back failed"
    if (o.previous_path_readback is not None) != (o.previous_path is not None):
        return "previous_path_readback belongs to a rename"
    if o.history in (_H.INCOMPLETE, _H.NOT_RECORDED) and o.history_reason is None:
        return "a target that was not committed always has its reason"
    if o.history in _RECORDED and o.seq is None:  # I-6
        return "a committed target has a seq"
    if o.history is _H.NOT_APPLICABLE:  # I-4
        if o.seq is not None or o.object_id is not None or o.adoption is not _A.NOT_NEEDED:
            return "not_applicable goes with null ids and no adoption"
    if (o.adopted_seq is not None) != (o.adoption in _ADOPTED):  # I-5
        return "adopted_seq exists exactly when an adoption committed"
    if o.history is _H.NOT_RECORDED and o.seq is not None:
        return "not_recorded means no target revision was allocated"
    if o.adoption in (_A.COMMITTED_SOURCE_CHANGED, _A.COMMITTED_UNVERIFIED, _A.INCOMPLETE, _A.NOT_RECORDED):
        if o.history is not _H.NOT_RECORDED:
            return f"adoption {o.adoption.value} stops phase H, so the target is not_recorded"
    if o.adoption is _A.COMMITTED_SOURCE_CHANGED and o.history_reason is not HistoryReason.ADOPTION_SOURCE_CHANGED:
        return "committed_source_changed has the reason adoption_source_changed"
    if o.adoption is _A.COMMITTED_UNVERIFIED and o.history_reason is not HistoryReason.ADOPTION_RECHECK_FAILED:
        return "committed_unverified has the reason adoption_recheck_failed"
    return None


class RebaselineStatus(str, Enum):
    REFUSED = "refused"
    NOT_RECORDED = "not_recorded"
    INCOMPLETE = "incomplete"
    RECORDED = "recorded"
    RECORDED_NOT_CURRENT = "recorded_not_current"
    RECORDED_UNVERIFIED = "recorded_unverified"


REBASELINE_MODES = frozenset({"upgrade", "restore"})  # there is no other mode (§7.12)
_REB_COMMITTED = frozenset({RebaselineStatus.RECORDED, RebaselineStatus.RECORDED_NOT_CURRENT,
                            RebaselineStatus.RECORDED_UNVERIFIED})


@dataclass(frozen=True)
class RebaselineOutcome:
    """The result of an explicit rebaseline (§7.12). It is not a ``WriteOutcome``: no canonical operation exists."""
    mode: str
    path: str
    object_id: str | None
    seq: str | None
    status: RebaselineStatus
    reason: str | None
    errno: int | None
    detail: str

    def __post_init__(self) -> None:
        if self.mode not in REBASELINE_MODES:
            raise InvalidOutcome(f"unknown rebaseline mode {self.mode!r}")
        if self.status in _REB_COMMITTED and self.seq is None:
            raise InvalidOutcome("a committed rebaseline has a seq")
        if self.status in (RebaselineStatus.REFUSED, RebaselineStatus.NOT_RECORDED):
            if self.seq is not None or self.reason is None:
                raise InvalidOutcome("a rebaseline with no revision has a reason and no seq")
        if self.status is RebaselineStatus.INCOMPLETE and self.reason is None:
            raise InvalidOutcome("an incomplete rebaseline has a reason")

    @property
    def complete(self) -> bool:
        return self.status is RebaselineStatus.RECORDED and self.reason is None


class OutcomeSink:
    """The outcomes one ``collect_outcomes`` block received, in write order."""

    def __init__(self, parent: "OutcomeSink | None") -> None:
        self._outcomes: list = []
        self._thread = threading.get_ident()
        self._parent = parent

    @property
    def outcomes(self) -> tuple:
        return tuple(self._outcomes)

    @property
    def last(self):
        return self._outcomes[-1] if self._outcomes else None

    def __iter__(self) -> Iterator:
        return iter(tuple(self._outcomes))

    def __len__(self) -> int:
        return len(self._outcomes)


_COLLECTOR: contextvars.ContextVar[OutcomeSink | None] = contextvars.ContextVar("lpbf_outcome_collector", default=None)


@contextlib.contextmanager
def collect_outcomes() -> Iterator[OutcomeSink]:
    """Collect the outcome of every library write made by this thread inside the block."""
    sink = OutcomeSink(_COLLECTOR.get())
    token = _COLLECTOR.set(sink)
    try:
        yield sink
    finally:
        _COLLECTOR.reset(token)


def deliver(outcome) -> bool:
    """Give ``outcome`` to every collector open in this thread. False when none is."""
    me = threading.get_ident()
    sink, delivered = _COLLECTOR.get(), False
    while sink is not None:
        if sink._thread == me:
            sink._outcomes.append(outcome)
            delivered = True
        sink = sink._parent
    return delivered
