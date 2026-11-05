"""Machine-profile references and consistency (audit D-11, D-14; Milestone B3-A).

A set is validated against the profile it names in its ``machine`` field. A missing, unknown or
invalid reference is reported with its own rule code and the machine-dependent checks are not run;
there is never a fallback to another profile.
"""

import json
import math

import pytest
from click.testing import CliRunner
from pydantic import BaseModel

from lpbfstudio.cli import main
from lpbfstudio.defaults import default_sets
from lpbfstudio.library import Library, LibraryError
from lpbfstudio.models import MachineProfile, ProcessSettings
from lpbfstudio.paths import get_path, set_path
from lpbfstudio.validation import RULES, Scope, Severity, check_machine, validate, validate_set

MACHINE_CHECK_CODES = {"MCH-001", "MCH-002", "MCH-003", "MCH-004", "ENG-001", "ENG-002"}
GENERIC = "Generic 400 W"


def part(machine: str = GENERIC, **changes):
    es = default_sets()[0].model_copy(deep=True)
    es.locked = False
    es.machine = machine
    for path, value in changes.items():
        set_path(es, path, value)
    return es


def codes(issues):
    return [i.code for i in issues]


def profiles(*mps):
    return {mp.name: mp for mp in mps}


LOW = MachineProfile(name="Low 200 W", max_power_w=200)


# ---------------------------------------------------------------- profile consistency (MPR)


@pytest.mark.parametrize("kwargs, code, path", [
    ({"name": "  "}, "MPR-001", "name"),
    ({"name": ""}, "MPR-001", "name"),
    ({"min_layer_mm": 0.1, "max_layer_mm": 0.02}, "MPR-002", "min_layer_mm"),
    ({"energy_window_j_mm3": (150, 30)}, "MPR-003", "energy_window_j_mm3"),
    ({"energy_window_j_mm3": (60, 60)}, "MPR-003", "energy_window_j_mm3"),
    ({"energy_window_j_mm3": (-1, 60)}, "MPR-003", "energy_window_j_mm3"),
    ({"energy_window_j_mm3": (30, math.inf)}, "MPR-003", "energy_window_j_mm3"),
    ({"energy_window_j_mm3": (math.nan, 60)}, "MPR-003", "energy_window_j_mm3"),
])
def test_inconsistent_profile_is_reported(kwargs, code, path):
    mp = MachineProfile(**kwargs)
    issues = check_machine(mp)
    hit = [i for i in issues if i.code == code]
    assert hit, issues
    assert hit[0].severity == Severity.ERROR
    assert hit[0].scope == Scope.MACHINE
    assert hit[0].path == path
    assert not isinstance(get_path(mp, path), BaseModel)


@pytest.mark.parametrize("kwargs", [
    {},
    {"min_layer_mm": 0.04, "max_layer_mm": 0.04},   # single allowed layer thickness is consistent
    {"energy_window_j_mm3": (0, 150)},               # zero lower bound is allowed
    {"energy_window_j_mm3": (59.9, 60)},
])
def test_consistent_profile_has_no_findings(kwargs):
    assert check_machine(MachineProfile(**kwargs)) == []


def test_profile_finding_context_names_the_values():
    issue = check_machine(MachineProfile(min_layer_mm=0.1, max_layer_mm=0.02))[0]
    assert issue.context["value"] == 0.1
    assert issue.context["limit"] == 0.02
    assert issue.context["parameters"] == ["max_layer_mm"]


# ---------------------------------------------------------------- references (MRF)


def test_set_is_validated_against_the_profile_it_names():
    es = part("Low 200 W")  # 285 W infill
    issues = validate_set(es, profiles(MachineProfile(), LOW))
    assert ("MCH-001", "hatch.infill.power_w") in [(i.code, i.path) for i in issues]
    assert codes(validate_set(part(GENERIC), profiles(MachineProfile(), LOW))) == []


def test_finding_context_names_the_profile_used():
    issue = [i for i in validate_set(part("Low 200 W"), profiles(LOW)) if i.code == "MCH-001"][0]
    assert issue.context["machine"] == "Low 200 W"


@pytest.mark.parametrize("ref", ["", "   "])
def test_blank_reference_is_reported_and_no_machine_checks_run(ref):
    es = part(ref, **{"hatch.infill.power_w": 1999})
    issues = validate_set(es, profiles(MachineProfile()))
    mrf = [i for i in issues if i.code == "MRF-001"]
    assert len(mrf) == 1 and mrf[0].path == "machine" and mrf[0].scope == Scope.PARAMETER
    assert mrf[0].severity == Severity.WARNING
    assert not MACHINE_CHECK_CODES & set(codes(issues))


