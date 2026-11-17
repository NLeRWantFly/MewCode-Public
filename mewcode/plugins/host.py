"""Application composition built on the Cordis runtime.

The adapters in this module are intentionally boring. Their job is to bridge
legacy registries to slots once; feature modules then stop knowing who created
the registry or in which order another feature was initialized.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from mewcode.cordis import Context, Disposer, PluginSpec, PluginState, plugin
from mewcode.commands.handlers import register_all_commands
from mewcode.commands.registry import Command, CommandRegistry
from mewcode.hooks import HookEngine
from mewcode.skills.loader import SkillLoader
from mewcode.agents.loader import AgentLoader
from mewcode.tools import ToolRegistry, create_default_registry
from mewcode.cache_slots import create_cache_slot_registry
from mewcode.experts.definitions import ExpertRegistry
from mewcode.experts.flow import DEFAULT_EXPERT_FLOW, ExpertFlowRegistry
from mewcode.context.routing import context_router_plugin
from mewcode.model_catalog import ModelCatalogService, model_catalog_plugin
from mewcode.model_registry import ProviderRegistry
from mewcode.runtime_scheduler import RuntimeScheduler

from .catalog import PluginCatalog, PluginCatalogError, PluginPackage
from .services import (
    AGENT_CATALOG,
    AGENT_FACTORY,
    AGENT_SOURCE_SLOT,
    CACHE_PROVIDERS,
    CACHE_PROVIDER_SLOT,
    CLIENT_FACTORY,
    CLIENT_FACTORY_SLOT,
    COMMANDS,
    COMMAND_SLOT,
    CONVERSATION_FACTORY,
    CORE_SLOTS,
    EXPERTS,
    EXPERT_FLOWS,
    EXPERT_FLOW_SLOT,
    EXPERT_SLOT,
    HOOKS,
    HOST,
    MODEL_CATALOG,
    PROVIDERS,
    PROVIDER_SLOT,
    RUNTIME_SCHEDULER,
    SESSION_FACTORY,
    TASK_FACTORY,
    TRACE_FACTORY,
    SKILLS,
    SKILL_SOURCE_SLOT,
    TOOLS,
    TOOL_SLOT,
    WORK_DIR,
)
from .runtime import (
    AgentRuntimeFactory,
    ConversationFactory,
    RuntimeClientFactory,
    SessionFactory,
    TaskFactory,
    TraceFactory,
)

log = logging.getLogger(__name__)


class ApplicationHost:
    """Own one process or session plugin graph.

    ``start`` is safe for the synchronous TUI bootstrap. ``astart`` is the
    equivalent entry point for a server or another running event loop.
    """

    def __init__(
        self,
        work_dir: str,
        *,
        context: Context | None = None,
        file_cache: Any = None,
        hook_engine: HookEngine | None = None,
        enable_verification_agent: bool = False,
        user_plugin_dir: str | Path | None = None,
        discover_plugins: bool = True,
    ) -> None:
        self.work_dir = str(Path(work_dir).resolve())
        self.context = context or Context("mewcode")
        self.file_cache = file_cache
        self.hook_engine = hook_engine
        self.enable_verification_agent = enable_verification_agent
        self.catalog = PluginCatalog(self.work_dir, user_dir=user_plugin_dir)
        self.discover_plugins = discover_plugins
        self._external: dict[str, PluginPackage] = {}
        self._bindings: dict[str, str] = {}
        self._binding_lock = asyncio.Lock()
        self._reconfigure_lock = asyncio.Lock()
        self._started = False
        self._register_core_specs()

    def _register_core_specs(self) -> None:
        from mewcode.context.tool_history_shadow import plugin_spec as tool_history_shadow_plugin
        model_catalog = replace(
            model_catalog_plugin(ModelCatalogService()),
            requires=("core.runtime",),
        )
        context_router = replace(
            context_router_plugin(),
            requires=("core.runtime",),
        )
        specs = (
            PluginSpec(
                "core.runtime",
                self._apply_runtime,
                version="1",
                purpose="Host services and extension slot declarations",
            ),
            model_catalog,
            context_router,
            tool_history_shadow_plugin(),
            PluginSpec(
                "core.tools",
                self._apply_tools,
                requires=("core.runtime",),
                version="1",
                purpose="ToolRegistry adapter and dynamic tool slot",
            ),
            PluginSpec(
                "core.commands",
                self._apply_commands,
                requires=("core.runtime",),
                version="1",
                purpose="CommandRegistry adapter and dynamic command slot",
            ),
            PluginSpec(
                "core.catalogs",
                self._apply_catalogs,
                requires=("core.runtime",),
                version="1",
                purpose="Project, user, built-in, and plugin Agent/Skill catalogs",
            ),
            PluginSpec(
                "core.hooks",
                self._apply_hooks,
                requires=("core.runtime",),
                version="1",
                purpose="Lifecycle hook service",
            ),
            PluginSpec(
                "core.providers",
                self._apply_providers,
                requires=("core.runtime", model_catalog.name),
                version="1",
                purpose="Provider registry and protocol client factories",
            ),
            PluginSpec(
                "core.scheduler",
                self._apply_scheduler,
                requires=("core.runtime",),
                version="1",
                purpose="Process runtime work scheduler",
            ),
            PluginSpec(
                "core.cache",
                self._apply_cache,
                requires=("core.runtime",),
                version="1",
                purpose="Cache accounting provider registry",
            ),
            PluginSpec(
                "core.experts",
                self._apply_experts,
                requires=("core.runtime",),
                version="1",
                purpose="Expert definition registry",
            ),
            PluginSpec(
                "core.factories",
                self._apply_factories,
                requires=(
                    "core.providers",
                    "core.scheduler",
                    "core.cache",
                    context_router.name,
                ),
                version="1",
                purpose="Lifecycle-owned Agent runtime factory",
            ),
        )
        for spec in specs:
            self.context.register_plugin(spec)

    def _apply_runtime(self, ctx: Context) -> Disposer:
        ctx.provide(HOST, self)
        ctx.provide(WORK_DIR, self.work_dir)
        for key, kind in CORE_SLOTS:
            ctx.slots.declare(key, kind=kind, owner="core.runtime")
        return None

    def _apply_tools(self, ctx: Context) -> None:
        registry = create_default_registry(file_cache=self.file_cache)
        ctx.provide(TOOLS, registry)
        installed: dict[str, Any] = {}
        previous: dict[str, Any] = {}

        def sync(values: dict[str, Any] | None) -> None:
            values = values or {}
            for name in list(installed):
                if name in values and installed[name] is values[name]:
                    continue
                registry.unregister(name)
                old = previous.pop(name, None)
                if old is not None:
                    registry.register(old)
                installed.pop(name, None)
            for name, tool in values.items():
                if name in installed:
                    continue
                previous[name] = registry.get(name)
                registry.register(tool)
                installed[name] = tool

        ctx.slots.inject(TOOL_SLOT, sync)

    def _apply_commands(self, ctx: Context) -> None:
        registry = CommandRegistry()
        register_all_commands(registry)
        ctx.provide(COMMANDS, registry)
        installed: dict[str, Command] = {}
        previous: dict[str, Command | None] = {}

        def sync(values: dict[str, Any] | None) -> None:
            values = values or {}
            for name in list(installed):
                command = values.get(name)
                if command is installed[name]:
                    continue
                registry.unregister(name)
                old = previous.pop(name, None)
                if old is not None:
                    registry.register_sync(old)
                installed.pop(name, None)
            for name, command in values.items():
                if name in installed:
                    continue
                if not isinstance(command, Command):
                    log.warning("plugin command %s is not a Command", name)
                    continue
                previous[name] = registry.find(name)
                registry.register_sync(command)
                installed[name] = command

        ctx.slots.inject(COMMAND_SLOT, sync)

    def _apply_catalogs(self, ctx: Context) -> None:
        skills = SkillLoader(self.work_dir)
        agents = AgentLoader(
            self.work_dir,
            enable_verification=self.enable_verification_agent,
        )
        ctx.provide(SKILLS, skills)
        ctx.provide(AGENT_CATALOG, agents)

        def sync_skills(paths: list[Path] | None) -> None:
            skills.set_plugin_sources(_paths(paths))
            skills.load_all()

        def sync_agents(paths: list[Path] | None) -> None:
            agents.set_plugin_sources(_paths(paths))
            agents.load_all()

        ctx.slots.inject(SKILL_SOURCE_SLOT, sync_skills)
        ctx.slots.inject(AGENT_SOURCE_SLOT, sync_agents)
        skills.load_all()
        agents.load_all()

    def _apply_hooks(self, ctx: Context) -> None:
        ctx.provide(HOOKS, self.hook_engine)

    def _apply_providers(self, ctx: Context) -> None:
        client_factory = RuntimeClientFactory(ctx)
        registry = ProviderRegistry(
            client_factory=client_factory.create,
            catalog_service=ctx.get(MODEL_CATALOG),
            desktop_source=ctx.parent.get(PROVIDERS, None) if ctx.parent is not None else None,
        )
        ctx.provide(PROVIDERS, registry)
        ctx.provide(CLIENT_FACTORY, client_factory)
        ctx.slots.register(
            CLIENT_FACTORY_SLOT,
            client_factory,
            priority=-100,
            select=lambda _provider: True,
        )

        def sync(values: dict[str, Any] | None) -> None:
            slots = [
                value for value in (values or {}).values()
                if hasattr(value, "provider") and hasattr(value, "key")
            ]
            registry.refresh_discovered(slots)

        ctx.slots.inject(PROVIDER_SLOT, sync)

    def _apply_scheduler(self, ctx: Context) -> None:
        # Desktop sessions are nested hosts, not independent execution pools.
        # Shadowing the parent's scheduler loses its clocks/load notifications
        # and lets each session bypass the process-wide concurrency limits.
        inherited = ctx.parent.get(RUNTIME_SCHEDULER, None) if ctx.parent is not None else None
        ctx.provide(
            RUNTIME_SCHEDULER,
            inherited if inherited is not None else RuntimeScheduler(
                limit=8,
                kind_limits={"model": 4, "tool": 4, "usage": 1},
            ),
        )

    def _apply_cache(self, ctx: Context) -> None:
        registry = create_cache_slot_registry()
        ctx.provide(CACHE_PROVIDERS, registry)

        def sync(values: dict[str, Any] | None) -> None:
            values = values or {}
            installed = registry.providers()
            for key in list(installed):
                if key in {"deepseek", "anthropic", "openai"}:
                    continue
                if key not in values:
                    registry.unregister(key)
            for key, provider in values.items():
                registry.register(key, provider)

        ctx.slots.inject(CACHE_PROVIDER_SLOT, sync)

    def _apply_experts(self, ctx: Context) -> None:
        roles = ExpertRegistry()
        flows = ExpertFlowRegistry((DEFAULT_EXPERT_FLOW,))
        ctx.provide(EXPERTS, roles)
        ctx.provide(EXPERT_FLOWS, flows)

        installed_roles: dict[str, str] = {}
        installed_flows: dict[str, str] = {}

        def sync_roles(values: dict[str, Any] | None) -> None:
            values = values or {}
            for key in set(installed_roles) - set(values):
                roles.unregister(installed_roles.pop(key))
            for key, definition in values.items():
                parsed = roles.register(definition)
                installed_roles[key] = parsed.name

        def sync_flows(values: dict[str, Any] | None) -> None:
            values = values or {}
            for key in set(installed_flows) - set(values):
                flows.unregister(installed_flows.pop(key))
            for key, flow in values.items():
                parsed = flows.register(flow)
                installed_flows[key] = parsed.flow_id

        ctx.slots.inject(EXPERT_SLOT, sync_roles)
        ctx.slots.inject(EXPERT_FLOW_SLOT, sync_flows)

    def _apply_factories(self, ctx: Context) -> None:
        ctx.provide(AGENT_FACTORY, AgentRuntimeFactory(ctx))
        ctx.provide(CONVERSATION_FACTORY, ConversationFactory())
        ctx.provide(SESSION_FACTORY, SessionFactory())
        ctx.provide(TASK_FACTORY, TaskFactory())
        ctx.provide(TRACE_FACTORY, TraceFactory())

    def start(self) -> "ApplicationHost":
        if self._started:
            return self
        try:
            self._load_core_sync()
            if self.discover_plugins:
                self.load_external()
        except BaseException:
            self._unload_core_sync()
            raise
        self._started = True
        return self

    async def astart(self) -> "ApplicationHost":
        if self._started:
            return self
        try:
            await self._load_core_async()
            if self.discover_plugins:
                await self.aload_external()
        except BaseException:
            await self._unload_core_async()
            raise
        self._started = True
        return self

    @staticmethod
    def _core_names() -> tuple[str, ...]:
        return (
            "core.runtime",
            "model_catalog",
            "context_router",
            "core.tool_history_shadow",
            "core.tools",
            "core.commands",
            "core.catalogs",
            "core.hooks",
            "core.providers",
            "core.scheduler",
            "core.cache",
            "core.experts",
            "core.factories",
        )

    def _load_core_sync(self) -> None:
        for name in self._core_names():
            self.context.load(name)

    async def _load_core_async(self) -> None:
        for name in self._core_names():
            await self.context.aload(name)

    def _unload_core_sync(self) -> None:
        for name in reversed(self._core_names()):
            try:
                self.context.unload(name)
            except Exception:
                log.exception("failed to roll back core plugin %s", name)

    async def _unload_core_async(self) -> None:
        for name in reversed(self._core_names()):
            try:
                await self.context.aunload(name)
            except Exception:
                log.exception("failed to roll back async core plugin %s", name)

    def load_external(self) -> list[str]:
        loaded: list[str] = []
        packages = self.catalog.discover()
        registered: dict[str, PluginSpec] = {}
        for package in packages:
            spec = _ensure_runtime_dependency(package.spec)
            try:
                self.context.register_plugin(spec, replace=True)
                registered[package.name] = spec
                self._external[package.name] = replace(package, spec=spec)
            except Exception as exc:
                log.error("plugin %s failed to load: %s", package.name, exc)
        for package in packages:
            spec = registered.get(package.name)
            if spec is None:
                continue
            try:
                self.context.load(spec.name)
                loaded.append(package.name)
            except Exception as exc:
                log.error("plugin %s failed to load: %s", package.name, exc)
        return loaded

    async def aload_external(self) -> list[str]:
        loaded: list[str] = []
        packages = self.catalog.discover()
        registered: dict[str, PluginSpec] = {}
        for package in packages:
            spec = _ensure_runtime_dependency(package.spec)
            try:
                self.context.register_plugin(spec, replace=True)
                registered[package.name] = spec
                self._external[package.name] = replace(package, spec=spec)
            except Exception as exc:
                log.error("plugin %s failed to load: %s", package.name, exc)
        for package in packages:
            spec = registered.get(package.name)
            if spec is None:
                continue
            try:
                await self.context.aload(spec.name)
                loaded.append(package.name)
            except Exception as exc:
                log.error("plugin %s failed to load: %s", package.name, exc)
        return loaded

    def bind(self, name: str, value: Any) -> Disposer:
        """Publish a changing runtime object through a disposable plugin."""

        plugin_name = self._bindings.get(name, f"runtime.binding.{name}")
        if _has_plugin(self.context, plugin_name) and self.context.plugin_state(plugin_name) in {
            PluginState.ACTIVE,
            PluginState.WAITING,
        }:
            self.context.unload(plugin_name)
        spec = PluginSpec(
            plugin_name,
            lambda ctx: ctx.provide(name, value, replace=True),
            requires=("core.runtime",),
            version="1",
            purpose=f"Runtime binding for {name}",
            source="runtime",
        )
        self.context.register_plugin(spec, replace=True)
        self.context.load(spec.name)
        self._bindings[name] = spec.name
        run = self.context.plugin_run(spec.name)
        run_id = run.run_id if run is not None else ""
        return Disposer(lambda: self._dispose_binding(name, spec.name, run_id))

    async def abind(self, name: str, value: Any) -> Disposer:
        async with self._binding_lock:
            plugin_name = self._bindings.get(name, f"runtime.binding.{name}")
            if _has_plugin(self.context, plugin_name) and self.context.plugin_state(plugin_name) in {
                PluginState.ACTIVE,
                PluginState.WAITING,
            }:
                await self.context.aunload(plugin_name)
            spec = PluginSpec(
                plugin_name,
                lambda ctx: ctx.provide(name, value, replace=True),
                requires=("core.runtime",),
                version="1",
                purpose=f"Runtime binding for {name}",
                source="runtime",
            )
            self.context.register_plugin(spec, replace=True)
            await self.context.aload(spec.name)
            self._bindings[name] = spec.name
            run = self.context.plugin_run(spec.name)
            run_id = run.run_id if run is not None else ""
            return Disposer(lambda: self._adispose_binding(name, spec.name, run_id))

    def _dispose_binding(self, name: str, plugin_name: str, run_id: str) -> None:
        current = self.context.plugin_run(plugin_name)
        if current is None or current.run_id != run_id:
            return
        if current.state in {PluginState.ACTIVE, PluginState.WAITING}:
            self.context.unload(plugin_name)
            if self._bindings.get(name) == plugin_name:
                self._bindings.pop(name, None)

    async def _adispose_binding(self, name: str, plugin_name: str, run_id: str) -> None:
        async with self._binding_lock:
            current = self.context.plugin_run(plugin_name)
            if current is None or current.run_id != run_id:
                return
            if current.state in {PluginState.ACTIVE, PluginState.WAITING}:
                await self.context.aunload(plugin_name)
                if self._bindings.get(name) == plugin_name:
                    self._bindings.pop(name, None)

    def reconfigure_work_dir(self, work_dir: str) -> "ApplicationHost":
        """Switch project-scoped catalogs and external packages in place."""

        resolved = str(Path(work_dir).resolve())
        if resolved == self.work_dir:
            return self
        self._unload_external_sync()

        user_dir = self.catalog.user_dir
        self.work_dir = resolved
        self.catalog = PluginCatalog(resolved, user_dir=user_dir)
        self.context.unload("core.catalogs")
        self.context.load("core.catalogs")
        self.bind("work_dir", resolved)
        if self.discover_plugins:
            self.load_external()
        return self

    async def areconfigure_work_dir(self, work_dir: str) -> "ApplicationHost":
        """Await every project-scoped cleanup before loading the next graph."""

        resolved = str(Path(work_dir).resolve())
        async with self._reconfigure_lock:
            if resolved == self.work_dir:
                return self
            await self._unload_external_async()

            user_dir = self.catalog.user_dir
            self.work_dir = resolved
            self.catalog = PluginCatalog(resolved, user_dir=user_dir)
            await self.context.aunload("core.catalogs")
            await self.context.aload("core.catalogs")
            await self.abind("work_dir", resolved)
            if self.discover_plugins:
                await self.aload_external()
        return self

    def _unload_external_sync(self) -> None:
        for name in reversed(list(self._external)):
            try:
                self.context.unload(name)
            except Exception:
                log.exception("failed to unload project plugin %s", name)
            try:
                self.context.unregister_plugin(name)
            except Exception:
                log.exception("failed to unregister project plugin %s", name)
        self._external.clear()

    async def _unload_external_async(self) -> None:
        for name in reversed(list(self._external)):
            try:
                await self.context.aunload(name)
            except Exception:
                log.exception("failed to unload async project plugin %s", name)
            try:
                self.context.unregister_plugin(name)
            except Exception:
                log.exception("failed to unregister project plugin %s", name)
        self._external.clear()

    def snapshot(self) -> dict[str, Any]:
        return {
            "plugins": self.context.inspect_plugins(),
            "slots": self.context.slots.inspect(),
            "catalog_errors": [
                {"path": str(error.path), "message": error.message}
                for error in self.catalog.errors
            ],
        }

    def frontend_manifest(self) -> list[dict[str, Any]]:
        values = self.context.slots.resolve("web.assets") or []
        return [dict(value) for value in values if isinstance(value, dict)]

    def resolve_plugin_asset(self, plugin_name: str, relative: str) -> Path:
        package = self._external.get(plugin_name)
        if package is None or package.frontend is None:
            raise KeyError(plugin_name)
        normalized = relative.replace("\\", "/").lstrip("/")
        declared = {
            *package.frontend.get("scripts", []),
            *package.frontend.get("styles", []),
            *package.frontend.get("assets", []),
        }
        if normalized not in declared:
            raise KeyError(normalized)
        path = (package.root / normalized).resolve()
        if package.root.resolve() not in path.parents or not path.is_file():
            raise KeyError(normalized)
        return path

    def close(self) -> None:
        self.context.close()
        self._external.clear()
        self._bindings.clear()
        self._started = False

    async def aclose(self) -> None:
        await self.context.aclose()
        self._external.clear()
        self._bindings.clear()
        self._started = False


def _ensure_runtime_dependency(spec: PluginSpec) -> PluginSpec:
    if spec.name.startswith("core.") or "core.runtime" in spec.requires:
        return spec
    return replace(spec, requires=("core.runtime", *spec.requires))


def _paths(values: list[Path] | None) -> list[Path]:
    return [Path(value) for value in values or []]


def _has_plugin(context: Context, name: str) -> bool:
    return any(row["name"] == name for row in context.inspect_plugins())


__all__ = ["ApplicationHost"]
