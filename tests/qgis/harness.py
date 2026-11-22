"""Minimal headless QGIS environment for integration tests and screenshots.

Creates a QgsApplication (offscreen), a main window with a real map canvas
and a fake ``iface`` with the methods Profile Plot uses.
"""

import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))


def plugin_package():
    """Import the repository as the 'profile_plot' package, the way QGIS
    loads it from the plugins folder."""
    if "profile_plot" in sys.modules:
        return sys.modules["profile_plot"]
    parent = tempfile.mkdtemp(prefix="pp_plugins_")
    os.symlink(ROOT, os.path.join(parent, "profile_plot"))
    sys.path.insert(0, parent)
    import profile_plot  # noqa: F401
    return profile_plot


plugin_package()

import numpy as np  # noqa: E402
from osgeo import gdal, osr  # noqa: E402
from qgis.core import (  # noqa: E402
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
)
from qgis.gui import QgsMapCanvas, QgsMessageBar  # noqa: E402
from qgis.PyQt.QtCore import QEvent, QObject, QPoint, Qt  # noqa: E402
from qgis.PyQt.QtWidgets import QAction, QMainWindow  # noqa: E402

_app = None

ORIGIN_X = 467300.0
ORIGIN_Y = 6946240.0
EPSG = 7856  # GDA2020 / MGA zone 56


def app():
    global _app
    if _app is None:
        _app = QgsApplication([], True)
        QgsApplication.setPrefixPath("/usr", True)
        _app.initQgis()
    return _app


class FakeIface(QObject):
    def __init__(self):
        super().__init__()
        self.mw = QMainWindow()
        self.mw.resize(1100, 820)
        self.canvas = QgsMapCanvas(self.mw)
        self.canvas.setObjectName("theMapCanvas")
        self.mw.setCentralWidget(self.canvas)
        self.bar = QgsMessageBar()
        self.messages = []
        self.bar.widgetAdded.connect(self._on_message)
        self._active = None
        self._pan = QAction("Pan", self.mw)
        self.registered = []

    def _on_message(self, w):
        item = self.bar.currentItem()
        self.messages.append(item.text() if item is not None else str(w))

    def mainWindow(self):
        return self.mw

    def mapCanvas(self):
        return self.canvas

    def messageBar(self):
        return self.bar

    def registerMainWindowAction(self, action, shortcut):
        self.registered.append((action, shortcut))
        self.mw.addAction(action)
        return True

    def unregisterMainWindowAction(self, action):
        return True

    def addToolBarIcon(self, action):
        return 0

    def addToolBar(self, name):
        return self.mw.addToolBar(name)

    def removeToolBarIcon(self, action):
        pass

    def addPluginToRasterMenu(self, menu, action):
        pass

    def removePluginRasterMenu(self, menu, action):
        pass

    def addDockWidget(self, area, dock):
        self.mw.addDockWidget(area, dock)

    def removeDockWidget(self, dock):
        self.mw.removeDockWidget(dock)

    def layerTreeView(self):
        return None

    def activeLayer(self):
        return self._active

    def setActiveLayer(self, layer):
        self._active = layer

    def actionPan(self):
        return self._pan


def write_tif(path, arr, gt, nodata=None, epsg=EPSG, block=256, overviews=False):
    drv = gdal.GetDriverByName("GTiff")
    h, w = arr.shape
    ds = drv.Create(path, w, h, 1, gdal.GDT_Float32,
                    ["TILED=YES", "BLOCKXSIZE=%d" % block, "BLOCKYSIZE=%d" % block,
                     "COMPRESS=DEFLATE", "PREDICTOR=3"])
    ds.SetGeoTransform(gt)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    ds.SetProjection(srs.ExportToWkt())
    b = ds.GetRasterBand(1)
    if nodata is not None:
        b.SetNoDataValue(nodata)
    b.WriteArray(arr)
    if overviews:
        ds.BuildOverviews("AVERAGE", [2, 4, 8, 16])
    ds.FlushCache()
    ds = None
    return path


