"""Read-only reconciliation reports for format 1 -> format 2 upgrades (B7 Phase 2, S3-B2a).

``reconcile_file`` and ``reconcile_library`` read the sets folder, the migration folder and the
migration records, and classify each set file from what the disk shows. They follow the decision
table of the S3-B2 design report: the global rules G1-G5, then the per-file rules P1-P17, first
match wins. Every outcome names the rule that decided it and gives a reason.

This stage is **report only**. Nothing is created, written, moved, renamed or deleted: not the
migration folder, not a record, not a temporary file. ``apply=True`` is refused before anything
is read. A ``recoverable`` outcome names, in ``planned_status``, the record state that a later,
separately approved stage would write; nothing is written here.

Decisions come only from the disk, read under the library upgrade lock (an exclusive ``flock``
on the migration folder, the same lock that ``upgrade.upgrade_file`` holds). The lock is taken
on the existing folder and never creates it; when there is no migration folder there is no
evidence of any upgrade and nothing is locked (G2). Record texts and earlier outcome messages
are never trusted: a record is judged by its status and digests against the files.

Upgrade residue without a record (a staged upgrade file, or a record temporary file whose record
is missing) proves that a record existed: such a file is a conflict, never a verified original,
and every staged file is reported, also when its set has no file, record or backup. Reasons say
what was observed and, separately, what is inferred from it.

Files are read through folder descriptors without following links (``upgrade._read_file``);
temporary files are hashed in chunks, and stray entries are only ``lstat``-ed, never read.
Links, folders, FIFOs and other non-regular files, and files with more than one hard link, are
refused (never read as evidence). Unexpected entries are listed; any unexpected entry in the
records folder, or an unsafe temporary file of the set, keeps an interrupted run from being
called recoverable.

**Restore evidence (S3-B2c-a).** A future Restore (S3-B2c-b, not implemented) keeps one attempt
folder per attempt in ``.format1-backup/restores/<restore_id>/`` (``restore_record``). The report
reads it under the same lock, never creating it. An empty or absent ``restores`` folder changes
nothing (C-1). Any unsafe, unreadable or unattributable restore evidence, or any unknown entry in
``restores`` or in an attempt folder, blocks the whole library (G6, after G5). A restore record
that refers to a file's ``upgraded`` record means P12-P15 are never reached for that file; the
reference proves nothing by itself, and the files decide: P18-P22 and P24 (P23 is retired, its
case is G6). P18-P20 are ``recoverable`` with a report-only ``restore_planned_status``
(``RESTORE_PLANNED``); finalising restore records is not available (D-6 deferred), and apply
writes nothing for them. Frozen design: ``docs/audit/b7p2_s3b2c/design/report_rev5.md``.

Supported where ``upgrade_file`` is: local Linux file systems were tested. Platforms without the
primitives (notably Windows) are refused before anything is read (G1). No claim is made for
macOS, network file systems, FUSE or cloud-synchronised folders.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable

from . import restore_record as rr
from . import upgrade as up
from .library import _parse_set
from .migration import (
    SOURCE_SCHEMA_VERSION, TARGET_SCHEMA_VERSION, MigrationRecord, RecordError, RecordStatus, parse_record,
)
from .models import ExposureSet
from .restore_record import DisplacedObservation, RestoreRecord, RestoreRecordError, RestoreStatus, parse_restore_record

M, R = up.MIGRATION_DIR, up.RECORDS_DIR
RS = rr.RESTORES_DIR
_RESTORE_ID = re.compile(r"^[0-9a-f]{32}$")
_STAGED = re.compile(r"^\.(?P<file>.+)\.[0-9a-f]{12}" + re.escape(up.TMP_SUFFIX) + r"$")
_RECORD_TMP = re.compile(r"^\.(?P<id>[0-9a-f]{32})\.[0-9a-f]{12}\.tmp$")


class Outcome(str, Enum):
    VERIFIED_UPGRADED = "verified_upgraded"    # an upgraded record that the disk confirms
    VERIFIED_ORIGINAL = "verified_original"    # format 1, untouched by any upgrade the records show
    VERIFIED_RESTORED = "verified_restored"    # a restore record whose evidence the disk confirms (P20R, P22)
    FORMAT_2_NO_RECORD = "format_2_no_record"  # format 2 with no migration evidence (informational)
    RECOVERABLE = "recoverable"                # an interrupted run whose state the disk proves
    CONFLICT = "conflict"                      # needs the user; nothing would be written
    UNVERIFIABLE = "unverifiable"              # evidence that cannot be read or attributed safely
    BUSY = "busy"                              # another upgrade or reconciliation holds the lock


@dataclass(frozen=True)
class FileEvidence:
    """One entry as read: ``kind`` is missing, regular, symlink, directory, fifo, socket, device,
    other or unreadable. Size and SHA-256 are given only for a regular file that was read."""
    path: str
    kind: str
    size: int | None = None
    sha256: str | None = None
    links: int | None = None
    error: str | None = None

    @property
    def safe(self) -> bool:
        """A regular file with one link that was read without error."""
        return self.kind == "regular" and self.links == 1 and self.error is None


@dataclass(frozen=True)
class ReconcileOutcome:
    """The report for one set file. ``record_id``/``record_status`` are those of the record the
    rule judged (None when no single record decided); ``records`` lists every record id for the
    file; ``wrote`` is always None in this report-only stage.

    Restore evidence (S3-B2c-a; all None or empty when no restore record refers to the file's
    upgrade record): ``restores`` lists every restore id that refers to it; ``restore_id`` and
    ``restore_status`` are those of the restore record that decided (None when no single one
    did); ``restore_planned_status`` is report-only, the restore record status that a later,
    separately approved stage (D-6, deferred) would write for P18-P20; it is not an apply
    instruction and nothing writes it. ``preserved`` and ``swap`` are the files read in the
    attempt folder; ``displaced_observed_at_restore`` is the record's observation and
    ``displaced_observed_at_report`` this report's own, never stored."""
    file_name: str
    outcome: Outcome
    rule: str
    reason: str
    record_id: str | None = None
    record_status: RecordStatus | None = None
    planned_status: RecordStatus | None = None
    records: tuple[str, ...] = ()
    source: FileEvidence | None = None
    backup: FileEvidence | None = None
    temps: tuple[FileEvidence, ...] = ()
    unexpected: tuple[FileEvidence, ...] = ()
    wrote: Path | None = None
    restore_id: str | None = None
    restore_status: RestoreStatus | None = None
    restore_planned_status: RestoreStatus | None = None
    restores: tuple[str, ...] = ()
    preserved: FileEvidence | None = None
    swap: FileEvidence | None = None
    displaced_observed_at_restore: DisplacedObservation | None = None
    displaced_observed_at_report: DisplacedObservation | None = None


