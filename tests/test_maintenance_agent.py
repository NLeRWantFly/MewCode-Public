"""Fix has a real tool loop, scoped writes and a resumable user interaction."""
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from frontend import orch_maintenance, server
from mewcode.agent import Agent
from mewcode.conversation import ConversationManager
from mewcode.orchestration.maintenance import SCHEMA, MaintenanceStore
from mewcode.orchestration.maintenance_agent import (
    FixTurn, MaintenanceTurn, agent_history, build_fix_agent, build_maintenance_agent, evidence_sources,
)
from mewcode.permissions.modes import PermissionMode
from mewcode.permissions.policy import build_permission_checker
from mewcode.permissions.rules import Rule
from mewcode.tools import create_default_registry
from mewcode.tools.ask_user import AskUserTool
from mewcode.tools.base import TextDelta, ToolCallComplete, StreamEnd
from mewcode.tools.read_file import ReadFile
from mewcode.tools.read_stage_result import ReadStageResult, ReadStageResultParams
from mewcode.tools.write_file import WriteFile
from tests.test_agent import MockLLMClient
from tests.test_orchestration_maintenance import maintenance_env as maintenance_env, poll, spec_for


def interactive_builder(monkeypatch, root):
    made = []
    def build(*, state, **kwargs):
        registry = create_default_registry()
        registry.register(AskUserTool())
        registry.mark_discovered("AskUserQuestion")
        client = MockLLMClient([
            [ToolCallComplete(tool_id="question", tool_name="AskUserQuestion", arguments={"purpose": "user_input",
                "questions": [{"name": "approach", "type": "radio", "message": "选择修复方式",
                               "options": ["只调查", "写入修复说明"]}]}), StreamEnd(stop_reason="tool_use")],
            [ToolCallComplete(tool_id="write", tool_name="WriteFile", arguments={
                "file_path": str(root / "repair.txt"), "content": "用户回答后才写入"}), StreamEnd(stop_reason="tool_use")],
            [TextDelta(text=json.dumps({"schema": SCHEMA, "reply": "修复说明已保存", "proposal": None})),
             StreamEnd(stop_reason="end_turn", input_tokens=10, output_tokens=10)],
        ])
        checker = build_permission_checker(root, mode=PermissionMode.AUTO, writable_scope=[root])
        checker.task_id = "maintenance:" + state["id"]
        agent = Agent(client, registry, "anthropic", work_dir=str(root), permission_checker=checker, stall_policy=False)
        agent.session_id = state["conversation_id"]
        conversation = ConversationManager()
        conversation.add_user_message(state["messages"][-1]["text"])
        made.append((agent, conversation))
        return agent, conversation
    monkeypatch.setattr(orch_maintenance, "build_fix_agent", build)
    return made


async def wait_question(client, owner, identity):
    for _ in range(100):
        state = (await client.get("/api/orchestration/maintenance/state", params={**owner, "id": identity})).json()
        if (state.get("agent") or {}).get("pending"):
            return state
        if state["status"] != "running":
            pytest.fail(str(state))
        await asyncio.sleep(.01)
    pytest.fail("Maintenance did not ask its question")


@pytest.mark.asyncio
async def test_fix_question_blocks_then_answer_continues_same_agent(maintenance_env, monkeypatch):
    app, owner, current, durable, _ = maintenance_env
    root = Path(owner["project"])
    made = interactive_builder(monkeypatch, root)
    original = copy.deepcopy(server.SESSION.conversation.history)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        state = (await client.post("/api/orchestration/maintenance/open", json={**owner, "mode": "fix"})).json()
        await client.post("/api/orchestration/maintenance/send", json={**owner, "id": state["id"], "revision": 0, "message": "帮我修复，先确认方式"})
        waiting = await wait_question(client, owner, state["id"])
        assert waiting["agent"]["status"] == "waiting_user"
        assert not (root / "repair.txt").exists()
        request_id = waiting["agent"]["pending"]["request_id"]
        panel = server.SESSION.agent_tasks_payload()
        assert panel["groups"][0]["kind"] == "maintenance"
        assert panel["groups"][0]["rows"][0]["waiting_user"] is True
        task_id = waiting["agent"]["task_id"]
        assert panel["groups"][0]["rows"][0]["task_id"] == task_id
        # Closing/reopening the view preserves the same live question.
        reopened = (await client.post("/api/orchestration/maintenance/open", json={**owner, "mode": "fix", "id": state["id"]})).json()
        assert reopened["agent"]["pending"]["request_id"] == request_id
        answer = {**owner, "id": state["id"], "request_id": request_id, "answers": {"approach": "写入修复说明，并保留原验收"}}
        wrong = await client.post("/api/orchestration/maintenance/answer", json={**answer, "conversation_id": "other"})
        assert wrong.status_code == 409
        assert (await client.post("/api/orchestration/maintenance/answer", json=answer)).status_code == 200
        done = await poll(client, owner, state)
        assert done["agent"]["task_id"] == task_id and len(made) == 1
        assert done["agent"]["status"] == "completed" and done["agent"]["pending"] is None
        assert (root / "repair.txt").read_text(encoding="utf-8") == "用户回答后才写入"
        assert "修复说明已保存" in done["messages"][-1]["text"]
        assert "并保留原验收" in done["messages"][-2]["text"]
        assert (await client.post("/api/orchestration/maintenance/answer", json=answer)).status_code == 409
    history = server.MAINTENANCE_STORE.get(*[owner["project"], owner["conversation_id"], state["id"]])["agent_history"]
    results = [r for message in history for r in message["tool_results"]]
    assert any("并保留原验收" in row["content"] for row in results)
    assert all("interrupted before completion" not in row["content"] for row in results)
    assert "agent_history" not in done
    assert server.SESSION.collect_task_notifications() == []
    assert server.SESSION.conversation.history == original


