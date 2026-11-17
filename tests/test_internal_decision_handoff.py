"""Offline regressions for AI ownership, real answer delivery and stale reports."""
import asyncio
import copy
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from frontend import interaction_choices as choices_api, server
from mewcode.experts.case_runtime import CaseAnswered, CaseCoordinator, answer_case
from mewcode.experts.handling_state import handling_for_snapshot
from mewcode.experts.node_ledger import ledger_for_run
from mewcode.experts.recovery_case import apply_event, decision_version, get_case, open_case, reconcile_wait_state, save_case
from mewcode.experts.waits import BranchWaitPaused
from tests.test_recovery_checkpoint_choices import checkpoint, close_body, endpoint
from tests.test_recovery_case_e2e import RUN, harness, option, run


def answered_option(store, run_id, case_id):
    def mutate(record):
        case = get_case(record, case_id)
        case['plan'] = {'options': [option(1)], 'recommended_option_id': 'method-1'}
        case['pending']['options'].append({'value': 'method-1', 'label': '处理现有技术方案'})
        save_case(record, case)
    store.mutate(run_id, mutate)


def test_reconcile_selected_legacy_open_pending_preserves_choice_and_scope(tmp_path):
    _, store, run_id, case_id = checkpoint(tmp_path)
    answered_option(store, run_id, case_id)
    def corrupt(record):
        pending = copy.deepcopy(get_case(record, case_id)['pending'])
        c = answer_case(record, case_id, round=0, choice='method-1')
        c['pending'] = pending
        save_case(record, c)
    store.mutate(run_id, corrupt)
    before = store.load(run_id)
    assert handling_for_snapshot({**before, 'stage_cursor': 'work'})['handling_state'] == 'blocked'
    store.mutate(run_id, lambda record: reconcile_wait_state(record, case_id))
    after = store.load(run_id)
    c = get_case(after, case_id)
    assert c['status'] == 'open' and not c['pending']
    assert c['selected_recovery_option']['option_id'] == 'method-1'
    assert c['retired_questions'] and after['node_states']['work']['status'] == 'pending'
    assert after.get('authorizations') == before.get('authorizations')


@pytest.mark.asyncio
async def test_late_report_never_reopens_consumed_answer(tmp_path):
    _, store, run_id, case_id = checkpoint(tmp_path)
    answered_option(store, run_id, case_id)
    stale = get_case(store.load(run_id), case_id)
    store.mutate(run_id, lambda r: answer_case(r, case_id, round=0, choice='method-1'))
    before = store.load(run_id)
    coordinator = CaseCoordinator(store=store, run_id=run_id, emit=AsyncMock())
    with pytest.raises(CaseAnswered):
        await coordinator.pause(stale, report={'reason': 'same report', 'options': [{'value': 'method-1'}]})
    assert store.load(run_id) == before


@pytest.mark.asyncio
async def test_duplicate_api_choice_has_one_real_resume_dispatch(monkeypatch, tmp_path):
    _, store, run_id, case_id = checkpoint(tmp_path)
    answered_option(store, run_id, case_id)
    runtime = endpoint(monkeypatch, tmp_path, store)
    runtime.recovery_resume_dispatcher = AsyncMock()
    body = close_body(tmp_path, run_id, case_id).model_copy(update={'choice': 'method-1'})
    first, second = await asyncio.gather(server.api_recovery_case_answer(body), server.api_recovery_case_answer(body))
    assert first.status_code == second.status_code == 200
    await asyncio.gather(*runtime._case_resume_tasks.values())
    assert runtime.recovery_resume_dispatcher.await_count == 1
    deliveries = store.load(run_id)['recovery_resume_deliveries']
    assert len(deliveries) == 1 and next(iter(deliveries.values()))['status'] == 'dispatched'
    assert not store.load(run_id).get('authorizations', {}).get('entries')


