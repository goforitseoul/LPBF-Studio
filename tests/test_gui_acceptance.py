"""Milestone C acceptance tests of existing editor behaviour, offscreen: C-AC4 (actions on the selected set), C-AC5 (the
unsaved-change prompt) and C-AC6 (the compare, estimate and calculator dialogs). Test-only: they pin what the editor does
today and add nothing to it."""

import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.environ.get("LPBF_REQUIRE_GUI_TESTS"):
    from PySide6 import QtWidgets
else:
    QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from lpbfstudio.library import Library  # noqa: E402
from lpbfstudio.models import ProcessSettings  # noqa: E402

B = QtWidgets.QMessageBox.StandardButton


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def win(app, tmp_path, monkeypatch):
    from lpbfstudio.gui import app as appmod

    monkeypatch.setattr(appmod.QMessageBox, "warning", staticmethod(lambda *a, **k: pytest.fail(f"warning shown: {a[2:]}")))
    lib = Library(tmp_path / "lib")
    lib.copy("Default_Part_40um", "P1")
    lib.copy("Default_Part_40um", "P2")
    w = appmod.MainWindow(lib)
    w.refresh_library(select="P1")
    w.load_set("P1")
    yield w
    w.dirty = False
    w.close()


def answer(monkeypatch, text, ok=True):
    from lpbfstudio.gui import app as appmod

    monkeypatch.setattr(appmod.QInputDialog, "getText", staticmethod(lambda *a, **k: (text, ok)))


def make_dirty(w):
    infill = w.tree.topLevelItem(0).child(0).child(0)
    w.tree.setCurrentItem(infill)
    w.form.widgets["power_w"].setValue(299)
    assert w.dirty


# --------------------------------------------------------------------------- C-AC4


def test_copy_action_copies_the_selected_set(win, monkeypatch):
    answer(monkeypatch, "C1")
    win.copy_set()
    assert win.lib.exists("C1") and win.current.name == "C1"


def test_copy_action_cancelled_does_nothing(win, monkeypatch):
    answer(monkeypatch, "C1", ok=False)
    names = win.lib.names()
    win.copy_set()
    assert win.lib.names() == names and win.current.name == "P1"


def test_rename_action_renames_the_selected_set(win, monkeypatch):
    answer(monkeypatch, "R1")
    win.rename_set()
    assert win.lib.exists("R1") and not win.lib.exists("P1") and win.current.name == "R1"


def test_rename_action_is_unavailable_for_a_locked_set(win, monkeypatch):
    answer(monkeypatch, "R1")
    win.load_set("Default_Part_40um")
    win.rename_set()
    assert not win.lib.exists("R1")


def checks_rows(win):
    return [(win.checks_table.item(r, 1).text(), win.checks_table.item(r, 3).text()) for r in range(win.checks_table.rowCount())]


def expected_rows(win, es):
    issues = win.lib.validate(es, win.process)
    return [(i.code, i.message) for i in issues] or [("", "No issues found.")]


def test_validate_shows_the_checks_of_the_selected_set(win):
    win.refresh_analysis()
    assert checks_rows(win) == expected_rows(win, win.current)
    assert win.analysis.tabText(1).startswith("Checks")


def test_validate_follows_the_selected_set(win):
    es = win.lib.get("P2")
    es.get("hatch").infill.power_w = 450.0  # above the machine maximum of 400 W: an error only P2 has
    win.lib.save(es)
    win.refresh_analysis()
    p1 = checks_rows(win)
    win.load_set("P2")
    p2 = checks_rows(win)
    assert p2 == expected_rows(win, win.current) and p2 != p1
    assert ("MCH-001", "450.0 W exceeds the machine maximum of 400.0 W") in p2 and all(code != "MCH-001" for code, _ in p1)


