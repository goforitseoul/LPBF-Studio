"""Locked reference sets shipped with the application.

The numbers are illustrative starting points for a generic aluminium-type powder on a
400 W single-laser machine. They are NOT qualified material parameters; real sets must
come from the user's own process development.
"""

from __future__ import annotations

from .models import (
    Contour, Core, Edge, ExposureSet, Hatch, SetCategory, SupportHatch,
)

DISCLAIMER = "Illustrative example values, not a qualified material parameter set."


def default_sets() -> list[ExposureSet]:
    part = ExposureSet(
        name="Default_Part_40um",
        category=SetCategory.PART,
        description=f"Hatch + contour + edge reference set. {DISCLAIMER}",
        material="Generic Al alloy (example)",
        machine="Generic 400 W",
        locked=True,
        color="#2a9d8f",
        layer_thickness_mm=0.04,
        exposure_types=[Hatch(), Contour(), Edge()],
    )
    core = ExposureSet(
        name="Default_Part_Core_40um",
        category=SetCategory.PART,
        description=f"Hatch skin with a faster chess-pattern core for bulky parts. {DISCLAIMER}",
        material="Generic Al alloy (example)",
        machine="Generic 400 W",
        locked=True,
        color="#e9c46a",
        layer_thickness_mm=0.04,
        exposure_types=[Hatch(), Core(), Contour()],
    )
    support = ExposureSet(
        name="Default_Support_40um",
        category=SetCategory.SUPPORT,
        description=f"Line exposure for block/lattice supports. {DISCLAIMER}",
        material="Generic Al alloy (example)",
        machine="Generic 400 W",
        locked=True,
        color="#8d99ae",
        layer_thickness_mm=0.04,
        exposure_types=[SupportHatch()],
    )
    return [part, core, support]


def new_set(name: str, category: SetCategory = SetCategory.PART) -> ExposureSet:
    if category == SetCategory.SUPPORT:
        return ExposureSet(name=name, category=category, color="#8d99ae", exposure_types=[SupportHatch()])
    return ExposureSet(name=name, category=category, exposure_types=[Hatch(), Contour()])
