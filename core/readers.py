"""Raster tile access for Profile Plot.

Values are read in tiles aligned to the source block layout, converted to
floating point with NoData / mask / scale / offset applied (NoData -> NaN)
and kept in a shared, memory-capped LRU cache.  GDAL dataset handles are
checked out by worker threads for the duration of a job, so no handle is
ever used by two threads at once.

This module does not import QGIS.  A QGIS provider can still be used as a
fallback source through :class:`ProviderSource`, whose block reader is
supplied by the caller.
"""

import logging
import os
import threading
import warnings
from collections import OrderedDict

import numpy as np

from . import sampling

_log = logging.getLogger(__name__)

try:  # GDAL ships with every QGIS installation.
    from osgeo import gdal
except ImportError:  # pragma: no cover - only without GDAL bindings
    gdal = None

DEFAULT_CACHE_BYTES = 384 * 1024 * 1024
# Pseudo resolution level marking quick (coarse) previews.
COARSE_LEVEL = 1000
_MAX_RUN_TILES = 8


# ---------------------------------------------------------------------------
# Tile cache
# ---------------------------------------------------------------------------

class TileCache:
    """Thread-safe LRU cache of float tiles with in-flight de-duplication."""

    def __init__(self, max_bytes=DEFAULT_CACHE_BYTES):
        self.max_bytes = int(max_bytes)
        self._lock = threading.Lock()
        self._data = OrderedDict()
        self._bytes = 0
        self._inflight = {}
        self.hits = 0
        self.misses = 0

    def __len__(self):
        return len(self._data)

    @property
    def nbytes(self):
        return self._bytes

    def contains(self, key):
        with self._lock:
            return key in self._data

    def _insert_locked(self, key, arr):
        old = self._data.pop(key, None)
        if old is not None:
            self._bytes -= old.nbytes
        self._data[key] = arr
        self._bytes += arr.nbytes
        while self._bytes > self.max_bytes and len(self._data) > 1:
            _, dropped = self._data.popitem(last=False)
            self._bytes -= dropped.nbytes

    def claim(self, keys):
        """Mark the given keys as being loaded by the caller.  Returns the
        subset that was neither cached nor already in flight."""
        claimed = []
        with self._lock:
            for k in keys:
                if k in self._data or k in self._inflight:
                    continue
                self._inflight[k] = threading.Event()
                claimed.append(k)
        return claimed

    def fulfil(self, key, arr):
        with self._lock:
            if arr is not None:
                self._insert_locked(key, arr)
            ev = self._inflight.pop(key, None)
        if ev is not None:
            ev.set()

    def get(self, key, loader):
        while True:
            with self._lock:
                arr = self._data.get(key)
                if arr is not None:
                    self._data.move_to_end(key)
                    self.hits += 1
                    return arr
                ev = self._inflight.get(key)
                if ev is None:
                    ev = threading.Event()
                    self._inflight[key] = ev
                    owner = True
                    self.misses += 1
                else:
                    owner = False
            if owner:
                arr = None
                try:
                    arr = loader()
                    return arr
                finally:
                    self.fulfil(key, arr)
            ev.wait()
            # Loop: the tile is now cached (or the load failed and we retry).

    def drop(self, source_key):
        """Release every tile of a source (``key[0] == source_key``)."""
        with self._lock:
            for k in [k for k in self._data if k[0] == source_key]:
                self._bytes -= self._data.pop(k).nbytes

    def clear(self):
        with self._lock:
            self._data.clear()
            self._bytes = 0


# ---------------------------------------------------------------------------
# Raster specification
# ---------------------------------------------------------------------------

class Level:
    """One resolution level (0 = full resolution, then overviews)."""

    __slots__ = ("index", "width", "height", "tile_w", "tile_h", "scale_x",
                 "scale_y")

    def __init__(self, index, width, height, tile_w, tile_h, scale_x=1.0,
                 scale_y=1.0):
        self.index = index
        self.width = int(width)
        self.height = int(height)
        self.tile_w = int(tile_w)
        self.tile_h = int(tile_h)
        # Pixel-space scale from full resolution to this level.
        self.scale_x = scale_x
        self.scale_y = scale_y


