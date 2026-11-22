"""Headless integration tests against a real QGIS (offscreen).

Run with:  QT_QPA_PLATFORM=offscreen python3 -m pytest tests
Skipped automatically when PyQGIS is not importable.
"""

import csv
import os
import time

import numpy as np
import pytest

qgis_core = pytest.importorskip("qgis.core")

import harness as H  # noqa: E402
from qgis.core import (  # noqa: E402
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QPointF, Qt  # noqa: E402
from qgis.PyQt.QtGui import QKeySequence  # noqa: E402


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    app = H.app()
    from qgis.core import QgsSettings
    QgsSettings().remove("ProfilePlot")     # start every run from the defaults
    folder = str(tmp_path_factory.mktemp("rasters"))
    paths = H.make_creek_rasters(folder)
    QgsProject.instance().removeAllMapLayers()
    layers = H.load_layers(paths, ["LiDAR DEM", "1% AEP WSE", "5% AEP WSE"])
    iface = H.FakeIface()
    H.setup_canvas(iface)
    from profile_plot import classFactory
    plugin = classFactory(iface)
    plugin.initGui()
    yield {"app": app, "iface": iface, "plugin": plugin, "layers": layers,
           "folder": folder}
    plugin.unload()
    QgsProject.instance().removeAllMapLayers()


def draw(env, points, finish=True, moves=8):
    """Simulate clicks (with intermediate mouse moves) through the tool."""
    iface, plugin = env["iface"], env["plugin"]
    tool = plugin.dock.map_tool
    canvas = iface.canvas
    prev = None
    for x, y in points:
        if prev is not None:
            for k in range(1, moves + 1):
                t = k / float(moves)
                H.mouse(tool, canvas, "move", prev[0] + (x - prev[0]) * t,
                        prev[1] + (y - prev[1]) * t)
                env["app"].processEvents()
        H.mouse(tool, canvas, "press", x, y)
        env["app"].processEvents()
        prev = (x, y)
    if finish:
        H.mouse(tool, canvas, "press", prev[0], prev[1], Qt.MouseButton.RightButton)
    return H.wait_idle(plugin.dock.session)


Y = H.ORIGIN_Y - 20.0
X0 = H.ORIGIN_X


def test_action_registered_without_default_shortcut(env):
    acts = env["iface"].registered
    assert len(acts) == 1
    action, shortcut = acts[0]
    assert shortcut == ""
    assert action.objectName() == "mActionProfilePlot"
    assert action.shortcut().isEmpty()


def test_run_without_rasters_opens_dock_only(env):
    plugin = env["plugin"]
    plugin.run()
    dock = plugin.dock
    assert dock.isVisible()
    assert env["iface"].canvas.mapTool() is not dock.map_tool
    assert not plugin.action.isChecked()


def test_shortcut_shown_in_tooltips(env):
    plugin = env["plugin"]
    plugin.action.setShortcut(QKeySequence("Ctrl+Alt+L"))
    env["app"].processEvents()
    assert "Ctrl+Alt+L" in plugin.action.toolTip()
    assert "Ctrl+Alt+L" in plugin.dock.act_draw.toolTip()
    plugin.action.setShortcut(QKeySequence())
    env["app"].processEvents()
    assert "Keyboard Shortcuts" in plugin.dock.act_draw.toolTip()


def test_add_rasters_and_start_drawing(env):
    plugin = env["plugin"]
    dock = plugin.dock
    dock.session.add_layers(env["layers"])
    assert [e.name for e in dock.session.rasters] == ["LiDAR DEM", "1% AEP WSE", "5% AEP WSE"]
    assert dock.raster_panel.title.text() == "Rasters (3)"
    plugin.run()  # same action: dock open -> start drawing
    assert env["iface"].canvas.mapTool() is dock.map_tool
    assert plugin.action.isChecked()


def test_live_plot_while_drawing(env):
    dock = env["plugin"].dock
    s = dock.session
    tool = dock.map_tool
    H.mouse(tool, env["iface"].canvas, "press", X0, Y)
    H.mouse(tool, env["iface"].canvas, "move", X0 + 30, Y)
    H.wait_idle(s)
    # The moving tail is already plotted, before any second click.
    p = s.profiles[s.rasters[0].uid]
    assert s.total_length() == pytest.approx(30, abs=0.2)
    assert p.length == pytest.approx(s.total_length())
    assert np.isfinite(p.v).all()
    H.mouse(tool, env["iface"].canvas, "press", X0 + 30, Y)
    H.mouse(tool, env["iface"].canvas, "move", X0 + 60, Y)
    H.wait_idle(s)
    assert s.total_length() == pytest.approx(60, abs=0.3)
    # Backspace undo removes the last vertex
    s.remove_last_vertex()
    H.wait_idle(s)
    assert len(s.line_points) == 1
    # Esc cancels
    tool.drawing = True
    from qgis.PyQt.QtGui import QKeyEvent
    from qgis.PyQt.QtCore import QEvent
    tool.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                 Qt.KeyboardModifier.NoModifier))
    assert not s.has_line


