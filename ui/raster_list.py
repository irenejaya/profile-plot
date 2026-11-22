"""Raster list panel: rows with visibility, colour, name and cell size."""

from qgis.core import Qgis, QgsMimeDataUtils, QgsProject, QgsRasterLayer, QgsUnitTypes
from qgis.gui import QgsColorDialog
from qgis.PyQt.QtCore import QRect, QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QPainter, QPalette, QPen
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QBoxLayout,
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
    QSizePolicy,
    QStyle,
    QStyleOptionComboBox,
    QStyleOptionHeader,
    QStyledItemDelegate,
    QStylePainter,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.compat import set_text_alignment
from . import icons


def flag_align_left_vcenter():
    return Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter


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


class ColorSwatchButton(QToolButton):
    """Solid colour box; click it to pick a colour with the QGIS colour
    dialog.  Drawn with the same swatch as the slope selector."""

    colorChanged = pyqtSignal(QColor)

    def __init__(self, color, parent=None):
        super().__init__(parent)
        self._color = QColor(color)
        self.dialog_title = "Curve colour"
        self.setAutoRaise(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        # Fills its table cell; only the centred swatch is painted, so the
        # row lines underneath stay visible.
        self.setMinimumSize(icons.SWATCH_SIZE + 4, icons.SWATCH_SIZE + 4)
        self.clicked.connect(self.pick_color)

    def color(self):
        return QColor(self._color)

    def setColor(self, color):  # noqa: N802 (Qt-style API)
        color = QColor(color)
        if not color.isValid() or color == self._color:
            return
        self._color = color
        self.update()
        self.colorChanged.emit(QColor(color))

    def pick_color(self):
        c = QgsColorDialog.getColor(self._color, self, self.dialog_title, False)
        if c.isValid():
            self.setColor(c)

    def paintEvent(self, event):
        p = QPainter(self)
        s = icons.SWATCH_SIZE
        rect = QRect((self.width() - s) // 2, (self.height() - s) // 2, s, s)
        if self.underMouse() or self.hasFocus():
            hl = QColor(self.palette().color(QPalette.ColorRole.Highlight))
            hl.setAlpha(90)
            p.fillRect(rect.adjusted(-3, -3, 3, 3), hl)
        icons.paint_swatch(p, rect, self._color)
        p.end()


class ElidedComboBox(QComboBox):
    """Combo box that shortens a long current text with "…" instead of
    clipping it mid-letter."""

    def paintEvent(self, event):
        painter = QStylePainter(self)
        opt = QStyleOptionComboBox()
        self.initStyleOption(opt)
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, opt)
        field = self.style().subControlRect(QStyle.ComplexControl.CC_ComboBox, opt,
                                            QStyle.SubControl.SC_ComboBoxEditField, self)
        avail = field.width() - 4
        if not opt.currentIcon.isNull():
            avail -= opt.iconSize.width() + 4
        opt.currentText = self.fontMetrics().elidedText(
            opt.currentText, Qt.TextElideMode.ElideRight, max(avail, 0))
        painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, opt)


def grid_color(palette):
    base = palette.color(QPalette.ColorRole.Base)
    text = palette.color(QPalette.ColorRole.Text)
    t = 0.16
    return QColor(int(base.red() * (1 - t) + text.red() * t),
                  int(base.green() * (1 - t) + text.green() * t),
                  int(base.blue() * (1 - t) + text.blue() * t))


