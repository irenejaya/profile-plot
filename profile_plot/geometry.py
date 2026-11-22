"""CRS handling, chainage and pixel-space pieces (GUI thread)."""

import math

import numpy as np
from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCsException,
    QgsPointXY,
    QgsProject,
)

# Transform curvature tolerance when densifying, in pixels.
DENSIFY_TOLERANCE_PX = 0.05
_MAX_DEPTH = 16
# Segments longer than this (measurement units) are always split once when
# the raster CRS differs, so a symmetric distortion cannot hide.
_FORCE_SPLIT_LENGTH = 2000.0

_BAD_PROJECTIONS = {"merc", "webmerc", "longlat", "latlong", "lonlat", "latlon"}


def _metres_unit():
    try:
        return Qgis.DistanceUnit.Meters
    except AttributeError:  # QGIS < 3.30
        from qgis.core import QgsUnitTypes
        return QgsUnitTypes.DistanceMeters


def is_good_measure_crs(crs):
    """A projected CRS in metres that is not a world Mercator."""
    if crs is None or not crs.isValid() or crs.isGeographic():
        return False
    if crs.mapUnits() != _metres_unit():
        return False
    return crs.projectionAcronym().lower() not in _BAD_PROJECTIONS


def transform_context():
    return QgsProject.instance().transformContext()


def make_transform(src, dst):
    """``None`` when no transform is needed."""
    if src is None or dst is None or src == dst:
        return None
    return QgsCoordinateTransform(src, dst, transform_context())


def choose_measure_crs(line_crs, first_point):
    """Horizontal distances are measured in a local projected CRS in metres:
    the line's own CRS when suitable, otherwise a transverse Mercator
    centred on the first vertex."""
    if is_good_measure_crs(line_crs):
        return line_crs
    wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    lon, lat = first_point.x(), first_point.y()
    if line_crs is not None and line_crs.isValid() and line_crs != wgs84:
        try:
            p = QgsCoordinateTransform(line_crs, wgs84,
                                       transform_context()).transform(first_point)
            lon, lat = p.x(), p.y()
        except QgsCsException:
            pass
    lat = max(-89.0, min(89.0, lat))
    proj = ("+proj=tmerc +lat_0={:.8f} +lon_0={:.8f} +k=1 +x_0=0 +y_0=0 "
            "+datum=WGS84 +units=m +no_defs").format(lat, lon)
    crs = QgsCoordinateReferenceSystem.fromProj(proj)
    return crs if crs.isValid() else line_crs


def crs_key(crs):
    if crs is None:
        return ""
    return crs.authid() or crs.toProj() or crs.toWkt()


def to_xy(xf, points):
    """Transform a list of QgsPointXY into an (N, 2) array."""
    out = np.empty((len(points), 2))
    for i, p in enumerate(points):
        if xf is not None:
            p = xf.transform(p)
        out[i, 0] = p.x()
        out[i, 1] = p.y()
    return out


def cumulative_chainage(xy):
    if len(xy) == 0:
        return np.empty(0)
    d = np.hypot(np.diff(xy[:, 0]), np.diff(xy[:, 1]))
    return np.concatenate(([0.0], np.cumsum(d)))


def _outside_piece(ca, cb):
    return (np.array([-10.0, -5.0]), np.array([-10.0, -5.0]), np.array([ca, cb]))


def build_piece(spec, xf, a, b, ca, cb):
    """Pixel-space piece for the segment ``a -> b`` (measurement CRS).

    ``xf`` transforms measurement CRS -> raster CRS (``None`` = same CRS).
    """
    if xf is None:
        px, py = spec.world_to_pixel(np.array([a[0], b[0]]), np.array([a[1], b[1]]))
        return px, py, np.array([ca, cb], dtype=float)

    def to_pix(x, y):
        p = xf.transform(QgsPointXY(x, y))
        q = spec.world_to_pixel(p.x(), p.y())
        return float(q[0]), float(q[1])

    try:
        pa = to_pix(a[0], a[1])
        pb = to_pix(b[0], b[1])
        out_x = [pa[0]]
        out_y = [pa[1]]
        out_c = [ca]
        seg_len = math.hypot(b[0] - a[0], b[1] - a[1])
        # Iterative depth-first subdivision keeping order.
        stack = [(a, b, ca, cb, pa, pb, 0)]
        while stack:
            sa, sb, sca, scb, spa, spb, depth = stack.pop()
            ma = ((sa[0] + sb[0]) * 0.5, (sa[1] + sb[1]) * 0.5)
            pm = to_pix(ma[0], ma[1])
            dev = math.hypot(pm[0] - (spa[0] + spb[0]) * 0.5,
                             pm[1] - (spa[1] + spb[1]) * 0.5)
            length = seg_len / (2 ** depth)
            if depth < _MAX_DEPTH and (dev > DENSIFY_TOLERANCE_PX or
                                       length > _FORCE_SPLIT_LENGTH):
                cm = (sca + scb) * 0.5
                # push second half first so the first half is processed first
                stack.append((ma, sb, cm, scb, pm, spb, depth + 1))
                stack.append((sa, ma, sca, cm, spa, pm, depth + 1))
            else:
                out_x.append(spb[0])
                out_y.append(spb[1])
                out_c.append(scb)
        return np.array(out_x), np.array(out_y), np.array(out_c)
    except QgsCsException:
        return _outside_piece(ca, cb)


class ChainageMapper:
    """Maps chainage back to map (canvas) coordinates for the cursor
    marker.  Built once per line change."""

    def __init__(self, xy_m, ch, measure_crs, canvas_crs):
        self.ch = ch
        self.xy = xy_m
        self.xf = make_transform(measure_crs, canvas_crs)

    def point_at(self, chainage):
        if self.ch.size == 0:
            return None
        if self.ch.size == 1:
            x, y = self.xy[0]
        else:
            c = min(max(chainage, 0.0), float(self.ch[-1]))
            x = float(np.interp(c, self.ch, self.xy[:, 0]))
            y = float(np.interp(c, self.ch, self.xy[:, 1]))
        p = QgsPointXY(x, y)
        if self.xf is not None:
            try:
                p = self.xf.transform(p)
            except QgsCsException:
                return None
        return p
