# LPBF Studio 0.1.0: implementation audit and plan (Milestone A)

Audit date: 2026-10-26. Scope: the code at `lpbf-studio/` in the project folder, version 0.1.0.
No code was changed for this audit. The probe scripts that reproduce each confirmed defect are in
[`docs/audit/`](audit/) (`probe.py`, `probe_gui.py`) together with their captured output.

Contents

1. Repository audit
2. README feature-to-code matrix
3. Test baseline
4. Defects and risks (confirmed defects, then unverified assumptions, then architectural weaknesses)
5. Calculation audit
6. Proposed target architecture
7. Recommended changes, ranked
8. Milestone plan with acceptance criteria
9. Recommended first implementation task

How to read the labels

- **Confirmed defect (D-nn)**: reproduced by running code (probe id in brackets) or directly visible in the
  source with no interpretation needed. Each has a file and line reference.
- **Unverified engineering assumption (A-nn)**: the code does something plausible, but whether it is
  *correct* depends on process physics or on how a commercial build processor behaves, which this audit
  cannot verify. These need an engineering decision or a reference, not a bug fix.
- **Architectural weakness (W-nn)**: not wrong today, but blocks the requirements in the task brief.
- **Missing (M-nn)**: required by the brief, not present at all.

Severity: **Critical** (silent loss or corruption of user data, or a safety-relevant number that is
wrong), **High** (wrong result or broken protection that a user can hit in normal use), **Medium**
(wrong or misleading in an edge case), **Low** (cosmetic or maintainability).

---

## 1. Repository audit

### 1.1 Layout and size

| Path | Lines | Role |
|---|---:|---|
| `src/lpbfstudio/models.py` | 416 | Pydantic v2 domain model; parameter metadata (label, unit, guard, help, min/max) lives in `Field(json_schema_extra=…)` |
| `src/lpbfstudio/calc.py` | 213 | Energy input, power reduction, skin ridge, corridor angle, hatch angles, offsets, edge geometry |
| `src/lpbfstudio/validation.py` | 152 | Rule checks returning `Issue(severity, path, message)` |
| `src/lpbfstudio/paths.py` | 116 | Dotted parameter paths: get/set/flatten/diff, string coercion |
| `src/lpbfstudio/doe.py` | 73 | Full-factorial DOE and name templates |
| `src/lpbfstudio/estimate.py` | 82 | Build-time estimate for a prismatic block and z-segments |
| `src/lpbfstudio/library.py` | 145 | Folder of JSON files, one per set; `machine.json`, `process.json` |
| `src/lpbfstudio/defaults.py` | 57 | Three locked example sets |
| `src/lpbfstudio/cli.py` | 299 | Click CLI `lpbf` (also holds `write_csv`) |
| `src/lpbfstudio/gui/app.py` | 713 | Main window: library list, structure tree, form, analysis tabs |
| `src/lpbfstudio/gui/forms.py` | 178 | Form generated from model metadata |
| `src/lpbfstudio/gui/dialogs.py` | 319 | DOE, compare, estimate, calculators |
| `tests/test_core.py` | 190 | 20 tests: calc, model, validation, paths, DOE, library, estimate, one CLI flow |
| `tests/test_gui.py` | 42 | 2 offscreen GUI tests |

Total: about 3 000 lines, of which about 1 200 are GUI.

### 1.2 Stack, dependencies and licences

| Dependency | Declared | Installed for the baseline | Licence | Notes |
|---|---|---|---|---|
| Python | `>=3.10` | 3.13.17 | PSF | Only 3.13 was exercised; 3.10 is untested |
| pydantic | `>=2.5` | 2.14.1 | MIT | |
| click | `>=8.1` | 8.5.0 | BSD-3-Clause | |
| PySide6-Essentials (extra `gui`) | `>=6.6` | 6.12.0 | LGPL-3.0 (or commercial) | Redistribution in an installer carries LGPL obligations (dynamic linking, licence text, allow relinking). No third-party notice file exists yet. |
| pytest (extra `dev`) | `>=7` | 9.1.1 | MIT | |
| SQLite | not used yet | (stdlib `sqlite3`) | Public domain | Needed for Milestone D, no new dependency |

`pyproject.toml` declares `license = { text = "Proprietary" }`. That is compatible with the above if the
LGPL terms for Qt are met; it needs a `THIRD_PARTY_LICENSES` file before any release (Milestone F).

### 1.3 Repository hygiene

- **No version control.** The project folder is not a git repository. The same source exists three
  times: the folder, `lpbf-studio-0.1.0.zip` (byte-identical to the folder) and `install_lpbf_studio.py`
  (embeds every file; identical except `docs/editor.png`). Any change from Milestone B onward will make
  these copies diverge. This is the first thing to fix (see section 9).
- No CI, no linter or type-checker config, no `CHANGELOG`, no `THIRD_PARTY_LICENSES`.
- `README.md` says "22 tests (core, CLI, GUI offscreen)". That count is accurate, but see 3.2: two of
  them are skipped without a visible failure when Qt's system libraries are missing.

### 1.4 Separation of core and GUI

The core package (`models`, `calc`, `validation`, `paths`, `doe`, `estimate`, `library`, `defaults`)
does not import PySide6. Good. Two layering leaks:

- `gui/app.py:681` imports `write_csv` from `cli.py`. Export belongs in the core.
- Lock enforcement is spread across `Library.save` (`library.py:72-79`), the CLI (`cli.py:114-115`) and the
  GUI (`ModelForm(readonly=…)`, `app.py:349`). There is no single domain-level guard.

### 1.5 Provenance of domain knowledge

The project notes (`.notes/inputs.md`) record that domain details (offset additivity, minimum edge
factor 1.45, the power-reduction formula, the ridge example) were taken from a vendor training deck
that is marked confidential. The code does not copy text from it, but some help strings and constants
paraphrase it. This is an IP and documentation question, not a code defect: each such constant should
carry a neutral, citable rationale (or be marked "configurable, no default claimed") before release.
See A-07.

---

## 2. README feature-to-code matrix

Status key: **OK** implemented and tested; **Partial-T** implemented, partly tested; **Untested**
implemented, no test; **Partial** implemented with confirmed gaps; **Missing**.

