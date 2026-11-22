"""Render screenshots of Profile Plot in a headless QGIS window.

Usage:  QT_QPA_PLATFORM=offscreen python3 scripts/screenshot.py OUT_DIR
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tests"))

import qgis_harness as H  # noqa: E402


def style(layer, colors, opacity=1.0):
    from qgis.core import (QgsColorRampShader, QgsRasterShader,
                           QgsSingleBandPseudoColorRenderer)
    from qgis.PyQt.QtGui import QColor
    shader = QgsRasterShader()
    ramp = QgsColorRampShader()
    ramp.setColorRampType(QgsColorRampShader.Type.Interpolated)
    ramp.setColorRampItemList([QgsColorRampShader.ColorRampItem(v, QColor(c))
                               for v, c in colors])
    shader.setRasterShaderFunction(ramp)
    r = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
    r.setOpacity(opacity)
    layer.setRenderer(r)


def main():
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    app = H.app()
    from qgis.PyQt.QtCore import QPointF, Qt
    paths = H.make_creek_rasters(os.path.join(out, "data"))
    layers = H.load_layers(paths, ["LiDAR DEM", "1% AEP WSE", "5% AEP WSE"])
    style(layers[0], [(78.4, "#2f6b2f"), (79.5, "#7fae5a"), (81, "#d8c98f"), (82.3, "#f3efe0")])
    style(layers[1], [(80.0, "#2a6fdb"), (81.0, "#2a6fdb")], 0.55)
    style(layers[2], [(80.0, "#35c9e8"), (81.0, "#35c9e8")], 0.55)
    iface = H.FakeIface()
    iface.mw.resize(1100, 860)
    H.setup_canvas(iface)
    from profile_plot import classFactory
    plugin = classFactory(iface)
    plugin.initGui()
    plugin.run()
    dock = plugin.dock
    s = dock.session
    s.add_layers(layers)
    plugin.run()
    s.set_slope(s.rasters[0].uid)
    tool = dock.map_tool
    y = H.ORIGIN_Y - 20
    H.mouse(tool, iface.canvas, "press", H.ORIGIN_X, y)
    H.mouse(tool, iface.canvas, "move", H.ORIGIN_X + 100, y)
    H.mouse(tool, iface.canvas, "press", H.ORIGIN_X + 100, y)
    H.mouse(tool, iface.canvas, "press", H.ORIGIN_X + 100, y, Qt.MouseButton.RightButton)
    H.wait_idle(s)
    iface.canvas.refresh()
    iface.canvas.waitWhileRendering()
    app.processEvents()

    plot = dock.plot
    dem = s.profiles[s.rasters[0].uid]

    def at(ch, value):
        px, py = plot._to_px(ch, value)
        plot._cursor = QPointF(px, py)
        dock._on_plot_cursor(ch)
        plot.update()
        app.processEvents()

    dock.act_cross.setChecked(True)
    plot.grab()
    at(50.0, dem.value_at(50.0))
    iface.mw.grab().save(os.path.join(out, "window_crosshair.png"))
    dock.grab().save(os.path.join(out, "dock_crosshair.png"))

    dock.act_hover.setChecked(True)
    wse = s.profiles[s.rasters[1].uid]
    at(45.0, wse.value_at(45.0))
    dock.grab().save(os.path.join(out, "dock_hover.png"))

    # Collapsed raster list (compact layout)
    dock.act_rasters.setChecked(False)
    app.processEvents()
    at(30.2, dem.value_at(30.2))
    dock.grab().save(os.path.join(out, "dock_compact.png"))
    dock.act_rasters.setChecked(True)

    plot._cursor = None
    dock.tabs.setCurrentIndex(1)
    app.processEvents()
    dock.grab().save(os.path.join(out, "dock_table.png"))
    dock.tabs.setCurrentIndex(0)
    plugin.unload()
    print("saved to", out)


if __name__ == "__main__":
    main()
