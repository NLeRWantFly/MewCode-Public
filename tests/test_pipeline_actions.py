from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from mewcode.pipeline.actions import PipelineAction, PipelineActionRequest
from mewcode.pipeline.compiler import PipelineCompiler
from mewcode.pipeline.conversation import PipelineConversationAction
from mewcode.pipeline.executor import PipelineExecutor
from mewcode.pipeline.models import (
    GraphEdge,
    GraphNode,
    NodeType,
    PipelineGraph,
    RunStatus,
)
from mewcode.pipeline.tool_actions import (
    build_tool_actions,
    stable_tool_action_id,
    stable_tool_action_ids,
)
from mewcode.tools import ToolRegistry
from mewcode.tools.base import Tool, ToolResult


def action_graph(*, params=None, action_id="demo.echo") -> PipelineGraph:
    return PipelineGraph(
        graph_id="g_actions",
        name="creative actions",
        nodes=[
            GraphNode(id="start", type=NodeType.START),
            GraphNode(
                id="action",
                type=NodeType.ACTION,
                title="Echo",
                action_id=action_id,
                params=params or {"text": "{{ input.topic }}"},
                output_key="echo",
            ),
            GraphNode(id="end", type=NodeType.END),
        ],
        edges=[
            GraphEdge(from_node="start", to_node="action"),
            GraphEdge(from_node="action", to_node="end"),
        ],
    )


def test_action_descriptor_exports_safe_capability() -> None:
    action = PipelineAction(
        id="demo.echo",
        title="Echo",
        execute=lambda request: request.params,
        category="插件",
        config_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        output_schema={"text": "string"},
        permissions=("network.read",),
        side_effects="none",
    )

    capability = action.capability()
    assert capability["id"] == "demo.echo"
    assert capability["layer"] == "plugin"
    assert capability["permissions"] == ["network.read"]
    assert "execute" not in capability


def test_action_node_interpolates_invokes_and_cleans_up() -> None:
    calls = []
    cleanups = []

    async def execute(request):
        calls.append(request)
        request.emit({"type": "plugin_progress", "message": "working"})
        return {"text": request.params["text"], "conversation_id": "conv-1"}

    async def cleanup(run_id: str) -> None:
        cleanups.append(run_id)

    action = PipelineAction(
        id="demo.echo",
        title="Echo",
        execute=execute,
        cleanup_run=cleanup,
    )
    executor = PipelineExecutor(action_provider=lambda: {action.id: action})
    events = []
    executor.on_event = events.append

    record = asyncio.run(
        executor.execute(action_graph(), {"topic": "liquid glass"}, run_id="run-actions")
    )

    assert record.status == RunStatus.COMPLETED
    assert record.context_outputs["echo"] == {
        "text": "liquid glass",
        "conversation_id": "conv-1",
    }
    assert calls[0].run_id == "run-actions"
    assert calls[0].context_outputs["start"] == {"status": "started"}
    assert cleanups == ["run-actions"]
    action_event = next(event for event in events if event["type"] == "action_call")
    assert action_event["layer"] == "plugin"
    assert any(event["type"] == "plugin_progress" for event in events)


def test_unknown_or_unattended_external_action_fails_closed() -> None:
    missing = asyncio.run(PipelineExecutor().execute(action_graph(), {}))
    assert missing.status == RunStatus.FAILED
    assert "not installed" in (missing.error_message or "")

    action = PipelineAction(
        id="demo.echo",
        title="External echo",
        execute=lambda request: "should not run",
        side_effects="external",
    )
    blocked = asyncio.run(
        PipelineExecutor(action_provider=lambda: {action.id: action}).execute(
            action_graph(), {"trigger": {"type": "schedule"}}
        )
    )
    assert blocked.status == RunStatus.FAILED
    assert "allow_unattended=true" in (blocked.error_message or "")


def test_action_node_requires_action_id() -> None:
    graph = action_graph(action_id="")
    result = PipelineCompiler.validate(graph)
    assert not result.valid
    assert any("missing action_id" in error for error in result.errors)


def test_action_id_validation() -> None:
    with pytest.raises(ValueError):
        PipelineAction(id="Bad Action", title="Bad", execute=lambda request: None)


def test_conversation_action_keeps_real_state_across_nodes() -> None:
    created = []
    closed = []

    def create(request):
        state = {"messages": [], "model": request.model}
        created.append(state)
        return state

    async def send(state, prompt, request):
        state["messages"].append(prompt)
        return f"reply {len(state['messages'])}: {prompt}", {
            "input_tokens": 2,
            "output_tokens": 3,
        }

    async def close(state):
        closed.append(state)

    conversation = PipelineConversationAction(create=create, send=send, close=close)
    action = conversation.action()
    graph = PipelineGraph(
        graph_id="g_conversation",
        name="stateful conversation",
        nodes=[
            GraphNode(id="start", type=NodeType.START),
            GraphNode(
                id="research",
                type=NodeType.ACTION,
                action_id=action.id,
                params={"operation": "start", "prompt": "Research glass"},
                output_key="research",
                model="model-x",
            ),
            GraphNode(
                id="review",
                type=NodeType.ACTION,
                action_id=action.id,
                params={
                    "operation": "continue",
                    "conversation_id": "{{ research.conversation_id }}",
                    "prompt": "Review: {{ research.message }}",
                },
                output_key="review",
                model="model-x",
            ),
            GraphNode(id="end", type=NodeType.END),
        ],
        edges=[
            GraphEdge(from_node="start", to_node="research"),
            GraphEdge(from_node="research", to_node="review"),
            GraphEdge(from_node="review", to_node="end"),
        ],
    )
    executor = PipelineExecutor(action_provider=lambda: {action.id: action})
    record = asyncio.run(executor.execute(graph, {}, run_id="run-conversation"))

    assert record.status == RunStatus.COMPLETED
    assert record.context_outputs["review"]["conversation_id"] == record.context_outputs["research"]["conversation_id"]
    assert created[0]["messages"] == [
        "Research glass",
        "Review: reply 1: Research glass",
    ]
    assert closed == created
    assert record.total_input_tokens == 4
    assert record.total_output_tokens == 6