def test_unknown_reference_is_an_error_without_fallback():
    es = part("Unknown machine", **{"hatch.infill.power_w": 1999})
    issues = validate_set(es, profiles(MachineProfile()))
    mrf = [i for i in issues if i.code == "MRF-002"]
    assert len(mrf) == 1 and mrf[0].severity == Severity.ERROR and mrf[0].path == "machine"
    assert mrf[0].context["available"] == [GENERIC]
    assert not MACHINE_CHECK_CODES & set(codes(issues))


def test_reference_match_is_exact_and_suggests_case_variant():
    issues = validate_set(part("generic 400 w"), profiles(MachineProfile()))
    mrf = [i for i in issues if i.code == "MRF-002"][0]
    assert mrf.context["did_you_mean"] == GENERIC


def test_invalid_named_profile_is_an_error_and_machine_checks_are_skipped():
    bad = MachineProfile(min_layer_mm=0.1, max_layer_mm=0.02)
    issues = validate_set(part(GENERIC, **{"hatch.infill.power_w": 1999}), profiles(bad))
    found = codes(issues)
    assert "MRF-003" in found and "MPR-002" in found
    assert not MACHINE_CHECK_CODES & set(found)
    mpr = [i for i in issues if i.code == "MPR-002"][0]
    assert mpr.scope == Scope.MACHINE and mpr.context["machine"] == GENERIC


def test_reference_to_unreadable_profile_lists_the_files():
    issues = validate_set(part("Other"), {}, unreadable=["machines/broken.json"])
    mrf = [i for i in issues if i.code == "MRF-002"][0]
    assert mrf.context["unreadable"] == ["machines/broken.json"]


def test_validate_without_profile_reports_it_and_runs_no_machine_checks():
    issues = validate(part(**{"hatch.infill.power_w": 1999, "layer_thickness_mm": 0.2}))
    assert "MRF-004" in codes(issues)
    assert [i for i in issues if i.code == "MRF-004"][0].scope == Scope.SET
    assert not MACHINE_CHECK_CODES & set(codes(issues))


def test_explicit_profile_argument_still_works():
    issues = validate(part(**{"hatch.infill.power_w": 450}), MachineProfile(max_power_w=400))
    assert "MCH-001" in codes(issues) and "MRF-004" not in codes(issues)


def test_platform_temperature_checked_against_named_profile():
    issues = validate_set(part("Low 200 W"), profiles(LOW), ProcessSettings(platform_temperature_c=250))
    assert "MCH-004" in codes(issues)


def test_new_codes_are_registered():
    for code, sev, scope in [("MPR-001", "error", "machine"), ("MPR-002", "error", "machine"),
                             ("MPR-003", "error", "machine"), ("MRF-001", "warning", "parameter"),
                             ("MRF-002", "error", "parameter"), ("MRF-003", "error", "parameter"),
                             ("MRF-004", "warning", "set")]:
        assert RULES[code].severity.value == sev and RULES[code].scope.value == scope


# ---------------------------------------------------------------- library storage


@pytest.fixture
def lib(tmp_path):
    return Library(tmp_path / "lib")


def test_fresh_library_has_the_generic_profile(lib):
    assert list(lib.load_machines()) == [GENERIC]
    assert lib.load_machines()[GENERIC] == MachineProfile()
    assert (lib.root / "machines").is_dir()


def test_shipped_sets_validate_clean_in_a_fresh_library(lib):
    for name in ("Default_Part_40um", "Default_Part_Core_40um", "Default_Support_40um"):
        assert lib.validate(lib.get(name)) == []


def test_save_and_load_several_profiles(lib):
    lib.save_machine(LOW)
    assert set(lib.load_machines()) == {GENERIC, "Low 200 W"}
    assert lib.get_machine("Low 200 W").max_power_w == 200
    with pytest.raises(LibraryError):
        lib.get_machine("nope")


def test_profile_name_collision_is_refused(lib):
    lib.save_machine(MachineProfile(name="My M"))
    with pytest.raises(LibraryError, match="My M"):
        lib.save_machine(MachineProfile(name="My_M"))


def test_corrupt_profile_file_is_reported_not_fatal(lib):
    bad = lib.root / "machines" / "broken.json"
    bad.write_text("{", encoding="utf-8")
    assert GENERIC in lib.load_machines()
    assert [p.name for p, _ in lib.machine_errors] == ["broken.json"]
    assert bad.read_text(encoding="utf-8") == "{"


def test_import_machine_profile(lib, tmp_path):
    f = tmp_path / "m.json"
    f.write_text(LOW.model_dump_json(), encoding="utf-8")
    assert lib.import_machine(f).name == "Low 200 W"
    with pytest.raises(LibraryError, match="already exists"):
        lib.import_machine(f)
    lib.import_machine(f, overwrite=True)


# ---------------------------------------------------------------- legacy (0.1.0) libraries


def legacy_library(tmp_path, machine: dict | None):
    """A 0.1.0 library: sets/ plus an optional single machine.json, no machines/ folder."""
    root = tmp_path / "legacy"
    (root / "sets").mkdir(parents=True)
    for es in default_sets():
        (root / "sets" / f"{es.name}.json").write_text(es.model_dump_json(indent=2), encoding="utf-8")
    if machine is not None:
        (root / "machine.json").write_text(json.dumps(machine), encoding="utf-8")
    return root


