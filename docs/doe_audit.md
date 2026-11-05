# DOE range and combination audit (Milestone B4)

Date: 2026-10-26. Code audited: commit `cd7e10b` (`src/lpbfstudio/doe.py`, the `lpbf doe` command in
`src/lpbfstudio/cli.py`, `DoeDialog` in `src/lpbfstudio/gui/dialogs.py`).
Evidence: `docs/audit/b4_doe_probe.py`, output of the pre-fix run in `docs/audit/b4_doe_probe_output.txt`.
Probe ids (R.., L.., G.., N.., C..) below refer to that output.

This document was written and committed before any DOE code was changed. Section 6 records what
was then fixed.

## 1. How DOE works today

- `parse_values(text, like)` turns one factor's level text into a list. With a `:` in the text and a
  numeric current value it reads `start:stop:step`, computes `n = round((stop − start) / step) + 1`
  and returns `round(start + i·step, 9)` for `i < n` (cast to `int` for integer fields). Otherwise it
  splits on commas, semicolons and whitespace and coerces each part to the type of the current
  value (bool, enum, int, float or str).
- `generate(base, factors, template)` checks that each path exists, takes the Cartesian product of
  the level lists in factor order (first factor varies slowest), deep-copies the base for each
  combination, clears `locked`, assigns each level with `set_path`, renders the name, overwrites the
  description with `DOE variant of <base>: p=v, …`, and finally rejects the batch if two names are
  equal.
- Names: `{base}` is replaced first by plain string substitution, then `{i}` (1-based index), a full
  factor path, or the last segment of a factor path. Values are formatted with `:g` (6 significant
  digits) and enums by their value.
- `lpbf doe` parses each `-f path=levels` into a dict, generates all variants, then saves them one by
  one with `overwrite=False` and stops at the first error. `--dry-run` prints `would create <name>`.
- The GUI dialog offers up to three numeric parameters, regenerates the whole product on every
  keystroke for its preview, marks names that already exist as "will be skipped", and on Create saves
  the others.

What works correctly today: list levels of every type (L01, L09, L11), ranges whose step divides the
span (R01, R07, R08, R16, R19), nested paths including `contour.passes.N.*` and
`hatch.infill.stripes.*` (G05, G07), unknown paths rejected (G06), product size and completeness when
levels are distinct (G01, G02, G11), out-of-range values rejected by the model (G12), `name` refused as
a factor (G15), unknown placeholders and duplicate names rejected (N04, N05), and existing sets are
never overwritten (C01: `P1_DOE2` kept its power of 111 W).

## 2. Failure modes found

Severity: High = silently produces a wrong experiment or can freeze or partly write the library;
Medium = silently surprising result or misleading report; Low = poor message, no wrong data.

### Range parser

| Id | Sev | Finding | Probe |
|---|---|---|---|
| D-04 | High | A step that does not divide the span overshoots the stop: `0.09:0.15:0.04` → `[0.09, 0.13, 0.17]`; `0.09:0.14:0.02` → `…, 0.15`. A level outside the requested window is created and named like any other. | R02, R03, C08 |
| D-05 | Medium | A reversed range (`0.13:0.09:0.02`) gives no levels; the CLI exits 0 having created nothing and printing nothing. | R04, C06 |
| D-06 | Medium | Integer fields truncate: `1:3:0.5` → `[1, 1, 2, 2, 3]` (duplicates), `0.5:2.5:1` → `[0, 1, 2]` (none of the requested values). | R17, R18 |
| D-29 | Medium | Non-finite input: `nan:1:0.1` gives a bare `ValueError`, `0:inf:1` raises `OverflowError`, which the CLI does not catch (traceback, C07). `nan` and `inf` are accepted as list levels and left to the model to judge. | R12, R13, L14, L15, C07 |
| D-30 | Low | A malformed range reports Python unpacking or conversion messages (`not enough values to unpack (expected 3, got 2)`, `could not convert string to float: '0.02,0.15'`) that do not say what form is expected. | R09, R10, R11, R15 |
| D-31 | Medium | A step below the 1e-9 rounding resolution collapses levels into duplicates (`0:1e-9:1e-12` → 1001 levels, almost all equal). | R14 |
| D-07 | High | No limit on the number of levels or variants. `0:1e9:1` would build a billion-element list before anything is checked (R20); `0:1e6:1` builds a million in 0.5 s (R21). A 100 × 10 × 5 dry run lists 5000 full set copies with no count shown first (C09). The GUI preview regenerates the whole product on every keystroke. | R20, R21, C09 |

