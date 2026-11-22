"""Small helpers so one code base runs on QGIS 3.x (Qt5) and 4.x (Qt6)."""

try:  # Qt6: QAction lives in QtGui
    from qgis.PyQt.QtGui import QAction, QActionGroup  # noqa: F401
except ImportError:  # Qt5
    from qgis.PyQt.QtWidgets import QAction, QActionGroup  # noqa: F401


def flag_int(*flags):
    """Combine Qt flags of possibly different enum types into an int
    (PyQt6 enums expose ``.value``; PyQt5 enums are ints)."""
    v = 0
    for f in flags:
        v |= int(getattr(f, "value", f))
    return v


def set_text_alignment(item, column, *flags):
    combined = flags[0]
    for f in flags[1:]:
        combined = combined | f
    try:
        item.setTextAlignment(column, combined)
    except TypeError:
        item.setTextAlignment(column, flag_int(*flags))
