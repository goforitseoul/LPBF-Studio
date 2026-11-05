"""B5: DOE save-failure reporting, file-name preflight, exact names, decimal-comma guard.

Item numbers refer to the approved B5 scope; see docs/doe_audit.md section 7.
"""

import errno
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from lpbfstudio import library as libmod
from lpbfstudio.cli import main
from lpbfstudio.defaults import default_sets
from lpbfstudio.doe import generate, parse_values, render_name
from lpbfstudio.library import Library
from lpbfstudio.paths import get_path

HD = "hatch.infill.hatch_distance_mm"
SPEED = "hatch.infill.speed_mm_s"
SKIP = "hatch.infill.skip_layers"


def part():
    return default_sets()[0].model_copy(deep=True)


@pytest.fixture
def cli(tmp_path):
    r = CliRunner()
    base = ["--library", str(tmp_path / "lib")]
    assert r.invoke(main, base + ["copy", "Default_Part_40um", "P1"]).exit_code == 0

    def run(*args):
        return r.invoke(main, base + list(args))

    run.lib = Library(tmp_path / "lib")
    return run


def fail_on_write(monkeypatch, n, exc):
    """Make the n-th set-file write raise ``exc`` (machine profiles etc. are not counted)."""
    real = libmod._atomic_write
    count = {"n": 0}

    def write(path, text):
        if path.parent.name == "sets":
            count["n"] += 1
            if count["n"] == n:
                raise exc
        return real(path, text)

    monkeypatch.setattr(libmod, "_atomic_write", write)


# ---------------------------------------------------------------- item 3: exact names

@pytest.mark.parametrize("value", [0.09, 0.13, 0.11, 285.0, 960.0, 0.0001, 1e-05, -10.0, 0.0, -0.0, 212.5,
                                   0.3, 0.7, 1.0, 67.0, 0.015, 123456.0])
def test_names_that_are_exact_today_are_unchanged(value):
    assert float(f"{value:g}") == value  # the precondition: today's text is exact
    assert render_name("HD_{hatch_distance_mm}", {HD: value}, 1) == f"HD_{value:g}"


def test_names_from_existing_range_syntax_are_unchanged():
    levels = parse_values("0:1:0.1", 0.1) + parse_values("0.09:0.13:0.02", 0.1) + parse_values("200:300:12.5", 1.0)
    for v in levels:
        assert render_name("X{hatch_distance_mm}", {HD: v}, 1) == f"X{v:g}"


@pytest.mark.parametrize("value, text", [(0.1234567, "0.1234567"), (12345.67, "12345.67"),
                                         (1234567.0, "1234567.0"), (0.12345678, "0.12345678")])
def test_lossy_values_are_written_exactly(value, text):
    assert render_name("V{hatch_distance_mm}", {HD: value}, 1) == f"V{text}"


def test_description_carries_the_exact_value():
    v = generate(part(), {HD: [0.1234567]}, "HD_{hatch_distance_mm}")[0]
    assert v.name == "HD_0.1234567"
    assert v.description.endswith(f"{HD}=0.1234567")
    assert get_path(v, HD) == 0.1234567


def test_levels_differing_in_the_seventh_digit_get_different_names():
    variants = generate(part(), {HD: [0.1234561, 0.1234564]}, "HD_{hatch_distance_mm}")
    assert [v.name for v in variants] == ["HD_0.1234561", "HD_0.1234564"]
    variants = generate(part(), {SPEED: [1234.5671, 1234.5674]}, "S{speed_mm_s}_{i}")
    assert len({v.name for v in variants}) == 2
    assert len({v.description for v in variants}) == 2


def test_distinct_levels_always_give_distinct_names():
    levels = [0.1 + i * 1e-8 for i in range(50)]
    names = [v.name for v in generate(part(), {HD: levels}, "{hatch_distance_mm}")]
    assert len(set(names)) == 50


def test_cli_second_batch_with_a_seventh_digit_level_is_not_a_collision(cli):
    assert cli("doe", "P1", "-f", f"{HD}=0.1234561", "--name", "HD_{hatch_distance_mm}").exit_code == 0
    res = cli("doe", "P1", "-f", f"{HD}=0.1234564", "--name", "HD_{hatch_distance_mm}")
    assert res.exit_code == 0, res.output
    assert {"HD_0.1234561", "HD_0.1234564"} <= set(cli.lib.names())