### Level lists and types

| Id | Sev | Finding | Probe |
|---|---|---|---|
| D-32 | Medium | An empty level list (`''`, `' , ; '`) gives no levels; `generate` then returns zero variants without error. | L02, L03, G03 |
| D-33 | Medium | Duplicate levels are kept (`0.09,0.09`, `0.1,0.10`) and produce identical variants with different `{i}` names. | L04, L05, G04 |
| D-34 | Medium | Text levels are split on whitespace too: `material=Alloy A,Alloy B` becomes four levels `Alloy`, `A`, `Alloy`, `B`, so a text value containing a space cannot be a level. | L13, C04 |
| — | Info | A decimal comma (`0,09`) is read as two levels, 0 and 9, because the comma is the separator. This is the documented syntax and stays; out-of-range results such as a 9 mm hatch distance are then caught by validation, small ones are not. Listed as a limitation. | L06 |

### Generator and paths

| Id | Sev | Finding | Probe |
|---|---|---|---|
| D-35 | High | Two spellings of the same parameter (`hatch.infill.power_w` and `exposure_types.0.infill.power_w`) are accepted as two factors. Both write the same field, the second wins, and the first factor's levels (250, 300 W) are lost while the variant count still multiplies. In the CLI, giving the same `-f path=` twice silently keeps only the last one (C05). | G09, G10, C05 |
| D-36 | Medium | Factors on fields that the generator itself controls are accepted and then overridden or misapplied: `description` is overwritten by the DOE description (G13); `locked=true` produces locked variants (G14); `schema_version` can be changed (G16). | G13, G14, G16 |
| D-37 | Low | A path to a group rather than a parameter (`hatch.infill`) reaches pydantic and fails with `1 validation error for Hatch`. Out-of-range levels likewise report only the model name (`1 validation error for InFill`), not the factor and level. | G08, G12 |
| D-38 | Low | Each assignment is validated on its own sub-model; the finished variant is not re-validated as a whole (B2 introduced `with_assignments` for this, but DOE does not use it). No current model validator is affected, so this is a robustness gap, not an observed wrong result. | code review |

### Names

| Id | Sev | Finding | Probe |
|---|---|---|---|
| D-39 | Medium | A short placeholder that matches more than one factor (`{power_w}` with `hatch.infill.power_w` and `edge.power_w`) silently uses the first one; names then show only one of the two powers. | N03 |
| D-40 | Low | `{base}` is substituted before the template is parsed, so a base name containing braces (`Ti{64}`) breaks naming with a confusing error. | N08 |
| D-41 | Low | An empty template yields a variant named `''`; it fails only when saved. | N07 |
| D-42 | Medium | Two variant names that differ but map to the same file name (`M_Alloy A`, `M_Alloy_A`) are accepted by the generator; the second save fails in the middle of the run. | N10 |
| — | Info | `:g` keeps 6 significant digits, so levels differing in the 7th digit give equal names; this is caught by the duplicate-name check and the message suggests `{i}`. Kept unchanged so that existing names stay the same; listed as a limitation. | N09 |

### Saving and overwrite protection

| Id | Sev | Finding | Probe |
|---|---|---|---|
| D-21 | High | The CLI saves one by one and stops at the first name that is taken: `P1_DOE1` was created, then the run failed on `P1_DOE2`, leaving a partial design in the library (C01). The GUI instead skips taken names and creates the rest, so the existing set of that name (here with 111 W instead of the DOE value) stands in for a variant. Neither is all-or-nothing, and the two front ends disagree. | C01 |
| D-43 | Medium | `--dry-run` does not check the library: it reports `would create P1_DOE2` although that name is taken (C02), and `would create P1` for a variant named like its base (C03). | C02, C03 |
| — | OK | No probe overwrote or renamed an existing set; `save(overwrite=False)` and the B1 slug checks hold. | C01 |

### Variant count

