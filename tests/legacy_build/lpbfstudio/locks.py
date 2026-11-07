"""Library folder locks and their D-1 classification (B7 Phase 2, S3-B2c-b, milestone b1b).

The lock primitive that later, separately authorized stages build on: the library writers (b2a),
upgrade (b2b) and restore (b3). Design: frozen revision 5 sections 2.2 and 3, revision 3 sections
2.2 to 2.4, and plan revision 4.7 state machine section 3.2 (R2) with decision D-1. **Nothing in
the application calls this module yet**, so it changes no existing behaviour. It imports no
application module.

The lock objects (L-A, D-2.4):

* the **sets folder itself**: an ``flock`` on a descriptor of the folder. No lock file is created,
  so nothing can go stale: a killed holder's lock is released by the kernel;
* the **migration folder M** (``.format1-backup``), locked as before so that builds which lock only
  M stay mutually excluded. M is opened relative to the sets folder descriptor with
  ``O_DIRECTORY | O_NOFOLLOW``, never through a link, and is **never created** here (C-1).

Who takes what:

* ``acquire_exclusive`` (upgrade, report, apply, restore): an exclusive lock on the sets folder,
  its path identity, then, **whenever M exists**, an exclusive lock on M and its identity (R2 steps
  1 to 4). The module finds and opens M itself, so leaving out a descriptor can never skip the M
  lock. A caller that already opened M passes that descriptor instead: restore must, because the
  frozen survey repeats its ``flock`` on the descriptor cached in ``ctx.dirs`` (plan revision 4.7
  state machine sections 3.1 to 3.3), and a second open file description would be busy against the
  run's own lock. An operation that creates M does so while holding the sets lock and then calls
  ``Held.lock_m``; that is the only reason for a later M lock.
* ``acquire_writer`` (normal set writers): a shared lock on the sets folder, its path identity,
  then, if M exists, a shared lock on M and its identity (revision 5 section 2.2 steps 1 and 2).
  ``Held.recheck_m`` is step 3: just before the final replace, a writer that found no M checks
  again and, if M has appeared, locks it or refuses.

Every request is non-blocking (D-2.1) and made exactly once: there is no retry and no wait. Every
failure refuses (fail closed, D-2.5 (ii)); nothing here ever proceeds unlocked.

Classification (D-1), by errno and never by exception type:

* ``EWOULDBLOCK``/``EAGAIN`` from ``flock``: contention, ``Refusal.BUSY`` (``refused_busy``);
* any other ``OSError`` from ``flock`` (``ENOLCK``, ``EOPNOTSUPP``, ``EINVAL``, ``EIO``, ``EBADF``,
  an errno-less error, ...), a folder that cannot be opened or seen as a non-link folder, an
  ``fstat``/``stat`` failure, and a path identity mismatch after locking:
  ``Refusal.UNVERIFIABLE`` (``refused_unverifiable``);
* a platform without the primitives: ``Refusal.UNSUPPORTED`` (``refused_platform``), before any
  system call. Under F-1 this makes writes read-only there (Windows included).

Contention is never reported as unverifiable, and an unverifiable state is never reported as busy.
A refusal releases every lock the call itself took and closes every descriptor it opened; the
caller's descriptors are never closed here.

Cleanup never replaces the outcome. A close that fails while an exception is on its way out is
recorded as ``close_errno`` on that exception, which stays the primary result. A close that fails
with nothing else going wrong (``Held.release``) is raised as the ``OSError`` it is, after every
lock has been dealt with; it is not a refusal.

Lifecycle, for the caller:

* ``Held.released`` means release was carried out, not that every lock is gone. If ``LOCK_UN``
  fails, ``Held.unlock_error`` holds the error. A descriptor this module opened is closed anyway,
  which frees its lock; a **caller's** descriptor cannot be closed here, so its lock lasts until
  the caller closes that descriptor. Callers therefore close their descriptors after release, in
  every case.
* An interrupt that arrives after ``flock`` has granted a lock is cleaned up by the acquiring call.
  One window cannot be closed from here: an interrupt after the acquiring call has finished its
  work and before the caller has received ``Held``. The caller then has no ``Held`` to release, and
  closing its sets descriptor (or ending the process) frees the sets lock; a descriptor this module
  opened for M stays open, and its lock held, until the process ends.

Limits, stated: the identity check after locking detects a swapped folder; it cannot stop a
program that ignores the locks, and path-based writers keep the D-2.6 window between this check
and their own path operations. ``flock`` is advisory and coordinates this host's processes only.
"""

from __future__ import annotations

import enum
import errno as _errno
import os
import stat

try:
    import fcntl
except ImportError:  # Windows: refused by platform_problem(); the module must still import
    fcntl = None  # type: ignore[assignment]

M_NAME = ".format1-backup"  # the migration folder (upgrade.MIGRATION_DIR, pinned by a test)

