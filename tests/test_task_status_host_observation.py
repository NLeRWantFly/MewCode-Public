"""Structured host observation rides alongside truncated model-facing output."""
import json
import re
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from mewcode.agent import Agent
from mewcode.agents.task_manager import BackgroundTask, TaskManager
from mewcode.conversation import ConversationManager
from mewcode.permissions.reviewer import SafetyReviewer
from mewcode.tools import create_default_registry
from mewcode.tools.agent_status import (
    AgentStatus, BackgroundParams, BackgroundTasks, StatusParams, _task_observation,
)
from mewcode.tools.base import StreamEnd, TextDelta, Tool, ToolCallComplete, ToolResult
from tests.test_permissions import MockLLMClient


def owner(tmp_path, *, session_id="owner"):
    return SimpleNamespace(
        session_id=session_id, work_dir=str(tmp_path),
        registry=create_default_registry(), permission_mode="auto",
        client=SimpleNamespace(model="m", effort="x", _usage_provider_name="p"),
        _chat_turn_id="turn", _chat_turn_baseline_task_ids=frozenset(),
        verification_routes=[])


def finished(identity, tmp_path, *, session_id="owner"):
    return BackgroundTask(
        id=identity, name=identity, agent=None,
        task="核对候选清单并汇总结果 " + "x" * 380,
        status="completed", result="PASS",
        owner_session_id=session_id, owner_scope="project",
        owner_store_dir=str(tmp_path),
        lifecycle_finalized=True, cleanup_confirmed=True,
        native_execution={"phase": "finished", "process_started": True,
                          "cleanup_confirmed": True})


@pytest.mark.asyncio
async def test_real_background_tools_emit_host_observation_without_changing_output(tmp_path):
    agent = owner(tmp_path)
    manager = TaskManager()
    manager._tasks["A1"] = finished("A1", tmp_path)
    manager._tasks["A2"] = finished("A2", tmp_path)
    tool = BackgroundTasks(agent, manager)

    listing = await tool.execute(BackgroundParams())
    output = json.loads(listing.output)
    assert output["view"] == "task_list_compact" and output["task_count"] == 2
    assert output["returned"] == 2 and output["next_offset"] is None
    rows = [dict(zip(output["columns"], row)) for row in output["tasks"]]
    assert [row["id"] for row in rows] == ["A1", "A2"]
    assert rows[0]["status"] == "completed" and rows[0]["cleanup_confirmed"] is True
    for column in ("task", "result", "task_summary"):
        assert column not in output["columns"]
    assert listing.host_observation == [
        {key: value for key, value in
         _task_observation(agent, manager, manager._tasks[identity], summary=True).items()
         if key != "task_summary"}
        for identity in ("A1", "A2")]

    detail = await tool.execute(BackgroundParams(task_id="A1"))
    obs = json.loads(detail.output)
    assert obs["task"].endswith("x" * 380) and obs["result"] == "PASS"
    assert detail.host_observation == {
        key: value for key, value in obs.items() if key not in {"task", "result"}}
    assert "task" not in detail.host_observation

    missing = await tool.execute(BackgroundParams(task_id="missing"))
    assert missing.is_error and missing.host_observation is None

    no_manager = BackgroundTasks(agent, None)
    empty = await no_manager.execute(BackgroundParams())
    assert json.loads(empty.output)["task_count"] == 0 and empty.host_observation == []
    denied = await no_manager.execute(BackgroundParams(task_id="A1"))
    assert denied.is_error and denied.host_observation is None


@pytest.mark.asyncio
async def test_real_agent_status_output_unchanged_and_host_observation_is_scoped(tmp_path):
    agent = owner(tmp_path)
    manager = TaskManager()
    manager._tasks["A1"] = finished("A1", tmp_path)
    status = AgentStatus(agent, manager)
    result = await status.execute(StatusParams())
    payload = json.loads(result.output)
    for key in ("tools", "capabilities", "browser_connected", "current_turn",
                "tasks", "model_availability", "configured_model"):
        assert key in payload
    assert payload["tasks"]["view"] == "task_list_compact"
    assert payload["tasks"]["task_count"] == 1
    assert dict(zip(payload["tasks"]["columns"],
                    payload["tasks"]["tasks"][0]))["id"] == "A1"
    assert set(result.host_observation) == {
        "host_work_dir", "ui_endpoint", "permission_mode", "runtime_route", "current_turn", "tasks"}
    assert result.host_observation["permission_mode"] == payload["permission_mode"]
    assert result.host_observation["current_turn"] == payload["current_turn"]
    assert result.host_observation["runtime_route"] == payload["runtime_route"]
    assert result.host_observation["ui_endpoint"] == payload["ui_endpoint"]
    assert result.host_observation["tasks"] == [
        {key: value for key, value in
         _task_observation(agent, manager, manager._tasks["A1"], summary=True).items()
         if key != "task_summary"}]


class EmptyParams(BaseModel):
    pass


class HugeOutput(Tool):
    name = "HugeOutput"
    description = "test tool whose output exceeds the conversation limit"
    params_model = EmptyParams
    category = "read"
    consumes_runtime_slot = False

    async def execute(self, params):
        return ToolResult(output="x" * 12000, host_observation={"k": 1})


def _result_block(conversation, call_id):
    return next(block for message in conversation.history
                for block in message.tool_results if block.tool_use_id == call_id)