class _ToolParams(BaseModel):
    value: str


class _ReadPipelineTool(Tool):
    name = "Read File"
    description = "Read one test value"
    category = "read"
    params_model = _ToolParams

    def __init__(self, *, is_error: bool = False):
        self.calls: list[dict] = []
        self.is_error = is_error

    async def execute(self, params: _ToolParams) -> ToolResult:
        self.calls.append(params.model_dump())
        return ToolResult(
            output=params.value if not self.is_error else "failed",
            is_error=self.is_error,
            content_blocks=[{"type": "text", "text": params.value}],
        )


class _WritePipelineTool(_ReadPipelineTool):
    name = "Write/File"
    category = "write"


class _CommandPipelineTool(_ReadPipelineTool):
    name = "Shell+Tool"
    category = "command"


class _InteractivePipelineTool(_ReadPipelineTool):
    name = "Browser Tool"
    interaction_kind = "browser"


def test_tool_action_ids_are_stable_and_collision_safe() -> None:
    names = ["Read File", "read-file", "HTTP/Request"]
    first = stable_tool_action_ids(names)
    second = stable_tool_action_ids(names)
    assert first == second
    assert len(set(first.values())) == len(names)
    assert all(action_id.startswith("tool.") for action_id in first.values())
    assert stable_tool_action_id("Read File") == stable_tool_action_id("Read File")


def test_tool_action_catalog_filters_interactive_and_exposes_side_effect_schema() -> None:
    registry = ToolRegistry()
    read = _ReadPipelineTool()
    write = _WritePipelineTool()
    interactive = _InteractivePipelineTool()
    registry.register(read)
    registry.register(write)
    registry.register(interactive)
    registry.disable(write.name)

    actions = build_tool_actions(registry)
    assert set(actions) == {stable_tool_action_id(read.name)}

    registry.enable(write.name)
    actions = build_tool_actions(registry)
    write_action = actions[stable_tool_action_id(write.name)]
    assert write_action.layer == "tool"
    assert write_action.capability()["layer"] == "tool"
    assert write_action.config_schema == write_action.input_schema
    assert write_action.config_schema["properties"]["allow_unattended"]["type"] == "boolean"


def test_tool_action_validates_permissions_events_and_structured_output() -> None:
    registry = ToolRegistry()
    tool = _ReadPipelineTool()
    registry.register(tool)
    action = build_tool_actions(registry)[stable_tool_action_id(tool.name)]
    events: list[dict] = []
    result = asyncio.run(action.invoke(PipelineActionRequest(
        action_id=action.id,
        run_id="run-tool",
        node_id="node-tool",
        params={"value": "ok"},
        trigger_input={},
        context_outputs={},
        emit=events.append,
    )))
    assert result == {
        "output": "ok",
        "content_blocks": [{"type": "text", "text": "ok"}],
    }
    assert [event["type"] for event in events] == ["tool_use", "tool_result"]
    assert events[-1]["is_error"] is False

    denied = SimpleNamespace(effect="deny", reason="blocked by test")
    denied_action = build_tool_actions(
        registry, permission_checker=SimpleNamespace(check=lambda *_: denied)
    )[stable_tool_action_id(tool.name)]
    with pytest.raises(RuntimeError, match="Permission denied"):
        asyncio.run(denied_action.invoke(PipelineActionRequest(
            action_id=denied_action.id,
            run_id="run-deny",
            node_id="node-deny",
            params={"value": "secret"},
            trigger_input={},
            context_outputs={},
            emit=events.append,
        )))
    assert events[-1]["type"] == "tool_result"
    assert events[-1]["is_error"] is True
    assert tool.calls == [{"value": "ok"}]


def test_tool_action_validation_and_tool_error_fail_closed() -> None:
    registry = ToolRegistry()
    failing = _ReadPipelineTool(is_error=True)
    registry.register(failing)
    action = build_tool_actions(registry)[stable_tool_action_id(failing.name)]
    with pytest.raises(RuntimeError, match="Parameter validation error"):
        asyncio.run(action.invoke(PipelineActionRequest(
            action_id=action.id,
            run_id="run-invalid",
            node_id="node-invalid",
            params={},
            trigger_input={},
            context_outputs={},
        )))
    with pytest.raises(RuntimeError, match="failed"):
        asyncio.run(action.invoke(PipelineActionRequest(
            action_id=action.id,
            run_id="run-failed",
            node_id="node-failed",
            params={"value": "bad"},
            trigger_input={},
            context_outputs={},
        )))
    assert failing.calls == [{"value": "bad"}]


def test_scheduled_external_tool_requires_opt_in_and_strips_flag() -> None:
    registry = ToolRegistry()
    command = _CommandPipelineTool()
    registry.register(command)
    action = build_tool_actions(registry)[stable_tool_action_id(command.name)]
    request = dict(
        action_id=action.id,
        run_id="run-scheduled",
        node_id="node-scheduled",
        trigger_input={},
        context_outputs={},
        unattended=True,
    )
    with pytest.raises(RuntimeError, match="allow_unattended=true"):
        asyncio.run(action.invoke(PipelineActionRequest(
            **request, params={"value": "blocked"}
        )))
    result = asyncio.run(action.invoke(PipelineActionRequest(
        **request, params={"value": "allowed", "allow_unattended": True}
    )))
    assert result["output"] == "allowed"
    assert command.calls == [{"value": "allowed"}]