@pytest.mark.asyncio
async def test_cancel_before_queued_resume_never_starts(monkeypatch, tmp_path):
    _, store, run_id, case_id = checkpoint(tmp_path)
    answered_option(store, run_id, case_id)
    runtime = endpoint(monkeypatch, tmp_path, store)
    release = asyncio.Event()
    runtime.task = asyncio.create_task(release.wait())
    runtime.recovery_resume_dispatcher = AsyncMock()
    body = close_body(tmp_path, run_id, case_id).model_copy(update={'choice': 'method-1'})
    assert (await server.api_recovery_case_answer(body)).status_code == 200
    runtime.cancel_requested = True
    release.set()
    await asyncio.gather(*runtime._case_resume_tasks.values())
    assert not runtime.recovery_resume_dispatcher.called
    assert next(iter(store.load(run_id)['recovery_resume_deliveries'].values()))['status'] == 'retired'


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', ['epoch', 'input_revision', 'terminal'])
async def test_queued_answer_rechecks_binding_after_previous_turn(monkeypatch, tmp_path, changed):
    _, store, run_id, case_id = checkpoint(tmp_path)
    answered_option(store, run_id, case_id)
    runtime = endpoint(monkeypatch, tmp_path, store)
    release = asyncio.Event()
    runtime.task = asyncio.create_task(release.wait())
    runtime.recovery_resume_dispatcher = AsyncMock()
    body = close_body(tmp_path, run_id, case_id).model_copy(update={'choice': 'method-1'})
    assert (await server.api_recovery_case_answer(body)).status_code == 200
    def invalidate(record):
        if changed == 'epoch':
            ledger_for_run(record).record('work', 'epoch_change', {}, epoch=1, identity='new-generation')
        elif changed == 'input_revision':
            record['node_states']['work']['input_revision'] = 'new-input'
        else:
            record['node_states']['work']['status'] = 'completed'
    store.mutate(run_id, invalidate)
    before_dispatch = copy.deepcopy(store.load(run_id)['node_states']['work'])
    release.set()
    await asyncio.gather(*runtime._case_resume_tasks.values())
    assert not runtime.recovery_resume_dispatcher.called
    assert next(iter(store.load(run_id)['recovery_resume_deliveries'].values()))['status'] == 'retired'
    assert store.load(run_id)['node_states']['work'] == before_dispatch


@pytest.mark.asyncio
@pytest.mark.parametrize('finished', ['resolved', 'superseded'])
async def test_late_report_cannot_replace_finished_case(tmp_path, finished):
    _, store, run_id, case_id = checkpoint(tmp_path)
    stale = get_case(store.load(run_id), case_id)
    def finish(record):
        case = get_case(record, case_id)
        case.update(status=finished, pending={})
        save_case(record, case)
        record['node_states']['work']['status'] = 'running'
        if finished == 'superseded':
            open_case(record, 'work', identity='new-obstacle')
    store.mutate(run_id, finish)
    before = store.load(run_id)
    coordinator = CaseCoordinator(store=store, run_id=run_id, emit=AsyncMock())
    with pytest.raises(BranchWaitPaused):
        await coordinator.report(stale, '迟到的业务报告', root='business_decision')
    assert store.load(run_id) == before


@pytest.mark.asyncio
async def test_actual_business_report_stays_answerable(tmp_path):
    _, store, run_id, case_id = checkpoint(tmp_path)
    coordinator = CaseCoordinator(store=store, run_id=run_id, emit=AsyncMock())
    with pytest.raises(BranchWaitPaused):
        await coordinator.report(get_case(store.load(run_id), case_id),
            '两种处理范围有实际费用差异，需要你决定。', root='business_decision')
    case = get_case(store.load(run_id), case_id)
    assert case['handling']['owner'] == 'user' and case['pending']
    assert store.load(run_id)['node_states']['work']['status'] == 'paused'