There is no function that predicts the count, and nothing reports it before generating. Today the
count of a successful run equals the product of the level-list lengths, but only because nothing
dedups: with duplicate levels (D-33) or aliased factors (D-35) that product overstates the number of
distinct experiments.

## 3. Changes proposed (implemented in this milestone)

1. Range `start:stop:step` stays inclusive. It is rejected unless all three are finite numbers,
   `step > 0`, `stop ≥ start`, and the step divides the span (within a relative tolerance of 1e-9 on
   the number of steps); the message names the nearest valid stop values. The last level is exactly
   `stop`. For integer fields all three must be whole numbers. Fixes D-04, D-05, D-06, D-29, D-30.
2. Level lists must be non-empty, finite and distinct after parsing. Text fields split on commas and
   semicolons only; numbers, booleans and enums keep comma, semicolon and whitespace. Fixes D-31,
   D-32, D-33, D-34.
3. Size limit: a default maximum of 500 variants (`doe.DEFAULT_MAX_VARIANTS`). A range is counted
   before its levels are built and rejected if it alone exceeds the limit; the product of the level
   counts is checked before any variant is built. `count_variants(factors)` returns the predicted
   count. The CLI gains `--max-variants N` to raise or lower the limit. Fixes D-07.
4. `generate` rejects aliased or repeated factors (paths are compared after resolving an exposure
   given by position to its kind), group paths, and the fields `name`, `description`, `locked` and
   `schema_version`. Each variant is built with `with_assignments` (whole-set re-validation, base
   untouched), and an invalid level is reported as `factor <path>: level <v> is invalid: …`. Fixes
   D-35, D-36, D-37, D-38.
5. Names: `{base}` becomes an ordinary placeholder, ambiguous short placeholders are an error that
   lists the matching paths, blank names are an error. Fixes D-39, D-40, D-41.
6. Saving is checked as a whole first: `Library.check_new_names(names)` reports every name that is
   taken and every pair of names in the batch that share a file name. The CLI and the GUI save
   nothing if any conflict exists, and list all conflicts. `--dry-run` runs the same check. Fixes
   D-21, D-42, D-43.
7. The CLI prints a count line (`DOE: 3 × 2 = 6 variants`) before the variant list. The GUI dialog
   shows the count, refuses to build a preview beyond the limit, and disables Create while any name
   conflicts instead of skipping those names.

Out of scope for B4 (unchanged): energy calculations and E_eff, validation rules and thresholds,
GUI architecture, storage format.

## 4. Compatibility changes

Existing valid syntax keeps its meaning: comma, semicolon or whitespace separated numeric lists,
`start:stop:step` ranges whose step divides the span, all placeholders, the default template,
`--dry-run` and the `created <name>` / `would create <name>` lines.

Inputs that change behaviour, all of which previously produced a wrong or empty experiment:

| Input | Before | After |
|---|---|---|
| range whose step does not divide the span | extra level beyond `stop` | error naming the valid stops |
| reversed range, empty list | zero variants, exit 0 | error |
| integer range with fractional step or endpoints | truncated, duplicated levels | error |
| duplicate levels | identical variants | error |
| `nan` / `inf` | traceback or model error | error |
| same parameter twice (repeated or aliased path) | last one silently wins | error |
| factor on `description`, `locked`, `schema_version` | overridden or applied | error |
| ambiguous short placeholder | first match used | error listing the paths |
| text levels containing spaces | split into words | kept whole |
| more than 500 variants | generated | error unless `--max-variants` is raised (CLI) |
| any name taken (CLI) | partial save, exit 1 | nothing saved, all conflicts listed, exit 1 |
| any name taken (GUI) | taken names skipped | Create disabled until the template changes |
| `--dry-run` with taken names | `would create`, exit 0 | conflicts listed, exit 1 |
| CLI output | variant lines only | a count line first |

## 5. Limitations that remain

- Contour passes are still addressed by position (D-15); a DOE on `contour.passes.1.*` follows
  whatever pass is second.
- A decimal comma is read as a separator (L06).
- Names keep 6 significant digits; finer levels need `{i}` in the template.
- Only full-factorial designs; no fractional-factorial, Latin-hypercube or centre points.
- Variants record their base by name in the description only, not its revision or a hash.
- Saving is not transactional. If the disk fails part-way through saving after the conflict check
  passed, the sets already written stay. *Correction (B5):* as shipped in B4 the list of written
  and unwritten sets was printed only for library errors; a file-system error ended in a traceback.
  B5 fixed this (section 7).