@pytest.mark.asyncio
async def test_update_uses_real_agent_tools_and_keeps_proposal_scope(maintenance_env, monkeypatch):
    from mewcode.orchestration.chat_routing import set_dialogue_focus
    from mewcode.plugins.services import AGENT_FACTORY
    from mewcode.tools.base import StreamEnd

    app, owner, _current, durable, spec = maintenance_env
    root = Path(owner["project"])
    (root / "review-notes.txt").write_text("保留已有验收条件", encoding="utf-8")
    run_id = "b" * 32
    snapshot = {
        "run_id": run_id, "conversation_id": owner["conversation_id"], "status": "paused",
        "stage_cursor": "work", "locked_flow": {"flow_id": spec.spec_id},
        "node_states": {"work": {"status": "completed", "text": "完整运行记录尾部证据", "handoff": {
            "inline_outputs": ["完整交接产出"]}}},
        "stages": {},
    }
    monkeypatch.setattr(server.EXPERT_RUNS, "load", lambda identity: copy.deepcopy(snapshot) if identity == run_id else None)
    set_dialogue_focus(SimpleNamespace(session=durable), "orchestration", run_id=run_id)

    question = {"name": "keep_acceptance", "type": "radio", "message": "保留现有验收条件吗？", "options": ["保留"]}
    response = {"schema": SCHEMA, "reply": "已读取完整证据并保留原验收条件。", "proposal": {
        "summary": "只更新 work 环节的标签与计划摘要", "node_changes": [{"node_id": "work", "changes": {
            "label": "增加核验", "plan_brief": {"summary": "复核已有交付，不删除原验收"}}}]}}
    client = MockLLMClient([
        [ToolCallComplete("index", "ReadStageResult", {"node_id": ""}), StreamEnd(stop_reason="tool_use")],
        [ToolCallComplete("graph", "ReadStageResult", {"node_id": "@graph"}), StreamEnd(stop_reason="tool_use")],
        [ToolCallComplete("run", "ReadStageResult", {"node_id": "@run"}), StreamEnd(stop_reason="tool_use")],
        [ToolCallComplete("file", "ReadFile", {"file_path": str(root / "review-notes.txt")}),
         StreamEnd(stop_reason="tool_use")],
        [TextDelta(text="我已读完图、运行记录和复核说明。"),
         ToolCallComplete("question", "AskUserQuestion", {"purpose": "user_input", "questions": [question]}),
         StreamEnd(stop_reason="tool_use")],
        [TextDelta(text=json.dumps(response, ensure_ascii=False)), StreamEnd(stop_reason="end_turn")],
    ])
    captured = {"requests": [], "agents": []}

    class Runtime:
        def __init__(self, request):
            registry = create_default_registry()
            for tool in request.extra_tools:
                registry.register(tool)
            self.agent = Agent(client, registry, request.provider.protocol, work_dir=request.work_dir,
                permission_checker=request.permission_checker, instructions_content=request.instructions_content,
                stall_policy=False)
            self.agent._current_conversation = request.conversation
            captured["requests"].append(request)
            captured["agents"].append(self.agent)

        async def aclose(self):
            return None

    class Factory:
        def create(self, request):
            return Runtime(request)

    monkeypatch.setattr(server.SESSION, "context", SimpleNamespace(get=lambda key: Factory() if key == AGENT_FACTORY else None), raising=False)
    provider = SimpleNamespace(protocol="anthropic")
    monkeypatch.setattr(server, "_orchestration_attempt_context", lambda: {
        "model": "mock-update", "requested_effort": "high", "provider": provider})
    monkeypatch.setattr(server, "_new_orchestration_client", lambda _context, _effort: client)
    monkeypatch.setattr(orch_maintenance, "build_maintenance_agent", build_maintenance_agent)

    original_request = "work 请检查运行记录和 review-notes.txt，问我是否保留验收后只调整标签与摘要。"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        opened = (await http.post("/api/orchestration/maintenance/open", json={**owner, "mode": "update",
            "node_ids": ["work"]})).json()
        started = await http.post("/api/orchestration/maintenance/send", json={**owner, "id": opened["id"],
            "revision": opened["revision"], "message": original_request})
        assert started.status_code == 200, started.text
        waiting = await wait_question(http, owner, opened["id"])
        assert waiting["agent"]["name"] == "Update · 编排更新"
        assert waiting["agent"]["pending"]["kind"] == "ask_user"
        assert waiting["agent"]["task_id"]
        request_id = waiting["agent"]["pending"]["request_id"]
        assert (await http.post("/api/orchestration/maintenance/answer", json={**owner, "id": opened["id"],
            "request_id": request_id, "answers": {"keep_acceptance": "保留"}})).status_code == 200
        done = await poll(http, owner, opened)

    assert done["agent"]["status"] == "completed"
    assert done["proposal"] is not None and done["proposal"]["runtime_actions"] == []
    completed_state = server.MAINTENANCE_STORE.get(owner["project"], owner["conversation_id"], opened["id"])
    assert completed_state["proposal"]["spec"]["nodes"][1]["label"] == "增加核验"
    assert server._conversation_plan_store(owner["conversation_id"]).get(owner["project"]).spec.as_dict() == spec.as_dict()
    assert snapshot["status"] == "paused" and snapshot["node_states"]["work"]["text"] == "完整运行记录尾部证据"
    history = server.MAINTENANCE_STORE.get(owner["project"], owner["conversation_id"], opened["id"])["agent_history"]
    results = [item for message in history for item in message["tool_results"]]
    combined = "\n".join(item["content"] for item in results)
    assert "@graph" in combined and "@run" in combined and "完整运行记录尾部证据" in combined
    assert "保留已有验收条件" in combined and "work" in combined
    request = captured["requests"][0]
    assert original_request in str(request.conversation.history)
    assert '"allowed_node_ids": ["work"]' in str(request.conversation.history)
    assert "Update 的 changes 仅允许 label/goal/plan_brief" in request.instructions_content
    assert "proposal.edges" not in request.instructions_content
    assert any(message["role"] == "user" and message["content"] == original_request
               for message in agent_history(captured["agents"][0], request.conversation))
    with pytest.raises(ValueError):
        from mewcode.orchestration.maintenance import preview_proposal
        preview_proposal(spec.as_dict(), "update", ["work"], {"summary": "扩大范围", "edges": []})
    with pytest.raises(ValueError):
        from mewcode.orchestration.maintenance import preview_proposal
        preview_proposal(spec.as_dict(), "update", ["work"], {"summary": "扩大范围", "node_changes": [
            {"node_id": "first", "changes": {"label": "越界"}}]})


