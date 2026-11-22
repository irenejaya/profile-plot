"""Profile session: rasters, the alignment, sampling jobs and assembly.

Everything here runs on the GUI thread except the jobs themselves.

Speed strategy
--------------
* Each fixed segment (vertex i -> i+1) is sampled once per raster and
  cached by a key made of the raster revision, sampling mode, the segment
  coordinates and its start chainage.  Adding a vertex samples one new
  segment; removing one re-uses the cached prefix.
* The moving tail (last vertex -> cursor) is sampled with coalescing: at
  most one tail job per raster is in flight, newer cursor positions
  replace the queued one, so the preview never falls behind.
* When a piece would read many uncached tiles and the raster has
  overviews, an explicitly labelled preview from an overview is produced
  first, then replaced by full-resolution values.
* Results are only ever looked up by the keys of the *current* line, so a
  late result from an older line can never be shown.
"""

import itertools
import math
import threading

import numpy as np
from qgis.core import (
    Qgis,
    QgsProject,
    QgsProviderRegistry,
    QgsRasterLayer,
    QgsRectangle,
)
from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QColor

from . import geometry, readers
from .assembly import (
    GradeInfo,
    NativeAssembler,
    TableData,
    assemble_stations,
    grade_for,
)
from .engine import PRIORITY_INTERACTIVE, PRIORITY_NORMAL, Job, SamplingEngine

# Estimated uncached tiles above which an overview preview is produced.
PREVIEW_TILE_THRESHOLD = 48
# Long segments are split into pieces of about this many tiles.
SPLIT_TILES = 24
# A moving tail crossing more cells than this is previewed coarsely (from an
# overview, or sparse samples) and refined to full resolution once idle.
TAIL_PREVIEW_CELLS = 12000
TAIL_TARGET_CELLS = 3000

# Colour presets in the order rasters are added: the DEM brown first, then
# five water-level colours.  Every raster after those is red.  Users can
# change any colour from the swatch in the raster list.
PRESET_COLORS = [
    "#8c5a2b",  # 1 terrain / DEM brown
    "#1f45e0",  # 2 water level: blue
    "#14c8b4",  # 3 water level: cyan
    "#7b3fe4",  # 4 water level: violet
    "#f08c00",  # 5 water level: orange
    "#2e9e44",  # 6 water level: green
]
OVERFLOW_COLOR = "#e00000"  # 7th raster onwards

MODES = ("native", "station", "bilinear")

_uid = itertools.count(1)


def _qgis_dtype_map():
    m = {}
    try:
        dt = Qgis.DataType
        m = {dt.Byte: np.uint8, dt.UInt16: np.uint16, dt.Int16: np.int16,
             dt.UInt32: np.uint32, dt.Int32: np.int32, dt.Float32: np.float32,
             dt.Float64: np.float64}
        if hasattr(dt, "Int8"):
            m[dt.Int8] = np.int8
    except AttributeError:  # pragma: no cover
        pass
    return m


