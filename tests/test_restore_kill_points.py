"""Restore killed with a real SIGKILL at each significant boundary (B7 Phase 2, S3-B2c-b, b3d).

A child process runs ``restore_file`` with ``restore._trace`` replaced so that it kills itself just
before the named system call. The parent then runs the frozen report twice: it must give the rule
the plan predicts for that disk state (state machine section 5, failure matrix part A), write
nothing, give the same answer twice, and find the locks free. Nothing the run created is ever
missing afterwards.

**A SIGKILL test proves behaviour after an abrupt process stop only.** The kernel, its page cache
and every completed system call survive it, so these are not power-loss tests (SM 0.11, OD-15),
and a kill cannot land inside a system call.
"""

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from lpbfstudio import restore as rs
from lpbfstudio import restore_record as rr

from _restore_support import F, fs_root, lock_free, make_library, names, posix, report  # noqa: F401

pytestmark = posix

SRC = str(Path(rs.__file__).resolve().parents[1])

CHILD = """
import os, signal, sys
from lpbfstudio import restore as rs
op, what, inject = sys.argv[3], sys.argv[4], sys.argv[5]
def trace(o, w=""):
    if (o, w) == (op, what):
        os.kill(os.getpid(), signal.SIGKILL)
rs._trace = trace
if inject:
    real_write = rs._write
    def write(fd, data, w):
        if w == inject:
            raise OSError(5, "EIO (injected)")
        return real_write(fd, data, w)
    rs._write = write
out = rs.restore_file(sys.argv[1], sys.argv[2])
print("returned", out.outcome.value, out.reason)
"""

# (kill just before this call, injected failure, the predicted rule of the next report)
POINTS = [
    (("mkdir", "W-01"), "", "P12"),
    (("opendir", "RS"), "", "P12"),
    (("survey", "R3"), "", "P12"),
    (("fsync", "RS"), "", "G6"),                         # W-02 done: an attempt folder without a record
    (("open", "W-05 restore.json"), "", "G6"),           # W-04 done
    (("fsync", "W-05 restore.json"), "", "P18"),         # the record written, not yet fsynced
    (("open", "W-06 P/f"), "", "P18"),
    (("write", "W-06 P/f"), "", "P18"),                  # an empty preserved copy is a 0-byte prefix
    (("read", "P/f"), "", "P18"),
    (("write", "W-07.u3 T(x)"), "", "G6"),               # inside the U-update: T(x) exists
    (("rename", "W-07.u7"), "", "G6"),
    (("fsync", "W-07.u8 A"), "", "P19"),                 # renamed, not yet fsynced
    (("open", "W-08 W/f"), "", "P19"),
    (("write", "W-08 W/f"), "", "P19"),
    (("lstat", "chain restores"), "", "P19"),            # R9-Ω
    (("write", "W-10.u3 T(x)"), "", "G6"),
    (("exchange", F), "", "P19"),                        # exchanging, not exchanged
    (("fsync", "W-12 sets"), "", "P20"),                 # exchanged, not yet fsynced
    (("fsync", "W-12 W"), "", "P20"),                    # between the two W-12 calls
    (("stat", "PX sets path"), "", "P20"),               # R11
    (("write", "W-13.u3 T(x)"), "", "G6"),
    (("rename", "W-13.u7"), "", "G6"),
    (("fsync", "W-13.u8 A"), "", "P20R"),
    (("read", "W-13.u9"), "", "P20R"),
    (("unlink", "W-07.W-16"), "W-07.u3 T(x)", "G6"),     # inside W-16: T(x) still there
    (("fsync", "W-07.u10 A"), "W-07.u3 T(x)", "P18"),    # T(x) removed, u10 not yet made
]


@pytest.mark.parametrize("point,inject,rule", POINTS, ids=[f"{p[0]}:{p[1]}" + (f"+{i}" if i else "")
                                                           for p, i, _ in POINTS])
def test_a_kill_at_each_boundary_leaves_a_state_the_report_classifies(fs_root, point, inject, rule):
    sets = make_library(fs_root[1])
    before = names(sets.parent)
    env = {**os.environ, "PYTHONPATH": SRC, "PYTHONDONTWRITEBYTECODE": "1"}
    p = subprocess.run([sys.executable, "-c", CHILD, str(sets), F, point[0], point[1], inject],
                       capture_output=True, text=True, env=env, timeout=120)
    assert p.returncode == -signal.SIGKILL, (p.returncode, p.stdout, p.stderr)
    assert lock_free(sets)
    gone = before - names(sets.parent)
    assert not gone, gone  # nothing that existed before is ever removed
    out = report(sets)
    assert out.rule == rule, (out.rule, out.reason)
    assert rr.RECORD_NAME  # the record name the report reads (frozen contract)
