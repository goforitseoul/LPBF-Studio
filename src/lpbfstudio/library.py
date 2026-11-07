"""Folder-backed exposure-set library (one JSON file per set).

The set writers (``save``, ``copy``, ``rename``, ``delete``, ``import_file``, ``ensure_defaults``)
take the writer locks: a shared lock on the sets folder, then a shared lock on the
migration folder if it exists, held for the whole read-check-write sequence. A writer that cannot
take them refuses with ``LibraryLocked`` and writes nothing. Reading takes no lock. Process and
machine files are outside the sets folder and take none.

The migration folder is checked again just before each ``os.replace``: the one replace of ``save``,
``copy``, ``import_file`` and of ``rename``'s new name, and every replace of ``ensure_defaults``,
which stops at the first refusal and keeps the complete defaults already replaced. There is no such
check before ``delete``'s unlink or before ``rename`` removes the old name.

**Platform rule.** Where ``fcntl.flock`` does not exist (Windows), the set writers stay unlocked, by
this explicit rule: no upgrade, report, apply or restore can run there, so a save races none of them.
Two processes saving at once are not coordinated there. Everywhere else every failure refuses: a
file system that cannot lock never falls back to an unlocked write.
"""

from __future__ import annotations

import contextlib
import contextvars
import errno
import json
import os
import re
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: the platform rule above
    fcntl = None  # type: ignore[assignment]

from .defaults import default_sets
from .locks import RESULT, Held, LockRefused, Refusal, Step, acquire_writer, open_sets
from .models import (
    ExposureSet, FileFormatError, MachineProfile, PassIdError, ProcessSettings, check_schema_version,
    identity_problem, pass_id_problem,
)
from .validation import Issue, validate_set

ENV_VAR = "LPBF_LIBRARY"


class LibraryError(Exception):
    pass


class LibraryLocked(LibraryError):
    """A set writer was refused by the library locks; nothing was saved. ``refusal`` and ``result``
    are the D-1 classification."""

    def __init__(self, message: str, refusal: Refusal) -> None:
        super().__init__(message)
        self.refusal = refusal

    @property
    def result(self) -> str:
        return RESULT[self.refusal]


_BUSY_TEXT = "an upgrade, report, apply or restore is in progress for this library; nothing was saved"
_CANNOT_LOCK_TEXT = "this library's file system cannot lock; nothing was saved"
_ABANDONED_TEXT = "an upgrade started while saving; nothing was saved"
_CANNOT_LOCK_ERRNOS = frozenset({errno.ENOLCK, errno.EOPNOTSUPP, errno.EINVAL})

# The re-check of the writer that is running, called just before its final change.
_FINAL_CHECK: contextvars.ContextVar[Callable[[], None] | None] = contextvars.ContextVar(
    "lpbf_library_final_check", default=None)


def _flock_exists() -> bool:
    """Whether this platform has ``flock`` at all. False selects the platform rule above; it is the
    only condition that does, so a lock failure where ``flock`` exists can never select it."""
    return fcntl is not None and hasattr(fcntl, "flock")


def _refusal(exc: LockRefused) -> LibraryLocked:
    if exc.refusal is Refusal.BUSY:
        text = _BUSY_TEXT
    elif exc.errno in _CANNOT_LOCK_ERRNOS and exc.step in (Step.SETS_LOCK, Step.M_LOCK):
        text = _CANNOT_LOCK_TEXT
    else:
        text = f"{exc.detail}; nothing was saved"
    return LibraryLocked(text, exc.refusal)


def default_library_path() -> Path:
    env = os.environ.get(ENV_VAR)
    return Path(env).expanduser() if env else Path.home() / ".lpbfstudio" / "library"


def _slug(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.+-]+", "_", name).strip("_")
    if not slug:
        raise LibraryError("name must contain letters or digits")
    return slug


_TMP_SUFFIX = ".tmp"
_TMP_RANDOM_CHARS = 8  # length of the random part tempfile.mkstemp adds (CPython)
_FALLBACK_NAME_MAX = 255  # only when the platform cannot report its limit


