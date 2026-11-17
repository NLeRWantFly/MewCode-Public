from __future__ import annotations

from pathlib import Path

import pytest

from mewcode.agent import Agent, UsageEvent
from mewcode.auxiliary import create_auxiliary_client
from mewcode.config import PipelineConfig, ProviderConfig
from mewcode.conversation import ConversationManager
from mewcode.pipeline.executor import PipelineExecutor
from mewcode.pipeline.models import (
    GraphEdge,
    GraphNode,
    NodeType,
    PipelineGraph,
    RunStatus,
)
from mewcode.tools import ToolRegistry
from mewcode.tools.base import StreamEnd, TextDelta


class _EffortClient:
    def __init__(self) -> None:
        self.effort = ""

    def set_effort(self, effort: str) -> None:
        self.effort = effort


def test_auxiliary_router_uses_configured_light_model(monkeypatch) -> None:
    captured: dict[str, str] = {}
    client = _EffortClient()

    def fake_create(provider: ProviderConfig):
        captured["model"] = provider.model
        return client

    provider = ProviderConfig(
        name="test",
        protocol="anthropic",
        base_url="https://example.invalid",
        model="claude-opus-4-1",
        api_key="x",
    )

    result = create_auxiliary_client(
        provider,
        PipelineConfig(auto_light_model="claude-haiku-4-5"),
        client_factory=fake_create,
    )

    assert result is client
    assert captured["model"] == "claude-haiku-4-5"
    assert client.effort == "low"
    assert provider.model == "claude-opus-4-1"


@pytest.mark.asyncio
async def test_memory_extraction_uses_auxiliary_client(tmp_path: Path) -> None:
    primary = object()
    auxiliary = object()

    class Memory:
        seen = None

        async def extract(self, client, conversation, protocol) -> None:
            self.seen = client

    memory = Memory()
    agent = Agent(
        client=primary,
        auxiliary_client=auxiliary,
        registry=ToolRegistry(),
        protocol="anthropic",
        work_dir=str(tmp_path),
        memory_manager=memory,
    )

    await agent._extract_memories(ConversationManager())

    assert memory.seen is auxiliary


@pytest.mark.asyncio
async def test_agent_accumulates_prompt_cache_usage(tmp_path: Path) -> None:
    class Client:
        async def stream(self, conversation, system="", tools=None):
            yield TextDelta("done")
            yield StreamEnd(
                stop_reason="end_turn",
                input_tokens=100,
                output_tokens=20,
                cache_read=900,
                cache_creation=50,
            )

        def set_max_output_tokens(self, tokens: int) -> None:
            pass

    client = Client()
    agent = Agent(
        client=client,
        registry=ToolRegistry(),
        protocol="anthropic",
        work_dir=str(tmp_path),
    )
    conversation = ConversationManager()
    conversation.add_user_message("hello")

    events = [event async for event in agent.run(conversation)]
    usage = next(event for event in events if isinstance(event, UsageEvent))

    assert usage.input_tokens == 100
    assert usage.output_tokens == 20
    assert usage.cache_read_tokens == 900
    assert usage.cache_creation_tokens == 50
    assert usage.request_input_tokens == 1050
    assert usage.cache_usage_available is True
    assert usage.request_cache_hit_rate == 900 / 1050
    assert usage.cumulative_cache_hit_rate == 900 / 1050
    assert usage.cache_provider == "anthropic"
    assert agent.total_cache_read_tokens == 900
    assert agent.cache_usage_available is True
    assert agent.total_deepseek_cache_hit_tokens == 0


class _PipelineFactory:
    def __init__(self, replies: list[str], usage: dict | None = None) -> None:
        self.replies = replies
        self.usage = usage or {"input_tokens": 1, "output_tokens": 1}
        self.calls: list[str] = []

    async def __call__(
        self,
        prompt,
        schema=None,
        model=None,
        effort=None,
        isolation=None,
        on_event=None,
    ):
        index = len(self.calls)
        self.calls.append(prompt)
        reply = self.replies[min(index, len(self.replies) - 1)]
        return reply, dict(self.usage)


def _two_task_graph(second_prompt: str = "") -> PipelineGraph:
    return PipelineGraph(
        graph_id="g_token_efficiency",
        name="token-efficiency",
        nodes=[
            GraphNode(id="s", type=NodeType.START),
            GraphNode(
                id="t1",
                type=NodeType.TASK,
                goal="produce",
                auto_split=False,
            ),
            GraphNode(
                id="t2",
                type=NodeType.TASK,
                goal="consume",
                prompt=second_prompt,
                auto_split=False,
            ),
            GraphNode(id="e", type=NodeType.END),
        ],
        edges=[
            GraphEdge(id="e1", from_node="s", to_node="t1"),
            GraphEdge(id="e2", from_node="t1", to_node="t2"),
            GraphEdge(id="e3", from_node="t2", to_node="e"),
        ],
    )


@pytest.mark.asyncio
async def test_large_pipeline_interpolation_persists_full_output(
    tmp_path: Path,
) -> None:
    large = "BEGIN-" + ("x" * 20_000) + "-END"
    factory = _PipelineFactory([large, "done"])
    events: list[dict] = []
    executor = PipelineExecutor(
        agent_factory=factory,
        config=PipelineConfig(max_inline_output_chars=1_000),
        artifact_dir=tmp_path,
    )
    executor.on_event = lambda event: events.append(event)

    record = await executor.execute(_two_task_graph("upstream={{t1}}"))

    assert record.status == RunStatus.COMPLETED
    downstream_prompt = factory.calls[1]
    assert "<persisted-output" in downstream_prompt
    assert len(downstream_prompt) < 3_000
    assert large not in downstream_prompt
    compact_event = next(
        event for event in events if event["type"] == "interpolation_compacted"
    )
    persisted = Path(compact_event["path"])
    assert persisted.read_text(encoding="utf-8") == large
    assert record.context_outputs["t1"] == large


@pytest.mark.asyncio
async def test_pipeline_budget_counts_cached_prompt_tokens() -> None:
    factory = _PipelineFactory(
        ["first", "second"],
        usage={
            "input_tokens": 100,
            "output_tokens": 0,
            "cache_read": 950,
            "cache_creation": 0,
        },
    )
    executor = PipelineExecutor(agent_factory=factory)

    record = await executor.execute(_two_task_graph(), budget_total=1_000)

    assert record.status == RunStatus.INTERRUPTED
    assert len(factory.calls) == 1
    assert record.total_input_tokens == 100
    assert record.total_cache_read_tokens == 950
    assert record.cache_usage_available is True
    assert "budget" in (record.error_message or "").lower()
