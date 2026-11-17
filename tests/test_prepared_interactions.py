"""Exercise actual wait/input submission boundaries without provider calls."""
import copy
import json
from types import SimpleNamespace

import pytest

from mewcode.expert_runs import ExpertRunStore
from mewcode.experts.flow import ExpertFlowSpec
from mewcode.experts.stage_run_inputs import collect_stage_run_inputs
from mewcode.experts.waits import BranchWaitPaused
from mewcode.orchestration.interaction_choices import prepare_choices


def current(tmp_path, declarations):
    flow = ExpertFlowSpec.from_value({"flow_id": "choices", "label": "材料选择",
        "nodes": [{"node_id": "work", "role": "planner", "label": "选择材料"}],
        "edges": [], "finalizer_node_id": "work",
        "meta": {"run_input_contract": declarations}})
    store = ExpertRunStore(tmp_path / "runs")
    run_id = "c" * 32
    store.create(run_id=run_id, conversation_id="choices-chat", task="材料选择",
                 attachments=[], profile={}, strategy="stable", flow=flow)
    return flow, store, run_id, SimpleNamespace()


def bundle(name, value, *, action=None, version="v1"):
    choice = {"id": "candidate", "kind": "value", "value": value,
              "label": "使用已核验的材料", "description": "选中后交给原节点",
              "recommended": True, "verification": "verified", "evidence": []}
    if action:
        choice = {"id": "candidate", "kind": "action", "action": action,
                  "label": action, "requires_input": False}
    return {"id": "bundle", "version": version, "status": "ready",
            "questions": [{"name": name, "choices": [choice,
                {"id": "defer", "kind": "action", "action": "defer", "requires_input": False}]}]}


@pytest.mark.asyncio
async def test_real_wait_resolves_four_discovered_pdfs_to_typed_stage_values(tmp_path):
    paths = []
    for index in range(4):
        path = tmp_path / f"book-{index}.pdf"
        path.write_bytes(b"%PDF-1.4\n")
        paths.append(str(path))
    flow, store, run_id, session = current(tmp_path, [
        {"name": "sources", "type": "list", "required": True, "collect_at": "work"}])
    calls = []
    async def complete(system, text):
        calls.append(json.loads(text))
        if len(calls) == 1:
            return json.dumps({"searches": [{"root": str(tmp_path), "pattern": "*.pdf"}], "questions": []})
        assert len(calls[-1]["discovered_files"]) == 4
        return json.dumps({"questions": [{"name": "sources", "choices": [{
            "id": "pdfs", "kind": "value", "value": paths,
            "label": "使用这四份 PDF", "description": "已核验的文件清单",
            "recommended": True, "verification": "verified"}]}]})
    async def prepare(**kwargs):
        return await prepare_choices(identity={"run_id": run_id},
            questions=kwargs["request"].source_questions, context={}, complete=complete,
            allowed_roots=[str(tmp_path)], cached=kwargs.get("cached"))
    session.interaction_choice_preparer = prepare
    async def emit(event):
        if event.get("type") == "ask_user":
            displayed = event["choice_bundle"]["questions"][0]["choices"]
            assert displayed[0]["verification"] == "verified"
            session._ask_user_request.future.set_result({"sources": displayed[0]["id"]})
    values = await collect_stage_run_inputs(node=flow.node_map["work"], flow=flow,
        session=session, store=store, run_id=run_id, emit=emit)
    assert values["sources"] == paths
    snapshot = store.load(run_id)
    assert snapshot["run_inputs"]["sources"] == paths
    assert not snapshot.get("gate_confirmations")
    assert snapshot["node_states"]["work"].get("status") != "completed"


@pytest.mark.asyncio
async def test_defer_preserves_current_prompt_without_binding_or_completing(tmp_path):
    flow, store, run_id, session = current(tmp_path, [
        {"name": "sources", "type": "list", "required": True, "collect_at": "work"}])
    async def prepare(**kwargs):
        return bundle("sources", None, action="defer")
    session.interaction_choice_preparer = prepare
    async def emit(event):
        if event.get("type") == "ask_user":
            session._ask_user_request.future.set_result({"sources": "candidate"})
    with pytest.raises(BranchWaitPaused):
        await collect_stage_run_inputs(node=flow.node_map["work"], flow=flow,
            session=session, store=store, run_id=run_id, emit=emit)
    stage = store.load(run_id)["node_states"]["work"]
    assert stage["pending_interaction"]["choice_bundle"]["version"] == "v1"
    assert not store.load(run_id).get("run_inputs", {}).get("sources")
    assert not stage.get("closed_by_user") and stage["status"] != "completed"


