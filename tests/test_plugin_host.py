from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from mewcode.cordis import Context, PluginSpec, PluginState, plugin
from mewcode.plugins import ApplicationHost
from mewcode.plugins.fastapi import FastAPIInstallResult, fastapi_plugin
from mewcode.plugins.runtime import AgentRuntimeRequest
from mewcode.plugins.services import (
    AGENT_FACTORY,
    CACHE_PROVIDERS,
    CLIENT_FACTORY,
    CONTEXT_ROUTER,
    EXPERTS,
    MODEL_CATALOG,
    PROVIDERS,
    RUNTIME_SCHEDULER,
)


def test_application_host_owns_core_registries_and_dynamic_package(tmp_path: Path) -> None:
    package = tmp_path / ".mewcode" / "plugins" / "demo"
    skills = package / "skills" / "demo-skill"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: from plugin\n---\nPlugin SOP\n",
        encoding="utf-8",
    )
    (package / "plugin.py").write_text(
        """
from types import SimpleNamespace
from mewcode.cordis import PluginSpec

def apply(ctx):
    ctx.slots.register(
        'core.tools',
        SimpleNamespace(name='DemoTool', description='demo', get_schema=lambda: {
            'name': 'DemoTool', 'description': 'demo', 'input_schema': {}
        }),
        item_key='DemoTool',
    )

PLUGIN = PluginSpec('demo', apply, purpose='test package')
""",
        encoding="utf-8",
    )

    host = ApplicationHost(str(tmp_path)).start()
    try:
        snapshot = host.snapshot()
        states = {item["name"]: item["state"] for item in snapshot["plugins"]}
        assert states["core.tools"] == "active"
        assert states["demo"] == "active"
        assert host.context.tools.get("DemoTool") is not None
        assert host.context.skills.get("demo-skill") is not None

        host.context.unload("demo")
        assert host.context.tools.get("DemoTool") is None
        assert host.context.skills.get("demo-skill") is None
        assert host.context.plugin_state("demo") == PluginState.UNLOADED
    finally:
        host.close()


def test_external_frontend_assets_are_discovered_and_unloaded(tmp_path: Path) -> None:
    package = tmp_path / ".mewcode" / "plugins" / "ui-demo"
    ui = package / "ui"
    ui.mkdir(parents=True)
    (package / "plugin.toml").write_text(
        """
[plugin]
name = "ui-demo"
version = "1.2.0"

[plugin.frontend]
entry = "ui/index.js"
styles = ["ui/style.css"]
menus = [{ id = "demo", label = "Demo" }]
settings = [{ id = "demo.enabled", type = "boolean" }]
""",
        encoding="utf-8",
    )
    (ui / "index.js").write_text("window.demoLoaded = true;\n", encoding="utf-8")
    (ui / "style.css").write_text(".demo { color: black; }\n", encoding="utf-8")

    host = ApplicationHost(
        str(tmp_path),
        user_plugin_dir=tmp_path / "user",
    ).start()
    try:
        manifest = host.frontend_manifest()
        assert manifest == [{
            "plugin": "ui-demo",
            "version": "1.2.0",
            "scripts": ["ui/index.js"],
            "styles": ["ui/style.css"],
            "module": False,
            "menus": [{"plugin": "ui-demo", "id": "demo", "label": "Demo"}],
            "settings": [{
                "plugin": "ui-demo",
                "id": "demo.enabled",
                "type": "boolean",
            }],
        }]
        assert host.resolve_plugin_asset("ui-demo", "ui/index.js") == (
            ui / "index.js"
        ).resolve()
        host.context.unload("ui-demo")
        assert host.frontend_manifest() == []
    finally:
        host.close()