# the restore record status each recoverable restore rule would lead to, once D-6 is approved;
# report-only: nothing in this build writes a restore record
RESTORE_PLANNED = {"P18": RestoreStatus.FAILED, "P19": RestoreStatus.FAILED, "P20": RestoreStatus.RESTORED}


@dataclass(frozen=True)
class LibraryReport:
    """The report for a whole sets folder. ``blocked`` is set when a global rule (G1, G3-G6)
    decided for every file; then ``outcomes`` is empty."""
    blocked: Outcome | None
    rule: str | None
    reason: str
    outcomes: tuple[ReconcileOutcome, ...] = ()
    unexpected: tuple[FileEvidence, ...] = ()

    def get(self, file_name: str) -> ReconcileOutcome:
        for o in self.outcomes:
            if o.file_name == file_name:
                return o
        raise KeyError(file_name)


@dataclass(frozen=True)
class _Block:
    outcome: Outcome
    rule: str
    reason: str


@dataclass
class _Survey:
    base: Path
    migration: bool                    # the migration folder exists (and is locked)
    sets: list[str] = field(default_factory=list)
    backups: list[str] = field(default_factory=list)
    records: dict[str, list[MigrationRecord]] = field(default_factory=dict)
    staged: dict[str, list[str]] = field(default_factory=dict)        # set file -> staged temp names
    record_tmps: dict[str, list[str]] = field(default_factory=dict)   # record id -> temp names
    unexpected: list[FileEvidence] = field(default_factory=list)
    unexpected_in_records: bool = False
    orphan_record_tmps: list[str] = field(default_factory=list)       # record temps whose record is missing
    restores: dict[str, list[RestoreRecord]] = field(default_factory=dict)  # upgrade record id -> restore records


def _refuse_apply(apply: bool) -> None:
    if apply:
        raise ValueError("applying reconciliation is not available in this build (S3-B2a reports only); "
                         "nothing was read or written")


def _platform_block() -> _Block | None:
    problem = up._platform_problem()
    if problem:
        return _Block(Outcome.UNVERIFIABLE, "G1", f"reconciliation is not supported on this platform: it {problem}; "
                                                  "nothing was read")
    return None


# --------------------------------------------------------------------------- reading evidence


def _kind(mode: int) -> str:
    for test, name in ((stat.S_ISREG, "regular"), (stat.S_ISLNK, "symlink"), (stat.S_ISDIR, "directory"),
                       (stat.S_ISFIFO, "fifo"), (stat.S_ISSOCK, "socket"), (stat.S_ISCHR, "device"),
                       (stat.S_ISBLK, "device")):
        if test(mode):
            return name
    return "other"


def _inspect(rel: str, path: Path) -> tuple[FileEvidence, bytes | None]:
    """What ``path`` holds, read through its open folder without following a link. The bytes are
    returned only for a regular file whose identity did not change between lstat and read."""
    try:
        st = up._lstat(path)
    except FileNotFoundError:
        return FileEvidence(rel, "missing"), None
    except OSError as exc:
        return FileEvidence(rel, "unreadable", error=up._text(exc)), None
    kind = _kind(st.st_mode)
    if kind != "regular":
        return FileEvidence(rel, kind, links=st.st_nlink), None
    try:
        data = up._read_file(path)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return FileEvidence(rel, "symlink"), None
        return FileEvidence(rel, "unreadable", links=st.st_nlink, error=up._text(exc)), None
    ctx = up._CTX.get()
    if ctx is None or ctx.ident.get(path) != (st.st_dev, st.st_ino):
        return FileEvidence(rel, "unreadable", error="it changed while it was being read"), None
    return FileEvidence(rel, "regular", len(data), hashlib.sha256(data).hexdigest(), st.st_nlink), data


def _stat_only(rel: str, path: Path) -> FileEvidence:
    """What ``path`` is, from ``lstat`` alone: for entries that never decide anything (stray
    entries), so that they are listed without being opened or read, whatever their size."""
    try:
        st = up._lstat(path)
    except FileNotFoundError:
        return FileEvidence(rel, "missing")
    except OSError as exc:
        return FileEvidence(rel, "unreadable", error=up._text(exc))
    return FileEvidence(rel, _kind(st.st_mode), size=st.st_size, links=st.st_nlink)


