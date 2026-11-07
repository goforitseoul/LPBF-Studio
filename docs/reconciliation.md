# Reconciling interrupted format 2 upgrades

Status: S3-B2a (read-only report) and S3-B2b (record-only finalisation, with the Q-12
correction) were both accepted with follow-up on 2026-11-01 (`docs/audit/b7p2_s3b2/s3b2b_acceptance.md`).
Neither is committed yet. There is no CLI or GUI for either. The approved design and its review
trail are in `docs/audit/b7p2_s3b2/`.

S3-B2c-a (2026-11-03) extends **the report only** to read restore evidence; see "Restore
evidence in the report" below. the project owner accepted it with follow-up on 2026-11-04
(`docs/audit/b7p2_s3b2c/s3b2c_a_acceptance.md`); it is not committed. **Restore itself (S3-B2c-b) is not
implemented**: nothing in this build creates, exchanges, finalises or removes restore evidence.
The frozen Restore design is `docs/audit/b7p2_s3b2c/design/report_rev5.md`.

## Two separate capabilities

| | Report (S3-B2a) | Apply (S3-B2b) |
|---|---|---|
| Module | `lpbfstudio.reconcile` | `lpbfstudio.reconcile_apply` |
| Entry points | `reconcile_file(sets_dir, name)`, `reconcile_library(sets_dir)` | `apply_file(sets_dir, name, now=None)` (one file only) |
| Writes | nothing, ever (`apply=True` is refused before any I/O) | at most one migration record per call |
| Lock | the library upgrade lock, never creating the migration folder | the same lock |

Both use the same decision table: global rules G1-G6, then per-file rules P1-P24, first match
wins (design §6.2; P14 as approved, see the addendum; G6 and P18-P24 from S3-B2c-a, with P23
retired).

## What apply writes

Only three rules write, and only when the report calls the file `recoverable`:

| Rule | Record | Evidence | Written |
|---|---|---|---|
| P5 | preparing | the file holds the original bytes | `failed`, error "the upgrade was interrupted before its backup was verified; the file is unchanged (finalised by reconciliation)" |
| P7 | backed_up | the file holds the original bytes | `failed` via `confirm_replace(SOURCE)`: "the file was not replaced; it is unchanged" |
| P8 | backed_up | the file is the upgraded bytes, parses as format 2, verifies against the backup, and the backup holds the original bytes | `upgraded` via `confirm_replace(UPGRADED)` |

Everything else writes nothing, including:
- conflicts;
- unverifiable and busy libraries;
- `uncertain` records;
- stale `failed` records;
- completed upgrades (P12 to P15, including P14);
- a P5, P7 or P8 case that an unexpected entry or an unsafe temporary file turned into a conflict.

**The write boundary.**
- The one write replaces the decided record through `upgrade._update_record`. That function
  re-checks the record's identity and bytes, writes an exclusively created temporary file,
  renames it over the record, and reads it back.
- The only removal is that temporary file, and only when its own rename fails.
- Never written, moved, renamed or deleted:
  - set files;
  - backups;
  - staged files;
  - other records;
  - temporary files left by earlier runs;
  - unexpected entries.
- No pass id is generated.

**Re-checks.** Just before writing, apply reads these again under the lock:
- the record;
- the file;
- the backup, for P8.

Each must be the same file (device and inode) with the same bytes that decided the rule.
Otherwise the result is `refused` and nothing is written.

**Results.**

| Result | Meaning |
|---|---|
| `finalised` | the record was written and read back as written |
| `no_write` | the rule never writes |
| `refused` | a re-check or the write failed, and the record was read again and found unchanged |
| `unverified` | the write failed and the record's state is unknown; run the report again |

**Idempotent.** After a write, the next run matches a rule that writes nothing: P5 and P7 become
P16, and P8 becomes P12.
- **Killed before the rename:** the old record and a temporary file remain. The next run writes
  the record again. The temporary file is reported and kept.
- **Killed after the rename:** the record is final.

## Accepted limitations (carried forward from the S3-B2a final review)

These were accepted as follow-ups when S3-B2a was accepted. S3-B2b does not change them.

- **L-1. A staged file can be hidden by P1.** When a record names a backup path that this build
  never writes, P1 returns before the file's evidence is gathered, so a staged upgrade file of
  that set is not listed in the outcome. The outcome is still `unverifiable` and nothing is
  written.
- **L-2. Globally blocked reports list no evidence.** When G3, G4 or G5 decides for the whole
  library, the report has no per-file outcomes, so it does not list orphan staged files or other
  entries.
