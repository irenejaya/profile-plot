"""Pure-numpy geometry kernels for Profile Plot.

Nothing in this module imports QGIS or Qt, so it is cheap to import, easy
to unit test and safe to run on worker threads.

Conventions
-----------
* Pixel space: ``x`` is the column coordinate and ``y`` the row coordinate
  obtained by applying the inverse GDAL geotransform.  Cell ``(row, col)``
  covers the half-open square ``[col, col + 1) x [row, row + 1)``.
* A *piece* is a polyline in pixel space with a chainage (metres along the
  alignment) at every vertex.  Chainage is linear in the segment parameter
  between consecutive piece vertices, which is exact when the measurement
  CRS equals the raster CRS and is guaranteed to within a small fraction of
  a pixel otherwise, because the caller densifies the piece.
"""

import numpy as np

# Intervals shorter than this (in pixels) are treated as corner touches.
_MIN_PIXEL_LENGTH = 1e-9
# Station spacing for 1 cm station mode, in metres.
STATION_STEP = 0.01
# Row/col value used for intervals that fall outside the raster.
OUTSIDE = -1


def _clip_param(x0, y0, x1, y1, width, height):
    """Liang-Barsky clip of a segment against [0, width] x [0, height].

    Returns ``(t0, t1)`` or ``None`` when the segment misses the box.
    """
    t0, t1 = 0.0, 1.0
    dx = x1 - x0
    dy = y1 - y0
    for p, q in ((-dx, x0), (dx, width - x0), (-dy, y0), (dy, height - y0)):
        if p == 0.0:
            if q < 0.0:
                return None
            continue
        r = q / p
        if p < 0.0:
            if r > t1:
                return None
            if r > t0:
                t0 = r
        else:
            if r < t0:
                return None
            if r < t1:
                t1 = r
    if t1 - t0 <= 0.0:
        return None
    return t0, t1


def _axis_crossings(a0, a1, t0, t1):
    """Parameters where coordinate a(t) = a0 + t*(a1-a0) crosses integers
    strictly inside (t0, t1)."""
    da = a1 - a0
    if da == 0.0:
        return np.empty(0)
    lo = a0 + t0 * da
    hi = a0 + t1 * da
    if lo > hi:
        lo, hi = hi, lo
    k0 = np.floor(lo) + 1.0
    k1 = np.ceil(hi) - 1.0
    if k1 < k0:
        return np.empty(0)
    ks = np.arange(k0, k1 + 1.0)
    return (ks - a0) / da


def traverse_segment(x0, y0, x1, y1, width, height):
    """Walk one pixel-space segment through the raster grid.

    Returns ``(t_start, t_end, row, col)`` arrays describing every cell the
    segment crosses over a positive distance, in order.  Portions outside
    the raster are returned as intervals with ``row = col = OUTSIDE``.
    Corner-only touches are ignored.
    """
    seg_px = float(np.hypot(x1 - x0, y1 - y0))
    if seg_px <= 0.0:
        empty_f = np.empty(0)
        empty_i = np.empty(0, dtype=np.int32)
        return empty_f, empty_f, empty_i, empty_i

    clip = _clip_param(x0, y0, x1, y1, float(width), float(height))
    ts_parts = []
    rows_parts = []
    cols_parts = []
    te_parts = []

    if clip is None:
        return (np.array([0.0]), np.array([1.0]),
                np.array([OUTSIDE], dtype=np.int32),
                np.array([OUTSIDE], dtype=np.int32))

    c0, c1 = clip
    if c0 > 0.0:
        ts_parts.append(np.array([0.0]))
        te_parts.append(np.array([c0]))
        rows_parts.append(np.array([OUTSIDE], dtype=np.int32))
        cols_parts.append(np.array([OUTSIDE], dtype=np.int32))

    t = np.concatenate((
        [c0],
        _axis_crossings(x0, x1, c0, c1),
        _axis_crossings(y0, y1, c0, c1),
        [c1],
    ))
    t.sort(kind="mergesort")
    keep = np.diff(t) * seg_px > _MIN_PIXEL_LENGTH
    ta = t[:-1][keep]
    tb = t[1:][keep]
    if ta.size:
        tm = 0.5 * (ta + tb)
        cols = np.floor(x0 + tm * (x1 - x0)).astype(np.int64)
        rows = np.floor(y0 + tm * (y1 - y0)).astype(np.int64)
        outside = (cols < 0) | (rows < 0) | (cols >= width) | (rows >= height)
        cols[outside] = OUTSIDE
        rows[outside] = OUTSIDE
        ts_parts.append(ta)
        te_parts.append(tb)
        rows_parts.append(rows.astype(np.int32))
        cols_parts.append(cols.astype(np.int32))

    if c1 < 1.0:
        ts_parts.append(np.array([c1]))
        te_parts.append(np.array([1.0]))
        rows_parts.append(np.array([OUTSIDE], dtype=np.int32))
        cols_parts.append(np.array([OUTSIDE], dtype=np.int32))

    if not ts_parts:
        empty_f = np.empty(0)
        empty_i = np.empty(0, dtype=np.int32)
        return empty_f, empty_f, empty_i, empty_i
    return (np.concatenate(ts_parts), np.concatenate(te_parts),
            np.concatenate(rows_parts), np.concatenate(cols_parts))


