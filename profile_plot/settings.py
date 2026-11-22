"""Persistent user settings and the small settings dialog."""

from qgis.core import QgsSettings
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

PREFIX = "ProfilePlot/"


def _s():
    return QgsSettings()


def get(key, default=None, type_=None):
    s = _s()
    if type_ is bool:
        v = s.value(PREFIX + key, default)
        if isinstance(v, str):
            return v.lower() in ("1", "true", "yes")
        return bool(v)
    if type_ is not None:
        try:
            return type_(s.value(PREFIX + key, default))
        except (TypeError, ValueError):
            return default
    return s.value(PREFIX + key, default)


def set(key, value):  # noqa: A001 - mirrors QgsSettings naming
    _s().setValue(PREFIX + key, value)


def show_units():
    return get("showUnits", False, bool)


def unit_label():
    text = get("unit", "m", str)
    return text.strip() or "m"


class SettingsDialog(QDialog):
    """Units: hidden by default; when shown, chainage is in metres and the
    elevation unit is the text entered here (default "m")."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Profile Plot settings")
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.show_units = QCheckBox("Show units on the plot, tooltips, table and CSV")
        self.show_units.setChecked(show_units())
        form.addRow(self.show_units)
        self.unit = QLineEdit(unit_label())
        self.unit.setPlaceholderText("m")
        self.unit.setToolTip("Elevation unit label, e.g. m, m AHD, ft")
        form.addRow("Elevation unit", self.unit)
        lay.addLayout(form)
        note = QLabel("Chainage is always measured in metres.")
        note.setEnabled(False)
        lay.addWidget(note)
        self.show_units.toggled.connect(self.unit.setEnabled)
        self.unit.setEnabled(self.show_units.isChecked())
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                              QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def accept(self):
        set("showUnits", self.show_units.isChecked())
        set("unit", self.unit.text().strip() or "m")
        super().accept()
