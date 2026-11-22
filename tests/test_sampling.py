"""Unit tests for the pure-numpy kernels and the GDAL tile readers.

Run with:  python3 -m pytest tests
(needs numpy and GDAL's Python bindings; QGIS is not required)
"""

import os
import sys
import threading

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from profile_plot import sampling  # noqa: E402
from profile_plot.sampling import OUTSIDE  # noqa: E402

gdal = pytest.importorskip("osgeo.gdal")
from profile_plot import readers  # noqa: E402


# ---------------------------------------------------------------------------
# traversal
# ---------------------------------------------------------------------------

def cells_of(x0, y0, x1, y1, w=10, h=10):
    ta, tb, rows, cols = sampling.traverse_segment(x0, y0, x1, y1, w, h)
    return ta, tb, list(zip(rows.tolist(), cols.tolist()))


def test_horizontal_line_cells_and_params():
    ta, tb, cells = cells_of(0.5, 2.5, 4.5, 2.5)
    assert cells == [(2, 0), (2, 1), (2, 2), (2, 3), (2, 4)]
    np.testing.assert_allclose(ta, [0, 0.125, 0.375, 0.625, 0.875])
    np.testing.assert_allclose(tb, [0.125, 0.375, 0.625, 0.875, 1.0])


def test_corner_only_touch_is_ignored():
    # Exact diagonal through grid corners: only the diagonal cells.
    _, _, cells = cells_of(0, 0, 3, 3)
    assert cells == [(0, 0), (1, 1), (2, 2)]


def test_short_diagonal_clip_is_kept():
    # A line that clips the corner of cell (row 1, col 1) by a tiny amount.
    _, tb, cells = cells_of(0.2, 1.95, 1.9, 0.2)
    assert (1, 1) in cells or (0, 1) in cells
    # Every crossed cell has positive length.
    ta, tb, _, _ = sampling.traverse_segment(0.2, 1.95, 1.9, 0.2, 10, 10)
    assert np.all(tb - ta > 0)


def test_half_open_boundaries():
    # Along the line y = 2 the cells below (row 2) are used.
    _, _, cells = cells_of(0.5, 2.0, 2.5, 2.0)
    assert cells == [(2, 0), (2, 1), (2, 2)]
    # Along the bottom edge of the raster nothing is inside.
    _, _, cells = cells_of(0.5, 10.0, 2.5, 10.0)
    assert all(rc == (OUTSIDE, OUTSIDE) for rc in cells)


def test_outside_parts_are_single_intervals():
    ta, tb, cells = cells_of(-5, 0.5, 2.5, 0.5, w=2, h=2)
    assert cells[0] == (OUTSIDE, OUTSIDE)
    assert cells[1:3] == [(0, 0), (0, 1)]
    assert cells[3] == (OUTSIDE, OUTSIDE)
    np.testing.assert_allclose(tb[0], 5 / 7.5)
    # Whole segment outside
    _, _, cells = cells_of(-5, -5, -1, -1)
    assert cells == [(OUTSIDE, OUTSIDE)]


def test_native_cells_merges_across_vertices():
    px = np.array([0.5, 1.5, 1.6, 3.5])
    py = np.array([0.5, 0.5, 0.6, 0.5])
    ch = np.array([0.0, 1.0, 1.14, 3.05])
    ch0, ch1, rows, cols = sampling.native_cells(px, py, ch, 10, 10)
    assert list(cols) == [0, 1, 2, 3]
    assert list(rows) == [0, 0, 0, 0]
    assert ch0[0] == 0 and ch1[-1] == pytest.approx(3.05)
    # contiguous intervals
    np.testing.assert_allclose(ch0[1:], ch1[:-1])


def test_diagonal_crest_native_vs_fixed_step():
    """A one-cell embankment clipped diagonally is kept by native mode but
    can be missed by a fixed step equal to the pixel width."""
    grid = np.zeros((4, 4))
    grid[1, 2] = 5.0  # narrow crest cell
    # Line y = x - 0.1 clips only the corner of cell (1, 2) for x in [2, 2.1)
    x0, y0, x1, y1 = 0.1, 0.0, 3.9, 3.8
    ch_len = np.hypot(x1 - x0, y1 - y0)
    px = np.array([x0, x1])
    py = np.array([y0, y1])
    ch = np.array([0.0, ch_len])
    ch0, ch1, rows, cols = sampling.native_cells(px, py, ch, 4, 4)
    vals = grid[rows, cols]
    assert vals.max() == 5.0
    k = list(zip(rows.tolist(), cols.tolist())).index((1, 2))
    assert (ch1 - ch0)[k] == pytest.approx(0.1 * np.sqrt(2))
    # Fixed 1-pixel step sampling along the same line
    s = np.arange(0, ch_len, 1.0)
    sx = x0 + (x1 - x0) * s / ch_len
    sy = y0 + (y1 - y0) * s / ch_len
    fixed = grid[np.floor(sy).astype(int), np.floor(sx).astype(int)]
    assert fixed.max() == 0.0


