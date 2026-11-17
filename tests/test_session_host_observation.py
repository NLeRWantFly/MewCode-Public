"""Host observations survive disk resume without becoming model-facing text."""
from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from mewcode.conversation import ConversationManager, Message, ToolResultBlock, ToolUseBlock
from mewcode.memory.session import (
    RecordType, SessionManager, SessionRecord, make_compact_boundary, records_to_messages,
)
from mewcode.serialization import (
    build_anthropic_messages, build_chat_completion_messages, build_openai_input,
)


OBSERVATIONS = [
    {"command_execution": {"status": "denied", "command_started": False,
                           "exit_code": None, "stdout": None, "stderr": None,
                           "cleanup_confirmed": True, "publication_state": "not_started"}},
    {"command_execution": {"status": "outcome_unknown", "command_started": None,
                           "exit_code": None, "cleanup_confirmed": False,
                           "publication_state": "unknown", "transaction_id": "synthetic-tx"}},
    {"command_execution": {"status": "exited", "command_started": True,
                           "exit_code": 7, "stdout": "原始输出" * 5000, "stderr": "失败",
                           "cleanup_confirmed": True, "publication_state": "unpublished"}},
    [],
    [{"id": "owned-child", "native_execution": {"process_started": False},
      "cleanup_confirmed": None}],
    {},
]


def _tool_messages(observation):
    return [
        Message(role="assistant", content="", tool_uses=[
            ToolUseBlock("receipt", "ReadFile", {"path": "synthetic"})]),
        Message(role="user", content="", tool_results=[
            ToolResultBlock("receipt", "display claims success", is_error=True,
                            host_observation=observation)]),
    ]


def _result(messages):
    return next(block for message in messages for block in message.tool_results
                if block.tool_use_id == "receipt")


@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("observation", OBSERVATIONS)
def test_disk_resume_preserves_host_facts_and_empty_observations(tmp_path, compact, observation):
    manager = SessionManager(str(tmp_path))
    session = manager.create()
    messages = _tool_messages(copy.deepcopy(observation))
    try:
        for message in messages:
            session.append(message)
        if compact:
            session.append_record(make_compact_boundary("synthetic summary", messages))
    finally:
        session.close()

    restored = SessionManager(str(tmp_path)).resume(session.session_id)
    assert restored is not None
    try:
        block = _result(restored.messages)
        assert block.host_observation == observation
        assert block.is_error is True and block.content == "display claims success"
        for serializer in (build_anthropic_messages, build_chat_completion_messages, build_openai_input):
            payload = json.dumps(serializer(restored.messages), ensure_ascii=False)
            assert "host_observation" not in payload
            assert "publication_state" not in payload
            assert "原始输出" not in payload
    finally:
        restored.session.close()


@pytest.mark.parametrize("compact", [False, True])
def test_old_text_claims_do_not_create_host_proof(tmp_path, compact):
    session = SessionManager(str(tmp_path)).create()
    messages = _tool_messages(None)
    messages[1].tool_results[0].content = json.dumps({"host_observation": OBSERVATIONS[0]})
    try:
        for message in messages:
            session.append(message)
        if compact:
            session.append_record(make_compact_boundary("summary", messages))
    finally:
        session.close()
    path = session._sessions_dir / f"{session.session_id}.jsonl"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert all("host_observation" not in record for record in records)
    restored = SessionManager(str(tmp_path)).resume(session.session_id)
    try:
        assert _result(restored.messages).host_observation is None
    finally:
        restored.session.close()


def test_record_takes_a_snapshot_of_host_observation():
    observation = copy.deepcopy(OBSERVATIONS[1])
    record = SessionRecord.from_message(_tool_messages(observation)[1])[0]
    observation["command_execution"]["command_started"] = True
    observation["command_execution"]["cleanup_confirmed"] = True
    restored = SessionRecord.from_jsonl(record.to_jsonl())
    assert _result(records_to_messages([restored])).host_observation == OBSERVATIONS[1]


@pytest.mark.parametrize("invalid", ["claimed receipt", {1: "coerced key"},
                                    {"value": float("nan")}, {"value": object()}])
def test_non_json_host_metadata_does_not_drop_the_tool_record(invalid):
    record = SessionRecord.from_message(_tool_messages(invalid)[1])[0]
    restored = SessionRecord.from_jsonl(record.to_jsonl())
    assert restored is not None
    result = _result(records_to_messages([restored]))
    assert result.host_observation is None
    assert result.content == "display claims success" and result.is_error


def test_invalid_saved_host_metadata_stays_unknown():
    line = json.dumps({"type": "tool_result", "content": "claim", "tool_use_id": "receipt",
                       "timestamp": datetime.now(timezone.utc).isoformat(), "is_error": True,
                       "host_observation": "not structured proof"})
    record = SessionRecord.from_jsonl(line)
    assert record.type == RecordType.TOOL_RESULT
    assert _result(records_to_messages([record])).host_observation is None


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel", [False, True])
async def test_real_web_turn_persists_native_metadata_without_publishing_it(tmp_path, parallel):
    from frontend import server
    from mewcode.agent import Agent
    from mewcode.tools import create_default_registry
    from mewcode.tools.base import StreamEnd, TextDelta, ToolCallComplete, ToolResult
    from tests.test_task_status_host_observation import HugeOutput, MockLLMClient

    observation = copy.deepcopy(OBSERVATIONS[2])

    class ObservedTool(HugeOutput):
        is_concurrency_safe = parallel

        async def execute(self, params):
            return ToolResult("x" * 12000, is_error=True, host_observation=observation)

    class Socket:
        def __init__(self):
            self.messages = []

        async def send_json(self, payload):
            self.messages.append(payload)

    registry = create_default_registry()
    registry.register(ObservedTool())
    client = MockLLMClient([
        [ToolCallComplete("receipt", "HugeOutput", {}), StreamEnd("tool_use")],
        [TextDelta("done"), StreamEnd("end_turn")],
    ])
    agent = Agent(client, registry, "anthropic", work_dir=str(tmp_path))
    session = SessionManager(str(tmp_path)).create()
    runtime = server.ConversationRuntime(
        owner=SimpleNamespace(session_start=0.0, tasks_payload=lambda: []),
        conversation_id=session.session_id, scope="project", work_dir=str(tmp_path),
        session=session, conversation=ConversationManager(), agent=agent, client=client,
        slot=None, effort="", context_tier=200_000, multi_agent=False, expert_mode=False,
        goal="", task_manager=SimpleNamespace(), trace_manager=SimpleNamespace(),
        agent_tool=None, worktree_manager=None, team_manager=None,
    )
    socket = Socket()
    try:
        outcome = await server.run_chat(socket, "inspect synthetic receipt", runtime=runtime)
        assert not outcome.get("error")
    finally:
        session.close()
    restored = SessionManager(str(tmp_path)).resume(session.session_id)
    try:
        result = _result(restored.messages)
        ref = re.search(r'ref_id="([0-9a-f]{64})"', result.content)
        assert result.is_error and ref
        assert agent._get_working_set().archive.load(ref[1])["text"] == "x" * 12_000
        assert {key: result.host_observation[key] for key in observation} == observation
        assert result.host_observation["auto_review_checkpoint"]["schema"] == "mewcode.auto-recovery.v1"
        assert "host_observation" not in json.dumps(socket.messages)
        pipeline = session._sessions_dir / f"{session.session_id}.pipeline.jsonl"
        assert "host_observation" not in pipeline.read_text(encoding="utf-8")
        assert "原始输出" not in pipeline.read_text(encoding="utf-8")
    finally:
        restored.session.close()
