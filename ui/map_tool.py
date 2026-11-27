"""Line capture map tool and the alignment canvas item."""

import logging

from qgis.core import QgsApplication, QgsPointXY, QgsProject, QgsSnappingConfig
from qgis.gui import QgsMapCanvasItem, QgsMapCanvasSnappingUtils, QgsMapTool, QgsSnapIndicator
from qgis.PyQt.QtCore import QRectF, Qt, pyqtSignal
from qgis.PyQt.QtGui import QBrush, QColor, QFont, QFontMetricsF, QPainter, QPen

from ..core.compat import flag_int

_log = logging.getLogger(__name__)

LINE_COLOR = QColor("#e815e8")
MARKER_COLOR = QColor("#ffd60a")
PIN_COLOR = QColor(55, 71, 79)      # same as the plot's marker lines


class ProfileMapTool(QgsMapTool):
    """Click to add vertices, Backspace to undo, right-click (or Enter) to
    finish, Esc to cancel.  The tool stays active after a line is finished
    so the next click starts a new profile."""

    startRequested = pyqtSignal(QgsPointXY)
    vertexRequested = pyqtSignal(QgsPointXY)
    tailMoved = pyqtSignal(QgsPointXY)
    finishRequested = pyqtSignal()
    undoRequested = pyqtSignal()
    cancelRequested = pyqtSignal()
    exitRequested = pyqtSignal()

    def __init__(self, canvas):
        super().__init__(canvas)
        self.drawing = False
        self.snapping = False    # Profile Plot's own snapping toggle (off by default)
        self._snap = QgsSnapIndicator(canvas)
        self._snap_utils = None
        try:
            self.setCursor(QgsApplication.getThemeCursor(
                QgsApplication.Cursor.CapturePoint))
        except Exception:
            self.setCursor(Qt.CursorShape.CrossCursor)

    def set_snapping(self, on):
        """Profile Plot's snapping toggle.  It is independent of QGIS's own
        snapping button: on, clicks snap using the project's snapping setup
        (layers, vertex/segment type, tolerance) even if QGIS snapping is
        off; off, clicks are never snapped, even if QGIS snapping is on."""
        self.snapping = bool(on)
        if not self.snapping:
            self._snap.setMatch(_null_match())

    def _snapping_utils(self):
        canvas = self.canvas()
        if self._snap_utils is None:
            self._snap_utils = QgsMapCanvasSnappingUtils(canvas, self)
        cfg = QgsSnappingConfig(QgsProject.instance().snappingConfig())
        cfg.setEnabled(True)
        if cfg != self._snap_utils.config():
            self._snap_utils.setConfig(cfg)
        self._snap_utils.setCurrentLayer(canvas.currentLayer())
        return self._snap_utils

    def _point(self, event):
        """Clicked map position, snapped only when our toggle is on."""
        if self.snapping:
            try:
                match = self._snapping_utils().snapToMap(event.pixelPoint())
                self._snap.setMatch(match)
                if match.isValid():
                    return QgsPointXY(match.point())
            except Exception:
                _log.debug("Snapping failed; using unsnapped point", exc_info=True)
        return QgsPointXY(event.mapPoint())

    def canvasPressEvent(self, event):
        btn = event.button()
        if btn == Qt.MouseButton.LeftButton:
            p = self._point(event)
            if not self.drawing:
                self.drawing = True
                self.startRequested.emit(p)
            else:
                self.vertexRequested.emit(p)
        elif btn == Qt.MouseButton.RightButton and self.drawing:
            self.drawing = False
            self.finishRequested.emit()

    def canvasMoveEvent(self, event):
        p = self._point(event)
        if self.drawing:
            self.tailMoved.emit(p)

    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete) and self.drawing:
            self.undoRequested.emit()
            event.accept()
            return
        if key == Qt.Key.Key_Escape:
            if self.drawing:
                self.drawing = False
                self.cancelRequested.emit()
            else:
                self.exitRequested.emit()
            event.accept()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.drawing:
            self.drawing = False
            self.finishRequested.emit()
            event.accept()
            return
        event.ignore()

    def deactivate(self):
        self._snap.setMatch(_null_match())
        if self.drawing:
            self.drawing = False
            self.cancelRequested.emit()
        super().deactivate()


def _null_match():
    from qgis.core import QgsPointLocator
    return QgsPointLocator.Match()