@pytest.mark.asyncio
async def test_system_blocked_can_stop_without_manufacturing_user_question(monkeypatch, tmp_path):
    _, store, run_id, case_id = checkpoint(tmp_path)
    coordinator = CaseCoordinator(store=store, run_id=run_id, emit=AsyncMock())
    with pytest.raises(BranchWaitPaused):
        await coordinator.report(get_case(store.load(run_id), case_id),
            '当前 OCR 运行器未就绪，无法执行原失败步骤。', root='environment_fixable')
    assert not get_case(store.load(run_id), case_id)['pending']
    runtime = endpoint(monkeypatch, tmp_path, store)
    runtime.recovery_resume_dispatcher = AsyncMock()
    response = await server.api_recovery_case_answer(close_body(tmp_path, run_id, case_id))
    assert response.status_code == 200, response.body
    assert get_case(store.load(run_id), case_id)['closed_by_user']
    assert not runtime.recovery_resume_dispatcher.called
    assert not store.load(run_id).get('authorizations', {}).get('entries')


@pytest.mark.asyncio
async def test_lost_websocket_notification_does_not_lose_answer_delivery(monkeypatch, tmp_path):
    _, store, run_id, case_id = checkpoint(tmp_path)
    answered_option(store, run_id, case_id)
    runtime = endpoint(monkeypatch, tmp_path, store)
    runtime.recovery_resume_dispatcher = AsyncMock()
    server.WS_EVENTS.send_json.side_effect = ConnectionError('disconnected')
    body = close_body(tmp_path, run_id, case_id).model_copy(update={'choice': 'method-1'})
    with pytest.raises(ConnectionError):
        await server.api_recovery_case_answer(body)
    await asyncio.gather(*runtime._case_resume_tasks.values())
    assert runtime.recovery_resume_dispatcher.await_count == 1
    server.WS_EVENTS.send_json.side_effect = None
    assert (await server.api_recovery_case_answer(body)).status_code == 200
    assert runtime.recovery_resume_dispatcher.await_count == 1


@pytest.mark.asyncio
async def test_durable_pending_delivery_can_be_recovered_by_repeat_answer(monkeypatch, tmp_path):
    _, store, run_id, case_id = checkpoint(tmp_path)
    answered_option(store, run_id, case_id)
    body = close_body(tmp_path, run_id, case_id).model_copy(update={'choice': 'method-1'})
    def saved_but_not_dispatched(record):
        case = answer_case(record, case_id, round=0, choice='method-1')
        from mewcode.experts.recovery_case import digest
        import hashlib
        decision = decision_version(case)
        identity = hashlib.sha256((run_id + case_id + decision).encode()).hexdigest()
        case['answer_receipts'] = {digest(body.model_dump()): {'resume_delivery_id': identity}}
        record['recovery_resume_deliveries'] = {identity: {
            'case_id': case_id, 'decision_version': decision, 'status': 'pending'}}
        save_case(record, case)
    store.mutate(run_id, saved_but_not_dispatched)
    runtime = endpoint(monkeypatch, tmp_path, store)
    runtime.recovery_resume_dispatcher = AsyncMock()
    assert (await server.api_recovery_case_answer(body)).status_code == 200
    await asyncio.gather(*runtime._case_resume_tasks.values())
    assert runtime.recovery_resume_dispatcher.await_count == 1


@pytest.mark.asyncio
async def test_idle_conversation_rehydrates_runtime_before_answer_delivery(monkeypatch, tmp_path):
    _, store, run_id, case_id = checkpoint(tmp_path)
    answered_option(store, run_id, case_id)
    runtime = endpoint(monkeypatch, tmp_path, store, busy=False)
    runtime.recovery_resume_dispatcher = AsyncMock()
    server.SESSION.runtime_for = lambda _: None
    server.SESSION.session = SimpleNamespace(session_id='choices-chat')
    captured = []
    server.SESSION.capture_conversation_runtime = lambda: (captured.append(runtime), runtime)[1]
    body = close_body(tmp_path, run_id, case_id).model_copy(update={'choice': 'method-1'})
    response = await server.api_recovery_case_answer(body)
    assert response.status_code == 200, response.body
    await asyncio.gather(*runtime._case_resume_tasks.values())
    assert len(captured) == 1 and runtime.recovery_resume_dispatcher.await_count == 1


