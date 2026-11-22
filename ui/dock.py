"""The Profile Plot dock widget."""

import os

from qgis.core import (
    Qgis,
    QgsCoordinateTransform,
    QgsCsException,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
)
from qgis.gui import QgsDockWidget
from qgis.PyQt.QtCore import QSize, Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core import settings
from ..core.compat import QAction, QActionGroup
from ..core.export import export_alignment, write_csv
from ..core.sampling import grade_ratio_text
from ..core.session import ProfileSession
from . import icons
from .map_tool import ProfileCanvasItem, ProfileMapTool
from .plot_widget import ProfilePlotWidget, SeriesView
from .raster_list import AddRasterDialog, RasterPanel, layers_from_mime, mime_has_rasters
from .table_view import TablePanel


MINUS = "−"

SAMPLING_MODES = [
    ("native", "Native cells", "Every raster cell crossed by the line, drawn as steps (default)"),
    ("station", "1 cm stations", "A value every 0.01 m plus the end station (cell value)"),
    ("bilinear", "1 cm bilinear", "A value every 0.01 m, bilinear interpolation (labelled)"),
]


def _num(v, decimals=2):
    s = "{:.{}f}".format(v, decimals)
    return s.replace("-", MINUS)


def _tool_button(action, text_beside=False):
    b = QToolButton()
    b.setDefaultAction(action)
    b.setAutoRaise(True)
    b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon if text_beside
                         else Qt.ToolButtonStyle.ToolButtonIconOnly)
    b.setIconSize(QSize(18, 18))
    return b


def _separator():
    f = QFrame()
    f.setFrameShape(QFrame.Shape.VLine)
    f.setFrameShadow(QFrame.Shadow.Sunken)
    return f


