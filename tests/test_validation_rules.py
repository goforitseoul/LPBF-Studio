"""Validation findings: stable rule codes, canonical paths, scopes and boundaries (audit D-10, W-06).

Each rule has one trigger fixture below. Expected values are written out explicitly so that a
change to a code, a scope or a path fails a test.
"""

import re
from pathlib import Path

import pytest
from pydantic import BaseModel

from lpbfstudio.defaults import default_sets
from lpbfstudio.models import (
    Contour, ContourPass, Core, Edge, ExposureSet, Hatch, HatchPattern, Homogenization,
    MachineProfile, ProcessSettings,
)
from lpbfstudio.paths import get_path, set_path
from lpbfstudio.validation import RULES, Scope, Severity, validate

DOCS = Path(__file__).resolve().parents[1] / "docs" / "validation_rules.md"

EXPECTED_CODES = {
    "SET-001", "SET-002", "SET-003",
    "MCH-001", "MCH-002", "MCH-003", "MCH-004",
    "ENG-001", "ENG-002",
    "SKN-001", "SKN-002", "SKN-003",
    "SKY-001",
    "HAT-001", "HAT-002", "HAT-003", "HAT-004", "HAT-005",
    "HOM-001", "HOM-002", "HOM-003",
    "COR-001", "CON-001", "EDG-001", "EDG-002",
}
# B3-A: machine-profile consistency and references (tests/test_machine_profiles.py)
MACHINE_CODES = {"MPR-001", "MPR-002", "MPR-003", "MRF-001", "MRF-002", "MRF-003", "MRF-004"}
# Approved E_eff addition: idealized layer-cycle average energy (tests/test_layer_cycle_energy.py)
CYCLE_CODES = {"ENG-003"}

M400 = MachineProfile(max_power_w=400, max_speed_mm_s=7000, min_layer_mm=0.02, max_layer_mm=0.10,
                      max_platform_temperature_c=200)


def part(**changes) -> ExposureSet:
    es = default_sets()[0].model_copy(deep=True)  # Hatch, Contour, Edge
    for path, value in changes.items():
        set_path(es, path.replace("__", "."), value)
    return es


def core_set() -> ExposureSet:
    return default_sets()[1].model_copy(deep=True)  # Hatch, Core, Contour


def support_set() -> ExposureSet:
    return default_sets()[2].model_copy(deep=True)


def chess_part() -> ExposureSet:
    es = part()
    es.get("hatch").infill.pattern = HatchPattern.CHESS
    return es


def dup_pass_names() -> ExposureSet:
    es = part()
    for p in es.get("contour").passes:
        p.name = "C"
    return es


def skip_without_upskin() -> ExposureSet:
    es = part(**{"hatch.infill.skip_layers": 1})
    es.get("hatch").upskin.enabled = False
    return es


def with_core(es_core: ExposureSet, path: str, value) -> ExposureSet:
    set_path(es_core, path, value)
    return es_core


def with_support(path: str, value) -> ExposureSet:
    es = support_set()
    set_path(es, path, value)
    return es


def with_chess(path: str, value) -> ExposureSet:
    es = chess_part()
    set_path(es, path, value)
    return es


