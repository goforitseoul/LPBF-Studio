"""B4: DOE range parser, full-factorial generator, names and saving.

Defect ids refer to docs/doe_audit.md.
"""

import itertools
import math

import pytest
from click.testing import CliRunner

from lpbfstudio import doe
from lpbfstudio.cli import main
from lpbfstudio.defaults import default_sets
from lpbfstudio.doe import generate, parse_values
from lpbfstudio.library import Library
from lpbfstudio.models import ExposureSet, HatchPattern
from lpbfstudio.paths import get_path

HD = "hatch.infill.hatch_distance_mm"
POWER = "hatch.infill.power_w"
SKIP = "hatch.infill.skip_layers"


def part() -> ExposureSet:
    return default_sets()[0].model_copy(deep=True)


def levels(text, path=HD, **kw):
    return parse_values(text, get_path(part(), path), **kw)


# ---------------------------------------------------------------- ranges

@pytest.mark.parametrize("text, expected", [
    ("0.09:0.13:0.02", [0.09, 0.11, 0.13]),
    ("0.1:0.1:0.05", [0.1]),
    ("0.001:0.005:0.001", [0.001, 0.002, 0.003, 0.004, 0.005]),
    ("0:1:0.1", [round(i / 10, 9) for i in range(11)]),
    ("0.05 : 0.07 : 0.01", [0.05, 0.06, 0.07]),
])
def test_range_is_inclusive_and_ends_exactly_at_stop(text, expected):
    vals = levels(text)
    assert vals == expected
    assert vals[-1] == float(text.split(":")[1])


def test_range_with_negative_start_and_large_values():
    assert levels("-10:10:10", "hatch.infill.offset_angle_deg") == [-10.0, 0.0, 10.0]
    assert levels("200:300:12.5", POWER) == [200 + 12.5 * i for i in range(9)]


@pytest.mark.parametrize("text, stops", [
    ("0.09:0.15:0.04", ("0.13", "0.17")),  # D-04: used to give 0.17, beyond the stop
    ("0.09:0.14:0.02", ("0.13", "0.15")),
])
def test_step_that_does_not_divide_the_span_is_rejected(text, stops):
    with pytest.raises(ValueError) as exc:
        levels(text)
    for s in stops:
        assert s in str(exc.value)


def test_reversed_range_is_rejected():  # D-05
    with pytest.raises(ValueError, match="stop"):
        levels("0.13:0.09:0.02")


@pytest.mark.parametrize("text", ["0.09:0.13:0", "0.13:0.09:-0.02", "0.09:0.13:-0.02"])
def test_zero_or_negative_step_is_rejected(text):
    with pytest.raises(ValueError, match="step"):
        levels(text)


@pytest.mark.parametrize("text", ["nan:1:0.1", "0:inf:1", "0:1:nan", "-inf:0:1"])
def test_non_finite_range_is_a_value_error(text):  # D-29: inf used to raise OverflowError
    with pytest.raises(ValueError, match="finite"):
        levels(text)


@pytest.mark.parametrize("text", ["0.09:0.13", "0.09:0.13:0.02:1", "a:b:c", "0.09:0.13:0.02,0.15"])
def test_malformed_range_names_the_expected_form(text):  # D-30
    with pytest.raises(ValueError, match="start:stop:step"):
        levels(text)


def test_step_below_level_resolution_is_rejected():  # D-31
    with pytest.raises(ValueError):
        levels("0:1e-9:1e-12")


def test_integer_range_gives_integers():
    vals = levels("0:4:2", SKIP)
    assert vals == [0, 2, 4] and all(type(v) is int for v in vals)
    assert levels("1.0:3.0:1", SKIP) == [1, 2, 3]


@pytest.mark.parametrize("text", ["1:3:0.5", "0.5:2.5:1", "0:3:1.5"])
def test_integer_range_with_fractional_values_is_rejected(text):  # D-06
    with pytest.raises(ValueError, match="whole"):
        levels(text, SKIP)


def test_range_larger_than_the_limit_is_rejected_before_it_is_built():  # D-07
    with pytest.raises(ValueError, match="500"):
        levels("0:1e6:1")
    with pytest.raises(ValueError, match="10000001"):
        levels("0:1e7:1")  # counted, not materialised
    assert len(levels("0:999:1", POWER, max_levels=1000)) == 1000


