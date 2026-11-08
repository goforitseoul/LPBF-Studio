"""B6: Ctrl+C during DOE batch saves is reported from the persisted state; dry runs show levels.

Write sequence of one set (library._atomic_write): create a temporary file, write, flush, fsync,
os.replace into place. Interrupts are injected at each of these points and after the save has
returned. See docs/doe_audit.md section 8.
"""

import os
import re

import pytest
from click.testing import CliRunner

from lpbfstudio import doe
from lpbfstudio.cli import main
from lpbfstudio.defaults import default_sets
from lpbfstudio.doe import generate, save_batch
from lpbfstudio.library import Library
from lpbfstudio.models import ExposureSet
from lpbfstudio.paths import get_path

HD = "hatch.infill.hatch_distance_mm"
POWER = "hatch.infill.power_w"


def part():
    return default_sets()[0].model_copy(deep=True)


@pytest.fixture
def lib(tmp_path):
    lib = Library(tmp_path / "lib")
    lib.copy("Default_Part_40um", "P1")
    return lib


def variants(n=4):
    return generate(part(), {HD: [0.09 + i / 100 for i in range(n)]}, "P1_DOE{i}")


def in_sets(path) -> bool:
    return os.path.basename(os.path.dirname(os.fspath(path))) == "sets"