class GridDelegate(QStyledItemDelegate):
    """Table look for the raster rows: a line under every row and a column
    divider between the name and the resolution."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.row_height = 0

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        painter.save()
        painter.setPen(QPen(grid_color(option.palette), 1))
        r = option.rect
        painter.drawLine(r.left(), r.bottom(), r.right(), r.bottom())
        if index.column() == COL_NAME:
            painter.drawLine(r.right(), r.top(), r.right(), r.bottom())
        painter.restore()

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        size.setHeight(max(size.height(), self.row_height))
        return size


RESOLUTION_HEADER = "\u0394x"   # Δx
RESOLUTION_TIP = "\u0394x: resolution (cell size)"
NAME_SPAN = (COL_VISIBLE, COL_COLOR, COL_NAME)


class MergedHeader(QHeaderView):
    """Header with one "Name" section spanning the tick, colour and name
    columns."""

    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)

    def _span_rect(self, rect):
        x0 = self.sectionViewportPosition(NAME_SPAN[0])
        last = NAME_SPAN[-1]
        x1 = self.sectionViewportPosition(last) + self.sectionSize(last)
        return QRect(x0, rect.y(), x1 - x0, rect.height())

    def paintSection(self, painter, rect, logical_index):  # noqa: N802
        if not rect.isValid():
            return
        opt = QStyleOptionHeader()
        try:
            self.initStyleOption(opt)
        except AttributeError:
            opt.initFrom(self)
        model = self.model()
        if logical_index in NAME_SPAN:
            opt.rect = self._span_rect(rect)
            opt.section = NAME_SPAN[0]
            opt.text = model.headerData(COL_NAME, Qt.Orientation.Horizontal) if model else ""
            opt.textAlignment = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            opt.position = QStyleOptionHeader.SectionPosition.Beginning
        else:
            opt.rect = rect
            opt.section = logical_index
            opt.text = model.headerData(logical_index, Qt.Orientation.Horizontal) if model else ""
            opt.textAlignment = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            opt.position = QStyleOptionHeader.SectionPosition.End
        opt.text = opt.text or ""
        painter.save()
        painter.setClipRect(opt.rect)
        self.style().drawControl(QStyle.ControlElement.CE_Header, opt, painter, self)
        painter.restore()


class RasterTree(QTreeWidget):
    """List widget accepting layer drops and internal reordering."""

    layersDropped = pyqtSignal(list, list)
    orderChanged = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setColumnCount(4)
        self.setHeader(MergedHeader(self))
        self.setHeaderLabels(["", "", "Name", RESOLUTION_HEADER])
        self._delegate = GridDelegate(self)
        self._delegate.row_height = max(self.fontMetrics().height() + 8, icons.SWATCH_SIZE + 10)
        self.setItemDelegate(self._delegate)
        self.setAllColumnsShowFocus(True)
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
        hdr.resizeSection(COL_COLOR, icons.SWATCH_SIZE + 12)
        hdr.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(COL_CELL, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionsMovable(False)
        hdr.setSectionsClickable(False)
        hdr.setHighlightSections(False)
        hdr.setDefaultAlignment(flag_align_left_vcenter())
        set_text_alignment(self.headerItem(), COL_CELL, Qt.AlignmentFlag.AlignRight,
                           Qt.AlignmentFlag.AlignVCenter)
        self.headerItem().setToolTip(COL_CELL, RESOLUTION_TIP)
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
        # In the panel the label sits above a full-width selector, so the
        # selector lines up with the raster box; in the compact layout the
        # same widget sits inline in the footer.
        self.slope_box = QWidget(self)
        self._slope_layout = QBoxLayout(QBoxLayout.Direction.TopToBottom, self.slope_box)
        self._slope_layout.setContentsMargins(0, 0, 0, 0)
        self.slope_label = QLabel("Slope layer")
        self._slope_layout.addWidget(self.slope_label)
        self.slope_combo = ElidedComboBox()
        self.slope_combo.setMinimumContentsLength(4)
        self.slope_combo.setIconSize(QSize(icons.SWATCH_SIZE, icons.SWATCH_SIZE))
        self.slope_combo.currentIndexChanged.connect(self._on_slope_changed)
        self._slope_layout.addWidget(self.slope_combo, 1)
        self.slope_slot = QVBoxLayout()
        self.slope_slot.setContentsMargins(0, 0, 0, 0)
        self.slope_slot.addWidget(self.slope_box)
        lay.addLayout(self.slope_slot)
        self._set_slope_layout(compact=False)

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
                tip = e.name + "\n" + e.layer.source()
                if e.error:
                    tip = e.error + "\n" + tip
                it.setToolTip(COL_NAME, tip)
                it.setText(COL_CELL, format_cell_size(e, self.show_units))
                set_text_alignment(it, COL_CELL, Qt.AlignmentFlag.AlignRight,
                                   Qt.AlignmentFlag.AlignVCenter)
                it.setToolTip(COL_CELL, RESOLUTION_TIP)
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
        # The drop-down list shows full names even when the box is narrow.
        fm = combo.fontMetrics()
        widest = max([fm.horizontalAdvance(combo.itemText(i)) for i in range(combo.count())] + [0])
        combo.view().setMinimumWidth(widest + icons.SWATCH_SIZE + 36)
        self._update_slope_tooltip()

    def _update_slope_tooltip(self):
        name = self.slope_combo.currentText()
        self.slope_combo.setToolTip(
            "Slope layer: {}\nRaster used for ΔZ and average slope (start to end of the "
            "line).\nLeave as (none) to hide the grade.".format(name))

    def _color_button(self, e):
        """QGIS colour box: click it to pick the curve colour."""
        btn = ColorSwatchButton(e.color)
        btn.dialog_title = "Curve colour – {}".format(e.name)
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
            self._update_slope_tooltip()

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
        self._update_slope_tooltip()

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

    def _set_slope_layout(self, compact):
        combo = self.slope_combo
        if compact:
            self._slope_layout.setDirection(QBoxLayout.Direction.LeftToRight)
            self._slope_layout.setSpacing(6)
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
            combo.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            combo.setMaximumWidth(320)
        else:
            self._slope_layout.setDirection(QBoxLayout.Direction.TopToBottom)
            self._slope_layout.setSpacing(2)
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            combo.setMaximumWidth(16777215)
        combo.updateGeometry()

    def needed_width(self):
        """Panel width that shows every name and resolution in full."""
        tree = self.tree
        fm = tree.fontMetrics()
        hdr = tree.header()
        name_w = max([fm.horizontalAdvance(e.name) for e in self.session.rasters] +
                     [fm.horizontalAdvance("Name")]) + 16
        cell_w = max([fm.horizontalAdvance(format_cell_size(e, self.show_units))
                      for e in self.session.rasters] + [fm.horizontalAdvance(RESOLUTION_HEADER)]) + 18
        cols = hdr.sectionSize(COL_VISIBLE) + hdr.sectionSize(COL_COLOR) + name_w + cell_w
        frame = tree.frameWidth() * 2 + tree.verticalScrollBar().sizeHint().width()
        margins = self.layout().contentsMargins()
        return cols + frame + margins.left() + margins.right()

    def take_slope_box(self):
        self.slope_slot.removeWidget(self.slope_box)
        self._set_slope_layout(compact=True)
        return self.slope_box

    def restore_slope_box(self):
        self._set_slope_layout(compact=False)
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