@pytest.mark.asyncio
async def test_fix_stop_cancels_pending_question_and_invalidates_answer(maintenance_env, monkeypatch):
    app, owner, _, _, _ = maintenance_env
    interactive_builder(monkeypatch, Path(owner["project"]))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        state = (await client.post("/api/orchestration/maintenance/open", json={**owner, "mode": "fix"})).json()
        await client.post("/api/orchestration/maintenance/send", json={**owner, "id": state["id"], "revision": 0, "message": "调查"})
        waiting = await wait_question(client, owner, state["id"])
        response = await client.post("/api/orchestration/maintenance/stop", json={**owner, "id": state["id"], "revision": waiting["revision"]})
        assert response.status_code == 200
        done = await poll(client, owner, state)
        assert done["agent"]["status"] == "cancelled" and done["agent"]["pending"] is None
        answer = await client.post("/api/orchestration/maintenance/answer", json={**owner, "id": state["id"],
            "request_id": waiting["agent"]["pending"]["request_id"], "answers": {"approach": "写入修复说明"}})
        assert answer.status_code == 409
        assert not (Path(owner["project"]) / "repair.txt").exists()


@pytest.mark.asyncio
async def test_fix_background_result_stays_with_owner_after_navigation(maintenance_env, monkeypatch):
    app, owner, current, _, _ = maintenance_env
    entered, release = asyncio.Event(), asyncio.Event()
    async def model(*args, **kwargs):
        entered.set()
        await release.wait()
        return json.dumps({"schema": SCHEMA, "reply": "原对话的调查已完成", "proposal": None})
    monkeypatch.setattr(orch_maintenance, "collect_model_text", model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        state = (await client.post("/api/orchestration/maintenance/open", json={**owner, "mode": "fix"})).json()
        await client.post("/api/orchestration/maintenance/send", json={**owner, "id": state["id"], "revision": 0, "message": "调查"})
        await asyncio.wait_for(entered.wait(), 5)
        current["cid"] = "other"
        release.set()
        for _ in range(100):
            done = server.MAINTENANCE_STORE.get(owner["project"], owner["conversation_id"], state["id"])
            if done["status"] != "running":
                break
            await asyncio.sleep(.01)
        assert done["status"] == "ready" and not done["error"]
        assert done["messages"][-1]["text"] == "原对话的调查已完成"
        current["cid"] = owner["conversation_id"]
        reopened = (await client.post("/api/orchestration/maintenance/open", json={**owner, "mode": "fix", "id": state["id"]})).json()
        assert reopened["messages"][-1]["text"] == "原对话的调查已完成"


def test_readonly_scope_survives_grants_descendants_and_native_policy(tmp_path):
    from mewcode.sandbox.lpac_runtime import NativeScope
    protected = tmp_path / "framework"
    protected.mkdir()
    target = protected / "architecture.py"
    target.write_text("original")
    parent = build_permission_checker(tmp_path, mode="auto", writable_scope=[tmp_path], readonly_scope=[protected])
    parent.sandbox.grant_directory(str(protected), writable=True)
    parent.rule_engine.append_session_rule(Rule("WriteFile", "*", "allow"))
    child = build_permission_checker(tmp_path, parent_policy=parent)
    child.task_id = "fix-test"
    assert child.check(ReadFile(), {"file_path": str(target)}).effect == "allow"
    assert child.check(WriteFile(), {"file_path": str(target), "content": "changed"}).effect == "deny"
    assert not NativeScope.from_checker(child).allows(target, write=True)
    assert child.check(WriteFile(), {"file_path": str(tmp_path / "business.py"), "content": "ok"}).effect == "allow"


@pytest.mark.asyncio
async def test_full_owned_evidence_is_paged_without_losing_review_tail():
    state = {"base": spec_for().as_dict(), "conversation_id": "test", "run_id": "run"}
    review = "计划和审查\n" * 5000 + "完整结尾证据"
    snapshot = {"run_id": "run", "conversation_id": "test", "locked_flow": {"flow_id": "maintenance"},
                "node_states": {"work": {"text": review, "handoff": {"inline_outputs": ["全文产出"]}}}}
    tool = ReadStageResult(evidence_sources(state, snapshot, {}))
    text, offset = "", 0
    while True:
        result = await tool.execute(ReadStageResultParams(node_id="work", offset=offset, limit=24000))
        cursor = result.host_observation["read_cursor"]
        text += result.output.split("\n", 5)[5]
        offset = cursor["next_offset"]
        if not cursor["more"]:
            break
    assert json.loads(text)["text"] == review
    with pytest.raises(ValueError):
        evidence_sources(state, {**snapshot, "conversation_id": "foreign"}, {})


def test_factory_installs_auto_scoped_agent_and_visible_ask_tool(tmp_path, monkeypatch):
    captured = []
    root = tmp_path / "project"
    root.mkdir()
    hostroot = tmp_path / "host"
    hostroot.mkdir()
    monkeypatch.setattr("mewcode.orchestration.maintenance_agent.FRAMEWORK_ROOT", hostroot)
    parent_checker = build_permission_checker(root, mode="plan")
    class Factory:
        def create(self, request):
            captured.append(request)
            registry = create_default_registry()
            for tool in request.extra_tools:
                registry.register(tool)
            return SimpleNamespace(agent=Agent(MockLLMClient([]), registry, "anthropic", work_dir=str(root),
                permission_checker=request.permission_checker, stall_policy=False))
    host = SimpleNamespace(context=SimpleNamespace(get=lambda _: Factory()),
        agent=SimpleNamespace(agent_id="parent", permission_checker=parent_checker))
    state = {"project": str(root), "conversation_id": "test", "id": "a" * 32, "mode": "fix", "node_ids": [],
             "run_id": "", "base": spec_for().as_dict(), "messages": [{"role": "user", "text": "调查并修复"}]}
    agent, conversation = build_fix_agent(host=host, provider_context={"provider": object()}, client=object(),
        state=state, snapshot=None, diagnostics={})
    assert agent.permission_checker.mode == PermissionMode.AUTO
    assert parent_checker.mode == PermissionMode.PLAN
    assert agent.permission_checker.rule_engine is parent_checker.rule_engine
    assert agent.permission_checker.policy_writable_roots == (root.resolve(),)
    assert agent.registry.is_discovered("AskUserQuestion")
    assert agent.registry.get("ReadStageResult") is not None
    assert not captured[0].inherit_parent_tools
    assert agent.parent_id == "parent" and conversation.history[-1].content == "调查并修复"
