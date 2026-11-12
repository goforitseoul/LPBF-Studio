"""R7-B, INV-1: the resolver gives exactly what it gave at the pushed baseline ``ce62165``.

Every on-disk state below is resolved by the current module and by a byte copy of the baseline module
(``_r7b_support.reference``). Compared: every ``ObjectView`` of every type (state, head, committed records, incomplete
folders, diagnostic text, ``A_max``), every ``Resolution`` (state, object, claimant, revision, head, ``history_ahead``,
the bytes read, the damaged list, the observations, errno), and the trace of what was examined on disk: each ``lstat``,
directory listing, open and fault-hook step, with its path, in order.

The states cover the rows of DC-001 §6.2, the object states of §6.1, the revision folder rules of §5.2, links and
Windows junctions, names outside ASCII, and the persistent states of §7.10, which are produced by stopping the real
writers at every step.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from _history_support import (ID_A, ID_B, ID_C, add_revision, can_symlink, canonical, identity_doc, junction, make_identity,
                              make_object, objdir, record_doc, seq)
from _r7b_support import Trace, age, plain, reference
from lpbfstudio import history as h
from lpbfstudio.defaults import default_sets
from lpbfstudio.library import Library

ref = reference()
P, Q = "sets/a.json", "sets/b.json"
PATHS = {"exposure_set": [P, Q, "sets/none.json", "sets/Tëst ß 測試.json"],
         "machine_profile": ["machines/m.json", "machine.json"],
         "process_settings": ["process.json"]}


# --------------------------------------------------------------------------- hand-built states


def rev_dir(root, n, oid=ID_A):
    d = objdir(root, oid) / "revisions" / seq(n)
    d.mkdir(parents=True)
    return d


def s_no_history(root):
    canonical(root)


def s_nothing(root):
    root.mkdir(exist_ok=True)


def s_tracked(root):
    make_object(root, (b"1", b"22", b"333"))
    canonical(root, b"333")


def s_history_ahead(root):
    make_object(root, (b"1", b"22", b"333"))
    canonical(root, b"22")


def s_same_bytes_twice(root):
    make_object(root, (b"1", b"22", b"1"))
    canonical(root, b"1")


def s_changed_outside(root):
    make_object(root, (b"1", b"22"))
    canonical(root, b"other")


def s_missing(root):
    make_object(root, (b"1",))


def s_tombstoned(root):
    make_object(root, (b"1",))
    add_revision(root, 2, kind="deleted")


def s_tombstoned_present(root):
    s_tombstoned(root)
    canonical(root, b"1")


def s_content_after_tombstone(root):
    s_tombstoned(root)
    add_revision(root, 3, b"y")
    canonical(root, b"y")


def s_untracked_beside_objects(root):
    make_object(root, (b"1",), Q, oid=ID_B)
    canonical(root, b"1", Q)
    canonical(root, b"u")


def s_two_claimants(root):
    make_object(root, (b"1",))
    make_object(root, (b"1",), oid=ID_B)
    canonical(root, b"1")


def s_renamed(root):
    make_identity(root)
    add_revision(root, 1, b"1", Q)
    add_revision(root, 2, b"2", P, operation="renamed", previous_path=Q)
    canonical(root, b"2")
    canonical(root, b"1", Q)


def s_locked_and_sources(root):
    make_identity(root)
    add_revision(root, 1, b"1", operation="created", lock_change="locked")
    make_identity(root, ID_B)
    add_revision(root, 1, b"c", Q, oid=ID_B, operation="copied", source={"object_id": ID_A, "seq": seq(1)})
    make_identity(root, ID_C)
    add_revision(root, 1, b"i", "sets/i.json", oid=ID_C, operation="imported",
                 source={"external_path": "C:\\x\\é.json", "content_hash": {"algorithm": "sha256", "value": "0" * 64}})
    canonical(root, b"1")
    canonical(root, b"c", Q)


def s_d1_empty(root):
    objdir(root).mkdir(parents=True)
    canonical(root)


def s_d1_with_temp(root):
    objdir(root).mkdir(parents=True)
    (objdir(root) / ".identity.json.abc123.tmp").write_bytes(b"{")
    canonical(root)


def s_d1_two_temps(root):
    s_d1_with_temp(root)
    (objdir(root) / ".identity.json.def456.tmp").write_bytes(b"{")


def s_d2_identity_only(root):
    make_identity(root)
    canonical(root)


def s_d2_empty_revisions(root):
    make_identity(root)
    (objdir(root) / "revisions").mkdir()


def s_d2_incomplete_only(root):
    make_identity(root)
    rev_dir(root, 1)
    (rev_dir(root, 2) / "content").write_bytes(b"x")
    d = rev_dir(root, 3)
    (d / "content").write_bytes(b"x")
    (d / ".record.json.abc.tmp").write_bytes(b"{")
    canonical(root)


def s_incomplete_between_committed(root):
    make_object(root, (b"1",))
    rev_dir(root, 2)
    add_revision(root, 3, b"3")
    (rev_dir(root, 4) / ".record.json.zz.tmp").write_bytes(b"")
    canonical(root, b"3")


def s_two_record_temps(root):
    make_object(root, (b"1",))
    d = rev_dir(root, 2)
    (d / ".record.json.a.tmp").write_bytes(b"")
    (d / ".record.json.b.tmp").write_bytes(b"")
    canonical(root, b"1")


def s_record_and_temp(root):
    make_object(root, (b"1",))
    (objdir(root) / "revisions" / seq(1) / ".record.json.a.tmp").write_bytes(b"")
    canonical(root, b"1")


def s_bad_object_name(root):
    make_object(root, (b"1",))
    (objdir(root).parent / "not-an-id").mkdir()
    canonical(root, b"1")


def s_file_as_object(root):
    make_object(root, (b"1",))
    (objdir(root).parent / ID_B).write_bytes(b"")
    canonical(root, b"1")


def s_unexpected_in_object(root):
    make_object(root, (b"1",))
    (objdir(root) / "junk").write_bytes(b"")
    canonical(root, b"1")


def s_identity_invalid(root):
    make_object(root, (b"1",))
    (objdir(root) / "identity.json").write_bytes(b"{not json")


def s_identity_mismatch(root):
    make_object(root, (b"1",))
    (objdir(root) / "identity.json").write_bytes(identity_doc(ID_B))


def s_identity_duplicate_key(root):
    make_object(root, (b"1",))
    (objdir(root) / "identity.json").write_bytes(
        b'{"object_type":"exposure_set","object_id":"' + ID_A.encode() + b'","created_at":"x","created_at":"y"}')


def s_identity_and_temp(root):
    make_object(root, (b"1",))
    (objdir(root) / ".identity.json.abc.tmp").write_bytes(b"")


def s_identity_is_a_folder(root):
    objdir(root).mkdir(parents=True)
    (objdir(root) / "identity.json").mkdir()


def s_no_identity_but_revisions(root):
    (objdir(root) / "revisions").mkdir(parents=True)


def s_revisions_is_a_file(root):
    make_identity(root)
    (objdir(root) / "revisions").write_bytes(b"")


def s_bad_revision_name(root):
    make_object(root, (b"1",))
    (objdir(root) / "revisions" / "2").mkdir()


def s_revision_zero(root):
    make_object(root, (b"1",))
    (objdir(root) / "revisions" / "00000000").mkdir()


def s_revision_is_a_file(root):
    make_object(root, (b"1",))
    (objdir(root) / "revisions" / seq(2)).write_bytes(b"")


def s_gap(root):
    make_object(root, (b"1",))
    add_revision(root, 3, b"3")
    canonical(root, b"3")


def s_unexpected_in_revision(root):
    make_object(root, (b"1",))
    (objdir(root) / "revisions" / seq(1) / "Thumbs.db").write_bytes(b"")
    canonical(root, b"1")


def s_record_not_json(root):
    make_object(root, (b"1",))
    (objdir(root) / "revisions" / seq(1) / "record.json").write_bytes(b"\xff\xfe")


def s_record_duplicate_key(root):
    make_object(root, (b"1",))
    text = json.dumps(record_doc(1, data=b"1"))
    (objdir(root) / "revisions" / seq(1) / "record.json").write_text(text[:-1] + ', "kind": "content"}', encoding="utf-8")


def s_record_wrong_seq(root):
    make_object(root, (b"1",))
    (objdir(root) / "revisions" / seq(1) / "record.json").write_text(json.dumps(record_doc(2, data=b"1")), encoding="utf-8")


def s_record_unknown_key(root):
    make_object(root, (b"1",))
    (objdir(root) / "revisions" / seq(1) / "record.json").write_text(json.dumps(record_doc(1, data=b"1", extra=1)),
                                                                     encoding="utf-8")


def s_record_bad_source(root):
    make_object(root, (b"1",))
    add_revision(root, 2, b"2", source={"object_id": ID_B, "seq": seq(1)})


def s_record_bad_path(root):
    make_identity(root)
    add_revision(root, 1, b"1", "../sets/a.json")


def s_missing_blob(root):
    make_object(root, (b"1", b"2"))
    (objdir(root) / "revisions" / seq(1) / "content").unlink()
    canonical(root, b"2")


def s_altered_blob_same_length(root):
    make_object(root, (b"1", b"2"))
    (objdir(root) / "revisions" / seq(1) / "content").write_bytes(b"9")
    canonical(root, b"2")


def s_altered_blob_other_length(root):
    make_object(root, (b"1", b"2"))
    (objdir(root) / "revisions" / seq(2) / "content").write_bytes(b"222")
    canonical(root, b"2")


def s_tombstone_with_blob(root):
    make_object(root, (b"1",))
    add_revision(root, 2, kind="deleted")
    (objdir(root) / "revisions" / seq(2) / "content").write_bytes(b"x")


def s_record_is_a_folder(root):
    make_object(root, (b"1",))
    rev_dir(root, 2).joinpath("record.json").mkdir()


def s_content_is_a_folder(root):
    make_object(root, (b"1",))
    rev_dir(root, 2).joinpath("content").mkdir()


def s_one_damaged_among_good(root):
    make_object(root, (b"1",))
    make_object(root, (b"q",), Q, oid=ID_B)
    make_object(root, (b"c",), "sets/c.json", oid=ID_C)
    (objdir(root, ID_C) / "junk").write_bytes(b"")
    canonical(root, b"1")
    canonical(root, b"q", Q)


def s_history_is_a_file(root):
    root.mkdir(exist_ok=True)
    (root / ".history").write_bytes(b"")
    canonical(root)


def s_objects_is_a_file(root):
    (root / ".history").mkdir(parents=True)
    (root / ".history" / "objects").write_bytes(b"")
    canonical(root)


def s_type_is_a_file(root):
    (root / ".history" / "objects").mkdir(parents=True)
    (root / ".history" / "objects" / "exposure_set").write_bytes(b"")
    canonical(root)


def s_unknown_in_history(root):
    make_object(root, (b"1",))
    (root / ".history" / "extra").mkdir()
    canonical(root, b"1")


def s_unknown_type(root):
    make_object(root, (b"1",))
    (root / ".history" / "objects" / "toolpath").mkdir()
    canonical(root, b"1")


def s_staging_and_jobs(root):
    make_object(root, (b"1",))
    (root / ".history" / "staging").mkdir()
    (root / ".history" / "staging" / "leftover.json").write_bytes(b"x")
    (root / ".history" / "jobs" / ("d" * 32)).mkdir(parents=True)
    canonical(root, b"1")


def s_staging_is_a_file(root):
    make_object(root, (b"1",))
    (root / ".history" / "staging").write_bytes(b"")
    canonical(root, b"1")


def s_other_types(root):
    make_object(root, (b"m",), "machines/m.json", otype="machine_profile")
    make_object(root, (b"old", b"p"), "process.json", oid=ID_B, otype="process_settings")
    make_object(root, (b"legacy",), "machine.json", oid=ID_C, otype="machine_profile")
    canonical(root, b"m", "machines/m.json")
    canonical(root, b"p", "process.json")
    canonical(root, b"edited", "machine.json")


def s_non_ascii(root):
    name = "sets/Tëst ß 測試.json"
    make_object(root, ("é".encode(), "測試".encode()), name)
    canonical(root, "測試".encode(), name)


def s_many(root):
    for i in range(12):
        oid = f"{i:032x}"
        make_object(root, tuple(f"{i}-{r}".encode() for r in range(1 + i % 4)), f"sets/s{i}.json", oid=oid)
        if i % 5 == 4:
            add_revision(root, 2 + i % 4, kind="deleted", path=f"sets/s{i}.json", oid=oid)
        else:
            canonical(root, f"{i}-{i % 4}".encode(), f"sets/s{i}.json")
    objdir(root, "f" * 32).mkdir()
    make_identity(root, "e" * 32)
    make_object(root, (b"1", b"2"))
    canonical(root, b"1")


STATES = [v for k, v in sorted(globals().items()) if k.startswith("s_") and callable(v)]


# links: a link or junction under .history is never followed


def link_to(link: Path, target: Path) -> None:
    os.symlink(target, link, target_is_directory=target.is_dir())


def l_object_folder(root, make):
    make_object(root, (b"1",), oid=ID_B, path=Q)
    elsewhere = root / "elsewhere"
    make_object(elsewhere, (b"1",))
    make(objdir(root), objdir(elsewhere))
    canonical(root, b"1")


def l_revisions(root, make):
    make_identity(root)
    elsewhere = root / "elsewhere"
    make_object(elsewhere, (b"1",))
    make(objdir(root) / "revisions", objdir(elsewhere) / "revisions")
    canonical(root, b"1")


def l_revision_folder(root, make):
    make_object(root, (b"1",))
    elsewhere = root / "elsewhere"
    make_object(elsewhere, (b"1", b"2"))
    make(objdir(root) / "revisions" / seq(2), objdir(elsewhere) / "revisions" / seq(2))
    canonical(root, b"2")


def l_history(root, make):
    elsewhere = root / "elsewhere"
    make_object(elsewhere, (b"1",))
    make(root / ".history", elsewhere / ".history")
    canonical(root, b"1")


def l_type_folder(root, make):
    elsewhere = root / "elsewhere"
    make_object(elsewhere, (b"1",))
    (root / ".history" / "objects").mkdir(parents=True)
    make(root / ".history" / "objects" / "exposure_set", elsewhere / ".history" / "objects" / "exposure_set")
    canonical(root, b"1")


def l_record(root, make):
    make_object(root, (b"1",))
    rec = objdir(root) / "revisions" / seq(1) / "record.json"
    real = root / "real-record.json"
    rec.replace(real)
    make(rec, real)
    canonical(root, b"1")


def l_content(root, make):
    make_object(root, (b"1",))
    blob = objdir(root) / "revisions" / seq(1) / "content"
    real = root / "real-content"
    blob.replace(real)
    make(blob, real)
    canonical(root, b"1")


def l_identity(root, make):
    make_object(root, (b"1",))
    ident = objdir(root) / "identity.json"
    real = root / "real-identity.json"
    ident.replace(real)
    make(ident, real)
    canonical(root, b"1")


FOLDER_LINKS = [l_object_folder, l_revisions, l_revision_folder, l_history, l_type_folder]
FILE_LINKS = [l_record, l_content, l_identity]


# --------------------------------------------------------------------------- comparison


def results(module, root):
    out = {}
    for t in module.ObjectType:
        try:
            out[f"scan {t.value}"] = plain(module.scan_objects(root, t))
        except module.Unverifiable as exc:
            out[f"scan {t.value}"] = ("unverifiable", str(exc), exc.errno)
        for path in PATHS[t.value]:
            out[f"resolve {path}"] = plain(module.resolve(root, t, path))
    return out


def traced(monkeypatch, module, root):
    with monkeypatch.context() as m:
        trace = Trace(m, module)
        got = results(module, root)
    return got, trace.events


def assert_equivalent(monkeypatch, root):
    want, want_trace = traced(monkeypatch, ref, root)
    got, got_trace = traced(monkeypatch, h, root)
    assert got == want
    assert got_trace == want_trace  # the same entries examined, the same way, in the same order
    with h.read_cache():  # the batch memory: a second pass answers from it and must say the same
        assert results(h, root) == want
        assert results(h, root) == want
    age(root)
    with h.read_cache(h.OPERATION_SETTLE_NS):  # the memory of one operation
        assert results(h, root) == want
        assert results(h, root) == want
    assert results(h, root) == want


@pytest.mark.parametrize("build", STATES, ids=lambda f: f.__name__[2:])
def test_hand_built_states(tmp_path, monkeypatch, build):
    root = tmp_path / "lib"
    build(root)
    assert_equivalent(monkeypatch, root)


def test_a_library_root_outside_ascii(tmp_path, monkeypatch):
    root = tmp_path / "bibliothèque 測試 ß"
    s_many(root)
    name = "sets/Tëst ß 測試.json"
    make_object(root, ("é".encode(), "測試".encode()), name, oid=ID_B)
    canonical(root, "測試".encode(), name)
    assert_equivalent(monkeypatch, root)


def test_one_object_is_classified_the_same_from_a_path_and_from_text(tmp_path):
    root = tmp_path / "lib"
    s_many(root)
    folder = objdir(root)
    want = plain(ref._classify_object(folder, "exposure_set", ID_A))
    assert plain(h._classify_object(folder, "exposure_set", ID_A)) == want
    assert plain(h._classify_object(str(folder), "exposure_set", ID_A)) == want
    with h.read_cache():  # and the two forms share one memory entry
        h._classify_object(folder, "exposure_set", ID_A)
        h._classify_object(str(folder), "exposure_set", ID_A)
        assert list(h._READ_CACHE.get()) == [str(folder)]


def test_a_root_given_as_text(tmp_path):
    root = tmp_path / "lib"
    s_many(root)
    assert results(h, str(root)) == results(ref, str(root)) == results(h, root)


@pytest.mark.parametrize("build", FOLDER_LINKS + FILE_LINKS, ids=lambda f: f.__name__[2:])
def test_symbolic_links_are_never_followed(tmp_path, monkeypatch, build):
    if not can_symlink(tmp_path):
        pytest.skip("this account cannot create symbolic links")
    root = tmp_path / "lib"
    build(root, link_to)
    assert_equivalent(monkeypatch, root)
    for name, value in results(h, root).items():  # and the linked history is never used as history
        if name == f"resolve {P}":
            assert value["state"] in ("unknown", "unverifiable", "untracked")


@pytest.mark.skipif(sys.platform != "win32", reason="junctions exist on Windows only")
@pytest.mark.parametrize("build", FOLDER_LINKS, ids=lambda f: f.__name__[2:])
def test_junctions_are_never_followed(tmp_path, monkeypatch, build):
    root = tmp_path / "lib"
    build(root, junction)
    assert_equivalent(monkeypatch, root)
    assert results(h, root)[f"resolve {P}"]["state"] in ("unknown", "unverifiable", "untracked")


def test_an_entry_that_cannot_be_examined_is_unverifiable_with_the_same_errno(tmp_path, monkeypatch):
    root = tmp_path / "lib"
    s_many(root)
    target = str(objdir(root) / "revisions" / seq(2) / "record.json")
    for module in (ref, h):
        real = module._lstat

        def lstat(path, real=real):
            if str(path) == target:
                raise PermissionError(13, "injected", str(path))
            return real(path)

        monkeypatch.setattr(module, "_lstat", lstat)
    want, got = results(ref, root), results(h, root)
    assert got == want and got[f"resolve {P}"]["state"] == "unverifiable" and got[f"resolve {P}"]["errno"] == 13


def test_a_blob_that_cannot_be_read_is_unverifiable_with_the_same_errno(tmp_path, monkeypatch):
    root = tmp_path / "lib"
    s_many(root)
    target = str(objdir(root) / "revisions" / seq(1) / "content")
    for module in (ref, h):
        real = module._os_open

        def os_open(path, *args, real=real):
            if str(path) == target:
                raise OSError(5, "injected", str(path))
            return real(path, *args)

        monkeypatch.setattr(module, "_os_open", os_open)
    want, got = results(ref, root), results(h, root)
    assert got == want and got[f"resolve {P}"]["errno"] == 5


# --------------------------------------------------------------------------- states left by the real writers (§7.10)


class Stop(BaseException):
    """Not an ``OSError``: history does not catch it, so the writer stops as an interrupted process would."""


DEFAULT = default_sets()[0].name


def prepared(root: Path) -> Library:
    lib = Library(root)
    lib.copy(DEFAULT, "P1")
    es = lib.get("P1")
    es.description = "second"
    lib.save(es)
    untracked = lib.get("P1").model_copy(deep=True)
    untracked.name = "U1"
    (root / "sets" / "U1.json").write_text(untracked.model_dump_json(indent=2), encoding="utf-8")
    return lib


def op_save_new(lib):
    es = lib.get("P1").model_copy(deep=True)
    es.name = "N1"
    lib.save(es)


def op_save_tracked(lib):
    es = lib.get("P1")
    es.description = "third"
    lib.save(es)


def op_save_untracked(lib):
    es = lib.get("U1")
    es.description = "adopted"
    lib.save(es)


def op_copy(lib):
    lib.copy("P1", "C1")


def op_copy_untracked(lib):
    lib.copy("U1", "C2")


def op_rename(lib):
    lib.rename("P1", "R1")


def op_rename_untracked(lib):
    lib.rename("U1", "R2")


def op_delete(lib):
    lib.delete("P1")


def op_delete_untracked(lib):
    lib.delete("U1")


OPERATIONS = [op_save_new, op_save_tracked, op_save_untracked, op_copy, op_copy_untracked, op_rename, op_rename_untracked,
              op_delete, op_delete_untracked]
MAX_STEPS = 40


def library_results(module, root):
    out = {}
    for t in module.ObjectType:
        out[f"scan {t.value}"] = plain(module.scan_objects(root, t))
    for f in sorted((root / "sets").iterdir()):
        out[f"resolve {f.name}"] = plain(module.resolve(root, module.ObjectType.EXPOSURE_SET, f"sets/{f.name}"))
    for name in ("P1", "U1", "N1", "C1", "C2", "R1", "R2"):
        out[f"resolve absent or present {name}"] = plain(module.resolve(root, module.ObjectType.EXPOSURE_SET, f"sets/{name}.json"))
    return out


@pytest.mark.parametrize("operation", OPERATIONS, ids=lambda f: f.__name__[3:])
def test_every_interrupted_state_of_a_writer(tmp_path, monkeypatch, operation):
    """Stop the writer just before its n-th history step, for every n, and compare the resolution of what is left."""
    steps: list[str] = []
    for n in range(MAX_STEPS):
        root = tmp_path / f"lib{n}"
        lib = prepared(root)
        seen: list[str] = []

        def fault(step, seen=seen, n=n):
            if step == "scan.read":
                return
            if len(seen) == n:
                raise Stop(step)
            seen.append(step)

        monkeypatch.setattr(h, "_fault", fault)
        try:
            operation(lib)
            finished = True
        except Stop:
            finished = False
        monkeypatch.setattr(h, "_fault", None)
        assert h._READ_CACHE.get() is None  # no memory survives an interrupted operation
        want = library_results(ref, root)
        assert library_results(h, root) == want
        with h.read_cache():
            assert library_results(h, root) == want
        for name, value in want.items():
            if name.startswith("scan "):
                assert all(v["state"] != "damaged" for v in value), (seen, value)  # §7.10: no row produces a D5
        if finished:
            steps = seen
            break
    else:
        pytest.fail("the writer never finished")
    assert len(steps) >= 3