def test_application_host_provides_runtime_architecture_services(tmp_path: Path) -> None:
    host = ApplicationHost(str(tmp_path), discover_plugins=False).start()
    try:
        assert host.context.get(PROVIDERS) is not None
        assert host.context.get(CLIENT_FACTORY) is not None
        assert host.context.get(MODEL_CATALOG).describe("gpt-5.6-sol") is not None
        assert host.context.get(CONTEXT_ROUTER).resolve("claude-sonnet-4-6").name == (
            "anthropic-modern"
        )
        assert host.context.get(AGENT_FACTORY) is not None
        runtime_load = host.context.get(RUNTIME_SCHEDULER).snapshot()
        assert runtime_load["limit"] == 8
        assert runtime_load["kind_limits"] == {
            "model": 4,
            "tool": 4,
            "usage": 1,
        }
        assert host.context.get(CACHE_PROVIDERS).resolve("generic", "openai")
        assert "manager" in host.context.get(EXPERTS)
    finally:
        host.close()


@pytest.mark.parametrize("nested_host", [False, True])
def test_agent_runtime_owns_dependencies_tools_and_lifecycle_events(
    tmp_path: Path, nested_host: bool,
) -> None:
    from mewcode.config import ProviderConfig
    from mewcode.tools.base import StreamEnd, TextDelta, ToolCallComplete

    (tmp_path / "note.txt").write_text("runtime-owned", encoding="utf-8")

    class ScriptedClient:
        def __init__(self) -> None:
            self.model = "gpt-5.6-sol"
            self.base_url = "https://api.openai.com/v1"
            self.responses = [
                [
                    ToolCallComplete(
                        "read-1", "ReadFile", {"file_path": "note.txt"}
                    ),
                    StreamEnd("tool_use", input_tokens=4, output_tokens=2),
                ],
                [
                    TextDelta("done"),
                    StreamEnd("end_turn", input_tokens=6, output_tokens=1),
                ],
            ]
            self.usage_recorder = None

        def set_usage_recorder(self, recorder):
            self.usage_recorder = recorder

        async def stream(self, *_args, **_kwargs):
            for event in self.responses.pop(0):
                yield event

    async def scenario() -> None:
        root = ApplicationHost(str(tmp_path), discover_plugins=False)
        await root.astart()
        host = root
        if nested_host:
            host = ApplicationHost(str(tmp_path), context=root.context.scope("desktop"),
                                   discover_plugins=False)
            await host.astart()
        scheduler = root.context.get(RUNTIME_SCHEDULER)
        loads = []
        scheduler.set_event_callback(lambda event: loads.append(event["runtime_load"]))
        client = ScriptedClient()
        runtime = await host.context.get(AGENT_FACTORY).acreate(
            AgentRuntimeRequest(
                provider=ProviderConfig(
                    name="test",
                    protocol="openai-compat",
                    base_url="http://localhost",
                    model="test-model",
                ),
                work_dir=str(tmp_path),
                client=client,
                auxiliary_client=client,
                memory_manager=SimpleNamespace(load_always_on=lambda: ""),
            )
        )
        events: list[str] = []

        def observe(ctx: Context) -> None:
            for event in (
                "agent/session_start",
                "agent/turn_start",
                "agent/before_model",
                "agent/after_model",
                "agent/before_tool",
                "agent/after_tool",
                "agent/turn_end",
                "agent/session_end",
            ):
                ctx.on(event, lambda _payload, event=event: events.append(event))

        runtime.install(PluginSpec("runtime.observer", observe))
        assert runtime.agent.context_policy.name == "openai-gpt-5.6"
        assert runtime.registry is not host.context.tools
        assert runtime.registry.get("ReadFile") is not None
        assert await runtime.agent.run_to_completion(
            "read note.txt", runtime.conversation
        ) == "done"
        assert events.count("agent/before_model") == 2
        assert events.count("agent/after_model") == 2
        assert "agent/before_tool" in events
        assert "agent/after_tool" in events
        assert events[0] == "agent/session_start"
        assert events[-1] == "agent/session_end"
        assert runtime.agent.runtime_scheduler is scheduler
        clock = scheduler.agent_runtime(runtime.agent.agent_id)
        assert clock["started"] is True and clock["elapsed"] > 0
        assert clock["active"] is False
        assert any(load["running_by_kind"].get("model", 0) for load in loads)
        assert any(load["running_by_kind"].get("tool", 0) for load in loads)

        child = runtime.context
        await runtime.aclose()
        assert child._closed is True
        assert child not in host.context._children
        await host.aclose()
        if nested_host:
            await root.aclose()

    asyncio.run(scenario())


