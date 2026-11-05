"""Read-only identity and format-version fields (B7 Phase 2 S2, audit finding G-1).

``ContourPass.id`` and ``ExposureSet.schema_version`` are read-only. Before G-1 only the path
editor (``set_path``), the GUI form and DOE refused them; direct model edits went through and were
saved. Now there are two independent layers:

* the model refuses ordinary assignment to either field (``assign_field`` included);
* the library refuses to write a set whose version or pass ids differ from the values it was read
  or created with (changes made by ``model_copy(update=...)`` or other validation bypasses), and
  refuses to save over a file of a different format.

Every refused save leaves an existing file byte-for-byte unchanged and creates no new file.
"""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from lpbfstudio import doe
from lpbfstudio.cli import main
from lpbfstudio.defaults import default_sets, new_set
from lpbfstudio.library import Library, LibraryError
from lpbfstudio.models import ContourPass, ExposureSet, SetCategory, new_pass_id
from lpbfstudio.paths import assign_field, with_assignments

A, B = "3f9c2e1a", "b7d10c44"


def part_data(name: str, version: int, ids=(A, B)) -> dict:
    """A part set as raw JSON data; format 2 gets the given pass ids, format 1 none."""
    data = json.loads(default_sets()[0].model_dump_json())
    data.update(name=name, locked=False, schema_version=version)
    if version == 2:
        for et in data["exposure_types"]:
            if et["kind"] == "contour":
                et["passes"] = [{"id": i, **p} for i, p in zip(ids, et["passes"])]
    return data


def snapshot(f: Path):
    st = f.stat()
    return f.read_bytes(), st.st_mtime_ns


def listing(lib: Library):
    return sorted(p.name for p in lib.sets_dir.iterdir())


def cli(lib: Library, *args):
    return CliRunner().invoke(main, ["--library", str(lib.root), *args])


def passes(es: ExposureSet):
    return es.get("contour").passes


@pytest.fixture
def lib(tmp_path):
    lib = Library(tmp_path / "lib")
    lib.copy("Default_Part_40um", "P1")                       # format 1 part set
    lib.save(new_set("Sup1", SetCategory.SUPPORT), overwrite=False)  # format 1, no contour
    (lib.sets_dir / "V2.json").write_text(json.dumps(part_data("V2", 2)), encoding="utf-8")
    lib.save(lib.get("V2"))                                   # normalized format 2 file
    return lib


def files(lib):
    return {n: lib.sets_dir / f"{n}.json" for n in ("P1", "Sup1", "V2")}


# ================================================================ layer 1: the model

@pytest.mark.parametrize("value", ["cafebabe", None, A], ids=repr)
def test_direct_assignment_to_a_pass_id_is_refused(lib, value):
    for name in ("V2", "P1"):
        es = lib.get(name)
        p = passes(es)[1]
        before = p.id
        with pytest.raises(ValidationError, match="frozen"):
            p.id = value
        assert p.id == before


@pytest.mark.parametrize("value", [1, 2, 3, "2"], ids=repr)
def test_direct_assignment_to_schema_version_is_refused(lib, value):
    for name in ("P1", "Sup1", "V2"):
        es = lib.get(name)
        before = es.schema_version
        with pytest.raises(ValidationError, match="frozen"):
            es.schema_version = value
        assert es.schema_version == before


def test_assign_field_cannot_change_either_field(lib):
    for name in ("P1", "Sup1", "V2"):
        es = lib.get(name)
        before = es.model_dump()
        with pytest.raises(ValidationError, match="frozen"):
            assign_field(es, "schema_version", 2 if es.schema_version == 1 else 1)
        if es.get("contour") is not None:
            with pytest.raises(ValidationError, match="frozen"):
                assign_field(passes(es)[0], "id", "cafebabe")
        assert es.model_dump() == before


