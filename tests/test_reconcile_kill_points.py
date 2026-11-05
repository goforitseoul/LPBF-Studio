"""Reconciliation reports after a real upgrade process was killed at each failure point (S3-B2a).

Each test runs ``upgrade_file`` in a separate Python process with one I/O helper replaced so that
the process exits (``os._exit``, no clean-up, like a kill) at one point of the write sequence,
then checks what the disk holds and how the read-only report classifies it (the failure-point
matrix and decision table of the S3-B2 design report). The report must change nothing and must
give the same answer twice.

Synthetic data in temporary folders only.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from lpbfstudio import migration as mig
from lpbfstudio import reconcile as rc
from lpbfstudio import upgrade as up
from lpbfstudio.migration import RecordStatus
from test_reconcile_report import UPGRADED, lock_is_free, snap  # noqa: F401
from test_upgrade_file import ORIGINAL, lib, sets  # noqa: F401 (fixtures)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX primitives; Windows is refused (see upgrade.py)")

SRC = str(Path(up.__file__).resolve().parents[1])
O = rc.Outcome
M, R = up.MIGRATION_DIR, up.RECORDS_DIR

PATCHES = {
    # a record half written, then killed (F1a)
    "F1a": ("real = up._write_new\n"
            "def w(path, data):\n"
            "    if path.parent.name == up.RECORDS_DIR and not path.name.startswith('.'):\n"
            "        with up._in_dir(path) as d:\n"
            "            fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=d)\n"
            "            os.write(fd, data[:len(data) // 2])\n"
            "        os._exit(9)\n"
            "    return real(path, data)\n"
            "up._write_new = w\n"),
    # preparing written, killed before the backup (F2)
    "F2": ("real = up._write_new\n"
           "def w(path, data):\n"
           "    if path.parent.name == up.MIGRATION_DIR:\n"
           "        os._exit(9)\n"
           "    return real(path, data)\n"
           "up._write_new = w\n"),
    # backup half written, then killed (F3a)
    "F3a": ("real = up._write_new\n"
            "def w(path, data):\n"
            "    if path.parent.name == up.MIGRATION_DIR:\n"
            "        with up._in_dir(path) as d:\n"
            "            fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=d)\n"
            "            os.write(fd, data[:len(data) // 2])\n"
            "        os._exit(9)\n"
            "    return real(path, data)\n"
            "up._write_new = w\n"),
    # the backed_up record written to its temporary file, killed before its rename (F4b)
    "F4b": ("real = up._rename\n"
            "def r(src, dst):\n"
            "    if src.parent.name == up.RECORDS_DIR:\n"
            "        os._exit(9)\n"
            "    return real(src, dst)\n"
            "up._rename = r\n"),
    # backed_up, staged, killed before the replace (F5a)
    "F5a": ("real = up._stage\n"
            "def s(*a):\n"
            "    real(*a)\n"
            "    os._exit(9)\n"
            "up._stage = s\n"),
    # killed during the replace, before the rename took effect (F6a, not renamed)
    "F6a-before": ("def p(src, dst):\n"
                   "    os._exit(9)\n"
                   "up._replace = p\n"),
    # killed right after the rename (F6a renamed / F7a)
    "F7a": ("real = up._replace\n"
            "def p(src, dst):\n"
            "    real(src, dst)\n"
            "    os._exit(9)\n"
            "up._replace = p\n"),
    # the final record written to its temporary file, killed before its rename (F8a)
    "F8a": ("real = up._rename\n"
            "n = [0]\n"
            "def r(src, dst):\n"
            "    if src.parent.name == up.RECORDS_DIR:\n"
            "        n[0] += 1\n"
            "        if n[0] == 2:\n"
            "            os._exit(9)\n"
            "    return real(src, dst)\n"
            "up._rename = r\n"),
}


def kill_at(sets: Path, point: str) -> None:
    code = ("import os, sys\nfrom lpbfstudio import upgrade as up\n" + PATCHES[point] +
            "up.upgrade_file(sys.argv[1], 'P1.json')\nos._exit(3)\n")
    env = {**os.environ, "PYTHONPATH": SRC, "PYTHONDONTWRITEBYTECODE": "1"}
    assert subprocess.run([sys.executable, "-c", code, str(sets)], env=env, timeout=60).returncode == 9
    assert lock_is_free(sets)  # released by the OS


def records(sets: Path) -> list[Path]:
    return sorted((sets / M / R).glob("*.json"))


def report_twice(sets: Path):
    before = snap(sets.parent)
    out = rc.reconcile_file(sets, "P1.json")
    again = rc.reconcile_file(sets, "P1.json")
    assert snap(sets.parent) == before
    assert (again.outcome, again.rule, again.reason) == (out.outcome, out.rule, out.reason)
    return out


def test_f1a_partial_record_blocks_everything(sets):
    kill_at(sets, "F1a")
    assert (sets / "P1.json").read_bytes() == ORIGINAL and not (sets / M / "P1.json").exists()
    out = report_twice(sets)
    assert (out.outcome, out.rule) == (O.UNVERIFIABLE, "G5")
    assert rc.reconcile_library(sets).blocked is O.UNVERIFIABLE


def test_f2_preparing_without_backup_is_recoverable_as_failed(sets):
    kill_at(sets, "F2")
    assert mig.parse_record(records(sets)[0].read_text()).status is RecordStatus.PREPARING
    out = report_twice(sets)
    assert (out.outcome, out.rule, out.planned_status) == (O.RECOVERABLE, "P5", RecordStatus.FAILED)
    assert out.backup.kind == "missing"


def test_f3a_preparing_with_a_partial_backup_is_recoverable_as_failed(sets):
    kill_at(sets, "F3a")
    assert 0 < len((sets / M / "P1.json").read_bytes()) < len(ORIGINAL)
    out = report_twice(sets)
    assert (out.outcome, out.rule, out.planned_status) == (O.RECOVERABLE, "P5", RecordStatus.FAILED)
    assert out.backup.size == len(ORIGINAL) // 2


def test_f4b_preparing_with_a_record_temporary_file_is_recoverable_as_failed(sets):
    kill_at(sets, "F4b")
    assert len(list((sets / M / R).glob(".*.tmp"))) == 1
    out = report_twice(sets)
    assert (out.outcome, out.rule, out.planned_status) == (O.RECOVERABLE, "P5", RecordStatus.FAILED)
    assert out.record_status is RecordStatus.PREPARING and len(out.temps) == 1


@pytest.mark.parametrize("point", ["F5a", "F6a-before"])
def test_backed_up_and_staged_but_not_replaced_is_recoverable_as_failed(sets, point):
    kill_at(sets, point)
    assert (sets / "P1.json").read_bytes() == ORIGINAL
    out = report_twice(sets)
    assert (out.outcome, out.rule, out.planned_status) == (O.RECOVERABLE, "P7", RecordStatus.FAILED)
    assert len(out.temps) == 1 and out.temps[0].path.endswith(up.TMP_SUFFIX)


def test_f7a_replaced_but_not_recorded_is_recoverable_as_upgraded(sets):
    kill_at(sets, "F7a")
    assert mig.parse_record(records(sets)[0].read_text()).status is RecordStatus.BACKED_UP
    out = report_twice(sets)
    assert (out.outcome, out.rule, out.planned_status) == (O.RECOVERABLE, "P8", RecordStatus.UPGRADED)
    assert out.temps == ()


def test_f8a_final_record_not_renamed_is_recoverable_as_upgraded(sets):
    kill_at(sets, "F8a")
    assert mig.parse_record(records(sets)[0].read_text()).status is RecordStatus.BACKED_UP
    out = report_twice(sets)
    assert (out.outcome, out.rule, out.planned_status) == (O.RECOVERABLE, "P8", RecordStatus.UPGRADED)
    assert len(out.temps) == 1  # the record's temporary file holding the final state, not trusted