| # | README feature | Implementation | Tests | Status | Notes |
|---|---|---|---|---|---|
| 1 | Exposure set: layer thickness, ordered list of up to 4 types | `models.py:344-375` | `test_set_rejects_duplicate_and_wrong_kinds` | OK | Ordering exists; no test that paths survive reordering |
| 2 | Part sets use Hatch/Core/Contour/Edge, support sets use Support | `models.py:333-338`, validator `:358-369` | same test | Partial | Validator failure on assignment leaves the invalid value in the object (D-08) |
| 3 | Hatch: InFill/UpSkin/DownSkin, stripes/chess, offset/rotation/restriction angles, skipping, skywriting, flow optimisation, double exposure, homogenisation | `models.py:141-226` | `test_skywriting_off_warns` (one field) | Partial-T | Chess pattern not validated (D-13); energy ignores skipping (D-12) |
| 4 | Core: skin x/y and z, open-to-platform, base radius (quarter circle / Mattheck), own pattern | `models.py:232-250` | none specific | Untested | Fields are stored only; `open_to_platform`, base radius and skin_z are used by no calculation |
| 5 | Contour: 1-4 passes with offset and in-skin/upskin/downskin P and v, corridor and thickness detection | `models.py:256-289` | `test_paths_get_set_and_diff` (one value) | Partial-T | Pass identity is the list index (D-15) |
| 6 | Edge: offset, method 1 (factor, threshold), method 2 (min radius factor) | `models.py:295-312`, `calc.py:127-136` | none | Untested | `edge_length`, `edge_min_peak_width` are never called by app, CLI or tests |
| 7 | Guard levels free/caution/critical, amber/red dot | `models.py:28-38`, `gui/forms.py:16-19,65-81` | none | Untested | Colour-only indicator, no text cue (accessibility) |
| 8 | Locked reference sets cannot be edited or deleted; copy to start | `library.py:72-107`, `cli.py:114-115`, `gui/app.py:349,427` | `test_library_locking`, `test_locked_set_is_read_only` | Partial | Lock is a plain boolean in a user-editable file (D-18); can be set but never cleared (D-17) |
| 9 | Live analysis: volumetric `P/(v·h·t)` for hatch, areal `P/(v·t)` for contour/edge/support | `calc.py:19-31,153-187` | `test_volumetric_energy_matches_reference_example`, CLI `71.02` check | Partial-T | Areal formula untested; double exposure and skipping handling are assumptions (A-02, D-12) |
| 10 | Effective offsets: GBO, PBO, contour/hatch/edge offsets add up | `calc.py:122-124,190-204` | `test_offsets_are_additive` (hatch only) | Partial-T | PBO is always 0 (no part model); sign convention undocumented for hatch offset (A-04) |
| 11 | Rule checks (machine P/v limits, layer range, energy window, hatch vs stripe width, skin multiple of layer, positive ridge, skywriting off, high power-reduction factor, skipping without skins) | `validation.py:31-148` | 3 tests | Partial-T | Every listed check exists. Issue paths often do not resolve (D-10); machine profile on the set is ignored (D-11); thresholds hard-coded (W-05) |
| 12 | DOE: full factorial, name template | `doe.py` | `test_parse_values`, `test_doe_full_factorial`, `test_doe_duplicate_names_rejected`, CLI | Partial | Range parsing defects (D-04, D-05, D-06), no size guard (D-07), partial saves (D-21) |
| 13 | Compare two sets parameter by parameter | `paths.py:109-116` (`diff`), `cli.py:168-179`, `gui/dialogs.py:124-162` | `test_paths_get_set_and_diff` | Partial-T | Index-based pass paths mis-align (D-15); no unit or guard in output |
| 14 | Build-time estimate for a block, z-segmented plan | `estimate.py` | `test_thicker_layers_build_faster`, `test_overlapping_segments_rejected`, CLI | Partial | Several omissions not reported in output (D-19, D-20, A-05) |
| 15 | Calculators: skin ridge, power-reduction curve | `calc.py:37-71`, `gui/dialogs.py:250-319` | `test_ridge_for_70_degrees`, `test_power_reduction_formula` | OK (core) / Untested (GUI) | `power_reduction_table` untested |
| 16 | Process settings and machine profile stored in the library | `models.py:381-416`, `library.py:125-137`, `gui/app.py:630-642` | none | Untested | One global machine and one process per library (W-03); machine profile not internally validated (D-14) |
| 17 | Import/export JSON (re-importable) or CSV table | `library.py:117-121,140-145`, `cli.py:244-269,281-288`, `gui/app.py:661-685` | CLI CSV export only | Partial-T | JSON round trip through files untested; non-.csv extension writes JSON (D-22); import does not report rule errors |
| 18 | Parameter paths stay valid when exposure types are reordered | `paths.py:17-26` (kind-based first segment) | none | Untested | True for exposure types, false for contour passes (D-15) |
| 19 | Library at `~/.lpbfstudio/library`, `--library` / `LPBF_LIBRARY` | `library.py:20-22`, `cli.py:31-33` | via tmp path in tests | Partial-T | |
| 20 | Seeded with three locked example sets | `defaults.py`, `library.py:45-48` | `test_defaults_have_no_errors` | OK | Values are clearly marked illustrative (good) |
| 21 | Shipped sets are illustrative, not qualified | `defaults.py:3-6,14` | none | Partial | Only a description string; no machine-readable qualification status (M-03) |
| 22 | `lpbf show SET` lists every path | `cli.py:90-103` | none | Untested | |

Requirements from the task brief that the README does not claim and the code does not have:
material model (M-01), machine-specific rule configuration (M-02), qualification status (M-03),
revision history and provenance (M-04), schema migration (M-05), SQLite persistence (M-06),
summary panel and validation-to-parameter navigation in the GUI (M-07), display of units and ranges
next to fields (partly: unit is a spin-box suffix, range is not shown) (M-08).

---

## 3. Test baseline

### 3.1 Environment

Linux container, Python 3.13.17, fresh venv, `pip install -e ".[gui,dev]"` from a copy of the project
folder; `QT_QPA_PLATFORM=offscreen`.

### 3.2 Results

| Run | Result |
|---|---|
| 1. As installed (no Qt system libraries) | **20 passed, 1 skipped** in 0.25 s. The skip is the whole `tests/test_gui.py` module: `could not import 'PySide6.QtWidgets': libEGL.so.1: cannot open shared object file`. Both GUI tests were therefore not run, and pytest still exits 0. |
| 2. After `apt-get install libegl1 libgl1 libxkbcommon0 libfontconfig1 libdbus-1-3` | **22 passed** in 0.40 s. |

No test failed. No test was changed.

Consequence: on a machine without the Qt runtime libraries, the GUI is not tested and nobody notices.
Acceptance criteria below require the GUI tests to *fail*, not skip, when `LPBF_REQUIRE_GUI_TESTS=1`
is set (for CI).

### 3.3 Coverage (run 2, `pytest-cov`, statement coverage)

| Module | Cover | Main untested areas |
|---|---:|---|
| models.py | 100 % | (definitions only; no behavioural tests of ranges beyond one field) |
| calc.py | 85 % | `line_energy`, `power_reduction_table`, `corridor_angle`, `edge_length`, `edge_min_peak_width`, homogenisation note |
| doe.py | 91 % | error branches |
| estimate.py | 91 % | core branch, support branch |
| paths.py | 82 % | `coerce` for bool/enum/int, error paths |
| library.py | 79 % | import, export, process/machine persistence, `unique_name` |
| validation.py | 74 % | most individual rules (only 3 rules are asserted) |
| cli.py | 71 % | `new`, `delete`, `show`, `validate`, `compare`, `import`, all error exits |
| gui/forms.py | 80 % | error restore path, colour picker, reload |
| gui/app.py | 67 % | copy/rename/delete/new, structure edits, import/export, settings dialogs, discard prompt |
| gui/dialogs.py | 11 % | DOE, compare, estimate and calculator dialogs are never opened in a test |
| **Total** | **69 %** | |

### 3.4 Quality of existing tests

Good: deterministic, use `tmp_path`, assert explicit numbers for the two reference calculations
(67.47 J/mm³, −0.0328 mm).

Weak: most validation tests check `"power" in i.path` substrings rather than exact paths and messages,
which is why D-10 went unnoticed; no test covers reordering, migrations, revisions, import round trips,
DOE size limits, or any dialog. Several asserted behaviours are only exercised through one CLI flow
test that checks substrings of the output.

---

## 4. Defects and risks

### 4.1 Confirmed defects

Probe ids refer to `docs/audit/probe.py` (P..) and `docs/audit/probe_gui.py` (G..).

#### Data integrity and protection

| ID | Sev. | Defect | Evidence | Location |
|---|---|---|---|---|
| D-01 | **Critical** | **Different names that map to the same file overwrite each other.** `_slug("My Part") == _slug("My_Part")`; saving `My_Part` silently replaces `My Part`. | P01: after saving `My_Part`, `My Part` is gone | `library.py:25-29,42-43,72-79` |
| D-02 | **Critical** | **Renaming through `lpbf set X name=Y` or the GUI "Set properties" name field creates a duplicate** and leaves the old file; the library then holds two sets that differ only in name. | P02, G01 | `cli.py:110-125`, `gui/forms.py` (name is editable, `SKIP_FIELDS` omits it), `library.py:72-79` |
| D-03 | **Critical** | **The same path overwrites a different, unrelated set.** `lpbf set A name=B` where `B` exists replaces `B` (a support set) with a copy of `A` without warning. | P03: `B.category` changed from support to part | `cli.py:124` → `library.py:72-79` (`overwrite=True` default) |
| D-16 | High | **One unreadable JSON file makes the whole library unusable** (`names()`, `load_all()`, GUI start-up all raise). | P05 | `library.py:53-58` |
| D-23 | High | **Writes are not atomic.** A crash or full disk during `write_text` leaves a truncated set file, which then triggers D-16. | P28 | `library.py:69-70,130,137,141` |
| D-24 | Medium | File name and internal `name` can diverge (manual edit, older export). The set is listed under one name but `get()` looks for another file and fails. | P04 | `library.py:53-67` |
| D-25 | Medium | Renaming to a name with the same slug (for example `My Part` → `My_Part`) fails with a misleading "already exists". | P27 | `library.py:92-101` |
| D-17 | Medium | A user can lock a set (`lpbf set A locked=true`) but there is no way to unlock it, and no record of who locked it or why. | P19 | `cli.py:114-115`, `models.py:352` |
| D-18 | Medium | Reference protection is a boolean in a plain JSON file; editing the file unlocks and alters a reference set with no detection. | P20 | `models.py:352`, `library.py` |
| D-28 | Medium | `schema_version` is stored but never checked: a file written by a future version (`schema_version: 99`) loads as if current; a file with an added field fails with an opaque `extra_forbidden` error. No migration path exists. | P06 | `models.py:24,346`, `library.py` |

