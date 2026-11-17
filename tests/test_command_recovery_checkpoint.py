import copy

import pytest

from mewcode.expert_group import ExpertRoleResult
from mewcode.experts.recovery_case import cases, open_case
from mewcode.experts.recovery_context import context_sections, verify_checkpoint
from mewcode.tools.submit_recovery_plan import Strategy, Verification
from tests.test_recovery_case_e2e import RUN, harness, option, run


def seed_command(h):
    args = {'command': 'python -c "print(1)"', 'timeout': 120, 'publication': 'discard'}
    def prepare(record):
        record['node_states']['work']['tools'] = {'failed-command': {'name': 'Bash', 'args': args,
            'epoch': 0, 'status': 'failed', 'is_error': True, 'declared_at': 9, 'completed_at': 10,
            'host_observation': {'command_execution': {'status': 'denied', 'command_started': False}}}}
        open_case(record, 'work', capability='command')
    h.store.mutate(RUN, prepare)
    return args, cases(h.store.load(RUN))[0]


def completed_command(args):
    return {'name': 'Bash', 'args': args, 'epoch': 0, 'status': 'completed', 'is_error': False,
        'declared_at': 11, 'completed_at': 12, 'host_observation': {'command_execution': {
            'status': 'exited', 'command_started': True, 'exit_code': 0, 'stdout': '1\n', 'stderr': '',
            'output_truncated': False, 'cleanup_confirmed': True, 'transaction_id': 'host-transaction',
            'publication_state': 'discarded'}}}


@pytest.mark.parametrize('guard', ['', 'denied', 'exit', 'cleanup', 'transaction', 'truncated', 'stdout',
    'args', 'stale', 'unknown_write', 'permission', 'case', 'epoch', 'revision', 'foreign_helper', 'publication'])
def test_command_receipt_requires_exact_success_and_settled_boundary(tmp_path, guard):
    h = harness(tmp_path)
    args, case = seed_command(h)
    record = h.store.load(RUN)
    stage = record['node_states']['work']
    stage['tools']['success'] = completed_command(copy.deepcopy(args))
    host = stage['tools']['success']['host_observation']['command_execution']
    values = {'case_id': case['case_id'], 'epoch': 0, 'input_revision': 'rev'}
    if guard == 'denied': host.update(status='denied', command_started=False)
    elif guard == 'exit': host['exit_code'] = 1
    elif guard == 'cleanup': host['cleanup_confirmed'] = False
    elif guard == 'transaction': host.pop('transaction_id')
    elif guard == 'truncated': host['output_truncated'] = True
    elif guard == 'stdout': host['stdout'] = None
    elif guard == 'args': stage['tools']['success']['args']['command'] = 'different command'
    elif guard == 'stale': stage['tools']['success']['declared_at'] = 8
    elif guard == 'unknown_write': stage['tools']['unknown'] = {'name': 'WriteFile', 'status': 'outcome_unknown'}
    elif guard == 'permission': stage['pending_permission'] = {'tool_id': 'pending'}
    elif guard == 'case': values['case_id'] = 'foreign'
    elif guard == 'epoch': values['epoch'] = 1
    elif guard == 'revision': values['input_revision'] = 'different'
    elif guard == 'foreign_helper':
        record['node_states']['work__assist_foreign'] = {'tools': {'success': stage['tools'].pop('success')}}
    elif guard == 'publication': host['publication_state'] = 'published'
    before = copy.deepcopy(record)
    proof = verify_checkpoint(record, 'work', 'success', **values)
    assert proof['callback_eligible'] is (not guard)
    assert not proof['business_complete'] and record == before
    if not guard:
        assert context_sections(record, 'work')['commands'][-1]['host_observation']['command_execution']['stdout'] == '1\n'


