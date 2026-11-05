"""Upgrade one format 1 set file to format 2 (B7 Phase 2, S3-B1: the safe file-operation core).

``upgrade_file`` follows the S3-A state model (``migration.py``) and writes, in this order:

1. nothing at all until the source has been read, validated and found eligible, then read and
   validated again under the library upgrade lock, and the complete format 2 target has been
   built through the model and verified;
2. the migration record (``preparing``), created exclusively, so evidence exists before any
   other write;
3. the backup, created exclusively with the exact source bytes, then read back and compared
   (length and SHA-256); an existing backup is never overwritten;
4. the record moved to ``backed_up``, with the digest of the upgraded bytes;
5. the target staged in a temporary file in the sets folder (same file system), fsynced, read
   back and validated;
6. just before the replace: the record and the source are read again and must be the same files
   (device and inode) with the same bytes that this run wrote and read; any difference stops the
   run without replacing;
7. ``rename`` of the staged file over the source;
8. the source read back: ``upgraded`` only when its length and SHA-256 match the upgraded bytes
   and it parses and validates as the intended format 2 set (``confirm_replace``).

A stop before the replace is recorded ``failed`` only when the source still matches the original
bytes; otherwise ``uncertain``. Nothing is retried, and no pass ids are generated a second time:
a file with an unresolved record is refused until that record is resolved (a later stage).
Nothing is deleted except this run's own staging and record-update temporary files.

Records and backups live in ``<sets>/.format1-backup/`` (backups) and
``<sets>/.format1-backup/records/<record_id>.json``. The library lists only ``<sets>/*.json``, so
neither is ever loaded as a set.

**One upgrade at a time.** ``library_lock`` takes an exclusive ``flock`` on the migration folder
itself (no lock file). It is taken after the read-only pre-check and before the first write, and
held until the outcome is recorded; a second LPBF Studio upgrade of the same library, in this or
another process, gets a ``busy`` result and writes nothing. The OS releases the lock when its
holder closes it or exits, killed or not, so there is never a stale lock to judge or delete. A
file system that cannot lock (for example some network or FUSE mounts) is refused.

**Paths and links.** The sets folder is resolved once and opened; every other folder and file is
opened relative to an already-open folder descriptor (``dir_fd``) with ``O_NOFOLLOW``, so swapping
a folder or a file for a link after a check cannot redirect a read or a write. Files are read
from the opened descriptor only after ``fstat`` shows a regular file (``O_NONBLOCK`` keeps a FIFO
from blocking the open), and created with ``O_CREAT | O_EXCL``, which never follows or replaces an
existing entry, dangling links included. The sets folder itself may be reached through a link.

**Platforms.** These primitives exist on Linux and macOS (``os.supports_dir_fd``, ``O_NOFOLLOW``,
``O_DIRECTORY``, ``fcntl.flock``). Where any is missing, notably Windows, ``upgrade_file`` refuses
before reading or writing anything (``_platform_problem``). ``rename`` within one folder is atomic
on POSIX file systems; folders are fsynced after entries are created or renamed, which some file
systems ignore.

**What the lock does not cover.** Programs that do not take it: other editors, sync tools,
older LPBF Studio builds, and this build's ordinary saves (``Library.save``), which do not take
the upgrade lock. Such a write between the last re-check (step 6) and the rename is lost to the
rename; the backup still holds the original, not that write. The window is a few system calls,
but it is not closed. Operational precondition: upgrade a library only while nothing else writes
to it, on a local file system.
"""

from __future__ import annotations

import contextlib
import contextvars
import errno
import hashlib
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # Windows: refused by _platform_problem
    fcntl = None  # type: ignore[assignment]

from .library import _parse_set
from .migration import (
    TARGET_SCHEMA_VERSION, DiskState, FileResult, MigrationRecord, Observation, RecordError, RecordStatus,
    advance, classify_bytes, confirm_replace, disk_state, dump_record, new_record, parse_record, result_for,
)
from .models import ExposureSet, new_pass_id

