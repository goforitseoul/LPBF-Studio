"""Restore evidence in the read-only reconciliation report (B7 Phase 2, S3-B2c-a).

The report reads ``.format1-backup/restores/`` (a future Restore's attempt folders; Restore itself
is S3-B2c-b and is **not implemented**) and classifies files that a restore attempt refers to.
The frozen design is ``docs/audit/b7p2_s3b2c/design/report_rev5.md``; the classification is
revision 2 §9 with revision 3 §§4, 5, 7 and the project owner's preflight decisions Q-1 to Q-9 (2026-11-01).

- **G6** (new global rule): any unreadable, invalid, misnamed, linked or hard-linked restore
  record, any attempt folder without a valid record, any unknown entry in ``restores/`` or in an
  attempt folder (including a leftover ``restore.json`` temporary file, Q-7), and any restore
  record whose upgrade record is missing, not ``upgraded``, or for other paths or bytes (Q-4)
  blocks the whole library.
- **Ordering** (C-3, revision 3 §7): a restore record that refers to a file's upgrade record
  means P12-P15 are never reached for that file. The reference proves nothing by itself; the rule
  is decided by the files.
- **P18-P22, P24** (P23 is retired: Q-2). P18-P20 are ``recoverable`` with a report-only
  ``restore_planned_status``; nothing writes them (D-6 deferred, Q-9).
- **C-1**: a library without ``restores/``, or with an empty one, is classified exactly as before.
- **C-5/C-8**: reports write nothing; ``apply=True`` stays refused; apply writes nothing for
  every new rule, and ``reconcile_apply.py`` is byte-identical to its accepted version.

Synthetic data in temporary folders only.
"""

