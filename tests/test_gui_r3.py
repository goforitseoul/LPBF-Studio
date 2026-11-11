"""R3: C-AC1 (selecting a check selects the affected node and focuses its field) and C-AC2 (each field shows its unit
and allowed range, and its guard level as text as well as colour). Offscreen."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.environ.get("LPBF_REQUIRE_GUI_TESTS"):
    from PySide6 import QtWidgets
else:
    QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from lpbfstudio.library import Library  # noqa: E402
from lpbfstudio.models import Guard, Hatch, InFill  # noqa: E402

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
    es = lib.get("P1")
    es.get("hatch").infill.power_w = 450.0  # MCH-001: above the generic machine's 400 W
    lib.save(es)
    w = appmod.MainWindow(lib)
    w.show()  # focus needs a shown window, even offscreen
    w.refresh_library(select="P1")
    w.load_set("P1")
    app.processEvents()
    yield w
    w.dirty = False
    w.close()


def row_of(win, code):
    for r in range(win.checks_table.rowCount()):
        if win.checks_table.item(r, 1).text() == code:
            return r
    raise AssertionError(f"no {code} row in {[win.checks_table.item(r, 1).text() for r in range(win.checks_table.rowCount())]}")


def select_row(app, win, row):
    win.checks_table.selectRow(row)
    app.processEvents()


# --------------------------------------------------------------------------- C-AC1


def test_selecting_a_check_selects_its_node_and_focuses_its_field(app, win):
    win.tree.setCurrentItem(win.tree.topLevelItem(0))  # start elsewhere: the set properties
    app.processEvents()
    select_row(app, win, row_of(win, "MCH-001"))
    node = win.tree.currentItem().data(0, win_obj_role())
    assert node is win.current.get("hatch").infill  # the very object the check is about
    assert win.form.obj is node
    assert QtWidgets.QApplication.focusWidget() is win.form.widgets["power_w"]
    assert win.form.widgets["power_w"].value() == 450.0


def test_navigation_keeps_the_check_selected_and_the_set_unchanged(app, win):
    before = win.current.model_dump()
    select_row(app, win, row_of(win, "MCH-001"))
    assert win.checks_table.currentRow() == row_of(win, "MCH-001")  # kept although the table was rebuilt
    assert win.current.model_dump() == before and not win.dirty


def test_navigation_does_not_touch_unsaved_changes(app, win):
    infill = win.current.get("hatch").infill
    edge = next(i for i in iter_items(win) if type(i.data(0, win_obj_role())).__name__ == "Edge")
    win.tree.setCurrentItem(edge)
    app.processEvents()
    first = next(n for n, w in win.form.widgets.items() if isinstance(w, QtWidgets.QDoubleSpinBox))
    win.form.widgets[first].setValue(win.form.widgets[first].value() + 1)
    assert win.dirty
    edited = win.current.model_dump()
    select_row(app, win, row_of(win, "MCH-001"))
    assert win.dirty and win.current.model_dump() == edited and win.form.obj is infill


def test_navigation_reaches_a_node_that_is_scrolled_out_of_view(app, win):
    win.tree.resize(200, 60)  # only a row or two visible
    win.tree.scrollToTop()
    app.processEvents()
    select_row(app, win, row_of(win, "MCH-001"))
    item = win.tree.currentItem()
    assert item.data(0, win_obj_role()) is win.current.get("hatch").infill
    assert win.tree.visualItemRect(item).intersects(win.tree.viewport().rect())  # scrolled into view


def test_a_check_with_no_editor_field_says_so_and_changes_nothing(app, win, monkeypatch):
    from lpbfstudio.gui import app as appmod

    win.tree.setCurrentItem(win.tree.topLevelItem(0))
    app.processEvents()
    form_before = win.form
    win.checks_table.setRowCount(0)
    win._add_row(win.checks_table, ["warning", "XXX-001", "process.recoat_time_s", "not a set parameter"])
    win.checks_table.item(0, 0).setData(appmod.CHECK_ROLE, ("process", "recoat_time_s"))
    select_row(app, win, 0)
    assert win.form is form_before and win.tree.currentItem() is win.tree.topLevelItem(0)
    assert "not a parameter of this set" in win.statusBar().currentMessage()


@pytest.mark.parametrize("target", [("parameter", "hatch.infill.no_such_field"), ("parameter", "")])
def test_an_unknown_target_is_reported_and_changes_nothing(app, win, target):
    from lpbfstudio.gui import app as appmod

    win.tree.setCurrentItem(win.tree.topLevelItem(0))
    app.processEvents()
    win.checks_table.setRowCount(0)
    win._add_row(win.checks_table, ["error", "XXX-002", target[1], "stale"])
    win.checks_table.item(0, 0).setData(appmod.CHECK_ROLE, target)
    select_row(app, win, 0)
    assert win.tree.currentItem() is win.tree.topLevelItem(0)
    assert win.statusBar().currentMessage()


def test_a_parameter_hidden_in_the_tree_is_reported(app, win):
    """Pattern settings the selected pattern does not use are not in the tree (chess when stripes is chosen)."""
    from lpbfstudio.gui import app as appmod

    assert win.current.get("hatch").infill.pattern.value == "stripes"
    win.checks_table.setRowCount(0)
    win._add_row(win.checks_table, ["warning", "XXX-003", "hatch.infill.chess.square_width_mm", "hidden"])
    win.checks_table.item(0, 0).setData(appmod.CHECK_ROLE, ("parameter", "hatch.infill.chess.square_width_mm"))
    current = win.tree.currentItem()
    select_row(app, win, 0)
    assert win.tree.currentItem() is current and "not shown" in win.statusBar().currentMessage()


def test_the_ok_row_navigates_nowhere(app, win):
    es = win.lib.get("P1")
    es.get("hatch").infill.power_w = 285.0
    win.lib.save(es)
    win.load_set("P1")
    rows = [win.checks_table.item(r, 3).text() for r in range(win.checks_table.rowCount())]
    if rows != ["No issues found."]:
        pytest.skip("the default set has other findings; the OK row is not shown")
    current = win.tree.currentItem()
    select_row(app, win, 0)
    assert win.tree.currentItem() is current


def test_every_parameter_finding_of_the_shipped_sets_navigates(app, win):
    """Each parameter-scope finding of every shipped set reaches its field, or explains why not."""
    for name in win.lib.names():
        win.load_set(name)
        for r in range(win.checks_table.rowCount()):
            target = win.checks_table.item(r, 0).data(win_check_role())
            if not target or target[0] != "parameter":
                continue
            win.statusBar().clearMessage()
            select_row(app, win, r)
            focused = QtWidgets.QApplication.focusWidget()
            assert focused in win.form.widgets.values() or win.statusBar().currentMessage(), (name, target)


# --------------------------------------------------------------------------- C-AC2


def test_guard_level_is_shown_as_text(app, win):
    infill = win.current.get("hatch").infill
    win.tree.setCurrentItem(item_for(win, infill))
    app.processEvents()
    meta = type(infill).model_fields["power_w"].json_schema_extra
    assert meta["guard"] == Guard.CAUTION
    assert win.form.guard_labels["power_w"].text() == "caution"
    assert win.form.guard_labels["power_w"].isVisibleTo(win.form)


def test_critical_and_free_fields(app, win):
    from lpbfstudio.gui.forms import ModelForm

    for model in (InFill(), Hatch(), win.current):
        form = ModelForm(model)
        for name, label in form.guard_labels.items():
            meta = type(model).model_fields[name].json_schema_extra or {}
            guard = str(getattr(meta.get("guard"), "value", meta.get("guard") or "free"))
            assert label.text() == ("" if guard == "free" else guard), (type(model).__name__, name)
    assert any(lbl.text() == "critical" for lbl in ModelForm(InFill()).guard_labels.values()) or \
        any(lbl.text() == "critical" for m in (Hatch(), win.current) for lbl in ModelForm(m).guard_labels.values())


def test_fields_show_unit_and_range(app, win):
    infill = win.current.get("hatch").infill
    win.tree.setCurrentItem(item_for(win, infill))
    app.processEvents()
    spin = win.form.widgets["power_w"]
    meta = type(infill).model_fields["power_w"].json_schema_extra
    assert spin.suffix() == " W" and meta["unit"] == "W"
    assert (spin.minimum(), spin.maximum()) == (0.0, 2000.0)


# --------------------------------------------------------------------------- helpers


def win_obj_role():
    from lpbfstudio.gui import app as appmod

    return appmod.OBJ_ROLE


def win_check_role():
    from lpbfstudio.gui import app as appmod

    return appmod.CHECK_ROLE


def iter_items(win):
    stack = [win.tree.topLevelItem(i) for i in range(win.tree.topLevelItemCount())]
    while stack:
        item = stack.pop()
        yield item
        stack.extend(item.child(i) for i in range(item.childCount()))


def item_for(win, obj):
    return next(i for i in iter_items(win) if i.data(0, win_obj_role()) is obj)
