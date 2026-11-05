# Calculation specification: idealized layer-cycle average energy density (E_eff)

Status: **approved 2026-10-26 with conditions (revision 2).** E_eff is added as a separate,
explicitly qualified metric. Nominal volumetric energy density (VED), its calculation and all
existing energy-window checks (`ENG-001`, `ENG-002`) are unchanged. Double-exposure energy
accounting is not specified and not implemented.

Audit references: D-12 (energy ignores skipping), A-02 (double exposure), A-03 (effective layer),
A-10 (one energy window for all regions).

## 1. What infill skipping means in the existing implementation

Confirmed from the code, not assumed from any commercial processor:

| Item | Where | What it says |
|---|---|---|
| Parameter | `models.InFill.skip_layers` (`int`, 0 to 10, unit "layers", guard caution) | "Expose infill only every (n+1)-th layer. Up/downskin and contours are still exposed every layer." |
| Layer rule | `calc.infill_exposed(layer_index, skip_layers)` | exposed when `layer_index % (n + 1) == 0` |
| Used by | `estimate.estimate_segment` | counts exposed infill layers, with `layer_index` 0 at the bottom of each z-segment |

So **n = `skip_layers` is the number of skipped layers between two exposed infill layers**:
n = 0 exposes every layer (no skipping), n = 1 exposes every second layer, n = 2 every third.
The test `test_n_is_the_number_of_skipped_layers_between_exposures` checks this against
`calc.infill_exposed`.

Further facts the formula relies on:

1. The unit of skipping is a whole layer of the infill region. An exposed layer is scanned with
   the full pattern at hatch distance h; a skipped layer has no infill scan. No hatch lines or
   groups of lines are skipped.
2. Skipping does not depend on the pattern (stripes, chess) and does not change h. The factor
   (n+1) belongs to the powder depth per exposure, not to h; applying it to h would also wrongly
   change h-based results such as scan time.
3. Only infill skips. UpSkin, DownSkin, Core, Contour and Edge are exposed every layer.

## 2. Variables and units

| Symbol | Meaning | Unit | Field |
|---|---|---|---|
| P | infill laser power | W = J/s | `hatch.infill.power_w` |
| v | infill scan speed | mm/s | `hatch.infill.speed_mm_s` |
| h | infill hatch distance | mm | `hatch.infill.hatch_distance_mm` |
| t | layer thickness | mm | `layer_thickness_mm` |
| n | skipped layers between exposed infill layers | – | `hatch.infill.skip_layers` |
| N | layers in a finite infill region | – | geometry, not in the model |
| K | exposed infill layers in that region | – | depends on N, n and the layer start |

## 3. Nominal VED (existing, unchanged)

E_nom = f · P / (v · h · t)  [J/mm³], with f = 2 for double exposure (existing assumption A-02).

E_nom is the energy density of one infill exposure referred to one nominal layer. It ignores n.
`ENG-001`/`ENG-002` compare E_nom with the machine energy window; this is unchanged.

## 4. The new metric

**Name:** Idealized layer-cycle average energy density, symbol E_eff. Displayed only with that
qualifier.

**Definition:** the infill energy delivered per unit volume, averaged over one complete periodic
layer cycle of n+1 layers that contains exactly one infill exposure.

E_eff = P / (v · h · (n + 1) · t)  [J/mm³]

Units: (J/s) / ((mm/s) · mm · mm) = J/mm³. For n = 0, E_eff = E_nom (single exposure).

Derivation: over one cycle the infill area A receives one exposure, P/(v·h) · A joules, and the
cycle adds (n+1)·t·A of material, so the cycle average is P/(v·h·(n+1)·t). Which layer of the cycle
is exposed does not matter for a complete cycle.

### 4.1 Assumptions (all required)

1. Uniform layer thickness t over the cycle.
2. Constant infill geometry over the cycle (the same infill area on all n+1 layers).
3. Constant exposure parameters: the same P, v, h on every exposure.
4. Periodic skipping: exactly one exposed layer per cycle of n+1 layers.
5. Supported exposure model only (4.2).

### 4.2 Supported and unsupported cases

E_eff is calculated only when every condition below holds; otherwise it is **unavailable** and the
reason is reported instead of a number.

| Condition | Unavailable reason when it fails |
|---|---|
| infill enabled | "infill is disabled" |
| single exposure (`exposure_mode = single`) | "double exposure: energy accounting for the second pass is not specified" |
| homogenization not power reduction, or power-reduction factor 0 | "power-reduction homogenization varies the power along the vectors" |
| pattern stripes or chess | "exposure pattern 'none'" |

Invalid inputs to the pure function `calc.layer_cycle_energy` (P < 0, v ≤ 0, h ≤ 0, t ≤ 0,
n negative or not an integer, non-finite values) raise `ValueError`. The model's field ranges
already prevent these values in stored sets.

### 4.3 What E_eff is not

E_eff is an idealized average over a periodic layer cycle. It is **not** a prediction of
melt-pool depth or behaviour, powder fusion, density or part quality, and it is not an exact
local or per-part value. Whether one exposure fuses (n+1)·t of powder depends on melt-pool depth,
material and machine, none of which are in the model. Changing the displayed metric changes
nothing physical; changing `skip_layers` does change the energy the infill receives, which E_nom
does not show and E_eff does.

## 5. Layer start and finite builds

### 5.1 Layer start

The model does not define which physical layer is `layer_index` 0: the platform, the part, the
infill region or the z-segment (the estimator uses the segment start). E_eff does not need this
choice, because a complete cycle contains exactly one exposure whatever the starting layer. A
finite region does depend on it.

