from __future__ import annotations

import httpx
import pytest

from frontend import server
from mewcode.orchestration.task_design import TaskDesign
from mewcode.permissions import PermissionMode
from tests.test_workflow_drafts import api_env


@pytest.fixture
def task_api_env(tmp_path, monkeypatch):
    # Reuse the project's canonical isolated server/session setup without
    # depending on pytest importing a fixture from a different test module.
    current, plans, path, owner = api_env.__wrapped__(tmp_path, monkeypatch)
    mode_changes = []
    monkeypatch.setattr(server.SESSION, "expert_mode", False)
    monkeypatch.setattr(server.SESSION, "orchestration_planning", False)
    monkeypatch.setattr(server.SESSION, "permission_mode", PermissionMode.DEFAULT)

    def set_expert(value):
        mode_changes.append(("expert", value))
        server.SESSION.expert_mode = value

    def set_design(value):
        mode_changes.append(("planning", value))
        server.SESSION.orchestration_planning = value

    monkeypatch.setattr(server.SESSION, "set_expert_mode", set_expert)
    monkeypatch.setattr(server.SESSION, "set_orchestration_planning", set_design)
    return current, plans, path, owner, mode_changes


@pytest.mark.asyncio
async def test_entry_start_is_idempotent_and_does_not_upgrade_the_selected_intent(task_api_env):
    _, _, _, owner, mode_changes = task_api_env
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        first = await client.post("/api/orchestration/task-design", json={**owner, "action": "start", "intent": "plan"})
        assert first.status_code == 200, first.text
        created = first.json()["task_design"]

        second = await client.post("/api/orchestration/task-design", json={**owner, "action": "start", "intent": "execute"})
        assert second.status_code == 200, second.text
        reopened = second.json()["task_design"]
        assert reopened["task_id"] == created["task_id"]
        assert reopened["intent"] == "plan"
        assert reopened["revision"] == created["revision"]

        current = await client.get("/api/orchestration/task-design", params=owner)
        assert current.status_code == 200, current.text
        assert current.json()["task_design"]["task_id"] == created["task_id"]
        assert current.json()["task_design"]["intent"] == "plan"
    assert mode_changes == [("expert", False), ("planning", True), ("expert", False), ("planning", True)]


@pytest.mark.asyncio
async def test_entry_controls_require_the_current_task_revision(task_api_env):
    _, _, _, owner, _ = task_api_env
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        started = await client.post("/api/orchestration/task-design", json={**owner, "action": "start", "intent": "execute"})
        assert started.status_code == 200, started.text
        task = started.json()["task_design"]

        stale = await client.post("/api/orchestration/task-design", json={
            **owner, "action": "pause", "intent": task["intent"],
            "expected_task_id": task["task_id"], "expected_revision": task["revision"] - 1,
        })
        assert stale.status_code == 409
        assert "任务规格已变化" in stale.json()["error"]
        assert TaskDesign(owner["project"], owner["conversation_id"]).read()["status"] == "designing"

        paused = await client.post("/api/orchestration/task-design", json={
            **owner, "action": "pause", "intent": task["intent"],
            "expected_task_id": task["task_id"], "expected_revision": task["revision"],
        })
        assert paused.status_code == 200, paused.text
        paused_task = paused.json()["task_design"]
        assert paused_task["task_id"] == task["task_id"] and paused_task["status"] == "paused"

        resumed = await client.post("/api/orchestration/task-design", json={
            **owner, "action": "resume", "intent": task["intent"],
            "expected_task_id": paused_task["task_id"], "expected_revision": paused_task["revision"],
        })
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["task_design"]["status"] == "designing"


@pytest.mark.asyncio
async def test_entry_rejects_a_different_project_or_conversation(task_api_env):
    _, _, _, owner, _ = task_api_env
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        started = await client.post("/api/orchestration/task-design", json={**owner, "action": "start", "intent": "plan"})
        assert started.status_code == 200
        wrong_project = await client.get("/api/orchestration/task-design", params={
            **owner, "project": owner["project"] + "-other"})
        wrong_conversation = await client.get("/api/orchestration/task-design", params={
            **owner, "conversation_id": "other"})
    assert wrong_project.status_code == wrong_conversation.status_code == 409


@pytest.mark.asyncio
async def test_existing_execute_task_has_a_versioned_plan_only_correction_entry(task_api_env):
    _, _, _, owner, _ = task_api_env
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        started = await client.post("/api/orchestration/task-design", json={**owner, "action": "start", "intent": "execute"})
        created = started.json()["task_design"]
        design = TaskDesign(owner["project"], owner["conversation_id"])
        current = design.record_turn("本次只生成和保存编排图。")
        stale = await client.post("/api/orchestration/task-design", json={
            **owner, "action": "set_intent", "intent": "plan",
            "expected_task_id": created["task_id"], "expected_revision": created["revision"]})
        assert stale.status_code == 409
        corrected = await client.post("/api/orchestration/task-design", json={
            **owner, "action": "set_intent", "intent": "plan",
            "expected_task_id": current["task_id"], "expected_revision": current["revision"]})
        assert corrected.status_code == 200, corrected.text
        value = corrected.json()["task_design"]
        assert value["task_id"] == created["task_id"] and value["intent"] == "plan"
        assert "不会启动业务执行" in value["reason"]
        rejected = await client.post("/api/orchestration/task-design", json={
            **owner, "action": "set_intent", "intent": "execute",
            "expected_task_id": value["task_id"], "expected_revision": value["revision"]})
        assert rejected.status_code == 409


@pytest.mark.asyncio
async def test_plan_start_can_explicitly_downgrade_an_existing_execute_task(task_api_env):
    _, _, _, owner, _ = task_api_env
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        started = await client.post("/api/orchestration/task-design", json={**owner, "action": "start", "intent": "execute"})
        created = started.json()["task_design"]
        corrected = await client.post("/api/orchestration/task-design", json={
            **owner, "action": "start", "intent": "plan",
            "expected_task_id": created["task_id"], "expected_revision": created["revision"]})
        assert corrected.status_code == 200, corrected.text
        assert corrected.json()["task_design"]["intent"] == "plan"


@pytest.mark.asyncio
async def test_start_without_explicit_intent_cannot_create_execution_authority(task_api_env):
    _, _, _, owner, _ = task_api_env
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        response = await client.post("/api/orchestration/task-design", json={**owner, "action": "start"})
    assert response.status_code == 409
    assert TaskDesign(owner["project"], owner["conversation_id"]).read() is None