def test_deepseek_runtime_keeps_local_gateway_when_parent_tools_are_copied(
    tmp_path: Path,
) -> None:
    from mewcode.config import ProviderConfig

    class DeepSeekClient:
        model = "deepseek-flash"
        base_url = "https://api.deepseek.com"

        def set_usage_recorder(self, recorder) -> None:
            self.usage_recorder = recorder

        async def stream(self, *_args, **_kwargs):
            if False:
                yield None

    async def scenario() -> None:
        host = ApplicationHost(str(tmp_path), discover_plugins=False)
        await host.astart()
        provider = ProviderConfig(
            name="deepseek",
            protocol="openai-compat",
            base_url="https://api.deepseek.com",
            model="deepseek-flash",
        )
        parent = None
        child = None
        try:
            parent_client = DeepSeekClient()
            parent = await host.context.get(AGENT_FACTORY).acreate(
                AgentRuntimeRequest(
                    provider=provider,
                    work_dir=str(tmp_path),
                    client=parent_client,
                    auxiliary_client=parent_client,
                    enable_memory=False,
                    enable_skills=False,
                    enable_tool_search=False,
                )
            )
            copied_gateway = parent.registry.get("InvokeDeferredTool")
            assert copied_gateway is not None

            child_client = DeepSeekClient()
            child = await host.context.get(AGENT_FACTORY).acreate(
                AgentRuntimeRequest(
                    provider=provider,
                    work_dir=str(tmp_path),
                    client=child_client,
                    auxiliary_client=child_client,
                    enable_memory=False,
                    enable_skills=False,
                    enable_tool_search=False,
                    extra_tools=(copied_gateway,),
                )
            )

            local_gateway = child.registry.get("InvokeDeferredTool")
            assert local_gateway is not None
            assert local_gateway is not copied_gateway
            assert local_gateway._registry is child.registry
        finally:
            if child is not None:
                await child.aclose()
            if parent is not None:
                await parent.aclose()
            await host.aclose()

    asyncio.run(scenario())


def test_async_plugin_lifecycle_is_serial_and_rolls_back() -> None:
    async def scenario() -> None:
        ctx = Context()
        events: list[str] = []

        @plugin("async", version="2", purpose="async test")
        async def async_plugin(runtime: Context):
            events.append("load")
            runtime.on("event", lambda _: events.append("event"))

            async def cleanup() -> None:
                await asyncio.sleep(0)
                events.append("unload")

            return cleanup

        await ctx.aload(async_plugin)
        assert ctx.plugin_state("async") == PluginState.ACTIVE
        await ctx.events.emit("event")
        await ctx.aunload("async")
        assert events == ["load", "event", "unload"]

        @plugin("broken")
        def broken(runtime: Context):
            runtime.slots.register("temporary", "half-installed", kind="single")
            raise RuntimeError("boom")

        try:
            await ctx.aload(broken)
        except RuntimeError as exc:
            assert str(exc) == "boom"
        assert ctx.slots.resolve("temporary") is None
        assert ctx.plugin_state("broken") == PluginState.FAILED
        await ctx.aclose()

    asyncio.run(scenario())


def test_waiting_plugin_activates_when_injected_service_appears() -> None:
    ctx = Context()
    seen: list[object] = []

    @plugin("consumer", inject=("runtime.client",))
    def consumer(runtime: Context):
        seen.append(runtime.get("runtime.client"))

    ctx.register_plugin(consumer)
    ctx.load(consumer)
    assert ctx.plugin_state("consumer") == PluginState.WAITING
    ctx.provide("runtime.client", "client")
    assert ctx.plugin_state("consumer") == PluginState.ACTIVE
    assert seen == ["client"]
    ctx.close()


