"""Restore one upgraded set file to its original format 1 bytes (B7 Phase 2, S3-B2c-b, stage b3).

``restore_file(sets_dir, file_name, *, now=None)`` puts a format 2 set back to the format 1 bytes
kept in its backup. It is the implementation of the frozen design revision 5
(``docs/audit/b7p2_s3b2c/design/report_rev5.md``, with revisions 2 and 3 where revision 5 does not
replace them) and of the accepted plan revision 4.7 (state machine, failure matrix and decision
table; the project owner accepted it as the documentation baseline on 2026-11-05). Section numbers
below ("SM") are those of the revision 4.7 state machine.

**What it does, in order** (SM sections 3.0 to 3.13):

* R0: the name, ``now`` and the platform; nothing is opened before this passes.
* R1: open the sets folder, identify its file system (ext4 or tmpfs only, by ``fstatfs`` magic,
  ``/proc/self/fdinfo`` mount ID and a strict ``/proc/self/mountinfo`` parse), open the migration
  folder M (never created, never through a link).
* R2: the exclusive sets-folder lock, then the exclusive M lock, each with its identity check
  (``locks.acquire_exclusive``; D-1: contention is ``refused_busy``, every other lock problem
  ``refused_unverifiable``).
* R2a: ``M/restores`` is created if missing (W-01; the only write before the baseline) and opened.
* R3: one observation round: a complete listing of every folder involved, one call of the frozen
  report's survey and decision, a second complete listing; both listings must be equal. The file
  is eligible only as P12 or P24 ``verified_upgraded`` with the upgrade history rules H1 to H9.
* R4, R5: complete reads of the set file (the upgraded bytes U) and of its backup (the original
  bytes O).
* R6 to R8: a new attempt folder ``M/restores/<restore_id>/`` with ``restore.json``
  (``preparing``), the preserved copy ``preserved/<file>`` = U and the staged original
  ``swap/<file>`` = O, each created exclusively, written in full, fsynced strictly and read back.
* R9: every check again in one round; R9b: the record says ``exchanging``.
* R10: exactly one ``renameat2(swap_fd, f, sets_fd, f, RENAME_EXCHANGE)``, then both folder
  ``fsync`` calls (OD-29: both are always made, once each, in order).
* R11: the 27 post-exchange checks; R12: the record becomes ``restored`` or ``uncertain``.

**What it never does.** It never deletes, moves or merges a displaced file, a preserved copy, a
backup, an upgrade record, another attempt, or anything it did not create; the only removal it can
make is of its own record temporary file inside a failed record update, and only where SM 0.5
allows it (after a failure at u3, u6 or u7, never after u4 or u5, never after a W-12 failure). It
never retries anything, never continues an earlier attempt, never writes an upgrade record, and has
no force, batch or automatic mode. Finalising an interrupted restore (P18 to P20) and resolving G6
are not part of it (D-6, OD-6, OD-7).

**Outcomes** (SM 0.9) are ``refused_platform``, ``refused_invalid``, ``refused_busy``,
``refused_unverifiable``, ``refused_not_restorable``, ``already_restored``, ``restored``, ``failed``
and ``uncertain``. Each says whether it is ``confirmed`` (observed **and** durable, SM 0.5a), keeps
what the run observed apart from what is inferred, and names the next report it *predicts*; a
prediction is never an observation. ``fsync`` returning is what the kernel reported; it is not a
power-loss guarantee, and nothing here claims one.

**Platform.** Linux x86_64 with glibc ``renameat2``, on ext4 or tmpfs, only; everything else is
refused before any write. The exchange flag is silently ignored by some FUSE mounts, which is why
the file-system allowlist is enforced.
"""

from __future__ import annotations

import errno as _errno
import hashlib
import os
import secrets
import stat
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from . import reconcile as rc
from . import restore_record as rr
from . import upgrade as up
from .fsprim import (
    FsprimError, Incomplete, UnsupportedPlatform, WriteIncomplete, check_architecture, complete_listing,
    complete_read, exchange, fd_mount_id, identify_fs, strict_fsync, write_in_full,
)
from .fsprim import _renameat2, _symbol
from .library import _parse_set
from .locks import LockRefused, Refusal, acquire_exclusive
from .locks import platform_problem as _lock_platform_problem
from .migration import SOURCE_SCHEMA_VERSION, TARGET_SCHEMA_VERSION, MigrationRecord, RecordStatus, parse_record
from .restore_record import DisplacedObservation, RestoreRecord, RestoreRecordError, RestoreStatus

M, R, RS = up.MIGRATION_DIR, up.RECORDS_DIR, rr.RESTORES_DIR
RECORD, PRESERVED, SWAP = rr.RECORD_NAME, rr.PRESERVED_DIR, rr.SWAP_DIR
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_TMP_PREFIX = f".{RECORD}."
_TMP_SUFFIX = ".tmp"

# SM 3.11 / OD-21, OD-25: the errno classes of an exchange error. Class A may lead to ``failed`` only
# with complete, matching position evidence read after the error (E-5); class C (ENOENT) and every
# other errno (class B, EROFS and EINVAL included) never do.
CLASS_A = frozenset({_errno.ENOSYS, _errno.EXDEV, _errno.EBADF, _errno.EFAULT, _errno.ENOTDIR, _errno.ENAMETOOLONG})
CLASS_C = frozenset({_errno.ENOENT})


class Result(str, Enum):
    REFUSED_PLATFORM = "refused_platform"
    REFUSED_INVALID = "refused_invalid"
    REFUSED_BUSY = "refused_busy"
    REFUSED_UNVERIFIABLE = "refused_unverifiable"
    REFUSED_NOT_RESTORABLE = "refused_not_restorable"
    ALREADY_RESTORED = "already_restored"
    RESTORED = "restored"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class Observation:
    """One fact of the run. ``label`` is ``observed`` (read in this run, with the check named),
    ``inferred`` (a conclusion from observations and a stated assumption) or ``predicted`` (what
    the next report is expected to return; only running it confirms it)."""
    what: str
    value: str
    label: str = "observed"


@dataclass(frozen=True)
class RestoreOutcome:
    """What one restore call did (SM 0.2, 0.5a, 0.8, 0.9). This is the returned value only: it is
    not stored anywhere, and it does not exist if the process is killed (F-03)."""
    outcome: Result
    reason: str
    confirmed: bool
    restore_id: str | None = None
    failed_kind: str | None = None        # "a", "b" or "c" for ``failed`` (SM 0.9)
    record_observed: str = "none"         # a record status, "none" or "unknown"
    record_durable: str = "n/a"           # yes, no, tmp-removal-not-durable, unknown, n/a
    exchange_called: str = "no"           # no, error:<errno>, ok
    exchange_durable: str = "n/a"         # yes, no, unknown, n/a
    restores_dir: str = "unknown"         # pre-existing, created-durable, created-not-durable, unknown
    attempt_dir: str = "not-created"      # not-created, created-durable, created-not-durable, unknown
    last_observed_op: str | None = None
    last_durable_op: str | None = None
    predicted_report: str | None = None   # the predicted next report rule (never observed here)
    record: RestoreRecord | None = None   # the record as last observed, when it is
    observations: tuple[Observation, ...] = ()

    def facts(self, what: str) -> list[str]:
        return [o.value for o in self.observations if o.what == what]


# --------------------------------------------------------------------------- narrow helpers
# Each wraps one system call or one primitive, so that tests can inject a failure at one point and
# record the order of calls (``_trace``). Nothing here retries.


def _trace(event: str, detail: str = "") -> None:
    """Called before every system call that reads or writes; a no-op unless a test replaces it."""


def _mkdirat(dir_fd: int, name: str, what: str) -> None:
    _trace("mkdir", what)
    os.mkdir(name, 0o755, dir_fd=dir_fd)


def _openat(dir_fd: int, name: str, flags: int, mode: int, what: str) -> int:
    _trace("open", what)
    return os.open(name, flags, mode, dir_fd=dir_fd)


def _open_dir(ctx, path: Path, what: str) -> int:
    _trace("opendir", what)
    return up._open_sub(ctx, path)


def _fstat(fd: int, what: str) -> os.stat_result:
    _trace("fstat", what)
    return os.fstat(fd)


def _lstat_at(dir_fd: int, name: str, what: str) -> os.stat_result:
    _trace("lstat", what)
    return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)


def _stat_path(path: Path, what: str) -> os.stat_result:
    _trace("stat", what)
    return os.stat(path)


def _write(fd: int, data: bytes, what: str) -> None:
    _trace("write", what)
    write_in_full(fd, data)


def _fsync(fd: int, what: str) -> None:
    _trace("fsync", what)
    strict_fsync(fd)


def _close(fd: int, what: str) -> None:
    _trace("close", what)
    os.close(fd)


def _renameat(dir_fd: int, src: str, dst: str, what: str) -> None:
    _trace("rename", what)
    os.rename(src, dst, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)


def _unlinkat(dir_fd: int, name: str, what: str) -> None:
    _trace("unlink", what)
    os.unlink(name, dir_fd=dir_fd)


def _exchange(w_fd: int, sets_fd: int, name: str) -> None:
    _trace("exchange", name)
    exchange(w_fd, name, sets_fd, name)


def _listing(fd: int, what: str):
    _trace("list", what)
    return complete_listing(fd)


def _read(dir_fd: int, name: str, what: str):
    _trace("read", what)
    return complete_read(dir_fd, name)


def _mount_id(fd: int, what: str) -> int:
    _trace("mnt_id", what)
    return fd_mount_id(fd)


def _identify(fd: int):
    _trace("identify", "sets")
    return identify_fs(fd)


def _survey(ctx, base: Path):
    _trace("survey", "R3")
    return rc._survey(ctx, base)


def _new_rid() -> str:
    return secrets.token_hex(16)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- small helpers


def _ident(st) -> tuple[int, int]:
    return st.st_dev, st.st_ino


def _text(exc: BaseException) -> str:
    if isinstance(exc, Incomplete):
        return exc.reason
    if isinstance(exc, OSError):
        return f"{_errno.errorcode.get(exc.errno, exc.errno)}: {exc.strerror or exc}" if exc.errno else str(exc)
    return str(exc) or type(exc).__name__


def _errname(exc: BaseException) -> str:
    err = getattr(exc, "errno", None)
    return _errno.errorcode.get(err, str(err)) if err else "no-errno"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _valid_now(now) -> bool:
    if now is None:
        return True
    if not isinstance(now, str):
        return False
    try:
        when = datetime.fromisoformat(now.replace("Z", "+00:00"))
    except ValueError:
        return False
    return when.utcoffset() is not None and when.utcoffset().total_seconds() == 0


def _entries(listing) -> dict:
    """A listing as {name: (kind, nlink, dev, ino)}: the comparison form of SM 3.4."""
    return {n: (e.kind, e.nlink, e.dev, e.ino) for n, e in listing.entries.items()}


class _Check:
    """One named check of an observation round: pass, fail or incomplete (never stops the round)."""
    __slots__ = ("name", "status", "detail", "kind")

    def __init__(self, name: str, kind: str = "") -> None:
        self.name, self.status, self.detail, self.kind = name, "pass", "", kind

    def fail(self, detail: str) -> None:
        if self.status != "fail":
            self.status, self.detail = "fail", detail

    def incomplete(self, detail: str) -> None:
        if self.status == "pass":
            self.status, self.detail = "incomplete", detail

    def __str__(self) -> str:
        return f"{self.name}: {self.status}" + (f" ({self.detail})" if self.detail else "")