_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_CONTENTION = frozenset({_errno.EWOULDBLOCK, _errno.EAGAIN})


# --------------------------------------------------------------------------- results


class Refusal(enum.Enum):
    BUSY = "busy"
    UNVERIFIABLE = "unverifiable"
    UNSUPPORTED = "unsupported"


RESULT = {
    Refusal.BUSY: "refused_busy",
    Refusal.UNVERIFIABLE: "refused_unverifiable",
    Refusal.UNSUPPORTED: "refused_platform",
}


class Step(enum.Enum):
    """Where the refusal happened."""

    PLATFORM = "platform"
    SETS_FOLDER = "sets_folder"  # opening or seeing the sets folder as a folder
    SETS_LOCK = "sets_lock"
    SETS_IDENTITY = "sets_identity"  # stat(sets_path) against fstat(sets_fd)
    M_OPEN = "m_open"  # opening M as a non-link folder, or seeing it
    M_LOCK = "m_lock"
    M_IDENTITY = "m_identity"  # fstatat(sets_fd, M, no follow) against fstat(M_fd)


class Mode(enum.Enum):
    SHARED = "shared"
    EXCLUSIVE = "exclusive"


class LockRefused(Exception):
    """A lock was not taken. ``refusal`` is the D-1 class, ``result`` its result name."""

    def __init__(self, refusal: Refusal, step: Step, err: int | None, detail: str) -> None:
        super().__init__(f"{RESULT[refusal]} at {step.value}: {detail}")
        self.refusal = refusal
        self.step = step
        self.errno = err
        self.detail = detail
        self.close_errno: int | None = None  # a close that also failed; this refusal stays primary

    @property
    def result(self) -> str:
        return RESULT[self.refusal]

    @property
    def busy(self) -> bool:
        return self.refusal is Refusal.BUSY


def classify(exc: OSError) -> Refusal:
    """D-1: only ``EWOULDBLOCK``/``EAGAIN`` is contention; every other lock error is unverifiable."""
    return Refusal.BUSY if exc.errno in _CONTENTION else Refusal.UNVERIFIABLE


# --------------------------------------------------------------------------- platform


def platform_problem() -> str | None:
    """What this platform lacks for these locks, or None."""
    missing = []
    if fcntl is None or not hasattr(fcntl, "flock"):
        missing.append("fcntl.flock")
    for flag in ("O_DIRECTORY", "O_NOFOLLOW"):
        if not hasattr(os, flag):
            missing.append(f"os.{flag}")
    for fn, name in ((os.open, "os.open"), (os.stat, "os.stat")):
        if fn not in os.supports_dir_fd:
            missing.append(f"{name} with dir_fd")
    if os.stat not in os.supports_follow_symlinks:
        missing.append("os.stat without following links")
    return f"needs {', '.join(missing)}" if missing else None


def _require_platform() -> None:
    problem = platform_problem()
    if problem:
        raise LockRefused(Refusal.UNSUPPORTED, Step.PLATFORM, None,
                          f"folder locks are not supported on this platform: it {problem}")


# --------------------------------------------------------------------------- narrow helpers
# Each wraps exactly one system call, so tests can inject a failure at one point.


def _flock(fd: int, op: int) -> None:
    fcntl.flock(fd, op)


def _fstat(fd: int) -> os.stat_result:
    return os.fstat(fd)


def _stat_path(path) -> os.stat_result:
    return os.stat(path)


def _lstat_at(dir_fd: int, name: str) -> os.stat_result:
    return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)


def _open_at(dir_fd: int, name: str, flags: int) -> int:
    return os.open(name, flags, dir_fd=dir_fd)


def _close(fd: int) -> None:
    os.close(fd)


def _close_during(exc: BaseException, fd: int) -> None:
    """Close ``fd`` while ``exc`` propagates: a failure is recorded on ``exc``, never replaces it."""
    try:
        _close(fd)
    except OSError as close_exc:
        exc.close_errno = close_exc.errno


def _dir_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | _CLOEXEC


def _ident(st) -> tuple[int, int]:
    return st.st_dev, st.st_ino


def _text(exc: OSError) -> str:
    return exc.strerror or str(exc) or type(exc).__name__


# --------------------------------------------------------------------------- steps


def open_sets(sets_path) -> int:
    """Open the sets folder (following the user-given path once). The caller owns the descriptor."""
    _require_platform()
    try:
        fd = os.open(sets_path, _dir_flags())
    except OSError as exc:
        raise LockRefused(Refusal.UNVERIFIABLE, Step.SETS_FOLDER, exc.errno,
                          f"the sets folder cannot be opened as a folder: {_text(exc)}") from None
    return fd