# (id, set factory, process or None, expected code, severity, scope, path)
CASES = [
    ("no types", lambda: ExposureSet(name="E"), None, "SET-001", Severity.ERROR, Scope.SET, ""),
    ("no hatch/core", lambda: ExposureSet(name="E", exposure_types=[Contour()]), None,
     "SET-002", Severity.WARNING, Scope.SET, ""),
    ("no contour", lambda: ExposureSet(name="E", exposure_types=[Hatch()]), None,
     "SET-003", Severity.INFO, Scope.SET, ""),
    ("infill power", lambda: part(**{"hatch.infill.power_w": 450}), None,
     "MCH-001", Severity.ERROR, Scope.PARAMETER, "hatch.infill.power_w"),
    ("upskin power", lambda: part(**{"hatch.upskin.power_w": 450}), None,
     "MCH-001", Severity.ERROR, Scope.PARAMETER, "hatch.upskin.power_w"),
    ("chess gap power", lambda: with_chess("hatch.infill.chess.gap_power_w", 450), None,
     "MCH-001", Severity.ERROR, Scope.PARAMETER, "hatch.infill.chess.gap_power_w"),
    ("contour pass 1 downskin power", lambda: part(**{"contour.passes.1.downskin_power_w": 450}), None,
     "MCH-001", Severity.ERROR, Scope.PARAMETER, "contour.passes.1.downskin_power_w"),
    ("contour pass 0 upskin power", lambda: part(**{"contour.passes.0.upskin_power_w": 450}), None,
     "MCH-001", Severity.ERROR, Scope.PARAMETER, "contour.passes.0.upskin_power_w"),
    ("edge power", lambda: part(**{"edge.power_w": 450}), None,
     "MCH-001", Severity.ERROR, Scope.PARAMETER, "edge.power_w"),
    ("core power", lambda: with_core(core_set(), "core.power_w", 450), None,
     "MCH-001", Severity.ERROR, Scope.PARAMETER, "core.power_w"),
    ("support power", lambda: with_support("support.power_w", 450), None,
     "MCH-001", Severity.ERROR, Scope.PARAMETER, "support.power_w"),
    ("infill speed", lambda: part(**{"hatch.infill.speed_mm_s": 8000}), None,
     "MCH-002", Severity.ERROR, Scope.PARAMETER, "hatch.infill.speed_mm_s"),
    ("chess gap speed", lambda: with_chess("hatch.infill.chess.gap_speed_mm_s", 8000), None,
     "MCH-002", Severity.ERROR, Scope.PARAMETER, "hatch.infill.chess.gap_speed_mm_s"),
    ("contour pass 0 speed", lambda: part(**{"contour.passes.0.speed_mm_s": 8000}), None,
     "MCH-002", Severity.ERROR, Scope.PARAMETER, "contour.passes.0.speed_mm_s"),
    ("support speed", lambda: with_support("support.speed_mm_s", 8000), None,
     "MCH-002", Severity.ERROR, Scope.PARAMETER, "support.speed_mm_s"),
    ("layer range", lambda: part(layer_thickness_mm=0.12), None,
     "MCH-003", Severity.ERROR, Scope.PARAMETER, "layer_thickness_mm"),
    ("platform temperature", lambda: part(), ProcessSettings(platform_temperature_c=250),
     "MCH-004", Severity.ERROR, Scope.PROCESS, "platform_temperature_c"),
    ("energy low", lambda: part(**{"hatch.infill.power_w": 50}), None,
     "ENG-001", Severity.WARNING, Scope.NODE, "hatch.infill"),
    ("energy high", lambda: part(**{"hatch.infill.speed_mm_s": 200}), None,
     "ENG-002", Severity.WARNING, Scope.NODE, "hatch.infill"),
    ("core energy low", lambda: with_core(core_set(), "core.power_w", 20), None,
     "ENG-001", Severity.WARNING, Scope.NODE, "core"),
    ("skin thinner than layer", lambda: part(**{"hatch.upskin.thickness_mm": 0.02}), None,
     "SKN-001", Severity.ERROR, Scope.PARAMETER, "hatch.upskin.thickness_mm"),
    ("skin not multiple", lambda: part(**{"hatch.downskin.thickness_mm": 0.1}), None,
     "SKN-002", Severity.WARNING, Scope.PARAMETER, "hatch.downskin.thickness_mm"),
    ("positive ridge", lambda: part(**{"hatch.upskin.ridge_mm": 0.1}), None,
     "SKN-003", Severity.WARNING, Scope.PARAMETER, "hatch.upskin.ridge_mm"),
    ("infill skywriting", lambda: part(**{"hatch.infill.skywriting": False}), None,
     "SKY-001", Severity.WARNING, Scope.PARAMETER, "hatch.infill.skywriting"),
    ("downskin skywriting", lambda: part(**{"hatch.downskin.skywriting": False}), None,
     "SKY-001", Severity.WARNING, Scope.PARAMETER, "hatch.downskin.skywriting"),
    ("hatch vs stripe", lambda: part(**{"hatch.infill.stripes.stripe_width_mm": 0.5, "hatch.infill.hatch_distance_mm": 0.6}),
     None, "HAT-001", Severity.ERROR, Scope.PARAMETER, "hatch.infill.hatch_distance_mm"),
    ("stripe overlap", lambda: part(**{"hatch.infill.stripes.stripe_width_mm": 1.0, "hatch.infill.stripes.stripe_overlap_mm": 0.5}),
     None, "HAT-002", Severity.WARNING, Scope.PARAMETER, "hatch.infill.stripes.stripe_overlap_mm"),
    ("restriction 90", lambda: part(**{"hatch.infill.restriction_angle_deg": 90}), None,
     "HAT-003", Severity.ERROR, Scope.PARAMETER, "hatch.infill.restriction_angle_deg"),
    ("rotation 180", lambda: part(**{"hatch.infill.rotation_angle_deg": 180}), None,
     "HAT-004", Severity.WARNING, Scope.PARAMETER, "hatch.infill.rotation_angle_deg"),
    ("skipping without skins", skip_without_upskin, None,
     "HAT-005", Severity.WARNING, Scope.PARAMETER, "hatch.infill.skip_layers"),
    ("PR factor zero", lambda: part(**{"hatch.infill.homogenization": Homogenization.POWER_REDUCTION}), None,
     "HOM-001", Severity.INFO, Scope.PARAMETER, "hatch.infill.power_reduction_factor"),
    ("PR factor high", lambda: part(**{"hatch.infill.homogenization": Homogenization.POWER_REDUCTION,
                                       "hatch.infill.power_reduction_factor": 0.7}), None,
     "HOM-002", Severity.WARNING, Scope.PARAMETER, "hatch.infill.power_reduction_factor"),
    ("PR factor unused", lambda: part(**{"hatch.infill.power_reduction_factor": 0.2}), None,
     "HOM-003", Severity.INFO, Scope.PARAMETER, "hatch.infill.power_reduction_factor"),
    ("core without hatch", lambda: ExposureSet(name="E", exposure_types=[Core(), Contour()]), None,
     "COR-001", Severity.WARNING, Scope.NODE, "core"),
    ("duplicate pass names", dup_pass_names, None,
     "CON-001", Severity.WARNING, Scope.NODE, "contour"),
    ("edge methods off", lambda: part(**{"edge.method1_enabled": False, "edge.method2_enabled": False}), None,
     "EDG-001", Severity.WARNING, Scope.NODE, "edge"),
    ("edge factor low", lambda: part(**{"edge.edge_factor": 1.0}), None,
     "EDG-002", Severity.INFO, Scope.PARAMETER, "edge.edge_factor"),
]


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_rule_reports_code_severity_scope_and_path(case):
    _id, make, process, code, severity, scope, path = case
    issues = validate(make(), M400, process)
    hits = [i for i in issues if i.code == code and i.path == path]
    assert hits, f"expected {code} at {path!r}, got {[(i.code, i.path) for i in issues]}"
    assert hits[0].severity == severity
    assert hits[0].scope == scope