def interrupt_replace(monkeypatch, n, after: bool):
    """Raise KeyboardInterrupt at the n-th move of a set file into place, before or after it."""
    real = os.replace
    count = {"n": 0}

    def replace(src, dst, *a, **k):
        if in_sets(dst):
            count["n"] += 1
            if count["n"] == n:
                if after:
                    real(src, dst, *a, **k)
                raise KeyboardInterrupt
        return real(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", replace)


def interrupt_fsync(monkeypatch, n):
    real = os.fsync
    count = {"n": 0}

    def fsync(fd):
        count["n"] += 1
        if count["n"] == n:
            raise KeyboardInterrupt
        return real(fd)

    monkeypatch.setattr(os, "fsync", fsync)


def run_batch(lib, vs, **kw):
    """save_batch, failing the test (instead of stopping pytest) if the interrupt escapes."""
    try:
        return save_batch(lib, vs, **kw)
    except KeyboardInterrupt:
        pytest.fail("KeyboardInterrupt escaped save_batch")


def no_temp_files(lib):
    return [f for f in os.listdir(lib.sets_dir) if f.endswith(".tmp")] == []


def stored(lib, name):
    return lib.get(name) if lib.exists(name) else None


# ---------------------------------------------------------------- save_batch outcome

def test_outcome_of_a_complete_batch(lib):
    vs = variants(3)
    out = run_batch(lib, vs)
    assert out.saved == [v.name for v in vs]
    assert out.failed == [] and out.uncertain == [] and out.not_attempted == []
    assert not out.interrupted and out.complete


@pytest.mark.parametrize("n", [1, 2, 4])
def test_interrupt_just_after_a_file_is_moved_into_place_counts_it_as_saved(lib, monkeypatch, n):
    vs = variants(4)
    interrupt_replace(monkeypatch, n, after=True)
    out = run_batch(lib, vs)
    names = [v.name for v in vs]
    assert out.interrupted and not out.complete
    assert out.saved == names[:n]  # the n-th is on disk, although save() never returned
    assert out.failed == [] and out.uncertain == []
    assert out.not_attempted == names[n:]
    monkeypatch.undo()
    for v in vs[:n]:
        assert stored(lib, v.name) == v
    for v in vs[n:]:
        assert not lib.exists(v.name)
    assert no_temp_files(lib)


@pytest.mark.parametrize("n", [1, 3, 4])
def test_interrupt_just_before_the_move_counts_it_as_failed(lib, monkeypatch, n):
    vs = variants(4)
    interrupt_replace(monkeypatch, n, after=False)
    out = run_batch(lib, vs)
    names = [v.name for v in vs]
    assert out.saved == names[:n - 1]
    assert [name for name, _ in out.failed] == [names[n - 1]]
    assert "interrupted" in out.failed[0][1]
    assert out.not_attempted == names[n:]
    monkeypatch.undo()
    assert not lib.exists(names[n - 1]) and no_temp_files(lib)


def test_interrupt_while_the_temporary_file_is_written(lib, monkeypatch):
    vs = variants(3)
    # every set write now runs the routine twice, staging then canonical (DC-001 v0.6 S3 and S7): 4 is variant 2's own write
    interrupt_fsync(monkeypatch, 4)
    out = run_batch(lib, vs)
    assert out.saved == [vs[0].name]
    assert [name for name, _ in out.failed] == [vs[1].name]
    assert out.not_attempted == [vs[2].name]
    monkeypatch.undo()
    assert no_temp_files(lib)


def test_interrupt_after_save_returned_is_reported_from_disk(lib):
    vs = variants(3)
    seen = []

    def on_saved(name):
        seen.append(name)
        if len(seen) == 2:
            raise KeyboardInterrupt

    out = run_batch(lib, vs, on_saved=on_saved)
    assert out.interrupted
    assert out.saved == [vs[0].name, vs[1].name]
    assert out.not_attempted == [vs[2].name]


def test_report_is_based_on_disk_not_on_the_counter(lib, monkeypatch):
    """An earlier 'saved' variant that is no longer on disk is reported as uncertain."""
    vs = variants(3)
    real = os.replace
    count = {"n": 0}

    def replace(src, dst, *a, **k):
        if in_sets(dst):
            count["n"] += 1
            if count["n"] == 2:
                os.unlink(lib.sets_dir / f"{vs[0].name}.json")  # something removes the first set
                raise KeyboardInterrupt
        return real(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", replace)
    out = run_batch(lib, vs)
    assert out.saved == []
    assert [name for name, _ in out.uncertain] == [vs[0].name]
    assert [name for name, _ in out.failed] == [vs[1].name]


def test_unverifiable_state_is_uncertain_never_guessed(lib, monkeypatch):
    vs = variants(3)
    interrupt_replace(monkeypatch, 2, after=True)
    real_read = Library._read

    def unreadable(f):
        if f.name == f"{vs[1].name}.json":
            raise OSError(5, "Input/output error")
        return real_read(f)

    monkeypatch.setattr(Library, "_read", staticmethod(unreadable))
    out = run_batch(lib, vs)
    assert out.saved == [vs[0].name]
    assert [name for name, _ in out.uncertain] == [vs[1].name]
    assert "Input/output error" in out.uncertain[0][1]
    assert out.failed == []


def test_file_with_different_content_is_not_counted_as_saved(lib, monkeypatch):
    vs = variants(2)
    interrupt_replace(monkeypatch, 2, after=True)
    real_read = Library._read

    def other_content(f):
        es = real_read(f)
        if f.name == f"{vs[1].name}.json":
            es = es.model_copy(update={"description": "written by someone else"})
        return es

    monkeypatch.setattr(Library, "_read", staticmethod(other_content))
    out = run_batch(lib, vs)
    assert out.saved == [vs[0].name]
    assert vs[1].name not in out.saved


def test_second_interrupt_during_verification_leaves_the_rest_uncertain(lib, monkeypatch):
    vs = variants(3)
    real_replace, real_read = os.replace, Library._read
    count, reads = {"n": 0}, {"n": 0}

    def read(f):
        reads["n"] += 1
        if reads["n"] == 2:  # the user presses Ctrl+C again while the outcome is being checked
            raise KeyboardInterrupt
        return real_read(f)

    def replace(src, dst, *a, **k):
        if in_sets(dst):
            count["n"] += 1
            if count["n"] == 3:
                real_replace(src, dst, *a, **k)
                monkeypatch.setattr(Library, "_read", staticmethod(read))
                raise KeyboardInterrupt
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", replace)
    out = run_batch(lib, vs)
    assert out.interrupted
    assert out.saved == [vs[0].name]
    assert {name for name, _ in out.uncertain} == {vs[1].name, vs[2].name}
    assert all("not verified" in reason for _, reason in out.uncertain)


def test_report_text_lists_all_four_outcomes():
    out = doe.BatchSave(saved=["A"], failed=[("B", "interrupted before it was saved")],
                        uncertain=[("C", "cannot be read")], not_attempted=["D"], interrupted=True)
    text = out.report()
    assert "saving interrupted" in text
    assert "saved (1): A" in text
    assert "failed (1): B (interrupted before it was saved)" in text
    assert "uncertain (1): C (cannot be read)" in text
    assert "not attempted (1): D" in text
    assert "not transactional" in text


# ---------------------------------------------------------------- CLI

@pytest.fixture
def cli(tmp_path):
    r = CliRunner()
    base = ["--library", str(tmp_path / "lib")]
    assert r.invoke(main, base + ["copy", "Default_Part_40um", "P1"]).exit_code == 0

    def run(*args):
        return r.invoke(main, base + list(args))

    run.lib = Library(tmp_path / "lib")
    return run


def test_cli_ctrl_c_right_after_a_save_reports_it(cli, monkeypatch):
    interrupt_replace(monkeypatch, 2, after=True)
    res = cli("doe", "P1", "-f", f"{HD}=0.09,0.10,0.11")
    monkeypatch.undo()
    assert res.exit_code == 130 and isinstance(res.exception, SystemExit), res.output
    out = res.output
    assert "saving interrupted" in out
    assert "saved (2): P1_DOE1, P1_DOE2" in out
    assert "failed (0): none" in out and "uncertain (0): none" in out
    assert "not attempted (1): P1_DOE3" in out
    assert "not transactional" in out
    assert {"P1_DOE1", "P1_DOE2"} <= set(cli.lib.names()) and not cli.lib.exists("P1_DOE3")
    assert get_path(cli.lib.get("P1_DOE2"), HD) == 0.10


def test_cli_ctrl_c_before_the_move(cli, monkeypatch):
    interrupt_replace(monkeypatch, 1, after=False)
    res = cli("doe", "P1", "-f", f"{HD}=0.09,0.10")
    monkeypatch.undo()
    assert res.exit_code == 130
    assert "saved (0): none" in res.output and "failed (1): P1_DOE1" in res.output
    assert "not attempted (1): P1_DOE2" in res.output
    assert not cli.lib.exists("P1_DOE1")


def test_cli_error_report_keeps_the_b5_lines(cli, monkeypatch):
    from lpbfstudio import library as libmod

    real = libmod._atomic_write
    count = {"n": 0}

    def write(path, text):
        if path.parent.name == "sets":
            count["n"] += 1
            if count["n"] == 2:
                raise OSError(28, "No space left on device")
        return real(path, text)

    monkeypatch.setattr(libmod, "_atomic_write", write)
    res = cli("doe", "P1", "-f", f"{HD}=0.09,0.10,0.11")
    assert res.exit_code == 1
    assert "saving stopped at P1_DOE2: No space left on device" in res.output
    assert "uncertain (0): none" in res.output


# ---------------------------------------------------------------- dry-run levels

def _dry(cli, *factors):
    args = ["doe", "P1"]
    for f in factors:
        args += ["-f", f]
    res = cli(*args, "--dry-run")
    assert res.exit_code == 0, res.output
    return res.output.splitlines()


def test_dry_run_shows_parsed_levels_after_the_count(cli):
    lines = _dry(cli, f"{HD}=0.09:0.13:0.02", "layer_thickness_mm=0.03,0.04")
    assert lines[:3] == ["DOE: 3 × 2 = 6 variants",
                         f"  {HD}: 0.09, 0.11, 0.13",
                         "  layer_thickness_mm: 0.03, 0.04"]
    assert lines[3:] == [f"would create P1_DOE{i}" for i in range(1, 7)]


def test_dry_run_shows_a_misread_decimal_comma(cli):
    lines = _dry(cli, f"{POWER}=285,5")
    assert lines[:2] == ["DOE: 2 variants", f"  {POWER}: 285, 5"]


def test_dry_run_levels_of_every_type(cli):
    lines = _dry(cli, "hatch.infill.pattern=stripes,chess", "hatch.infill.skywriting=true,false",
                 "hatch.infill.skip_layers=0:2:1", "material=Alloy A; Alloy B")
    assert lines[1:5] == ["  hatch.infill.pattern: stripes, chess",
                          "  hatch.infill.skywriting: True, False",
                          "  hatch.infill.skip_layers: 0, 1, 2",
                          "  material: Alloy A, Alloy B"]


def test_dry_run_levels_are_exact(cli):
    lines = _dry(cli, f"{HD}=0.1234561,0.1234564")
    assert lines[1] == f"  {HD}: 0.1234561, 0.1234564"


def test_dry_run_levels_and_count_match_what_the_real_run_saves(cli):
    factors = [f"{HD}=0.09:0.13:0.02", f"{POWER}=250, 300", "hatch.infill.pattern=stripes,chess"]
    lines = _dry(cli, *factors)
    count = int(re.search(r"= (\d+) variants", lines[0]).group(1))
    shown = {}
    for line in lines[1:4]:
        path, _, vals = line.strip().partition(": ")
        shown[path] = vals.split(", ")
    args = ["doe", "P1"]
    for f in factors:
        args += ["-f", f]
    res = cli(*args)
    assert res.exit_code == 0, res.output
    created = [line.removeprefix("created ") for line in res.output.splitlines() if line.startswith("created ")]
    assert len(created) == count
    saved = [cli.lib.get(n) for n in created]
    for path, vals in shown.items():
        assert sorted({doe.format_level(get_path(es, path)) for es in saved}) == sorted(vals)


def test_real_run_output_is_unchanged(cli):
    res = cli("doe", "P1", "-f", f"{HD}=0.09,0.11")
    assert res.exit_code == 0
    assert res.output.splitlines() == ["DOE: 2 variants", "created P1_DOE1", "created P1_DOE2"]


def test_dry_run_levels_shown_even_when_names_are_taken(cli):
    assert cli("copy", "Default_Part_40um", "P1_DOE1").exit_code == 0
    res = cli("doe", "P1", "-f", f"{HD}=0.09,0.11", "--dry-run")
    assert res.exit_code == 1
    assert res.output.splitlines()[:2] == ["DOE: 2 variants", f"  {HD}: 0.09, 0.11"]


def test_exposure_set_equality_survives_a_save(lib):
    v = variants(1)[0]
    lib.save(v, overwrite=False)
    assert lib.get(v.name) == v and isinstance(lib.get(v.name), ExposureSet)
