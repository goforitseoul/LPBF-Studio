"""Contour-pass identity, format 2 (B7 Phase 2, stages S1+S2).

Design: plans/b7p2_design_contour_pass_ids.md, revision 2. Format 2 adds a required, read-only
``id`` to every contour pass (serialized first). Format 1 files are read and written exactly as
before. Nothing in S2 converts a set between formats: new sets are format 1, and copy, rename, DOE
and import keep the source's format and ids. There is no upgrade command in S2.
"""

import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from lpbfstudio import calc, doe, models
from lpbfstudio.cli import main, write_csv
from lpbfstudio.defaults import default_sets, new_set
from lpbfstudio.estimate import estimate_segment
from lpbfstudio.library import Library, LibraryError, load_json
from lpbfstudio.models import Contour, ContourPass, ExposureSet, MachineProfile, ProcessSettings
from lpbfstudio.paths import assign_field, diff, flatten, get_path, numeric_paths, set_path, with_assignments
from lpbfstudio.validation import validate

A, B = "3f9c2e1a", "b7d10c44"
MISSING = object()
HEX8 = re.compile(r"^[0-9a-f]{8}$")


def v2_data(name="V2", ids=(A, B), version=2):
    """A part set (hatch, contour, edge) as raw JSON data with the given pass ids (MISSING omits one)."""
    data = json.loads(default_sets()[0].model_dump_json())
    data.update(name=name, locked=False, schema_version=version)
    for et in data["exposure_types"]:
        if et["kind"] == "contour":
            et["passes"] = [p if i is MISSING else {"id": i, **p} for i, p in zip(ids, et["passes"])]
    return data


def write(lib: Library, data: dict, fname: str | None = None) -> Path:
    f = lib.sets_dir / f"{fname or data['name']}.json"
    f.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return f


def snapshot(f: Path):
    st = f.stat()
    return f.read_bytes(), st.st_mtime_ns


def cli(lib: Library, *args):
    return CliRunner().invoke(main, ["--library", str(lib.root), *args])


def passes(es: ExposureSet):
    return es.get("contour").passes


def file_passes(f: Path):
    data = json.loads(f.read_text(encoding="utf-8"))
    return next(et for et in data["exposure_types"] if et["kind"] == "contour")["passes"]


@pytest.fixture
def lib(tmp_path):
    lib = Library(tmp_path / "lib")
    lib.copy("Default_Part_40um", "P1")  # a format 1 set
    return lib


@pytest.fixture
def v2(lib):
    """A valid format 2 file in the library, normalized to the app's own serialization."""
    f = write(lib, v2_data())
    lib.save(lib.get("V2"))
    return f


# ================================================================ model and constants

def test_supported_versions_include_2():
    assert models.SUPPORTED_SCHEMA_VERSIONS == (1, 2)


def test_id_is_the_first_field_of_a_pass():
    assert next(iter(ContourPass.model_fields)) == "id"


def test_new_pass_id_is_8_hex_and_avoids_existing():
    ids = {models.new_pass_id() for _ in range(50)}
    assert all(HEX8.match(i) for i in ids)
    taken = {"00000000"}
    assert models.new_pass_id(taken) not in taken


@pytest.mark.parametrize("good", ["a", "3f9c2e1a", "p1", "a_b-c", "0" * 32])
def test_valid_ids(good):
    assert ContourPass(id=good).id == good


@pytest.mark.parametrize("bad", ["", "A", "a.b", "@a", "a b", "-a", "a" * 33, 7, 1.5, True, [], {}], ids=repr)
def test_malformed_ids_are_refused(bad):
    with pytest.raises(ValidationError):
        ContourPass(id=bad)


def test_contour_refuses_mixed_and_duplicate_ids():
    with pytest.raises(ValidationError, match="every pass or none"):
        Contour(passes=[ContourPass(id=A), ContourPass()])
    with pytest.raises(ValidationError, match="share the id"):
        Contour(passes=[ContourPass(id=A), ContourPass(id=A)])
    assert [p.id for p in Contour(passes=[ContourPass(id=A), ContourPass(id=B)]).passes] == [A, B]


