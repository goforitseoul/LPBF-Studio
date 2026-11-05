"""Schema-version guard for exposure-set files (B7 phase 1).

Policy (docs/file_format.md): versions 1 and 2 are supported (format 2 adds contour-pass ids, see
tests/test_pass_ids.py). A file without a ``schema_version`` key is read as version 1 (legacy
policy); any other value is refused before the file's fields are read, and a refused file is
never modified.
"""

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from lpbfstudio.cli import main
from lpbfstudio.defaults import default_sets
from lpbfstudio.library import Library, LibraryError, load_json
from lpbfstudio import models
from lpbfstudio.models import ExposureSet
from lpbfstudio.paths import set_path

FUTURE = [3, 99, 10**6]
INVALID = [0, -1, "1", "2", "", 1.0, 2.5, True, False, None, [], {}]


@pytest.fixture
def lib(tmp_path):
    lib = Library(tmp_path / "lib")
    lib.copy("Default_Part_40um", "P1")
    return lib


def write_set(lib: Library, name: str, version, *, drop_key: bool = False) -> Path:
    """Write a set file holding ``name`` with the given raw schema_version (bypassing the model)."""
    data = json.loads(default_sets()[0].model_dump_json())
    data.update(name=name, locked=False)
    if drop_key:
        del data["schema_version"]
    else:
        data["schema_version"] = version
    f = lib.sets_dir / f"{name}.json"
    f.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return f


def snapshot(f: Path):
    st = f.stat()
    return f.read_bytes(), st.st_mtime_ns


def cli(lib: Library, *args):
    return CliRunner().invoke(main, ["--library", str(lib.root), *args])


# ---------------------------------------------------------------- policy constants

def test_supported_versions():
    assert getattr(models, "SUPPORTED_SCHEMA_VERSIONS", None) == (1, 2)
    assert ExposureSet().schema_version == 1


# ---------------------------------------------------------------- legacy (no key) and supported

def test_file_without_schema_version_is_read_as_version_1(lib):
    """Legacy policy, part 1: a file without the key is read as version 1 and is NOT modified
    by loading, listing, showing or validating it (no key is added on load)."""
    f = write_set(lib, "Legacy", None, drop_key=True)
    before = snapshot(f)
    assert "Legacy" in lib.names() and lib.load_errors == []
    es = lib.get("Legacy")
    assert es.schema_version == 1
    assert cli(lib, "validate", "Legacy").exit_code == 0
    for args in (["list"], ["validate"], ["show", "Legacy"]):
        assert cli(lib, *args).exit_code == 0
    assert snapshot(f) == before  # loading and validating never write
    assert "schema_version" not in json.loads(f.read_text())


def test_saving_a_legacy_file_writes_version_1(lib):
    """Legacy policy, part 2: only an explicit save writes the file, and the saved file gains
    ``schema_version: 1``; apart from the edited value nothing else changes."""
    f = write_set(lib, "Legacy", None, drop_key=True)
    original = json.loads(f.read_text())
    es = lib.get("Legacy")
    es.layer_thickness_mm = 0.03
    lib.save(es)
    saved = json.loads(f.read_text())
    assert saved["schema_version"] == 1
    assert {k: v for k, v in saved.items() if k != "schema_version"} == {**original, "layer_thickness_mm": 0.03}
    assert lib.get("Legacy") == es


def test_saving_a_legacy_file_unchanged_adds_only_the_key(lib):
    f = write_set(lib, "Legacy", None, drop_key=True)
    original = json.loads(f.read_text())
    lib.save(lib.get("Legacy"))  # explicit save with no edit
    assert json.loads(f.read_text()) == {"schema_version": 1, **original}


def test_version_1_file_written_by_the_app_round_trips_byte_for_byte(lib):
    """An existing version-1 file keeps its serialization: loading and saving it again without
    an edit writes the same bytes the app wrote before the guard."""
    f = lib.sets_dir / "P1.json"
    before = f.read_bytes()
    lib.save(lib.get("P1"))
    assert f.read_bytes() == before
    assert next(iter(json.loads(before))) == "schema_version"  # key stays first


def test_supported_version_loads_validates_and_saves_as_before(lib):
    f = write_set(lib, "V1", 1)
    es = lib.get("V1")
    assert cli(lib, "validate", "V1").exit_code == 0
    es.layer_thickness_mm = 0.03
    lib.save(es)
    assert f.read_text(encoding="utf-8") == es.model_dump_json(indent=2)  # same bytes as before the guard
    assert lib.get("V1") == es


def test_cli_set_and_copy_on_a_supported_file(lib):
    assert cli(lib, "set", "P1", "hatch.infill.power_w=300").exit_code == 0
    assert cli(lib, "copy", "P1", "P1_copy").exit_code == 0
    assert lib.get("P1_copy").schema_version == 1


def test_legacy_file_imports(lib, tmp_path):
    src = tmp_path / "legacy.json"
    data = json.loads(default_sets()[0].model_dump_json())
    del data["schema_version"]
    data["name"] = "Imported_Legacy"
    src.write_text(json.dumps(data))
    res = cli(lib, "import", str(src))
    assert res.exit_code == 0, res.output
    assert lib.get("Imported_Legacy").schema_version == 1


# ---------------------------------------------------------------- future versions

