import copy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from mewcode.expert_group import ExpertExecutionPolicy, ExpertRoleResult
from mewcode.expert_runs import ExpertRunStore
from mewcode.expert_web import _role_registry
from mewcode.experts.assistance import AssistanceCoordinator, AssistanceRequest, RunContext, validate_clearances
from mewcode.experts.assistance_policy import attach_assistance_checks
from mewcode.experts.node_ledger import ledger_for_run
from mewcode.permissions.checker import Decision
from mewcode.tools.request_assistance import RequestAssistance, RequestAssistanceParams
from tests.test_expert_children import make_registry
from tests.test_retry_memory import retry_flow


@pytest.mark.parametrize('role', ['executor', 'auditor', 'planner', 'manager'])
def test_control_always_available_and_assistance_uses_primary_tools(tmp_path, role):
    registry = make_registry('ReadFile', 'Bash', 'WriteFile', 'PreparePythonEnvironment')
    session = SimpleNamespace(agent=SimpleNamespace(registry=registry), work_dir=str(tmp_path))
    policy = ExpertExecutionPolicy('stable', False, 0, node_tools=('ReadFile',))
    assert _role_registry(session, role, policy).get('RequestAssistance')
    available = _role_registry(session, 'executor', replace(policy, assistance_session=True))
    assert all(available.get(name) for name in ['ReadFile', 'Bash', 'WriteFile', 'PreparePythonEnvironment'])


def test_audit_output_guard_keeps_global_checks_and_records_denial(tmp_path):
    checker = SimpleNamespace(check=Mock(return_value=Decision('allow', 'global checked')))
    original = checker.check
    log = []
    policy = ExpertExecutionPolicy('stable', False, 0, assistance_permission_log=log,
        assistance_protected_paths=(str(tmp_path / 'reviewed.pdf'),))
    attach_assistance_checks(checker, policy, str(tmp_path))
    tool = SimpleNamespace(name='WriteFile')
    assert checker.check(tool, {'file_path': str(tmp_path / 'reviewed.pdf')}).effect == 'deny'
    assert checker.check(tool, {'file_path': str(tmp_path / 'notes.txt')}).effect == 'allow'
    assert original.call_count == 2
    assert '审查协助保护' in log[0]['permission_decision']


@pytest.mark.asyncio
@pytest.mark.parametrize('policy_fields', [{'loop_context': {'loop_id': 'loop'}}, {'candidate_context': {'root': 'candidate'}}, {'parallel_readonly': True}, {'audit_contract': {'artifact_dir': 'reviewed'}}])
async def test_callback_preserves_original_checkpoint(tmp_path, policy_fields):
    flow = retry_flow(); node = flow.node_map['work']
    store = ExpertRunStore(tmp_path / 'runs')
    store.create(run_id='a' * 32, conversation_id='c', task='task', attachments=[], profile={}, strategy='stable', flow=flow)
    store.mutate('a' * 32, lambda r: r['node_states'].setdefault('work', {}).update(messages=[{'role': 'user', 'content': 'retained'}], status='needs_attention'))
    before = copy.deepcopy(store.load('a' * 32)['node_states']['work']['messages'])
    async def dispatch(helper, prompt, policy):
        assert policy.node_tools == ('*',)
        assert policy.assistance_session and not policy.parallel_readonly
        policy.assistance_holder.update(next_step='resume', notes='verified local capability', clearances=[])
        return ExpertRoleResult('done', status='completed', handoff={'status': 'completed'})
    coordinator = AssistanceCoordinator(store=store, run_id='a' * 32, dispatch=dispatch, emit=AsyncMock(),
        before_resume=AsyncMock(), task='task', work_dir=str(tmp_path), model={})
    receipt = await coordinator.run(AssistanceRequest('r', 'work', 0, 'diagnosis', 'check', (), (), 'node'),
        RunContext(node, ExpertExecutionPolicy('stable', False, 0, **policy_fields)))
    snapshot = store.load('a' * 32)
    assert receipt.next_step == 'resume'
    assert snapshot['node_states']['work']['messages'] == before
    assert ledger_for_run(snapshot).view('work').phase == 'running'
    assert any(f.kind == 'assistance' for f in ledger_for_run(snapshot).view('work').history)


def test_forged_clearance_is_rejected():
    record = {'node_states': {'n': {}}, 'node_ledgers': {}}
    ledger = ledger_for_run(record)
    blocker = ledger.record('n', 'blocker', {'kind': 'capability_missing'})
    request = AssistanceRequest('r', 'n', 0, 'capability', 'need', (), (), 'node')
    rows, errors = validate_clearances(record, request, [{'blocker_fact_id': blocker.fact_id,
        'clearance_kind': 'capability_binding_receipt', 'evidence_ref': 'forged'}])
    assert not rows and errors
    assert ledger.view('n').active_blockers


@pytest.mark.asyncio
async def test_request_is_durable_idempotent_control(tmp_path):
    store = ExpertRunStore(tmp_path / 'runs')
    store.create(run_id='b' * 32, conversation_id='c', task='task', attachments=[], profile={}, strategy='stable', flow=retry_flow())
    store.mutate('b' * 32, lambda r: r['node_states'].setdefault('work', {}))
    tool = RequestAssistance(store, 'b' * 32, 'work')
    params = RequestAssistanceParams(kind='capability', need='probe import', requested_tools=['Bash'])
    assert not (await tool.execute(params)).is_error
    snapshot = store.load('b' * 32)
    request_id = snapshot['node_states']['work']['assistance_request']['request_id']
    assert not (await tool.execute(params)).is_error
    assert store.load('b' * 32)['node_states']['work']['assistance_request']['request_id'] == request_id