@pytest.mark.asyncio
async def test_explicit_research_reprepares_instead_of_suppressing_changed_bundle(tmp_path):
    flow, store, run_id, session = current(tmp_path, [
        {"name": "sources", "type": "list", "required": True, "collect_at": "work"}])
    count = 0
    async def prepare(**kwargs):
        nonlocal count
        count += 1
        if count == 1:
            return bundle("sources", None, action="research")
        assert kwargs["refresh"] is True
        return bundle("sources", ["found.pdf"], version="v2")
    session.interaction_choice_preparer = prepare
    async def emit(event):
        if event.get("type") == "ask_user":
            session._ask_user_request.future.set_result({"sources": "candidate"})
    values = await collect_stage_run_inputs(node=flow.node_map["work"], flow=flow,
        session=session, store=store, run_id=run_id, emit=emit)
    assert count == 2 and values["sources"] == ["found.pdf"]
    assert not store.load(run_id).get("recovery_cases")


def test_desktop_response_rejects_old_bundle_before_future_transition():
    import asyncio
    from mewcode.agent import HumanOnCallRequest
    from frontend.server import _human_oncall_response_result
    loop = asyncio.new_event_loop()
    try:
        request = HumanOnCallRequest("request", "human_oncall", [{"name": "choice",
            "options": [{"value": "candidate", "label": "方案"}], "allowOther": True}],
            loop.create_future(), choice_bundle=bundle("choice", "continue"))
        runtime = SimpleNamespace(human_oncall_request=request)
        before = copy.deepcopy(request.answer_notes)
        result = _human_oncall_response_result(runtime, {"request_id": "request",
            "conversation_id": "chat", "answers": {"choice": "candidate"},
            "choice_bundle_id": "bundle", "choice_version": "old", "notes": {"choice": "备注"}})
        assert result["ok"] is False and not request.future.done()
        assert request.answer_notes == before
        request.choice_resolution = "wait"
        result = _human_oncall_response_result(runtime, {"request_id": "request",
            "conversation_id": "chat", "answers": {"choice": "candidate"},
            "choice_bundle_id": "bundle", "choice_version": "v1"})
        assert result["ok"] is True and request.future.result() == {"choice": "candidate"}
    finally:
        loop.close()


@pytest.mark.asyncio
async def test_selected_fundamental_recovery_option_reaches_actual_executor(tmp_path):
    from unittest.mock import AsyncMock
    from mewcode.expert_group import ExpertExecutionPolicy, ExpertRoleResult
    from mewcode.experts.case_runtime import CaseCoordinator, answer_case
    from mewcode.experts.problem_dossier import incident_report
    from mewcode.experts.recovery_case import apply_event, open_case, save_case
    from tests.test_recovery_prepared_choices import _option, _plan
    flow, store, run_id, _session = current(tmp_path, [])
    chosen = {}
    def seed(record):
        case = open_case(record, 'work')
        case['plan'] = _plan(case, _option('first'), _option('second'))
        save_case(record, case)
        report = incident_report(record, case, conclusion='请选择修复方案', root_cause='contract_defect')
        case = apply_event(record, case['case_id'], {'kind': 'report', 'identity': 'report', 'report': report})
        answer_case(record, case['case_id'], round=0, choice='second')
    store.mutate(run_id, seed)
    class Executed(Exception):
        pass
    async def execute(request, context):
        chosen['prompt'] = request.need
        raise Executed()
    executor = SimpleNamespace(model={}, dispatch=AsyncMock(), before_resume=AsyncMock(), run=execute)
    async def emit(_event):
        pass
    coordinator = CaseCoordinator(store=store, run_id=run_id, emit=emit, executor=executor)
    with pytest.raises(Executed):
        await coordinator._run(node=flow.node_map['work'],
            result=ExpertRoleResult(text='原动作尚未恢复', status='needs_attention'),
            policy=ExpertExecutionPolicy('stable', False, 0))
    assert "'option_id': 'second'" in chosen['prompt']
    assert "'option_id': 'first'" not in chosen['prompt']


def test_selected_denied_recovery_does_not_fall_through_to_another_plan(monkeypatch):
    from mewcode.experts import authorization_ledger
    from mewcode.experts.recovery_analyst import select_option
    from tests.test_recovery_prepared_choices import _option
    first, second = _option('first'), _option('second')
    second['changes_acceptance'] = True
    case = {'case_id': 'case', 'rounds': [], 'selected_recovery_option': {
        'case_id': 'case', 'round': 0, 'option_id': 'second'}}
    monkeypatch.setattr(authorization_ledger, 'covers', lambda *a, **k: SimpleNamespace(status='covered'))
    option, _ = select_option({'options': [first, second], 'recommended_option_id': 'second'}, {}, case)
    assert option is None