@pytest.mark.asyncio
async def test_run_carries_host_observation_past_output_truncation(tmp_path):
    registry = create_default_registry()
    registry.register(HugeOutput())
    client = MockLLMClient([
        [ToolCallComplete("big", "HugeOutput", {}), StreamEnd("tool_use")],
        [TextDelta("done"), StreamEnd("end_turn")],
    ])
    agent = Agent(client, registry, "anthropic", work_dir=str(tmp_path))
    conversation = ConversationManager()
    conversation.add_user_message("run the tool")
    async for _ in agent.run(conversation):
        pass
    block = _result_block(conversation, "big")
    ref = re.search(r'ref_id="([0-9a-f]{64})"', block.content)
    assert ref and agent._get_working_set().archive.load(ref[1])["text"] == "x" * 12_000
    assert block.host_observation == {"k": 1}


@pytest.mark.asyncio
async def test_run_to_completion_carries_host_observation_past_output_truncation(tmp_path):
    registry = create_default_registry()
    registry.register(HugeOutput())
    client = MockLLMClient([
        [ToolCallComplete("big", "HugeOutput", {}), StreamEnd("tool_use")],
        [TextDelta("done"), StreamEnd("end_turn")],
    ])
    agent = Agent(client, registry, "anthropic", work_dir=str(tmp_path), max_iterations=5)
    conversation = ConversationManager()
    await agent.run_to_completion("run the tool", conversation=conversation)
    block = _result_block(conversation, "big")
    ref = re.search(r'ref_id="([0-9a-f]{64})"', block.content)
    assert ref and agent._get_working_set().archive.load(ref[1])["text"] == "x" * 12_000
    assert block.host_observation == {"k": 1}


@pytest.mark.asyncio
async def test_compact_list_pages_all_tasks_without_truncation(tmp_path):
    agent = owner(tmp_path)
    manager = TaskManager()
    ids = [f"A{n:02d}" for n in range(40)]
    for identity in ids:
        manager._tasks[identity] = finished(identity, tmp_path)
    manager._tasks["A39"].dispatch_turn_id = "turn"
    tool = BackgroundTasks(agent, manager)
    seen, offset = [], 0
    while True:
        result = await tool.execute(BackgroundParams(offset=offset))
        assert len(result.output) <= 10000
        page = json.loads(result.output)
        assert page["task_count"] == 40 and page["offset"] == offset
        assert page["current_turn_task_ids"] == ["A39"]
        assert len(result.host_observation) == 40
        seen += [row[0] for row in page["tasks"]]
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert seen == ids
    past = json.loads((await tool.execute(BackgroundParams(offset=len(ids)))).output)
    assert past["task_count"] == 40 and past["returned"] == 0
    assert past["next_offset"] is None


@pytest.mark.asyncio
async def test_agent_status_compact_payload_stays_under_limit(tmp_path):
    agent = owner(tmp_path)
    for n in range(60):
        agent.registry.register(SimpleNamespace(name=f"dummy-{n:02d}", category="read"))
    manager = TaskManager()
    for identity in [f"A{n:02d}" for n in range(40)]:
        manager._tasks[identity] = finished(identity, tmp_path)
    result = await AgentStatus(agent, manager).execute(StatusParams())
    assert len(result.output) <= 10000
    payload = json.loads(result.output)
    order = list(payload)
    assert order.index("permission_mode") < order.index("tools")
    assert order.index("current_turn") < order.index("tools")
    assert order.index("tasks") < order.index("tools")
    assert order.index("tasks") < order.index("capabilities")
    assert payload["tasks"]["task_count"] == 40
    assert len(result.host_observation["tasks"]) == 40
    assert all("task_summary" not in task for task in result.host_observation["tasks"])


@pytest.mark.asyncio
async def test_compact_list_empty_state(tmp_path):
    agent = owner(tmp_path)
    manager = TaskManager()
    result = await BackgroundTasks(agent, manager).execute(BackgroundParams())
    page = json.loads(result.output)
    assert page["task_count"] == 0 and page["returned"] == 0
    assert page["next_offset"] is None and page["tasks"] == []
    assert result.host_observation == []


@pytest.mark.asyncio
async def test_real_loop_inventory_survives_compact_model_output(tmp_path):
    registry = create_default_registry()
    manager = TaskManager()
    client = MockLLMClient([
        [ToolCallComplete("list", "BackgroundTasks", {}), StreamEnd("tool_use")],
        [TextDelta("done"), StreamEnd("end_turn")],
    ])
    agent = Agent(client, registry, "anthropic", work_dir=str(tmp_path))
    registry.register(BackgroundTasks(agent, manager))
    ids = [f"A{n:02d}" for n in range(40)]
    for identity in ids:
        manager._tasks[identity] = finished(identity, tmp_path,
                                          session_id=agent.session_id)
    conversation = ConversationManager()
    conversation.add_user_message("检查后台任务清单")
    async for _ in agent.run(conversation):
        pass
    block = _result_block(conversation, "list")
    assert not block.content.endswith("(output truncated)")
    assert json.loads(block.content)["task_count"] == 40
    rows = SafetyReviewer._evidence_context(conversation)
    inventory = next((json.loads(row["text"]) for row in rows
                      if json.loads(row["text"]).get("observation_kind")
                      == "task_inventory_at_query_time"), None)
    assert inventory is not None
    assert inventory["inventory_complete"] is True and inventory["task_count"] == 40
    assert [entry[0] for entry in inventory["tasks"]] == ids