def test_station_positions_count_and_end():
    px = np.array([0.0, 100.0])
    py = np.array([0.5, 0.5])
    ch = np.array([0.0, 100.0])
    s, sx, sy, ex, ey = sampling.stations_positions(px, py, ch)
    # [0, 100) on the 1 cm grid plus the end station -> 10,001 stations
    assert s.size + 1 == 10001
    assert s[1] == pytest.approx(0.01)
    assert ex == 100.0


def test_station_indices_partition_without_duplicates():
    a = sampling.station_indices(0.0, 0.37)
    b = sampling.station_indices(0.37, 1.0)
    allidx = np.concatenate((a, b))
    assert np.all(np.diff(allidx) == 1)
    assert allidx[0] == 0 and allidx[-1] == 99


def test_bilinear():
    grid = np.array([[0.0, 10.0], [20.0, 30.0]])
    rows, cols, w, inside = sampling.bilinear_setup(np.array([1.0]),
                                                     np.array([1.0]), 2, 2)
    v = sampling.bilinear_combine(grid[rows, cols], w, inside)
    assert v[0] == pytest.approx(15.0)
    grid2 = grid.copy()
    grid2[0, 0] = np.nan
    v2 = sampling.bilinear_combine(grid2[rows, cols], w, inside)
    assert np.isnan(v2[0])


# ---------------------------------------------------------------------------
# grade
# ---------------------------------------------------------------------------

def test_grade_example_from_plan():
    g = sampling.endpoint_grade(82.00, 80.55, 100.0)
    assert round(g, 2) == -1.45
    assert sampling.grade_ratio_text(g) == "1 in 69 falling"
    # Reverse the line: sign flips
    g2 = sampling.endpoint_grade(80.55, 82.00, 100.0)
    assert round(g2, 2) == 1.45
    assert sampling.grade_ratio_text(g2) == "1 in 69 rising"
    assert sampling.endpoint_grade(80.0, 80.0, 50.0) == 0.0
    assert sampling.endpoint_grade(80.0, 81.0, 0.0) is None
    assert sampling.endpoint_grade(float("nan"), 81.0, 10.0) is None


def test_union_boundaries_and_values():
    a0 = np.array([0.0, 1.0, 2.0])
    a1 = np.array([1.0, 2.0, 3.0])
    b0 = np.array([0.0, 1.5])
    b1 = np.array([1.5, 3.0])
    bounds = sampling.union_boundaries([a0, a1, b0, b1 + 1e-9])
    np.testing.assert_allclose(bounds, [0, 1, 1.5, 2, 3])
    mids = 0.5 * (bounds[:-1] + bounds[1:])
    va = sampling.step_values_at(a0, a1, np.array([10.0, 11, 12]), mids)
    vb = sampling.step_values_at(b0, b1, np.array([20.0, np.nan]), mids)
    np.testing.assert_allclose(va, [10, 11, 11, 12])
    assert vb[0] == 20 and np.isnan(vb[2])


# ---------------------------------------------------------------------------
# readers
# ---------------------------------------------------------------------------

def make_tif(path, arr, gt=(1000.0, 1.0, 0.0, 2000.0, 0.0, -1.0), nodata=None,
             tiled=True, block=16, scale=None, offset=None, dtype=None):
    drv = gdal.GetDriverByName("GTiff")
    h, w = arr.shape
    gdt = dtype or gdal.GDT_Float32
    opts = ["TILED=YES", "BLOCKXSIZE=%d" % block, "BLOCKYSIZE=%d" % block] if tiled else []
    ds = drv.Create(str(path), w, h, 1, gdt, opts)
    ds.SetGeoTransform(gt)
    b = ds.GetRasterBand(1)
    if nodata is not None:
        b.SetNoDataValue(nodata)
    if scale is not None:
        b.SetScale(scale)
    if offset is not None:
        b.SetOffset(offset)
    b.WriteArray(arr)
    ds.FlushCache()
    ds = None
    return str(path)


@pytest.fixture
def engine_parts():
    return readers.TileCache(64 * 1024 * 1024), readers.DatasetPool()