class ProfileCanvasItem(QgsMapCanvasItem):
    """Draws the alignment, its A/B ends and the plot-cursor marker.

    The item covers the viewport and repaints only dirty regions."""

    def __init__(self, canvas):
        super().__init__(canvas)
        self._canvas = canvas
        self.setZValue(1000)
        self.points = []
        self.tail = None
        self.finished = False
        self.marker = None
        self.pins = []        # plot markers on the map: QgsPointXY, numbered 1..n
        self.show_ab = False   # A/B labels and end markers (Settings)
        self._font = QFont()
        self._font.setBold(True)
        self.setPos(0, 0)

    # -- geometry -------------------------------------------------------------

    def boundingRect(self):
        s = self._canvas.viewport().size()
        return QRectF(-50, -50, s.width() + 100, s.height() + 100)

    def updatePosition(self):
        self.prepareGeometryChange()
        self.setPos(0, 0)
        self.update()

    def _px(self, p):
        return self.toCanvasCoordinates(p) - self.pos()

    def _rect_of(self, pts, pad=24.0):
        if not pts:
            return QRectF()
        xs = [q.x() for q in pts]
        ys = [q.y() for q in pts]
        return QRectF(min(xs) - pad, min(ys) - pad, max(xs) - min(xs) + 2 * pad,
                      max(ys) - min(ys) + 2 * pad)

    # -- state ---------------------------------------------------------------------

    def set_line(self, points, tail, finished):
        """Full update of the line (vertex added, finished, cleared)."""
        self.points = list(points)
        self.tail = tail
        self.finished = finished
        self.update()

    def set_tail(self, tail):
        """Fast path: only the moving segment changes."""
        if not self.points:
            self.tail = tail
            return
        anchor = self._px(self.points[-1])
        dirty = [anchor]
        if self.tail is not None:
            dirty.append(self._px(self.tail))
        self.tail = tail
        if tail is not None:
            dirty.append(self._px(tail))
        self.update(self._rect_of(dirty))

    def set_pins(self, points):
        """Numbered markers matching the plot's markers (None = off map)."""
        self.pins = list(points)
        self.update()

    def set_marker(self, point):
        dirty = []
        if self.marker is not None:
            dirty.append(self._px(self.marker))
        self.marker = point
        if point is not None:
            dirty.append(self._px(point))
        if dirty:
            self.update(self._rect_of(dirty, 12))

    def set_show_ab(self, show):
        show = bool(show)
        if show != self.show_ab:
            self.show_ab = show
            self.update()

    def clear(self):
        self.points = []
        self.tail = None
        self.marker = None
        self.pins = []
        self.update()

    # -- painting ---------------------------------------------------------------------

    def paint(self, painter, option=None, widget=None):
        if not self.points:
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pts = [self._px(p) for p in self.points]
        halo = QPen(QColor(255, 255, 255, 170))
        halo.setWidthF(4.5)
        halo.setCapStyle(Qt.PenCapStyle.RoundCap)
        halo.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        pen = QPen(LINE_COLOR)
        pen.setWidthF(2.2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        if len(pts) > 1:
            for pn in (halo, pen):
                painter.setPen(pn)
                for a, b in zip(pts[:-1], pts[1:]):
                    painter.drawLine(a, b)
        if self.tail is not None and not self.finished:
            t = self._px(self.tail)
            painter.setPen(halo)
            painter.drawLine(pts[-1], t)
            dash = QPen(pen)
            dash.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(dash)
            painter.drawLine(pts[-1], t)
        if self.show_ab:
            # vertices, end markers and the A / B labels
            painter.setPen(QPen(LINE_COLOR, 1.5))
            painter.setBrush(QBrush(QColor("#ffffff")))
            for q in pts[1:-1]:
                painter.drawEllipse(q, 2.5, 2.5)
            painter.drawEllipse(pts[0], 4.5, 4.5)
            if len(pts) > 1:
                painter.drawEllipse(pts[-1], 4.5, 4.5)
            self._label(painter, pts[0], "A")
            if self.finished and len(pts) > 1:
                self._label(painter, pts[-1], "B")
        for n, pin in enumerate(self.pins, start=1):
            if pin is None:
                continue
            q = self._px(pin)
            painter.setPen(QPen(QColor("#ffffff"), 1.5))
            painter.setBrush(QBrush(PIN_COLOR))
            painter.drawEllipse(q, 7.5, 7.5)
            painter.setFont(self._font)
            painter.setPen(QPen(QColor("#ffffff")))
            painter.drawText(QRectF(q.x() - 8, q.y() - 8, 16, 16),
                             flag_int(Qt.AlignmentFlag.AlignCenter), str(n))
        if self.marker is not None:
            m = self._px(self.marker)
            painter.setPen(QPen(QColor("#202020"), 1.5))
            painter.setBrush(QBrush(MARKER_COLOR))
            painter.drawEllipse(m, 5.5, 5.5)

    def _label(self, painter, at, text):
        painter.setFont(self._font)
        fm = QFontMetricsF(self._font)
        w = fm.horizontalAdvance(text) + 8
        h = fm.height() + 2
        box = QRectF(at.x() - w / 2, at.y() - h - 9, w, h)
        painter.setPen(QPen(QColor("#202020"), 1.0))
        painter.setBrush(QBrush(QColor("#ffffff")))
        painter.drawRect(box)
        painter.drawText(box, flag_int(Qt.AlignmentFlag.AlignCenter), text)