@pytest.mark.asyncio
@pytest.mark.parametrize('failed', [False, True])
async def test_case_research_instruction_survives_durable_reconnect(monkeypatch, tmp_path, failed):
    from frontend import interaction_choices as api
    from mewcode.experts.recovery_case import apply_event, open_case, save_case
    from tests.test_interaction_choices_api import FakeSrv
    declaration = {'name': 'sources', 'type': 'list', 'required': True, 'collect_at': 'work'}
    _flow, store, run_id, _session = current(tmp_path, [declaration])
    case_id = ''
    def seed(record):
        nonlocal case_id
        case = open_case(record, 'work')
        case_id = case['case_id']
        pending = {'kind': 'input', 'round': 0, 'question': '请选择 PDF',
                   'input_definitions': [declaration], 'questions': [declaration],
                   'options': [{'value': 'input'}, {'value': 'close'}]}
        case = apply_event(record, case_id, {'kind': 'wait', 'identity': 'waiting', 'pending': pending})
        case['pending_choice_action'] = {'action': 'research', 'input': '仅查本项目，不复用旧路径',
                                        'case_id': case_id, 'round': 0}
        save_case(record, case)
    store.mutate(run_id, seed)
    srv = FakeSrv(tmp_path)
    srv.SESSION.conversation_id = 'choices-chat'
    srv.EXPERT_RUNS = store
    calls = []
    async def complete(_client, **kwargs):
        calls.append(kwargs['user_text'])
        if failed:
            raise ValueError('离线模拟：模型生成失败')
        return json.dumps({'searches': [], 'questions': []})
    monkeypatch.setattr(api, 'collect_model_text', complete)
    first = await api.refresh_case_choices(srv, runtime=srv.runtime,
        snapshot=store.load(run_id), case_id=case_id, action='research')
    pending = first['node_states']['work']['recovery_case']['pending']
    assert pending['choice_instruction'] == '仅查本项目，不复用旧路径'
    assert pending['choice_bundle']['status'] == ('failed' if failed else 'ready')
    count = len(calls)
    assert count > 0
    # The reconnect host has no process-local cache. Only the saved case remains.
    reconnected = FakeSrv(tmp_path)
    reconnected.SESSION.conversation_id = 'choices-chat'
    reconnected.EXPERT_RUNS = store
    second = await api.refresh_case_choices(reconnected, runtime=reconnected.runtime,
        snapshot=store.load(run_id), case_id=case_id, action='')
    new_pending = second['node_states']['work']['recovery_case']['pending']
    assert new_pending['choice_bundle']['id'] == pending['choice_bundle']['id']
    assert new_pending['choice_bundle']['version'] == pending['choice_bundle']['version']
    assert len(calls) == count
    assert not second.get('run_inputs', {}).get('sources')
    assert second['node_states']['work']['status'] != 'completed'