def test_async_waiting_plugin_resumes_on_async_service_binding() -> None:
    async def scenario() -> None:
        ctx = Context()
        seen: list[str] = []

        @plugin("async-consumer", inject=("client",))
        async def consumer(runtime: Context):
            seen.append(runtime.get("client"))

        await ctx.aload(consumer)
        assert ctx.plugin_state("async-consumer") == PluginState.WAITING
        ctx.provide("client", "async-client")
        await asyncio.sleep(0)
        assert ctx.plugin_state("async-consumer") == PluginState.ACTIVE
        assert seen == ["async-client"]
        await ctx.aclose()

    asyncio.run(scenario())


def test_fastapi_plugin_removes_only_its_routes() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.get("/base")(lambda: {"ok": True})
    ctx = Context()
    ctx.register_plugin(
        fastapi_plugin(
            "api.demo",
            app,
            lambda: _install_demo_route(app),
            requires=(),
        )
    )
    ctx.load("api.demo")
    with TestClient(app) as client:
        assert client.get("/base").status_code == 200
        assert client.get("/demo").status_code == 200
    ctx.unload("api.demo")
    with TestClient(app) as client:
        assert client.get("/base").status_code == 200
        assert client.get("/demo").status_code == 404


def test_fastapi_plugin_rolls_back_routes_when_slot_registration_fails() -> None:
    from fastapi import FastAPI

    app = FastAPI()
    ctx = Context()
    ctx.slots.declare("web.apis", kind="single", owner="test")
    ctx.register_plugin(
        fastapi_plugin(
            "api.broken",
            app,
            lambda: _install_then_fail(app),
            requires=(),
        )
    )
    try:
        ctx.load("api.broken")
    except ValueError as exc:
        assert "expects single" in str(exc)
    assert not any(getattr(route, "path", "") == "/broken" for route in app.routes)
    assert ctx.plugin_state("api.broken") == PluginState.FAILED


def test_fastapi_plugin_awaits_installer_cleanup() -> None:
    class Router:
        def __init__(self) -> None:
            self.routes: list[object] = ["base"]
            self.lifespan_context = "base-lifespan"

    app = SimpleNamespace(router=Router())
    events: list[str] = []
    installed_route = object()
    old_lifespan = app.router.lifespan_context

    def install() -> FastAPIInstallResult:
        app.router.routes.append(installed_route)

        async def cleanup() -> None:
            await asyncio.sleep(0)
            events.append("installer-cleanup")

        return FastAPIInstallResult(service="pipeline", cleanup=cleanup)

    ctx = Context()
    ctx.register_plugin(fastapi_plugin("api.async-cleanup", app, install, requires=()))
    ctx.load("api.async-cleanup")
    assert app.router.routes == ["base", installed_route]

    async def scenario() -> None:
        await ctx.aunload("api.async-cleanup")

    asyncio.run(scenario())
    assert events == ["installer-cleanup"]
    assert app.router.routes == ["base"]
    assert app.router.lifespan_context == old_lifespan
    ctx.close()


