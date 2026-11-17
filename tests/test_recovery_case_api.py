import copy
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from mewcode.experts.recovery_case import cases
from mewcode.experts.waits import BranchWaitPaused
from tests.test_recovery_case_e2e import harness, run, option, RUN


@pytest.mark.asyncio
async def test_endpoint_verifies_owner_stale_round_and_schedules_answer_once(tmp_path, monkeypatch):
    from frontend import server
    h = harness(tmp_path, make_options=lambda n: [option(n, authority=True), option(90)])
    with pytest.raises(BranchWaitPaused):
        await run(h)
    case = cases(h.store.load(RUN))[0]
    runtime = SimpleNamespace(_case_waiters={}, conversation_id='conv', recovery_resume_dispatcher=AsyncMock())
    session = SimpleNamespace(session=SimpleNamespace(session_id='conv'), work_dir=str(tmp_path),
        runtime_for=lambda _: runtime, chat_busy=lambda _: False)
    monkeypatch.setattr(server, 'SESSION', session)
    monkeypatch.setattr(server, 'EXPERT_RUNS', h.store)
    monkeypatch.setattr(server, 'WS_EVENTS', SimpleNamespace(send_json=AsyncMock()))
    body = dict(conversation_id='conv', project=str(tmp_path), run_id=RUN, case_id=case['case_id'], round=0, choice='approve')
    before = copy.deepcopy(h.store.load(RUN))
    assert (await server.api_recovery_case_answer(server.RecoveryCaseAnswerBody(**{**body, 'conversation_id': 'foreign'}))).status_code == 409
    assert (await server.api_recovery_case_answer(server.RecoveryCaseAnswerBody(**{**body, 'round': 1}))).status_code == 409
    assert (await server.api_recovery_case_answer(server.RecoveryCaseAnswerBody(**{**body, 'choice': ''}))).status_code == 409
    assert h.store.load(RUN) == before
    calls = len(h.calls)
    assert (await server.api_recovery_case_answer(server.RecoveryCaseAnswerBody(**body))).status_code == 200
    assert len(h.calls) == calls and cases(h.store.load(RUN))[0]['status'] == 'open'
    assert (await server.api_recovery_case_answer(server.RecoveryCaseAnswerBody(**body))).status_code == 200
    await asyncio.gather(*runtime._case_resume_tasks.values())
    assert runtime.recovery_resume_dispatcher.await_count == 1


@pytest.mark.asyncio
async def test_api_keeps_waiter_as_conditional_input_card_expands(tmp_path, monkeypatch):
    from frontend import server
    from mewcode.expert_runs import ExpertRunStore
    from mewcode.experts.problem_dossier import incident_report
    from mewcode.experts.recovery_case import apply_event, cases, open_case
    from tests.test_retry_memory import retry_flow

    flow = retry_flow()
    flow.meta['run_input_contract'] = [
        {'name': 'enable', 'required': True},
        {'name': 'details', 'required': False, 'collect_at': 'work',
         'required_when': {'input': 'enable', 'equals': 'true'}},
    ]
    store = ExpertRunStore(tmp_path / 'runs')
    store.create(run_id=RUN, conversation_id='conv', task='task', attachments=[],
        profile={}, strategy='stable', flow=flow)

    def prepare(record):
        record.update(status='paused', active_node_ids=['work'], run_inputs={})
        record.setdefault('node_states', {}).setdefault('work', {}).update(
            status='paused', input_revision='rev')
        case = open_case(record, 'work')
        report = incident_report(record, case, conclusion='请补充当前字段。', root_cause='input_defect')
        apply_event(record, case['case_id'], {
            'kind': 'report', 'identity': 'conditional-input-report', 'report': report,
        })

    store.mutate(RUN, prepare)
    case = cases(store.load(RUN))[0]
    waiter = asyncio.get_running_loop().create_future()
    runtime = SimpleNamespace(_case_waiters={case['case_id']: waiter})
    session = SimpleNamespace(session=SimpleNamespace(session_id='conv'), work_dir=str(tmp_path),
        runtime_for=lambda _: runtime, chat_busy=lambda _: False)
    monkeypatch.setattr(server, 'SESSION', session)
    monkeypatch.setattr(server, 'EXPERT_RUNS', store)
    monkeypatch.setattr(server, 'WS_EVENTS', SimpleNamespace(send_json=AsyncMock()))
    body = dict(conversation_id='conv', project=str(tmp_path), run_id=RUN,
        case_id=case['case_id'], round=0, choice='input')

    first = await server.api_recovery_case_answer(server.RecoveryCaseAnswerBody(
        **{**body, 'answer': '{"enable":"true"}'}))
    assert first.status_code == 200, first.body
    assert not waiter.done()
    partial = cases(store.load(RUN))[0]
    assert partial['status'] == 'awaiting_user'
    assert [item['name'] for item in partial['pending']['input_definitions']] == ['details']

    second = await server.api_recovery_case_answer(server.RecoveryCaseAnswerBody(
        **{**body, 'answer': '{"details":"ready"}'}))
    assert second.status_code == 200 and waiter.done() and waiter.result() == 'input'
    assert cases(store.load(RUN))[0]['status'] == 'open'