@pytest.mark.asyncio
async def test_recovery_http_custom_envelope_requests_revision_without_completing(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock
    from frontend import server
    from mewcode.experts.recovery_case import apply_event, open_case
    _flow, store, run_id, _session = current(tmp_path, [])
    case_id = ''
    def seed(record):
        nonlocal case_id
        case = open_case(record, 'work')
        case_id = case['case_id']
        choices = bundle('recovery_action', 'existing')
        choices['questions'][0]['choices'].append({'id': 'manual', 'kind': 'action',
            'action': 'manual', 'requires_input': True})
        pending = {'kind': 'incident', 'round': 0, 'question': '选择下一步',
                   'choice_bundle': choices, 'options': [{'value': 'candidate'}, {'value': 'close'}]}
        apply_event(record, case_id, {'kind': 'wait', 'identity': 'waiting', 'pending': pending})
    store.mutate(run_id, seed)
    runtime = SimpleNamespace(_case_waiters={}, conversation_id='choices-chat', work_dir=str(tmp_path))
    monkeypatch.setattr(server, 'EXPERT_RUNS', store)
    monkeypatch.setattr(server, 'SESSION', SimpleNamespace(
        runtime_for=lambda cid: runtime, chat_busy=lambda cid: False))
    monkeypatch.setattr(server, '_capability_executor_scope_matches', lambda body: True)
    monkeypatch.setattr(server, 'WS_EVENTS', SimpleNamespace(send_json=AsyncMock()))
    body = server.RecoveryCaseAnswerBody(conversation_id='choices-chat', project=str(tmp_path),
        run_id=run_id, case_id=case_id, round=0,
        choice={'custom': '查找项目内替代材料并修订合同'}, note='保留原检查点',
        choice_bundle_id='bundle', choice_version='v1')
    response = await server.api_recovery_case_answer(body)
    assert response.status_code == 200, response.body
    payload = json.loads(response.body)
    assert payload['resume_required'] is False
    stage = store.load(run_id)['node_states']['work']
    assert stage['recovery_case']['status'] == 'awaiting_user'
    assert stage['recovery_case']['pending']['maintenance']['entry']['body']['mode'] == 'fix'
    assert '查找项目内替代材料并修订合同' in stage['recovery_case']['pending']['maintenance']['entry']['body']['request']
    assert stage['status'] != 'completed'
    assert not store.load(run_id).get('authorizations', {}).get('entries')


@pytest.mark.asyncio
@pytest.mark.parametrize('restricted_input', [False, True])
async def test_open_graph_question_custom_text_crosses_real_draft_choice_boundary(tmp_path, restricted_input):
    from frontend import interaction_choices as api
    from mewcode.orchestration.draft_store import DraftStore
    from tests.test_graph_discussion import prepare
    from tests.test_interaction_choices_api import FakeSrv
    store = DraftStore(tmp_path, 'chat')
    candidate = prepare(store)
    if restricted_input:
        linked = next(item for item in candidate['document']['entries'] if item['id'] == 'labels')
        linked['data'].update(default='actual', options=[
            {'value': 'actual', 'label': '保留实际名称'},
            {'value': 'fixed', 'label': '使用固定名称'},
        ])
        candidate = store.checkpoint(candidate, candidate['document'], candidate['processed_ids'],
                                     'ready', autobuild=candidate['autobuild'])
    questions = api._graph_questions(candidate['document'])
    assert next(q for q in questions if q['name'] == 'meaning')['allow_other'] is True
    async def complete(_system, _text):
        return json.dumps({'searches': [], 'questions': []})
    prepared = await prepare_choices(identity={'candidate_id': candidate['id']},
        questions=questions, context={}, complete=complete, allowed_roots=[str(tmp_path)])
    srv = FakeSrv(tmp_path)
    srv.draft_state = store.get()
    api._cache_graph(srv, 'chat', candidate['id'], candidate['sequence'], prepared,
                     questions, candidate=candidate)
    resolved = api.resolve_graph_selections(srv, {'meaning': {'custom': '名称保持原样，层级固定'}},
        prepared['id'], prepared['version'], conversation_id='chat',
        candidate_id=candidate['id'], sequence=candidate['sequence'])
    store.discuss(None, 'choose', candidate_id=candidate['id'], sequence=candidate['sequence'],
        selections=resolved['selections'], prepared_options=resolved['prepared_options'],
        choice_notes={'meaning': '仅为说明，不是新授权'})
    saved = store.get()['candidate']
    answer = next(item for item in saved['document']['entries'] if item['id'] == 'meaning')
    assert answer['kind'] == 'decision' and answer['body'] == '名称保持原样，层级固定'
    if restricted_input:
        assert next(item for item in saved['document']['entries'] if item['id'] == 'labels') == linked
        assert saved['autobuild']['review_stale'] and saved['autobuild']['phase'] == 'autofix'
    choices = saved['autobuild']['discussion']['messages']
    assert next(item for item in choices if item.get('kind') == 'choice')['notes_role'] == 'context_only'


def test_preparation_context_reads_effective_draft_and_authored_dialogue_only(tmp_path):
    from frontend import interaction_choices as api
    from tests.test_interaction_choices_api import FakeSrv
    srv = FakeSrv(tmp_path)
    srv.runtime.conversation = SimpleNamespace(history=[
        {'role': 'user', 'content': '处理四份 PDF，数据库采用 SQLite'},
        {'role': 'assistant', 'content': '建议逐页分批'},
        {'role': 'tool', 'content': '日志：仍在等待'},
        {'role': 'assistant', 'transient': True, 'content': '正在准备'}])
    srv.draft_state = {'candidate': {'document': {'entries': [
        {'id': 'current', 'kind': 'decision', 'state': 'active', 'body': '采用 SQLite'},
        {'id': 'old', 'kind': 'decision', 'state': 'withdrawn', 'body': '采用 MySQL'}]}}}
    context = api._context_for(srv, srv.runtime, {}, [{'name': 'sources'}])
    evidence = context['dialogue']
    assert [item['id'] for item in evidence['effective_entries']] == ['current']
    assert [item['role'] for item in evidence['authored_chat']] == ['user', 'assistant']
    assert '不是执行授权' in evidence['use']