def files(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_legacy_library_without_machine_json_keeps_0_1_0_results(tmp_path):
    root = legacy_library(tmp_path, None)
    before = files(root)
    lib = Library(root)
    for name, data in before.items():
        assert files(root)[name] == data  # existing files untouched
    for es in default_sets():
        assert lib.validate(lib.get(es.name)) == validate(es, MachineProfile())  # 0.1.0 used MachineProfile()


def test_legacy_machine_json_is_used_by_name_and_left_in_place(tmp_path):
    custom = MachineProfile(max_power_w=250).model_dump(mode="json")  # name "Generic 400 W"
    root = legacy_library(tmp_path, custom)
    before = (root / "machine.json").read_bytes()
    lib = Library(root)
    assert not (root / "machines").exists()  # no generic profile seeded next to the user's one
    issues = lib.validate(lib.get("Default_Part_40um"))
    assert ("MCH-001", "hatch.infill.power_w") in [(i.code, i.path) for i in issues]
    assert (root / "machine.json").read_bytes() == before


def test_legacy_profile_with_another_name_is_not_used_silently(tmp_path):
    root = legacy_library(tmp_path, MachineProfile(name="My M290").model_dump(mode="json"))
    lib = Library(root)
    issues = lib.validate(lib.get("Default_Part_40um"))
    mrf = [i for i in issues if i.code == "MRF-002"]
    assert mrf and mrf[0].context["available"] == ["My M290"]


def test_legacy_inconsistent_machine_json_still_loads(tmp_path):
    bad = MachineProfile(min_layer_mm=0.1, max_layer_mm=0.02).model_dump(mode="json")
    root = legacy_library(tmp_path, bad)
    lib = Library(root)
    found = codes(lib.validate(lib.get("Default_Part_40um")))
    assert "MRF-003" in found and "MPR-002" in found


def test_saving_a_legacy_profile_writes_back_to_machine_json(tmp_path):
    root = legacy_library(tmp_path, MachineProfile().model_dump(mode="json"))
    lib = Library(root)
    lib.save_machine(MachineProfile(max_power_w=350))
    assert json.loads((root / "machine.json").read_text(encoding="utf-8"))["max_power_w"] == 350
    assert not (root / "machines").exists()


def test_folder_profile_shadows_legacy_profile_of_same_name_and_says_so(tmp_path):
    root = legacy_library(tmp_path, MachineProfile(max_power_w=250).model_dump(mode="json"))
    (root / "machines").mkdir()
    (root / "machines" / "Generic_400_W.json").write_text(MachineProfile().model_dump_json(), encoding="utf-8")
    lib = Library(root)
    assert lib.load_machines()[GENERIC].max_power_w == 400
    assert [p.name for p, _ in lib.machine_errors] == ["machine.json"]


# ---------------------------------------------------------------- CLI


def run(tmp_path, *args):
    return CliRunner().invoke(main, ["--library", str(tmp_path / "lib"), *args])


def test_cli_validate_uses_the_named_profile(tmp_path):
    lib = Library(tmp_path / "lib")
    lib.save_machine(LOW)
    run(tmp_path, "copy", "Default_Part_40um", "A")
    assert run(tmp_path, "validate", "A").exit_code == 0
    assert run(tmp_path, "set", "A", "machine=Low 200 W").exit_code == 0
    res = run(tmp_path, "validate", "A")
    assert res.exit_code == 1 and "MCH-001" in res.output


def test_cli_validate_unknown_profile_fails(tmp_path):
    run(tmp_path, "copy", "Default_Part_40um", "A")
    run(tmp_path, "set", "A", "machine=Nope")
    res = run(tmp_path, "validate", "A")
    assert res.exit_code == 1 and "MRF-002" in res.output


def test_cli_machine_list_check_import_export(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text(MachineProfile(name="Bad", min_layer_mm=0.1, max_layer_mm=0.02).model_dump_json(), encoding="utf-8")
    assert run(tmp_path, "machine", "import", str(f)).exit_code == 0
    res = run(tmp_path, "machine", "list")
    assert res.exit_code == 0 and GENERIC in res.output and "Bad" in res.output and "invalid" in res.output
    assert run(tmp_path, "machine", "check", GENERIC).exit_code == 0
    res = run(tmp_path, "machine", "check", "Bad")
    assert res.exit_code == 1 and "MPR-002" in res.output
    out = tmp_path / "g.json"
    assert run(tmp_path, "machine", "export", GENERIC, str(out)).exit_code == 0
    assert MachineProfile.model_validate_json(out.read_text(encoding="utf-8")) == MachineProfile()
    assert run(tmp_path, "machine", "check", "Nope").exit_code == 1