@pytest.mark.asyncio
async def test_failed_delivery_keeps_answer_and_reports_concrete_system_fault(monkeypatch, tmp_path):
    _, store, run_id, case_id = checkpoint(tmp_path)
    answered_option(store, run_id, case_id)
    runtime = endpoint(monkeypatch, tmp_path, store)
    runtime.recovery_resume_dispatcher = AsyncMock(side_effect=ValueError('原生执行环境正在维护'))
    body = close_body(tmp_path, run_id, case_id).model_copy(update={'choice': 'method-1'})
    assert (await server.api_recovery_case_answer(body)).status_code == 200
    await asyncio.gather(*runtime._case_resume_tasks.values())
    case = get_case(store.load(run_id), case_id)
    assert not case['pending'] and case['selected_recovery_option']['option_id'] == 'method-1'
    assert case['handling']['owner'] == 'system' and case['handling']['handling_state'] == 'blocked'
    assert '原生执行环境正在维护' in case['handling']['reason']
    assert (await server.api_recovery_case_answer(body)).status_code == 200
    assert runtime.recovery_resume_dispatcher.await_count == 1


def lifecycle_owner(runtime):
    owner = SimpleNamespace(session=SimpleNamespace(session_id=runtime.conversation_id),
                            chat_task=None, conversation_runs={runtime.conversation_id: runtime})
    runtime.session = owner.session
    runtime.owner = owner
    runtime.queued_prompts = []
    runtime.starting = runtime.cancel_requested = False
    runtime.running = MethodType(server.ConversationRuntime.running, runtime)
    for name in ('runtime_for', 'chat_busy', 'attach_conversation_task', '_continue_conversation_queue'):
        setattr(owner, name, MethodType(getattr(server.WebSession, name), owner))
    return owner


@pytest.mark.asyncio
async def test_pending_delivery_keeps_runtime_registered_after_real_old_turn_cleanup(monkeypatch, tmp_path):
    _, store, run_id, case_id = checkpoint(tmp_path)
    answered_option(store, run_id, case_id)
    runtime = endpoint(monkeypatch, tmp_path, store)
    owner = lifecycle_owner(runtime)
    monkeypatch.setattr(server, 'SESSION', owner)
    prior_release, resumed_release = asyncio.Event(), asyncio.Event()
    previous = asyncio.create_task(prior_release.wait())
    owner.attach_conversation_task(runtime, previous)
    resumed = []
    async def starter(**kwargs):
        assert owner.conversation_runs[runtime.conversation_id] is runtime
        resumed.append(asyncio.create_task(resumed_release.wait()))
        owner.attach_conversation_task(runtime, resumed[0])
        assert owner.runtime_for(runtime.conversation_id) is runtime
    runtime.recovery_resume_dispatcher = starter
    body = close_body(tmp_path, run_id, case_id).model_copy(update={'choice': 'method-1'})
    assert (await server.api_recovery_case_answer(body)).status_code == 200
    await asyncio.sleep(0)
    prior_release.set()
    await asyncio.gather(*runtime._case_resume_tasks.values())
    assert owner.chat_busy(runtime.conversation_id) and owner.runtime_for(runtime.conversation_id) is runtime
    resumed_release.set()
    await resumed[0]
    await asyncio.sleep(0)
    assert not owner.chat_busy(runtime.conversation_id)


