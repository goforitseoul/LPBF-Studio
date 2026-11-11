# Calculations

Every calculation LPBF Studio performs, with its formula, units, assumptions, valid inputs, limitations, implementation and tests. All of them are **comparison and planning aids**. None of them is a validated prediction of melt-pool behaviour, fusion, density, microstructure, distortion, mechanical properties or actual build time. They describe the parameters, not what the machine or the material will do.

Units: power P in W, speed v in mm/s, hatch distance h and layer thickness t in mm, angles in degrees, time in s.

## Energy input (`calc.py`)

| Name | Formula | Result | Used for | Tests |
|---|---|---|---|---|
| Volumetric energy density (VED) | E = P / (v · h · t) | J/mm³ | Hatch infill, upskin, downskin and core regions in the energy table; the energy-window checks `ENG-001` and `ENG-002` | `test_calculations.py::test_volumetric_energy_hand_value`; `test_core.py`; `test_energy_characterization.py` |
| Areal energy | E = P / (v · t) | J/mm² | Contours, edges, support lines and chess gaps | `test_calculations.py::test_areal_energy_hand_value_and_zero_speed` |
| Line energy | E = P / v | J/mm | Available to callers; not shown in the editor | `test_calculations.py::test_line_energy_hand_value_and_zero_speed` |

**Double exposure.** The energy table doubles the infill VED and labels the row "(double)". This is a bookkeeping convention: an approved specification of double-exposure energy accounting does not exist yet.

**Assumptions.** P, v, h and t are constant over the region, and the region is a uniform continuum. Jumps, delays, skywriting, overlap between vectors and laser–material coupling are ignored.

**Valid inputs.** v, h and t must be positive. The model's field limits already keep them away from zero; a direct call with v = 0 raises `ZeroDivisionError`.

**Limitations.** VED is a widely used screening number. Equal VED does not mean equal melting: the same value can come from very different melt-pool regimes.

**Energy window** (`validation.py`, rules `ENG-001` and `ENG-002`). The rule compares the nominal VED of each hatch and core region with the machine profile's `energy_window_j_mm3` (default 30 to 150 J/mm³). Below the window gives a lack-of-fusion warning, above it an overheating warning. The window is a user-maintained screening range, not material data. Profile consistency is checked by `MPR-003`. Tests: `test_validation_rules.py`.

## Infill skipping: idealized layer-cycle average (`calc.layer_cycle_energy`)

E_eff = P / (v · h · (n + 1) · t), where n is `skip_layers` (infill exposed every (n+1)-th layer).

| | |
|---|---|
| Result | J/mm³, shown as a separately labelled row; the energy-window checks keep the nominal VED |
| Assumptions | Uniform t, constant geometry and constant P, v, h over a complete cycle of n + 1 layers |
| Valid inputs | n ≥ 0 integer; finite inputs; P ≥ 0; v, h, t > 0. Anything else raises `ValueError` |
| Unavailable | Double exposure, power-reduction homogenization with a factor above 0, pattern "none", or disabled infill (`infill_cycle_energy` gives the reason) |
| Limitations | A periodic-cycle average, not an exact per-part value: short regions can differ by up to n/N, where N is the number of layers. Not a prediction of melt-pool behaviour or quality. Rule `ENG-003` reports it |
| Full specification | [calc_spec_infill_skipping.md](calc_spec_infill_skipping.md) |
| Tests | `test_layer_cycle_energy.py`; `test_calculations.py::test_layer_cycle_energy_hand_value` |

`calc.infill_exposed(i, n)` is true when i mod (n + 1) = 0: layer 0 is exposed, then the next n are skipped. Tests: `test_core.py`, `test_calculations.py::test_infill_exposed_pattern`.

## Power reduction for short vectors (`calc.reduced_power`, `power_reduction_table`)

P(h) = P0 − r · P0 · (1 − h / h0), with the vector length h clamped to [0, h0]. Here h0 is the stripe width and r the reduction factor (0 to 1). A zero-length vector gets P0 · (1 − r); a vector of length h0 or longer gets P0.