# ---------------------------------------------------------------- level lists

def test_existing_list_syntax_is_unchanged():
    assert levels("0.09, 0.11,0.13") == [0.09, 0.11, 0.13]
    assert levels("0.09 0.11;0.13") == [0.09, 0.11, 0.13]
    assert levels("true,false", "hatch.infill.skywriting") == [True, False]
    assert levels("stripes,chess", "hatch.infill.pattern") == [HatchPattern.STRIPES, HatchPattern.CHESS]
    assert levels("0 1 2", SKIP) == [0, 1, 2]


@pytest.mark.parametrize("text", ["", "   ", " , ; "])
def test_empty_level_list_is_rejected(text):  # D-32
    with pytest.raises(ValueError, match="no levels"):
        levels(text)


@pytest.mark.parametrize("text", ["0.09,0.09", "0.1,0.10", "1,1.0"])
def test_duplicate_levels_are_rejected(text):  # D-33
    with pytest.raises(ValueError, match="duplicate"):
        levels(text)


@pytest.mark.parametrize("text", ["nan", "0.09,inf", "-inf"])
def test_non_finite_list_levels_are_rejected(text):  # D-29
    with pytest.raises(ValueError, match="finite"):
        levels(text)


def test_text_levels_keep_their_spaces():  # D-34
    assert levels("Alloy A, Alloy B", "material") == ["Alloy A", "Alloy B"]
    assert levels("Alloy A;Alloy B", "material") == ["Alloy A", "Alloy B"]


def test_invalid_typed_levels_still_fail():
    with pytest.raises(ValueError):
        levels("1.5", SKIP)
    with pytest.raises(ValueError):
        levels("stripes,zigzag", "hatch.infill.pattern")


# ---------------------------------------------------------------- counting and size limit

def test_predicted_count_matches_generated_count_and_product_is_complete():
    factors = {POWER: [250.0, 285.0, 300.0], HD: [0.09, 0.11], SKIP: [0, 1]}
    assert doe.count_variants(factors) == 12
    variants = generate(part(), factors)
    assert len(variants) == 12
    combos = [tuple(get_path(v, p) for p in factors) for v in variants]
    assert combos == list(itertools.product(*factors.values()))  # first factor varies slowest
    assert [v.name for v in variants] == [f"Default_Part_40um_DOE{i}" for i in range(1, 13)]


def test_default_limit():
    assert doe.DEFAULT_MAX_VARIANTS == 500


def test_excessive_combinations_are_rejected_before_any_variant_is_built(monkeypatch):  # D-07
    copies = []
    real = ExposureSet.model_copy
    monkeypatch.setattr(ExposureSet, "model_copy", lambda self, *a, **k: copies.append(1) or real(self, *a, **k))
    factors = {POWER: [200.0 + i for i in range(10)], HD: [0.05 + i / 100 for i in range(10)],
               "hatch.infill.speed_mm_s": [800.0 + 10 * i for i in range(10)]}
    base = part()
    copies.clear()
    with pytest.raises(ValueError) as exc:
        generate(base, factors)
    assert "1000" in str(exc.value) and "500" in str(exc.value)
    assert copies == []
    assert len(generate(base, factors, max_variants=1000)) == 1000


# ---------------------------------------------------------------- generator: factors

def test_empty_or_duplicate_levels_rejected_by_generate():
    with pytest.raises(ValueError, match="no levels"):
        generate(part(), {HD: []})
    with pytest.raises(ValueError, match="duplicate"):
        generate(part(), {HD: [0.09, 0.09]})


def test_aliased_paths_are_one_factor():  # D-35
    with pytest.raises(ValueError) as exc:
        generate(part(), {POWER: [250.0, 300.0], "exposure_types.0.infill.power_w": [200.0]})
    assert POWER in str(exc.value)


@pytest.mark.parametrize("path", ["description", "locked", "schema_version", "name"])
def test_metadata_fields_are_not_factors(path):  # D-36
    es = part()
    with pytest.raises(ValueError, match=path):
        generate(es, {path: [get_path(es, path)]})


@pytest.mark.parametrize("path", ["hatch.infill", "contour.passes", "hatch"])
def test_group_paths_are_not_factors(path):  # D-37
    with pytest.raises(ValueError, match="not a parameter"):
        generate(part(), {path: ["x"]})


