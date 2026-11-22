"""Persistent user settings (read and written through QgsSettings)."""

from qgis.core import QgsSettings

PREFIX = "ProfilePlot/"


def _s():
    return QgsSettings()


def get(key, default=None, type_=None):
    s = _s()
    if type_ is bool:
        v = s.value(PREFIX + key, default)
        if isinstance(v, str):
            return v.lower() in ("1", "true", "yes")
        return bool(v)
    if type_ is not None:
        try:
            return type_(s.value(PREFIX + key, default))
        except (TypeError, ValueError):
            return default
    return s.value(PREFIX + key, default)


def set(key, value):  # noqa: A001 - mirrors QgsSettings naming
    _s().setValue(PREFIX + key, value)


def show_units():
    return get("showUnits", False, bool)


def unit_label():
    text = get("unit", "m", str)
    return text.strip() or "m"


def show_legend():
    return get("showLegend", False, bool)


SLOPE_UNITS = (("%", "% (percent)"), ("m/m", "m/m"))
LINE_STYLES = (("smooth", "Smooth line"), ("steps", "Steps (native cells)"))
DEFAULT_X_TITLE = "Chainage"
DEFAULT_Y_TITLE = "Elevation"


def show_ab():
    return get("showAB", False, bool)


def snap_drawing():
    """Use the project's snapping settings while drawing (off by default)."""
    return get("snapDrawing", False, bool)


def line_style():
    style = get("lineStyle", "smooth", str)
    return style if style in dict(LINE_STYLES) else "smooth"


def x_title():
    return (get("xTitle", DEFAULT_X_TITLE, str) or "").strip() or DEFAULT_X_TITLE


def y_title():
    return (get("yTitle", DEFAULT_Y_TITLE, str) or "").strip() or DEFAULT_Y_TITLE


def slope_unit():
    unit = get("slopeUnit", "%", str)
    return unit if unit in dict(SLOPE_UNITS) else "%"
