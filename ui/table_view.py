"""Table tab: X (chainage) and one column per raster, with copy and CSV."""

import math

import numpy as np
from qgis.PyQt.QtCore import QAbstractTableModel, QModelIndex, Qt, pyqtSignal
from qgis.PyQt.QtGui import QGuiApplication, QKeySequence
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ..core.compat import flag_int
from ..core.export import NODATA, headers, table_text
from . import icons


class ProfileTableModel(QAbstractTableModel):
    """Virtual model over TableData: column 0 is X, then the rasters."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.table = None
        self._headers = []
        self._ch_fmt = "{:.3f}"

    def set_table(self, table, show_units, unit, x_title="Chainage"):
        self.beginResetModel()
        self.table = table
        self._headers = headers(table, show_units, unit, x_title) if table is not None else []
        self._ch_fmt = "{:.3f}" if table is not None and table.kind == "native" else "{:.2f}"
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):  # noqa: N802
        if parent.isValid() or self.table is None:
            return 0
        return self.table.row_count

    def columnCount(self, parent=QModelIndex()):  # noqa: N802
        if parent.isValid():
            return 0
        return len(self._headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
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
            return self._ch_fmt.format(t.x[r])
        v = t.columns[c - 1][r]
        return "{:.2f}".format(v) if math.isfinite(v) else NODATA


class ProfileTableView(QTableView):
    """Excel-like selection; Ctrl+C copies the selection (the whole table
    when everything is selected); right-click offers both copies."""

    copyRequested = pyqtSignal(bool)     # True = whole table

    def keyPressEvent(self, event):  # noqa: N802
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copyRequested.emit(False)
            event.accept()
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event):  # noqa: N802
        self.build_context_menu().exec(event.globalPos())

    def build_context_menu(self):
        menu = QMenu(self)
        sel = menu.addAction(icons.icon("copy"), "Copy selection",
                             lambda: self.copyRequested.emit(False))
        sel.setShortcut(QKeySequence(QKeySequence.StandardKey.Copy))
        sel.setEnabled(self.selectionModel() is not None and self.selectionModel().hasSelection())
        menu.addAction(icons.icon("copy"), "Copy whole table",
                       lambda: self.copyRequested.emit(True))
        menu.addSeparator()
        menu.addAction("Select all", self.selectAll)
        return menu


class TablePanel(QWidget):
    copied = pyqtSignal(int)             # number of rows put on the clipboard

    def __init__(self, parent=None):
        super().__init__(parent)
        self._show_units = False
        self._unit = "m"
        self._x_title = "Chainage"
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)
        top = QHBoxLayout()
        self.info = QLabel("")
        top.addWidget(self.info, 1)
        self.copy_button = QPushButton(icons.icon("copy"), "Copy")
        self.copy_button.setToolTip("Copy the whole table to the clipboard (paste into Excel).\n"
                                    "Ctrl+C copies just the selected cells.")
        self.copy_button.setEnabled(False)
        top.addWidget(self.copy_button)
        self.export_button = QPushButton(icons.icon("save"), "Export CSV")
        self.export_button.setToolTip("Write all final rows to a CSV file (gaps marked NoData)")
        self.export_button.setEnabled(False)
        top.addWidget(self.export_button)
        lay.addLayout(top)
        self.view = ProfileTableView()
        self.model = ProfileTableModel(self)
        self.view.setModel(self.model)
        self.view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
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
        self.copy_button.clicked.connect(lambda: self.copy(whole=True))
        self.view.copyRequested.connect(lambda whole: self.copy(whole=whole))

    def set_table(self, table, show_units, unit, final, x_title="Chainage"):
        self._show_units, self._unit, self._x_title = show_units, unit, x_title
        self.model.set_table(table, show_units, unit, x_title)
        hh = self.view.horizontalHeader()
        for c in range(self.model.columnCount()):
            hh.resizeSection(c, max(80, self.fontMetrics().horizontalAdvance(
                self.model.headerData(c, Qt.Orientation.Horizontal) or "") + 24))
        n = table.row_count if table is not None else 0
        self.export_button.setEnabled(n > 0 and final)
        self.copy_button.setEnabled(n > 0 and final)
        if table is None or n == 0:
            self.info.setText("")
        else:
            self.info.setText("{:,} rows{}".format(n, "" if final else " (updating…)"))

    # -- copying ----------------------------------------------------------------------

    def _selection_ranges(self):
        sm = self.view.selectionModel()
        if sm is None:
            return []
        return [(r.top(), r.bottom(), r.left(), r.right()) for r in sm.selection()]

    def selection_text(self):
        """(text, rows) for the selected cells, or (None, 0) when nothing is
        selected.  Everything selected gives the whole table with headings;
        a single cell gives just its value; other selections give the
        selected rows x columns with headings (unselected cells left empty
        when the selection is not a rectangle)."""
        table = self.model.table
        ranges = self._selection_ranges()
        if table is None or not ranges:
            return None, 0
        nrows, ncols = self.model.rowCount(), self.model.columnCount()
        rows = np.unique(np.concatenate([np.arange(t, b + 1) for t, b, _, _ in ranges]))
        cols = sorted({c for _, _, left, right in ranges for c in range(left, right + 1)})
        area = sum((b - t + 1) * (r - left + 1) for t, b, left, r in ranges)
        rectangle = len(ranges) == 1 or area == rows.size * len(cols)
        if rows.size == nrows and len(cols) == ncols and rectangle:
            return self.whole_text(), nrows                      # fast path
        single = rows.size == 1 and len(cols) == 1
        blank = None if rectangle else (
            lambda r, c: not any(t <= r <= b and left <= c <= right
                                 for t, b, left, right in ranges))
        text = table_text(table, self._show_units, self._unit, self._x_title,
                          rows=rows, cols=cols, header=not single, blank=blank)
        if single:
            text = text.rstrip("\n")
        return text, int(rows.size)

    def whole_text(self):
        table = self.model.table
        if table is None:
            return ""
        return table_text(table, self._show_units, self._unit, self._x_title)

    def copy(self, whole=False):
        table = self.model.table
        if table is None or table.row_count == 0:
            return 0
        if whole:
            text, n = self.whole_text(), table.row_count
        else:
            text, n = self.selection_text()
            if text is None:
                return 0
        QGuiApplication.clipboard().setText(text)
        self.copied.emit(n)
        return n

    @staticmethod
    def ask_path(parent):
        path, _ = QFileDialog.getSaveFileName(parent, "Export profile table", "profile.csv",
                                              "CSV (*.csv)")
        if path and not path.lower().endswith(".csv"):
            path += ".csv"
        return path
