"""Profile Plot speed benchmark (headless QGIS).

Creates 1 cm test grids (tiled, DEFLATE-compressed GeoTIFFs with
overviews) and measures what the engineer sees:

* pointer -> station guide on a cached profile (overlay repaint + readout)
* map edit -> first preview while drawing (tail update, warm section)
* line finished -> final native values (cold and warm tile cache)

Usage:
    QT_QPA_PLATFORM=offscreen python3 scripts/benchmark.py OUT_DIR [--reps 30]

The plan asks for p95 over 30 repetitions; cold full-resolution runs
default to fewer repetitions because each one decodes hundreds of MB.
"""

import argparse
import json
import os
import platform
import resource
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tests"))

import numpy as np  # noqa: E402
import qgis_harness as H  # noqa: E402
from osgeo import gdal, osr  # noqa: E402


def make_grid(path, width, height, res, seed, wet=False):
    if os.path.exists(path):
        return path
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(path, width, height, 1, gdal.GDT_Float32,
                    ["TILED=YES", "BLOCKXSIZE=512", "BLOCKYSIZE=512", "COMPRESS=DEFLATE",
                     "PREDICTOR=3", "BIGTIFF=IF_SAFER", "NUM_THREADS=ALL_CPUS"])
    ds.SetGeoTransform((H.ORIGIN_X, res, 0.0, H.ORIGIN_Y, 0.0, -res))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(H.EPSG)
    ds.SetProjection(srs.ExportToWkt())
    b = ds.GetRasterBand(1)
    b.SetNoDataValue(-9999)
    rng = np.random.default_rng(seed)
    x = np.arange(width) * res
    for y0 in range(0, height, 512):
        h = min(512, height - y0)
        y = (np.arange(h) + y0) * res
        xx, yy = np.meshgrid(x, y)
        if wet:
            z = np.full(xx.shape, 80.8, dtype=np.float32)
            z[np.sin(xx * 0.05) < -0.3] = -9999
        else:
            z = (78.5 + 2.0 * np.sin(xx * 0.01) + 0.3 * np.sin(xx * 0.37 + yy)
                 + 0.002 * rng.standard_normal(xx.shape)).astype(np.float32)
        b.WriteArray(z, 0, y0)
    gdal.SetConfigOption("GDAL_NUM_THREADS", "ALL_CPUS")
    ds.BuildOverviews("AVERAGE", [4, 16, 64, 256])
    ds.FlushCache()
    ds = None
    return path


def p95(values):
    v = sorted(values)
    if not v:
        return float("nan")
    k = max(0, int(np.ceil(0.95 * len(v))) - 1)
    return v[k]


