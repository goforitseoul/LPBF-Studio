"""R7-B O-3: finding the file of a set does not list the folder when the set is in the file named after it.

``baseline_locate`` is the routine as it was at ``ce62165``, word for word. The current one must return the same file
for every name, read the same files in the same order whenever it has to search, and read one file when it does not.
"""

import os
from pathlib import Path

import pytest

from lpbfstudio.defaults import default_sets
from lpbfstudio.library import Library, LibraryError

DEFAULT = default_sets()[0].name


def baseline_locate(self, name):
    direct = self._file(name)
    candidates = [direct] + [f for f in sorted(self.sets_dir.glob("*.json")) if f != direct]
    for f in candidates:
        if not f.exists():
            continue
        try:
            if self._read(f).name == name:
                return f
        except (OSError, ValueError):
            continue
    return None


class Watch:
    """Records the files read and the folder listings made through a library."""

    def __init__(self, monkeypatch, lib) -> None:
        self.reads: list[str] = []
        self.listings = 0
        real_read, real_glob = Library._read, type(lib.sets_dir).glob

        def read(f):
            self.reads.append(f.name)
            return real_read(f)

        def glob(path, pattern, *args, **kwargs):
            if path == lib.sets_dir:
                self.listings += 1
            return real_glob(path, pattern, *args, **kwargs)

        monkeypatch.setattr(Library, "_read", staticmethod(read))
        monkeypatch.setattr(type(lib.sets_dir), "glob", glob)

    def take(self):
        got = (list(self.reads), self.listings)
        del self.reads[:]
        self.listings = 0
        return got


def put(lib, file_name, set_name, like=DEFAULT):
    """A file in ``sets`` holding the set ``set_name``, whatever the file is called."""
    es = lib.get(like).model_copy(deep=True)
    es.name = set_name
    es.locked = False
    (lib.sets_dir / file_name).write_text(es.model_dump_json(indent=2), encoding="utf-8")


@pytest.fixture
def lib(tmp_path):
    lib = Library(tmp_path / "lib")
    put(lib, "Plain.json", "Plain")
    put(lib, "My_Part.json", "My Part")  # B1: "My Part" and "My_Part" share the file name My_Part.json
    put(lib, "zz_elsewhere.json", "Misnamed")  # a file that holds a name its own name does not say
    put(lib, "Wrong.json", "Right")  # Wrong.json holds Right; nothing holds Wrong
    put(lib, "aa_first.json", "Twice")  # two files hold the same name: the first in sorted order wins
    put(lib, "bb_second.json", "Twice")
    put(lib, "Direct.json", "Direct")  # held by its own file and by another: the direct file wins
    put(lib, "a_also_direct.json", "Direct")
    put(lib, "Hidden.json", "Other")  # Hidden.json holds Other; Hidden is in a file sorted before it and one after
    put(lib, "A_hidden.json", "Hidden")
    put(lib, "z_hidden.json", "Hidden")
    (lib.sets_dir / "Broken.json").write_text("{ not json", encoding="utf-8")  # the direct file cannot be parsed
    put(lib, "holds_broken.json", "Broken")
    (lib.sets_dir / "Folder.json").mkdir()  # the direct entry is a folder
    put(lib, "holds_folder.json", "Folder")
    put(lib, "T_st.json", "Tëst 測試")  # letters outside ASCII become "_" in the file name
    put(lib, "Ünïcode.json", "elsewhere ß")
    (lib.sets_dir / "notes.txt").write_text("not a set", encoding="utf-8")
    (lib.sets_dir / "invalid.json").write_text('{"name": 3}', encoding="utf-8")
    return lib


NAMES = ["Plain", "My Part", "My_Part", "Misnamed", "Wrong", "Right", "Twice", "Direct", "Hidden", "Other", "Broken", "Folder",
         "Tëst 測試", "elsewhere ß", "ß elsewhere", "absent", "Absent Set", "plain", "PLAIN", "invalid", "notes", "zz_elsewhere",
         "a/b", "..", "con", "x" * 300, DEFAULT]


def outcome(locate, lib, name):
    try:
        return locate(lib, name)
    except (OSError, LibraryError) as exc:
        return type(exc), str(exc)


@pytest.mark.parametrize("name", NAMES)
def test_the_same_file_as_the_baseline(lib, name):
    assert outcome(Library._locate, lib, name) == outcome(baseline_locate, lib, name)