@pytest.mark.parametrize("action", ["copy", "rename"])
def test_copy_and_rename_to_a_taken_name_warn_and_change_nothing(win, monkeypatch, action):
    from lpbfstudio.gui import app as appmod

    warned = []
    monkeypatch.setattr(appmod.QMessageBox, "warning", staticmethod(lambda *a, **k: warned.append(a[2])))
    answer(monkeypatch, "P2")
    before = {n: win.lib.get(n).model_dump() for n in win.lib.names()}
    win.copy_set() if action == "copy" else win.rename_set()
    assert warned and "P2" in warned[0]
    assert {n: win.lib.get(n).model_dump() for n in win.lib.names()} == before and win.current.name == "P1"


def test_compare_action_opens_with_the_selected_set(win, monkeypatch):
    from lpbfstudio.gui import app as appmod

    seen = []
    monkeypatch.setattr(appmod.CompareDialog, "exec", lambda self: seen.append(self.a.currentText()) or 0)
    win.open_compare()
    assert seen == ["P1"]


@pytest.mark.parametrize("suffix", [".json", ".csv"])
def test_export_action_writes_the_selected_set(win, monkeypatch, tmp_path, suffix):
    from lpbfstudio.gui import app as appmod

    out = tmp_path / f"out{suffix}"
    monkeypatch.setattr(appmod.QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(out), "")))
    win.export_set()
    assert out.exists()
    if suffix == ".json":
        assert json.loads(out.read_text(encoding="utf-8"))["name"] == "P1"
    else:
        assert "power_w" in out.read_text(encoding="utf-8")


def test_editor_export_round_trips_values_and_schema_version(win, monkeypatch, tmp_path):
    from lpbfstudio.gui import app as appmod

    out = tmp_path / "round.json"
    monkeypatch.setattr(appmod.QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(out), "")))
    win.export_set()
    other = Library(tmp_path / "other")
    imported = other.import_file(out)
    assert imported.schema_version == win.current.schema_version
    assert imported.model_dump() == win.lib.get("P1").model_dump()


def test_export_cancelled_writes_nothing(win, monkeypatch, tmp_path):
    from lpbfstudio.gui import app as appmod

    monkeypatch.setattr(appmod.QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: ("", "")))
    win.export_set()
    assert list(tmp_path.glob("out*")) == []


# --------------------------------------------------------------------------- C-AC5


def prompt(monkeypatch, reply):
    from lpbfstudio.gui import app as appmod

    asked = []
    monkeypatch.setattr(appmod.QMessageBox, "question", staticmethod(lambda *a, **k: asked.append(a[2]) or reply))
    return asked


def test_switching_sets_with_unsaved_changes_and_cancel_stays(win, monkeypatch):
    make_dirty(win)
    asked = prompt(monkeypatch, B.Cancel)
    items = [win.library_list.item(r) for r in range(win.library_list.count())]
    target = next(i for i in items if i.text().startswith("P2"))
    win.library_list.setCurrentItem(target)
    assert asked == ["Save changes to P1?"] and win.current.name == "P1" and win.dirty


def test_switching_sets_and_discard_loads_the_other(win, monkeypatch):
    make_dirty(win)
    prompt(monkeypatch, B.Discard)
    items = [win.library_list.item(r) for r in range(win.library_list.count())]
    win.library_list.setCurrentItem(next(i for i in items if i.text().startswith("P2")))
    assert win.current.name == "P2" and not win.dirty
    assert win.lib.get("P1").get("hatch").infill.power_w != 299  # discarded


@pytest.fixture
def qt_messages():
    """Qt's own warnings during a test, such as "shared QObject was deleted directly"."""
    from PySide6.QtCore import qInstallMessageHandler

    seen = []
    old = qInstallMessageHandler(lambda mode, context, message: seen.append(message))
    yield seen
    qInstallMessageHandler(old)


def switch_with_save(win, monkeypatch):
    make_dirty(win)
    prompt(monkeypatch, B.Save)
    items = [win.library_list.item(r) for r in range(win.library_list.count())]
    win.library_list.setCurrentItem(next(i for i in items if i.text().startswith("P2")))  # D2 raised RuntimeError here


def test_switching_sets_and_save_saves_first(win, monkeypatch):
    switch_with_save(win, monkeypatch)
    assert win.lib.get("P1").get("hatch").infill.power_w == 299