def test_invalid_level_names_the_factor_and_level():  # D-37
    with pytest.raises(ValueError) as exc:
        generate(part(), {POWER: [250.0, -5.0]})
    msg = str(exc.value).splitlines()[0]
    assert POWER in msg and "-5" in msg


def test_base_is_untouched_and_variants_are_unlocked():
    base = part()
    assert base.locked
    before = base.model_dump()
    variants = generate(base, {POWER: [250.0, 300.0]})
    assert base.model_dump() == before
    assert not any(v.locked for v in variants)
    assert [get_path(v, POWER) for v in variants] == [250.0, 300.0]


def test_mixed_parameter_types():
    factors = {"hatch.infill.skywriting": [True, False], "hatch.infill.pattern": [HatchPattern.STRIPES, HatchPattern.CHESS],
               SKIP: [0, 1], HD: [0.09, 0.11]}
    variants = generate(part(), factors)
    assert len(variants) == 16
    got = {tuple(get_path(v, p) for p in factors) for v in variants}
    assert got == set(itertools.product(*factors.values()))
    assert all(type(get_path(v, SKIP)) is int for v in variants)


def test_nested_contour_pass_factor_changes_only_that_pass():
    variants = generate(part(), {"contour.passes.1.power_w": [70.0, 90.0]})
    assert [get_path(v, "contour.passes.1.power_w") for v in variants] == [70.0, 90.0]
    assert all(get_path(v, "contour.passes.0.power_w") == 150.0 for v in variants)
    assert len(generate(part(), {"hatch.infill.stripes.stripe_width_mm": [4.0, 6.0]})) == 2


# ---------------------------------------------------------------- names

def test_ambiguous_short_placeholder_is_rejected():  # D-39
    factors = {POWER: [250.0, 300.0], "edge.power_w": [90.0, 110.0]}
    with pytest.raises(ValueError) as exc:
        generate(part(), factors, "P{power_w}_{i}")
    assert POWER in str(exc.value) and "edge.power_w" in str(exc.value)
    names = [v.name for v in generate(part(), factors, "P{hatch.infill.power_w}_E{edge.power_w}")]
    assert names == ["P250_E90", "P250_E110", "P300_E90", "P300_E110"]


def test_base_name_with_braces():  # D-40
    base = part().model_copy(update={"name": "Ti{64}"})
    assert [v.name for v in generate(base, {HD: [0.09, 0.11]})] == ["Ti{64}_DOE1", "Ti{64}_DOE2"]


@pytest.mark.parametrize("template", ["", "   "])
def test_blank_names_are_rejected(template):  # D-41
    with pytest.raises(ValueError, match="name"):
        generate(part(), {HD: [0.09]}, template)


def test_existing_name_rules_are_unchanged():
    assert [v.name for v in generate(part(), {HD: [0.09, 0.11]}, "HD_{hatch_distance_mm}")] == ["HD_0.09", "HD_0.11"]
    with pytest.raises(KeyError):
        generate(part(), {HD: [0.09]}, "X_{foo}")
    with pytest.raises(ValueError, match="duplicate"):
        generate(part(), {HD: [0.09, 0.11]}, "same")


# ---------------------------------------------------------------- library pre-check

def test_check_new_names_reports_every_conflict(tmp_path):  # D-21, D-42
    lib = Library(tmp_path / "lib")
    lib.copy("Default_Part_40um", "P1_DOE2")
    problems = lib.check_new_names(["P1_DOE1", "P1_DOE2", "Default_Part_40um", "M_Alloy A", "M_Alloy_A"])
    text = "\n".join(problems)
    assert "P1_DOE2" in text and "Default_Part_40um" in text
    assert "M_Alloy A" in text and "M_Alloy_A" in text
    assert "P1_DOE1" not in text
    assert lib.check_new_names(["Fresh_1", "Fresh_2"]) == []


# ---------------------------------------------------------------- CLI

@pytest.fixture
def cli(tmp_path):
    r = CliRunner()
    base = ["--library", str(tmp_path / "lib")]
    assert r.invoke(main, base + ["copy", "Default_Part_40um", "P1"]).exit_code == 0

    def run(*args):
        return r.invoke(main, base + list(args))

    run.lib = Library(tmp_path / "lib")
    return run


