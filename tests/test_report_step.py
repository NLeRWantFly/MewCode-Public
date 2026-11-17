from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

import pytest

from mewcode.agent import Agent, ToolResultEvent
from mewcode.client import LLMClient
from mewcode.conversation import ConversationManager
from mewcode.expert_web import _copy_discovered_tools, _role_registry
from mewcode.tools import create_default_registry
from mewcode.tools.ask_user import AskUserTool
from mewcode.tools.base import StreamEnd, StreamEvent, TextDelta, ToolCallComplete
from mewcode.tools.report_step import (
    ReportStepParams,
    ReportStepTool,
    build_expert_step_event,
    expert_step_from_report,
    parse_step_output,
)


class _ScriptedClient(LLMClient):
    def __init__(self, responses: list[list[StreamEvent]]) -> None:
        self._responses = responses
        self._index = 0

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        events = self._responses[self._index]
        self._index += 1
        for event in events:
            yield event


class FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeAgentHost:
    def __init__(self, registry) -> None:
        self.registry = registry


class FakeSession:
    def __init__(self, registry) -> None:
        self.agent = _FakeAgentHost(registry)
        self.context = None


@pytest.mark.asyncio
async def test_report_step_execute_formats_output() -> None:
    tool = ReportStepTool()
    result = await tool.execute(
        ReportStepParams(label="准备查找网络", status="running")
    )
    assert result.output == "STEP\trunning\t准备查找网络"
    assert result.is_error is False


def test_parse_step_output_round_trip() -> None:
    assert parse_step_output("STEP\tcompleted\t写入配置") == ("写入配置", "completed")
    assert parse_step_output("not-a-step") is None


def test_build_expert_step_event_fields() -> None:
    event = build_expert_step_event(
        label="准备查找网络", status="running", role="executor", node_id="node-1"
    )
    assert event == {
        "type": "expert_step",
        "label": "准备查找网络",
        "status": "running",
        "role": "executor",
        "node_id": "node-1",
    }


def test_expert_step_from_report_prefers_args() -> None:
    event = expert_step_from_report(
        args={"label": "写入配置", "status": "completed"},
        output="STEP\trunning\tignored",
        role="planner",
        node_id="node-2",
    )
    assert event == {
        "type": "expert_step",
        "label": "写入配置",
        "status": "completed",
        "role": "planner",
        "node_id": "node-2",
    }


def test_default_registry_includes_report_step() -> None:
    registry = create_default_registry()
    assert registry.get("report_step") is not None


def test_role_registry_registers_report_step_for_experts() -> None:
    from mewcode.expert_group import ExpertExecutionPolicy

    parent = create_default_registry()
    policy = ExpertExecutionPolicy(
        strategy="fast", allow_delegation=True, max_direct_agents=8
    )
    for role in ("manager", "planner", "executor", "auditor"):
        registry = _role_registry(FakeSession(parent), role, policy)
        assert registry.get("report_step") is not None
        assert registry.is_discovered("report_step")
        assert registry.get("human_oncall") is not None
        assert registry.is_discovered("human_oncall")


def test_copy_discovered_tools_marks_deferred_ask_user() -> None:
    source = create_default_registry()
    source.register(AskUserTool())
    source.mark_discovered("AskUserQuestion")
    dest = create_default_registry()
    dest.register(source.get("AskUserQuestion"))
    assert dest.is_discovered("AskUserQuestion") is False
    _copy_discovered_tools(source, dest)
    assert dest.is_discovered("AskUserQuestion") is True


@pytest.mark.asyncio
async def test_agent_report_step_tool_result() -> None:
    registry = create_default_registry()
    agent = Agent(
        _ScriptedClient(
            [
                [
                    ToolCallComplete(
                        "call-step",
                        "report_step",
                        {"label": "准备查找网络", "status": "running"},
                    ),
                    StreamEnd("end_turn", input_tokens=10, output_tokens=5),
                ],
                [
                    TextDelta("继续。"),
                    StreamEnd("end_turn", input_tokens=20, output_tokens=8),
                ],
            ]
        ),
        registry,
        "anthropic",
    )
    conversation = ConversationManager()
    conversation.add_user_message("开始")

    results: list[ToolResultEvent] = []

    async def drive() -> None:
        async for event in agent.run(conversation):
            if isinstance(event, ToolResultEvent):
                results.append(event)

    await asyncio.wait_for(drive(), timeout=5)

    assert len(results) == 1
    assert results[0].tool_name == "report_step"
    assert results[0].output == "STEP\trunning\t准备查找网络"
