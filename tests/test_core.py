import math

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from lpbfstudio import calc
from lpbfstudio.cli import main
from lpbfstudio.defaults import default_sets
from lpbfstudio.doe import generate, parse_values
from lpbfstudio.estimate import estimate_plan
from lpbfstudio.library import Library, LibraryError
from lpbfstudio.models import Contour, Core, Edge, ExposureSet, Hatch, MachineProfile, SetCategory, SupportHatch
from lpbfstudio.paths import diff, flatten, get_path, set_path
from lpbfstudio.validation import Severity, has_errors, validate


@pytest.fixture
def lib(tmp_path):
    return Library(tmp_path / "lib")


def part() -> ExposureSet:
    return default_sets()[0].model_copy(deep=True)


# ---------------------------------------------------------------- calculations

def test_volumetric_energy_matches_reference_example():
    # 285 W, 960 mm/s, 0.11 mm hatch, 0.04 mm layer -> 67.47 J/mm³
    assert calc.volumetric_energy(285, 960, 0.11, 0.04) == pytest.approx(67.47, abs=0.01)


def test_power_reduction_formula():
    assert calc.reduced_power(285, 0.0, 0.0, 5) == 285
    assert calc.reduced_power(285, 1.0, 0.0, 5) == 0
    assert calc.reduced_power(285, 0.2, 2.5, 5) == pytest.approx(285 - 0.2 * 285 * 0.5)
    assert calc.reduced_power(285, 0.5, 99, 5) == 285  # clamped to h0


def test_ridge_for_70_degrees():
    assert calc.ridge_for_angle(0.09, 70) == pytest.approx(-0.0328, abs=1e-4)


def test_infill_skipping_pattern():
    assert [calc.infill_exposed(i, 2) for i in range(7)] == [True, False, False, True, False, False, True]
    assert all(calc.infill_exposed(i, 0) for i in range(5))


def test_hatch_angles_rotate_and_skip_restricted():
    assert calc.hatch_angles(0, 90, 0, 3) == [0, 90, 0]
    angles = calc.hatch_angles(0, 67, 20, 40)
    assert all(not calc.is_restricted(a, 20) for a in angles)


def test_offsets_are_additive():
    es = part()
    hatch = es.get("hatch")
    from lpbfstudio.models import ProcessSettings
    offs = calc.effective_offsets(es, ProcessSettings(global_beam_offset_mm=0.04))
    assert offs["Hatch"] == pytest.approx(0.04 + hatch.hatch_offset_mm)


# ---------------------------------------------------------------- model rules

def test_set_rejects_duplicate_and_wrong_kinds():
    with pytest.raises(ValidationError):
        ExposureSet(exposure_types=[Hatch(), Hatch()])
    with pytest.raises(ValidationError):
        ExposureSet(category=SetCategory.SUPPORT, exposure_types=[Hatch()])
    with pytest.raises(ValidationError):
        ExposureSet(exposure_types=[Hatch(), Core(), Contour(), Edge(), SupportHatch()])


def test_field_ranges_are_enforced():
    es = part()
    with pytest.raises(ValidationError):
        es.get("edge").min_radius_factor = 1.5


def test_json_roundtrip():
    es = part()
    again = ExposureSet.model_validate_json(es.model_dump_json())
    assert again == es


# ---------------------------------------------------------------- validation

def test_defaults_have_no_errors():
    for es in default_sets():
        assert not has_errors(validate(es)), es.name


def test_machine_limits_flagged():
    es = part()
    set_path(es, "hatch.infill.power_w", "450")
    issues = validate(es, MachineProfile(max_power_w=400))
    assert any(i.severity == Severity.ERROR and "power" in i.path for i in issues)


def test_skywriting_off_warns():
    es = part()
    es.get("hatch").infill.skywriting = False
    assert any("skywriting" in i.path for i in validate(es))


# ---------------------------------------------------------------- paths / doe