MIGRATION_DIR = ".format1-backup"
RECORDS_DIR = "records"
TMP_SUFFIX = ".upgrade-tmp"
_RECORD_TMP_SUFFIX = ".tmp"
_NAME = re.compile(r"^[^./\\\x00][^/\\\x00]*\.json$")  # a plain, visible file name in the sets folder
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


@dataclass(frozen=True)
class UpgradeOutcome:
    """What happened to one file. ``record`` is the last record state written to disk (None when
    no record was written). When updating the record failed, it is the record as then read back
    from disk, or None when that read failed or did not give this run's record (its state on disk
    is then unknown, and the reason says so); ``pass_ids`` are the ids of the format 2 file this run wrote;
    ``busy`` is True when another upgrade held the library lock (nothing was written)."""
    result: FileResult
    reason: str
    record: MigrationRecord | None = None
    record_path: Path | None = None
    backup_path: Path | None = None
    pass_ids: tuple[str, ...] = ()
    busy: bool = False


class LibraryBusy(Exception):
    """Another upgrade holds this library's upgrade lock."""


class _RecordChanged(Exception):
    """The record file on disk is not the one this run last wrote."""


def _platform_problem() -> str | None:
    """What this platform lacks for a safe upgrade, or None."""
    missing = []
    if fcntl is None or not hasattr(fcntl, "flock"):
        missing.append("fcntl.flock")
    for flag in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK"):
        if not hasattr(os, flag):
            missing.append(f"os.{flag}")
    for fn, name in ((os.open, "os.open"), (os.stat, "os.stat"), (os.mkdir, "os.mkdir"),
                     (os.unlink, "os.unlink"), (os.rename, "os.rename")):
        if fn not in os.supports_dir_fd:
            missing.append(f"{name} with dir_fd")
    if os.stat not in os.supports_follow_symlinks:
        missing.append("os.stat without following links")
    if os.listdir not in os.supports_fd:
        missing.append("os.listdir of a folder descriptor")
    return f"needs {', '.join(missing)}" if missing else None


# --------------------------------------------------------------------------- folder descriptors


class _Ctx:
    """The folder descriptors of one operation, and the identity (device, inode) of the file each
    path last read or created, and of the files this run owns."""

    def __init__(self) -> None:
        self.dirs: dict[Path, int] = {}
        self.ident: dict[Path, tuple[int, int]] = {}
        self.owned: dict[Path, tuple[int, int]] = {}
        self.fds: list[int] = []

    def close(self) -> None:
        for fd in reversed(self.fds):
            with contextlib.suppress(OSError):
                os.close(fd)


_CTX: contextvars.ContextVar[_Ctx | None] = contextvars.ContextVar("lpbf_upgrade_ctx", default=None)


@contextlib.contextmanager
def _context(fresh: bool) -> Iterator[_Ctx]:
    current = _CTX.get()
    if current is not None and not fresh:
        yield current
        return
    ctx = _Ctx()
    token = _CTX.set(ctx)
    try:
        yield ctx
    finally:
        _CTX.reset(token)
        ctx.close()


def _dir_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | _CLOEXEC


def _open_base(ctx: _Ctx, base: Path) -> int:
    if base not in ctx.dirs:
        fd = os.open(base, _dir_flags())
        ctx.fds.append(fd)
        ctx.dirs[base] = fd
    return ctx.dirs[base]


def _open_sub(ctx: _Ctx, path: Path, create: bool = False) -> int:
    """Open folder ``path`` relative to its open parent, never through a link (ELOOP or ENOTDIR)."""
    if path in ctx.dirs:
        return ctx.dirs[path]
    parent = ctx.dirs[path.parent]
    if create:
        try:
            os.mkdir(path.name, 0o755, dir_fd=parent)
        except FileExistsError:
            pass
        else:
            _fsync_fd(parent)
    fd = os.open(path.name, _dir_flags() | os.O_NOFOLLOW, dir_fd=parent)
    ctx.fds.append(fd)
    ctx.dirs[path] = fd
    return fd