def test_lookup_matches_array(tmp_path, engine_parts):
    cache, pool = engine_parts
    rng = np.random.default_rng(1)
    arr = rng.normal(80, 2, (70, 90)).astype(np.float32)
    path = make_tif(tmp_path / "a.tif", arr, block=32)
    spec = readers.build_gdal_spec("a", path)
    assert spec.levels[0].tile_w == 32
    ctx = readers.ReadContext(spec, cache, pool)
    rows = rng.integers(0, 70, 500)
    cols = rng.integers(0, 90, 500)
    rows[:5] = OUTSIDE
    cols[:5] = OUTSIDE
    vals = ctx.lookup(spec.full, rows, cols)
    ctx.close()
    assert np.all(np.isnan(vals[:5]))
    np.testing.assert_allclose(vals[5:], arr[rows[5:], cols[5:]])


def test_strip_tiff_and_nodata_hole(tmp_path, engine_parts):
    cache, pool = engine_parts
    arr = np.full((20, 20), 50.0, dtype=np.float32)
    arr[5:8, 5:8] = -9999
    path = make_tif(tmp_path / "s.tif", arr, nodata=-9999, tiled=False)
    spec = readers.build_gdal_spec("s", path)
    ctx = readers.ReadContext(spec, cache, pool)
    piece = (np.array([0.5, 19.5]), np.array([6.5, 6.5]), np.array([0.0, 19.0]))
    res = readers.sample_piece(ctx, piece, "native")
    ctx.close()
    assert list(res.col) == list(range(20))
    assert np.isnan(res.value[5:8]).all()
    assert np.all(res.value[:5] == 50.0)


def test_scale_offset_and_int_nodata(tmp_path, engine_parts):
    cache, pool = engine_parts
    arr = np.array([[100, 200], [0, 300]], dtype=np.int16)
    path = make_tif(tmp_path / "i.tif", arr, nodata=0, tiled=False,
                    scale=0.01, offset=50.0, dtype=gdal.GDT_Int16)
    spec = readers.build_gdal_spec("i", path)
    assert spec.store_dtype == np.float64
    ctx = readers.ReadContext(spec, cache, pool)
    vals = ctx.lookup(spec.full, np.array([0, 0, 1, 1]), np.array([0, 1, 0, 1]))
    ctx.close()
    np.testing.assert_allclose(vals[[0, 1, 3]], [51.0, 52.0, 53.0])
    assert np.isnan(vals[2])


def test_rotated_grid_world_to_pixel(tmp_path, engine_parts):
    cache, pool = engine_parts
    ang = np.radians(30)
    res = 2.0
    gt = (500.0, res * np.cos(ang), -res * np.sin(ang),
          800.0, -res * np.sin(ang), -res * np.cos(ang))
    arr = np.arange(100, dtype=np.float32).reshape(10, 10)
    path = make_tif(tmp_path / "r.tif", arr, gt=gt, tiled=False)
    spec = readers.build_gdal_spec("r", path)
    # Centre of cell (row 3, col 4) in world coordinates
    wx, wy = spec.pixel_to_world(4.5, 3.5)
    px, py = spec.world_to_pixel(wx, wy)
    assert px == pytest.approx(4.5) and py == pytest.approx(3.5)
    # A world-space segment between two cell centres crosses expected cells
    ax, ay = spec.pixel_to_world(0.5, 3.5)
    bx, by = spec.pixel_to_world(9.5, 3.5)
    apx, apy = spec.world_to_pixel(np.array([ax, bx]), np.array([ay, by]))
    length = np.hypot(bx - ax, by - ay)
    assert length == pytest.approx(9 * res)
    ctx = readers.ReadContext(spec, cache, pool)
    out = readers.sample_piece(ctx, (apx, apy, np.array([0.0, length])), "native")
    ctx.close()
    np.testing.assert_allclose(out.value, arr[3, :])
    # Each full cell spans one pixel width = 2 m of chainage
    np.testing.assert_allclose((out.ch1 - out.ch0)[1:-1], res)


def test_offset_wse_cells_union(tmp_path, engine_parts):
    cache, pool = engine_parts
    dem = np.tile(np.arange(10, dtype=np.float32), (4, 1))
    wse = np.full((4, 10), 20.0, dtype=np.float32)
    p1 = make_tif(tmp_path / "dem.tif", dem, gt=(0.0, 1.0, 0, 4.0, 0, -1.0))
    p2 = make_tif(tmp_path / "wse.tif", wse, gt=(-0.5, 1.0, 0, 4.0, 0, -1.0))
    results = []
    for key, path in (("dem", p1), ("wse", p2)):
        spec = readers.build_gdal_spec(key, path)
        xs, ys = spec.world_to_pixel(np.array([1.0, 6.0]), np.array([2.5, 2.5]))
        ctx = readers.ReadContext(spec, cache, pool)
        results.append(readers.sample_piece(ctx, (xs, ys, np.array([0.0, 5.0])),
                                            "native"))
        ctx.close()
    bounds = sampling.union_boundaries([r.ch0 for r in results] +
                                       [r.ch1 for r in results])
    # DEM boundaries at 0,1,2,..5 and WSE boundaries at 0.5,1.5,...
    np.testing.assert_allclose(bounds, np.arange(0, 5.01, 0.5))