def summary(name, values):
    values = list(values)
    return {"name": name, "n": len(values), "p50_ms": 1000 * statistics.median(values),
            "p95_ms": 1000 * p95(values), "max_ms": 1000 * max(values)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--reps", type=int, default=30)
    ap.add_argument("--cold-reps", type=int, default=5)
    ap.add_argument("--width", type=int, default=100_000)   # 1 km at 1 cm
    ap.add_argument("--height", type=int, default=1024)     # 10.24 m at 1 cm
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    app = H.app()
    from qgis.core import Qgis, QgsPointXY, QgsRectangle
    from qgis.PyQt.QtCore import QT_VERSION_STR, QPointF

    t0 = time.perf_counter()
    res = 0.01
    p_dem = make_grid(os.path.join(args.out, "dem_1cm.tif"), args.width, args.height, res, 1)
    p_wse = make_grid(os.path.join(args.out, "wse_1cm.tif"), args.width, args.height, res, 2,
                      wet=True)
    gen_s = time.perf_counter() - t0
    layers = H.load_layers([p_dem, p_wse], ["DEM 1 cm", "WSE 1 cm"])
    iface = H.FakeIface()
    span = args.width * res
    H.setup_canvas(iface, QgsRectangle(H.ORIGIN_X - 5, H.ORIGIN_Y - args.height * res - 5,
                                       H.ORIGIN_X + span + 5, H.ORIGIN_Y + 5))
    from profile_plot import classFactory
    plugin = classFactory(iface)
    plugin.initGui()
    plugin.run()
    dock = plugin.dock
    s = dock.session
    crs = iface.canvas.mapSettings().destinationCrs()
    y_top = H.ORIGIN_Y - 0.5
    y_bot = H.ORIGIN_Y - args.height * res + 0.5
    results = []

    def line(length):
        # diagonal across the strip so rows and columns are both crossed
        return [QgsPointXY(H.ORIGIN_X + 0.3, y_top),
                QgsPointXY(H.ORIGIN_X + 0.3 + length * 0.99995, y_bot)]

    def final_time(pts):
        t = time.perf_counter()
        s.set_line(pts, crs)
        first = None
        while True:
            app.processEvents()
            if first is None and s.profiles:
                first = time.perf_counter() - t
            if not s.busy() and not s._refresh.isActive():
                app.processEvents()
                if s.is_final():
                    return first if first is not None else time.perf_counter() - t, \
                        time.perf_counter() - t
            time.sleep(0.0002)

    def cold_reset():
        s.engine.cache.clear()
        s.engine.pool.close_all()

    length_full = min(1000.0, span - 1)
    for n_rasters in (1, 2):
        for e in list(s.rasters):
            s.remove_entry(e.uid)
        s.add_layers(layers[:n_rasters])
        for length in (100.0, length_full):
            pts = line(length)
            cold_first, cold_final = [], []
            for _ in range(args.cold_reps):
                cold_reset()
                f, t = final_time(pts)
                cold_first.append(f)
                cold_final.append(t)
            warm_final = []
            for _ in range(args.reps if length <= 100 else max(5, args.reps // 3)):
                _, t = final_time(pts)
                warm_final.append(t)
            tag = "{:.0f} m, {} x 1 cm grid".format(length, n_rasters)
            results.append(summary("cold first preview, " + tag, cold_first))
            results.append(summary("cold final native, " + tag, cold_final))
            results.append(summary("warm final native, " + tag, warm_final))
            print(json.dumps(results[-3:], indent=1))

    # Pointer -> station guide on a cached profile
    dock.resize(1100, 360)
    dock.show()
    app.processEvents()
    plot = dock.plot
    plot.grab()
    times = []
    r = plot._plot_rect
    for i in range(args.reps):
        plot._cursor = QPointF(r.left() + 5 + i * (r.width() - 10) / args.reps, r.center().y())
        t = time.perf_counter()
        plot.grab()
        times.append(time.perf_counter() - t)
    results.append(summary("pointer -> station guide (overlay repaint)", times))

    # Live drawing: tail update latency (warm section)
    times = []
    tool = dock.map_tool
    H.mouse(tool, iface.canvas, "press", H.ORIGIN_X + 0.3, y_top)
    for i in range(args.reps):
        x = H.ORIGIN_X + 1 + (i + 1) * (length_full - 2) / args.reps
        t = time.perf_counter()
        H.mouse(tool, iface.canvas, "move", x, y_bot)
        target = s.tail_ch
        while True:
            app.processEvents()
            ps = [s.profiles.get(e.uid) for e in s.rasters]
            if all(p is not None and abs(p.length - target) < 1e-6 for p in ps) and \
                    not any(p.pending for p in ps):
                break
            time.sleep(0.0002)
        times.append(time.perf_counter() - t)
    results.append(summary("map edit -> first preview (tail up to 1 km, 2 rasters)", times))
    # Click: the fixed segment keeps showing values while it is refined
    H.mouse(tool, iface.canvas, "press", x, y_bot)
    app.processEvents()
    gaps = [s.profiles[e.uid].pending for e in s.rasters]
    results.append({"name": "gap shown right after click", "value": any(gaps)})

    info = {
        "machine": platform.machine(),
        "processor": platform.processor() or "",
        "cpus": os.cpu_count(),
        "python": platform.python_version(),
        "qgis": Qgis.QGIS_VERSION,
        "qt": QT_VERSION_STR,
        "gdal": gdal.__version__,
        "grid": "{} x {} px at 1 cm, tiled 512, DEFLATE".format(args.width, args.height),
        "data_generation_s": gen_s,
        "peak_rss_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0,
        "viewport": "{}x{}".format(plot.width(), plot.height()),
        "note": "cold = empty Profile Plot tile cache; the OS file cache may be warm",
    }
    try:
        with open("/proc/cpuinfo") as fh:
            for ln in fh:
                if ln.startswith("model name"):
                    info["processor"] = ln.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    report = {"environment": info, "results": results}
    with open(os.path.join(args.out, "benchmark.json"), "w") as fh:
        json.dump(report, fh, indent=2)
    print(json.dumps(report, indent=2))
    plugin.unload()


if __name__ == "__main__":
    main()
