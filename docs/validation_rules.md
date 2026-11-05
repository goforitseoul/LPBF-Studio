# Validation rules and parameter paths

This page lists every rule in `src/lpbfstudio/validation.py`, the meaning of its code, and the
path conventions that findings use. The rules, severities and thresholds are the same as in
0.1.0; Milestone B2 added codes, scopes, correct paths and structured context, nothing else.
`tests/data/validation_baseline_0_1_0.json` pins the 0.1.0 severities and messages, and the test
suite fails if they change.

The thresholds are illustrative engineering defaults, not qualified limits. The energy window and
the machine limits come from the machine profile; the other numbers (0.6, 90°, 1.45, …) are fixed in
code for now and are listed here so they are visible. Making them configurable per machine and
material is planned for a later milestone.

## Finding structure

`validate_set(set, profiles, process)` checks a set against the machine profile it names and
returns a list of `Issue` objects; `Library.validate(set)` calls it with the library's profiles and
process settings. `validate(set, machine, process)` checks against an explicitly given profile. The
CLI and the GUI both print the same objects.

| Field | Meaning |
|---|---|
| `code` | Stable rule code, for example `MCH-001`. Does not depend on message wording, labels or language. |
| `severity` | `error`, `warning` or `info`. Always the rule's registered severity. |
| `scope` | What the finding is about: `parameter`, `node`, `set` or `process` (below). |
| `path` | Canonical path for the scope; empty for `set`. |
| `message` | Human-readable explanation, same wording as 0.1.0. May change between versions; do not parse it. |
| `context` | Machine-readable details. Keys used: `value`, `limit`, `unit`, `parameters` (related parameter paths), `energy_j_mm3`, `window_j_mm3`, `layers`, `min_power_w`. |

Text form (`str(issue)`, used by `lpbf validate`): `[SEVERITY] CODE location: message`, where
location is the path, `(set)` for set-level findings, `process.<path>` for process settings, or
`machine.<path>` for machine-profile fields. Machine-dependent findings (`MCH-*`, `ENG-*`) carry
`context["machine"]` with the name of the profile they were checked against.

## Machine-profile references (since B3-A)

Each set names its profile in its `machine` field. Matching is exact and case-sensitive.

| Situation | Finding | Machine-dependent rules (`MCH-*`, `ENG-*`) |
|---|---|---|
| `machine` names a consistent profile | none (plus any `MPR` warnings, currently none defined) | run against that profile |
| `machine` is blank | `MRF-001` warning at `machine` | not run |
| `machine` names no known profile | `MRF-002` error at `machine`; context lists `available` names, `unreadable` profile files and a case-insensitive `did_you_mean` | not run |
| `machine` names a profile with `MPR` errors | `MRF-003` error at `machine`, plus the profile's `MPR` findings (scope `machine`) | not run |
| `validate(set)` called with no profile | `MRF-004` warning (set) | not run |

There is no fallback to another profile in any of these cases. Machine-independent rules always run.

Storage: one JSON file per profile in `<library>/machines/`. A 0.1.0 library's single `machine.json`
is read as one more profile under its own `name`, saved back in place, and never moved or deleted;
if a profile of the same name also exists in `machines/`, the folder copy is used and the duplicate is
reported. A library that has neither gets the generic example profile `Generic 400 W`, identical to
the 0.1.0 built-in default, so 0.1.0 libraries without `machine.json` validate exactly as before.

## Scopes and path conventions

| Scope | `path` refers to | Resolves with | Example |
|---|---|---|---|
| `parameter` | One leaf value in the exposure set | `get_path(set, path)` returns a scalar | `hatch.infill.power_w`, `contour.passes.1.downskin_power_w`, `layer_thickness_mm` |
| `node` | An exposure type or region; the finding is derived from several of its parameters, listed in `context["parameters"]` | `get_path(set, path)` returns a model | `hatch.infill`, `hatch.upskin`, `core`, `contour`, `edge` |
| `set` | The set as a whole (structure of its exposure types) | – (`path` is `""`) | |
| `process` | A field of the process settings, not of the set | `get_path(process, path)` | `platform_temperature_c` |
| `machine` | A field of a machine profile; `context["machine"]` names the profile | `get_path(profile, path)` | `min_layer_mm`, `energy_window_j_mm3` |

Path rules:

1. Segments are separated by `.` and use the model's field names, which carry the unit
   (`_w`, `_mm_s`, `_mm`, `_deg`, `_c`, `_pct`).
2. The first segment of an exposure-type path is the kind (`hatch`, `core`, `contour`, `edge`,
   `support`), never its position. Reordering exposure types does not change any path; a test
   checks this.
3. Regions inside Hatch are `infill`, `upskin`, `downskin`; pattern settings are
   `hatch.infill.stripes.*` and `hatch.infill.chess.*`.
4. Contour passes of format 1 sets are addressed by list index: `contour.passes.<i>.<field>`.
   **Limitation:** removing or reordering passes changes these indices (audit D-15). Passes of
   format 2 sets are addressed by id, `contour.passes.@<id>.<field>`, which does not change, and
   their findings add `pass_id` and `pass_position` to the context (`docs/file_format.md`).
5. Every emitted path is checked by the test suite to resolve for its scope.

## Rule inventory