def test_station_mode_through_reader(tmp_path, engine_parts):
    cache, pool = engine_parts
    arr = np.tile(np.arange(10, dtype=np.float32), (3, 1))
    path = make_tif(tmp_path / "st.tif", arr, gt=(0.0, 1.0, 0, 3.0, 0, -1.0))
    spec = readers.build_gdal_spec("st", path)
    ctx = readers.ReadContext(spec, cache, pool)
    piece = (np.array([0.0, 10.0]), np.array([1.5, 1.5]), np.array([0.0, 10.0]))
    res = readers.sample_piece(ctx, piece, "station")
    assert res.ch.size == 1000
    assert res.value[0] == 0.0 and res.value[150] == 1.0
    assert np.isnan(res.end_value)  # x = 10 is outside (half-open)
    bil = readers.sample_piece(ctx, piece, "bilinear")
    ctx.close()
    # Between centres 1.5 and 2.5 -> linear ramp
    assert bil.value[200] == pytest.approx(1.5)


def test_tile_cache_eviction_and_threads():
    cache = readers.TileCache(max_bytes=4 * 1024)
    calls = []

    def loader(i):
        calls.append(i)
        return np.zeros(128, dtype=np.float64)  # 1 KiB

    for i in range(10):
        cache.get(("k", 0, 0, i), lambda i=i: loader(i))
    assert len(cache) <= 4
    cache.drop("k")
    assert len(cache) == 0

    # Concurrent getters of the same key load once.
    cache2 = readers.TileCache()
    gate = threading.Event()
    n_loads = []

    def slow():
        n_loads.append(1)
        gate.wait(1.0)
        return np.ones(4)

    out = []
    ts = [threading.Thread(target=lambda: out.append(cache2.get(("x", 0, 0, 0), slow)))
          for _ in range(4)]
    for t in ts:
        t.start()
    gate.set()
    for t in ts:
        t.join()
    assert len(n_loads) == 1 and len(out) == 4


def test_overview_preview_level(tmp_path, engine_parts):
    cache, pool = engine_parts
    arr = np.random.default_rng(3).normal(10, 1, (512, 512)).astype(np.float32)
    path = make_tif(tmp_path / "ov.tif", arr, block=64)
    ds = gdal.Open(path, gdal.GA_Update)
    ds.BuildOverviews("AVERAGE", [2, 4, 8])
    ds = None
    spec = readers.build_gdal_spec("ov", path)
    assert len(spec.levels) == 4
    lvl = readers.choose_preview_level(spec, 512.0, target_cells=100)
    assert lvl >= 1
    ctx = readers.ReadContext(spec, cache, pool)
    piece = (np.array([0.5, 511.5]), np.array([100.5, 100.5]), np.array([0.0, 511.0]))
    full = readers.sample_piece(ctx, piece, "native", 0)
    prev = readers.sample_piece(ctx, piece, "native", lvl)
    ctx.close()
    assert prev.ch0.size < full.ch0.size
    assert prev.level == lvl
    assert prev.ch1[-1] == pytest.approx(full.ch1[-1])


# ---------------------------------------------------------------------------
# packaging
# ---------------------------------------------------------------------------

def test_package_zip_installs_as_profile_plot(tmp_path):
    """QGIS names a ZIP-installed plugin after the folder holding the first
    metadata.txt; it must be exactly 'profile_plot' (a ZIP of the whole
    repository would give 'profile-plot/profile_plot' and fail to load)."""
    import subprocess
    import zipfile
    root = os.path.join(os.path.dirname(__file__), "..")
    out = subprocess.run([sys.executable, os.path.join(root, "scripts", "package.py"),
                          str(tmp_path)], capture_output=True, text=True, check=True)
    path = out.stdout.strip().splitlines()[-1]
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
    meta = sorted(n for n in names if n.endswith("metadata.txt"))
    assert os.path.split(meta[0])[0] == "profile_plot"     # QGIS installer rule
    assert all(n.startswith("profile_plot/") for n in names)
    assert "profile_plot/__init__.py" in names
    assert not any("__pycache__" in n or n.endswith(".pyc") for n in names)
    assert not any(n.startswith(("profile_plot/tests", "profile_plot/docs")) for n in names)
