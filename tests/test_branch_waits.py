from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from mewcode.agent import AskUserRequest
from mewcode.expert_runs import ExpertRunStore
from mewcode.experts.iteration_recovery import CONTINUE, PAUSE
from mewcode.experts.node_ledger import NodeLedger, ledger_for_run
from mewcode.experts.waits import BranchWaitPaused, wait_for_interaction


def _flow(nodes):
    return {
        "flow_id": "branch-waits",
        "version": 1,
        "label": "Branch waits",
        "nodes": nodes,
        "edges": [],
        "finalizer_node_id": nodes[-1]["node_id"],
    }


def _store(tmp_path, *, run_id="a" * 32, nodes=None):
    nodes = nodes or [{"node_id": "work", "role": "executor", "label": "工作"}]
    store = ExpertRunStore(tmp_path / "runs")
    flow = _flow(nodes)
    store.create(run_id=run_id, conversation_id="chat", task="任务", attachments=[],
        profile={}, strategy="stable", flow=flow)
    node_id = nodes[0]["node_id"]
    store.mutate(run_id, lambda value: value["node_states"].setdefault(node_id, {}).update(status="running"))
    workspace = tmp_path / 'workspace'
    workspace.mkdir(exist_ok=True)
    store.mutate(run_id, lambda value: value.update(authorization_manifest={
        'schema_version': 2, 'workspace': str(workspace), 'nodes': {}, 'scope_digest': 'fixture', 'approved': True}))
    return store, run_id, flow


def _request(request_id, *, tool="AskUserQuestion", options=None, question="需要补充业务信息"):
    return AskUserRequest(request_id, tool, [{
        "name": "direction",
        "type": "radio" if options else "text",
        "message": question,
        "options": options or [],
    }], asyncio.get_running_loop().create_future())


@pytest.mark.asyncio
async def test_business_question_timeout_pauses_only_its_branch(tmp_path, monkeypatch):
    from mewcode.expert_group import ExpertGroupOrchestrator, ExpertProfile, ExpertRoleResult
    from mewcode.experts.flow import ExpertFlowSpec
    from mewcode.model_registry import ModelRef

    monkeypatch.setenv("MEWCODE_EXPERT_HITL_SOFT_TIMEOUT", "0.02")
    run_id = "b" * 32
    nodes = [
        {"node_id": "needs_user", "role": "executor", "label": "等待业务决定"},
        {"node_id": "independent", "role": "executor", "label": "独立工作"},
        {"node_id": "dependent", "role": "executor", "label": "等待前置任务"},
        {"node_id": "final", "role": "manager", "label": "汇总"},
    ]
    flow_value = _flow(nodes)
    flow_value["edges"] = [
        {"from": "needs_user", "to": "dependent"},
        {"from": "dependent", "to": "final"},
        {"from": "independent", "to": "final"},
    ]
    flow = ExpertFlowSpec.from_value(flow_value)
    runs = ExpertRunStore(tmp_path / "runs")
    runs.create(run_id=run_id, conversation_id="chat", task="任务", attachments=[],
        profile={}, strategy="stable", flow=flow)
    visible = []
    completed = []
    session = SimpleNamespace()

    async def node_runner(node, _model, _prompt, _policy, member=None):
        if node.node_id == 'independent':
            assert runs.load(run_id)['status'] == 'running'
        if node.node_id == "needs_user":
            request = _request("business-one")

            async def emit(event):
                visible.append(event)

            await wait_for_interaction(request, kind="ask_user", session=session, emit=emit,
                store=runs, run_id=run_id, stage_key=node.node_id)
        completed.append(node.node_id)
        return ExpertRoleResult(text=f"已完成：{node.node_id}", status="completed")

    async def fallback(*_args):
        raise AssertionError("the graph node runner owns execution")

    profile = ExpertProfile(
        manager=ModelRef("openai", "test", ""),
        executor=ModelRef("openai", "test", ""),
        auditor=ModelRef("openai", "test", ""),
    )
    result = await ExpertGroupOrchestrator(
        runner=fallback,
        node_runner=node_runner,
        run_store=runs,
        archive_root=tmp_path / "archives",
    ).run("任务", profile, {"providers": []}, strategy="stable", run_id=run_id, flow=flow)

    snapshot = runs.load(run_id)
    assert result.status == "paused", (result.final_summary, completed, snapshot, visible)
    assert "needs_user" not in completed
    assert "independent" in completed
    assert "final" not in completed
    assert "dependent" not in completed
    assert snapshot["status"] != "paused"
    assert snapshot["node_states"]["needs_user"]["status"] == "paused"
    for node_id in ("dependent", "final"):
        assert snapshot["node_states"].get(node_id, {}).get("status", "pending") == "pending"
    assert not any(event.get('reason') == 'branch_not_taken' for event in snapshot['events'])
    assert any(event.get("type") == "ask_user" for event in visible)