@pytest.mark.asyncio
@pytest.mark.parametrize('effect,expected', [('applied', 'completed'), ('not_applied', 'not_started'), ('partial', 'effect_partial')])
async def test_effect_verification_binds_real_helper_observation_and_keeps_history(tmp_path, effect, expected):
    from mewcode.experts.command_receipts import request_receipts
    store = ExpertRunStore(tmp_path / 'runs')
    identity = 'c' * 32
    flow = retry_flow()
    store.create(run_id=identity, conversation_id='c', task='verify', attachments=[], profile={}, strategy='stable', flow=flow)
    rows = request_receipts('work', 'current', [{'command': 'echo changed > result.txt'}], status='outcome_unknown')
    def setup(record):
        record['node_states'].setdefault('work', {}).update(status='needs_attention', input_revision='current', command_receipts=rows)
        ledger_for_run(record).record('work', 'blocker', {'kind': 'effect_unknown', 'request_id': rows[0]['request_id']})
    store.mutate(identity, setup)
    async def dispatch(helper, prompt, policy):
        from mewcode.tools.read_file import ReadFile, Params
        target = tmp_path / 'result.txt'
        if effect != 'not_applied':
            target.write_text('changed\n' if effect == 'applied' else 'different\n', encoding='utf-8')
        observed = await ReadFile().execute(Params(file_path=str(target)))
        store.mutate(identity, lambda r: r['node_states'].setdefault(helper.node_id, {}).update(
            tools={'observed-file': {'name': 'ReadFile', 'args': {'file_path': str(target)},
                'status': 'failed' if observed.is_error else 'completed', 'is_error': observed.is_error,
                'output': observed.output, 'host_observation': observed.host_observation}}))
        policy.assistance_holder.update(next_step='resume', notes='verified target state',
            effect_verifications=[{'request_id': rows[0]['request_id'], 'effect': effect, 'evidence_refs': ['observed-file']}])
        return ExpertRoleResult('verified', status='completed')
    coordinator = AssistanceCoordinator(store=store, run_id=identity, dispatch=dispatch, emit=AsyncMock(),
        before_resume=AsyncMock(), task='verify', work_dir=str(tmp_path), model={})
    receipt = await coordinator.run(AssistanceRequest('effect-check', 'work', 0, 'effect_verification', 'check file state', (), (), 'controller'),
        RunContext(flow.node_map['work'], ExpertExecutionPolicy('stable', False, 0)))
    snapshot = store.load(identity)
    assert receipt.next_step == ('ask_human' if effect == 'partial' else 'resume')
    assert snapshot['node_states']['work']['command_receipts'][0]['status'] == expected
    view = ledger_for_run(snapshot).view('work')
    assert bool(view.active_blockers) == (effect == 'partial')
    assert any(f.payload.get('status') == 'outcome_unknown' for f in view.history)
    assert any(f.payload.get('clearance_kind') == 'independent_effect_receipt' for f in view.history) == (effect != 'partial')


def test_proof_receipt_does_not_replace_current_command_receipt():
    ledger = ledger_for_run({'node_ledgers': {}})
    command = ledger.record('n', 'receipt', {'request_id': 'r', 'status': 'outcome_unknown'})
    ledger.record('n', 'receipt', {'request_id': 'r', 'effect_verification_observation': True})
    assert ledger.view('n').current_receipts == (command,)


def test_automatic_patch_cannot_remove_explicit_tool_denials():
    from mewcode.experts.maintenance_adoption import adopt_assistance_patch
    flow = retry_flow().as_dict()
    work = next(n for n in flow['nodes'] if n['node_id'] == 'work')
    work['deny_tools'] = ['Bash', 'WriteFile']
    record = {'locked_flow': flow, 'node_states': {'work': {'status': 'needs_attention'}}, 'node_ledgers': {}}
    original = copy.deepcopy(record)
    request = AssistanceRequest('patch', 'work', 0, 'diagnosis', 'fix', (), (), 'controller')
    with pytest.raises(ValueError, match='显式禁用'):
        adopt_assistance_patch(record, request, {'nodes': [{'node_id': 'work', 'deny_tools': []}]})
    assert record == original


@pytest.mark.asyncio
async def test_resume_claim_before_helper_write_failure_cannot_resume_parent(tmp_path):
    identity = 'd' * 32
    store = ExpertRunStore(tmp_path / 'runs')
    flow = retry_flow()
    store.create(run_id=identity, conversation_id='c', task='verify', attachments=[], profile={}, strategy='stable', flow=flow)
    store.mutate(identity, lambda r: r['node_states'].setdefault('work', {'status': 'needs_attention'}))
    async def dispatch(helper, prompt, policy):
        policy.assistance_holder.update(next_step='resume', notes='claimed before tool completion')
        store.mutate(identity, lambda r: r['node_states'].setdefault(helper.node_id, {}).update(
            tools={'uncertain': {'name': 'Bash', 'args': {'command': 'echo changed >> result.txt'},
                'status': 'outcome_unknown'}}))
        return ExpertRoleResult('connection lost', status='failed')
    coordinator = AssistanceCoordinator(store=store, run_id=identity, dispatch=dispatch, emit=AsyncMock(),
        before_resume=AsyncMock(), task='verify', work_dir=str(tmp_path), model={})
    receipt = await coordinator.run(AssistanceRequest('uncertain-helper', 'work', 0, 'diagnosis', 'check', (), (), 'controller'),
        RunContext(flow.node_map['work'], ExpertExecutionPolicy('stable', False, 0)))
    assert receipt.next_step == 'ask_human'
    saved = store.load(identity)
    assert saved['node_states']['work']['status'] != 'running'
    assert any(f.payload.get('kind') == 'unknown_effect' for f in ledger_for_run(saved).view('work').active_blockers)