def test_full_profile_native(env):
    dock = env["plugin"].dock
    s = dock.session
    draw(env, [(X0, Y), (X0 + 100, Y)])
    assert s.finished
    dem = s.profiles[s.rasters[0].uid]
    # 0.1 m cells across 100 m -> 1000 native cells, chainage 0..100
    assert dem.kind == "steps"
    assert dem.x0.size == pytest.approx(1000, abs=2)
    assert dem.x0[0] == 0.0 and dem.x1[-1] == pytest.approx(100.0, abs=0.15)
    assert not dem.preview and not dem.pending
    wse = s.profiles[s.rasters[1].uid]
    assert np.isnan(wse.start_value) and wse.has_gap
    assert np.nanmax(wse.v) == pytest.approx(80.8)
    # Tool stays active for the next line
    assert env["iface"].canvas.mapTool() is dock.map_tool
    assert dock.act_export.isEnabled()


def test_grade_slope_layer_optional(env):
    dock = env["plugin"].dock
    s = dock.session
    assert s.grade() is None
    assert dock.lbl_slope.isHidden()
    s.set_slope(s.rasters[0].uid)
    H.wait_idle(s)
    g = s.grade()
    dem = s.profiles[s.rasters[0].uid]
    assert g.grade == pytest.approx(100 * (dem.end_value - dem.start_value) / dem.length)
    assert "Average slope" in dock.lbl_slope.text()
    # Hidden slope layer -> grade hidden too
    s.set_visible(s.rasters[0].uid, False)
    H.wait_idle(s)
    assert s.grade() is None
    s.set_visible(s.rasters[0].uid, True)
    H.wait_idle(s)
    assert s.grade() is not None


def test_table_and_csv_agree_with_inspection(env, tmp_path):
    dock = env["plugin"].dock
    s = dock.session
    tbl = s.table()
    assert tbl.kind == "intervals"
    assert tbl.names == ["LiDAR DEM", "1% AEP WSE", "5% AEP WSE"]
    path = str(tmp_path / "p.csv")
    from profile_plot.core.export import write_csv
    n = write_csv(path, tbl, False, "m", [np.float32] * 3)
    assert n == tbl.row_count
    with open(path) as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == ["From", "To", "LiDAR DEM", "1% AEP WSE", "5% AEP WSE"]
    assert rows[1][3] == "NoData"
    # Inspection readout and CSV agree at test stations
    for ch in (5.05, 37.0, 50.0, 50.55, 62.3, 99.95):
        i = int(np.searchsorted(tbl.from_ch, ch, side="right")) - 1
        row = rows[i + 1]
        for k, e in enumerate(s.rasters):
            v = s.profiles[e.uid].value_at(ch)
            csv_v = row[2 + k]
            if np.isfinite(v):
                assert float(csv_v) == pytest.approx(v, abs=1e-4)
            else:
                assert csv_v == "NoData"
    # Units in headings when switched on
    from profile_plot.core.export import headers
    assert headers(tbl, True, "m AHD")[:3] == ["From (m)", "To (m)", "LiDAR DEM (m AHD)"]


def test_export_temporary_layer(env):
    dock = env["plugin"].dock
    s = dock.session
    before = set(QgsProject.instance().mapLayers())
    dock.export_line()
    new = [QgsProject.instance().mapLayer(i) for i in set(QgsProject.instance().mapLayers()) - before]
    assert len(new) == 1
    lyr = new[0]
    assert lyr.name() == "Profile alignment"
    assert lyr.crs() == s.line_crs
    f = next(lyr.getFeatures())
    pts = f.geometry().asPolyline()
    assert [(p.x(), p.y()) for p in pts] == [(p.x(), p.y()) for p in s.line_points]
    QgsProject.instance().removeMapLayer(lyr.id())


def test_hover_and_crosshair_overlay(env):
    from qgis.PyQt.QtGui import QColor
    dock = env["plugin"].dock
    plot = dock.plot
    dock.resize(1000, 330)
    dock.show()
    env["app"].processEvents()
    plot.grab()  # render once
    s = dock.session
    dem = s.profiles[s.rasters[0].uid]
    # Crosshair at chainage 50
    px, py = plot._to_px(50.0, dem.value_at(50.0))
    plot._cursor = QPointF(px, py)
    plot.set_mode("crosshair")
    assert not plot.grab().isNull()
    # Hover: one row per raster with a value, just coloured dot + value,
    # highest first; rasters without a value at that chainage are left out
    plot.set_mode("hover")
    rows = plot.hover_rows(50.0)
    expected = sorted((p.value_at(50.0), e.color.name())
                      for e, p in ((e, s.profiles[e.uid]) for e in s.rasters
                                   if e.visible and e.uid in s.profiles)
                      if np.isfinite(p.value_at(50.0)))[::-1]
    assert [(c.name(), t) for c, t in rows] == [(c, "{:.2f}".format(v)) for v, c in expected]
    assert all(not any(ch.isalpha() for ch in t) for _, t in rows)   # no names
    assert len(plot.hover_rows(5.0)) == 1          # only the DEM has data at 5 m
    assert plot.hover_chainage_text(316.1) == "316.10"
    # The dashed guide is drawn the full height at the pointer, in both
    # halves of the plot (not only near a curve)
    img = plot.grab().toImage()
    r = plot._plot_rect
    dpr = img.devicePixelRatio()
    col = int(round(px * dpr))
    bg = QColor(img.pixel(col + int(8 * dpr), int((r.top() + r.height() * 0.85) * dpr)))
    marks = [QColor(img.pixel(col, int(y * dpr))) != bg
             for y in np.linspace(r.top() + r.height() * 0.6, r.bottom() - 3, 40)]
    assert sum(marks) >= 8
    plot._cursor = None