def test_format_2_requires_ids_and_format_1_forbids_them():
    with pytest.raises(ValidationError, match="format 2"):
        ExposureSet.model_validate({**json.loads(default_sets()[0].model_dump_json()), "schema_version": 2})
    with pytest.raises(ValidationError, match="format 1"):
        ExposureSet.model_validate(v2_data(version=1))
    assert ExposureSet(schema_version=2).schema_version == 2  # no contour, nothing to identify


def test_mixed_state_cannot_be_created_by_assignment():
    es = ExposureSet.model_validate(v2_data())
    contour = es.get("contour")
    with pytest.raises(ValidationError):  # edits go through assign_field (B2), which leaves the set unchanged
        assign_field(contour, "passes", [*contour.passes, ContourPass(name="Contour 3")])
    assert [p.id for p in contour.passes] == [A, B]


# ================================================================ format 1 unchanged

def test_format_1_serialization_has_no_id_key(lib):
    es = lib.get("P1")
    assert es.schema_version == 1 and all(p.id is None for p in passes(es))
    assert '"id"' not in es.model_dump_json(indent=2)
    lib.save(es)
    assert all("id" not in p for p in file_passes(lib.sets_dir / "P1.json"))


def test_format_1_outputs_keep_index_paths_and_no_id_rows(lib):
    out = cli(lib, "show", "P1").output
    assert "contour.passes.0.power_w" in out and "@" not in out and ".id " not in out
    assert not any(p.endswith(".id") for p in flatten(lib.get("P1")))


def test_list_marks_only_format_2(lib, v2):
    lines = cli(lib, "list").output.splitlines()
    v2_line = next(ln for ln in lines if " V2 " in ln)
    assert v2_line.endswith("(format 2)")
    assert [ln for ln in lines if "format" in ln] == [v2_line]


@pytest.mark.parametrize("ids", [(A, B), (A, MISSING)], ids=["ids", "partial"])
def test_format_1_or_legacy_file_with_ids_is_refused(lib, ids):
    for drop_key in (False, True):
        data = v2_data("WithIds", ids=ids, version=1)
        if drop_key:
            del data["schema_version"]
        f = write(lib, data)
        before = snapshot(f)
        assert "WithIds" not in lib.names()
        (path, reason), = lib.load_errors
        assert "has an id" in reason and "format 1" in reason
        for args in (["validate"], ["show", "WithIds"], ["copy", "P1", "WithIds"], ["set", "WithIds", "layer_thickness_mm=0.03"]):
            cli(lib, *args)
        assert snapshot(f) == before
        f.unlink()


# ================================================================ format 2 read, edit, save

def test_format_2_file_loads_with_its_ids(lib, v2):
    es = lib.get("V2")
    assert es.schema_version == 2 and [p.id for p in passes(es)] == [A, B]
    assert cli(lib, "validate", "V2").exit_code == 0


def test_format_2_round_trip_is_byte_stable_and_id_first(lib, v2):
    before = v2.read_bytes()
    lib.save(lib.get("V2"))
    assert v2.read_bytes() == before
    data = json.loads(before)
    assert next(iter(data)) == "schema_version" and data["schema_version"] == 2
    assert all(next(iter(p)) == "id" for p in file_passes(v2))
    assert [p["id"] for p in file_passes(v2)] == [A, B]


def test_load_json_reads_format_2(tmp_path):
    f = tmp_path / "x.json"
    f.write_text(json.dumps(v2_data()))
    assert [p.id for p in passes(load_json(f))] == [A, B]


