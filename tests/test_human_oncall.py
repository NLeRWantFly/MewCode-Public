from __future__ import annotations

import json
from typing import Any, AsyncIterator

import pytest
from pydantic import ValidationError

from mewcode.agent import Agent, HumanOnCallRequest, ToolResultEvent
from mewcode.client import LLMClient
from mewcode.conversation import ConversationManager
from mewcode.tools import create_default_registry
from mewcode.tools.base import StreamEnd, StreamEvent, TextDelta, ToolCallComplete
from mewcode.tools.human_oncall import (
    HumanOnCallParams,
    HumanOnCallTool,
    validate_human_answers,
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


def _arguments() -> dict[str, Any]:
    return {
        "question": "选择发布方式",
        "context": "这会决定后续是否直接写入生产环境。",
        "options": [
            {
                "id": "draft",
                "label": "创建草稿",
                "description": "先生成可审核版本",
                "recommended": True,
            },
            {
                "id": "publish",
                "label": "直接发布",
                "description": "立即写入生产环境",
            },
        ],
        "allow_other": False,
    }


def test_human_oncall_schema_rejects_duplicate_ids() -> None:
    payload = _arguments()
    payload["options"][1]["id"] = "draft"
    with pytest.raises(ValidationError, match="option ids must be unique"):
        HumanOnCallParams.model_validate(payload)


def test_validate_human_answers_rejects_unlisted_choice() -> None:
    tool = HumanOnCallTool()
    params = HumanOnCallParams.model_validate(_arguments())
    questions = tool.build_questions(params)
    with pytest.raises(ValueError, match="invalid answer"):
        validate_human_answers(questions, {"choice": "surprise"})


@pytest.mark.asyncio
async def test_agent_waits_for_human_and_continues_from_tool_result() -> None:
    client = _ScriptedClient(
        [
            [
                ToolCallComplete("call-1", "human_oncall", _arguments()),
                StreamEnd("end_turn", input_tokens=10, output_tokens=5),
            ],
            [
                TextDelta("已按草稿方式继续。"),
                StreamEnd("end_turn", input_tokens=20, output_tokens=8),
            ],
        ]
    )
    agent = Agent(client, create_default_registry(), "anthropic")
    conversation = ConversationManager()
    conversation.add_user_message("准备发布")

    requests: list[HumanOnCallRequest] = []
    results: list[ToolResultEvent] = []
    async for event in agent.run(conversation):
        if isinstance(event, HumanOnCallRequest):
            requests.append(event)
            event.future.set_result({"choice": "draft"})
        elif isinstance(event, ToolResultEvent):
            results.append(event)

    assert len(requests) == 1
    assert requests[0].request_id == "call-1"
    assert len(results) == 1
    payload = json.loads(results[0].output)
    assert payload == {
        "status": "answered",
        "choice": {"id": "draft", "label": "创建草稿", "is_other": False},
    }
    assert conversation.history[-1].content == "已按草稿方式继续。"


@pytest.mark.asyncio
async def test_cancel_is_a_structured_non_error_result() -> None:
    tool = HumanOnCallTool()
    params = HumanOnCallParams.model_validate(_arguments())
    result = tool.format_answers(params, None)
    assert result.is_error is False
    assert json.loads(result.output)["status"] == "cancelled"


@pytest.mark.asyncio
async def test_noninteractive_execution_fails_fast() -> None:
    tool = HumanOnCallTool()
    result = await tool.execute(HumanOnCallParams.model_validate(_arguments()))
    assert result.is_error is True
    assert "non-interactive" in result.output


@pytest.mark.asyncio
async def test_run_to_completion_returns_tool_error_instead_of_waiting() -> None:
    client = _ScriptedClient(
        [
            [
                ToolCallComplete("call-1", "human_oncall", _arguments()),
                StreamEnd("end_turn", input_tokens=10, output_tokens=5),
            ],
            [
                TextDelta("无法交互，已安全退出。"),
                StreamEnd("end_turn", input_tokens=20, output_tokens=8),
            ],
        ]
    )
    agent = Agent(client, create_default_registry(), "anthropic")

    result = await agent.run_to_completion("准备发布")

    assert result == "无法交互，已安全退出。"