def _summary(checks) -> str:
    return "; ".join(str(c) for c in checks)


class _Stop(Exception):
    """Ends the run with ``outcome``; raised only by the run's own decisions."""

    def __init__(self, outcome: RestoreOutcome) -> None:
        super().__init__(outcome.reason)
        self.outcome = outcome


@dataclass
class _UFail:
    """A failed U-update (SM 0.5): where it failed, what W-16 did and what Q-Ω observed."""
    step: str
    error: str
    w16: str = "not-run"              # not-run, ok, unlink-failed, u10-failed
    q: str | None = None              # new, old, old+tmp, unknown; None when Q-Ω did not run (u1)
    q_detail: str = ""
    q_record: RestoreRecord | None = None
    record_durable: str = "unknown"


# --------------------------------------------------------------------------- the run


class _Run:
    """The state of one restore call: descriptors, identities recorded at defined points, the
    baseline L1, the record as last written, and the result fields of SM 0.5a."""

    def __init__(self, base: Path, f: str, now: str | None) -> None:
        self.base, self.f, self.now = base, f, now
        self.obs: list[Observation] = []
        self.restores_dir = "unknown"
        self.attempt_dir = "not-created"
        self.last_observed: str | None = None
        self.last_durable: str | None = None
        self.exchange_called = "no"
        self.exchange_durable = "n/a"
        self.stage = "ST-PRE"
        self.rid: str | None = None
        self.record: RestoreRecord | None = None
        self.rbytes: bytes | None = None
        self.rident: tuple[int, int] | None = None
        self.w12_failed = False
        self.created_restores = False
        self.i_p_candidate: tuple[int, int] | None = None
        self.i_t_candidate: tuple[int, int] | None = None
        self.r8_started = False
        self.fds: list[int] = []
        self.release: list[str] = []
        self.a_fd = self.p_fd = self.w_fd = None
        self.i_a = self.i_p = self.i_w = None
        self.i_a_nlink: int | None = None
        self.i_s = self.i_t = self.i_pf = None
        self.r_fd: int | None = None

    # ---------------------------------------------------------------- reporting

    def note(self, what: str, value: str, label: str = "observed") -> None:
        self.obs.append(Observation(what, value, label))

    def outcome(self, result: Result, reason: str, *, confirmed: bool, failed_kind: str | None = None,
                record_observed: str | None = None, record_durable: str | None = None,
                predicted: str | None = None, record: RestoreRecord | None = None) -> RestoreOutcome:
        if predicted is not None:
            self.note("predicted report", predicted, "predicted")
        if self.created_restores and result.value.startswith("refused"):
            reason += f"; the empty folder {M}/{RS} created by this run is left in place"
        return RestoreOutcome(
            result, reason, confirmed, self.rid, failed_kind,
            record_observed if record_observed is not None else (self.record.status.value if self.record else "none"),
            record_durable if record_durable is not None else ("yes" if self.record else "n/a"),
            self.exchange_called, self.exchange_durable, self.restores_dir, self.attempt_dir,
            self.last_observed, self.last_durable, predicted,
            record if record is not None else self.record, tuple(self.obs))

    def stop(self, *a, **k) -> None:
        raise _Stop(self.outcome(*a, **k))

    def refuse(self, result: Result, reason: str, confirmed: bool = True) -> None:
        if self.restores_dir == "created-not-durable":
            confirmed = False
        self.stop(result, reason, confirmed=confirmed, record_observed="none", record_durable="n/a",
                  predicted="unchanged")

    def wrote(self, op: str, durable: bool) -> None:
        self.last_observed = op
        if durable:
            self.last_durable = op

    def keep(self, fd: int) -> int:
        self.fds.append(fd)
        return fd

    def close_all(self, ctx) -> None:
        """Close every descriptor of the run once: the attempt folders, then the folders held in the
        operation's context (the sets folder, M, records, restores and the baseline attempts), which
        frees the locks. A failure is kept as a release fact; it never replaces the outcome."""
        for what, fds in (("attempt folder", self.fds), ("library folder", ctx.fds)):
            for fd in reversed(fds):
                try:
                    _close(fd, what)
                except OSError as exc:
                    self.release.append(f"closing a {what} descriptor failed ({_text(exc)}); on Linux the "
                                        "descriptor is released even so (inferred from close(2))")
            fds.clear()
        ctx.dirs.clear()


def _with_release(run: _Run, outcome: RestoreOutcome) -> RestoreOutcome:
    """The outcome with the release facts added. Cleanup never replaces the outcome (the B1b
    contract): the result, ``confirmed`` and every record and exchange field are kept as decided; the
    facts are appended to the reason and to the observations."""
    if not run.release:
        return outcome
    return replace(outcome, reason=f"{outcome.reason}; releasing the locks: {'; '.join(run.release)}",
                   observations=outcome.observations + tuple(Observation("lock release", t) for t in run.release))


def restore_file(sets_dir: Path | str, file_name: str, *, now: str | None = None) -> RestoreOutcome:
    """Restore ``<sets_dir>/<file_name>`` to its original format 1 bytes (see the module docstring).

    Every expected problem gives an outcome; nothing is retried. A ``KeyboardInterrupt`` or a kill
    is passed on: the locks are released by closing the descriptors, and whatever the run had
    written stays on disk as evidence for the next report."""
    # R0: arguments and platform, before anything is opened
    if not isinstance(file_name, str) or not up._NAME.match(file_name):
        return RestoreOutcome(Result.REFUSED_INVALID, f"{file_name!r} is not a plain set file name in the sets "
                                                      "folder; nothing was read or written", True)
    if not _valid_now(now):
        return RestoreOutcome(Result.REFUSED_INVALID, f"now={now!r} is not an ISO 8601 UTC time; nothing was read "
                                                      "or written", True)
    problem = _platform_problem()
    if problem:
        return RestoreOutcome(Result.REFUSED_PLATFORM, f"restore is not supported on this platform: {problem}; "
                                                       "nothing was read or written", True)
    base = Path(os.path.realpath(sets_dir))
    run = _Run(base, file_name, now)
    with up._context(fresh=True) as ctx:
        try:
            outcome = _restore(run, ctx)
        except _Stop as stop:
            outcome = stop.outcome
        finally:  # also when an interrupt propagates: the locks end with the descriptors
            run.close_all(ctx)
    return _with_release(run, outcome)


def _platform_problem() -> str | None:
    for problem in (up._platform_problem(), _lock_platform_problem()):
        if problem:
            return f"it {problem}"
    try:
        check_architecture()
        _symbol("fstatfs")
        _renameat2()
    except UnsupportedPlatform as exc:
        return str(exc)
    if not hasattr(os, "O_CLOEXEC"):
        return "it needs os.O_CLOEXEC"
    return None


def _restore(run: _Run, ctx) -> RestoreOutcome:
    base = run.base

    # ---------------------------------------------------------------- R1: open and identify
    try:
        sets_fd = up._open_base(ctx, base)
    except OSError as exc:
        run.refuse(Result.REFUSED_UNVERIFIABLE, f"the sets folder cannot be opened: {_text(exc)}; nothing was "
                                                "written")
    try:
        fsid = _identify(sets_fd)
    except UnsupportedPlatform as exc:
        run.refuse(Result.REFUSED_PLATFORM, f"restore is not supported here: {exc}; nothing was written")
    except (FsprimError, OSError) as exc:
        run.refuse(Result.REFUSED_PLATFORM, f"the file system of the sets folder is not a supported, identified "
                                            f"local file system (ext4 or tmpfs): {_text(exc)}; nothing was written")
    run.fsid = fsid
    run.note("file system", f"{fsid.fs_type} (magic {fsid.magic:#x}), mount {fsid.mnt_id}")
    try:
        m_fd = _open_dir(ctx, base / M, "M")
    except FileNotFoundError:
        run.refuse(Result.REFUSED_NOT_RESTORABLE, f"there is no migration folder {M}: this library was never "
                                                  "upgraded; nothing was written")
    except OSError as exc:
        run.refuse(Result.REFUSED_UNVERIFIABLE, f"the migration folder {M} cannot be opened as a folder (it is "
                                                f"not followed through a link): {_text(exc)}; nothing was written")
    run.sets_fd, run.m_fd = sets_fd, m_fd
    _same_mount(run, m_fd, "M", Result.REFUSED_PLATFORM)

    # ---------------------------------------------------------------- R2: locks
    try:
        held = acquire_exclusive(base, sets_fd, m_fd)
    except LockRefused as exc:
        result = {Refusal.BUSY: Result.REFUSED_BUSY, Refusal.UNSUPPORTED: Result.REFUSED_PLATFORM}.get(
            exc.refusal, Result.REFUSED_UNVERIFIABLE)
        run.note("lock refusal", f"{exc.step.value}: {exc.detail}")
        run.refuse(result, f"{exc.detail}; nothing was written")
    try:
        return _locked(run, ctx, held)
    finally:  # the outcome is already decided; release facts are added to it afterwards (_with_release)
        try:
            held.release()
        except OSError as exc:  # B1b contract: only a descriptor locks.py owns is closed there; none is here
            run.release.append(f"release failed ({_text(exc)})")
        if held.unlock_error is not None:
            run.release.append(f"unlocking failed ({_text(held.unlock_error)}); the locks end when the run closes "
                               "its descriptors, which it does next")


def _same_mount(run: _Run, fd: int, what: str, result: Result) -> None:
    """The folder ``fd`` is on the sets folder's device and mount (SM 0.7, OD-19)."""
    try:
        st = _fstat(fd, what)
        mnt = _mount_id(fd, what)
    except (FsprimError, OSError) as exc:
        run.refuse(result, f"the mount of {what} cannot be identified: {_text(exc)}; nothing was written")
    if st.st_dev != run.fsid.st_dev or mnt != run.fsid.mnt_id:
        run.refuse(result, f"{what} is on another device or mount (device {st.st_dev}, mount {mnt}; the sets folder: "
                           f"device {run.fsid.st_dev}, mount {run.fsid.mnt_id}); a mount point or bind mount inside "
                           "the library is not supported; nothing was written")