@pytest.mark.parametrize("ids, fragment", [
    ((A, MISSING), "#2 'Contour 2' has no id"),
    ((MISSING, MISSING), "#1 'Contour 1' has no id"),
    ((A, None), "#2 'Contour 2' has no id"),
    ((A, "A.1"), "#2 'Contour 2': id 'A.1' is invalid"),
    ((A, 7), "#2 'Contour 2': id 7 is invalid"),
    ((A, ""), "is invalid"),
    ((A, "x" * 33), "is invalid"),
    ((A, A), "#1 and #2 share the id '3f9c2e1a'"),
], ids=["missing", "none-at-all", "null", "dotted", "number", "empty", "too-long", "duplicate"])
def test_bad_ids_in_format_2_files_are_refused_and_kept(lib, tmp_path, ids, fragment):
    f = write(lib, v2_data("Bad", ids=ids))
    before = snapshot(f)
    assert "Bad" not in lib.names()
    (path, reason), = lib.load_errors
    assert fragment in reason, reason
    with pytest.raises(LibraryError):
        lib.get("Bad")
    for args in (["list"], ["validate"], ["show", "Bad"], ["set", "Bad", "layer_thickness_mm=0.03"],
                 ["copy", "P1", "Bad"], ["rename", "Bad", "Other"], ["delete", "Bad"], ["new", "Bad"]):
        cli(lib, *args)
    assert cli(lib, "validate").exit_code == 1
    src = tmp_path / "bad_import.json"
    src.write_text(json.dumps(v2_data("Imported_Bad", ids=ids)))
    for extra in ([], ["--overwrite"]):
        res = cli(lib, "import", str(src), *extra)
        assert res.exit_code == 1 and isinstance(res.exception, SystemExit) and fragment in res.output
    assert snapshot(f) == before and not (lib.sets_dir / "Imported_Bad.json").exists()


def test_format_2_with_an_unknown_field_is_refused(lib):
    data = v2_data("Extra")
    data["exposure_types"][1]["passes"][0]["colour"] = "red"
    f = write(lib, data)
    before = snapshot(f)
    assert "Extra" not in lib.names() and snapshot(f) == before


def test_future_versions_are_still_refused_with_2_supported(lib):
    f = write(lib, v2_data("Future", version=3))
    lib.names()
    (path, reason), = lib.load_errors
    assert "version 3" in reason and "not supported" in reason and "1, 2" in reason
    assert f.exists()


# ================================================================ paths (S1)

def test_id_and_index_paths_name_the_same_field(lib, v2):
    es = lib.get("V2")
    for idx, pid in ((0, A), (1, B), (-1, B), (-2, A)):
        assert get_path(es, f"contour.passes.{idx}") is get_path(es, f"contour.passes.@{pid}")
    set_path(es, f"contour.passes.@{B}.power_w", 91)
    assert get_path(es, "contour.passes.1.power_w") == 91
    set_path(es, "contour.passes.0.speed_mm_s", 555)
    assert get_path(es, f"contour.passes.@{A}.speed_mm_s") == 555


def test_unknown_id_lists_the_passes_and_never_falls_back(lib, v2):
    es = lib.get("V2")
    with pytest.raises(KeyError) as exc:
        get_path(es, "contour.passes.@deadbeef.power_w")
    msg = str(exc.value)
    assert "unknown parameter path: contour.passes.@deadbeef.power_w" in msg
    assert f"#1 @{A} 'Contour 1'" in msg and f"#2 @{B} 'Contour 2'" in msg
    with pytest.raises(KeyError, match="format 1"):
        get_path(lib.get("P1"), "contour.passes.@deadbeef.power_w")
    with pytest.raises(KeyError, match="no contour pass has id '1'"):
        get_path(es, "contour.passes.@1.power_w")  # '@1' is an id, not position 1


def test_unknown_index_message_is_unchanged(lib):
    with pytest.raises(KeyError) as exc:
        get_path(lib.get("P1"), "contour.passes.4.power_w")
    assert str(exc.value) == "'unknown parameter path: contour.passes.4.power_w'"


