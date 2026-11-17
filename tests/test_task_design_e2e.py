from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.responses import JSONResponse

from frontend import orch_drafts, server
from mewcode.conversation import ConversationManager
from mewcode.expert_runs import ExpertRunStore
from mewcode.orchestration import OrchestrationSpec
from mewcode.orchestration.convert import to_expert_flow
from mewcode.orchestration.draft_sources import digest
from mewcode.orchestration.draft_store import DraftStore
from mewcode.orchestration.task_design import TaskBuildError, TaskDesign
from mewcode.orchestration.workflow_blocks import WorkflowBlocks
from mewcode.pipeline.store import PipelineStore
from mewcode.permissions import PermissionMode
from mewcode.tools.task_design import AdvanceTaskDesign, AdvanceTaskDesignParams
from mewcode.tools.workflow_blocks import SubmitWorkflowBlocks, SubmitWorkflowBlocksParams
from tests.test_task_design_entry import task_api_env
from tests.test_orchestration_start_run import _bound_spec
from tests.test_workflow_drafts import cited, entry, record, write_history


def _complete_synthetic_material(source):
    rows = [entry("task_goal", "goal", body="生成一个离线样例流程")]
    rows.extend([
        entry("prepare", body="整理目标", data={"node_kind": "plan", "inputs": ["用户目标"],
            "outputs": ["整理后的目标"], "acceptance": ["目标范围列明输入与验收"]}),
        entry("review", body="独立核对", data={"node_kind": "audit", "inputs": ["整理后的目标"],
            "outputs": ["核对结论"], "acceptance": ["核对结论覆盖目标"]}),
        entry("deliver", body="形成交付", data={"node_kind": "reduce", "inputs": ["核对结论"],
            "outputs": ["最终交付"], "acceptance": ["交付包含核对结论与目标摘要"]}),
        entry("prepare_to_review", "edge", "核对前置", data={"from_node": "prepare", "to_node": "review", "condition": "always"}),
        entry("review_to_deliver", "edge", "交付前置", data={"from_node": "review", "to_node": "deliver", "condition": "always"}),
    ])
    return [cited(row, source) for row in rows]


async def _prepare_queued_task(client, owner, history_path, intent):
    started = await client.post("/api/orchestration/task-design", json={
        **owner, "action": "start", "intent": intent,
    })
    assert started.status_code == 200, started.text
    goal = "离线验收目标：为合成样例生成流程图，不调用真实执行工具。"
    write_history(history_path, [record("user", goal)], append=True)
    design = TaskDesign(owner["project"], owner["conversation_id"])
    design.record_turn(goal)

    blocks = WorkflowBlocks(owner["project"], owner["conversation_id"], source_path=history_path,
                            base_spec=lambda: None, guard=lambda: True,
                            model_routes=lambda: [], tool_services=lambda: [])
    read = blocks.read()
    source_id = next(row["id"] for row in reversed(read["source_index"])
                     if row["role"] == "user" and "离线验收目标" in row["excerpt"])
    source = blocks.read(source_ids=[source_id])["sources"][0]
    submit = SubmitWorkflowBlocks(blocks, turn_id=lambda: "synthetic-main-turn")
    submitted = await submit.execute(SubmitWorkflowBlocksParams(
        expected_sequence=read["sequence"], source_hash=read["source_hash"],
        base_graph_hash=read["base_graph_hash"], upserts=_complete_synthetic_material(source),
        ignored=[], use_jev=False))
    assert not submitted.is_error, submitted.output
    receipt = json.loads(submitted.output)
    assert receipt["saved_entry_ids"] and not receipt["pending"]

    updated = blocks.read()
    current_design = design.read()
    advance = AdvanceTaskDesign(blocks)
    advanced = await advance.execute(AdvanceTaskDesignParams(
        action="build", expected_revision=current_design["revision"],
        expected_sequence=updated["sequence"], source_hash=updated["source_hash"],
        base_graph_hash=updated["base_graph_hash"]))
    assert not advanced.is_error, advanced.output
    result = json.loads(advanced.output)
    assert result["business_started"] is False
    assert result["task_design"]["status"] == "queued"
    return design, result["task_design"]


async def _wait_for_status(design, expected, *, timeout=15):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        current = design.read()
        if current and current["status"] in expected:
            return current
        await asyncio.sleep(0.01)
    pytest.fail(f"task design did not reach one of {sorted(expected)}; current={design.read()}")


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_at", ["build", "check", "preview", "apply"])
async def test_leaving_design_during_async_step_cannot_auto_activate(task_api_env, monkeypatch, exit_at):
    _, _, history_path, owner, _ = task_api_env
    calls = []

    def step(name, result=None):
        async def run(*args, **kwargs):
            calls.append(name)
            if name == exit_at:
                server.SESSION.orchestration_planning = False
            return result
        return run

    monkeypatch.setattr(server, "task_design_build_graph", step("build"))
    monkeypatch.setattr(server, "task_design_check_build", step("check"))
    monkeypatch.setattr(server, "task_design_preview_graph", step("preview", {"id": "preview"}))
    monkeypatch.setattr(server, "task_design_apply_graph", step("apply", {"spec": {"meta": {}}}))
    confirm = AsyncMock(side_effect=AssertionError("exiting design must not launch execution"))
    repair = AsyncMock(side_effect=AssertionError("exiting design must not reopen a repair chat"))
    monkeypatch.setattr(server, "api_orchestration_confirm", confirm)
    monkeypatch.setattr(server, "run_chat", repair)
    monkeypatch.setattr(server.WS_EVENTS, "send_json", AsyncMock())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        design, _ = await _prepare_queued_task(client, owner, history_path, "execute")
        server.task_design_after_turn(SimpleNamespace(work_dir=owner["project"],
            conversation_id=owner["conversation_id"], execution_kind="chat"))
        final = await _wait_for_status(design, {"paused", "running", "needs_input"})
    assert final["status"] == "paused"
    assert final["build_request"]["state"] == "paused"
    assert final["build_request"]["epoch"] > 1
    assert calls == ["build", "check", "preview", "apply"][:["build", "check", "preview", "apply"].index(exit_at) + 1]
    confirm.assert_not_awaited()
    repair.assert_not_awaited()


