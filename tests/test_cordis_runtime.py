from __future__ import annotations

import asyncio

import pytest

from mewcode.cordis import (
    Context,
    ServiceConsumer,
    ServiceDefinition,
    ServiceProvider,
    plugin,
)


def test_plugins_load_by_dependency_and_unload_all_effects() -> None:
    ctx = Context()
    events: list[str] = []
    tools = ServiceDefinition[list[str]]("tools")

    @plugin("tools")
    def tools_plugin(runtime: Context):
        runtime.provide(tools, ["ReadFile"])
        runtime.on("turn/end", lambda _payload: events.append("tools"))

    @plugin("ui", requires=("tools",))
    def ui_plugin(runtime: Context):
        runtime.on("turn/end", lambda _payload: events.append("ui"))
        runtime.slots.register(
            "conversation.renderer",
            "renderer-v1",
            plugin="ui",
            kind="single",
        )

    ctx.register_plugin(tools_plugin)
    ctx.register_plugin(ui_plugin)
    ctx.load("ui")

    assert ctx.get(tools) == ["ReadFile"]
    assert ctx.slots.resolve("conversation.renderer") == "renderer-v1"
    asyncio.run(ctx.events.emit("turn/end"))
    assert events == ["tools", "ui"]

    ctx.unload("ui")
    assert ctx.slots.resolve("conversation.renderer") is None
    asyncio.run(ctx.events.emit("turn/end"))
    assert events == ["tools", "ui", "tools"]


def test_pending_injection_does_not_force_a_slot_kind() -> None:
    ctx = Context()
    received: list[object] = []
    ctx.slots.inject("renderers", received.append)
    ctx.slots.register(
        "renderers",
        "markdown",
        plugin="markdown",
        kind="chain",
        priority=10,
    )

    assert received == ["markdown"]
    assert ctx.slots.resolve("renderers", owner={"format": "md"}) == "markdown"


def test_parent_late_slot_declaration_adopts_child_injection() -> None:
    root = Context()
    child = root.scope("session")
    received: list[object] = []
    disposer = child.slots.inject("late.slot", received.append)

    @plugin("slot-owner")
    def slot_owner(runtime: Context):
        runtime.slots.declare("late.slot", kind="list", owner="slot-owner")

    root.load(slot_owner)
    assert received == [[]]
    provider = root.slots.register("late.slot", "value", kind="list")
    assert received == [[], ["value"]]

    provider.dispose()
    assert received == [[], ["value"], []]
    root.unload("slot-owner")
    assert received == [[], ["value"], [], None]
    root.load(slot_owner)
    assert received == [[], ["value"], [], None, []]

    disposer.dispose()
    root.unload("slot-owner")
    child.close()
    root.close()


def test_dependency_failure_marks_outer_plugin_failed() -> None:
    ctx = Context()

    @plugin("broken-dependency")
    def broken(_runtime: Context):
        raise RuntimeError("dependency boom")

    @plugin("dependent", requires=("broken-dependency",))
    def dependent(_runtime: Context):
        raise AssertionError("dependent should not be applied")

    ctx.register_plugin(broken)
    ctx.register_plugin(dependent)
    with pytest.raises(RuntimeError, match="dependency boom"):
        ctx.load("dependent")

    assert ctx.plugin_state("broken-dependency").value == "failed"
    assert ctx.plugin_state("dependent").value == "failed"
    assert ctx.plugin_run("dependent").error == "RuntimeError: dependency boom"
    ctx.close()


def test_async_dependency_failure_marks_outer_plugin_failed() -> None:
    async def scenario() -> None:
        ctx = Context()

        @plugin("async-broken-dependency")
        async def broken(_runtime: Context):
            raise RuntimeError("async dependency boom")

        @plugin("async-dependent", requires=("async-broken-dependency",))
        async def dependent(_runtime: Context):
            raise AssertionError("dependent should not be applied")

        ctx.register_plugin(broken)
        ctx.register_plugin(dependent)
        with pytest.raises(RuntimeError, match="async dependency boom"):
            await ctx.aload("async-dependent")

        assert ctx.plugin_state("async-broken-dependency").value == "failed"
        assert ctx.plugin_state("async-dependent").value == "failed"
        await ctx.aclose()

    asyncio.run(scenario())


def test_sync_cycle_is_rejected_and_runs_are_failed() -> None:
    ctx = Context()

    @plugin("cycle-a", requires=("cycle-b",))
    def cycle_a(_runtime: Context):
        pass

    @plugin("cycle-b", requires=("cycle-a",))
    def cycle_b(_runtime: Context):
        pass

    ctx.register_plugin(cycle_a)
    ctx.register_plugin(cycle_b)
    with pytest.raises(RuntimeError, match="cyclic plugin dependency"):
        ctx.load("cycle-a")
    assert ctx.plugin_state("cycle-a").value == "failed"
    assert ctx.plugin_state("cycle-b").value == "failed"
    ctx.close()


