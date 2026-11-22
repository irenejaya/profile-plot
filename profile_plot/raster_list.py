"""Raster list panel: rows with visibility, colour, name and cell size."""

from qgis.core import Qgis, QgsMimeDataUtils, QgsProject, QgsRasterLayer, QgsUnitTypes
from qgis.gui import QgsColorButton, QgsColorDialog
from qgis.PyQt.QtCore import QSize, Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import icons
from .compat import set_text_alignment

UID_ROLE = 0x0100 + 1  # Qt.UserRole + 1 (plain int works on Qt5 and Qt6)
COL_VISIBLE, COL_COLOR, COL_NAME, COL_CELL = range(4)


def _raster_layer_type():
    return Qgis.LayerType.Raster


def layers_from_mime(mime):
    """Raster layers referenced by a drag from the Layers panel or the
    Browser.  Returns ``(project_layers, new_layers)``."""
    project_layers, new_layers = [], []
    if mime is None or not QgsMimeDataUtils.isUriList(mime):
        return project_layers, new_layers
    for uri in QgsMimeDataUtils.decodeUriList(mime):
        layer_id = getattr(uri, "layerId", "")
        if layer_id:
            lyr = QgsProject.instance().mapLayer(layer_id)
            if isinstance(lyr, QgsRasterLayer):
                project_layers.append(lyr)
            continue
        if uri.layerType != "raster":
            continue
        lyr = QgsRasterLayer(uri.uri, uri.name, uri.providerKey or "gdal")
        if lyr.isValid():
            new_layers.append(lyr)
    return project_layers, new_layers


def mime_has_rasters(mime):
    if mime is None or not QgsMimeDataUtils.isUriList(mime):
        return False
    for uri in QgsMimeDataUtils.decodeUriList(mime):
        if getattr(uri, "layerId", "") or uri.layerType == "raster":
            return True
    return False


def format_cell_size(entry, show_units):
    sx, sy = entry.cell_size()

    def f(v):
        if v >= 0.01:
            return "{:.2f}".format(v)
        return "{:.3g}".format(v)
    text = f(sx) if abs(sx - sy) <= 1e-9 * max(sx, sy, 1) else "{} × {}".format(f(sx), f(sy))
    if show_units:
        try:
            unit = QgsUnitTypes.toAbbreviatedString(entry.crs.mapUnits())
        except Exception:
            unit = ""
        if unit:
            text += " " + unit
    return text