def test_format_2_outputs_use_id_paths_without_id_rows(lib, v2, tmp_path):
    es = lib.get("V2")
    flat = flatten(es)
    assert f"contour.passes.@{A}.power_w" in flat and f"contour.passes.@{B}.name" in flat
    assert not any(".passes.0." in p or ".passes.1." in p or p.endswith(".id") for p in flat)
    assert f"contour.passes.@{B}.speed_mm_s" in numeric_paths(es)
    out = cli(lib, "show", "V2").output
    assert f"contour.passes.@{A}.power_w" in out and ".id " not in out
    csv_file = tmp_path / "v2.csv"
    write_csv(es, csv_file)
    text = csv_file.read_text(encoding="utf-8")
    assert f"contour.passes.@{A}.power_w" in text and f"@{A}.id," not in text


def test_reordered_passes_keep_ids_and_paths(lib, v2):
    data = json.loads(v2.read_text())
    data["exposure_types"][1]["passes"].reverse()
    v2.write_text(json.dumps(data, indent=2))
    es = lib.get("V2")
    assert [p.id for p in passes(es)] == [B, A]
    assert get_path(es, f"contour.passes.@{A}.name") == "Contour 1"
    assert get_path(es, "contour.passes.0.name") == "Contour 2"  # index = current position
    lib.save(es)
    assert [p["id"] for p in file_passes(v2)] == [B, A]


def test_flat_compare_matches_format_2_passes_by_id(lib, v2):
    a = lib.get("V2")
    b = a.model_copy(deep=True)
    b.get("contour").passes = list(reversed(b.get("contour").passes))
    assert diff(a, b) == []  # same ids, same values: reordering alone gives no pass rows
    set_path(b, f"contour.passes.@{B}.power_w", 95)
    assert diff(a, b) == [(f"contour.passes.@{B}.power_w", 80.0, 95.0)]


# ================================================================ read-only fields

def test_id_cannot_be_edited(lib, v2):
    es = lib.get("V2")
    for path in (f"contour.passes.@{A}.id", "contour.passes.0.id"):
        with pytest.raises(ValueError, match="read-only"):
            set_path(es, path, "cafebabe")
        with pytest.raises(ValueError, match="read-only"):
            with_assignments(es, [(path, "cafebabe")])
    assert [p.id for p in passes(es)] == [A, B]
    before = snapshot(v2)
    res = cli(lib, "set", "V2", f"contour.passes.@{A}.id=cafebabe")
    assert res.exit_code == 1 and "read-only" in res.output and snapshot(v2) == before


@pytest.mark.parametrize("value", [1, 2, "1", "2"], ids=repr)
def test_schema_version_cannot_be_edited(lib, v2, value):
    for name, f in (("P1", lib.sets_dir / "P1.json"), ("V2", v2)):
        es = lib.get(name)
        with pytest.raises(ValueError, match="read-only"):
            set_path(es, "schema_version", value)
        before = snapshot(f)
        res = cli(lib, "set", name, f"schema_version={value}")
        assert res.exit_code == 1 and "read-only" in res.output and snapshot(f) == before
    assert lib.get("P1").schema_version == 1 and lib.get("V2").schema_version == 2


def test_cli_set_by_id_changes_only_that_pass(lib, v2):
    before = file_passes(v2)
    res = cli(lib, "set", "V2", f"contour.passes.@{B}.power_w=90")
    assert res.exit_code == 0, res.output
    after = file_passes(v2)
    assert after[0] == before[0]
    assert after[1] == {**before[1], "power_w": 90.0}
    assert json.loads(v2.read_text())["schema_version"] == 2
    res = cli(lib, "set", "V2", "contour.passes.0.power_w=140")  # index alias on a format 2 set
    assert res.exit_code == 0 and file_passes(v2)[0]["power_w"] == 140.0 and file_passes(v2)[0]["id"] == A


def test_cli_set_unknown_id_fails_without_writing(lib, v2):
    before = snapshot(v2)
    res = cli(lib, "set", "V2", "contour.passes.@deadbeef.power_w=90")
    assert res.exit_code == 1 and "no contour pass has id" in res.output and snapshot(v2) == before


# ================================================================ formats are kept (D2, Q1)