- **L-3. Orphan record temporary files act library-wide.** A record temporary file whose record
  is missing turns every set without a record into a `conflict` (under G2 or P2), including
  unrelated sets. This is conservative and deliberate, but not required. It could be narrowed to
  the set named in the temporary file's `source_path` when that file is a complete, valid record.
  Narrowing it needs its own decision, so it is not done here. The `reconcile.py` module
  docstring does not mention the library-wide scope.
- **L-4. Test gaps in the report tests:**
  - several orphan record temporary files;
  - recorded and unrecorded files mixed in one library report;
  - the L-1 case;
  - the L-2 case with residue present.

## Restore evidence in the report (S3-B2c-a)

A future Restore keeps one attempt folder per attempt:

```
.format1-backup/restores/<restore_id>/
    restore.json        the restore record (lpbfstudio.restore_record, schema version 1)
    preserved/<file>    a copy of the upgraded bytes, made before the exchange
    swap/<file>         the staged original before the exchange; the displaced file after it
```

**What the report does with it:**
- It reads `restores/` under the same lock as before, and never creates it.
- **C-1.** A library without `restores/`, or with an empty one, is classified exactly as before. An
  empty `restores/` is not evidence and is not listed as an unexpected entry. An early Restore
  refusal (S3-B2c-b) may leave one behind, and it is never removed.
- **Ordering (C-3).** A restore record that refers to a file's `upgraded` record means P12-P15 are
  never reached for that file. The reference only does that: it proves nothing about the restore.
  The rule is decided by the set file, the backup, the preserved copy and the swap file, read by
  this report. P1-P4 still come first, because they establish that there is safe evidence and a
  single `upgraded` record.
- **Writes.** Nothing, ever. `apply=True` stays refused (C-8). Apply writes nothing for any of the
  new rules (C-2): its gate is unchanged (P5, P7 and P8 only) and `reconcile_apply.py` is
  unchanged.

**G6 blocks the whole library** (after G5, like G5). There are no per-file outcomes. These all
cause it:
- `restores` that is a link or not a folder;
- an entry in `restores` that is not a 32-hex attempt folder;
- an attempt folder without `restore.json`, `preserved/` or `swap/`;
- any other entry in the attempt folder, or in `preserved/` or `swap/`;
- a restore record that is unreadable, invalid, of another version, misnamed, a link,
  hard-linked or not a regular file;
- a restore record whose upgrade record is missing or not `upgraded`;
- a restore record whose source path, backup path, upgraded (current) digest and size, or original
  (restored) digest and size differ from its upgrade record (Q-4).

**Q-7, strict.** A leftover `restore.json` temporary file (a restore killed while updating its
record) is an unknown entry, so G6 applies. The whole library then stays report-blocked until a
person looks at it:
- nothing cleans it up;
- nothing forces a classification;
- nothing deletes or repairs anything.

**Per-file rules.** Restore records are grouped by the upgrade record they refer to. Times are
never used to pick one (Q-3).

| Rule | Situation | Outcome |
|---|---|---|
| P18 | one `preparing` record; the file is the upgraded bytes; the preserved copy absent, partial or complete; no swap file | `recoverable`, `restore_planned_status` = failed |
| P19 | one `preserved` record (the preserved copy equals the upgrade, the swap file absent, partial or the original), or one `exchanging` record whose bytes show no exchange (the file is the upgrade, the swap file is the original) | `recoverable`, `restore_planned_status` = failed |
| P20 | one `exchanging` record whose bytes show the exchange: the file is the original and parses as format 1, the swap file is the upgrade, the backup the original, and the preserved copy the upgrade | `recoverable`, `restore_planned_status` = restored |
| P20R | one `restored` record, with the same evidence as P20 | `verified_restored`, with the displaced file compared (below) |
| P21 | an `uncertain` record; two or more records that are not `failed`; only `failed` records but the file is not the upgrade (Q-6, never P14 or P15); a `restored` record whose backup or preserved copy is wrong, missing or unsafe (Q-5); any other combination | `conflict`; the outcome is unknown |
| P22 | one `restored` record and a file that differs from the original | format 1: `verified_restored`, informational (Q-8). Otherwise (format 2, the upgrade, missing, unreadable): `conflict` |
| P23 | **retired**: superseded by G6 (whole-library block). Never emitted, and its code is not reused (Q-2) | — |
| P24 | only `failed` records, and the file is the upgrade | judged as P12: `verified_upgraded`, rule P24, when the file and backup verify (a new restore attempt is allowed); `conflict`, rule P24, otherwise |

**What P24 and P21 mean, and what they do not (accepted readings I-1 and I-3).**
- **P24 keeps its own rule code** whenever every restore record for the upgrade is `failed`.
  - If the P12 conditions hold, the outcome is `verified_upgraded` under P24, never under P12.
  - Otherwise it is `conflict` under P24, with the P13-style backup reason or the verification
    reason.
  - P24 is **not** evidence that a restore succeeded, and **not** evidence that no restore
    happened. It says only that the recorded attempts failed and that the file now verifies (or
    does not verify) as the upgrade.
