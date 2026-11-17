"""Revision reuse and host identity must follow actual dependencies, not graph prose."""
import copy
from dataclasses import replace

from mewcode.experts.flow import ExpertFlowSpec
from mewcode.experts.stage_context import input_revision, model_choices_prompt, runtime_prompt
from mewcode.model_registry import ModelRef
from mewcode.orchestration.draft_sources import digest
from mewcode.orchestration.execution_scope import prepare_orchestration_scope
from mewcode.orchestration.task_design import TaskDesign, question_binding, validate_question_binding
from tests.test_expert_task_identity import runtime_for
from tests.test_orchestration_start_run import _bound_spec, confirm_env
import pytest
from types import SimpleNamespace


def flow():
    return ExpertFlowSpec.from_value({"flow_id": "design", "version": 1, "finalizer_node_id": "work",
        "nodes": [{"node_id": "work", "role": "executor", "responsibility": "处理一份样本"},
                  {"node_id": "sibling", "role": "executor", "responsibility": "准备其他材料"}],
        "edges": [], "meta": {"task_design": {"task_id": "fixture"},
            "workflow_draft": {"node_work_keys": {"work": "sample"},
                "node_input_bindings": {"work": {"原稿": "source"}}}}})


def revision(value):
    return input_revision(sources={"source": {"text": "same", "source_revision": "receipt"}},
                          flow=value, node=value.node_map["work"], model=ModelRef("p", "m", ""))


def test_unrelated_flow_revision_reuses_node_but_changed_contract_does_not():
    before = flow()
    unrelated = replace(before, version=2, nodes=(before.nodes[0], replace(before.nodes[1], responsibility="新方法")))
    assert revision(before) == revision(unrelated)
    changed = replace(unrelated, nodes=(replace(before.nodes[0], responsibility="处理全部材料"), before.nodes[1]))
    assert revision(before) != revision(changed)
    changed_binding = copy.deepcopy(before.meta)
    changed_binding["workflow_draft"]["node_input_bindings"]["work"]["原稿"] = "other_source"
    assert revision(before) != revision(replace(before, meta=changed_binding))


def test_runtime_prompt_reports_actual_version_model_and_bound_inputs():
    text = runtime_prompt(run_id="run", flow=flow(), node_id="work", model=ModelRef("p", "m", ""),
                          work_dir="fixture", attempt=2)
    for value in ('"flow_version": 1', '"task_id": "fixture"', '"work_key": "sample"',
                  '"原稿": "source"', '"model_id": "m"'):
        assert value in text


def model_choice_record():
    selected = {'provider_key': 'relay', 'model_id': 'new-model', 'effort': 'max'}
    return {'locked_models': {'work': selected}, 'difficult_node_model_changes': [{
        'change_id': 'actual-user-choice', 'authorized_by': 'user', 'model': selected,
        'apply_node_ids': ['work'], 'node_ids': ['work', 'future-scope']}]}


def test_current_user_model_choice_overrides_only_old_model_labels():
    record = model_choice_record()
    text = model_choices_prompt(record)
    assert 'actual-user-choice' in text and 'new-model' in text and 'max' in text
    assert 'future-scope' not in text
    assert '历史产物保留原模型身份' in text
    assert '不放宽业务范围、质量验收、路径、费用或外发权限' in text


@pytest.mark.parametrize('fault', ['unconfirmed', 'different_binding', 'missing_receipt', 'superseded'])
def test_model_choice_context_cannot_replay_unconfirmed_or_superseded_changes(fault):
    record = model_choice_record()
    if fault == 'unconfirmed':
        record['difficult_node_model_changes'][0]['authorized_by'] = 'assistant'
    elif fault == 'different_binding':
        record['locked_models']['work'] = {'provider_key': 'other', 'model_id': 'unrelated', 'effort': 'max'}
    elif fault == 'missing_receipt':
        record['difficult_node_model_changes'][0].pop('change_id')
    else:
        newer = copy.deepcopy(record['difficult_node_model_changes'][0])
        newer['change_id'] = 'later-user-choice'
        newer['model'] = {'provider_key': 'other', 'model_id': 'latest-choice', 'effort': 'max'}
        record['difficult_node_model_changes'].append(newer)
    assert model_choices_prompt(record) == ''


def test_projected_bindings_reach_runtime_context_and_revision():
    from mewcode.orchestration.convert import from_expert_flow, to_expert_flow
    from mewcode.orchestration.workflow_draft import project_graph
    from tests.test_task_design_projection import document, node, other

    material = document(
        node("work", kind="plan", data={"work_key": "prepare-selected-output",
            "inputs": ["topic"], "input_bindings": {"topic": "topic_input"}}),
        other("topic_input", "input", data={"name": "topic", "collect_at": "start"}))
    spec = project_graph(material, None, conversation_id="test", revision=1)
    spec = replace(spec, meta={**spec.meta, "task_design": {"task_id": "fixture"}})
    projected = to_expert_flow(spec)
    restored = from_expert_flow(projected)
    assert restored.meta["task_design"] == spec.meta["task_design"]
    assert restored.meta["workflow_draft"] == spec.meta["workflow_draft"]
    text = runtime_prompt(run_id="run", flow=projected, node_id="work",
                          model=ModelRef("p", "m", ""), work_dir="fixture", attempt=1)
    assert '"work_key": "prepare-selected-output"' in text
    assert '"topic": "topic_input"' in text
    assert '"task_id": "fixture"' in text
    changed = copy.deepcopy(projected.meta)
    changed["workflow_draft"]["node_input_bindings"]["work"]["topic"] = "updated_input"
    assert revision(projected) != revision(replace(projected, meta=changed))