def test_pointer_moves_do_not_rerender(env):
    plot = env["plugin"].dock.plot
    plot.grab()
    n = plot.render_count
    t0 = time.perf_counter()
    for i in range(30):
        plot._cursor = QPointF(plot._plot_rect.left() + 10 + i * 5, plot._plot_rect.center().y())
        plot.grab()
    dt = (time.perf_counter() - t0) / 30
    assert plot.render_count == n  # static layer reused
    assert dt < 0.05


def test_rapid_redraws_never_restore_older_line(env):
    dock = env["plugin"].dock
    s = dock.session
    tool = dock.map_tool
    c = env["iface"].canvas
    # Start several lines quickly without waiting for results.
    for k in range(5):
        H.mouse(tool, c, "press", X0 + 2 * k, Y)
        H.mouse(tool, c, "press", X0 + 10 + 15 * k, Y - 5)
        H.mouse(tool, c, "press", X0 + 10 + 15 * k, Y - 5, Qt.MouseButton.RightButton)
    H.wait_idle(s)
    last_len = np.hypot(10 + 15 * 4 - 8, 5)
    assert s.total_length() == pytest.approx(last_len, abs=0.15)
    for p in s.profiles.values():
        assert p.length == pytest.approx(last_len, abs=0.15)
        assert p.x1 is None or p.x1[-1] == pytest.approx(last_len, abs=0.15)


def test_station_mode(env):
    dock = env["plugin"].dock
    s = dock.session
    dock.sampling_combo.setCurrentIndex(dock.sampling_combo.findData("station"))
    draw(env, [(X0, Y), (X0 + 100, Y)])
    p = s.profiles[s.rasters[0].uid]
    assert p.kind == "points"
    assert p.x0.size == 10001
    assert p.x0[1] == pytest.approx(0.01)
    tbl = s.table()
    assert tbl.kind == "stations" and tbl.row_count == 10001
    dock.sampling_combo.setCurrentIndex(dock.sampling_combo.findData("bilinear"))
    H.wait_idle(s)
    p = s.profiles[s.rasters[0].uid]
    assert p.interpolated
    dock.sampling_combo.setCurrentIndex(dock.sampling_combo.findData("native"))
    H.wait_idle(s)
    assert s.profiles[s.rasters[0].uid].kind == "steps"


def test_use_selected_line(env):
    dock = env["plugin"].dock
    s = dock.session
    vl = QgsVectorLayer("LineString?crs=EPSG:7856", "lines", "memory")
    f = QgsFeature()
    pts = [QgsPointXY(X0 + 5, Y + 5), QgsPointXY(X0 + 40, Y - 10), QgsPointXY(X0 + 90, Y)]
    f.setGeometry(QgsGeometry.fromPolylineXY(pts))
    vl.dataProvider().addFeatures([f])
    QgsProject.instance().addMapLayer(vl)
    vl.selectAll()
    env["iface"].setActiveLayer(vl)
    dock.use_selected_line()
    H.wait_idle(s)
    expected = QgsGeometry.fromPolylineXY(pts).length()
    assert s.finished
    assert s.total_length() == pytest.approx(expected, rel=1e-9)
    assert s.profiles[s.rasters[0].uid].x1[-1] == pytest.approx(expected, abs=1e-6)
    QgsProject.instance().removeMapLayer(vl.id())


def test_endpoint_grade_example(env, tmp_path):
    """Fall from 82.00 to 80.55 over 100 m gives -1.45 % (1 in 69 falling)."""
    dock = env["plugin"].dock
    s = dock.session
    arr = np.tile(np.linspace(82.0, 80.55, 101, dtype=np.float64), (3, 1)).astype(np.float32)
    gt = (X0 - 0.5, 1.0, 0.0, Y + 1.5, 0.0, -1.0)
    path = H.write_tif(str(tmp_path / "grade.tif"), arr, gt, -9999, block=16)
    lyr = QgsRasterLayer(path, "grade", "gdal")
    QgsProject.instance().addMapLayer(lyr)
    s.add_layers([lyr])
    e = s.rasters[-1]
    s.set_slope(e.uid)
    draw(env, [(X0, Y), (X0 + 100, Y)])
    g = s.grade()
    assert round(g.grade, 2) == -1.45
    assert "1 in 69 falling" in dock.lbl_slope.text()
    # Reverse the line: the sign flips
    draw(env, [(X0 + 100, Y), (X0, Y)])
    assert round(s.grade().grade, 2) == 1.45
    QgsProject.instance().removeMapLayer(lyr.id())
    assert e.uid not in [x.uid for x in s.rasters]
    assert s.slope_uid is None