@pytest.mark.asyncio
async def test_passive_mode_always_has_report_without_model_call(tmp_path):
    h = harness(tmp_path)
    h.coordinator.autonomous = False
    with pytest.raises(BranchWaitPaused):
        await run(h)
    assert not h.calls
    case = cases(h.store.load(RUN))[0]
    assert not case['pending'] and case['report']['reason']
    assert case['handling']['owner'] == 'system'


@pytest.mark.parametrize('already_migrated', [False, True])
@pytest.mark.parametrize('conversation_summary', [False, True])
def test_public_restores_legacy_decision_card_before_continue_without_execution(tmp_path, already_migrated, conversation_summary):
    from mewcode.experts.recovery_case import migrate_legacy, save_case
    from mewcode.experts.case_runtime import answer_case
    h = harness(tmp_path)
    def old_wait(record):
        record['status'] = 'paused'
        stage = record['node_states']['work']
        stage.update(status='escalated', assistance_receipts=[
            {'next_step': 'ask_human', 'request_id': str(n), 'question': ''} for n in range(3)])
        if already_migrated:
            c = migrate_legacy(record, 'work')
            c['pending'] = {}  # Previously saved card-less reported case.
            save_case(record, c)
    h.store.mutate(RUN, old_wait)
    before = h.store.load(RUN)
    public = h.store.public_for_conversation('conv')[0] if conversation_summary else h.store.public(RUN)
    case = public['node_states']['work']['recovery_case']
    assert case['status'] == 'reported' and len(case['rounds']) == 3
    assert not case['pending'] and case['handling']['owner'] == 'system'
    after = h.store.load(RUN)
    assert after['node_states']['work']['messages'] == before['node_states']['work']['messages']
    assert after.get('authorizations') == before.get('authorizations') and not h.calls
    unchanged = copy.deepcopy(after)
    assert h.store.public(RUN)['node_states']['work']['recovery_case'] == case
    assert h.store.load(RUN) == unchanged
    # The restored choices use the original atomic answer protocol.
    h.store.mutate(RUN, lambda r: answer_case(r, case['case_id'], round=3, choice='close'))
    stopped = h.store.public(RUN)['node_states']['work']['recovery_case']
    assert stopped['closed_by_user'] and not stopped['pending']
    assert h.store.load(RUN)['node_states']['work']['status'] == 'paused' and not h.calls


def test_public_does_not_turn_completed_legacy_assistance_into_a_new_case(tmp_path):
    h = harness(tmp_path)
    def completed(record):
        record.update(status='paused')
        record['node_states']['work'].update(status='completed',
            assistance_receipts=[{'next_step': 'ask_human', 'question': ''}])
    h.store.mutate(RUN, completed)
    before = copy.deepcopy(h.store.load(RUN))
    assert not h.store.public(RUN)['node_states']['work'].get('recovery_case')
    assert h.store.load(RUN) == before


