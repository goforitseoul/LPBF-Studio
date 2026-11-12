"""Test helpers for R7-B (history scan redundancy removal).

``reference()`` loads the history module exactly as it was at the pushed baseline ``ce62165``, from a byte copy kept in
``tests/data``. The R7-B tests compare the current resolver with it: same results, same diagnostics, and the same files
examined in the same order. The copy is test data only; nothing in the product imports it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.machinery
import importlib.util
import os
import sys
import time
from enum import Enum
from pathlib import Path

REFERENCE_FILE = Path(__file__).parent / "data" / "history_reference_ce62165.py.txt"
REFERENCE_SHA256 = "027938146898a4a2fe592b81ef2fbd5e8f37243c5ec39db2919ba4dfacf57e18"  # of the LF form, = ce62165 history.py
_NAME = "lpbfstudio._history_reference_ce62165"


def reference():
    """The baseline history module, loaded once beside the real one."""
    if _NAME in sys.modules:
        return sys.modules[_NAME]
    raw = REFERENCE_FILE.read_bytes().replace(b"\r\n", b"\n")  # a Windows checkout may have converted the line ends
    assert hashlib.sha256(raw).hexdigest() == REFERENCE_SHA256, "the baseline copy of history.py was changed"
    loader = importlib.machinery.SourceFileLoader(_NAME, str(REFERENCE_FILE))
    spec = importlib.util.spec_from_loader(_NAME, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[_NAME] = module
    try:
        loader.exec_module(module)
    except BaseException:
        del sys.modules[_NAME]
        raise
    return module


def plain(value):
    """A result as plain data, so that results of the two modules (whose classes differ) can be compared."""
    if isinstance(value, Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    return value


def age(root: Path, seconds: float = 30.0) -> None:
    """Make every folder under ``.history/objects`` look unchanged for ``seconds``, as in a library at rest."""
    then = time.time() - seconds
    for base, dirs, _ in os.walk(Path(root) / ".history" / "objects"):
        for d in dirs:
            os.utime(os.path.join(base, d), (then, then))


class Trace:
    """Records every file-system examination a history module makes, through the module's own hooks."""

    def __init__(self, monkeypatch, module) -> None:
        self.events: list[tuple[str, str]] = []
        real_lstat, real_open, real_names = module._lstat, module._os_open, module._names

        def lstat(path):
            self.events.append(("lstat", str(path)))
            return real_lstat(path)

        def os_open(path, *args):
            self.events.append(("open", str(path)))
            return real_open(path, *args)

        def names(path):
            self.events.append(("list", str(path)))
            return real_names(path)

        monkeypatch.setattr(module, "_lstat", lstat)
        monkeypatch.setattr(module, "_os_open", os_open)
        monkeypatch.setattr(module, "_names", names)
        monkeypatch.setattr(module, "_fault", lambda step: self.events.append(("step", step)))