class RasterEntry:
    """One row of the raster list."""

    def __init__(self, layer, band=1, color=None, owned=False):
        self.uid = next(_uid)
        self.layer = layer
        self.layer_id = layer.id()
        self.band = band
        self.color = QColor(color) if color is not None else QColor("#1f45e0")
        self.visible = True
        self.owned = owned  # True when the layer is not in the project
        self.spec = None
        self.error = ""
        self._xf = None
        self._xf_key = None

    @property
    def name(self):
        try:
            name = self.layer.name()
        except RuntimeError:
            name = "?"
        if self.layer.bandCount() > 1:
            name = "{} [b{}]".format(name, self.band)
        return name

    @property
    def crs(self):
        return self.layer.crs()

    def cell_size(self):
        if self.spec is not None:
            return self.spec.pixel_size()
        return (self.layer.rasterUnitsPerPixelX(), self.layer.rasterUnitsPerPixelY())

    def transform_from(self, measure_crs):
        key = geometry.crs_key(measure_crs) + "|" + geometry.crs_key(self.crs)
        if key != self._xf_key:
            src = measure_crs
            dst = self.crs
            # A raster without a valid CRS is assumed to share the line's CRS.
            same = src == dst or dst is None or not dst.isValid()
            self._xf = None if same else geometry.make_transform(src, dst)
            self._xf_key = key
        return self._xf

    # -- spec -------------------------------------------------------------

    def build_spec(self):
        """(Re)build the reader spec.  Returns True when the spec changed."""
        layer = self.layer
        provider = layer.dataProvider()
        band = self.band
        use_src_nd = True
        user_nd = []
        try:
            use_src_nd = provider.useSourceNoDataValue(band)
            for r in provider.userNoDataValues(band):
                b = int(r.bounds()) if not hasattr(r.bounds(), "value") else r.bounds().value
                # 0 IncludeMinAndMax, 1 IncludeMax, 2 IncludeMin, 3 Exclusive
                user_nd.append((r.min(), r.max(), b in (0, 2), b in (0, 1)))
        except Exception:
            pass
        spec = None
        if layer.providerType() == "gdal":
            source = layer.source()
            path = source
            open_options = None
            try:
                parts = QgsProviderRegistry.instance().decodeUri("gdal", source)
                path_only = parts.get("path") or source
                open_options = parts.get("openOptions") or None
            except Exception:
                path_only = source
            rev = readers.source_revision(path_only)
            key = "{}|{}|{}|{}|{}".format(self.layer_id, band, rev, use_src_nd, user_nd)
            if self.spec is not None and self.spec.key == key:
                return False
            spec = readers.build_gdal_spec(key, path, band, None, use_src_nd, user_nd)
            if spec is None and path_only != source:
                spec = readers.build_gdal_spec(key, path_only, band, open_options,
                                               use_src_nd, user_nd)
        if spec is None:
            key = "{}|{}|provider".format(self.layer_id, band)
            if self.spec is not None and self.spec.key == key:
                return False
            spec = self._provider_spec(key)
        old = self.spec
        self.spec = spec
        self.error = "" if spec is not None else "Cannot read this raster"
        return old is None or spec is None or old.key != spec.key

    def _provider_spec(self, key):
        layer = self.layer
        provider = layer.dataProvider()
        if provider is None or not provider.isValid():
            return None
        clone = provider.clone()
        if clone is None:
            return None
        width = provider.xSize()
        height = provider.ySize()
        ext = provider.extent()
        if width <= 0 or height <= 0 or ext.isEmpty():
            return None
        rx = ext.width() / width
        ry = ext.height() / height
        spec = readers.RasterSpec()
        spec.key = key
        spec.kind = "provider"
        spec.band = self.band
        spec.width = width
        spec.height = height
        spec.gt = (ext.xMinimum(), rx, 0.0, ext.yMaximum(), 0.0, -ry)
        spec.inv_gt = readers.invert_geotransform(spec.gt)
        spec.levels = [readers.Level(0, width, height, min(256, width), min(256, height))]
        spec.store_dtype = np.float64
        dtypes = _qgis_dtype_map()
        band = self.band

        def reader(xoff, yoff, w, h):
            rect = QgsRectangle(spec.gt[0] + xoff * rx, spec.gt[3] - (yoff + h) * ry,
                                spec.gt[0] + (xoff + w) * rx, spec.gt[3] - yoff * ry)
            block = clone.block(band, rect, int(w), int(h))
            dtype = dtypes.get(block.dataType(), np.float64)
            raw = np.frombuffer(bytes(block.data()), dtype=dtype)
            if raw.size != w * h:
                return np.full((h, w), np.nan), None
            raw = raw.reshape(h, w)
            invalid = None
            if block.hasNoDataValue():
                invalid = raw == block.noDataValue()
            if block.hasNoData():
                extra = np.zeros((h, w), dtype=bool)
                for r in range(h):
                    for c in range(w):
                        if block.isNoData(r, c):
                            extra[r, c] = True
                invalid = extra if invalid is None else (invalid | extra)
            return raw, invalid

        spec.provider_reader = reader
        spec.provider_lock = threading.Lock()
        return spec