def test_new_sets_are_format_1(lib):
    assert new_set("X").schema_version == 1
    assert cli(lib, "new", "Fresh").exit_code == 0
    assert json.loads((lib.sets_dir / "Fresh.json").read_text())["schema_version"] == 1
    assert all("id" not in p for p in file_passes(lib.sets_dir / "Fresh.json"))


def test_format_1_workflows_stay_format_1(lib, tmp_path):
    assert cli(lib, "copy", "P1", "P1_copy").exit_code == 0
    assert cli(lib, "rename", "P1_copy", "P1_renamed").exit_code == 0
    assert cli(lib, "set", "P1", "hatch.infill.power_w=300").exit_code == 0
    assert cli(lib, "doe", "P1", "-f", "contour.passes.1.power_w=70,90").exit_code == 0
    src = tmp_path / "v1.json"
    src.write_text(json.dumps({**json.loads(default_sets()[0].model_dump_json()), "name": "Imported_V1", "locked": False}))
    assert cli(lib, "import", str(src)).exit_code == 0
    for name in ("P1", "P1_renamed", "P1_DOE1", "P1_DOE2", "Imported_V1"):
        f = lib.sets_dir / f"{name}.json"
        assert json.loads(f.read_text())["schema_version"] == 1
        assert all("id" not in p for p in file_passes(f)), name


def test_format_2_copy_rename_doe_import_keep_format_and_ids(lib, v2, tmp_path):
    src_before = snapshot(v2)
    assert cli(lib, "copy", "V2", "V2_copy").exit_code == 0
    assert snapshot(v2) == src_before
    assert cli(lib, "rename", "V2_copy", "V2_renamed").exit_code == 0
    res = cli(lib, "doe", "V2", "-f", f"contour.passes.@{B}.power_w=70,90")
    assert res.exit_code == 0, res.output
    exported = tmp_path / "v2_export.json"
    assert cli(lib, "export", "V2", str(exported)).exit_code == 0
    data = json.loads(exported.read_text())
    assert data["schema_version"] == 2
    data["name"] = "V2_imported"
    exported.write_text(json.dumps(data))
    assert cli(lib, "import", str(exported)).exit_code == 0
    for name in ("V2_renamed", "V2_DOE1", "V2_DOE2", "V2_imported"):
        f = lib.sets_dir / f"{name}.json"
        assert json.loads(f.read_text())["schema_version"] == 2, name
        assert [p["id"] for p in file_passes(f)] == [A, B], name
    assert [p["power_w"] for p in (file_passes(lib.sets_dir / f"V2_DOE{i}.json")[1] for i in (1, 2))] == [70.0, 90.0]
    assert not (lib.sets_dir / "V2_copy.json").exists()


def test_import_overwrite_of_format_2_keeps_ids(lib, v2, tmp_path):
    src = tmp_path / "again.json"
    data = json.loads(v2.read_text())
    data["layer_thickness_mm"] = 0.03
    src.write_text(json.dumps(data))
    assert cli(lib, "import", str(src), "--overwrite").exit_code == 0
    es = lib.get("V2")
    assert es.layer_thickness_mm == 0.03 and [p.id for p in passes(es)] == [A, B] and es.schema_version == 2


# ================================================================ DOE (S1 normalization)

def test_doe_canonical_path_unifies_index_and_id_forms(lib, v2):
    es = lib.get("V2")
    for path in (f"contour.passes.@{B}.power_w", "contour.passes.1.power_w", "contour.passes.-1.power_w"):
        assert doe.canonical_path(es, path) == f"contour.passes.@{B}.power_w"
    assert doe.canonical_path(lib.get("P1"), "contour.passes.-1.power_w") == "contour.passes.1.power_w"
    with pytest.raises(ValueError, match="same parameter"):
        doe.generate(es, {"contour.passes.1.power_w": [70.0], f"contour.passes.@{B}.power_w": [80.0]})


