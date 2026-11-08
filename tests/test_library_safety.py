"""Regression tests for library overwrite, rename and file-safety defects (audit D-01..D-03, D-16, D-23..D-25).

Every test that exercises a failing operation also checks that the data on disk is unchanged.
"""

import json
import os

import pytest
from click.testing import CliRunner

from lpbfstudio import library as library_mod
from lpbfstudio.cli import main
from lpbfstudio.defaults import default_sets
from lpbfstudio.library import Library, LibraryError
from lpbfstudio.paths import set_path


@pytest.fixture
def lib(tmp_path):
    return Library(tmp_path / "lib")


def snapshot(lib: Library) -> dict[str, bytes]:
    """File name -> exact bytes for every file in the sets folder."""
    return {p.name: p.read_bytes() for p in sorted(lib.sets_dir.iterdir())}


def part(name: str):
    es = default_sets()[0].model_copy(deep=True)
    es.name = name
    es.locked = False
    return es


# ---------------------------------------------------------------- D-01: names that share a file name


def test_save_refuses_name_that_collides_with_another_set(lib):
    lib.copy("Default_Part_40um", "My Part")
    before = snapshot(lib)
    other = part("My_Part")
    other.layer_thickness_mm = 0.03
    with pytest.raises(LibraryError, match="My Part"):
        lib.save(other)
    assert snapshot(lib) == before
    assert lib.get("My Part").layer_thickness_mm == 0.04


def test_copy_refuses_name_that_collides_with_another_set(lib):
    lib.copy("Default_Part_40um", "My Part")
    before = snapshot(lib)
    with pytest.raises(LibraryError, match="My Part"):
        lib.copy("Default_Support_40um", "My_Part")
    assert snapshot(lib) == before


def test_import_refuses_name_that_collides_with_another_set(lib, tmp_path):
    lib.copy("Default_Part_40um", "My Part")
    before = snapshot(lib)
    src = tmp_path / "in.json"
    src.write_text(part("My_Part").model_dump_json(), encoding="utf-8")
    for overwrite in (False, True):
        with pytest.raises(LibraryError, match="My Part"):
            lib.import_file(src, overwrite=overwrite)
    assert snapshot(lib) == before


def test_unique_name_skips_colliding_names(lib):
    lib.copy("Default_Part_40um", "My Part")
    assert lib.unique_name("My_Part") == "My_Part_2"


# ---------------------------------------------------------------- D-02 / D-03: renaming through `set`


def test_set_path_refuses_to_change_the_set_name():
    es = part("A")
    with pytest.raises(ValueError, match="rename"):
        set_path(es, "name", "B")
    assert es.name == "A"


def test_contour_pass_name_is_still_settable():
    es = part("A")
    set_path(es, "contour.passes.0.name", "Outer")
    assert es.get("contour").passes[0].name == "Outer"


def test_cli_set_name_does_not_duplicate_the_set(tmp_path):
    r, base = CliRunner(), ["--library", str(tmp_path / "lib")]
    assert r.invoke(main, base + ["copy", "Default_Part_40um", "A"]).exit_code == 0
    lib = Library(tmp_path / "lib", seed=False)
    before = snapshot(lib)
    res = r.invoke(main, base + ["set", "A", "name=B"])
    assert res.exit_code == 1
    assert "rename" in res.output
    assert snapshot(lib) == before


def test_cli_set_name_does_not_overwrite_another_set(tmp_path):
    r, base = CliRunner(), ["--library", str(tmp_path / "lib")]
    r.invoke(main, base + ["copy", "Default_Part_40um", "A"])
    r.invoke(main, base + ["copy", "Default_Support_40um", "B"])
    lib = Library(tmp_path / "lib", seed=False)
    before = snapshot(lib)
    res = r.invoke(main, base + ["set", "A", "name=B"])
    assert res.exit_code == 1
    assert snapshot(lib) == before
    assert lib.get("B").category.value == "support"


def test_cli_set_still_changes_other_parameters(tmp_path):
    r, base = CliRunner(), ["--library", str(tmp_path / "lib")]
    r.invoke(main, base + ["copy", "Default_Part_40um", "A"])
    assert r.invoke(main, base + ["set", "A", "description=hello"]).exit_code == 0
    assert Library(tmp_path / "lib", seed=False).get("A").description == "hello"


# ---------------------------------------------------------------- D-25: rename


def test_rename_between_names_that_share_a_file(lib):
    lib.copy("Default_Part_40um", "My Part")
    lib.rename("My Part", "My_Part")
    assert "My_Part" in lib.names()
    assert "My Part" not in lib.names()
    assert sorted(p.name for p in lib.sets_dir.glob("My*")) == ["My_Part.json"]


def test_rename_onto_existing_set_is_refused_and_both_stay_intact(lib):
    lib.copy("Default_Part_40um", "A")
    lib.copy("Default_Support_40um", "B")
    before = snapshot(lib)
    with pytest.raises(LibraryError, match="already exists"):
        lib.rename("A", "B")
    assert snapshot(lib) == before


def test_rename_onto_colliding_name_of_another_set_is_refused(lib):
    lib.copy("Default_Part_40um", "A")
    lib.copy("Default_Support_40um", "My Part")
    before = snapshot(lib)
    with pytest.raises(LibraryError, match="My Part"):
        lib.rename("A", "My_Part")
    assert snapshot(lib) == before


def test_rename_keeps_the_parameters(lib):
    copy = lib.copy("Default_Part_40um", "A")
    copy.layer_thickness_mm = 0.03
    lib.save(copy)
    renamed = lib.rename("A", "B")
    assert renamed.layer_thickness_mm == 0.03
    assert lib.get("B").layer_thickness_mm == 0.03
    assert not lib.exists("A")