def test_every_rule_has_a_trigger_case():
    assert {c[3] for c in CASES} == EXPECTED_CODES


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_every_finding_path_resolves_for_its_scope(case):
    _id, make, process, *_ = case
    es = make()
    process = process or ProcessSettings()
    for issue in validate(es, M400, process):
        if issue.scope == Scope.SET:
            assert issue.path == "", issue
        elif issue.scope == Scope.PROCESS:
            assert not isinstance(get_path(process, issue.path), BaseModel), issue
        elif issue.scope == Scope.NODE:
            assert isinstance(get_path(es, issue.path), BaseModel), issue
        else:
            assert issue.scope == Scope.PARAMETER
            assert not isinstance(get_path(es, issue.path), BaseModel), issue


# ---------------------------------------------------------------- rule inventory


def test_rule_inventory_is_frozen():
    # Codes are identifiers: renaming or renumbering one is a breaking change and must be deliberate.
    assert set(RULES) == EXPECTED_CODES | MACHINE_CODES | CYCLE_CODES


def test_rule_codes_are_well_formed_and_carry_metadata():
    for code, rule in RULES.items():
        assert re.fullmatch(r"[A-Z]{3}-\d{3}", code)
        assert rule.code == code
        assert rule.title
        assert isinstance(rule.severity, Severity)
        assert isinstance(rule.scope, Scope)


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_findings_use_registered_codes_with_registered_severity(case):
    _id, make, process, *_ = case
    for issue in validate(make(), M400, process):
        assert issue.code in RULES
        assert issue.severity == RULES[issue.code].severity
        assert issue.scope == RULES[issue.code].scope


