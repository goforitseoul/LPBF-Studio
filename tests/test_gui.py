import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.environ.get("LPBF_REQUIRE_GUI_TESTS"):
    # CI: a missing Qt runtime must fail the run instead of skipping the GUI tests
    from PySide6 import QtWidgets
else:
    QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from lpbfstudio.library import Library  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_edit_and_save_through_gui(app, tmp_path, monkeypatch):
    from lpbfstudio.gui import app as appmod

    monkeypatch.setattr(appmod.QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    lib = Library(tmp_path / "lib")
    lib.copy("Default_Part_40um", "Edit_Me")
    w = appmod.MainWindow(lib)
    w.refresh_library(select="Edit_Me")
    w.load_set("Edit_Me")
    infill = w.tree.topLevelItem(0).child(0).child(0)
    w.tree.setCurrentItem(infill)
    w.form.widgets["power_w"].setValue(310)
    assert w.dirty
    w.add_exposure("hatch")  # duplicate is refused without corrupting the set
    assert w.current.kinds() == ["hatch", "contour", "edge"]
    assert w.save_current()
    assert lib.get("Edit_Me").get("hatch").infill.power_w == 310


def test_locked_set_is_read_only(app, tmp_path):
    from lpbfstudio.gui.app import MainWindow

    w = MainWindow(Library(tmp_path / "lib"))
    w.load_set("Default_Part_40um")
    assert w.form.readonly
    assert not w.act_save.isEnabled()
    assert not w.add_btn.isEnabled()


def test_set_name_field_cannot_rename_or_duplicate(app, tmp_path):
    from lpbfstudio.gui.app import MainWindow

    lib = Library(tmp_path / "lib")
    lib.copy("Default_Part_40um", "A")
    w = MainWindow(lib)
    w.refresh_library(select="A")
    w.load_set("A")
    w.tree.setCurrentItem(w.tree.topLevelItem(0))
    name_field = w.form.widgets["name"]
    assert not name_field.isEnabled()  # rename goes through the Rename action
    name_field.setText("B")
    name_field.editingFinished.emit()
    assert w.current.name == "A"
    w.current.description = "edited"
    w.mark_dirty()
    assert w.save_current()
    assert "B" not in lib.names()
    assert lib.get("A").description == "edited"


def test_contour_pass_name_stays_editable(app, tmp_path):
    from lpbfstudio.gui.app import MainWindow
    from lpbfstudio.models import ContourPass

    lib = Library(tmp_path / "lib")
    lib.copy("Default_Part_40um", "A")
    w = MainWindow(lib)
    w.load_set("A")
    w.rebuild_tree(keep=w.current.get("contour").passes[0])
    assert isinstance(w.form.obj, ContourPass)
    assert w.form.widgets["name"].isEnabled()


def test_rejected_form_edit_leaves_model_unchanged(app):
    from lpbfstudio.defaults import default_sets
    from lpbfstudio.gui.forms import ModelForm
    from lpbfstudio.models import SetCategory

    es = default_sets()[0].model_copy(deep=True)
    es.locked = False
    errors = []
    set_form = ModelForm(es)
    set_form.error.connect(errors.append)
    before = es.model_dump(mode="json")
    set_form._commit("category", SetCategory.SUPPORT)  # rejected by the set-level validator
    assert es.model_dump(mode="json") == before
    infill_form = ModelForm(es.get("hatch").infill)
    infill_form.error.connect(errors.append)
    infill_form._commit("power_w", 5000.0)  # above the field maximum
    assert es.model_dump(mode="json") == before
    assert len(errors) == 2


def test_checks_table_shows_codes_and_set_level_scope(app, tmp_path):
    from lpbfstudio.gui.app import MainWindow
    from lpbfstudio.models import Contour

    lib = Library(tmp_path / "lib")
    lib.copy("Default_Part_40um", "A")
    w = MainWindow(lib)
    w.load_set("A")
    w.current.exposure_types = [Contour()]
    w.mark_dirty()
    rows = [[w.checks_table.item(r, c).text() for c in range(w.checks_table.columnCount())]
            for r in range(w.checks_table.rowCount())]
    assert ["warning", "SET-002", "(set)"] == rows[0][:3]


def test_checks_use_the_profile_the_set_names(app, tmp_path):
    from lpbfstudio.gui.app import MainWindow
    from lpbfstudio.models import MachineProfile

    lib = Library(tmp_path / "lib")
    lib.save_machine(MachineProfile(name="Low 200 W", max_power_w=200))
    lib.copy("Default_Part_40um", "A")
    w = MainWindow(lib)

    def rows():
        return [(w.checks_table.item(r, 1).text(), w.checks_table.item(r, 2).text())
                for r in range(w.checks_table.rowCount())]

    w.load_set("A")
    assert ("MCH-001", "hatch.infill.power_w") not in rows()
    w.current.machine = "Low 200 W"
    w.mark_dirty()
    assert ("MCH-001", "hatch.infill.power_w") in rows()
    w.current.machine = "Nope"
    w.mark_dirty()
    assert ("MRF-002", "machine") in rows()


def test_machine_profile_name_is_read_only_in_its_form(app):
    from lpbfstudio.gui.forms import ModelForm
    from lpbfstudio.models import MachineProfile

    form = ModelForm(MachineProfile())
    assert not form.widgets["name"].isEnabled()
    assert form.widgets["max_power_w"].isEnabled()


def test_energy_table_shows_qualified_e_eff_only_with_skipping(app, tmp_path):
    from lpbfstudio.gui.app import MainWindow

    lib = Library(tmp_path / "lib")
    lib.copy("Default_Part_40um", "A")
    w = MainWindow(lib)
    w.load_set("A")

    def texts():
        return [" | ".join(w.energy_table.item(r, c).text() for c in range(w.energy_table.columnCount())
                           if w.energy_table.item(r, c)) for r in range(w.energy_table.rowCount())]

    assert not any("E_eff" in t for t in texts())
    w.current.get("hatch").infill.skip_layers = 1
    w.mark_dirty()
    rows = [t for t in texts() if "E_eff" in t]
    assert len(rows) == 1
    assert "Idealized layer-cycle average" in rows[0] and "33.74 J/mm³" in rows[0]
    assert any("InFill" in t and "67.47 J/mm³" in t for t in texts())  # nominal row unchanged


# ---------------------------------------------------------------- DOE dialog (B4)

def _doe_dialog(lib, rows):
    from lpbfstudio.gui.dialogs import DoeDialog

    dlg = DoeDialog(lib.get("P1"), lib)
    for r, (path, text) in enumerate(rows):
        dlg.combos[r].setCurrentIndex(dlg.combos[r].findData(path))
        dlg.table.item(r, 2).setText(text)
    return dlg


def test_doe_dialog_shows_the_count(app, tmp_path):
    lib = Library(tmp_path / "lib")
    lib.copy("Default_Part_40um", "P1")
    dlg = _doe_dialog(lib, [("hatch.infill.hatch_distance_mm", "0.09:0.13:0.02"), ("layer_thickness_mm", "0.03,0.04")])
    assert dlg.summary.text() == "DOE: 3 × 2 = 6 variants"
    assert dlg.preview_list.count() == 6
    assert dlg.generate_btn.isEnabled()


def test_doe_dialog_refuses_too_many_variants_before_building_them(app, tmp_path):
    lib = Library(tmp_path / "lib")
    lib.copy("Default_Part_40um", "P1")
    dlg = _doe_dialog(lib, [("hatch.infill.power_w", "200:209:1"), ("hatch.infill.hatch_distance_mm", "0.05:0.14:0.01"),
                            ("hatch.infill.speed_mm_s", "800:890:10")])
    assert "1000" in dlg.message.text() and "500" in dlg.message.text()
    assert dlg.preview_list.count() == 0 and dlg.variants == []
    assert not dlg.generate_btn.isEnabled()


def test_doe_dialog_rejects_the_same_parameter_twice(app, tmp_path):
    lib = Library(tmp_path / "lib")
    lib.copy("Default_Part_40um", "P1")
    dlg = _doe_dialog(lib, [("hatch.infill.power_w", "250,300"), ("hatch.infill.power_w", "200")])
    assert "hatch.infill.power_w" in dlg.message.text()
    assert not dlg.generate_btn.isEnabled()


def test_doe_dialog_creates_nothing_when_a_name_is_taken(app, tmp_path):
    lib = Library(tmp_path / "lib")
    lib.copy("Default_Part_40um", "P1")
    lib.copy("Default_Part_40um", "P1_DOE2")
    before = sorted(lib.names())
    dlg = _doe_dialog(lib, [("hatch.infill.hatch_distance_mm", "0.09,0.11,0.13")])
    assert "P1_DOE2" in dlg.message.text()
    assert not dlg.generate_btn.isEnabled()
    dlg.create()
    assert sorted(lib.names()) == before and dlg.created == []
    dlg.template.setText("P1_HD{hatch_distance_mm}")
    assert dlg.generate_btn.isEnabled()
    dlg.create()
    assert dlg.created == ["P1_HD0.09", "P1_HD0.11", "P1_HD0.13"]
    assert set(lib.names()) == set(before) | set(dlg.created)


# ---------------------------------------------------------------- DOE dialog (B5)

def test_doe_dialog_preview_reports_too_long_names(app, tmp_path):
    lib = Library(tmp_path / "lib")
    lib.copy("Default_Part_40um", "P1")
    dlg = _doe_dialog(lib, [("hatch.infill.hatch_distance_mm", "0.09,0.11")])
    dlg.template.setText("X" * 300 + "_{i}")  # must not raise from the preview
    assert "too long" in dlg.message.text()
    assert not dlg.generate_btn.isEnabled()


def test_doe_dialog_rejects_decimal_comma(app, tmp_path):
    lib = Library(tmp_path / "lib")
    lib.copy("Default_Part_40um", "P1")
    dlg = _doe_dialog(lib, [("hatch.infill.hatch_distance_mm", "0,05")])
    assert "0.05" in dlg.message.text()
    assert not dlg.generate_btn.isEnabled()


def test_doe_dialog_reports_a_failed_save(app, tmp_path, monkeypatch):
    import errno

    from lpbfstudio import library as libmod
    from lpbfstudio.gui import dialogs

    lib = Library(tmp_path / "lib")
    lib.copy("Default_Part_40um", "P1")
    real, count = libmod._atomic_write, {"n": 0}

    def write(path, text):
        if path.parent.name == "sets":
            count["n"] += 1
            if count["n"] == 2:
                raise OSError(errno.ENOSPC, "No space left on device")
        return real(path, text)

    monkeypatch.setattr(libmod, "_atomic_write", write)
    shown = []
    monkeypatch.setattr(dialogs.QMessageBox, "warning", staticmethod(lambda *a, **k: shown.append(a[2])))
    dlg = _doe_dialog(lib, [("hatch.infill.hatch_distance_mm", "0.09,0.10,0.11")])
    assert dlg.generate_btn.isEnabled()
    dlg.create()  # must not raise
    assert dlg.created == ["P1_DOE1"]
    assert len(shown) == 1
    text = shown[0]
    assert "saved (1): P1_DOE1" in text and "failed (1): P1_DOE2" in text and "No space left" in text
    assert "not attempted (1): P1_DOE3" in text and "not transactional" in text
    assert lib.exists("P1_DOE1") and not lib.exists("P1_DOE2") and not lib.exists("P1_DOE3")


# ---------------------------------------------------------------- DOE dialog (B6)

def test_doe_dialog_shows_parsed_levels(app, tmp_path):
    lib = Library(tmp_path / "lib")
    lib.copy("Default_Part_40um", "P1")
    dlg = _doe_dialog(lib, [("hatch.infill.power_w", "285,5"), ("layer_thickness_mm", "0.03:0.04:0.01")])
    assert dlg.summary.text() == "DOE: 2 × 2 = 4 variants"
    assert dlg.levels.text().splitlines() == ["hatch.infill.power_w: 285, 5", "layer_thickness_mm: 0.03, 0.04"]


def test_doe_dialog_reports_an_interrupted_save_from_disk(app, tmp_path, monkeypatch):
    import os

    from lpbfstudio.gui import dialogs

    lib = Library(tmp_path / "lib")
    lib.copy("Default_Part_40um", "P1")
    real, count = os.replace, {"n": 0}

    def replace(src, dst, *a, **k):
        if os.path.basename(os.path.dirname(os.fspath(dst))) == "sets":
            count["n"] += 1
            if count["n"] == 2:
                real(src, dst, *a, **k)
                raise KeyboardInterrupt
        return real(src, dst, *a, **k)

    shown = []
    monkeypatch.setattr(dialogs.QMessageBox, "warning", staticmethod(lambda *a, **k: shown.append(a[2])))
    dlg = _doe_dialog(lib, [("hatch.infill.hatch_distance_mm", "0.09,0.10,0.11")])
    monkeypatch.setattr(os, "replace", replace)
    try:
        dlg.create()  # must not raise
    except KeyboardInterrupt:
        pytest.fail("KeyboardInterrupt escaped the dialog")
    monkeypatch.undo()
    assert dlg.created == ["P1_DOE1", "P1_DOE2"]
    assert len(shown) == 1 and "saved (2): P1_DOE1, P1_DOE2" in shown[0]
    assert "not attempted (1): P1_DOE3" in shown[0]


# ---------------------------------------------------------------- schema-version guard (B7 phase 1)

def _future_file(lib, name, version=99):
    import json

    from lpbfstudio.defaults import default_sets

    data = json.loads(default_sets()[0].model_dump_json())
    data.update(name=name, locked=False, schema_version=version)
    f = lib.sets_dir / f"{name}.json"
    f.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return f


def test_gui_explains_a_refused_schema_version(app, tmp_path):
    from lpbfstudio.gui.app import MainWindow

    lib = Library(tmp_path / "lib")
    f = _future_file(lib, "Future")
    before = f.read_bytes()
    w = MainWindow(lib)
    w.refresh_library()
    msg = w.statusBar().currentMessage()
    assert "Future.json" in msg and "version 99" in msg and "not supported" in msg
    assert "Future" not in [w.library_list.item(i).data(0x0100) for i in range(w.library_list.count())]
    assert f.read_bytes() == before


def test_gui_import_explains_a_refused_schema_version(app, tmp_path, monkeypatch):
    import json

    from lpbfstudio.defaults import default_sets
    from lpbfstudio.gui import app as appmod

    lib = Library(tmp_path / "lib")
    src = tmp_path / "future.json"
    data = json.loads(default_sets()[0].model_dump_json())
    data.update(name="From_Future", schema_version=99)
    src.write_text(json.dumps(data))
    shown = []
    monkeypatch.setattr(appmod.QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(src), "")))
    monkeypatch.setattr(appmod.QMessageBox, "warning", staticmethod(lambda *a, **k: shown.append(a[2])))
    w = appmod.MainWindow(lib)
    w.import_set()
    assert len(shown) == 1 and "version 99" in shown[0] and "not supported" in shown[0]
    assert not lib.exists("From_Future")