@pytest.mark.asyncio
async def test_late_cleanup_does_not_clear_a_newly_attached_turn():
    runtime = SimpleNamespace(conversation_id='choices-chat', task=None)
    owner = lifecycle_owner(runtime)
    old_release, new_release = asyncio.Event(), asyncio.Event()
    previous = asyncio.create_task(old_release.wait())
    following = asyncio.create_task(new_release.wait())
    owner.attach_conversation_task(runtime, previous)
    owner.attach_conversation_task(runtime, following)
    old_release.set()
    await previous
    await asyncio.sleep(0)
    assert runtime.task is following and owner.chat_task is following
    assert owner.runtime_for(runtime.conversation_id) is runtime
    new_release.set()
    await following


@pytest.mark.asyncio
async def test_real_interrupt_can_cancel_answer_before_delivery_starts(monkeypatch, tmp_path):
    _, store, run_id, case_id = checkpoint(tmp_path)
    answered_option(store, run_id, case_id)
    runtime = endpoint(monkeypatch, tmp_path, store)
    runtime.task = None
    owner = lifecycle_owner(runtime)
    monkeypatch.setattr(server, 'SESSION', owner)
    monkeypatch.setattr(server, 'hold_continuation', lambda *args, **kwargs: None)
    runtime.recovery_resume_dispatcher = AsyncMock()
    body = close_body(tmp_path, run_id, case_id).model_copy(update={'choice': 'method-1'})
    assert (await server.api_recovery_case_answer(body)).status_code == 200
    assert owner.chat_busy(runtime.conversation_id)
    result = server.WebSession.interrupt_conversation(owner, runtime.conversation_id)
    assert result['cancelled'] and result['status'] == 'cancelled_before_start'
    await asyncio.gather(*runtime._case_resume_tasks.values())
    assert not runtime.recovery_resume_dispatcher.called
    assert get_case(store.load(run_id), case_id)['handling']['handling_state'] == 'blocked'
    assert store.load(run_id)['status'] == 'paused'


@pytest.mark.asyncio
async def test_answer_delivery_never_replaces_newer_conversation_runtime(monkeypatch, tmp_path):
    _, store, run_id, case_id = checkpoint(tmp_path)
    answered_option(store, run_id, case_id)
    runtime = endpoint(monkeypatch, tmp_path, store)
    server.SESSION.conversation_runs = {runtime.conversation_id: runtime}
    release = asyncio.Event()
    runtime.task = asyncio.create_task(release.wait())
    runtime.recovery_resume_dispatcher = AsyncMock()
    body = close_body(tmp_path, run_id, case_id).model_copy(update={'choice': 'method-1'})
    assert (await server.api_recovery_case_answer(body)).status_code == 200
    newer = SimpleNamespace(conversation_id=runtime.conversation_id)
    server.SESSION.conversation_runs[runtime.conversation_id] = newer
    release.set()
    await asyncio.gather(*runtime._case_resume_tasks.values())
    assert server.SESSION.conversation_runs[runtime.conversation_id] is newer
    assert not runtime.recovery_resume_dispatcher.called
    assert next(iter(store.load(run_id)['recovery_resume_deliveries'].values()))['status'] == 'retired'


@pytest.mark.asyncio
@pytest.mark.parametrize('selector_result', [{'status': 'selected', 'choice': 'method-2', 'confidence': 1},
                                            {'status': 'rethink', 'confidence': .1}])
async def test_internal_selector_and_primary_fallback_never_ask_user(tmp_path, selector_result):
    h = harness(tmp_path, make_options=lambda n: [option(1), option(2)])
    async def select(candidates, context):
        assert len(candidates) == 2
        assert set(context) == {'issue'}
        assert not any(candidate['description'] for candidate in candidates)
        assert all(set(candidate['facts']) == {'kind', 'reversible', 'recommended'} for candidate in candidates)
        return selector_result
    h.coordinator.decision_selector = select
    assert (await run(h))['action'] == 'continue_checkpoint'
    case = get_case(h.store.load(RUN), h.events[-1]['case']['case_id'])
    expected = 'method-2' if selector_result['status'] == 'selected' else 'method-1'
    assert case['rounds'][0]['option_id'] == expected
    assert not any(event.get('case', {}).get('status') == 'awaiting_user' for event in h.events)


