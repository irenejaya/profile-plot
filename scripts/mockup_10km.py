"""Render a mock-up of a 10 km DEM profile (headless QGIS).

Builds a synthetic 1 m LiDAR-style DEM (10.4 km x 2.6 km, tiled GeoTIFF with
overviews) with rolling hills, a meandering river valley, a ridge and a road
embankment, plus a 1% AEP flood surface along the river.  Draws a 10 km
alignment with three vertices, times the profile and saves screenshots.

Usage:  QT_QPA_PLATFORM=offscreen python3 scripts/mockup_10km.py OUT_DIR
"""

import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tests"))
sys.path.insert(0, HERE)

import numpy as np  # noqa: E402
import qgis_harness as H  # noqa: E402
from osgeo import gdal, osr  # noqa: E402

W, HGT = 10400, 2600           # 1 m cells
OX, OY = 455000.0, 6952000.0   # MGA zone 56 origin (top-left)


def river_y(x):
    return 1300 + 330 * np.sin(x / 950.0) + 110 * np.sin(x / 310.0 + 1.0)


def floor_z(x):
    return 52.0 - 0.0018 * x    # river falls ~19 m over 10 km


def terrain(xx, yy, rng=None):
    hills = (78 + 22 * np.sin(xx / 1700.0) * np.cos(yy / 900.0)
             + 13 * np.sin(xx / 650.0 + yy / 1100.0)
             + 6 * np.cos(xx / 290.0 - yy / 470.0))
    ridge = 34 * np.exp(-((xx - 7600) / 520.0) ** 2 - ((yy - 450) / 800.0) ** 2)
    hills = hills + ridge
    d = np.abs(yy - river_y(xx))
    valley = np.exp(-(d / 380.0) ** 2)
    z = hills - (hills - floor_z(xx)) * valley
    z -= 3.2 * np.exp(-(d / 11.0) ** 2)                      # low-flow channel
    dx = np.abs((xx - 3050) - 0.22 * yy)                     # road embankment
    z += 3.0 * np.clip((17 - dx) / 6.0, 0, 1)
    z += (0.9 * np.sin(xx / 61.0 + yy / 83.0)                # gentle texture
          + 0.6 * np.sin(xx / 137.0 - yy / 97.0 + 0.7))
    if rng is not None:
        z += 0.03 * rng.standard_normal(z.shape)
    return z


def write(path, arr_fn, width, height, res, nodata=-9999.0, overviews=True, block=256):
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(path, width, height, 1, gdal.GDT_Float32,
                    ["TILED=YES", "BLOCKXSIZE=%d" % block, "BLOCKYSIZE=%d" % block,
                     "COMPRESS=DEFLATE", "PREDICTOR=3", "NUM_THREADS=ALL_CPUS"])
    ds.SetGeoTransform((OX, res, 0.0, OY, 0.0, -res))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(H.EPSG)
    ds.SetProjection(srs.ExportToWkt())
    b = ds.GetRasterBand(1)
    b.SetNoDataValue(nodata)
    for y0 in range(0, height, 512):
        h = min(512, height - y0)
        b.WriteArray(arr_fn(y0, h).astype(np.float32), 0, y0)
    if overviews:
        gdal.SetConfigOption("GDAL_NUM_THREADS", "ALL_CPUS")
        ds.BuildOverviews("AVERAGE", [2, 4, 8, 16, 32])
    ds.FlushCache()
    ds = None
    return path


def make_data(folder):
    os.makedirs(folder, exist_ok=True)
    dem = os.path.join(folder, "lidar_dem_1m.tif")
    wse = os.path.join(folder, "wse_1pct_5m.tif")
    rng = np.random.default_rng(7)
    if not os.path.exists(dem):
        x = np.arange(W) + 0.5

        def dem_rows(y0, h):
            y = np.arange(y0, y0 + h) + 0.5
            xx, yy = np.meshgrid(x, y)
            return terrain(xx, yy, rng)
        write(dem, dem_rows, W, HGT, 1.0)
    if not os.path.exists(wse):
        r = 5.0
        w5, h5 = W // 5, HGT // 5
        x = (np.arange(w5) + 0.5) * r

        def wse_rows(y0, h):
            y = (np.arange(y0, y0 + h) + 0.5) * r
            xx, yy = np.meshgrid(x, y)
            level = floor_z(xx) + 5.2
            ground = terrain(xx, yy)
            return np.where(ground < level, level, -9999.0)
        write(wse, wse_rows, w5, h5, r, overviews=False, block=128)
    return dem, wse


