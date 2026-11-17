"""Filesystem discovery for zero-install MewCode plugins."""
from __future__ import annotations

import hashlib
import importlib.util
import inspect
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from mewcode.cordis import PluginSpec
from mewcode.plugins.services import (
    AGENT_SOURCE_SLOT,
    SKILL_SOURCE_SLOT,
    WEB_ASSET_SLOT,
    WEB_MENU_SLOT,
    WEB_SETTINGS_SLOT,
)

_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


class PluginLoadError(RuntimeError):
    pass


@dataclass(frozen=True)
class PluginPackage:
    name: str
    root: Path
    source: str
    spec: PluginSpec
    frontend: dict[str, Any] | None = None


@dataclass(frozen=True)
class PluginCatalogError:
    path: Path
    message: str


class PluginCatalog:
    """Discovers user and project packages, with project packages winning."""

    def __init__(
        self,
        work_dir: str | Path,
        *,
        user_dir: str | Path | None = None,
    ) -> None:
        self.work_dir = Path(work_dir).resolve()
        self.user_dir = (
            Path(user_dir).expanduser().resolve()
            if user_dir is not None
            else (Path.home() / ".mewcode" / "plugins").resolve()
        )
        self.project_dir = self.work_dir / ".mewcode" / "plugins"
        self.errors: list[PluginCatalogError] = []

    def discover(self) -> list[PluginPackage]:
        self.errors.clear()
        packages: dict[str, PluginPackage] = {}
        for source, root in (("user", self.user_dir), ("project", self.project_dir)):
            if not root.is_dir():
                continue
            for path in sorted(root.iterdir()):
                if not path.is_dir() or path.name.startswith("."):
                    continue
                try:
                    package = self._load_package(path, source)
                except Exception as exc:
                    self.errors.append(PluginCatalogError(path, str(exc)))
                    continue
                if package is not None:
                    packages[package.name] = package
        return list(packages.values())

    def _load_package(self, root: Path, source: str) -> PluginPackage | None:
        manifest_path = root / "plugin.toml"
        manifest: dict[str, Any] = {}
        if manifest_path.is_file():
            with manifest_path.open("rb") as handle:
                raw = tomllib.load(handle)
            manifest = raw.get("plugin", raw)
            if not isinstance(manifest, dict):
                raise PluginLoadError("plugin.toml must contain a [plugin] table")
        if manifest.get("enabled", True) is False:
            return None

        name = str(manifest.get("name") or root.name)
        if not _NAME_RE.fullmatch(name):
            raise PluginLoadError(f"invalid plugin name: {name!r}")
        version = str(manifest.get("version") or "0")
        purpose = str(manifest.get("purpose") or "")
        requires = _string_tuple(manifest.get("requires", ()), "requires")
        inject = _string_tuple(manifest.get("inject", ()), "inject")
        frontend = _frontend_manifest(root, manifest.get("frontend"), name, version)
        entry = (root / str(manifest.get("entry") or "plugin.py")).resolve()
        if root.resolve() not in entry.parents and entry != root.resolve():
            raise PluginLoadError("plugin entry must stay inside its package directory")

        user_spec = self._load_entry(entry, name) if entry.is_file() else None
        skills_dir = root / "skills"
        agents_dir = root / "agents"
        if (
            user_spec is None
            and not skills_dir.is_dir()
            and not agents_dir.is_dir()
            and frontend is None
        ):
            raise PluginLoadError(
                "package needs plugin.py, skills/, agents/, or [plugin.frontend]"
            )

        def apply(ctx: Any) -> Any:
            if skills_dir.is_dir():
                ctx.slots.register(
                    SKILL_SOURCE_SLOT,
                    skills_dir,
                    kind="list",
                )
            if agents_dir.is_dir():
                ctx.slots.register(
                    AGENT_SOURCE_SLOT,
                    agents_dir,
                    kind="list",
                )
            if frontend is not None:
                ctx.slots.register(WEB_ASSET_SLOT, frontend, kind="list")
                for menu in frontend.get("menus", []):
                    ctx.slots.register(WEB_MENU_SLOT, menu, kind="list")
                for setting in frontend.get("settings", []):
                    ctx.slots.register(WEB_SETTINGS_SLOT, setting, kind="list")
            return user_spec.apply(ctx) if user_spec is not None else None

        spec = PluginSpec(
            name=name,
            apply_fn=apply,
            requires=requires or (user_spec.requires if user_spec else ()),
            inject=inject or (user_spec.inject if user_spec else ()),
            version=version if manifest else (user_spec.version if user_spec else version),
            purpose=purpose or (user_spec.purpose if user_spec else ""),
            source=f"{source}:{root}",
        )
        return PluginPackage(
            name=name,
            root=root,
            source=source,
            spec=spec,
            frontend=frontend,
        )

    @staticmethod
    def _load_entry(path: Path, package_name: str) -> PluginSpec:
        digest = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:12]
        module_name = f"mewcode_dynamic_plugin_{digest}"
        _clear_module_namespace(module_name)
        spec = importlib.util.spec_from_file_location(
            module_name,
            path,
            submodule_search_locations=[str(path.parent)],
        )
        if spec is None or spec.loader is None:
            raise PluginLoadError(f"cannot import {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
            exported = _find_export(module)
        except Exception:
            _clear_module_namespace(module_name)
            raise
        if isinstance(exported, PluginSpec):
            return exported
        if callable(exported):
            return PluginSpec(package_name, exported)
        _clear_module_namespace(module_name)
        raise PluginLoadError(
            "plugin.py must export PLUGIN, mewcode_plugin, or plugin"
        )


def _clear_module_namespace(module_name: str) -> None:
    for name in list(sys.modules):
        if name == module_name or name.startswith(f"{module_name}."):
            sys.modules.pop(name, None)


def _find_export(module: ModuleType) -> Any:
    for name in ("PLUGIN", "mewcode_plugin", "plugin"):
        value = getattr(module, name, None)
        if isinstance(value, PluginSpec):
            return value
        if inspect.isfunction(value) and value.__module__ == module.__name__:
            return value
    return None


def _string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise PluginLoadError(f"{field_name} must be an array of plugin/service names")
    return tuple(value)


def _frontend_manifest(
    root: Path,
    value: Any,
    plugin_name: str,
    version: str,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise PluginLoadError("frontend must be a table")
    scripts = _asset_list(value.get("scripts", value.get("entry", [])), "scripts")
    styles = _asset_list(value.get("styles", []), "styles")
    assets = _asset_list(value.get("assets", []), "assets")
    menus = _dict_list(value.get("menus", []), "menus")
    settings = _dict_list(value.get("settings", []), "settings")
    if not scripts and not styles and not assets and not menus and not settings:
        raise PluginLoadError("frontend needs assets, menus, or settings")
    for relative in (*scripts, *styles, *assets):
        path = (root / relative).resolve()
        if root.resolve() not in path.parents or not path.is_file():
            raise PluginLoadError(f"frontend asset is unavailable: {relative}")
    result = {
        "plugin": plugin_name,
        "version": version,
        "scripts": scripts,
        "styles": styles,
        "module": bool(value.get("module", False)),
        "menus": [{"plugin": plugin_name, **item} for item in menus],
        "settings": [{"plugin": plugin_name, **item} for item in settings],
    }
    if assets:
        result["assets"] = assets
    return result


def _asset_list(value: Any, field_name: str) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if value is None:
        return []
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise PluginLoadError(f"frontend.{field_name} must be a string or array")
    return [item.replace("\\", "/").lstrip("/") for item in value]


def _dict_list(value: Any, field_name: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise PluginLoadError(f"frontend.{field_name} must be an array of tables")
    return [dict(item) for item in value]


__all__ = [
    "PluginCatalog",
    "PluginCatalogError",
    "PluginLoadError",
    "PluginPackage",
]
