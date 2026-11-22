"""QPainter profile chart.

* The axes, grid and curves are rendered into a cached pixmap that is only
  rebuilt when data, view, size or theme change.  Pointer moves repaint
  just the overlay (crosshair, tooltip, readout) on top of it.
* Zoomed out, each continuous run keeps the first, minimum, maximum and
  last value per screen column in chainage order, so spikes and gaps
  survive; zoomed in, native cells are drawn as exact steps.
"""

import math

import numpy as np
from qgis.PyQt.QtCore import QEvent, QPointF, QRectF, QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QImage,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QPolygonF,
)
from qgis.PyQt.QtWidgets import QApplication, QFileDialog, QMenu, QSizePolicy, QWidget

from .compat import flag_int

HIT_TOLERANCE_PX = 5.0
# Tooltip background opacity (about 70 %).
TOOLTIP_ALPHA = 179
# Exported raster images are rendered at this multiple of screen resolution.
EXPORT_SCALE = 2.0
_CLAMP = 1e5


# ---------------------------------------------------------------------------
# numpy -> Qt helpers
# ---------------------------------------------------------------------------

def _qpolygon_fast(pts):
    n = len(pts)
    poly = QPolygonF()
    if hasattr(poly, "resize"):
        poly.resize(n)
    else:
        poly.fill(QPointF(), n)
    ptr = poly.data()
    ptr.setsize(16 * n)
    buf = np.frombuffer(ptr, dtype=np.float64, count=2 * n)
    buf[:] = pts.ravel()
    return poly


def _qpolygon_slow(pts):
    return QPolygonF([QPointF(float(x), float(y)) for x, y in pts])


_qpolygon_impl = None


def qpolygon(pts):
    """QPolygonF from an (N, 2) float64 array, without a Python loop when
    the binding allows writing into the polygon's buffer."""
    global _qpolygon_impl
    pts = np.ascontiguousarray(pts, dtype=np.float64)
    if _qpolygon_impl is None:
        try:
            probe = np.array([[1.5, 2.5], [3.5, 4.5]])
            p = _qpolygon_fast(probe)
            ok = (p.size() == 2 and p.at(1).x() == 3.5 and p.at(1).y() == 4.5)
            _qpolygon_impl = _qpolygon_fast if ok else _qpolygon_slow
        except Exception:
            _qpolygon_impl = _qpolygon_slow
    return _qpolygon_impl(pts)


def nice_step(span, target):
    if span <= 0 or not math.isfinite(span):
        return 1.0
    raw = span / max(target, 1)
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def ticks(lo, hi, target):
    step = nice_step(hi - lo, target)
    start = math.ceil(lo / step - 1e-9) * step
    out = []
    v = start
    while v <= hi + step * 1e-9 and len(out) < 200:
        out.append(0.0 if abs(v) < step * 1e-9 else v)
        v += step
    return out, step


# Minor grid: preferred spacing (1 m) and the smallest gap between minor
# lines on screen, in pixels.
MINOR_PREFERRED = 1.0
MINOR_MIN_PX = 7.0


def minor_step(major, px_per_unit, preferred=MINOR_PREFERRED, min_px=MINOR_MIN_PX):
    """Spacing of the light minor grid between labelled (major) ticks.

    Every ``preferred`` units (1 m) when that divides the major step and is
    at least ``min_px`` apart on screen; otherwise the finest of major/10,
    /5, /4, /2 that is legible.  ``None`` when even major/2 is too dense.
    """
    if major <= 0 or px_per_unit <= 0:
        return None
    candidates = []
    n = major / preferred
    if n > 1.5 and abs(n - round(n)) < 1e-6:
        candidates.append(preferred)
    candidates += [major / d for d in (10, 5, 4, 2)]
    for c in candidates:
        if c * px_per_unit >= min_px:
            return c
    return None


def minor_ticks(lo, hi, major, minor):
    """Minor tick positions in [lo, hi] that are not on a major tick."""
    if minor is None:
        return []
    out = []
    k = math.ceil(lo / minor - 1e-9)
    v = k * minor
    count = 0
    while v <= hi + minor * 1e-9 and count < 2000:
        r = v / major
        if abs(r - round(r)) > 1e-6:
            out.append(v)
        k += 1
        v = k * minor
        count += 1
    return out


def tick_decimals(step):
    text = "{:.10f}".format(step).rstrip("0")
    return len(text.split(".")[1]) if "." in text else 0


