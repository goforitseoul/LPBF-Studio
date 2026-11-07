"""LPBF Studio desktop editor (PySide6)."""

from __future__ import annotations

import sys
from pathlib import Path

from pydantic import BaseModel, ValidationError
from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QIcon, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFileDialog, QFrame, QHBoxLayout, QHeaderView,
    QInputDialog, QLabel, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPushButton, QScrollArea, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QToolBar,
    QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .. import APP_NAME, __version__, calc
from ..defaults import new_set
from ..library import Library, LibraryError, export_json
from ..models import (
    EXPOSURE_CLASSES, MAX_EXPOSURE_TYPES, PASS_ID_VERSION, Contour, ContourPass, ExposureSet, HatchPattern,
    MachineProfile, ProcessSettings, SetCategory, allowed_kinds, new_pass_id,
)
from ..paths import assign_field
from ..validation import Severity, validate_set
from .dialogs import CompareDialog, DoeDialog, EstimateDialog, ToolsDialog
from .forms import ModelForm

SEVERITY_COLOR = {Severity.ERROR: "#c0392b", Severity.WARNING: "#c98a00", Severity.INFO: "#2e86c1"}
OBJ_ROLE = Qt.ItemDataRole.UserRole


def color_icon(color: str, size: int = 14) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(QColor(color))
    return QIcon(pm)