def test_pipeline_scheduler_stops_when_api_plugin_unloads(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    import mewcode.pipeline.api as papi

    app = FastAPI()
    ctx = Context()
    ctx.register_plugin(
        fastapi_plugin(
            "api.pipeline",
            app,
            lambda: papi.register_pipeline_api(app, str(tmp_path)),
            requires=(),
        )
    )
    ctx.load("api.pipeline")
    scheduler = papi._scheduler
    assert scheduler is not None

    async def scenario() -> None:
        await scheduler.start()
        assert scheduler._running is True
        await ctx.aunload("api.pipeline")
        assert scheduler._running is False
        assert scheduler._task is None

    asyncio.run(scenario())
    ctx.close()


def test_cleanup_failures_do_not_leave_later_effects_installed() -> None:
    ctx = Context()
    events: list[str] = []

    @plugin("cleanup-errors")
    def cleanup_plugin(runtime: Context):
        runtime.effect(lambda: events.append("first"))

        def broken_cleanup() -> None:
            events.append("broken")
            raise RuntimeError("cleanup boom")

        runtime.effect(broken_cleanup)
        runtime.effect(lambda: events.append("last"))

    ctx.load(cleanup_plugin)
    ctx.unload("cleanup-errors")
    assert events == ["last", "broken", "first"]
    assert ctx.plugin_state("cleanup-errors") == PluginState.UNLOADED
    assert "cleanup errors: 1" in (ctx.plugin_run("cleanup-errors").error or "")
    ctx.close()


def test_child_slot_injection_is_removed_after_parent_slot_unmounts() -> None:
    root = Context()

    @plugin("slot-owner")
    def owner(runtime: Context):
        runtime.slots.declare("temporary", kind="list", owner="slot-owner")

    root.load(owner)
    child = root.scope("child")
    seen: list[object] = []
    child.slots.inject("temporary", seen.append)
    assert seen == [[]]
    root.unload("slot-owner")
    assert seen == [[], None]
    child.close()
    root.load("slot-owner")
    assert seen == [[], None]
    root.close()


def test_external_dependency_is_registered_before_discovery_order_load(tmp_path: Path) -> None:
    plugin_root = tmp_path / ".mewcode" / "plugins"
    _write_plugin(
        plugin_root / "a-first",
        """
from mewcode.cordis import PluginSpec
def apply(ctx):
    assert ctx.get('plugin.b.ready') is True
    ctx.provide('plugin.a.ready', True)
PLUGIN = PluginSpec('a-first', apply, requires=('b-second',))
""",
    )
    _write_plugin(
        plugin_root / "b-second",
        """
from mewcode.cordis import PluginSpec
def apply(ctx):
    ctx.provide('plugin.b.ready', True)
PLUGIN = PluginSpec('b-second', apply)
""",
    )
    host = ApplicationHost(str(tmp_path), user_plugin_dir=tmp_path / "user").start()
    try:
        assert host.context.get("plugin.a.ready") is True
        assert host.context.plugin_state("a-first") == PluginState.ACTIVE
        assert host.context.plugin_state("b-second") == PluginState.ACTIVE
    finally:
        host.close()


def test_reconfigure_work_dir_replaces_project_plugins_and_catalogs(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_plugin(
        first / ".mewcode" / "plugins" / "first",
        """
from types import SimpleNamespace
from mewcode.cordis import PluginSpec
def apply(ctx):
    ctx.slots.register('core.tools', SimpleNamespace(name='FirstTool'), item_key='FirstTool')
PLUGIN = PluginSpec('first', apply)
""",
    )
    _write_plugin(
        second / ".mewcode" / "plugins" / "second",
        """
from types import SimpleNamespace
from mewcode.cordis import PluginSpec
def apply(ctx):
    ctx.slots.register('core.tools', SimpleNamespace(name='SecondTool'), item_key='SecondTool')
PLUGIN = PluginSpec('second', apply)
""",
    )
    host = ApplicationHost(str(first), user_plugin_dir=tmp_path / "user").start()
    try:
        assert host.context.tools.get("FirstTool") is not None
        host.reconfigure_work_dir(str(second))
        assert host.context.tools.get("FirstTool") is None
        assert host.context.tools.get("SecondTool") is not None
        assert host.context.get("work_dir") == str(second.resolve())
        assert host.context.plugin_state("first") == PluginState.UNLOADED
        assert host.context.plugin_state("second") == PluginState.ACTIVE
    finally:
        host.close()


def test_old_runtime_binding_disposer_cannot_unload_new_binding() -> None:
    host = ApplicationHost(".", discover_plugins=False).start()
    try:
        old = host.bind("runtime.value", "old")
        new = host.bind("runtime.value", "new")
        old.dispose()
        assert host.context.get("runtime.value") == "new"
        new.dispose()
        assert host.context.get("runtime.value", None) is None
    finally:
        host.close()


def test_async_reconfigure_awaits_cleanup_and_unregisters_old_plugin(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        first = tmp_path / "first"
        second = tmp_path / "second"
        cleanup_marker = tmp_path / "cleanup.txt"
        _write_plugin(
            first / ".mewcode" / "plugins" / "first",
            f"""
from pathlib import Path
from mewcode.cordis import PluginSpec
async def apply(ctx):
    ctx.provide('project.marker', 'first')
    async def cleanup():
        Path({str(cleanup_marker)!r}).write_text('done', encoding='utf-8')
    return cleanup
PLUGIN = PluginSpec('first', apply)
""",
        )
        _write_plugin(
            second / ".mewcode" / "plugins" / "second",
            """
from mewcode.cordis import PluginSpec
def apply(ctx):
    ctx.provide('project.marker', 'second')
PLUGIN = PluginSpec('second', apply)
""",
        )
        host = ApplicationHost(
            str(first),
            user_plugin_dir=tmp_path / "user",
        )
        await host.astart()
        try:
            await host.areconfigure_work_dir(str(second))

            assert cleanup_marker.read_text(encoding="utf-8") == "done"
            assert host.context.get("project.marker") == "second"
            assert host.context.plugin_spec("first") is None
            assert host.context.plugin_state("second") == PluginState.ACTIVE
        finally:
            await host.aclose()

    asyncio.run(scenario())


def test_concurrent_async_reconfigure_keeps_catalog_and_plugin_graph_consistent(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        first = tmp_path / "first"
        target_a = tmp_path / "target-a"
        target_b = tmp_path / "target-b"
        for target, name, marker in (
            (target_a, "project-a", "a"),
            (target_b, "project-b", "b"),
        ):
            _write_plugin(
                target / ".mewcode" / "plugins" / name,
                f"""
import asyncio
from mewcode.cordis import PluginSpec
async def apply(ctx):
    await asyncio.sleep(0.01)
    ctx.provide('project.marker', {marker!r}, replace=True)
PLUGIN = PluginSpec({name!r}, apply)
""",
            )

        host = ApplicationHost(
            str(first), user_plugin_dir=tmp_path / "user"
        )
        await host.astart()
        try:
            await asyncio.gather(
                host.areconfigure_work_dir(str(target_a)),
                host.areconfigure_work_dir(str(target_b)),
            )
            expected = {
                str(target_a.resolve()): ("project-a", "a", "project-b"),
                str(target_b.resolve()): ("project-b", "b", "project-a"),
            }
            final_dir = host.work_dir
            active_name, marker, stale_name = expected[final_dir]
            assert host.context.get("work_dir") == final_dir
            assert host.context.get("project.marker") == marker
            assert host.context.plugin_state(active_name) == PluginState.ACTIVE
            assert host.context.plugin_spec(stale_name) is None
        finally:
            await host.aclose()

    asyncio.run(scenario())


def test_concurrent_async_bind_replaces_one_runtime_binding_atomically() -> None:
    async def scenario() -> None:
        host = ApplicationHost(".", discover_plugins=False)
        await host.astart()
        try:
            first, second = await asyncio.gather(
                host.abind("runtime.llm", "first"),
                host.abind("runtime.llm", "second"),
            )
            assert host.context.get("runtime.llm") == "second"
            await first.adispose()
            assert host.context.get("runtime.llm") == "second"
            await second.adispose()
            assert host.context.get("runtime.llm", None) is None
        finally:
            await host.aclose()

    asyncio.run(scenario())


def test_reconfigure_reloads_relative_plugin_modules(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    package = tmp_path / "user" / "reloadable"
    _write_plugin(
        package,
        """
from .helper import VALUE
from mewcode.cordis import PluginSpec
def apply(ctx):
    ctx.provide('reload.value', VALUE)
PLUGIN = PluginSpec('reloadable', apply)
""",
    )
    (package / "helper.py").write_text("VALUE = 'old'\n", encoding="utf-8")
    host = ApplicationHost(str(first), user_plugin_dir=tmp_path / "user").start()
    try:
        assert host.context.get("reload.value") == "old"
        (package / "helper.py").write_text(
            "VALUE = 'new-value'\n",
            encoding="utf-8",
        )
        host.reconfigure_work_dir(str(second))
        assert host.context.get("reload.value") == "new-value"
    finally:
        host.close()


def _install_demo_route(app):
    app.get("/demo")(lambda: {"demo": True})
    return "demo-service"


def _install_then_fail(app):
    app.get("/broken")(lambda: {"broken": True})
    return "broken-service"


def _write_plugin(root: Path, source: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.py").write_text(source, encoding="utf-8")
