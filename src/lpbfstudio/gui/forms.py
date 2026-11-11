"""Form widgets generated from model field metadata."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ValidationError
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QSpinBox, QWidget,
)

from ..models import ContourPass, ExposureSet, MachineProfile
from ..paths import assign_field

GUARD_STYLE = {
    "free": ("", ""),
    "caution": ("#c98a00", "Caution: better know what this does."),
    "critical": ("#c0392b", "Critical: you have to know what this does."),
}
SKIP_FIELDS = {"kind", "schema_version", "locked", "category"}


def is_fixed(obj: BaseModel, name: str) -> bool:
    """Shown but not editable: set and machine-profile names are identities that other data refers to.
    The set name is changed only through Rename, which keeps the library consistent. A contour pass
    id (format 2) never changes."""
    return (isinstance(obj, (ExposureSet, MachineProfile)) and name == "name") or \
        (isinstance(obj, ContourPass) and name == "id")


def is_scalar(value: Any) -> bool:
    return isinstance(value, (bool, int, float, str, Enum))


def enum_text(member: Enum) -> str:
    return str(member.value).replace("_", " ").capitalize()


class ModelForm(QWidget):
    """Edits the scalar fields of one pydantic model in place."""

    changed = Signal(str)   # field name
    error = Signal(str)

    def __init__(self, obj: BaseModel, readonly: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.obj = obj
        self.readonly = readonly
        self.widgets: dict[str, QWidget] = {}
        self.guard_labels: dict[str, QLabel] = {}  # the guard level as text ("caution", "critical"); empty when free
        self._loading = False
        layout = QFormLayout(self)
        layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        for name, info in type(obj).model_fields.items():
            if name in SKIP_FIELDS:
                continue
            value = getattr(obj, name)
            if not is_scalar(value):
                continue
            meta = info.json_schema_extra if isinstance(info.json_schema_extra, dict) else {}
            widget = self._make_widget(name, value, meta)
            if widget is None:
                continue
            widget.setEnabled(not readonly and not is_fixed(obj, name))
            help_text = meta.get("help") or info.description or ""
            if is_fixed(obj, name):
                help_text = ("Use Rename in the toolbar to change the set name." if isinstance(obj, ExposureSet)
                             else "Sets refer to this profile by name, so the name cannot be changed here.")
            widget.setToolTip(help_text)
            layout.addRow(self._make_label(name, meta.get("label", name), meta.get("guard", "free"), help_text), widget)
            self.widgets[name] = widget

    # ------------------------------------------------------------------ construction

    def _make_label(self, name: str, text: str, guard: str, help_text: str) -> QWidget:
        color, guard_tip = GUARD_STYLE.get(guard, ("", ""))
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        marker = QLabel("●" if color else "")
        marker.setFixedWidth(10)
        if color:
            marker.setStyleSheet(f"color: {color};")
            marker.setToolTip(guard_tip)
        level = QLabel(str(getattr(guard, "value", guard)) if color else "")  # the guard as text, not colour alone
        if color:
            level.setStyleSheet(f"color: {color}; font-size: 8pt;")
            level.setToolTip(guard_tip)
        self.guard_labels[name] = level
        label = QLabel(text)
        label.setToolTip("\n".join(t for t in (help_text, guard_tip) if t))
        row.addStretch(1)
        row.addWidget(marker)
        row.addWidget(level)
        row.addWidget(label)
        return box

    def _make_widget(self, name: str, value: Any, meta: dict[str, Any]) -> QWidget | None:
        if isinstance(value, bool):
            w = QCheckBox()
            w.setChecked(value)
            w.toggled.connect(lambda v, n=name: self._commit(n, v))
            return w
        if isinstance(value, Enum):
            w = QComboBox()
            for member in type(value):
                w.addItem(enum_text(member), member)
            w.setCurrentIndex(list(type(value)).index(value))
            w.currentIndexChanged.connect(lambda i, n=name, cb=w: self._commit(n, cb.itemData(i)))
            return w
        if isinstance(value, int):
            w = QSpinBox()
            w.setRange(int(meta.get("min", -1_000_000)), int(meta.get("max", 1_000_000)))
            w.setValue(value)
            if meta.get("unit"):
                w.setSuffix(f" {meta['unit']}")
            w.valueChanged.connect(lambda v, n=name: self._commit(n, v))
            return w
        if isinstance(value, float):
            w = QDoubleSpinBox()
            decimals = int(meta.get("decimals", 3))
            w.setDecimals(decimals)
            w.setRange(float(meta.get("min", -1e9)), float(meta.get("max", 1e9)))
            w.setSingleStep(10.0 ** -(decimals - 1) if decimals >= 2 else 1.0)
            w.setKeyboardTracking(False)
            w.setValue(value)
            if meta.get("unit"):
                w.setSuffix(f" {meta['unit']}")
            w.valueChanged.connect(lambda v, n=name: self._commit(n, v))
            return w
        if isinstance(value, str):
            if name == "color":
                return self._color_widget(name, value)
            w = QLineEdit(value)
            w.editingFinished.connect(lambda n=name, le=w: self._commit(n, le.text()))
            return w
        return None

    def _color_widget(self, name: str, value: str) -> QWidget:
        btn = QPushButton(value)
        self._paint_color(btn, value)

        def pick() -> None:
            c = QColorDialog.getColor(QColor(getattr(self.obj, name)), self, "Exposure set color")
            if c.isValid():
                btn.setText(c.name())
                self._paint_color(btn, c.name())
                self._commit(name, c.name())

        btn.clicked.connect(pick)
        return btn

    @staticmethod
    def _paint_color(btn: QPushButton, value: str) -> None:
        fg = "#000" if QColor(value).lightness() > 128 else "#fff"
        btn.setStyleSheet(f"background: {value}; color: {fg}; border-radius: 3px; padding: 3px 8px;")

    # ------------------------------------------------------------------ updates

    def _commit(self, name: str, value: Any) -> None:
        if self._loading or self.readonly or is_fixed(self.obj, name):
            return
        old = getattr(self.obj, name)
        if value == old:
            return
        try:
            assign_field(self.obj, name, value)  # leaves obj unchanged when rejected
        except ValidationError as exc:
            msg = exc.errors()[0].get("msg", str(exc))
            self.error.emit(f"{name}: {msg}")
            self.reload()
            return
        self.changed.emit(name)

    def reload(self) -> None:
        """Push model values back into the widgets."""
        self._loading = True
        try:
            for name, w in self.widgets.items():
                v = getattr(self.obj, name)
                if isinstance(w, QCheckBox):
                    w.setChecked(v)
                elif isinstance(w, QComboBox):
                    w.setCurrentIndex(w.findData(v))
                elif isinstance(w, (QSpinBox, QDoubleSpinBox)):
                    w.setValue(v)
                elif isinstance(w, QLineEdit):
                    w.setText(v)
        finally:
            self._loading = False