def test_raster_in_other_crs(env, tmp_path):
    """A raster in geographic GDA2020 is sampled along the same chainage."""
    from qgis.core import QgsCoordinateTransform
    dock = env["plugin"].dock
    s = dock.session
    src = QgsCoordinateReferenceSystem("EPSG:7856")
    dst = QgsCoordinateReferenceSystem("EPSG:7844")
    xf = QgsCoordinateTransform(src, dst, QgsProject.instance())
    a = xf.transform(QgsPointXY(X0 - 10, H.ORIGIN_Y + 10))
    b = xf.transform(QgsPointXY(X0 + 110, H.ORIGIN_Y - 50))
    nx, ny = 240, 120
    rx = (b.x() - a.x()) / nx
    ry = (a.y() - b.y()) / ny
    lon = a.x() + (np.arange(nx) + 0.5) * rx
    arr = np.tile(np.where(lon < xf.transform(QgsPointXY(X0 + 50, Y)).x(), 10.0, 20.0),
                  (ny, 1)).astype(np.float32)
    path = H.write_tif(str(tmp_path / "geo.tif"), arr, (a.x(), rx, 0, a.y(), 0, -ry),
                       -9999, epsg=7844, block=16)
    lyr = QgsRasterLayer(path, "geo", "gdal")
    QgsProject.instance().addMapLayer(lyr)
    s.add_layers([lyr])
    draw(env, [(X0, Y), (X0 + 100, Y)])
    p = s.profiles[s.rasters[-1].uid]
    # The step from 10 to 20 lies within one geographic cell of chainage 50
    i = int(np.argmax(p.v >= 20))
    cell_m = rx * 111320 * np.cos(np.radians(a.y()))
    assert abs(p.x0[i] - 50.0) <= cell_m + 0.01
    assert p.x1[-1] == pytest.approx(100.0, abs=1e-6)
    QgsProject.instance().removeMapLayer(lyr.id())


def test_units_setting(env):
    from profile_plot.core import settings
    dock = env["plugin"].dock
    settings.set("showUnits", True)
    settings.set("unit", "m AHD")
    dock._apply_settings()
    assert dock.plot.unit_suffix() == " m AHD"
    assert dock.plot.unit_suffix(True) == " m"
    assert dock.lbl_length.text().endswith(" m")
    settings.set("showUnits", False)
    dock._apply_settings()
    assert dock.plot.unit_suffix() == ""
    assert not dock.lbl_length.text().endswith(" m")


def test_compact_layout_moves_slope_selector(env):
    dock = env["plugin"].dock
    box = dock.raster_panel.slope_box
    dock.act_rasters.setChecked(False)
    assert box.parent() is dock.footer
    dock.act_rasters.setChecked(True)
    assert box.parent() is dock.raster_panel


def test_layer_removal_releases_tiles(env):
    dock = env["plugin"].dock
    s = dock.session
    e = s.rasters[2]
    key = e.spec.key
    assert any(k[0] == key for k in s.engine.cache._data)
    QgsProject.instance().removeMapLayer(e.layer_id)
    env["app"].processEvents()
    assert e.uid not in [x.uid for x in s.rasters]
    assert not any(k[0] == key for k in s.engine.cache._data)


# ---------------------------------------------------------------------------
# UI refinements: + button, colours, legend, tooltips, image export
# ---------------------------------------------------------------------------

def test_add_button_is_icon_only(env):
    b = env["plugin"].dock.raster_panel.add_button
    assert b.text() == "" and not b.icon().isNull()
    assert b.accessibleName() == "Add raster"


def test_temporary_layer_and_image_export_buttons(env):
    dock = env["plugin"].dock
    assert dock.act_export.toolTip() == "Export line to temporary layer"
    assert not dock.act_export.icon().isNull()
    texts = [a.text() for a in dock.image_menu.actions() if a.text()]
    assert texts == ["Save as PNG…", "Save as JPEG…", "Save as SVG (vector)…",
                     "Copy to clipboard"]


def test_color_presets_then_red(env):
    from profile_plot.core.session import OVERFLOW_COLOR, PRESET_COLORS
    from qgis.PyQt.QtGui import QColor
    s = env["plugin"].dock.session
    assert PRESET_COLORS[0] == "#8c5a2b" and len(PRESET_COLORS) == 6
    assert [e.color.name() for e in s.rasters[:2]] == PRESET_COLORS[:2]

    class Fake:
        def __init__(self, c):
            self.color = QColor(c)
    saved = s.rasters
    try:
        s.rasters = []
        got = []
        for _ in range(8):
            c = s.next_color()
            got.append(c)
            s.rasters.append(Fake(c))
        assert got == PRESET_COLORS + [OVERFLOW_COLOR, OVERFLOW_COLOR]
    finally:
        s.rasters = saved


def test_color_box_changes_curve_colour_without_rebuilding(env):
    from qgis.PyQt.QtGui import QColor
    dock = env["plugin"].dock
    s = dock.session
    e = s.rasters[0]
    btn = dock.raster_panel.color_button(e.uid)
    assert btn is not None and btn.color() == e.color
    btn.setColor(QColor("#123456"))
    env["app"].processEvents()
    assert e.color.name() == "#123456"
    assert dock.raster_panel.color_button(e.uid) is btn   # list not rebuilt
    assert dock.plot.series[0].color.name() == "#123456"
    s.set_color(e.uid, QColor("#8c5a2b"))
    assert btn.color().name() == "#8c5a2b"