@pytest.mark.asyncio
async def test_host_continuation_timeout_records_recommended_answer_without_pausing(tmp_path, monkeypatch):
    from mewcode.experts.node_ledger import NodeLedger

    monkeypatch.setenv("MEWCODE_EXPERT_HITL_SOFT_TIMEOUT", "0.02")
    store, run_id, _ = _store(tmp_path)
    request = _request("continue-one", tool="IterationRecovery", options=[
        {"value": CONTINUE, "label": CONTINUE, "recommended": True},
        {"value": PAUSE, "label": PAUSE, "recommended": False},
    ])
    events = []

    async def emit(event):
        events.append(event)

    answer = await wait_for_interaction(request, kind="ask_user", session=SimpleNamespace(),
        emit=emit, store=store, run_id=run_id, stage_key="work")
    snapshot = store.load(run_id)
    view = NodeLedger(snapshot.get("node_ledgers") or {}).view("work")
    answer_fact = next(fact for fact in view.history if fact.kind == "answer")
    assert answer == {"direction": CONTINUE}
    assert answer_fact.payload["mode"] == "auto_selected_recommended"
    assert answer_fact.payload["host_continuation"] is True
    assert answer_fact.payload["expands_authority"] is False
    assert snapshot["status"] == "running"
    assert snapshot["node_states"]["work"]["status"] != "paused"
    assert not any(event.get("type") == "question_suppressed" for event in events)


@pytest.mark.asyncio
async def test_real_business_answer_clears_the_original_request_and_all_question_projections(tmp_path):
    store, run_id, _ = _store(tmp_path)
    request = _request('business-answered')
    async def emit(event):
        if event.get('type') == 'ask_user':
            request.future.set_result({'direction': 'use the supplied acceptance criteria'})
    answer = await wait_for_interaction(request, kind='ask_user', session=SimpleNamespace(),
        emit=emit, store=store, run_id=run_id, stage_key='work')
    view = ledger_for_run(store.load(run_id)).view('work')
    assert answer['direction'] == 'use the supplied acceptance criteria'
    assert view.pending_question is None
    assert not any(f.payload.get('kind') == 'human_question' for f in view.active_blockers)
    assert len([f for f in view.history if f.kind == 'question']) == 1
    assert store.load(run_id)['node_states']['work']['status'] != 'completed'


@pytest.mark.asyncio
@pytest.mark.parametrize('answer', [{}, {'direction': ''}, {'direction': '  '}, {'wrong_name': 'continue'}, None, ' '])
async def test_blank_or_cancelled_submission_does_not_answer_or_complete_the_node(tmp_path, answer):
    store, run_id, _ = _store(tmp_path)
    request = _request('empty-submission')

    async def emit(event):
        if event.get('type') == 'ask_user':
            request.future.set_result(answer)

    with pytest.raises(BranchWaitPaused, match='branch_wait:work'):
        await wait_for_interaction(request, kind='ask_user', session=SimpleNamespace(),
            emit=emit, store=store, run_id=run_id, stage_key='work')
    snapshot = store.load(run_id)
    stage = snapshot['node_states']['work']
    view = ledger_for_run(snapshot).view('work')
    assert stage['status'] == 'paused'
    assert stage['pending_interaction']['request_id'] == request.request_id
    assert view.pending_question is not None
    assert not any(f.kind == 'answer' for f in view.history)
    assert not any(f.kind == 'clearance' for f in view.history)


