"""QGIS plugin entry: one action for the toolbar icon, menu and shortcut.

The action has no default shortcut.  Users assign one in
Settings > Keyboard Shortcuts (search "Profile Plot"); QGIS stores it and
it respects existing bindings.  Pressing it (or clicking the icon) opens
the dock and starts drawing a line; if the dock is already open it starts
a new line.
"""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QKeySequence

from .core.compat import QAction
from .ui import icons

ACTION_NAME = "mActionProfilePlot"
TOOLBAR_NAME = "ProfilePlotToolbar"
MENU = "&Profile Plot"


class ProfilePlotPlugin:

    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.toolbar = None
        self.dock = None

    # -- QGIS hooks -----------------------------------------------------------

    def initGui(self):  # noqa: N802
        mw = self.iface.mainWindow()
        self.action = QAction(icons.icon("plugin"), "Profile Plot", mw)
        self.action.setObjectName(ACTION_NAME)
        self.action.setCheckable(True)
        self.action.setStatusTip("Open Profile Plot and draw a profile line")
        self.action.triggered.connect(self.run)
        # Register in Settings > Keyboard Shortcuts with no default key.
        self.iface.registerMainWindowAction(self.action, "")
        # Its own toolbar (not the shared Plugins toolbar); QGIS remembers
        # where the user puts it through the object name.
        self.toolbar = self.iface.addToolBar("Profile Plot")
        self.toolbar.setObjectName(TOOLBAR_NAME)
        self.toolbar.setToolTip("Profile Plot")
        self.toolbar.addAction(self.action)
        self.iface.addPluginToRasterMenu(MENU, self.action)
        self.action.changed.connect(self._update_tooltip)
        self._update_tooltip()

    def unload(self):
        if self.dock is not None:
            self.dock.cleanup()
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
        if self.action is not None:
            try:
                self.action.changed.disconnect(self._update_tooltip)
            except (TypeError, RuntimeError):
                pass
            self.iface.unregisterMainWindowAction(self.action)
            self.iface.removePluginRasterMenu(MENU, self.action)
            self.action.deleteLater()
            self.action = None
        if self.toolbar is not None:
            self.iface.mainWindow().removeToolBar(self.toolbar)
            # Detach now (deleteLater only runs on the next event loop pass)
            # so a plugin reload never finds a stale toolbar.
            self.toolbar.setParent(None)
            self.toolbar.deleteLater()
            self.toolbar = None

    # -- behaviour --------------------------------------------------------------

    def _update_tooltip(self):
        sc = self.action.shortcut()
        text = "Profile Plot — draw a profile line"
        if not sc.isEmpty():
            text += " ({})".format(sc.toString(QKeySequence.SequenceFormat.NativeText))
        if self.action.toolTip() != text:
            self.action.setToolTip(text)

    def _ensure_dock(self):
        if self.dock is None:
            from .ui.dock import ProfileDock
            self.dock = ProfileDock(self.iface, self.action)
            self.iface.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock)
        return self.dock

    def run(self, *_):
        """Open the dock (if needed) and start drawing."""
        dock = self._ensure_dock()
        if not dock.isVisible() or not dock.isUserVisible():
            dock.setUserVisible(True)
        dock.raise_()
        if not dock.start_drawing():
            # Nothing to plot yet: keep the icon unpressed.
            self.action.setChecked(dock.canvas.mapTool() is dock.map_tool)