def _tile_dim(block, full):
    if 32 <= block <= 1024 and block < full:
        return block
    if block >= full and full <= 1024:
        return full
    return 256


class RasterSpec:
    """Everything a worker needs to read one band of one raster.

    Instances are treated as immutable once built.
    """

    def __init__(self):
        self.key = ""            # unique per source revision + band
        self.kind = "gdal"       # "gdal" or "provider"
        self.path = ""
        self.open_options = []
        self.band = 1
        self.width = 0
        self.height = 0
        self.gt = (0.0, 1.0, 0.0, 0.0, 0.0, -1.0)
        self.inv_gt = (0.0, 1.0, 0.0, 0.0, 0.0, -1.0)
        self.levels = []
        self.nodata = None
        self.user_nodata = []    # list of (min, max, include_min, include_max)
        self.scale = 1.0
        self.offset = 0.0
        self.use_mask = False
        self.store_dtype = np.float32
        self.provider_reader = None  # callable(xoff, yoff, w, h) -> ndarray
        self.provider_lock = None

    @property
    def full(self):
        return self.levels[0]

    def pixel_size(self):
        gt = self.gt
        return (float(np.hypot(gt[1], gt[4])), float(np.hypot(gt[2], gt[5])))

    def world_to_pixel(self, x, y):
        ig = self.inv_gt
        x = np.asarray(x, dtype=float)
        y = np.asarray(y, dtype=float)
        return ig[0] + ig[1] * x + ig[2] * y, ig[3] + ig[4] * x + ig[5] * y

    def pixel_to_world(self, px, py):
        g = self.gt
        px = np.asarray(px, dtype=float)
        py = np.asarray(py, dtype=float)
        return g[0] + g[1] * px + g[2] * py, g[3] + g[4] * px + g[5] * py


def invert_geotransform(gt):
    a, b, c, d, e, f = gt
    det = b * f - c * e
    if det == 0:
        raise ValueError("Geotransform is not invertible")
    ib = f / det
    ic = -c / det
    ie = -e / det
    if_ = b / det
    ia = -(ib * a + ic * d)
    id_ = -(ie * a + if_ * d)
    return (ia, ib, ic, id_, ie, if_)


def _store_dtype(gdal_type, scale, offset):
    if gdal is None:
        return np.float64
    small = {gdal.GDT_Byte, gdal.GDT_UInt16, gdal.GDT_Int16, gdal.GDT_Float32}
    if hasattr(gdal, "GDT_Int8"):
        small.add(gdal.GDT_Int8)
    if gdal_type in small and scale == 1.0 and offset == 0.0:
        return np.float32
    return np.float64


def gdal_open_quiet(path, open_options=None):
    """Open a dataset read-only, returning ``None`` on failure, without
    touching GDAL's global exception state."""
    if gdal is None:
        return None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        gdal.PushErrorHandler("CPLQuietErrorHandler")
        try:
            if open_options:
                ds = gdal.OpenEx(path, gdal.OF_RASTER | gdal.OF_READONLY,
                                 open_options=list(open_options))
            else:
                ds = gdal.Open(path, gdal.GA_ReadOnly)
        except Exception:  # GDAL exceptions may be enabled globally
            ds = None
        finally:
            gdal.PopErrorHandler()
    return ds


def source_revision(path):
    """Cheap token that changes when a file is rewritten."""
    try:
        st = os.stat(path)
        return "{}:{}".format(st.st_mtime_ns, st.st_size)
    except (OSError, ValueError, TypeError):
        return "0"