def test_paths_get_set_and_diff():
    a, b = part(), part()
    set_path(b, "contour.passes.0.power_w", "175")
    assert get_path(b, "contour.passes.0.power_w") == 175
    assert ("contour.passes.0.power_w", 150.0, 175.0) in diff(a, b)
    assert "hatch.infill.stripes.stripe_width_mm" in flatten(a)


def test_parse_values():
    assert parse_values("0.09, 0.11,0.13", 0.1) == [0.09, 0.11, 0.13]
    assert parse_values("0.09:0.13:0.02", 0.1) == [0.09, 0.11, 0.13]
    assert parse_values("1:3:1", 0) == [1, 2, 3]


def test_doe_full_factorial():
    variants = generate(part(), {"hatch.infill.hatch_distance_mm": [0.09, 0.13], "layer_thickness_mm": [0.03, 0.04]},
                        "HD_{hatch_distance_mm}_LT{layer_thickness_mm}")
    assert [v.name for v in variants] == ["HD_0.09_LT0.03", "HD_0.09_LT0.04", "HD_0.13_LT0.03", "HD_0.13_LT0.04"]
    assert all(not v.locked for v in variants)


def test_doe_duplicate_names_rejected():
    with pytest.raises(ValueError):
        generate(part(), {"hatch.infill.power_w": [200, 250]}, "same")


# ---------------------------------------------------------------- library

def test_library_locking(lib):
    assert set(lib.names()) >= {"Default_Part_40um", "Default_Support_40um"}
    es = lib.get("Default_Part_40um")
    es.layer_thickness_mm = 0.03
    with pytest.raises(LibraryError):
        lib.save(es)
    with pytest.raises(LibraryError):
        lib.delete("Default_Part_40um")
    copy = lib.copy("Default_Part_40um", "Mine")
    assert not copy.locked
    copy.layer_thickness_mm = 0.03
    lib.save(copy)
    assert lib.get("Mine").layer_thickness_mm == 0.03
    lib.rename("Mine", "Mine2")
    lib.delete("Mine2")
    assert not lib.exists("Mine2")


# ---------------------------------------------------------------- estimate

def test_thicker_layers_build_faster():
    thin = part()
    thick = part()
    thick.layer_thickness_mm = 0.06
    t_thin = estimate_plan([(0, 30, thin)], 400, 80)[0].total_s
    t_thick = estimate_plan([(0, 30, thick)], 400, 80)[0].total_s
    assert t_thick < t_thin


def test_overlapping_segments_rejected():
    with pytest.raises(ValueError):
        estimate_plan([(0, 20, part()), (10, 30, part())], 400, 80)


# ---------------------------------------------------------------- cli

def test_cli_flow(tmp_path):
    r = CliRunner()
    base = ["--library", str(tmp_path / "lib")]
    assert r.invoke(main, base + ["list"]).exit_code == 0
    assert r.invoke(main, base + ["copy", "Default_Part_40um", "P1"]).exit_code == 0
    res = r.invoke(main, base + ["set", "P1", "hatch.infill.power_w=300"])
    assert res.exit_code == 0, res.output
    res = r.invoke(main, base + ["set", "Default_Part_40um", "hatch.infill.power_w=300"])
    assert res.exit_code == 1
    res = r.invoke(main, base + ["energy", "P1"])
    assert "71.02" in res.output
    res = r.invoke(main, base + ["doe", "P1", "-f", "hatch.infill.hatch_distance_mm=0.09,0.13", "--name", "HD_{hatch_distance_mm}"])
    assert "created HD_0.09" in res.output
    out = tmp_path / "p1.csv"
    assert r.invoke(main, base + ["export", "P1", str(out)]).exit_code == 0
    assert "hatch.infill.power_w" in out.read_text()
    res = r.invoke(main, base + ["estimate", "--area", "400", "--perimeter", "80", "-s", "0:10:P1"])
    assert "Total" in res.output
