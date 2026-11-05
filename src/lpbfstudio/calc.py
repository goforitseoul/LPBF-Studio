"""Process calculations: energy input, power reduction, skin geometry, hatch angles, offsets."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .models import (
    Contour, Core, Edge, ExposureMode, ExposureSet, Hatch, HatchPattern, Homogenization, InFill,
    ProcessSettings, SupportHatch,
)

MIN_EDGE_FACTOR = 1.45


# --------------------------------------------------------------------------- energy input


def volumetric_energy(power_w: float, speed_mm_s: float, hatch_mm: float, layer_mm: float) -> float:
    """Volumetric energy density E = P / (v · h · t) in J/mm³ (hatch exposures)."""
    return power_w / (speed_mm_s * hatch_mm * layer_mm)


def areal_energy(power_w: float, speed_mm_s: float, layer_mm: float) -> float:
    """Energy input E = P / (v · t) in J/mm² (contours, edges, support lines)."""
    return power_w / (speed_mm_s * layer_mm)


def line_energy(power_w: float, speed_mm_s: float) -> float:
    """Line energy P / v in J/mm."""
    return power_w / speed_mm_s


# --------------------------------------------------------------------------- infill skipping

CYCLE_ENERGY_LABEL = "Idealized layer-cycle average energy density (E_eff)"
CYCLE_ENERGY_CAVEAT = ("idealized average over a periodic layer cycle; not a prediction of melt-pool behaviour, "
                       "powder fusion, density or part quality, and not an exact per-part value")


def layer_cycle_energy(power_w: float, speed_mm_s: float, hatch_mm: float, layer_mm: float,
                       skip_layers: int) -> float:
    """Idealized layer-cycle average energy density E_eff = P / (v · h · (n + 1) · t) in J/mm³.

    n = ``skip_layers`` is the number of skipped layers between two exposed infill layers (n = 0:
    every layer exposed, E_eff = nominal VED). The value is the infill energy per unit volume over
    one complete cycle of n + 1 layers with uniform t, constant geometry and constant P, v, h.
    See docs/calc_spec_infill_skipping.md for assumptions and limits.
    """
    if isinstance(skip_layers, bool) or not isinstance(skip_layers, int) or skip_layers < 0:
        raise ValueError(f"skip_layers must be a non-negative integer, got {skip_layers!r}")
    values = {"power_w": power_w, "speed_mm_s": speed_mm_s, "hatch_mm": hatch_mm, "layer_mm": layer_mm}
    for name, x in values.items():
        if not math.isfinite(x):
            raise ValueError(f"{name} must be finite, got {x!r}")
    if power_w < 0:
        raise ValueError(f"power_w must not be negative, got {power_w!r}")
    for name in ("speed_mm_s", "hatch_mm", "layer_mm"):
        if values[name] <= 0:
            raise ValueError(f"{name} must be positive, got {values[name]!r}")
    return power_w / (speed_mm_s * hatch_mm * (skip_layers + 1) * layer_mm)


@dataclass
class CycleEnergy:
    """E_eff for one infill, or the reason it cannot be given."""

    skip_layers: int
    value: float | None
    reason: str | None = None

    @property
    def available(self) -> bool:
        return self.value is not None


def cycle_energy_text(ce: CycleEnergy) -> str:
    return f"{ce.value:.2f} J/mm³" if ce.available else f"unavailable: {ce.reason}"


def infill_cycle_energy(infill: InFill, layer_mm: float) -> CycleEnergy:
    """E_eff for an infill, unavailable where its energy accounting is not specified."""
    n = infill.skip_layers
    if not infill.enabled:
        return CycleEnergy(n, None, "infill is disabled")
    if infill.exposure_mode != ExposureMode.SINGLE:
        return CycleEnergy(n, None, "double exposure: energy accounting for the second pass is not specified")
    if infill.homogenization == Homogenization.POWER_REDUCTION and infill.power_reduction_factor > 0:
        return CycleEnergy(n, None, "power-reduction homogenization varies the power along the vectors")
    if infill.pattern not in (HatchPattern.STRIPES, HatchPattern.CHESS):
        return CycleEnergy(n, None, f"exposure pattern {infill.pattern.value!r}")
    return CycleEnergy(n, layer_cycle_energy(infill.power_w, infill.speed_mm_s, infill.hatch_distance_mm,
                                             layer_mm, n))


# --------------------------------------------------------------------------- homogenization


def reduced_power(p0: float, r: float, h: float, h0: float) -> float:
    """Power for a hatch vector of length h with power reduction factor r.

    P(h) = P0 - r · P0 · (1 - h / h0), with h clamped to [0, h0] (h0 = stripe width).
    A zero-length vector receives P0 · (1 - r).
    """
    if h0 <= 0:
        raise ValueError("nominal vector length h0 must be positive")
    ratio = min(max(h / h0, 0.0), 1.0)
    return p0 - r * p0 * (1.0 - ratio)


def power_reduction_table(p0: float, r: float, h0: float, steps: int = 5) -> list[tuple[float, float]]:
    return [(h0 * i / steps, reduced_power(p0, r, h0 * i / steps, h0)) for i in range(steps + 1)]


# --------------------------------------------------------------------------- skin geometry


def skin_layers(thickness_mm: float, layer_mm: float) -> tuple[int, bool]:
    """Number of layers a skin thickness spans, and whether it is an exact multiple."""
    n = thickness_mm / layer_mm
    whole = round(n)
    return whole, math.isclose(n, whole, abs_tol=1e-6)


def ridge_for_angle(skin_thickness_mm: float, max_overhang_angle_deg: float) -> float:
    """Ridge value that suppresses skin exposure on overhangs steeper than the given angle.

    Measured from the horizontal: ridge = -thickness / tan(angle). Example: 90 µm skin and
    70° gives -0.033 mm.
    """
    if not 0 < max_overhang_angle_deg < 90:
        raise ValueError("angle must be between 0 and 90 degrees")
    return -skin_thickness_mm / math.tan(math.radians(max_overhang_angle_deg))


def corridor_angle(thickness_mm: float, corridor_mm: float) -> float:
    """Approximate surface angle (from horizontal) at which contour skin detection switches.

    Inferred from the corridor/thickness geometry: a contour shift of at most ``corridor``
    over ``thickness`` of height. 90° means every wall counts.
    """
    if corridor_mm <= 0:
        return 90.0
    return math.degrees(math.atan(thickness_mm / corridor_mm))


# --------------------------------------------------------------------------- hatch angles


def _axis_distance(a: float, b: float) -> float:
    d = abs((a - b) % 180.0)
    return min(d, 180.0 - d)


def is_restricted(angle_deg: float, restriction_deg: float, flow_axis_deg: float = 0.0) -> bool:
    """A hatch direction is restricted when it lies within ±restriction of the gas-flow axis."""
    return restriction_deg > 0 and _axis_distance(angle_deg, flow_axis_deg) < restriction_deg


def hatch_angles(offset_deg: float, rotation_deg: float, restriction_deg: float, layers: int,
                 flow_axis_deg: float = 0.0) -> list[float]:
    """Hatch direction (0-180°) per layer, skipping restricted directions by rotating further."""
    out: list[float] = []
    angle = offset_deg
    for i in range(layers):
        if i > 0:
            angle += rotation_deg
        tries = 0
        while is_restricted(angle, restriction_deg, flow_axis_deg) and tries < 360:
            angle += rotation_deg if rotation_deg % 180 else 1.0
            tries += 1
        out.append(round(angle % 180.0, 6))
    return out


def infill_exposed(layer_index: int, skip_layers: int) -> bool:
    """Infill skipping: expose layer 0, skip ``skip_layers``, expose the next, ..."""
    return layer_index % (skip_layers + 1) == 0


# --------------------------------------------------------------------------- offsets


def total_beam_offset(gbo_mm: float, pbo_mm: float = 0.0, extra_mm: float = 0.0) -> float:
    """Offsets are additive: TBO = GBO + PBO (+ edge / contour / hatch offset)."""
    return gbo_mm + pbo_mm + extra_mm


def edge_length(edge: Edge, gbo_mm: float, pbo_mm: float = 0.0, peak_longer_than_threshold: bool = False) -> float:
    """Method 1: distance from the contour intersection to the edge end = factor × TBO."""
    tbo = total_beam_offset(gbo_mm, pbo_mm, edge.edge_offset_mm)
    factor = MIN_EDGE_FACTOR if peak_longer_than_threshold else edge.edge_factor
    return factor * tbo


def edge_min_peak_width(edge: Edge, gbo_mm: float, pbo_mm: float = 0.0) -> float:
    """Method 2: edges are added where the peak width exceeds factor × 2 × TBO."""
    return edge.min_radius_factor * 2 * total_beam_offset(gbo_mm, pbo_mm, edge.edge_offset_mm)


# --------------------------------------------------------------------------- summary


@dataclass
class Metric:
    exposure: str
    region: str
    power_w: float
    speed_mm_s: float
    hatch_mm: float | None
    energy: float
    unit: str
    cycle: CycleEnergy | None = None  # InFill with skipping only; ``energy`` stays the nominal VED


def energy_summary(es: ExposureSet) -> list[Metric]:
    """Energy input for every enabled region of every exposure type in a set."""
    t = es.layer_thickness_mm
    rows: list[Metric] = []

    def vol(exp: str, region: str, p: float, v: float, h: float, factor: float = 1.0) -> None:
        rows.append(Metric(exp, region, p, v, h, volumetric_energy(p, v, h, t) * factor, "J/mm³"))

    def area(exp: str, region: str, p: float, v: float) -> None:
        rows.append(Metric(exp, region, p, v, None, areal_energy(p, v, t), "J/mm²"))

    for et in es.exposure_types:
        if isinstance(et, Hatch):
            f = et.infill
            if f.enabled:
                factor = 2.0 if f.exposure_mode == ExposureMode.DOUBLE else 1.0
                vol("Hatch", "InFill" + (" (double)" if factor > 1 else ""), f.power_w, f.speed_mm_s, f.hatch_distance_mm, factor)
                if f.skip_layers > 0:
                    rows[-1].cycle = infill_cycle_energy(f, t)
                if f.pattern == HatchPattern.CHESS:
                    area("Hatch", "Chess gap", f.chess.gap_power_w, f.chess.gap_speed_mm_s)
            for name, s in (("UpSkin", et.upskin), ("DownSkin", et.downskin)):
                if s.enabled:
                    vol("Hatch", name, s.power_w, s.speed_mm_s, s.hatch_distance_mm)
        elif isinstance(et, Core) and et.enabled:
            vol("Core", "Core", et.power_w, et.speed_mm_s, et.hatch_distance_mm)
        elif isinstance(et, Contour):
            for cp in et.passes:
                if cp.enabled:
                    area("Contour", f"{cp.name} in-skin", cp.power_w, cp.speed_mm_s)
                    area("Contour", f"{cp.name} upskin", cp.upskin_power_w, cp.upskin_speed_mm_s)
                    area("Contour", f"{cp.name} downskin", cp.downskin_power_w, cp.downskin_speed_mm_s)
        elif isinstance(et, Edge) and et.enabled:
            area("Edge", "Edge", et.power_w, et.speed_mm_s)
        elif isinstance(et, SupportHatch) and et.enabled:
            area("Support", "Support line", et.power_w, et.speed_mm_s)
    return rows


def effective_offsets(es: ExposureSet, process: ProcessSettings, pbo_mm: float = 0.0) -> dict[str, float]:
    """Total offset of each scan line from the nominal contour (positive = inwards)."""
    gbo = process.global_beam_offset_mm
    out: dict[str, float] = {"GBO + PBO": total_beam_offset(gbo, pbo_mm)}
    hatch = es.get("hatch")
    if isinstance(hatch, Hatch):
        out["Hatch"] = total_beam_offset(gbo, pbo_mm, hatch.hatch_offset_mm)
    contour = es.get("contour")
    if isinstance(contour, Contour):
        for cp in contour.passes:
            out[cp.name] = total_beam_offset(gbo, pbo_mm, cp.offset_mm)
    edge = es.get("edge")
    if isinstance(edge, Edge):
        out["Edge"] = total_beam_offset(gbo, pbo_mm, edge.edge_offset_mm)
    return out


def homogenization_note(es: ExposureSet) -> str | None:
    hatch = es.get("hatch")
    if not isinstance(hatch, Hatch) or hatch.infill.homogenization != Homogenization.POWER_REDUCTION:
        return None
    f = hatch.infill
    p_min = reduced_power(f.power_w, f.power_reduction_factor, 0, f.stripes.stripe_width_mm)
    return f"Power reduction: shortest vectors get {p_min:.1f} W (of {f.power_w:.1f} W)."