- h0 ≤ 0 raises `ValueError`. The editor's calculator shows six points from 0 to h0, and `homogenization_note` reports the minimum power when power reduction is selected.
- Limitation: it is a linear rule. Whether it achieves thermal homogenization is not modelled.
- Tests: `test_core.py`; `test_calculations.py::test_reduced_power_hand_values_and_clamping`, `test_power_reduction_table_hand_values`, `test_homogenization_note_hand_value_and_absence`.

## Skin geometry

| Name | Formula | Notes | Tests |
|---|---|---|---|
| `skin_layers` | n = thickness / t, rounded; exact if within 10⁻⁶ of a whole number | Feeds `SKN-001` (thinner than one layer) and `SKN-002` (not a multiple) | `test_calculations.py::test_skin_layers_exact_and_inexact`; `test_validation_rules.py` |
| `ridge_for_angle` | ridge = − thickness / tan(α), α measured from the horizontal | 0 < α < 90, otherwise `ValueError`. Example: 90 µm and 70° give −0.033 mm. "Ridge" follows the convention described in the field help | `test_core.py`; `test_calculations.py::test_ridge_for_angle_hand_value_and_domain` |
| `corridor_angle` | α = atan(thickness / corridor); corridor ≤ 0 gives 90° | An approximation of the angle at which contour skin detection switches; not used by the editor | `test_calculations.py::test_corridor_angle_hand_value_and_zero_corridor` |

## Hatch directions (`calc.is_restricted`, `hatch_angles`)

- A direction is restricted when its distance to the gas-flow axis, taken modulo 180°, is strictly less than the restriction angle. A restriction of 0 restricts nothing.
- `hatch_angles` starts at the offset, adds the rotation per layer, and keeps rotating while the direction is restricted.
- Limitation: with some combinations the sequence collapses to few directions. After 360 tries a restricted angle is returned without warning.
- Tests: `test_core.py`; `test_calculations.py::test_hatch_restriction_boundary`.

## Offsets and edges

| Name | Formula | Notes | Tests |
|---|---|---|---|
| `total_beam_offset` | TBO = GBO + PBO + extra (all mm, positive inwards) | PBO is 0 in the editor | `test_calculations.py::test_total_beam_offset_is_additive` |
| `effective_offsets` | TBO for GBO + PBO, hatch, each contour pass and the edge | Shown in the Offsets tab | `test_core.py`, `test_pass_ids.py` |
| `edge_length` (Method 1) | factor · TBO, where the factor is the edge factor or 1.45 for peaks longer than the threshold | Not shown in the editor | `test_calculations.py::test_edge_methods_hand_values` |
| `edge_min_peak_width` (Method 2) | min radius factor · 2 · TBO | Not shown in the editor | same |

These describe the parameter conventions only. No vectors are generated.

## Build-time estimate (`estimate.py`)

For a prismatic block of constant cross-section A (mm²) and contour length L (mm) from z0 to z1:

- layers = ceil((z1 − z0) / t)
- Hatch time = exposed layers · (2 for double exposure) · A_hatch / (h · v). Exposed layers count infill skipping. A_hatch is A, or, with a core, only the skin band min(A, L · core skin thickness xy).
- Core time = layers · (A − skin band) / (h_core · v_core)
- Contour time = layers · Σ (L / v) over the enabled passes
- Support time = layers · L / v, where L is the support line length per layer
- Recoating = layers · recoat time (process setting, default 9 s)

| | |
|---|---|
| Valid inputs | z1 > z0, otherwise `ValueError`; segments of a plan must not overlap (`ValueError`). Gaps between segments are not reported |
| Limitations | Jumps, delays, skywriting, laser on/off times, scanner acceleration, multiple lasers, upskin and downskin areas and actual part geometry are all ignored. Use it to compare sets and plans, not to quote build times; the CLI and the editor say so |
| Tests | `test_core.py`, `test_energy_characterization.py`, `test_pass_ids.py`; `test_calculations.py::test_estimate_segment_hand_value`, `test_estimate_boundaries` |

## DOE level parsing (`doe.py`)

A range `start:stop:step` has n = round((stop − start) / step) + 1 levels, and it must end exactly at `stop`. Integer parameters need whole numbers, and the levels must be distinct. The variant count is the product of the level counts, checked against the limit (default 500) before anything is built. Rules: [doe_audit.md](doe_audit.md). Tests: `test_core.py`, `test_doe.py`, `test_doe_b5.py`.
