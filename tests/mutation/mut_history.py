"""History mutation check (ABP-0.2 onward). Each mutation edits one source file in a scratch copy, checks that every
target string occurs exactly once, runs the named test files, and reports which tests failed. A mutation is CAUGHT only if
at least one of its *expected* tests fails; a failure in any other test is listed separately and never counts. Runs on
Windows and Linux; a mutation that names ``needs`` is run only where that holds. Usage, from the candidate root:

    python tests/mutation/mut_history.py <python> <out.txt> [ids...]
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY, OUT = sys.argv[1], Path(sys.argv[2])
ONLY = set(sys.argv[3:])
H, O = "src/lpbfstudio/history.py", "src/lpbfstudio/outcomes.py"
RES, WR, OC = "tests/test_history_resolver.py", "tests/test_history_writes.py", "tests/test_history_outcomes.py"

# (id, what it violates, file, [(old, new), ...], test files, expected failing test names[, extra pytest args])
MUTATIONS = [
    ("H01", "skip the blob verification", H,
     [("    if len(back) != len(data) or sha256(back) != sha256(data):", "    if False:")],
     [WR], ["test_blob_verify_mismatch_has_no_errno"]),
    ("H02", "commit the record before the blob", H,
     [('    _write_blob(rev, data, "S5")\n', ""),
      ('    durability = _commit_record(rev, doc, "S6")\n',
       '    durability = _commit_record(rev, doc, "S6")\n    _write_blob(rev, data, "S5")\n')],
     [WR], ["test_s2_blob_failures_leave_incomplete", "test_a_kill_at_every_step_leaves_a_defined_state"]),
    ("H03", "only the head can be the current matching revision", H,
     [("        for record in x.committed:  # highest first", "        for record in x.committed[:1]:")],
     [RES], ["test_row4_history_ahead"]),
    ("H04", "never report history ahead", H,
     [("history_ahead=record.seq != x.head.seq, **base)", "history_ahead=False, **base)")],
     [RES], ["test_row4_history_ahead"]),
    ("H05", "write during resolution (adoption on read)", H,
     [("    return Resolution(PathState.UNTRACKED if present else PathState.NONE, **base)",
       "    ensure_object_root(root, object_type)\n    return Resolution(PathState.UNTRACKED if present else PathState.NONE, **base)")],
     [RES], ["test_resolve_reads_only"]),
    ("H06", "let an OSError leave phase H", H,
     [("raise StepFailed(HistoryReason.BLOB_WRITE_FAILED, exc.errno) from exc", "raise")],
     [WR], ["test_s2_blob_failures_leave_incomplete"]),
    ("H07", "reuse a revision number on a collision", H,
     [("_mkdir(revisions / seq, 0o755)\n        except FileExistsError:\n            continue",
       "_mkdir(revisions / seq, 0o755)\n        except FileExistsError:\n            return seq, revisions / seq")],
     [WR], ["test_collision_takes_the_next_number", "test_s2_reservation_collisions"]),
    ("H08", "accept a damaged object", H,
     [("    if damaged:\n        return Resolution(PathState.UNKNOWN, damaged=damaged, **base)",
       "    if False:\n        return Resolution(PathState.UNKNOWN, damaged=damaged, **base)")],
     [RES], ["test_row1_any_d5_makes_every_path_of_the_type_unknown"]),
    ("H09", "run the B2a re-check inside phase H", H,
     [("    token = library._FINAL_CHECK.set(None)\n    try:\n        library._atomic_write(path, text)\n    finally:\n"
       "        library._FINAL_CHECK.reset(token)\n", "    library._atomic_write(path, text)\n")],
     [WR], ["test_staging_clears_the_final_check_and_restores_it", "test_the_check_is_not_run_at_any_history_step"]),
    ("H10", "leave the re-check cleared after staging", H,
     [("        library._FINAL_CHECK.reset(token)\n", "        pass\n")],
     [WR], ["test_staging_clears_the_final_check_and_restores_it", "test_final_check_is_restored_when_staging_raises"]),
    ("H11", "report an unverified adoption as committed", H,
     [("        hr.adoption = AdoptionStatus.COMMITTED_UNVERIFIED\n"
       "        hr.reason, hr.errno = HistoryReason.ADOPTION_RECHECK_FAILED, exc.errno\n        return None\n",
       "        hr.adoption = AdoptionStatus.COMMITTED\n        return seq\n")],
     [WR], ["test_a9_recheck_failed"]),
    ("H12", "count a record of unknown durability as complete", O,
     [("                and self.history_reason is None          # excludes record_durability_failed\n", "")],
     [OC, WR], ["test_complete_is_true_only_for_the_first_row", "test_record_durability_failure_is_committed_and_never_complete"]),
    ("H13", "ignore the reparse-point attribute", H,
     [('    return bool(getattr(st, "st_file_attributes", 0) & _REPARSE_ATTRIBUTE)', "    return False")],
     [RES], ["test_reparse_attribute_alone_is_enough"]),
    ("H14", "predict the blob from the text", H,
     [("    data = hr.staged.data\n", "    data = text.encode()\n")],
     [WR], ["test_staged_bytes_are_read_from_the_file"]),
    ("H15", "skip the blob fsync", H,
     [("            _fsync(fh.fileno())\n    except OSError as exc:\n        raise StepFailed(HistoryReason.BLOB_WRITE_FAILED",
       "            pass\n    except OSError as exc:\n        raise StepFailed(HistoryReason.BLOB_WRITE_FAILED")],
     [WR], ["test_blob_record_and_identity_are_fsynced"]),
    ("H16", "accept a tombstone that has a blob", H,
     [('        if CONTENT in names:\n            raise _Damaged(f"{rev.name} is a tombstone with a blob")',
       '        if False:\n            raise _Damaged(f"{rev.name} is a tombstone with a blob")')],
     [RES], ["test_tombstone_with_a_blob_is_integrity_failure"]),
    ("H17", "accept any record source", H,
     [("    _valid_source(doc, int(seq), object_id)\n", "")],
     [RES], ["test_invalid_sources_fail_closed"]),
    ("H18", "accept the unapproved external rebaseline kind", H,
     [('if src["kind"] not in ("upgrade", "restore"):', 'if src["kind"] not in ("upgrade", "restore", "external"):')],
     [RES], ["test_invalid_sources_fail_closed"]),
    ("H19", "accept a gap in the revision numbers", H,
     [("    if [int(s) for s in seqs] != list(range(1, len(seqs) + 1)):", "    if False:")],
     [RES], ["test_d5_gap_in_folder_numbers", "test_d5_does_not_start_at_one"]),
    ("H20", "trust a blob without hashing it", H,
     [("    if len(blob) != record.byte_length or sha256(blob) != record.hash:", "    if False:")],
     [RES], ["test_committed_with_blob_mismatch_is_integrity_failure"]),
    ("H21", "never close a collector", O,
     [("        _COLLECTOR.reset(token)\n", "        pass\n")],
     [OC], ["test_collector_resets_after_an_exception", "test_collector_keeps_order_and_resets"]),
    ("H22", "let another thread reach a collector", O,
     [("        if sink._thread == me:\n", "        if True:\n")],
     [OC], ["test_a_copied_context_run_in_another_thread_does_not_reach_the_collector"]),
    ("H23", "create the blob non-exclusively", H,
     [("(os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr", "(os.O_WRONLY | os.O_CREAT | getattr")],
     [WR], ["test_blob_is_created_exclusively"]),
    ("H24", "catch interrupts inside phase H", H,
     [("            _fsync(fh.fileno())\n    except OSError as exc:\n        raise StepFailed(HistoryReason.BLOB_WRITE_FAILED, exc.errno) from exc",
       "            _fsync(fh.fileno())\n    except BaseException as exc:\n        raise StepFailed(HistoryReason.BLOB_WRITE_FAILED, "
       "getattr(exc, 'errno', None)) from exc")],
     [WR], ["test_interrupts_are_not_caught"]),
    ("H25", "call an operation not done without seeing the prior state", H,
     [("        shows_prior = hr.prior.shows(readback, digest) and (", "        shows_prior = True or (")],
     [WR], ["test_c2b_raised_and_other_bytes"]),
    ("H26", "skip the adoption re-check comparison", H,
     [("    if again != source:\n", "    if False:\n")],
     [WR], ["test_a8_source_changed_stops_h_and_c_still_runs"]),
    ("H27", "derive the match from the sequence and not the bytes", H,
     [("            match = readback is Readback.READ and digest == hr.target_hash",
       "            match = hr.seq is not None and readback is Readback.READ")],
     [WR], ["test_s6_returned_and_differs", "test_c1_canonical_failed_prior_bytes"]),
    ("H28", "adopt a changed-outside-history file", H,
     [("    if res.state in _ANOMALY_REASON:\n        hr.fail(_ANOMALY_REASON[res.state], res.errno)\n        return hr\n    try:\n"
       "        if res.state is PathState.NONE:",
       "    if res.state in _ANOMALY_REASON and res.state is not PathState.CHANGED_OUTSIDE:\n"
       "        hr.fail(_ANOMALY_REASON[res.state], res.errno)\n        return hr\n    try:\n"
       "        if res.state is PathState.NONE:")],
     [WR], ["test_anomalies_are_never_adopted_and_the_save_goes_on"]),
    ("H29", "follow a link inside a revision folder", H,
     [("    for name in names:\n        if not _is_regular(_lstat(rev / name)):", "    for name in ():\n        if not _is_regular(_lstat(rev / name)):")],
     [RES], ["test_symlink_as_blob_or_record"], ["-k", "not fifo"]),
    ("H30", "reattach a later file to a tombstoned object", H,
     [("    if present and any(v.state is ObjectState.D4 and v.head.path == path for v in views):\n"
       "        return Resolution(PathState.TOMBSTONED_PRESENT, **base)\n", "")],
     [RES], ["test_row6_tombstoned_file_present"]),
]


def failed_tests(text: str) -> list[str]:
    return sorted(set(re.findall(r"^FAILED \S+::(\w+)", text, re.M)) | set(re.findall(r"^ERROR \S+::(\w+)", text, re.M)))


def main() -> None:
    lines = []
    caught = total = 0
    for m in MUTATIONS:
        mid, what, target, pairs, files, expected = m[:6]
        extra = list(m[6]) if len(m) > 6 else []
        if ONLY and mid not in ONLY:
            continue
        total += 1
        text = (ROOT / target).read_text(encoding="utf-8")
        for old, new in pairs:
            n = text.count(old)
            if n != 1:
                lines.append(f"{mid} INVALID: target occurs {n} times: {old[:60]!r}")
                break
            text = text.replace(old, new)
        else:
            work = Path(tempfile.mkdtemp(prefix=f"hist{mid}-"))
            try:
                shutil.copytree(ROOT / "src", work / "src", ignore=shutil.ignore_patterns("__pycache__"))
                shutil.copytree(ROOT / "tests", work / "tests", ignore=shutil.ignore_patterns("__pycache__", "legacy_build"))
                (work / target).write_text(text, encoding="utf-8", newline="\n")
                env = {**os.environ, "PYTHONPATH": str(work / "src"), "PYTHONDONTWRITEBYTECODE": "1",
                       "LPBF_REQUIRE_GUI_TESTS": "1", "QT_QPA_PLATFORM": "offscreen"}
                where = subprocess.run([PY, "-c", "import lpbfstudio.history as r; print(r.__file__)"], cwd=work,
                                       capture_output=True, text=True, env=env).stdout.strip()
                if not os.path.realpath(where).startswith(os.path.realpath(work)):
                    lines.append(f"{mid} INVALID: the mutated copy is not the one imported ({where})")
                    continue
                p = subprocess.run([PY, "-m", "pytest", "-p", "no:cacheprovider", "-q", "-rfE", "-o", "addopts=", *extra, *files],
                                   cwd=work, capture_output=True, text=True, env=env, timeout=1800)
                failed = failed_tests(p.stdout)
                hit = [t for t in failed if t in expected]
                other = [t for t in failed if t not in expected]
                status = "CAUGHT" if hit else "SURVIVED"
                caught += bool(hit)
                lines.append(f"{mid} {status}: {what}; expected-test failures {hit}; other failures {other}; "
                             f"pytest exit {p.returncode}")
            finally:
                shutil.rmtree(work, ignore_errors=True)
    lines.append(f"TOTAL {caught}/{total} caught")
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    sys.exit(0 if caught == total else 1)  # a survivor or an invalid target fails the run


if __name__ == "__main__":
    main()
