"""Shared test setup.

Accepted B2 and B6 tests hook ``os.replace``, ``os.fsync``, ``os.unlink`` or ``tempfile.mkstemp`` to act at a set file's own
write. History's staging (DC-001 v0.6 S3) runs the same routine earlier in the same call, so here it runs with the real
primitives: those hooks keep addressing the canonical write they were written for.
"""

import os
import tempfile

import pytest

from lpbfstudio import history

_REAL = os.replace, os.fsync, os.unlink, tempfile.mkstemp


@pytest.fixture(autouse=True)
def staging_uses_the_real_primitives(monkeypatch):
    original = history._staging_write

    def staged(path, text):
        hooked = os.replace, os.fsync, os.unlink, tempfile.mkstemp
        os.replace, os.fsync, os.unlink, tempfile.mkstemp = _REAL
        try:
            original(path, text)
        finally:
            os.replace, os.fsync, os.unlink, tempfile.mkstemp = hooked

    monkeypatch.setattr(history, "_staging_write", staged)