def contrast_text(color):
    lum = 0.299 * color.redF() + 0.587 * color.greenF() + 0.114 * color.blueF()
    return QColor("#000000") if lum > 0.6 else QColor("#ffffff")


# ---------------------------------------------------------------------------
# polyline construction
# ---------------------------------------------------------------------------

def _runs(finite):
    f = np.concatenate(([0], finite.astype(np.int8), [0]))
    d = np.diff(f)
    return np.flatnonzero(d == 1), np.flatnonzero(d == -1)


def _decimate(xpix, vals, run_ids, width):
    """Keep the first, minimum, maximum and last point of every (run,
    screen column), in chainage order and at their true chainage.

    Drawing the kept points gives the same picture as drawing every point,
    so long slopes stay smooth and narrow spikes are not lost.
    Returns (points (4G, 2) as [x_pixel, value], group_run_ids)."""
    col = np.clip(np.floor(xpix), -1, width + 1).astype(np.int64)
    key = run_ids * (width + 4) + (col + 1)
    gs = np.concatenate(([0], np.flatnonzero(key[1:] != key[:-1]) + 1))
    ge = np.concatenate((gs[1:], [key.size]))
    mn = np.minimum.reduceat(vals, gs)
    mx = np.maximum.reduceat(vals, gs)
    counts = ge - gs
    idx = np.arange(vals.size)
    big = vals.size + 1
    i_min = np.minimum.reduceat(np.where(vals == np.repeat(mn, counts), idx, big), gs)
    i_max = np.minimum.reduceat(np.where(vals == np.repeat(mx, counts), idx, big), gs)
    i_a = np.minimum(i_min, i_max)
    i_b = np.maximum(i_min, i_max)
    order = np.stack((gs, i_a, i_b, ge - 1), axis=1).reshape(-1)
    pts = np.empty((order.size, 2))
    pts[:, 0] = xpix[order]
    pts[:, 1] = vals[order]
    return pts, run_ids[gs]


def series_polylines(profile, xmin, xmax, ymin, ymax, rect, style="smooth"):
    """Pixel-space polylines (list of (k, 2) arrays) for one profile.

    Native cells are drawn as a smooth line through the cell values
    (``style="smooth"``) or as horizontal steps (``style="steps"``)."""
    left, top, w, h = rect.left(), rect.top(), rect.width(), rect.height()
    if w <= 1 or h <= 1 or xmax <= xmin or ymax <= ymin:
        return []
    sx = w / (xmax - xmin)
    sy = h / (ymax - ymin)
    width = int(math.ceil(w))
    if profile.kind == "steps":
        x0, x1, v = profile.x0, profile.x1, profile.v
        i0 = int(np.searchsorted(x1, xmin, side="right"))
        i1 = int(np.searchsorted(x0, xmax, side="left"))
        if i1 <= i0:
            return []
        x0 = x0[i0:i1]
        x1 = x1[i0:i1]
        v = v[i0:i1]
        finite = np.isfinite(v)
        # A discontinuity in chainage also breaks the run.
        if x0.size > 1:
            jump = np.concatenate(([False], np.abs(x0[1:] - x1[:-1]) > 1e-9))
            starts_extra = jump & finite & np.concatenate(([False], finite[:-1]))
        else:
            starts_extra = None
        n_finite = int(finite.sum())
        if n_finite == 0:
            return []
        if n_finite <= 2 * width:
            X0 = np.clip(left + (x0 - xmin) * sx, left - _CLAMP, left + w + _CLAMP)
            X1 = np.clip(left + (x1 - xmin) * sx, left - _CLAMP, left + w + _CLAMP)
            Y = np.clip(top + (ymax - v) * sy, top - _CLAMP, top + h + _CLAMP)
            if style == "steps":
                pts = np.empty((x0.size * 2, 2))
                pts[0::2, 0] = X0
                pts[1::2, 0] = X1
                pts[0::2, 1] = Y
                pts[1::2, 1] = Y
            else:
                XM = 0.5 * (X0 + X1)
            rs, re = _runs(finite)
            out = []
            for s, e in zip(rs, re):
                if starts_extra is not None:
                    cuts = np.flatnonzero(starts_extra[s + 1:e]) + s + 1
                    bounds = [s] + cuts.tolist() + [e]
                else:
                    bounds = [s, e]
                for bs, be in zip(bounds[:-1], bounds[1:]):
                    if style == "steps":
                        out.append(pts[2 * bs:2 * be])
                    else:
                        # through the cell centres, flat over the first and
                        # last half-cell so the line spans the whole run
                        n = be - bs
                        seg = np.empty((n + 2, 2))
                        seg[0] = (X0[bs], Y[bs])
                        seg[1:-1, 0] = XM[bs:be]
                        seg[1:-1, 1] = Y[bs:be]
                        seg[-1] = (X1[be - 1], Y[be - 1])
                        out.append(seg)
            return out
        xm = 0.5 * (x0 + x1)
        xpix = (xm - xmin) * sx
    else:
        x, v = profile.x0, profile.v
        i0 = max(0, int(np.searchsorted(x, xmin, side="left")) - 1)
        i1 = min(x.size, int(np.searchsorted(x, xmax, side="right")) + 1)
        if i1 <= i0:
            return []
        x = x[i0:i1]
        v = v[i0:i1]
        finite = np.isfinite(v)
        starts_extra = None
        n_finite = int(finite.sum())
        if n_finite == 0:
            return []
        if n_finite <= 2 * width:
            X = np.clip(left + (x - xmin) * sx, left - _CLAMP, left + w + _CLAMP)
            Y = np.clip(top + (ymax - v) * sy, top - _CLAMP, top + h + _CLAMP)
            pts = np.column_stack((X, Y))
            rs, re = _runs(finite)
            return [pts[s:e] for s, e in zip(rs, re)]
        xpix = (x - xmin) * sx

    # Decimated path
    run_start = finite & ~np.concatenate(([False], finite[:-1]))
    if starts_extra is not None:
        run_start |= starts_extra
    run_ids = np.cumsum(run_start)[finite]
    pts, gr = _decimate(xpix[finite], v[finite], run_ids, width)
    pts[:, 0] = np.clip(pts[:, 0] + left, left - _CLAMP, left + w + _CLAMP)
    pts[:, 1] = np.clip(top + (ymax - pts[:, 1]) * sy, top - _CLAMP, top + h + _CLAMP)
    cut = np.flatnonzero(gr[1:] != gr[:-1]) + 1
    bounds = np.concatenate(([0], cut, [gr.size]))
    return [pts[4 * s:4 * e] for s, e in zip(bounds[:-1], bounds[1:])]


