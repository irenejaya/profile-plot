"""Export the alignment to a temporary line layer."""

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
