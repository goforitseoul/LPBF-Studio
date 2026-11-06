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
  its path identity, then, when an M descriptor is given, an exclusive lock on M and its identity
  (R2 steps 1 to 4). An operation that creates M does so while holding the sets lock and then calls
  ``Held.lock_m``.
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


def _unlock(fd: int) -> None:
    try:
        _flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass  # best effort; closing the descriptor (or process exit) releases it in any case


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
    except BaseException:
        _close(fd)
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

    def __enter__(self) -> Held:
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    def _take_m(self, m_fd: int, owned: bool) -> None:
        """Lock ``m_fd`` in this holder's mode and check its identity; on refusal leave M unheld."""
        try:
            _check_folder(m_fd, Step.M_OPEN, "migration folder")
            _lock(m_fd, self.mode, Step.M_LOCK, "migration folder")
        except BaseException:
            if owned:
                _close(m_fd)
            raise
        try:
            _check_m_identity(self.sets_fd, m_fd)
        except BaseException:
            _unlock(m_fd)
            if owned:
                _close(m_fd)
            raise
        self.m_fd = m_fd
        self._owns_m = owned

    def lock_m(self, m_fd: int) -> None:
        """Exclusive holders only: lock M (for example one just created under the sets lock)."""
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
        """Release M, then the sets folder; close M if this module opened it. Idempotent."""
        if self.released:
            return
        self.released = True
        m_fd, self.m_fd = self.m_fd, None
        if m_fd is not None:
            _unlock(m_fd)
            if self._owns_m:
                try:
                    _close(m_fd)
                except OSError:
                    pass
        if self._sets_locked:
            self._sets_locked = False
            _unlock(self.sets_fd)


def _acquire(mode: Mode, sets_path, sets_fd: int) -> Held:
    _require_platform()
    _check_folder(sets_fd, Step.SETS_FOLDER, "sets folder")
    held = Held(mode, sets_path, sets_fd)
    _lock(sets_fd, mode, Step.SETS_LOCK, "library folder")
    held._sets_locked = True
    try:
        _check_sets_identity(sets_path, sets_fd)
    except BaseException:
        held.release()
        raise
    return held


def acquire_exclusive(sets_path, sets_fd: int, m_fd: int | None = None) -> Held:
    """Exclusive operations (R2): the sets lock, its identity, then the M lock and its identity.

    ``sets_fd`` is the caller's descriptor of the folder at ``sets_path``; ``m_fd``, if given, the
    caller's descriptor of M opened from it. Neither is ever closed here."""
    held = _acquire(Mode.EXCLUSIVE, sets_path, sets_fd)
    if m_fd is not None:
        try:
            held._take_m(m_fd, owned=False)
        except BaseException:
            held.release()
            raise
    return held


def acquire_writer(sets_path, sets_fd: int) -> Held:
    """Normal set writers: the shared sets lock and its identity, then, if M exists, the shared M
    lock and its identity. M is opened here (and closed on release) and never created."""
    held = _acquire(Mode.SHARED, sets_path, sets_fd)
    try:
        fd = _open_m(sets_fd)
        if fd is not None:
            held._take_m(fd, owned=True)
    except BaseException:
        held.release()
        raise
    return held
