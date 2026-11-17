import pytest

from mewcode.experts.lessons import FlowLessonStore, user_lesson_from_supplemental
from mewcode.expert_group import ExpertGroupOrchestrator, ExpertRoleResult
from mewcode.experts.flow import ExpertFlowSpec
from tests.test_expert_flow import _profile


def test_store_scope_confirmation_counts_and_delete(tmp_path):
    store = FlowLessonStore(tmp_path / "lessons.json")
    active = store.add("flow", "work", "WORK LESSON")
    pending = store.add("flow", "*", "UNCONFIRMED", source="reflection")
    store.add("*", "role:auditor", "AUDIT LESSON")
    picked = store.select("flow", "work", "executor")
    assert [r["text"] for r in picked] == ["WORK LESSON"]
    store.record_success(picked)
    assert store.list("flow")[0]["success_hits"] == 1
    assert [r["text"] for r in store.select("flow", "audit", "auditor")] == ["AUDIT LESSON"]
    store.update("flow", pending["lesson_id"], status="active")
    assert "UNCONFIRMED" in [r["text"] for r in store.select("flow", "work", "executor")]
    store.update("flow", active["lesson_id"], status="disabled")
    assert "WORK LESSON" not in [r["text"] for r in store.select("flow", "work", "executor")]
    store.update("flow", active["lesson_id"], delete=True)
    assert active["lesson_id"] not in [r["lesson_id"] for r in store.list("flow")]
    assert user_lesson_from_supplemental("记住：@work 先安装依赖，继续任务") == ("work", "先安装依赖，继续任务")
    assert user_lesson_from_supplemental("继续") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", [False, True])
async def test_lesson_injection_and_reflection_pending(tmp_path, blocked):
    store = FlowLessonStore(tmp_path / "lessons.json")
    store.add("flow", "work", "WORK MEMORY")
    store.add("flow", "*", "PENDING MEMORY", source="reflection")
    events = []
    flow = ExpertFlowSpec.from_value({"flow_id": "flow", "label": "flow", "finalizer_node_id": "done",
        "nodes": [{"node_id": "work", "role": "executor"}, {"node_id": "done", "role": "manager"}],
        "edges": [{"from": "work", "to": "done"}]})

    async def runner(node, model, prompt, policy, member=None):
        assert "PENDING MEMORY" not in prompt
        assert ("WORK MEMORY" in prompt) == (node.node_id == "work")
        return ExpertRoleResult("work", status="blocked" if blocked and node.node_id == "work" else "completed")

    async def reflection(records):
        assert records and records[0]["node_id"] == "work"
        return [{"scope": "work", "text": "REFLECTION"}, {"scope": "unknown", "text": "INVALID"}]

    orchestrator = ExpertGroupOrchestrator(runner=lambda *_: None, node_runner=runner, archive_root=tmp_path,
        lesson_store=store, reflection_runner=reflection, event_sink=events.append)
    result = await orchestrator.run("task", _profile(), {}, strategy="stable", flow=flow)
    assert (result.status == "blocked") == blocked
    proposed = [e for e in events if e["type"] == "expert_lessons_proposed"]
    assert bool(proposed) == blocked
    if blocked:
        assert proposed[0]["count"] == 1
        assert store.list("flow")[-1]["status"] == "pending"
    else:
        assert store.list("flow")[0]["success_hits"] == 1


@pytest.mark.asyncio
async def test_lesson_api_confirmation_disable_delete(tmp_path, monkeypatch):
    import httpx
    monkeypatch.setenv("MEWCODE_USER_DATA_DIR", str(tmp_path / "user-data"))
    import frontend.server as server
    store = FlowLessonStore(tmp_path / "api-lessons.json")
    monkeypatch.setattr(server, "EXPERT_LESSONS", store)
    monkeypatch.setattr(server, "DESKTOP_TOKEN", "")
    pending = store.add("flow", "*", "REVIEW ME", source="reflection")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        url = "/api/expert/lessons?flow_id=flow"
        assert (await client.get(url)).json()["lessons"][0]["status"] == "pending"
        assert store.select("flow", "work", "executor") == []
        assert (await client.patch(url, json={"lesson_id": pending["lesson_id"], "status": "active"})).status_code == 200
        assert len(store.select("flow", "work", "executor")) == 1
        assert (await client.patch(url, json={"lesson_id": pending["lesson_id"], "status": "disabled"})).status_code == 200
        assert store.select("flow", "work", "executor") == []
        assert (await client.patch(url, json={"lesson_id": pending["lesson_id"], "delete": True})).status_code == 200
        assert (await client.get(url)).json()["lessons"] == []
        assert (await client.post(url, json={"text": "USER MEMORY"})).json()["lesson"]["status"] == "active"
        assert (await client.post(url, json={"text": ""})).status_code == 400
        assert (await client.patch(url, json={"lesson_id": "absent", "status": "active"})).status_code == 404
        kinds = (await client.get("/api/orchestration/kinds")).json()["kinds"]
        assert {row["kind"]: row["role"] for row in kinds}["audit"] == "auditor"
