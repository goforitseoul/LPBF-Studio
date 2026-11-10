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


def test_validate_shows_the_checks_of_the_selected_set(win):
    win.refresh_analysis()
    rows = [win.checks_table.item(r, 3).text() for r in range(win.checks_table.rowCount())]
    assert rows  # one row per issue, or a "No issues found." row
    assert win.analysis.tabText(1).startswith("Checks")


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


def switch_with_save(win, monkeypatch):
    make_dirty(win)
    prompt(monkeypatch, B.Save)
    items = [win.library_list.item(r) for r in range(win.library_list.count())]
    try:
        win.library_list.setCurrentItem(next(i for i in items if i.text().startswith("P2")))
    except RuntimeError:
        pass  # see the expected failure below


def test_switching_sets_and_save_saves_first(win, monkeypatch):
    switch_with_save(win, monkeypatch)
    assert win.lib.get("P1").get("hatch").infill.power_w == 299 and not win.dirty


@pytest.mark.xfail(strict=True, reason="known defect: Save refreshes the list, deleting the item being switched to, so the "
                                       "switch stops with a RuntimeError and P1 stays open")
def test_switching_sets_and_save_then_opens_the_other(win, monkeypatch):
    switch_with_save(win, monkeypatch)
    assert win.current.name == "P2"


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
    dlg = CompareDialog(win.lib, "P1")
    dlg.b.setCurrentText("P2")
    rows = {dlg.table.item(r, 0).text(): (dlg.table.item(r, 1).text(), dlg.table.item(r, 2).text())
            for r in range(dlg.table.rowCount())}
    assert rows["description"][1] == "different"
    dlg.b.setCurrentText("P1")
    assert dlg.table.rowCount() == 0  # a set compared with itself


def test_compare_dialog_with_no_current_set(win):
    from lpbfstudio.gui.dialogs import CompareDialog

    dlg = CompareDialog(win.lib, None)
    assert dlg.a.count() == len(win.lib.names())


def test_estimate_dialog_calculates_a_plan(win):
    from lpbfstudio.gui.dialogs import EstimateDialog

    dlg = EstimateDialog(win.lib, ProcessSettings(), "P1")
    dlg.add_segment()  # continues from the last segment
    assert dlg.table.rowCount() == 2 and dlg.table.cellWidget(1, 0).value() == dlg.table.cellWidget(0, 1).value()
    dlg.calculate()
    assert "Total" in dlg.result.text() and "P1" in dlg.result.text()


def test_estimate_dialog_reports_an_invalid_plan(win):
    from lpbfstudio.gui.dialogs import EstimateDialog

    dlg = EstimateDialog(win.lib, ProcessSettings(), "P1")
    dlg.add_segment(10, 5, "P1")  # overlaps and runs backwards
    dlg.calculate()
    assert "c0392b" in dlg.result.text() and "Total" not in dlg.result.text()


def test_tools_dialog_calculates_ridge_and_power_table(win):
    from lpbfstudio import calc
    from lpbfstudio.gui.dialogs import ToolsDialog

    dlg = ToolsDialog(win.current)
    dlg.r_angle.setValue(60.0)
    assert dlg.r_out.text() == f"<b>{calc.ridge_for_angle(dlg.r_thick.value(), 60.0):.3f} mm</b>"
    dlg.p0.setValue(300.0)
    rows = calc.power_reduction_table(300.0, dlg.rf.value(), dlg.h0.value())
    assert dlg.p_table.rowCount() == len(rows) and dlg.p_table.item(0, 1).text() == f"{rows[0][1]:.1f}"


def test_tools_dialog_without_a_set_uses_its_defaults(win):
    from lpbfstudio.gui.dialogs import ToolsDialog

    dlg = ToolsDialog(None)
    assert dlg.p0.value() == 285.0 and dlg.r_thick.value() == pytest.approx(0.09)


@pytest.mark.parametrize("opener", ["open_estimate", "open_tools"])
def test_editor_opens_each_dialog(win, monkeypatch, opener):
    from lpbfstudio.gui import app as appmod

    opened = []
    for cls in ("EstimateDialog", "ToolsDialog"):
        monkeypatch.setattr(getattr(appmod, cls), "exec", lambda self, c=cls: opened.append(c) or 0)
    getattr(win, opener)()
    assert opened == ["EstimateDialog" if opener == "open_estimate" else "ToolsDialog"]