def _confirm_dispatch_runtime(monkeypatch, owner):
    """Use the real reservation/dispatch code with an isolated idle runtime."""
    runtime = SimpleNamespace(
        owner=server.SESSION, session=server.SESSION.session, scope="project",
        work_dir=owner["project"], conversation_id=owner["conversation_id"],
        conversation=ConversationManager(), task=None, starting=False,
        execution_kind="chat", expert_mode=False, orchestration_planning=True,
        agent=SimpleNamespace(permission_mode=PermissionMode.DEFAULT),
        permission_before_plan="default", permission_before_orch="",
        cancel_requested=False, queued_prompts=[], task_manager=None,
    )
    runtime.running = lambda: runtime.starting or (runtime.task is not None and not runtime.task.done())
    monkeypatch.setattr(server.SESSION, "conversation_runs", {owner["conversation_id"]: runtime})
    monkeypatch.setattr(server.SESSION, "capture_conversation_runtime", lambda: runtime)
    monkeypatch.setattr(server.SESSION, "runtime_for", lambda *_: runtime)
    monkeypatch.setattr(server.SESSION, "chat_busy", lambda *_: runtime.running())
    monkeypatch.setattr(server.SESSION, "tasks_payload", lambda: [])
    monkeypatch.setattr(server.SESSION, "permission_before_plan", "default")
    monkeypatch.setattr(server.SESSION, "permission_before_orch", "")
    monkeypatch.setattr(server.MODEL_REGISTRY, "resolve", lambda ref: SimpleNamespace(ref=ref))
    monkeypatch.setattr(server, "_save_web_state", lambda *_: None)
    saved_modes = []
    monkeypatch.setattr(server.SESSION, "_save_active_conversation_settings", lambda: None)
    monkeypatch.setattr(server.SESSION, "_save_runtime_settings", lambda captured: saved_modes.append(
        (captured, captured.expert_mode, captured.orchestration_planning, captured.agent.permission_mode)))

    def set_expert(on, **kwargs):
        server.SESSION.expert_mode = runtime.expert_mode = bool(on)
        if on:
            server.SESSION.orchestration_planning = runtime.orchestration_planning = False

    def set_planning(on):
        server.SESSION.orchestration_planning = runtime.orchestration_planning = bool(on)

    def set_permission(mode, **_kwargs):
        server.SESSION.permission_mode = PermissionMode(mode)
        server._apply_runtime_permission_mode(runtime, mode)

    monkeypatch.setattr(server.SESSION, "set_expert_mode", set_expert)
    monkeypatch.setattr(server.SESSION, "set_orchestration_planning", set_planning)
    monkeypatch.setattr(server.SESSION, "set_permission_mode", set_permission)
    monkeypatch.setattr(server.SESSION, "_set_internal_permission_mode", set_permission)
    business = AsyncMock()
    monkeypatch.setattr(server, "run_expert_chat", business)
    runs = ExpertRunStore(owner["project"] + "/isolated-runs")
    monkeypatch.setattr(server, "EXPERT_RUNS", runs)
    return runtime, business, saved_modes


async def _saved_dispatch_task(client, owner, *, resume):
    started = await client.post("/api/orchestration/task-design", json={**owner, "action": "start", "intent": "execute"})
    assert started.status_code == 200, started.text
    design = TaskDesign(owner["project"], owner["conversation_id"])
    current = design.record_turn("完成离线合成样例并独立核对交付。")
    saved = design.transition(current, "ready", "合成编排已保存。", build_request={
        "id": "synthetic-saved-build", "epoch": 1, "state": "saved"})
    raw = _bound_spec("完成离线合成样例并独立核对交付。").as_dict()
    raw.setdefault("meta", {})["task_design"] = {"task_id": saved["task_id"], "intent": "execute"}
    spec = OrchestrationSpec.from_value(raw)
    if resume:
        run_id = "d" * 32
        server.EXPERT_RUNS.create(run_id=run_id, conversation_id=owner["conversation_id"],
            task=spec.goal, attachments=[], profile={}, strategy="stable", flow=to_expert_flow(spec))
        server.EXPERT_RUNS.transition(run_id, "paused")
        saved = design.transition(saved, "paused", "保留合成检查点。", run_id=run_id)
        plan = server.build_plan_session(spec, goal=spec.goal, confirmed=True, activate=False)
        server._conversation_plan_store(owner["conversation_id"]).put(owner["project"], plan)
    else:
        saved = design.claim_execution(saved)
    return design, saved, spec


def _assert_control_with_not_started_receipt(current, authoritative):
    expected = copy.deepcopy(authoritative)
    request = expected.get("build_request") or {}
    claim_id = request.pop("execution_claim_id", None)
    if claim_id:
        request.pop("execution_state", None)
        receipt = current["build_request"].get("execution_not_started") or {}
        assert receipt.get("claim_id") == claim_id and receipt.get("reason")
        request["execution_not_started"] = receipt
    assert current == expected


