# Exposure-set file format and schema versions

Each exposure set is one JSON file, `sets/<name>.json`, written from the `ExposureSet` model.
The top-level key `schema_version` names the file-format version. This page is the policy for
reading that key (B7 phase 1) and describes format 2, which gives each contour pass an id
(B7 Phase 2, stages S1+S2; design in `plans/b7p2_design_contour_pass_ids.md`, revision 2).

## Supported versions

| Value of `schema_version`              | What LPBF Studio does                                     |
|----------------------------------------|-----------------------------------------------------------|
| `1`                                    | Loads, validates and saves as format 1. Saved files hold `1`.|
| key absent                             | **Legacy policy:** read as version 1 (see below).         |
| `2`                                    | Loads, validates and saves as format 2 (contour-pass ids, below). |
| whole number above 2 (`3`, `99`, ...)  | Refused: "file format version N is not supported".        |
| anything else                          | Refused: "schema_version ... is invalid".                 |

"Anything else" means `0`, negative numbers, strings (including `"1"` and `"2"`), decimals
(including `1.0`), `true`/`false`, `null`, lists and objects. Only the JSON integers `1` and `2`
are versions.

The supported versions are `SUPPORTED_SCHEMA_VERSIONS = (1, 2)` in `src/lpbfstudio/models.py`.

**A set keeps its format.** New sets are format 1. Saving, copying, renaming, DOE variants and
import keep the source's format, and a format 2 source keeps its pass ids. `schema_version` is
read-only (`lpbf set`, `set_path`, the editor, DOE), so no ordinary edit changes a set's format.
There is no upgrade command yet; it is a separate, unapproved stage (S3).

**Read-only at the model and at the save boundary.** `schema_version` and the pass `id` are
frozen model fields: assigning either one (directly or with `assign_field`) raises a validation
error and leaves the set unchanged. Each set and pass also remembers the version and id it was read
or created with, and the library refuses to write a set in which either differs, which covers
changes made by bypassing validation (`model_copy(update=...)`, `__dict__`, `object.__setattr__`);
`with_assignments`, and therefore DOE, refuses such a set too. Saving over an existing set file
must keep that file's format: `lpbf import --overwrite` of a format 1 file over a format 2 set,
or the reverse, exits 1 with "'NAME' was not saved: its file is format 2 and this set is format 1;
...". Every refused save leaves the existing file unchanged and creates no new file. Import such a
file under another name instead.

## Format 2: contour-pass ids

Format 2 is format 1 plus one key: every contour pass has an `id`, written as the first key of
the pass object. Nothing else differs.

```json
"passes": [
  { "id": "3f9c2e1a", "name": "Contour 1", "enabled": true, "offset_mm": 0.06, ... },
  { "id": "b7d10c44", "name": "Contour 2", ... }
]
```

- An id is 1 to 32 characters `a-z`, `0-9`, `_` or `-`, starting with a letter or digit. LPBF
  Studio generates 8 random hex characters; it never derives an id from a position, a name or a
  value. Ids are unique within the contour; sets copied from one another share them.
- Ids are read-only: refused by `lpbf set`, `set_path` and as a DOE factor, shown but not editable
  in the editor. Edits, reordering and removing other passes never change an id. A pass added in
  the editor to a format 2 set gets a new random id.
- The list order is the exposure order; position is not identity.
- **Paths.** A format 2 pass can be named by id, `contour.passes.@3f9c2e1a.power_w`, or by its
  current position, `contour.passes.0.power_w` (negative indices work as before). Both name the same
  field, and DOE treats them as one factor. An unknown id is an error that lists the passes; it
  never falls back to a position. Output for format 2 sets (`lpbf show`, CSV export, `lpbf compare`,
  the DOE parameter list and validation findings) uses the id form, with no separate id row.
  Validation findings for format 2 passes also carry `pass_id` and `pass_position` in their
  context; codes, severities, messages and thresholds are the same as for format 1. Format 1 output
  is unchanged and uses the index form.
- `lpbf list` appends `(format 2)` to format 2 sets; format 1 lines are unchanged.
- **`lpbf show --filter` is a plain text match on the listed paths.** Format 2 paths use the id
  form, so an index filter such as `--filter contour.passes.1` prints nothing for a format 2 set
  (exit 0). Filter by id instead (`--filter @b7d10c44`), or by `contour.passes` for all passes.