def _locked(run: _Run, ctx, held) -> RestoreOutcome:
    base, f, m_fd, sets_fd = run.base, run.f, run.m_fd, run.sets_fd
    run.i_sets = _ident(os.fstat(sets_fd))
    run.i_m = _ident(os.fstat(m_fd))

    # ---------------------------------------------------------------- R2a: M/restores (W-01)
    try:
        _mkdirat(m_fd, RS, "W-01")
    except FileExistsError:
        run.restores_dir = "pre-existing"
    except OSError as exc:
        run.note("W-01", f"mkdirat failed: {_text(exc)}")
        _observe_restores(run)
        run.refuse(Result.REFUSED_UNVERIFIABLE, f"the restores folder {M}/{RS} could not be created "
                                                f"({_text(exc)}); nothing else was written")
    else:
        run.created_restores = True
        run.wrote("W-01", False)
        try:
            _fsync(m_fd, "M")
        except OSError as exc:
            run.restores_dir = "created-not-durable"
            run.note("W-01 fsync(M)", f"failed: {_text(exc)}")
            _observe_restores(run)
            run.created_restores = False  # named in the reason below instead
            run.stop(Result.REFUSED_UNVERIFIABLE, f"the restores folder {M}/{RS} was created, but its durability "
                                                  f"could not be confirmed ({_text(exc)}); nothing else was written; "
                                                  "the folder is left in place", confirmed=False,
                     record_observed="none", record_durable="n/a", predicted="unchanged")
        run.restores_dir = "created-durable"
        run.wrote("W-01", True)
    try:
        rs_fd = _open_dir(ctx, base / M / RS, "RS")
        st_rs, at = _fstat(rs_fd, "RS"), _lstat_at(m_fd, RS, "M/restores")
    except OSError as exc:
        run.refuse(Result.REFUSED_UNVERIFIABLE, f"the restores folder {M}/{RS} cannot be opened as a folder (a "
                                                f"link or not a folder): {_text(exc)}")
    if not stat.S_ISDIR(at.st_mode) or _ident(at) != _ident(st_rs):
        run.refuse(Result.REFUSED_UNVERIFIABLE, f"{M}/{RS} is not the folder that was opened")
    run.rs_fd, run.i_rs = rs_fd, _ident(st_rs)
    _same_mount(run, rs_fd, f"{M}/{RS}", Result.REFUSED_PLATFORM)

    # ---------------------------------------------------------------- R3: the round R3-Ω
    l0 = _baseline(run, ctx)
    sv = _survey(ctx, base)
    decision = None
    survey_problem = None
    if isinstance(sv, rc._Block):
        survey_problem = sv
    else:
        decision = rc._decide(sv, f)
        urec_read = _read_urec(run, sv, decision)
    l1 = _baseline(run, ctx)
    if survey_problem is not None:
        run.note("survey", f"{survey_problem.rule}: {survey_problem.reason}")
        what = ("the survey found the migration folder locked although this run holds its lock (the lock state "
                "cannot be verified)" if survey_problem.outcome is rc.Outcome.BUSY else
                f"the library cannot be judged ({survey_problem.rule}: {survey_problem.reason})")
        run.refuse(Result.REFUSED_UNVERIFIABLE, f"{what}; nothing was written")
    if isinstance(l0, str) or isinstance(l1, str) or l0 != l1:
        detail = l0 if isinstance(l0, str) else l1 if isinstance(l1, str) else "the two listings differ"
        run.note("R3-Ω listings", detail)
        run.refuse(Result.REFUSED_UNVERIFIABLE, "the library changed or could not be listed completely during the "
                                                f"baseline observation ({detail}); nothing was written")
    run.l1 = l1
    run.note("R3 decision", f"{decision.rule} {decision.outcome.value}: {decision.reason}")
    if decision.rule == "P20R" and decision.outcome is rc.Outcome.VERIFIED_RESTORED:
        run.stop(Result.ALREADY_RESTORED, f"already restored (P20R): {decision.reason}; nothing was written",
                 confirmed=run.restores_dir != "created-not-durable", record_observed="none", record_durable="n/a",
                 predicted="P20R")
    if decision.rule not in ("P12", "P24") or decision.outcome is not rc.Outcome.VERIFIED_UPGRADED:
        result = (Result.REFUSED_UNVERIFIABLE if decision.outcome in (rc.Outcome.UNVERIFIABLE, rc.Outcome.BUSY)
                  else Result.REFUSED_NOT_RESTORABLE)
        run.refuse(result, f"not restorable: the report gives {decision.rule} ({decision.outcome.value}): "
                           f"{decision.reason}; a restore needs P12 or P24 verified_upgraded; nothing was written")
    urec = _history(run, sv, decision)
    if isinstance(urec_read, str):
        run.refuse(Result.REFUSED_UNVERIFIABLE, f"the upgrade record could not be read completely: {urec_read}")
    run.urec, (run.urec_bytes, run.i_urec) = urec, urec_read

    # ---------------------------------------------------------------- R4, R5: S and B
    s = _complete(run, sets_fd, f, "S")
    if isinstance(s, str):
        run.refuse(Result.REFUSED_UNVERIFIABLE, f"the set file could not be read completely: {s}")
    if (len(s.data), _sha(s.data)) != (urec.upgraded_size_bytes, urec.upgraded_sha256):
        run.refuse(Result.REFUSED_NOT_RESTORABLE, "the set file does not hold the upgraded bytes its record names")
    run.U, run.i_s = s.data, (s.dev, s.ino)
    b = _complete(run, m_fd, f, "B")
    if isinstance(b, str):
        run.refuse(Result.REFUSED_UNVERIFIABLE, f"the backup could not be read completely: {b}")
    if (len(b.data), _sha(b.data)) != (urec.source_size_bytes, urec.source_sha256):
        run.refuse(Result.REFUSED_NOT_RESTORABLE, f"the backup {M}/{f} does not hold the original bytes")
    run.O, run.i_b = b.data, (b.dev, b.ino)
    run.note("R4/R5", f"S = U at {run.i_s}; B = O")

    # ---------------------------------------------------------------- R6: the attempt (W-02 to W-05)
    rid = _new_rid()
    while rid in l1["RS"]:
        rid = _new_rid()
    try:
        first = _build(run, rid, RestoreStatus.PREPARING)
    except RestoreRecordError as exc:
        run.refuse(Result.REFUSED_UNVERIFIABLE, f"the restore record cannot be built: {exc}")
    _create_attempt(run, rid)
    _write_record(run, first)

    # ---------------------------------------------------------------- R7: the preserved copy
    _stage_copy(run, "P")
    _update(run, "W-07", run.record.model_copy(update={"status": RestoreStatus.PRESERVED}))
    # ---------------------------------------------------------------- R8: the staged original
    run.r8_started = True
    _stage_copy(run, "W")
    _update(run, "W-09", run.record.model_copy(update={"staged_identity": list(run.i_t)}))
    # ---------------------------------------------------------------- R9: R9-Ω, then R9b (W-10)
    _r9(run)
    _update(run, "W-10", run.record.model_copy(update={"status": RestoreStatus.EXCHANGING,
                                                       "exchange_attempted": True}))
    run.stage = "ST-NOTCALLED"
    # ---------------------------------------------------------------- R10: the exchange
    return _exchange_and_finish(run)


def _observe_restores(run: _Run) -> None:
    try:
        st = _lstat_at(run.m_fd, RS, "M/restores")
    except FileNotFoundError:
        run.note(f"{M}/{RS}", "absent")
        return
    except OSError as exc:
        run.note(f"{M}/{RS}", f"incomplete: {_text(exc)}")
        return
    run.note(f"{M}/{RS}", "present (folder)" if stat.S_ISDIR(st.st_mode) else "other")


def _complete(run: _Run, dir_fd: int, name: str, what: str):
    """A complete read (SM 0.4), or the reason it is incomplete."""
    try:
        return _read(dir_fd, name, what)
    except (FsprimError, OSError) as exc:
        return _text(exc)


def _list(fd: int, what: str):
    """A complete listing as {name: entry}, or the reason it is incomplete."""
    try:
        return _entries(_listing(fd, what))
    except (FsprimError, OSError) as exc:
        return _text(exc)


def _baseline(run: _Run, ctx):
    """L0 or L1 (SM 3.4): complete listings of sets, M, R, RS, and of every attempt folder in RS with
    its P and W, as {component: {name: entry}}; or the reason a listing is incomplete."""
    base = run.base
    out = {}
    for comp, fd in (("sets", run.sets_fd), ("M", run.m_fd)):
        got = _list(fd, comp)
        if isinstance(got, str):
            return f"the listing of {comp} is incomplete: {got}"
        out[comp] = got
    if R in out["M"]:
        try:
            run.r_fd = _open_dir(ctx, base / M / R, "R")
        except OSError as exc:
            return f"the records folder cannot be opened: {_text(exc)}"
        got = _list(run.r_fd, "R")
        if isinstance(got, str):
            return f"the listing of R is incomplete: {got}"
        out["R"] = got
    got = _list(run.rs_fd, "RS")
    if isinstance(got, str):
        return f"the listing of RS is incomplete: {got}"
    out["RS"] = got
    for name, entry in sorted(got.items()):
        if entry[0] != "directory":
            continue  # not an attempt folder; the survey's G6 decides
        try:
            afd = _open_dir(ctx, base / M / RS / name, f"A[{name}]")
        except OSError as exc:
            return f"the attempt folder {name} cannot be opened: {_text(exc)}"
        sub = _list(afd, f"A[{name}]")
        if isinstance(sub, str):
            return f"the listing of attempt {name} is incomplete: {sub}"
        out[("A", name)] = sub
        for part, label in ((PRESERVED, "P"), (SWAP, "W")):
            if sub.get(part, ("",))[0] != "directory":
                continue
            try:
                pfd = _open_dir(ctx, base / M / RS / name / part, f"{label}[{name}]")
            except OSError as exc:
                return f"the folder {name}/{part} cannot be opened: {_text(exc)}"
            inside = _list(pfd, f"{label}[{name}]")
            if isinstance(inside, str):
                return f"the listing of {name}/{part} is incomplete: {inside}"
            out[(label, name)] = inside
    return out