@contextlib.contextmanager
def _in_dir(path: Path) -> Iterator[int]:
    """The descriptor of ``path``'s folder: the operation's own when registered, else a new one."""
    ctx = _CTX.get()
    fd = ctx.dirs.get(path.parent) if ctx is not None else None
    if fd is not None:
        yield fd
        return
    fd = os.open(path.parent, _dir_flags())
    try:
        yield fd
    finally:
        os.close(fd)


def _remember(path: Path, st: os.stat_result) -> None:
    ctx = _CTX.get()
    if ctx is not None:
        ctx.ident[path] = (st.st_dev, st.st_ino)


def _fsync_fd(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError:
        pass  # not supported by this file system


# --------------------------------------------------------------------------- I/O helpers
# Narrow helpers, replaced one at a time by the tests to reach each failure window.


def _read_file(path: Path) -> bytes:
    """The bytes of regular file ``path``, read from a descriptor opened without following a link
    and checked with fstat (a FIFO, device or folder is refused without blocking)."""
    with _in_dir(path) as dfd:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _CLOEXEC, dir_fd=dfd)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise OSError(errno.EINVAL, f"{path.name} is not a regular file")
            chunks = []
            while True:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            os.close(fd)
    _remember(path, st)
    return b"".join(chunks)


def _write_new(path: Path, data: bytes) -> None:
    """Create ``path`` (never an existing entry, never through a link), write, fsync, and fsync
    its folder. A failure after creating it leaves the partial file for the caller to report."""
    with _in_dir(path) as dfd:
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _CLOEXEC, 0o644,
                     dir_fd=dfd)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
            st = os.fstat(fd)
        finally:
            os.close(fd)
        _fsync_fd(dfd)
    _remember(path, st)


def _lstat(path: Path) -> os.stat_result:
    with _in_dir(path) as dfd:
        return os.stat(path.name, dir_fd=dfd, follow_symlinks=False)


def _unlink(path: Path) -> None:
    with _in_dir(path) as dfd:
        os.unlink(path.name, dir_fd=dfd)


def _rename(src: Path, dst: Path) -> None:
    """POSIX rename: atomically replaces ``dst`` within one file system."""
    with _in_dir(src) as sfd, _in_dir(dst) as dfd:
        os.rename(src.name, dst.name, src_dir_fd=sfd, dst_dir_fd=dfd)
        _fsync_fd(dfd)


def _replace(src: Path, dst: Path) -> None:
    _rename(src, dst)


def _flock(fd: int, op: int) -> None:
    fcntl.flock(fd, op)


def _stage(base: Path, file_name: str, data: bytes) -> Path:
    """Write the upgraded bytes to a new temporary file next to the source."""
    tmp = base / f".{file_name}.{secrets.token_hex(6)}{TMP_SUFFIX}"
    _write_new(tmp, data)
    return tmp


def _identity(st: os.stat_result) -> tuple[int, int]:
    return st.st_dev, st.st_ino


def _update_record(path: Path, old: bytes, new: MigrationRecord) -> bytes:
    """Replace this run's record file by ``new`` and return the bytes written.

    The record's path is unique to this run (its random record_id, created exclusively), and all
    LPBF Studio upgrades of the library hold the library lock while they write records. On top of
    that the file is replaced only if it is still the same file (device and inode) holding exactly
    the bytes this run last wrote, checked once when read and again just before the rename, so a
    different, edited or re-saved record is never overwritten. The new state goes to an exclusively
    created temporary file that is moved over the record by ``rename``, so a reader sees the old or
    the new state, never a partial one; either is a valid state that is judged against the disk.
    A program that ignores the lock and rewrites the record between the last check and the rename
    can still lose its write; the window is one system call."""
    ctx = _CTX.get()
    expected = ctx.owned.get(path) if ctx is not None else None
    if _read_file(path) != old or (ctx is not None and ctx.ident.get(path) != expected):
        raise _RecordChanged(f"its migration record {path.name} changed on disk; it was not overwritten")
    data = dump_record(new).encode("utf-8")
    tmp = path.parent / f".{path.stem}.{secrets.token_hex(6)}{_RECORD_TMP_SUFFIX}"
    _write_new(tmp, data)
    tmp_ident = ctx.ident.get(tmp) if ctx is not None else None
    try:
        if ctx is not None and _identity(_lstat(path)) != expected:
            raise _RecordChanged(f"its migration record {path.name} changed on disk; it was not overwritten")
        _rename(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            _unlink(tmp)
        raise
    if ctx is not None:
        ctx.owned[path] = tmp_ident
    if _read_file(path) != data or (ctx is not None and ctx.ident.get(path) != tmp_ident):
        raise _RecordChanged(f"its migration record {path.name} did not read back as written")
    return data


def _observe(path: Path) -> tuple[Observation, bytes | None]:
    try:
        data = _read_file(path)
    except FileNotFoundError:
        return Observation(exists=False), None
    except OSError as exc:
        return Observation(exists=True, error=exc.strerror or str(exc)), None
    return Observation(exists=True, size=len(data), sha256=hashlib.sha256(data).hexdigest()), data


def _folder_problem(path: Path) -> str | None:
    """Why ``path`` cannot be used as a migration folder; None if it is a real folder or absent."""
    try:
        st = _lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode):
        return f"{path.name} is a link; the upgrade does not follow links"
    if not stat.S_ISDIR(st.st_mode):
        return f"{path.name} is not a folder"
    return None


