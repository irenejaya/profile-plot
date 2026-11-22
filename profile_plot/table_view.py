"""Table tab: one row per native interval or 1 cm station, CSV export."""

import csv
import math

import numpy as np
from qgis.PyQt.QtCore import QAbstractTableModel, QModelIndex, Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from . import icons
from .compat import flag_int

NODATA = "NoData"


def headers(table, show_units, unit):
    cu = " (m)" if show_units else ""
    vu = " ({})".format(unit) if show_units else ""
    interp = getattr(table, "interpolated", False)
    names = [n + vu + (" (bilinear)" if interp else "") for n in table.names]
    if table.kind == "intervals":
        return ["From" + cu, "To" + cu] + names
    if table.kind == "stations":
        return ["Chainage" + cu] + names
    return []


class ProfileTableModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.table = None
        self._headers = []
        self._ch_fmt = "{:.3f}"

    def set_table(self, table, show_units, unit):
        self.beginResetModel()
        self.table = table
        self._headers = headers(table, show_units, unit) if table is not None else []
        self._ch_fmt = "{:.3f}" if table is not None and table.kind == "intervals" else "{:.2f}"
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        if parent.isValid() or self.table is None:
            return 0
        return self.table.row_count

    def columnCount(self, parent=QModelIndex()):
        if parent.isValid():
            return 0
        return len(self._headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            if 0 <= section < len(self._headers):
                return self._headers[section]
            return None
        return str(section + 1)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or self.table is None:
            return None
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return flag_int(Qt.AlignmentFlag.AlignRight, Qt.AlignmentFlag.AlignVCenter)
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        r = index.row()
        c = index.column()
        t = self.table
        if c == 0:
            return self._ch_fmt.format(t.from_ch[r])
        if t.kind == "intervals":
            if c == 1:
                return self._ch_fmt.format(t.to_ch[r])
            col = t.columns[c - 2]
        else:
            col = t.columns[c - 1]
        v = col[r]
        return "{:.2f}".format(v) if math.isfinite(v) else NODATA


def _value_formatter(dtype):
    fmt = "%.7g" if dtype == np.float32 else "%.15g"

    def f(v):
        return fmt % v if v == v else NODATA
    return f


def write_csv(path, table, show_units, unit, dtypes=None):
    """Write every row of ``table``.  Returns the number of rows written."""
    hdr = headers(table, show_units, unit)
    n = table.row_count
    dtypes = dtypes or [np.float64] * len(table.columns)
    fmts = [_value_formatter(d) for d in dtypes]
    ch_fmt = "%.3f" if table.kind == "intervals" else "%.2f"
    chunk = 50000
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(hdr)
        for s in range(0, n, chunk):
            e = min(n, s + chunk)
            cols = [[ch_fmt % v for v in table.from_ch[s:e].tolist()]]
            if table.kind == "intervals":
                cols.append([ch_fmt % v for v in table.to_ch[s:e].tolist()])
            for f, col in zip(fmts, table.columns):
                cols.append([f(v) for v in col[s:e].tolist()])
            w.writerows(zip(*cols))
    return n


class TablePanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)
        top = QHBoxLayout()
        self.info = QLabel("")
        top.addWidget(self.info, 1)
        self.export_button = QPushButton(icons.icon("save"), "Export CSV")
        self.export_button.setToolTip("Write all final rows to a CSV file (gaps marked NoData)")
        self.export_button.setEnabled(False)
        top.addWidget(self.export_button)
        lay.addLayout(top)
        self.view = QTableView()
        self.model = ProfileTableModel(self)
        self.view.setModel(self.model)
        self.view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.view.setAlternatingRowColors(True)
        self.view.setWordWrap(False)
        vh = self.view.verticalHeader()
        vh.setVisible(False)
        vh.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        vh.setDefaultSectionSize(self.fontMetrics().height() + 6)
        hh = self.view.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hh.setStretchLastSection(False)
        lay.addWidget(self.view, 1)

    def set_table(self, table, show_units, unit, final):
        self.model.set_table(table, show_units, unit)
        hh = self.view.horizontalHeader()
        for c in range(self.model.columnCount()):
            hh.resizeSection(c, max(80, self.fontMetrics().horizontalAdvance(
                self.model.headerData(c, Qt.Orientation.Horizontal) or "") + 24))
        n = table.row_count if table is not None else 0
        self.export_button.setEnabled(n > 0 and final)
        if table is None or n == 0:
            self.info.setText("")
        else:
            kind = "intervals" if table.kind == "intervals" else "stations"
            self.info.setText("{:,} {}{}".format(n, kind, "" if final else " (updating…)"))

    @staticmethod
    def ask_path(parent):
        path, _ = QFileDialog.getSaveFileName(parent, "Export profile table", "profile.csv",
                                              "CSV (*.csv)")
        if path and not path.lower().endswith(".csv"):
            path += ".csv"
        return path