def test_legend_is_a_settings_tickbox(env):
    from profile_plot.core import settings
    from profile_plot.ui.settings_dialog import SettingsDialog
    dock = env["plugin"].dock
    assert not hasattr(dock, "act_legend")          # no toolbar icon
    dlg = SettingsDialog(dock)
    assert dlg.show_legend.text() == "Show legend"
    dlg.show_legend.setChecked(True)
    dlg.accept()
    dock._apply_settings()
    assert dock.plot.show_legend
    dlg = SettingsDialog(dock)
    assert dlg.show_legend.isChecked()
    dlg.show_legend.setChecked(False)
    dlg.accept()
    dock._apply_settings()
    assert not dock.plot.show_legend
    assert settings.show_legend() is False


def test_slope_unit_setting(env):
    from profile_plot.core import settings
    from profile_plot.ui.settings_dialog import SettingsDialog
    from qgis.core import QgsSettings
    dock = env["plugin"].dock
    s = dock.session
    s.set_slope(s.rasters[0].uid)
    draw(env, [(X0, Y), (X0 + 100, Y)])
    QgsSettings().remove(settings.PREFIX + "slopeUnit")
    assert settings.slope_unit() == "%"            # default
    dock._apply_settings()
    g = s.grade().grade
    assert dock.lbl_slope.text().startswith("Average slope  {}%".format(
        "{:.2f}".format(g).replace("-", "\u2212")))
    dlg = SettingsDialog(dock)
    dlg.slope_unit.setCurrentIndex(dlg.slope_unit.findData("m/m"))
    dlg.accept()
    dock._apply_settings()
    expected = "{:.4f} m/m".format(g / 100).replace("-", "\u2212")
    assert expected in dock.lbl_slope.text()
    settings.set("slopeUnit", "%")
    dock._apply_settings()
    assert "%" in dock.lbl_slope.text()


def test_legend_has_no_chainage_title(env):
    plot = env["plugin"].dock.plot
    plot.set_show_legend(True)
    plot.grab()
    r = plot._plot_rect
    plot._cursor = QPointF(r.center().x(), r.center().y())
    img = plot.grab()       # renders legend with values, no "Ch" title
    assert not img.isNull()
    plot._cursor = None
    plot.set_show_legend(False)


def test_raster_box_is_a_table(env):
    from profile_plot.ui.raster_list import (COL_CELL, COL_COLOR, COL_NAME, COL_VISIBLE,
                                          GridDelegate, MergedHeader)
    tree = env["plugin"].dock.raster_panel.tree
    assert isinstance(tree.itemDelegate(), GridDelegate)
    assert not tree.isHeaderHidden()
    # One "Name" header over tick, colour and name; resolution is Δx
    assert isinstance(tree.header(), MergedHeader)
    assert tree.headerItem().text(COL_VISIBLE) == ""
    assert tree.headerItem().text(COL_COLOR) == ""
    assert tree.headerItem().text(COL_NAME) == "Name"
    assert tree.headerItem().text(COL_CELL) == "\u0394x"
    assert not tree.header().grab().isNull()
    # Names and resolutions are not cut once the panel fits itself
    dock = env["plugin"].dock
    dock.resize(1100, 340)
    dock.show()
    env["app"].processEvents()
    dock._fit_raster_panel()
    env["app"].processEvents()
    fm = tree.fontMetrics()
    widest = max(fm.horizontalAdvance(e.name) for e in dock.session.rasters)
    assert tree.header().sectionSize(COL_NAME) >= widest + 8


def test_legend_hidden_by_default(tmp_path):
    from profile_plot.ui.plot_widget import ProfilePlotWidget
    assert ProfilePlotWidget().show_legend is False
    from profile_plot.core import settings
    from qgis.core import QgsSettings
    QgsSettings().remove(settings.PREFIX + "showLegend")
    assert settings.get("showLegend", False, bool) is False


def test_tooltip_formats(env):
    plot = env["plugin"].dock.plot
    plot.set_units(False, "m")
    assert plot.crosshair_text(50.0, -100.0) == "x[50.00], y[-100.00]"
    assert plot.hover_chainage_text(50.0) == "50.00"
    plot.set_units(True, "m AHD")
    assert plot.crosshair_text(50.0, 78.5) == "x[50.00 m], y[78.50 m AHD]"
    assert plot.hover_chainage_text(50.0) == "50.00 m"
    plot.set_units(False, "m")


def test_image_export(env, tmp_path):
    from qgis.PyQt.QtGui import QImage
    plot = env["plugin"].dock.plot
    plot.resize(600, 300)
    for fmt in ("png", "jpg", "svg"):
        path = str(tmp_path / ("plot." + fmt))
        assert plot.export_to(path, fmt)
        assert os.path.getsize(path) > 1000
    img = QImage(str(tmp_path / "plot.png"))
    assert (img.width(), img.height()) == (1200, 600)   # 2x resolution
    with open(str(tmp_path / "plot.svg")) as fh:
        assert "<svg" in fh.read(500)