def _tmp_prefix(path: Path) -> str:
    return f".{path.name}."


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temporary file in the same folder and ``os.replace``, so a failed write
    never leaves a truncated file behind. A locked writer's re-check runs just before the replace;
    if it refuses, only this write's own temporary file is removed, identified by its inode."""
    fd, tmp = tempfile.mkstemp(prefix=_tmp_prefix(path), suffix=_TMP_SUFFIX, dir=path.parent)
    check = _FINAL_CHECK.get()
    own = os.fstat(fd) if check is not None else None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if check is not None:
            check()
        os.replace(tmp, path)
    except LibraryLocked as exc:
        left = _remove_own(tmp, own)
        if left:
            exc.args = (f"{exc}; {left}",)
        raise
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _atomic_write_all(items: list[tuple[Path, str]]) -> None:
    """Several new files, all staged first. A locked writer re-checks once after staging and again
    just before every replace. A refusal before the first replace leaves none of them created; a
    later one stops the batch and keeps the complete files already replaced (no rollback). Either
    way only this batch's own remaining temporary files are removed."""
    staged: list[tuple[str, os.stat_result, Path]] = []
    check = _FINAL_CHECK.get()
    created = 0
    try:
        for path, text in items:
            fd, tmp = tempfile.mkstemp(prefix=_tmp_prefix(path), suffix=_TMP_SUFFIX, dir=path.parent)
            staged.append((tmp, os.fstat(fd), path))
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
        if check is not None:
            check()
        while staged:
            tmp, _, path = staged[0]
            if check is not None:
                check()  # rev5 §2.2 step 3, before each replace (B2a follow-up of 2026-11-07)
            os.replace(tmp, path)
            staged.pop(0)
            created += 1
    except LibraryLocked as exc:
        left = [text for text in (_remove_own(tmp, own) for tmp, own, _ in staged) if text]
        if created:
            left.append(f"{created} of {len(items)} files were already complete and stay in place")
        if left:
            exc.args = ("; ".join([str(exc), *left]),)
        raise
    except BaseException:
        for tmp, _, _ in staged:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        raise


def _remove_own(tmp: str, own: os.stat_result) -> str | None:
    """Remove ``tmp`` if it is still the file ``own`` describes; what stays on disk, or None."""
    name = os.path.basename(tmp)
    try:
        st = os.lstat(tmp)
        if (st.st_dev, st.st_ino) != (own.st_dev, own.st_ino):
            return f"{name} is no longer this save's temporary file and was left in place"
        os.unlink(tmp)
    except OSError as exc:
        return f"its temporary file {name} could not be removed ({exc.strerror or exc})"
    return None


def _pathconf(folder: Path, key: str) -> int | None:
    try:
        value = os.pathconf(folder, key)
    except (AttributeError, OSError, ValueError):  # not available on this platform or file system
        return None
    return value if value and value > 0 else None


def _file_name_problem(f: Path) -> str | None:
    """Why ``f`` cannot be written by ``_atomic_write`` on this platform, as far as it reports
    its limits; None when no limit is exceeded. Saving still handles errors at run time."""
    reserved = getattr(os.path, "isreserved", None)  # the platform's own rule (Windows)
    if reserved is not None and reserved(f.name):
        return f"file name {f.name} is reserved on this platform"
    name_max = _pathconf(f.parent, "PC_NAME_MAX") or _FALLBACK_NAME_MAX
    size = len(os.fsencode(f.name))
    tmp_size = len(os.fsencode(_tmp_prefix(f))) + _TMP_RANDOM_CHARS + len(_TMP_SUFFIX)
    if max(size, tmp_size) > name_max:
        return (f"file name {f.name[:40]}… is too long for this file system: {size} bytes, plus a "
                f"temporary file of {tmp_size} bytes while saving; the limit is {name_max}")
    path_max = _pathconf(f.parent, "PC_PATH_MAX")
    tmp_path = len(os.fsencode(str(f.parent.resolve()))) + 1 + tmp_size
    if path_max is not None and tmp_path >= path_max:
        return f"the path of {f.name[:40]}… is too long for this file system ({tmp_path} bytes, limit {path_max})"
    return None