def main():
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    app = H.app()
    from qgis.core import (QgsHillshadeRenderer, QgsPointXY, QgsProject, QgsRasterLayer,
                           QgsRectangle)
    from qgis.PyQt.QtCore import QEvent, QPointF, Qt
    from qgis.PyQt.QtGui import QMouseEvent
    from qgis.PyQt.QtWidgets import QApplication
    from screenshot import style

    t0 = time.perf_counter()
    p_dem, p_wse = make_data(os.path.join(out, "data"))
    print("data ready in %.1f s" % (time.perf_counter() - t0))

    shade = QgsRasterLayer(p_dem, "Hillshade", "gdal")
    hs = QgsHillshadeRenderer(shade.dataProvider(), 1, 315, 40)
    hs.setZFactor(2.5)
    hs.setMultiDirectional(True)
    shade.setRenderer(hs)
    dem = QgsRasterLayer(p_dem, "LiDAR DEM 1 m", "gdal")
    style(dem, [(28, "#2f6b2f"), (55, "#7fae5a"), (80, "#d8c98f"), (105, "#b08a5a"),
                (130, "#f3efe0")], 1.0)
    from qgis.PyQt.QtGui import QPainter
    dem.setBlendMode(QPainter.CompositionMode.CompositionMode_Multiply)
    wse = QgsRasterLayer(p_wse, "1% AEP WSE", "gdal")
    style(wse, [(20, "#2a6fdb"), (60, "#2a6fdb")], 0.6)
    for lyr in (shade, dem, wse):
        QgsProject.instance().addMapLayer(lyr)

    iface = H.FakeIface()
    iface.mw.resize(1200, 900)
    H.setup_canvas(iface, QgsRectangle(OX - 100, OY - HGT - 100, OX + W + 100, OY + 100))
    iface.canvas.setLayers([wse, dem, shade])

    from profile_plot import classFactory, settings
    settings.set("showLegend", False)
    settings.set("slopeUnit", "%")
    plugin = classFactory(iface)
    plugin.initGui()
    plugin.run()
    dock = plugin.dock
    s = dock.session
    s.add_layers([dem, wse])
    s.set_slope(s.rasters[0].uid)

    # 10 km alignment with three vertices (A -> bend -> B)
    a = (OX + 200.0, OY - 650.0)
    v = (OX + 5000.0, OY - 1850.0)
    seg1 = math.hypot(v[0] - a[0], v[1] - a[1])
    dirx, diry = 0.9925, 0.1222
    n = math.hypot(dirx, diry)
    rest = 10000.0 - seg1
    b = (v[0] + rest * dirx / n, v[1] + rest * diry / n)
    pts = [QgsPointXY(*a), QgsPointXY(*v), QgsPointXY(*b)]
    crs = iface.canvas.mapSettings().destinationCrs()
    t = time.perf_counter()
    s.set_line(pts, crs)
    first = None
    while True:
        app.processEvents()
        if first is None and s.profiles:
            first = time.perf_counter() - t
        if s.is_final() and not s._refresh.isActive():
            break
        time.sleep(0.0005)
    final = time.perf_counter() - t
    prof = s.profiles[s.rasters[0].uid]
    print("10 km profile: first plot %.0f ms, final native %.0f ms, %d DEM cells"
          % (1000 * first, 1000 * final, prof.x0.size))

    iface.canvas.refresh()
    iface.canvas.waitWhileRendering()
    dock.resize(1200, 360)
    dock.act_cross.setChecked(True)
    plot = dock.plot
    plot.grab()
    # crosshair at the river crossing (lowest DEM point)
    i = int(np.nanargmin(prof.v))
    ch = 0.5 * (prof.x0[i] + prof.x1[i])
    px, py = plot._to_px(ch, prof.v[i] + 2.0)
    local = QPointF(px, py)
    ev = QMouseEvent(QEvent.Type.MouseMove, local, QPointF(plot.mapToGlobal(local.toPoint())),
                     Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                     Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(plot, ev)
    app.processEvents()
    iface.canvas.refresh()
    iface.canvas.waitWhileRendering()
    app.processEvents()
    iface.mw.grab().save(os.path.join(out, "mockup_10km.png"))
    dock.grab().save(os.path.join(out, "mockup_10km_dock.png"))
    plot.render_image().save(os.path.join(out, "mockup_10km_plot.png"))
    # Zoomed in on the river crossing: native 1 m cells drawn as steps
    lo, hi = ch - 60.0, ch + 60.0
    sel = (prof.x1 > lo) & (prof.x0 < hi)
    vmin = float(np.nanmin(prof.v[sel]))
    vmax = float(np.nanmax(prof.v[sel]))
    plot.view = (lo, hi, vmin - 0.8, vmax + 0.8)
    plot.auto_view = False
    plot._cursor = None
    plot._invalidate()
    plot.render_image().save(os.path.join(out, "mockup_10km_zoom.png"))
    plugin.unload()
    print("saved to", out)


if __name__ == "__main__":
    main()
