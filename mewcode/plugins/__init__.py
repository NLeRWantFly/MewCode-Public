"""MewCode's plugin composition layer."""
from .catalog import PluginCatalog, PluginCatalogError, PluginLoadError, PluginPackage
from .fastapi import FastAPIInstallResult, fastapi_plugin
from .host import ApplicationHost
from .services import *
from .services import __all__ as _service_all

__all__ = [
    "ApplicationHost",
    "PluginCatalog",
    "PluginCatalogError",
    "PluginLoadError",
    "PluginPackage",
    "fastapi_plugin",
    "FastAPIInstallResult",
] + list(_service_all)
