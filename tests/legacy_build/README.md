# Old-build fixture

A frozen copy of the top-level `lpbfstudio` modules at commit `7516fca75cfc5f58b06b0b8ed1c2b1b39c31a649`, the build before writer locks and the OD-1 lock entry (`gui/` is left out: no fixture test uses it). It stands for "old build" in revision 5 section 5, lock tests 10 to 12.

It runs only in subprocesses, with this folder on `PYTHONPATH`. Its bytes are never edited. `SHA256SUMS` pins them, and `tests/test_lock_mixed_version.py` checks the pins.