class RasterTree(QTreeWidget):
    """List widget accepting layer drops and internal reordering."""

    layersDropped = pyqtSignal(list, list)
    orderChanged = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setColumnCount(4)
        self.setHeaderHidden(True)
        self.setRootIsDecorated(False)
        self.setIndentation(0)
        self.setUniformRowHeights(True)
        self.setIconSize(QSize(14, 14))
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        hdr = self.header()
        hdr.setStretchLastSection(False)
        hdr.setMinimumSectionSize(4)
        hdr.setSectionResizeMode(COL_VISIBLE, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(COL_COLOR, QHeaderView.ResizeMode.Fixed)
        hdr.resizeSection(COL_COLOR, 32)
        hdr.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(COL_CELL, QHeaderView.ResizeMode.ResizeToContents)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def dragEnterEvent(self, event):
        if event.source() is self or mime_has_rasters(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.source() is self:
            super().dragMoveEvent(event)
        elif mime_has_rasters(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        if event.source() is self:
            super().dropEvent(event)
            uids = [self.topLevelItem(i).data(0, UID_ROLE)
                    for i in range(self.topLevelItemCount())]
            self.orderChanged.emit(uids)
            return
        proj, new = layers_from_mime(event.mimeData())
        if proj or new:
            event.acceptProposedAction()
            self.layersDropped.emit(proj, new)
        else:
            event.ignore()


class RasterPanel(QWidget):
    """Left side of the dock: header, raster rows and the slope selector."""

    addRequested = pyqtSignal()
    collapseRequested = pyqtSignal()

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        self.show_units = False
        self._updating = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(4)
        self.title = QLabel("Rasters (0)")
        f = self.title.font()
        f.setBold(True)
        self.title.setFont(f)
        head.addWidget(self.title)
        head.addStretch(1)
        self.add_button = QToolButton()
        self.add_button.setIcon(icons.icon("add"))
        self.add_button.setIconSize(QSize(18, 18))
        self.add_button.setAutoRaise(True)
        self.add_button.setAccessibleName("Add raster")
        self.add_button.setToolTip("Add raster\nAdd one or more rasters from the project or from files.\n"
                                   "You can also drag layers here from the Layers panel or Browser.")
        self.add_button.clicked.connect(self.addRequested)
        head.addWidget(self.add_button)
        lay.addLayout(head)

        self.tree = RasterTree(self)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.orderChanged.connect(self.session.reorder)
        lay.addWidget(self.tree, 1)

        # Slope selector (re-parented to the footer in the compact layout).
        self.slope_box = QWidget(self)
        sl = QHBoxLayout(self.slope_box)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(6)
        self.slope_label = QLabel("Slope layer")
        sl.addWidget(self.slope_label)
        self.slope_combo = QComboBox()
        self.slope_combo.setToolTip("Raster used for ΔZ and average slope (start to end of the line).\n"
                                    "Leave as (none) to hide the grade.")
        self.slope_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.slope_combo.setMinimumContentsLength(10)
        self.slope_combo.setMaximumWidth(260)
        self.slope_combo.currentIndexChanged.connect(self._on_slope_changed)
        sl.addWidget(self.slope_combo, 1)
        self.slope_slot = QVBoxLayout()
        self.slope_slot.setContentsMargins(0, 0, 0, 0)
        self.slope_slot.addWidget(self.slope_box)
        lay.addLayout(self.slope_slot)

        session.rastersChanged.connect(self.refresh)
        session.colorChanged.connect(self._on_color_changed)
        session.slopeChanged.connect(self._sync_slope_combo)
        self.refresh()

    # -- rows -----------------------------------------------------------------

    def refresh(self):
        self._updating = True
        try:
            sel = self.current_uid()
            self.tree.clear()
            for e in self.session.rasters:
                it = QTreeWidgetItem()
                it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable |
                            Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsDragEnabled)
                it.setData(0, UID_ROLE, e.uid)
                it.setCheckState(COL_VISIBLE, Qt.CheckState.Checked if e.visible
                                 else Qt.CheckState.Unchecked)
                it.setToolTip(COL_VISIBLE, "Show or hide this curve")
                it.setText(COL_NAME, e.name)
                tip = e.layer.source()
                if e.error:
                    tip = e.error + "\n" + tip
                it.setToolTip(COL_NAME, tip)
                it.setText(COL_CELL, format_cell_size(e, self.show_units))
                set_text_alignment(it, COL_CELL, Qt.AlignmentFlag.AlignRight,
                                   Qt.AlignmentFlag.AlignVCenter)
                it.setToolTip(COL_CELL, "Cell size")
                self.tree.addTopLevelItem(it)
                self.tree.setItemWidget(it, COL_COLOR, self._color_button(e))
                if e.uid == sel:
                    self.tree.setCurrentItem(it)
            self.title.setText("Rasters ({})".format(len(self.session.rasters)))
            self._refresh_slope_combo()
        finally:
            self._updating = False

    def _refresh_slope_combo(self):
        combo = self.slope_combo
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("(none)", None)
        for e in self.session.rasters:
            combo.addItem(icons.color_swatch(e.color), e.name, e.uid)
        idx = combo.findData(self.session.slope_uid) if self.session.slope_uid else 0
        combo.setCurrentIndex(max(idx, 0))
        if idx < 0:
            self.session.set_slope(None)
        combo.blockSignals(False)

    def _color_button(self, e):
        """QGIS colour box: click it to pick the curve colour."""
        btn = QgsColorButton()
        btn.setShowMenu(False)
        btn.setAllowOpacity(False)
        btn.setColorDialogTitle("Curve colour")
        btn.setColor(e.color)
        btn.setFixedSize(26, 16)
        btn.setToolTip("Click to change the colour of this curve")
        btn.setAccessibleName("Colour of {}".format(e.name))
        btn.setProperty("uid", e.uid)
        btn.colorChanged.connect(lambda c, uid=e.uid: self.session.set_color(uid, c))
        return btn

    def color_button(self, uid):
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            if it.data(0, UID_ROLE) == uid:
                return self.tree.itemWidget(it, COL_COLOR)
        return None

    def _on_color_changed(self, uid, color):
        btn = self.color_button(uid)
        if btn is not None and btn.color() != color:
            btn.blockSignals(True)
            btn.setColor(color)
            btn.blockSignals(False)
        idx = self.slope_combo.findData(uid)
        if idx >= 0:
            self.slope_combo.setItemIcon(idx, icons.color_swatch(color))

    def _sync_slope_combo(self):
        combo = self.slope_combo
        idx = combo.findData(self.session.slope_uid) if self.session.slope_uid else 0
        if idx >= 0 and idx != combo.currentIndex():
            combo.blockSignals(True)
            combo.setCurrentIndex(idx)
            combo.blockSignals(False)

    def set_show_units(self, show):
        self.show_units = show
        self.refresh()

    def current_uid(self):
        it = self.tree.currentItem()
        return it.data(0, UID_ROLE) if it is not None else None

    def _on_item_changed(self, item, column):
        if self._updating or column != COL_VISIBLE:
            return
        uid = item.data(0, UID_ROLE)
        self.session.set_visible(uid, item.checkState(COL_VISIBLE) == Qt.CheckState.Checked)

    def _on_slope_changed(self, index):
        self.session.set_slope(self.slope_combo.itemData(index))

    def _choose_color(self, uid):
        e = self.session.entry(uid)
        if e is None:
            return
        c = QgsColorDialog.getColor(e.color, self, "Curve colour")
        if c.isValid():
            self.session.set_color(uid, c)

    def _context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if item is None:
            menu = QMenu(self)
            menu.addAction(icons.icon("add"), "Add raster…", self.addRequested.emit)
            menu.exec(self.tree.viewport().mapToGlobal(pos))
            return
        uid = item.data(0, UID_ROLE)
        e = self.session.entry(uid)
        if e is None:
            return
        menu = QMenu(self)
        n = e.layer.bandCount()
        if n > 1:
            bands = menu.addMenu("Band")
            for b in range(1, n + 1):
                name = e.layer.bandName(b) or "Band {}".format(b)
                act = bands.addAction(name)
                act.setCheckable(True)
                act.setChecked(b == e.band)
                act.triggered.connect(lambda _=False, b=b: self.session.set_band(uid, b))
        idx = self.session.rasters.index(e)
        up = menu.addAction(icons.icon("up"), "Move up",
                            lambda: self.session.move_entry(uid, idx - 1))
        up.setEnabled(idx > 0)
        down = menu.addAction(icons.icon("down"), "Move down",
                              lambda: self.session.move_entry(uid, idx + 1))
        down.setEnabled(idx < len(self.session.rasters) - 1)
        menu.addAction("Change colour…", lambda: self._choose_color(uid))
        menu.addSeparator()
        menu.addAction(icons.icon("remove"), "Remove", lambda: self.session.remove_entry(uid))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    # -- compact layout ------------------------------------------------------------

    def take_slope_box(self):
        self.slope_slot.removeWidget(self.slope_box)
        return self.slope_box

    def restore_slope_box(self):
        self.slope_box.setParent(self)
        self.slope_slot.addWidget(self.slope_box)
        self.slope_box.show()


class AddRasterDialog(QDialog):
    """Pick several project rasters at once, or browse for files."""

    def __init__(self, session, preselect_ids=(), parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add raster")
        self.session = session
        self.files = []
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Rasters in the project:"))
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        lay.addWidget(self.list, 1)
        rtype = _raster_layer_type()
        layers = [lyr for lyr in QgsProject.instance().mapLayers().values()
                  if lyr.type() == rtype and lyr.isValid()]
        root = QgsProject.instance().layerTreeRoot()
        order = {lyr.id(): i for i, lyr in enumerate(root.layerOrder())}
        layers.sort(key=lambda lyr: order.get(lyr.id(), 1 << 20))
        for lyr in layers:
            it = QListWidgetItem(lyr.name())
            it.setData(Qt.ItemDataRole.UserRole, lyr.id())
            it.setToolTip(lyr.source())
            already = session.has_layer(lyr.id())
            it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
                        | Qt.ItemFlag.ItemIsSelectable)
            if already:
                it.setFlags(Qt.ItemFlag.NoItemFlags)
                it.setText(lyr.name() + "  (added)")
            it.setCheckState(Qt.CheckState.Checked if (lyr.id() in preselect_ids and not already)
                             else Qt.CheckState.Unchecked)
            self.list.addItem(it)
        if not layers:
            self.list.addItem(QListWidgetItem("No raster layers in the project"))
        self.list.itemDoubleClicked.connect(self._toggle)
        row = QHBoxLayout()
        browse = QToolButton()
        browse.setText("Browse files…")
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        self.files_label = QLabel("")
        row.addWidget(self.files_label, 1)
        lay.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                              QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.resize(380, 360)

    def _toggle(self, item):
        if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
            item.setCheckState(Qt.CheckState.Unchecked if item.checkState() == Qt.CheckState.Checked
                               else Qt.CheckState.Checked)

    def _browse(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add raster files", "",
            "Rasters (*.tif *.tiff *.vrt *.asc *.flt *.img *.ecw *.jp2 *.nc *.bil *.dem *.adf);;"
            "All files (*)")
        if paths:
            self.files = paths
            self.files_label.setText("{} file(s) selected".format(len(paths)))

    def selected_layers(self):
        out = []
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.checkState() == Qt.CheckState.Checked or (
                    it.isSelected() and it.flags() & Qt.ItemFlag.ItemIsUserCheckable):
                lyr = QgsProject.instance().mapLayer(it.data(Qt.ItemDataRole.UserRole) or "")
                if lyr is not None:
                    out.append(lyr)
        return out