def _read_urec(run: _Run, sv, decision):
    """The complete read of the file's ``upgraded`` record, in R3-Ω: (bytes, identity) or a reason."""
    recs = [r for r in sv.records.get(run.f, []) if r.status is RecordStatus.UPGRADED]
    if len(recs) != 1 or run.r_fd is None:
        return "there is not exactly one upgraded record for the file"
    got = _complete(run, run.r_fd, f"{recs[0].record_id}.json", "U-rec")
    if isinstance(got, str):
        return got
    try:
        again = parse_record(got.data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return f"it does not parse: {exc}"
    if again != recs[0]:
        return "it is not the record the survey read"
    return got.data, (got.dev, got.ino)


def _history(run: _Run, sv, decision) -> MigrationRecord:
    """H1 to H9 (revision 3 section 6) on the records the survey parsed, plus the residue rule of
    revision 1 section 4.4. Any doubt refuses (``refused_not_restorable``)."""
    f = run.f
    backup = f"{M}/{f}"
    mine = sv.records.get(f, [])
    every = [r for recs in sv.records.values() for r in recs]

    def refuse(rule: str, why: str) -> None:
        run.refuse(Result.REFUSED_NOT_RESTORABLE, f"the upgrade history of this file cannot be established uniquely "
                                                  f"({rule}: {why}); nothing was written")

    upgraded = [r for r in mine if r.status is RecordStatus.UPGRADED]
    if len(upgraded) != 1:
        refuse("H7", f"{len(upgraded)} upgraded records")
    urec = upgraded[0]
    for r in mine:
        if r.source_path != f:
            refuse("H1", f"record {r.record_id} names {r.source_path!r}")
        if r.backup_path != backup:
            refuse("H2", f"record {r.record_id} names the backup {r.backup_path!r}")
        if r.source_schema_version != SOURCE_SCHEMA_VERSION or r.target_schema_version != TARGET_SCHEMA_VERSION:
            refuse("H4", f"record {r.record_id} is not a format 1 to format 2 attempt")
        if r is urec:
            continue
        if r.status is not RecordStatus.FAILED:
            refuse("H7", f"record {r.record_id} is {r.status.value}")
        if (r.source_sha256, r.source_size_bytes) != (urec.source_sha256, urec.source_size_bytes):
            refuse("H3", f"the failed record {r.record_id} names other original bytes")
        if r.upgraded_sha256 is not None and r.upgraded_sha256 == urec.upgraded_sha256:
            refuse("H5", f"the failed record {r.record_id} claims the same upgraded bytes")
        if _when(r.created_at) > _when(urec.completed_at):
            refuse("H8", f"the failed record {r.record_id} was created after the upgrade completed")
    for r in every:
        if r.backup_path == backup and r.source_path != f:
            refuse("H2", f"record {r.record_id} of {r.source_path!r} names this backup")
    ids = [r.record_id for r in every]
    if len(ids) != len(set(ids)):
        refuse("H6", "a record id appears twice")
    if sv.orphan_record_tmps or sv.unexpected_in_records:
        refuse("H9", "an orphan record temporary file or an unexpected entry is in the records folder")
    if sv.staged.get(f) or any(sv.record_tmps.get(r.record_id) for r in mine):
        refuse("H9", "upgrade residue (a staged file or a record temporary file) exists for this file")
    return urec


def _when(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _build(run: _Run, rid: str, status: RestoreStatus) -> RestoreRecord:
    u = run.urec
    data = {
        "restore_record_schema_version": rr.RESTORE_RECORD_SCHEMA_VERSION, "restore_id": rid,
        "upgrade_record_id": u.record_id, "source_path": run.f, "backup_path": f"{M}/{run.f}",
        "current_sha256": u.upgraded_sha256, "current_size_bytes": u.upgraded_size_bytes,
        "restored_sha256": u.source_sha256, "restored_size_bytes": u.source_size_bytes,
        "source_identity": list(run.i_s), "staged_identity": None,
        "preserved_path": f"{RS}/{rid}/{PRESERVED}/{run.f}", "swap_path": f"{RS}/{rid}/{SWAP}/{run.f}",
        "exchange_attempted": False, "displaced_observed_at_restore": None, "displaced_matches_upgraded": None,
        "status": status.value, "created_at": run.now or _now(), "completed_at": None, "error": None,
    }
    return _validated(data)


def _validated(data: dict) -> RestoreRecord:
    """The record through the frozen contract (FE-7): parse(dump(record))."""
    try:
        rec = RestoreRecord.model_validate(data) if isinstance(data, dict) else data
    except ValueError as exc:  # pydantic's ValidationError: refused by the frozen contract
        raise RestoreRecordError(f"invalid restore record: {exc}") from None
    return rr.parse_restore_record(rr.dump_restore_record(rec))


def _final(run: _Run, status: RestoreStatus, error: str | None, observation: DisplacedObservation | None):
    """A final record from the last one written; raises RestoreRecordError if the contract refuses."""
    data = run.record.model_dump(mode="json")
    data.update(status=status.value, completed_at=run.now or _now(), error=error)
    if observation is not None:
        data.update(displaced_observed_at_restore=observation.model_dump(mode="json"),
                    displaced_matches_upgraded=observation.kind == "regular" and
                    (observation.sha256, observation.size) == (run.urec.upgraded_sha256, run.urec.upgraded_size_bytes))
    return _validated(data)


# --------------------------------------------------------------------------- R6


def _create_attempt(run: _Run, rid: str) -> None:
    """W-02 to W-04 with the R6C-Ω capture (SM 3.6). Every failure stops the run with no cleanup."""
    rs_fd = run.rs_fd
    run.rid = rid
    try:
        _mkdirat(rs_fd, rid, "W-02")
    except OSError as exc:
        _r6n(run, exc)
    run.attempt_dir = "unknown"
    run.wrote("W-02", False)
    try:
        _fsync(rs_fd, "RS")
    except OSError as exc:
        run.attempt_dir = "created-not-durable"
        run.note("W-02 fsync(RS)", f"failed: {_text(exc)}")
        _observe_attempt(run)
        run.stop(Result.UNCERTAIN, f"the attempt folder {rid} was created, but its durability could not be "
                                   f"confirmed ({_text(exc)}); nothing else was written; the folder is left in place",
                 confirmed=False, record_observed="none", record_durable="n/a",
                 predicted=run.r6pd_prediction)
    run.attempt_dir = "created-durable"
    run.wrote("W-02", True)

    def r6p(why: str) -> None:
        run.note("R6-P", why)
        run.stop(Result.UNCERTAIN, f"attempt folder partly created ({why}); the set file was not touched; nothing "
                                   "was removed", confirmed=False, record_observed="none", record_durable="n/a",
                 predicted="G6")

    def open_checked(parent: int, name: str, what: str) -> tuple[int, tuple[int, int]]:
        try:
            fd = run.keep(_openat(parent, name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC, 0, what))
            st, at = _fstat(fd, what), _lstat_at(parent, name, what)
            mnt = _mount_id(fd, what)
        except (FsprimError, OSError) as exc:
            r6p(f"{what} could not be opened and identified: {_text(exc)}")
        if not stat.S_ISDIR(at.st_mode) or _ident(at) != _ident(st):
            r6p(f"{what} is not the folder that was created")
        if st.st_dev != run.fsid.st_dev or mnt != run.fsid.mnt_id:
            r6p(f"{what} is on another device or mount")
        return fd, _ident(st)

    run.a_fd, run.i_a = open_checked(rs_fd, rid, "A")
    try:
        _mkdirat(run.a_fd, PRESERVED, "W-03")
    except OSError as exc:
        r6p(f"W-03 mkdirat failed: {_text(exc)}")
    run.wrote("W-03", False)
    run.p_fd, run.i_p = open_checked(run.a_fd, PRESERVED, "P")
    try:
        _mkdirat(run.a_fd, SWAP, "W-04")
    except OSError as exc:
        r6p(f"W-04 mkdirat failed: {_text(exc)}")
    run.wrote("W-04", False)
    run.w_fd, run.i_w = open_checked(run.a_fd, SWAP, "W")
    try:
        _fsync(run.a_fd, "A")
    except OSError as exc:
        r6p(f"W-04 fsync(A) failed: {_text(exc)}")
    run.wrote("W-04", True)
    # R6C-Ω (DG-1): no write between the W-04 fsync and the end of this round
    try:
        before = _fstat(run.a_fd, "A")
        if _ident(before) != run.i_a:
            r6p("R6C-Ω: A is not at its recorded identity")
        listing = _listing(run.a_fd, "A")
    except (FsprimError, OSError) as exc:
        r6p(f"R6C-Ω: the listing of A is incomplete: {_text(exc)}")
    got = _entries(listing)
    want = {PRESERVED: ("directory",) + (got.get(PRESERVED, (None, 0))[1],) + run.i_p,
            SWAP: ("directory",) + (got.get(SWAP, (None, 0))[1],) + run.i_w}
    if got != want:
        r6p(f"R6C-Ω: A holds {sorted(got)} with identities other than recorded, not exactly preserved and swap")
    if listing.folder[:2] != run.i_a:
        r6p("R6C-Ω: the listed A is not at its recorded identity")
    run.i_a_nlink = listing.folder[2]
    run.note("I_A_nlink_after_W04", str(run.i_a_nlink))


def _r6n(run: _Run, exc: OSError) -> None:
    """A W-02 ``mkdirat`` error: R6-N only if all six checks pass in one round (SM 3.6)."""
    run.note("W-02", f"mkdirat returned {_errname(exc)}")
    checks = []
    c2 = _Check("R6N-2 RS identity")
    try:
        st = _fstat(run.rs_fd, "RS")
        at = _lstat_at(run.m_fd, RS, "M/restores")
        if _ident(st) != run.i_rs or not stat.S_ISDIR(at.st_mode) or _ident(at) != run.i_rs:
            c2.fail("RS is no longer the baseline folder")
    except OSError as e:
        c2.incomplete(_text(e))
    checks.append(c2)
    c3 = _Check("R6N-3/4/5 RS listing")
    try:
        listing = _listing(run.rs_fd, "RS")
        if listing.folder[:2] != run.i_rs:
            c3.fail("the listed RS is not the baseline folder")
        if _entries(listing) != run.l1["RS"]:
            c3.fail("RS differs from L1")
        names = listing.entries
    except (FsprimError, OSError) as e:
        c3.incomplete(_text(e))
        names = {}
    checks.append(c3)
    c6 = _Check("R6N-6 rid absent")
    try:
        _lstat_at(run.rs_fd, run.rid, "rid")
        c6.fail("an entry exists at rid")
    except FileNotFoundError:
        if run.rid in names:
            c6.fail("rid is listed")
    except OSError as e:
        c6.incomplete(_text(e))
    checks.append(c6)
    run.note("R6N-Ω", _summary(checks))
    if all(c.status == "pass" for c in checks):
        run.attempt_dir = "not-created"
        run.stop(Result.FAILED, f"mkdirat returned {_errname(exc)}; R6N-Ω observed no attempt folder at rid and RS "
                                "unchanged from the baseline; no record was written", failed_kind="b",
                 confirmed=run.restores_dir in ("pre-existing", "created-durable"), record_observed="none",
                 record_durable="n/a", predicted="unchanged (P12 or P24)")
    run.attempt_dir = "unknown"
    g6 = any(c.name.startswith("R6N-6") and c.status == "fail" for c in checks)
    run.stop(Result.UNCERTAIN, f"attempt folder state unknown after mkdirat returned {_errname(exc)} "
                               f"({_summary(checks)})", confirmed=False, record_observed="none", record_durable="n/a",
             predicted="G6" if g6 else "unknown")


_Run.r6pd_prediction = "G6"


def _observe_attempt(run: _Run) -> None:
    try:
        st = _lstat_at(run.rs_fd, run.rid, "rid")
    except FileNotFoundError:
        run.note("attempt folder", "absent")
        run.r6pd_prediction = "unchanged"
        return
    except OSError as exc:
        run.note("attempt folder", f"incomplete: {_text(exc)}")
        run.r6pd_prediction = "unknown"
        return
    if not stat.S_ISDIR(st.st_mode):
        run.note("attempt folder", "not a folder")
        return
    try:
        fd = run.keep(_openat(run.rs_fd, run.rid, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC, 0, "A"))
        names = sorted(_listing(fd, "A").entries)
        run.note("attempt folder", f"a folder holding {names}")
    except (FsprimError, OSError) as exc:
        run.note("attempt folder", f"a folder whose listing is incomplete: {_text(exc)}")
        run.r6pd_prediction = "unknown"


def _write_record(run: _Run, rec: RestoreRecord) -> None:
    """W-05: ``restore.json`` = preparing (SM 3.6, cases R6-O, R6-W, R6-D, R6-D′, R6-R)."""
    data = rr.dump_restore_record(rec).encode("utf-8")
    a_fd = run.a_fd

    def observe() -> None:
        listing = _list(a_fd, "A")
        run.note("A after W-05", listing if isinstance(listing, str) else f"{sorted(listing)}")
        got = _complete(run, a_fd, RECORD, "restore.json")
        run.note("restore.json after W-05", got if isinstance(got, str) else f"{len(got.data)} bytes, sha256 "
                                                                             f"{_sha(got.data)}")

    run.note("W-05", "attempted")
    try:
        fd = _openat(a_fd, RECORD, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _CLOEXEC, 0o644,
                     "W-05 restore.json")
    except OSError as exc:
        run.note("W-05 openat", f"failed: {_text(exc)}")
        observe()
        run.stop(Result.UNCERTAIN, f"the record file could not be created ({_text(exc)}); nothing else was written; "
                                   "nothing was removed", confirmed=False, record_observed="unknown",
                 record_durable="unknown", predicted="G6")
    durable_error = None
    try:
        try:
            ident = _ident(_fstat(fd, "W-05 restore.json"))
        except OSError as exc:
            run.note("W-05 fstat", f"failed: {_text(exc)}")
            observe()
            run.stop(Result.UNCERTAIN, f"record file created; identity not established ({_text(exc)}); nothing was "
                                       "removed", confirmed=False, record_observed="unknown",
                     record_durable="unknown", predicted="G6")
        try:
            _write(fd, data, "W-05 restore.json")
        except (WriteIncomplete, OSError) as exc:
            run.note("W-05 write", f"failed: {_text(exc)}")
            observe()
            run.stop(Result.UNCERTAIN, f"record partly written ({_text(exc)}); nothing was removed", confirmed=False,
                     record_observed="unknown", record_durable="unknown", predicted="G6 or P18")
        try:
            _fsync(fd, "W-05 restore.json")
            _fsync(a_fd, "A")
        except OSError as exc:
            durable_error = exc
    finally:
        try:
            _close(fd, "W-05 restore.json")
        except OSError as exc:
            run.note("W-05 close", f"failed: {_text(exc)}")
            durable_error = durable_error or exc
    got = _complete(run, a_fd, RECORD, "restore.json")
    exact = not isinstance(got, str) and got.data == data and (got.dev, got.ino) == ident
    if durable_error is not None:  # R6-D or R6-D′ (D-STOP)
        run.note("W-05 durability", f"failed: {_text(durable_error)}")
        if exact:
            run.record, run.rbytes, run.rident = rec, data, ident
            run.wrote("W-05", False)
            run.stop(Result.FAILED, f"no exchange; the record is observable but not durable ({_text(durable_error)})",
                     failed_kind="c", confirmed=False, record_observed="preparing", record_durable="no",
                     predicted="P18")
        run.stop(Result.UNCERTAIN, f"the record durability failed ({_text(durable_error)}) and it reads back "
                                   f"{got if isinstance(got, str) else 'as other bytes or another file'}",
                 confirmed=False, record_observed="unknown", record_durable="unknown", predicted="G6 or P18")
    if not exact:  # R6-R
        run.stop(Result.UNCERTAIN, "the record was written but not verified (its read-back is "
                                   f"{got if isinstance(got, str) else 'other bytes or another file'})",
                 confirmed=False, record_observed="unknown", record_durable="yes", predicted="P18 or G6")
    run.record, run.rbytes, run.rident = rec, data, ident
    run.wrote("W-05", True)


# --------------------------------------------------------------------------- R7, R8


def _stage_copy(run: _Run, which: str) -> None:
    """W-06 (``P/f`` = U, R7b) or W-08 (``W/f`` = O, R8b). A failure goes to FE-Ω (SM 3.7, 3.8)."""
    dir_fd, data, op = (run.p_fd, run.U, "W-06") if which == "P" else (run.w_fd, run.O, "W-08")
    what = f"{which}/f"
    error = None
    try:
        fd = _openat(dir_fd, run.f, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _CLOEXEC, 0o644,
                     f"{op} {what}")
    except OSError as exc:
        error = f"{op} openat failed: {_text(exc)}"
    else:
        try:
            ident = _ident(_fstat(fd, f"{op} {what}"))
            if which == "P":
                run.i_p_candidate = ident
            else:
                run.i_t_candidate = ident
            _write(fd, data, f"{op} {what}")
            _fsync(fd, f"{op} {what}")
            _fsync(dir_fd, which)
        except (WriteIncomplete, OSError) as exc:
            error = f"{op} failed: {_text(exc)}"
        finally:
            try:
                _close(fd, f"{op} {what}")
            except OSError as exc:
                error = error or f"{op} close failed: {_text(exc)}"
    if error is None:
        got = _complete(run, dir_fd, run.f, what)
        if isinstance(got, str):
            error = f"the read-back of {what} is incomplete: {got}"
        elif got.data != data:
            error = (f"the read-back of {what} holds {len(got.data)} bytes that are not exactly the "
                     f"{len(data)} bytes written")
        elif (got.dev, got.ino) != ident:
            error = f"the read-back of {what} is another file than the one written"
    if error is not None:
        run.note(op, error)
        _fe_path(run, error)
    run.wrote(op, True)
    if which == "P":
        run.i_pf = ident
    else:
        run.i_t = ident


# --------------------------------------------------------------------------- the U-update (SM 0.5)


def _tmp_name() -> str:
    return f"{_TMP_PREFIX}{secrets.token_hex(6)}{_TMP_SUFFIX}"


def _u_update(run: _Run, new: RestoreRecord, op: str) -> _UFail | None:
    """u1 to u9, the run's own W-16/u10 where SM 0.5 allows it, and the Q-Ω re-read on failure.
    Returns None on success (the run's record state is updated) or the failure."""
    a_fd = run.a_fd
    b_old, id_old = run.rbytes, run.rident
    b_new = rr.dump_restore_record(new).encode("utf-8")
    tname = _tmp_name()
    id_t = None

    got = _complete(run, a_fd, RECORD, f"{op}.u1")
    if isinstance(got, str) or got.data != b_old or (got.dev, got.ino) != id_old:
        why = got if isinstance(got, str) else "it is not the record this run last wrote"
        return _UFail("u1", f"u1: the record read {why}")

    def fail(step: str, exc: BaseException) -> _UFail:
        return _q_omega(run, _UFail(step, f"{step}: {_text(exc)}"), op, tname, id_t, b_old, id_old, b_new)

    try:
        fd_t = _openat(a_fd, tname, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY | _CLOEXEC, 0o644,
                       f"{op}.u2 T(x)")
    except OSError as exc:
        return fail("u2", exc)
    step = "u2"
    try:
        id_t = _ident(_fstat(fd_t, f"{op}.u2 T(x)"))
        step = "u3"
        _write(fd_t, b_new, f"{op}.u3 T(x)")
        step = "u4"
        _fsync(fd_t, f"{op}.u4 T(x)")
    except (WriteIncomplete, OSError) as exc:
        failure = exc
    else:
        failure = None
    try:
        _close(fd_t, f"{op} T(x)")
    except OSError as exc:  # a close failure is treated as a u4 failure: no W-16, T(x) kept (fail closed)
        if failure is None:
            step, failure = "u4", exc
    if failure is not None:
        return fail(step, failure)
    try:
        _fsync(a_fd, f"{op}.u5 A")
    except OSError as exc:
        return fail("u5", exc)
    try:
        cur, tmp = _lstat_at(a_fd, RECORD, f"{op}.u6"), _lstat_at(a_fd, tname, f"{op}.u6")
        if _ident(cur) != id_old or _ident(tmp) != id_t:
            raise OSError(_errno.ESTALE, "the record or its temporary file is not the file this run holds")
    except OSError as exc:
        return fail("u6", exc)
    try:
        _renameat(a_fd, tname, RECORD, f"{op}.u7")
    except OSError as exc:
        return fail("u7", exc)
    try:
        _fsync(a_fd, f"{op}.u8 A")
    except OSError as exc:
        return fail("u8", exc)
    got = _complete(run, a_fd, RECORD, f"{op}.u9")
    if isinstance(got, str) or got.data != b_new or (got.dev, got.ino) != id_t:
        why = got if isinstance(got, str) else "it is not the record just written"
        return _q_omega(run, _UFail("u9", f"u9: the read-back {why}"), op, tname, id_t, b_old, id_old, b_new)
    run.record, run.rbytes, run.rident = new, b_new, id_t
    return None


def _q_omega(run: _Run, fail: _UFail, op: str, tname: str, id_t, b_old: bytes, id_old, b_new: bytes) -> _UFail:
    """W-16 where allowed, then the one Q-Ω round (SM 0.5); nothing else is written."""
    a_fd = run.a_fd
    if fail.step in ("u3", "u6", "u7") and not run.w12_failed and id_t is not None:
        try:
            st = _lstat_at(a_fd, tname, f"{op}.W-16 lstat")
        except OSError:
            st = None
        if st is not None and _ident(st) == id_t:
            try:
                _unlinkat(a_fd, tname, f"{op}.W-16")
            except OSError as exc:
                fail.w16 = "unlink-failed"
                run.note(f"{op} W-16", f"unlinkat failed: {_text(exc)}")
            else:
                try:
                    _fsync(a_fd, f"{op}.u10 A")
                    fail.w16 = "ok"
                except OSError as exc:
                    fail.w16 = "u10-failed"
                    run.note(f"{op} u10", f"failed: {_text(exc)}")
    listing = _list(a_fd, "Q-Ω A")
    got = _complete(run, a_fd, RECORD, "Q-Ω restore.json")
    if isinstance(listing, str) or isinstance(got, str):
        fail.q, fail.q_detail = "unknown", listing if isinstance(listing, str) else got
    else:
        names = set(listing)
        tmp_present = tname in names
        others = names - {RECORD, PRESERVED, SWAP, tname}
        ident = (got.dev, got.ino)
        if others:
            fail.q, fail.q_detail = "unknown", f"unexpected entries {sorted(others)} in A"
        elif id_t is not None and got.data == b_new and ident == id_t and not tmp_present:
            fail.q = "new"
        elif got.data == b_old and ident == id_old:
            fail.q = "old+tmp" if tmp_present else "old"
        else:
            fail.q, fail.q_detail = "unknown", "the record holds other bytes or is another file"
    if fail.q == "new":
        fail.record_durable = "yes" if fail.step == "u9" else "no"
        fail.q_record = _parse_quiet(b_new)
        run.record, run.rbytes, run.rident = fail.q_record, b_new, id_t
    elif fail.q == "old":
        fail.record_durable = "tmp-removal-not-durable" if fail.w16 == "u10-failed" else "yes"
        fail.q_record = run.record
    run.note(f"{op} failed", f"{fail.error}; W-16 {fail.w16}; Q-Ω {fail.q}" + (f" ({fail.q_detail})" if fail.q_detail
                                                                              else ""))
    return fail


def _parse_quiet(data: bytes) -> RestoreRecord | None:
    try:
        return rr.parse_restore_record(data.decode("utf-8"))
    except (UnicodeDecodeError, RestoreRecordError):
        return None


def _update(run: _Run, op: str, new: RestoreRecord) -> None:
    """W-07, W-09 or W-10 (before the exchange). A failure ends the run by SM 3.7, 3.8, 3.10."""
    try:
        new = _validated(new.model_dump(mode="json"))
    except RestoreRecordError as exc:  # never expected: the run stops before the update, with nothing written
        run.stop(Result.UNCERTAIN, f"{op}: the next record is refused by the contract ({exc}); nothing was written",
                 confirmed=False, predicted="unknown")
    fail = _u_update(run, new, op)
    if fail is None:
        run.wrote(op, True)
        return
    run.note("stage", run.stage)
    if fail.step == "u1":
        run.stop(Result.UNCERTAIN, f"{op}: {fail.error}; nothing was written", confirmed=False,
                 record_observed="unknown", record_durable="unknown", predicted="unknown")
    if fail.q in ("new", "old"):
        if fail.q == "new":
            run.wrote(op, fail.record_durable == "yes")
        status = (fail.q_record.status.value if fail.q_record else "unknown")
        predicted = {"preparing": "P18", "preserved": "P19", "exchanging": "P19"}.get(status, "unknown")
        run.stop(Result.FAILED, f"no exchange was made; updating the record ({op}) failed ({fail.error}); the record "
                                f"reads back as {status}" + ("; observable, not durable" if fail.record_durable != "yes"
                                                             else ""),
                 failed_kind="c", confirmed=fail.record_durable == "yes", record_observed=status,
                 record_durable=fail.record_durable, predicted=predicted, record=fail.q_record)
    run.stop(Result.UNCERTAIN, f"updating the record ({op}) failed ({fail.error}); the record state is "
                               f"{'the old record plus its temporary file' if fail.q == 'old+tmp' else 'unknown'}",
             confirmed=False, record_observed="unknown", record_durable="unknown",
             predicted="G6" if fail.q == "old+tmp" else "unknown")


# --------------------------------------------------------------------------- FE-Ω (SM 0.6)


def _fe(run: _Run, intended: dict) -> dict:
    """One FE-Ω round, after the failing operation returned: every item pass, fail or incomplete."""
    f = run.f
    fe = {k: _Check(k) for k in ("FE-2", "FE-3", "FE-4", "FE-5", "FE-6", "FE-7")}
    rec = run.record
    if run.stage == "ST-PRE":
        if rec.exchange_attempted or rec.status not in (RestoreStatus.PREPARING, RestoreStatus.PRESERVED):
            fe["FE-2"].fail(f"the last record is {rec.status.value}")
    elif run.stage.startswith("ST-ERR"):
        if rec.status is not RestoreStatus.EXCHANGING or not rec.exchange_attempted:
            fe["FE-2"].fail(f"the last record is {rec.status.value}")
    # FE-3: the G6 pre-check
    c = fe["FE-3"]
    _chain(run, c, include_mount=True)
    rs = _list(run.rs_fd, "FE RS")
    if isinstance(rs, str):
        c.incomplete(f"RS: {rs}")
    elif rs != _e_rs(run):
        c.fail("RS differs from its expected listing")
    a = _list(run.a_fd, "FE A")
    if isinstance(a, str):
        c.incomplete(f"A: {a}")
    else:
        if set(a) != {RECORD, PRESERVED, SWAP}:
            c.fail(f"A holds {sorted(a)}")
        elif a[PRESERVED][2:] != run.i_p or a[SWAP][2:] != run.i_w:
            c.fail("preserved or swap is another folder")
    contents = {}
    for which, fd, ident in (("P", run.p_fd, run.i_p), ("W", run.w_fd, run.i_w)):
        got = _list(fd, f"FE {which}")
        if isinstance(got, str):
            c.incomplete(f"{which}: {got}")
            contents[which] = None
            continue
        contents[which] = got
        if not set(got) <= {f}:
            c.fail(f"{which} holds {sorted(got)}")
    _baseline_attempts(run, c)
    # FE-4: the record as last written
    got = _complete(run, run.a_fd, RECORD, "FE restore.json")
    if isinstance(got, str):
        fe["FE-4"].incomplete(got)
    elif got.data != run.rbytes or (got.dev, got.ino) != run.rident:
        fe["FE-4"].fail("restore.json is not the record this run last wrote")
    # FE-5: S
    got = _complete(run, run.sets_fd, f, "FE S")
    run.fe_s = got
    if isinstance(got, str):
        fe["FE-5"].incomplete(got)
    elif (got.dev, got.ino) != run.i_s or got.data != run.U:
        fe["FE-5"].fail("the set file is not the upgraded file at its recorded identity")
    # FE-6: P and W hold nothing, or only this run's own file
    c6 = fe["FE-6"]
    for which, cand in (("P", run.i_p_candidate), ("W", run.i_t_candidate)):
        got = contents.get(which)
        if got is None:
            c6.incomplete(f"{which} could not be listed")
            continue
        if which == "W" and not run.r8_started and got:
            c6.fail("W is not empty although R8 never started")
        if f in got:
            ident = got[f][2:]
            if cand is None or ident != cand:
                c6.fail(f"{which}/f is not the file this run created")
            if ident == run.i_s:
                c6.fail(f"{which}/f is the set file's original inode")
    run.fe_w = _complete(run, run.w_fd, f, "FE W/f") if contents.get("W") and f in contents["W"] else None
    # FE-7: the intended record under the frozen contract
    for key, data in list(intended.items()):
        try:
            intended[key] = _validated(data)
        except (RestoreRecordError, ValueError) as exc:
            intended[key] = None
            fe["FE-7"].fail(f"the {key} record is refused by the contract: {exc}")
    run.note("FE-Ω", _summary(fe.values()))
    return fe


def _chain(run: _Run, c: _Check, include_mount: bool) -> None:
    """The folder chain: restores, A, P, W at their recorded identities, on the same mount."""
    links = ((run.m_fd, RS, run.rs_fd, run.i_rs, "restores"), (run.rs_fd, run.rid, run.a_fd, run.i_a, "A"),
             (run.a_fd, PRESERVED, run.p_fd, run.i_p, "P"), (run.a_fd, SWAP, run.w_fd, run.i_w, "W"))
    for parent, name, fd, ident, what in links:
        try:
            at, st = _lstat_at(parent, name, f"chain {what}"), _fstat(fd, f"chain {what}")
        except OSError as exc:
            c.incomplete(f"{what}: {_text(exc)}")
            continue
        if not stat.S_ISDIR(at.st_mode) or _ident(at) != ident or _ident(st) != ident:
            c.fail(f"{what} is not the folder recorded")
        if include_mount:
            try:
                if _mount_id(fd, what) != run.fsid.mnt_id or st.st_dev != run.fsid.st_dev:
                    c.fail(f"{what} is on another mount")
            except (FsprimError, OSError) as exc:
                c.incomplete(f"the mount of {what}: {_text(exc)}")


def _baseline_attempts(run: _Run, c: _Check) -> None:
    """Every attempt folder of the baseline is unchanged (its listing, its P and W)."""
    for comp, want in run.l1.items():
        if not isinstance(comp, tuple):
            continue
        label, name = comp
        path = run.base / M / RS / name if label == "A" else run.base / M / RS / name / (
            PRESERVED if label == "P" else SWAP)
        fd = up._CTX.get().dirs.get(path) if up._CTX.get() is not None else None
        if fd is None:
            c.incomplete(f"the baseline attempt {name} is not held")
            continue
        got = _list(fd, f"baseline {label}[{name}]")
        if isinstance(got, str):
            c.incomplete(f"baseline {label}[{name}]: {got}")
        elif got != want:
            c.fail(f"the baseline attempt {name} changed ({label})")


def _e_rs(run: _Run) -> dict:
    out = dict(run.l1["RS"])
    out[run.rid] = ("directory", run.i_a_nlink) + run.i_a
    return out


def _e_m(run: _Run) -> dict | None:
    out = dict(run.l1["M"])
    kind, nlink, dev, ino = out[RS]
    if nlink < 2:  # e.g. an ext4 dir_nlink overflow: the expected value cannot be derived (fail closed)
        return None
    out[RS] = (kind, nlink + 1, dev, ino)
    return out


def _fe_path(run: _Run, error: str) -> None:
    """A failure before the exchange (stage ST-PRE): FE-Ω, then W-14, W-15 or nothing (SM 0.6)."""
    failed = _final_data(run, RestoreStatus.FAILED, f"the restore stopped before the exchange: {error}; the set file "
                                                    "was verified unchanged (the upgraded bytes)")
    fe = _fe(run, {"failed": failed, "uncertain": _final_data(run, RestoreStatus.UNCERTAIN, error)})
    _fe_decide(run, fe, error, allow_failed=True)


def _final_data(run: _Run, status: RestoreStatus, error: str) -> dict:
    data = run.record.model_dump(mode="json")
    data.update(status=status.value, completed_at=run.now or _now(), error=error)
    return data


def _fe_decide(run: _Run, fe: dict, error: str, allow_failed: bool) -> None:
    """The FE decision table for ST-PRE (SM 0.6), first matching row wins."""
    listed = _summary(fe.values())
    if fe["FE-3"].status == "fail":
        run.stop(Result.UNCERTAIN, f"{error}; G6 condition observed: {fe['FE-3'].detail}; no record was written",
                 confirmed=False, predicted="G6")
    if fe["FE-3"].status == "incomplete" or fe["FE-4"].status == "incomplete":
        run.stop(Result.UNCERTAIN, f"{error}; G6 not excluded; check incomplete ({listed}); no record was written",
                 confirmed=False, predicted="unknown")
    if fe["FE-4"].status == "fail":
        run.stop(Result.UNCERTAIN, f"{error}; the record is not this run's as last written; no record was written",
                 confirmed=False, predicted="unknown")
    others = [fe[k] for k in ("FE-2", "FE-5", "FE-6", "FE-7")]
    if allow_failed and all(c.status == "pass" for c in others):
        _failure_write(run, "W-14", RestoreStatus.FAILED, error, listed)
    _failure_write(run, "W-15", RestoreStatus.UNCERTAIN, f"{error}; failure checks: {listed}", listed)


def _failure_write(run: _Run, op: str, status: RestoreStatus, error: str, listed: str) -> None:
    """W-14 (failed) or W-15 (uncertain); the run ends here in every case."""
    if status is RestoreStatus.FAILED and run.stage == "ST-PRE":
        text = (f"the restore stopped before the exchange: {error}; the set file was verified unchanged (the "
                "upgraded bytes)")
    elif status is RestoreStatus.FAILED:  # E-5: the exchange call returned a class A error
        text = f"{error}; the set file was verified unchanged (the upgraded bytes) after the error"
    else:
        text = error
    try:
        new = _validated(_final_data(run, status, text))
    except (RestoreRecordError, ValueError) as exc:  # FE-7 refused it: nothing is written
        run.stop(Result.UNCERTAIN, f"{error}; the {status.value} record is refused by the contract ({exc}); no "
                                   "record was written", confirmed=False, predicted="unknown")
    fail = _u_update(run, new, op)
    pre = run.stage == "ST-PRE"
    if fail is None:
        run.wrote(op, True)
        if status is RestoreStatus.FAILED:
            run.stop(Result.FAILED, f"{error}; recorded as failed", failed_kind="a", confirmed=run.exchange_durable
                     in ("n/a",), predicted="P24")
        run.stop(Result.UNCERTAIN, f"{error}; recorded as uncertain", confirmed=pre, predicted="P21")
    if fail.step == "u1":
        run.stop(Result.UNCERTAIN, f"{error}; writing the {status.value} record failed ({fail.error}); nothing was "
                                   "written", confirmed=False, record_observed="unknown", record_durable="unknown",
                 predicted="unknown")
    q_status = fail.q_record.status.value if fail.q_record is not None else "unknown"
    if fail.q == "new":
        run.wrote(op, fail.record_durable == "yes")
        if status is RestoreStatus.FAILED:
            run.stop(Result.FAILED, f"{error}; recorded as failed, but writing it reported an error ({fail.error})",
                     failed_kind="a", confirmed=fail.record_durable == "yes", record_observed="failed",
                     record_durable=fail.record_durable, predicted="P24")
        run.stop(Result.UNCERTAIN, f"{error}; recorded as uncertain, but writing it reported an error "
                                   f"({fail.error})", confirmed=pre and fail.record_durable == "yes",
                 record_observed="uncertain", record_durable=fail.record_durable, predicted="P21")
    if fail.q == "old" and status is RestoreStatus.FAILED and pre:
        predicted = {"preparing": "P18", "preserved": "P19"}.get(q_status, "unknown")
        run.stop(Result.FAILED, f"{error}; no exchange was made; writing the failed record failed ({fail.error}); "
                                f"the record reads back as {q_status}", failed_kind="c",
                 confirmed=fail.record_durable == "yes", record_observed=q_status,
                 record_durable=fail.record_durable, predicted=predicted, record=fail.q_record)
    if fail.q == "old":
        predicted = {"preparing": "P18", "preserved": "P19", "exchanging": "P19" if pre else "P19 or P20"}.get(
            q_status, "unknown")
        run.stop(Result.UNCERTAIN, f"{error}; the outcome could not be recorded ({fail.error}); the record reads "
                                   f"back as {q_status}", confirmed=False, record_observed=q_status,
                 record_durable=fail.record_durable, predicted=predicted, record=fail.q_record)
    run.stop(Result.UNCERTAIN, f"{error}; writing the {status.value} record failed ({fail.error}); record state "
                               f"{'old plus its temporary file' if fail.q == 'old+tmp' else 'unknown'}",
             confirmed=False, record_observed="unknown", record_durable="unknown",
             predicted="G6" if fail.q == "old+tmp" else "unknown")


# --------------------------------------------------------------------------- R9


def _r9(run: _Run) -> None:
    """R9-Ω (SM 3.9): every check, grouped K-G, K-F and K-L, then the decision."""
    f = run.f
    kg, kf, kl = _Check("K-G", "G"), _Check("K-F", "F"), _Check("K-L", "L")
    _chain(run, kg, include_mount=True)
    rs = _list(run.rs_fd, "R9 RS")
    if isinstance(rs, str):
        kg.incomplete(f"RS: {rs}")
    elif rs != _e_rs(run):
        kg.fail("RS differs from its expected listing")
    a = _list(run.a_fd, "R9 A")
    if isinstance(a, str):
        kg.incomplete(f"A: {a}")
    elif set(a) != {RECORD, PRESERVED, SWAP} or a[PRESERVED][2:] != run.i_p or a[SWAP][2:] != run.i_w:
        kg.fail(f"A holds {sorted(a)}, not exactly the record, preserved and swap")
    for which, fd in (("P", run.p_fd), ("W", run.w_fd)):
        got = _list(fd, f"R9 {which}")
        if isinstance(got, str):
            kg.incomplete(f"{which}: {got}")
        elif set(got) != {f}:
            kg.fail(f"{which} holds {sorted(got)}")
    got = _complete(run, run.a_fd, RECORD, "R9 restore.json")
    if isinstance(got, str):
        kg.incomplete(f"restore.json: {got}")
    elif got.data != run.rbytes or (got.dev, got.ino) != run.rident:
        kg.fail("restore.json is not the record this run last wrote")
    _baseline_attempts(run, kg)
    # K-F
    for what, dir_fd, name, ident, data in (("S", run.sets_fd, f, run.i_s, run.U), ("B", run.m_fd, f, run.i_b, run.O),
                                            ("U-rec", run.r_fd, f"{run.urec.record_id}.json", run.i_urec,
                                             run.urec_bytes), ("P/f", run.p_fd, f, run.i_pf, run.U),
                                            ("W/f", run.w_fd, f, run.i_t, run.O)):
        got = _complete(run, dir_fd, name, f"R9 {what}")
        if isinstance(got, str):
            kf.incomplete(f"{what}: {got}")
        elif (got.dev, got.ino) != ident or got.data != data:
            kf.fail(f"{what} is not the recorded file with the expected bytes")
    _sets_and_m_chain(run, kf)
    # K-L
    for comp, fd, want in (("sets", run.sets_fd, run.l1["sets"]), ("M", run.m_fd, _e_m(run)),
                           ("R", run.r_fd, run.l1.get("R"))):
        got = _list(fd, f"R9 {comp}")
        if isinstance(got, str):
            kl.incomplete(f"{comp}: {got}")
        elif want is None or got != want:
            kl.fail(f"the {comp} listing differs from its expected listing")
    run.note("R9-Ω", _summary((kg, kf, kl)))
    if kg.status == "incomplete":
        run.stop(Result.UNCERTAIN, f"G6 conditions not excluded; check incomplete ({kg.detail}); no record was "
                                   "written", confirmed=False, predicted="unknown")
    if kg.status == "fail":
        run.stop(Result.UNCERTAIN, f"G6 condition observed: {kg.detail}; no record was written", confirmed=False,
                 predicted="G6")
    if "incomplete" in (kf.status, kl.status):
        error = f"a check before the exchange was incomplete ({_summary((kf, kl))})"
        fe = _fe(run, {"uncertain": _final_data(run, RestoreStatus.UNCERTAIN, error)})
        _fe_decide(run, fe, error, allow_failed=False)
    if "fail" in (kf.status, kl.status):
        _fe_path(run, f"a check before the exchange failed ({_summary((kf, kl))})")


def _sets_and_m_chain(run: _Run, c: _Check) -> None:
    """The sets folder identity and the M and R chain (K-F; PX-01, PX-02)."""
    try:
        if _ident(_stat_path(run.base, "sets path")) != run.i_sets or _ident(_fstat(run.sets_fd, "sets")) != run.i_sets:
            c.fail("the sets folder is not the folder locked")
        at = _lstat_at(run.sets_fd, M, "sets/M")
        if not stat.S_ISDIR(at.st_mode) or _ident(at) != run.i_m:
            c.fail("M is not the folder locked")
        if run.r_fd is not None:
            at = _lstat_at(run.m_fd, R, "M/records")
            if not stat.S_ISDIR(at.st_mode) or _ident(at) != _ident(_fstat(run.r_fd, "R")):
                c.fail("the records folder is not the one listed")
    except OSError as exc:
        c.incomplete(f"the folder chain: {_text(exc)}")


# --------------------------------------------------------------------------- R10 to R12


def _exchange_and_finish(run: _Run) -> RestoreOutcome:
    f = run.f
    try:
        _exchange(run.w_fd, run.sets_fd, f)
    except (OSError, FsprimError) as exc:
        err = getattr(exc, "errno", None)
        run.exchange_called = f"error:{_errname(exc)}"
        run.stage = f"ST-ERR({_errname(exc)})"
        run.note("W-11", f"renameat2 returned {_errname(exc)}")
        _exchange_error(run, err)
    run.exchange_called = "ok"
    run.stage = "ST-OK"
    run.wrote("W-11", False)
    # W-12 (OD-29): both calls, once each, in order; neither retried
    results = {}
    for label, fd in (("sets", run.sets_fd), ("W", run.w_fd)):
        try:
            _fsync(fd, f"W-12 {label}")
            results[label] = "success"
        except OSError as exc:
            results[label] = f"error:{_errname(exc)}"
        run.note(f"W-12 fsync({label}_fd)", results[label])
    run.w12_failed = any(v != "success" for v in results.values())
    run.exchange_durable = "no" if run.w12_failed else "yes"
    if not run.w12_failed:
        run.wrote("W-11", True)
    w12_text = (f"W-12: fsync(sets_fd) {results['sets']}; fsync(W_fd) {results['W']}")
    # R11: PX-Ω, every check
    px, observation = _px(run)
    g_bad = [c for c in px if c.kind == "G" and c.status != "pass"]
    other_bad = [c for c in px if c.kind != "G" and c.status != "pass"]
    listed = _summary(px)
    run.note("PX-Ω", listed)
    exchanged = all(c.status == "pass" for c in px if c.name in ("PX-10", "PX-15"))
    if g_bad:
        run.stop(Result.UNCERTAIN, f"the exchange returned 0 ({w12_text}); a G6-type post-exchange check did not pass "
                                   f"({_summary(g_bad)}); no record was written"
                                   + ("; exchanged per PX-10/PX-15" if exchanged else ""),
                 confirmed=False, predicted="G6" if any(c.status == "fail" for c in g_bad) else "P20 or P21")
    if not other_bad and not run.w12_failed:
        try:
            new = _final(run, RestoreStatus.RESTORED, None, observation)
        except (RestoreRecordError, ValueError) as exc:
            other_bad = [_named_fail("FE-7", f"the restored record is refused by the contract: {exc}")]
    if other_bad or run.w12_failed:
        reasons = []
        if run.w12_failed:
            reasons.append(f"durability of the exchange not confirmed ({w12_text})")
        if other_bad:
            reasons.append(f"post-exchange checks did not pass: {_summary(other_bad)}")
        if observation is not None and observation.kind == "regular" and (observation.sha256, observation.size) != (
                run.urec.upgraded_sha256, run.urec.upgraded_size_bytes):
            reasons.append(_displaced_facts(run, observation))
        error = "exchanged; " + "; ".join(reasons)
        try:
            new = _final(run, RestoreStatus.UNCERTAIN, error, observation)
        except (RestoreRecordError, ValueError):
            new = _final(run, RestoreStatus.UNCERTAIN, error, None)
        intended = "uncertain"
    else:
        error, intended = None, "restored"
    fail = _u_update(run, new, "W-13")
    exch_yes = run.exchange_durable == "yes"
    if fail is None:
        run.wrote("W-13", True)
        if intended == "restored":
            return run.outcome(Result.RESTORED, f"restored to the original format 1 bytes; every post-exchange check "
                                                f"held ({w12_text}); the displaced file is kept at {RS}/{run.rid}/"
                                                f"{SWAP}/{f}", confirmed=exch_yes, predicted="P20R")
        return run.outcome(Result.UNCERTAIN, f"{error}; recorded as uncertain", confirmed=exch_yes, predicted="P21")
    if fail.step == "u1":
        return run.outcome(Result.UNCERTAIN, f"exchanged ({w12_text}); the record before the final update is not "
                                             f"the one this run wrote ({fail.error}); nothing was written",
                           confirmed=False, record_observed="unknown", record_durable="unknown", predicted="unknown")
    if fail.q == "new":
        run.wrote("W-13", fail.record_durable == "yes")
        durable = fail.record_durable == "yes"
        if intended == "restored":
            text = "" if durable else "; restored record observable, not durable"
            return run.outcome(Result.RESTORED, f"restored ({w12_text}); writing the final record reported an error "
                                                f"({fail.error}){text}", confirmed=durable and exch_yes,
                               record_observed="restored", record_durable=fail.record_durable, predicted="P20R")
        return run.outcome(Result.UNCERTAIN, f"{error}; writing the uncertain record reported an error "
                                             f"({fail.error})" + ("" if durable else "; observable, not durable"),
                           confirmed=durable and exch_yes, record_observed="uncertain",
                           record_durable=fail.record_durable, predicted="P21")
    if fail.q == "old":
        return run.outcome(Result.UNCERTAIN, f"exchanged; record not finalised ({w12_text}; {fail.error})",
                           confirmed=fail.record_durable == "yes" and exch_yes, record_observed="exchanging",
                           record_durable=fail.record_durable, predicted="P20", record=fail.q_record)
    return run.outcome(Result.UNCERTAIN, f"exchanged ({w12_text}); writing the final record failed ({fail.error}); "
                                         "record state " + ("old plus its temporary file" if fail.q == "old+tmp" else
                                                            "unknown"),
                       confirmed=False, record_observed="unknown", record_durable="unknown",
                       predicted="G6" if fail.q == "old+tmp" else "unknown")


def _named_fail(name: str, detail: str) -> _Check:
    c = _Check(name, "F")
    c.fail(detail)
    return c


def _displaced_facts(run: _Run, obs: DisplacedObservation) -> str:
    return (f"The displaced file `{RS}/{run.rid}/{SWAP}/{run.f}` held bytes other than the upgraded bytes when the "
            f"restore observed it (`{obs.sha256} / {obs.size}`, {obs.kind}). Those bytes remain in that attempt folder "
            f"and are kept. The active set file `{run.f}` does not include those changes and was not modified because "
            "of them. Nothing has been merged or restored automatically.")


def _exchange_error(run: _Run, err) -> None:
    """ST-ERR(e): one FE-Ω round after the return, then the errno class rules E-1 to E-6 (SM 3.11)."""
    kind = "A" if err in CLASS_A else "C" if err in CLASS_C else "B"
    run.note("errno class", kind)
    run.exchange_durable = "unknown"
    name = _errno.errorcode.get(err, str(err)) if err else "no-errno"
    fe = _fe(run, {"failed": _final_data(run, RestoreStatus.FAILED, "x"),
                   "uncertain": _final_data(run, RestoreStatus.UNCERTAIN, "x")})
    pos = _Check("position")
    s = run.fe_s
    if isinstance(s, str) or s is None:
        pos.incomplete(f"S: {s}")
    elif (s.dev, s.ino) != run.i_s or s.data != run.U:
        pos.fail("S is not at I_s with U")
    w = run.fe_w
    if w is None or isinstance(w, str):
        pos.incomplete(f"W/f: {w if w is not None else 'absent'}")
    elif (w.dev, w.ino) != run.i_t or w.data != run.O:
        pos.fail("W/f is not at I_t with O")
    run.note("position evidence", str(pos))
    for label, got in (("S", s), ("W/f", w)):
        if got is not None and not isinstance(got, str):
            run.note(f"after the error, {label}", f"{(got.dev, got.ino)}, sha256 {_sha(got.data)} (uninterpreted)")
    listed = _summary(list(fe.values()) + [pos])
    if fe["FE-3"].status == "fail":  # E-1
        run.stop(Result.UNCERTAIN, f"the exchange returned {name}; G6 condition observed ({fe['FE-3'].detail}); no "
                                   "record was written", confirmed=False, predicted="G6")
    if "incomplete" in (fe["FE-3"].status, fe["FE-4"].status) or fe["FE-4"].status == "fail":  # E-2
        run.stop(Result.UNCERTAIN, f"the exchange returned {name}; G6 not excluded or the record is not this run's "
                                   f"({listed}); no record was written", confirmed=False, predicted="unknown")
    if kind == "C":  # E-3
        _st_err_uncertain(run, fe, f"exchange returned ENOENT; whether an exchange occurred is not established; "
                                   f"{listed}")
    if kind == "B":  # E-4
        text = ("interrupted exchange; outcome not established" if err == _errno.EINTR else
                f"exchange returned {name}; whether an exchange occurred is not established")
        _st_err_uncertain(run, fe, f"{text}; {listed}")
    if pos.status == "pass" and all(fe[k].status == "pass" for k in ("FE-6", "FE-7")):  # E-5
        run.exchange_durable = "n/a"
        run.note("inference", f"The exchange call returned {name}, which, for this call's arguments, the documented "
                              "kernel semantics return before the file system's rename operation (a conservative "
                              "classification, not runtime-verified for every file system). Both entries were "
                              "observed in their pre-exchange positions after the error. That no exchange occurred is "
                              "inferred from these two facts. An exchange followed by a reverse exchange by a "
                              "lock-ignoring program cannot be excluded.", "inferred")
        _failure_write(run, "W-14", RestoreStatus.FAILED, f"the exchange returned {name} and changed nothing "
                                                          "(inferred; see the outcome)", listed)
    _st_err_uncertain(run, fe, f"exchange returned {name}; the position evidence does not establish that nothing "
                               f"changed; {listed}")  # E-6


def _st_err_uncertain(run: _Run, fe: dict, error: str) -> None:
    if fe["FE-7"].status != "pass":
        run.stop(Result.UNCERTAIN, f"{error}; the uncertain record is refused by the contract; no record was written",
                 confirmed=False, predicted="unknown")
    _failure_write(run, "W-15", RestoreStatus.UNCERTAIN, error, "")


def _px(run: _Run):
    """PX-01 to PX-27 (SM 4), all run, in order, in one round; and the displaced observation."""
    f = run.f
    checks = []

    def check(name: str, kind: str) -> _Check:
        c = _Check(name, kind)
        checks.append(c)
        return c

    c = check("PX-01", "F")
    try:
        if _ident(_stat_path(run.base, "PX sets path")) != run.i_sets or _ident(_fstat(run.sets_fd, "PX sets")) != \
                run.i_sets:
            c.fail("the sets folder is not the folder locked")
    except OSError as exc:
        c.incomplete(_text(exc))
    c = check("PX-02", "F")
    try:
        at = _lstat_at(run.sets_fd, M, "PX M")
        if not stat.S_ISDIR(at.st_mode) or _ident(at) != run.i_m:
            c.fail("M is not the folder locked")
    except OSError as exc:
        c.incomplete(_text(exc))
    for name, parent, entry, ident, what in (("PX-03", run.m_fd, RS, run.i_rs, "restores"),
                                             ("PX-04", run.rs_fd, run.rid, run.i_a, "A"),
                                             ("PX-05", run.a_fd, PRESERVED, run.i_p, "preserved"),
                                             ("PX-06", run.a_fd, SWAP, run.i_w, "swap")):
        c = check(name, "G")
        try:
            at = _lstat_at(parent, entry, f"PX {what}")
            if not stat.S_ISDIR(at.st_mode) or _ident(at) != ident:
                c.fail(f"{what} is not the folder recorded")
        except OSError as exc:
            c.incomplete(_text(exc))
    c = check("PX-07", "F")
    for fd, what in ((run.sets_fd, "sets"), (run.m_fd, "M"), (run.rs_fd, "RS"), (run.a_fd, "A"), (run.p_fd, "P"),
                     (run.w_fd, "W")):
        try:
            if _mount_id(fd, f"PX {what}") != run.fsid.mnt_id or _fstat(fd, f"PX {what}").st_dev != run.fsid.st_dev:
                c.fail(f"{what} is on another mount or device")
        except (FsprimError, OSError) as exc:
            c.incomplete(f"{what}: {_text(exc)}")
    # S: PX-08 to PX-12
    s_l, s_r = _lstat_quiet(run.sets_fd, f, "PX S"), _complete(run, run.sets_fd, f, "PX S")
    _file_checks(check, ("PX-08", "PX-09", "PX-10", "PX-11"), s_l, s_r, run.i_t, run.O)
    c = check("PX-12", "F")
    if isinstance(s_r, str):
        c.incomplete(s_r)
    else:
        try:
            es = _parse_set(s_r.data.decode("utf-8"))
            if es.schema_version != SOURCE_SCHEMA_VERSION:
                c.fail(f"it parses as format {es.schema_version}")
        except (UnicodeDecodeError, ValueError) as exc:
            c.fail(f"it does not parse: {str(exc).splitlines()[0] if str(exc) else type(exc).__name__}")
    # W/f: PX-13 to PX-16, and the displaced observation
    w_l, w_r = _lstat_quiet(run.w_fd, f, "PX W/f"), _complete(run, run.w_fd, f, "PX W/f")
    _file_checks(check, ("PX-13", "PX-14", "PX-15", "PX-16"), w_l, w_r, run.i_s, run.U)
    observation = _observation(w_l, w_r)
    for name, dir_fd, entry, ident, data, what in (
            ("PX-17", run.p_fd, f, run.i_pf, run.U, "P/f"), ("PX-18", run.m_fd, f, run.i_b, run.O, "B"),
            ("PX-19", run.r_fd, f"{run.urec.record_id}.json", run.i_urec, run.urec_bytes, "U-rec")):
        c = check(name, "F")
        got = _complete(run, dir_fd, entry, f"PX {what}")
        if isinstance(got, str):
            c.incomplete(got)
        elif (got.dev, got.ino) != ident or got.data != data:
            c.fail(f"{what} is not the recorded file with the expected bytes")
    c = check("PX-20", "G")
    got = _complete(run, run.a_fd, RECORD, "PX restore.json")
    if isinstance(got, str):
        c.incomplete(got)
    elif got.data != run.rbytes or (got.dev, got.ino) != run.rident:
        c.fail("restore.json is not the exchanging record this run wrote")
    e1_sets = dict(run.l1["sets"])
    e1_sets[f] = (e1_sets[f][0], e1_sets[f][1]) + run.i_t
    for name, kind, fd, want, what in (("PX-21", "L", run.sets_fd, e1_sets, "sets"),
                                       ("PX-22", "L", run.m_fd, _e_m(run), "M"),
                                       ("PX-23", "L", run.r_fd, run.l1.get("R"), "R"),
                                       ("PX-24", "G", run.rs_fd, _e_rs(run), "RS")):
        c = check(name, kind)
        got = _list(fd, f"PX {what}")
        if isinstance(got, str):
            c.incomplete(got)
        elif want is None or got != want:
            c.fail(f"the {what} listing differs from its expected listing")
    c = check("PX-25", "G")
    got = _list(run.a_fd, "PX A")
    if isinstance(got, str):
        c.incomplete(got)
    elif set(got) != {RECORD, PRESERVED, SWAP}:
        c.fail(f"A holds {sorted(got)}")
    c = check("PX-26", "G")
    got = _list(run.p_fd, "PX P")
    if isinstance(got, str):
        c.incomplete(got)
    elif set(got) != {f}:
        c.fail(f"P holds {sorted(got)}")
    c = check("PX-27", "G")
    got = _list(run.w_fd, "PX W")
    if isinstance(got, str):
        c.incomplete(got)
    elif got != {f: ("regular", 1) + run.i_s}:
        c.fail("W is not exactly the displaced file at I_s")
    return checks, observation


def _lstat_quiet(dir_fd: int, name: str, what: str):
    try:
        return _lstat_at(dir_fd, name, what)
    except FileNotFoundError:
        return "missing"
    except OSError as exc:
        return _text(exc)


def _file_checks(check, names, lst, read, ident, data) -> None:
    kind_c, nlink_c, ident_c, bytes_c = (check(n, "F") for n in names)
    if isinstance(lst, str):
        for c in (kind_c, nlink_c, ident_c):
            (c.fail if lst == "missing" else c.incomplete)(lst)
    else:
        if not stat.S_ISREG(lst.st_mode):
            kind_c.fail("not a regular file")
        if lst.st_nlink != 1:
            nlink_c.fail(f"{lst.st_nlink} links")
        if _ident(lst) != ident:
            ident_c.fail("another identity")
    if isinstance(read, str):
        bytes_c.incomplete(read)
    elif read.data != data:
        bytes_c.fail(f"{len(read.data)} bytes, sha256 {_sha(read.data)}")


def _observation(lst, read) -> DisplacedObservation | None:
    if isinstance(lst, str):
        if lst == "missing":
            return DisplacedObservation(kind="missing", size=None, sha256=None, dev=None, ino=None, nlink=None)
        return None
    kind = rc._kind(lst.st_mode)
    if kind == "regular" and not isinstance(read, str) and (read.dev, read.ino) == _ident(lst):
        return DisplacedObservation(kind="regular", size=len(read.data), sha256=_sha(read.data), dev=lst.st_dev,
                                    ino=lst.st_ino, nlink=lst.st_nlink)
    if kind == "regular":
        kind = "unreadable"
    return DisplacedObservation(kind=kind, size=lst.st_size, sha256=None, dev=lst.st_dev, ino=lst.st_ino,
                                nlink=lst.st_nlink)
