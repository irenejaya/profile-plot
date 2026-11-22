"""The small Profile Plot settings dialog (gear button in the dock)."""

from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from ..core.settings import (
    DEFAULT_X_TITLE,
    DEFAULT_Y_TITLE,
    LINE_STYLES,
    SLOPE_UNITS,
    line_style,
    set,
    show_ab,
    show_legend,
    show_units,
    slope_unit,
    unit_label,
    x_title,
    y_title,
)


class SettingsDialog(QDialog):
    """Legend and A/B map labels (both off by default), profile line style (smooth by default),
    axis titles (Chainage / Elevation by default), units (hidden by
    default; when shown, chainage is in metres and the elevation unit is
    the text entered here, default "m") and the slope unit (% by default,
    or m/m)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Profile Plot settings")
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.show_legend = QCheckBox("Show legend")
        self.show_legend.setToolTip("Raster names in the corner of the plot, with their values "
                                    "at the pointer")
        self.show_legend.setChecked(show_legend())
        form.addRow(self.show_legend)
        self.show_ab = QCheckBox("Show A / B labels on the map line")
        self.show_ab.setToolTip("Mark the start (A) and end (B) of the line on the map.\n"
                                "Off: only the line is drawn.")
        self.show_ab.setChecked(show_ab())
        form.addRow(self.show_ab)
        self.line_style = QComboBox()
        for key, label in LINE_STYLES:
            self.line_style.addItem(label, key)
        self.line_style.setCurrentIndex(max(0, self.line_style.findData(line_style())))
        self.line_style.setToolTip("Smooth: a line through the raster cell values.\n"
                                   "Steps: each native cell drawn as a flat step.")
        form.addRow("Profile line", self.line_style)
        self.x_title = QLineEdit(x_title())
        self.x_title.setPlaceholderText(DEFAULT_X_TITLE)
        form.addRow("X-axis title", self.x_title)
        self.y_title = QLineEdit(y_title())
        self.y_title.setPlaceholderText(DEFAULT_Y_TITLE)
        form.addRow("Y-axis title", self.y_title)
        self.show_units = QCheckBox("Show units on the plot, tooltips, table and CSV")
        self.show_units.setChecked(show_units())
        form.addRow(self.show_units)
        self.unit = QLineEdit(unit_label())
        self.unit.setPlaceholderText("m")
        self.unit.setToolTip("Elevation unit label, e.g. m, m AHD, ft")
        form.addRow("Elevation unit", self.unit)
        self.slope_unit = QComboBox()
        for key, label in SLOPE_UNITS:
            self.slope_unit.addItem(label, key)
        self.slope_unit.setCurrentIndex(max(0, self.slope_unit.findData(slope_unit())))
        self.slope_unit.setToolTip("Unit of the average slope in the footer")
        form.addRow("Slope unit", self.slope_unit)
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
        set("showLegend", self.show_legend.isChecked())
        set("showAB", self.show_ab.isChecked())
        set("lineStyle", self.line_style.currentData())
        set("xTitle", self.x_title.text().strip() or DEFAULT_X_TITLE)
        set("yTitle", self.y_title.text().strip() or DEFAULT_Y_TITLE)
        set("slopeUnit", self.slope_unit.currentData())
        set("showUnits", self.show_units.isChecked())
        set("unit", self.unit.text().strip() or "m")
        super().accept()
