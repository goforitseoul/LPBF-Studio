# Revision history of a library

LPBF Studio keeps a history of every set, machine profile and process file beside the files themselves, in `<library>/.history/`. The files in `sets/`, `machines/` and `process.json` are written exactly as before. Older builds ignore the folder.

## What is kept

Each explicit write through LPBF Studio adds one revision: the exact bytes of the file, and a record of what produced it. Revisions are numbered per object and are never changed or removed. A delete adds a marker and keeps the earlier content. Nothing purges history, so the folder grows with every save.

A file that was already in the library has no history until LPBF Studio first writes it. That write first keeps the file's exact bytes as revision 1. Reading, listing, validating or comparing never adds anything.

## States of a file

| State | Meaning |
|---|---|
| tracked | The file's bytes match a revision of its object |
| tracked, history ahead | The file matches an older revision; a newer one exists that does not match it |
| untracked | No object claims the file yet |
| changed outside history | An object claims the file, and no revision matches it |
| missing | An object claims the file, and the file is absent |
| deleted, file present | The object was deleted, and a file is at its old path. It is never attached to that object again |
| unknown | The history of that kind of file is damaged, or two objects claim the file |
| unverifiable | The history or the file could not be read |

LPBF Studio never guesses, repairs or deletes anything in `.history/`. Any damage makes every file of that kind `unknown` until it is dealt with.

## When history cannot be written

A save never fails because of its history. If a history step fails, or the file is in one of the last five states above, the file is still saved and the save is reported as not recorded. A file changed outside history stays that way: later saves are not recorded either.

After an upgrade or a restore, a tracked file can be left changed outside history, because those operations do not write history. An internal operation can add a revision for such a file when the upgrade or restore records prove what happened. It has no command or menu entry yet. Files edited by hand or by other tools have no such route.

## Build job manifests

A build job names the sets (with a role and an optional Z range), the machine profile and the process file it uses. Finalizing it pins the exact revision of each one, and is refused if any of them is not tracked. A later save does not change what was pinned. Validating a manifest checks every pinned revision again and never substitutes another one.

A manifest records which bytes a build used. It says nothing about whether the parameters are qualified or the build can run. Jobs have no command or menu entry yet.

## Limits

- One writing platform and one build generation per library. Do not share a library between Windows and Linux, or through a synced folder.
- On Windows, saves are not locked, and two processes saving at once are not coordinated. Their history can end up `unknown`.
- Machine profile and process saves are not locked on any platform.
- Saves take longer as a library's history grows, because each one verifies the stored revisions of that kind of file.
- No guarantee is made against power loss.
- The command line and the editor do not show the history status yet.