def _check_pass_ids(data: dict, version: int) -> None:
    """Raise PassIdError when the contour-pass ids in raw set data do not fit ``version``: format 1
    files hold no id key, format 2 files a valid, unique id on every pass. Checked before the model
    so the message names the pass; anything else malformed is left to the model."""
    types = data.get("exposure_types")
    for et in types if isinstance(types, list) else ():
        passes = et.get("passes") if isinstance(et, dict) and et.get("kind") == "contour" else None
        if isinstance(passes, list) and all(isinstance(p, dict) for p in passes):
            problem = pass_id_problem([("id" in p, p.get("id"), p.get("name")) for p in passes], version)
            if problem:
                raise PassIdError(problem)


def _parse_set(text: str) -> ExposureSet:
    """Read an exposure set from JSON text, checking its schema_version before any other field
    so a refused file is reported by its version, not by fields a newer format may have changed,
    then its contour-pass ids. A missing key means version 1 (legacy policy, docs/file_format.md).
    Never writes anything."""
    try:
        data = json.loads(text)
    except ValueError:
        data = None  # malformed JSON: reported by the model below, exactly as before the guard
    if isinstance(data, dict):
        version = check_schema_version(data["schema_version"]) if "schema_version" in data else 1
        _check_pass_ids(data, version)
    return ExposureSet.model_validate_json(text)


def _final_check(held: Held) -> None:
    """Revision 5 section 2.2 step 3: M may have appeared since the locks were taken, and the folders
    must still be the locked ones. Any refusal abandons the write."""
    try:
        held.recheck_m()
        held.verify()
    except LockRefused as exc:
        raise LibraryLocked(_ABANDONED_TEXT, exc.refusal) from exc