def test_doe_refuses_id_as_a_factor(lib, v2):
    with pytest.raises(ValueError, match="cannot be a DOE factor"):
        doe.generate(lib.get("V2"), {f"contour.passes.@{A}.id": ["aaaaaaaa", "bbbbbbbb"]})
    res = cli(lib, "doe", "V2", "-f", f"contour.passes.@{A}.id=aaaaaaaa,bbbbbbbb")
    assert res.exit_code == 1


def test_doe_values_and_names_are_the_same_for_both_path_forms(lib, v2):
    es = lib.get("V2")
    by_id = doe.generate(es, {f"contour.passes.@{B}.power_w": [70.0, 90.0]}, "{base}_{power_w}")
    by_index = doe.generate(es, {"contour.passes.1.power_w": [70.0, 90.0]}, "{base}_{power_w}")
    assert [v.name for v in by_id] == [v.name for v in by_index] == ["V2_70", "V2_90"]
    # the description records the factor as given; everything else is identical
    assert [v.model_dump(exclude={"description"}) for v in by_id] == [v.model_dump(exclude={"description"}) for v in by_index]
    assert all([p.id for p in passes(v)] == [A, B] and v.schema_version == 2 for v in by_id)


# ================================================================ validation findings (D5)

def test_format_2_findings_use_id_paths(lib, v2):
    es = lib.get("V2")
    set_path(es, f"contour.passes.@{B}.speed_mm_s", 8000)
    found = [i for i in validate(es, MachineProfile()) if i.code == "MCH-002"]
    assert [i.path for i in found] == [f"contour.passes.@{B}.speed_mm_s"]
    assert found[0].context["pass_id"] == B and found[0].context["pass_position"] == 2
    v1 = lib.get("P1")
    set_path(v1, "contour.passes.1.speed_mm_s", 8000)
    found1 = [i for i in validate(v1, MachineProfile()) if i.code == "MCH-002"]
    assert [i.path for i in found1] == ["contour.passes.1.speed_mm_s"] and "pass_id" not in found1[0].context
    assert found[0].message == found1[0].message and found[0].severity == found1[0].severity


def test_format_2_duplicate_names_finding_lists_id_paths(lib, v2):
    es = lib.get("V2")
    set_path(es, f"contour.passes.@{B}.name", "Contour 1")
    con, = [i for i in validate(es, MachineProfile()) if i.code == "CON-001"]
    assert con.context["parameters"] == [f"contour.passes.@{A}.name", f"contour.passes.@{B}.name"]


def test_findings_are_the_same_apart_from_paths(lib, v2):
    v1, v2s = lib.get("P1"), lib.get("V2")
    v1.name = v2s.name
    strip = lambda issues: [(i.code, i.severity, i.message) for i in issues]
    assert strip(validate(v1, MachineProfile())) == strip(validate(v2s, MachineProfile()))


# ================================================================ calculations unaffected

def test_energy_offsets_and_estimate_do_not_depend_on_ids(lib, v2):
    v1, v2s = lib.get("P1"), lib.get("V2")
    assert calc.energy_summary(v1) == calc.energy_summary(v2s)
    ps = ProcessSettings()
    assert calc.effective_offsets(v1, ps) == calc.effective_offsets(v2s, ps)
    e1 = estimate_segment(v1, 100, 40, 0, 10)
    e2 = estimate_segment(v2s, 100, 40, 0, 10)
    assert e1.seconds == e2.seconds


# ================================================================ writer protection (P-4)

def test_inconsistent_set_is_never_written(lib, v2):
    es = lib.get("V2")
    bad = es.get("contour").passes[1].model_copy(update={"id": None})
    es.get("contour").__dict__["passes"] = [es.get("contour").passes[0], bad]  # bypass validation
    before = snapshot(v2)
    with pytest.raises(LibraryError, match="not saved"):
        lib.save(es)
    assert snapshot(v2) == before
    fresh = es.model_copy(deep=True)
    fresh.__dict__["name"] = "Brand_New"
    with pytest.raises(LibraryError):
        lib.save(fresh)
    assert not (lib.sets_dir / "Brand_New.json").exists()