# ---------------------------------------------------------------- D-16: unreadable files


def test_corrupt_file_does_not_break_the_library(lib):
    bad = lib.sets_dir / "broken.json"
    bad.write_text("{not json", encoding="utf-8")
    names = lib.names()
    assert "Default_Part_40um" in names
    assert [p.name for p, _ in lib.load_errors] == ["broken.json"]
    assert bad.read_text(encoding="utf-8") == "{not json"


def test_save_refuses_to_overwrite_an_unreadable_file(lib):
    bad = lib.sets_dir / "Mine.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(LibraryError, match="cannot be read"):
        lib.save(part("Mine"))
    assert bad.read_text(encoding="utf-8") == "{not json"


def test_cli_list_reports_unreadable_files(tmp_path):
    r, base = CliRunner(), ["--library", str(tmp_path / "lib")]
    r.invoke(main, base + ["list"])
    (tmp_path / "lib" / "sets" / "broken.json").write_text("{", encoding="utf-8")
    res = r.invoke(main, base + ["list"])
    assert res.exit_code == 0
    assert "Default_Part_40um" in res.output
    assert "broken.json" in res.output


# ---------------------------------------------------------------- D-23: atomic writes


def test_failed_write_leaves_the_original_file_intact(lib, monkeypatch):
    copy = lib.copy("Default_Part_40um", "A")
    before = snapshot(lib)

    def boom(*_a, **_k):
        raise OSError("simulated failure while replacing the file")

    monkeypatch.setattr(library_mod.os, "replace", boom)
    copy.layer_thickness_mm = 0.03
    with pytest.raises((LibraryError, OSError)):
        lib.save(copy)
    assert snapshot(lib) == before  # no partial file, no left-over temp file


# ---------------------------------------------------------------- D-24: file name differs from the stored name


@pytest.fixture
def mismatched(lib):
    """X.json that holds a set named Y (for example after a manual edit)."""
    lib.copy("Default_Part_40um", "X")
    f = lib.sets_dir / "X.json"
    data = json.loads(f.read_text(encoding="utf-8"))
    data["name"] = "Y"
    f.write_text(json.dumps(data), encoding="utf-8")
    return lib


def test_set_is_found_by_its_stored_name(mismatched):
    assert "Y" in mismatched.names()
    assert mismatched.get("Y").name == "Y"
    assert mismatched.exists("Y")


def test_saving_a_mismatched_set_updates_its_own_file(mismatched):
    es = mismatched.get("Y")
    es.layer_thickness_mm = 0.03
    mismatched.save(es)
    assert not (mismatched.sets_dir / "Y.json").exists()
    assert mismatched.get("Y").layer_thickness_mm == 0.03


def test_name_of_mismatched_file_is_not_reused_for_another_set(mismatched):
    before = snapshot(mismatched)
    with pytest.raises(LibraryError, match="Y"):
        mismatched.save(part("X"))
    assert snapshot(mismatched) == before


def test_delete_removes_the_file_that_holds_the_set(mismatched):
    mismatched.delete("Y")
    assert not (mismatched.sets_dir / "X.json").exists()
    assert "Y" not in mismatched.names()


def test_two_files_with_the_same_stored_name_are_reported(lib):
    lib.copy("Default_Part_40um", "A")
    data = json.loads((lib.sets_dir / "A.json").read_text(encoding="utf-8"))
    (lib.sets_dir / "A_dup.json").write_text(json.dumps(data), encoding="utf-8")
    lib.names()
    assert any(p.name in ("A.json", "A_dup.json") for p, _ in lib.load_errors)


# ---------------------------------------------------------------- existing behaviour kept


def test_existing_overwrite_rules_unchanged(lib):
    copy = lib.copy("Default_Part_40um", "A")
    lib.save(copy)  # same set, overwrite allowed by default
    with pytest.raises(LibraryError, match="already exists"):
        lib.save(copy, overwrite=False)
    with pytest.raises(LibraryError, match="already exists"):
        lib.copy("Default_Part_40um", "A")
    locked = lib.get("Default_Part_40um")
    with pytest.raises(LibraryError, match="locked"):
        lib.save(locked)


def test_no_temp_files_left_after_normal_saves(lib):
    copy = lib.copy("Default_Part_40um", "A")
    for t in (0.03, 0.05):
        copy.layer_thickness_mm = t
        lib.save(copy)
    lib.rename("A", "B")
    assert all(p.suffix == ".json" for p in lib.sets_dir.iterdir())
    # .history is the sidecar DC-001 v0.6 section 3.2 defines; anything else with a leading dot would be a stray temporary
    assert not [p for p in lib.root.iterdir() if p.name.startswith(".") and p.name != ".history"]
    assert os.listdir(lib.sets_dir)


def test_cli_rename(tmp_path):
    r, base = CliRunner(), ["--library", str(tmp_path / "lib")]
    r.invoke(main, base + ["copy", "Default_Part_40um", "A"])
    r.invoke(main, base + ["copy", "Default_Support_40um", "B"])
    assert r.invoke(main, base + ["rename", "A", "C"]).exit_code == 0
    res = r.invoke(main, base + ["rename", "C", "B"])
    assert res.exit_code == 1 and "already exists" in res.output
    assert r.invoke(main, base + ["rename", "Default_Part_40um", "X"]).exit_code == 1
    names = Library(tmp_path / "lib", seed=False).names()
    assert "C" in names and "A" not in names and "X" not in names