@pytest.mark.asyncio
async def test_task_owned_retry_reaches_runner_with_dispatch_fence(task_api_env, monkeypatch):
    _, _, _, owner, _ = task_api_env
    runtime, business, _ = _confirm_dispatch_runtime(monkeypatch, owner)
    set_expert = server.SESSION.set_expert_mode
    monkeypatch.setattr(server.SESSION, 'set_expert_mode', lambda on, **kwargs: set_expert(on))
    monkeypatch.setattr(server.WS_EVENTS, 'send_json', AsyncMock())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        design, saved, spec = await _saved_dispatch_task(client, owner, resume=True)
        response = await client.post('/api/orchestration/task-design', json={**owner,
            'action': 'resume', 'retry_node_id': spec.nodes[0].node_id,
            'expected_task_id': saved['task_id'], 'expected_revision': saved['revision']})
        assert response.status_code == 200, response.text
        assert response.json()['task_design']['status'] == 'running'
        if runtime.task is not None:
            await runtime.task
    business.assert_awaited_once()
    assert business.await_args.kwargs['retry_node_id'] == spec.nodes[0].node_id
    assert business.await_args.kwargs['task_design_dispatch']['task_id'] == saved['task_id']
    assert business.await_args.kwargs['resume_run_id'] == saved['run_id']


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True], ids=["first-start", "checkpoint-resume"])
@pytest.mark.parametrize("action", ["pause", "set_intent"])
async def test_real_confirm_dispatch_is_fenced_after_pause_or_plan_correction(task_api_env, monkeypatch, resume, action):
    _, _, _, owner, _ = task_api_env
    runtime, business, saved_modes = _confirm_dispatch_runtime(monkeypatch, owner)
    entered, release = asyncio.Event(), asyncio.Event()
    events = []

    async def send(payload):
        events.append(payload)
        if payload.get("type") == "turn_started":
            entered.set()
            await release.wait()

    monkeypatch.setattr(server.WS_EVENTS, "send_json", send)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        design, saved, spec = await _saved_dispatch_task(client, owner, resume=resume)
        pending = asyncio.create_task(client.post("/api/orchestration/task-design" if resume else "/api/orchestration/confirm", json={
            **owner, "action": "resume", "expected_task_id": saved["task_id"], "expected_revision": saved["revision"],
        } if resume else {"spec": spec.as_dict(), "conversation_id": owner["conversation_id"], "activate": True,
                          "task_design_dispatch": design.execution_token(saved)}))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            current = design.read()
            assert runtime.starting and runtime.task is None
            controlled = await client.post("/api/orchestration/task-design", json={**owner, "action": action,
                "intent": "plan", "expected_task_id": current["task_id"], "expected_revision": current["revision"]})
            assert controlled.status_code == 200, controlled.text
            authoritative = design.read()
            release.set()
            rejected = await asyncio.wait_for(pending, 3)
            assert rejected.status_code == (409 if resume else 400), rejected.text
            _assert_control_with_not_started_receipt(design.read(), authoritative)
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
    business.assert_not_called()
    assert not runtime.starting and not runtime.running() and runtime.task is None
    assert not server.SESSION.expert_mode and server.SESSION.orchestration_planning
    assert saved_modes == [(runtime, False, True, PermissionMode.DEFAULT)]
    assert any(event.get("type") == "done" and event.get("rejected") for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("new_owner", ["none", "conversation", "project", "mode", "reservation"])
async def test_cancelled_real_confirm_restores_only_its_current_start_context(task_api_env, monkeypatch, new_owner):
    current_owner, _, _, owner, _ = task_api_env
    runtime, business, saved_modes = _confirm_dispatch_runtime(monkeypatch, owner)
    entered = asyncio.Event()
    events = []

    async def send(payload):
        events.append(payload)
        if payload.get("type") == "turn_started":
            entered.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(server.WS_EVENTS, "send_json", send)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        design, saved, spec = await _saved_dispatch_task(client, owner, resume=False)
        pending = asyncio.create_task(client.post("/api/orchestration/confirm", json={
            "spec": spec.as_dict(), "conversation_id": owner["conversation_id"], "activate": True,
            "task_design_dispatch": design.execution_token(saved)}))
        await asyncio.wait_for(entered.wait(), 3)
        if new_owner == "conversation":
            current_owner["cid"] = "new-conversation"
            server.SESSION.session = SimpleNamespace(session_id="new-conversation")
        elif new_owner == "project":
            server.SESSION.work_dir = owner["project"] + "/new-project"
        elif new_owner == "mode":
            server.SESSION.set_permission_mode(PermissionMode.BYPASS.value)
        elif new_owner == "reservation":
            server.SESSION.begin_conversation_turn(runtime)
        authoritative = design.read()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    business.assert_not_called()
    assert runtime.task is None
    _assert_control_with_not_started_receipt(design.read(), authoritative)
    if new_owner == "none":
        assert saved_modes == [(runtime, False, True, PermissionMode.DEFAULT)]
        assert not server.SESSION.expert_mode and server.SESSION.orchestration_planning
    else:
        assert not saved_modes
        assert server.SESSION.expert_mode and not server.SESSION.orchestration_planning
        if new_owner == "mode":
            assert server.SESSION.permission_mode == runtime.agent.permission_mode == PermissionMode.BYPASS
    rejected = [event for event in events if event.get("type") == "done" and event.get("rejected")]
    assert bool(rejected) == (new_owner != "reservation")
    assert runtime.starting == (new_owner == "reservation")


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["pause", "set_intent"])
async def test_cancelling_auto_dispatch_during_real_confirm_restores_design_mode(task_api_env, monkeypatch, action):
    _, _, history_path, owner, _ = task_api_env
    runtime, business, saved_modes = _confirm_dispatch_runtime(monkeypatch, owner)
    entered = asyncio.Event()
    events = []

    async def send(payload):
        events.append(payload)
        if payload.get("type") == "turn_started":
            entered.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(server.WS_EVENTS, "send_json", send)
    # These synthetic build adapters return an already validated fixture graph;
    # confirmation, cancellation, and the final dispatch fence stay real.
    monkeypatch.setattr(server, "task_design_build_graph", AsyncMock())
    monkeypatch.setattr(server, "task_design_check_build", AsyncMock())
    monkeypatch.setattr(server, "task_design_preview_graph", AsyncMock(return_value={"id": "offline-preview"}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        design, queued = await _prepare_queued_task(client, owner, history_path, "execute")
        raw = _bound_spec("完成合成离线样例。").as_dict()
        raw.setdefault("meta", {})["task_design"] = {"task_id": queued["task_id"], "intent": "execute"}
        monkeypatch.setattr(server, "task_design_apply_graph", AsyncMock(return_value={"spec": raw}))
        server.task_design_after_turn(runtime)
        await asyncio.wait_for(entered.wait(), 3)
        driver = runtime._turn_start_owner
        current = design.read()
        controlled = await client.post("/api/orchestration/task-design", json={**owner, "action": action,
            "intent": "plan", "expected_task_id": current["task_id"], "expected_revision": current["revision"]})
        assert controlled.status_code == 200, controlled.text
        authoritative = design.read()
        await asyncio.wait_for(asyncio.gather(driver, return_exceptions=True), 3)
    business.assert_not_called()
    _assert_control_with_not_started_receipt(design.read(), authoritative)
    assert not runtime.starting and not runtime.running() and runtime.task is None
    assert not server.SESSION.expert_mode and server.SESSION.orchestration_planning
    assert saved_modes == [(runtime, False, True, PermissionMode.DEFAULT)]
    assert any(event.get("type") == "done" and event.get("rejected") for event in events)


@pytest.mark.asyncio
async def test_first_dispatch_paused_before_worker_can_resume_after_not_started_receipt(task_api_env, monkeypatch):
    _, _, _, owner, _ = task_api_env
    runtime, business, _ = _confirm_dispatch_runtime(monkeypatch, owner)
    entered, release = asyncio.Event(), asyncio.Event()

    async def send(payload):
        if payload.get("type") == "turn_started":
            entered.set()
            await release.wait()

    monkeypatch.setattr(server.WS_EVENTS, "send_json", send)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        design, saved, spec = await _saved_dispatch_task(client, owner, resume=False)
        pending = asyncio.create_task(client.post("/api/orchestration/confirm", json={
            "spec": spec.as_dict(), "conversation_id": owner["conversation_id"], "activate": True,
            "task_design_dispatch": design.execution_token(saved)}))
        await asyncio.wait_for(entered.wait(), 3)
        current = design.read()
        paused = await client.post("/api/orchestration/task-design", json={**owner, "action": "pause",
            "expected_task_id": current["task_id"], "expected_revision": current["revision"]})
        assert paused.status_code == 200, paused.text
        paused_control = paused.json()["task_design"]
        release.set()
        rejected = await asyncio.wait_for(pending, 3)
        assert rejected.status_code == 400, rejected.text
        business.assert_not_called()
        _assert_control_with_not_started_receipt(design.read(), paused_control)

        # A real capture replaces an idle runtime. Use another isolated captured
        # runtime so the previous pause's cancel_requested flag cannot leak.
        runtime, business, _ = _confirm_dispatch_runtime(monkeypatch, owner)
        worker_release = asyncio.Event()
        async def wait_for_worker(*_args, **_kwargs):
            await worker_release.wait()
        business.side_effect = wait_for_worker
        resumed = await client.post("/api/orchestration/task-design", json={**owner, "action": "resume",
            "expected_task_id": paused_control["task_id"], "expected_revision": paused_control["revision"]})
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["continuation"]["kind"] == "execution"
        fresh = design.read()
        assert fresh["status"] == "running" and fresh["build_request"]["execution_state"] == "started"
        assert fresh["build_request"]["execution_claim_id"] != saved["build_request"]["execution_claim_id"]
        task = runtime.task
        await asyncio.sleep(0)
        business.assert_awaited_once()
        worker_release.set()
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["claim", "build"])
async def test_old_not_started_receipt_cannot_release_a_new_claim_or_request(task_api_env, monkeypatch, replacement):
    _, _, _, owner, _ = task_api_env
    _confirm_dispatch_runtime(monkeypatch, owner)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        design, saved, _ = await _saved_dispatch_task(client, owner, resume=False)
        old_token = design.execution_token(saved)
        released = design.reject_execution(saved, "合成启动拒绝。")
        ready = design.transition(released, "ready", build_request={"id": "replacement-build", "epoch": 1, "state": "saved"}
                                  if replacement == "build" else released["build_request"])
        new_claim = design.claim_execution(ready)
        assert new_claim["build_request"]["execution_claim_id"] != old_token["claim_id"]
        assert design.record_execution_not_started(old_token, "迟到的旧未启动收据。") is False
        assert design.read() == new_claim


@pytest.mark.asyncio
async def test_created_worker_with_uncertain_attach_cannot_be_marked_not_started(task_api_env, monkeypatch):
    _, _, _, owner, _ = task_api_env
    runtime, business, saved_modes = _confirm_dispatch_runtime(monkeypatch, owner)
    monkeypatch.setattr(server.WS_EVENTS, "send_json", AsyncMock())
    worker_release = asyncio.Event()
    created = []

    async def wait_for_worker(*_args, **_kwargs):
        await worker_release.wait()

    def uncertain_attach(_runtime, task):
        created.append(task)
        raise ValueError("合成任务挂接失败，启动结果待核对。")

    business.side_effect = wait_for_worker
    monkeypatch.setattr(server.SESSION, "attach_conversation_task", uncertain_attach)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        design, saved, spec = await _saved_dispatch_task(client, owner, resume=False)
        rejected = await client.post("/api/orchestration/confirm", json={
            "spec": spec.as_dict(), "conversation_id": owner["conversation_id"], "activate": True,
            "task_design_dispatch": design.execution_token(saved)})
        assert rejected.status_code == 400, rejected.text
        assert created and runtime.starting
        assert not saved_modes and server.SESSION.expert_mode
        uncertain = design.read()
        assert uncertain["build_request"]["execution_state"] == "dispatching"
        assert uncertain["build_request"]["execution_claim_id"] == saved["build_request"]["execution_claim_id"]
        assert not uncertain["build_request"].get("execution_not_started")
        paused = design.control("pause", task_id=uncertain["task_id"], revision=uncertain["revision"])
        with pytest.raises(ValueError, match="启动结果尚未确认"):
            design.control("resume", task_id=paused["task_id"], revision=paused["revision"])
        created[0].cancel()
        await asyncio.gather(*created, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["plan", "execute"])
async def test_main_task_flow_submits_materials_builds_and_saves_real_graph_without_upgrading_plan(
    task_api_env, monkeypatch, intent,
):
    _, _, history_path, owner, _ = task_api_env
    expected_calls = {"build": [], "check": [], "apply": []}
    real_build = server.task_design_build_graph
    real_check = server.task_design_check_build
    real_apply = server.task_design_apply_graph

    async def observe_build(project, cid, *, expected):
        expected_calls["build"].append(dict(expected))
        return await real_build(project, cid, expected=expected)

    async def observe_check(project, cid, expected):
        expected_calls["check"].append(dict(expected))
        return await real_check(project, cid, expected)

    async def observe_apply(project, cid, preview_id, *, expected):
        expected_calls["apply"].append(dict(expected))
        return await real_apply(project, cid, preview_id, expected=expected)

    monkeypatch.setattr(server, "task_design_build_graph", observe_build)
    monkeypatch.setattr(server, "task_design_check_build", observe_check)
    monkeypatch.setattr(server, "task_design_apply_graph", observe_apply)
    monkeypatch.setattr(server, "_load_review_model", lambda: {})
    model_phases = []

    async def fake_model(_client, **kwargs):
        payload = json.loads(kwargs["user_text"])
        if isinstance(payload.get("sources"), list):
            model_phases.append("history")
            return json.dumps({"upserts": [], "ignored": [
                {"source_id": item["id"], "reason": "完整目标已由当前规格素材记录"}
                for item in payload["sources"]]}, ensure_ascii=False)
        if isinstance(payload.get("requirements"), list):
            model_phases.append("coverage-review")
            return json.dumps({"gaps": [], "notes": "合成规格已覆盖全部目标"}, ensure_ascii=False)
        model_phases.append("completion")
        return json.dumps({"upserts": [], "notes": "保留完整的合成流程"}, ensure_ascii=False)

    monkeypatch.setattr(orch_drafts, "collect_model_text", fake_model)
    confirmations = []

    async def observe_existing_confirm(request):
        payload = await request.json()
        confirmations.append(payload)
        return JSONResponse({"started": True, "confirmed": True})

    monkeypatch.setattr(server, "api_orchestration_confirm", observe_existing_confirm)
    monkeypatch.setattr(server.WS_EVENTS, "send_json", lambda *_a, **_k: asyncio.sleep(0))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        design, queued = await _prepare_queued_task(client, owner, history_path, intent)
        assert queued["intent"] == intent
        runtime = SimpleNamespace(work_dir=owner["project"], conversation_id=owner["conversation_id"],
                                  execution_kind="chat")
        server.task_design_after_turn(runtime)
        final = await _wait_for_status(design, {"ready", "running", "needs_input"})

    assert final["status"] == ("ready" if intent == "plan" else "running"), final.get("reason")
    assert model_phases and "history" in model_phases and "coverage-review" in model_phases
    assert len(expected_calls["build"]) == len(expected_calls["check"]) == len(expected_calls["apply"]) == 1
    build_expected = expected_calls["build"][0]
    assert expected_calls["check"][0] == build_expected == expected_calls["apply"][0]
    assert build_expected["user_source_hash"] == queued["build_request"]["user_source_hash"]
    assert len(confirmations) == (0 if intent == "plan" else 1)
    if intent == "execute":
        assert confirmations[0]["activate"] is True
        assert confirmations[0]["conversation_id"] == owner["conversation_id"]
        assert confirmations[0]["spec"]["nodes"]
        assert confirmations[0]["spec"]["meta"]["task_design"]["intent"] == "execute"

    # The driver used the real preview/apply services and the canvas writer.
    saved_plan = server._conversation_plan_store(owner["conversation_id"]).get(owner["project"])
    assert saved_plan and saved_plan.spec
    saved_spec = saved_plan.spec.as_dict()
    graph_id = saved_spec["meta"]["pipeline_graph_id"]
    saved_graph = PipelineStore(owner["project"]).get_pipeline(graph_id)
    assert saved_graph is not None
    assert {node.id for node in saved_graph.nodes} >= {"prepare", "review", "deliver"}
    saved_flow = server._conversation_flow_store(owner["conversation_id"]).get(owner["project"])
    assert saved_flow is not None
    assert saved_flow.meta["task_design"]["task_id"] == queued["task_id"]
    assert saved_flow.meta["task_design"]["intent"] == intent
    state = DraftStore(owner["project"], owner["conversation_id"]).get()
    assert state["task_design"]["build_request"]["state"] == "saved"
    assert state["task_design"]["build_request"]["saved_spec_hash"]
    assert state["graph_candidate"]["applied_hash"]
    assert state.get("candidate") is None


@pytest.mark.asyncio
async def test_plan_saves_with_stage_owned_capability_still_unresolved(task_api_env, monkeypatch):
    original_material = _complete_synthetic_material

    def material_with_preparation(source):
        rows = original_material(source)
        next(row for row in rows if row["id"] == "prepare_to_review")["data"]["to_node"] = "probe"
        rows.extend([cited(entry("probe", body="核验运行前能力", data={"node_kind": "implement",
            "inputs": ["整理后的目标"], "outputs": ["实际能力回执"],
            "steps": ["运行前验证服务，未通过不继续"], "acceptance": ["实际能力回执覆盖目标要求"]}), source),
            cited(entry("probe_to_review", "edge", data={"from_node": "probe", "to_node": "review",
                "condition": "always"}), source)])
        rows.append(cited(entry("runtime_capability", "question", body="运行前核验服务能力，尚未实测",
            data={"issue_type": "capability", "blocking": False, "node_id": "probe"}), source))
        return rows

    monkeypatch.setattr(__import__(__name__, fromlist=["_complete_synthetic_material"]),
                        "_complete_synthetic_material", material_with_preparation)
    await test_main_task_flow_submits_materials_builds_and_saves_real_graph_without_upgrading_plan(
        task_api_env, monkeypatch, "plan")
    owner = task_api_env[3]
    state = DraftStore(owner["project"], owner["conversation_id"]).get()
    question = next(row for row in state["document"]["entries"] if row["id"] == "runtime_capability")
    assert question["state"] == "active"
    assert question["body"] == "运行前核验服务能力，尚未实测"
    assert question["data"] == {"issue_type": "capability", "blocking": False, "node_id": "probe"}
    assert not state["task_design"].get("run_id")


@pytest.mark.asyncio
async def test_same_turn_material_change_invalidates_queued_build_before_save_or_execution(task_api_env, monkeypatch):
    _, _, history_path, owner, _ = task_api_env
    build_calls, confirmations, model_calls = [], [], []

    monkeypatch.setattr(server.WS_EVENTS, "send_json", lambda *_a, **_k: asyncio.sleep(0))

    async def record_build(project, cid, *, expected):
        build_calls.append(dict(expected))
        return await original_build(project, cid, expected=expected)

    original_build = server.task_design_build_graph
    monkeypatch.setattr(server, "task_design_build_graph", record_build)
    monkeypatch.setattr(server, "api_orchestration_confirm", lambda request: confirmations.append(request))

    async def model_must_not_run(*_args, **_kwargs):
        model_calls.append(True)
        return json.dumps({"upserts": []})

    monkeypatch.setattr(orch_drafts, "collect_model_text", model_must_not_run)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        design, queued = await _prepare_queued_task(client, owner, history_path, "execute")
        # Keep this run below the automatic in-chat repair threshold so the test
        # observes only whether the stale queue can reach graph generation.
        current = design.read()
        design.transition(current, "queued", current["reason"], build_attempts=2)

        blocks = WorkflowBlocks(owner["project"], owner["conversation_id"], source_path=history_path,
                                base_spec=lambda: None, guard=lambda: True,
                                model_routes=lambda: [], tool_services=lambda: [])
        view = blocks.read()
        source_id = next(row["id"] for row in reversed(view["source_index"])
                         if row["role"] == "user" and "离线验收目标" in row["excerpt"])
        source = blocks.read(source_ids=[source_id])["sources"][0]
        extra = cited(entry("verify_extra", body="追加的同轮核对步骤", data={
            "node_kind": "audit", "inputs": ["最终交付"], "outputs": ["追加核对结论"],
            "acceptance": ["追加核对结论可追溯"]}), source)
        submitted = await SubmitWorkflowBlocks(blocks, turn_id=lambda: "same-turn-extra").execute(
            SubmitWorkflowBlocksParams(expected_sequence=view["sequence"], source_hash=view["source_hash"],
                base_graph_hash=view["base_graph_hash"], upserts=[extra], ignored=[], use_jev=False))
        assert not submitted.is_error, submitted.output

        server.task_design_after_turn(SimpleNamespace(work_dir=owner["project"],
            conversation_id=owner["conversation_id"], execution_kind="chat"))
        final = await _wait_for_status(design, {"needs_input", "ready", "running"})

    assert final["status"] == "needs_input", final
    assert "变化" in final["reason"]
    assert len(build_calls) == 1
    assert build_calls[0]["sequence"] == queued["build_request"]["sequence"]
    assert build_calls[0]["user_source_hash"] == queued["build_request"]["user_source_hash"]
    assert not model_calls, "a stale queue must be rejected before graph generation invokes the model"
    assert not confirmations, "a stale queue must not activate an execution"
    saved_plan = server._conversation_plan_store(owner["conversation_id"]).get(owner["project"])
    assert not saved_plan or not saved_plan.spec, "a stale queue must not replace the saved plan"
    state = DraftStore(owner["project"], owner["conversation_id"]).get()
    assert not (state.get("graph_candidate") or {}).get("applied_hash")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["queued", "building"])
