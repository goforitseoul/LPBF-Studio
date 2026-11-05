"""Idealized layer-cycle average energy density E_eff (docs/calc_spec_infill_skipping.md, rev. 2).

Expected values are calculated by hand in the specification, section 6:
v·h·t = 960 × 0.11 × 0.04 = 4.224; E_nom = 285 / 4.224 = 67.4716 J/mm³; E_eff = E_nom / (n + 1).
"""

import math

import pytest
from click.testing import CliRunner

from lpbfstudio import calc
from lpbfstudio.cli import main
from lpbfstudio.defaults import default_sets
from lpbfstudio.library import Library
from lpbfstudio.models import ExposureMode, HatchPattern, Homogenization, MachineProfile
from lpbfstudio.validation import Severity, Scope, validate

E_NOM = 67.4716


def part(skip: int = 0):
    es = default_sets()[0].model_copy(deep=True)
    es.get("hatch").infill.skip_layers = skip
    return es


def infill(es):
    return es.get("hatch").infill


# ---------------------------------------------------------------- interpretation of n


@pytest.mark.parametrize("n", [0, 1, 2, 10])
def test_n_is_the_number_of_skipped_layers_between_exposures(n):
    exposed = [i for i in range(60) if calc.infill_exposed(i, n)]
    gaps = {b - a - 1 for a, b in zip(exposed, exposed[1:])}
    assert gaps == {n}


# ---------------------------------------------------------------- pure formula


@pytest.mark.parametrize("n, expected", [(0, 67.4716), (1, 33.7358), (2, 22.4905), (10, 6.1338)])
def test_reference_values(n, expected):
    assert calc.layer_cycle_energy(285, 960, 0.11, 0.04, n) == pytest.approx(expected, abs=1e-4)


@pytest.mark.parametrize("n, expected", [(0, 100.0), (1, 50.0), (2, 33.3333)])
def test_second_example(n, expected):
    assert calc.layer_cycle_energy(300, 1000, 0.10, 0.03, n) == pytest.approx(expected, abs=1e-4)


def test_n0_equals_nominal_ved():
    assert calc.layer_cycle_energy(285, 960, 0.11, 0.04, 0) == calc.volumetric_energy(285, 960, 0.11, 0.04)


def test_zero_power_is_valid():
    assert calc.layer_cycle_energy(0, 960, 0.11, 0.04, 1) == 0


@pytest.mark.parametrize("args", [
    (285, 0, 0.11, 0.04, 1),
    (285, 960, 0, 0.04, 1),
    (285, 960, 0.11, 0, 1),
    (285, 960, 0.11, 0.04, -1),
    (285, 960, 0.11, 0.04, 1.5),
    (285, 960, 0.11, 0.04, True),
    (-1, 960, 0.11, 0.04, 1),
    (math.inf, 960, 0.11, 0.04, 1),
    (285, math.nan, 0.11, 0.04, 1),
])
def test_invalid_inputs_raise(args):
    with pytest.raises(ValueError):
        calc.layer_cycle_energy(*args)


# ---------------------------------------------------------------- availability


def test_available_for_single_exposure_stripes():
    ce = calc.infill_cycle_energy(infill(part(1)), 0.04)
    assert ce.available and ce.value == pytest.approx(33.7358, abs=1e-4) and ce.reason is None
    assert ce.skip_layers == 1


def test_available_for_chess():
    es = part(1)
    infill(es).pattern = HatchPattern.CHESS
    assert calc.infill_cycle_energy(infill(es), 0.04).value == pytest.approx(33.7358, abs=1e-4)


@pytest.mark.parametrize("change, reason", [
    (lambda f: setattr(f, "exposure_mode", ExposureMode.DOUBLE), "double exposure"),
    (lambda f: (setattr(f, "homogenization", Homogenization.POWER_REDUCTION),
                setattr(f, "power_reduction_factor", 0.2)), "power-reduction"),
    (lambda f: setattr(f, "pattern", HatchPattern.NONE), "pattern 'none'"),
    (lambda f: setattr(f, "enabled", False), "infill is disabled"),
])
def test_unavailable_cases_give_a_reason_and_no_number(change, reason):
    es = part(1)
    change(infill(es))
    ce = calc.infill_cycle_energy(infill(es), 0.04)
    assert not ce.available
    assert ce.value is None
    assert reason in ce.reason