def test_switching_sets_and_save_then_opens_the_other(win, monkeypatch, qt_messages):
    """D2 (fixed 2026-11-10): the switch used to read a list item the save had deleted, and P1 stayed open."""
    switch_with_save(win, monkeypatch)
    assert win.current.name == "P2" and not win.dirty
    assert win.library_list.currentItem().data(appmod_role()) == "P2"  # what is shown is what is open
    assert win.lib.get("P2").model_dump() == win.current.model_dump()
    assert not [m for m in qt_messages if "QObject" in m or "deleted" in m]


def test_switching_sets_when_the_save_fails_stays_on_the_edited_set(win, monkeypatch):
    from lpbfstudio.gui import app as appmod

    warned = []
    monkeypatch.setattr(appmod.QMessageBox, "warning", staticmethod(lambda *a, **k: warned.append(a[2])))
    monkeypatch.setattr(win.lib, "save", lambda es: (_ for _ in ()).throw(appmod.LibraryError("disk full")))
    make_dirty(win)
    prompt(monkeypatch, B.Save)
    items = [win.library_list.item(r) for r in range(win.library_list.count())]
    win.library_list.setCurrentItem(next(i for i in items if i.text().startswith("P2")))
    assert warned and win.current.name == "P1" and win.dirty
    assert win.library_list.currentItem().data(appmod_role()) == "P1"


def appmod_role():
    from lpbfstudio.gui import app as appmod

    return appmod.OBJ_ROLE


def test_closing_with_unsaved_changes_asks_and_cancel_keeps_the_window(win, monkeypatch):
    from PySide6.QtGui import QCloseEvent

    make_dirty(win)
    asked = prompt(monkeypatch, B.Cancel)
    event = QCloseEvent()
    win.closeEvent(event)
    assert asked and not event.isAccepted()
    prompt(monkeypatch, B.Discard)
    event = QCloseEvent()
    win.closeEvent(event)
    assert event.isAccepted()


@pytest.mark.parametrize("action", ["new", "copy"])
def test_new_and_copy_ask_first_and_cancel_does_nothing(win, monkeypatch, action):
    from lpbfstudio.models import SetCategory

    make_dirty(win)
    asked = prompt(monkeypatch, B.Cancel)
    answer(monkeypatch, "X1")
    names = win.lib.names()
    win.new_set(SetCategory.PART) if action == "new" else win.copy_set()
    assert asked == ["Save changes to P1?"] and win.lib.names() == names and win.dirty


@pytest.mark.parametrize("action", ["new", "copy"])
def test_new_and_copy_with_save_save_first_then_act(win, monkeypatch, action):
    from lpbfstudio.models import SetCategory

    make_dirty(win)
    prompt(monkeypatch, B.Save)
    answer(monkeypatch, "X1")
    win.new_set(SetCategory.PART) if action == "new" else win.copy_set()
    assert win.lib.get("P1").get("hatch").infill.power_w == 299
    assert win.lib.exists("X1") and win.current.name == "X1" and not win.dirty
    if action == "copy":
        assert win.lib.get("X1").get("hatch").infill.power_w == 299  # the copy is of the saved set


@pytest.mark.parametrize("action", ["new", "copy"])
def test_new_and_copy_with_discard_drop_the_edit_then_act(win, monkeypatch, action):
    from lpbfstudio.models import SetCategory

    before = win.lib.get("P1").get("hatch").infill.power_w
    make_dirty(win)
    prompt(monkeypatch, B.Discard)
    answer(monkeypatch, "X1")
    win.new_set(SetCategory.PART) if action == "new" else win.copy_set()
    assert win.lib.get("P1").get("hatch").infill.power_w == before
    assert win.lib.exists("X1") and win.current.name == "X1" and not win.dirty
    if action == "copy":
        assert win.lib.get("X1").get("hatch").infill.power_w == before


def test_closing_with_save_saves_and_closes(win, monkeypatch):
    from PySide6.QtGui import QCloseEvent

    make_dirty(win)
    prompt(monkeypatch, B.Save)
    event = QCloseEvent()
    win.closeEvent(event)
    assert event.isAccepted() and win.lib.get("P1").get("hatch").infill.power_w == 299