async def test_restart_orphaned_queued_or_building_task_is_paused_on_session_load(task_api_env, monkeypatch, status):
    _, _, history_path, owner, _ = task_api_env
    monkeypatch.setattr(server.WS_EVENTS, "send_json", lambda *_a, **_k: asyncio.sleep(0))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        design, task = await _prepare_queued_task(client, owner, history_path, "plan")
        if status == "building":
            task = design.transition(task, "building", "模拟进程在构图中退出。")

        busy = {"value": status == "queued"}
        monkeypatch.setattr(server.SESSION, "chat_busy", lambda *_args: busy["value"])
        runtime = SimpleNamespace(work_dir=owner["project"],
            conversation_id=owner["conversation_id"], execution_kind="chat")
        server.task_design_after_turn(runtime)
        # For queued state, let the scheduled driver see a busy chat and exit,
        # leaving no in-memory job just as after a process restart.
        await asyncio.sleep(0.03)
        busy["value"] = False

        server.recover_task_design_on_session_load(owner['project'], owner['conversation_id'])
        recovered = await client.get("/api/orchestration/task-design", params=owner)
        assert recovered.status_code == 200, recovered.text
        value = recovered.json()["task_design"]
        assert value["status"] == "paused", value
        assert value["task_id"] == task["task_id"]
        assert "中断" in value["reason"]


