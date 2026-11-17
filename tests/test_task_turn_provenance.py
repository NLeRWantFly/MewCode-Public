import json
from types import SimpleNamespace

import pytest

from mewcode.agents.task_manager import TaskManager, BackgroundTask
from mewcode.tools.agent_status import BackgroundTasks, BackgroundParams, _current_turn
from mewcode.tools.agent_tool import AgentTool


@pytest.mark.asyncio
async def test_launch_provenance_survives_restart_and_queries_do_not_relabel(tmp_path):
    class Agent:
        agent_id = "child"
        team_name = ""
        total_input_tokens = total_output_tokens = total_tool_calls = 0
        runtime_scheduler = None
        async def run_to_completion(self, *args, **kwargs):
            return "complete"

    path = tmp_path / "tasks.json"
    manager = TaskManager(path)
    owner = SimpleNamespace(_chat_turn_id="turn-A4", _chat_task_id="parent",
        _chat_task_title="acceptance", session_id="session", work_dir=str(tmp_path))
    tool = SimpleNamespace(_parent_agent=owner)
    task_id = manager.launch(Agent(), "x" * 400, name="A4", **AgentTool._task_owner(tool))
    await manager._async_tasks[task_id]
    restored = TaskManager(path)
    query = BackgroundTasks(owner, restored)
    listed = json.loads((await query.execute(BackgroundParams())).output)
    assert listed["view"] == "task_list_compact" and listed["task_count"] == 1
    row = dict(zip(listed["columns"], listed["tasks"][0]))
    assert row["id"] == task_id and row["dispatch_relation"] == "current_turn"
    # Compact rows carry no task text, result or summary; details stay behind task_id.
    for column in ("task", "result", "task_summary"):
        assert column not in listed["columns"]
    full = json.loads((await query.execute(BackgroundParams(task_id=task_id))).output)
    assert full["created_at"] and full["dispatch_turn_id"] == "turn-A4"
    assert full["result"] == "complete" and full["task"] == "x" * 400
    assert _current_turn(owner, restored)["dispatched_task_ids"] == [task_id]
    owner._chat_turn_id = "turn-A5"
    owner._chat_turn_baseline_task_ids = frozenset({task_id})
    prior = json.loads((await query.execute(BackgroundParams(task_id=task_id))).output)
    assert prior["dispatch_relation"] == "existing_before_turn"
    assert prior["dispatch_turn_id"] == "turn-A4"
    assert _current_turn(owner, restored)["dispatched_task_ids"] == []
    assert restored.task_payload(task_id)["created_at"] == full["created_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize("recorded,current,baseline,expected", [
    ("", "", False, "unknown"), ("", "current", False, "unknown"),
    ("", "current", True, "existing_before_turn"),
    ("old", "current", False, "other_turn"),
    ("current", "current", False, "current_turn"),
])
async def test_no_invented_legacy_provenance(tmp_path, recorded, current, baseline, expected):
    manager = TaskManager()
    task = BackgroundTask(id="old", name="old", agent=None, task="historical",
        status="completed", owner_session_id="session", owner_store_dir=str(tmp_path),
        dispatch_turn_id=recorded)
    manager._tasks[task.id] = task
    agent = SimpleNamespace(session_id="session", work_dir=str(tmp_path),
        _chat_turn_id=current, _chat_turn_baseline_task_ids=frozenset({"old"} if baseline else set()))
    tool = BackgroundTasks(agent, manager)
    before = manager.task_payload(task.id)
    listing = json.loads((await tool.execute(BackgroundParams())).output)
    item = dict(zip(listing["columns"], listing["tasks"][0]))
    assert item["dispatch_relation"] == expected
    detail = json.loads((await tool.execute(BackgroundParams(task_id="old"))).output)
    assert detail["created_at"] is None
    assert manager.task_payload(task.id) == before
    assert not manager._async_tasks


@pytest.mark.asyncio
async def test_provenance_does_not_expose_other_conversations(tmp_path):
    from tests.test_agent_capabilities import parent
    from mewcode.tools.agent_status import AgentStatus

    manager = TaskManager()
    agent = parent(tmp_path)
    agent._chat_turn_id = "current"
    for identity, session in [("mine", "owner"), ("foreign", "another")]:
        manager._tasks[identity] = BackgroundTask(
            id=identity, name=identity, agent=None, task="original " + identity,
            owner_session_id=session, owner_store_dir=str(tmp_path),
            dispatch_turn_id="current")
    status = AgentStatus(agent, manager)
    payload = json.loads((await status.execute(status.params_model())).output)
    assert payload["current_turn"]["dispatched_task_ids"] == ["mine"]
    listed = payload["tasks"]
    rows = [dict(zip(listed["columns"], row)) for row in listed["tasks"]]
    assert listed["task_count"] == 1 and [item["id"] for item in rows] == ["mine"]
    assert "task" not in listed["columns"] and "result" not in listed["columns"]
    query = BackgroundTasks(agent, manager)
    assert (await query.execute(BackgroundParams(task_id="foreign"))).is_error
