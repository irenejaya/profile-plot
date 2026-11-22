"""Profile Plot - fast raster profiles for QGIS."""


def classFactory(iface):  # noqa: N802 (QGIS entry point name)
    """Load the plugin.  Imports are deferred so QGIS starts fast."""
    from .plugin import ProfilePlotPlugin
    return ProfilePlotPlugin(iface)
