"""Characterization of the CURRENT energy behaviour with infill skipping (Milestone B3-B).

These tests pin what 0.1.0 does today, including what the audit flags as questionable (D-12):
the reported infill energy ignores ``skip_layers``. They are expected to change only after the
specification in docs/calc_spec_infill_skipping.md has been approved. Expected values are
computed by hand: 285 / (960 * 0.11 * 0.04) = 285 / 4.224 = 67.4716 J/mm³.
"""

import pytest

from lpbfstudio import calc
from lpbfstudio.defaults import default_sets
from lpbfstudio.estimate import estimate_plan
from lpbfstudio.models import ExposureMode, MachineProfile
from lpbfstudio.validation import validate

E_NOM = 67.4716


def part(skip: int = 0):
    es = default_sets()[0].model_copy(deep=True)
    es.get("hatch").infill.skip_layers = skip
    return es


def infill_row(es):
    return [m for m in calc.energy_summary(es) if m.exposure == "Hatch" and m.region.startswith("InFill")][0]


@pytest.mark.parametrize("skip", [0, 1, 2, 10])
def test_reported_infill_energy_ignores_skip_layers(skip):
    row = infill_row(part(skip))
    assert row.energy == pytest.approx(E_NOM, abs=1e-4)
    assert row.unit == "J/mm³"
    assert row.region == "InFill"


def test_double_exposure_doubles_reported_energy_regardless_of_skipping():
    es = part(1)
    es.get("hatch").infill.exposure_mode = ExposureMode.DOUBLE
    row = infill_row(es)
    assert row.energy == pytest.approx(2 * E_NOM, abs=1e-4)
    assert row.region == "InFill (double)"


def test_skins_and_contours_unaffected_by_skip_layers():
    a = {(m.exposure, m.region): m.energy for m in calc.energy_summary(part(0)) if not m.region.startswith("InFill")}
    b = {(m.exposure, m.region): m.energy for m in calc.energy_summary(part(3)) if not m.region.startswith("InFill")}
    assert a == b


def test_energy_window_check_uses_nominal_energy():
    # 300 W, 1000 mm/s, 0.10 mm, 0.03 mm -> 100 J/mm³ nominal; with skip 3 it would be 25 if divided by 4.
    es = part(3)
    es.layer_thickness_mm = 0.03
    f = es.get("hatch").infill
    f.power_w, f.speed_mm_s, f.hatch_distance_mm = 300, 1000, 0.10
    issues = validate(es, MachineProfile())
    assert not [i for i in issues if i.code in ("ENG-001", "ENG-002") and i.path == "hatch.infill"]


@pytest.mark.parametrize("layers, skip, exposed", [(250, 0, 250), (250, 1, 125), (251, 1, 126), (250, 2, 84), (1, 5, 1)])
def test_infill_exposed_layer_count(layers, skip, exposed):
    assert sum(calc.infill_exposed(i, skip) for i in range(layers)) == exposed


def test_estimate_counts_only_exposed_infill_layers():
    # 10 mm block at 0.04 mm = 250 layers; area 400 mm²; time per exposure 400 / (0.11 * 960) s
    per_layer = 400 / (0.11 * 960)
    t0 = estimate_plan([(0, 10, part(0))], 400, 80)[0].seconds["Hatch"]
    t1 = estimate_plan([(0, 10, part(1))], 400, 80)[0].seconds["Hatch"]
    assert t0 == pytest.approx(250 * per_layer)
    assert t1 == pytest.approx(125 * per_layer)