@pytest.mark.asyncio
async def test_command_success_callback_reuses_receipt_without_rerunning_or_completing_node(tmp_path):
    case = None
    def options(n):
        selected = option(n)
        selected['strategy']['tools'] = ['Bash', 'VerifyRecoveryCheckpoint']
        selected['verification'].update(tool='VerifyRecoveryCheckpoint', arguments={
            'evidence_tool_id': 'success', 'case_id': case['case_id'], 'epoch': 0, 'input_revision': 'rev'})
        return [selected]
    h = harness(tmp_path, make_options=options)
    args, case = seed_command(h)
    h.coordinator.capability_bind = None
    original = h.coordinator.executor.dispatch
    async def dispatch(helper, prompt, policy):
        if policy.recovery_analysis:
            return await original(helper, prompt, policy)
        policy.assistance_holder.update(next_step='resume', notes='real host receipt', clearances=[])
        h.store.mutate(RUN, lambda r: r['node_states'].setdefault(helper.node_id, {}).update(
            status='completed', input_revision='rev', tools={'success': completed_command(args)},
            handoff={'status': 'completed', 'blockers': [], 'question': ''}))
        return ExpertRoleResult('command exit confirmed', handoff={'status': 'completed'}, billable_tokens=3)
    h.coordinator.executor.dispatch = dispatch
    h.coordinator.analyst.dispatch = dispatch
    async def probe(node, selected):
        proof = verify_checkpoint(h.store.load(RUN), 'work', 'success', case_id=case['case_id'], epoch=0, input_revision='rev')
        return {'passed': proof['callback_eligible'], 'checkpoint': proof}
    h.coordinator.probe = probe
    result = await run(h)
    record = h.store.load(RUN)
    assert result['action'] == 'continue_checkpoint'
    assert record['node_states']['work']['status'] == 'running'
    assert '禁止重跑' in record['node_states']['work']['assistance_resume_message']
    assert cases(record)[0]['rounds'][0]['verification']['callback_source'] == 'host_command_checkpoint'


@pytest.mark.parametrize('tool,args', [('ReadAuditArtifact', {'tool_id': 'call'}),
    ('ReadAuditArtifact', {'file_path': 'evidence.pdf', 'action': 'clear_cache'}),
    ('ReadFile', {'file_path': 'receipt.json', 'made_up_argument': True})])
def test_invalid_probes_are_rejected_before_executor_dispatch(tool, args):
    with pytest.raises(ValueError):
        Verification(tool=tool, arguments=args, probe='read receipt', expected_evidence='original action works')


def test_nested_assistance_is_not_an_executable_recovery_strategy():
    with pytest.raises(ValueError, match='不能嵌套'):
        Strategy(kind='native_binding', tools=['RequestAssistance', 'Bash'], target='original command')


@pytest.mark.parametrize('ready,current', [(True, True), (False, True), (True, False)])
def test_command_capability_preflight_does_not_bind_vision_or_execute(tmp_path, monkeypatch, ready, current):
    from types import SimpleNamespace
    from mewcode.experts.capability_broker import bind_command
    from mewcode.sandbox import native_gate, lpac_runtime
    from mewcode.tools import ToolRegistry
    from mewcode.tools.bash import Bash
    registry = ToolRegistry()
    registry.register(Bash())
    gate = SimpleNamespace(ready=ready, state=SimpleNamespace(value='enabled'), reason='not enabled')
    monkeypatch.setattr(native_gate, 'native_gate_status', lambda: gate)
    scope = SimpleNamespace(workspace=tmp_path, task_id='bound-task', publication_mode='discard', current=lambda: current)
    monkeypatch.setattr(lpac_runtime.NativeScope, 'from_checker', lambda *args, **kwargs: scope)
    checker = object()
    outcome = bind_command(SimpleNamespace(agent=SimpleNamespace(registry=registry, permission_checker=checker)))
    assert outcome.status == ('bound' if ready and current else 'capability_absent')
    if ready and current:
        assert outcome.route['capability'] == 'command' and outcome.route['execution_confirmed'] is False
        assert outcome.resolver is None


def test_command_plan_must_use_existing_tool_and_real_receipt_verification():
    from mewcode.experts.root_cause import validate_plan
    case = {'case_id':'case', 'failed_step': {'tool':'Bash'}}
    dossier = {'blockers':[], 'capabilities': {'recovery_executor_tools':['Bash','VerifyRecoveryCheckpoint']}}
    selected = option(1)
    selected['strategy']['tools'] = ['Bash', 'VerifyRecoveryCheckpoint']
    selected['verification']['tool'] = 'VerifyRecoveryCheckpoint'
    plan = {'root_cause':{'class':'environment_fixable','evidence_refs':['dossier:case']}, 'options':[selected]}
    assert validate_plan(plan, case, dossier) is plan
    selected['strategy']['tools'] = ['UnimplementedNativeBinding']
    with pytest.raises(ValueError, match='没有挂载'):
        validate_plan(plan, case, dossier)
    selected['strategy']['tools'] = ['Bash']
    selected['verification']['tool'] = 'ReadFile'
    with pytest.raises(ValueError, match='真实命令回执'):
        validate_plan(plan, case, dossier)