def test_rule_codes_are_documented():
    text = DOCS.read_text(encoding="utf-8")
    for code in RULES:
        assert f"`{code}`" in text, f"{code} missing from {DOCS.name}"


def test_code_does_not_depend_on_message_text():
    a = validate(part(**{"hatch.infill.power_w": 450}), M400)
    b = validate(part(**{"hatch.infill.power_w": 401}), M400)
    pa = [i for i in a if i.code == "MCH-001"][0]
    pb = [i for i in b if i.code == "MCH-001"][0]
    assert pa.message != pb.message
    assert (pa.code, pa.path) == (pb.code, pb.path)


# ---------------------------------------------------------------- structured context


def test_machine_limit_finding_carries_value_and_limit():
    issue = [i for i in validate(part(**{"hatch.infill.power_w": 450}), M400) if i.code == "MCH-001"][0]
    assert issue.context["value"] == 450
    assert issue.context["limit"] == 400
    assert issue.context["unit"] == "W"


def test_energy_finding_lists_the_parameters_it_depends_on():
    issue = [i for i in validate(part(**{"hatch.infill.power_w": 50}), M400) if i.code == "ENG-001"][0]
    assert issue.context["window_j_mm3"] == (30.0, 150.0)
    assert issue.context["energy_j_mm3"] == pytest.approx(50 / (960 * 0.11 * 0.04))
    assert set(issue.context["parameters"]) == {
        "hatch.infill.power_w", "hatch.infill.speed_mm_s", "hatch.infill.hatch_distance_mm", "layer_thickness_mm",
    }
    for p in issue.context["parameters"]:
        get_path(part(), p)


def test_str_format_still_shows_severity_path_and_message():
    issue = [i for i in validate(part(**{"hatch.infill.power_w": 450}), M400) if i.code == "MCH-001"][0]
    text = str(issue)
    assert text.startswith("[ERROR] ")
    assert "hatch.infill.power_w" in text and "MCH-001" in text and issue.message in text


# ---------------------------------------------------------------- reorder stability


def triggers(es: ExposureSet) -> ExposureSet:
    set_path(es, "hatch.infill.power_w", 450)
    set_path(es, "edge.power_w", 450)
    set_path(es, "contour.passes.1.speed_mm_s", 8000)
    set_path(es, "hatch.downskin.thickness_mm", 0.1)
    return es


def test_findings_do_not_change_when_exposure_types_are_reordered():
    a = triggers(part())
    b = triggers(part())
    b.exposure_types = list(reversed(b.exposure_types))
    assert a.kinds() != b.kinds()
    key = lambda issues: sorted((i.code, i.severity.value, i.scope.value, i.path) for i in issues)  # noqa: E731
    assert key(validate(a, M400)) == key(validate(b, M400))


