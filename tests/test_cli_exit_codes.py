"""E-AC5: every CLI command has at least one success and one failure test, with its exact exit code."""

import pytest
from click.testing import CliRunner

from lpbfstudio.cli import main
from lpbfstudio.defaults import default_sets
from lpbfstudio.library import Library
from lpbfstudio.models import MachineProfile

LOCKED = default_sets()[0].name
GENERIC = MachineProfile().name


@pytest.fixture
def world(tmp_path):
    lib = Library(tmp_path / "lib")
    lib.copy(LOCKED, "P1")
    lib.copy(LOCKED, "P2")
    es = lib.get("P1")
    (tmp_path / "set.json").write_text(es.model_copy(update={"name": "IN"}).model_dump_json(indent=2), encoding="utf-8")
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    (tmp_path / "machine.json").write_text(MachineProfile(name="M2").model_dump_json(indent=2), encoding="utf-8")
    return tmp_path


def run(world, *args):
    return CliRunner().invoke(main, ["--library", str(world / "lib"), *[a.format(tmp=world) for a in args]])


SEGMENT = "0:1:P1"
HD = "hatch.infill.hatch_distance_mm"

SUCCESS = [
    ("list",), ("new", "N1"), ("copy", "P1", "C1"), ("rename", "P1", "R1"), ("delete", "P1"), ("show", "P1"),
    ("set", "P1", "layer_thickness_mm=0.05"), ("validate", "P1"), ("energy", "P1"), ("compare", "P1", "P2"),
    ("doe", "P1", "-f", f"{HD}=0.09,0.10"), ("estimate", "--area", "100", "--perimeter", "40", "-s", SEGMENT),
    ("export", "P1", "{tmp}/out.json"), ("import", "{tmp}/set.json"), ("machine", "list"), ("machine", "check", GENERIC),
    ("machine", "import", "{tmp}/machine.json"), ("machine", "export", GENERIC, "{tmp}/m_out.json"),
]

FAILURE = [
    (("list", "--nope"), 2), (("new", "P1"), 1), (("copy", "nope", "C1"), 1), (("rename", "nope", "R1"), 1), (("delete", "nope"), 1),
    (("show", "nope"), 1), (("set", "nope", "a=1"), 1), (("validate", "nope"), 1), (("energy", "nope"), 1), (("compare", "nope", "P2"), 1),
    (("doe", "nope", "-f", f"{HD}=0.09"), 1), (("estimate", "--area", "100", "--perimeter", "40", "-s", "bad"), 1),
    (("export", "nope", "{tmp}/out.json"), 1), (("import", "{tmp}/bad.json"), 1), (("machine", "list", "--nope"), 2),
    (("machine", "check", "nope"), 1), (("machine", "import", "{tmp}/bad.json"), 1), (("machine", "export", "nope", "{tmp}/m.json"), 1),
]


@pytest.mark.parametrize("args", SUCCESS, ids=lambda a: "-".join(a[:2]))
def test_success_exits_zero(world, args):
    res = run(world, *args)
    assert res.exit_code == 0, res.output


@pytest.mark.parametrize("args,code", FAILURE, ids=lambda a: "-".join(a[:2]) if isinstance(a, tuple) else str(a))
def test_failure_has_its_exact_exit_code(world, args, code):
    res = run(world, *args)
    assert res.exit_code == code, res.output
    if args != ("import", "{tmp}/bad.json"):  # see the expected failure below
        assert not isinstance(res.exception, Exception) or isinstance(res.exception, SystemExit), repr(res.exception)


@pytest.mark.xfail(strict=True, reason="known defect: import_cmd catches only LibraryError, so malformed JSON ends in a traceback")
def test_import_of_malformed_json_is_reported_without_a_traceback(world):
    res = run(world, "import", "{tmp}/bad.json")
    assert isinstance(res.exception, SystemExit) and "error" in res.output


@pytest.mark.parametrize("returned", [0, 3])
def test_gui_exits_with_the_applications_status(world, monkeypatch, returned):
    pytest.importorskip("PySide6")
    from lpbfstudio.gui import app

    monkeypatch.setattr(app, "run", lambda root: returned)
    assert run(world, "gui").exit_code == returned
