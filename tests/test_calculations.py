"""docs/calculations.md (B-AC8, carried into F-AC3): one hand-computed value and one boundary or invalid-input case for each
documented formula that had none. Values are worked by hand in the comments; tolerances are for float rounding only."""

import math

import pytest

from lpbfstudio import calc
from lpbfstudio.defaults import default_sets
from lpbfstudio.estimate import estimate_plan, estimate_segment
from lpbfstudio.models import Edge, Hatch, Homogenization


def test_volumetric_energy_hand_value():
    # 285 W / (960 mm/s · 0.11 mm · 0.04 mm) = 285 / 4.224 = 67.4716 J/mm³
    assert calc.volumetric_energy(285, 960, 0.11, 0.04) == pytest.approx(67.4716, abs=1e-4)


def test_areal_energy_hand_value_and_zero_speed():
    # 200 W / (1000 mm/s · 0.04 mm) = 5.0 J/mm²
    assert calc.areal_energy(200, 1000, 0.04) == pytest.approx(5.0)
    with pytest.raises(ZeroDivisionError):
        calc.areal_energy(200, 0, 0.04)


def test_line_energy_hand_value_and_zero_speed():
    # 200 W / 1000 mm/s = 0.2 J/mm
    assert calc.line_energy(200, 1000) == pytest.approx(0.2)
    with pytest.raises(ZeroDivisionError):
        calc.line_energy(200, 0)


def test_layer_cycle_energy_hand_value():
    # n = 1: 67.4716 / 2 = 33.7358 J/mm³
    assert calc.layer_cycle_energy(285, 960, 0.11, 0.04, 1) == pytest.approx(33.7358, abs=1e-4)


def test_reduced_power_hand_values_and_clamping():
    # P(h) = 300 − 0.2 · 300 · (1 − h/5)
    assert calc.reduced_power(300, 0.2, 0, 5) == pytest.approx(240.0)
    assert calc.reduced_power(300, 0.2, 2.5, 5) == pytest.approx(270.0)
    assert calc.reduced_power(300, 0.2, 5, 5) == pytest.approx(300.0)
    assert calc.reduced_power(300, 0.2, 9, 5) == pytest.approx(300.0)  # h clamped to h0
    assert calc.reduced_power(300, 0.2, -1, 5) == pytest.approx(240.0)  # h clamped to 0
    with pytest.raises(ValueError):
        calc.reduced_power(300, 0.2, 1, 0)


def test_power_reduction_table_hand_values():
    rows = calc.power_reduction_table(300, 0.2, 5, steps=5)
    assert [round(h, 9) for h, _ in rows] == [0, 1, 2, 3, 4, 5]
    assert [round(p, 9) for _, p in rows] == [240, 252, 264, 276, 288, 300]


def test_skin_layers_exact_and_inexact():
    assert calc.skin_layers(0.09, 0.03) == (3, True)
    assert calc.skin_layers(0.10, 0.03) == (3, False)  # 3.33 layers, not a multiple
    assert calc.skin_layers(0.01, 0.03) == (0, False)  # thinner than one layer


def test_ridge_for_angle_hand_value_and_domain():
    # −0.09 / tan(70°) = −0.09 / 2.747477 = −0.032757 mm
    assert calc.ridge_for_angle(0.09, 70) == pytest.approx(-0.032757, abs=1e-6)
    for bad in (0, 90, -5, 120):
        with pytest.raises(ValueError):
            calc.ridge_for_angle(0.09, bad)


def test_corridor_angle_hand_value_and_zero_corridor():
    assert calc.corridor_angle(0.5, 0.5) == pytest.approx(45.0)  # atan(1)
    assert calc.corridor_angle(0.5, 0) == 90.0
    assert calc.corridor_angle(0.5, -1) == 90.0


def test_hatch_restriction_boundary():
    assert calc.is_restricted(10, 20) and not calc.is_restricted(20, 20)  # strictly inside ±restriction
    assert calc.is_restricted(175, 20)  # 5° from the axis across 180°
    assert not calc.is_restricted(10, 0)  # restriction 0 restricts nothing


def test_infill_exposed_pattern():
    assert [calc.infill_exposed(i, 2) for i in range(7)] == [True, False, False, True, False, False, True]
    assert all(calc.infill_exposed(i, 0) for i in range(5))


def test_total_beam_offset_is_additive():
    assert calc.total_beam_offset(0.05, 0.01, 0.02) == pytest.approx(0.08)
    assert calc.total_beam_offset(0.05) == pytest.approx(0.05)


def test_edge_methods_hand_values():
    e = Edge()  # edge_offset 0.02 mm, edge_factor 2.0, min_radius_factor 0.5
    # TBO = 0.05 + 0 + 0.02 = 0.07 mm
    assert calc.edge_length(e, 0.05) == pytest.approx(2.0 * 0.07)
    assert calc.edge_length(e, 0.05, peak_longer_than_threshold=True) == pytest.approx(1.45 * 0.07)
    assert calc.edge_min_peak_width(e, 0.05) == pytest.approx(0.5 * 2 * 0.07)


def test_homogenization_note_hand_value_and_absence():
    es = default_sets()[0].model_copy(deep=True)
    assert calc.homogenization_note(es) is None  # no power reduction
    f = es.get("hatch").infill
    f.homogenization = Homogenization.POWER_REDUCTION
    f.power_reduction_factor = 0.2
    # shortest vectors: 285 · (1 − 0.2) = 228.0 W
    assert calc.homogenization_note(es) == "Power reduction: shortest vectors get 228.0 W (of 285.0 W)."
    assert isinstance(es.get("hatch"), Hatch)


def test_estimate_segment_hand_value():
    es = default_sets()[0]  # t 0.04; infill h 0.11, v 960, no skipping; contour passes at 600 and 900 mm/s
    r = estimate_segment(es, area_mm2=100, perimeter_mm=40, z_from_mm=0, z_to_mm=1)
    assert r.layers == 25  # 1 mm / 0.04 mm
    assert r.seconds["Hatch"] == pytest.approx(25 * 100 / (0.11 * 960))  # 23.674 s
    assert r.seconds["Contour"] == pytest.approx(25 * (40 / 600 + 40 / 900))  # 2.778 s
    assert r.seconds["Recoating"] == pytest.approx(25 * 9.0)
    assert r.total_s == pytest.approx(sum(r.seconds.values()))


def test_estimate_boundaries():
    es = default_sets()[0]
    with pytest.raises(ValueError):
        estimate_segment(es, 100, 40, 1, 1)  # zero height
    with pytest.raises(ValueError):
        estimate_plan([(0, 2, es), (1, 3, es)], 100, 40)  # overlap
    r = estimate_segment(es, 100, 40, 0, 0.041)
    assert r.layers == 2 and math.isclose(r.seconds["Recoating"], 18.0)  # a partial layer counts as one