class _TailState:
    __slots__ = ("running", "pending", "result", "result_key", "anchor")

    def __init__(self):
        self.running = None   # Job in flight
        self.pending = None   # (key, piece, level) waiting
        self.result = None    # last result
        self.result_key = None
        self.anchor = None    # (ax, ay, ca) of the result


class ProfileSession(QObject):
    """State and logic behind the dock (no widgets here)."""

    profilesChanged = pyqtSignal()
    lineChanged = pyqtSignal()
    rastersChanged = pyqtSignal()
    colorChanged = pyqtSignal(int, QColor)   # uid, colour (no list rebuild)
    slopeChanged = pyqtSignal()
    message = pyqtSignal(str, int)   # text, Qgis.MessageLevel value

    def __init__(self, canvas, parent=None):
        super().__init__(parent)
        self.canvas = canvas
        self.engine = SamplingEngine(parent=self)
        self.engine.jobFinished.connect(self._on_job_finished)
        self.rasters = []
        self.mode = "native"
        self.slope_uid = None

        self.line_points = []     # QgsPointXY, line CRS
        self.line_crs = None
        self.finished = False
        self.tail_point = None    # QgsPointXY, line CRS
        self.measure_crs = None
        self._measure_key = ""
        self.xy_m = np.empty((0, 2))
        self.ch = np.empty(0)
        self.tail_xy = None
        self.tail_ch = None
        self.revision = 0

        self._results = {}
        self._jobs = {}
        self._tails = {}
        self._provisional = {}
        self._assemblers = {}
        self.profiles = {}
        self._table = None
        self._errors_reported = set()
        self.mapper = None

        self._refresh = QTimer(self)
        self._refresh.setSingleShot(True)
        self._refresh.setInterval(0)
        self._refresh.timeout.connect(self._assemble)

        project = QgsProject.instance()
        project.layersWillBeRemoved.connect(self._on_layers_removed)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def shutdown(self):
        try:
            QgsProject.instance().layersWillBeRemoved.disconnect(self._on_layers_removed)
        except (TypeError, RuntimeError):
            pass
        for job in list(self._jobs.values()):
            job.cancel()
        self._jobs.clear()
        self.engine.shutdown()

    # ------------------------------------------------------------------
    # rasters
    # ------------------------------------------------------------------

    def next_color(self):
        """First preset not used by a raster in the list, else red."""
        used = {e.color.name().lower() for e in self.rasters}
        for c in PRESET_COLORS:
            if c not in used:
                return c
        return OVERFLOW_COLOR

    def entry(self, uid):
        for e in self.rasters:
            if e.uid == uid:
                return e
        return None

    def has_layer(self, layer_id, band=None):
        return any(e.layer_id == layer_id and (band is None or e.band == band)
                   for e in self.rasters)

    def add_layers(self, layers, owned=False):
        added = 0
        for layer in layers:
            if not isinstance(layer, QgsRasterLayer) or not layer.isValid():
                continue
            if self.has_layer(layer.id(), 1):
                continue
            entry = RasterEntry(layer, 1, self.next_color(), owned=owned)
            entry.build_spec()
            if entry.spec is None:
                self.message.emit("Profile Plot cannot read raster '{}'".format(
                    layer.name()), 1)
            self.rasters.append(entry)
            layer.nameChanged.connect(self.rastersChanged)
            added += 1
        if added:
            self.rastersChanged.emit()
            self._request_all()
        return added

    def remove_entry(self, uid):
        e = self.entry(uid)
        if e is None:
            return
        self.rasters.remove(e)
        if self.slope_uid == uid:
            self.slope_uid = None
        self._release(e)
        self.rastersChanged.emit()
        self._schedule()

    def _release(self, e):
        try:
            e.layer.nameChanged.disconnect(self.rastersChanged)
        except (TypeError, RuntimeError):
            pass
        if e.spec is not None and not any(o.spec is not None and o.spec.key == e.spec.key
                                          for o in self.rasters):
            self.engine.release_source(e.spec)
        self._tails.pop(e.uid, None)
        self._assemblers.pop(e.uid, None)
        self.profiles.pop(e.uid, None)

    def move_entry(self, uid, new_index):
        e = self.entry(uid)
        if e is None:
            return
        self.rasters.remove(e)
        new_index = max(0, min(new_index, len(self.rasters)))
        self.rasters.insert(new_index, e)
        self.rastersChanged.emit()
        self._schedule()

    def reorder(self, uids):
        order = {u: i for i, u in enumerate(uids)}
        self.rasters.sort(key=lambda e: order.get(e.uid, 1 << 30))
        self.rastersChanged.emit()
        self._schedule()

    def set_visible(self, uid, visible):
        e = self.entry(uid)
        if e is None or e.visible == visible:
            return
        e.visible = visible
        self._request_all()
        self._schedule()

    def set_color(self, uid, color):
        e = self.entry(uid)
        color = QColor(color)
        if e is None or not color.isValid() or e.color == color:
            return
        e.color = color
        self.colorChanged.emit(uid, QColor(color))

    def set_band(self, uid, band):
        e = self.entry(uid)
        if e is None or e.band == band:
            return
        old = e.spec
        e.band = band
        e.build_spec()
        if old is not None:
            self.engine.release_source(old)
        self._tails.pop(uid, None)
        self._assemblers.pop(uid, None)
        self.rastersChanged.emit()
        self._request_all()

    def set_slope(self, uid):
        if uid == self.slope_uid:
            return
        self.slope_uid = uid
        self.slopeChanged.emit()
        self._schedule()

    def set_mode(self, mode):
        if mode not in MODES or mode == self.mode:
            return
        self.mode = mode
        self._provisional = {}
        for st in self._tails.values():
            if st.running is not None:
                st.running.cancel()
        self._tails.clear()
        self._assemblers.clear()
        self._prune_jobs()
        self._request_all()

    def _on_layers_removed(self, layers):
        ids = {lyr if isinstance(lyr, str) else lyr.id() for lyr in layers}
        gone = [e for e in self.rasters if e.layer_id in ids and not e.owned]
        if not gone:
            return
        for e in gone:
            self.rasters.remove(e)
            if self.slope_uid == e.uid:
                self.slope_uid = None
            self._release(e)
        self.rastersChanged.emit()
        self._schedule()

    def refresh_sources(self):
        """Pick up rewritten files (new mtime -> new revision)."""
        changed = False
        for e in self.rasters:
            old = e.spec
            if e.build_spec():
                changed = True
                if old is not None:
                    self.engine.release_source(old)
                self._tails.pop(e.uid, None)
        return changed

    # ------------------------------------------------------------------
    # alignment
    # ------------------------------------------------------------------

    @property
    def has_line(self):
        return len(self.line_points) > 0

    def distinct_vertex_count(self):
        if not self.line_points:
            return 0
        n = 1
        for a, b in zip(self.line_points[:-1], self.line_points[1:]):
            if a != b:
                n += 1
        return n

    def total_length(self):
        length = float(self.ch[-1]) if self.ch.size else 0.0
        if self.tail_ch is not None:
            length = self.tail_ch
        return length

    def fixed_length(self):
        return float(self.ch[-1]) if self.ch.size else 0.0

    def start_line(self, point, crs):
        self._provisional = {}
        self._cancel_all_jobs()
        self._results.clear()
        self._tails.clear()
        self._assemblers.clear()
        self.profiles = {}
        self._table = None
        self.revision += 1
        self.refresh_sources()
        self.line_crs = crs
        self.line_points = [point]
        self.finished = False
        self.tail_point = None
        self.tail_xy = None
        self.tail_ch = None
        self.measure_crs = geometry.choose_measure_crs(crs, point)
        self._measure_key = geometry.crs_key(self.measure_crs)
        self._line_xf = geometry.make_transform(crs, self.measure_crs)
        self._update_xy()
        self.lineChanged.emit()
        self._schedule()

    def add_vertex(self, point):
        if not self.line_points:
            return
        if point == self.line_points[-1]:
            return
        prev_xy = self.xy_m[-1].copy()
        prev_ch = self.fixed_length()
        self.line_points.append(point)
        self.revision += 1
        self._capture_provisional(prev_xy, prev_ch, point)
        self.tail_point = None
        self.tail_xy = None
        self.tail_ch = None
        self._update_xy()
        self._request_fixed()
        self.lineChanged.emit()
        self._schedule()

    def _capture_provisional(self, a, ca, point):
        """Keep the last tail result as a stand-in for the segment just
        fixed, so the plot does not flash a gap while full-resolution
        values for that segment load."""
        self._provisional = {}
        b = geometry.to_xy(self._line_xf, [point])[0]
        for uid, st in self._tails.items():
            k = st.result_key
            if st.result is None or k is None:
                continue
            if (k[4], k[5], k[6], k[7], k[8]) == (float(a[0]), float(a[1]), float(b[0]),
                                                  float(b[1]), ca) and k[1] == self.mode:
                self._provisional[uid] = (len(self.line_points) - 2, k[0], st.result)

    def remove_last_vertex(self):
        if len(self.line_points) <= 1:
            self.clear_line()
            return
        self.line_points.pop()
        self._provisional = {}
        self.revision += 1
        self._update_xy()
        self._prune_jobs()
        if self.tail_point is not None:
            self.set_tail(self.tail_point)
        self.lineChanged.emit()
        self._schedule()

    def set_tail(self, point):
        """Moving end of the line while drawing (``None`` hides it)."""
        if not self.line_points or self.finished:
            return
        self.tail_point = point
        if point is None:
            self.tail_xy = None
            self.tail_ch = None
        else:
            xy = geometry.to_xy(self._line_xf, [point])[0]
            last = self.xy_m[-1]
            d = float(np.hypot(xy[0] - last[0], xy[1] - last[1]))
            if d <= 0:
                self.tail_xy = None
                self.tail_ch = None
            else:
                self.tail_xy = xy
                self.tail_ch = self.fixed_length() + d
                self._request_tail()
        # No lineChanged here: the dock moves the canvas tail on a fast path.
        self._schedule()

    def finish_line(self):
        if self.distinct_vertex_count() < 2:
            self.clear_line()
            return False
        self.finished = True
        self.tail_point = None
        self.tail_xy = None
        self.tail_ch = None
        self._tails.clear()
        self.revision += 1
        self._request_fixed()
        self.lineChanged.emit()
        self._schedule()
        return True

    def set_line(self, points, crs):
        """Use an existing line (e.g. a selected feature)."""
        pts = [p for i, p in enumerate(points) if i == 0 or p != points[i - 1]]
        if len(pts) < 2:
            return False
        self.start_line(pts[0], crs)
        self.line_points = list(pts)
        self._update_xy()
        self.finished = True
        self._request_fixed()
        self.lineChanged.emit()
        self._schedule()
        return True

    def reset(self):
        """Forget everything that was plotted: the line, the profiles, the
        raster list and the slope layer, and free cached tiles.  Used when
        the dock is closed, so it reopens empty."""
        self.clear_line()
        rasters, self.rasters = self.rasters, []
        for e in rasters:
            self._release(e)
        self.slope_uid = None
        self._tails.clear()
        self._assemblers.clear()
        self._errors_reported.clear()
        self.engine.cache.clear()
        self.engine.pool.close_all()
        self.rastersChanged.emit()
        self.slopeChanged.emit()
        self.profilesChanged.emit()

    def clear_line(self):
        self._provisional = {}
        self._cancel_all_jobs()
        self.line_points = []
        self.finished = False
        self.tail_point = None
        self.tail_xy = None
        self.tail_ch = None
        self.xy_m = np.empty((0, 2))
        self.ch = np.empty(0)
        self.revision += 1
        self._results.clear()
        self._tails.clear()
        self._assemblers.clear()
        self.profiles = {}
        self._table = None
        self.mapper = None
        self.lineChanged.emit()
        self.profilesChanged.emit()

    def _update_xy(self):
        self.xy_m = geometry.to_xy(self._line_xf, self.line_points)
        self.ch = geometry.cumulative_chainage(self.xy_m)

    def update_mapper(self, canvas_crs):
        xy = self.xy_m
        ch = self.ch
        if self.tail_xy is not None:
            xy = np.vstack((xy, self.tail_xy))
            ch = np.append(ch, self.tail_ch)
        self.mapper = geometry.ChainageMapper(xy, ch, self.measure_crs, canvas_crs)
        return self.mapper

    # ------------------------------------------------------------------
    # job management
    # ------------------------------------------------------------------

    def _segments(self, entry=None):
        """Fixed segments (vertex i -> i+1).  With ``entry``, long segments
        are split into sub-pieces of about ``SPLIT_TILES`` tiles of that
        raster so several workers share the work.  Splits depend only on
        the segment and the raster, so keys stay stable as vertices are
        added."""
        split = self._split_length(entry) if entry is not None else None
        segs = []
        for i in range(len(self.xy_m) - 1):
            a = self.xy_m[i]
            b = self.xy_m[i + 1]
            ca = float(self.ch[i])
            cb = float(self.ch[i + 1])
            n = 1 if split is None else max(1, int(math.ceil((cb - ca) / split)))
            if n == 1:
                segs.append((a, b, ca, cb, i))
                continue
            prev_p, prev_c = a, ca
            for j in range(1, n + 1):
                if j == n:
                    p, c = b, cb
                else:
                    t = j / float(n)
                    p = a + (b - a) * t
                    c = ca + (cb - ca) * t
                segs.append((prev_p, p, prev_c, c, i))
                prev_p, prev_c = p, c
        return segs

    @staticmethod
    def _split_length(entry):
        spec = entry.spec
        lvl = spec.levels[0]
        px = min(spec.pixel_size())
        try:
            if entry.crs.isGeographic():
                px *= 111320.0
        except Exception:
            pass
        return max(SPLIT_TILES * min(lvl.tile_w, lvl.tile_h) * px, 1.0)

    def _key(self, entry, level, a, b, ca, mode=None):
        return (entry.spec.key, mode or self.mode, level, self._measure_key,
                float(a[0]), float(a[1]), float(b[0]), float(b[1]), ca)

    def _active_entries(self):
        return [e for e in self.rasters if e.visible and e.spec is not None]

    def _request_all(self):
        self._request_fixed()
        if self.tail_xy is not None:
            self._request_tail()
        self._schedule()

    def _request_fixed(self):
        if self.measure_crs is None:
            return
        for e in self._active_entries():
            xf = e.transform_from(self.measure_crs)
            for a, b, ca, cb, _ in self._segments(e):
                if cb <= ca:
                    continue
                key = self._key(e, 0, a, b, ca)
                if key in self._results or key in self._jobs:
                    continue
                piece = geometry.build_piece(e.spec, xf, a, b, ca, cb)
                est = readers.estimate_tiles(e.spec, piece, 0, self.engine.cache)
                if est > PREVIEW_TILE_THRESHOLD and len(e.spec.levels) > 1:
                    lvl = readers.choose_preview_level(e.spec, cb - ca)
                    pkey = self._key(e, lvl, a, b, ca)
                    if pkey not in self._results and pkey not in self._jobs:
                        self._submit(pkey, e, piece, lvl, PRIORITY_INTERACTIVE, "fixed")
                self._submit(key, e, piece, 0, PRIORITY_NORMAL, "fixed")

    def _submit(self, key, entry, piece, level, priority, tag):
        mode = key[1]
        job = Job(key, entry.spec, piece, mode, level, priority, (tag, entry.uid))
        self._jobs[key] = job
        self.engine.submit(job)
        return job

    def _request_tail(self):
        if self.tail_xy is None or not self.xy_m.size:
            return
        a = self.xy_m[-1]
        b = self.tail_xy
        ca = self.fixed_length()
        cb = self.tail_ch
        for e in self._active_entries():
            st = self._tails.setdefault(e.uid, _TailState())
            xf = e.transform_from(self.measure_crs)
            piece = geometry.build_piece(e.spec, xf, a, b, ca, cb)
            level, coarse = self._tail_plan(e, piece, cb - ca)
            key = self._key(e, -1 if coarse else level, a, b, ca)
            if st.result_key == key or (st.running is not None and st.running.key == key):
                st.pending = None
                continue
            if st.running is not None and st.running.refine:
                st.running.cancel()   # a new position beats refining an old one
                st.running = None
            if st.running is None:
                st.running = self._submit_tail(key, e, piece, level, coarse)
                st.pending = None
            else:
                st.pending = (key, piece, level, coarse)

    def _tail_plan(self, entry, piece, length):
        """(level, coarse spacing) used to sample a moving tail."""
        spec = entry.spec
        has_overviews = len(spec.levels) > 1
        if readers.estimate_cells(spec, piece) > TAIL_PREVIEW_CELLS:
            if has_overviews:
                return readers.choose_preview_level(spec, length, TAIL_TARGET_CELLS), 0.0
            return 0, max(length / TAIL_TARGET_CELLS, 1e-6)
        est = readers.estimate_tiles(spec, piece, 0, self.engine.cache)
        if est > PREVIEW_TILE_THRESHOLD and has_overviews:
            return readers.choose_preview_level(spec, length), 0.0
        return 0, 0.0

    def _submit_tail(self, key, entry, piece, level, coarse=0.0, refine=False):
        job = Job(key, entry.spec, piece, key[1], level,
                  PRIORITY_NORMAL if refine else PRIORITY_INTERACTIVE,
                  ("tail", entry.uid), coarse, refine)
        self.engine.submit(job)
        return job

    def _cancel_all_jobs(self):
        for job in self._jobs.values():
            job.cancel()
        self._jobs.clear()
        for st in self._tails.values():
            if st.running is not None:
                st.running.cancel()

    def _needed_keys(self):
        keys = set()
        for e in self._active_entries():
            for a, b, ca, cb, _ in self._segments(e):
                for lvl in range(len(e.spec.levels)):
                    keys.add(self._key(e, lvl, a, b, ca))
        return keys

    def _prune_jobs(self):
        needed = self._needed_keys()
        for key in [k for k in self._jobs if k not in needed]:
            self._jobs.pop(key).cancel()
        for key in [k for k in self._results if k not in needed]:
            del self._results[key]

    def _on_job_finished(self, job, result):
        tag, uid = job.tag
        if isinstance(result, Exception):
            self._report_error(job, result)
            result = None
        if tag == "tail":
            st = self._tails.get(uid)
            if st is None or st.running is not job:
                return
            st.running = None
            if result is not None:
                st.result = result
                st.result_key = job.key
                st.anchor = (job.key[4], job.key[5], job.key[8], job.key[0], job.key[1])
            if st.pending is not None:
                key, piece, level, coarse = st.pending
                st.pending = None
                e = self.entry(uid)
                if e is not None and e.spec is not None:
                    st.running = self._submit_tail(key, e, piece, level, coarse)
            elif result is not None and (job.level > 0 or job.coarse > 0):
                # Idle with a preview tail: refine to full resolution.
                e = self.entry(uid)
                if e is not None and e.spec is not None:
                    key = (job.key[0], job.key[1], 0) + job.key[3:]
                    st.running = self._submit_tail(key, e, job.piece, 0, 0.0, refine=True)
        else:
            if self._jobs.get(job.key) is job:
                del self._jobs[job.key]
                if result is not None:
                    self._results[job.key] = result
                else:
                    self._results[job.key] = None  # error: keep as gap
        self._schedule()

    def _report_error(self, job, exc):
        sig = (job.spec.key, str(exc))
        if sig in self._errors_reported:
            return
        self._errors_reported.add(sig)
        self.message.emit("Profile Plot: {}".format(exc), 1)

    def busy(self):
        if self._jobs:
            return True
        return any(st.running is not None for st in self._tails.values())

    # ------------------------------------------------------------------
    # assembly
    # ------------------------------------------------------------------

    def _schedule(self):
        if not self._refresh.isActive():
            self._refresh.start()

    def _best_result(self, entry, a, b, ca):
        res = self._results.get(self._key(entry, 0, a, b, ca))
        if res is not None:
            return res
        for lvl in range(1, len(entry.spec.levels)):
            res = self._results.get(self._key(entry, lvl, a, b, ca))
            if res is not None:
                return res
        return None

    def _fixed_parts(self, entry):
        prov = self._provisional.get(entry.uid)
        if prov is not None and prov[1] != entry.spec.key:
            prov = None
        parts = []
        last_seg = []
        for a, b, ca, cb, seg in self._segments(entry):
            if cb <= ca:
                continue
            if prov is not None and seg == prov[0]:
                full = self._results.get(self._key(entry, 0, a, b, ca))
                last_seg.append((ca, cb, full))
                continue
            parts.append((ca, cb, self._best_result(entry, a, b, ca)))
        if last_seg:
            if all(r is not None for _, _, r in last_seg):
                parts.extend(last_seg)
                self._provisional.pop(entry.uid, None)
            else:
                parts.append((last_seg[0][0], last_seg[-1][1], prov[2]))
        return parts

    def _tail_part(self, entry):
        if self.tail_xy is None:
            return None
        st = self._tails.get(entry.uid)
        if st is None or st.result is None or st.anchor is None:
            return (self.fixed_length(), self.tail_ch, None)
        a = self.xy_m[-1]
        ax, ay, ca, skey, mode = st.anchor
        if (ax != float(a[0]) or ay != float(a[1]) or ca != self.fixed_length()
                or skey != entry.spec.key or mode != self.mode):
            return (self.fixed_length(), self.tail_ch, None)
        res = st.result
        end = res.ch1[-1] if hasattr(res, "ch1") and res.ch1.size else getattr(res, "end_ch", ca)
        return (ca, float(end), res)

    def _assemble(self):
        profiles = {}
        if self.line_points and self.measure_crs is not None:
            for e in self._active_entries():
                fixed = self._fixed_parts(e)
                tail = self._tail_part(e)
                if not fixed and tail is None:
                    continue
                length = tail[1] if tail is not None else (fixed[-1][1] if fixed else 0.0)
                if self.mode == "native":
                    asm = self._assemblers.setdefault(e.uid, NativeAssembler())
                    prof = asm.assemble(fixed, tail, length)
                else:
                    prof = assemble_stations(fixed, tail, length,
                                             self.mode == "bilinear")
                if prof is not None:
                    profiles[e.uid] = prof
        self.profiles = profiles
        self._table = None
        self.profilesChanged.emit()

    # ------------------------------------------------------------------
    # derived values
    # ------------------------------------------------------------------

    def grade(self):
        """GradeInfo for the slope layer, or ``None`` when no slope layer is
        set or its curve is hidden."""
        if self.slope_uid is None:
            return None
        e = self.entry(self.slope_uid)
        if e is None or not e.visible:
            return None
        prof = self.profiles.get(e.uid)
        if prof is None:
            return GradeInfo(reason="unavailable", length=self.total_length())
        return grade_for(prof)

    def visible_entries(self):
        return [e for e in self.rasters if e.visible]

    def table(self):
        if self._table is None:
            entries = [e for e in self.visible_entries() if e.uid in self.profiles]
            self._table = TableData([self.profiles[e.uid] for e in entries],
                                    [e.name for e in entries])
            self._table.interpolated = self.mode == "bilinear"
        return self._table

    def is_final(self):
        """True when the finished line has full-resolution values for every
        visible raster."""
        if not self.finished or self.busy():
            return False
        return not any(p.preview or p.pending for p in self.profiles.values())
