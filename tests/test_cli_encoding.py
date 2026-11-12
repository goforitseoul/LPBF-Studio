"""CLEANUP-01: on Windows, `lpbf` output sent to a file or a pipe is UTF-8, so a non-UTF-8 code page (GBK) no longer
makes `lpbf list` fail. The policy lives in `cli._utf8_when_redirected`; these tests check the bytes written."""

import io
import locale
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lpbfstudio import cli
from lpbfstudio.defaults import default_sets
from lpbfstudio.library import Library

SRC = str(Path(cli.__file__).resolve().parents[1])
NAME = "Ti64-µ-é-中文"


def env_without_overrides():
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    env["PYTHONPATH"] = os.pathsep.join([SRC, os.environ.get("PYTHONPATH", "")])
    return env


@pytest.fixture
def library(tmp_path):
    lib = Library(tmp_path / "lib")
    lib.copy(default_sets()[0].name, NAME)
    es = lib.get(NAME)
    es.description = "Beschreibung mit Umlaut ä"
    lib.save(es)
    return tmp_path / "lib"


def run_lpbf(library, *args, **kw):
    return subprocess.run([sys.executable, "-m", "lpbfstudio", "--library", str(library), *args], env=env_without_overrides(),
                          timeout=60, **kw)


def test_list_to_a_pipe_is_utf8_with_every_character(library):
    p = run_lpbf(library, "list", capture_output=True)
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
    text = p.stdout.decode("utf-8")  # strict: the bytes must be UTF-8
    assert f"40 µm  {NAME}" in text and "Default_Part_40um" in text
    assert b"Traceback" not in p.stderr


def test_list_to_a_file_is_utf8(library, tmp_path):
    out = tmp_path / "out.txt"
    with open(out, "wb") as fh:
        p = run_lpbf(library, "list", stdout=fh, stderr=subprocess.PIPE)
    assert p.returncode == 0
    assert NAME in out.read_bytes().decode("utf-8")


def test_show_with_non_ascii_values_to_a_pipe(library):
    p = run_lpbf(library, "show", NAME, capture_output=True)
    assert p.returncode == 0 and "Beschreibung mit Umlaut ä" in p.stdout.decode("utf-8")


def test_errors_on_a_redirected_stderr_are_utf8_and_keep_their_exit_code(library):
    p = run_lpbf(library, "show", "nope-µ", capture_output=True)
    assert p.returncode == 1 and p.stdout == b""
    assert p.stderr.decode("utf-8").startswith("error: ") and "nope-µ" in p.stderr.decode("utf-8")


def test_an_explicit_pythonioencoding_is_respected(library):
    env = env_without_overrides()
    env["PYTHONIOENCODING"] = "utf-16"
    p = subprocess.run([sys.executable, "-m", "lpbfstudio", "--library", str(library), "list"], env=env, capture_output=True,
                       timeout=60)
    assert p.returncode == 0 and NAME in p.stdout.decode("utf-16")


@pytest.mark.skipif(sys.platform != "win32" or locale.getpreferredencoding(False).lower().replace("-", "") == "utf8",
                    reason="needs Windows with a non-UTF-8 code page; the policy is also tested below with a simulated stream")
def test_this_machine_has_a_non_utf8_code_page_and_still_works(library):
    """The original failure: GBK (cp936) here. Before the fix this exited 1 with UnicodeEncodeError."""
    p = run_lpbf(library, "list", capture_output=True)
    assert p.returncode == 0 and "µm" in p.stdout.decode("utf-8")


# --------------------------------------------------------------------------- the policy, on simulated streams


class Stream(io.TextIOWrapper):
    def __init__(self, encoding, tty=False):
        super().__init__(io.BytesIO(), encoding=encoding)
        self._tty = tty

    def isatty(self):
        return self._tty


class Flags:
    """``sys.flags`` with a chosen UTF-8 mode: Python turns that mode on by itself under the C locale (Linux CI)."""

    def __init__(self, utf8_mode):
        self.utf8_mode = utf8_mode
        self._real = sys.flags

    def __getattr__(self, name):
        return getattr(self._real, name)


def policy(monkeypatch, platform, stdout, stderr=None, env=None, utf8_mode=0):
    monkeypatch.setattr(cli.sys, "flags", Flags(utf8_mode))
    monkeypatch.setattr(cli.sys, "platform", platform)
    monkeypatch.setattr(cli.sys, "stdout", stdout)
    monkeypatch.setattr(cli.sys, "stderr", stderr or Stream("ascii", tty=True))
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    cli._utf8_when_redirected()


def test_a_redirected_gbk_stream_becomes_utf8_on_windows(monkeypatch):
    out = Stream("gbk")
    policy(monkeypatch, "win32", out)
    out.write("40 µm Ti64")
    out.flush()
    assert out.buffer.getvalue() == "40 µm Ti64".encode("utf-8")


def test_a_utf8_code_page_is_left_as_it_is(monkeypatch):
    out = Stream("utf-8")
    policy(monkeypatch, "win32", out)
    assert out.encoding == "utf-8"


def test_a_console_is_left_as_it_is(monkeypatch):
    out = Stream("gbk", tty=True)
    policy(monkeypatch, "win32", out)
    assert out.encoding == "gbk"


def test_pythonioencoding_is_left_as_it_is(monkeypatch):
    out = Stream("gbk")
    policy(monkeypatch, "win32", out, env={"PYTHONIOENCODING": "gbk"})
    assert out.encoding == "gbk"


def test_pythons_utf8_mode_is_left_as_it_is(monkeypatch):
    out = Stream("gbk")  # an encoding chosen explicitly for the stream while the mode is on
    policy(monkeypatch, "win32", out, utf8_mode=1)
    assert out.encoding == "gbk"


def test_other_platforms_are_unchanged(monkeypatch):
    out = Stream("latin-1")
    policy(monkeypatch, "linux", out)
    assert out.encoding == "latin-1"


def test_a_stream_that_cannot_be_reconfigured_is_left_alone(monkeypatch):
    class Plain:
        encoding = "gbk"

        def isatty(self):
            return False

    plain = Plain()
    policy(monkeypatch, "win32", plain)
    assert plain.encoding == "gbk"


def test_cli_runner_output_and_exit_codes_are_unchanged(tmp_path):
    from click.testing import CliRunner

    lib = Library(tmp_path / "lib")
    lib.copy(default_sets()[0].name, "P1")
    res = CliRunner().invoke(cli.main, ["--library", str(tmp_path / "lib"), "list"])
    assert res.exit_code == 0 and "40 µm" in res.output
    res = CliRunner().invoke(cli.main, ["--library", str(tmp_path / "lib"), "show", "nope"])
    assert res.exit_code == 1