def build_gdal_spec(key, path, band=1, open_options=None, use_source_nodata=True,
                    user_nodata=None):
    """Inspect a GDAL dataset and return a :class:`RasterSpec`, or ``None``
    when GDAL cannot open it."""
    ds = gdal_open_quiet(path, open_options)
    if ds is None:
        return None
    if band < 1 or band > ds.RasterCount:
        return None
    b = ds.GetRasterBand(band)
    spec = RasterSpec()
    spec.key = key
    spec.kind = "gdal"
    spec.path = path
    spec.open_options = list(open_options or [])
    spec.band = band
    spec.width = ds.RasterXSize
    spec.height = ds.RasterYSize
    gt = ds.GetGeoTransform(can_return_null=True)
    if gt is None:
        gt = (0.0, 1.0, 0.0, float(spec.height), 0.0, -1.0)
    spec.gt = tuple(float(v) for v in gt)
    spec.inv_gt = invert_geotransform(spec.gt)
    bw, bh = b.GetBlockSize()
    levels = [Level(0, spec.width, spec.height, _tile_dim(bw, spec.width),
                    _tile_dim(bh, spec.height))]
    for i in range(b.GetOverviewCount()):
        ov = b.GetOverview(i)
        if ov is None:
            continue
        obw, obh = ov.GetBlockSize()
        levels.append(Level(i + 1, ov.XSize, ov.YSize,
                            _tile_dim(obw, ov.XSize), _tile_dim(obh, ov.YSize),
                            ov.XSize / float(spec.width),
                            ov.YSize / float(spec.height)))
    spec.levels = levels
    nd = b.GetNoDataValue()
    spec.nodata = nd if (use_source_nodata and nd is not None) else None
    spec.user_nodata = list(user_nodata or [])
    scale = b.GetScale()
    offset = b.GetOffset()
    spec.scale = 1.0 if scale in (None, 0) else float(scale)
    spec.offset = 0.0 if offset is None else float(offset)
    flags = b.GetMaskFlags()
    spec.use_mask = not (flags & gdal.GMF_ALL_VALID) and not (flags & gdal.GMF_NODATA)
    spec.store_dtype = _store_dtype(b.DataType, spec.scale, spec.offset)
    return spec


# ---------------------------------------------------------------------------
# Dataset pool (worker-owned handles)
# ---------------------------------------------------------------------------

class DatasetPool:
    """Hands out GDAL dataset handles, one per concurrent user."""

    def __init__(self):
        self._lock = threading.Lock()
        self._free = {}
        self._closed = set()

    def checkout(self, spec):
        k = (spec.path, tuple(spec.open_options))
        with self._lock:
            self._closed.discard(k)
            free = self._free.get(k)
            if free:
                return free.pop()
        if gdal is not None:
            # Read uncompressed strip TIFFs window-by-window on this thread.
            gdal.SetThreadLocalConfigOption("GTIFF_DIRECT_IO", "YES")
        return gdal_open_quiet(spec.path, spec.open_options)

    def checkin(self, spec, ds):
        if ds is None:
            return
        k = (spec.path, tuple(spec.open_options))
        with self._lock:
            if k in self._closed:
                return
            self._free.setdefault(k, []).append(ds)

    def close(self, path):
        with self._lock:
            for k in [k for k in self._free if k[0] == path]:
                self._free.pop(k, None)
                self._closed.add(k)

    def close_all(self):
        with self._lock:
            self._closed.update(self._free.keys())
            self._free.clear()


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _finish_values(raw, spec, mask=None):
    """Convert a raw window to the cache dtype with NoData -> NaN."""
    arr = raw.astype(spec.store_dtype, copy=True)
    invalid = None
    if spec.nodata is not None:
        nd = spec.nodata
        if np.issubdtype(raw.dtype, np.integer):
            info = np.iinfo(raw.dtype)
            if info.min <= nd <= info.max and float(nd).is_integer():
                invalid = raw == raw.dtype.type(nd)
        elif np.isnan(nd):
            invalid = np.isnan(raw)
        else:
            invalid = raw == raw.dtype.type(nd)
    if spec.user_nodata:
        vals = raw.astype(np.float64)
        for lo, hi, inc_lo, inc_hi in spec.user_nodata:
            lo_ok = vals >= lo if inc_lo else vals > lo
            hi_ok = vals <= hi if inc_hi else vals < hi
            hit = lo_ok & hi_ok
            invalid = hit if invalid is None else (invalid | hit)
    if mask is not None:
        hit = mask == 0
        invalid = hit if invalid is None else (invalid | hit)
    if spec.scale != 1.0 or spec.offset != 0.0:
        arr = arr * spec.scale + spec.offset
    if invalid is not None:
        arr[invalid] = np.nan
    return arr