- The GUI dialog still offers three numeric factors and has no way to raise the variant limit.

## 6. Result (2026-10-26)

Implemented as proposed in section 3, in `doe.py`, the `lpbf doe` command, `Library.check_new_names`
and `DoeDialog`. Probe output after the fix: `docs/audit/b4_doe_probe_output_after.txt`.

Tests: `tests/test_doe.py` (73 tests) and four DOE dialog tests in `tests/test_gui.py`. Of the 76
committed before the fix, 58 failed on the pre-fix code (`docs/audit/b4_tests_before_fix.txt`); the others pin behaviour that
already worked and must keep working (existing list syntax, nested paths, mixed types,
unknown placeholders). Full suite after the fix: 415 passed, 0 failed, 0 skipped, with
`LPBF_REQUIRE_GUI_TESTS=1`.

New public names in `lpbfstudio.doe`: `DEFAULT_MAX_VARIANTS`, `PROTECTED_FIELDS`, `count_variants`,
`count_text`, `check_levels`, `canonical_path`, `collect_factors`; `parse_values` gained
`max_levels`, `generate` gained `max_variants`, `render_name` gained `base`. In the CLI,
`--max-variants` is new and the variant count is printed first.

Measured on the container: building 500 variants takes about 0.1 s and the name check a few
milliseconds; saving 500 sets takes about 5 s on local disk, because each save scans the library
for name conflicts as introduced in B1. That cost is in the storage layer and is left for the
storage milestone.

## 7. Milestone B5: save failures, file names, exact names, decimal comma (2026-10-26)

Approved scope: items 1 to 4 of the B5 plan. Not in scope and not done: staged writes or any
change to the write order, a `--decimal-comma` option, stable contour-pass identifiers, file-format
or storage changes, deletion of saved variants. Baseline tag: `b4-accepted` (`34cb1f5`).

**Batch saving is not transactional.** The name check before saving refuses a batch with any
known conflict, but it is not a reservation. Each set file is written atomically on its own
(temporary file, then rename), so no file is ever half-written, but the batch as a whole is not:
a failure while saving leaves the variants saved before it in the library. Nothing is rolled back
or deleted.

| Item | Before (B4) | After (B5) |
|---|---|---|
| 1. Save failure | Library errors listed created / not created; a file-system error (`OSError`: disk full, permission denied) ended in a traceback with no list | `doe.save_batch` stops at the first library or file-system error and reports three lists: saved, failed (with the reason), not attempted, plus the non-transactional notice. CLI exit 1 without traceback; GUI shows the same text and keeps the saved sets |
| 2. File names | A name too long for the file system made the pre-check, `--dry-run` and the GUI preview raise `OSError`; a name that fitted but whose temporary save file did not passed the check and failed during the save | `Library.check_new_names` reports, without raising: a file name or its temporary save name longer than the file system's reported `PC_NAME_MAX`, a path longer than `PC_PATH_MAX`, a name the platform reserves (`os.path.isreserved`, where Python provides it), and a file whose existence cannot be checked. The 255-byte value is used only where the platform reports no limit. Saving still handles errors at run time (item 1), for races and limits the platform does not report |
| 3. Names | `:g` (6 significant digits) in names and descriptions; `0.1234561` and `0.1234564` both showed `0.123456` | `:g` is kept whenever it reads back as the same value, so every name that was exact is byte-identical; otherwise the shortest exact text (`0.1234567`) |
| 4. Decimal comma | `0,05` read as two levels, 0 and 5 | For decimal parameters, a comma directly between digits whose right part starts with 0 and another digit (`0,05`, `960,05`) is refused with the point form as a hint |

Compatibility: list, range and template syntax are unchanged. Inputs that now behave differently:
lists such as `0,05` (refused); names and descriptions of values that `:g` rounded (now exact);
file names the platform reports as unusable (refused before saving instead of failing during it);
CLI and GUI failure messages (new three-list text).