def test_only_host_bound_exact_flow_inherits_task_identity(tmp_path):
    runtime, checker = runtime_for(tmp_path)
    runtime.work_dir = str(tmp_path)
    service = TaskDesign(tmp_path, runtime.conversation_id)
    design = service.start("execute")
    spec = flow().as_dict()
    spec["meta"]["task_design"]["task_id"] = design["task_id"]
    service.transition(design, "ready", execution_flow_hash=digest(spec))
    receipt = prepare_orchestration_scope(runtime, task="目标", run_id="a" * 32, checkpoint={"locked_flow": spec})
    assert receipt["task_id"] == design["task_id"]
    assert checker.task_authorization is None  # Identity is never a new grant.
    changed = copy.deepcopy(spec)
    changed["nodes"][0]["responsibility"] = "另一个工作"
    other = prepare_orchestration_scope(runtime, task="目标", run_id="b" * 32, checkpoint={"locked_flow": changed})
    assert other["task_id"] != design["task_id"]


def test_paused_native_run_keeps_bound_design_identity_after_graph_edits(tmp_path):
    runtime, checker = runtime_for(tmp_path)
    runtime.work_dir = str(tmp_path)
    service = TaskDesign(tmp_path, runtime.conversation_id)
    design = service.start('execute')
    rid = 'c' * 32
    spec = flow().as_dict()
    spec['meta']['task_design']['task_id'] = design['task_id']
    ready = service.transition(design, 'ready', execution_flow_hash='older-revision', run_id=rid)
    service.transition(ready, 'paused')
    checkpoint = dict(run_id=rid, locked_flow=spec, workflow_core={'graph': {'revision': 3}})
    receipt = prepare_orchestration_scope(runtime, task='Continue', run_id=rid, checkpoint=checkpoint)
    assert receipt['task_id'] == checker.task_id == design['task_id']
    assert checker.task_authorization is None
    unrelated = prepare_orchestration_scope(runtime, task='Other run', run_id='d' * 32, checkpoint=checkpoint)
    assert unrelated['task_id'] != design['task_id']


@pytest.mark.asyncio
async def test_actual_confirmation_binds_design_to_the_dispatched_flow(confirm_env, monkeypatch):
    import httpx
    from frontend import server

    runtime = confirm_env.runtime
    monkeypatch.setattr(runtime, "work_dir", server.SESSION.work_dir, raising=False)
    service = TaskDesign(runtime.work_dir, runtime.conversation_id)
    design = service.start("execute")
    design = service.transition(design, "ready")
    spec = _bound_spec("按同一任务执行").as_dict()
    spec.setdefault("meta", {})["task_design"] = {"task_id": design["task_id"], "intent": "execute"}
    captured = []

    async def start(_runtime, _text, *, confirmed_new_flow):
        captured.append(confirmed_new_flow)
        assert service.read()["execution_flow_hash"] == digest(confirmed_new_flow.as_dict())

    monkeypatch.setattr(server, "start_runtime_turn", start)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        response = await client.post("/api/orchestration/confirm", json={
            "spec": spec, "activate": True, "conversation_id": runtime.conversation_id})
    assert response.status_code == 200, response.text
    assert response.json()["started"] is True
    assert len(captured) == 1
    assert captured[0].meta["task_design"]["task_id"] == design["task_id"]
    assert service.read()["revision"] == design["revision"]


def test_question_is_invalid_after_revision_or_material_change(tmp_path):
    service = TaskDesign(tmp_path, "paper")
    current = service.start("plan")
    binding = question_binding(service.store.get())
    validate_question_binding(tmp_path, "paper", binding)
    service.control("pause", task_id=current["task_id"], revision=current["revision"])
    with pytest.raises(ValueError, match="旧答案"):
        validate_question_binding(tmp_path, "paper", binding)


@pytest.mark.parametrize("design_status,run_status,expected", [
    ("running", "completed", "completed"), ("paused", "paused", "paused")])
def test_completion_callback_uses_retained_run_identity_after_runner_cleanup(tmp_path, monkeypatch,
        design_status, run_status, expected):
    from frontend import server
    service = TaskDesign(tmp_path, "paper")
    design = service.start("execute")
    service.transition(design, design_status)
    monkeypatch.setattr(server.EXPERT_RUNS, "load", lambda run_id: {
        "run_id": run_id, "status": run_status, "conversation_id": "paper",
        "locked_flow": {"meta": {"task_design": {"task_id": design["task_id"]}}}})
    server.task_design_after_turn(SimpleNamespace(work_dir=str(tmp_path), conversation_id="paper",
        execution_kind="orchestration", expert_run_id="", last_expert_run_id="finished-run"))
    assert service.read()["status"] == expected
    assert service.read()["run_id"] == "finished-run"