### 5.2 Periodic average versus finite-build average

For a finite infill region of N consecutive layers with constant geometry, the finite-build average
is E_fin = K · P/(v·h) / (N · t), so

E_fin / E_eff = (n + 1) · K / N.

Among any N consecutive layers the number of exposed layers is K = ⌊N/(n+1)⌋ or ⌈N/(n+1)⌉,
depending on the layer start. Therefore

(n+1)·⌊N/(n+1)⌋ / N  ≤  E_fin / E_eff  ≤  (n+1)·⌈N/(n+1)⌉ / N.

- If N is a multiple of n+1, both bounds are 1: E_fin = E_eff exactly, for any layer start.
- Otherwise E_fin differs from E_eff because of one partial cycle, with or without its exposed
  layer. The relative difference is at most n/N in either direction: |E_fin/E_eff − 1| ≤ n/N
  (from ⌈N/(n+1)⌉ ≤ (N+n)/(n+1) and ⌊N/(n+1)⌋ ≥ (N−n)/(n+1)).
- Short regions are dominated by this: with n = 10 and N = 5, E_fin is either 0 (no infill layer
  exposed) or 2.2 × E_eff (one exposure over five layers). The earlier draft quoted only the
  "+120 %" case; the full range is 0 to +120 % for that example, and up to a factor n+1 for N = 1.

E_fin also changes when skins replace infill on some layers of a cycle, when the first exposure
of a region sits on fewer than n+1 layers of powder, and when the cross-section varies. None of
these are modelled. E_eff is therefore presented only as a periodic idealization, never as the
average of a specific part or region.

### 5.3 Consistency with the estimator (an identity, not a validation)

`estimate.py` counts K with layer start 0 at the segment bottom, so K = ⌈N/(n+1)⌉. Its hatch time is
proportional to K. For N a multiple of n+1, the energy implied by that count equals E_eff exactly;
this follows from the same counting rule and is not independent evidence about the process.

## 6. Hand-calculated examples

### 6.1 Reference set (Default_Part_40um infill, single exposure)

P = 285 W, v = 960 mm/s, h = 0.11 mm, t = 0.04 mm.
v·h·t = 960 × 0.11 × 0.04 = 105.6 × 0.04 = 4.224 mm³/s. E_nom = 285 / 4.224 = 67.4716 J/mm³.

| n | (n+1)·t [mm] | E_nom [J/mm³] (unchanged) | E_eff [J/mm³] |
|---:|---:|---:|---:|
| 0 | 0.04 | 67.4716 | 67.4716 |
| 1 | 0.08 | 67.4716 | 285 / 8.448 = 33.7358 |
| 2 | 0.12 | 67.4716 | 285 / 12.672 = 22.4905 |
| 10 (field maximum) | 0.44 | 67.4716 | 285 / 46.464 = 6.1338 |

### 6.2 Second set

P = 300 W, v = 1000 mm/s, h = 0.10 mm, t = 0.03 mm: E_nom = 300/3 = 100.0 J/mm³;
E_eff = 100.0 (n = 0), 50.0 (n = 1), 33.3333 (n = 2).

### 6.3 Finite regions (section 5.2), reference set, E_eff for n = 1 is 33.7358

| N | n | K range | E_fin range [J/mm³] |
|---:|---:|---|---|
| 250 | 1 | 125 | 33.7358 (exact) |
| 251 | 1 | 125 to 126 | 33.6014 to 33.8702 |
| 5 | 10 | 0 to 1 | 0 to 13.4943 (E_eff = 6.1338) |

### 6.4 Unavailable and boundary cases

| Case | Result |
|---|---|
| n = 0 | E_eff = E_nom, no `ENG-003` |
| double exposure, n = 1 | unavailable (double exposure); `ENG-003` reports the reason |
| power reduction r = 0.2, n = 1 | unavailable (power reduction) |
| power reduction selected with r = 0, n = 1 | available (constant power) |
| infill disabled, n = 1 | no infill row and no `ENG-003` |
| pattern none, n = 1 | unavailable |
| P = 0 | E_eff = 0 (valid input) |
| v = 0, h = 0, t = 0, n = −1, n = 1.5, P = −1, P = ∞ | `ValueError` from `calc.layer_cycle_energy` |

## 7. Presentation

- E_nom keeps its row, value and label.
- When n > 0, the CLI `lpbf energy` and the GUI energy table add a separate line
  "Idealized layer-cycle average energy density (E_eff), n = …" with the value or "unavailable:
  reason", and the qualifier that it is a periodic-cycle idealization, not a per-part or quality
  prediction.
- Rule `ENG-003` (severity warning, the caution level of the validation scale; node `hatch.infill`)
  fires whenever infill skipping is enabled (n > 0) on an enabled infill. Its message states E_eff
  or the reason it is unavailable, and that it is an idealized average. It is machine-independent
  and does not use the energy window.
- No existing finding changes. Energy-window semantics are unchanged.

## 8. Unresolved and out of scope

1. Double-exposure energy accounting (A-02): not specified; E_eff unavailable.
2. The physical layer that `layer_index` 0 refers to: undefined in the model; E_eff does not
   depend on it, finite regions do (section 5).
3. Melt-pool penetration, powder packing and shrinkage: not in the model.
4. Geometry (varying sections, skin transitions): needs the geometry pipeline.
5. Energy windows for E_eff: none; E_eff is not compared with any window.
