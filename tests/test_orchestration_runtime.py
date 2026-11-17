from __future__ import annotations

import asyncio
from types import SimpleNamespace

from pydantic import BaseModel

from mewcode.agent_calls import _build_registry, run_agent_call
from mewcode.config import ProviderConfig
from mewcode.context.critic import CompletenessCritic
from mewcode.conversation import ConversationManager
from mewcode.pipeline.executor import PipelineExecutor
from mewcode.pipeline.models import (
    GraphEdge,
    GraphNode,
    NodeStatus,
    NodeType,
    PipelineGraph,
    RunStatus,
)
from mewcode.tools import ToolRegistry
from mewcode.tools.base import StreamEnd, TextDelta, Tool, ToolResult


class _Params(BaseModel):
    value: str = ""


class _NamedTool(Tool):
    description = "test tool"
    params_model = _Params

    def __init__(self, name: str) -> None:
        self.name = name

    async def execute(self, params: BaseModel) -> ToolResult:
        return ToolResult(output="ok")


class _FakeClient:
    def __init__(self, text: str) -> None:
        self.text = text
        self.effort = ""

    def set_effort(self, effort: str) -> None:
        self.effort = effort

    def set_max_output_tokens(self, tokens: int) -> None:
        pass

    async def stream(self, conversation, system="", tools=None):
        yield TextDelta(self.text)
        yield StreamEnd(
            stop_reason="end_turn", input_tokens=11, output_tokens=7
        )


class _StructuredResult(BaseModel):
    answer: str


def _parent(tmp_path, registry: ToolRegistry | None = None):
    return SimpleNamespace(
        work_dir=str(tmp_path),
        max_iterations=3,
        registry=registry or ToolRegistry(),
        permission_checker=None,
        instructions_content="",
        memory_manager=None,
        hook_engine=None,
        agent_id="parent",
        trace_id=None,
    )


def _provider() -> ProviderConfig:
    return ProviderConfig(
        name="test",
        protocol="anthropic",
        base_url="https://example.invalid",
        model="claude-sonnet-test",
        api_key="test-key",
    )


def test_agent_call_uses_fresh_light_client_and_returns_usage(tmp_path, monkeypatch):
    created = []
    client = _FakeClient('{"answer":"done"}')

    def fake_create_client(config):
        created.append(config)
        return client

    monkeypatch.setattr("mewcode.client.create_client", fake_create_client)

    result, usage = asyncio.run(
        run_agent_call(
            parent_agent=_parent(tmp_path),
            provider=_provider(),
            prompt="check this",
            schema=_StructuredResult,
            model_hint="haiku",
            effort="off",
            tools=[],
        )
    )

    assert result == _StructuredResult(answer="done")
    assert usage == {"input_tokens": 11, "output_tokens": 7}
    assert created[0].model == "claude-haiku-4-5-20251001"
    assert client.effort == "off"


def test_agent_call_treats_inherit_as_current_model(tmp_path, monkeypatch):
    created = []

    def fake_create_client(config):
        created.append(config)
        return _FakeClient("done")

    monkeypatch.setattr("mewcode.client.create_client", fake_create_client)

    result, _ = asyncio.run(
        run_agent_call(
            parent_agent=_parent(tmp_path),
            provider=_provider(),
            prompt="run",
            model="inherit",
            tools=[],
        )
    )

    assert result == "done"
    assert created[0].model == "claude-sonnet-test"


def test_agent_call_registry_honors_explicit_tool_filter(tmp_path):
    registry = ToolRegistry()
    registry.register(_NamedTool("Allowed"))
    registry.register(_NamedTool("Blocked"))
    parent = _parent(tmp_path, registry)

    filtered = _build_registry(parent, ["Allowed"])

    assert filtered.get("Allowed") is not None
    assert filtered.get("Blocked") is None
    assert _build_registry(parent, []).list_tools() == []


def test_critic_accepts_agent_factory_result_with_usage():
    calls = []

    async def factory(**kwargs):
        calls.append(kwargs)
        return '{"status":"clean","suggestions":[]}', {
            "input_tokens": 3,
            "output_tokens": 2,
        }

    conversation = ConversationManager()
    conversation.add_user_message("implement it")
    critic = CompletenessCritic(enabled=True, agent_factory=factory)

    result = asyncio.run(critic.check(conversation, "implemented and tested"))

    assert result.status == "clean"
    assert calls[0]["model_hint"] == "haiku"
    assert calls[0]["effort"] == "off"
    assert calls[0]["tools"] == []


def _two_task_graph() -> PipelineGraph:
    return PipelineGraph(
        name="budgeted",
        nodes=[
            GraphNode(id="start", type=NodeType.START),
            GraphNode(id="task-1", type=NodeType.TASK, goal="first", auto_split=False),
            GraphNode(id="task-2", type=NodeType.TASK, goal="second", auto_split=False),
            GraphNode(id="end", type=NodeType.END),
        ],
        edges=[
            GraphEdge(id="e1", **{"from": "start", "to": "task-1"}),
            GraphEdge(id="e2", **{"from": "task-1", "to": "task-2"}),
            GraphEdge(id="e3", **{"from": "task-2", "to": "end"}),
        ],
    )


def test_pipeline_budget_stops_before_next_billed_node_and_records_usage():
    calls = []

    async def factory(**kwargs):
        calls.append(kwargs)
        return "done", {"input_tokens": 40, "output_tokens": 20}

    record = asyncio.run(
        PipelineExecutor(agent_factory=factory).execute(
            _two_task_graph(), budget_total=60
        )
    )

    assert record.status == RunStatus.INTERRUPTED
    assert len(calls) == 1
    assert record.total_input_tokens == 40
    assert record.total_output_tokens == 20
    assert record.budget_total == 60
    assert record.node_records["task-1"].status == NodeStatus.COMPLETED
    assert record.node_records["task-1"].input_tokens == 40
    assert record.node_records["task-1"].output_tokens == 20
    assert record.node_records["task-2"].status == NodeStatus.FAILED
    assert "budget exhausted" in (record.error_message or "").lower()


def test_pipeline_without_budget_accumulates_all_agent_usage():
    async def factory(**kwargs):
        return "done", {"input_tokens": 5, "output_tokens": 3}

    record = asyncio.run(PipelineExecutor(agent_factory=factory).execute(
        _two_task_graph()
    ))

    assert record.status == RunStatus.COMPLETED
    assert record.total_input_tokens == 10
    assert record.total_output_tokens == 6
    assert record.node_records["task-2"].status == NodeStatus.COMPLETED


def test_pipeline_counts_skill_usage():
    graph = PipelineGraph(
        name="skill-budget",
        nodes=[
            GraphNode(id="start", type=NodeType.START),
            GraphNode(
                id="skill", type=NodeType.SKILL, skill_name="review"
            ),
            GraphNode(id="end", type=NodeType.END),
        ],
        edges=[
            GraphEdge(id="e1", **{"from": "start", "to": "skill"}),
            GraphEdge(id="e2", **{"from": "skill", "to": "end"}),
        ],
    )

    async def skill_executor(name, args):
        return "reviewed", {"input_tokens": 13, "output_tokens": 4}

    record = asyncio.run(
        PipelineExecutor(skill_executor=skill_executor).execute(
            graph, budget_total=100
        )
    )

    assert record.status == RunStatus.COMPLETED
    assert record.total_input_tokens == 13
    assert record.total_output_tokens == 4
    assert record.node_records["skill"].input_tokens == 13