@pytest.mark.asyncio
async def test_actual_analyst_registry_has_no_write_shell_browser_or_delegation(tmp_path, monkeypatch):
    from mewcode.config import ProviderConfig
    from mewcode.model_registry import ModelRef
    from mewcode.plugins.host import AGENT_FACTORY, ApplicationHost
    from mewcode.plugins.runtime import AgentRuntimeRequest
    from mewcode.expert_web import _build_role_agent
    from mewcode.expert_group import ExpertExecutionPolicy
    from mewcode.experts.recovery_analyst import ANALYST_TOOLS
    monkeypatch.setenv('MEWCODE_USER_DATA_DIR', str(tmp_path / 'userdata'))
    host = ApplicationHost(str(tmp_path), discover_plugins=False).start()
    config = ProviderConfig(name='fixture', protocol='openai-compat', model='test', base_url='https://example.test')
    client = SimpleNamespace(model='test', set_usage_recorder=lambda _: None, get_usage_recorder=lambda: None)
    root = child = None
    try:
        root = host.context.get(AGENT_FACTORY).create(AgentRuntimeRequest(provider=config, client=client,
            auxiliary_client=client, work_dir=str(tmp_path), enable_memory=False))
        session = SimpleNamespace(agent=root.agent, context=root.context, work_dir=str(tmp_path), session=None, task_manager=None)
        service = SimpleNamespace(resolve=lambda *a, **k: SimpleNamespace(config=config, client=client, context_window=64000))
        policy = ExpertExecutionPolicy('stable', False, 0, node_tools=ANALYST_TOOLS, parallel_readonly=True,
            recovery_analysis={'case_id': 'case', 'round': 1, 'holder': {}, 'dossier': {}},
            authorization_node_id='work', stage_inputs={'@run': {'run_id': 'test-run',
                'node_states': {'work': {'input_revision': 'rev'}}}})
        agent, delegation, child = _build_role_agent(session, service, 'planner', ModelRef('test', 'test'), policy)
        names = {t.name for t in agent.registry.list_tools()}
        assert names <= set(ANALYST_TOOLS) and delegation is None
        assert {'ReadCaseDossier', 'ReadAuthorizations', 'ReadRecoveryContext',
            'VerifyRecoveryCheckpoint', 'SubmitRecoveryPlan'} <= names
        for name in ['WriteFile', 'Bash', 'InvokeDeferredTool', 'Agent', 'browser_navigate', 'SubmitHandoff']:
            assert agent.registry.get(name) is None
    finally:
        if child:
            await child.aclose()
        if root:
            await root.aclose()
        await host.aclose()


@pytest.mark.asyncio
async def test_original_scheduler_advances_after_verified_callback(tmp_path):
    from mewcode.expert_group import ExpertGroupOrchestrator, ExpertRoleResult
    from tests.test_expert_flow import _profile
    from tests.test_retry_memory import retry_flow
    h = harness(tmp_path)
    scheduled = []
    async def node_runner(node, model, prompt, policy, member=None):
        scheduled.append(node.node_id)
        if len(scheduled) == 1:
            return ExpertRoleResult('capability not prepared', status='needs_attention', error_kind='capability_missing')
        return ExpertRoleResult('done')
    async def recovery(**kwargs):
        return await h.coordinator.run(node=kwargs['node'], result=kwargs['result'], policy=kwargs['policy'])
    result = await ExpertGroupOrchestrator(runner=lambda *_: None, node_runner=node_runner, archive_root=tmp_path,
        recovery_handler=recovery).run('task', _profile(), {}, strategy='stable', flow=retry_flow())
    assert result.status == 'completed' and scheduled == ['work', 'work', 'done']


@pytest.mark.asyncio
async def test_restart_after_answer_executes_saved_recovery_before_business_node(tmp_path):
    from mewcode.expert_group import ExpertGroupOrchestrator, ExpertRoleResult
    from mewcode.experts.case_runtime import answer_case
    from tests.test_expert_flow import _profile
    from tests.test_retry_memory import retry_flow
    h = harness(tmp_path, make_options=lambda n: [option(n, authority=True), option(90)])
    with pytest.raises(BranchWaitPaused):
        await run(h)
    c = cases(h.store.load(RUN))[0]
    h.store.mutate(RUN, lambda r: answer_case(r, c['case_id'], round=0, choice='approve'))
    scheduled = []
    async def node_runner(node, model, prompt, policy, member=None):
        assert cases(h.store.load(RUN))[0]['status'] == 'resolved'
        scheduled.append(node.node_id)
        return ExpertRoleResult('done')
    async def recovery(**kwargs):
        return await h.coordinator.run(node=kwargs['node'], result=kwargs['result'], policy=kwargs['policy'])
    result = await ExpertGroupOrchestrator(runner=lambda *_: None, node_runner=node_runner,
        recovery_handler=recovery, run_store=h.store, archive_root=tmp_path).run('task', _profile(), {},
            strategy='stable', run_id=RUN, checkpoint=h.store.load(RUN), flow=retry_flow())
    assert result.status == 'completed' and scheduled == ['work', 'done']
    assert len([call for call in h.calls if call[2].recovery_analysis]) == 1
    assert len([call for call in h.calls if call[2].assistance_session]) == 1
