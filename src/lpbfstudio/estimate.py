"""Rough build-time estimates for comparing parameter sets and z-segmentation plans.

The model is a prismatic block with a constant cross-section: scan time is area / (h · v)
for hatch exposures plus perimeter / v for each contour pass, and every layer adds a recoat.
Jumps, skywriting and delays are not modelled, so use the numbers to compare options,
not to quote build times.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import calc
from .models import Contour, Core, ExposureMode, ExposureSet, Hatch, SupportHatch


@dataclass
class Estimate:
    set_name: str
    z_from_mm: float
    z_to_mm: float
    layers: int
    seconds: dict[str, float] = field(default_factory=dict)

    @property
    def total_s(self) -> float:
        return sum(self.seconds.values())


def estimate_segment(es: ExposureSet, area_mm2: float, perimeter_mm: float, z_from_mm: float, z_to_mm: float,
                     recoat_s: float = 9.0) -> Estimate:
    if z_to_mm <= z_from_mm:
        raise ValueError("segment must have positive height")
    t = es.layer_thickness_mm
    layers = math.ceil(round((z_to_mm - z_from_mm) / t, 6))
    est = Estimate(es.name, z_from_mm, z_to_mm, layers)
    s = est.seconds

    hatch = es.get("hatch")
    core = es.get("core")
    hatch_area = area_mm2
    if isinstance(core, Core) and core.enabled:
        # with a hatch skin, the core takes everything inside a band of skin_thickness_xy
        skin_area = min(area_mm2, perimeter_mm * core.skin_thickness_xy_mm) if isinstance(hatch, Hatch) else 0.0
        hatch_area = skin_area
        s["Core"] = layers * (area_mm2 - skin_area) / (core.hatch_distance_mm * core.speed_mm_s)
    if isinstance(hatch, Hatch) and hatch.infill.enabled and hatch_area > 0:
        f = hatch.infill
        exposed = sum(1 for i in range(layers) if calc.infill_exposed(i, f.skip_layers))
        factor = 2.0 if f.exposure_mode == ExposureMode.DOUBLE else 1.0
        s["Hatch"] = exposed * factor * hatch_area / (f.hatch_distance_mm * f.speed_mm_s)

    contour = es.get("contour")
    if isinstance(contour, Contour):
        per_layer = sum(perimeter_mm / cp.speed_mm_s for cp in contour.passes if cp.enabled)
        s["Contour"] = layers * per_layer

    support = es.get("support")
    if isinstance(support, SupportHatch) and support.enabled:
        # for support sets the perimeter argument is the total support line length per layer
        s["Support"] = layers * perimeter_mm / support.speed_mm_s

    s["Recoating"] = layers * recoat_s
    return est


def estimate_plan(segments: list[tuple[float, float, ExposureSet]], area_mm2: float, perimeter_mm: float,
                  recoat_s: float = 9.0) -> list[Estimate]:
    """Estimate a z-segmented part: [(z_from, z_to, set), ...] in ascending z."""
    ordered = sorted(segments, key=lambda seg: seg[0])
    for (a0, a1, _), (b0, _, _) in zip(ordered, ordered[1:]):
        if b0 < a1 - 1e-9:
            raise ValueError(f"segments overlap at z={b0}")
    return [estimate_segment(es, area_mm2, perimeter_mm, z0, z1, recoat_s) for z0, z1, es in ordered]


def fmt_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"
