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
    assert not isinstance(res.exception, Exception) or isinstance(res.exception, SystemExit), repr(res.exception)


# D1 (fixed 2026-11-10): malformed JSON used to end in a pydantic traceback. These run in-process and as a real subprocess,
# which is what a user sees.


def test_import_of_malformed_json_is_reported_without_a_traceback(world):
    res = run(world, "import", "{tmp}/bad.json")
    assert isinstance(res.exception, SystemExit) and res.exit_code == 1
    assert res.output.strip().splitlines() == [res.output.strip().splitlines()[0]]  # one line
    assert res.output.startswith("error: bad.json was not imported: ")
    assert "Traceback" not in res.output and "validation error" not in res.output


def test_import_of_a_valid_json_file_with_invalid_values_is_reported_cleanly(world):
    import json

    data = json.loads((world / "set.json").read_text(encoding="utf-8"))
    data["layer_thickness_mm"] = "thick"
    (world / "badvalue.json").write_text(json.dumps(data), encoding="utf-8")
    res = run(world, "import", "{tmp}/badvalue.json")
    assert res.exit_code == 1 and isinstance(res.exception, SystemExit)
    assert res.output.startswith("error: badvalue.json was not imported: ") and "Traceback" not in res.output
    assert not (world / "lib/sets/IN.json").exists()


def test_import_of_malformed_json_in_a_real_process(world):
    import os
    import subprocess
    import sys

    import lpbfstudio

    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(os.path.dirname(os.path.dirname(lpbfstudio.__file__))),
                                                       os.environ.get("PYTHONPATH", "")]))
    p = subprocess.run([sys.executable, "-m", "lpbfstudio", "--library", str(world / "lib"), "import", str(world / "bad.json")],
                       capture_output=True, text=True, env=env, timeout=60)
    assert p.returncode == 1 and "Traceback" not in p.stderr and p.stdout == ""
    assert p.stderr.startswith("error: bad.json was not imported: ")


def test_valid_import_is_unchanged(world):
    res = run(world, "import", "{tmp}/set.json")
    assert res.exit_code == 0 and res.output == "imported IN\n"


def test_an_unexpected_error_during_import_still_shows_its_traceback(world, monkeypatch):
    from lpbfstudio import library

    def broken(self, path, overwrite=False):
        raise RuntimeError("a programming error")

    monkeypatch.setattr(library.Library, "import_file", broken)
    res = run(world, "import", "{tmp}/set.json")
    assert isinstance(res.exception, RuntimeError)  # not swallowed as a user error


@pytest.mark.parametrize("returned", [0, 3])
def test_gui_exits_with_the_applications_status(world, monkeypatch, returned):
    pytest.importorskip("PySide6")
    from lpbfstudio.gui import app

    monkeypatch.setattr(app, "run", lambda root: returned)
    assert run(world, "gui").exit_code == returned