def merge_runs(ch0, ch1, rows, cols):
    """Merge consecutive intervals that refer to the same cell (or to
    consecutive outside intervals)."""
    n = ch0.size
    if n < 2:
        return ch0, ch1, rows, cols
    change = (rows[1:] != rows[:-1]) | (cols[1:] != cols[:-1])
    starts = np.concatenate(([0], np.flatnonzero(change) + 1))
    if starts.size == n:
        return ch0, ch1, rows, cols
    ends = np.concatenate((starts[1:] - 1, [n - 1]))
    return ch0[starts], ch1[ends], rows[starts], cols[starts]


def native_cells(px, py, ch, width, height, cancelled=None):
    """Native-cell traversal of a pixel-space piece.

    ``px``, ``py`` and ``ch`` are equal-length vertex arrays.  Returns
    ``(ch0, ch1, row, col)`` with consecutive identical cells merged.
    """
    ch0_parts, ch1_parts, r_parts, c_parts = [], [], [], []
    for i in range(len(px) - 1):
        if cancelled is not None and (i & 63) == 0 and cancelled():
            return None
        ca, cb = ch[i], ch[i + 1]
        ta, tb, rows, cols = traverse_segment(px[i], py[i], px[i + 1],
                                              py[i + 1], width, height)
        if ta.size == 0:
            continue
        dc = cb - ca
        ch0_parts.append(ca + ta * dc)
        ch1_parts.append(ca + tb * dc)
        r_parts.append(rows)
        c_parts.append(cols)
    if not ch0_parts:
        empty_f = np.empty(0)
        empty_i = np.empty(0, dtype=np.int32)
        return empty_f, empty_f, empty_i, empty_i
    return merge_runs(np.concatenate(ch0_parts), np.concatenate(ch1_parts),
                      np.concatenate(r_parts), np.concatenate(c_parts))


def station_indices(ca, cb, step=STATION_STEP):
    """Global station indices whose chainage lies in ``[ca, cb)``."""
    eps = 1e-9
    i0 = int(np.ceil(ca / step - eps))
    i1 = int(np.ceil(cb / step - eps))
    if i1 <= i0:
        return np.empty(0, dtype=np.int64)
    return np.arange(i0, i1, dtype=np.int64)


def stations_positions(px, py, ch, step=STATION_STEP):
    """Positions of regular stations along a piece.

    Returns ``(stations, sx, sy, end_x, end_y)`` where ``stations`` are the
    chainages in ``[ch[0], ch[-1])`` on the global ``step`` grid and
    ``end_x, end_y`` is the pixel position of the piece end.
    """
    idx = station_indices(ch[0], ch[-1], step)
    s = idx * step
    if s.size:
        # Guard against duplicate chainages from zero-length segments.
        keep = np.concatenate(([True], np.diff(ch) > 0))
        chk, pxk, pyk = ch[keep], px[keep], py[keep]
        sx = np.interp(s, chk, pxk)
        sy = np.interp(s, chk, pyk)
    else:
        sx = np.empty(0)
        sy = np.empty(0)
    return s, sx, sy, float(px[-1]), float(py[-1])


def nearest_cells(sx, sy, width, height):
    """Cell containing each pixel-space point (half-open convention)."""
    cols = np.floor(sx).astype(np.int64)
    rows = np.floor(sy).astype(np.int64)
    outside = (cols < 0) | (rows < 0) | (cols >= width) | (rows >= height)
    cols[outside] = OUTSIDE
    rows[outside] = OUTSIDE
    return rows, cols