class Library:
    def __init__(self, root: Path | str | None = None, seed: bool = True) -> None:
        self.root = Path(root) if root else default_library_path()
        self.sets_dir = self.root / "sets"
        self.sets_dir.mkdir(parents=True, exist_ok=True)
        self.machines_dir = self.root / "machines"
        self.load_errors: list[tuple[Path, str]] = []
        self.machine_errors: list[tuple[Path, str]] = []
        self.unreadable_machines: list[str] = []
        if seed:
            try:
                self.ensure_defaults()
            except LibraryLocked as exc:
                if exc.refusal is not Refusal.BUSY:
                    raise  # only contention skips seeding: reading must go on during an upgrade
            self.ensure_default_machine()

    # ------------------------------------------------------------------ sets
    #
    # A set is stored in ``sets/<slug(name)>.json``. Different names can share a slug
    # ("My Part" and "My_Part"), and a file can hold a name that does not match its file
    # name (manual edits). Every write therefore checks which set actually owns the target
    # file and refuses to replace a different set.

    @contextlib.contextmanager
    def _writer_locks(self) -> Iterator[None]:
        """Hold the writer locks for one read-check-write sequence, or refuse before it starts."""
        if not _flock_exists():  # the platform rule in the module docstring
            yield
            return
        try:
            sets_fd = open_sets(self.sets_dir)
            try:
                with acquire_writer(self.sets_dir, sets_fd) as held:
                    token = _FINAL_CHECK.set(lambda: _final_check(held))
                    try:
                        yield
                    finally:
                        _FINAL_CHECK.reset(token)
            finally:
                with contextlib.suppress(OSError):  # never replaces the writer's own outcome
                    os.close(sets_fd)  # also frees the sets lock if its LOCK_UN failed
        except LockRefused as exc:
            raise _refusal(exc) from exc

    def _file(self, name: str) -> Path:
        return self.sets_dir / f"{_slug(name)}.json"

    @staticmethod
    def _read(f: Path) -> ExposureSet:
        return _parse_set(f.read_text(encoding="utf-8"))

    def _locate(self, name: str) -> Path | None:
        """The file that holds the set called ``name``, or None."""
        direct = self._file(name)
        candidates = [direct] + [f for f in sorted(self.sets_dir.glob("*.json")) if f != direct]
        for f in candidates:
            if not f.exists():
                continue
            try:
                if self._read(f).name == name:
                    return f
            except (OSError, ValueError):
                continue
        return None

    def _conflict(self, name: str, f: Path) -> str:
        try:
            other = self._read(f).name
        except (OSError, ValueError):
            return f"{f.name} exists but cannot be read; refusing to overwrite it with {name!r}"
        return f"{name!r} would be stored in {f.name}, which holds the set {other!r}; choose another name"

    def _check_free(self, name: str, ignore: Path | None = None) -> None:
        """Raise unless ``name`` can be stored without touching another set's file."""
        holder = self._locate(name)
        if holder is not None and holder != ignore:
            raise LibraryError(f"{name!r} already exists")
        f = self._file(name)
        if f.exists() and f != ignore:
            raise LibraryError(self._conflict(name, f))

    def ensure_defaults(self) -> None:
        sets = default_sets()
        if all(self._file(es.name).exists() for es in sets):
            return  # nothing to write, so no lock: a read-only start never waits on or meets one
        with self._writer_locks():
            _atomic_write_all([(self._file(es.name), self._text(es)) for es in sets
                               if not self._file(es.name).exists()])

    def names(self) -> list[str]:
        return sorted(self.load_all(), key=str.lower)

    def load_all(self) -> dict[str, ExposureSet]:
        """All readable sets by name. Unreadable files and duplicate names are skipped and
        listed in ``load_errors`` as (path, reason)."""
        out: dict[str, ExposureSet] = {}
        source: dict[str, Path] = {}
        self.load_errors = []
        for f in sorted(self.sets_dir.glob("*.json")):
            try:
                es = self._read(f)
            except (OSError, ValueError) as exc:
                self.load_errors.append((f, f"cannot be read: {str(exc).splitlines()[0]}"))
                continue
            if es.name in out:
                self.load_errors.append((f, f"duplicate of {es.name!r} in {source[es.name].name}; ignored"))
                continue
            out[es.name] = es
            source[es.name] = f
        return out

    def exists(self, name: str) -> bool:
        """True when the name is taken: a set has this name, or its file name is in use."""
        return self._locate(name) is not None or self._file(name).exists()

    def check_new_names(self, names: list[str]) -> list[str]:
        """Problems that would stop saving all of ``names`` as new sets: names that are taken
        (as by ``exists``), names without a usable file name (no letters or digits, too long or
        reserved on this platform, or a file that cannot be checked), and names in the list
        that would share a file (compared ignoring case, for case-insensitive file systems).
        Empty when every name is free.

        This is a check before saving, not a reservation: another process can still take a
        name, or the disk can fail, while the batch is being saved."""
        taken = set(self.load_all())
        problems: list[str] = []
        first_by_file: dict[str, str] = {}
        for name in names:
            try:
                f = self._file(name)
            except LibraryError as exc:
                problems.append(f"{name!r}: {exc}")
                continue
            unusable = _file_name_problem(f)
            if unusable:
                problems.append(f"{name[:40]!r}: {unusable}")
            elif name in taken:
                problems.append(f"{name!r} already exists")
            else:
                try:
                    in_use = f.exists()
                except OSError as exc:
                    problems.append(f"{name!r}: {f.name} cannot be checked ({exc.strerror or exc})")
                    in_use = False
                if in_use:
                    problems.append(self._conflict(name, f))
            other = first_by_file.setdefault(f.name.casefold(), name)
            if other != name:
                problems.append(f"{other!r} and {name!r} would both be stored in {f.name}")
        return problems

    def get(self, name: str) -> ExposureSet:
        f = self._locate(name)
        if f is not None:
            return self._read(f)
        direct = self._file(name)
        if direct.exists():
            try:
                self._read(direct)
            except (OSError, ValueError) as exc:
                raise LibraryError(f"{direct.name} cannot be read: {str(exc).splitlines()[0]}") from exc
        raise LibraryError(f"no exposure set named {name!r}")

    def _write(self, es: ExposureSet, f: Path) -> None:
        _atomic_write(f, self._text(es))

    @staticmethod
    def _text(es: ExposureSet) -> str:
        changed = identity_problem(es)  # read-only fields changed by bypassing validation
        if changed:
            raise LibraryError(f"{es.name!r} was not saved: {changed}")
        text = es.model_dump_json(indent=2)
        try:  # never write a file that this build would refuse to read back (format and ids)
            _parse_set(text)
        except ValueError as exc:
            errors = getattr(exc, "errors", None)
            reason = errors()[0]["msg"] if callable(errors) and errors() else str(exc).splitlines()[0]
            raise LibraryError(f"{es.name!r} was not saved: {reason}") from exc
        return text

    def save(self, es: ExposureSet, overwrite: bool = True) -> None:
        with self._writer_locks():
            f = self._locate(es.name)
            if f is not None:
                current = self._read(f)
                if current.locked:
                    raise LibraryError(f"{es.name!r} is locked; copy it to make changes")
                if not overwrite:
                    raise LibraryError(f"{es.name!r} already exists")
                if es.schema_version != current.schema_version:
                    raise LibraryError(
                        f"{es.name!r} was not saved: its file is format {current.schema_version} and this set "
                        f"is format {es.schema_version}; the file format changes only through an explicit upgrade")
            else:
                f = self._file(es.name)
                if f.exists():
                    raise LibraryError(self._conflict(es.name, f))
            self._write(es, f)

    def copy(self, source: str, new_name: str) -> ExposureSet:
        with self._writer_locks():
            self._check_free(new_name)
            es = self.get(source).model_copy(deep=True)
            es.name = new_name
            es.locked = False
            if es.description and not es.description.startswith("Copy of"):
                es.description = f"Copy of {source}. {es.description}"
            self._write(es, self._file(new_name))
            return es

    def rename(self, old: str, new: str) -> ExposureSet:
        with self._writer_locks():
            src = self._locate(old)
            if src is None:
                raise LibraryError(f"no exposure set named {old!r}")
            es = self._read(src)
            if es.locked:
                raise LibraryError(f"{old!r} is locked")
            if new == old:
                raise LibraryError(f"{new!r} already exists")
            self._check_free(new, ignore=src)
            es.name = new
            dst = self._file(new)
            self._write(es, dst)
            if dst != src:
                try:
                    src.unlink()
                except OSError as exc:
                    dst.unlink(missing_ok=True)
                    raise LibraryError(f"cannot rename {old!r}: {exc}") from exc
            return es

    def delete(self, name: str) -> None:
        with self._writer_locks():
            f = self._locate(name)
            if f is None:
                raise LibraryError(f"no exposure set named {name!r}")
            if self._read(f).locked:
                raise LibraryError(f"{name!r} is locked and cannot be deleted")
            f.unlink()

    def unique_name(self, base: str) -> str:
        if not self.exists(base):
            return base
        i = 2
        while self.exists(f"{base}_{i}"):
            i += 1
        return f"{base}_{i}"

    def import_file(self, path: Path | str, overwrite: bool = False) -> ExposureSet:
        path = Path(path)
        try:
            es = _parse_set(path.read_text(encoding="utf-8"))
        except FileFormatError as exc:
            raise LibraryError(f"{path.name} was not imported: {exc}") from exc
        es.locked = False
        self.save(es, overwrite=overwrite)
        return es

    # ------------------------------------------------------------------ settings

    def load_process(self) -> ProcessSettings:
        f = self.root / "process.json"
        return ProcessSettings.model_validate_json(f.read_text(encoding="utf-8")) if f.exists() else ProcessSettings()

    def save_process(self, ps: ProcessSettings) -> None:
        _atomic_write(self.root / "process.json", ps.model_dump_json(indent=2))

    # ------------------------------------------------------------------ machine profiles
    #
    # Profiles live in ``machines/<slug(name)>.json``; sets refer to them by name. A 0.1.0
    # library has a single ``machine.json`` instead: it is read as one more profile, written
    # back in place when that profile is saved, and never moved or deleted. Only a library with
    # neither gets the generic example profile, which matches the 0.1.0 built-in default.

    @property
    def legacy_machine_file(self) -> Path:
        return self.root / "machine.json"

    def ensure_default_machine(self) -> None:
        if not self.machines_dir.exists() and not self.legacy_machine_file.exists():
            self.machines_dir.mkdir(parents=True)
            mp = MachineProfile()
            _atomic_write(self.machines_dir / f"{_slug(mp.name)}.json", mp.model_dump_json(indent=2))

    def load_machines(self) -> dict[str, MachineProfile]:
        """All readable profiles by name. Unreadable files and shadowed duplicates are listed in
        ``machine_errors`` as (path, reason)."""
        out: dict[str, MachineProfile] = {}
        self._machine_source: dict[str, Path] = {}
        self.machine_errors = []
        self.unreadable_machines: list[str] = []
        files = sorted(self.machines_dir.glob("*.json")) if self.machines_dir.exists() else []
        if self.legacy_machine_file.exists():
            files.append(self.legacy_machine_file)
        for f in files:
            try:
                mp = MachineProfile.model_validate_json(f.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                self.machine_errors.append((f, f"cannot be read: {str(exc).splitlines()[0]}"))
                self.unreadable_machines.append(f.relative_to(self.root).as_posix())
                continue
            if mp.name in out:
                self.machine_errors.append(
                    (f, f"duplicate of profile {mp.name!r} in {self._machine_source[mp.name].name}; ignored"))
                continue
            out[mp.name] = mp
            self._machine_source[mp.name] = f
        return out

    def get_machine(self, name: str) -> MachineProfile:
        profiles = self.load_machines()
        if name not in profiles:
            raise LibraryError(f"no machine profile named {name!r}")
        return profiles[name]

    def save_machine(self, mp: MachineProfile, overwrite: bool = True) -> None:
        """Save a profile to the file that already holds it, or to a new file in ``machines/``."""
        self.load_machines()
        f = self._machine_source.get(mp.name)
        if f is not None:
            if not overwrite:
                raise LibraryError(f"machine profile {mp.name!r} already exists")
        else:
            f = self.machines_dir / f"{_slug(mp.name)}.json"
            if f.exists():
                try:
                    other = MachineProfile.model_validate_json(f.read_text(encoding="utf-8")).name
                    reason = f"which holds the profile {other!r}"
                except (OSError, ValueError):
                    reason = "which cannot be read"
                raise LibraryError(f"{mp.name!r} would be stored in {f.name}, {reason}; choose another name")
            self.machines_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(f, mp.model_dump_json(indent=2))

    def import_machine(self, path: Path | str, overwrite: bool = False) -> MachineProfile:
        mp = MachineProfile.model_validate_json(Path(path).read_text(encoding="utf-8"))
        self.save_machine(mp, overwrite=overwrite)
        return mp

    def validate(self, es: ExposureSet, process: ProcessSettings | None = None) -> list[Issue]:
        """Validate a set against the machine profile it names (see ``validation.validate_set``)."""
        profiles = self.load_machines()
        return validate_set(es, profiles, self.load_process() if process is None else process,
                            unreadable=self.unreadable_machines)


def export_json(es: ExposureSet, path: Path | str) -> None:
    Path(path).write_text(es.model_dump_json(indent=2), encoding="utf-8")


def load_json(path: Path | str) -> ExposureSet:
    return _parse_set(Path(path).read_text(encoding="utf-8"))
