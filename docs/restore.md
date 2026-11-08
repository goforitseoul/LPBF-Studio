# Restoring an upgraded set file (B3)

`lpbfstudio.restore.restore_file(sets_dir, file_name, *, now=None)` puts one format 2 set file back
to the exact format 1 bytes kept in its backup. It is the implementation of the frozen Restore
design (revision 5, with revisions 2 and 3 where revision 5 does not replace them) and of the
accepted implementation plan revision 4.7 (state machine, failure matrix and decision table).

Restore is a library function only. **There is no CLI command and no GUI action for it**, and there
is no force, batch or automatic mode. Finalising an interrupted restore and resolving a G6 block are
not part of it (see "What restore does not do").

## Supported platform

- Linux on x86_64, with glibc `renameat2`, and a local **ext4** or **tmpfs** file system.
- The file system is identified from the open sets-folder descriptor: the `fstatfs` magic, the
  mount ID in `/proc/self/fdinfo`, and a strict parse of `/proc/self/mountinfo`, which must agree.
  The migration folder, `restores`, the attempt folder and its two subfolders must be on the same
  device **and** the same mount. A mount point or bind mount inside the library is refused.
- Everything else is refused **before any write** with `refused_platform`: Windows, macOS, other
  architectures, FUSE (including cloud-synchronised folders), network and overlay file systems,
  XFS and Btrfs (not yet tested), and anything that cannot be identified completely.
  Some FUSE mounts accept the exchange flag and silently do a plain rename, which is why the
  allowlist is enforced.

## Who may be restored (eligibility)

A file is restored only when every one of these holds, observed under the locks in one baseline
round:

1. The frozen report classifies it as **P12** `verified_upgraded` (upgraded, the file still holds
   the upgraded bytes and verifies against the backup) or **P24** `verified_upgraded` (every earlier
   restore attempt failed, and the same holds).
2. Its upgrade history is unique (rules H1-H9): exactly one `upgraded` record; any other record of
   the file is `failed`, for the same original bytes and the same backup, with other upgraded
   bytes, and created no later than the upgrade completed; no other file's record names this
   backup; no record id is repeated; no orphan record temporary file or unexpected entry in the
   records folder; and no upgrade residue (a staged file, a record temporary file) for this file.
3. Two complete listings of every folder involved, taken before and after the survey, are equal.
4. The set file is read completely and holds exactly the upgraded bytes; the backup holds exactly
   the original bytes.

**Never restorable:** P13 (the backup is missing or wrong), **P14** (a format 2 file that differs
from the recorded upgrade: an edited set; a restore would discard the edits, so it is refused on
purpose), P15, P21 (an uncertain or ambiguous restore), P18-P20 (an interrupted restore), every
conflict and every G rule. A file whose restore is already confirmed (**P20R**) gives
`already_restored` and nothing is written.

## What a restore writes

Everything goes into one new attempt folder; nothing in the sets folder is created or removed.

```
<sets>/.format1-backup/restores/<restore_id>/
    restore.json          the restore record (schema version 1, frozen)
    preserved/<file>      a copy of the upgraded bytes
    swap/<file>           the staged original before the exchange; the displaced file after it
```

In order: `restores` (once, if missing), the attempt folder and its two subfolders, the record
(`preparing`), the preserved copy (then `preserved`), the staged original (then `preserved` with
its identity), a complete re-check, the record `exchanging`, **one**
`renameat2(swap, file, sets, file, RENAME_EXCHANGE)`, both folder `fsync` calls, 27 post-exchange
checks, and the final record (`restored` or `uncertain`). Each file is created exclusively, written
in full, fsynced strictly (an error is never swallowed) and read back completely; each record
update goes through a new temporary file that is fsynced and renamed into place, and is read back.

The exchange never unlinks anything: whatever was at the set file's name is moved to
`swap/<file>`, and the staged original takes its place. **The displaced file is never deleted,
moved or merged**; neither is the preserved copy, the backup, an upgrade record (never written) or
any other attempt folder.

## Outcomes

| Outcome | Meaning |
|---|---|
| `refused_invalid`, `refused_platform` | nothing was opened or written |
| `refused_busy` | another upgrade, report, apply, restore or a save holds the library; nothing written |
| `refused_unverifiable` | a lock error, an identity mismatch, an incomplete listing or read, or a G-rule block; nothing written |
| `refused_not_restorable` | not eligible (above); nothing written |
| `already_restored` | P20R; nothing written |
| `restored` | the exchange happened and every post-exchange check held |
| `failed` | no exchange was made: (a) a `failed` record is observed; (b) creating the attempt folder failed and a complete re-observation shows nothing was created; (c) a record update or record `fsync` failed before the exchange and the record reads back exactly as last written or as intended |
| `uncertain` | anything else; the result lists what was observed |

A refusal after `restores` was created leaves that **empty** folder in place; an empty `restores`
changes no report.

Every outcome carries:
- `confirmed`: true only when the claim is both **observed** (a complete read-back) and **durable**
  (every `fsync` of its durability set returned success). A read-back alone never makes it true;
- `record_observed` and `record_durable` (`yes`, `no`, `tmp-removal-not-durable`, `unknown`, `n/a`);
- `exchange_called` (`no`, `error:<errno>`, `ok`) and `exchange_durable` (`yes`, `no`, `unknown`,
  `n/a`);
- `restores_dir`, `attempt_dir`, `last_observed_op`, `last_durable_op`;
- `predicted_report`: the rule the next report is expected to give. It is a prediction, not an
  observation; only running the report confirms it;