def _check_folder(fd: int, step: Step, what: str) -> os.stat_result:
    try:
        st = _fstat(fd)
    except OSError as exc:
        raise LockRefused(Refusal.UNVERIFIABLE, step, exc.errno,
                          f"the {what} cannot be examined: {_text(exc)}") from None
    if not stat.S_ISDIR(st.st_mode):
        raise LockRefused(Refusal.UNVERIFIABLE, step, None, f"the {what} descriptor is not a folder")
    return st


def _lock(fd: int, mode: Mode, step: Step, what: str) -> None:
    op = (fcntl.LOCK_EX if mode is Mode.EXCLUSIVE else fcntl.LOCK_SH) | fcntl.LOCK_NB
    try:
        _flock(fd, op)  # once: no retry, never blocking
    except OSError as exc:
        kind = classify(exc)
        detail = (f"the {what} is locked by another operation" if kind is Refusal.BUSY
                  else f"the {what} cannot be locked: {_text(exc)}")
        raise LockRefused(kind, step, exc.errno, detail) from None


def _unlock(fd: int) -> OSError | None:
    """Best effort; the error, if any. Only closing the descriptor (or process exit) then frees it."""
    try:
        _flock(fd, fcntl.LOCK_UN)
    except OSError as exc:
        return exc
    return None


def _check_sets_identity(sets_path, sets_fd: int) -> None:
    try:
        held = _ident(_fstat(sets_fd))
        now = _ident(_stat_path(sets_path))
    except OSError as exc:
        raise LockRefused(Refusal.UNVERIFIABLE, Step.SETS_IDENTITY, exc.errno,
                          f"the library folder cannot be identified: {_text(exc)}") from None
    if held != now:
        raise LockRefused(Refusal.UNVERIFIABLE, Step.SETS_IDENTITY, None,
                          "the library folder changed while it was being locked")


def _check_m_identity(sets_fd: int, m_fd: int) -> None:
    try:
        held = _ident(_fstat(m_fd))
        now = _ident(_lstat_at(sets_fd, M_NAME))
    except OSError as exc:
        raise LockRefused(Refusal.UNVERIFIABLE, Step.M_IDENTITY, exc.errno,
                          f"the migration folder cannot be identified: {_text(exc)}") from None
    if held != now:
        raise LockRefused(Refusal.UNVERIFIABLE, Step.M_IDENTITY, None,
                          "the migration folder changed while it was being locked")


