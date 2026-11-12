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
H, O, L, RB = "src/lpbfstudio/history.py", "src/lpbfstudio/outcomes.py", "src/lpbfstudio/library.py", "src/lpbfstudio/rebaseline.py"
RES, WR, OC = "tests/test_history_resolver.py", "tests/test_history_writes.py", "tests/test_history_outcomes.py"
LIB, HX, PF = "tests/test_history_library.py", "tests/test_history_rebaseline.py", "tests/test_history_profiles.py"
J, JT = "src/lpbfstudio/jobs.py", "tests/test_jobs.py"
POSIX_ONLY = {"L14", "L25", "P02"}  # what they break can only be observed where flock and the frozen report exist
WINDOWS_ONLY = {"R15"}  # on POSIX the mutated text is the same path
EQ, OPS, LOC = "tests/test_history_r7b_equivalence.py", "tests/test_history_r7b_operations.py", "tests/test_library_locate_r7b.py"

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
     [('        if CONTENT in names:\n            raise _Damaged(f"{seq} is a tombstone with a blob")',
       '        if False:\n            raise _Damaged(f"{seq} is a tombstone with a blob")')],
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
     [("    for name in names:\n        if not _is_regular(_lstat(rev + _SEP + name)):",
       "    for name in ():\n        if not _is_regular(_lstat(rev + _SEP + name)):")],
     [RES], ["test_symlink_as_blob_or_record"], ["-k", "not fifo"]),
    ("H30", "reattach a later file to a tombstoned object", H,
     [("    if present and any(v.state is ObjectState.D4 and v.head.path == path for v in views):\n"
       "        return Resolution(PathState.TOMBSTONED_PRESENT, **base)\n", "")],
     [RES], ["test_row6_tombstoned_file_present"]),

    # ABP-0.3: the set writers
    ("L01", "run the canonical write before phase H", L,
     [("        hr = _history.phase_h_write(self.root, _SETS, self._rel(f), text, new_operation=intent.new_operation,\n",
       "        _atomic_write(f, text)\n        hr = _history.phase_h_write(self.root, _SETS, self._rel(f), text, new_operation=intent.new_operation,\n"),
      ("        _phase_c(scope, hr, lambda: _atomic_write(f, text))\n", "        _phase_c(scope, hr, lambda: None)\n")],
     [LIB], ["test_the_re_check_runs_once_after_the_record_committed", "test_abandon_at_c_on_save_keeps_the_committed_record_and_the_prior_file"]),
    ("L02", "let a history failure refuse the save", L,
     [("        _phase_c(scope, hr, lambda: _atomic_write(f, text))\n",
       "        if hr.reason is not None:\n            raise LibraryError('history failed')\n        _phase_c(scope, hr, lambda: _atomic_write(f, text))\n")],
     [LIB], ["test_no_history_failure_changes_a_save"]),
    ("L03", "skip the B2a re-check of the canonical write", L,
     [("        _phase_c(scope, hr, lambda: _atomic_write(f, text))\n", "        _phase_c(scope, hr, lambda: _history._staging_write(f, text))\n")],
     [LIB], ["test_the_re_check_runs_once_after_the_record_committed", "test_abandon_at_c_on_save_keeps_the_committed_record_and_the_prior_file"]),
    ("L04", "serialize the model a second time for the canonical write", L,
     [("        _phase_c(scope, hr, lambda: _atomic_write(f, text))\n", "        _phase_c(scope, hr, lambda: _atomic_write(f, self._text(es)))\n")],
     [LIB], ["test_the_model_is_serialized_once"]),
    ("L05", "lose the outcome of a failed canonical step", L,
     [("    except Exception as exc:\n        _report(scope, hr, exc)\n        raise\n    _report(scope, hr, None)",
       "    except Exception as exc:\n        raise\n    _report(scope, hr, None)")],
     [LIB], ["test_canonical_failure_keeps_its_own_exception_and_message"]),
    ("L06", "give no outcome for a refusal", L,
     [("        if not scope.delivered:\n", "        if False:\n")],
     [LIB], ["test_refusals_in_phase_p"]),
    ("L07", "deliver a failed write twice", L,
     [("    scope.delivered = True\n    try:\n        outcome = _history.conclude", "    try:\n        outcome = _history.conclude")],
     [LIB], ["test_the_outcome_is_in_the_sink_before_the_exception_leaves"]),
    ("L08", "let a defective report change the canonical result", L,
     [("    except _outcomes.InvalidOutcome:  # a defect", "    except ZeroDivisionError:  # a defect")],
     [LIB], ["test_an_invalid_outcome_never_changes_the_canonical_result"]),
    ("L09", "record an import overwrite (IMP-1 is undecided)", L,
     [('_Intent("import", "imported", lambda: _history.external_source(path), overwrite)', '_Intent("import", "imported", lambda: _history.external_source(path), False)')],
     [LIB], ["test_import_overwrite_of_an_existing_set_is_not_recorded"]),
    ("L10", "treat every copy source as untracked", H,
     [('    res = resolve(root, object_type, path)\n    if res.state is PathState.TRACKED:\n        return {"object_id": res.object_id, "seq": res.seq}\n    if res.data is None:',
       '    res = resolve(root, object_type, path)\n    if res.data is None:')],
     [LIB], ["test_copy_is_a_new_object_naming_its_tracked_source"]),
    ("L12", "drop the lock change", L,
     [('                                    lock_change="locked" if es.locked else None)\n        _phase_c', '                                    lock_change=None)\n        _phase_c')],
     [LIB], ["test_a_new_locked_set_records_the_lock_change"]),
    ("L13", "drop the delete re-check", L,
     [("                    check()  # rev5 §2.2 step 3a", "                    pass  # rev5 §2.2 step 3a")],
     [LIB], ["test_delete_re_checks_after_the_tombstone_committed", "test_abandon_at_c_on_delete_leaves_tombstone_and_file"]),
    ("L14", "run a save outside the writer locks", L,
     [("        with _phase_p(intent.operation, self._target_text(es.name)) as scope, self._writer_locks():",
       "        with _phase_p(intent.operation, self._target_text(es.name)) as scope:")],
     [LIB], ["test_phase_h_runs_inside_the_lock_hold"]),
    ("L16", "trust the batch cache without checking the folders", H,
     [("        if known is not None and known[0] == signature:\n", "        if known is not None:\n")],
     [LIB], ["test_inside_a_batch_a_new_revision_is_seen", "test_inside_a_batch_a_new_entry_in_an_object_is_damage"]),
    ("L17", "skip the prior head check", RB,
     [('    if res.head is None or res.head.kind != "content" or (res.head.hash, res.head.byte_length) != evidence.want_head:', "    if False:")],
     [HX], ["test_prior_head_hash_must_match_the_source", "test_restore_prior_head_must_match"]),
    ("L18", "skip the caller's expected hash", RB,
     [("    if digest != expected_sha256:\n", "    if False:\n")],
     [HX], ["test_expected_hash_must_match"]),
    ("L19", "skip the comparison of the file with its record under the locks", RB,
     [("    if (digest, len(data)) != evidence.want_bytes:\n", "    if False:\n")],
     [HX], ["test_bytes_changed_after_the_report_refuse"]),
    ("L20", "accept a changed-since-upgrade report (P14)", RB,
     [('report.rule not in ("P12", "P24"):', 'report.rule not in ("P12", "P24", "P14"):')],
     [HX], ["test_upgrade_mode_refuses_any_other_report"]),
    ("L21", "accept P22 as restored evidence", RB,
     [('or report.rule != "P20R":', 'or report.rule not in ("P20R", "P22"):')],
     [HX], ["test_restore_mode_refuses_any_other_report"]),
    ("L22", "let a rebaseline write the canonical file", RB,
     [("    source = {**evidence.source, \"prior_head_seq\": res.head_seq}\n",
       "    from . import library as _l\n    _l._atomic_write(lib.root / path, data.decode())\n"
       "    source = {**evidence.source, \"prior_head_seq\": res.head_seq}\n")],
     [HX], ["test_the_canonical_file_is_never_opened_for_writing"]),
    ("L23", "rebaseline a tracked file", RB,
     [("    if res.state is not h.PathState.CHANGED_OUTSIDE or res.claimant != object_id:",
       "    if res.state not in (h.PathState.CHANGED_OUTSIDE, h.PathState.TRACKED) or res.claimant != object_id:")],
     [HX], ["test_only_row_5_is_accepted"]),
    ("L24", "ignore which object the caller named", RB,
     [("    if res.state is not h.PathState.CHANGED_OUTSIDE or res.claimant != object_id:", "    if res.state is not h.PathState.CHANGED_OUTSIDE:")],
     [HX], ["test_object_id_must_be_the_claimant"]),
    ("L25", "run the frozen report under the writer locks", RB,
     [("    try:\n        evidence = _evidence(lib.sets_dir, file_name, mode)\n    except _Refused as exc:\n        return refused(exc.reason, exc.detail, exc.errno)\n",
       "    evidence = None\n"),
      ("            outcome = _under_lock(lib, path, mode, object_id, expected_sha256, evidence)\n",
       "            evidence = _evidence(lib.sets_dir, file_name, mode)\n            outcome = _under_lock(lib, path, mode, object_id, expected_sha256, evidence)\n")],
     [HX], ["test_real_upgrade_then_rebaseline"]),
    # ABP-0.4: machine profiles and process settings
    ("P01", "write the process file with no history", L,
     [('            self._write_settings(scope, _PROCESS, f, ps.model_dump_json(indent=2), _Intent())',
       '            _atomic_write(f, ps.model_dump_json(indent=2))')],
     [PF], ["test_first_process_save_creates_the_one_object"]),
    ("P02", "bring profile saves inside the set locks (SIDE-1 is open)", L,
     [('        with _phase_p(intent.operation, f"machines/{mp.name}.json") as scope:\n',
       '        with _phase_p(intent.operation, f"machines/{mp.name}.json") as scope, self._writer_locks():\n')],
     [PF], ["test_these_writers_take_no_lock"]),
    ("P03", "let a history failure refuse a settings save", L,
     [("                                    new_source=intent.new_source, overwrite_undefined=intent.overwrite_undefined)\n        _phase_c",
       "                                    new_source=intent.new_source, overwrite_undefined=intent.overwrite_undefined)\n"
       "        if hr.reason is not None:\n            raise LibraryError('history failed')\n        _phase_c")],
     [PF], ["test_no_history_failure_changes_a_process_save", "test_no_history_failure_changes_a_profile_save"]),
    ("P04", "record a profile import overwrite (IMP-1 is undecided)", L,
     [('_Intent("import", "imported", lambda: _history.external_source(Path(path)), overwrite)',
       '_Intent("import", "imported", lambda: _history.external_source(Path(path)), False)')],
     [PF], ["test_import_overwrite_of_an_existing_profile_is_not_recorded"]),
    ("P05", "file process settings under the wrong object type", L,
     [('            self._write_settings(scope, _PROCESS, f, ps.model_dump_json(indent=2), _Intent())',
       '            self._write_settings(scope, _MACHINES, f, ps.model_dump_json(indent=2), _Intent())')],
     [PF], ["test_first_process_save_creates_the_one_object"]),
    ("P06", "drop the outcome of a seeded default profile", L,
     [('                self._write_settings(scope, _MACHINES, f, mp.model_dump_json(indent=2), _Intent("create", "created"))',
       '                _atomic_write(f, mp.model_dump_json(indent=2))')],
     [PF], ["test_seeded_default_profile_is_created_as_one_object"]),
    # ABP-0.5: Build Job Manifest v0
    ("J01", "pin the head instead of the current matching revision", J,
     [('"object_id": res.object_id, "seq": res.seq,', '"object_id": res.object_id, "seq": res.head_seq,')],
     [JT], ["test_history_ahead_pins_the_matching_revision_with_the_exact_warning"]),
    ("J02", "accept an untracked input", J,
     [("    if res.state is not h.PathState.TRACKED:\n", "    if res.state not in (h.PathState.TRACKED, h.PathState.UNTRACKED):\n")],
     [JT], ["test_untracked_is_refused"]),
    ("J03", "drop the history-ahead warning", J,
     [("    if res.history_ahead:\n", "    if False:\n")],
     [JT], ["test_history_ahead_pins_the_matching_revision_with_the_exact_warning"]),
    ("J04", "make finalization not single-winner", J,
     [("        h._mkdir(final, 0o755)\n", "        final.mkdir(exist_ok=True)\n")],
     [JT], ["test_final_found_at_mkdir_loses_the_race_and_changes_nothing"]),
    ("J05", "skip the pinned blob's hash in validation", J,
     [('    if len(blob) != item["byte_length"] or h.sha256(blob) != want:\n', "    if False:\n")],
     [JT], ["test_hash_or_length_mismatch"]),
    ("J07", "allow a draft write after final exists", J,
     [('        if state in ("finalized", "finalization_incomplete"):\n            return refused("job_finalized")\n', "")],
     [JT], ["test_a_draft_after_final_exists_is_refused"]),
    ("J08", "skip the blob re-verification at finalization", J,
     [("    if blob != res.data:\n", "    if False:\n")],
     [JT], ["test_a_blob_that_fails_its_re_verification_refuses"]),
    ("J09", "skip the manifest read-back comparison", J,
     [("    if back != json.loads(json.dumps(doc)):\n", "    if False:\n")],
     [JT], ["test_read_back_of_another_valid_manifest_is_unverified"]),
    ("J10", "finalize a damaged job", J,
     [('    if state == "damaged":\n        return stop("job_damaged")\n', "")],
     [JT], ["test_unknown_entry_is_damage_and_refuses"]),
    ("J11", "accept an unknown manifest version", J,
     [('    if doc["manifest_version"] != MANIFEST_VERSION or isinstance(doc["manifest_version"], bool):\n', "    if False:\n")],
     [JT], ["test_unsupported_version"]),
    # R7-B: one scan per copy and per finalization (O-1), text paths in the scan (O-2), lookup without a listing (O-3)
    ("R01", "let an operation remember objects that have not settled", H,
     [("        if settle == 0 or max(signature[1], signature[3]) <= before - settle:\n", "        if True:\n")],
     [OPS], ["test_that_revision_is_seen_even_if_the_folder_times_are_put_back", "test_only_objects_at_rest_are_remembered_by_an_operation"]),
    ("R02", "settle on the object folder only", H,
     [("max(signature[1], signature[3]) <= before - settle", "signature[1] <= before - settle")],
     [OPS], ["test_either_folder_changed_lately_keeps_an_object_out_of_the_memory"]),
    ("R03", "settle on the revisions folder only", H,
     [("max(signature[1], signature[3]) <= before - settle", "signature[3] <= before - settle")],
     [OPS], ["test_either_folder_changed_lately_keeps_an_object_out_of_the_memory"]),
    ("R04", "reuse a remembered view without checking the folders (stale view)", H,
     [("        if known is not None and known[0] == signature:\n", "        if known is not None:\n")],
     [OPS], ["test_a_revision_added_by_another_process_during_a_copy_is_seen",
             "test_a_revision_added_by_another_process_during_a_finalization_is_seen"]),
    ("R05", "keep the memory after its block (a session cache)", H,
     [("        _READ_SETTLE.reset(settle)\n        _READ_CACHE.reset(token)\n", "        pass\n")],
     [OPS], ["test_the_memory_of_a_copy_is_gone_before_the_canonical_write", "test_no_memory_outside_any_block"]),
    ("R06", "keep the memory of a copy through phases C and V", L,
     [("                                        lock_change=\"locked\" if es.locked else None)\n        _phase_c(scope, hr, lambda: _atomic_write(f, text))",
       "                                        lock_change=\"locked\" if es.locked else None)\n            _phase_c(scope, hr, lambda: _atomic_write(f, text))")],
     [OPS], ["test_the_memory_of_a_copy_is_gone_before_the_canonical_write", "test_the_memory_of_a_copy_is_open_only_for_phase_h"]),
    ("R07", "give every save the memory of an operation", L,
     [("if one_scan else contextlib.nullcontext():", "if True else contextlib.nullcontext():")],
     [OPS], ["test_saves_renames_and_deletes_keep_no_memory"]),
    ("R08", "pin the bytes already in memory instead of reading the blob again", J,
     [("        blob = _read_blob(lib.root, object_type, res.object_id, res.seq)\n", "        blob = res.data\n")],
     [OPS], ["test_the_pin_reads_the_blob_itself_and_refuses_one_changed_after_the_scan",
             "test_a_finalization_examines_each_type_once_and_reads_every_pinned_blob_again"]),
    ("R09", "keep a memory open after step 2 of a finalization", J,
     [("    final = folder / FINAL\n    try:  # step 3: the single winner\n",
       "    final = folder / FINAL\n    h._READ_CACHE.set({})\n    try:  # step 3: the single winner\n")],
     [OPS], ["test_the_memory_of_a_finalization_ends_before_the_manifest_is_reserved"]),
    ("R10", "give a finalization the batch rule (no settling)", J,
     [("    with h.read_cache(h.OPERATION_SETTLE_NS):  # step 2", "    with h.read_cache():  # step 2")],
     [OPS], ["test_that_revision_is_seen_by_a_finalization_even_if_the_folder_times_are_put_back",
             "test_a_finalization_in_a_library_just_written_reads_everything_each_time"]),
    ("R11", "give a copy the batch rule (no settling)", L,
     [("_history.read_cache(_history.OPERATION_SETTLE_NS) if one_scan", "_history.read_cache() if one_scan")],
     [OPS], ["test_that_revision_is_seen_even_if_the_folder_times_are_put_back",
             "test_a_copy_in_a_library_just_written_still_reads_what_has_not_settled"]),
    ("R12", "scan twice in a copy again (O-1 undone)", L,
     [("self._write(es, self._file(new_name), scope, intent, one_scan=True)", "self._write(es, self._file(new_name), scope, intent)")],
     [OPS], ["test_a_copy_examines_history_once"]),
    ("R13", "scan once per pinned set again (O-1 undone)", J,
     [("    with h.read_cache(h.OPERATION_SETTLE_NS):  # step 2", "    if True:  # step 2")],
     [OPS], ["test_a_finalization_examines_each_type_once_and_reads_every_pinned_blob_again"]),
    ("R14", "examine the entries of a folder in the order the file system gives", H,
     [("    return sorted(os.listdir(path))\n", "    return os.listdir(path)[::-1]\n")],
     [EQ], ["test_hand_built_states"]),
    ("R15", "join scan paths with a fixed separator", H,
     [("_SEP = os.sep\n", '_SEP = "/"\n')],
     [EQ], ["test_hand_built_states", "test_every_interrupted_state_of_a_writer"]),
    ("R16", "do not examine a revision folder before listing it (scan step dropped)", H,
     [("        if not _is_real_dir(_lstat(revisions + _SEP + s)):\n", "        if False:\n")],
     [EQ], ["test_hand_built_states"]),
    ("R17", "remember a damaged or incomplete object", H,
     [("    if signature is not None and view.state in (ObjectState.D3, ObjectState.D4) and not view.incomplete:\n",
       "    if signature is not None:\n")],
     [OPS], ["test_an_object_with_an_incomplete_revision_is_never_remembered", "test_a_damaged_object_is_never_remembered"]),
    ("R18", "never try the file named after the set", L,
     [("        if self._holds(direct, name):  # the usual case", "        if False:  # the usual case")],
     [LOC], ["test_the_same_file_as_the_baseline", "test_a_set_in_its_own_file_is_found_by_reading_that_file_only"]),
    ("R19", "read the direct file again during the search", L,
     [("            if f != direct and self._holds(f, name):\n", "            if self._holds(f, name):\n")],
     [LOC], ["test_any_other_name_is_searched_for_exactly_as_at_the_baseline"]),
    ("R20", "search the other files in another order", L,
     [('        for f in sorted(self.sets_dir.glob("*.json")):\n            if f != direct',
       '        for f in sorted(self.sets_dir.glob("*.json"), reverse=True):\n            if f != direct')],
     [LOC], ["test_the_corpus_means_what_it_says", "test_the_same_file_as_the_baseline"]),
    ("R21", "take a file that cannot be parsed as the set", L,
     [("        except (OSError, ValueError):\n            return False\n", "        except (OSError, ValueError):\n            return True\n")],
     [LOC], ["test_the_same_file_as_the_baseline"]),
    ("R22", "skip the search when the direct file holds another set", L,
     [('        for f in sorted(self.sets_dir.glob("*.json")):\n            if f != direct',
       '        for f in ([] if direct.exists() else sorted(self.sets_dir.glob("*.json"))):\n            if f != direct')],
     [LOC], ["test_the_same_file_as_the_baseline", "test_a_direct_file_that_holds_another_name_is_read_first_and_once"]),
    ("L26", "allow an external rebaseline mode", O,
     [('REBASELINE_MODES = frozenset({"upgrade", "restore"})', 'REBASELINE_MODES = frozenset({"upgrade", "restore", "external"})')],
     [OC, HX], ["test_rebaseline_external_mode_cannot_be_built", "test_there_is_no_other_mode"]),
]


def failed_tests(text: str) -> list[str]:
    return sorted(set(re.findall(r"^FAILED \S+::(\w+)", text, re.M)) | set(re.findall(r"^ERROR \S+::(\w+)", text, re.M)))


def main() -> None:
    lines = []
    caught = total = 0
    for m in MUTATIONS:
        mid, what, target, pairs, files, expected = m[:6]
        extra = list(m[6]) if len(m) > 6 else []
        if (ONLY and mid not in ONLY) or (mid in POSIX_ONLY and sys.platform == "win32") or (mid in WINDOWS_ONLY and sys.platform != "win32"):
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