def bilinear_setup(sx, sy, width, height):
    """Neighbour cells and weights for bilinear interpolation between cell
    centres.  Points outside the raster get ``inside = False``."""
    inside = (sx >= 0) & (sy >= 0) & (sx < width) & (sy < height)
    u = sx - 0.5
    v = sy - 0.5
    c0 = np.floor(u)
    r0 = np.floor(v)
    fx = u - c0
    fy = v - r0
    c0 = c0.astype(np.int64)
    r0 = r0.astype(np.int64)
    c1 = c0 + 1
    r1 = r0 + 1
    # Clamp to the grid so the outer half-cell extends the edge value.
    c0c = np.clip(c0, 0, width - 1)
    c1c = np.clip(c1, 0, width - 1)
    r0c = np.clip(r0, 0, height - 1)
    r1c = np.clip(r1, 0, height - 1)
    rows = np.stack((r0c, r0c, r1c, r1c))
    cols = np.stack((c0c, c1c, c0c, c1c))
    w = np.stack(((1 - fx) * (1 - fy), fx * (1 - fy), (1 - fx) * fy, fx * fy))
    return rows, cols, w, inside


def bilinear_combine(values4, weights, inside):
    """Combine neighbour values; any NaN neighbour with weight > 0 or a
    point outside the raster yields NaN."""
    used = weights > 0
    bad = np.any(np.isnan(values4) & used, axis=0) | ~inside
    out = np.nansum(np.where(used, values4 * weights, 0.0), axis=0)
    out[bad] = np.nan
    return out


# ---------------------------------------------------------------------------
# Assembly helpers used on the GUI thread
# ---------------------------------------------------------------------------

def endpoint_grade(start_value, end_value, length):
    """Signed net grade in percent, or ``None`` when unavailable."""
    if length is None or length <= 0:
        return None
    if start_value is None or end_value is None:
        return None
    if not (np.isfinite(start_value) and np.isfinite(end_value)):
        return None
    return 100.0 * (end_value - start_value) / length


def grade_ratio_text(grade):
    """'1 in 69 falling' style text for a grade in percent."""
    if grade is None:
        return ""
    if abs(grade) < 1e-12:
        return "flat"
    n = 100.0 / abs(grade)
    if n >= 100:
        ratio = "{:,.0f}".format(n)
    elif n >= 10:
        ratio = "{:.0f}".format(n)
    else:
        ratio = "{:.1f}".format(n).rstrip("0").rstrip(".")
    return "1 in {} {}".format(ratio, "falling" if grade < 0 else "rising")


def value_at(ch0, ch1, values, chainage):
    """Value of a step profile at ``chainage`` (NaN in gaps)."""
    if ch0.size == 0:
        return float("nan")
    i = int(np.searchsorted(ch0, chainage, side="right")) - 1
    if i < 0:
        return float("nan")
    if chainage > ch1[i] + 1e-12:
        return float("nan")
    return float(values[i])


def value_at_station(stations, values, chainage):
    """Value of a station profile at ``chainage`` (nearest station at or
    before ``chainage``, NaN outside)."""
    n = stations.size
    if n == 0:
        return float("nan")
    i = int(np.searchsorted(stations, chainage, side="right")) - 1
    if i < 0:
        return float("nan")
    if i < n - 1:
        # Choose the closer of the bracketing stations.
        if chainage - stations[i] > stations[i + 1] - chainage:
            i += 1
    elif chainage > stations[-1] + 1e-9:
        return float("nan")
    return float(values[i])


def union_boundaries(arrays, tol=1e-6):
    """Sorted union of interval boundaries with near-duplicates removed."""
    if not arrays:
        return np.empty(0)
    b = np.concatenate(arrays)
    if b.size == 0:
        return b
    b.sort(kind="mergesort")
    keep = np.concatenate(([True], np.diff(b) > tol))
    return b[keep]


def step_values_at(ch0, ch1, values, mids):
    """Vectorised :func:`value_at` for many chainages."""
    out = np.full(mids.shape, np.nan)
    if ch0.size == 0:
        return out
    idx = np.searchsorted(ch0, mids, side="right") - 1
    ok = idx >= 0
    idx_c = np.clip(idx, 0, ch0.size - 1)
    ok &= mids <= ch1[idx_c] + 1e-12
    out[ok] = values[idx_c[ok]]
    return out