def _exists(path: Path) -> bool:
    try:
        _lstat(path)
    except FileNotFoundError:
        return False
    return True


def _same(data: bytes, sha256: str, size: int) -> bool:
    return len(data) == size and hashlib.sha256(data).hexdigest() == sha256


def _digest_matches(a: bytes, b: bytes) -> bool:
    return _same(a, hashlib.sha256(b).hexdigest(), len(b))


@contextlib.contextmanager
def library_lock(sets_dir: Path | str) -> Iterator[None]:
    """Hold the library's upgrade lock: an exclusive, non-blocking ``flock`` on a new descriptor of
    the migration folder (created if missing, never through a link). Raises LibraryBusy when
    another holder has it, OSError when the platform or file system cannot lock. Released on exit,
    and by the OS if the process dies."""
    problem = _platform_problem()
    if problem:
        raise OSError(f"upgrading is not supported on this platform: it {problem}")
    base = Path(os.path.realpath(sets_dir))
    mdir = base / MIGRATION_DIR
    with _context(fresh=False) as ctx:
        parent = _open_base(ctx, base)
        try:
            os.mkdir(MIGRATION_DIR, 0o755, dir_fd=parent)
        except FileExistsError:
            pass
        else:
            _fsync_fd(parent)
        fd = os.open(MIGRATION_DIR, _dir_flags() | os.O_NOFOLLOW, dir_fd=parent)
        try:
            try:
                _flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LibraryBusy("another upgrade is using this library") from None
            if mdir not in ctx.dirs:  # the locked folder is the one this operation writes in
                ctx.dirs[mdir] = fd
                ctx.fds.append(fd)
                fd = -1
            try:
                yield
            finally:
                with contextlib.suppress(OSError):
                    _flock(ctx.dirs[mdir] if fd == -1 else fd, fcntl.LOCK_UN)
        finally:
            if fd != -1:
                os.close(fd)


# --------------------------------------------------------------------------- target


def _without_format(es: ExposureSet) -> dict[str, Any]:
    data = es.model_dump()
    data.pop("schema_version")
    for et in data["exposure_types"]:
        for p in et.get("passes", ()):
            p.pop("id", None)
    return data


def _build_target(es: ExposureSet) -> tuple[ExposureSet, tuple[str, ...]]:
    """The format 2 set: the same data, ``schema_version`` 2 and a new random id per contour pass,
    validated by the model."""
    data = es.model_dump()
    data["schema_version"] = TARGET_SCHEMA_VERSION
    ids: list[str] = []
    for et in data["exposure_types"]:
        if et.get("kind") == "contour":
            for p in et["passes"]:
                p["id"] = new_pass_id(ids)
                ids.append(p["id"])
    return ExposureSet.model_validate(data), tuple(ids)