def test_default_names_are_unchanged(cli):
    res = cli("doe", "P1", "-f", f"{HD}=0.09,0.11", "-f", "hatch.infill.power_w=250:300:50", "--name",
              "HD{hatch_distance_mm}_P{power_w}")
    assert res.exit_code == 0, res.output
    assert {"HD0.09_P250", "HD0.09_P300", "HD0.11_P250", "HD0.11_P300"} <= set(cli.lib.names())


# ---------------------------------------------------------------- item 4: decimal comma

@pytest.mark.parametrize("text", ["0,05", "960,05", "0,050", "0.09,0,05", "1,05;2"])
def test_decimal_comma_with_zero_led_fraction_is_rejected_with_guidance(text):
    with pytest.raises(ValueError) as exc:
        parse_values(text, 0.1)
    msg = str(exc.value)
    assert "decimal" in msg and "." in msg


def test_decimal_comma_message_suggests_the_point_form():
    with pytest.raises(ValueError, match=r"0\.05"):
        parse_values("0,05", 0.1)


@pytest.mark.parametrize("text, expected", [
    ("0.05", [0.05]),
    ("0.05,0.07", [0.05, 0.07]),
    ("0.09,0.11,0.13", [0.09, 0.11, 0.13]),
    ("0.05, 0.5", [0.05, 0.5]),
    ("05", [5.0]),                 # a lone leading-zero number is not ambiguous
    ("05 06", [5.0, 6.0]),
    ("0;05", [0.0, 5.0]),          # semicolon is never a decimal mark
    ("1,0.5", [1.0, 0.5]),
])
def test_unambiguous_lists_are_unchanged(text, expected):
    assert parse_values(text, 0.1) == expected


def test_integer_parameters_are_not_affected():
    assert parse_values("0,05", 0) == [0, 5]


def test_ranges_are_not_affected():
    assert parse_values("0.05:0.07:0.01", 0.1) == [0.05, 0.06, 0.07]


def test_cli_rejects_decimal_comma(cli):
    res = cli("doe", "P1", "-f", f"{HD}=0,05", "--dry-run")
    assert res.exit_code == 1 and isinstance(res.exception, SystemExit)
    assert "0.05" in res.output


# ---------------------------------------------------------------- item 2: file-name preflight

def _name_max(lib):
    try:
        return os.pathconf(lib.sets_dir, "PC_NAME_MAX")
    except (AttributeError, OSError, ValueError):
        return 255


def test_too_long_name_is_a_preflight_problem_not_an_exception(tmp_path):
    lib = Library(tmp_path / "lib")
    name = "X" * (_name_max(lib) + 10)
    problems = lib.check_new_names([name, "Fine_1"])
    assert len(problems) == 1 and "too long" in problems[0]


def test_name_whose_temporary_file_would_be_too_long_is_reported(tmp_path):
    lib = Library(tmp_path / "lib")
    limit = _name_max(lib)
    near = "Y" * (limit - 10)  # Y...Y.json fits, but the save's temporary file name does not
    with pytest.raises(OSError):  # demonstrates the check is not spurious on this file system
        lib.save(generate(part(), {HD: [0.09]}, near)[0], overwrite=False)
    assert any("too long" in p for p in lib.check_new_names([near]))


def test_longest_accepted_name_really_saves(tmp_path):
    lib = Library(tmp_path / "lib")
    limit = _name_max(lib)
    ok = None
    for n in range(limit, 0, -1):
        if not lib.check_new_names(["Z" * n]):
            ok = "Z" * n
            break
    assert ok is not None and len(ok) > 200
    lib.save(generate(part(), {HD: [0.09]}, ok)[0], overwrite=False)
    assert lib.exists(ok)


def test_multibyte_names_are_measured_in_bytes(tmp_path):
    lib = Library(tmp_path / "lib")
    # the slug keeps only ASCII, so this checks that the measured name is the file name actually used
    name = "é" * 10 + "A" * (_name_max(lib) + 1)
    assert any("too long" in p for p in lib.check_new_names([name]))