@pytest.mark.asyncio
async def test_gate_recovery_hands_back_to_normal_confirmation_without_probe(tmp_path):
    h = harness(tmp_path)
    def seed(record):
        from mewcode.experts.recovery_case import open_case
        record['locked_flow'].setdefault('meta', {}).setdefault('node_kinds', {})['work'] = 'gate'
        c = open_case(record, 'work')
        c.update(status='open', pending={'kind': 'incident', 'options': [{'value': 'repair'}]})
        save_case(record, c)
    h.store.mutate(RUN, seed)
    result = await run(h)
    assert result['source'] == 'normal_gate_handoff'
    assert not h.calls
    record = h.store.load(RUN)
    assert record['node_states']['work']['status'] == 'pending'
    assert not record.get('gate_confirmations') and not record.get('authorizations')


@pytest.mark.parametrize('answered', [False, True])
def test_saved_gate_deadlock_resumes_into_normal_gate_without_approval(tmp_path, answered):
    h = harness(tmp_path)
    def seed(record):
        record['locked_flow'].setdefault('meta', {}).setdefault('node_kinds', {})['work'] = 'gate'
        c = open_case(record, 'work')
        c = apply_event(record, c['case_id'], {'kind': 'wait', 'identity': 'report-v1',
            'pending': {'kind': 'incident', 'options': [{'value': 'method-1', 'label': '返回原门禁'}]}})
        if answered:
            c['plan'] = {'options': [option(1)], 'recommended_option_id': 'method-1'}
            save_case(record, c)
            pending = copy.deepcopy(c['pending'])
            c = answer_case(record, c['case_id'], round=0, choice='method-1')
            c['pending'] = pending  # Exact historical open+pending corruption.
            save_case(record, c)
        record['node_states']['work'].update(status='paused', pending_recovery_case={'case_id': c['case_id']})
        record.update(status='running', active_node_ids=[])
    h.store.mutate(RUN, seed)
    before = h.store.load(RUN)
    result = h.store.resume(RUN, strategy='stable')
    case = get_case(result, before['node_states']['work']['recovery_case']['case_id'])
    assert case['status'] == 'superseded' and not case['pending']
    assert len(case['rounds']) == len(before['node_states']['work']['recovery_case']['rounds'])
    assert result['node_states']['work']['status'] != 'completed'
    assert not result.get('gate_confirmations') and not result.get('authorizations')
    assert not h.calls


def test_technical_parameter_binding_does_not_choose_authority_or_invalid_values(tmp_path):
    questions = [{'name': name} for name in ['batch_size', 'book_scopes', 'allow_external', 'temp_dir', 'max_retries']]
    bundle = {'questions': [{'name': name, 'choices': [{'id': name, 'kind': 'value', 'value': value,
                                                       'recommended': True}]} for name, value in
               [('batch_size', '4'), ('book_scopes', 'all'), ('allow_external', 'yes'), ('temp_dir', '../private'), ('max_retries', 'nan')]]}
    result = choices_api._route_prepared_parameters(bundle, questions, tmp_path)
    assert result['system_answers'] == {'batch_size': '4'}
    assert result['handling_by_question']['book_scopes']['owner'] == 'user'
    assert result['handling_by_question']['allow_external']['owner'] == 'user'
    assert result['handling_by_question']['temp_dir']['handling_state'] == 'blocked'


def test_waiting_stage_is_not_projected_as_running():
    snapshot = {'status': 'running', 'stage_cursor': 'gate', 'active_node_ids': [],
                'node_states': {'gate': {'status': 'paused', 'pending_interaction': {'kind': 'human_oncall'}}}}
    assert handling_for_snapshot(snapshot)['handling_state'] == 'needs_user'
    snapshot['node_states']['gate'] = {'status': 'running'}
    snapshot['active_node_ids'] = ['gate']
    assert handling_for_snapshot(snapshot)['handling_state'] == 'working'