Tests: `tests/test_doe_b5.py` (56 tests) and three DOE dialog tests in `tests/test_gui.py`. 29 of
them failed on `b4-accepted` (`docs/audit/b5_tests_before_fix.txt`). Full suite after B5:
474 passed, 0 failed, 0 skipped, with `LPBF_REQUIRE_GUI_TESTS=1`.

Remaining limitations:

- Partial batches remain possible (failure, Ctrl+C, power loss); a Ctrl+C prints click's
  "Aborted!" without the three lists. *(Ctrl+C reporting fixed in B6, section 8.)*
- `285,5` (decimal comma with a fraction not starting with 0) is still read as two levels, 285 and
  5; the field ranges catch some such cases, the decimal-comma check does not.
- The temporary-name length assumes CPython's 8-character random part; a longer one would surface
  as a reported save failure (item 1), not a traceback.
- Without `PC_NAME_MAX` (Windows) the 255-character fallback is used, and full-path length is not
  checked unless the platform reports `PC_PATH_MAX`; reserved names are checked only where
  `os.path.isreserved` exists (Python 3.13+ on Windows).
- Contour passes are still addressed by position.

## 8. Milestone B6: Ctrl+C reporting and visible levels (2026-10-26)

Approved scope: direction 3 of the B6 plan. Rollback point: tag `b5-accepted` (`31f94dc`). Not in
scope and not done: staging, journals, rollback or any change to the write order; deleting files;
a decimal-comma mode; stable contour-pass identifiers, structural compare, schema or file-format
changes (B7 is a specification only).

**Batch saving is still not transactional.** B6 changes only what is reported after an early stop.

Defect fixed: a Ctrl+C stopped the save with click's "Aborted!". The `created` lines printed so far
could under-report: interrupted right after the second file was moved into place, the output
named only `P1_DOE1` while `P1_DOE1` and `P1_DOE2` were both in the library.

| Item | Behaviour after B6 |
|---|---|
| 1. Early stop | `doe.save_batch` stops at the first library or file-system error or at a Ctrl+C. It then reads back the file of every attempted variant: **saved** when the file holds exactly that variant; **failed** when it verifiably does not (file absent, or holding another set); **uncertain** when the file cannot be read or an earlier saved variant is no longer there, with the reason. It never guesses. A second Ctrl+C while checking marks the rest uncertain ("not verified"). **Not attempted** lists the variants after the stop. The CLI prints these four lists and exits 130 after Ctrl+C, 1 after an error. The GUI dialog shows the same text. `save_batch` returns the outcome instead of re-raising the interrupt; the CLI turns it into exit status 130. |
| 2. Levels | Every `--dry-run` prints, after the count line, one line per factor with the parsed levels (`  path: v1, v2`), taken from the same factor dict that is generated and counted and formatted like names. Shown even when the name check then fails. The GUI dialog shows the same lines under its count. Real-run output is unchanged. |

Interrupt points covered by tests: while the temporary file is written (fsync), just before the
file is moved into place, just after it is moved (first, middle and last variant), after
`save()` has returned, and during the check itself.

Compatibility: exit status after Ctrl+C is 130 (was 1); the failure report gains an `uncertain`
line and lists failed variants with their reason; dry runs gain the level lines, so the B4
assertion in `test_cli_prints_the_count_first` was updated (count line and `would create` lines
unchanged). No file, format, write-order, energy or validation change.

Tests: `tests/test_doe_b6.py` (25 tests) and two DOE dialog tests in `tests/test_gui.py`; 26 tests
failed on `b5-accepted` (`docs/audit/b6_tests_before_fix.txt`, including the updated B4 assertion).
Full suite after B6: 501 passed, 0 failed, 0 skipped, with `LPBF_REQUIRE_GUI_TESTS=1`.

Remaining limitations:

- A kill (SIGKILL, SIGTERM without a handler) or power loss gives no report and can leave a partial
  batch; Python receives no exception to act on.
- An interrupt that arrives while the report itself is printed can cut the report short; the
  library is not affected.
- `285,5` is still read as two levels (285 and 5); the dry-run level line makes it visible but
  does not prevent it. Only an opt-in decimal-comma mode or a grammar change could.
- Verification compares the saved file with the variant in memory; a concurrent writer that
  saves an identical set under the same name in the same instant would be counted as ours.
- Contour passes are still addressed by position (B7 specification).
