"""Cordis-inspired plugin runtime used by every MewCode surface.

The host deliberately knows nothing about tools, agents, HTTP routes, or UI.
Plugins publish those capabilities as services and slot entries. Every effect
is owned by one activation, so a failed or stopped plugin leaves no partial
registration behind.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Generic, TypeVar

log = logging.getLogger(__name__)
T = TypeVar("T")
MISSING = object()


class Disposer:
    """Idempotent synchronous and asynchronous cleanup handle."""

    def __init__(
        self,
        cleanup: Callable[[], Any],
        *,
        scheduler: Callable[[Any], None] | None = None,
    ) -> None:
        self._cleanup = cleanup
        self._scheduler = scheduler
        self._disposed = False

    @property
    def disposed(self) -> bool:
        return self._disposed

    def _take_cleanup(self) -> Callable[[], Any] | None:
        if self._disposed:
            return None
        self._disposed = True
        cleanup = self._cleanup
        self._cleanup = lambda: None
        return cleanup

    def dispose(self) -> None:
        cleanup = self._take_cleanup()
        if cleanup is None:
            return
        result = cleanup()
        if not inspect.isawaitable(result):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(result)
        else:
            if self._scheduler is not None:
                self._scheduler(result)
            else:
                loop.create_task(result)

    async def adispose(self) -> None:
        cleanup = self._take_cleanup()
        if cleanup is None:
            return
        result = cleanup()
        if inspect.isawaitable(result):
            await result

    __call__ = dispose


def _disposer(
    value: Any,
    *,
    scheduler: Callable[[Any], None] | None = None,
) -> Disposer:
    if isinstance(value, Disposer):
        if scheduler is not None and value._scheduler is None:
            value._scheduler = scheduler
        return value
    if value is None:
        return Disposer(lambda: None, scheduler=scheduler)
    if callable(value):
        return Disposer(value, scheduler=scheduler)
    raise TypeError("plugin cleanup must be callable or None")


@dataclass(frozen=True)
class ServiceDefinition(Generic[T]):
    name: str
    factory: Callable[["Context"], T] | None = None


@dataclass(frozen=True)
class ServiceProvider(Generic[T]):
    definition: ServiceDefinition[T]
    value: T

    def install(self, ctx: "Context") -> Disposer:
        return ctx.provide(self.definition, self.value)


@dataclass(frozen=True)
class ServiceConsumer(Generic[T]):
    definition: ServiceDefinition[T]

    def bind(self, ctx: "Context", callback: Callable[[T], Any]) -> Disposer:
        return ctx.consume(self.definition, callback)


class PluginState(str, Enum):
    REGISTERED = "registered"
    LOADING = "loading"
    WAITING = "waiting"
    ACTIVE = "active"
    FAILED = "failed"
    UNLOADING = "unloading"
    UNLOADED = "unloaded"


@dataclass(frozen=True)
class PluginSpec:
    """Immutable plugin package definition.

    ``requires`` names hard plugin dependencies. ``inject`` names services the
    plugin may legally wait for, matching Cordis' parked-fiber semantics.
    """

    name: str
    apply_fn: Callable[["Context"], Any]
    requires: tuple[str, ...] = ()
    inject: tuple[str, ...] = ()
    version: str = "0"
    purpose: str = ""
    source: str = "builtin"

    @property
    def package_id(self) -> str:
        return f"{self.name}@{self.version}"

    def apply(self, ctx: "Context") -> Any:
        return self.apply_fn(ctx)


@dataclass
class PluginRun:
    """One exact activation and its retained diagnostic state."""

    run_id: str
    plugin_name: str
    package_id: str
    state: PluginState = PluginState.REGISTERED
    waiting_for: tuple[str, ...] = ()
    error: str | None = None
    started_at: float = field(default_factory=time.monotonic)
    activated_at: float | None = None
    stopped_at: float | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "plugin": self.plugin_name,
            "package_id": self.package_id,
            "state": self.state.value,
            "waiting_for": list(self.waiting_for),
            "error": self.error,
            "started_at": self.started_at,
            "activated_at": self.activated_at,
            "stopped_at": self.stopped_at,
        }


def plugin(
    name: str,
    *,
    requires: tuple[str, ...] = (),
    inject: tuple[str, ...] = (),
    version: str = "0",
    purpose: str = "",
    source: str = "builtin",
):
    """Decorate a function into an immutable loadable plugin definition."""

    def decorate(fn: Callable[["Context"], Any]) -> PluginSpec:
        return PluginSpec(
            name=name,
            apply_fn=fn,
            requires=requires,
            inject=inject,
            version=version,
            purpose=purpose,
            source=source,
        )

    return decorate


@dataclass
class _Listener:
    token: str
    callback: Callable[[Any], Any]
    priority: int


@dataclass(frozen=True)
class _PluginOwner:
    name: str
    run_id: str


class EventBus:
    def __init__(
        self,
        bind_callback: Callable[[Callable[..., Any]], Callable[..., Any]] | None = None,
    ) -> None:
        self._listeners: dict[str, list[_Listener]] = {}
        self._bind_callback = bind_callback or (lambda callback: callback)

    def on(
        self, event: str, callback: Callable[[Any], Any], *, priority: int = 0
    ) -> Disposer:
        listener = _Listener(
            uuid.uuid4().hex,
            self._bind_callback(callback),
            priority,
        )
        self._listeners.setdefault(event, []).append(listener)
        self._listeners[event].sort(key=lambda item: item.priority, reverse=True)

        def remove() -> None:
            listeners = self._listeners.get(event, [])
            self._listeners[event] = [
                item for item in listeners if item.token != listener.token
            ]

        return Disposer(remove)

    async def emit(self, event: str, payload: Any = None) -> list[Any]:
        results: list[Any] = []
        for listener in list(self._listeners.get(event, [])):
            value = listener.callback(payload)
            if inspect.isawaitable(value):
                value = await value
            results.append(value)
        return results

    async def waterfall(self, event: str, value: T) -> T:
        current = value
        for listener in list(self._listeners.get(event, [])):
            result = listener.callback(current)
            if inspect.isawaitable(result):
                result = await result
            if result is not None:
                current = result
        return current


@dataclass
class _SlotEntry:
    token: str
    plugin: str
    value: Any
    key: str | None
    priority: int
    selector: Callable[[Any], bool] | None


@dataclass
class _SlotInjection:
    callback: Callable[[Any], Any]
    owner: Any = None
    item_key: str | None = None
    source: "SlotRegistry | None" = None


@dataclass
class _Slot:
    key: str
    kind: str
    owner: str
    renderer: Any = None
    entries: list[_SlotEntry] = field(default_factory=list)
    injections: dict[str, _SlotInjection] = field(default_factory=dict)


class SlotRegistry:
    """Dynamic single/list/keyed/chain slots with lifecycle-aware injection."""

    VALID_KINDS = {"single", "list", "keyed", "chain"}

    def __init__(self, ctx: "Context", parent: "SlotRegistry | None" = None) -> None:
        self._ctx = ctx
        self._parent = parent
        self._slots: dict[str, _Slot] = {}
        self._pending_injections: dict[str, dict[str, _SlotInjection]] = {}

    def _registries(self) -> list["SlotRegistry"]:
        registries: list[SlotRegistry] = []
        current: SlotRegistry | None = self
        while current is not None:
            registries.append(current)
            current = current._parent
        return registries

    def _descendants(self) -> list["SlotRegistry"]:
        registries: list[SlotRegistry] = []
        for child in list(self._ctx._children):
            registries.append(child.slots)
            registries.extend(child.slots._descendants())
        return registries

    def _visible(self, key: str) -> _Slot | None:
        if key in self._slots:
            return self._slots[key]
        return self._parent._visible(key) if self._parent is not None else None

    def declare(
        self,
        key: str,
        *,
        kind: str = "single",
        owner: str = "root",
        renderer: Any = None,
    ) -> _Slot:
        if kind not in self.VALID_KINDS:
            raise ValueError(f"unsupported slot kind: {kind}")
        existing = self._slots.get(key)
        if existing is not None:
            if existing.kind != kind or existing.owner != owner:
                raise ValueError(f"slot {key!r} is already declared differently")
            if renderer is not None:
                existing.renderer = renderer
            return existing
        inherited = self._parent._visible(key) if self._parent is not None else None
        if inherited is not None and inherited.kind != kind:
            raise ValueError(f"slot {key!r} conflicts with its parent scope")
        slot = _Slot(key=key, kind=kind, owner=owner, renderer=renderer)
        self._slots[key] = slot
        slot.injections.update(self._pending_injections.pop(key, {}))
        adopted: list[_SlotInjection] = []
        for descendant in self._descendants():
            pending = descendant._pending_injections.pop(key, {})
            slot.injections.update(pending)
            adopted.extend(pending.values())
        for injection in adopted:
            self._invoke_injection(
                injection.callback,
                self._resolve(slot, injection.owner, injection.item_key),
            )
        if self._ctx.active_plugin is not None:
            self._ctx.effect(lambda: self._undeclare(key, slot))
        return slot

    def _undeclare(self, key: str, slot: _Slot) -> None:
        if self._slots.get(key) is not slot:
            return
        if slot.entries:
            if not self._ctx._closed:
                raise RuntimeError(f"cannot undeclare occupied slot {key!r}")
            # Late runtime registrations belong to the scope, not necessarily
            # to the declaring plugin. Scope shutdown owns all remaining
            # entries; their idempotent disposers still run in normal cleanup.
            slot.entries.clear()
        self._slots.pop(key, None)
        if slot.injections:
            for token, injection in slot.injections.items():
                source = injection.source or self
                source._pending_injections.setdefault(key, {})[token] = injection
            for injection in slot.injections.values():
                self._invoke_injection(injection.callback, None)

    def register(
        self,
        key: str,
        value: Any,
        *,
        plugin: str | None = None,
        kind: str | None = None,
        item_key: str | None = None,
        priority: int = 0,
        select: Callable[[Any], bool] | None = None,
        renderer: Any = None,
    ) -> Disposer:
        registrant = plugin or self._ctx.active_plugin or "anonymous"
        slot = self._slots.get(key)
        if slot is None:
            inherited = self._parent._visible(key) if self._parent is not None else None
            if inherited is not None:
                slot = inherited
            else:
                slot = self.declare(
                    key,
                    kind=kind or "single",
                    owner=registrant,
                    renderer=renderer,
                )
        if kind is not None and slot.kind != kind:
            raise ValueError(f"slot {key!r} expects {slot.kind}, got {kind}")
        if slot.kind == "single" and slot.entries:
            raise ValueError(f"single slot {key!r} already has a provider")
        if slot.kind == "keyed" and not item_key:
            raise ValueError(f"keyed slot {key!r} requires item_key")
        if slot.kind == "keyed" and any(
            entry.key == item_key for entry in slot.entries
        ):
            raise ValueError(f"key {item_key!r} is already registered in slot {key!r}")
        entry = _SlotEntry(
            token=uuid.uuid4().hex,
            plugin=registrant,
            value=value,
            key=item_key,
            priority=priority,
            selector=select,
        )
        slot.entries.append(entry)
        self._notify(slot)
        return self._ctx.effect(Disposer(lambda: self._remove(slot, entry.token)))

    def _remove(self, slot: _Slot, token: str) -> None:
        slot.entries = [entry for entry in slot.entries if entry.token != token]
        self._notify(slot)

    @staticmethod
    def _ordered(slot: _Slot) -> list[_SlotEntry]:
        return sorted(slot.entries, key=lambda item: item.priority, reverse=True)

    def _resolve(
        self, slot: _Slot, owner: Any = None, item_key: str | None = None
    ) -> Any:
        entries = self._ordered(slot)
        if slot.kind == "list":
            return [entry.value for entry in entries]
        if slot.kind == "keyed":
            if item_key is None:
                return {entry.key: entry.value for entry in entries}
            entry = next((entry for entry in entries if entry.key == item_key), None)
            return entry.value if entry is not None else None
        if slot.kind == "chain":
            entry = next(
                (
                    entry
                    for entry in entries
                    if entry.selector is None or entry.selector(owner)
                ),
                None,
            )
            return entry.value if entry is not None else None
        return entries[0].value if entries else None

    def resolve(self, key: str, owner: Any = None, *, item_key: str | None = None) -> Any:
        slot = self._visible(key)
        if slot is None:
            return None
        return self._resolve(slot, owner, item_key)

    def inject(
        self,
        key: str,
        callback: Callable[[Any], Any],
        *,
        owner: Any = None,
        item_key: str | None = None,
    ) -> Disposer:
        slot = self._visible(key)
        token = uuid.uuid4().hex
        callback = self._ctx._bind_callback(callback)
        injection = _SlotInjection(callback, owner, item_key, source=self)
        if slot is None:
            self._pending_injections.setdefault(key, {})[token] = injection
        else:
            slot.injections[token] = injection
            self._invoke_injection(callback, self._resolve(slot, owner, item_key))

        def remove() -> None:
            # The visible slot may belong to a parent scope and can be
            # undeclared before this child disposer runs. Search the complete
            # scope chain so an injection cannot survive its owning Context.
            for registry in self._registries():
                registry._pending_injections.get(key, {}).pop(token, None)
                slot = registry._slots.get(key)
                if slot is not None:
                    slot.injections.pop(token, None)

        return self._ctx.effect(Disposer(remove))

    def _notify(self, slot: _Slot) -> None:
        for injection in list(slot.injections.values()):
            self._invoke_injection(
                injection.callback,
                self._resolve(slot, injection.owner, injection.item_key),
            )

    def _invoke_injection(self, callback: Callable[[Any], Any], value: Any) -> None:
        self._ctx._dispatch_callback(callback, value, label="slot injection")

    def renderer(self, key: str, *, owner: str) -> Any:
        slot = self._visible(key)
        if slot is None:
            raise KeyError(key)
        if slot.owner != owner:
            raise PermissionError(f"plugin {owner!r} cannot render slot {key!r}")
        return slot.renderer

    def inspect(self) -> list[dict[str, Any]]:
        return [
            {
                "key": slot.key,
                "kind": slot.kind,
                "owner": slot.owner,
                "occupants": [entry.plugin for entry in self._ordered(slot)],
            }
            for slot in self._slots.values()
        ]


class Context:
    """Plugin host, service container, event bus, and scope boundary."""

    def __init__(self, name: str = "root", *, parent: "Context | None" = None) -> None:
        self.name = name
        self.parent = parent
        self._children: list[Context] = []
        if parent is not None:
            parent._children.append(self)
        self._active_owner: ContextVar[_PluginOwner | None] = ContextVar(
            f"mewcode_plugin_owner_{id(self):x}",
            default=None,
        )
        self.events = EventBus(self._bind_callback)
        self.slots = SlotRegistry(self, parent.slots if parent is not None else None)
        if parent is None:
            self.slots.declare("root", owner="system")
        self._services: dict[str, Any] = {}
        self._definitions: dict[str, ServiceDefinition[Any]] = {}
        self._effects: list[Disposer] = []
        self._plugin_effects: dict[str, list[Disposer]] = {}
        self._consumers: dict[str, dict[str, Callable[[Any], Any]]] = {}
        self._plugins: dict[str, PluginSpec] = {}
        self._runs: dict[str, PluginRun] = {}
        self._run_history: list[PluginRun] = []
        self._loading: set[str] = set()
        self._operation_lock = asyncio.Lock()
        self._async_depth = 0
        self._resuming_async = False
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._closed = False

    @property
    def active_plugin(self) -> str | None:
        owner = self._active_owner.get()
        return owner.name if owner is not None else None

    def scope(self, name: str) -> "Context":
        return Context(name, parent=self)

    @property
    def tools(self) -> Any:
        return self.get("tools", None)

    @property
    def llm(self) -> Any:
        return self.get("llm", None)

    @property
    def sessions(self) -> Any:
        return self.get("sessions", None)

    @property
    def agents(self) -> Any:
        return self.get("agents", None)

    @property
    def commands(self) -> Any:
        return self.get("commands", None)

    @property
    def skills(self) -> Any:
        return self.get("skills", None)

    @property
    def hooks(self) -> Any:
        return self.get("hooks", None)

    def effect(self, cleanup: Any) -> Disposer:
        disposer = _disposer(cleanup, scheduler=self._schedule_cleanup)
        owner = self._active_owner.get()
        if self._closed or (owner is not None and not self._owner_is_live(owner)):
            disposer.dispose()
            return disposer
        if disposer not in self._effects:
            self._effects.append(disposer)
        if owner is not None:
            effects = self._plugin_effects.setdefault(owner.name, [])
            if disposer not in effects:
                effects.append(disposer)
        return disposer

    def _schedule_cleanup(self, awaitable: Any) -> None:
        self._schedule_awaitable(awaitable, label="plugin cleanup")

    def _owner_is_live(self, owner: _PluginOwner) -> bool:
        run = self._runs.get(owner.name)
        return bool(
            run is not None
            and run.run_id == owner.run_id
            and run.state
            in {PluginState.LOADING, PluginState.WAITING, PluginState.ACTIVE}
        )

    def _bind_callback(self, callback: Callable[..., Any]) -> Callable[..., Any]:
        """Keep callbacks in the activation scope that registered them."""

        owner = self._active_owner.get()
        if owner is None:
            return callback

        def owned(*args: Any, **kwargs: Any) -> Any:
            token = self._active_owner.set(owner)
            try:
                result = callback(*args, **kwargs)
            finally:
                self._active_owner.reset(token)
            if not inspect.isawaitable(result):
                return result

            async def await_owned() -> Any:
                async_token = self._active_owner.set(owner)
                try:
                    return await result
                finally:
                    self._active_owner.reset(async_token)

            return await_owned()

        return owned

    def _dispatch_callback(
        self,
        callback: Callable[[Any], Any],
        value: Any,
        *,
        label: str,
    ) -> None:
        try:
            result = callback(value)
        except Exception:
            log.exception("%s failed", label)
            return
        if inspect.isawaitable(result):
            self._schedule_awaitable(result, label=label)

    def _schedule_awaitable(self, awaitable: Any, *, label: str) -> None:
        if self._closed:
            self._discard_awaitable(awaitable)
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            try:
                asyncio.run(awaitable)
            except Exception:
                log.exception("%s failed", label)
            return
        task = asyncio.ensure_future(awaitable)
        self._background_tasks.add(task)

        def consume(completed: asyncio.Task[Any]) -> None:
            self._background_tasks.discard(completed)
            try:
                completed.result()
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("%s failed", label)

        task.add_done_callback(consume)

    @staticmethod
    def _discard_awaitable(awaitable: Any) -> None:
        if isinstance(awaitable, asyncio.Future):
            awaitable.cancel()
            return
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()

    def on(self, event: str, callback: Callable[[Any], Any], *, priority: int = 0) -> Disposer:
        return self.effect(self.events.on(event, callback, priority=priority))

    def define(self, definition: ServiceDefinition[T]) -> ServiceDefinition[T]:
        self._definitions[definition.name] = definition
        return definition

    def provide(
        self,
        definition: ServiceDefinition[T] | str,
        value: T,
        *,
        replace: bool = False,
    ) -> Disposer:
        name = definition.name if isinstance(definition, ServiceDefinition) else definition
        if isinstance(definition, ServiceDefinition):
            self.define(definition)
        if name in self._services and not replace:
            raise ValueError(f"service {name!r} already provided")
        previous = self._services.get(name, MISSING)
        self._services[name] = value
        self._notify_service(name, value)
        self._resume_waiting()

        def remove() -> None:
            if self._services.get(name) is not value:
                return
            if previous is MISSING:
                self._services.pop(name, None)
                restored = self.parent.get(name, None) if self.parent is not None else None
            else:
                self._services[name] = previous
                restored = previous
            self._notify_service(name, restored)

        return self.effect(Disposer(remove))

    def get(self, definition: ServiceDefinition[T] | str, default: Any = MISSING) -> T:
        name = definition.name if isinstance(definition, ServiceDefinition) else definition
        if name in self._services:
            return self._services[name]
        if self.parent is not None:
            return self.parent.get(definition, default)
        service_definition = self._definitions.get(name)
        if service_definition is not None and service_definition.factory is not None:
            value = service_definition.factory(self)
            self.provide(service_definition, value)
            return value
        if default is not MISSING:
            return default
        raise KeyError(name)

    def consume(
        self,
        definition: ServiceDefinition[T] | str,
        callback: Callable[[T], Any],
    ) -> Disposer:
        name = definition.name if isinstance(definition, ServiceDefinition) else definition
        token = uuid.uuid4().hex
        callback = self._bind_callback(callback)
        self._consumers.setdefault(name, {})[token] = callback
        self._invoke_consumer(callback, self.get(name, None))

        def remove() -> None:
            self._consumers.get(name, {}).pop(token, None)

        return self.effect(Disposer(remove))

    def _notify_service(self, name: str, value: Any) -> None:
        for callback in list(self._consumers.get(name, {}).values()):
            self._invoke_consumer(callback, value)
        for child in list(self._children):
            if name not in child._services:
                child._notify_service(name, value)

    def _invoke_consumer(self, callback: Callable[[Any], Any], value: Any) -> None:
        self._dispatch_callback(callback, value, label="service consumer")

    def register_plugin(self, spec: PluginSpec, *, replace: bool = False) -> PluginSpec:
        existing = self._plugins.get(spec.name)
        if existing is not None and existing != spec and not replace:
            raise ValueError(f"plugin {spec.name!r} is already registered")
        run = self._runs.get(spec.name)
        if existing is not None and run is not None and run.state in {
            PluginState.LOADING,
            PluginState.WAITING,
            PluginState.ACTIVE,
        }:
            raise RuntimeError(f"cannot replace running plugin {spec.name!r}")
        self._plugins[spec.name] = spec
        return spec

    def unregister_plugin(self, name: str) -> None:
        run = self._runs.get(name)
        if run is not None and run.state in {
            PluginState.LOADING,
            PluginState.WAITING,
            PluginState.ACTIVE,
        }:
            raise RuntimeError(f"cannot unregister running plugin {name!r}")
        self._plugins.pop(name, None)

    def _resolve_spec(self, spec: PluginSpec | str) -> PluginSpec:
        if isinstance(spec, PluginSpec):
            registered = self._plugins.get(spec.name)
            if registered is None:
                self.register_plugin(spec)
                return spec
            return registered
        try:
            return self._plugins[spec]
        except KeyError as exc:
            raise KeyError(f"plugin {spec!r} is not registered") from exc

    def _new_run(self, spec: PluginSpec) -> PluginRun:
        run = PluginRun(
            run_id=f"run-{uuid.uuid4().hex[:12]}",
            plugin_name=spec.name,
            package_id=spec.package_id,
            state=PluginState.LOADING,
        )
        self._runs[spec.name] = run
        self._run_history.append(run)
        return run

    def _missing(self, spec: PluginSpec) -> tuple[str, ...]:
        missing: list[str] = []
        for name in spec.inject:
            try:
                self.get(name)
            except KeyError:
                missing.append(name)
        for name in spec.requires:
            try:
                state = self.plugin_state(name)
            except KeyError:
                state = PluginState.REGISTERED
            if state != PluginState.ACTIVE:
                missing.append(f"plugin:{name}")
        return tuple(missing)

    def _prepare_load(self, spec: PluginSpec) -> PluginRun | None:
        if self._closed:
            raise RuntimeError("context is closed")
        if spec.name in self._loading:
            raise RuntimeError(f"cyclic plugin dependency at {spec.name!r}")
        current = self._runs.get(spec.name)
        if current is not None and current.state in {
            PluginState.LOADING,
            PluginState.WAITING,
            PluginState.ACTIVE,
        }:
            return None
        return self._new_run(spec)

    def _park_if_missing(self, spec: PluginSpec, run: PluginRun) -> bool:
        missing = self._missing(spec)
        if not missing:
            return False
        run.state = PluginState.WAITING
        run.waiting_for = missing
        self._plugin_effects.setdefault(spec.name, [])
        return True

    def _begin_apply(self, spec: PluginSpec, run: PluginRun) -> Any:
        self._plugin_effects.setdefault(spec.name, [])
        run.state = PluginState.LOADING
        run.waiting_for = ()
        return self._active_owner.set(_PluginOwner(spec.name, run.run_id))

    @staticmethod
    def _finish_apply(run: PluginRun) -> None:
        run.state = PluginState.ACTIVE
        run.error = None
        run.activated_at = time.monotonic()

    def _fail_apply(self, spec: PluginSpec, run: PluginRun, error: BaseException) -> None:
        effects = self._plugin_effects.pop(spec.name, [])
        cleanup_errors = self._dispose_effects(effects)
        run.state = PluginState.FAILED
        run.error = f"{type(error).__name__}: {error}"
        if cleanup_errors:
            run.error += f"; cleanup errors: {len(cleanup_errors)}"
        run.stopped_at = time.monotonic()

    @staticmethod
    def _dispose_effects(effects: list[Disposer]) -> list[BaseException]:
        errors: list[BaseException] = []
        for disposer in reversed(effects):
            try:
                disposer.dispose()
            except BaseException as exc:
                errors.append(exc)
                log.exception("plugin cleanup failed")
        return errors

    @staticmethod
    async def _adispose_effects(effects: list[Disposer]) -> list[BaseException]:
        errors: list[BaseException] = []
        for disposer in reversed(effects):
            try:
                await disposer.adispose()
            except BaseException as exc:
                errors.append(exc)
                log.exception("async plugin cleanup failed")
        return errors

    def load(self, spec: PluginSpec | str) -> Disposer:
        """Load a synchronous plugin; async callers should use ``aload``."""

        resolved = self._resolve_spec(spec)
        run = self._prepare_load(resolved)
        if run is None:
            return Disposer(lambda: self.unload(resolved.name))
        self._loading.add(resolved.name)
        try:
            try:
                for required in resolved.requires:
                    self.load(required)
                if self._park_if_missing(resolved, run):
                    return Disposer(lambda: self.unload(resolved.name))
                owner_token = self._begin_apply(resolved, run)
                try:
                    result = resolved.apply(self)
                    if inspect.isawaitable(result):
                        try:
                            asyncio.get_running_loop()
                        except RuntimeError:
                            result = asyncio.run(result)
                        else:
                            close = getattr(result, "close", None)
                            if callable(close):
                                close()
                            raise RuntimeError(
                                f"plugin {resolved.name!r} is asynchronous; use await ctx.aload()"
                            )
                    if result is not None:
                        self.effect(result)
                    self._finish_apply(run)
                finally:
                    self._active_owner.reset(owner_token)
            except BaseException as exc:
                if run.state != PluginState.FAILED:
                    self._fail_apply(resolved, run, exc)
                raise
        finally:
            self._loading.discard(resolved.name)
        self._resume_waiting()
        return Disposer(lambda: self.unload(resolved.name))

    async def aload(self, spec: PluginSpec | str) -> Disposer:
        """Serialize and await one plugin activation and its dependencies."""

        async with self._operation_lock:
            self._async_depth += 1
            try:
                resolved = self._resolve_spec(spec)
                await self._aload_unlocked(resolved)
            finally:
                self._async_depth -= 1
        return Disposer(lambda: self.aunload(resolved.name))

    async def _aload_unlocked(self, spec: PluginSpec) -> None:
        run = self._prepare_load(spec)
        if run is None:
            if self.plugin_state(spec.name) == PluginState.WAITING:
                await self._resume_one_async(spec)
            return
        self._loading.add(spec.name)
        try:
            try:
                for required in spec.requires:
                    await self._aload_unlocked(self._resolve_spec(required))
                if self._park_if_missing(spec, run):
                    return
                owner_token = self._begin_apply(spec, run)
                try:
                    result = spec.apply(self)
                    if inspect.isawaitable(result):
                        result = await result
                    if result is not None:
                        self.effect(result)
                    self._finish_apply(run)
                finally:
                    self._active_owner.reset(owner_token)
            except BaseException as exc:
                if run.state != PluginState.FAILED:
                    await self._afail_apply(spec, run, exc)
                raise
        finally:
            self._loading.discard(spec.name)
        await self._resume_waiting_async()

    async def _afail_apply(
        self, spec: PluginSpec, run: PluginRun, error: BaseException
    ) -> None:
        effects = self._plugin_effects.pop(spec.name, [])
        cleanup_errors = await self._adispose_effects(effects)
        run.state = PluginState.FAILED
        run.error = f"{type(error).__name__}: {error}"
        if cleanup_errors:
            run.error += f"; cleanup errors: {len(cleanup_errors)}"
        run.stopped_at = time.monotonic()

    def _resume_waiting(self) -> None:
        if self._async_depth or self._closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            self._schedule_awaitable(
                self._resume_waiting_serialized(),
                label="waiting plugin resume task",
            )
            return
        waiting = [
            (name, run)
            for name, run in self._runs.items()
            if run.state == PluginState.WAITING
        ]
        for name, run in waiting:
            spec = self._plugins.get(name)
            if spec is None or self._missing(spec):
                continue
            try:
                self._resume_one_sync(spec, run)
            except Exception:
                log.exception("waiting plugin %s failed to activate", name)

    async def _resume_waiting_serialized(self) -> None:
        async with self._operation_lock:
            await self._resume_waiting_async()

    def _resume_one_sync(self, spec: PluginSpec, run: PluginRun) -> None:
        owner_token = self._begin_apply(spec, run)
        try:
            result = spec.apply(self)
            if inspect.isawaitable(result):
                close = getattr(result, "close", None)
                if callable(close):
                    close()
                run.state = PluginState.WAITING
                run.waiting_for = ("async-activation",)
                return
            if result is not None:
                self.effect(result)
            self._finish_apply(run)
        except BaseException as exc:
            self._fail_apply(spec, run, exc)
            raise
        finally:
            self._active_owner.reset(owner_token)

    async def _resume_one_async(self, spec: PluginSpec) -> None:
        run = self._runs[spec.name]
        if run.state != PluginState.WAITING or self._missing(spec):
            return
        owner_token = self._begin_apply(spec, run)
        try:
            result = spec.apply(self)
            if inspect.isawaitable(result):
                result = await result
            if result is not None:
                self.effect(result)
            self._finish_apply(run)
        except BaseException as exc:
            await self._afail_apply(spec, run, exc)
            raise
        finally:
            self._active_owner.reset(owner_token)

    async def _resume_waiting_async(self) -> None:
        if self._closed or getattr(self, "_resuming_async", False):
            return
        self._resuming_async = True
        self._async_depth += 1
        made_progress = True
        try:
            while made_progress:
                made_progress = False
                for name, run in list(self._runs.items()):
                    spec = self._plugins.get(name)
                    if (
                        run.state != PluginState.WAITING
                        or spec is None
                        or self._missing(spec)
                    ):
                        continue
                    try:
                        await self._resume_one_async(spec)
                    except Exception:
                        # One parked plugin must not prevent unrelated parked
                        # plugins from receiving the same newly available service.
                        log.exception("waiting plugin %s failed to activate", name)
                    made_progress = True
        finally:
            self._async_depth -= 1
            self._resuming_async = False

    def unload(self, name: str) -> None:
        for dependent, spec in list(self._plugins.items()):
            if self.plugin_state(dependent) in {
                PluginState.ACTIVE,
                PluginState.WAITING,
            } and name in spec.requires:
                self.unload(dependent)
        run = self._runs.get(name)
        if run is None or run.state in {PluginState.UNLOADED, PluginState.REGISTERED}:
            return
        run.state = PluginState.UNLOADING
        effects = self._plugin_effects.pop(name, [])
        cleanup_errors = self._dispose_effects(effects)
        run.state = PluginState.UNLOADED
        if cleanup_errors:
            run.error = f"cleanup errors: {len(cleanup_errors)}"
        run.stopped_at = time.monotonic()
        run.waiting_for = ()

    async def aunload(self, name: str) -> None:
        async with self._operation_lock:
            await self._aunload_unlocked(name)

    async def _aunload_unlocked(self, name: str) -> None:
        for dependent, spec in list(self._plugins.items()):
            if self.plugin_state(dependent) in {
                PluginState.ACTIVE,
                PluginState.WAITING,
            } and name in spec.requires:
                await self._aunload_unlocked(dependent)
        run = self._runs.get(name)
        if run is None or run.state in {PluginState.UNLOADED, PluginState.REGISTERED}:
            return
        run.state = PluginState.UNLOADING
        effects = self._plugin_effects.pop(name, [])
        cleanup_errors = await self._adispose_effects(effects)
        run.state = PluginState.UNLOADED
        if cleanup_errors:
            run.error = f"cleanup errors: {len(cleanup_errors)}"
        run.stopped_at = time.monotonic()
        run.waiting_for = ()

    async def areload(self, name: str, spec: PluginSpec | None = None) -> Disposer:
        async with self._operation_lock:
            await self._aunload_unlocked(name)
            if spec is not None:
                self._plugins[name] = spec
            resolved = self._resolve_spec(name)
            await self._aload_unlocked(resolved)
        return Disposer(lambda: self.aunload(name))

    def plugin_state(self, name: str) -> PluginState:
        run = self._runs.get(name)
        if run is not None:
            return run.state
        if name in self._plugins:
            return PluginState.REGISTERED
        raise KeyError(name)

    def plugin_run(self, name: str) -> PluginRun | None:
        """Return the latest activation record for identity-safe disposers."""
        return self._runs.get(name)

    def plugin_spec(self, name: str) -> PluginSpec | None:
        return self._plugins.get(name)

    def inspect_plugins(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for name, spec in self._plugins.items():
            run = self._runs.get(name)
            rows.append(
                {
                    "name": name,
                    "version": spec.version,
                    "package_id": spec.package_id,
                    "purpose": spec.purpose,
                    "source": spec.source,
                    "requires": list(spec.requires),
                    "inject": list(spec.inject),
                    "state": (run.state if run else PluginState.REGISTERED).value,
                    "run": run.snapshot() if run else None,
                }
            )
        return rows

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for task in list(self._background_tasks):
            task.cancel()
        for child in reversed(list(self._children)):
            child.close()
        for name in reversed(list(self._runs)):
            self.unload(name)
        for disposer in reversed(self._effects):
            try:
                disposer.dispose()
            except BaseException:
                log.exception("context cleanup failed")
        self._effects.clear()
        if self.parent is not None:
            self.parent._children = [child for child in self.parent._children if child is not self]

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        current_task = asyncio.current_task()
        tasks = [
            task
            for task in self._background_tasks
            if task is not current_task and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for child in reversed(list(self._children)):
            await child.aclose()
        async with self._operation_lock:
            for name in reversed(list(self._runs)):
                await self._aunload_unlocked(name)
        await self._adispose_effects(self._effects)
        self._effects.clear()
        if self.parent is not None:
            self.parent._children = [child for child in self.parent._children if child is not self]


__all__ = [
    "Context",
    "Disposer",
    "EventBus",
    "PluginRun",
    "PluginSpec",
    "PluginState",
    "ServiceConsumer",
    "ServiceDefinition",
    "ServiceProvider",
    "SlotRegistry",
    "plugin",
]