def test_constructing_and_copying_still_work(lib):
    p = ContourPass(id="cafebabe", name="Contour 9")
    assert p.id == "cafebabe" and p.model_copy(deep=True).id == "cafebabe"
    es = lib.get("V2")
    clone = es.model_copy(deep=True)
    assert clone.schema_version == 2 and [p.id for p in passes(clone)] == [A, B]
    assert ExposureSet.model_validate(es.model_dump()) == es


# ================================================================ layer 2: the save boundary

def test_version_changed_by_model_copy_is_not_saved(lib):
    f = files(lib)["Sup1"]
    before = snapshot(f)
    changed = lib.get("Sup1").model_copy(update={"schema_version": 2})  # bypasses validation
    with pytest.raises(LibraryError, match="not saved.*format"):
        lib.save(changed)
    assert snapshot(f) == before and lib.get("Sup1").schema_version == 1


def test_version_changed_by_model_copy_is_not_saved_as_a_new_set(lib):
    names = listing(lib)
    changed = new_set("Sup_New", SetCategory.SUPPORT).model_copy(update={"schema_version": 2})
    with pytest.raises(LibraryError, match="not saved.*format"):
        lib.save(changed, overwrite=False)
    assert listing(lib) == names


def test_version_changed_through_dict_or_setattr_is_not_saved(lib):
    for how in ("dict", "setattr"):
        f = files(lib)["Sup1"]
        before = snapshot(f)
        es = lib.get("Sup1")
        if how == "dict":
            es.__dict__["schema_version"] = 2
        else:
            object.__setattr__(es, "schema_version", 2)
        with pytest.raises(LibraryError, match="not saved"):
            lib.save(es)
        assert snapshot(f) == before


def test_pass_id_changed_by_model_copy_is_not_saved(lib):
    f = files(lib)["V2"]
    before = snapshot(f)
    es = lib.get("V2")
    first, second = passes(es)
    es.get("contour").passes = [first.model_copy(update={"id": "cafebabe"}), second]  # ordinary list edit
    with pytest.raises(LibraryError, match="not saved.*id"):
        lib.save(es)
    assert snapshot(f) == before and [p.id for p in passes(lib.get("V2"))] == [A, B]


def test_pass_id_changed_through_dict_or_setattr_is_not_saved(lib):
    for how in ("dict", "setattr"):
        f = files(lib)["V2"]
        before = snapshot(f)
        es = lib.get("V2")
        p = passes(es)[1]
        if how == "dict":
            p.__dict__["id"] = "cafebabe"
        else:
            object.__setattr__(p, "id", "cafebabe")
        with pytest.raises(LibraryError, match="not saved.*id"):
            lib.save(es)
        assert snapshot(f) == before


def test_changed_pass_id_is_not_saved_by_copy_or_rename_either(lib):
    names = listing(lib)
    es = lib.get("V2")
    passes(es)[0].__dict__["id"] = "cafebabe"
    renamed = es.model_copy(deep=True)
    renamed.name = "V2_other"
    with pytest.raises(LibraryError, match="not saved"):
        lib.save(renamed, overwrite=False)
    assert listing(lib) == names


def test_a_format_1_file_cannot_gain_pass_ids(lib):
    f = files(lib)["P1"]
    before = snapshot(f)
    es = lib.get("P1")
    passes(es)[0].__dict__["id"] = "cafebabe"  # bypass: an id in a format 1 set
    with pytest.raises(LibraryError, match="not saved"):
        lib.save(es)
    assert snapshot(f) == before
    as_v2 = ExposureSet.model_validate(part_data("P1", 2))  # a valid format 2 set named P1
    with pytest.raises(LibraryError, match="not saved.*format 1"):
        lib.save(as_v2)
    assert snapshot(f) == before


def test_a_format_2_file_cannot_be_saved_as_format_1(lib):
    f = files(lib)["V2"]
    before = snapshot(f)
    as_v1 = ExposureSet.model_validate(part_data("V2", 1))  # a valid format 1 set named V2
    with pytest.raises(LibraryError, match="not saved.*format 2"):
        lib.save(as_v1)
    assert snapshot(f) == before