#### Domain model

| ID | Sev. | Defect | Evidence | Location |
|---|---|---|---|---|
| D-08 | High | **A rejected assignment leaves the invalid value in the object.** With `validate_assignment=True`, a failing `model_validator(mode="after")` raises, but the field already holds the new value (for example a part set with `category=support`). The GUI works around it by writing `obj.__dict__` directly; the CLI and DOE do not. | P11 | `models.py:86,358-369`; workaround `gui/forms.py:153-156` |
| D-14 | Medium | `MachineProfile` accepts `min_layer_mm > max_layer_mm` and an inverted `energy_window_j_mm3` (150, 30). Validation against such a profile then gives nonsense. | P15 | `models.py:405-416` |
| D-15 | Medium | **Contour pass paths are list indices** (`contour.passes.0.power_w`). Removing or reordering a pass shifts every path, so compare reports 8 changed values instead of "pass removed", and DOE factors silently target a different pass. This contradicts the README's path-stability claim for passes. | P18 | `paths.py:20-21,84-87` |
| D-09 | Medium | Field ranges are universal constants (power ≤ 2000 W, speed ≤ 20 000 mm/s). A machine profile up to 5000 W is valid, but a 3000 W parameter is rejected by the model before any machine rule runs. Machine capability belongs in the machine profile, not the field. | P16 | `models.py:64-71,408` |

#### Calculations and validation

| ID | Sev. | Defect | Evidence | Location |
|---|---|---|---|---|
| D-10 | High | **Validation issue paths are not valid parameter paths** (`hatch.infill.power`, `contour.passes.0 upskin.power`). The GUI cannot link a message to a field, and tests only match substrings. | P12 | `validation.py:54,56,130-133` |
| D-11 | High | **The set's `machine` field is ignored.** `validate()` falls back to a hard-coded `MachineProfile()` when none is passed, and the library has exactly one machine profile regardless of what the set says. A set labelled for a 1 kW machine is checked against generic 400 W limits without any notice. | P17 | `validation.py:33`, `library.py:132-137`, `models.py:351` |
| D-12 | High | **Energy input ignores infill skipping.** With `skip_layers = n`, infill is exposed every (n+1)-th layer over (n+1)·t of powder, but `energy_summary` and the energy-window check use the nominal t. A set with `skip_layers=1` reports twice the energy actually delivered per unit volume, so a lack-of-fusion risk can be shown as "in window". (The corrected formula is itself an assumption, A-03, and needs sign-off.) | source | `calc.py:155-169`, `validation.py:58-64,89` |
| D-13 | Medium | Chess pattern is not checked: hatch distance 1.0 mm inside 0.5 mm squares gives no issue (the stripes branch has this check). | P14 | `validation.py:90-96` |
| D-26 | Low | Power reduction always takes `h0` from `stripes.stripe_width_mm`, even for the chess pattern. The warning value at h = 0 is unaffected (it is P0·(1−r) for any h0), but the calculator dialog pre-fills the wrong h0 and any future vector-length evaluation would be wrong. | P13 | `calc.py:212`, `validation.py:109`, `gui/dialogs.py:277-279` |
| D-27 | Low | A restriction angle plus rotation can collapse every layer to one direction (rotation 90°, restriction 89.9° → 90°, 90°, 90°) with no anisotropy warning; the existing check only fires for rotation % 180 == 0. | P25 (no crash; result shown) | `calc.py:98-111`, `validation.py:102-104` |

#### DOE, comparison, estimation, export

| ID | Sev. | Defect | Evidence | Location |
|---|---|---|---|---|
| D-04 | High | **Range overshoots its stop value**: `0.09:0.15:0.04` → `[0.09, 0.13, 0.17]`. `round()` on a half step rounds up past `stop`. | P07 | `doe.py:22-23` |
| D-05 | Medium | A reversed range (`0.13:0.09:0.02`) silently yields zero levels and zero variants, no error. | P08 | `doe.py:22-23`, `doe.py:52-55` |
| D-06 | Medium | Integer parameters with a fractional step truncate to duplicate levels: `1:3:0.5` → `[1, 1, 2, 2, 3]`. | P09 | `doe.py:24` |
| D-07 | High | **No size guard.** 10×10×10×10 builds 10 000 full set copies in memory without a count shown first. The GUI DOE dialog recomputes the full product on every keystroke (`dialogs.py:93-110`), so a large range freezes the editor. | P10 | `doe.py:52-73`, `gui/dialogs.py:93-110` |
| D-21 | Medium | The CLI saves DOE variants one by one; a collision mid-run exits with an error after saving part of the design. The GUI instead skips existing names silently. Two different behaviours, neither transactional. | P30 | `cli.py:207-213`, `gui/dialogs.py:112-121` |
| D-19 | Medium | Estimate ignores up/downskin, edge, chess-gap time, and core `skin_thickness_z` (a 2 mm and a 9 mm z-skin give identical times on a 10 mm block). | P21, P22 | `estimate.py:31-65` |
| D-20 | Low | Gaps between z-segments (0-5 and 10-15) are accepted silently; only overlaps are rejected. | P23 | `estimate.py:68-75` |
| D-22 | Low | `lpbf export SET file.xlsx` (or any non-.csv extension) writes JSON without warning. | P29 | `cli.py:252-255` |

#### GUI

| ID | Sev. | Defect | Evidence | Location |
|---|---|---|---|---|
| G-01 | (= D-02) | Name field in "Set properties" duplicates the set on save. | G01 | `gui/forms.py:21`, `gui/app.py:348-361` |
| G-02 | Medium | Checks table rows are not linked to parameters (no click-to-navigate), and messages do not show the allowed range. | G02 | `gui/app.py:601-605` |
| G-03 | Low | Allowed range is enforced by the spin box but never displayed; values outside the range are clamped silently by Qt instead of reported. | G03, `forms.py:98,108` | `gui/forms.py:96-115` |

### 4.2 Unverified engineering assumptions

These are not bugs. Each needs either a citable reference, an engineering decision recorded in the
calculation spec, or to be made configurable. None of them should be presented as matching any
vendor's internal behaviour.