def _hit_distance(polylines, cx, cy, tol):
    """Smallest distance from (cx, cy) to any polyline segment near cx."""
    best = math.inf
    for pts in polylines:
        if pts.shape[0] == 0:
            continue
        if pts[-1, 0] < cx - tol or pts[0, 0] > cx + tol:
            continue
        xs = pts[:, 0]
        i0 = max(0, int(np.searchsorted(xs, cx - tol, side="left")) - 1)
        i1 = min(pts.shape[0], int(np.searchsorted(xs, cx + tol, side="right")) + 1)
        seg = pts[i0:i1]
        if seg.shape[0] == 1:
            d = math.hypot(seg[0, 0] - cx, seg[0, 1] - cy)
            best = min(best, d)
            continue
        a = seg[:-1]
        b = seg[1:]
        ab = b - a
        ap = np.array([cx, cy]) - a
        denom = (ab ** 2).sum(axis=1)
        t = np.where(denom > 0, (ap * ab).sum(axis=1) / np.where(denom > 0, denom, 1), 0)
        t = np.clip(t, 0, 1)
        proj = a + ab * t[:, None]
        d = np.hypot(proj[:, 0] - cx, proj[:, 1] - cy).min()
        best = min(best, float(d))
    return best


# ---------------------------------------------------------------------------
# widget
# ---------------------------------------------------------------------------

class SeriesView:
    __slots__ = ("uid", "name", "color", "profile")

    def __init__(self, uid, name, color, profile):
        self.uid = uid
        self.name = name
        self.color = QColor(color)
        self.profile = profile