- `observations`, each labelled observed, inferred or predicted. The two W-12 `fsync` results are
  two separate observations.

`RestoreOutcome` is a returned value only. It is not stored, and it does not exist if the process
is killed.

## Locking

From the start to the end of the run restore holds an **exclusive, non-blocking** `flock` on the
sets folder, then on the migration folder, each followed by an identity check of its path. It
never waits and never retries:
- contention (`EWOULDBLOCK`) is `refused_busy`;
- every other lock error, an unverifiable lock state, or a path identity mismatch is
  `refused_unverifiable`.

While it runs, this build's saves, copies, renames, deletes and imports are refused at once (the
editor keeps its unsaved changes), and upgrades, reports and applies of this or an older build get
`busy`. A killed restore leaves no lock behind.

At the end restore unlocks both folders and then closes every descriptor it opened, once each,
which ends the locks even if an unlock failed. A failed unlock or close never changes the outcome
already decided (its result, `confirmed`, and the record and exchange fields stay as they were); it
is added to the reason ("releasing the locks: …") and to the observations under "lock release". An
interrupt that is already on its way out is passed on unchanged. Older builds' saves take no lock and are not
coordinated: see the operating restriction in `docs/file_format.md`.

## Durability, and what it does not mean

- Every `fsync` is strict. A record `fsync` failure stops the run at once (no further write), and
  the result says the record is observable but not durable.
- After the exchange both folder `fsync` calls are always made, once each, in order, whatever the
  first returned. If either fails, the result is `uncertain`, `exchange_durable = no`,
  `confirmed: false`; no `restored` record is written, nothing is cleaned up, and the final record
  (`uncertain`) names both results. If the process is killed before that record is written, the two
  results are lost; the outcome is fail-closed either way.
- **"Returned success" is what the kernel reported. It is not a power-loss guarantee.** The tests
  kill processes with SIGKILL; they are not power-loss tests, and no claim is made about what a
  power loss leaves.

## Failures and interruptions

- **Before the exchange** a failure of a data file goes through a complete re-observation (FE-Ω):
  `failed` is written only if the stage, the record, the set file (identity and bytes), the attempt
  folder's contents and the record contract all check out; otherwise `uncertain` is written, or
  nothing when a G6 condition or an incomplete check is seen.
- **An exchange error** is decided by errno class first. Only `ENOSYS`, `EXDEV`, `EBADF`, `EFAULT`,
  `ENOTDIR` and `ENAMETOOLONG` may give `failed`, and only when both names are re-read after the
  error at their pre-exchange identities and bytes. `ENOENT`, `EIO`, `EINTR`, `EINVAL`, every
  `EROFS` and every other errno give `uncertain`. The call is never retried.
- **A failed record update** is followed by one re-read: exact bytes **and** identity decide; a
  status-only match is never accepted. The run's own temporary record file is removed only after a
  write, identity-check or rename failure (never after an `fsync` failure, never after a W-12
  failure); otherwise it is kept, and the next report gives G6.
- **A kill** at any point leaves a state the report classifies: P12 (nothing done), G6 (an attempt
  folder without a record, or a record temporary file), P18 (`preparing`), P19 (`preserved` or
  `exchanging`, not exchanged), P20 (exchanged, record not finalised) or P20R (restored).

## Retry

Nothing is retried automatically, and no attempt folder is reused or continued.
- After `failed` (P24): a new call makes a new attempt; the failed one is kept unchanged.
- After `restored`: `already_restored`.
- After `uncertain`, an interrupted restore (P18-P20) or a G6 block: a new restore is refused.
  There is no approved way out yet (see below).
- After `refused_busy`: a new call once the other operation has finished. After
  `refused_unverifiable`: only after the cause is fixed.

## File metadata

Only the bytes are restored. The restored file is a new inode created by restore, requested with
mode `0o644` (so `0o644` less the umask, unless the folder has a default ACL), owned by the user
who ran it, with new timestamps and no copied extended attributes. The displaced file keeps its own
metadata. Re-apply any mode, owner, ACL or attribute you rely on.

## After a restore

- The active set file is the original format 1 file. `swap/<file>` is an archive copy: a program
  that still had the old file open writes into it, and its changes are kept there, never applied.
  The report says so whenever the displaced file differs from what the restore observed.
- The upgrade record is unchanged. A new upgrade of the restored file is refused.

## What restore does not do (non-goals and known limitations)

- **No recovery of interrupted restores (P18-P20) and no way out of G6.** These designs (OD-6,
  OD-7) are not decided. An interrupted or uncertain restore blocks that file, and a G6 condition
  blocks the whole library for reports, upgrades and restores until a recovery procedure is
  approved. **This is why restore is not exposed in the CLI or the GUI.**
- Writers that ignore the locks (other programs, older builds' saves, sync tools, other machines)
  are not coordinated. Restore detects some of their changes (identity, bytes, listings) and keeps
  every version, but detection is not guaranteed: an in-place write that keeps the identity, inode
  reuse, a write after restore's last read, or a change that is undone between two reads (ABA) may
  go unseen.
- The listing stability check compares the folder before and after listing. It is a bounded
  operational check, not an atomic snapshot: a change within the timestamp granularity can be missed.
- A folder replaced in the single system call before the exchange is detected afterwards
  (`uncertain`), not prevented.
- The two W-12 `fsync` results have no field in the frozen record; they are kept as text in an
  `uncertain` record's error and in the returned outcome only.
- Older builds' reports (before B2c-a) classify restore evidence wrongly but safely, and write
  nothing for it.
