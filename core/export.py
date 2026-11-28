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


def headers(table, show_units, unit, x_title="Chainage"):
    """Column headings: the X-axis title, then one per raster (its name)."""
    if not table.kind:
        return []
    cu = " (m)" if show_units else ""
    vu = " ({})".format(unit) if show_units else ""
    names = [n + vu + (" (bilinear)" if table.interpolated else "") for n in table.names]
    return [(x_title or "Chainage") + cu] + names


def _value_formatter(dtype):
    fmt = "%.7g" if dtype == np.float32 else "%.15g"

    def f(v):
        return fmt % v if v == v else NODATA
    return f


def _column_formatters(table):
    """One formatter per column: chainage then raster values (full
    precision of the source raster, NoData for gaps)."""
    ch_fmt = "%.3f" if table.kind == "native" else "%.2f"
    dtypes = list(table.dtypes) + [np.float64] * (len(table.columns) - len(table.dtypes))
    return [lambda v, f=ch_fmt: f % v] + [_value_formatter(d) for d in dtypes]


def _column_arrays(table):
    return [table.x] + list(table.columns)


def table_rows(table, rows=None, cols=None, chunk=50000):
    """Yield lists of formatted cells for ``rows`` (all when None, else a
    sorted index array) and ``cols`` (all when None, 0 = X)."""
    arrays = _column_arrays(table)
    fmts = _column_formatters(table)
    cols = list(range(len(arrays))) if cols is None else list(cols)
    n = table.row_count
    if rows is None:
        for s in range(0, n, chunk):
            e = min(n, s + chunk)
            blocks = [[fmts[c](v) for v in arrays[c][s:e].tolist()] for c in cols]
            yield from (list(r) for r in zip(*blocks))
    else:
        rows = np.asarray(rows, dtype=np.int64)
        for s in range(0, rows.size, chunk):
            idx = rows[s:s + chunk]
            blocks = [[fmts[c](v) for v in arrays[c][idx].tolist()] for c in cols]
            yield from (list(r) for r in zip(*blocks))


def table_text(table, show_units, unit, x_title="Chainage", rows=None, cols=None,
               header=True, blank=None):
    """Tab-separated text for the clipboard (pastes into Excel as cells).
    ``blank``: optional function (row, col) -> True for cells to leave empty
    (for selections that are not a full rectangle)."""
    hdr = headers(table, show_units, unit, x_title)
    cols = list(range(len(hdr))) if cols is None else list(cols)
    lines = []
    if header:
        lines.append("\t".join(hdr[c] for c in cols))
    row_ids = range(table.row_count) if rows is None else rows
    for r, cells in zip(row_ids, table_rows(table, rows, cols)):
        if blank is not None:
            cells = ["" if blank(int(r), c) else v for c, v in zip(cols, cells)]
        lines.append("\t".join(cells))
    return "\n".join(lines) + "\n"


def write_csv(path, table, show_units, unit, dtypes=None, x_title="Chainage"):
    """Write every row of ``table``.  Returns the number of rows written."""
    if dtypes is not None:
        table.dtypes = list(dtypes)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(headers(table, show_units, unit, x_title))
        w.writerows(table_rows(table))
    return table.row_count