def make_creek_rasters(folder):
    """A creek cross-section like the design mockup: 0.1 m LiDAR DEM and two
    2 m WSE grids that are NoData outside the wet area."""
    os.makedirs(folder, exist_ok=True)
    res = 0.1
    w, h = 1000, 400                       # 100 m x 40 m
    x = (np.arange(w) + 0.5) * res
    y = (np.arange(h) + 0.5) * res
    xx, yy = np.meshgrid(x, y)
    # valley: 82 at the left bank, 78.5 at the bed (x = 50), 81 at the right
    z = np.where(xx < 50, 78.5 + 3.5 * (np.abs(50 - xx) / 50) ** 1.3,
                 78.5 + 2.5 * (np.abs(xx - 50) / 50) ** 1.1)
    z += 0.08 * np.sin(xx * 1.7) + 0.05 * np.sin(yy * 2.3 + xx * 0.7)
    z[:, 300:303] += 0.6                   # a narrow kerb / embankment
    dem = z.astype(np.float32)
    gt_dem = (ORIGIN_X, res, 0.0, ORIGIN_Y, 0.0, -res)
    p_dem = write_tif(os.path.join(folder, "lidar_dem.tif"), dem, gt_dem, -9999,
                      overviews=True)

    r2 = 2.0
    w2, h2 = 50, 20
    x2 = (np.arange(w2) + 0.5) * r2
    wse1 = np.full((h2, w2), -9999.0, dtype=np.float32)
    wse1[:, np.abs(x2 - 50) < 12.5] = 80.8
    wse2 = np.full((h2, w2), -9999.0, dtype=np.float32)
    wse2[:, np.abs(x2 - 50) < 12.5] = 80.3
    gt2 = (ORIGIN_X, r2, 0.0, ORIGIN_Y, 0.0, -r2)
    p1 = write_tif(os.path.join(folder, "wse_1pct.tif"), wse1, gt2, -9999, block=16)
    p2 = write_tif(os.path.join(folder, "wse_5pct.tif"), wse2, gt2, -9999, block=16)
    return p_dem, p1, p2


def load_layers(paths, names):
    layers = []
    for p, n in zip(paths, names):
        lyr = QgsRasterLayer(p, n, "gdal")
        assert lyr.isValid(), p
        QgsProject.instance().addMapLayer(lyr)
        layers.append(lyr)
    return layers


def setup_canvas(iface, extent=None):
    c = iface.canvas
    c.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:%d" % EPSG))
    c.setLayers(list(QgsProject.instance().mapLayers().values()))
    iface.mw.show()
    app().processEvents()
    if extent is None:
        extent = QgsRectangle(ORIGIN_X - 5, ORIGIN_Y - 45, ORIGIN_X + 105, ORIGIN_Y + 5)
    c.setExtent(extent)
    c.refresh()
    app().processEvents()


def map_to_pixel(canvas, x, y):
    from qgis.core import QgsPointXY
    p = canvas.getCoordinateTransform().transform(QgsPointXY(x, y))
    return QPoint(int(round(p.x())), int(round(p.y())))


def mouse(tool, canvas, kind, x, y, button=Qt.MouseButton.LeftButton):
    from qgis.gui import QgsMapMouseEvent
    pos = map_to_pixel(canvas, x, y)
    etype = {"press": QEvent.Type.MouseButtonPress, "move": QEvent.Type.MouseMove,
             "release": QEvent.Type.MouseButtonRelease}[kind]
    btns = button if kind != "move" else Qt.MouseButton.NoButton
    ev = QgsMapMouseEvent(canvas, etype, pos, button if kind != "move" else Qt.MouseButton.NoButton,
                          btns, Qt.KeyboardModifier.NoModifier)
    if kind == "press":
        tool.canvasPressEvent(ev)
    elif kind == "move":
        tool.canvasMoveEvent(ev)
    else:
        tool.canvasReleaseEvent(ev)


def wait_idle(session, timeout=20.0):
    a = app()
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        a.processEvents()
        if not session.busy() and not session._refresh.isActive():
            a.processEvents()
            return time.perf_counter() - t0
        time.sleep(0.0005)
    raise TimeoutError("session still busy")