# ---------------------------------------------------------------- contour-pass ids (B7 Phase 2, S1+S2)

def _no_dialogs(monkeypatch):
    """Record warnings instead of opening modal dialogs, which would hang an offscreen run."""
    from lpbfstudio.gui import app as appmod

    shown = []
    monkeypatch.setattr(appmod.QMessageBox, "warning", staticmethod(lambda *a, **k: shown.append(a[2])))
    monkeypatch.setattr(appmod.QMessageBox, "information", staticmethod(lambda *a, **k: shown.append(a[2])))
    monkeypatch.setattr(appmod.QMessageBox, "critical", staticmethod(lambda *a, **k: shown.append(a[2])))
    return shown


def _v2_set(lib, name="V2", ids=("3f9c2e1a", "b7d10c44")):
    import json

    from lpbfstudio.defaults import default_sets

    data = json.loads(default_sets()[0].model_dump_json())
    data.update(name=name, locked=False, schema_version=2)
    for et in data["exposure_types"]:
        if et["kind"] == "contour":
            et["passes"] = [{"id": i, **p} for i, p in zip(ids, et["passes"])]
    (lib.sets_dir / f"{name}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def test_gui_pass_id_is_shown_read_only(app, tmp_path, monkeypatch):
    from lpbfstudio.gui.app import MainWindow

    _no_dialogs(monkeypatch)
    lib = Library(tmp_path / "lib")
    _v2_set(lib)
    w = MainWindow(lib)
    w.load_set("V2")
    w.rebuild_tree(keep=w.current.get("contour").passes[0])
    field = w.form.widgets["id"]
    assert not field.isEnabled() and field.text() == "3f9c2e1a"
    field.setText("cafebabe")
    field.editingFinished.emit()
    assert w.current.get("contour").passes[0].id == "3f9c2e1a"
    assert "schema_version" not in w.form.widgets
    lib.copy("Default_Part_40um", "V1")
    w.load_set("V1")
    w.rebuild_tree(keep=w.current.get("contour").passes[0])
    assert "id" not in w.form.widgets  # format 1 passes have no id


def test_gui_add_and_remove_pass_keep_ids_and_format(app, tmp_path, monkeypatch):
    import json
    import re

    from lpbfstudio.gui.app import MainWindow

    shown = _no_dialogs(monkeypatch)
    lib = Library(tmp_path / "lib")
    _v2_set(lib)
    w = MainWindow(lib)
    w.load_set("V2")
    contour = w.current.get("contour")
    w.rebuild_tree(keep=contour)
    w.add_contour_pass()
    ids = [p.id for p in contour.passes]
    assert ids[:2] == ["3f9c2e1a", "b7d10c44"] and re.fullmatch(r"[0-9a-f]{8}", ids[2]) and len(set(ids)) == 3
    w.rebuild_tree(keep=contour.passes[0])
    w.remove_contour_pass()
    assert [p.id for p in contour.passes] == ids[1:]
    assert w.save_current(), shown
    data = json.loads((lib.sets_dir / "V2.json").read_text())
    passes = next(et for et in data["exposure_types"] if et["kind"] == "contour")["passes"]
    assert data["schema_version"] == 2 and [p["id"] for p in passes] == ids[1:]

    lib.copy("Default_Part_40um", "V1")
    w.load_set("V1")
    contour = w.current.get("contour")
    w.rebuild_tree(keep=contour)
    w.add_contour_pass()
    assert [p.id for p in contour.passes] == [None, None, None]
    assert w.save_current(), shown
    data = json.loads((lib.sets_dir / "V1.json").read_text())
    assert data["schema_version"] == 1 and '"id"' not in (lib.sets_dir / "V1.json").read_text()


def test_gui_new_set_is_format_1(app, tmp_path, monkeypatch):
    from lpbfstudio.gui import app as appmod
    from lpbfstudio.models import SetCategory

    lib = Library(tmp_path / "lib")
    shown = _no_dialogs(monkeypatch)
    monkeypatch.setattr(appmod.QInputDialog, "getText", staticmethod(lambda *a, **k: ("Fresh", True)))
    w = appmod.MainWindow(lib)
    w.new_set(SetCategory.PART)
    assert shown == [] and lib.get("Fresh").schema_version == 1
    assert '"id"' not in (lib.sets_dir / "Fresh.json").read_text()


def test_gui_has_no_pass_reorder_or_duplicate_controls(app, tmp_path):
    from lpbfstudio.gui.app import MainWindow

    from PySide6.QtWidgets import QPushButton

    w = MainWindow(Library(tmp_path / "lib"))
    assert {n for n in dir(w) if "pass" in n.lower()} == {
        "add_contour_pass", "add_pass_btn", "remove_contour_pass", "remove_pass_btn"}
    assert {b.text() for b in w.findChildren(QPushButton) if "pass" in b.text()} == {
        "Add contour pass", "Remove contour pass"}