class ProfilePlotWidget(QWidget):
    """Elevation vs chainage chart."""

    cursorChainage = pyqtSignal(object)   # float or None
    viewChanged = pyqtSignal()

    MODE_HOVER = "hover"
    MODE_CROSSHAIR = "crosshair"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(160, 120)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.series = []
        self.length = 0.0
        self.mode = self.MODE_CROSSHAIR
        self.show_units = False
        self.unit = "m"
        self.placeholder = ""
        self.status_text = ""
        self.show_legend = False
        self.line_style = "smooth"
        self.x_title = "Chainage"
        self.y_title = "Elevation"
        self.auto_view = True
        self.view = (0.0, 1.0, 0.0, 1.0)
        self._cache = None
        self._dirty = True
        self._plot_rect = QRectF()
        self._polylines = {}
        self._cursor = None
        self._drag_origin = None
        self._drag_view = None
        self.render_count = 0
        self._ystep = 1.0

    # -- configuration ------------------------------------------------------

    def sizeHint(self):
        return QSize(520, 260)

    def set_series(self, series, length, status_text=""):
        self.series = series
        self.length = float(length)
        self.status_text = status_text
        if self.auto_view:
            self._fit()
        self._invalidate()

    def set_mode(self, mode):
        self.mode = mode
        self.update()

    def set_units(self, show, unit):
        self.show_units = bool(show)
        self.unit = unit or "m"
        self._invalidate()

    def set_line_style(self, style):
        style = "steps" if style == "steps" else "smooth"
        if style != self.line_style:
            self.line_style = style
            self._invalidate()

    def set_axis_titles(self, x_title, y_title):
        x_title = (x_title or "").strip() or "Chainage"
        y_title = (y_title or "").strip() or "Elevation"
        if (x_title, y_title) != (self.x_title, self.y_title):
            self.x_title, self.y_title = x_title, y_title
            self._invalidate()

    def set_show_legend(self, show):
        self.show_legend = bool(show)
        self.update()

    def set_placeholder(self, text):
        if text != self.placeholder:
            self.placeholder = text
            self._invalidate()

    def unit_suffix(self, chainage=False):
        if not self.show_units:
            return ""
        return " m" if chainage else " " + self.unit

    def zoom_full(self):
        self.auto_view = True
        self._fit()
        self._invalidate()
        self.viewChanged.emit()

    def reset_view(self):
        self.auto_view = True

    def _data_y_range(self):
        lo, hi = math.inf, -math.inf
        for s in self.series:
            p = s.profile
            if p is None or p.vmin is None:
                continue
            lo = min(lo, p.vmin)
            hi = max(hi, p.vmax)
        if not math.isfinite(lo):
            return None
        return lo, hi

    def _fit(self):
        xr = self.length if self.length > 0 else 1.0
        yr = self._data_y_range()
        if yr is None:
            ymin, ymax = 0.0, 1.0
        else:
            ymin, ymax = yr
            span = ymax - ymin
            if span < 1e-6:
                pad = max(0.5, abs(ymax) * 0.001)
            else:
                pad = span * 0.06
            ymin -= pad
            ymax += pad
        self.view = (0.0, xr, ymin, ymax)

    def _invalidate(self):
        self._dirty = True
        self.update()

    # -- geometry -------------------------------------------------------------

    def _layout(self, width=None, height=None):
        width = self.width() if width is None else width
        height = self.height() if height is None else height
        fm = QFontMetricsF(self.font())
        xmin, xmax, ymin, ymax = self.view
        h_est = max(60.0, height - 3.2 * fm.height() - 12)
        yt, ystep = ticks(ymin, ymax, max(3, int(h_est / (fm.height() * 1.5))))
        ydec = tick_decimals(ystep)
        ylabels = ["{:.{}f}".format(v, ydec) for v in yt]
        label_w = max([fm.horizontalAdvance(t) for t in ylabels] + [fm.horizontalAdvance("0")])
        left = 6 + fm.height() + 6 + label_w + 6
        bottom = fm.height() * 2 + 12
        top = 10
        right = 14
        rect = QRectF(left, top, max(10.0, width - left - right),
                      max(10.0, height - top - bottom))
        self._ystep = ystep
        return rect, yt, ylabels, fm

    def _to_px(self, x, y):
        r = self._plot_rect
        xmin, xmax, ymin, ymax = self.view
        return (r.left() + (x - xmin) / (xmax - xmin) * r.width(),
                r.top() + (ymax - y) / (ymax - ymin) * r.height())

    def _to_data(self, px, py):
        r = self._plot_rect
        xmin, xmax, ymin, ymax = self.view
        return (xmin + (px - r.left()) / r.width() * (xmax - xmin),
                ymax - (py - r.top()) / r.height() * (ymax - ymin))

    # -- colours ----------------------------------------------------------------

    def _colors(self):
        pal = self.palette()
        bg = pal.color(QPalette.ColorRole.Base)
        fg = pal.color(QPalette.ColorRole.Text)

        def mix(a, b, t):
            return QColor(int(a.red() * (1 - t) + b.red() * t),
                          int(a.green() * (1 - t) + b.green() * t),
                          int(a.blue() * (1 - t) + b.blue() * t))
        return {
            "bg": bg,
            "fg": fg,
            "grid": mix(bg, fg, 0.14),
            "grid_minor": mix(bg, fg, 0.06),
            "axis": mix(bg, fg, 0.55),
            "muted": mix(bg, fg, 0.6),
            "window": pal.color(QPalette.ColorRole.Window),
        }

    # -- static rendering -----------------------------------------------------------

    def _render_static(self):
        dpr = self.devicePixelRatioF()
        size = self.size()
        pm = QPixmap(max(1, int(size.width() * dpr)), max(1, int(size.height() * dpr)))
        pm.setDevicePixelRatio(dpr)
        pm.fill(self._colors()["bg"])
        p = QPainter(pm)
        try:
            self._paint_static(p, size.width(), size.height())
        finally:
            p.end()
        self._cache = pm
        self._dirty = False
        self.render_count += 1

    def _paint_static(self, p, width, height, export=False):
        """Axes, grid, curves and labels.  With ``export`` the legend is
        included (when shown) and the interactive state is left alone."""
        colors = self._colors()
        rect, yt, ylabels, fm = self._layout(width, height)
        self._plot_rect = rect
        xmin, xmax, ymin, ymax = self.view
        p.setFont(self.font())
        # minor grid (light, every 1 m where legible)
        xt, xstep = ticks(xmin, xmax, max(2, int(rect.width() / 80)))
        ystep = self._ystep
        minor_pen = QPen(colors["grid_minor"])
        minor_pen.setWidthF(1.0)
        p.setPen(minor_pen)
        mx = minor_step(xstep, rect.width() / (xmax - xmin))
        for v in minor_ticks(xmin, xmax, xstep, mx):
            x, _ = self._to_px(v, ymin)
            p.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
        my = minor_step(ystep, rect.height() / (ymax - ymin))
        for v in minor_ticks(ymin, ymax, ystep, my):
            _, y = self._to_px(xmin, v)
            p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
        # major grid + y ticks
        grid_pen = QPen(colors["grid"])
        grid_pen.setWidthF(1.0)
        text_pen = QPen(colors["fg"])
        for v, lab in zip(yt, ylabels):
            _, y = self._to_px(xmin, v)
            p.setPen(grid_pen)
            p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            p.setPen(text_pen)
            tr = QRectF(0, y - fm.height() / 2, rect.left() - 6, fm.height())
            p.drawText(tr, flag_int(Qt.AlignmentFlag.AlignRight, Qt.AlignmentFlag.AlignVCenter), lab)
        xdec = tick_decimals(xstep)
        for v in xt:
            x, _ = self._to_px(v, ymin)
            p.setPen(grid_pen)
            p.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            p.setPen(text_pen)
            lab = "{:.{}f}".format(v, xdec)
            tw = fm.horizontalAdvance(lab)
            # keep end labels (e.g. 10000) inside the widget
            tx = min(max(x - tw / 2, 2.0), width - tw - 2.0)
            p.drawText(QPointF(tx, rect.bottom() + fm.ascent() + 4), lab)
        # axes frame
        p.setPen(QPen(colors["axis"]))
        p.drawRect(rect)
        # titles
        p.setPen(text_pen)
        xtitle = self.x_title + (" (m)" if self.show_units else "")
        ytitle = self.y_title + (" ({})".format(self.unit) if self.show_units else "")
        tw = fm.horizontalAdvance(xtitle)
        p.drawText(QPointF(rect.center().x() - tw / 2,
                           rect.bottom() + fm.height() + fm.ascent() + 6), xtitle)
        p.save()
        p.translate(4 + fm.ascent(), rect.center().y())
        p.rotate(-90)
        tw = fm.horizontalAdvance(ytitle)
        p.drawText(QPointF(-tw / 2, 0), ytitle)
        p.restore()

        # curves
        p.setClipRect(rect.adjusted(-1, -1, 1, 1))
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self._polylines = {}
        any_data = False
        for s in self.series:
            prof = s.profile
            if prof is None:
                continue
            lines = series_polylines(prof, xmin, xmax, ymin, ymax, rect, self.line_style)
            self._polylines[s.uid] = lines
            pen = QPen(s.color)
            pen.setWidthF(1.6)
            if self.line_style == "steps":
                pen.setCapStyle(Qt.PenCapStyle.FlatCap)
                pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
            else:
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            if prof.interpolated:
                pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            for pts in lines:
                if pts.shape[0] == 1:
                    p.drawPoint(QPointF(pts[0, 0], pts[0, 1]))
                else:
                    p.drawPolyline(qpolygon(pts))
                any_data = True
        p.setClipping(False)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        if not any_data and self.placeholder:
            p.setPen(QPen(colors["muted"]))
            p.drawText(rect, flag_int(Qt.AlignmentFlag.AlignCenter, Qt.TextFlag.TextWordWrap),
                       self.placeholder)
        if self.status_text and not export:
            f = QFont(self.font())
            f.setItalic(True)
            p.setFont(f)
            p.setPen(QPen(colors["muted"]))
            p.drawText(QPointF(rect.left() + 6, rect.top() + fm.ascent() + 4),
                       self.status_text)
            p.setFont(self.font())
        if export and self.show_legend:
            visible = [s for s in self.series if s.profile is not None]
            if visible:
                self._paint_readout(p, visible, None, fm, colors)

    # -- painting ---------------------------------------------------------------------

    def paintEvent(self, event):
        if self._dirty or self._cache is None or self._cache.size() != QSize(
                int(self.width() * self.devicePixelRatioF()),
                int(self.height() * self.devicePixelRatioF())):
            self._render_static()
        p = QPainter(self)
        try:
            p.drawPixmap(0, 0, self._cache)
            self._paint_overlay(p)
        finally:
            p.end()

    def _format(self, v, chainage=False):
        if v is None or not math.isfinite(v):
            return "NoData"
        return "{:.2f}{}".format(v, self.unit_suffix(chainage))

    def _paint_overlay(self, p):
        cur = self._cursor
        rect = self._plot_rect
        colors = self._colors()
        fm = QFontMetricsF(self.font())
        visible = [s for s in self.series if s.profile is not None]
        ch = None
        if cur is not None and rect.contains(cur) and self.length > 0:
            ch, ye = self._to_data(cur.x(), cur.y())
            if self.mode == self.MODE_CROSSHAIR:
                pen = QPen(colors["axis"])
                pen.setStyle(Qt.PenStyle.DashLine)
                p.setPen(pen)
                p.drawLine(QPointF(cur.x(), rect.top()), QPointF(cur.x(), rect.bottom()))
                p.drawLine(QPointF(rect.left(), cur.y()), QPointF(rect.right(), cur.y()))
                bg = QColor(255, 255, 255, TOOLTIP_ALPHA)
                self._tooltip(p, cur, self.crosshair_text(ch, ye), bg, QColor("#000000"),
                              QColor(0, 0, 0, 70))
            else:
                hit = self._hit_test(cur.x(), cur.y())
                if hit is not None:
                    s, value = hit
                    bg = QColor(s.color)
                    bg.setAlpha(TOOLTIP_ALPHA)
                    self._tooltip(p, cur, self.hover_text(ch, value), bg, contrast_text(s.color))
        if visible and self.show_legend:
            self._paint_readout(p, visible, ch, fm, colors)

    def crosshair_text(self, chainage, elevation):
        """Crosshair tooltip, e.g. ``x[50.00], y[78.50]``."""
        return "x[{}], y[{}]".format(self._format(chainage, True), self._format(elevation))

    def hover_text(self, chainage, value):
        """Hover tooltip, e.g. ``[50.00], [78.50]``."""
        return "[{}], [{}]".format(self._format(chainage, True), self._format(value))

    def _tooltip(self, p, cur, text, bg, fg, border=None):
        fm = QFontMetricsF(self.font())
        w = fm.horizontalAdvance(text) + 12
        h = fm.height() + 6
        x = cur.x() + 14
        y = cur.y() + 14
        r = self._plot_rect
        if x + w > r.right():
            x = cur.x() - 14 - w
        if y + h > r.bottom():
            y = cur.y() - 14 - h
        box = QRectF(x, y, w, h)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if border is not None:
            p.setPen(QPen(border, 1.0))
        else:
            p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(box, 3, 3)
        p.setPen(QPen(fg))
        p.drawText(box, flag_int(Qt.AlignmentFlag.AlignCenter), text)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)

    def _paint_readout(self, p, visible, ch, fm, colors):
        """Legend box: one row per raster with its value at the pointer's
        chainage (the chainage itself is in the hover/crosshair tooltip)."""
        rows = []
        for s in visible:
            val = s.profile.value_at(ch) if ch is not None else None
            label = s.name + (" (bilinear)" if s.profile.interpolated else "")
            rows.append((s, label, self._format(val) if ch is not None else ""))
        name_w = max(fm.horizontalAdvance(r[1]) for r in rows)
        val_w = max([fm.horizontalAdvance(r[2]) for r in rows] + [0])
        swatch = 18
        w = 8 + swatch + 6 + name_w + (14 + val_w if val_w else 0) + 8
        line_h = fm.height() + 2
        h = 6 + line_h * len(rows) + 4
        r = self._plot_rect
        w = min(w, r.width() - 8)
        box = QRectF(r.right() - w - 6, r.top() + 6, w, h)
        bg = QColor(colors["bg"])
        bg.setAlpha(225)
        p.setPen(QPen(colors["grid"]))
        p.setBrush(bg)
        p.drawRect(box)
        y = box.top() + 5
        for s, label, val in rows:
            mid = y + line_h / 2
            pen = QPen(s.color)
            pen.setWidthF(2.0)
            if s.profile.interpolated:
                pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawLine(QPointF(box.left() + 8, mid), QPointF(box.left() + 8 + swatch, mid))
            p.setPen(QPen(colors["fg"]))
            name_rect = QRectF(box.left() + 8 + swatch + 6, y,
                               box.width() - swatch - 22 - (val_w + 14 if val_w else 0), line_h)
            p.drawText(name_rect, flag_int(Qt.AlignmentFlag.AlignLeft, Qt.AlignmentFlag.AlignVCenter),
                       fm.elidedText(label, Qt.TextElideMode.ElideRight, name_rect.width()))
            if val:
                p.drawText(QRectF(box.right() - 8 - val_w, y, val_w, line_h),
                           flag_int(Qt.AlignmentFlag.AlignRight, Qt.AlignmentFlag.AlignVCenter), val)
            y += line_h

    def _hit_test(self, cx, cy):
        best = None
        best_d = HIT_TOLERANCE_PX
        for s in reversed(self.series):  # topmost first
            lines = self._polylines.get(s.uid)
            if not lines:
                continue
            d = _hit_distance(lines, cx, cy, HIT_TOLERANCE_PX)
            if d <= best_d:
                best_d = d
                best = s
        if best is None:
            return None
        ch, _ = self._to_data(cx, cy)
        value = best.profile.value_at(ch)
        if not math.isfinite(value):
            # Pointer is on a riser: report the nearest side.
            for dx in (-1.0, 1.0):
                c2, _ = self._to_data(cx + dx, cy)
                value = best.profile.value_at(c2)
                if math.isfinite(value):
                    break
        if not math.isfinite(value):
            return None
        return best, value

    # -- interaction ----------------------------------------------------------------------

    def _event_pos(self, event):
        """Pointer position with sub-pixel precision (Qt6 position(),
        Qt5 localPos() for mouse events)."""
        if hasattr(event, "position"):
            return QPointF(event.position())
        if hasattr(event, "localPos"):
            return QPointF(event.localPos())
        if hasattr(event, "posF"):  # Qt5 wheel events
            return QPointF(event.posF())
        return QPointF(event.pos())

    def mouseMoveEvent(self, event):
        pos = self._event_pos(event)
        if self._drag_origin is not None:
            dx = pos.x() - self._drag_origin.x()
            dy = pos.y() - self._drag_origin.y()
            xmin, xmax, ymin, ymax = self._drag_view
            r = self._plot_rect
            ux = (xmax - xmin) / max(r.width(), 1)
            uy = (ymax - ymin) / max(r.height(), 1)
            self.view = (xmin - dx * ux, xmax - dx * ux, ymin + dy * uy, ymax + dy * uy)
            self.auto_view = False
            self._invalidate()
        self._cursor = pos
        self.update()
        if self._plot_rect.contains(pos) and self.length > 0:
            ch, _ = self._to_data(pos.x(), pos.y())
            self.cursorChainage.emit(min(max(ch, 0.0), self.length))
        else:
            self.cursorChainage.emit(None)

    def leaveEvent(self, event):
        self._cursor = None
        self.update()
        self.cursorChainage.emit(None)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_origin = self._event_pos(event)
            self._drag_view = self.view
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._drag_origin is not None:
            self._drag_origin = None
            self.unsetCursor()
            self.viewChanged.emit()
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.zoom_full()

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta == 0:
            return
        pos = self._event_pos(event)
        cx, cy = self._to_data(pos.x(), pos.y())
        f = 0.8 ** (delta / 120.0)
        mods = event.modifiers()
        zoom_x = not (mods & Qt.KeyboardModifier.ShiftModifier)
        zoom_y = not (mods & Qt.KeyboardModifier.ControlModifier)
        xmin, xmax, ymin, ymax = self.view
        if zoom_x:
            xmin = cx - (cx - xmin) * f
            xmax = cx + (xmax - cx) * f
        if zoom_y:
            ymin = cy - (cy - ymin) * f
            ymax = cy + (ymax - cy) * f
        if xmax - xmin < 1e-6 or ymax - ymin < 1e-6:
            return
        self.view = (xmin, xmax, ymin, ymax)
        self.auto_view = False
        self._invalidate()
        self.viewChanged.emit()
        event.accept()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Home, Qt.Key.Key_0):
            self.zoom_full()
            return
        super().keyPressEvent(event)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.StyleChange,
                            QEvent.Type.FontChange):
            self._invalidate()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._dirty = True

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.addAction("Zoom full", self.zoom_full)
        menu.addSeparator()
        self.add_export_actions(menu)
        menu.exec(event.globalPos())

    # -- export -----------------------------------------------------------------------

    def add_export_actions(self, menu):
        """Image export entries, shared by the dock's export button and the
        plot's context menu."""
        from . import icons
        menu.addAction(icons.icon("image"), "Save as PNG…", lambda: self.save_image("png"))
        menu.addAction(icons.icon("image"), "Save as JPEG…", lambda: self.save_image("jpg"))
        menu.addAction(icons.icon("svg"), "Save as SVG (vector)…", lambda: self.save_image("svg"))
        menu.addSeparator()
        menu.addAction(icons.icon("copy"), "Copy to clipboard", self.copy_image)

    def render_image(self, scale=EXPORT_SCALE, opaque=False):
        """The plot (without pointer overlay) as a QImage, ``scale`` times
        the on-screen resolution."""
        w, h = self.width(), self.height()
        fmt = QImage.Format.Format_RGB32 if opaque else QImage.Format.Format_ARGB32_Premultiplied
        img = QImage(max(1, int(w * scale)), max(1, int(h * scale)), fmt)
        img.setDevicePixelRatio(scale)
        img.fill(self._colors()["bg"])
        p = QPainter(img)
        try:
            self._paint_static(p, w, h, export=True)
        finally:
            p.end()
        self._dirty = True  # the on-screen cache keeps its own polylines
        return img

    def save_svg(self, path):
        from qgis.PyQt.QtSvg import QSvgGenerator
        gen = QSvgGenerator()
        gen.setFileName(path)
        gen.setSize(self.size())
        gen.setViewBox(QRectF(0, 0, self.width(), self.height()))
        gen.setTitle("Profile Plot")
        p = QPainter(gen)
        try:
            p.fillRect(QRectF(0, 0, self.width(), self.height()), self._colors()["bg"])
            self._paint_static(p, self.width(), self.height(), export=True)
        finally:
            p.end()
        self._dirty = True
        self.update()
        return True

    def export_to(self, path, fmt):
        fmt = fmt.lower()
        if fmt == "svg":
            return self.save_svg(path)
        if fmt in ("jpg", "jpeg"):
            return self.render_image(opaque=True).save(path, "JPG", 95)
        return self.render_image().save(path, "PNG")

    def save_image(self, fmt="png"):
        filters = {"png": "PNG image (*.png)", "jpg": "JPEG image (*.jpg *.jpeg)",
                   "svg": "SVG vector image (*.svg)"}
        path, _ = QFileDialog.getSaveFileName(self, "Export plot image", "profile." + fmt,
                                              filters[fmt])
        if not path:
            return False
        exts = {"png": (".png",), "jpg": (".jpg", ".jpeg"), "svg": (".svg",)}[fmt]
        if not path.lower().endswith(exts):
            path += exts[0]
        return self.export_to(path, fmt)

    def copy_image(self):
        (QGuiApplication.clipboard() or QApplication.clipboard()).setImage(self.render_image())