import hashlib
import json
import os
import signal
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from lpbfstudio import migration as mig
from lpbfstudio import reconcile as rc
from lpbfstudio import reconcile_apply as ra
from lpbfstudio import restore_record as rr
from lpbfstudio import upgrade as up
from lpbfstudio.migration import RecordStatus
from test_reconcile_report import (  # noqa: F401 (fixtures and helpers of the accepted B2a tests)
    FOREIGN_UPGRADED, ORIGINAL, OTHER_V1, OTHER_V2, UPGRADED, _upgraded_of, check, lib, put_backup, put_record,
    put_source, rdir, report, set_data, sets, sha, snap,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX primitives; Windows is refused (see upgrade.py)")

O = rc.Outcome
S = rr.RestoreStatus
M = up.MIGRATION_DIR
T0, T1, T2 = "2026-11-03T08:00:00Z", "2026-11-03T08:00:05Z", "2026-11-03T08:00:09Z"
ACCEPTED_APPLY_SHA256 = "e52ffdeeb819f32f5d93a702ff05063e2374dfa19c9eafee91d5bfbbfa416680"

ORIGINAL_2 = json.dumps(set_data("P2"), indent=2).encode()
UPGRADED_2, _ = _upgraded_of(ORIGINAL_2)


# ================================================================ helpers

def restores(sets: Path) -> Path:
    return sets / M / rr.RESTORES_DIR


def observe(path: Path) -> dict:
    """The displaced observation of ``path`` as a restore would record it."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return {"kind": "missing", "size": None, "sha256": None, "dev": None, "ino": None, "nlink": None}
    if not os.path.isfile(path) or os.path.islink(path):
        kind = "symlink" if os.path.islink(path) else "directory" if os.path.isdir(path) else "other"
        return {"kind": kind, "size": st.st_size, "sha256": None, "dev": st.st_dev, "ino": st.st_ino,
                "nlink": st.st_nlink}
    return {"kind": "regular", "size": st.st_size, "sha256": sha(path.read_bytes()), "dev": st.st_dev,
            "ino": st.st_ino, "nlink": st.st_nlink}


def attempt(sets: Path, urec: mig.MigrationRecord, status: S, *, rid: str | None = None,
            preserved: bytes | None = UPGRADED, swap: bytes | None = None, displaced: dict | str | None = "auto",
            error: str = "stopped", name: str = "P1.json", **over) -> tuple[str, Path]:
    """An attempt folder as a restore in ``status`` would leave it, for upgrade record ``urec``."""
    rid = rid or uuid.uuid4().hex
    a = restores(sets) / rid
    (a / rr.PRESERVED_DIR).mkdir(parents=True)
    (a / rr.SWAP_DIR).mkdir()
    if preserved is not None:
        (a / rr.PRESERVED_DIR / name).write_bytes(preserved)
    if swap is not None:
        (a / rr.SWAP_DIR / name).write_bytes(swap)
    d = {
        "restore_record_schema_version": 1, "restore_id": rid, "upgrade_record_id": urec.record_id,
        "source_path": name, "backup_path": f"{M}/{name}",
        "current_sha256": urec.upgraded_sha256, "current_size_bytes": urec.upgraded_size_bytes,
        "restored_sha256": urec.source_sha256, "restored_size_bytes": urec.source_size_bytes,
        "source_identity": [1, 2], "staged_identity": None,
        "preserved_path": f"{rr.RESTORES_DIR}/{rid}/{rr.PRESERVED_DIR}/{name}",
        "swap_path": f"{rr.RESTORES_DIR}/{rid}/{rr.SWAP_DIR}/{name}",
        "exchange_attempted": False, "displaced_observed_at_restore": None, "displaced_matches_upgraded": None,
        "status": status.value, "created_at": T1, "completed_at": None, "error": None,
    }
    if status in (S.PRESERVED, S.EXCHANGING, S.RESTORED, S.UNCERTAIN) and swap is not None:
        d["staged_identity"] = [1, 3]
    if status in (S.EXCHANGING, S.RESTORED):
        d.update(exchange_attempted=True, staged_identity=[1, 3])
    if status in rr.FINAL:
        d["completed_at"] = T2
    if status in (S.FAILED, S.UNCERTAIN):
        d["error"] = error
    if status in (S.RESTORED, S.UNCERTAIN) and displaced is not None:
        obs = observe(a / rr.SWAP_DIR / name) if displaced == "auto" else displaced
        if status is S.RESTORED or swap is not None or displaced != "auto":
            d.update(exchange_attempted=True, displaced_observed_at_restore=obs,
                     displaced_matches_upgraded=obs["kind"] == "regular" and
                     (obs["sha256"], obs["size"]) == (urec.upgraded_sha256, urec.upgraded_size_bytes))
    d.update(over)
    (a / rr.RECORD_NAME).write_text(json.dumps(d, indent=2), encoding="utf-8")
    rr.parse_restore_record((a / rr.RECORD_NAME).read_text(encoding="utf-8"))  # the helper builds valid records
    return rid, a


def upgraded_lib(sets: Path, *, source: bytes | None = UPGRADED, backup: bytes | None = ORIGINAL,
                 name: str = "P1.json", original: bytes = ORIGINAL, upgraded: bytes = UPGRADED):
    put_source(sets, source, name)
    put_backup(sets, backup, name)
    return put_record(sets, RecordStatus.UPGRADED, source=original, upgraded=upgraded, name=name)


def restored_lib(sets: Path, **kw):
    """A completed restore: the file and backup hold the original, the preserved copy and the
    displaced file the upgraded bytes."""
    urec = upgraded_lib(sets, source=ORIGINAL)
    rid, a = attempt(sets, urec, S.RESTORED, swap=UPGRADED, **kw)
    return urec, rid, a


def rcheck(out, outcome, rule, *words, restore_planned=None, rid=None):
    check(out, outcome, rule, *words)
    assert out.restore_planned_status is restore_planned, out.restore_planned_status
    if rid is not None:
        assert out.restore_id == rid, (out.restore_id, rid)


def g6(sets: Path, *words, name: str = "P1.json"):
    out = report(sets, name)
    check(out, O.UNVERIFIABLE, "G6", *words)
    lib_report = rc.reconcile_library(sets)
    assert (lib_report.blocked, lib_report.rule, lib_report.outcomes) == (O.UNVERIFIABLE, "G6", ())
    other = rc.reconcile_file(sets, "V2.json")
    assert (other.outcome, other.rule) == (O.UNVERIFIABLE, "G6"), "G6 blocks every file, not one"
    return out


def apply_writes_nothing(sets: Path, name: str = "P1.json"):
    before = snap(sets.parent)
    res = ra.apply_file(sets, name, now="2099-01-01T00:00:00Z")
    assert res.result is ra.ApplyResult.NO_WRITE, (res.result, res.reason)
    assert res.wrote is None and res.status_after is None
    assert snap(sets.parent) == before
    return res


# ================================================================ scenarios (shared by several tests)

def _sc_p18(sets):
    attempt(sets, upgraded_lib(sets), S.PREPARING, preserved=None)


def _sc_p19(sets):
    attempt(sets, upgraded_lib(sets), S.PRESERVED, swap=ORIGINAL)


def _sc_p19x(sets):
    attempt(sets, upgraded_lib(sets), S.EXCHANGING, swap=ORIGINAL)


def _sc_p20(sets):
    attempt(sets, upgraded_lib(sets, source=ORIGINAL), S.EXCHANGING, swap=UPGRADED)


def _sc_p20r(sets):
    restored_lib(sets)


def _sc_p21(sets):
    attempt(sets, upgraded_lib(sets), S.UNCERTAIN, swap=ORIGINAL, displaced=None)


def _sc_p22(sets):
    restored_lib(sets)
    put_source(sets, OTHER_V1)


def _sc_p22c(sets):
    restored_lib(sets)
    put_source(sets, OTHER_V2)


def _sc_p24(sets):
    attempt(sets, upgraded_lib(sets), S.FAILED, swap=ORIGINAL)


def _sc_g6(sets):
    upgraded_lib(sets)
    (restores(sets) / "notes.txt").parent.mkdir(parents=True)
    (restores(sets) / "notes.txt").write_text("x")


SCENARIOS = {"P18": _sc_p18, "P19": _sc_p19, "P19-exchanging": _sc_p19x, "P20": _sc_p20, "P20R": _sc_p20r,
             "P21": _sc_p21, "P22": _sc_p22, "P22-conflict": _sc_p22c, "P24": _sc_p24, "G6": _sc_g6}


# ================================================================ C-1 and the empty restores folder (F-3)

def _states():
    """Libraries covering the accepted B2a rules, built in a sets folder."""
    def p2(s): pass
    def p5(s):
        put_record(s, RecordStatus.PREPARING)
    def p7(s):
        put_backup(s, ORIGINAL)
        put_record(s, RecordStatus.BACKED_UP)
    def p8(s):
        put_source(s, UPGRADED)
        put_backup(s, ORIGINAL)
        put_record(s, RecordStatus.BACKED_UP)
    def p11(s):
        put_record(s, RecordStatus.UNCERTAIN)
    def p12(s):
        upgraded_lib(s)
    def p13(s):
        upgraded_lib(s, backup=b"damaged")
    def p14(s):
        upgraded_lib(s, source=OTHER_V2)
    def p15(s):
        upgraded_lib(s, source=ORIGINAL)
    def p16(s):
        put_record(s, RecordStatus.FAILED)
    def p17(s):
        put_source(s, OTHER_V1)
        put_record(s, RecordStatus.FAILED)
    return {"P2": p2, "P5": p5, "P7": p7, "P8": p8, "P11": p11, "P12": p12, "P13": p13, "P14": p14, "P15": p15,
            "P16": p16, "P17": p17}


def _key(o):
    return (o.file_name, o.outcome, o.rule, o.reason, o.record_id, o.record_status, o.planned_status, o.records,
            o.source, o.backup, o.temps, o.unexpected)


@pytest.mark.parametrize("state", sorted(_states()))
def test_c1_an_empty_restores_folder_changes_no_classification(sets, state):
    """The same library, reported without and then with an empty restores folder (F-3)."""
    outs = []
    (sets / M).mkdir()
    _states()[state](sets)
    for variant in ("without", "empty"):
        if variant == "empty":
            restores(sets).mkdir()
        out = report(sets)
        assert out.rule == state or (state == "P2" and out.rule == "P2"), (state, out.rule, out.reason)
        assert out.restore_id is None and out.restore_status is None and out.restore_planned_status is None
        assert out.restores == () and out.preserved is None and out.swap is None
        assert out.displaced_observed_at_restore is None and out.displaced_observed_at_report is None
        lib_report = rc.reconcile_library(sets)
        outs.append(([_key(o) for o in lib_report.outcomes], lib_report.unexpected, lib_report.blocked))
    assert outs[0] == outs[1]


def test_c1_an_empty_restores_folder_is_not_listed_as_unexpected(sets):
    upgraded_lib(sets)
    restores(sets).mkdir()
    out = report(sets)
    check(out, O.VERIFIED_UPGRADED, "P12")
    assert all(rr.RESTORES_DIR not in e.path for e in out.unexpected)
    assert all(rr.RESTORES_DIR not in e.path for e in rc.reconcile_library(sets).unexpected)


def test_c1_restore_evidence_for_another_file_changes_nothing_for_this_one(sets):
    """P13, P14 and P15 keep their accepted classification when only another file has restore
    evidence (here a completed restore of P2.json)."""
    put_source(sets, ORIGINAL_2, "P2.json")
    u2 = upgraded_lib(sets, source=ORIGINAL_2, backup=ORIGINAL_2, name="P2.json", original=ORIGINAL_2,
                      upgraded=UPGRADED_2)
    attempt(sets, u2, S.RESTORED, swap=UPGRADED_2, preserved=UPGRADED_2, name="P2.json")
    assert report(sets, "P2.json").rule == "P20R"
    for source, backup, rule, outcome in ((UPGRADED, b"damaged", "P13", O.CONFLICT),
                                          (OTHER_V2, ORIGINAL, "P14", O.VERIFIED_UPGRADED),
                                          (ORIGINAL, ORIGINAL, "P15", O.CONFLICT),
                                          (UPGRADED, ORIGINAL, "P12", O.VERIFIED_UPGRADED)):
        for p in rdir(sets).glob("*.json"):
            if mig.parse_record(p.read_text()).source_path == "P1.json":
                p.unlink()
        upgraded_lib(sets, source=source, backup=backup)
        out = report(sets)
        check(out, outcome, rule)
        assert out.restores == () and out.restore_planned_status is None


# ================================================================ G6 (C-4, Q-4, Q-7)

def test_g6_restores_that_is_a_link_blocks(sets, tmp_path):
    upgraded_lib(sets)
    (tmp_path / "elsewhere").mkdir()
    restores(sets).symlink_to(tmp_path / "elsewhere")
    g6(sets, "link")


def test_g6_restores_that_is_a_file_blocks(sets):
    upgraded_lib(sets)
    restores(sets).write_text("x")
    g6(sets, "not a folder")


@pytest.mark.parametrize("entry", ["notes.txt", ".hidden", "abc", "0123456789ABCDEF0123456789ABCDEF",
                                   "0123456789abcdef0123456789abcde"])
def test_g6_unknown_entry_in_restores_blocks(sets, entry):
    upgraded_lib(sets)
    restores(sets).mkdir()
    (restores(sets) / entry).mkdir()
    g6(sets, entry)


def test_g6_attempt_named_like_an_id_but_a_file_or_link_blocks(sets, tmp_path):
    upgraded_lib(sets)
    restores(sets).mkdir()
    rid = uuid.uuid4().hex
    (restores(sets) / rid).write_text("x")
    g6(sets, rid)
    (restores(sets) / rid).unlink()
    (tmp_path / "real").mkdir()
    (restores(sets) / rid).symlink_to(tmp_path / "real")
    g6(sets, rid)


def test_g6_attempt_folder_without_a_record_blocks(sets):
    """A restore killed while creating its folder (revision 2 §10.3, R6) leaves a folder without a
    valid record: the whole library is unverifiable."""
    urec = upgraded_lib(sets)
    rid, a = attempt(sets, urec, S.PREPARING, preserved=None)
    (a / rr.RECORD_NAME).unlink()
    g6(sets, rid, "without a valid record")
    for sub in (rr.PRESERVED_DIR, rr.SWAP_DIR):
        (a / sub).rmdir()
    g6(sets, rid)


def _bad_restore_records():
    return {
        "invalid JSON": b"{",
        "not an object": b"[]",
        "upgrade record": json.dumps({"record_schema_version": 1}).encode(),
        "future version": json.dumps({"restore_record_schema_version": 2}).encode(),
        "empty": b"",
        "not UTF-8": b"\xff\xfe",
    }


@pytest.mark.parametrize("case", sorted(_bad_restore_records()))
def test_g6_unreadable_or_invalid_restore_record_blocks(sets, case):
    urec = upgraded_lib(sets)
    _, a = attempt(sets, urec, S.PREPARING, preserved=None)
    (a / rr.RECORD_NAME).write_bytes(_bad_restore_records()[case])
    g6(sets, "restore record")


def test_g6_record_in_the_wrong_folder_blocks(sets):
    urec = upgraded_lib(sets)
    rid, a = attempt(sets, urec, S.PREPARING, preserved=None)
    other = restores(sets) / uuid.uuid4().hex
    a.rename(other)
    g6(sets, rid)


@pytest.mark.parametrize("how", ["symlink", "hardlink", "folder", "fifo"])
def test_g6_restore_record_that_is_not_a_safe_regular_file_blocks(sets, tmp_path, how):
    urec = upgraded_lib(sets)
    _, a = attempt(sets, urec, S.PREPARING, preserved=None)
    p = a / rr.RECORD_NAME
    if how == "symlink":
        (tmp_path / "rec.json").write_bytes(p.read_bytes())
        p.unlink()
        p.symlink_to(tmp_path / "rec.json")
    elif how == "hardlink":
        os.link(p, tmp_path / "twin.json")
    elif how == "folder":
        p.unlink()
        p.mkdir()
    else:
        p.unlink()
        os.mkfifo(p)
    g6(sets, "restore record")


def test_g6_restore_record_that_cannot_be_read_blocks(sets, monkeypatch):
    urec = upgraded_lib(sets)
    _, a = attempt(sets, urec, S.PREPARING, preserved=None)
    real = up._read_file

    def deny(path):
        if path.name == rr.RECORD_NAME:
            raise PermissionError(13, "Permission denied")
        return real(path)
    monkeypatch.setattr(up, "_read_file", deny)
    out = rc.reconcile_file(sets, "P1.json")
    check(out, O.UNVERIFIABLE, "G6", "cannot be read")


@pytest.mark.parametrize("missing", [rr.PRESERVED_DIR, rr.SWAP_DIR])
def test_g6_attempt_folder_missing_a_subfolder_blocks(sets, missing):
    urec = upgraded_lib(sets)
    _, a = attempt(sets, urec, S.PREPARING, preserved=None)
    (a / missing).rmdir()
    g6(sets, missing)


@pytest.mark.parametrize("how", ["file", "symlink"])
@pytest.mark.parametrize("sub", [rr.PRESERVED_DIR, rr.SWAP_DIR])
def test_g6_subfolder_that_is_not_a_real_folder_blocks(sets, tmp_path, sub, how):
    urec = upgraded_lib(sets)
    _, a = attempt(sets, urec, S.PREPARING, preserved=None)
    (a / sub).rmdir()
    if how == "file":
        (a / sub).write_text("x")
    else:
        (tmp_path / "d").mkdir()
        (a / sub).symlink_to(tmp_path / "d")
    g6(sets, sub)


@pytest.mark.parametrize("where", ["attempt", rr.PRESERVED_DIR, rr.SWAP_DIR])
def test_g6_unknown_entry_inside_an_attempt_folder_blocks(sets, where):
    urec = upgraded_lib(sets)
    _, a = attempt(sets, urec, S.PRESERVED, swap=ORIGINAL)
    folder = a if where == "attempt" else a / where
    (folder / "P2.json").write_bytes(b"x")
    g6(sets, "P2.json")


def test_g6_q7_leftover_restore_record_temporary_file_blocks_strictly(sets):
    """Q-7 (strict): a record helper's temporary file left in the attempt folder is an unknown
    entry; the whole library stays report-blocked; nothing is cleaned up or forced."""
    urec = upgraded_lib(sets)
    rid, a = attempt(sets, urec, S.PRESERVED, swap=ORIGINAL)
    tmp = a / f".{rr.RECORD_NAME}.0123456789ab.tmp"
    tmp.write_bytes((a / rr.RECORD_NAME).read_bytes())
    out = g6(sets, tmp.name)
    assert tmp.exists() and (a / rr.RECORD_NAME).exists()
    apply_writes_nothing(sets)


_KILLED_WRITER = r"""
import os, signal, sys
folder = sys.argv[1]
fd = os.open(os.path.join(folder, ".restore.json.0123456789ab.tmp"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
os.write(fd, b'{"restore_record_schema_version": 1, "sta')
os.fsync(fd)
os.kill(os.getpid(), signal.SIGKILL)
"""


def test_g6_q7_kill_point_during_a_restore_record_update_leaves_a_report_blocked_unchanged_tree(sets):
    """Interruption test for Q-7: a process killed (SIGKILL) while writing a restore record's
    temporary file, before its rename. Restore does not exist in this build, so the writer is a
    stand-in that leaves exactly that state. The report is G6 for every file, and neither the
    report nor apply changes anything."""
    urec = upgraded_lib(sets)
    rid, a = attempt(sets, urec, S.PRESERVED, swap=ORIGINAL)
    proc = subprocess.run([sys.executable, "-c", _KILLED_WRITER, str(a)], capture_output=True)
    assert proc.returncode == -signal.SIGKILL
    before = snap(sets.parent)
    g6(sets, ".restore.json.0123456789ab.tmp")
    apply_writes_nothing(sets)
    apply_writes_nothing(sets, "V2.json")
    assert snap(sets.parent) == before


# ---- Q-4: the restore record must match an upgraded record exactly

def test_g6_q4_restore_record_for_a_missing_upgrade_record_blocks(sets):
    urec = upgraded_lib(sets)
    attempt(sets, urec, S.PREPARING, preserved=None)
    (rdir(sets) / f"{urec.record_id}.json").unlink()
    g6(sets, urec.record_id, "missing")


def test_g6_q4_restore_record_without_any_records_folder_blocks(sets):
    urec = upgraded_lib(sets)
    attempt(sets, urec, S.PREPARING, preserved=None)
    for p in rdir(sets).iterdir():
        p.unlink()
    rdir(sets).rmdir()
    g6(sets, urec.record_id)


@pytest.mark.parametrize("status", [RecordStatus.FAILED, RecordStatus.BACKED_UP, RecordStatus.PREPARING,
                                    RecordStatus.UNCERTAIN])
def test_g6_q4_restore_record_for_an_upgrade_record_that_is_not_upgraded_blocks(sets, status):
    put_backup(sets, ORIGINAL)
    rec = put_record(sets, status)
    fake = rec.model_copy(update={"upgraded_sha256": sha(UPGRADED), "upgraded_size_bytes": len(UPGRADED)})
    attempt(sets, fake, S.PREPARING, preserved=None)
    g6(sets, rec.record_id, status.value)


def test_g6_q4_restore_record_for_another_source_path_blocks(sets):
    put_source(sets, ORIGINAL_2, "P2.json")
    urec = upgraded_lib(sets)
    attempt(sets, urec, S.PREPARING, preserved=None, name="P2.json")
    g6(sets, "P2.json")


@pytest.mark.parametrize("field,value", [
    ("current_sha256", "c" * 64), ("current_size_bytes", 1),
    ("restored_sha256", "d" * 64), ("restored_size_bytes", 2),
])
def test_g6_q4_restore_record_with_other_digests_than_its_upgrade_record_blocks(sets, field, value):
    urec = upgraded_lib(sets)
    attempt(sets, urec, S.PREPARING, preserved=None, **{field: value})
    g6(sets, field)


def test_g5_comes_before_g6(sets):
    urec = upgraded_lib(sets)
    attempt(sets, urec, S.PREPARING, preserved=None)
    (rdir(sets) / "broken.json").write_text("{")
    check(report(sets), O.UNVERIFIABLE, "G5")


def test_g4_busy_comes_before_reading_restore_evidence(sets):
    urec = upgraded_lib(sets)
    _, a = attempt(sets, urec, S.PREPARING, preserved=None)
    (a / rr.RECORD_NAME).write_text("{")  # would be G6
    with up.library_lock(sets):
        out = rc.reconcile_file(sets, "P1.json")
    check(out, O.BUSY, "G4")


# ================================================================ P18-P20: interrupted restores (Q-9)

@pytest.mark.parametrize("preserved", [None, UPGRADED[:10], UPGRADED[:-1], UPGRADED])
def test_p18_preparing_with_the_upgraded_file_is_recoverable_as_failed_but_never_finalised(sets, preserved):
    rid, _ = attempt(sets, upgraded_lib(sets), S.PREPARING, preserved=preserved)
    out = report(sets)
    rcheck(out, O.RECOVERABLE, "P18", "observed:", "preparing", "inferred:", "D-6", restore_planned=S.FAILED, rid=rid)
    assert out.planned_status is None and out.restore_status is S.PREPARING and out.restores == (rid,)
    assert out.preserved is not None
    apply_writes_nothing(sets)


@pytest.mark.parametrize("case", ["preserved is not a prefix", "swap file present", "file changed",
                                  "file missing", "preserved a link"])
def test_p18_with_inconsistent_evidence_is_p21(sets, tmp_path, case):
    urec = upgraded_lib(sets)
    rid, a = attempt(sets, urec, S.PREPARING, preserved=b"something else" if case == "preserved is not a prefix"
                     else None, swap=ORIGINAL if case == "swap file present" else None)
    if case == "file changed":
        put_source(sets, OTHER_V2)
    elif case == "file missing":
        put_source(sets, None)
    elif case == "preserved a link":
        (tmp_path / "u").write_bytes(UPGRADED)
        (a / rr.PRESERVED_DIR / "P1.json").symlink_to(tmp_path / "u")
    out = report(sets)
    rcheck(out, O.CONFLICT, "P21", "observed:", "unknown", rid=rid)
    apply_writes_nothing(sets)


@pytest.mark.parametrize("swap", [None, ORIGINAL[:7], ORIGINAL])
def test_p19_preserved_with_the_upgraded_file_is_recoverable_as_failed(sets, swap):
    rid, _ = attempt(sets, upgraded_lib(sets), S.PRESERVED, swap=swap)
    out = report(sets)
    rcheck(out, O.RECOVERABLE, "P19", "observed:", "inferred:", "not take place", "D-6", restore_planned=S.FAILED,
           rid=rid)
    apply_writes_nothing(sets)


def test_p19_exchanging_whose_bytes_show_no_exchange_is_recoverable_as_failed(sets):
    rid, _ = attempt(sets, upgraded_lib(sets), S.EXCHANGING, swap=ORIGINAL)
    rcheck(report(sets), O.RECOVERABLE, "P19", "exchanging", restore_planned=S.FAILED, rid=rid)
    apply_writes_nothing(sets)


@pytest.mark.parametrize("case", ["exchanging with a partial swap", "exchanging without a swap file",
                                  "preserved copy differs", "swap differs from the original", "backup damaged"])
def test_p19_with_inconsistent_evidence_is_p21(sets, case):
    urec = upgraded_lib(sets, backup=b"damaged" if case == "backup damaged" else ORIGINAL)
    status = S.EXCHANGING if case.startswith("exchanging") else S.PRESERVED
    swap = {"exchanging with a partial swap": ORIGINAL[:7], "exchanging without a swap file": None,
            "swap differs from the original": OTHER_V1, "backup damaged": ORIGINAL[:7]}.get(case, ORIGINAL)
    if status is S.EXCHANGING and swap is None:
        rid, a = attempt(sets, urec, status, swap=b"tmp")
        (a / rr.SWAP_DIR / "P1.json").unlink()
    else:
        rid, _ = attempt(sets, urec, status, swap=swap,
                         preserved=UPGRADED[:-3] if case == "preserved copy differs" else UPGRADED)
    rcheck(report(sets), O.CONFLICT, "P21", "unknown", rid=rid)
    apply_writes_nothing(sets)


def test_p20_exchanging_whose_bytes_show_the_exchange_is_recoverable_as_restored(sets):
    rid, _ = attempt(sets, upgraded_lib(sets, source=ORIGINAL), S.EXCHANGING, swap=UPGRADED)
    out = report(sets)
    rcheck(out, O.RECOVERABLE, "P20", "observed:", "inferred:", "D-6", restore_planned=S.RESTORED, rid=rid)
    assert out.planned_status is None
    apply_writes_nothing(sets)


@pytest.mark.parametrize("case", ["backup damaged", "backup missing", "preserved differs", "preserved missing",
                                  "swap differs"])
def test_p20_with_any_condition_broken_is_p21(sets, case):
    urec = upgraded_lib(sets, source=ORIGINAL,
                        backup={"backup damaged": b"x", "backup missing": None}.get(case, ORIGINAL))
    rid, _ = attempt(sets, urec, S.EXCHANGING, swap=OTHER_V2 if case == "swap differs" else UPGRADED,
                     preserved={"preserved differs": b"x", "preserved missing": None}.get(case, UPGRADED))
    rcheck(report(sets), O.CONFLICT, "P21", "unknown", rid=rid)
    apply_writes_nothing(sets)


def test_q9_restore_planned_status_mapping_is_explicit_and_one_to_one():
    assert rc.RESTORE_PLANNED == {"P18": S.FAILED, "P19": S.FAILED, "P20": S.RESTORED}
    assert set(ra.WRITES) == {"P5", "P7", "P8"}, "the apply gate is not widened"


def test_q9_reconcile_apply_is_byte_identical_to_its_accepted_version():
    assert hashlib.sha256(Path(ra.__file__).read_bytes()).hexdigest() == ACCEPTED_APPLY_SHA256


# ================================================================ P20R and the displaced file (C-6)

def test_p20r_completed_restore_is_verified_restored(sets):
    urec, rid, a = restored_lib(sets)
    out = report(sets)
    rcheck(out, O.VERIFIED_RESTORED, "P20R", "observed:", "restored", "format 1", rid=rid)
    assert out.record_id == urec.record_id and out.restore_status is S.RESTORED
    assert out.displaced_observed_at_report == out.displaced_observed_at_restore
    assert out.displaced_observed_at_report.sha256 == sha(UPGRADED)
    assert "changed" not in out.reason
    apply_writes_nothing(sets)


def test_c3_a_restored_file_is_no_longer_reported_as_p15(sets):
    restored_lib(sets)
    assert report(sets).rule != "P15"


def test_p20r_displaced_file_changed_after_the_restore_uses_the_fixed_wording(sets):
    """Revision 3 §4: a late write through an old handle lands in the displaced file. It is kept,
    it is not part of the active set file, and nothing is merged. No time, writer or cause."""
    _, rid, a = restored_lib(sets)
    disp = a / rr.SWAP_DIR / "P1.json"
    with open(disp, "r+b") as fh:  # the same inode, as an old handle would write
        fh.seek(0, os.SEEK_END)
        fh.write(b"late edit")
    out = report(sets)
    rcheck(out, O.VERIFIED_RESTORED, "P20R", rid=rid)
    at_restore, at_report = out.displaced_observed_at_restore, out.displaced_observed_at_report
    assert at_restore.sha256 == sha(UPGRADED) and at_report.sha256 == sha(UPGRADED + b"late edit")
    assert at_restore.ino == at_report.ino
    wording = (f"The displaced file `{M}/{rr.RESTORES_DIR}/{rid}/{rr.SWAP_DIR}/P1.json` changed after the restore "
               f"observed it (at restore: `{sha(UPGRADED)} / {len(UPGRADED)}`; now: "
               f"`{sha(UPGRADED + b'late edit')} / {len(UPGRADED) + 9}`). The changed bytes remain in that "
               "attempt folder and are kept. The active set file `P1.json` does not include those changes and was "
               "not modified because of them. Nothing has been merged or restored automatically.")
    assert wording in out.reason, out.reason
    for word in ("probably", "likely", "someone", "an editor", "o'clock"):
        assert word not in out.reason, word
    assert "between the restore's observation and this report" in out.reason
    assert sets.joinpath("P1.json").read_bytes() == ORIGINAL
    apply_writes_nothing(sets)


def test_p20r_displaced_file_missing_is_reported_without_g6(sets):
    _, rid, a = restored_lib(sets)
    (a / rr.SWAP_DIR / "P1.json").unlink()
    out = report(sets)
    rcheck(out, O.VERIFIED_RESTORED, "P20R", "missing or no longer a regular file", "active set file is unaffected",
           rid=rid)
    assert out.displaced_observed_at_report.kind == "missing"


def test_p20r_displaced_file_replaced_by_a_folder_is_reported(sets):
    _, rid, a = restored_lib(sets)
    (a / rr.SWAP_DIR / "P1.json").unlink()
    (a / rr.SWAP_DIR / "P1.json").mkdir()
    out = report(sets)
    rcheck(out, O.VERIFIED_RESTORED, "P20R", "no longer a regular file", rid=rid)
    assert out.displaced_observed_at_report.kind == "directory"


def test_p20r_a_different_file_at_the_displaced_path_is_stated_without_inference(sets):
    _, rid, a = restored_lib(sets)
    disp = a / rr.SWAP_DIR / "P1.json"
    keep = a.parent.parent / "keep"  # hold the old inode so that the new file gets another one
    os.link(disp, keep)
    disp.unlink()
    disp.write_bytes(UPGRADED)
    keep.unlink()
    out = report(sets)
    rcheck(out, O.VERIFIED_RESTORED, "P20R", "a different file is now at that path", rid=rid)
    assert out.displaced_observed_at_report.sha256 == out.displaced_observed_at_restore.sha256
    assert out.displaced_observed_at_report.ino != out.displaced_observed_at_restore.ino


# ---- Q-5: restored, S = O, other evidence wrong

@pytest.mark.parametrize("case", ["backup differs", "backup missing", "preserved differs", "preserved missing",
                                  "preserved a link", "preserved a folder"])
def test_q5_restored_with_inconsistent_supporting_evidence_is_p21_never_verified(sets, tmp_path, case):
    _, rid, a = restored_lib(sets)
    pres = a / rr.PRESERVED_DIR / "P1.json"
    if case == "backup differs":
        put_backup(sets, b"other")
    elif case == "backup missing":
        (sets / M / "P1.json").unlink()
    elif case == "preserved differs":
        pres.write_bytes(UPGRADED[:-1])
    elif case == "preserved missing":
        pres.unlink()
    elif case == "preserved a link":
        (tmp_path / "u").write_bytes(UPGRADED)
        pres.unlink()
        pres.symlink_to(tmp_path / "u")
    else:
        pres.unlink()
        pres.mkdir()
    out = report(sets)
    rcheck(out, O.CONFLICT, "P21", "observed:", "inferred:", "unknown", rid=rid)
    assert out.outcome is not O.VERIFIED_RESTORED
    apply_writes_nothing(sets)


# ================================================================ P21, P22, P24 (Q-3, Q-6, Q-8)

@pytest.mark.parametrize("swap,displaced", [(ORIGINAL, None), (UPGRADED, "auto"), (OTHER_V1, "auto")])
def test_p21_uncertain_restore_is_a_conflict_whatever_the_disk_shows(sets, swap, displaced):
    rid, _ = attempt(sets, upgraded_lib(sets, source=ORIGINAL if swap == UPGRADED else UPGRADED), S.UNCERTAIN,
                     swap=swap, displaced=displaced)
    out = report(sets)
    rcheck(out, O.CONFLICT, "P21", "observed:", "uncertain", "unknown", rid=rid)
    apply_writes_nothing(sets)


def test_p21_uncertain_with_a_displaced_file_other_than_the_upgrade_states_the_three_facts(sets):
    urec = upgraded_lib(sets, source=ORIGINAL)
    rid, _ = attempt(sets, urec, S.UNCERTAIN, swap=OTHER_V2)
    out = report(sets)
    rcheck(out, O.CONFLICT, "P21", "remain in that attempt folder", "does not include those changes",
           "Nothing has been merged or restored automatically", rid=rid)


@pytest.mark.parametrize("source", ["OTHER_V1", "OTHER_V2", "missing", "unparsable", "ORIGINAL"])
def test_q6_failed_restore_with_a_file_other_than_the_upgrade_is_p21_never_p14_or_p15(sets, source):
    data = {"OTHER_V1": OTHER_V1, "OTHER_V2": OTHER_V2, "missing": None, "unparsable": b"{not json",
            "ORIGINAL": ORIGINAL}[source]
    rid, _ = attempt(sets, upgraded_lib(sets, source=data), S.FAILED, swap=ORIGINAL)
    out = report(sets)
    rcheck(out, O.CONFLICT, "P21", "observed:", "failed restore", "unknown")
    assert out.restores == (rid,) and out.restore_id is None, "a failed record decides nothing on its own"
    assert out.rule not in ("P14", "P15")
    apply_writes_nothing(sets)


def test_p22_restored_then_changed_but_still_format_1_is_informational(sets):
    _, rid, _ = restored_lib(sets)
    put_source(sets, OTHER_V1)
    out = report(sets)
    rcheck(out, O.VERIFIED_RESTORED, "P22", "observed:", "format 1", "differs from the restored", "inferred:",
           "changed since the restore", rid=rid)
    assert "holds the original bytes" not in out.reason, "P22 must not imply that the file still equals O"
    apply_writes_nothing(sets)


@pytest.mark.parametrize("source", ["format 2", "the upgraded bytes", "missing", "unparsable"])
def test_p22_restored_then_changed_to_anything_but_format_1_is_a_conflict(sets, source):
    _, rid, _ = restored_lib(sets)
    put_source(sets, {"format 2": OTHER_V2, "the upgraded bytes": UPGRADED, "missing": None,
                      "unparsable": b"\x00\x01"}[source])
    rcheck(report(sets), O.CONFLICT, "P22", "observed:", rid=rid)
    apply_writes_nothing(sets)


def test_p24_every_restore_failed_and_the_file_is_the_verified_upgrade(sets):
    rid, _ = attempt(sets, upgraded_lib(sets), S.FAILED, swap=ORIGINAL)
    out = report(sets)
    rcheck(out, O.VERIFIED_UPGRADED, "P24", "observed:", "failed", "P12")
    assert out.restores == (rid,) and out.restore_id is None
    apply_writes_nothing(sets)


def test_p24_with_a_damaged_backup_is_a_conflict_not_p13(sets):
    attempt(sets, upgraded_lib(sets, backup=b"damaged"), S.FAILED, swap=ORIGINAL)
    rcheck(report(sets), O.CONFLICT, "P24", "is missing or does not match the original bytes",
           "a restore would not be possible")


def test_q3_two_failed_restore_records_are_p24(sets):
    urec = upgraded_lib(sets)
    r1, _ = attempt(sets, urec, S.FAILED, swap=ORIGINAL)
    r2, _ = attempt(sets, urec, S.FAILED, preserved=None)
    out = report(sets)
    rcheck(out, O.VERIFIED_UPGRADED, "P24")
    assert set(out.restores) == {r1, r2}


def test_q3_one_failed_and_one_restored_record_are_decided_by_the_restored_one(sets):
    urec = upgraded_lib(sets, source=ORIGINAL)
    attempt(sets, urec, S.FAILED, swap=ORIGINAL)
    rid, _ = attempt(sets, urec, S.RESTORED, swap=UPGRADED)
    rcheck(report(sets), O.VERIFIED_RESTORED, "P20R", rid=rid)


@pytest.mark.parametrize("pair", [(S.RESTORED, S.EXCHANGING), (S.RESTORED, S.RESTORED), (S.PREPARING, S.PRESERVED)])
def test_q3_two_or_more_records_that_are_not_failed_are_p21(sets, pair):
    urec = upgraded_lib(sets, source=ORIGINAL)
    ids = [attempt(sets, urec, s, swap=UPGRADED)[0] for s in pair]
    out = report(sets)
    rcheck(out, O.CONFLICT, "P21", "unknown")
    for rid in ids:
        assert rid in out.reason
    assert out.restore_id is None


def test_q3_the_newest_record_is_never_chosen_by_created_at(sets):
    urec = upgraded_lib(sets, source=ORIGINAL)
    attempt(sets, urec, S.EXCHANGING, swap=UPGRADED, created_at="2026-11-03T07:00:00Z")
    attempt(sets, urec, S.RESTORED, swap=UPGRADED, created_at="2026-11-03T08:00:00Z")
    rcheck(report(sets), O.CONFLICT, "P21")


def test_recoverable_restore_rules_become_conflicts_when_the_records_folder_has_unexpected_entries(sets):
    attempt(sets, upgraded_lib(sets), S.PREPARING, preserved=None)
    (rdir(sets) / "notes.txt").write_text("x")
    rcheck(report(sets), O.CONFLICT, "P18", "unexpected entries")


# ================================================================ C-7, C-8, Q-2 and the shared scenarios

@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_new_rule_writes_nothing_on_apply_and_reports_repeatably(sets, name):
    SCENARIOS[name](sets)
    out = report(sets)  # unchanged tree and the same result twice
    assert out.rule == name.split("-")[0], (name, out.rule, out.reason)
    apply_writes_nothing(sets)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_q2_p23_is_never_emitted(sets, name):
    SCENARIOS[name](sets)
    outs = [rc.reconcile_file(sets, "P1.json"), rc.reconcile_file(sets, "V2.json")]
    lib_report = rc.reconcile_library(sets)
    outs += list(lib_report.outcomes)
    assert all(o.rule != "P23" for o in outs) and lib_report.rule != "P23"


def test_q2_p23_is_not_a_rule_in_the_source():
    src = Path(rc.__file__).read_text(encoding="utf-8")
    assert '"P23"' not in src and "'P23'" not in src


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_c7_every_restore_reason_separates_observation_from_inference(sets, name):
    SCENARIOS[name](sets)
    out = report(sets)
    if out.rule == "G6":
        assert "observed:" in out.reason
        return
    assert "observed:" in out.reason, out.reason
    if out.outcome is O.CONFLICT:
        assert "inferred:" in out.reason, out.reason


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_c8_apply_true_stays_refused_before_any_io_with_restore_evidence(sets, name, monkeypatch):
    SCENARIOS[name](sets)
    before = snap(sets.parent)
    for fn in (lambda: rc.reconcile_file(sets, "P1.json", apply=True), lambda: rc.reconcile_library(sets, apply=True)):
        with pytest.raises(ValueError, match="not available"):
            fn()
    assert snap(sets.parent) == before


def test_c5_reports_never_create_the_restores_folder(sets):
    upgraded_lib(sets)
    report(sets)
    rc.reconcile_library(sets)
    assert not restores(sets).exists()


def test_the_library_report_includes_restore_fields(sets):
    _, rid, _ = restored_lib(sets)
    o = rc.reconcile_library(sets).get("P1.json")
    assert (o.rule, o.restore_id, o.restore_status) == ("P20R", rid, S.RESTORED)


def test_p1_still_comes_first_for_an_unsafe_set_file_with_restore_evidence(sets, tmp_path):
    restored_lib(sets)
    (tmp_path / "x.json").write_bytes(ORIGINAL)
    (sets / "P1.json").unlink()
    (sets / "P1.json").symlink_to(tmp_path / "x.json")
    check(report(sets), O.UNVERIFIABLE, "P1")


def test_a_displaced_file_is_hashed_in_chunks_and_never_read_whole(sets, monkeypatch):
    """A late writer can make the displaced file any size; the report hashes it in chunks and
    reads its bytes only when a comparison needs them (never for a completed restore)."""
    _, rid, a = restored_lib(sets)
    disp = a / rr.SWAP_DIR / "P1.json"
    with open(disp, "ab") as fh:
        fh.write(b"\0" * (8 << 20))
    real = up._read_file

    def guard(path):
        assert path.name != "P1.json" or path.parent.name != rr.SWAP_DIR, "the displaced file was read whole"
        return real(path)
    monkeypatch.setattr(up, "_read_file", guard)
    out = rc.reconcile_file(sets, "P1.json")
    rcheck(out, O.VERIFIED_RESTORED, "P20R", "changed after the restore observed it", rid=rid)
    assert out.displaced_observed_at_report.size == len(UPGRADED) + (8 << 20)


@pytest.mark.parametrize("status", [S.EXCHANGING, S.RESTORED])
def test_original_bytes_that_do_not_parse_as_format_1_are_never_called_restored(sets, status):
    """Defence in depth: the "original" in a (forged) upgrade record is a format 2 file. Matching
    digests are not enough; the file must also parse as format 1."""
    put_source(sets, OTHER_V2)
    put_backup(sets, OTHER_V2)
    urec = put_record(sets, RecordStatus.UPGRADED, source=OTHER_V2, upgraded=UPGRADED)
    rid, _ = attempt(sets, urec, status, swap=UPGRADED)
    rcheck(report(sets), O.CONFLICT, "P21", "does not parse as format 1", rid=rid)
    apply_writes_nothing(sets)