@pytest.mark.parametrize("target, version", [("V2", 1), ("P1", 2)])
def test_import_overwrite_cannot_change_a_files_format(lib, tmp_path, target, version):
    f = files(lib)[target]
    before = snapshot(f)
    src = tmp_path / "other_format.json"
    src.write_text(json.dumps(part_data(target, version)), encoding="utf-8")
    src_before = snapshot(src)
    res = cli(lib, "import", str(src), "--overwrite")
    assert res.exit_code == 1 and "not saved" in res.output, res.output
    assert snapshot(f) == before and snapshot(src) == src_before


def test_with_assignments_and_doe_do_not_launder_a_changed_id(lib):
    es = lib.get("V2")
    passes(es)[0].__dict__["id"] = "cafebabe"
    with pytest.raises(ValueError, match="id"):
        with_assignments(es, [("layer_thickness_mm", 0.03)])
    names = listing(lib)
    with pytest.raises(ValueError):
        variants = doe.generate(es, {"layer_thickness_mm": [0.03, 0.05]})
        doe.save_batch(lib, variants)
    assert listing(lib) == names


# ================================================================ legitimate edits still save

def test_format_2_edits_that_keep_ids_still_save(lib):
    f = files(lib)["V2"]
    es = lib.get("V2")
    first, second = passes(es)
    assign_field(passes(es)[0], "power_w", 155)
    es.get("contour").passes = [second, first]                    # reorder
    lib.save(es)
    assert [p.id for p in passes(lib.get("V2"))] == [B, A]
    es = lib.get("V2")
    new = ContourPass(id=new_pass_id({A, B}), name="Contour 3")    # add pass, as the GUI does
    assign_field(es.get("contour"), "passes", [*passes(es), new])
    lib.save(es)
    assert [p.id for p in passes(lib.get("V2"))] == [B, A, new.id]
    es = lib.get("V2")
    assign_field(es.get("contour"), "passes", passes(es)[1:])     # remove pass
    lib.save(es)
    saved = lib.get("V2")
    assert saved.schema_version == 2 and [p.id for p in passes(saved)] == [A, new.id]
    assert json.loads(f.read_text())["schema_version"] == 2


def test_format_1_edits_and_deep_copies_still_save(lib):
    for name in ("P1", "Sup1"):
        es = lib.get(name).model_copy(deep=True)
        assign_field(es, "layer_thickness_mm", 0.05)
        lib.save(es)
        saved = lib.get(name)
        assert saved.schema_version == 1 and saved.layer_thickness_mm == 0.05
        assert '"id"' not in files(lib)[name].read_text()


def test_copy_rename_import_and_doe_keep_format_and_ids(lib, tmp_path):
    assert cli(lib, "copy", "V2", "V2_copy").exit_code == 0
    assert cli(lib, "rename", "V2_copy", "V2_renamed").exit_code == 0
    assert cli(lib, "doe", "V2", "-f", f"contour.passes.@{A}.power_w=140,160").exit_code == 0
    src = tmp_path / "in.json"
    src.write_text(json.dumps(part_data("V2_imported", 2)), encoding="utf-8")
    assert cli(lib, "import", str(src)).exit_code == 0
    src.write_text(json.dumps({**part_data("V2", 2), "layer_thickness_mm": 0.03}), encoding="utf-8")
    assert cli(lib, "import", str(src), "--overwrite").exit_code == 0
    for name in ("V2", "V2_renamed", "V2_DOE1", "V2_DOE2", "V2_imported"):
        es = lib.get(name)
        assert es.schema_version == 2 and [p.id for p in passes(es)] == [A, B], name
    assert lib.get("V2").layer_thickness_mm == 0.03
    assert cli(lib, "copy", "P1", "P1_copy").exit_code == 0
    assert lib.get("P1_copy").schema_version == 1