def test_async_cycle_is_rejected_and_runs_are_failed() -> None:
    async def scenario() -> None:
        ctx = Context()

        @plugin("async-cycle-a", requires=("async-cycle-b",))
        async def cycle_a(_runtime: Context):
            pass

        @plugin("async-cycle-b", requires=("async-cycle-a",))
        async def cycle_b(_runtime: Context):
            pass

        ctx.register_plugin(cycle_a)
        ctx.register_plugin(cycle_b)
        with pytest.raises(RuntimeError, match="cyclic plugin dependency"):
            await ctx.aload(cycle_a)
        assert ctx.plugin_state("async-cycle-a").value == "failed"
        assert ctx.plugin_state("async-cycle-b").value == "failed"
        await ctx.aclose()

    asyncio.run(scenario())


def test_chain_slot_selects_highest_priority_matching_provider() -> None:
    ctx = Context()
    ctx.slots.register(
        "tool.renderer",
        "generic",
        plugin="generic",
        kind="chain",
        priority=1,
        select=lambda owner: owner.get("kind") == "tool",
    )
    ctx.slots.register(
        "tool.renderer",
        "python",
        plugin="python",
        kind="chain",
        priority=20,
        select=lambda owner: owner.get("language") == "python",
    )

    assert ctx.slots.resolve("tool.renderer", {"kind": "tool"}) == "generic"
    assert ctx.slots.resolve("tool.renderer", {"kind": "tool", "language": "python"}) == "python"


def test_service_provider_consumer_and_child_scope() -> None:
    definition = ServiceDefinition[str]("llm")
    ctx = Context()
    child = ctx.scope("session-a")
    seen: list[str] = []
    ServiceConsumer(definition).bind(child, seen.append)

    assert child.llm is None
    assert seen == [None]
    ServiceProvider(definition, "model-a").install(ctx)
    assert child.llm == "model-a"
    assert seen == [None, "model-a"]
    child.provide(definition, "model-b", replace=True)
    assert child.llm == "model-b"
    assert seen == [None, "model-a", "model-b"]
    assert ctx.llm == "model-a"

    child.close()
    assert child.llm == "model-a"
    assert ctx.llm == "model-a"


def test_delayed_async_effect_stays_owned_by_originating_plugin() -> None:
    async def scenario() -> None:
        ctx = Context()
        release = asyncio.Event()
        effect_registered = asyncio.Event()
        cleaned: list[str] = []

        @plugin("delayed-effect")
        def delayed_effect(runtime: Context):
            async def register_later() -> None:
                await release.wait()
                runtime.effect(lambda: cleaned.append("owned"))
                effect_registered.set()

            task = asyncio.create_task(register_later())
            runtime.effect(task.cancel)

        await ctx.aload(delayed_effect)
        release.set()
        await effect_registered.wait()
        await ctx.aunload("delayed-effect")

        assert cleaned == ["owned"]
        await ctx.aclose()

    asyncio.run(scenario())


def test_slot_callback_effect_belongs_to_injecting_plugin() -> None:
    ctx = Context()
    cleaned: list[object] = []

    @plugin("consumer")
    def consumer(runtime: Context):
        runtime.slots.inject(
            "dynamic.values",
            lambda value: runtime.effect(lambda: cleaned.append(value)),
        )

    @plugin("provider")
    def provider(runtime: Context):
        runtime.slots.register(
            "dynamic.values",
            "value",
            kind="list",
        )

    ctx.load(consumer)
    ctx.load(provider)
    ctx.unload("consumer")

    assert cleaned == [["value"]]
    ctx.close()


def test_aclose_cancels_waiting_plugin_resume_tasks() -> None:
    async def scenario() -> None:
        ctx = Context()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        @plugin("waiting", inject=("client",))
        async def waiting(_runtime: Context):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        await ctx.aload(waiting)
        ctx.provide("client", object())
        await started.wait()
        await ctx.aclose()

        assert cancelled.is_set()
        assert not ctx._background_tasks

    asyncio.run(scenario())


def test_sync_unload_tracks_async_cleanup_inside_running_loop() -> None:
    async def scenario() -> None:
        ctx = Context()
        cleaned: list[str] = []

        @plugin("sync-unload")
        def sync_unload(runtime: Context):
            async def cleanup() -> None:
                await asyncio.sleep(0)
                cleaned.append("done")

            runtime.effect(cleanup)

        ctx.load(sync_unload)
        ctx.unload("sync-unload")
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert cleaned == ["done"]
        await ctx.aclose()

    asyncio.run(scenario())