def test_no_prompt_without_changes(win, monkeypatch):
    asked = prompt(monkeypatch, B.Cancel)
    answer(monkeypatch, "C2")
    win.copy_set()
    assert asked == [] and win.lib.exists("C2")


# --------------------------------------------------------------------------- C-AC6


def test_compare_dialog_lists_the_differences(win):
    from lpbfstudio.gui.dialogs import CompareDialog

    es = win.lib.get("P2")
    es.description = "different"
    win.lib.save(es)
    dlg = CompareDialog(win.lib, "P1", win)
    dlg.b.setCurrentText("P2")
    rows = {dlg.table.item(r, 0).text(): (dlg.table.item(r, 1).text(), dlg.table.item(r, 2).text())
            for r in range(dlg.table.rowCount())}
    assert rows["description"][1] == "different"
    dlg.b.setCurrentText("P1")
    assert dlg.table.rowCount() == 0  # a set compared with itself


def test_compare_dialog_with_no_current_set(win):
    from lpbfstudio.gui.dialogs import CompareDialog

    dlg = CompareDialog(win.lib, None, win)
    assert dlg.a.count() == len(win.lib.names())


def test_estimate_dialog_calculates_a_plan(win):
    from lpbfstudio.gui.dialogs import EstimateDialog

    dlg = EstimateDialog(win.lib, ProcessSettings(), "P1", win)
    dlg.add_segment()  # continues from the last segment
    assert dlg.table.rowCount() == 2 and dlg.table.cellWidget(1, 0).value() == dlg.table.cellWidget(0, 1).value()
    dlg.calculate()
    assert "Total" in dlg.result.text() and "P1" in dlg.result.text()


def test_estimate_dialog_says_it_is_a_comparison_estimate(win):
    from lpbfstudio.gui.dialogs import EstimateDialog

    dlg = EstimateDialog(win.lib, ProcessSettings(), "P1", win)
    texts = [w.text() for w in dlg.findChildren(QtWidgets.QLabel)]
    assert any("compare sets and segmentation plans" in t and "jumps and delays are not modelled" in t for t in texts)


def test_estimate_dialog_reports_an_invalid_plan(win):
    from lpbfstudio.gui.dialogs import EstimateDialog

    dlg = EstimateDialog(win.lib, ProcessSettings(), "P1", win)
    dlg.add_segment(10, 5, "P1")  # overlaps and runs backwards
    dlg.calculate()
    assert "c0392b" in dlg.result.text() and "Total" not in dlg.result.text()


def test_tools_dialog_calculates_ridge_and_power_table(win):
    from lpbfstudio import calc
    from lpbfstudio.gui.dialogs import ToolsDialog

    dlg = ToolsDialog(win.current, win)
    dlg.r_angle.setValue(60.0)
    assert dlg.r_out.text() == f"<b>{calc.ridge_for_angle(dlg.r_thick.value(), 60.0):.3f} mm</b>"
    dlg.p0.setValue(300.0)
    rows = calc.power_reduction_table(300.0, dlg.rf.value(), dlg.h0.value())
    assert dlg.p_table.rowCount() == len(rows) and dlg.p_table.item(0, 1).text() == f"{rows[0][1]:.1f}"


def test_tools_dialog_without_a_set_uses_its_defaults(win):
    from lpbfstudio.gui.dialogs import ToolsDialog

    dlg = ToolsDialog(None, win)
    assert dlg.p0.value() == 285.0 and dlg.r_thick.value() == pytest.approx(0.09)


@pytest.mark.parametrize("opener", ["open_estimate", "open_tools"])
def test_editor_opens_each_dialog(win, monkeypatch, opener):
    from lpbfstudio.gui import app as appmod

    opened = []
    for cls in ("EstimateDialog", "ToolsDialog"):
        monkeypatch.setattr(getattr(appmod, cls), "exec", lambda self, c=cls: opened.append(c) or 0)
    getattr(win, opener)()
    assert opened == ["EstimateDialog" if opener == "open_estimate" else "ToolsDialog"]