class ProfileDock(QgsDockWidget):

    def __init__(self, iface, main_action, parent=None):
        super().__init__("Profile Plot", parent or iface.mainWindow())
        self.setObjectName("ProfilePlotDock")
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.main_action = main_action
        self.session = ProfileSession(self.canvas, self)
        self.map_tool = ProfileMapTool(self.canvas)
        self.map_tool.setAction(main_action)
        self._prev_tool = None
        self.canvas_item = None
        self._display_xf = None
        self._display_xf_key = None
        self._mapper_rev = None
        self._compact = False
        self.slope_unit = settings.slope_unit()

        self._build_ui()
        self._connect()
        self._apply_settings()
        self._update_placeholder()
        self._update_footer()
        self._update_actions()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QWidget()
        root.setAcceptDrops(True)
        root.dragEnterEvent = self._drag_enter
        root.dragMoveEvent = self._drag_enter
        root.dropEvent = self._drop
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        outer.addWidget(self.tabs)

        # --- Profile tab -------------------------------------------------
        prof = QWidget()
        pl = QVBoxLayout(prof)
        pl.setContentsMargins(4, 2, 4, 0)
        pl.setSpacing(2)

        bar = QHBoxLayout()
        bar.setSpacing(2)
        self.act_rasters = QAction(icons.icon("rasters"), "Rasters", self)
        self.act_rasters.setCheckable(True)
        self.act_rasters.setChecked(not settings.get("rastersCollapsed", False, bool))
        self.act_rasters.setToolTip("Show or hide the raster list")
        bar.addWidget(_tool_button(self.act_rasters))
        bar.addWidget(_separator())

        self.act_draw = QAction(icons.icon("draw_line"), "Draw line", self)
        self.act_select = QAction(icons.icon("use_selected"), "Use selected line", self)
        self.act_select.setToolTip("Profile the selected line feature")
        bar.addWidget(_tool_button(self.act_draw, True))
        bar.addWidget(_tool_button(self.act_select, True))
        bar.addWidget(_separator())

        self.act_hover = QAction(icons.icon("hover"), "Hover", self)
        self.act_hover.setCheckable(True)
        self.act_hover.setToolTip("Hover: point at a line to read its chainage and elevation")
        self.act_cross = QAction(icons.icon("crosshair"), "Crosshair", self)
        self.act_cross.setCheckable(True)
        self.act_cross.setToolTip("Crosshair: read chainage and elevation anywhere in the plot")
        grp = QActionGroup(self)
        grp.setExclusive(True)
        grp.addAction(self.act_hover)
        grp.addAction(self.act_cross)
        mode = settings.get("inspectMode", "crosshair", str)
        (self.act_hover if mode == "hover" else self.act_cross).setChecked(True)
        bar.addWidget(_tool_button(self.act_hover, True))
        bar.addWidget(_tool_button(self.act_cross, True))
        bar.addWidget(_separator())

        self.act_zoom = QAction(icons.icon("zoom_full"), "Zoom full", self)
        self.act_zoom.setToolTip("Zoom full (fit the whole profile)")
        b_zoom = _tool_button(self.act_zoom)
        b_zoom.setAccessibleName("Zoom full")
        bar.addWidget(b_zoom)
        bar.addWidget(_separator())
        self.act_export = QAction(icons.icon("temporary"), "Export line to temporary layer", self)
        self.act_export.setToolTip("Export line to temporary layer")
        b_export = _tool_button(self.act_export)
        b_export.setAccessibleName("Export line to temporary layer")
        bar.addWidget(b_export)
        self.image_button = QToolButton()
        self.image_button.setIcon(icons.icon("export"))
        self.image_button.setIconSize(QSize(18, 18))
        self.image_button.setAutoRaise(True)
        self.image_button.setToolTip("Export plot image\nClick: save as PNG\n"
                                     "Arrow: PNG, JPEG, SVG (vector) or copy to clipboard")
        self.image_button.setAccessibleName("Export plot image")
        self.image_button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.image_menu = QMenu(self.image_button)
        self.image_button.setMenu(self.image_menu)
        bar.addWidget(self.image_button)
        bar.addStretch(1)
        self.act_settings = QAction(icons.icon("settings"), "Settings", self)
        self.act_settings.setToolTip("Profile Plot settings (legend, A/B labels, line, axis "
                                     "titles, units, slope unit)")
        b_set = _tool_button(self.act_settings)
        b_set.setAccessibleName("Profile Plot settings")
        bar.addWidget(b_set)
        pl.addLayout(bar)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(True)
        self.raster_panel = RasterPanel(self.session)
        self.raster_panel.setMinimumWidth(150)
        self.plot = ProfilePlotWidget()
        self.splitter.addWidget(self.raster_panel)
        self.splitter.addWidget(self.plot)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        state = settings.get("splitter")
        if state is not None:
            try:
                self.splitter.restoreState(state)
            except TypeError:
                pass
        else:
            self.splitter.setSizes([235, 600])
        pl.addWidget(self.splitter, 1)

        # footer
        self.footer = QWidget()
        fl = QHBoxLayout(self.footer)
        fl.setContentsMargins(4, 3, 4, 3)
        fl.setSpacing(14)
        self.lbl_length = QLabel()
        self.lbl_dz = QLabel()
        self.lbl_slope = QLabel()
        self.lbl_slope.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        for w in (self.lbl_length, self.lbl_dz, self.lbl_slope):
            fl.addWidget(w)
        self.footer_slope_slot = QHBoxLayout()
        self.footer_slope_slot.setContentsMargins(0, 0, 0, 0)
        fl.addLayout(self.footer_slope_slot)
        fl.addStretch(1)
        fl.addWidget(QLabel("Sampling"))
        self.sampling_combo = QComboBox()
        for key, label, tip in SAMPLING_MODES:
            self.sampling_combo.addItem(label, key)
            self.sampling_combo.setItemData(self.sampling_combo.count() - 1, tip,
                                            Qt.ItemDataRole.ToolTipRole)
        mode = settings.get("samplingMode", "native", str)
        idx = self.sampling_combo.findData(mode)
        self.sampling_combo.setCurrentIndex(max(0, idx))
        self.session.set_mode(self.sampling_combo.currentData())
        fl.addWidget(self.sampling_combo)
        self.footer.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        pl.addWidget(self.footer)
        self.tabs.addTab(prof, "Profile")

        # --- Table tab -----------------------------------------------------
        self.table_panel = TablePanel()
        self.tabs.addTab(self.table_panel, "Table")
        self.setWidget(root)
        self._set_compact(not self.act_rasters.isChecked())

    def _connect(self):
        s = self.session
        s.profilesChanged.connect(self._on_profiles)
        s.lineChanged.connect(self._sync_canvas_item)
        s.rastersChanged.connect(self._on_rasters_changed)
        s.message.connect(self._message)
        self.act_draw.triggered.connect(self.start_drawing)
        self.act_select.triggered.connect(self.use_selected_line)
        self.act_hover.toggled.connect(self._on_inspect_mode)
        self.act_zoom.triggered.connect(self.plot.zoom_full)
        self.act_export.triggered.connect(self.export_line)
        self.act_settings.triggered.connect(self.open_settings)
        self.act_rasters.toggled.connect(self._on_rasters_toggled)
        self.sampling_combo.currentIndexChanged.connect(self._on_sampling)
        self.raster_panel.addRequested.connect(self.add_rasters)
        self.raster_panel.tree.layersDropped.connect(self._layers_dropped)
        self.splitter.splitterMoved.connect(self._on_splitter_moved)
        self.plot.cursorChainage.connect(self._on_plot_cursor)
        self.plot.add_export_actions(self.image_menu)
        self.image_button.clicked.connect(lambda: self.plot.save_image("png"))
        s.colorChanged.connect(lambda *_: self._on_profiles())
        self.plot.set_mode("hover" if self.act_hover.isChecked() else "crosshair")
        self.tabs.currentChanged.connect(lambda _i: self._refresh_table())
        self.table_panel.export_button.clicked.connect(self.export_csv)
        self.closedStateChanged.connect(self._on_closed_state)
        t = self.map_tool
        t.startRequested.connect(self._tool_start)
        t.vertexRequested.connect(self._tool_vertex)
        t.tailMoved.connect(self._tool_tail)
        t.finishRequested.connect(self._tool_finish)
        t.undoRequested.connect(self._tool_undo)
        t.cancelRequested.connect(self._tool_cancel)
        t.exitRequested.connect(self._exit_tool)
        self.canvas.destinationCrsChanged.connect(self._sync_canvas_item)
        self.main_action.changed.connect(self._update_shortcut_tooltips)
        self._update_shortcut_tooltips()
        self._table_timer = QTimer(self)
        self._table_timer.setSingleShot(True)
        self._table_timer.setInterval(150)
        self._table_timer.timeout.connect(self._refresh_table)

    # ------------------------------------------------------------------
    # shortcut / tooltips
    # ------------------------------------------------------------------

    def shortcut_text(self):
        try:
            from qgis.PyQt.QtGui import QKeySequence
            fmt = QKeySequence.SequenceFormat.NativeText
        except Exception:
            fmt = None
        sc = self.main_action.shortcut()
        if sc.isEmpty():
            return ""
        return sc.toString(fmt) if fmt is not None else sc.toString()

    def _update_shortcut_tooltips(self):
        sc = self.shortcut_text()
        how = ("Click to add vertices, Backspace to undo, right-click to finish, "
               "Esc to cancel.")
        if sc:
            self.act_draw.setToolTip("Draw line ({})\n{}".format(sc, how))
        else:
            self.act_draw.setToolTip(
                "Draw line\n{}\nTip: assign a shortcut in Settings ▸ Keyboard Shortcuts "
                "(search “Profile Plot”).".format(how))
        self._update_placeholder()

    # ------------------------------------------------------------------
    # drawing
    # ------------------------------------------------------------------

    def start_drawing(self):
        """Activate line capture.  Returns False when there is nothing to
        plot yet (no raster)."""
        if not self.session.rasters:
            self.raster_panel.add_button.setFocus()
            self._update_placeholder()
            self.iface.messageBar().pushMessage(
                "Profile Plot", "Add a raster first (Add raster, or drag layers into the dock).",
                Qgis.MessageLevel.Info, 3)
            return False
        current = self.canvas.mapTool()
        if current is not self.map_tool:
            self._prev_tool = current
            self.canvas.setMapTool(self.map_tool)
        self.main_action.setChecked(True)
        self.canvas.setFocus()
        return True

    def _exit_tool(self):
        if self.canvas.mapTool() is self.map_tool:
            prev = self._prev_tool
            self._prev_tool = None
            try:
                if prev is not None:
                    self.canvas.setMapTool(prev)
                else:
                    self.canvas.unsetMapTool(self.map_tool)
                    self.iface.actionPan().trigger()
            except RuntimeError:
                self.iface.actionPan().trigger()

    def _canvas_crs(self):
        return self.canvas.mapSettings().destinationCrs()

    def _tool_start(self, point):
        self.plot.reset_view()
        self.session.start_line(point, self._canvas_crs())

    def _tool_vertex(self, point):
        self.session.add_vertex(point)

    def _tool_tail(self, point):
        self.session.set_tail(point)
        item = self._ensure_canvas_item()
        item.set_tail(self._to_display(point))

    def _tool_finish(self):
        self.session.finish_line()
        self._update_actions()

    def _tool_undo(self):
        self.session.remove_last_vertex()
        if not self.session.has_line:
            self.map_tool.drawing = False

    def _tool_cancel(self):
        self.session.clear_line()
        self._update_actions()

    # ------------------------------------------------------------------
    # canvas item
    # ------------------------------------------------------------------

    def _ensure_canvas_item(self):
        if self.canvas_item is None:
            self.canvas_item = ProfileCanvasItem(self.canvas)
            self.canvas_item.set_show_ab(settings.show_ab())
        return self.canvas_item

    def _display_transform(self):
        src = self.session.line_crs
        dst = self._canvas_crs()
        key = (src.authid() + src.toWkt()[:64] if src else "", dst.authid() + dst.toWkt()[:64])
        if key != self._display_xf_key:
            self._display_xf_key = key
            self._display_xf = (None if (src is None or src == dst)
                                else QgsCoordinateTransform(src, dst, QgsProject.instance()))
        return self._display_xf

    def _to_display(self, p):
        xf = self._display_transform()
        if xf is None or p is None:
            return p
        try:
            return xf.transform(p)
        except QgsCsException:
            return p

    def _sync_canvas_item(self, *args):
        s = self.session
        if not s.has_line:
            if self.canvas_item is not None:
                self.canvas_item.clear()
            self._update_actions()
            return
        item = self._ensure_canvas_item()
        pts = [self._to_display(p) for p in s.line_points]
        tail = self._to_display(s.tail_point) if s.tail_point is not None else None
        item.set_line(pts, tail, s.finished)
        item.setVisible(True)
        self._update_actions()

    def _on_plot_cursor(self, chainage):
        if self.canvas_item is None or not self.session.has_line:
            return
        if chainage is None:
            self.canvas_item.set_marker(None)
            return
        if self._mapper_rev != (self.session.revision, self.session.tail_ch) or \
                self.session.mapper is None:
            self.session.update_mapper(self._canvas_crs())
            self._mapper_rev = (self.session.revision, self.session.tail_ch)
        self.canvas_item.set_marker(self.session.mapper.point_at(chainage))

    # ------------------------------------------------------------------
    # selected line
    # ------------------------------------------------------------------

    def _selected_line(self):
        layers = []
        active = self.iface.activeLayer()
        if isinstance(active, QgsVectorLayer):
            layers.append(active)
        layers += [lyr for lyr in QgsProject.instance().mapLayers().values()
                   if isinstance(lyr, QgsVectorLayer) and lyr is not active]
        for lyr in layers:
            is_line = lyr.geometryType() == Qgis.GeometryType.Line
            if not is_line or lyr.selectedFeatureCount() == 0:
                continue
            for f in lyr.getSelectedFeatures():
                g = f.geometry()
                if g is None or g.isEmpty():
                    continue
                return lyr, g
        return None, None

    def use_selected_line(self):
        if not self.session.rasters:
            self.start_drawing()  # shows the hint
            return
        layer, geom = self._selected_line()
        if geom is None:
            self.iface.messageBar().pushMessage(
                "Profile Plot", "Select a line feature first.", Qgis.MessageLevel.Info, 3)
            return
        g = QgsGeometry(geom)
        if g.constGet() is not None and g.constGet().hasCurvedSegments():
            g = QgsGeometry(g.constGet().segmentize())
        note = ""
        if g.isMultipart():
            merged = g.mergeLines()
            if merged is not None and not merged.isEmpty():
                g = merged
        if g.isMultipart():
            parts = g.asMultiPolyline()
            pts = parts[0] if parts else []
            note = " (multipart line: first part used)"
        else:
            pts = g.asPolyline()
        pts = [QgsPointXY(p) for p in pts]
        if len(pts) < 2 or not self.session.set_line(pts, layer.crs()):
            self.iface.messageBar().pushMessage(
                "Profile Plot", "The selected line has fewer than two distinct vertices.",
                Qgis.MessageLevel.Warning, 3)
            return
        self.map_tool.drawing = False
        self.plot.reset_view()
        if note:
            self.iface.messageBar().pushMessage("Profile Plot", "Profiling selected line" + note,
                                                Qgis.MessageLevel.Info, 3)

    # ------------------------------------------------------------------
    # rasters
    # ------------------------------------------------------------------

    def add_rasters(self):
        pre = set()
        try:
            pre = {lyr.id() for lyr in self.iface.layerTreeView().selectedLayers()
                   if isinstance(lyr, QgsRasterLayer)}
        except Exception:
            pass
        dlg = AddRasterDialog(self.session, pre, self)
        if not dlg.exec():
            return
        self.session.add_layers(dlg.selected_layers())
        new = []
        for path in dlg.files:
            lyr = QgsRasterLayer(path, os.path.splitext(os.path.basename(path))[0], "gdal")
            if lyr.isValid():
                new.append(lyr)
            else:
                self._message("Cannot open raster: {}".format(path), 1)
        if new:
            self.session.add_layers(new, owned=True)

    def _layers_dropped(self, project_layers, new_layers):
        if project_layers:
            self.session.add_layers(project_layers)
        if new_layers:
            self.session.add_layers(new_layers, owned=True)

    def _drag_enter(self, event):
        if mime_has_rasters(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def _drop(self, event):
        proj, new = layers_from_mime(event.mimeData())
        if proj or new:
            event.acceptProposedAction()
            self._layers_dropped(proj, new)

    def _on_rasters_changed(self):
        self._update_placeholder()
        self._on_profiles()
        QTimer.singleShot(0, self._fit_raster_panel)

    def _fit_raster_panel(self):
        """Widen the raster list so names and resolutions are not cut,
        keeping at least 55 % of the dock for the plot."""
        if self._compact or not self.raster_panel.isVisible():
            return
        sizes = self.splitter.sizes()
        total = sum(sizes)
        if total <= 0:
            return
        want = min(self.raster_panel.needed_width(), int(total * 0.45))
        if want > sizes[0]:
            self.splitter.setSizes([want, total - want])

    # ------------------------------------------------------------------
    # profile updates
    # ------------------------------------------------------------------

    def _on_profiles(self):
        s = self.session
        series = []
        any_preview = False
        for e in s.rasters:
            if not e.visible:
                continue
            p = s.profiles.get(e.uid)
            if p is None:
                continue
            any_preview |= p.preview
            series.append(SeriesView(e.uid, e.name, e.color, p))
        status = "Preview from overview — refining…" if any_preview else ""
        self.plot.set_series(series, s.total_length() if s.has_line else 0.0, status)
        self.image_button.setEnabled(bool(series))
        self._update_placeholder()
        self._update_footer()
        if self.tabs.currentWidget() is self.table_panel:
            self._table_timer.start()

    def _update_placeholder(self):
        s = self.session
        if not s.rasters:
            text = "Add rasters with “Add raster” or drag layers here,\nthen press Draw line."
        elif not any(e.visible for e in s.rasters):
            text = "All rasters are hidden."
        elif not s.has_line:
            sc = self.shortcut_text()
            text = "Press Draw line{} and click on the map.\nRight-click to finish.".format(
                " ({})".format(sc) if sc else "")
        else:
            text = ""
        self.plot.set_placeholder(text)

    def _update_footer(self):
        s = self.session
        cu = " m" if self.plot.show_units else ""
        vu = (" " + self.plot.unit) if self.plot.show_units else ""
        length = s.total_length() if s.has_line else 0.0
        self.lbl_length.setText("Length  {}{}".format(_num(length), cu))
        info = s.grade()
        if info is None:
            self.lbl_dz.hide()
            self.lbl_slope.hide()
            return
        self.lbl_dz.show()
        self.lbl_slope.show()
        if info.grade is None:
            self.lbl_dz.setText("ΔZ  –")
            self.lbl_slope.setText("Average slope  unavailable" if s.has_line else
                                   "Average slope  –")
            return
        self.lbl_dz.setText("ΔZ  {}{}".format(_num(info.dz), vu))
        if self.slope_unit == "m/m":
            text = "Average slope  {} m/m".format(_num(info.grade / 100.0, 4))
        else:
            text = "Average slope  {}%".format(_num(info.grade))
        ratio = grade_ratio_text(info.grade)
        if ratio and ratio != "flat":
            text += "  ({})".format(ratio)
        if info.across_gap:
            text += "  · endpoint grade across gap, coverage {:.0f}%".format(
                100 * info.coverage)
        if info.preview:
            text += "  (preview)"
        self.lbl_slope.setText(text)

    def _update_actions(self):
        s = self.session
        self.act_export.setEnabled(s.finished and s.distinct_vertex_count() >= 2)

    def _refresh_table(self):
        if self.tabs.currentWidget() is not self.table_panel:
            return
        s = self.session
        table = s.table() if s.profiles else None
        self.table_panel.set_table(table, self.plot.show_units, self.plot.unit, s.is_final())

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------

    def _on_inspect_mode(self, hover_checked):
        mode = "hover" if hover_checked else "crosshair"
        self.plot.set_mode(mode)
        settings.set("inspectMode", mode)

    def _on_sampling(self, index):
        mode = self.sampling_combo.itemData(index)
        settings.set("samplingMode", mode)
        self.session.set_mode(mode)

    def export_line(self):
        s = self.session
        if not (s.finished and s.distinct_vertex_count() >= 2):
            return
        export_alignment(s.line_points, s.line_crs, s.fixed_length())

    def export_csv(self):
        s = self.session
        if not s.is_final():
            return
        table = s.table()
        if table.row_count == 0:
            return
        path = TablePanel.ask_path(self)
        if not path:
            return
        entries = [e for e in s.visible_entries() if e.uid in s.profiles]
        dtypes = [e.spec.store_dtype if e.spec is not None else None for e in entries]
        try:
            n = write_csv(path, table, self.plot.show_units, self.plot.unit, dtypes)
        except OSError as exc:
            self._message("Cannot write {}: {}".format(path, exc), 2)
            return
        self.iface.messageBar().pushMessage(
            "Profile Plot", "Exported {:,} rows to {}".format(n, path), Qgis.MessageLevel.Success, 4)

    def open_settings(self):
        from .settings_dialog import SettingsDialog
        dlg = SettingsDialog(self)
        if dlg.exec():
            self._apply_settings()

    def _apply_settings(self):
        show = settings.show_units()
        unit = settings.unit_label()
        self.slope_unit = settings.slope_unit()
        self.plot.set_units(show, unit)
        self.plot.set_show_legend(settings.show_legend())
        self.plot.set_line_style(settings.line_style())
        self.plot.set_axis_titles(settings.x_title(), settings.y_title())
        if self.canvas_item is not None:
            self.canvas_item.set_show_ab(settings.show_ab())
        self.raster_panel.set_show_units(show)
        self._update_footer()
        self._refresh_table()

    # ------------------------------------------------------------------
    # compact layout
    # ------------------------------------------------------------------

    def _on_rasters_toggled(self, shown):
        settings.set("rastersCollapsed", not shown)
        if shown and self.splitter.sizes()[0] == 0:
            self.splitter.setSizes([235, max(200, self.splitter.width() - 235)])
        self._set_compact(not shown)

    def _on_splitter_moved(self, *_):
        settings.set("splitter", self.splitter.saveState())
        collapsed = self.splitter.sizes()[0] == 0
        if collapsed != self._compact:
            self.act_rasters.blockSignals(True)
            self.act_rasters.setChecked(not collapsed)
            self.act_rasters.blockSignals(False)
            self._set_compact(collapsed)

    def _set_compact(self, compact):
        """Collapsed raster list: the slope selector moves beside the grade."""
        self._compact = compact
        self.raster_panel.setVisible(not compact)
        if compact:
            box = self.raster_panel.take_slope_box()
            box.setParent(self.footer)
            self.footer_slope_slot.addWidget(box)
            box.show()
        else:
            self.footer_slope_slot.removeWidget(self.raster_panel.slope_box)
            self.raster_panel.restore_slope_box()

    # ------------------------------------------------------------------
    # misc
    # ------------------------------------------------------------------

    def _message(self, text, level=0):
        levels = {0: Qgis.MessageLevel.Info, 1: Qgis.MessageLevel.Warning,
                  2: Qgis.MessageLevel.Critical}
        self.iface.messageBar().pushMessage("Profile Plot", text,
                                            levels.get(level, Qgis.MessageLevel.Info), 5)

    def _on_closed_state(self, closed):
        """Closing the dock resets the tool: nothing that was plotted is
        kept, so it reopens empty.  Settings are preferences and stay."""
        if closed:
            self._exit_tool()
            self.map_tool.drawing = False
            self.session.reset()
            self.plot.reset_view()
            if self.canvas_item is not None:
                self.canvas_item.clear()
                self.canvas_item.setVisible(False)
            self.tabs.setCurrentIndex(0)
            self._refresh_table()
        elif self.canvas_item is not None:
            self.canvas_item.setVisible(True)

    def cleanup(self):
        """Called when the plugin unloads."""
        try:
            self._exit_tool()
        except Exception:
            pass
        try:
            self.canvas.destinationCrsChanged.disconnect(self._sync_canvas_item)
        except (TypeError, RuntimeError):
            pass
        try:
            self.main_action.changed.disconnect(self._update_shortcut_tooltips)
        except (TypeError, RuntimeError):
            pass
        if self.canvas_item is not None:
            scene = self.canvas.scene()
            if scene is not None:
                scene.removeItem(self.canvas_item)
            self.canvas_item = None
        self.session.shutdown()