class SettingsDialog(QDialog):
    """Edits a copy of a settings model; ``result_model`` holds it after OK."""

    def __init__(self, model: BaseModel, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.result_model = model.model_copy(deep=True)
        layout = QVBoxLayout(self)
        form = ModelForm(self.result_model)
        form.error.connect(lambda m: QMessageBox.warning(self, title, m))
        layout.addWidget(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class MainWindow(QMainWindow):
    def __init__(self, library: Library) -> None:
        super().__init__()
        self.lib = library
        self.machines: dict[str, MachineProfile] = library.load_machines()
        self.process: ProcessSettings = library.load_process()
        self.current: ExposureSet | None = None
        self.dirty = False
        self.form: ModelForm | None = None
        self.setWindowTitle(APP_NAME)
        self.resize(1360, 820)
        self._build_actions()
        self._build_ui()
        self.refresh_library()
        if self.library_list.count():
            self.library_list.setCurrentRow(0)

    # ================================================================== layout

    def _build_actions(self) -> None:
        tb = QToolBar("Main")
        tb.setMovable(False)
        tb.setIconSize(QSize(16, 16))
        self.addToolBar(tb)

        def act(text: str, slot, shortcut: str | None = None, tip: str = "") -> QAction:
            a = QAction(text, self)
            a.triggered.connect(slot)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            a.setToolTip(tip or text)
            tb.addAction(a)
            return a

        self.act_new = act("New part set", lambda: self.new_set(SetCategory.PART), "Ctrl+N")
        self.act_new_support = act("New support set", lambda: self.new_set(SetCategory.SUPPORT))
        self.act_copy = act("Copy", self.copy_set, "Ctrl+D", "Copy the selected set (use this to edit a locked set)")
        self.act_rename = act("Rename", self.rename_set)
        self.act_delete = act("Delete", self.delete_set)
        tb.addSeparator()
        self.act_save = act("Save", self.save_current, "Ctrl+S")
        self.act_revert = act("Revert", self.revert_current)
        tb.addSeparator()
        act("DOE…", self.open_doe, tip="Generate parameter-set variants (design of experiments)")
        act("Compare…", self.open_compare)
        act("Build-time estimate…", self.open_estimate)
        act("Calculators…", self.open_tools)
        tb.addSeparator()
        act("Process settings…", self.edit_process)
        act("Machine profile…", self.edit_machine)
        tb.addSeparator()
        act("Import…", self.import_set)
        act("Export…", self.export_set)

        help_menu = self.menuBar().addMenu("&Help")
        about = QAction("About", self)
        about.triggered.connect(self.show_about)
        help_menu.addAction(about)

    def _build_ui(self) -> None:
        split = QSplitter(Qt.Orientation.Horizontal)
        self.setCentralWidget(split)

        # --- library
        lib_panel = QWidget()
        lv = QVBoxLayout(lib_panel)
        lv.setContentsMargins(6, 6, 6, 6)
        lv.addWidget(self._heading("Exposure set library"))
        self.library_list = QListWidget()
        self.library_list.currentItemChanged.connect(self._on_library_selection)
        lv.addWidget(self.library_list)
        self.library_hint = QLabel(f"Library: {self.lib.root}")
        self.library_hint.setWordWrap(True)
        self.library_hint.setStyleSheet("color: gray; font-size: 11px;")
        lv.addWidget(self.library_hint)
        split.addWidget(lib_panel)

        # --- structure tree
        tree_panel = QWidget()
        tv = QVBoxLayout(tree_panel)
        tv.setContentsMargins(6, 6, 6, 6)
        self.set_title = self._heading("")
        tv.addWidget(self.set_title)
        self.lock_banner = QFrame()
        self.lock_banner.setStyleSheet("QFrame { background: #fdecea; border-radius: 4px; }")
        bl = QHBoxLayout(self.lock_banner)
        bl.setContentsMargins(8, 4, 8, 4)
        bl.addWidget(QLabel("Locked reference set. Copy it to make changes."))
        copy_btn = QPushButton("Copy…")
        copy_btn.clicked.connect(self.copy_set)
        bl.addWidget(copy_btn)
        tv.addWidget(self.lock_banner)

        row = QHBoxLayout()
        self.add_btn = QToolButton()
        self.add_btn.setText("Add exposure type")
        self.add_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.add_menu = QMenu(self)
        self.add_btn.setMenu(self.add_menu)
        self.remove_btn = QPushButton("Remove")
        self.up_btn = QPushButton("▲")
        self.down_btn = QPushButton("▼")
        for b in (self.up_btn, self.down_btn):
            b.setFixedWidth(32)
        self.up_btn.setToolTip("Expose earlier")
        self.down_btn.setToolTip("Expose later")
        self.remove_btn.clicked.connect(self.remove_selected)
        self.up_btn.clicked.connect(lambda: self.move_selected(-1))
        self.down_btn.clicked.connect(lambda: self.move_selected(1))
        for w in (self.add_btn, self.remove_btn, self.up_btn, self.down_btn):
            row.addWidget(w)
        row.addStretch(1)
        tv.addLayout(row)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.currentItemChanged.connect(self._on_tree_selection)
        self.tree.itemChanged.connect(self._on_tree_item_changed)
        tv.addWidget(self.tree)

        prow = QHBoxLayout()
        self.add_pass_btn = QPushButton("Add contour pass")
        self.remove_pass_btn = QPushButton("Remove contour pass")
        self.add_pass_btn.clicked.connect(self.add_contour_pass)
        self.remove_pass_btn.clicked.connect(self.remove_contour_pass)
        prow.addWidget(self.add_pass_btn)
        prow.addWidget(self.remove_pass_btn)
        tv.addLayout(prow)
        legend = QLabel('<span style="color:#c98a00">●</span> caution &nbsp; '
                        '<span style="color:#c0392b">●</span> critical parameter')
        legend.setStyleSheet("color: gray; font-size: 11px;")
        tv.addWidget(legend)
        split.addWidget(tree_panel)

        # --- detail + analysis
        right = QSplitter(Qt.Orientation.Vertical)
        detail = QWidget()
        dv = QVBoxLayout(detail)
        dv.setContentsMargins(6, 6, 6, 6)
        self.detail_title = self._heading("")
        dv.addWidget(self.detail_title)
        self.form_area = QScrollArea()
        self.form_area.setWidgetResizable(True)
        dv.addWidget(self.form_area)
        right.addWidget(detail)

        self.analysis = QTabWidget()
        self.energy_table = self._table(["Exposure", "Region", "P [W]", "v [mm/s]", "h [mm]", "Energy input"])
        self.checks_table = self._table(["Severity", "Rule", "Parameter", "Message"])
        self.offset_table = self._table(["Scan line", "Offset from nominal contour [mm]"])
        self.analysis.addTab(self.energy_table, "Energy input")
        self.analysis.addTab(self.checks_table, "Checks")
        self.analysis.addTab(self.offset_table, "Offsets")
        right.addWidget(self.analysis)
        right.setSizes([480, 300])
        split.addWidget(right)
        split.setSizes([280, 360, 720])
        self.statusBar()

    @staticmethod
    def _heading(text: str) -> QLabel:
        lbl = QLabel(text)
        f = lbl.font()
        f.setBold(True)
        f.setPointSize(f.pointSize() + 1)
        lbl.setFont(f)
        return lbl

    @staticmethod
    def _table(headers: list[str]) -> QTableWidget:
        t = QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        t.horizontalHeader().setStretchLastSection(True)
        return t

    # ================================================================== library

    def refresh_library(self, select: str | None = None) -> None:
        self.library_list.blockSignals(True)
        self.library_list.clear()
        for name, es in sorted(self.lib.load_all().items(), key=lambda kv: kv[0].lower()):
            item = QListWidgetItem(color_icon(es.color), name)
            item.setData(OBJ_ROLE, name)
            sub = f"{es.category.value}, {es.layer_thickness_mm * 1000:.0f} µm"
            item.setToolTip(f"{name}\n{sub}\n{es.description}")
            if es.locked:
                item.setText(f"{name}   (locked)")
                item.setForeground(QBrush(QColor("#666")))
            self.library_list.addItem(item)
            if name == select:
                self.library_list.setCurrentItem(item)
        self.library_list.blockSignals(False)
        if self.lib.load_errors:
            skipped = "; ".join(f"{p.name} ({reason.splitlines()[0][:200]})" for p, reason in self.lib.load_errors)
            self.statusBar().showMessage(f"Skipped set files that could not be loaded: {skipped}", 15000)

    def _selected_name(self) -> str | None:
        item = self.library_list.currentItem()
        return item.data(OBJ_ROLE) if item else None

    def _on_library_selection(self, current: QListWidgetItem | None, previous: QListWidgetItem | None) -> None:
        if current is None:
            return
        if not self.confirm_discard():
            self.library_list.blockSignals(True)
            self.library_list.setCurrentItem(previous)
            self.library_list.blockSignals(False)
            return
        self.load_set(current.data(OBJ_ROLE))

    def confirm_discard(self) -> bool:
        if not self.dirty or self.current is None:
            return True
        r = QMessageBox.question(
            self, APP_NAME, f"Save changes to {self.current.name}?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel)
        if r == QMessageBox.StandardButton.Save:
            return self.save_current()
        if r == QMessageBox.StandardButton.Discard:
            self.dirty = False
            return True
        return False

    def load_set(self, name: str) -> None:
        try:
            self.current = self.lib.get(name)
        except (LibraryError, ValidationError) as exc:
            QMessageBox.critical(self, APP_NAME, f"Cannot open {name}:\n{exc}")
            return
        self.dirty = False
        self.rebuild_tree()
        self.update_state()

    def new_set(self, category: SetCategory) -> None:
        if not self.confirm_discard():
            return
        base = "New_Support" if category == SetCategory.SUPPORT else "New_Part"
        name, ok = QInputDialog.getText(self, "New exposure set", "Name:", text=self.lib.unique_name(base))
        if not ok or not name.strip():
            return
        try:
            self.lib.save(new_set(name.strip(), category), overwrite=False)
        except LibraryError as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        self.refresh_library(select=name.strip())
        self.load_set(name.strip())

    def copy_set(self) -> None:
        src = self.current.name if self.current else self._selected_name()
        if not src or not self.confirm_discard():
            return
        name, ok = QInputDialog.getText(self, "Copy exposure set", f"Name for the copy of {src}:",
                                        text=self.lib.unique_name(f"{src}_copy"))
        if not ok or not name.strip():
            return
        try:
            self.lib.copy(src, name.strip())
        except LibraryError as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        self.refresh_library(select=name.strip())
        self.load_set(name.strip())

    def rename_set(self) -> None:
        if not self.current or self.current.locked or not self.confirm_discard():
            return
        old = self.current.name
        name, ok = QInputDialog.getText(self, "Rename exposure set", "New name:", text=old)
        if not ok or not name.strip() or name.strip() == old:
            return
        try:
            self.lib.rename(old, name.strip())
        except LibraryError as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        self.refresh_library(select=name.strip())
        self.load_set(name.strip())

    def delete_set(self) -> None:
        if not self.current or self.current.locked:
            return
        name = self.current.name
        if QMessageBox.question(self, APP_NAME, f"Delete {name}? This cannot be undone.") != QMessageBox.StandardButton.Yes:
            return
        try:
            self.lib.delete(name)
        except LibraryError as exc:
            QMessageBox.warning(self, APP_NAME, f"Not deleted:\n{exc}")
            return
        self.current, self.dirty = None, False
        self.refresh_library()
        if self.library_list.count():
            self.library_list.setCurrentRow(0)

    def save_current(self) -> bool:
        if not self.current or self.current.locked:
            return False
        try:
            ExposureSet.model_validate(self.current.model_dump())
            self.lib.save(self.current)
        except (LibraryError, ValidationError) as exc:
            QMessageBox.warning(self, APP_NAME, f"Not saved:\n{exc}")
            return False
        self.dirty = False
        self.refresh_library(select=self.current.name)
        self.update_state()
        self.statusBar().showMessage(f"Saved {self.current.name}", 3000)
        return True

    def revert_current(self) -> None:
        if self.current:
            self.load_set(self.current.name)

    # ================================================================== tree

    def rebuild_tree(self, keep: object | None = None) -> None:
        es = self.current
        self.tree.blockSignals(True)
        self.tree.clear()
        if es is None:
            self.tree.blockSignals(False)
            return
        root = self._tree_item(None, f"{es.name}", es)
        root.setFont(0, self._bold())
        target = root
        for i, et in enumerate(es.exposure_types, start=1):
            item = self._tree_item(root, f"{i}   {et.label()}", et)
            item.setFont(0, self._bold())
            found = self._add_children(item, et, keep)
            if et is keep:
                target = item
            elif found is not None:
                target = found
        self.tree.expandAll()
        self.tree.blockSignals(False)
        self.tree.setCurrentItem(target)
        if target is root:
            self._show_form(es)

    def _add_children(self, parent: QTreeWidgetItem, obj: BaseModel, keep: object | None) -> QTreeWidgetItem | None:
        found = None
        for name in type(obj).model_fields:
            value = getattr(obj, name)
            if isinstance(value, BaseModel):
                if not self._relevant(obj, name):
                    continue
                child = self._tree_item(parent, value.label(), value)
                if value is keep:
                    found = child
                found = self._add_children(child, value, keep) or found
            elif isinstance(value, list):
                for v in value:
                    if isinstance(v, BaseModel):
                        pid = v.id if isinstance(v, ContourPass) else None
                        child = self._tree_item(parent, f"{v.label()}   @{pid}" if pid else v.label(), v)
                        if v is keep:
                            found = child
        return found

    @staticmethod
    def _relevant(obj: BaseModel, field: str) -> bool:
        """Hide pattern settings that the selected pattern does not use."""
        pattern = getattr(obj, "pattern", None)
        if field == "stripes":
            return pattern == HatchPattern.STRIPES
        if field == "chess":
            return pattern == HatchPattern.CHESS
        return True

    def _tree_item(self, parent: QTreeWidgetItem | None, text: str, obj: BaseModel) -> QTreeWidgetItem:
        item = QTreeWidgetItem([text])
        item.setData(0, OBJ_ROLE, obj)
        if "enabled" in type(obj).model_fields:
            flags = item.flags() | Qt.ItemFlag.ItemIsUserCheckable
            if self.current and self.current.locked:
                flags &= ~Qt.ItemFlag.ItemIsUserCheckable
            item.setFlags(flags)
            item.setCheckState(0, Qt.CheckState.Checked if obj.enabled else Qt.CheckState.Unchecked)
        if parent is None:
            self.tree.addTopLevelItem(item)
        else:
            parent.addChild(item)
        return item

    @staticmethod
    def _bold() -> QFont:
        f = QFont()
        f.setBold(True)
        return f

    def _selected_obj(self) -> BaseModel | None:
        item = self.tree.currentItem()
        return item.data(0, OBJ_ROLE) if item else None

    def _on_tree_selection(self, current: QTreeWidgetItem | None, _prev: QTreeWidgetItem | None) -> None:
        if current is not None:
            self._show_form(current.data(0, OBJ_ROLE))
        self.update_state()

    def _on_tree_item_changed(self, item: QTreeWidgetItem, _col: int) -> None:
        obj = item.data(0, OBJ_ROLE)
        if obj is None or "enabled" not in type(obj).model_fields or (self.current and self.current.locked):
            return
        checked = item.checkState(0) == Qt.CheckState.Checked
        if obj.enabled != checked:
            obj.enabled = checked
            if self.form and self.form.obj is obj:
                self.form.reload()
            self.mark_dirty()

    def _show_form(self, obj: BaseModel) -> None:
        readonly = bool(self.current and self.current.locked)
        self.form = ModelForm(obj, readonly=readonly)
        self.form.changed.connect(self._on_form_changed)
        self.form.error.connect(lambda m: self.statusBar().showMessage(f"Rejected: {m}", 6000))
        self.form_area.setWidget(self.form)
        self.detail_title.setText(obj.label() if not isinstance(obj, ExposureSet) else "Set properties")

    def _on_form_changed(self, field: str) -> None:
        obj = self.form.obj if self.form else None
        if field == "enabled":
            item = self.tree.currentItem()
            if item is not None:
                self.tree.blockSignals(True)
                item.setCheckState(0, Qt.CheckState.Checked if obj.enabled else Qt.CheckState.Unchecked)
                self.tree.blockSignals(False)
        if field in ("pattern", "name"):
            # deferred: rebuilding replaces the form whose widget is still emitting this signal
            QTimer.singleShot(0, lambda o=obj: self.rebuild_tree(keep=o))
        self.mark_dirty()

    # ================================================================== structure edits

    def _selected_exposure(self) -> BaseModel | None:
        item = self.tree.currentItem()
        while item is not None and item.parent() is not None and item.parent().parent() is not None:
            item = item.parent()
        if item is None or item.parent() is None:
            return None
        return item.data(0, OBJ_ROLE)

    def _populate_add_menu(self) -> None:
        self.add_menu.clear()
        if not self.current:
            return
        present = self.current.kinds()
        for kind in allowed_kinds(self.current.category):
            a = self.add_menu.addAction(EXPOSURE_CLASSES[kind].node_label)
            a.setEnabled(kind not in present and len(present) < MAX_EXPOSURE_TYPES)
            a.triggered.connect(lambda _=False, k=kind: self.add_exposure(k))

    def add_exposure(self, kind: str) -> None:
        es = self.current
        if kind in es.kinds() or len(es.exposure_types) >= MAX_EXPOSURE_TYPES:
            QMessageBox.warning(self, APP_NAME, f"A set holds each exposure type once and at most {MAX_EXPOSURE_TYPES} in total.")
            return
        es.exposure_types = [*es.exposure_types, EXPOSURE_CLASSES[kind]()]
        self.rebuild_tree(keep=es.exposure_types[-1])
        self.mark_dirty()

    def remove_selected(self) -> None:
        et = self._selected_exposure()
        if et is None:
            return
        self.current.exposure_types = [x for x in self.current.exposure_types if x is not et]
        self.rebuild_tree()
        self.mark_dirty()

    def move_selected(self, delta: int) -> None:
        et = self._selected_exposure()
        if et is None:
            return
        types = list(self.current.exposure_types)
        i = next(i for i, x in enumerate(types) if x is et)
        j = i + delta
        if not 0 <= j < len(types):
            return
        types[i], types[j] = types[j], types[i]
        self.current.exposure_types = types
        self.rebuild_tree(keep=self.current.exposure_types[j])
        self.mark_dirty()

    def _selected_contour(self) -> tuple[Contour | None, ContourPass | None]:
        obj = self._selected_obj()
        if isinstance(obj, ContourPass):
            contour = self.current.get("contour")
            return contour, obj
        if isinstance(obj, Contour):
            return obj, None
        return None, None

    def add_contour_pass(self) -> None:
        contour, _ = self._selected_contour()
        if contour is None or len(contour.passes) >= 4:
            return
        # a format 2 set gives the new pass a fresh random id; format 1 passes have none
        pid = new_pass_id({q.id for q in contour.passes}) if self.current.schema_version >= PASS_ID_VERSION else None
        assign_field(contour, "passes", [*contour.passes, ContourPass(id=pid, name=f"Contour {len(contour.passes) + 1}")])
        self.rebuild_tree(keep=contour.passes[-1])
        self.mark_dirty()

    def remove_contour_pass(self) -> None:
        contour, cp = self._selected_contour()
        if contour is None or cp is None or len(contour.passes) <= 1:
            return
        assign_field(contour, "passes", [p for p in contour.passes if p is not cp])
        self.rebuild_tree(keep=contour)
        self.mark_dirty()

    # ================================================================== state + analysis

    def mark_dirty(self) -> None:
        self.dirty = True
        self.update_state()

    def update_state(self) -> None:
        es = self.current
        locked = bool(es and es.locked)
        editable = es is not None and not locked
        self.lock_banner.setVisible(locked)
        title = es.name if es else "No set selected"
        self.set_title.setText(title + (" *" if self.dirty else ""))
        self.setWindowTitle(f"{APP_NAME} – {title}{' *' if self.dirty else ''}")
        self.act_save.setEnabled(editable and self.dirty)
        self.act_revert.setEnabled(editable and self.dirty)
        self.act_delete.setEnabled(editable)
        self.act_rename.setEnabled(editable)
        self._populate_add_menu()
        exp = self._selected_exposure()
        self.add_btn.setEnabled(editable)
        self.remove_btn.setEnabled(editable and exp is not None)
        self.up_btn.setEnabled(editable and exp is not None)
        self.down_btn.setEnabled(editable and exp is not None)
        contour, cp = self._selected_contour() if es else (None, None)
        self.add_pass_btn.setVisible(contour is not None)
        self.remove_pass_btn.setVisible(contour is not None)
        self.add_pass_btn.setEnabled(editable and contour is not None and len(contour.passes) < 4)
        self.remove_pass_btn.setEnabled(editable and cp is not None and len(contour.passes) > 1)
        self.refresh_analysis()

    def refresh_analysis(self) -> None:
        es = self.current
        for t in (self.energy_table, self.checks_table, self.offset_table):
            t.setRowCount(0)
        if es is None:
            return
        for m in calc.energy_summary(es):
            self._add_row(self.energy_table, [m.exposure, m.region, f"{m.power_w:.1f}", f"{m.speed_mm_s:.0f}",
                                              f"{m.hatch_mm:.3f}" if m.hatch_mm else "—", f"{m.energy:.2f} {m.unit}"])
            if m.cycle is not None:
                self._add_row(self.energy_table, [m.exposure, f"{calc.CYCLE_ENERGY_LABEL}, n={m.cycle.skip_layers}",
                                                  "", "", "", calc.cycle_energy_text(m.cycle)])
                tip = f"E_eff = P/(v·h·(n+1)·t): an {calc.CYCLE_ENERGY_CAVEAT}. Energy-window checks use the nominal value."
                for c in range(self.energy_table.columnCount()):
                    item = self.energy_table.item(self.energy_table.rowCount() - 1, c)
                    if item is not None:
                        item.setToolTip(tip)
        issues = validate_set(es, self.machines, self.process, self.lib.unreadable_machines)
        for i in issues:
            self._add_row(self.checks_table, [i.severity.value, i.code, i.location, i.message],
                          SEVERITY_COLOR[i.severity])
        if not issues:
            self._add_row(self.checks_table, ["ok", "", "", "No issues found."], "#27ae60")
        n_err = sum(i.severity == Severity.ERROR for i in issues)
        n_warn = sum(i.severity == Severity.WARNING for i in issues)
        self.analysis.setTabText(1, f"Checks ({n_err} errors, {n_warn} warnings)" if issues else "Checks")
        for k, v in calc.effective_offsets(es, self.process).items():
            self._add_row(self.offset_table, [k, f"{v:+.3f}"])
        note = calc.homogenization_note(es)
        if note:
            self._add_row(self.energy_table, ["Hatch", note, "", "", "", ""])

    @staticmethod
    def _add_row(table: QTableWidget, values: list[str], color: str | None = None) -> None:
        r = table.rowCount()
        table.insertRow(r)
        for c, v in enumerate(values):
            item = QTableWidgetItem(v)
            if color and c == 0:
                item.setForeground(QBrush(QColor(color)))
                f = item.font()
                f.setBold(True)
                item.setFont(f)
            table.setItem(r, c, item)

    # ================================================================== dialogs

    def edit_process(self) -> None:
        dlg = SettingsDialog(self.process, "Process settings", self)
        if dlg.exec():
            self.process = dlg.result_model
            self.lib.save_process(self.process)
            self.refresh_analysis()

    def edit_machine(self) -> None:
        """Edit the machine profile that the selected set names."""
        ref = self.current.machine.strip() if self.current else ""
        if ref not in self.machines:
            known = ", ".join(sorted(self.machines)) or "none"
            QMessageBox.information(
                self, APP_NAME,
                f"The selected set names no known machine profile ({ref or 'blank'}).\n"
                f"Set its Machine profile field to one of: {known}.")
            return
        dlg = SettingsDialog(self.machines[ref], f"Machine profile: {ref}", self)
        if dlg.exec():
            try:
                self.lib.save_machine(dlg.result_model)
            except LibraryError as exc:
                QMessageBox.warning(self, APP_NAME, str(exc))
            self.machines = self.lib.load_machines()
            self.refresh_analysis()

    def open_doe(self) -> None:
        if not self.current:
            return
        dlg = DoeDialog(self.current, self.lib, self)
        if dlg.exec() and dlg.created:
            self.refresh_library(select=self.current.name)
            self.statusBar().showMessage(f"Created {len(dlg.created)} sets", 5000)

    def open_compare(self) -> None:
        CompareDialog(self.lib, self.current.name if self.current else None, self).exec()

    def open_estimate(self) -> None:
        EstimateDialog(self.lib, self.process, self.current.name if self.current else None, self).exec()

    def open_tools(self) -> None:
        ToolsDialog(self.current, self).exec()

    def import_set(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import exposure set", "", "Exposure set (*.json)")
        if not path:
            return
        try:
            es = self.lib.import_file(path)
        except (LibraryError, ValidationError, ValueError) as exc:
            QMessageBox.warning(self, APP_NAME, f"Import failed:\n{exc}")
            return
        self.refresh_library(select=es.name)
        self.load_set(es.name)

    def export_set(self) -> None:
        if not self.current:
            return
        path, flt = QFileDialog.getSaveFileName(self, "Export exposure set", f"{self.current.name}.json",
                                                "Exposure set (*.json);;Parameter table (*.csv)")
        if not path:
            return
        if path.lower().endswith(".csv"):
            from ..cli import write_csv
            write_csv(self.current, Path(path))
        else:
            export_json(self.current, path)
        self.statusBar().showMessage(f"Exported to {path}", 5000)

    def show_about(self) -> None:
        QMessageBox.about(self, APP_NAME,
                          f"<b>{APP_NAME}</b> {__version__}<br>Parameter-set editor for laser powder bed fusion."
                          "<br><br>Shipped reference sets contain illustrative values only.")

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if self.confirm_discard():
            event.accept()
        else:
            event.ignore()


def run(library_root: Path | str | None = None) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    win = MainWindow(Library(library_root))
    win.show()
    return app.exec()


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