def test_the_corpus_means_what_it_says(lib):
    found = {name: (f.name if f else None) for name in NAMES if len(name) < 200 for f in [lib._locate(name)]}
    assert found["Plain"] == "Plain.json"
    assert found["My Part"] == "My_Part.json" and found["My_Part"] is None  # the slug collision is not a match
    assert found["Misnamed"] == "zz_elsewhere.json" and found["zz_elsewhere"] is None
    assert found["Wrong"] is None and found["Right"] == "Wrong.json"
    assert found["Twice"] == "aa_first.json"
    assert found["Direct"] == "Direct.json"
    assert found["Hidden"] == "A_hidden.json" and found["Other"] == "Hidden.json"
    assert found["Broken"] == "holds_broken.json" and found["Folder"] == "holds_folder.json"
    assert found["Tëst 測試"] == "T_st.json" and found["elsewhere ß"] == "Ünïcode.json"
    assert found["absent"] is None and found["invalid"] is None


@pytest.mark.parametrize("name", ["Plain", "My Part", "Direct", "Tëst 測試", DEFAULT])
def test_a_set_in_its_own_file_is_found_by_reading_that_file_only(lib, monkeypatch, name):
    watch = Watch(monkeypatch, lib)
    f = lib._locate(name)
    assert f == lib._file(name)
    assert watch.take() == ([f.name], 0)  # one read, and the folder was not listed


@pytest.mark.parametrize("name", [n for n in NAMES if n not in ("Plain", "My Part", "Direct", "Tëst 測試", DEFAULT)])
def test_any_other_name_is_searched_for_exactly_as_at_the_baseline(lib, monkeypatch, name):
    watch = Watch(monkeypatch, lib)
    want = outcome(baseline_locate, lib, name)
    want_reads, want_listings = watch.take()
    got = outcome(Library._locate, lib, name)
    reads, listings = watch.take()
    assert got == want
    assert reads == want_reads  # the same candidates, in the same order, stopping at the same one
    refused = isinstance(got, tuple)  # a name too long for the file system is refused at the direct file, before any listing
    assert want_listings == 1 and listings == (0 if refused else 1)


def test_a_miss_reads_every_set_file_once_in_sorted_order(lib, monkeypatch):
    watch = Watch(monkeypatch, lib)
    assert lib._locate("absent") is None
    reads, listings = watch.take()
    assert listings == 1 and reads == [f.name for f in sorted(lib.sets_dir.glob("*.json"))] and len(reads) > 15


def test_a_direct_file_that_holds_another_name_is_read_first_and_once(lib, monkeypatch):
    watch = Watch(monkeypatch, lib)
    assert lib._locate("Hidden").name == "A_hidden.json"
    reads, _ = watch.take()
    assert reads[0] == "Hidden.json" and reads.count("Hidden.json") == 1 and reads[-1] == "A_hidden.json"
    assert "z_hidden.json" not in reads  # the search stops at the first file that holds the name


def test_a_name_with_no_letters_is_refused_as_before(lib):
    for name in ("", "///", "   "):
        with pytest.raises(LibraryError):
            lib._locate(name)
        with pytest.raises(LibraryError):
            baseline_locate(lib, name)


def test_a_direct_file_that_cannot_be_read_does_not_hide_the_set(lib, monkeypatch):
    real = Library._read

    def read(f):
        if f.name == "Plain.json":
            raise PermissionError(13, "injected")
        return real(f)

    put(lib, "holds_plain.json", "Plain")
    monkeypatch.setattr(Library, "_read", staticmethod(read))
    assert lib._locate("Plain") == baseline_locate(lib, "Plain") == lib.sets_dir / "holds_plain.json"


def test_the_library_operations_find_the_same_sets(lib):
    assert lib.get("Misnamed").name == "Misnamed" and lib.get("Twice").name == "Twice"
    assert lib.exists("My Part") and lib.exists("Right")
    with pytest.raises(LibraryError, match="My_Part.json, which holds the set 'My Part'"):
        lib.copy("Plain", "My_Part")  # B1: the colliding name is still refused, with the same message
    lib.copy("Misnamed", "Copy of misnamed")
    assert lib.get("Copy of misnamed").name == "Copy of misnamed"
    lib.rename("Misnamed", "Named")
    assert not (lib.sets_dir / "zz_elsewhere.json").exists() and lib._locate("Named") == lib.sets_dir / "Named.json"
    lib.delete("Twice")
    assert not (lib.sets_dir / "aa_first.json").exists() and lib._locate("Twice") == lib.sets_dir / "bb_second.json"


def test_a_set_created_after_a_miss_is_found(lib):
    assert lib._locate("Later") is None
    put(lib, "Later.json", "Later")
    assert lib._locate("Later") == lib.sets_dir / "Later.json"
    os.replace(lib.sets_dir / "Later.json", lib.sets_dir / "moved.json")
    assert lib._locate("Later") == lib.sets_dir / "moved.json"  # nothing is remembered between two lookups
    assert isinstance(lib._locate("Later"), Path)
