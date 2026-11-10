"""Milestone D and E acceptance tests of existing behaviour (test-only): E-AC1 reproducible DOE output, D-AC7 JSON round trip
of values and schema version, and the estimate's "comparison estimate" label from E-AC4. Nothing here adds behaviour."""

import json

import pytest
from click.testing import CliRunner

from lpbfstudio.cli import main
from lpbfstudio.defaults import default_sets
from lpbfstudio.doe import generate
from lpbfstudio.library import Library, export_json, load_json

HD = "hatch.infill.hatch_distance_mm"
POWER = "hatch.infill.power_w"


def base():
    es = default_sets()[0].model_copy(deep=True)
    es.name, es.locked = "B", False
    return es


def test_e_ac1_same_inputs_give_identical_names_order_and_values():
    factors = {HD: [0.09, 0.1, 0.11], POWER: [250.0, 300.0]}
    first = generate(base(), factors, "{base}_{i}")
    again = generate(base(), dict(factors), "{base}_{i}")
    assert [v.name for v in first] == [v.name for v in again]
    assert [v.model_dump() for v in first] == [v.model_dump() for v in again]
    assert [v.model_dump_json() for v in first] == [v.model_dump_json() for v in again]


def test_e_ac1_the_first_factor_varies_slowest():
    out = generate(base(), {HD: [0.09, 0.1], POWER: [250.0, 300.0]}, "{base}_{i}")
    assert [(v.get("hatch").infill.hatch_distance_mm, v.get("hatch").infill.power_w) for v in out] == [
        (0.09, 250.0), (0.09, 300.0), (0.1, 250.0), (0.1, 300.0)]


@pytest.mark.parametrize("version", [1, 2])
def test_d_ac7_json_round_trip_keeps_values_and_schema_version(tmp_path, version):
    lib = Library(tmp_path / "lib")
    es = base()
    if version == 2:
        data = json.loads(es.model_dump_json())
        data["schema_version"] = 2
        for et in data["exposure_types"]:
            for i, p in enumerate(et.get("passes", []) if et.get("kind") == "contour" else [], start=1):
                p["id"] = f"pass-{i}"
        src = tmp_path / "v2.json"
        src.write_text(json.dumps(data, indent=2), encoding="utf-8")
        es = load_json(src)
    out = tmp_path / "exported.json"
    export_json(es, out)
    back = load_json(out)
    assert back.schema_version == version and back.model_dump() == es.model_dump()
    imported = lib.import_file(out)
    assert imported.schema_version == version and lib.get("B").model_dump() == es.model_dump()


def test_d_ac7_round_trip_through_the_cli(tmp_path):
    lib = Library(tmp_path / "lib")
    lib.save(base())
    run = lambda *a: CliRunner().invoke(main, ["--library", str(tmp_path / "lib"), *a])  # noqa: E731
    out = tmp_path / "b.json"
    assert run("export", "B", str(out)).exit_code == 0
    other = Library(tmp_path / "lib2")
    assert other.import_file(out).model_dump() == lib.get("B").model_dump()


def test_e_ac4_the_estimate_is_labelled_a_comparison_estimate(tmp_path):
    lib = Library(tmp_path / "lib")
    lib.save(base())
    res = CliRunner().invoke(main, ["--library", str(tmp_path / "lib"), "estimate", "--area", "100", "--perimeter", "40",
                                    "-s", "0:1:B"])
    assert res.exit_code == 0 and "comparison estimate; jumps and delays not modelled" in res.output