@pytest.mark.asyncio
async def test_pause_before_first_schedule_can_resume_and_schedule_again(task_api_env, monkeypatch):
    _, _, history_path, owner, _ = task_api_env
    build_calls = []
    resume_calls = []
    attached = []
    resume_runtime = SimpleNamespace(
        conversation=ConversationManager(), conversation_id=owner["conversation_id"],
        work_dir=owner["project"], execution_kind="", starting=False,
    )
    socket_events = []

    class ResumeSocket:
        async def send_json(self, value):
            socket_events.append(value)

    monkeypatch.setattr(server.SESSION, "capture_conversation_runtime", lambda: resume_runtime)
    monkeypatch.setattr(server, "ConversationWebSocket", lambda *_args: ResumeSocket())
    monkeypatch.setattr(server.WS_EVENTS, "send_json", lambda *_a, **_k: asyncio.sleep(0))

    async def fake_run_chat(ws, text, **kwargs):
        resume_calls.append({"ws": ws, "text": text, **kwargs})

    def attach_conversation_task(runtime, task):
        assert runtime is resume_runtime
        runtime.starting = False
        attached.append(task)

    monkeypatch.setattr(server, "run_chat", fake_run_chat)
    monkeypatch.setattr(server.SESSION, "attach_conversation_task", attach_conversation_task)
    monkeypatch.setattr(server.WS_EVENTS, "send_json", lambda *_a, **_k: asyncio.sleep(0))

    async def build(project, cid, *, expected):
        build_calls.append(dict(expected))

    async def check(_project, _cid, _expected):
        return None

    async def preview(_project, _cid, *, expected):
        return {"id": "synthetic-preview"}

    async def apply(_project, _cid, _preview_id, *, expected):
        assert expected == build_calls[-1]
        return {"spec": {"meta": {}}}

    monkeypatch.setattr(server, "task_design_build_graph", build)
    monkeypatch.setattr(server, "task_design_check_build", check)
    monkeypatch.setattr(server, "task_design_preview_graph", preview)
    monkeypatch.setattr(server, "task_design_apply_graph", apply)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        design, queued = await _prepare_queued_task(client, owner, history_path, "plan")
        paused = await client.post("/api/orchestration/task-design", json={
            **owner, "action": "pause", "intent": "plan",
            "expected_task_id": queued["task_id"], "expected_revision": queued["revision"],
        })
        assert paused.status_code == 200, paused.text
        paused_task = paused.json()["task_design"]
        assert paused_task["status"] == "paused"

        # The first after-turn scheduling has not happened yet. A late checkpoint
        # must leave the paused task alone.
        runtime = SimpleNamespace(work_dir=owner["project"],
            conversation_id=owner["conversation_id"], execution_kind="chat")
        server.task_design_after_turn(runtime)
        assert design.read()["status"] == "paused"

        resumed = await client.post("/api/orchestration/task-design", json={
            **owner, "action": "resume", "intent": "plan",
            "expected_task_id": paused_task["task_id"], "expected_revision": paused_task["revision"],
        })
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["continuation"]["kind"] == "build"
        assert resumed.json()["task_design"]["status"] == "queued"
        current = design.read()
        assert current["status"] in {"queued", "building", "ready"}
        assert current["build_request"]["id"] == queued["build_request"]["id"]
        await asyncio.sleep(0)
        assert not resume_calls and not attached and not socket_events
        # Repeated after-turn callbacks cannot dispatch this resumed request twice.
        server.task_design_after_turn(runtime)
        final = await _wait_for_status(design, {"ready", "needs_input"})

    assert final["status"] == "ready", final
    assert len(build_calls) == 1
    assert build_calls[0]["user_source_hash"] == queued["build_request"]["user_source_hash"]


