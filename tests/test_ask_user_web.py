from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

import pytest

from mewcode.agent import Agent, AskUserRequest, ToolResultEvent
from mewcode.client import LLMClient
from mewcode.conversation import ConversationManager
from mewcode.tools import create_default_registry
from mewcode.tools.ask_user import AskUserTool
from mewcode.tools.base import StreamEnd, StreamEvent, TextDelta, ToolCallComplete


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


def _arguments() -> dict[str, Any]:
    return {
        "purpose": "user_input",
        "questions": [
            {
                "type": "radio",
                "name": "deploy_env",
                "message": "部署到哪个环境？",
                "options": ["staging", "prod"],
            }
        ]
    }


def _make_agent(responses: list[list[StreamEvent]]) -> Agent:
    registry = create_default_registry()
    registry.register(AskUserTool())
    return Agent(_ScriptedClient(responses), registry, "anthropic")


@pytest.mark.asyncio
async def test_ask_user_request_is_yielded() -> None:
    agent = _make_agent(
        [
            [
                ToolCallComplete("call-1", "AskUserQuestion", _arguments()),
                StreamEnd("end_turn", input_tokens=10, output_tokens=5),
            ],
            [
                TextDelta("已按所选环境继续。"),
                StreamEnd("end_turn", input_tokens=20, output_tokens=8),
            ],
        ]
    )
    conversation = ConversationManager()
    conversation.add_user_message("准备部署")

    requests: list[AskUserRequest] = []
    results: list[ToolResultEvent] = []

    async def drive() -> None:
        async for event in agent.run(conversation):
            if isinstance(event, AskUserRequest):
                requests.append(event)
                event.future.set_result({"deploy_env": "prod"})
            elif isinstance(event, ToolResultEvent):
                results.append(event)

    await asyncio.wait_for(drive(), timeout=5)

    assert len(requests) == 1
    assert requests[0].request_id == "call-1"
    assert requests[0].tool_name == "AskUserQuestion"
    assert requests[0].questions[0]["name"] == "deploy_env"
    assert requests[0].questions[0]["message"] == "部署到哪个环境？"
    assert len(results) == 1
    assert results[0].is_error is False
    assert "deploy_env: prod" in results[0].output
    assert conversation.history[-1].content == "已按所选环境继续。"


def test_pending_runtime_prompts_include_ask_user() -> None:
    from types import SimpleNamespace

    from frontend import server

    future = SimpleNamespace(done=lambda: False)
    request = SimpleNamespace(
        request_id="call-ask",
        tool_name="AskUserQuestion",
        questions=[{"name": "deploy_env", "message": "部署到哪个环境？"}],
        future=future,
    )
    runtime = SimpleNamespace(
        conversation_id="conversation-a",
        permission_future=None,
        human_oncall_request=None,
        ask_user_request=request,
        running=lambda: True,
    )
    session = SimpleNamespace(conversation_runs={"conversation-a": runtime})

    assert server.pending_runtime_prompts(session) == [{
        "type": "ask_user",
        "conversation_id": "conversation-a",
        "request_id": "call-ask",
        "tool": "AskUserQuestion",
        "questions": request.questions,
    }]
