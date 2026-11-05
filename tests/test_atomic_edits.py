"""Rejected edits must leave the parameter set exactly as it was (audit D-08)."""

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from lpbfstudio.cli import main
from lpbfstudio.defaults import default_sets
from lpbfstudio.library import Library
from lpbfstudio.models import SetCategory
from lpbfstudio.paths import set_path, with_assignments


def part():
    return default_sets()[0].model_copy(deep=True)


def dump(es):
    return es.model_dump(mode="json")


# ---------------------------------------------------------------- single assignments


def test_rejected_set_level_change_leaves_set_unchanged():
    # The ExposureSet model validator rejects support category for a part set; 0.1.0 kept the value.
    es = part()
    before = dump(es)
    with pytest.raises(ValidationError):
        set_path(es, "category", "support")
    assert dump(es) == before
    assert es.category == SetCategory.PART


@pytest.mark.parametrize("path, value", [
    ("hatch.infill.power_w", "-5"),                    # below field minimum
    ("hatch.infill.power_w", "2000.1"),                # above field maximum
    ("hatch.infill.stripes.stripe_width_mm", "0.1"),  # nested two levels
    ("contour.passes.1.speed_mm_s", "0"),             # list element
    ("edge.min_radius_factor", "1.5"),
    ("layer_thickness_mm", "0.5"),
])
def test_rejected_value_leaves_set_unchanged(path, value):
    es = part()
    before = dump(es)
    with pytest.raises(ValidationError):
        set_path(es, path, value)
    assert dump(es) == before


@pytest.mark.parametrize("path, value", [
    ("hatch.infill.pattern", "zigzag"),   # unknown enum value
    ("hatch.infill.skywriting", "maybe"),  # not a boolean
    ("hatch.infill.skip_layers", "1.5"),   # not an integer
    ("name", "Other"),                     # protected since B1
])
def test_unparseable_value_leaves_set_unchanged(path, value):
    es = part()
    before = dump(es)
    with pytest.raises(ValueError):
        set_path(es, path, value)
    assert dump(es) == before


def test_boundary_values_are_accepted():
    es = part()
    set_path(es, "hatch.infill.power_w", "2000")
    set_path(es, "hatch.infill.power_w", "0")
    set_path(es, "edge.min_radius_factor", "1")
    assert es.get("edge").min_radius_factor == 1.0


# ---------------------------------------------------------------- multi-parameter updates


def test_with_assignments_applies_all_and_leaves_input_untouched():
    es = part()
    before = dump(es)
    new = with_assignments(es, [("hatch.infill.power_w", "300"), ("contour.passes.0.power_w", "160")])
    assert dump(es) == before
    assert new.get("hatch").infill.power_w == 300
    assert new.get("contour").passes[0].power_w == 160


@pytest.mark.parametrize("bad", [
    ("hatch.infill.speed_mm_s", "-1"),
    ("category", "support"),
    ("no.such.path", "1"),
    ("hatch.infill.pattern", "zigzag"),
])
def test_with_assignments_is_all_or_nothing(bad):
    es = part()
    before = dump(es)
    with pytest.raises((ValueError, KeyError)):
        with_assignments(es, [("hatch.infill.power_w", "300"), bad, ("layer_thickness_mm", "0.03")])
    assert dump(es) == before


def test_cli_set_rejected_assignment_saves_nothing(tmp_path):
    r, base = CliRunner(), ["--library", str(tmp_path / "lib")]
    r.invoke(main, base + ["copy", "Default_Part_40um", "A"])
    lib = Library(tmp_path / "lib", seed=False)
    before = dump(lib.get("A"))
    for args in (["hatch.infill.power_w=300", "hatch.infill.speed_mm_s=-1"],
                 ["hatch.infill.power_w=300", "category=support"]):
        res = r.invoke(main, base + ["set", "A", *args])
        assert res.exit_code == 1
        assert dump(lib.get("A")) == before


def test_cli_set_multiple_assignments_still_work(tmp_path):
    r, base = CliRunner(), ["--library", str(tmp_path / "lib")]
    r.invoke(main, base + ["copy", "Default_Part_40um", "A"])
    res = r.invoke(main, base + ["set", "A", "hatch.infill.power_w=300", "layer_thickness_mm=0.03"])
    assert res.exit_code == 0, res.output
    es = Library(tmp_path / "lib", seed=False).get("A")
    assert (es.get("hatch").infill.power_w, es.layer_thickness_mm) == (300, 0.03)