def test_slope_selector_matches_raster_box(env):
    """Same swatch as the raster rows, full width under the raster box, and
    the full name fits in the compact footer."""
    from profile_plot.ui import icons
    from profile_plot.ui.raster_list import ColorSwatchButton
    dock = env["plugin"].dock
    panel = dock.raster_panel
    s = dock.session
    e = s.rasters[0]
    s.set_slope(e.uid)
    dock.resize(1000, 340)
    dock.show()
    env["app"].processEvents()
    btn = panel.color_button(e.uid)
    assert isinstance(btn, ColorSwatchButton)
    combo = panel.slope_combo
    idx = combo.findData(e.uid)
    a = combo.itemIcon(idx).pixmap(icons.SWATCH_SIZE, icons.SWATCH_SIZE).toImage()
    b = icons.color_swatch(e.color).pixmap(icons.SWATCH_SIZE, icons.SWATCH_SIZE).toImage()
    assert a == b
    # Panel: selector as wide as the raster box, left edges aligned
    tree_geo = panel.tree.geometry()
    combo_left = combo.mapTo(panel, combo.rect().topLeft()).x()
    assert abs(combo_left - tree_geo.left()) <= 2
    assert abs(combo.width() - tree_geo.width()) <= 2
    # Compact: inline in the footer, wide enough for the full name
    dock.act_rasters.setChecked(False)
    env["app"].processEvents()
    fm = combo.fontMetrics()
    assert combo.width() >= fm.horizontalAdvance(combo.currentText()) + icons.SWATCH_SIZE
    dock.act_rasters.setChecked(True)
    env["app"].processEvents()


