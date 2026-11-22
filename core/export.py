"""Exports: the alignment to a temporary line layer, the table to CSV."""

import csv

import numpy as np

from qgis.core import (
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsProject,
    QgsVectorLayer,
)


def _field(name, kind):
    """QgsField with QMetaType (QGIS >= 3.38 / 4.x) or QVariant (older)."""
    try:
        from qgis.PyQt.QtCore import QMetaType
        return QgsField(name, getattr(QMetaType.Type, kind))
    except (ImportError, AttributeError, TypeError):
        from qgis.PyQt.QtCore import QVariant
        return QgsField(name, getattr(QVariant, kind))


def export_alignment(points, crs, length, name="Profile alignment"):
    """Add a memory layer holding the original vertices, direction and CRS."""
    layer = QgsVectorLayer("LineString", name, "memory")
    layer.setCrs(crs)
    pr = layer.dataProvider()
    pr.addAttributes([_field("length_m", "Double"), _field("vertices", "Int")])
    layer.updateFields()
    f = QgsFeature(layer.fields())
    f.setGeometry(QgsGeometry.fromPolylineXY(list(points)))
    f.setAttributes([float(length), len(points)])
    pr.addFeatures([f])
    layer.updateExtents()
    QgsProject.instance().addMapLayer(layer)
    return layer


NODATA = "NoData"


def headers(table, show_units, unit):
    cu = " (m)" if show_units else ""
    vu = " ({})".format(unit) if show_units else ""
    interp = getattr(table, "interpolated", False)
    names = [n + vu + (" (bilinear)" if interp else "") for n in table.names]
    if table.kind == "intervals":
        return ["From" + cu, "To" + cu] + names
    if table.kind == "stations":
        return ["Chainage" + cu] + names
    return []


def _value_formatter(dtype):
    fmt = "%.7g" if dtype == np.float32 else "%.15g"

    def f(v):
        return fmt % v if v == v else NODATA
    return f


def write_csv(path, table, show_units, unit, dtypes=None):
    """Write every row of ``table``.  Returns the number of rows written."""
    hdr = headers(table, show_units, unit)
    n = table.row_count
    dtypes = dtypes or [np.float64] * len(table.columns)
    fmts = [_value_formatter(d) for d in dtypes]
    ch_fmt = "%.3f" if table.kind == "intervals" else "%.2f"
    chunk = 50000
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(hdr)
        for s in range(0, n, chunk):
            e = min(n, s + chunk)
            cols = [[ch_fmt % v for v in table.from_ch[s:e].tolist()]]
            if table.kind == "intervals":
                cols.append([ch_fmt % v for v in table.to_ch[s:e].tolist()])
            for f, col in zip(fmts, table.columns):
                cols.append([f(v) for v in col[s:e].tolist()])
            w.writerows(zip(*cols))
    return n