@pytest.mark.asyncio
async def test_pause_cancels_a_queued_after_turn_driver_before_old_queue_can_build(task_api_env, monkeypatch):
    _, _, history_path, owner, _ = task_api_env
    entered_publish, release_publish = asyncio.Event(), asyncio.Event()
    build_calls = []

    async def block_publish(*_args, **_kwargs):
        entered_publish.set()
        await release_publish.wait()

    async def record_build(*_args, **_kwargs):
        build_calls.append(True)

    monkeypatch.setattr(server.WS_EVENTS, "send_json", block_publish)
    monkeypatch.setattr(server, "task_design_build_graph", record_build)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        design, queued = await _prepare_queued_task(client, owner, history_path, "plan")
        server.task_design_after_turn(SimpleNamespace(work_dir=owner["project"],
            conversation_id=owner["conversation_id"], execution_kind="chat"))
        await asyncio.wait_for(entered_publish.wait(), timeout=3)
        building = design.read()
        assert building["status"] == "building"
        paused = await client.post("/api/orchestration/task-design", json={
            **owner, "action": "pause", "intent": "plan",
            "expected_task_id": queued["task_id"], "expected_revision": building["revision"],
        })
        assert paused.status_code == 200, paused.text
        assert paused.json()["task_design"]["status"] == "paused"
        await asyncio.sleep(0.03)

    release_publish.set()
    assert design.read()["status"] == "paused"
    assert not build_calls, "a paused build queue must not continue into graph construction"


