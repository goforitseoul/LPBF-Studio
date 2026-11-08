# Release notes

## Unreleased: Restore (B3, 2026-11-07)

**New**
- `lpbfstudio.restore.restore_file(sets_dir, name)` puts one upgraded set file back to the exact
  format 1 bytes of its backup, with one atomic exchange. The upgraded file is kept in the attempt
  folder (`.format1-backup/restores/<id>/swap/`), next to a preserved copy; nothing is ever deleted.
- Only a verified, unedited upgrade with an unambiguous history is restored. An edited format 2 set
  (P14) is never restored. Every result says whether it is confirmed (observed and durable) or not.
- Restore holds the library locks for its whole run: saves and upgrades refuse or report busy
  meanwhile, in this build and, through the migration-folder lock, in older builds.

**Limits**
- Linux x86_64 on ext4 or tmpfs only; refused everywhere else (Windows, FUSE and network folders
  included) before anything is written.
- No CLI or GUI command yet, on purpose: an interrupted or uncertain restore cannot yet be
  finalised or cleared (no approved recovery procedure), and blocks that file, or with G6 the whole
  library, until one exists.
- Successful `fsync` calls are what the kernel reported, not a power-loss guarantee.
- Only the bytes are restored, not the file's mode, owner, ACLs, extended attributes or times.

Details: `docs/restore.md`.

## Unreleased: delete re-check (B2b, 2026-11-07)

**Changes**
- Deleting a set checks the library locks again immediately before the file is removed. If an
  upgrade, report, apply or restore has started meanwhile, or the library folder has changed, the
  delete is refused and the set stays. The editor shows the refusal under "Not deleted"; the CLI
  exits 1.

**Unchanged, now documented**
- Renaming a set does not check again before it removes the old name, and nothing is rolled back.
  The operating restriction for older builds below covers this.
- In the rare case that closing a lock descriptor fails after a save, copy, rename, import or
  delete has succeeded, the error is reported as an `OSError` although the change was made. Read
  the set again to see its state.

Details: `docs/file_format.md`, "Changes in B2b".

## Unreleased: library locks (B2a)

**Changes**
- The set writers take library locks: saving, copying, renaming, deleting, importing a set, and
  seeding the default sets. A writer refuses, and writes nothing, while an upgrade, report, apply
  or restore holds the library, or when the file system cannot lock. The CLI exits 1 with the
  reason; the editor shows it and keeps your unsaved changes.
- An upgrade now locks the sets folder before it creates or locks the migration folder, so it
  returns busy, and creates nothing, while a save is under way.
- Reading a library never takes a lock.

**Windows.** Saves are not locked: `fcntl.flock` does not exist there. Two LPBF Studio processes
writing to one library at the same time are not coordinated. Run one at a time.

**Using this build next to an older build: operating restriction**
- Close every older LPBF Studio process that can write to the library (CLI scripts, open editor
  windows, DOE runs) before using this build on it.
- While this build uses the library, older builds must not save to it or upgrade it. Read-only
  commands may run alongside.
- There is **no full mutual exclusion** between older and newer builds, and nothing enforces this
  restriction. Older builds take no writer lock. An older upgrade that finds no migration folder is
  not coordinated with a save already under way in this build. In one tested order of that case,
  both operations reported success and the save's content was lost. This is not fixed.

Details: `docs/file_format.md`, "Library locks and older builds", and `docs/reconciliation.md`.