@pytest.mark.parametrize("version", FUTURE)
def test_future_version_is_refused_with_a_clear_reason(lib, version):
    write_set(lib, "Future", version)
    assert "Future" not in lib.names()
    (path, reason), = lib.load_errors
    assert path.name == "Future.json"
    assert f"version {version}" in reason and "not supported" in reason and "1" in reason
    with pytest.raises(LibraryError) as exc:
        lib.get("Future")
    assert f"version {version}" in str(exc.value)


@pytest.mark.parametrize("version", FUTURE)
def test_future_version_file_is_never_modified(lib, version):
    f = write_set(lib, "Future", version)
    before = snapshot(f)
    lib.names()
    with pytest.raises(LibraryError):
        lib.get("Future")
    replacement = default_sets()[0].model_copy(update={"name": "Future", "locked": False})
    with pytest.raises(LibraryError):
        lib.save(replacement)  # would otherwise "save it back"
    with pytest.raises(LibraryError):
        lib.copy("P1", "Future")
    with pytest.raises(LibraryError):
        lib.rename("P1", "Future")
    with pytest.raises(LibraryError):
        lib.rename("Future", "Other")
    assert lib.check_new_names(["Future"]) != []
    for args in (["list"], ["validate"], ["validate", "Future"], ["show", "Future"],
                 ["set", "Future", "hatch.infill.power_w=300"], ["copy", "P1", "Future"],
                 ["rename", "Future", "Renamed"]):
        cli(lib, *args)
    assert snapshot(f) == before
    assert not (lib.sets_dir / "Renamed.json").exists()


def test_future_version_import_is_refused_without_traceback(lib, tmp_path):
    src = tmp_path / "future.json"
    data = json.loads(default_sets()[0].model_dump_json())
    data.update(name="From_Future", schema_version=99)
    src.write_text(json.dumps(data))
    before_src = src.read_bytes()
    names = sorted(lib.names())
    for extra in ([], ["--overwrite"]):
        res = cli(lib, "import", str(src), *extra)
        assert res.exit_code == 1 and isinstance(res.exception, SystemExit), res.output
        assert "version 99" in res.output and "not supported" in res.output
    assert sorted(lib.names()) == names
    assert not (lib.sets_dir / "From_Future.json").exists()
    assert src.read_bytes() == before_src


def test_future_version_import_over_an_existing_set_keeps_it(lib, tmp_path):
    before = snapshot(lib.sets_dir / "P1.json")
    src = tmp_path / "p1_future.json"
    data = json.loads((lib.sets_dir / "P1.json").read_text())
    data["schema_version"] = 3
    src.write_text(json.dumps(data))
    with pytest.raises(LibraryError):
        lib.import_file(src, overwrite=True)
    assert snapshot(lib.sets_dir / "P1.json") == before


def test_load_json_refuses_future_versions(tmp_path):
    src = tmp_path / "x.json"
    data = json.loads(default_sets()[0].model_dump_json())
    data["schema_version"] = 99
    src.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="version 99"):
        load_json(src)


# ---------------------------------------------------------------- invalid values

@pytest.mark.parametrize("value", INVALID, ids=repr)
def test_invalid_schema_version_is_refused_and_file_kept(lib, value):
    f = write_set(lib, "Bad", value)
    before = snapshot(f)
    assert "Bad" not in lib.names()
    (path, reason), = lib.load_errors
    assert "schema_version" in reason and "invalid" in reason
    with pytest.raises(LibraryError):
        lib.get("Bad")
    cli(lib, "validate")
    cli(lib, "copy", "P1", "Bad")
    assert snapshot(f) == before


@pytest.mark.parametrize("value", [3, 99, 0, "1"], ids=repr)
def test_model_refuses_unsupported_versions(value):
    with pytest.raises(ValidationError):
        ExposureSet(schema_version=value)


def test_schema_version_cannot_be_set_to_an_unsupported_value(lib):
    f = lib.sets_dir / "P1.json"
    before = f.read_bytes()
    res = cli(lib, "set", "P1", "schema_version=99")
    assert res.exit_code == 1
    assert f.read_bytes() == before
    es = lib.get("P1")
    with pytest.raises((ValueError, ValidationError)):
        set_path(es, "schema_version", 2)
    assert es.schema_version == 1


# ---------------------------------------------------------------- exit codes and messages

def test_validate_all_fails_when_a_file_is_refused(lib):
    write_set(lib, "Future", 99)
    res = cli(lib, "validate")
    assert res.exit_code == 1
    assert "Future.json" in res.output and "version 99" in res.output and "not supported" in res.output


def test_validate_all_still_passes_when_every_file_loads(lib):
    write_set(lib, "Legacy", None, drop_key=True)
    assert cli(lib, "validate").exit_code == 0


def test_validate_named_refused_file_fails(lib):
    write_set(lib, "Future", 99)
    res = cli(lib, "validate", "Future")
    assert res.exit_code == 1 and "version 99" in res.output


def test_list_names_the_reason(lib):
    write_set(lib, "Future", 3)
    res = cli(lib, "list")
    assert "Future.json" in res.output and "version 3" in res.output


def test_os_error_text_is_unaffected(lib):
    # corrupt JSON is still reported as unreadable, not as a version problem
    f = lib.sets_dir / "Broken.json"
    f.write_text("{not json")
    lib.names()
    (path, reason), = lib.load_errors
    assert path == f and "version" not in reason
    assert os.path.exists(f)
