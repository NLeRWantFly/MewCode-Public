from __future__ import annotations

from typing import Any, AsyncIterator

import pytest
from pydantic import BaseModel

from mewcode.agent import (
    Agent,
    _migrate_legacy_mode_reminders,
    _ORCHESTRATION_REMINDER_MARKER,
    _PLAN_MODE_REMINDER_MARKER,
)
from mewcode.client import LLMClient
from mewcode.conversation import ConversationManager, Message, ToolUseBlock
from mewcode.permissions import PermissionMode
from mewcode.prompts import build_plan_mode_reminder
from mewcode.tools import ToolRegistry
from mewcode.tools.base import StreamEnd, StreamEvent, TextDelta, Tool, ToolCallComplete, ToolResult


class _NoParams(BaseModel):
    pass


class _ModeSwitchTool(Tool):
    name = "TestSwitchMode"
    description = "Switch from actual Plan mode into ordinary-permission workflow design."
    params_model = _NoParams
    category = "read"

    def __init__(self, agent: Agent):
        self.agent = agent

    async def execute(self, _params: BaseModel) -> ToolResult:
        self.agent.set_permission_mode(PermissionMode.DEFAULT)
        self.agent.orchestration_planning = True
        return ToolResult("mode changed")


class _CapturingClient(LLMClient):
    def __init__(self, responses: list[list[StreamEvent]]):
        self.responses = list(responses)
        self.snapshots: list[list[str]] = []

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.snapshots.append([
            str(message.content)
            for message in conversation.history
            if _PLAN_MODE_REMINDER_MARKER in str(message.content)
            or _ORCHESTRATION_REMINDER_MARKER in str(message.content)
        ])
        events = self.responses.pop(0)
        for event in events:
            yield event


def test_legacy_reminder_migration_only_removes_known_complete_host_messages():
    conversation = ConversationManager()
    old_full = build_plan_mode_reminder("/tmp/legacy-plan.md", False, 1)
    old_sparse = build_plan_mode_reminder("/tmp/legacy-plan.md", True, 7)
    old_orchestration = (
        "编排商量模式已开启。用户现在不要你动手执行任务，只要你根据已知要求整理流程，并产出可查看的 Plan 执行流。"
        "\n\n旧版后续说明"
    )
    conversation.add_system_reminder(old_full)
    conversation.add_system_reminder(old_sparse)
    conversation.add_system_reminder(old_orchestration)
    conversation.add_user_message(f"请保留这段引用：{old_full[:80]}")

    incomplete = Message(
        role="user",
        content="<system-reminder>\n" + old_orchestration,
    )
    tool_message = Message(
        role="user",
        content=f"<system-reminder>\n{old_full}\n</system-reminder>",
        tool_uses=[ToolUseBlock("tool-1", "ReadFile", {"file_path": "x.txt"})],
    )
    conversation.history.extend([incomplete, tool_message])

    removed = _migrate_legacy_mode_reminders(conversation)

    assert removed == 3
    assert any(message.content.startswith("请保留这段引用：") for message in conversation.history)
    assert incomplete in conversation.history
    assert tool_message in conversation.history
    ordinary_messages = [
        str(message.content) for message in conversation.history
        if not message.tool_uses and not message.tool_results and not message.content_blocks
    ]
    assert not any(content.startswith("<system-reminder>\nPlan mode is active.") for content in ordinary_messages)
    assert not any(content.startswith("<system-reminder>\nPlan mode still active") for content in ordinary_messages)
    remaining_old_orchestration = [
        content for content in ordinary_messages
        if content.startswith("<system-reminder>\n编排商量模式已开启。")
    ]
    assert remaining_old_orchestration == [incomplete.content]


def test_workflow_design_reminder_does_not_create_a_plan_file(tmp_path):
    agent = Agent(_CapturingClient([]), ToolRegistry(), "anthropic", work_dir=str(tmp_path))
    agent.orchestration_planning = True
    conversation = ConversationManager()

    agent._update_mode_reminders(conversation, 1)

    assert agent._plan_path_cache is None
    assert not (tmp_path / ".mewcode" / "plans").exists()
    reminder = next(message for message in conversation.history if _ORCHESTRATION_REMINDER_MARKER in message.content)
    assert reminder.transient


def test_plan_reminder_keeps_full_instructions_on_late_iterations(tmp_path):
    agent = Agent(_CapturingClient([]), ToolRegistry(), "anthropic", work_dir=str(tmp_path))
    agent.set_permission_mode(PermissionMode.PLAN)
    conversation = ConversationManager()

    agent._update_mode_reminders(conversation, 7)

    reminder = next(message for message in conversation.history if _PLAN_MODE_REMINDER_MARKER in message.content)
    assert "Plan mode is active" in reminder.content
    assert "MUST NOT" in reminder.content
    assert "## Plan Workflow" in reminder.content
    assert "see full instructions earlier in conversation" not in reminder.content


@pytest.mark.asyncio
async def test_mode_reminders_replace_across_rounds_prioritize_plan_and_clear_on_exit(tmp_path):
    client = _CapturingClient([
        [
            ToolCallComplete("switch-1", "TestSwitchMode", {}),
            StreamEnd("end_turn", input_tokens=1, output_tokens=1),
        ],
        [TextDelta("设计流程已更新。"), StreamEnd("end_turn", input_tokens=1, output_tokens=1)],
        [TextDelta("下一轮没有模式提醒。"), StreamEnd("end_turn", input_tokens=1, output_tokens=1)],
    ])
    registry = ToolRegistry()
    agent = Agent(client, registry, "anthropic", work_dir=str(tmp_path), stall_policy=False)
    registry.register(_ModeSwitchTool(agent))
    agent.set_permission_mode(PermissionMode.PLAN)
    # Even if an old host state overlaps, the real Plan permission mode wins.
    agent.orchestration_planning = True

    conversation = ConversationManager()
    conversation.add_system_reminder(build_plan_mode_reminder("/tmp/old.md", False, 1))
    conversation.add_system_reminder(
        "编排商量模式已开启。用户现在不要你动手执行任务，只要你根据已知要求整理流程，并产出可查看的 Plan 执行流。旧禁令"
    )
    quoted = "我引用旧文案：‘编排商量模式已开启。用户现在不要你动手执行任务’。"
    conversation.add_user_message(quoted)

    async for _event in agent.run(conversation):
        pass

    assert len(client.snapshots) == 2
    first, second = client.snapshots
    assert len(first) == 1
    assert _PLAN_MODE_REMINDER_MARKER in first[0]
    assert "Plan mode is active" in first[0] and "MUST NOT" in first[0]
    assert "编排商量模式已开启" not in first[0]
    assert len(second) == 1
    assert _ORCHESTRATION_REMINDER_MARKER in second[0]
    assert _PLAN_MODE_REMINDER_MARKER not in second[0]
    assert "沿用用户当前的实际权限" in second[0]
    assert "Plan mode is active" not in second[0]
    assert not any(
        _PLAN_MODE_REMINDER_MARKER in str(message.content)
        or _ORCHESTRATION_REMINDER_MARKER in str(message.content)
        for message in conversation.history
    )
    assert any(message.content == quoted for message in conversation.history)

    agent.orchestration_planning = False
    conversation.add_user_message("新的普通对话轮次")
    async for _event in agent.run(conversation):
        pass
    assert client.snapshots[2] == []
    assert any(message.content == quoted for message in conversation.history)