def _plot_mouse_move(plot, x, y):
    from qgis.PyQt.QtCore import QEvent
    from qgis.PyQt.QtGui import QMouseEvent
    from qgis.PyQt.QtWidgets import QApplication
    local = QPointF(x, y)
    ev = QMouseEvent(QEvent.Type.MouseMove, local, QPointF(plot.mapToGlobal(local.toPoint())),
                     Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                     Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(plot, ev)


def _plot_leave(plot):
    from qgis.PyQt.QtCore import QEvent
    from qgis.PyQt.QtWidgets import QApplication
    QApplication.sendEvent(plot, QEvent(QEvent.Type.Leave))


@pytest.mark.parametrize("mode", ["hover", "crosshair"])
def test_map_marker_follows_plot_pointer(env, mode):
    """Real mouse moves over the plot put the yellow marker on the map line
    at the same chainage, in both inspection modes; leaving clears it."""
    dock = env["plugin"].dock
    s = dock.session
    draw(env, [(X0, Y), (X0 + 100, Y)])
    (dock.act_hover if mode == "hover" else dock.act_cross).setChecked(True)
    dock.resize(1000, 340)
    dock.show()
    plot = dock.plot
    plot.grab()
    env["app"].processEvents()
    a, b = s.line_points[0], s.line_points[-1]
    length = s.total_length()
    for ch in (10.0, 50.0, 87.5):
        dem = s.profiles[s.rasters[0].uid]
        px, py = plot._to_px(ch, dem.value_at(ch))
        _plot_mouse_move(plot, px, py)
        env["app"].processEvents()
        m = dock.canvas_item.marker
        assert m is not None
        t = ch / length
        assert m.x() == pytest.approx(a.x() + t * (b.x() - a.x()), abs=1e-6)
        assert m.y() == pytest.approx(a.y() + t * (b.y() - a.y()), abs=1e-6)
    _plot_leave(plot)
    env["app"].processEvents()
    assert dock.canvas_item.marker is None


def test_map_marker_on_line_still_being_drawn(env):
    """While a line is being drawn (moving tail), hovering the plot marks
    the live line too, including the tail segment."""
    dock = env["plugin"].dock
    s = dock.session
    tool = dock.map_tool
    c = env["iface"].canvas
    H.mouse(tool, c, "press", X0, Y)
    H.mouse(tool, c, "press", X0 + 40, Y)
    H.mouse(tool, c, "move", X0 + 40, Y - 30)     # tail goes south
    H.wait_idle(s)
    assert tool.drawing and s.tail_ch is not None
    plot = dock.plot
    plot.grab()
    total = s.total_length()
    fixed = s.fixed_length()
    ch = fixed + 0.5 * (total - fixed)              # middle of the tail
    px, _ = plot._to_px(ch, 0)
    _plot_mouse_move(plot, px, plot._plot_rect.center().y())
    env["app"].processEvents()
    m = dock.canvas_item.marker
    assert m is not None
    start = s.line_points[-1]
    tail = s.tail_point
    t = (ch - fixed) / (total - fixed)
    assert m.x() == pytest.approx(start.x() + t * (tail.x() - start.x()), abs=1e-6)
    assert m.y() == pytest.approx(start.y() + t * (tail.y() - start.y()), abs=1e-6)
    _plot_leave(plot)
    s.clear_line()
    tool.drawing = False


def test_minor_grid_spacing():
    from profile_plot.ui.plot_widget import minor_step, minor_ticks
    assert minor_step(20, 7.6) == 1.0          # 100 m profile: every 1 m
    assert minor_step(1, 55) == pytest.approx(0.2)
    assert minor_step(2000, 0.076) == pytest.approx(200)   # 10 km: legible
    assert minor_step(10, 0.1) is None          # too dense: no minor grid
    assert minor_ticks(0, 10, 5, 1) == [1, 2, 3, 4, 6, 7, 8, 9]


def test_end_tick_label_inside_plot(env):
    """The last chainage label (e.g. 10000) is not cut at the right edge."""
    from qgis.PyQt.QtGui import QColor, QFontMetricsF
    plot = env["plugin"].dock.plot
    plot.resize(600, 260)
    saved = (plot.view, plot.auto_view)
    plot.auto_view = False
    plot.view = (0.0, 10000.0, 0.0, 100.0)
    img = plot.render_image(scale=1.0, opaque=True)
    r = plot._plot_rect
    fm = QFontMetricsF(plot.font())
    bg = QColor(img.pixel(img.width() - 1, 2))
    band = range(int(r.bottom()) + 3, int(r.bottom() + fm.height() + 4))
    for x in (img.width() - 1, img.width() - 2):
        assert all(QColor(img.pixel(x, y)) == bg for y in band)
    # ...and the label itself was drawn just inside the edge
    inside = [QColor(img.pixel(x, y)) != bg for x in range(img.width() - 40, img.width() - 2)
              for y in band]
    assert any(inside)
    plot.view, plot.auto_view = saved


def _steps_profile(x0, x1, v):
    from profile_plot.core.assembly import RasterProfile
    x0 = np.asarray(x0, float)
    x1 = np.asarray(x1, float)
    v = np.asarray(v, float)
    return RasterProfile("steps", x0, x1, v, float(x1[-1]))


def test_smooth_line_through_cell_centres_and_steps_option():
    from qgis.PyQt.QtCore import QRectF
    from profile_plot.ui.plot_widget import series_polylines
    prof = _steps_profile([0, 1, 2], [1, 2, 3], [10, 12, 11])
    rect = QRectF(0, 0, 300, 100)          # 100 px per metre, y 10..12 -> 0..100
    smooth = series_polylines(prof, 0, 3, 10, 12, rect, "smooth")
    assert len(smooth) == 1
    xs = smooth[0][:, 0].tolist()
    assert xs == [0, 50, 150, 250, 300]     # ends + cell centres
    steps = series_polylines(prof, 0, 3, 10, 12, rect, "steps")
    assert steps[0][:, 0].tolist() == [0, 100, 100, 200, 200, 300]


def test_zoomed_out_line_is_smooth_and_keeps_spikes():
    """The zoomed-out drawing keeps points at their true chainage (a ramp
    stays a straight diagonal, not vertical bars) and keeps narrow spikes."""
    from qgis.PyQt.QtCore import QRectF
    from profile_plot.ui.plot_widget import series_polylines
    n = 100000                              # 10 km of 0.1 m cells
    x0 = np.arange(n) * 0.1
    v = x0 * 0.01                            # a smooth 1 % ramp, 0..100
    v[61234] += 20.0                         # one narrow embankment cell
    prof = _steps_profile(x0, x0 + 0.1, v)
    rect = QRectF(0, 0, 800, 400)
    lines = series_polylines(prof, 0, n * 0.1, 0, 130, rect, "smooth")
    pts = np.vstack(lines)
    assert pts.shape[0] < 4 * 800 + 10       # decimated
    ys = 130 - pts[:, 1] * 130 / 400         # pixel row back to elevation
    ch = pts[:, 0] / 800 * (n * 0.1)        # pixel column back to chainage
    assert ys.max() == pytest.approx(v.max(), abs=1e-6)    # spike kept
    # every kept point away from the spike lies on the ramp
    away = np.abs(ch - 6123.45) > 20
    assert np.abs(ys[away] - ch[away] * 0.01).max() < 0.02
    # no vertical bars: x positions are distinct for consecutive points
    assert (np.diff(pts[:, 0]) > 0).mean() > 0.4


def test_axis_titles_and_line_style_settings(env):
    from profile_plot.core import settings
    from profile_plot.ui.settings_dialog import SettingsDialog
    dock = env["plugin"].dock
    dlg = SettingsDialog(dock)
    assert dlg.x_title.text() == "Chainage" and dlg.y_title.text() == "Elevation"
    assert dlg.line_style.currentData() == "smooth"
    dlg.x_title.setText("Distance along creek")
    dlg.y_title.setText("Level")
    dlg.line_style.setCurrentIndex(dlg.line_style.findData("steps"))
    dlg.accept()
    dock._apply_settings()
    assert (dock.plot.x_title, dock.plot.y_title) == ("Distance along creek", "Level")
    assert dock.plot.line_style == "steps"
    assert not dock.plot.render_image(1.0).isNull()
    # empty titles fall back to the defaults
    dlg = SettingsDialog(dock)
    dlg.x_title.setText("")
    dlg.y_title.setText("  ")
    dlg.line_style.setCurrentIndex(dlg.line_style.findData("smooth"))
    dlg.accept()
    dock._apply_settings()
    assert (settings.x_title(), settings.y_title()) == ("Chainage", "Elevation")
    assert (dock.plot.x_title, dock.plot.y_title) == ("Chainage", "Elevation")
    assert dock.plot.line_style == "smooth"


def test_ab_labels_setting(env):
    """A/B labels are off by default (line only) and switch on in Settings."""
    from profile_plot.core import settings
    from profile_plot.ui.settings_dialog import SettingsDialog
    from qgis.core import QgsSettings
    from qgis.PyQt.QtGui import QImage, QPainter
    dock = env["plugin"].dock
    QgsSettings().remove(settings.PREFIX + "showAB")
    assert settings.show_ab() is False
    draw(env, [(X0, Y), (X0 + 100, Y)])
    dock._apply_settings()
    item = dock.canvas_item
    assert item.show_ab is False

    calls = []
    original = item._label
    item._label = lambda painter, at, text: calls.append(text)
    try:
        img = QImage(400, 300, QImage.Format.Format_ARGB32_Premultiplied)
        p = QPainter(img)
        item.paint(p)
        p.end()
        assert calls == []                       # line only
        dlg = SettingsDialog(dock)
        assert dlg.show_ab.isChecked() is False
        dlg.show_ab.setChecked(True)
        dlg.accept()
        dock._apply_settings()
        assert item.show_ab is True
        p = QPainter(img)
        item.paint(p)
        p.end()
        assert calls == ["A", "B"]
    finally:
        item._label = original
        settings.set("showAB", False)
        dock._apply_settings()


def test_own_toolbar(env):
    """The action lives in its own 'Profile Plot' toolbar, not the shared
    Plugins toolbar."""
    from qgis.PyQt.QtWidgets import QToolBar
    plugin = env["plugin"]
    tb = plugin.toolbar
    assert isinstance(tb, QToolBar)
    assert tb.objectName() == "ProfilePlotToolbar"
    assert tb.windowTitle() == "Profile Plot"
    assert plugin.action in tb.actions()
    assert env["iface"].mw.findChild(QToolBar, "ProfilePlotToolbar") is tb


def test_closing_the_dock_resets_the_tool(env):
    """Close the dock: the line, plot, table, rasters and slope layer are
    gone and it reopens empty.  (Last test: it restores the rasters.)"""
    plugin = env["plugin"]
    dock = plugin.dock
    s = dock.session

    def alive():
        out = []
        for lyr in env["layers"]:
            try:
                if QgsProject.instance().mapLayer(lyr.id()) is not None:
                    out.append(lyr)
            except RuntimeError:      # deleted by an earlier test
                pass
        return out
    if not s.rasters:
        s.add_layers(alive())
    s.set_slope(s.rasters[0].uid)
    draw(env, [(X0, Y), (X0 + 60, Y)])
    assert s.profiles and s.has_line and dock.canvas_item.points
    cache_before = len(s.engine.cache)
    assert cache_before > 0
    dock.close()
    env["app"].processEvents()
    assert not s.rasters and not s.profiles and not s.has_line
    assert s.slope_uid is None
    assert dock.raster_panel.tree.topLevelItemCount() == 0
    assert dock.raster_panel.slope_combo.currentText() == "(none)"
    assert dock.plot.series == []
    assert dock.canvas_item.points == [] and not dock.canvas_item.isVisible()
    assert len(s.engine.cache) == 0
    assert env["iface"].canvas.mapTool() is not dock.map_tool
    # Reopen with the icon/shortcut: empty dock, nothing plotted
    plugin.run()
    env["app"].processEvents()
    assert dock.isVisible() and not s.rasters and not s.has_line
    assert "Add rasters" in dock.plot.placeholder
    # restore for any later tests
    s.add_layers(alive())


def test_unload_removes_toolbar_and_reload_has_one(tmp_path):
    """Unload detaches the toolbar at once; loading again gives exactly one."""
    from qgis.PyQt.QtWidgets import QToolBar
    from profile_plot import classFactory
    iface = H.FakeIface()
    for _ in range(2):
        plugin = classFactory(iface)
        plugin.initGui()
        bars = iface.mw.findChildren(QToolBar, "ProfilePlotToolbar")
        assert len(bars) == 1
        plugin.unload()
        assert iface.mw.findChildren(QToolBar, "ProfilePlotToolbar") == []


def test_add_button_adds_selected_raster_without_dialog(env, monkeypatch):
    """+ adds the raster(s) selected in the Layers panel at once; the dialog
    only opens when no raster is selected."""
    from profile_plot.ui import raster_list
    plugin = env["plugin"]
    dock = plugin.dock
    s = dock.session
    iface = env["iface"]
    opened = []
    monkeypatch.setattr(raster_list.AddRasterDialog, "exec",
                        lambda self: opened.append(True) or 0)
    # start empty
    for e in list(s.rasters):
        s.remove_entry(e.uid)
    alive = []
    for lyr in env["layers"]:
        try:
            if QgsProject.instance().mapLayer(lyr.id()) is not None:
                alive.append(lyr)
        except RuntimeError:
            pass
    # one raster selected -> added directly, no dialog
    iface.select_layers([alive[0]])
    dock.raster_panel.add_button.click()
    assert [e.layer_id for e in s.rasters] == [alive[0].id()] and opened == []
    # several selected -> all new ones added, still no dialog
    iface.select_layers(alive)
    dock.add_rasters()
    assert [e.layer_id for e in s.rasters] == [lyr.id() for lyr in alive] and opened == []
    # selected raster already in the list -> nothing added, no dialog
    dock.add_rasters()
    assert len(s.rasters) == len(alive) and opened == []
    # nothing selected (and no raster active) -> the dialog opens
    iface.select_layers([])
    iface.setActiveLayer(None)
    dock.add_rasters()
    assert opened == [True]
    # active raster layer counts as selected when the panel has no selection
    for e in list(s.rasters):
        s.remove_entry(e.uid)
    iface.setActiveLayer(alive[0])
    dock.add_rasters()
    assert [e.layer_id for e in s.rasters] == [alive[0].id()] and opened == [True]
    iface.setActiveLayer(None)
    s.add_layers(alive)