def test_power_reduction_selected_with_zero_factor_is_available():
    es = part(1)
    infill(es).homogenization = Homogenization.POWER_REDUCTION
    assert calc.infill_cycle_energy(infill(es), 0.04).available


# ---------------------------------------------------------------- energy summary


def test_summary_adds_cycle_energy_only_with_skipping():
    row0 = [m for m in calc.energy_summary(part(0)) if m.region == "InFill"][0]
    assert row0.cycle is None
    row = [m for m in calc.energy_summary(part(2)) if m.region == "InFill"][0]
    assert row.energy == pytest.approx(E_NOM, abs=1e-4)  # nominal unchanged
    assert row.cycle.value == pytest.approx(22.4905, abs=1e-4)


def test_summary_other_rows_have_no_cycle_energy():
    assert all(m.cycle is None for m in calc.energy_summary(part(2)) if m.region != "InFill")


def test_summary_double_exposure_cycle_unavailable_nominal_unchanged():
    es = part(1)
    infill(es).exposure_mode = ExposureMode.DOUBLE
    row = [m for m in calc.energy_summary(es) if m.region.startswith("InFill")][0]
    assert row.energy == pytest.approx(2 * E_NOM, abs=1e-4)
    assert row.cycle is not None and not row.cycle.available


# ---------------------------------------------------------------- ENG-003


def eng3(es, machine=MachineProfile()):
    return [i for i in validate(es, machine) if i.code == "ENG-003"]


def test_eng003_reports_e_eff_when_skipping():
    (issue,) = eng3(part(1))
    assert issue.severity == Severity.WARNING and issue.scope == Scope.NODE and issue.path == "hatch.infill"
    assert issue.context["e_eff_j_mm3"] == pytest.approx(33.7358, abs=1e-4)
    assert issue.context["e_nom_j_mm3"] == pytest.approx(E_NOM, abs=1e-4)
    assert issue.context["skip_layers"] == 1
    assert "hatch.infill.skip_layers" in issue.context["parameters"]
    assert "33.7" in issue.message and "idealized" in issue.message
    assert "not a prediction" in issue.message


def test_eng003_absent_without_skipping_or_when_infill_disabled():
    assert eng3(part(0)) == []
    es = part(1)
    infill(es).enabled = False
    assert eng3(es) == []


def test_eng003_states_why_e_eff_is_unavailable():
    es = part(1)
    infill(es).exposure_mode = ExposureMode.DOUBLE
    (issue,) = eng3(es)
    assert issue.context["e_eff_j_mm3"] is None
    assert "unavailable" in issue.message and "double exposure" in issue.message


def test_eng003_is_machine_independent_and_leaves_window_checks_alone():
    es = part(2)  # E_eff 22.5 is below the 30 J/mm³ window minimum, E_nom 67.5 is inside
    with_machine = validate(es, MachineProfile())
    assert not [i for i in with_machine if i.code in ("ENG-001", "ENG-002") and i.path == "hatch.infill"]
    assert eng3(es, None)  # also reported when no machine profile is known


# ---------------------------------------------------------------- CLI


def test_cli_energy_shows_qualified_e_eff(tmp_path):
    r, base = CliRunner(), ["--library", str(tmp_path / "lib")]
    r.invoke(main, base + ["copy", "Default_Part_40um", "A"])
    res = r.invoke(main, base + ["energy", "A"])
    assert "67.47" in res.output and "E_eff" not in res.output
    r.invoke(main, base + ["set", "A", "hatch.infill.skip_layers=1"])
    res = r.invoke(main, base + ["energy", "A"])
    assert res.exit_code == 0
    assert "67.47" in res.output  # nominal row unchanged
    assert "Idealized layer-cycle average energy density (E_eff)" in res.output
    assert "33.74 J/mm³" in res.output
    assert "not a prediction" in res.output
    r.invoke(main, base + ["set", "A", "hatch.infill.exposure_mode=double"])
    res = r.invoke(main, base + ["energy", "A"])
    assert "unavailable" in res.output and "double exposure" in res.output
    assert Library(tmp_path / "lib", seed=False).get("A").get("hatch").infill.skip_layers == 1