class ReadContext:
    """Per-job reading state: a checked-out dataset handle (GDAL) and the
    shared tile cache."""

    def __init__(self, spec, cache, pool):
        self.spec = spec
        self.cache = cache
        self.pool = pool
        self._ds = None
        self._bands = {}
        self.tiles_read = 0

    def close(self):
        if self._ds is not None:
            # Our decoded tiles live in the shared TileCache; drop GDAL's copy
            # of the blocks read through this (worker-owned) handle so the
            # same data is not held twice.
            for b in self._bands.values():
                try:
                    b.FlushCache()
                except Exception:
                    _log.debug("GDAL FlushCache failed", exc_info=True)
            self.pool.checkin(self.spec, self._ds)
            self._ds = None
            self._bands.clear()

    def _band(self, level):
        b = self._bands.get(level.index)
        if b is not None:
            return b
        if self._ds is None:
            self._ds = self.pool.checkout(self.spec)
            if self._ds is None:
                raise IOError("Cannot open raster: {}".format(self.spec.path))
        b = self._ds.GetRasterBand(self.spec.band)
        if level.index > 0:
            b = b.GetOverview(level.index - 1)
        self._bands[level.index] = b
        return b

    def read_window(self, level, xoff, yoff, w, h):
        spec = self.spec
        self.tiles_read += 1
        if spec.kind == "provider":
            lock = spec.provider_lock
            if lock is not None:
                with lock:
                    raw, invalid = spec.provider_reader(xoff, yoff, w, h)
            else:
                raw, invalid = spec.provider_reader(xoff, yoff, w, h)
            arr = _finish_values(raw, spec)
            if invalid is not None:
                arr[invalid] = np.nan
            return arr
        band = self._band(level)
        raw = band.ReadAsArray(int(xoff), int(yoff), int(w), int(h))
        if raw is None:
            raise IOError("Read failed: {}".format(spec.path))
        mask = None
        if spec.use_mask:
            mb = band.GetMaskBand()
            mask = mb.ReadAsArray(int(xoff), int(yoff), int(w), int(h))
        return _finish_values(raw, spec, mask)

    def tile_key(self, level, tr, tc):
        return (self.spec.key, level.index, int(tr), int(tc))

    def load_tile(self, level, tr, tc):
        x0 = tc * level.tile_w
        y0 = tr * level.tile_h
        w = min(level.tile_w, level.width - x0)
        h = min(level.tile_h, level.height - y0)
        return self.read_window(level, x0, y0, w, h)

    def prefetch(self, level, tile_rows, tile_cols, cancelled=None):
        """Read missing tiles in horizontal runs with one call per run."""
        if self.spec.kind != "gdal" or tile_rows.size < 2:
            return
        keys = [self.tile_key(level, r, c) for r, c in zip(tile_rows, tile_cols)]
        claimed = set(self.cache.claim(keys))
        if not claimed:
            return
        pending = sorted((k[2], k[3]) for k in claimed)
        try:
            i = 0
            while i < len(pending):
                r, c = pending[i]
                j = i + 1
                while (j < len(pending) and pending[j][0] == r
                       and pending[j][1] == pending[j - 1][1] + 1
                       and j - i < _MAX_RUN_TILES):
                    j += 1
                if cancelled is not None and cancelled():
                    break
                c_first = c
                c_last = pending[j - 1][1]
                x0 = c_first * level.tile_w
                y0 = r * level.tile_h
                x1 = min((c_last + 1) * level.tile_w, level.width)
                h = min(level.tile_h, level.height - y0)
                block = self.read_window(level, x0, y0, x1 - x0, h)
                for cc in range(c_first, c_last + 1):
                    xs = (cc - c_first) * level.tile_w
                    tile = np.ascontiguousarray(
                        block[:, xs:xs + level.tile_w])
                    key = self.tile_key(level, r, cc)
                    self.cache.fulfil(key, tile)
                    claimed.discard(key)
                i = j
        finally:
            for key in claimed:  # release anything left after an error
                self.cache.fulfil(key, None)

    def lookup(self, level, rows, cols, cancelled=None):
        """Values at integer cells of ``level`` (NaN for OUTSIDE cells).

        Returns ``None`` when cancelled.
        """
        out = np.full(rows.shape, np.nan)
        inside = rows >= 0
        if not inside.any():
            return out
        r = rows[inside].astype(np.int64)
        c = cols[inside].astype(np.int64)
        tw, th = level.tile_w, level.tile_h
        ntc = (level.width + tw - 1) // tw
        tkey = (r // th) * ntc + (c // tw)
        if tkey.size > 1 and np.all(tkey[1:] >= tkey[:-1]):
            order = np.arange(tkey.size)
            ks = tkey
        else:
            order = np.argsort(tkey, kind="stable")
            ks = tkey[order]
        bounds = np.flatnonzero(ks[1:] != ks[:-1]) + 1
        starts = np.concatenate(([0], bounds))
        ends = np.concatenate((bounds, [ks.size]))
        uniq = ks[starts]
        self.prefetch(level, uniq // ntc, uniq % ntc, cancelled)
        vals = np.empty(r.size)
        for s, e, k in zip(starts, ends, uniq):
            if cancelled is not None and cancelled():
                return None
            tr, tc = divmod(int(k), ntc)
            tile = self.cache.get(self.tile_key(level, tr, tc),
                                  lambda tr=tr, tc=tc: self.load_tile(level, tr, tc))
            idx = order[s:e]
            vals[idx] = tile[r[idx] - tr * th, c[idx] - tc * tw]
        out[inside] = vals
        return out


# ---------------------------------------------------------------------------
# Piece sampling (runs on worker threads)
# ---------------------------------------------------------------------------

class NativeResult:
    """Cells crossed by a piece: chainage interval, cell and value."""

    __slots__ = ("ch0", "ch1", "row", "col", "value", "level")

    def __init__(self, ch0, ch1, row, col, value, level=0):
        self.ch0 = ch0
        self.ch1 = ch1
        self.row = row
        self.col = col
        self.value = value
        self.level = level


class StationResult:
    """Regular stations of a piece plus the value at the piece end."""

    __slots__ = ("ch", "value", "end_ch", "end_value", "level")

    def __init__(self, ch, value, end_ch, end_value, level=0):
        self.ch = ch
        self.value = value
        self.end_ch = end_ch
        self.end_value = end_value
        self.level = level


def sample_piece(ctx, piece, mode, level_index=0, cancelled=None, coarse_step=0.0):
    """Sample one pixel-space piece.

    ``piece`` is ``(px, py, ch)`` in full-resolution pixel space.
    ``mode`` is ``"native"``, ``"station"`` or ``"bilinear"``.
    ``coarse_step > 0`` produces a quick preview: cell values every
    ``coarse_step`` metres (flagged with ``COARSE_LEVEL``).
    Returns a :class:`NativeResult` / :class:`StationResult`, or ``None``
    when cancelled.
    """
    spec = ctx.spec
    level = spec.levels[level_index]
    px, py, ch = piece
    if level.index:
        px = px * level.scale_x
        py = py * level.scale_y
    if coarse_step and coarse_step > 0:
        return _sample_coarse(ctx, level, px, py, ch, mode, coarse_step, cancelled)
    if mode == "native":
        cells = sampling.native_cells(px, py, ch, level.width, level.height,
                                      cancelled)
        if cells is None:
            return None
        ch0, ch1, rows, cols = cells
        values = ctx.lookup(level, rows, cols, cancelled)
        if values is None:
            return None
        return NativeResult(ch0, ch1, rows, cols, values, level.index)

    s, sx, sy, ex, ey = sampling.stations_positions(px, py, ch)
    sx = np.append(sx, ex)
    sy = np.append(sy, ey)
    if mode == "bilinear":
        rows, cols, w, inside = sampling.bilinear_setup(sx, sy, level.width,
                                                         level.height)
        flat = ctx.lookup(level, rows.ravel(), cols.ravel(), cancelled)
        if flat is None:
            return None
        vals = sampling.bilinear_combine(flat.reshape(rows.shape), w, inside)
    else:
        rows, cols = sampling.nearest_cells(sx, sy, level.width, level.height)
        vals = ctx.lookup(level, rows, cols, cancelled)
        if vals is None:
            return None
    return StationResult(s, vals[:-1], float(ch[-1]), float(vals[-1]),
                         level.index)


def _sample_coarse(ctx, level, px, py, ch, mode, step, cancelled):
    s, sx, sy, ex, ey = sampling.stations_positions(px, py, ch, step)
    s = np.append(s, ch[-1])
    sx = np.append(sx, ex)
    sy = np.append(sy, ey)
    rows, cols = sampling.nearest_cells(sx, sy, level.width, level.height)
    vals = ctx.lookup(level, rows, cols, cancelled)
    if vals is None:
        return None
    if mode == "native":
        edges = np.concatenate(([ch[0]], 0.5 * (s[1:] + s[:-1]), [ch[-1]]))
        return NativeResult(edges[:-1], edges[1:], rows.astype(np.int32),
                            cols.astype(np.int32), vals, COARSE_LEVEL)
    return StationResult(s[:-1], vals[:-1], float(ch[-1]), float(vals[-1]),
                         COARSE_LEVEL)


def estimate_cells(spec, piece):
    """Approximate number of native cells a piece crosses."""
    px, py, _ = piece
    return float(np.sum(np.abs(np.diff(px)) + np.abs(np.diff(py))))


def estimate_tiles(spec, piece, level_index=0, cache=None):
    """Rough count of (uncached) tiles a piece touches, using the segment
    bounding boxes.  Cheap enough to call on the GUI thread."""
    level = spec.levels[level_index]
    px, py, _ = piece
    total = 0
    for i in range(len(px) - 1):
        x0, x1 = sorted((px[i] * level.scale_x, px[i + 1] * level.scale_x))
        y0, y1 = sorted((py[i] * level.scale_y, py[i + 1] * level.scale_y))
        x0 = max(x0, 0)
        y0 = max(y0, 0)
        x1 = min(x1, level.width)
        y1 = min(y1, level.height)
        if x1 <= x0 and y1 <= y0:
            continue
        nx = int((x1 - x0) / level.tile_w) + 1
        ny = int((y1 - y0) / level.tile_h) + 1
        total += nx + ny  # a line crosses ~nx + ny tiles, not nx * ny
    if cache is not None and total:
        # Sample the start tile to see whether we are warm.
        tr = int(max(py[0] * level.scale_y, 0) // level.tile_h)
        tc = int(max(px[0] * level.scale_x, 0) // level.tile_w)
        if cache.contains((spec.key, level.index, tr, tc)):
            total //= 4
    return total


def choose_preview_level(spec, length_m, target_cells=2000):
    """Overview level giving roughly ``target_cells`` along the line."""
    if len(spec.levels) < 2 or length_m <= 0:
        return 0
    psx, psy = spec.pixel_size()
    base = max(min(psx, psy), 1e-12)
    best = 0
    for lvl in spec.levels[1:]:
        cell = base / max(lvl.scale_x, 1e-12)
        if length_m / cell >= target_cells:
            best = lvl.index
        else:
            break
    if best == 0:
        # Even the finest overview is coarse enough? pick the first one.
        best = 1
    return best