def test_cli_saves_nothing_when_any_name_is_taken(cli):  # D-21
    assert cli("copy", "Default_Part_40um", "P1_DOE2").exit_code == 0
    assert cli("copy", "Default_Part_40um", "P1_DOE3").exit_code == 0
    assert cli("set", "P1_DOE2", "hatch.infill.power_w=111").exit_code == 0
    before = sorted(cli.lib.names())
    res = cli("doe", "P1", "-f", f"{HD}=0.09,0.11,0.13")
    assert res.exit_code == 1
    assert sorted(cli.lib.names()) == before
    assert "P1_DOE2" in res.output and "P1_DOE3" in res.output
    assert "created" not in res.output
    assert cli.lib.get("P1_DOE2").get("hatch").infill.power_w == 111


def test_cli_dry_run_reports_conflicts(cli):  # D-43
    assert cli("copy", "Default_Part_40um", "P1_DOE2").exit_code == 0
    res = cli("doe", "P1", "-f", f"{HD}=0.09,0.11", "--dry-run")
    assert res.exit_code == 1 and "P1_DOE2" in res.output
    res = cli("doe", "P1", "-f", f"{HD}=0.09", "--name", "{base}", "--dry-run")
    assert res.exit_code == 1 and "P1" in res.output


def test_cli_rejects_names_sharing_a_file(cli):  # D-42
    before = sorted(cli.lib.names())
    res = cli("doe", "P1", "-f", "material=Alloy A,Alloy_A", "--name", "M_{material}")
    assert res.exit_code == 1
    assert sorted(cli.lib.names()) == before


def test_cli_rejects_repeated_factor(cli):  # D-35
    res = cli("doe", "P1", "-f", f"{HD}=0.09,0.11", "-f", f"{HD}=0.13", "--dry-run")
    assert res.exit_code == 1 and HD in res.output


@pytest.mark.parametrize("levels_text", ["0.13:0.09:0.02", "0:inf:1", "0.09:0.15:0.04", "", "0.09,0.09"])
def test_cli_reports_bad_levels_cleanly(cli, levels_text):
    res = cli("doe", "P1", "-f", f"{HD}={levels_text}", "--dry-run")
    assert res.exit_code == 1
    assert isinstance(res.exception, SystemExit)
    assert res.output.startswith("error:")


def test_cli_prints_the_count_first(cli):
    res = cli("doe", "P1", "-f", f"{HD}=0.09:0.13:0.02", "-f", "layer_thickness_mm=0.03,0.04", "--dry-run")
    assert res.exit_code == 0, res.output
    lines = res.output.splitlines()
    assert lines[0] == "DOE: 3 × 2 = 6 variants"
    # B6: a dry run lists each factor's parsed levels after the count line (approved change)
    assert lines[1:3] == [f"  {HD}: 0.09, 0.11, 0.13", "  layer_thickness_mm: 0.03, 0.04"]
    assert lines[3:] == [f"would create P1_DOE{i}" for i in range(1, 7)]


def test_cli_variant_limit(cli):  # D-07
    args = ["doe", "P1", "-f", "hatch.infill.power_w=200:299:1", "-f", f"{HD}=0.05:0.10:0.01", "--dry-run"]
    res = cli(*args)
    assert res.exit_code == 1 and "600" in res.output and "--max-variants" in res.output
    res = cli(*args, "--max-variants", "600")
    assert res.exit_code == 0 and res.output.splitlines()[0] == "DOE: 100 × 6 = 600 variants"
    assert cli(*args, "--max-variants", "0").exit_code != 0


def test_cli_existing_syntax_still_creates(cli):
    res = cli("doe", "P1", "-f", f"{HD}=0.09 0.13", "--name", "HD_{hatch_distance_mm}")
    assert res.exit_code == 0, res.output
    assert "created HD_0.09" in res.output and "created HD_0.13" in res.output
    assert get_path(cli.lib.get("HD_0.13"), HD) == 0.13
    assert not cli.lib.get("HD_0.13").locked
    assert math.isclose(get_path(cli.lib.get("P1"), HD), 0.11)


def test_count_text():
    assert doe.count_text({HD: [0.09, 0.11, 0.13]}) == "3 variants"
    assert doe.count_text({HD: [0.09]}) == "1 variant"
    assert doe.count_text({HD: [0.09, 0.11], POWER: [250.0, 300.0]}) == "2 × 2 = 4 variants"