@pytest.mark.asyncio
async def test_partial_submission_preserves_all_questions_until_complete(tmp_path):
    store, run_id, _ = _store(tmp_path)
    request = _request('partial-submission')
    request.questions.append({'name': 'scope', 'type': 'text', 'message': '处理范围？'})

    async def emit(event):
        if event.get('type') == 'ask_user':
            request.future.set_result({'direction': 'use supplied files'})

    with pytest.raises(BranchWaitPaused):
        await wait_for_interaction(request, kind='ask_user', session=SimpleNamespace(),
            emit=emit, store=store, run_id=run_id, stage_key='work')
    pending = store.load(run_id)['node_states']['work']['pending_interaction']
    assert pending['questions'] == request.questions


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,name', [('ask_user', 'q0'), ('human_oncall', 'choice')])
async def test_legacy_unnamed_question_uses_the_same_answer_key_as_the_ui(tmp_path, kind, name):
    store, run_id, _ = _store(tmp_path)
    request = _request('legacy-answer-key')
    del request.questions[0]['name']

    async def emit(event):
        if event.get('type') == kind:
            request.future.set_result({name: 'provided choice'})

    answer = await wait_for_interaction(request, kind=kind, session=SimpleNamespace(),
        emit=emit, store=store, run_id=run_id, stage_key='work')
    assert answer == {name: 'provided choice'}
    assert store.load(run_id)['node_states']['work'].get('pending_interaction') is None


@pytest.mark.asyncio
async def test_host_continuation_can_autoanswer_reversible_business_choice_after_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("MEWCODE_EXPERT_HITL_SOFT_TIMEOUT", "0.02")
    store, run_id, _ = _store(tmp_path)
    store.mutate(run_id, lambda value: value["node_states"]["work"].update(
        status="running", blockers=[{"kind": "business_ambiguity", "reason": "需要用户决定"}]))
    request = _request("continue-blocked", tool="SchedulerRecovery", options=[
        {"value": CONTINUE, "label": CONTINUE, "recommended": True},
        {"value": PAUSE, "label": PAUSE},
    ])
    answer = await wait_for_interaction(request, kind="ask_user", session=SimpleNamespace(),
        emit=lambda _event: asyncio.sleep(0), store=store, run_id=run_id, stage_key="work")
    assert answer == {'direction': CONTINUE}
    assert store.load(run_id)["status"] == "running"
    assert store.load(run_id)['kernel_gates']['continue-blocked']['snapshot_id']


@pytest.mark.asyncio
async def test_same_unanswered_question_and_evidence_emits_question_suppressed(tmp_path, monkeypatch):
    monkeypatch.setenv("MEWCODE_EXPERT_HITL_SOFT_TIMEOUT", "0.02")
    store, run_id, _ = _store(tmp_path)
    session, events = SimpleNamespace(), []
    async def emit(event):
        events.append(event)

    with pytest.raises(BranchWaitPaused):
        await wait_for_interaction(_request("business-repeat"), kind="ask_user", session=session,
            emit=emit, store=store, run_id=run_id, stage_key="work", scope_hash="evidence-v1")
    events.clear()
    with pytest.raises(BranchWaitPaused):
        await wait_for_interaction(_request("business-repeat"), kind="ask_user", session=session,
            emit=emit, store=store, run_id=run_id, stage_key="work", scope_hash="evidence-v1")
    assert not any(event.get("type") == "ask_user" for event in events)
    assert any(event.get("question_suppressed") is True for event in events)


@pytest.mark.asyncio
async def test_changed_evidence_allows_same_question_to_be_asked_again(tmp_path, monkeypatch):
    monkeypatch.setenv("MEWCODE_EXPERT_HITL_SOFT_TIMEOUT", "0.02")
    store, run_id, _ = _store(tmp_path)
    session, events = SimpleNamespace(), []

    async def emit(event):
        events.append(event)
        if event.get("type") == "ask_user":
            request = getattr(session, "_ask_user_request")
            request.future.set_result({"direction": "补充了新事实"})

    with pytest.raises(BranchWaitPaused):
        await wait_for_interaction(_request("business-changed"), kind="ask_user", session=session,
            emit=lambda _event: asyncio.sleep(0), store=store, run_id=run_id, stage_key="work",
            scope_hash="evidence-v1")
    store.mutate(run_id, lambda value: ledger_for_run(value).record(
        "work", "artifact", {"path": "new-evidence.txt"}, source="host", identity="new-evidence"))
    answer = await wait_for_interaction(_request("business-changed"), kind="ask_user", session=session,
        emit=emit, store=store, run_id=run_id, stage_key="work", scope_hash="evidence-v1")
    assert answer == {"direction": "补充了新事实"}
    assert any(event.get("type") == "ask_user" for event in events)
    assert not any(event.get("question_suppressed") is True for event in events)
