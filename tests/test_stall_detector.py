import pytest
from pydantic import BaseModel

from mewcode.agent import Agent, ErrorEvent
from mewcode.conversation import ConversationManager
from mewcode.stall import StallDetector, StallPolicy
from mewcode.tools import ToolRegistry
from mewcode.tools.base import Tool, ToolResult, ToolCallComplete, StreamEnd
from tests.test_agent import MockLLMClient


def test_new_read_targets_do_not_count_as_stale_rounds():
    detector = StallDetector(StallPolicy(), executor=True)
    for i in range(20):
        assert detector.observe_tool("ReadFile", {"path": str(i)}, is_error=False, read_only=True) is None
        assert detector.end_round() is None


def test_same_failed_command_warns_then_stops():
    detector = StallDetector(StallPolicy())
    results = [detector.observe_tool("Bash", {"command": "check"}, is_error=True, read_only=False) for _ in range(6)]
    assert results[2][0] == "warn" and results[5][0] == "stop"


class CommandParams(BaseModel):
    command: str


class FakeCommand(Tool):
    name = "Bash"
    description = "offline failing command"
    category = "read"
    params_model = CommandParams

    async def execute(self, params):
        return ToolResult(output="test error", is_error=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("interactive,enabled", [(True, True), (False, True), (True, False)])
async def test_agent_stall_detection_in_both_runtime_paths(tmp_path, interactive, enabled):
    client = MockLLMClient([[ToolCallComplete(str(i), "Bash", {"command": "check"}), StreamEnd("tool_use", 10, 1)] for i in range(8)])
    registry = ToolRegistry()
    registry.register(FakeCommand())
    agent = Agent(client, registry, "anthropic", work_dir=str(tmp_path), stall_policy={} if enabled else None)
    conversation = ConversationManager()
    conversation.add_user_message("check offline")
    if interactive:
        events = [e async for e in agent.run(conversation)]
        assert any(isinstance(e, ErrorEvent) and e.kind == "stalled" for e in events) == enabled
    else:
        events = []
        await agent.run_to_completion("", conversation, event_callback=events.append)
        assert any(e.get("kind") == "stalled" for e in events)
    assert client._call_index == (6 if enabled else 8)
    assert any("检测到重复/无进展" in str(m.content) for m in conversation.history) == enabled
    assert conversation.repair_incomplete_tool_turns() == 0


def test_operation_blocks_do_not_trigger_global_error_or_idle_stop():
    detector = StallDetector(StallPolicy(repeated_calls=2, consecutive_errors=2,
        stale_rounds=2, repeated_failed_commands=2), executor=True)
    for _ in range(20):
        assert detector.observe_tool("Bash", {"command": "blocked"}, is_error=True,
            read_only=False, operation_blocked=True) is None
        assert detector.end_round() is None
    assert not detector.warned and detector.errors == 0 and not detector.failed_commands
    assert detector.observe_tool("WriteFile", {"file_path": "independent.txt"},
        is_error=False, read_only=False) is None
    assert detector.end_round() is None