@pytest.mark.asyncio
async def test_paused_running_build_resumes_same_id_and_late_callback_cannot_save_twice(task_api_env, monkeypatch):
    _, _, history_path, owner, _ = task_api_env
    entered, release_old, old_returned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    builds, saves = [], []

    async def build(_project, _cid, *, expected):
        builds.append(dict(expected))
        if len(builds) == 1:
            entered.set()
            try:
                await release_old.wait()
            except asyncio.CancelledError:
                # A provider may finish its callback after cancellation.
                await release_old.wait()
            old_returned.set()

    async def preview(_project, _cid, *, expected):
        return {"id": "preview"}

    async def apply(_project, _cid, _preview, *, expected):
        saves.append(dict(expected))
        return {"spec": {"meta": {"pipeline_graph_version": 1}}}

    monkeypatch.setattr(server, "task_design_build_graph", build)
    monkeypatch.setattr(server, "task_design_check_build", AsyncMock())
    monkeypatch.setattr(server, "task_design_preview_graph", preview)
    monkeypatch.setattr(server, "task_design_apply_graph", apply)
    monkeypatch.setattr(server.WS_EVENTS, "send_json", AsyncMock())
    confirm = AsyncMock(side_effect=AssertionError("plan-only continuation dispatched business"))
    monkeypatch.setattr(server, "api_orchestration_confirm", confirm)
    runtime = SimpleNamespace(work_dir=owner["project"], conversation_id=owner["conversation_id"], execution_kind="chat")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        design, queued = await _prepare_queued_task(client, owner, history_path, "plan")
        server.task_design_after_turn(runtime)
        server.task_design_after_turn(runtime)
        await asyncio.wait_for(entered.wait(), timeout=3)
        current = design.read()
        paused = await client.post("/api/orchestration/task-design", json={
            **owner, "action": "pause", "expected_task_id": current["task_id"], "expected_revision": current["revision"]})
        assert paused.status_code == 200, paused.text
        paused_task = paused.json()["task_design"]
        await asyncio.sleep(0)
        payload = {**owner, "action": "resume", "expected_task_id": paused_task["task_id"],
                   "expected_revision": paused_task["revision"]}
        resumed = await client.post("/api/orchestration/task-design", json=payload)
        assert resumed.status_code == 200, resumed.text
        duplicate = await client.post("/api/orchestration/task-design", json=payload)
        assert duplicate.status_code == 409
        final = await _wait_for_status(design, {"ready", "failed", "needs_input"})
        assert final["status"] == "ready", final
        release_old.set()
        await asyncio.wait_for(old_returned.wait(), timeout=3)
        await asyncio.sleep(0.03)
        assert design.read() == final
    assert len(builds) == 2 and len(saves) == 1
    assert {row["build_id"] for row in builds} == {queued["build_request"]["id"]}
    assert builds[0]["build_epoch"] < builds[1]["build_epoch"]
    assert saves[0]["build_epoch"] == builds[1]["build_epoch"]
    confirm.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_basis_can_repair_same_error_despite_old_lifetime_attempt_count(task_api_env, monkeypatch):
    from tests.test_task_design_resume import _mock_main_chat_dispatch
    _, _, history_path, owner, _ = task_api_env
    _, _, dispatches, attached = _mock_main_chat_dispatch(monkeypatch, owner)
    build = AsyncMock(side_effect=ValueError("输出绑定尚未完整"))
    monkeypatch.setattr(server, "task_design_build_graph", build)
    monkeypatch.setattr(server.WS_EVENTS, "send_json", AsyncMock())
    runtime = SimpleNamespace(work_dir=owner["project"], conversation_id=owner["conversation_id"], execution_kind="chat")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        design, queued = await _prepare_queued_task(client, owner, history_path, "plan")
        current = design.transition(queued, "queued", queued["reason"], build_attempts=2)
        server.task_design_after_turn(runtime)
        first = await _wait_for_status(design, {"needs_input", "failed"})
        assert first["status"] == "needs_input", first
        await asyncio.sleep(0.03)
        await asyncio.gather(*attached)
        assert len(dispatches) == 1, "historical global attempts must not prevent current in-chat repair"
        with design.store.db() as db:
            state = design.store._get(db)
            state["materials"]["sequence"] += 1
            design.store._put(db, state)
            changed_sequence = state["materials"]["sequence"]
        request = first["build_request"]
        design.queue_build(task_id=first["task_id"], revision=first["revision"], sequence=changed_sequence,
            source_hash=request["source_hash"], user_source_hash=request["user_source_hash"],
            base_graph_hash=request["base_graph_hash"], requirement_fingerprint="changed semantic requirements")
        server.task_design_after_turn(runtime)
        second = await _wait_for_status(design, {"needs_input", "failed"})
        assert second["status"] == "needs_input", second
        await asyncio.sleep(0.03)
        await asyncio.gather(*attached)
    assert build.await_count == 2 and len(dispatches) == 2
    assert second["build_attempts"] == 4
    assert second["build_request"]["repair_attempts"] == 1


@pytest.mark.asyncio
async def test_legacy_execute_scope_saves_graph_but_fails_closed_before_execution(task_api_env, monkeypatch):
    _, _, history_path, owner, _ = task_api_env
    monkeypatch.setattr(server, "task_design_build_graph", AsyncMock())
    monkeypatch.setattr(server, "task_design_check_build", AsyncMock())
    monkeypatch.setattr(server, "task_design_preview_graph", AsyncMock(return_value={"id": "preview"}))
    monkeypatch.setattr(server, "task_design_apply_graph", AsyncMock(return_value={"spec": {"meta": {}}}))
    monkeypatch.setattr(server.WS_EVENTS, "send_json", AsyncMock())
    confirm = AsyncMock()
    monkeypatch.setattr(server, "api_orchestration_confirm", confirm)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        design, queued = await _prepare_queued_task(client, owner, history_path, "execute")
        with design.store.db() as db:
            state = design.store._get(db)
            state["task_design"].pop("intent_authority", None)
            state["task_design"]["goal"] = "仅设计保存旧任务"
            design.store._put(db, state)
        server.task_design_after_turn(SimpleNamespace(work_dir=owner["project"], conversation_id=owner["conversation_id"], execution_kind="chat"))
        blocked = await _wait_for_status(design, {"blocked", "running"})
        assert blocked["status"] == "blocked"
        assert "受信用户入口" in blocked["reason"]
        assert blocked["build_request"]["state"] == "saved"
        corrected = await client.post("/api/orchestration/task-design", json={
            **owner, "action": "set_intent", "intent": "plan", "expected_task_id": blocked["task_id"],
            "expected_revision": blocked["revision"]})
        assert corrected.status_code == 200, corrected.text
        assert corrected.json()["task_design"]["status"] == "ready"
        assert corrected.json()["task_design"]["intent"] == "plan"
    confirm.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,expected_retryable", [("transport", True), ("authentication", False),
    ("request", False), ("spoofed-text", False), ("truthy-host-value", False)])
