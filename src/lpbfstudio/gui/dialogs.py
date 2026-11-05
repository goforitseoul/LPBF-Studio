"""Secondary dialogs: DOE generator, compare, build-time estimate, calculators."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListWidget, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from .. import calc
from ..doe import (DEFAULT_MAX_VARIANTS, collect_factors, count_text, generate, level_lines, parse_values,
                   save_batch)
from ..estimate import estimate_plan, fmt_duration
from ..library import Library
from ..models import ExposureSet, Hatch, ProcessSettings
from ..paths import diff, field_meta, get_path, numeric_paths


def _value_text(v) -> str:
    return str(v.value) if hasattr(v, "value") else str(v)


class DoeDialog(QDialog):
    """Full-factorial variants of the current set."""

    def __init__(self, base: ExposureSet, lib: Library, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.base, self.lib = base, lib
        self.created: list[str] = []
        self.setWindowTitle(f"DOE from {base.name}")
        self.resize(760, 520)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Pick up to three parameters and their levels. Levels: comma-separated values, "
                                "or start:stop:step."))
        self.table = QTableWidget(3, 3)
        self.table.setHorizontalHeaderLabels(["Parameter", "Current", "Levels"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        paths = numeric_paths(base)
        self.combos: list[QComboBox] = []
        for r in range(3):
            cb = QComboBox()
            cb.addItem("—", None)
            for p in paths:
                meta = field_meta(base, p)
                unit = f" [{meta['unit']}]" if meta.get("unit") else ""
                cb.addItem(f"{p}{unit}", p)
            cb.currentIndexChanged.connect(lambda _i, row=r: self._on_param(row))
            self.table.setCellWidget(r, 0, cb)
            self.table.setItem(r, 1, QTableWidgetItem(""))
            self.table.setItem(r, 2, QTableWidgetItem(""))
            self.combos.append(cb)
        self.table.cellChanged.connect(lambda *_: self.preview())
        layout.addWidget(self.table)
        form = QFormLayout()
        self.template = QLineEdit("{base}_DOE{i}")
        self.template.setToolTip("Placeholders: {base}, {i}, or a parameter's last name, e.g. {hatch_distance_mm}")
        self.template.textChanged.connect(self.preview)
        form.addRow("Name template", self.template)
        layout.addLayout(form)
        self.summary = QLabel("")
        layout.addWidget(self.summary)
        self.levels = QLabel("")  # the parsed levels, so a misread list (e.g. "285,5") is visible
        self.levels.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.levels)
        self.preview_list = QListWidget()
        layout.addWidget(self.preview_list)
        self.message = QLabel("")
        self.message.setStyleSheet("color: #c0392b;")
        layout.addWidget(self.message)
        buttons = QDialogButtonBox()
        self.generate_btn = buttons.addButton("Create sets", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.create)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.variants: list[ExposureSet] = []
        self.preview()

    def _on_param(self, row: int) -> None:
        path = self.combos[row].currentData()
        self.table.blockSignals(True)
        self.table.item(row, 1).setText(_value_text(get_path(self.base, path)) if path else "")
        self.table.blockSignals(False)
        self.preview()

    def _factors(self) -> dict:
        pairs = []
        for r, cb in enumerate(self.combos):
            path = cb.currentData()
            text = self.table.item(r, 2).text().strip()
            if path and text:
                pairs.append((path, parse_values(text, get_path(self.base, path), max_levels=DEFAULT_MAX_VARIANTS)))
        return collect_factors(pairs)

    def preview(self) -> None:
        """Rebuild the preview. The count is checked before any variant is built, and Create is
        only enabled when every variant name is free (nothing is skipped or overwritten)."""
        self.preview_list.clear()
        self.variants = []
        self.message.setText("")
        self.summary.setText("")
        self.levels.setText("")
        self.generate_btn.setEnabled(False)
        try:
            factors = self._factors()
            if not factors:
                return
            self.summary.setText(f"DOE: {count_text(factors)}")
            self.levels.setText("\n".join(level_lines(factors)))
            self.variants = generate(self.base, factors, self.template.text(), max_variants=DEFAULT_MAX_VARIANTS)
        except Exception as exc:  # show any parse / validation problem inline
            msg = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc).splitlines()[0]
            self.message.setText(str(msg))
            return
        problems = self.lib.check_new_names([v.name for v in self.variants])
        taken = set(self.lib.names()) if problems else set()  # names only; never probes a bad file name
        for v in self.variants:
            self.preview_list.addItem(v.name + (" (name taken)" if v.name in taken else ""))
        if problems:
            self.message.setText("Change the name template; nothing will be created while a name is taken:\n"
                                 + "\n".join(problems))
            return
        self.generate_btn.setEnabled(True)

    def create(self) -> None:
        if not self.variants:
            return
        problems = self.lib.check_new_names([v.name for v in self.variants])
        if problems:  # the library changed since the preview
            self.preview()
            return
        result = save_batch(self.lib, self.variants)
        self.created = result.saved
        if not result.complete:
            QMessageBox.warning(self, "DOE", result.report())
        self.accept()


class CompareDialog(QDialog):
    def __init__(self, lib: Library, current: str | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.lib = lib
        self.setWindowTitle("Compare exposure sets")
        self.resize(820, 560)
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        names = lib.names()
        self.a, self.b = QComboBox(), QComboBox()
        for cb in (self.a, self.b):
            cb.addItems(names)
            row.addWidget(cb)
        if current in names:
            self.a.setCurrentText(current)
        layout.addLayout(row)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Parameter", "A", "B"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        layout.addWidget(close)
        for cb in (self.a, self.b):
            cb.currentIndexChanged.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        self.table.setRowCount(0)
        if not self.a.currentText() or not self.b.currentText():
            return
        rows = diff(self.lib.get(self.a.currentText()), self.lib.get(self.b.currentText()))
        for path, va, vb in rows:
            r = self.table.rowCount()
            self.table.insertRow(r)
            for c, v in enumerate((path, _value_text(va), _value_text(vb))):
                self.table.setItem(r, c, QTableWidgetItem(v))


class EstimateDialog(QDialog):
    """Compare build time of a block for one set or a z-segmented plan."""

    def __init__(self, lib: Library, process: ProcessSettings, current: str | None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.lib, self.process = lib, process
        self.names = lib.names()
        self.setWindowTitle("Build-time estimate (z-segmentation)")
        self.resize(780, 520)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Prismatic block with constant cross-section. Use it to compare sets and "
                                "segmentation plans; jumps and delays are not modelled."))
        form = QFormLayout()
        self.area = self._spin(400.0, 1, 1e6, " mm²")
        self.perimeter = self._spin(80.0, 0, 1e6, " mm")
        form.addRow("Cross-section area", self.area)
        form.addRow("Contour length per layer", self.perimeter)
        layout.addLayout(form)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["z from [mm]", "z to [mm]", "Exposure set"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table)
        row = QHBoxLayout()
        add, rem, calc_btn = QPushButton("Add segment"), QPushButton("Remove segment"), QPushButton("Calculate")
        add.clicked.connect(lambda: self.add_segment())
        rem.clicked.connect(lambda: self.table.removeRow(self.table.currentRow()))
        calc_btn.clicked.connect(self.calculate)
        for b in (add, rem, calc_btn):
            row.addWidget(b)
        row.addStretch(1)
        layout.addLayout(row)
        self.result = QLabel("")
        self.result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.result.setWordWrap(True)
        layout.addWidget(self.result)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        layout.addWidget(close)
        self.add_segment(0, 25, current)

    @staticmethod
    def _spin(value: float, lo: float, hi: float, suffix: str) -> QDoubleSpinBox:
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setDecimals(2)
        s.setValue(value)
        s.setSuffix(suffix)
        return s

    def add_segment(self, z0: float | None = None, z1: float | None = None, name: str | None = None) -> None:
        r = self.table.rowCount()
        if z0 is None:
            prev = self.table.cellWidget(r - 1, 1) if r else None
            z0 = prev.value() if prev else 0.0
            z1 = z0 + 10.0
        self.table.insertRow(r)
        self.table.setCellWidget(r, 0, self._spin(z0, 0, 2000, ""))
        self.table.setCellWidget(r, 1, self._spin(z1, 0, 2000, ""))
        cb = QComboBox()
        cb.addItems(self.names)
        if name in self.names:
            cb.setCurrentText(name)
        self.table.setCellWidget(r, 2, cb)

    def calculate(self) -> None:
        plan = []
        for r in range(self.table.rowCount()):
            z0 = self.table.cellWidget(r, 0).value()
            z1 = self.table.cellWidget(r, 1).value()
            plan.append((z0, z1, self.lib.get(self.table.cellWidget(r, 2).currentText())))
        try:
            results = estimate_plan(plan, self.area.value(), self.perimeter.value(), self.process.recoat_time_s)
        except ValueError as exc:
            self.result.setText(f"<span style='color:#c0392b'>{exc}</span>")
            return
        lines = []
        for e in results:
            parts = ", ".join(f"{k} {fmt_duration(v)}" for k, v in e.seconds.items())
            lines.append(f"z {e.z_from_mm:g}–{e.z_to_mm:g} mm, <b>{e.set_name}</b>: {e.layers} layers, "
                         f"{fmt_duration(e.total_s)} ({parts})")
        total = sum(e.total_s for e in results)
        lines.append(f"<b>Total ≈ {fmt_duration(total)}</b>")
        self.result.setText("<br>".join(lines))


class ToolsDialog(QDialog):
    """Ridge-from-angle and power-reduction calculators."""

    def __init__(self, es: ExposureSet | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Calculators")
        self.resize(520, 520)
        layout = QVBoxLayout(self)

        ridge = QGroupBox("Skin ridge for a maximum overhang angle")
        rf = QFormLayout(ridge)
        thickness = 0.09
        if es is not None and isinstance(es.get("hatch"), Hatch):
            thickness = es.get("hatch").downskin.thickness_mm
        self.r_thick = self._spin(thickness, 0.001, 5, " mm", 3)
        self.r_angle = self._spin(70.0, 1, 89, " °", 1)
        self.r_out = QLabel()
        rf.addRow("Skin thickness", self.r_thick)
        rf.addRow("No skin on overhangs steeper than", self.r_angle)
        rf.addRow("Ridge", self.r_out)
        for s in (self.r_thick, self.r_angle):
            s.valueChanged.connect(self.update_ridge)
        layout.addWidget(ridge)

        pr = QGroupBox("Power reduction for short hatch vectors")
        pf = QFormLayout(pr)
        p0, h0, r = 285.0, 5.0, 0.2
        if es is not None and isinstance(es.get("hatch"), Hatch):
            f = es.get("hatch").infill
            p0, h0, r = f.power_w, f.stripes.stripe_width_mm, f.power_reduction_factor or 0.2
        self.p0 = self._spin(p0, 1, 5000, " W", 1)
        self.h0 = self._spin(h0, 0.1, 100, " mm", 2)
        self.rf = self._spin(r, 0, 1, "", 2)
        pf.addRow("Nominal power P0", self.p0)
        pf.addRow("Stripe width h0", self.h0)
        pf.addRow("Reduction factor r", self.rf)
        self.p_table = QTableWidget(0, 2)
        self.p_table.setHorizontalHeaderLabels(["Vector length [mm]", "Power [W]"])
        self.p_table.verticalHeader().setVisible(False)
        self.p_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        pf.addRow(self.p_table)
        for s in (self.p0, self.h0, self.rf):
            s.valueChanged.connect(self.update_power)
        layout.addWidget(pr)

        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        layout.addWidget(close)
        self.update_ridge()
        self.update_power()

    @staticmethod
    def _spin(value: float, lo: float, hi: float, suffix: str, decimals: int) -> QDoubleSpinBox:
        s = QDoubleSpinBox()
        s.setDecimals(decimals)
        s.setRange(lo, hi)
        s.setValue(value)
        s.setSuffix(suffix)
        return s

    def update_ridge(self) -> None:
        v = calc.ridge_for_angle(self.r_thick.value(), self.r_angle.value())
        self.r_out.setText(f"<b>{v:.3f} mm</b>")

    def update_power(self) -> None:
        rows = calc.power_reduction_table(self.p0.value(), self.rf.value(), self.h0.value())
        self.p_table.setRowCount(len(rows))
        for i, (h, p) in enumerate(rows):
            self.p_table.setItem(i, 0, QTableWidgetItem(f"{h:.2f}"))
            self.p_table.setItem(i, 1, QTableWidgetItem(f"{p:.1f}"))