- **Comparing a format 1 set with a format 2 set** (`lpbf compare P1 V2`, and the editor's compare
  dialog, which uses the same rows) does not match passes. The format 1 set's pass rows use the
  index form and the format 2 set's use the id form, so every pass field appears twice: once as
  `contour.passes.0.power_w  150.0  ->  —` (only in the first set) and once as
  `contour.passes.@3f9c2e1a.power_w  —  ->  150.0` (only in the second), even when the values are
  equal. The other fields are compared as usual, and `schema_version  1  ->  2` is listed. No
  position is used to pair passes. Two format 1 sets compare by position and two format 2 sets by
  id, as before. Matching passes across formats is part of the structural comparison (S5), which
  is not implemented.

**Refused id problems** (the file is listed with its reason, never written, `validate` exits 1,
import exits 1):

| Problem | Example reason |
|---|---|
| a format 2 pass without an id (or `"id": null`) | `contour pass #2 'Contour 2' has no id; format 2 files need an id on every pass` |
| malformed id | `contour pass #2 'Contour 2': id 'A.1' is invalid; ids are 1 to 32 characters ...` |
| duplicate id | `contour passes #1 and #2 share the id '3f9c2e1a'; ids must be unique within the contour` |
| an `id` key in a format 1 or legacy file | `contour pass #1 'Contour 1' has an id, which format 1 files cannot hold; the file is refused rather than guessed as format 2` |

The same rules hold in memory (some passes with ids and others without are refused), and the
writer checks every file before writing it: a set it could not read back is not saved.

**Older builds** cannot read format 2. The Phase 1 build (`a805f4f`) refuses such files with
"file format version 2 is not supported" and exit 1; `b6-accepted` and earlier skip them as
unreadable, and their `lpbf validate` still exits 0. Neither writes the file. Use one build per
library.

## Existing version-1 files keep their serialization

The guard only reads `schema_version`; it does not change how sets are written. A version-1
file written by LPBF Studio and saved again without an edit is written back byte for byte, with
`schema_version` as the first key (`indent=2`, field order of the model), exactly as before the
guard. Test: `test_version_1_file_written_by_the_app_round_trips_byte_for_byte`, which passes on
both `08dbd44` (before the guard) and the guarded code.

## Legacy policy: files without `schema_version`

A set file with no `schema_version` key is read as version 1. This is the only format there has
been, and every LPBF Studio release so far has written `schema_version: 1`, so a missing key can
only come from a hand-edited or externally generated file in the version 1 layout. The file is then
checked field by field like any other version 1 file; fields it lacks take their defaults and
unknown fields are refused, exactly as before.

- **Loading does not modify the file.** Listing, opening, showing or validating a legacy file
  never writes it, so it keeps having no `schema_version` key (bytes and modification time are
  checked in `test_file_without_schema_version_is_read_as_version_1`).
- **An explicit save adds `schema_version: 1`.** Only an explicit save of that set (an edit in
  the editor, `lpbf set`, `Library.save`) rewrites the file in the normal version-1 layout, which
  includes `schema_version: 1`. No other value changes because of this
  (`test_saving_a_legacy_file_writes_version_1`, `test_saving_a_legacy_file_unchanged_adds_only_the_key`).
- Nothing migrates files in bulk, and no command saves a set the user did not save.

## Refused files

The version is checked before any other field, so the reason given is the version and not a
field a newer format may have changed. A refused file:

- is not listed and cannot be opened, validated, copied, renamed or saved over; it is never
  written, renamed or deleted (its bytes and modification time stay the same);
- is reported with its reason: `lpbf list` prints a warning, `lpbf validate` prints
  `Future.json: FAIL, not validated (...)` and exits 1, `lpbf show/set/copy/rename` exit 1, and the
  editor's status bar names each skipped file with its reason;
- cannot be imported: `lpbf import` exits 1 with "file.json was not imported: file format version
  99 is not supported; ...", with or without `--overwrite`, and the editor shows the same reason in
  its "Import failed" dialog. The source file and any existing set are unchanged.

Malformed JSON is reported as unreadable, as before; it is not treated as a version problem.

`ExposureSet` itself refuses unsupported values, and `schema_version` is read-only (a frozen field,
also checked when saving), so `lpbf set NAME schema_version=...` and code that builds or edits a set
cannot produce a file the next start would refuse or silently change its format.

## `lpbf validate` with no names

`lpbf validate` (all sets) now fails, exit 1, when any set file could not be loaded, whatever the
reason (unsupported version, invalid JSON, a duplicate name). Before B7 phase 1 those files were
skipped silently and the command could exit 0. `lpbf validate NAME` is unchanged apart from
naming the reason when NAME's file is refused.

## Out of scope

Upgrading format 1 sets to format 2 (S3), exporting a format 2 set as a format 1 copy (S4) and
structural comparison of passes by id (S5) are designed in `plans/b7p2_design_contour_pass_ids.md`
(outside the repository) and are not implemented. Pass reordering and duplication in the editor
are not planned.
