"""Icons: QGIS theme icons where a match exists, otherwise bundled SVGs.

Bundled SVGs that use ``currentColor`` are recoloured from the active
palette so they follow light and dark themes.
"""

import os

from qgis.core import QgsApplication
from qgis.PyQt.QtCore import QByteArray, QRect, QRectF, Qt
from qgis.PyQt.QtGui import QColor, QIcon, QPainter, QPalette, QPixmap
from qgis.PyQt.QtWidgets import QApplication

try:
    from qgis.PyQt.QtSvg import QSvgRenderer
except ImportError:  # pragma: no cover
    QSvgRenderer = None

# Plugin root (one level up from ui/) holds icon.svg and the icons/ folder.
PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICON_DIR = os.path.join(PLUGIN_DIR, "icons")

# name -> (QGIS theme icon candidates, bundled fallback)
_MAP = {
    "draw_line": (["/mActionCaptureLine.svg"], "draw_line.svg"),
    "use_selected": (["/mActionSelectRectangle.svg", "/mActionSelect.svg"],
                     "use_selected.svg"),
    "hover": ([], "hover.svg"),
    "crosshair": ([], "crosshair.svg"),
    "zoom_full": (["/mActionZoomFullExtent.svg"], "zoom_full.svg"),
    "export": (["/mActionSharingExport.svg"], "export.svg"),
    "temporary": (["/mActionCreateMemory.svg", "/mIconMemory.svg"], "temporary.svg"),
    "image": (["/mActionSaveMapAsImage.svg"], None),
    "svg": (["/mActionSaveAsSVG.svg"], None),
    "copy": (["/mActionEditCopy.svg"], None),
    "settings": (["/mActionOptions.svg"], "settings.svg"),
    "add": (["/mActionAdd.svg", "/symbologyAdd.svg"], "add.svg"),
    "rasters": ([], "rasters.svg"),
    "save": (["/mActionFileSaveAs.svg", "/mActionFileSave.svg"], None),
    "remove": (["/mActionRemove.svg", "/symbologyRemove.svg"], None),
    "up": (["/mActionArrowUp.svg"], None),
    "down": (["/mActionArrowDown.svg"], None),
    "plugin": ([], "../icon.svg"),
}

_cache = {}


def svg_path(name):
    return os.path.join(ICON_DIR, name)


def themed_svg_icon(filename):
    path = svg_path(filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = f.read()
    except OSError:
        return QIcon()
    if "currentColor" not in data or QSvgRenderer is None:
        return QIcon(path)
    pal = QApplication.palette()
    icon = QIcon()
    modes = (
        (QIcon.Mode.Normal, pal.color(QPalette.ColorGroup.Active, QPalette.ColorRole.ButtonText)),
        (QIcon.Mode.Active, pal.color(QPalette.ColorGroup.Active, QPalette.ColorRole.ButtonText)),
        (QIcon.Mode.Disabled, pal.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText)),
    )
    for mode, color in modes:
        svg = data.replace("currentColor", QColor(color).name())
        renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        for size in (16, 20, 24, 32, 48, 64):
            pm = QPixmap(size, size)
            pm.fill(Qt.GlobalColor.transparent)
            p = QPainter(pm)
            renderer.render(p, QRectF(0, 0, size, size))
            p.end()
            icon.addPixmap(pm, mode)
    return icon


def icon(name):
    if name in _cache:
        return _cache[name]
    theme, fallback = _MAP.get(name, ([], None))
    result = QIcon()
    for t in theme:
        ic = QgsApplication.getThemeIcon(t)
        if not ic.isNull() and _has_pixmap(ic):
            result = ic
            break
    if result.isNull() and fallback:
        result = themed_svg_icon(fallback)
    _cache[name] = result
    return result


def _has_pixmap(ic):
    try:
        return not ic.pixmap(16, 16).isNull()
    except Exception:
        return False


def clear_cache():
    """Call after a theme/palette change."""
    _cache.clear()


SWATCH_SIZE = 14


def paint_swatch(painter, rect, color):
    """The one colour-swatch look used everywhere (raster rows, slope
    selector): a solid square with a thin dark outline."""
    painter.setPen(QColor(0, 0, 0, 110))
    painter.setBrush(QColor(color))
    painter.drawRect(rect.adjusted(0, 0, -1, -1))


def color_swatch(color, size=SWATCH_SIZE):
    icon = QIcon()
    for scale in (1, 2):
        pm = QPixmap(size * scale, size * scale)
        pm.setDevicePixelRatio(scale)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        paint_swatch(p, QRect(0, 0, size, size), color)
        p.end()
        icon.addPixmap(pm)
    return icon