def _open_m(sets_fd: int) -> int | None:
    """M as a non-link folder relative to the sets folder, or None when it does not exist."""
    try:
        fd = _open_at(sets_fd, M_NAME, _dir_flags() | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:  # ELOOP (a link), ENOTDIR (not a folder), or any other error
        raise LockRefused(Refusal.UNVERIFIABLE, Step.M_OPEN, exc.errno,
                          f"the migration folder cannot be opened as a folder: {_text(exc)}") from None
    try:
        _check_folder(fd, Step.M_OPEN, "migration folder")
    except BaseException as exc:
        _close_during(exc, fd)
        raise
    return fd


# --------------------------------------------------------------------------- the holder


class Held:
    """Locks held by one operation. Release with ``release()`` or by leaving the ``with`` block."""

    def __init__(self, mode: Mode, sets_path, sets_fd: int) -> None:
        self.mode = mode
        self.sets_path = sets_path
        self.sets_fd = sets_fd
        self.m_fd: int | None = None
        self._owns_m = False
        self._sets_locked = False
        self.released = False
        self.unlock_error: OSError | None = None  # a failed LOCK_UN: see the module's lifecycle notes

    def __enter__(self) -> Held:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc is None:
            self.release()
        else:
            self._release_during(exc)

    def _release_during(self, exc: BaseException) -> None:
        """``release`` while ``exc`` propagates: a close failure is recorded on it, not raised."""
        try:
            self.release()
        except OSError as close_exc:
            exc.close_errno = close_exc.errno

    def _take_m(self, m_fd: int, owned: bool) -> None:
        """Lock ``m_fd`` in this holder's mode and check its identity; on refusal leave M unheld."""
        try:
            _check_folder(m_fd, Step.M_OPEN, "migration folder")
            _lock(m_fd, self.mode, Step.M_LOCK, "migration folder")
            _check_m_identity(self.sets_fd, m_fd)
            self.m_fd = m_fd
            self._owns_m = owned
        except BaseException as exc:
            self.m_fd = None
            # a refused request took nothing; anything else (an interrupt too) may hold the lock
            if not (isinstance(exc, LockRefused) and exc.step in (Step.M_OPEN, Step.M_LOCK)):
                _unlock(m_fd)
            if owned:
                _close_during(exc, m_fd)
            raise

    def lock_m(self, m_fd: int) -> None:
        """Exclusive holders only: lock an M created under the sets lock, after ``acquire_exclusive``
        found none (revision 3 section 2.2). An M that already exists is locked at acquisition."""
        if self.released or self.mode is not Mode.EXCLUSIVE or self.m_fd is not None:
            raise ValueError("lock_m needs an exclusive holder that does not yet hold M")
        self._take_m(m_fd, owned=False)

    def recheck_m(self) -> bool:
        """Writers only (revision 5 section 2.2 step 3), just before the final replace.

        True when M is held (already, or newly locked now that it has appeared); False when M still
        does not exist. Raises ``LockRefused`` when M has appeared and cannot be locked: the caller
        then abandons its write. Never creates M."""
        if self.released or self.mode is not Mode.SHARED:
            raise ValueError("recheck_m is for a writer that still holds its locks")
        if self.m_fd is not None:
            return True
        try:
            _lstat_at(self.sets_fd, M_NAME)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise LockRefused(Refusal.UNVERIFIABLE, Step.M_OPEN, exc.errno,
                              f"the migration folder cannot be examined: {_text(exc)}") from None
        fd = _open_m(self.sets_fd)
        if fd is None:  # it was there a moment ago: something is changing the library
            raise LockRefused(Refusal.UNVERIFIABLE, Step.M_OPEN, _errno.ENOENT,
                              "the migration folder appeared and disappeared while saving")
        self._take_m(fd, owned=True)
        return True

    def verify(self) -> None:
        """Repeat both identity checks (exclusive operations' final re-check, revision 3 §2.3)."""
        if self.released:
            raise ValueError("the locks were released")
        _check_sets_identity(self.sets_path, self.sets_fd)
        if self.m_fd is not None:
            _check_m_identity(self.sets_fd, self.m_fd)

    def release(self) -> None:
        """Release M, then the sets folder; close M if this module opened it. Idempotent.

        A failed ``LOCK_UN`` is kept in ``unlock_error``. A failed close is raised, once, after the
        sets folder has been dealt with. The caller closes its own descriptors after this, in every
        case: until then a lock whose ``LOCK_UN`` failed may still be held."""
        if self.released:
            return
        self.released = True
        close_error = None
        m_fd, self.m_fd = self.m_fd, None
        if m_fd is not None:
            self.unlock_error = _unlock(m_fd)
            if self._owns_m:
                try:
                    _close(m_fd)
                except OSError as exc:
                    close_error = exc
        if self._sets_locked:
            self._sets_locked = False
            self.unlock_error = _unlock(self.sets_fd) or self.unlock_error
        if close_error is not None:
            raise close_error


def _acquire(mode: Mode, sets_path, sets_fd: int) -> Held:
    _require_platform()
    _check_folder(sets_fd, Step.SETS_FOLDER, "sets folder")
    held = Held(mode, sets_path, sets_fd)
    try:
        held._sets_locked = True  # before the request: an interrupt just after the grant is cleaned up
        _lock(sets_fd, mode, Step.SETS_LOCK, "library folder")
        _check_sets_identity(sets_path, sets_fd)
    except BaseException as exc:
        if isinstance(exc, LockRefused) and exc.step is Step.SETS_LOCK:
            held._sets_locked = False  # a refused request took nothing, so there is nothing to unlock
        held._release_during(exc)
        raise
    return held


def _take_existing_m(held: Held) -> None:
    fd = _open_m(held.sets_fd)
    if fd is not None:
        held._take_m(fd, owned=True)


def acquire_exclusive(sets_path, sets_fd: int, m_fd: int | None = None) -> Held:
    """Exclusive operations (R2): the sets lock, its identity, then the M lock and its identity.

    ``sets_fd`` is the caller's descriptor of the folder at ``sets_path``. ``m_fd`` is never a
    statement that M is absent. ``None`` means this module finds M: if M exists it is opened (never
    through a link), locked, identified and held in ``Held.m_fd`` until release, which closes it;
    if M does not exist the holder has the sets lock only, and M is not created. An ``m_fd`` given
    is the caller's own descriptor of M, for a caller whose later steps must lock that same open
    file description (restore). It is locked and identified the same way. The caller's descriptors
    are never closed here. M that exists but cannot be opened, locked or identified refuses, and
    nothing stays held."""
    held = _acquire(Mode.EXCLUSIVE, sets_path, sets_fd)
    try:
        if m_fd is None:
            _take_existing_m(held)
        else:
            held._take_m(m_fd, owned=False)
    except BaseException as exc:
        held._release_during(exc)
        raise
    return held


def acquire_writer(sets_path, sets_fd: int) -> Held:
    """Normal set writers: the shared sets lock and its identity, then, if M exists, the shared M
    lock and its identity. M is opened here (and closed on release) and never created."""
    held = _acquire(Mode.SHARED, sets_path, sets_fd)
    try:
        _take_existing_m(held)
    except BaseException as exc:
        held._release_during(exc)
        raise
    return held