def _target_problem(data: bytes, source: ExposureSet, ids: tuple[str, ...]) -> str | None:
    """Why ``data`` is not the intended format 2 file for ``source`` with ``ids``; None if it is."""
    try:
        es = _parse_set(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return f"it cannot be read back: {str(exc).splitlines()[0]}"
    if es.schema_version != TARGET_SCHEMA_VERSION:
        return f"it reads back as format {es.schema_version}"
    contour = es.get("contour")
    if (tuple(p.id for p in contour.passes) if contour is not None else ()) != ids:
        return "its pass ids differ"
    if _without_format(es) != _without_format(source):
        return "its values differ from the original"
    return None


# --------------------------------------------------------------------------- records


def _find_records(ctx: _Ctx, base: Path, file_name: str) -> list[MigrationRecord | RecordError]:
    mdir = base / MIGRATION_DIR
    rdir = mdir / RECORDS_DIR
    for folder in (mdir, rdir):
        problem = _folder_problem(folder)
        if problem:
            return [RecordError(problem)]
        if not _exists(folder):
            return []
    try:
        _open_sub(ctx, mdir)
        rfd = _open_sub(ctx, rdir)
        names = sorted(n for n in os.listdir(rfd) if n.endswith(".json") and not n.startswith("."))
    except OSError as exc:
        return [RecordError(f"the records folder cannot be read: {_text(exc)}")]
    out: list[MigrationRecord | RecordError] = []
    for name in names:
        f = rdir / name
        try:
            if stat.S_ISLNK(_lstat(f).st_mode):
                out.append(RecordError(f"{name} is a link"))
                continue
            rec = parse_record(_read_file(f).decode("utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            out.append(RecordError(f"{name} cannot be read: {_text(exc)}"))
            continue
        except RecordError as exc:
            out.append(RecordError(f"{name}: {exc}"))
            continue
        if f.stem != rec.record_id:
            out.append(RecordError(f"{name} holds record {rec.record_id}"))
        elif rec.source_path == file_name:
            out.append(rec)
    return out


def find_records(sets_dir: Path | str, file_name: str) -> list[MigrationRecord | RecordError]:
    """The migration records for ``file_name``, read only and never through a link. Any record
    file that cannot be read or validated is returned as a RecordError for every file, since it
    cannot be told whose it is."""
    base = Path(os.path.realpath(sets_dir))
    with _context(fresh=True) as ctx:
        _open_base(ctx, base)
        return _find_records(ctx, base, file_name)


# --------------------------------------------------------------------------- upgrade


class _Run:
    """One upgrade after its record exists: every stop goes through ``stop`` so that the record
    is never left claiming more than the disk shows."""

    def __init__(self, src: Path, rpath: Path, record: MigrationRecord, rbytes: bytes,
                 bpath: Path, ids: tuple[str, ...], now: str | None) -> None:
        self.src, self.rpath, self.record, self.rbytes = src, rpath, record, rbytes
        self.bpath, self.ids, self.now = bpath, ids, now

    def outcome(self, result: FileResult, reason: str) -> UpgradeOutcome:
        return UpgradeOutcome(result, reason, self.record, self.rpath, self.bpath, self.ids)

    def save(self, new: MigrationRecord) -> None:
        self.rbytes = _update_record(self.rpath, self.rbytes, new)
        self.record = new

    def on_disk(self, exc: BaseException) -> str:
        """After updating the record failed: read the record once (nothing is written) and set
        ``self.record`` to what the disk holds, or to None when that is unknown (Q-12). Returns
        the text for the reason. The rename may have happened before the failure, so the record
        in memory is never assumed to be the one on disk."""
        failed = f"updating its migration record failed ({_text(exc)})"
        expected, self.record = self.record, None
        try:
            data = _read_file(self.rpath)
        except OSError as again:
            return f"{failed}, and it cannot be read back ({_text(again)}), so its state on disk is unknown"
        try:
            rec = parse_record(data.decode("utf-8"))
        except (UnicodeDecodeError, RecordError):
            return f"{failed}, and it reads back but cannot be parsed, so its state on disk is unknown"
        same = ("record_id", "source_path", "backup_path", "source_sha256", "source_size_bytes", "created_at")
        if any(getattr(rec, k) != getattr(expected, k) for k in same):
            return f"{failed}, and it reads back as another record, so its state on disk is unknown"
        self.record = rec
        return f"{failed}; it reads back as {rec.status.value}"

    def stop(self, error: str) -> UpgradeOutcome:
        """Stop before any replace: failed if the source is verified unchanged, else uncertain."""
        obs, _ = _observe(self.src)
        state = disk_state(obs, self.record)
        if state is DiskState.SOURCE:
            new, result = advance(self.record, RecordStatus.FAILED, error=error, now=self.now), FileResult.FAILED
            reason = f"{error}; the file is unchanged"
        else:
            msg = f"{error}; the file is now {_STATE_TEXT[state]}"
            new, result = advance(self.record, RecordStatus.UNCERTAIN, error=msg, now=self.now), FileResult.UNCERTAIN
            reason = msg
        try:
            self.save(new)
        except (OSError, _RecordChanged) as exc:
            reason += f"; {self.on_disk(exc)}"
        return self.outcome(result, reason)


_STATE_TEXT = {DiskState.SOURCE: "unchanged", DiskState.UPGRADED: "the upgraded file",
               DiskState.OTHER: "different from the original", DiskState.MISSING: "missing",
               DiskState.UNREADABLE: "unreadable"}


def _text(exc: BaseException) -> str:
    return (exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)) or type(exc).__name__


def _read_source(src: Path) -> tuple[bytes, tuple[int, int]] | UpgradeOutcome:
    """The source's bytes and identity, or the outcome refusing it. Never follows a link."""
    try:
        st = _lstat(src)
    except OSError as exc:
        return UpgradeOutcome(FileResult.SKIPPED_UNREADABLE, f"cannot be read: {_text(exc)}")
    if stat.S_ISLNK(st.st_mode):
        return UpgradeOutcome(FileResult.SKIPPED_UNREADABLE, "the file is a link; the upgrade does not follow links")
    if not stat.S_ISREG(st.st_mode):
        return UpgradeOutcome(FileResult.SKIPPED_UNREADABLE, "not a regular file")
    try:
        data = _read_file(src)
    except OSError as exc:
        if exc.errno == errno.ELOOP:  # replaced by a link after the lstat
            return UpgradeOutcome(FileResult.SKIPPED_UNREADABLE, "the file is a link; the upgrade does not follow links")
        return UpgradeOutcome(FileResult.SKIPPED_UNREADABLE, f"cannot be read: {_text(exc)}")
    return data, _CTX.get().ident[src]


def upgrade_file(sets_dir: Path | str, file_name: str, *, include_locked: bool = False,
                 now: str | None = None) -> UpgradeOutcome:
    """Upgrade ``<sets_dir>/<file_name>`` from format 1 to format 2 (see the module docstring).
    Returns an outcome for every expected problem; a KeyboardInterrupt is passed on, releases the
    lock and leaves its record in progress, which is reported from the disk later and never as
    upgraded."""
    if not isinstance(file_name, str) or not _NAME.match(file_name):
        return UpgradeOutcome(FileResult.SKIPPED_UNREADABLE,
                              f"file name {file_name!r} is not a plain set file name in the sets folder")
    problem = _platform_problem()
    if problem:
        return UpgradeOutcome(FileResult.FAILED, f"upgrading is not supported on this platform: it {problem}; "
                                                 "nothing was read or written")
    base = Path(os.path.realpath(sets_dir))
    with _context(fresh=True) as ctx:
        try:
            _open_base(ctx, base)
        except OSError as exc:
            return UpgradeOutcome(FileResult.FAILED, f"the sets folder cannot be opened: {_text(exc)}")
        # read-only pre-check: a file that would be skipped causes no write at all, not even the lock
        read = _read_source(base / file_name)
        if isinstance(read, UpgradeOutcome):
            return read
        first = classify_bytes(read[0], include_locked=include_locked)
        if not first.upgrade:
            return UpgradeOutcome(first.result, first.reason)
        for folder in (base / MIGRATION_DIR, base / MIGRATION_DIR / RECORDS_DIR):
            problem = _folder_problem(folder)
            if problem:
                return UpgradeOutcome(FileResult.FAILED, f"{problem}; nothing was written")
        lock = library_lock(base)
        try:
            lock.__enter__()
        except LibraryBusy as exc:
            return UpgradeOutcome(FileResult.FAILED, f"{exc}; nothing was written", busy=True)
        except OSError as exc:
            return UpgradeOutcome(FileResult.FAILED, f"the migration folder cannot be locked or used "
                                                     f"({_text(exc)}); nothing was written")
        try:
            return _upgrade_locked(ctx, base, file_name, include_locked, now)
        finally:
            lock.__exit__(None, None, None)


def _upgrade_locked(ctx: _Ctx, base: Path, file_name: str, include_locked: bool,
                    now: str | None) -> UpgradeOutcome:
    src = base / file_name
    mdir, rdir = base / MIGRATION_DIR, base / MIGRATION_DIR / RECORDS_DIR

    # 1. under the lock: read and check the source again, then the records (still no writes)
    read = _read_source(src)
    if isinstance(read, UpgradeOutcome):
        return read
    original, source_ident = read
    first = classify_bytes(original, include_locked=include_locked)
    if not first.upgrade:
        return UpgradeOutcome(first.result, first.reason)
    problem = _folder_problem(rdir)
    if problem:
        return UpgradeOutcome(FileResult.FAILED, f"{problem}; nothing was written")
    records = _find_records(ctx, base, file_name)
    checked = classify_bytes(original, include_locked=include_locked, records=records)
    if not checked.upgrade:
        return UpgradeOutcome(checked.result, checked.reason)
    earlier_failed = bool(records)  # eligible, so every one is a failed record for these bytes

    # 2. the complete target, built and verified in memory
    source = _parse_set(original.decode("utf-8"))
    try:
        target, ids = _build_target(source)
    except ValueError as exc:
        return UpgradeOutcome(FileResult.FAILED, f"the format 2 set could not be built: {str(exc).splitlines()[0]}")
    upgraded = target.model_dump_json(indent=2).encode("utf-8")
    problem = _target_problem(upgraded, source, ids)
    if problem:
        return UpgradeOutcome(FileResult.FAILED, f"the format 2 set does not verify: {problem}; nothing was written")

    # 3. the record, before anything else is written
    try:
        _open_sub(ctx, rdir, create=True)
    except OSError as exc:
        return UpgradeOutcome(FileResult.FAILED, f"the records folder cannot be used: {_text(exc)}")
    record = new_record(file_name, f"{MIGRATION_DIR}/{file_name}", original, now=now)
    rpath = rdir / f"{record.record_id}.json"
    rbytes = dump_record(record).encode("utf-8")
    try:
        _write_new(rpath, rbytes)
    except FileExistsError:
        return UpgradeOutcome(FileResult.FAILED, f"a migration record {rpath.name} already exists; it was not "
                                                 "overwritten and nothing was written")
    except OSError as exc:
        return UpgradeOutcome(FileResult.FAILED, f"the migration record could not be written ({_text(exc)}); "
                                                 f"check {rpath.name} if it exists")
    ctx.owned[rpath] = ctx.ident[rpath]
    try:
        if _read_file(rpath) != rbytes or ctx.ident[rpath] != ctx.owned[rpath]:
            return UpgradeOutcome(FileResult.FAILED, f"the migration record {rpath.name} did not read back as written")
    except OSError as exc:
        return UpgradeOutcome(FileResult.FAILED, f"the migration record cannot be read back ({_text(exc)})")
    bpath = mdir / file_name
    run = _Run(src, rpath, record, rbytes, bpath, (), now)

    # 4. the backup: exact bytes, created exclusively, read back; an existing one is never overwritten
    try:
        _write_new(bpath, original)
    except FileExistsError:
        try:
            bst = _lstat(bpath)
            existing = None if stat.S_ISLNK(bst.st_mode) or not stat.S_ISREG(bst.st_mode) else _read_file(bpath)
        except OSError as exc:
            return run.stop(f"a backup {MIGRATION_DIR}/{file_name} exists but cannot be checked ({_text(exc)})")
        if existing is None:
            return run.stop(f"the backup path {MIGRATION_DIR}/{file_name} is a link or not a file; not overwritten")
        if not _digest_matches(existing, original):
            return run.stop(f"a different backup {MIGRATION_DIR}/{file_name} already exists; it was not overwritten")
        if not earlier_failed:
            return run.stop(f"a backup {MIGRATION_DIR}/{file_name} already exists without a migration record; "
                            "it was not overwritten")
        # identical bytes, left by an earlier attempt whose record is failed for these bytes: reuse
    except OSError as exc:
        return run.stop(f"the backup could not be written ({_text(exc)}); check {MIGRATION_DIR}/{file_name}")
    try:
        if not _digest_matches(_read_file(bpath), original):
            return run.stop("the backup did not read back identical to the original")
    except OSError as exc:
        return run.stop(f"the backup cannot be read back ({_text(exc)})")

    # 5. backed_up, with the digest of the bytes about to be written
    try:
        run.save(advance(record, RecordStatus.BACKED_UP, upgraded=upgraded))
    except (OSError, _RecordChanged) as exc:
        return run.stop(f"the migration record could not be updated ({_text(exc)})")

    # 6. stage and verify the target next to the source
    try:
        tmp = _stage(base, file_name, upgraded)
    except OSError as exc:
        return run.stop(f"the upgraded file could not be staged ({_text(exc)})")

    def discard_tmp() -> None:
        with contextlib.suppress(OSError):
            _unlink(tmp)

    try:
        staged = _read_file(tmp)
    except OSError as exc:
        discard_tmp()
        return run.stop(f"the staged file cannot be read back ({_text(exc)})")
    problem = None if _digest_matches(staged, upgraded) else "its bytes differ"
    problem = problem or _target_problem(staged, source, ids)
    if problem:
        discard_tmp()
        return run.stop(f"the staged file does not verify: {problem}")

    # 7. re-check the record and the source immediately before the replace: same files, same bytes
    try:
        record_now = _read_file(rpath)
    except OSError as exc:
        discard_tmp()
        return run.stop(f"the migration record cannot be re-read ({_text(exc)})")
    if record_now != run.rbytes or ctx.ident.get(rpath) != ctx.owned.get(rpath):
        discard_tmp()
        return run.stop("its migration record changed on disk; not replaced")
    try:
        st = _lstat(src)
        current = None if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) else _read_file(src)
    except OSError:
        current = None
    if current is None or not _digest_matches(current, original):
        discard_tmp()
        return run.stop("the file changed while upgrading; it was not replaced")
    if ctx.ident.get(src) != source_ident:
        discard_tmp()
        return run.stop("the file changed while upgrading (it was replaced by another file); it was not replaced")

    # 8. replace, then judge only by what the disk shows
    replace_error: OSError | None = None
    try:
        _replace(tmp, src)
    except OSError as exc:
        replace_error = exc
    if _exists(tmp):  # still there: the rename did not take it
        discard_tmp()
    _fsync_fd(ctx.dirs[base])
    run.ids = ids
    obs, written = _observe(src)
    state = disk_state(obs, run.record)
    if state is DiskState.UPGRADED and _target_problem(written, source, ids):
        state = DiskState.OTHER
    final = confirm_replace(run.record, state, now=now)
    try:
        run.save(final)
    except (OSError, _RecordChanged) as exc:
        return run.outcome(FileResult.UNCERTAIN, f"the file reads back as {_STATE_TEXT[state]}, but {run.on_disk(exc)}")
    result = result_for(final, state)
    if result is FileResult.UPGRADED:
        reason = "upgraded to format 2" + (f" (the replace reported: {_text(replace_error)})" if replace_error else "")
    elif replace_error is not None:
        reason = f"the replace failed ({_text(replace_error)}); {final.error}"
    else:
        reason = final.error or result.value
    if result is not FileResult.UPGRADED and state is DiskState.SOURCE:
        run.ids = ()  # nothing was committed
    return run.outcome(result, reason)