# ---------------------------------------------------------------- boundaries (thresholds unchanged)


@pytest.mark.parametrize("power, flagged", [(400, False), (400.1, True)])
def test_power_limit_boundary(power, flagged):
    issues = validate(part(**{"hatch.infill.power_w": power}), M400)
    assert any(i.code == "MCH-001" for i in issues) is flagged


@pytest.mark.parametrize("t, flagged", [(0.02, False), (0.10, False), (0.019, True), (0.101, True)])
def test_layer_range_boundary(t, flagged):
    issues = validate(part(layer_thickness_mm=t), M400)
    assert any(i.code == "MCH-003" for i in issues) is flagged


@pytest.mark.parametrize("r, code", [(0.6, None), (0.61, "HOM-002"), (0.0, "HOM-001")])
def test_power_reduction_threshold_boundary(r, code):
    es = part(**{"hatch.infill.homogenization": Homogenization.POWER_REDUCTION,
                 "hatch.infill.power_reduction_factor": r})
    codes = {i.code for i in validate(es, M400) if i.code.startswith("HOM")}
    assert codes == ({code} if code else set())


@pytest.mark.parametrize("angle, flagged", [(89.9, False), (90, True)])
def test_restriction_boundary(angle, flagged):
    issues = validate(part(**{"hatch.infill.restriction_angle_deg": angle}), M400)
    assert any(i.code == "HAT-003" for i in issues) is flagged


@pytest.mark.parametrize("rot, flagged", [(0, True), (67, False), (180, True), (360, True)])
def test_rotation_boundary(rot, flagged):
    issues = validate(part(**{"hatch.infill.rotation_angle_deg": rot}), M400)
    assert any(i.code == "HAT-004" for i in issues) is flagged


def test_skin_thickness_equal_to_layer_is_accepted():
    issues = validate(part(**{"hatch.upskin.thickness_mm": 0.04}), M400)
    assert not [i for i in issues if i.code in ("SKN-001", "SKN-002") and i.path == "hatch.upskin.thickness_mm"]


def test_disabled_exposures_are_not_checked():
    es = part(**{"edge.power_w": 450, "contour.passes.0.speed_mm_s": 8000})
    es.get("edge").enabled = False
    es.get("contour").passes[0].enabled = False
    assert not [i for i in validate(es, M400) if i.code in ("MCH-001", "MCH-002")]


def test_shipped_sets_findings_unchanged():
    # Same rules and thresholds as 0.1.0: the shipped sets give exactly these findings against the
    # profile they name ("Generic 400 W" = MachineProfile()). Since B3-A the profile must be passed
    # explicitly; validate(es) without one reports MRF-004 instead of assuming a default.
    found = {es.name: sorted((i.code, i.path) for i in validate(es, MachineProfile())) for es in default_sets()}
    assert found == {
        "Default_Part_40um": [],
        "Default_Part_Core_40um": [],
        "Default_Support_40um": [],
    }


# ---------------------------------------------------------------- no silent rule changes

BASELINE = Path(__file__).resolve().parent / "data" / "validation_baseline_0_1_0.json"


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_severity_and_messages_match_0_1_0(case):
    # tests/data/validation_baseline_0_1_0.json was captured from the 0.1.0 validator before the B2
    # change: same findings, same severities, same wording, so no rule or threshold changed.
    import json

    _id, make, process, *_ = case
    expected = json.loads(BASELINE.read_text(encoding="utf-8"))[_id]
    # Rules added after 0.1.0 (CYCLE_CODES: ENG-003 for infill skipping) are excluded here; they
    # add findings without changing any 0.1.0 finding, and have their own tests.
    got = sorted([i.severity.value, i.message] for i in validate(make(), M400, process)
                 if i.code not in CYCLE_CODES)
    assert got == expected
