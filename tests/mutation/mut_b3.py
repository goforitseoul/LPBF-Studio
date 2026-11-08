"""B3 mutation check (Linux only). Each mutation edits ``src/lpbfstudio/restore.py`` in a scratch copy,
checks that every target string occurs exactly once, runs the named tests, and reports which tests
failed. A mutation is CAUGHT only if at least one of its *expected* tests fails; a failure in any
other test is listed separately and never counts. Usage, from the candidate root:

    python tests/mutation/mut_b3.py <python> <out.txt>
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
F, FA, OR, FI, KP = ("tests/test_restore_file.py", "tests/test_restore_failures.py", "tests/test_restore_ordering.py",
                     "tests/test_restore_file.py", "tests/test_restore_kill_points.py")

# (id, what it violates, [(old, new), ...], test files, expected failing test name patterns)
MUTATIONS = [
    ("M01", "remove the identity check of the preserved copy's read-back",
     [("        elif (got.dev, got.ino) != ident:\n            error = f\"the read-back of {what} is another file",
       "        elif False:\n            error = f\"the read-back of {what} is another file")],
     [FA], ["test_r7b_a_foreign_identity_fails_fe6_and_gives_uncertain"]),
    ("M02", "remove the byte comparison of the set file at R4",
     [("    if (len(s.data), _sha(s.data)) != (urec.upgraded_size_bytes, urec.upgraded_sha256):",
       "    if False:")],
     [F], ["test_r4_an_in_place_change_after_the_decision_refuses"]),
    ("M03", "ignore a lock failure (continue without the locks)",
     [("        run.refuse(result, f\"{exc.detail}; nothing was written\")",
       "        return _locked(run, ctx, type(\"H\", (), {\"release\": lambda s: None, \"unlock_error\": None})())")],
     [F], ["test_lock_contention_is_refused_busy", "test_an_old_style_holder_of_m_alone_makes_restore_busy"]),
    ("M04", "change the lock order: create restores before the locks are taken",
     [("    # ---------------------------------------------------------------- R2: locks\n",
       "    # ---------------------------------------------------------------- R2: locks\n"
       "    try:\n        os.mkdir(RS, 0o755, dir_fd=m_fd)\n    except FileExistsError:\n        pass\n")],
     [F], ["test_lock_contention_is_refused_busy", "test_a_lock_error_is_refused_unverifiable_never_busy"]),
    ("M05", "move the position re-check to before the exchange (reuse R4/R8b reads after the error)",
     [("    run.U, run.i_s = s.data, (s.dev, s.ino)\n", "    run.U, run.i_s = s.data, (s.dev, s.ino)\n    run.r4_read = s\n"),
      ("        run.i_t = ident\n", "        run.i_t = ident\n        run.r8_read = _complete(run, dir_fd, run.f, what)\n"),
      ("    s = run.fe_s\n", "    s = run.r4_read\n"), ("    w = run.fe_w\n", "    w = run.r8_read\n")],
     [OR], ["test_class_a_without_matching_post_error_evidence_is_uncertain"]),
    ("M06", "skip a required fsync (u4 of every record update)",
     [("        _fsync(fd_t, f\"{op}.u4 T(x)\")\n", "        pass\n")],
     [FA, OR], ["test_a_u_update_failure_branch_by_branch", "test_a_w13_failure_is_decided_by_q_omega"]),
    ("M07", "skip the second W-12 fsync",
     [("    for label, fd in ((\"sets\", run.sets_fd), (\"W\", run.w_fd)):",
       "    for label, fd in ((\"sets\", run.sets_fd),):")],
     [F, OR], ["test_a_restore_puts_the_original_bytes_back_and_keeps_everything",
               "test_w12_both_calls_are_always_made_and_any_failure_is_uncertain"]),
    ("M08", "ignore a failed W-12 fsync",
     [("            results[label] = f\"error:{_errname(exc)}\"", "            results[label] = \"success\"")],
     [OR], ["test_w12_both_calls_are_always_made_and_any_failure_is_uncertain"]),
    ("M09", "ignore a failed record fsync (W-05)",
     [("        except OSError as exc:\n            durable_error = exc\n", "        except OSError as exc:\n            pass\n")],
     [FA], ["test_r6d_a_record_fsync_failure_with_an_exact_read_back_is_failed_c"]),
    ("M10", "stop the W-12 calls after the first failure (OD-29)",
     [("            results[label] = f\"error:{_errname(exc)}\"",
       "            results[label] = f\"error:{_errname(exc)}\"\n            break")],
     [OR], ["test_w12_both_calls_are_always_made_and_any_failure_is_uncertain"]),
    ("M11", "treat an uncertain result as confirmed after a W-12 failure",
     [("        return run.outcome(Result.UNCERTAIN, f\"{error}; recorded as uncertain\", confirmed=exch_yes, "
       "predicted=\"P21\")",
       "        return run.outcome(Result.UNCERTAIN, f\"{error}; recorded as uncertain\", confirmed=True, "
       "predicted=\"P21\")")],
     [OR], ["test_w12_both_calls_are_always_made_and_any_failure_is_uncertain"]),
    ("M12", "treat an observable but not durable record as confirmed (W-13 u8)",
     [("                               record_observed=\"restored\", record_durable=fail.record_durable, "
       "predicted=\"P20R\")",
       "                               record_observed=\"restored\", record_durable=\"yes\", predicted=\"P20R\")"),
      ("confirmed=durable and exch_yes,\n                               record_observed=\"restored\"",
       "confirmed=exch_yes,\n                               record_observed=\"restored\"")],
     [OR], ["test_a_w13_failure_is_decided_by_q_omega"]),
    ("M13", "delete the displaced file after a restore",
     [("        if intended == \"restored\":\n            return run.outcome(Result.RESTORED, f\"restored to the",
       "        if intended == \"restored\":\n            _unlinkat(run.w_fd, f, \"cleanup\")\n"
       "            return run.outcome(Result.RESTORED, f\"restored to the")],
     [F], ["test_a_restore_puts_the_original_bytes_back_and_keeps_everything"]),
    ("M14", "accept a status-only (ambiguous) record match in Q-Ω",
     [("        elif got.data == b_old and ident == id_old:", "        elif ident == id_old:")],
     [FA], ["test_a_status_only_match_is_never_accepted_by_q_omega"]),
    ("M15", "accept an ambiguous upgrade history (H5)",
     [("        if r.upgraded_sha256 is not None and r.upgraded_sha256 == urec.upgraded_sha256:",
       "        if False:")],
     [F], ["test_h5_a_failed_record_claiming_the_same_upgraded_bytes_refuses"]),
    ("M16", "ignore an extra temporary file in Q-Ω",
     [("            fail.q = \"old+tmp\" if tmp_present else \"old\"", "            fail.q = \"old\"")],
     [FA, OR], ["test_a_u_update_failure_branch_by_branch", "test_u2_failure_never_runs_w16"]),
    ("M17", "ignore an extra entry in an earlier attempt folder",
     [("    for comp, want in run.l1.items():\n        if not isinstance(comp, tuple):",
       "    for comp, want in ():\n        if not isinstance(comp, tuple):")],
     [FA], ["test_r9_a_changed_earlier_attempt_is_a_g6_condition"]),
    ("M18", "allow a non-restorable record (P13)",
     [("    if decision.rule not in (\"P12\", \"P24\")", "    if decision.rule not in (\"P12\", \"P24\", \"P13\")"),
      ("decision.outcome is not rc.Outcome.VERIFIED_UPGRADED:", "decision.outcome not in (rc.Outcome.VERIFIED_UPGRADED, "
       "rc.Outcome.CONFLICT):")],
     [F], ["test_p13_a_missing_backup_is_not_restorable"]),
    ("M19", "change a classification: write failed where uncertain is required (W-15 path)",
     [("    _failure_write(run, \"W-15\", RestoreStatus.UNCERTAIN, f\"{error}; failure checks: {listed}\", listed)",
       "    _failure_write(run, \"W-14\", RestoreStatus.FAILED, f\"{error}; failure checks: {listed}\", listed)")],
     [FA], ["test_r7b_a_foreign_identity_fails_fe6_and_gives_uncertain"]),
    ("M20", "treat ENOENT as class A (OD-17 precedence)",
     [("CLASS_C = frozenset({_errno.ENOENT})", "CLASS_C = frozenset()"),
      ("_errno.ENOTDIR, _errno.ENAMETOOLONG})", "_errno.ENOTDIR, _errno.ENAMETOOLONG, _errno.ENOENT})")],
     [OR], ["test_enoent_is_never_failed_even_with_every_fe_item_passing"]),
    ("M21", "treat EINVAL as class A (OD-21 round 4)",
     [("_errno.ENOTDIR, _errno.ENAMETOOLONG})", "_errno.ENOTDIR, _errno.ENAMETOOLONG, _errno.EINVAL})")],
     [OR], ["test_class_b_is_always_uncertain_and_never_retried"]),
    ("M22", "run W-16 after a u4 failure (OD-24)",
     [("    if fail.step in (\"u3\", \"u6\", \"u7\") and not run.w12_failed",
       "    if fail.step in (\"u3\", \"u4\", \"u6\", \"u7\") and not run.w12_failed")],
     [FA], ["test_a_u_update_failure_branch_by_branch"]),
    ("M23", "run W-16 after a W-12 failure (RC-1)",
     [("    if fail.step in (\"u3\", \"u6\", \"u7\") and not run.w12_failed and id_t is not None:",
       "    if fail.step in (\"u3\", \"u6\", \"u7\") and id_t is not None:")],
     [OR], ["test_after_a_w12_failure_w16_never_runs_whichever_step_of_w13_fails"]),
    ("M24", "retry the exchange once after an error",
     [("        _exchange(run.w_fd, run.sets_fd, f)\n    except (OSError, FsprimError) as exc:",
       "        try:\n            _exchange(run.w_fd, run.sets_fd, f)\n        except OSError:\n"
       "            _exchange(run.w_fd, run.sets_fd, f)\n    except (OSError, FsprimError) as exc:")],
     [OR], ["test_class_b_is_always_uncertain_and_never_retried", "test_class_a_with_matching_position_evidence_is_failed"]),
    ("M25", "reuse the pre-exchange listing instead of re-reading (FE-3 skips the attempt listing)",
     [("    a = _list(run.a_fd, \"FE A\")\n", "    a = {RECORD: None, PRESERVED: (\"directory\", 0) + run.i_p, "
       "SWAP: (\"directory\", 0) + run.i_w}\n")],
     [FA, OR], ["test_r7_a_g6_condition_seen_by_fe3_writes_nothing", "test_class_a_with_a_foreign_entry_in_a_is_g6_with_no_write"]),
    ("M26", "skip the R6C-Ω capture check (DG-1)",
     [("    if got != want:\n        r6p(f\"R6C-Ω: A holds", "    if False:\n        r6p(f\"R6C-Ω: A holds")],
     [FA], ["test_r6c_a_capture_precondition_failure_is_r6p"]),
    ("M27", "give R6-N on the errno alone (skip the RS listing check)",
     [("        if _entries(listing) != run.l1[\"RS\"]:\n            c3.fail(\"RS differs from L1\")",
       "        if False:\n            c3.fail(\"RS differs from L1\")")],
     [FA], ["test_r6n_prime_any_failing_check_gives_uncertain_with_no_record"]),
    ("M28", "skip the post-exchange displaced-bytes check (PX-16)",
     [("    _file_checks(check, (\"PX-13\", \"PX-14\", \"PX-15\", \"PX-16\"), w_l, w_r, run.i_s, run.U)",
       "    _file_checks(check, (\"PX-13\", \"PX-14\", \"PX-15\", \"PX-16\"), w_l, w_r, run.i_s, w_r.data if not "
       "isinstance(w_r, str) else run.U)")],
     [OR], ["test_a_displaced_file_with_other_bytes_is_uncertain_with_the_displaced_facts"]),
]


def failed_tests(text: str) -> list[str]:
    return sorted(set(re.findall(r"^FAILED \S+::(\w+)", text, re.M)) |
                  set(re.findall(r"^ERROR \S+::(\w+)", text, re.M)))


def main() -> None:
    lines = []
    caught = 0
    original = (ROOT / "src/lpbfstudio/restore.py").read_text(encoding="utf-8")
    for mid, what, pairs, files, expected in MUTATIONS:
        text = original
        for old, new in pairs:
            n = text.count(old)
            if n != 1:
                lines.append(f"{mid} INVALID: target occurs {n} times: {old[:60]!r}")
                break
            text = text.replace(old, new)
        else:
            work = Path(tempfile.mkdtemp(prefix=f"b3{mid}-"))
            try:
                shutil.copytree(ROOT / "src", work / "src")
                shutil.copytree(ROOT / "tests", work / "tests")
                (work / "src/lpbfstudio/restore.py").write_text(text, encoding="utf-8")
                env = {**os.environ, "PYTHONPATH": str(work / "src"), "PYTHONDONTWRITEBYTECODE": "1",
                       "LPBF_REQUIRE_GUI_TESTS": "1", "QT_QPA_PLATFORM": "offscreen"}
                where = subprocess.run([PY, "-c", "import lpbfstudio.restore as r; print(r.__file__)"], cwd=work,
                                       capture_output=True, text=True, env=env).stdout.strip()
                if not where.startswith(str(work)):
                    lines.append(f"{mid} INVALID: the mutated copy is not the one imported ({where})")
                    continue
                p = subprocess.run([PY, "-m", "pytest", "-p", "no:cacheprovider", "-q", "-rfE", "-o",
                                    "addopts=", *files], cwd=work, capture_output=True, text=True, env=env,
                                   timeout=3600)
                failed = failed_tests(p.stdout)
                hit = [t for t in failed if t in expected]
                other = [t for t in failed if t not in expected]
                status = "CAUGHT" if hit else "SURVIVED"
                caught += bool(hit)
                lines.append(f"{mid} {status}: {what}; expected-test failures {hit}; other failures {other}; "
                             f"pytest exit {p.returncode}")
            finally:
                shutil.rmtree(work, ignore_errors=True)
    lines.append(f"TOTAL {caught}/{len(MUTATIONS)} caught")
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    sys.exit(0 if caught == len(MUTATIONS) else 1)  # a survivor or an invalid target fails the run


if __name__ == "__main__":
    main()