- **G6 has global precedence.** It is decided for the whole library before any per-file rule.
- **P21 is file-level only.** It applies to evidence that passed every global check but does not
  satisfy one of the defined, safe restore classifications (P18, P19, P20, P20R, P22, P24).
  - The evidence rules are strict and fail closed. For example:
    - P18 requires no swap file;
    - P19 requires the preserved copy to equal the upgrade;
    - a partial swap file counts only as a prefix of a backup that equals the original;
    - P20 and P20R require the file to parse as format 1.
  - Anything that does not meet a defined rule exactly is P21, never a weaker classification.

**P18-P20 and `restore_planned_status` (Q-9).**
- `restore_planned_status` is report-only. It names the restore record status that a later stage
  would write (P18 and P19 → failed, P20 → restored; `reconcile.RESTORE_PLANNED`).
- It does not mean the record was finalised, and it is not an apply instruction.
- Finalising restore records (D-6) is **deferred**. Apply returns `no_write` for P18-P20. The
  upgrade field `planned_status` stays None.

**The displaced file** (revision 3 §§4-5).
- After a confirmed restore, the active set file is the restored original. `swap/<file>` is kept as
  an archive copy.
- A program that still had the old file open writes into the displaced file, not the set file.
- The record holds `displaced_observed_at_restore`, which is never changed. The report shows
  `displaced_observed_at_report`, which is never stored.
- When their digests differ, the report says, in fixed words, that:
  - the displaced file changed after the restore observed it;
  - the changed bytes remain in the attempt folder;
  - the active set file does not include those changes;
  - nothing has been merged or restored automatically.
- It infers nothing about when, by what, or why.
- A different file (another inode) with the same bytes is stated as such. A missing or non-regular
  displaced file is reported, and the active set file is unaffected.
- Displaced files are read only in chunks, for their digest. Device and inode are taken before
  and after hashing (a change in between is reported as unreadable). This does **not** make the
  report an atomic snapshot, and it does not remove races with writers that ignore the lock.
- Nothing ever merges, copies back, moves or deletes a displaced file.

**P22 wording.** "Verified restored" with P22 describes the earlier restore, not the current file.
The current file is not the original. It is not eligible for a new upgrade or restore because of
this outcome.

## Other known limits

- **P14** accepts any format 2 file under an `upgraded` record. The file is not compared with the
  upgrade or the backup (`docs/audit/b7p2_s3b2/addendum_m1_p14.md`). Apply never writes for it.
- **The window for writers that ignore the lock.** A program that ignores the lock can still
  change the file between apply's re-check and the record's rename. The window is a few system
  calls. The operating precondition is unchanged: nothing else writes to the library during an
  upgrade or a reconciliation.
- **No library-wide apply.** Batch processing is not authorized, so apply works on one named file
  at a time.
- **Q-12 (fixed 2026-11-01).** When updating a record fails, the upgrade now reads the record once
  more, without writing anything, and reports the state the disk holds ("it reads back as
  upgraded"), or says that the state is unknown, with `record` set to None. It no longer says "it
  stays backed_up" from memory. The result (uncertain or failed) is unchanged.
- **S3-B2b follow-up 1 (LOW). Re-checks before an apply write do not re-list the records folder or re-check the record's
  link count.** A lock-ignoring program that adds an entry there, or hard-links the record, after
  the decision does not stop the write. The other link keeps the old record bytes, because the
  record is replaced by a rename.
- **S3-B2b follow-up 2 (LOW). An invalid `now`** (for example, earlier than the record's `created_at`) makes `apply_file`
  raise `RecordError` before anything is written.
- **Windows is refused** for both the report and apply (G1).
- **Older and newer builds on one library (B2a).** This build's set writers take shared locks on the
  sets folder and, when it exists, the migration folder. Report and apply keep their
  migration-folder lock only (OD-2): with the migration folder present they exclude this build's
  writers; with it absent they write nothing (G2). Operating restriction:
  - close every older LPBF Studio process that can write to the library before this build uses it;
  - while this build uses the library, older builds must not save to it or upgrade it; read-only
    commands, reports without apply included, may run alongside;
  - **there is no full mutual exclusion between older and newer builds, and nothing enforces this.**
    An older save is never coordinated. An older upgrade that finds no migration folder is not
    coordinated with a save already under way in this build. In one tested order of that case,
    both reported success and the save's content was lost (neither in the set file nor the
    backup). This is not fixed. Details: `docs/file_format.md`, "Library locks and older builds".
- **S3-B2c-a reads restore evidence only.** No Restore, no D-6 finalisation, no force, batch or
  automatic restore, and no CLI or GUI exist.