@pytest.mark.asyncio
@pytest.mark.parametrize('context_changed', [False, True])
async def test_stage_binds_model_prepared_internal_value_only_to_current_context(tmp_path, context_changed):
    from mewcode.experts.flow import ExpertFlowSpec
    from mewcode.experts.stage_run_inputs import collect_stage_run_inputs
    h = harness(tmp_path)
    raw = h.store.load(RUN)['locked_flow']
    raw.setdefault('meta', {})['run_input_contract'] = [
        {'name': 'batch_size', 'type': 'text', 'required': True, 'collect_at': 'work'}]
    flow = ExpertFlowSpec.from_value(raw)
    async def prepare(**kwargs):
        if context_changed:
            h.store.mutate(RUN, lambda r: r.setdefault('run_inputs', {}).update(batch_size='6'))
        return {'id': 'technical', 'version': 'v1', 'system_answers': {'batch_size': '4'}}
    session = SimpleNamespace(stage_parameter_preparer=prepare)
    if context_changed:
        with pytest.raises(BranchWaitPaused) as stopped:
            await collect_stage_run_inputs(node=flow.node_map['work'], flow=flow, session=session,
                                           store=h.store, run_id=RUN, emit=AsyncMock())
        assert stopped.value.error_kind == 'system_parameter_stale'
        assert h.store.load(RUN)['run_inputs']['batch_size'] == '6'
        assert not h.store.load(RUN).get('system_input_choices')
    else:
        emit = AsyncMock()
        result = await collect_stage_run_inputs(node=flow.node_map['work'], flow=flow, session=session,
                                               store=h.store, run_id=RUN, emit=emit)
        assert result['batch_size'] == '4'
        assert not any(call.args[0].get('type') == 'ask_user' for call in emit.call_args_list)
        assert h.store.load(RUN)['system_input_choices']['work'][0]['source'] == 'primary_model_not_authorization'
        assert not h.store.load(RUN).get('authorizations')


@pytest.mark.asyncio
@pytest.mark.parametrize('condition', ['branch', 'required_when'])
async def test_technical_named_business_branch_is_not_auto_selected(tmp_path, condition):
    from tests.test_stage_run_inputs import input_flow
    from mewcode.expert_runs import ExpertRunStore
    from mewcode.experts.flow import ExpertFlowSpec
    from mewcode.experts.stage_run_inputs import collect_stage_run_inputs
    raw = input_flow().as_dict()
    inputs = raw['meta']['run_input_contract']
    if condition == 'branch':
        inputs[0].update(name='batch_size', default=None)
        inputs[1]['required_when']['input'] = 'batch_size'
        for edge in raw['edges']:
            if edge.get('input_when'):
                edge['input_when']['input'] = 'batch_size'
        node_id, answer = 'database', 'false'
    else:
        inputs[1]['name'] = 'batch_size'
        node_id, answer = 'vector', '4'
    flow = ExpertFlowSpec.from_value(raw)
    store = ExpertRunStore(tmp_path / 'runs')
    store.create(run_id=RUN, conversation_id='conv', task='条件合同', attachments=[], profile={}, strategy='stable', flow=flow)
    if condition == 'required_when':
        store.mutate(RUN, lambda r: r.setdefault('run_inputs', {}).update(vector_enabled='true'))
    prepare = AsyncMock(side_effect=AssertionError('不得自动选择条件分支'))
    session = SimpleNamespace(stage_parameter_preparer=prepare)
    asked = []
    async def emit(event):
        if event.get('type') == 'ask_user':
            asked.extend(question['name'] for question in event['questions'])
            session._ask_user_request.future.set_result({'batch_size': answer})
    result = await collect_stage_run_inputs(node=flow.node_map[node_id], flow=flow, session=session,
                                           store=store, run_id=RUN, emit=emit)
    assert asked == ['batch_size'] and result['batch_size'] == answer
    assert not prepare.called and not store.load(RUN).get('authorizations')