| ID | Assumption in the code | Where | Why it is uncertain | Proposed handling |
|---|---|---|---|---|
| A-01 | Contour, edge and support energy as `P/(v·t)` [J/mm²] | `calc.py:24-26` | Literature for contour/edge exposures mostly reports line energy P/v [J/mm]; dividing by t gives a quantity that is comparable across layer thicknesses but has no single accepted name or window. | Report both P/v and P/(v·t); name the second "layer-normalised line energy"; no window check until a machine/material rule defines one. |
| A-02 | Double exposure doubles volumetric energy and is checked against the single-exposure window | `calc.py:168-169` | The second pass re-melts solidified material; summing energies is a convention, not physics. | Report per-pass energy plus a labelled cumulative value; window check on per-pass value by default; make it a rule option. |
| A-03 | Correct handling of infill skipping is `E = P/(v·h·(n+1)·t)` | (D-12) | The physically relevant layer for skipped infill is the accumulated powder depth; but powder packing and the skins exposed in between change the effective value. | Implement as "effective layer thickness" with a documented formula and show nominal and effective values side by side. |
| A-04 | All offsets add with one sign convention (positive = inward) | `calc.py:122-124`, help texts `models.py:220-221,260-261,301-302,385-386` | The hatch offset help says "distance from the GBO line" without a direction; whether every offset is measured inward from the same reference is a convention of this tool. | State the convention once in the calc spec, show the sign in the UI, test each offset type. |
| A-05 | Build time = area/(h·v) + perimeter/v per pass + recoat | `estimate.py` | Omits jumps, skywriting, delays, acceleration, laser on/off latency, gas-flow wait, multi-laser sharing. The CLI prints "jumps and delays not modelled"; the GUI text says the same; the result object does not carry the list of omitted factors. | Keep the model, attach an explicit `omitted_factors` list to every estimate, show it next to every total, and label results "comparison estimate, not a machine prediction". |
| A-06 | Core region area = perimeter × skin_xy | `estimate.py:45` | Overestimates the skin band at corners and becomes > area for small parts (clamped by `min`). | Use `max(0, A − A_inset)` with an explicit inset model (for a block: (a−2s)(b−2s)); document. |
| A-07 | Minimum edge factor 1.45, edge length = factor × TBO, method 2 threshold = factor × 2 × TBO | `calc.py:13,127-136` | Taken from vendor training material. Not verifiable here and possibly not freely citable. | Move to configurable machine-rule data with "source: user-supplied" and no shipped claim of equivalence. |
| A-08 | Hatch restriction: when a direction is restricted, keep adding the rotation (or 1° when rotation is a multiple of 180°) and carry that offset into later layers | `calc.py:98-111` | Other strategies (skip the layer's angle but keep the nominal sequence, or mirror) are equally plausible. | Make the strategy explicit and named; test both; do not claim it matches any processor. |
| A-09 | Corridor angle = atan(thickness/corridor) | `calc.py:74-82` | README already calls it an approximation. | Keep, label "approximation" in UI and spec. |
| A-10 | A single energy window 30-150 J/mm³ applies to infill, skins and core alike | `models.py:416`, `validation.py:58-64` | Up- and downskin and core typically use deliberately different energies; one window produces false warnings or hides real ones. Values are material-dependent. | Windows per material × machine × region, with "no window defined" as an explicit state (info, not warning). |
| A-11 | Thresholds such as power-reduction factor > 0.6 → warning | `validation.py:108` | Arbitrary number. | Rule configuration with default, documented as illustrative. |
| A-12 | Shipped example values (285 W, 960 mm/s, …) | `defaults.py`, `models.py` defaults | Illustrative; correctly disclaimed in text, but nothing in the data model stops them being treated as qualified. | Qualification status field, default `example`; UI badge; export carries it. |

### 4.3 Duplicated logic

1. **Three parallel walks over exposure regions.** `calc.energy_summary`, `validation.validate` and
   `estimate.estimate_segment` each have their own `isinstance` ladder that decides which regions exist
   and which (P, v, h) belong to them. A new region (for example chess gaps) must be added three times;
   D-19 and D-13 are the result of the ladders drifting apart. One `iter_scan_regions(es)` should feed
   all three.
2. **Enum-to-text** (`v.value if hasattr(v, "value") else v`) appears in `cli.py` (show, compare,
   write_csv), `gui/dialogs.py:_value_text` and `doe._fmt`.
3. **Recoat default** `9.0` in `estimate.py:32,68` and `ProcessSettings.recoat_time_s`.
4. **Maximum contour passes** `4` in `models.py:289` and `gui/app.py:546,588`.
5. **Lock enforcement** in `Library`, CLI and GUI separately (1.4).
6. **CSV export** lives in `cli.py` and is imported by the GUI.
7. **Three copies of the source** (folder, zip, installer), see 1.3.

### 4.4 Architectural weaknesses

| ID | Weakness | Blocks requirement |
|---|---|---|
| W-01 | Parameter definitions (unit, range, guard, help) are embedded in the value model. There is no separate definition registry, so definitions cannot vary by machine or material, cannot be versioned, and cannot be listed without instantiating a set. | Separation of values / definitions / applicability |
| W-02 | Identity is the set's name (and its slug). No stable id, so rename = new object, and revisions, references from DOE variants to their base, and audit trails have nothing to attach to. | Revisions, provenance, rename |
| W-03 | One `machine.json` and one `process.json` per library; `ExposureSet.machine` and `.material` are free text. | Machine/material applicability, compatibility checks |
| W-04 | Mutable models with `validate_assignment` and a model-level validator (D-08). Edits are not transactional; the GUI patches `__dict__`. | Reliable editing, undo, unsaved-change detection |
| W-05 | Validation rules and thresholds are hard-coded in one function. | Machine-specific, configurable rules |
| W-06 | `Issue` has no stable rule code and no machine-readable reason, only a formatted string. | GUI navigation, filtering, tests on exact results |
| W-07 | Folder-of-JSON storage without atomic writes, locking, or an index. | Integrity constraints, transactions, revision history |
| W-08 | No schema version check or migration framework. | Schema versioning and migration |

### 4.5 Missing features (from the task brief)

| ID | Missing |
|---|---|
| M-01 | Material entity and material-specific process settings or windows |
| M-02 | Machine-specific rule configuration |
| M-03 | Qualification status (example / experimental / qualified / retired) and applicability per machine × material |
| M-04 | Revision history and provenance (who, when, why, derived from) |
| M-05 | Schema migration |
| M-06 | SQLite store; import/export records |
| M-07 | GUI summary panel; navigation from a message to its parameter; explicit validate, compare-with-selected and export-of-selected actions |
| M-08 | Display of unit and allowed range next to each field; text (not colour-only) guard indicator |
| M-09 | DOE count preview and size limit (D-07) |
| M-10 | Parameter-set compatibility check (set vs machine vs material vs process) |

### 4.6 High-risk calculations (summary)

Ranked by the consequence of being wrong:

1. Volumetric energy with skipping and double exposure (D-12, A-02, A-03): a wrong value can hide lack of fusion.
2. Machine limits taken from the wrong machine profile (D-11): a set can pass validation for a machine it exceeds.
3. Offsets and their sign (A-04): a sign error moves every scan line by up to the GBO, a dimensional error.
4. Power reduction (D-26, A-11): drives minimum power on short vectors.
5. DOE range generation (D-04): silently creates a level outside the intended experiment.

---

## 5. Calculation audit

One row per calculation in `calc.py`, `estimate.py` and `validation.py`. "Status" refers to the formula
as implemented, not to whether the engineering model is right (see 4.2).

| Calculation | Formula as implemented | Units | Valid input domain (as coded) | Edge cases found | Tests | Status |
|---|---|---|---|---|---|---|
| `volumetric_energy` | E = P / (v·h·t) | W, mm/s, mm, mm → J/mm³ | no guard; relies on field ranges (v ≥ 1, h ≥ 0.01, t ≥ 0.01) | Division by zero possible if called directly with 0 | 1 reference value | Formula correct; application to skipping/double exposure wrong or assumed (D-12, A-02) |
| `areal_energy` | E = P / (v·t) | → J/mm² | no guard | as above | none | Formula arithmetic correct; meaning is A-01 |
| `line_energy` | E = P / v | → J/mm | no guard | | none | Unused |
| `reduced_power` | P(h) = P0 − r·P0·(1 − clamp(h/h0, 0, 1)) | W, –, mm, mm → W | h0 > 0 checked; r not checked here (field 0-1) | h > h0 clamps (tested) | 4 assertions | Correct as specified; h0 source wrong for chess (D-26) |
| `power_reduction_table` | P(h) at h0·i/steps | | steps > 0 assumed | steps = 0 → ZeroDivisionError | none | Untested |
| `skin_layers` | n = thickness/t, exact if within 1e-6 | mm/mm | t > 0 | absolute tolerance 1e-6 on a ratio is fine for mm | indirect | OK |
| `ridge_for_angle` | ridge = −thickness / tan(α) | mm, ° → mm | 0 < α < 90 | | 1 reference value | Arithmetic matches the reference example; meaning of "ridge" is a convention (documented in help) |
| `corridor_angle` | α = atan(thickness/corridor) | mm/mm → ° | corridor ≤ 0 → 90° | | none | Approximation (A-09), unused in UI |
| `is_restricted` / `hatch_angles` | axis distance mod 180 < restriction; restricted → keep rotating | ° | restriction 0-90 | Can collapse to one direction (D-27); 360-try cap returns a restricted angle silently if no free direction | 2 tests | Strategy is A-08 |
| `infill_exposed` | i mod (n+1) == 0 | | n ≥ 0 | | 1 test | OK |
| `total_beam_offset`, `effective_offsets` | TBO = GBO + PBO + offset | mm | | PBO hard 0 | 1 test (hatch) | Arithmetic OK; convention A-04 |
| `edge_length`, `edge_min_peak_width` | factor·TBO; factor·2·TBO | mm | | | none | Unused; A-07 |
| `estimate_segment` | layers = ceil(Δz/t); hatch area/(h·v)·exposed layers·(2 if double); core (A−P·s)/(h·v); contour P/v per pass; recoat per layer | mm², mm, s | Δz > 0 | D-19, D-20, A-05, A-06 | 2 tests + CLI | Partial |
| `validate` energy window | lo ≤ E ≤ hi from machine | J/mm³ | window not validated (D-14) | | indirect | A-10 |
| `parse_values` range | n = round((stop−start)/step)+1 | | step > 0 | D-04, D-05, D-06 | 1 test | Defective |

Each of these needs a written spec (formula, units, assumptions, domain, edge cases, limitations,
rationale) in `docs/calculations.md` as part of Milestone B; the table above is the starting point.

---

## 6. Proposed target architecture

Principle: keep the current package and its working parts, and move toward this structure in small
steps. No big-bang rewrite. The GUI and CLI talk only to a service layer; the service layer talks to a
repository interface; calculations and rules are pure functions over the domain model.

```
lpbfstudio/
  domain/
    definitions.py    ParameterDefinition: path, label, unit, dtype, hard_min/max, guard, help,
                      applies_to (region kinds). Loaded from a versioned registry, not from Field()
    values.py         ExposureSet (+ stable set_id), ExposureType subclasses, ContourPass (+ pass_id),
                      ProcessSettings. Pure values; no UI metadata
    machine.py        MachineProfile with capability limits (P, v, t, temperature, build volume),
                      validated internally (min < max)
    material.py       Material, MaterialProcessWindow (per machine × region)
    qualification.py  QualificationStatus enum (example, experimental, qualified, retired),
                      Applicability(set_id, machine_id, material_id, status, evidence)
    paths.py          stable paths: kind-based for exposure types, id-based for passes
  calc/               one module per topic, pure functions, each with a docstring spec and tests
    energy.py offsets.py homogenization.py skin.py angles.py estimate.py
  rules/
    engine.py         Rule(code, severity, applies_to, check) → Issue(code, severity, path, message,
                      reason, value, limit)
    builtin.py        current checks, rewritten as Rule objects
    config.py         per-machine / per-material thresholds and enable flags (data, not code)
  services/
    sets.py           create, copy, rename, delete, edit-in-transaction, lock/unlock with reason,
                      revision on save
    doe.py            plan (count, preview) → generate (transactional save)
    compare.py        diff with units, guard and structural changes (added/removed passes)
  storage/
    repository.py     abstract interface
    sqlite_repo.py    primary store
    json_folder.py    legacy reader for 0.1 libraries (read-only) + import
    migrations/       numbered SQL migrations + data migrations for stored JSON payloads
  io/
    json_io.py csv_io.py   versioned exchange format with schema_version and provenance block
  cli/  gui/          depend on services only
```

### 6.1 Persistence decision: SQLite as the primary store

Recommendation: **yes**. Reasons: the brief needs revision history, protected references, import/export
records, provenance and qualification across sets, machines and materials. Those are relations with
integrity constraints and multi-row transactions, which a folder of JSON files cannot provide (D-01,
D-03, D-16, D-23 are all symptoms). SQLite is in the standard library, single-file, and needs no
service.

Proposed core tables (sketch; finalised in Milestone D):

- `schema_migrations(version PK, applied_at)`
- `parameter_set(set_id PK uuid, name UNIQUE COLLATE NOCASE, category, created_at, is_reference, locked_reason, locked_by, locked_at, head_revision_id FK)`
- `parameter_set_revision(revision_id PK, set_id FK, revision_no, schema_version, payload_json, created_at, author, comment, derived_from_revision_id FK NULL, origin ENUM(created, copied, doe, imported, migrated))` with `UNIQUE(set_id, revision_no)`; rows are append-only (trigger forbids UPDATE/DELETE)
- `material(material_id PK, name UNIQUE, …)`, `machine_profile(machine_id PK, name UNIQUE, payload_json, …)`, `process_settings(process_id PK, …)`
- `applicability(set_id, machine_id, material_id, status, evidence, updated_at, PRIMARY KEY(set_id, machine_id, material_id))`
- `exchange_log(id PK, direction ENUM(import, export), path, file_sha256, set_id, revision_id, at)`
- `rule_config(machine_id NULL, material_id NULL, rule_code, enabled, params_json)`

The set content stays one JSON document per revision (validated by Pydantic on read and write). That
keeps the domain model as the single source of truth for structure, while SQLite carries identity,
history, and relations.

Migration from 0.1 JSON libraries: read-only scan of the old folder, import each file as revision 1 with
`origin = migrated` and the source file's SHA-256 in `exchange_log`; report files that fail to parse
instead of aborting; never modify or delete the source folder; refuse to run if the target database
already contains the same file hash.

### 6.2 Things deliberately not in scope until later

Geometry import, slicing, vector generation, machine adapters. The architecture leaves room
(`geometry/`, `toolpath/`, `adapters/` packages consuming `services`) but none is started before
Milestone F.

---

## 7. Recommended changes, ranked

| Rank | Change | Fixes | Milestone |
|---:|---|---|---|
| 0 | Put the project under git; make the folder the single source; regenerate zip/installer from it at release | 1.3 | before B |
| 1 | Library data-safety hotfix: refuse slug collisions, block renames through `set`/form, atomic writes, tolerant loading with a list of bad files | D-01, D-02, D-03, D-16, D-23, D-24, D-25 | B1 |
| 2 | Fix validation issue paths and give each issue a rule code; test that every emitted path resolves | D-10, W-06 | B2 |
| 3 | Transactional editing: validate a candidate copy, then swap; remove the `__dict__` workaround | D-08, W-04 | B2 |
| 4 | Machine profile self-validation; validation uses the profile named by the set or reports "no machine profile" explicitly | D-11, D-14 | B3 |
| 5 | Energy: effective layer thickness for skipping, per-pass and cumulative for double exposure, both P/v and P/(v·t) for line exposures; write `docs/calculations.md` | D-12, A-01-A-03 | B3 |
| 6 | DOE range parser: inclusive stop without overshoot, reject reversed ranges and fractional steps on integers; count and limit before generating | D-04-D-07 | B4 (pulled forward from E: wrong DOE levels are a calculation defect) |
| 7 | Stable pass ids and path-stability tests; structural diff in compare | D-15 | B5 |
| 8 | Chess checks; restriction/rotation collapse warning; h0 by pattern | D-13, D-26, D-27 | B5 |
| 9 | Split parameter definitions from values; move machine capability out of field ranges | W-01, D-09 | B6 / C |
| 10 | GUI: summary panel, message → field navigation, range display, text guard indicator | G-02, G-03, M-07, M-08 | C |
| 11 | SQLite repository, migrations, revisions, lock with reason, legacy import | W-02, W-07, W-08, M-04-M-06, D-17, D-18 | D |
| 12 | Material and applicability/qualification model; compatibility check | M-01-M-03, M-10, A-10, A-12 | D/E |
| 13 | Estimate: omitted-factor list in the result, z-skin, gap detection | D-19, D-20, A-05, A-06 | E |
| 14 | Third-party licences, packaging, CI that fails when GUI tests skip | 1.2, 3.2 | F |

---

## 8. Milestone plan with acceptance criteria

General acceptance criteria for every milestone:

- G1. The full suite passes with `LPBF_REQUIRE_GUI_TESTS=1` (GUI tests fail instead of skip if Qt cannot load).
- G2. No existing test is deleted or weakened; a changed assertion is justified in the change summary.
- G3. Each confirmed defect fixed in the milestone has a regression test that fails on 0.1.0 and passes after.
- G4. README and the relevant `docs/*.md` describe only behaviour that the code and tests substantiate.
- G5. The milestone report lists files changed, test results as run (counts, failures), and known limitations.

### Milestone B: correct domain-model and calculation defects

Scope: items 1-8 of section 7 (B1-B5), plus the start of 9 if time allows. No storage-engine change.

Acceptance criteria:

- B-AC1. Saving, copying, importing or renaming can never replace a set other than the one named; a slug collision raises a clear error (test per entry point: Library, CLI `set`, `copy`, `import`, GUI form).
- B-AC2. `name` cannot be changed through `set_path`, the CLI `set` command or the properties form; `rename` is the only route and it is atomic (new file written via temp + `os.replace`, then old removed).
- B-AC3. A library with one corrupt file still lists every other set and reports the bad file by path.
- B-AC4. For every built-in rule, a fixture triggers it and the test asserts exact `code`, `severity` and `path`, and that `get_path(es, issue.path)` succeeds for leaf paths.
- B-AC5. A rejected edit (CLI, DOE, GUI) leaves the object exactly as before (test compares `model_dump()` before and after).
- B-AC6. `MachineProfile` rejects min ≥ max for layer thickness and energy window.
- B-AC7. Validation reports which machine profile it used; a set naming an unknown profile yields an explicit issue rather than silent defaults.
- B-AC8. `docs/calculations.md` covers every function in section 5 with formula, units, assumptions, domain, edge cases, limitations and rationale; each formula has at least one hand-computed test value and one boundary/invalid-input test.
- B-AC9. Energy output shows nominal and effective (skipping) values and per-pass/cumulative (double exposure) values with labels; the window check uses the documented one.
- B-AC10. `parse_values`: `0.09:0.15:0.04` → `[0.09, 0.13]`; reversed range and fractional integer step raise `ValueError`; DOE exposes `count()` before generation and refuses above a configurable limit (default 500) unless explicitly overridden.
- B-AC11. Removing contour pass 1 of 2 leaves the remaining pass's path unchanged; compare reports "pass removed".

### Milestone C: complete the parameter editor workflow

- C-AC1. Selecting a message in the checks table selects the affected node and focuses the field (offscreen test).
- C-AC2. Each field shows unit and allowed range; guard level appears as text (for example "critical") as well as colour.
- C-AC3. A summary panel shows the selected set's key parameters (t, P/v/h per region, energy, validation counts, qualification status) and updates on edit.
- C-AC4. Copy, rename, validate, compare-with, export actions work on the selected set; each has an offscreen test.
- C-AC5. Unsaved-change prompt is tested for switching sets, closing the window, and new/copy actions.
- C-AC6. All four dialogs (DOE, compare, estimate, calculators) are opened and exercised in tests; `gui/dialogs.py` coverage ≥ 70 %.
- C-AC7. DOE dialog shows the variant count before generating and never computes more than the limit.

### Milestone D: persistence and migration

- D-AC1. SQLite repository passes the same repository contract tests as the JSON folder implementation.
- D-AC2. Every save creates an append-only revision with author, time, comment and origin; history is listable via CLI and GUI.
- D-AC3. Reference sets: lock and unlock require a reason and are recorded; locked sets reject edits at the service layer (test bypassing the GUI and CLI).
- D-AC4. Migration from a 0.1 folder: all valid files imported, invalid files reported, source folder byte-identical before and after (hash comparison test); running twice does not duplicate.
- D-AC5. Schema migrations are numbered, run in a transaction, and a test migrates a fixture database from each previous version.
- D-AC6. Stored JSON payloads carry `schema_version`; loading a newer version is refused with a clear message; older versions are migrated explicitly.
- D-AC7. JSON import/export round trip preserves values, schema version, provenance and qualification status (test).

### Milestone E: DOE, comparison, export and CLI tests

- E-AC1. DOE output is reproducible: same inputs → identical names, order and values (test with fixed fixture); variants record the base revision id.
- E-AC2. DOE save is all-or-nothing.
- E-AC3. Compare shows unit and guard and distinguishes changed, added and removed parameters; tests for each.
- E-AC4. Estimate results carry `omitted_factors`; CLI and GUI print them; z-segment gaps produce a warning; the result is labelled as a comparison estimate.
- E-AC5. Every CLI command has at least one success and one failure test with exact exit codes.
- E-AC6. Compatibility check (set × machine × material × process) implemented and tested.

### Milestone F: documentation, packaging and release validation

- F-AC1. `THIRD_PARTY_LICENSES` lists PySide6 (LGPL-3.0), pydantic (MIT), click (BSD-3-Clause), and any added library, with the LGPL obligations met in the package.
- F-AC2. Clean-environment install and full test run on Python 3.10 and the newest supported version.
- F-AC3. README, user guide and calculation spec reviewed against the code; no claim of equivalence with any vendor software.
- F-AC4. Release artefacts (wheel, zip, installer) are generated from the tagged repository, not edited by hand.

---

## 9. Recommended first implementation task

**Task B1: library data-safety hotfix (with git as a prerequisite).**

Why first: D-01, D-02 and D-03 can silently destroy a user's parameter set today through ordinary
commands (`lpbf set X name=Y`, editing the name field, or two names that differ by a space). Every later
milestone builds on trustworthy storage, and the fix is small and contained to `library.py`, `paths.py`,
`cli.py` and `gui/forms.py` without touching the storage format.

Steps:

0. Initialise a git repository for `lpbf-studio/` (or attach a GitHub repository) and commit 0.1.0
   unchanged as the baseline.
1. Add failing regression tests reproducing D-01, D-02, D-03, D-16, D-23 (simulated), D-24, D-25
   (converted from `docs/audit/probe.py`).
2. `Library`: detect slug collisions on save/copy/import/rename (`"My Part"` vs `"My_Part"` is an error
   unless it is the same set); atomic writes via temp file and `os.replace`; `load_all` returns valid sets
   and a list of unreadable files; `get` falls back to scanning when the file name and internal name
   differ.
3. Make `name` (and `locked`) non-editable through `set_path` and the properties form; route renames
   only through `Library.rename`.
4. Run the full suite with GUI tests required; update README (no feature change, only notes) and
   this audit's status column.

Files expected to change: `src/lpbfstudio/library.py`, `src/lpbfstudio/paths.py`, `src/lpbfstudio/cli.py`,
`src/lpbfstudio/gui/forms.py`, `tests/test_core.py` (new tests only), `tests/test_library_safety.py` (new),
`README.md`.

Out of scope for B1: SQLite, revisions, the definitions split, any calculation change.

Acceptance: B-AC1, B-AC2, B-AC3 and G1-G5.

---

## 10. Status after Milestone B1 (2026-10-26)

B1 (library data-safety fix) is complete. Nothing outside its scope was changed: no domain-model,
calculation, DOE, storage-format or GUI-layout change.

| ID | Status | Root cause | Fix |
|---|---|---|---|
| D-01 | Fixed | The library addressed files only by `slug(name)` and never checked which set a file actually held, so two names with one slug wrote to the same file. | `Library._locate` finds the file that holds a name; `save`, `copy`, `import_file` and `rename` refuse a target file that holds a different set, naming that set in the error. |
| D-02 | Fixed | `name` was an ordinary editable field (CLI `set name=…`, GUI properties form), but the library uses it as identity, so the edited set was saved under a new file and the old one stayed. | `set_path` refuses `name` on an exposure set; the GUI shows the name read-only; renaming goes only through `Library.rename` (toolbar Rename, new `lpbf rename`). |
| D-03 | Fixed | Same cause as D-02 combined with `save(overwrite=True)`, which replaced whatever set held the new name. | Same as D-02; `save` also checks ownership (D-01). |
| D-16 | Fixed | `load_all` parsed every file with no error handling. | Unreadable files and duplicate stored names are skipped and listed in `Library.load_errors`; `lpbf list` warns on stderr, the editor shows them in the status bar. Bad files are never modified. Saving onto an unreadable file is refused. |
| D-23 | Fixed | Direct `write_text` truncates the file before writing. | Set, process and machine files are written to a temporary file in the same folder, flushed, then `os.replace`d. |
| D-24 | Fixed | `get`, `delete` and `rename` assumed the file name always matched the stored name. | Lookups use the stored name (slug file first, then a scan); saves go back to the file that holds the set. |
| D-25 | Fixed | `rename` called `exists(new)`, which was true for the set's own file when old and new share a slug. | Rename ignores its own source file in the conflict check and rewrites it in place when the slug is unchanged. |

Test results (Python 3.13.17, PySide6 6.12.0, offscreen, `LPBF_REQUIRE_GUI_TESTS=1`):

- On 0.1.0 with the new tests added: **18 failed, 30 passed** (all 18 failures are new regression tests; all 22 original tests passed).
- After the fix: **49 passed, 0 failed, 0 skipped** (22 original, 25 in `tests/test_library_safety.py`, 2 new GUI tests).

Audit probes re-run after the fix: P02, P03, P04, P05, P27, P28 no longer reproduce; P01 now raises a
clear `LibraryError` instead of overwriting. All other probes are unchanged, as intended.

Behaviour changes a user can notice:

- `lpbf set NAME name=…` now exits with an error that points to `lpbf rename` (new command).
- The set name in the editor's "Set properties" form is read-only; Rename in the toolbar is unchanged.
- `exists(name)` is now true when the name's file is taken by another set, so `unique_name` and the
  DOE dialog avoid colliding names (the dialog marks such a variant "exists, will be skipped").
- `get` on an unreadable file raises `LibraryError` (previously a raw `ValidationError`).

Remaining risks after B1:

- Lookups that miss the slug file scan the whole folder, so `exists` and `save` cost O(number of sets)
  file reads. Fine for hundreds of sets; DOE previews over very large libraries will be slower.
- No locking between processes: the editor and the CLI writing the same library at the same moment can
  still race (last write wins, but never a truncated file).
- On a case-insensitive file system, renaming `A` to `a` is refused as a conflict rather than performed.
- `LPBF_REQUIRE_GUI_TESTS=1` was verified with Qt available; the failure path (Qt missing) was checked
  only by reading the code.
- D-17, D-18, D-28 (lock flag and schema version) and D-21 (partial DOE saves) are unchanged and remain
  scheduled for Milestones D and E.

---

## 11. Status after Milestone B2 (2026-10-26)

B2 (validation rule codes, canonical paths, structured findings, atomic edits) is complete. Energy
formulas, DOE generation, build-time estimation, storage and GUI architecture were not changed.
No rule, severity, threshold or message wording changed: `tests/data/validation_baseline_0_1_0.json`
was captured from the 0.1.0 validator for all 37 trigger cases before the change, and the suite
compares against it.

| ID | Status | Root cause | Fix |
|---|---|---|---|
| D-10 | Fixed | Paths were built by string concatenation with shorthand suffixes (`.power`, `.speed`, `" upskin"`), and region- or set-level findings reused field names (`exposure_types`) or bare node names without saying so. 20 of the 37 trigger cases emitted at least one path that did not resolve. | Every finding now carries a `scope` (`parameter`, `node`, `set`, `process`) and a path that resolves for that scope; machine-limit paths name the actual field (`contour.passes.0.upskin_power_w`, `hatch.infill.chess.gap_power_w`). |
| W-06 | Fixed | Findings had no identifier other than message text. | 25 rules registered in `validation.RULES` with codes `XXX-nnn`, severity, scope and title; documented in `docs/validation_rules.md`; inventory frozen by a test. |
| D-08 | Fixed | With `validate_assignment`, pydantic stores a value before running the `ExposureSet` model validator and does not undo it when that validator fails (for example `category=support` on a part set). The GUI worked around it by writing `obj.__dict__`; `set_path` and the CLI did not. | `paths.assign_field` tries the assignment on a deep copy first; `set_path` and the GUI form use it, and the `__dict__` workaround is gone. New `paths.with_assignments` applies several assignments all-or-nothing on a copy; `lpbf set` uses it. |

Test results (Python 3.13.17, PySide6 6.12.0, offscreen, `LPBF_REQUIRE_GUI_TESTS=1`):

- Before the fix, with the new API names stubbed so the tests could be collected
  (`docs/audit/b2_old_api_stub.py`): **116 failed, 129 passed**. Without the stub, the two new test
  modules fail at import.
- After the fix: **245 passed, 0 failed, 0 skipped** (49 from B1, 194 in `test_validation_rules.py`
  and `test_atomic_edits.py`, 2 new GUI tests).

Visible behaviour changes:

- `lpbf validate` lines now read `[SEVERITY] CODE location: message`; set-level findings show
  `(set)` instead of `exposure_types`, process findings show `process.<field>`. Exit codes are
  unchanged.
- The editor's Checks tab has a Rule column.
- `lpbf set` with several assignments saves nothing if any one is invalid (0.1.0 also saved nothing,
  but left the in-memory set partly modified).

Remaining limitations:

- Contour pass paths are still list indices (D-15).
- Validation still uses the library's single machine profile (D-11); machine profiles are not
  self-validated (D-14); chess-pattern checks (D-13) are missing. All deliberately out of B2 scope.
- `assign_field` guards only validators on the object being assigned. Today the only model-level
  validator is on `ExposureSet`, so this covers every case; a future cross-level validator on a
  parent would need the same treatment.
- GUI structural edits (add/remove/move exposure types and contour passes) still assign directly;
  they are pre-checked against the same constraints, so they cannot be rejected, but they do not go
  through `assign_field`.

---

## 12. Status after staged Milestone B3 (2026-10-26)

### B3-A: machine-profile consistency and applicability (implemented)

| ID | Status | Root cause | Fix |
|---|---|---|---|
| D-11 | Fixed | The library stored exactly one `machine.json`, and `validate()` fell back to `MachineProfile()` when none was passed; `ExposureSet.machine` was never read. | Profiles stored in `machines/` and resolved by the set's `machine` name in `validate_set` / `Library.validate`. Blank (`MRF-001`), unknown (`MRF-002`) and inconsistent (`MRF-003`) references skip the machine-dependent rules; `validate()` without a profile reports `MRF-004`. No fallback anywhere. |
| D-14 | Fixed | `MachineProfile` had per-field ranges only. | `check_machine` reports `MPR-001` (blank name), `MPR-002` (min layer > max layer), `MPR-003` (window not finite, negative or min ≥ max). Profiles are not rejected at load time, so an inconsistent 0.1.0 `machine.json` still opens and is reported. |

Backward compatibility: a 0.1.0 library without `machine.json` gets the generic profile, equal to
the old built-in default, and its sets give the same findings as before (test). A 0.1.0
`machine.json` is read under its own name, saved back in place and never moved; if its name is not
the one the sets use, the sets now get `MRF-002` instead of being checked silently against it.

### B3-B: infill-skipping energy (specification only)

`docs/calc_spec_infill_skipping.md` is the draft for review. Energy code and expected values are
unchanged; `tests/test_energy_characterization.py` pins the current behaviour (energy ignores
`skip_layers`; the estimator counts only exposed layers).

### Tests

- Before the B3-A change, with old-semantics stubs (`docs/audit/b3_old_api_stub.py`):
  **34 failed, 266 passed**. The energy characterization tests passed (they describe current behaviour).
- After: **300 passed, 0 failed, 0 skipped** with `LPBF_REQUIRE_GUI_TESTS=1`.

### Remaining limitations

- Profile references match exact names; renaming a profile requires export, edit, import, and
  updating the sets that use it.
- The GUI "Machine profile" dialog edits only the profile the selected set names; there is no GUI
  to create profiles (CLI `lpbf machine import`).
- The energy window is still not editable in the GUI (tuple field, unchanged since 0.1.0).
- New sets start with a blank `machine` and therefore warn `MRF-001` until a profile is named.

### B3-B: E_eff addition (approved, implemented 2026-10-26)

Implemented per `docs/calc_spec_infill_skipping.md` revision 2: E_eff = P/(v·h·(n+1)·t) as a
separately labelled "idealized layer-cycle average energy density", rule `ENG-003`, and display in
CLI and GUI. Nominal VED, `ENG-001`/`ENG-002` and the energy-window semantics are unchanged;
`tests/test_core.py` and `tests/test_energy_characterization.py` are untouched. Double-exposure
accounting is not implemented (E_eff unavailable).

Tests: 34 new tests failed before the implementation; after it **338 passed, 0 failed, 0 skipped**
with `LPBF_REQUIRE_GUI_TESTS=1`. The B2 0.1.0-baseline comparison excludes the new `ENG-003` code,
which adds findings without changing any 0.1.0 finding. D-12 is addressed for reporting; the
physical questions in section 8 of the specification remain open.

## 13. Status after Milestone B4 (2026-10-26)

DOE range and combination audit in `docs/doe_audit.md` (committed before any code change, probes in
`docs/audit/b4_doe_probe.py`). D-04, D-05, D-06, D-07 and D-21 are fixed, together with the new
findings D-29 to D-43 recorded there; D-38 (no whole-set re-validation of a variant) had no
observable wrong result and is fixed without a dedicated regression test.

Tests: 77 new tests (73 in `tests/test_doe.py`, four DOE dialog tests in `tests/test_gui.py`). Of the
76 committed before the fix, 58 failed on `cd7e10b` (`docs/audit/b4_tests_before_fix.txt`);
`test_count_text` was added with the fix. After the fix **415 passed, 0 failed,
0 skipped** with `LPBF_REQUIRE_GUI_TESTS=1`. `tests/test_core.py` is unchanged and passes.
Energy calculations, E_eff, validation rules and thresholds, GUI architecture and storage format
are unchanged.

## 14. Status after Milestone B5 (2026-10-26)

DOE save and naming hardening, approved items 1 to 4 (details in `docs/doe_audit.md` section 7).
Baseline tag `b4-accepted` at `34cb1f5`. Batch saving remains non-transactional by design of this
milestone: a failure leaves the variants saved before it, and the CLI and GUI now list saved, failed
and not-attempted variants. Unusable file names are caught before saving; names are exact when
6 digits would round a value; `0,05`-style decimal commas are refused.

Tests: 59 new (56 in `tests/test_doe_b5.py`, three in `tests/test_gui.py`); 29 failed on
`b4-accepted`. Full suite **474 passed, 0 failed, 0 skipped** with `LPBF_REQUIRE_GUI_TESTS=1`.
No change to energy, E_eff, validation rules or thresholds, the file format or the write order.

## 15. Status after Milestone B6 (2026-10-26)

Ctrl+C reporting and visible DOE levels (details in `docs/doe_audit.md` section 8). Rollback point:
tag `b5-accepted` at `31f94dc`. After an early stop the DOE save reports saved, failed, uncertain
and not-attempted variants as read back from disk (exit 130 after Ctrl+C); every dry run lists the
parsed levels. Batch saving remains non-transactional.

Tests: 27 new (25 in `tests/test_doe_b6.py`, two in `tests/test_gui.py`), one B4 assertion updated
for the approved dry-run lines; 26 failed on `b5-accepted`. Full suite **501 passed, 0 failed,
0 skipped** with `LPBF_REQUIRE_GUI_TESTS=1`. No change to energy, E_eff, validation rules or
thresholds, storage, the file format, the schema or the write order. Next: B7, specification only
(stable contour-pass identifiers), awaiting approval.

## 16. Status after B7 phase 1, schema-version guard (2026-10-26)

Rollback points: tags `b5-accepted` (`31f94dc`) and `b6-accepted` (`08dbd44`). Policy in
`docs/file_format.md`: version 1 is supported; a missing `schema_version` is read as version 1
(legacy policy); future versions (2, 99, ...) and invalid values (0, negatives, strings, decimals,
booleans, null, lists, objects) are refused before any other field is read, in the library,
`lpbf import`, `load_json` and the model. Refused files are never written. `lpbf validate` with
no names now exits 1 when any set file could not be loaded (previously such files were skipped
and the command could exit 0); the editor's status bar now gives each skipped file's reason.

Tests: 43 new (41 in `tests/test_schema_version.py`, two in `tests/test_gui.py`); 34 failed before
the guard (`docs/audit/b7p1_tests_before_fix.txt`); two serialization tests were added after review
and also pass on `08dbd44`. Full suite **544 passed, 0 failed, 0 skipped**
with `LPBF_REQUIRE_GUI_TESTS=1`. No contour-pass ids, no migration, no change to DOE, E_eff,
energy, validation rules or thresholds, the write order or storage.

Entry-point audit (`docs/audit/b7p1_entry_point_audit.md`): no library operation, CLI command or
GUI action overwrites, renames or deletes a refused file; `lpbf export` to an explicit path can
(F1, pre-existing); GUI delete gives no dialog when refused (F2); DOE API wording (F3). Not fixed.

## 17. Status after B7 Phase 2, stages S1+S2: contour-pass ids (2026-10-26)

Design: `plans/b7p2_design_contour_pass_ids.md`, revision 2 (outside the repository). Rollback
points: tags `b4-accepted`, `b5-accepted`, `b6-accepted` and commit `a805f4f` (Phase 1 accepted).

Format 2 adds a required, read-only `id` to every contour pass, serialized first; nothing else
changes (`docs/file_format.md`). Format 1 files are read and written exactly as before.

- **Formats are kept.** New sets are format 1. Save, copy, rename, DOE and import keep the source's
  format and pass ids. `schema_version` and pass ids are read-only in `set_path`, `lpbf set`, the
  editor and DOE.
- **Id problems refuse the file.** Missing, malformed, duplicate or mixed ids, and ids in a
  format 1 or legacy file, are refused with a message naming the pass, and the file is never
  written. The writer refuses to save anything it could not read back.
- **Paths.** `contour.passes.@<id>` and the index form name the same field, and DOE treats them as
  one factor. Format 2 output (show, CSV, compare, DOE list, validation findings) uses the id form,
  with `pass_id`/`pass_position` context on findings.
- **Unchanged.** Energy, offsets and estimates do not depend on ids (tested).

Not implemented: the upgrade command (S3), the format 1 export (S4) and structural comparison (S5).

Tests: 70 new (66 in `tests/test_pass_ids.py`, 4 in `tests/test_gui.py`). Five Phase 1 test
functions now treat version 2 as supported and use 3 as the first unsupported version. 52 tests
failed or errored before the implementation (`docs/audit/b7p2_s2_tests_before_fix.txt`). Full suite
**612 passed, 0 failed, 0 skipped** with `LPBF_REQUIRE_GUI_TESTS=1`.

Format 1 outputs were compared with `a805f4f` using `docs/audit/b7p2_v1_outputs.sh`: `list`,
`show`, `validate`, `compare`, `energy`, the JSON and CSV export, the DOE dry run and save, an
unknown path and `rename`. Command output and written files are identical apart from the temporary
folder names.

## Criteria changes decided 2026-11-08

These replace the criteria in section 8 from this date. The earlier text and the status sections above are kept as the record of their time.

**B7 completion.** The format 1 export (S4) is tracked separately. Structural comparison (S5) is folded into E-AC3. B7 still has to account for the restore recovery decisions that remain open.

**Milestone C.** C-AC3's summary panel covers data the model defines; qualification status is deferred until a qualification model is designed.

**Milestone D** (folder-of-JSON history, DC-001):

- D-AC1 and D-AC5 are removed (SQLite is not used).
- D-AC2. Every save of a tracked or adoptable file creates an append-only revision with time and origin. History is listable (presentation is authorized separately).
- D-AC3. Deferred. Lock and unlock need their own design; this is not implemented.
- D-AC4. An untracked file is adopted on its first explicit write, with its exact bytes. Files in an anomaly state are never adopted. Adoption does not alter the file, and repeating the write creates no second object.
- D-AC6. Stored JSON carries `schema_version`; a newer version is refused; an older format changes only through the explicit upgrade.
- D-AC7. JSON import and export round trip preserves values and schema version. Provenance and qualification fields are deferred.

**Milestone E.**

- E-AC2 is replaced: a DOE save is not transactional. Each variant has its own outcome; variants already saved stay saved when a later one fails; every success and failure is reported; no atomicity or rollback is claimed.
- E-AC3 includes the comparison scope that S5 described.

**Milestone F.** `docs/calculations.md`, requested by B-AC8, does not exist yet. Writing and reviewing it is part of F-AC3; B-AC8 is not met until then.
