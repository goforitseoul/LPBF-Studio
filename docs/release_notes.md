# Release notes

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