def _inspect_streamed(rel: str, path: Path) -> FileEvidence:
    """``_inspect`` for temporary files, which are reported but never parsed: the SHA-256 is taken
    in chunks, so a large file is never held in memory. Opened like ``upgrade._read_file``
    (folder descriptor, no link followed, non-blocking, fstat must show the same regular file)."""
    try:
        st = up._lstat(path)
    except FileNotFoundError:
        return FileEvidence(rel, "missing")
    except OSError as exc:
        return FileEvidence(rel, "unreadable", error=up._text(exc))
    kind = _kind(st.st_mode)
    if kind != "regular":
        return FileEvidence(rel, kind, size=st.st_size, links=st.st_nlink)
    digest, size = hashlib.sha256(), 0
    try:
        with up._in_dir(path) as dfd:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | up._CLOEXEC, dir_fd=dfd)
            try:
                fst = os.fstat(fd)
                if not stat.S_ISREG(fst.st_mode) or (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
                    return FileEvidence(rel, "unreadable", error="it changed while it was being read")
                while True:
                    chunk = os.read(fd, 1 << 20)
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
            finally:
                os.close(fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return FileEvidence(rel, "symlink")
        return FileEvidence(rel, "unreadable", links=st.st_nlink, error=up._text(exc))
    return FileEvidence(rel, "regular", size, digest.hexdigest(), st.st_nlink)


def _unsafe(ev: FileEvidence, what: str) -> str | None:
    """Why ``ev`` cannot be used as evidence; None if it is a safe regular file or missing."""
    if ev.kind == "missing":
        return None
    if ev.kind == "symlink":
        return f"{what} is a link; links are not followed"
    if ev.kind == "unreadable":
        return f"{what} cannot be read: {ev.error}"
    if ev.kind != "regular":
        return f"{what} is not a regular file ({ev.kind})"
    if ev.links != 1:
        return f"{what} has {ev.links} hard links; hard-linked files are refused"
    return None


def _parse(data: bytes | None) -> tuple[ExposureSet | None, str]:
    if data is None:
        return None, "it was not read"
    try:
        return _parse_set(data.decode("utf-8")), ""
    except (UnicodeDecodeError, ValueError) as exc:
        return None, str(exc).splitlines()[0] if str(exc) else type(exc).__name__


# --------------------------------------------------------------------------- the survey


def _survey(ctx, sets_dir: Path | str) -> _Survey | _Block:
    """Open the sets folder, check the migration folders, take the lock, and read every record.
    Reads only; the caller's context closes every descriptor (releasing the lock)."""
    base = Path(os.path.realpath(sets_dir))
    try:
        base_fd = up._open_base(ctx, base)
        names = sorted(os.listdir(base_fd))
    except OSError as exc:
        return _Block(Outcome.UNVERIFIABLE, "G3", f"the sets folder cannot be opened or listed: {up._text(exc)}; "
                                                  "nothing was read")
    mdir, rdir = base / M, base / M / R
    problem = up._folder_problem(mdir)
    if problem:
        return _Block(Outcome.UNVERIFIABLE, "G3", f"{problem}; no migration evidence can be trusted")
    sv = _Survey(base, migration=M in names and up._exists(mdir))
    for name in names:
        if name == M:
            continue
        m = _STAGED.match(name)
        if up._NAME.match(name):
            sv.sets.append(name)
        elif m and up._NAME.match(m.group("file")):
            sv.staged.setdefault(m.group("file"), []).append(name)
        else:
            sv.unexpected.append(_stat_only(name, base / name))
    if not sv.migration:
        return sv  # G2: no evidence of any upgrade; nothing to lock

    try:
        mfd = up._open_sub(ctx, mdir)
    except OSError as exc:
        return _Block(Outcome.UNVERIFIABLE, "G3", f"the migration folder cannot be opened: {up._text(exc)}")
    try:
        up._flock(mfd, up.fcntl.LOCK_EX | up.fcntl.LOCK_NB)
    except BlockingIOError:
        return _Block(Outcome.BUSY, "G4", "another upgrade or reconciliation is using this library; "
                                          "no record was read")
    except OSError as exc:
        return _Block(Outcome.UNVERIFIABLE, "G4", f"the migration folder cannot be locked ({up._text(exc)})")

    # under the lock from here on
    problem = up._folder_problem(rdir)
    if problem:
        return _Block(Outcome.UNVERIFIABLE, "G3", f"{problem}; no migration evidence can be trusted")
    try:
        mnames = sorted(os.listdir(mfd))
    except OSError as exc:
        return _Block(Outcome.UNVERIFIABLE, "G3", f"the migration folder cannot be listed: {up._text(exc)}")
    for name in mnames:
        if name in (R, RS):  # the restores folder is surveyed below (G6), never listed as unexpected
            continue
        if up._NAME.match(name):
            sv.backups.append(name)
        else:
            sv.unexpected.append(_stat_only(f"{M}/{name}", mdir / name))
    if R not in mnames:
        return _survey_restores(ctx, sv, mnames)
    try:
        rfd = up._open_sub(ctx, rdir)
        rnames = sorted(os.listdir(rfd))
    except OSError as exc:
        return _Block(Outcome.UNVERIFIABLE, "G3", f"the records folder cannot be read: {up._text(exc)}")

    tmps: list[tuple[str, str]] = []
    for name in rnames:
        rel = f"{M}/{R}/{name}"
        if name.startswith("."):
            m = _RECORD_TMP.match(name)
            if m:
                tmps.append((m.group("id"), name))
            else:
                sv.unexpected.append(_stat_only(rel, rdir / name))
                sv.unexpected_in_records = True
            continue
        if not name.endswith(".json"):
            sv.unexpected.append(_stat_only(rel, rdir / name))
            sv.unexpected_in_records = True
            continue
        ev, data = _inspect(rel, rdir / name)
        problem = _unsafe(ev, f"the migration record {name}") if ev.kind != "missing" else \
            f"the migration record {name} disappeared while being read"
        if problem is None:
            try:
                rec = parse_record(data.decode("utf-8"))
            except UnicodeDecodeError as exc:
                problem = f"the migration record {name} cannot be read: {exc}"
            except RecordError as exc:
                problem = f"the migration record {name} cannot be used: {exc}"
            else:
                if name != f"{rec.record_id}.json":
                    problem = f"the migration record {name} holds record {rec.record_id}"
        if problem:
            return _Block(Outcome.UNVERIFIABLE, "G5", f"{problem}; it cannot be told which set it belongs to, so "
                                                      "no file can be judged")
        sv.records.setdefault(rec.source_path, []).append(rec)

    ids = {r.record_id for recs in sv.records.values() for r in recs}
    for rid, name in tmps:
        if rid in ids:
            sv.record_tmps.setdefault(rid, []).append(name)
        else:
            sv.unexpected.append(_inspect_streamed(f"{M}/{R}/{name}", rdir / name))
            sv.unexpected_in_records = True
            sv.orphan_record_tmps.append(f"{M}/{R}/{name}")
    return _survey_restores(ctx, sv, mnames)


def _g6(what: str) -> _Block:
    return _Block(Outcome.UNVERIFIABLE, "G6", f"observed: {what}; inferred: nothing; restore evidence that cannot be "
                                              "read or attributed safely blocks every file, so no file can be judged; "
                                              "nothing was touched")


def _survey_restores(ctx, sv: _Survey, mnames: list[str]) -> _Survey | _Block:
    """Read ``M/restores/`` under the lock (G6). Every attempt folder must hold exactly
    ``restore.json``, ``preserved/`` and ``swap/``, those subfolders at most the record's set file,
    and the record must match an ``upgraded`` upgrade record of the same file, backup and bytes."""
    if RS not in mnames:
        return sv
    rsdir = sv.base / M / RS
    rel_rs = f"{M}/{RS}"
    problem = up._folder_problem(rsdir)
    if problem:
        return _g6(f"{rel_rs}: {problem}")
    try:
        names = sorted(os.listdir(up._open_sub(ctx, rsdir)))
    except OSError as exc:
        return _g6(f"the restores folder {rel_rs} cannot be read: {up._text(exc)}")
    upgrades = {r.record_id: r for recs in sv.records.values() for r in recs}
    for name in names:
        rel = f"{rel_rs}/{name}"
        if not _RESTORE_ID.match(name):
            return _g6(f"an unknown entry {rel} in the restores folder")
        a = rsdir / name
        problem = up._folder_problem(a)
        if problem:
            return _g6(f"the attempt folder {rel}: {problem}")
        try:
            anames = sorted(os.listdir(up._open_sub(ctx, a)))
        except OSError as exc:
            return _g6(f"the attempt folder {rel} cannot be read: {up._text(exc)}")
        extra = [n for n in anames if n not in (rr.RECORD_NAME, rr.PRESERVED_DIR, rr.SWAP_DIR)]
        if extra:
            return _g6(f"unknown entries in the attempt folder {rel}: {', '.join(extra)}")
        if rr.RECORD_NAME not in anames:
            return _g6(f"the attempt folder {rel}, which is without a valid record (no {rr.RECORD_NAME})")
        ev, data = _inspect(f"{rel}/{rr.RECORD_NAME}", a / rr.RECORD_NAME)
        what = f"the restore record {rel}/{rr.RECORD_NAME}"
        problem = _unsafe(ev, what) if ev.kind != "missing" else f"{what} disappeared while being read"
        if problem is None:
            try:
                rec = parse_restore_record(data.decode("utf-8"))
            except UnicodeDecodeError as exc:
                problem = f"{what} cannot be read: {exc}"
            except RestoreRecordError as exc:
                problem = f"{what} cannot be used: {exc}"
            else:
                if rec.restore_id != name:
                    problem = f"{what} holds restore {rec.restore_id}"
        if problem:
            return _g6(problem)
        for sub in (rr.PRESERVED_DIR, rr.SWAP_DIR):
            if sub not in anames:
                return _g6(f"the attempt folder {rel} is missing its {sub} folder")
            problem = up._folder_problem(a / sub)
            if problem:
                return _g6(f"in the attempt folder {rel}, {problem}")
            try:
                inside = sorted(os.listdir(up._open_sub(ctx, a / sub)))
            except OSError as exc:
                return _g6(f"the folder {rel}/{sub} cannot be read: {up._text(exc)}")
            stray = [n for n in inside if n != rec.source_path]
            if stray:
                return _g6(f"unknown entries in {rel}/{sub}: {', '.join(stray)}")
        u = upgrades.get(rec.upgrade_record_id)
        if u is None:
            return _g6(f"{what} refers to the upgrade record {rec.upgrade_record_id}, which is missing")
        if u.status is not RecordStatus.UPGRADED:
            return _g6(f"{what} refers to the upgrade record {u.record_id}, which is {u.status.value}, not upgraded")
        for mine, theirs, value in (("source_path", "source_path", u.source_path),
                                    ("backup_path", "backup_path", u.backup_path),
                                    ("current_sha256", "upgraded_sha256", u.upgraded_sha256),
                                    ("current_size_bytes", "upgraded_size_bytes", u.upgraded_size_bytes),
                                    ("restored_sha256", "source_sha256", u.source_sha256),
                                    ("restored_size_bytes", "source_size_bytes", u.source_size_bytes)):
            if getattr(rec, mine) != value:
                return _g6(f"{what} has {mine} {getattr(rec, mine)!r}, but its upgrade record {u.record_id} has "
                           f"{theirs} {value!r}")
        sv.restores.setdefault(rec.upgrade_record_id, []).append(rec)
    return sv


# --------------------------------------------------------------------------- the decision table


def _digest(ev: FileEvidence) -> tuple[str | None, int | None]:
    return ev.sha256, ev.size


def _decide(sv: _Survey, file_name: str) -> ReconcileOutcome:
    base, mdir = sv.base, sv.base / M
    unexpected = tuple(sv.unexpected)

    def out(outcome: Outcome, rule: str, reason: str, **kw) -> ReconcileOutcome:
        return ReconcileOutcome(file_name, outcome, rule, reason, unexpected=unexpected, **kw)

    if not up._NAME.match(file_name):
        return out(Outcome.UNVERIFIABLE, "P1", f"{file_name!r} is not a plain set file name in the sets folder; "
                                                "its file and backup were not read")
    recs = sorted(sv.records.get(file_name, []), key=lambda r: (r.created_at, r.record_id))
    ids = tuple(r.record_id for r in recs)
    for r in recs:
        if r.backup_path != f"{M}/{file_name}":
            return out(Outcome.UNVERIFIABLE, "P1", f"record {r.record_id} names a backup path {r.backup_path!r} "
                                                    "that this build never writes", records=ids)

    source, sdata = _inspect(file_name, base / file_name)
    if sv.migration:
        backup, bdata = _inspect(f"{M}/{file_name}", mdir / file_name)
    else:
        backup, bdata = FileEvidence(f"{M}/{file_name}", "missing"), None
    temps: list[FileEvidence] = []
    for name in sv.staged.get(file_name, []):
        temps.append(_inspect_streamed(name, base / name))
    for rid in ids:
        for name in sv.record_tmps.get(rid, []):
            temps.append(_inspect_streamed(f"{M}/{R}/{name}", mdir / R / name))
    temps.sort(key=lambda e: e.path)
    ev = dict(records=ids, source=source, backup=backup, temps=tuple(temps))

    # P1: unsafe file types
    problem = _unsafe(source, "the file") or _unsafe(backup, f"the backup {M}/{file_name}")
    if problem:
        return out(Outcome.UNVERIFIABLE, "P1", f"{problem}; it was not used as evidence", **ev)

    # P2, P3: no record
    if not recs:
        if backup.kind != "missing":
            return out(Outcome.CONFLICT, "P3", f"a backup {M}/{file_name} exists without a migration record; "
                                                "it was not touched", **ev)
        rule = "P2" if sv.migration else "G2"
        staged_here = [e.path for e in temps]
        if staged_here or sv.orphan_record_tmps:
            seen = []
            if staged_here:
                seen.append(f"staged upgrade file(s) {', '.join(staged_here)}")
            if sv.orphan_record_tmps:
                seen.append(f"record temporary file(s) {', '.join(sv.orphan_record_tmps)} whose record is missing")
            where = "no migration record for this file" if sv.migration else "no migration folder"
            what = ("an upgrade of this file reached its staging step, which happens only after its record "
                    "is written" if staged_here else "a migration record existed and is now missing, possibly "
                    "this file's")
            return out(Outcome.CONFLICT, rule, f"observed: {'; '.join(seen)}, and {where}; inferred: {what}, so the "
                                               "record is missing and the file's state cannot be verified; nothing "
                                               "was touched and nothing would be recovered automatically", **ev)
        if source.kind == "missing":
            return out(Outcome.UNVERIFIABLE, "P2", "no such set file and no migration record", **ev)
        es, err = _parse(sdata)
        if es is None:
            return out(Outcome.UNVERIFIABLE, "P2", f"the file cannot be read as a set: {err}", **ev)
        where = "observed: no migration record and no upgrade files for this set" if sv.migration else \
            "observed: no migration folder and no upgrade files for this set"
        if es.schema_version == TARGET_SCHEMA_VERSION:
            return out(Outcome.FORMAT_2_NO_RECORD, rule, f"format 2; {where}", **ev)
        return out(Outcome.VERIFIED_ORIGINAL, rule, f"format {es.schema_version}; {where}", **ev)

    def is_source(r: MigrationRecord) -> bool:
        return source.safe and _digest(source) == (r.source_sha256, r.source_size_bytes)

    def is_upgraded_digest(r: MigrationRecord) -> bool:
        return source.safe and r.upgraded_sha256 is not None and \
            _digest(source) == (r.upgraded_sha256, r.upgraded_size_bytes)

    def backup_is_original(r: MigrationRecord) -> bool:
        return backup.safe and _digest(backup) == (r.source_sha256, r.source_size_bytes)

    def verifies_as_upgrade(r: MigrationRecord) -> str | None:
        """Why the file is not the verified format 2 upgrade of the backup; None if it is."""
        if not backup_is_original(r):
            return f"the backup {M}/{file_name} is missing or does not match the original bytes"
        es, err = _parse(sdata)
        if es is None or es.schema_version != TARGET_SCHEMA_VERSION:
            return f"the file does not parse as format 2 ({err or f'format {es.schema_version}'})"
        original, err = _parse(bdata)
        if original is None or original.schema_version != SOURCE_SCHEMA_VERSION:
            return f"the backup does not parse as format {SOURCE_SCHEMA_VERSION}"
        contour = es.get("contour")
        ids_in_file = tuple(p.id for p in contour.passes) if contour is not None else ()
        problem = up._target_problem(sdata, original, ids_in_file)
        return f"it does not verify against the backup: {problem}" if problem else None

    def blocked_by_unexpected() -> str | None:
        found = [e.path for e in temps if not e.safe]
        if sv.unexpected_in_records:
            found += [e.path for e in sv.unexpected if e.path.startswith(f"{M}/{R}/")]
        return ", ".join(found) if found else None

    def recoverable(rule: str, r: MigrationRecord, planned: RecordStatus, reason: str) -> ReconcileOutcome:
        found = blocked_by_unexpected()
        if found:
            return out(Outcome.CONFLICT, rule, f"{reason}, but unexpected entries were found ({found}); "
                                               "not recoverable automatically", record_id=r.record_id,
                       record_status=r.status, **ev)
        return out(Outcome.RECOVERABLE, rule, reason, record_id=r.record_id, record_status=r.status,
                   planned_status=planned, **ev)

    def state(r: MigrationRecord) -> str:
        if source.kind == "missing":
            return "missing"
        if is_source(r):
            return "the original bytes"
        if is_upgraded_digest(r):
            return "the upgraded bytes"
        return "different from both the original and the upgraded bytes"

    # P4: several records
    unresolved = [r for r in recs if r.status is not RecordStatus.FAILED]
    if len(unresolved) >= 2:
        statuses = ", ".join(r.status.value for r in unresolved)
        return out(Outcome.CONFLICT, "P4", f"{len(unresolved)} unresolved migration records ({statuses}) for this "
                                           "file; the order of the attempts cannot be proven", **ev)
    if unresolved:
        r = unresolved[0]
        if any((f.source_sha256, f.source_size_bytes) != (r.source_sha256, r.source_size_bytes)
               for f in recs if f is not r):
            return out(Outcome.CONFLICT, "P4", f"a {r.status.value} record and a failed record for different bytes "
                                               "exist for this file", record_id=r.record_id, record_status=r.status,
                       **ev)
        kw = dict(record_id=r.record_id, record_status=r.status, **ev)

        if r.status is RecordStatus.PREPARING:
            if is_source(r):
                note = "no backup" if backup.kind == "missing" else (
                    "the backup matches the original" if backup_is_original(r) else
                    "the backup is incomplete or different and was not touched")
                return recoverable("P5", r, RecordStatus.FAILED, "observed: a preparing record, and the file holds the "
                                   f"original bytes it recorded, so it is unchanged; {note}; inferred: the upgrade "
                                   "stopped before its backup was verified")
            return out(Outcome.CONFLICT, "P6", f"observed: a preparing record, and the file is now {state(r)}; "
                                               "inferred: this upgrade stopped before its replace step, so the change "
                                               "did not come from it; what changed the file is unknown", **kw)

        if r.status is RecordStatus.BACKED_UP:
            if is_source(r):
                return recoverable("P7", r, RecordStatus.FAILED, "observed: a backed_up record, and the file holds the "
                                   "original bytes, so it is unchanged; inferred: the replace did not take effect (it "
                                   "was not replaced, or a crash undid it)")
            if is_upgraded_digest(r):
                problem = verifies_as_upgrade(r)
                if problem is None:
                    return recoverable("P8", r, RecordStatus.UPGRADED, "observed: a backed_up record; the file holds "
                                       "the upgraded bytes, parses as format 2 and verifies against the backup, "
                                       "which holds the original bytes; inferred: the upgrade stopped after its "
                                       "replace, before its record was finished")
                return out(Outcome.CONFLICT, "P9", f"observed: the file holds the upgraded bytes, but {problem}", **kw)
            return out(Outcome.CONFLICT, "P10", f"observed: a backed_up record, and the file is now {state(r)}; "
                                                "inferred: the upgrade stopped around its replace, and the file was "
                                                "changed by something unknown", **kw)

        if r.status is RecordStatus.UNCERTAIN:
            return out(Outcome.CONFLICT, "P11", f"observed: an uncertain record (an earlier upgrade could not confirm "
                                                f"its result), and the file is now {state(r)}; inferred: nothing, an "
                                                "uncertain record is never resolved from the disk; resolving it is "
                                                "not available in this build", **kw)

        # upgraded: restore evidence first (C-3); P12-P15 only when no restore record refers to r
        if sv.restores.get(r.record_id):
            return _decide_restore(sv, file_name, r, sdata, bdata, kw, out, state, blocked_by_unexpected,
                                   verifies_as_upgrade)
        if is_upgraded_digest(r):
            es, _ = _parse(sdata)
            if es is not None and es.schema_version == TARGET_SCHEMA_VERSION:
                if not backup_is_original(r):
                    return out(Outcome.CONFLICT, "P13", f"the upgraded file matches its record, but the backup "
                                                        f"{M}/{file_name} is missing or does not match the original "
                                                        "bytes; a restore would not be possible", **kw)
                problem = verifies_as_upgrade(r)
                if problem is None:
                    return out(Outcome.VERIFIED_UPGRADED, "P12", "upgraded to format 2; the file and its backup "
                                                                 "match the record", **kw)
                return out(Outcome.CONFLICT, "P15", f"the upgraded record does not verify: {problem}", **kw)
        elif source.safe:
            # P14 follows the approved decision table (S != U, format 2 -> verified_upgraded) and is kept
            # unchanged on purpose (review finding M-1): the file is not compared with the upgrade or the
            # backup, so "changed since the upgrade" is an inference; reconsidering it is a separate decision.
            es, _ = _parse(sdata)
            if es is not None and es.schema_version == TARGET_SCHEMA_VERSION:
                return out(Outcome.VERIFIED_UPGRADED, "P14", "observed: an upgraded record, and a format 2 file that "
                                                             "differs from the recorded upgraded bytes; inferred: it "
                                                             "was changed since the upgrade (the file is not compared "
                                                             "with the upgrade or the backup); a restore would "
                                                             "discard those changes", **kw)
        return out(Outcome.CONFLICT, "P15", f"the record says upgraded, but the file is {state(r)} and is not a "
                                            "format 2 set", **kw)

    # P16, P17: failed records only
    if all(is_source(r) for r in recs):
        if backup.kind == "missing":
            note = "there is no backup"
        elif all(backup_is_original(r) for r in recs):
            note = "the backup matches the original"
        else:
            note = (f"the backup {M}/{file_name} holds other bytes, so a new upgrade of this file would refuse it; "
                    "it was not touched")
        return out(Outcome.VERIFIED_ORIGINAL, "P16", f"earlier upgrade attempts failed and the file is unchanged "
                                                     f"(the original bytes); {note}", **ev)
    current = "missing" if source.kind == "missing" else "different from the bytes those attempts recorded"
    return out(Outcome.CONFLICT, "P17", f"earlier upgrade attempts failed, and the file is now {current}; "
                                        "their records still block it", **ev)


def _describe(ev: FileEvidence) -> str:
    if ev.kind == "missing":
        return "missing"
    if ev.kind == "regular":
        extra = f", {ev.links} hard links" if ev.links != 1 else ""
        return f"{ev.size} bytes with SHA-256 {ev.sha256}{extra}"
    if ev.kind == "unreadable":
        return f"unreadable ({ev.error})"
    return f"a {ev.kind}, not a regular file"


def _attempt_file(rel: str, path: Path, limit: int | None) -> tuple[FileEvidence, bytes | None,
                                                               DisplacedObservation]:
    """A file in an attempt folder: hashed in chunks (a displaced file can be any size). Its bytes
    are read only when a comparison needs them and it is no larger than ``limit`` (a part of a
    file is never larger than the file). The observation adds device and inode, taken from
    ``lstat`` before and after hashing; a change in between is reported as unreadable."""
    none = dict(size=None, sha256=None, dev=None, ino=None, nlink=None)
    try:
        before = up._lstat(path)
    except FileNotFoundError:
        return FileEvidence(rel, "missing"), None, DisplacedObservation(kind="missing", **none)
    except OSError as exc:
        return (FileEvidence(rel, "unreadable", error=up._text(exc)), None,
                DisplacedObservation(kind="unreadable", **none))
    ev = _inspect_streamed(rel, path)
    try:
        after = up._lstat(path)
    except OSError:
        after = None
    ident = (before.st_dev, before.st_ino)
    if ev.kind == "regular" and (after is None or (after.st_dev, after.st_ino) != ident):
        ev = FileEvidence(rel, "unreadable", error="it changed while it was being read")
    data = None
    if ev.safe and limit is not None and ev.size <= limit:
        again, data = _inspect(rel, path)
        if (again.sha256, again.size) != (ev.sha256, ev.size):
            ev, data = FileEvidence(rel, "unreadable", error="it changed while it was being read"), None
    if ev.kind == "regular":
        obs = DisplacedObservation(kind="regular", size=ev.size, sha256=ev.sha256, dev=before.st_dev,
                                   ino=before.st_ino, nlink=before.st_nlink)
    elif ev.kind == "unreadable":
        obs = DisplacedObservation(kind="unreadable", size=None, sha256=None, dev=before.st_dev, ino=before.st_ino,
                                   nlink=before.st_nlink)
    else:
        obs = DisplacedObservation(kind=ev.kind, size=before.st_size, sha256=None, dev=before.st_dev,
                                   ino=before.st_ino, nlink=before.st_nlink)
    return ev, data, obs


def _decide_restore(sv: _Survey, file_name: str, r: MigrationRecord, sdata: bytes | None, bdata: bytes | None,
                    kw: dict, out, state, blocked_by_unexpected, verifies_as_upgrade) -> ReconcileOutcome:
    """P18-P22 and P24 for a file whose ``upgraded`` record ``r`` has restore records (revision 2
    §9, revision 3 §§4, 5, 7, preflight Q-3, Q-5, Q-6, Q-8, Q-9). Decided by the files only; a
    restore record is never trusted on its own and never ordered by its times."""
    group = sv.restores[r.record_id]
    rkw = dict(kw, restores=tuple(sorted(x.restore_id for x in group)))
    source, backup = kw["source"], kw["backup"]
    upg = (r.upgraded_sha256, r.upgraded_size_bytes)
    orig = (r.source_sha256, r.source_size_bytes)

    def equals(ev: FileEvidence, digest) -> bool:
        return ev.safe and (ev.sha256, ev.size) == digest

    s_is_u, s_is_o, b_is_o = equals(source, upg), equals(source, orig), equals(backup, orig)
    nonfailed = [x for x in group if x.status is not RestoreStatus.FAILED]

    if len(nonfailed) >= 2:
        listed = ", ".join(f"{x.restore_id} ({x.status.value})" for x in sorted(nonfailed, key=lambda x: x.restore_id))
        return out(Outcome.CONFLICT, "P21", f"observed: {len(nonfailed)} restore records that are not failed refer to "
                                            f"the upgrade record {r.record_id}: {listed}; inferred: nothing; which "
                                            "attempt, if any, decided the file is unknown (restore records are never "
                                            "ordered by their times); nothing was touched", **rkw)

    if not nonfailed:
        ids = ", ".join(rkw["restores"])
        if not s_is_u:
            return out(Outcome.CONFLICT, "P21", f"observed: only failed restore records ({ids}) refer to this upgrade, "
                                                f"and the file is now {state(r)}; inferred: nothing about what changed "
                                                "it; a failed restore record does not explain the file, so it is not "
                                                "judged as an upgrade (P14 or P15); the outcome is unknown; nothing "
                                                "was touched", **rkw)
        seen = f"observed: every restore attempt for this upgrade failed ({ids}), and the file still holds the " \
               "upgraded bytes; judged as P12"
        if not b_is_o:
            return out(Outcome.CONFLICT, "P24", f"{seen}: the backup {M}/{file_name} is missing or does not match the "
                                                "original bytes; inferred: a restore would not be possible; nothing "
                                                "was touched", **rkw)
        problem = verifies_as_upgrade(r)
        if problem:
            return out(Outcome.CONFLICT, "P24", f"{seen}: the upgraded record does not verify: {problem}; inferred: "
                                                "nothing; nothing was touched", **rkw)
        return out(Outcome.VERIFIED_UPGRADED, "P24", f"{seen}: upgraded to format 2; the file and its backup match the "
                                                     "record; a new restore attempt is allowed", **rkw)

    x = nonfailed[0]
    rid = x.restore_id
    a = sv.base / M / RS / rid
    a_rel = f"{M}/{RS}/{rid}"
    pres_ev, pres_data, _ = _attempt_file(f"{a_rel}/{rr.PRESERVED_DIR}/{file_name}",
                                          a / rr.PRESERVED_DIR / file_name, len(sdata) if sdata is not None else None)
    swap_rel = f"{a_rel}/{rr.SWAP_DIR}/{file_name}"
    swap_ev, swap_data, swap_obs = _attempt_file(swap_rel, a / rr.SWAP_DIR / file_name,
                                                 len(bdata) if bdata is not None else None)
    rkw.update(restore_id=rid, restore_status=x.status, preserved=pres_ev, swap=swap_ev,
               displaced_observed_at_restore=x.displaced_observed_at_restore)
    st = x.status.value
    article = "an" if st[0] in "aeiou" else "a"

    def p21(observed: str) -> ReconcileOutcome:
        return out(Outcome.CONFLICT, "P21", f"observed: {observed}; inferred: nothing; the restore's outcome is "
                                            "unknown and is not resolved from the disk; nothing was touched", **rkw)

    def recoverable(rule: str, reason: str) -> ReconcileOutcome:
        found = blocked_by_unexpected()
        if found:
            return out(Outcome.CONFLICT, rule, f"{reason}, but unexpected entries were found ({found}); not "
                                               "recoverable automatically", **rkw)
        return out(Outcome.RECOVERABLE, rule, f"{reason}; the restore record would be finalised as "
                                              f"{RESTORE_PLANNED[rule].value}, but finalising restore records is not "
                                              "available in this build (D-6 deferred); nothing was written", **rkw,
                   restore_planned_status=RESTORE_PLANNED[rule])

    def part_of(ev: FileEvidence, data: bytes | None, full: bytes | None) -> bool:
        return ev.kind == "missing" or (ev.safe and data is not None and full is not None and full.startswith(data))

    def how_much(ev: FileEvidence, digest) -> str:
        return "absent" if ev.kind == "missing" else "complete" if equals(ev, digest) else "a partial copy"

    def displaced_facts(obs: DisplacedObservation) -> str:
        return (f"The displaced file `{swap_rel}` held bytes other than the upgraded bytes when the restore observed "
                f"it (`{obs.sha256} / {obs.size}`, {obs.kind}). Those bytes remain in that attempt folder and are "
                f"kept. The active set file `{file_name}` does not include those changes and was not modified because "
                "of them. Nothing has been merged or restored automatically.")

    if x.status is RestoreStatus.UNCERTAIN:
        obs = x.displaced_observed_at_restore
        rkw["displaced_observed_at_report"] = swap_obs
        facts = f" {displaced_facts(obs)}" if obs is not None and not x.displaced_matches_upgraded else ""
        return p21(f"an uncertain restore record {rid} (the restore could not confirm its outcome), and the file is "
                   f"now {state(r)}.{facts}")

    if x.status is RestoreStatus.PREPARING:
        if not s_is_u:
            return p21(f"a preparing restore record {rid}, and the file is now {state(r)}")
        if swap_ev.kind != "missing":
            return p21(f"a preparing restore record {rid}, but {swap_rel} exists ({_describe(swap_ev)}), which a "
                       "preparing restore never stages")
        if not part_of(pres_ev, pres_data, sdata):
            return p21(f"a preparing restore record {rid}, and the preserved copy is neither absent nor part of the "
                       f"upgraded bytes ({_describe(pres_ev)})")
        return recoverable("P18", f"observed: a preparing restore record {rid}; the file holds the upgraded bytes the "
                                  f"restore recorded; the preserved copy is {how_much(pres_ev, upg)}; no staged "
                                  "original exists; inferred: the restore stopped before its preserved copy was "
                                  "verified, and nothing was exchanged")

    if x.status in (RestoreStatus.PRESERVED, RestoreStatus.EXCHANGING) and s_is_u:
        if not equals(pres_ev, upg):
            return p21(f"{article} {st} restore record {rid} and a file holding the upgraded bytes, but the preserved "
                       f"copy is {_describe(pres_ev)}")
        if x.status is RestoreStatus.PRESERVED:
            staged_ok = swap_ev.kind == "missing" or (b_is_o and part_of(swap_ev, swap_data, bdata))
        else:
            staged_ok = equals(swap_ev, orig)
        if not staged_ok:
            return p21(f"{article} {st} restore record {rid} and a file holding the upgraded bytes, but the staged "
                       f"original in {swap_rel} is {_describe(swap_ev)}"
                       + ("" if b_is_o else f", and the backup is {_describe(backup)}"))
        return recoverable("P19", f"observed: {article} {st} restore record {rid}; the file holds the upgraded bytes, "
                                  "the preserved copy equals them, and the staged original in the swap folder is "
                                  f"{how_much(swap_ev, orig)}; inferred: the exchange did not take place")

    if x.status is RestoreStatus.EXCHANGING and s_is_o:
        es, _ = _parse(sdata)
        problems = []
        if es is None or es.schema_version != SOURCE_SCHEMA_VERSION:
            problems.append(f"the file does not parse as format {SOURCE_SCHEMA_VERSION}")
        if not equals(swap_ev, upg):
            problems.append(f"the swap file is {_describe(swap_ev)}")
        if not b_is_o:
            problems.append(f"the backup is {_describe(backup)}")
        if not equals(pres_ev, upg):
            problems.append(f"the preserved copy is {_describe(pres_ev)}")
        if problems:
            return p21(f"an exchanging restore record {rid} and a file holding the original bytes, but "
                       + "; ".join(problems))
        return recoverable("P20", f"observed: an exchanging restore record {rid}; the file holds the original bytes "
                                  "and parses as format 1, the swap folder holds the upgraded bytes, the backup holds "
                                  "the original bytes and the preserved copy the upgraded bytes; inferred: the "
                                  "exchange took place, and the restore stopped before its record was finished")

    if x.status is RestoreStatus.RESTORED:
        at_restore = x.displaced_observed_at_restore
        at_report = swap_obs
        rkw["displaced_observed_at_report"] = at_report
        if not s_is_o:
            es, err = _parse(sdata) if source.safe else (None, "")
            if es is not None and es.schema_version == SOURCE_SCHEMA_VERSION:
                return out(Outcome.VERIFIED_RESTORED, "P22", f"observed: a restored record {rid}, and a format 1 file "
                                                             "that differs from the restored (original) bytes; "
                                                             "inferred: it was changed since the restore (the file "
                                                             "is not compared with anything else); this describes the "
                                                             "earlier restore, not the current file, and makes it "
                                                             "eligible for no new upgrade or restore; nothing was "
                                                             "touched", **rkw)
            now = ("missing" if source.kind == "missing" else "the upgraded bytes" if s_is_u else
                   f"a format {es.schema_version} set" if es is not None else "not a readable set")
            return out(Outcome.CONFLICT, "P22", f"observed: a restored record {rid}, and the file is now {now}, not a "
                                                "format 1 set; inferred: it was changed since the restore by "
                                                "something unknown; nothing was touched", **rkw)
        es, _ = _parse(sdata)
        problems = []
        if es is None or es.schema_version != SOURCE_SCHEMA_VERSION:
            problems.append(f"the file does not parse as format {SOURCE_SCHEMA_VERSION}")
        if not b_is_o:
            problems.append(f"the backup is {_describe(backup)}")
        if not equals(pres_ev, upg):
            problems.append(f"the preserved copy is {_describe(pres_ev)}")
        if problems:
            return p21(f"a restored record {rid} and a file holding the original bytes, but " + "; ".join(problems))
        if at_report.kind != "regular":
            disp = (f"the displaced file recorded at restore is missing or no longer a regular file ({at_report.kind}) "
                    f"at {swap_rel}; the active set file is unaffected")
        elif (at_report.sha256, at_report.size) != (at_restore.sha256, at_restore.size):
            disp = (f"The displaced file `{swap_rel}` changed after the restore observed it (at restore: "
                    f"`{at_restore.sha256} / {at_restore.size}`; now: `{at_report.sha256} / {at_report.size}`). The "
                    "changed bytes remain in that attempt folder and are kept. The active set file "
                    f"`{file_name}` does not include those changes and was not modified because of them. Nothing has "
                    "been merged or restored automatically. It changed between the restore's observation and this "
                    "report; nothing is inferred about when, by what or why")
        elif (at_report.dev, at_report.ino) != (at_restore.dev, at_restore.ino):
            disp = (f"the displaced file at {swap_rel} holds the bytes the restore observed, but a different file is "
                    "now at that path")
        else:
            disp = f"the displaced file at {swap_rel} still holds the bytes the restore observed, in the same file"
        return out(Outcome.VERIFIED_RESTORED, "P20R", f"observed: a restored record {rid}; the file holds the original "
                                                      "bytes and parses as format 1; the backup holds the original "
                                                      "bytes and the preserved copy the upgraded bytes; restored to "
                                                      f"format 1 (decided from the files, not the record); {disp}",
                   **rkw)

    return p21(f"{article} {st} restore record {rid}, and the file is now {state(r)}")


def _blocked_outcome(file_name: str, block: _Block) -> ReconcileOutcome:
    return ReconcileOutcome(file_name, block.outcome, block.rule, block.reason)


# --------------------------------------------------------------------------- public


def reconcile_file(sets_dir: Path | str, file_name: str, *, apply: bool = False) -> ReconcileOutcome:
    """The read-only reconciliation report for ``<sets_dir>/<file_name>``. Writes nothing."""
    _refuse_apply(apply)
    block = _platform_block()
    if block:
        return _blocked_outcome(file_name, block)
    if not isinstance(file_name, str) or not up._NAME.match(file_name):
        return ReconcileOutcome(str(file_name), Outcome.UNVERIFIABLE, "P1",
                                f"{file_name!r} is not a plain set file name in the sets folder; nothing was read")
    with up._context(fresh=True) as ctx:
        sv = _survey(ctx, sets_dir)
        if isinstance(sv, _Block):
            return _blocked_outcome(file_name, sv)
        return _decide(sv, file_name)


def reconcile_library(sets_dir: Path | str, *, apply: bool = False) -> LibraryReport:
    """The read-only reconciliation report for every set file, backup and record of a sets folder,
    in file-name order. Writes nothing."""
    _refuse_apply(apply)
    block = _platform_block()
    if block:
        return LibraryReport(block.outcome, block.rule, block.reason)
    with up._context(fresh=True) as ctx:
        sv = _survey(ctx, sets_dir)
        if isinstance(sv, _Block):
            return LibraryReport(sv.outcome, sv.rule, sv.reason)
        names: Iterable[str] = sorted(set(sv.sets) | set(sv.backups) | set(sv.records) | set(sv.staged))
        outcomes = tuple(_decide(sv, n) for n in names)
        return LibraryReport(None, None, f"{len(outcomes)} files reported; nothing was written", outcomes,
                             tuple(sv.unexpected))