def test_uncheckable_name_is_reported(tmp_path, monkeypatch):
    lib = Library(tmp_path / "lib")
    real = Path.exists

    def exists(self, *a, **k):
        if "Locked_Out" in self.name:
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "exists", exists)
    problems = lib.check_new_names(["Locked_Out_1"])
    assert len(problems) == 1 and "cannot be checked" in problems[0] and "Permission denied" in problems[0]


def test_platform_reserved_names_are_reported_where_the_platform_says_so(tmp_path, monkeypatch):
    lib = Library(tmp_path / "lib")
    monkeypatch.setattr(os.path, "isreserved", lambda p: Path(p).stem.upper() == "CON", raising=False)
    problems = lib.check_new_names(["CON", "CONTOUR_1"])
    assert len(problems) == 1 and "CON" in problems[0] and "reserved" in problems[0]


def test_reserved_name_check_is_not_applied_without_platform_support(tmp_path, monkeypatch):
    lib = Library(tmp_path / "lib")
    monkeypatch.delattr(os.path, "isreserved", raising=False)
    assert lib.check_new_names(["CON"]) == []


def test_cli_dry_run_reports_too_long_names(cli):
    res = cli("doe", "P1", "-f", f"{HD}=0.09,0.11", "--name", "X" * 300 + "_{i}", "--dry-run")
    assert res.exit_code == 1 and isinstance(res.exception, SystemExit)
    assert "too long" in res.output


def test_cli_saves_nothing_when_a_name_is_too_long(cli):
    before = sorted(cli.lib.names())
    res = cli("doe", "P1", "-f", f"{HD}=0.09,0.11", "--name", "{i}" + "X" * 260)
    assert res.exit_code == 1 and isinstance(res.exception, SystemExit)
    assert sorted(cli.lib.names()) == before


# ---------------------------------------------------------------- item 1: failure reporting

def test_cli_reports_saved_failed_and_not_attempted_on_disk_full(cli, monkeypatch):
    fail_on_write(monkeypatch, 3, OSError(errno.ENOSPC, "No space left on device"))
    res = cli("doe", "P1", "-f", f"{HD}=0.09,0.10,0.11,0.12")
    assert res.exit_code == 1 and isinstance(res.exception, SystemExit), res.output
    out = res.output
    assert "saved (2): P1_DOE1, P1_DOE2" in out
    assert "failed (1): P1_DOE3" in out and "No space left on device" in out
    assert "not attempted (1): P1_DOE4" in out
    assert "not transactional" in out
    names = set(cli.lib.names())
    assert {"P1_DOE1", "P1_DOE2"} <= names and not {"P1_DOE3", "P1_DOE4"} & names
    assert get_path(cli.lib.get("P1_DOE2"), HD) == 0.10  # saved variants are kept intact


def test_cli_reports_permission_error_on_first_write(cli, monkeypatch):
    fail_on_write(monkeypatch, 1, PermissionError(errno.EACCES, "Permission denied"))
    res = cli("doe", "P1", "-f", f"{HD}=0.09,0.10")
    assert res.exit_code == 1 and isinstance(res.exception, SystemExit)
    assert "saved (0): none" in res.output
    assert "failed (1): P1_DOE1" in res.output and "Permission denied" in res.output
    assert "not attempted (1): P1_DOE2" in res.output


def test_cli_reports_a_name_taken_after_the_preflight(cli, monkeypatch):
    real = Library.check_new_names

    def racing(self, names):
        out = real(self, names)
        Library(self.root).copy("Default_Part_40um", "P1_DOE2")
        return out

    monkeypatch.setattr(Library, "check_new_names", racing)
    res = cli("doe", "P1", "-f", f"{HD}=0.09,0.10,0.11")
    assert res.exit_code == 1 and isinstance(res.exception, SystemExit)
    assert "saved (1): P1_DOE1" in res.output
    assert "failed (1): P1_DOE2" in res.output and "already exists" in res.output
    assert "not attempted (1): P1_DOE3" in res.output
    assert get_path(cli.lib.get("P1_DOE2"), HD) == 0.11  # the other process's set is untouched