| Code | Severity | Scope | Path | Rule | Condition (unchanged from 0.1.0) |
|---|---|---|---|---|---|
| `SET-001` | error | set | – | Set has no exposure types | empty `exposure_types` |
| `SET-002` | warning | set | – | Part set has neither Hatch nor Core | part set without `hatch` and `core` |
| `SET-003` | info | set | – | Part set has no Contour | part set without `contour` |
| `MCH-001` | error | parameter | the power field, e.g. `hatch.infill.power_w`, `hatch.infill.chess.gap_power_w`, `contour.passes.0.upskin_power_w`, `core.power_w`, `edge.power_w`, `support.power_w` | Laser power above the machine maximum | P > machine `max_power_w` (enabled exposures only) |
| `MCH-002` | error | parameter | the speed field, same pattern (`*_speed_mm_s`, `gap_speed_mm_s`) | Scan speed above the machine maximum | v > machine `max_speed_mm_s` |
| `MCH-003` | error | parameter | `layer_thickness_mm` | Layer thickness outside the machine range | t < `min_layer_mm` or t > `max_layer_mm` |
| `MCH-004` | error | process | `platform_temperature_c` | Platform temperature above the machine maximum | process temperature > `max_platform_temperature_c` |
| `ENG-001` | warning | node | `hatch.infill`, `hatch.upskin`, `hatch.downskin`, `core` | Volumetric energy below the energy window | P/(v·h·t) < window minimum |
| `ENG-002` | warning | node | same | Volumetric energy above the energy window | P/(v·h·t) > window maximum |
| `ENG-003` | warning (caution) | node | `hatch.infill` | Infill skipping: idealized layer-cycle average energy density | enabled infill with `skip_layers` > 0; reports E_eff = P/(v·h·(n+1)·t) or why it is unavailable (double exposure, power reduction, pattern none). Machine-independent; no energy window. See `docs/calc_spec_infill_skipping.md` |
| `SKN-001` | error | parameter | `hatch.upskin.thickness_mm`, `hatch.downskin.thickness_mm` | Skin thinner than one layer | thickness < t |
| `SKN-002` | warning | parameter | same | Skin thickness not a multiple of the layer | thickness/t not whole (tolerance 1e-6) |
| `SKN-003` | warning | parameter | `hatch.upskin.ridge_mm`, `hatch.downskin.ridge_mm` | Positive skin ridge | ridge > 0 |
| `SKY-001` | warning | parameter | `hatch.infill.skywriting`, `hatch.upskin.skywriting`, `hatch.downskin.skywriting` | Skywriting switched off | skywriting false |
| `HAT-001` | error | parameter | `hatch.infill.hatch_distance_mm` | Hatch distance not smaller than the stripe width | stripes pattern and h ≥ stripe width |
| `HAT-002` | warning | parameter | `hatch.infill.stripes.stripe_overlap_mm` | Stripe overlap at least half the stripe width | overlap ≥ width/2 |
| `HAT-003` | error | parameter | `hatch.infill.restriction_angle_deg` | Restriction blocks every direction | restriction ≥ 90° |
| `HAT-004` | warning | parameter | `hatch.infill.rotation_angle_deg` | Rotation is a multiple of 180° | rotation mod 180 = 0 (includes 0° and 360°) |
| `HAT-005` | warning | parameter | `hatch.infill.skip_layers` | Infill skipping without both skins | skip_layers > 0 and upskin or downskin disabled |
| `HOM-001` | info | parameter | `hatch.infill.power_reduction_factor` | Power reduction selected with factor 0 | homogenization = power reduction, r = 0 |
| `HOM-002` | warning | parameter | same | Power reduction factor above 0.6 | homogenization = power reduction, r > 0.6 |
| `HOM-003` | info | parameter | same | Factor set but not used | homogenization ≠ power reduction, r > 0 |
| `COR-001` | warning | node | `core` | Core without Hatch | enabled core in a set without hatch |
| `CON-001` | warning | node | `contour` | Contour pass names not unique | duplicate pass names |
| `EDG-001` | warning | node | `edge` | Both edge methods off | method 1 and method 2 disabled |
| `EDG-002` | info | parameter | `edge.edge_factor` | Edge factor below the minimum edge factor | method 1 on, edge factor < 1.45 |
| `MPR-001` | error | machine | `name` | Machine profile has no name | name blank after trimming |
| `MPR-002` | error | machine | `min_layer_mm` | Minimum layer thickness above the maximum | `min_layer_mm` > `max_layer_mm` (equal is allowed) |
| `MPR-003` | error | machine | `energy_window_j_mm3` | Energy window invalid | either bound not finite, minimum < 0, or minimum ≥ maximum |
| `MRF-001` | warning | parameter | `machine` | Set names no machine profile | `machine` blank |
| `MRF-002` | error | parameter | `machine` | Named profile not found | no loaded profile has exactly that name |
| `MRF-003` | error | parameter | `machine` | Named profile is inconsistent | the profile has at least one `MPR` error |
| `MRF-004` | warning | set | – | No machine profile supplied | `validate()` called without a profile |

Code families: `SET` set structure, `MCH` machine limits, `ENG` energy input, `SKN` skins,
`SKY` skywriting, `HAT` hatch geometry and angles, `HOM` homogenisation, `COR` core,
`CON` contour, `EDG` edge, `MPR` machine-profile consistency, `MRF` machine-profile reference.

## Rules for changing this list

- A code is never renumbered, reused or given a different meaning. To change a rule's meaning,
  retire its code (leave it documented as retired) and add a new one.
- Message wording may be improved without changing the code.
- `tests/test_validation_rules.py` freezes the inventory: adding, removing or renaming a code fails
  the test until the test and this page are updated together.

## Known limitations

- (Resolved in B3-A: sets are checked against the profile they name, D-11; profiles are checked for
  consistency, D-14.)
- A machine profile cannot be renamed from the editor or CLI; export, edit the name, and import.
- Contour pass paths of format 1 sets use indices (D-15); format 2 sets use pass ids.
- The chess pattern has no hatch-distance check (D-13), and power reduction always uses the stripe
  width as h0 (D-26). These rules were deliberately not changed in B2.