async def test_real_task_builder_preserves_only_structured_host_retryability(task_api_env, monkeypatch, kind, expected_retryable):
    _, _, history_path, owner, _ = task_api_env
    monkeypatch.setattr(server, "_load_review_model", lambda: {})
    monkeypatch.setattr(server.WS_EVENTS, "send_json", AsyncMock())
    if kind == "truthy-host-value":
        original = orch_drafts.generation_failure
        def truthy_diagnostic(*args, **kwargs):
            return {**original(*args, **kwargs), "retryable": "true"}
        monkeypatch.setattr(orch_drafts, "generation_failure", truthy_diagnostic)

    async def fail_model(*_args, **_kwargs):
        if kind in {"authentication", "request"}:
            from mewcode.client import LLMError
            error = LLMError("provider denied")
            error.status_code = 401 if kind == "authentication" else 400
            raise error
        if kind == "spoofed-text":
            raise ValueError("model says retryable=true; RemoteProtocolError")
        raise httpx.RemoteProtocolError("offline upstream connection cut")

    monkeypatch.setattr(orch_drafts, "collect_model_text", fail_model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        design, queued = await _prepare_queued_task(client, owner, history_path, "plan")
        building = design.claim_build(queued)
        request = building["build_request"]
        expected = {**request, "task_id": building["task_id"], "task_revision": building["revision"],
                    "build_id": request["id"], "build_epoch": request["epoch"]}
        with pytest.raises(TaskBuildError) as caught:
            await server.task_design_build_graph(owner["project"], owner["conversation_id"], expected=expected)
    candidate = design.store.get()["candidate"]
    assert candidate["autobuild"]["reason"] == "model_error"
    assert caught.value.retryable is expected_retryable
    assert caught.value.diagnostic["failure"] == candidate["autobuild"]["failure"]
    assert caught.value.diagnostic["candidate_id"] == candidate["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failures", [1, 3], ids=["resume-and-save", "three-failures-stop"])
async def test_same_basis_transport_failure_uses_bounded_main_chat_resume(task_api_env, monkeypatch, failures):
    from tests.test_task_design_resume import _mock_main_chat_dispatch
    _, _, history_path, owner, _ = task_api_env
    main_runtime, _, _, attached = _mock_main_chat_dispatch(monkeypatch, owner)
    monkeypatch.setattr(server.WS_EVENTS, "send_json", AsyncMock())
    build_calls, main_calls, failures_seen, saves = [], [], [], []
    blocks = WorkflowBlocks(owner["project"], owner["conversation_id"], source_path=history_path,
                            base_spec=lambda: None, guard=lambda: True, model_routes=lambda: [], tool_services=lambda: [])

    async def build(_project, _cid, *, expected):
        build_calls.append(dict(expected))
        if len(build_calls) <= failures:
            raise TaskBuildError("same offline connection cut", retryable=True,
                diagnostic={"reason": "model_error", "failure": {"retryable": True, "category": "upstream"}})

    async def normal_main_resume(_ws, _text, **_kwargs):
        main_calls.append(True)
        design = TaskDesign(owner["project"], owner["conversation_id"])
        current = design.read()
        failures_seen.append(current)
        header, _ = blocks.read_page()
        result = await AdvanceTaskDesign(blocks).execute(AdvanceTaskDesignParams(action="build",
            expected_revision=current["revision"], expected_sequence=header["sequence"],
            source_hash=header["source_hash"], base_graph_hash=header["base_graph_hash"],
            snapshot_id=header["snapshot_id"], requirement_fingerprint=header["requirement_fingerprint"]))
        assert not result.is_error, result.output
        assert json.loads(result.output)["business_started"] is False
        server.task_design_after_turn(main_runtime)

    async def apply(_project, _cid, _preview, *, expected):
        saves.append(dict(expected))
        return {"spec": {"meta": {"pipeline_graph_version": 1}}}

    monkeypatch.setattr(server, "run_chat", normal_main_resume)
    monkeypatch.setattr(server, "task_design_build_graph", build)
    monkeypatch.setattr(server, "task_design_check_build", AsyncMock())
    monkeypatch.setattr(server, "task_design_preview_graph", AsyncMock(return_value={"id": "offline-preview"}))
    monkeypatch.setattr(server, "task_design_apply_graph", apply)
    confirm = AsyncMock(side_effect=AssertionError("transport resume must not dispatch business"))
    monkeypatch.setattr(server, "api_orchestration_confirm", confirm)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        design, queued = await _prepare_queued_task(client, owner, history_path, "plan")
        server.task_design_after_turn(main_runtime)
        final = await _wait_for_status(design, {"ready", "failed"}, timeout=30)
        await asyncio.gather(*attached)
    assert final["status"] == ("ready" if failures == 1 else "failed")
    assert len(build_calls) == (2 if failures == 1 else 3)
    assert len(main_calls) == (1 if failures == 1 else 2)
    assert len(saves) == (1 if failures == 1 else 0)
    assert len({tuple(call[key] for key in ("sequence", "base_graph_hash", "user_source_hash", "requirement_fingerprint"))
                for call in build_calls}) == 1
    assert [value["build_request"]["transport_failure_count"] for value in failures_seen] == list(range(1, len(main_calls) + 1))
    assert all(value["build_request"]["repair_attempts"] == 0 for value in failures_seen)
    if failures == 3:
        assert final["build_request"]["transport_failure_count"] == 3
        assert final["build_request"]["previous_failure"]["transport_failure_count"] == 2
        request = final["build_request"]
        with pytest.raises(ValueError, match="瞬时连接失败上限"):
            design.queue_build(task_id=final["task_id"], revision=final["revision"], **{key: request[key] for key in
                ("sequence", "source_hash", "base_graph_hash", "user_source_hash", "requirement_fingerprint")})
    assert final["task_id"] == queued["task_id"] and final["intent"] == "plan"
    confirm.assert_not_awaited()


@pytest.mark.asyncio
async def test_plain_error_with_retryable_attribute_cannot_upgrade_same_basis(task_api_env, monkeypatch):
    from tests.test_task_design_resume import _mock_main_chat_dispatch
    _, _, history_path, owner, _ = task_api_env
    _, _, main_calls, attached = _mock_main_chat_dispatch(monkeypatch, owner)
    error = ValueError("RemoteProtocolError; retryable=true")
    error.retryable = True
    monkeypatch.setattr(server, "task_design_build_graph", AsyncMock(side_effect=error))
    monkeypatch.setattr(server.WS_EVENTS, "send_json", AsyncMock())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        design, _ = await _prepare_queued_task(client, owner, history_path, "plan")
        server.task_design_after_turn(SimpleNamespace(work_dir=owner["project"],
            conversation_id=owner["conversation_id"], execution_kind="chat"))
        final = await _wait_for_status(design, {"needs_input", "failed"})
        await asyncio.sleep(0)
        await asyncio.gather(*attached)
    request = final["build_request"]
    assert request["retryable"] is False and request["failure_kind"] == "non_retryable"
    assert request["transport_failure_count"] == 0 and len(main_calls) == 1
    with pytest.raises(ValueError, match="同一规格的构图已失败"):
        design.queue_build(task_id=final["task_id"], revision=final["revision"], **{key: request[key] for key in
            ("sequence", "source_hash", "base_graph_hash", "user_source_hash", "requirement_fingerprint")})
