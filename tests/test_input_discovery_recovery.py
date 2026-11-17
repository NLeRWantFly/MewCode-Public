import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mewcode.experts.case_runtime import CaseCoordinator
from mewcode.expert_group import ExpertExecutionPolicy
from mewcode.expert_runs import ExpertRunStore
from mewcode.experts.problem_dossier import incident_report
from mewcode.experts.recovery_case import apply_event, digest, save_case
from mewcode.experts.recovery_context import verify_input_discovery


FIXTURE = Path(__file__).parent / 'fixtures' / 'recovery' / 'input_discovery.json'


class MemoryStore:
    def __init__(self, record):
        self.record = record

    def load(self, run_id):
        return self.record

    def mutate(self, run_id, callback, *, durable=True):
        callback(self.record)
        return copy.deepcopy(self.record)


def saved_record():
    with FIXTURE.open(encoding='utf-8') as stream:
        return json.load(stream)


def expert_store_from_fixture(tmp_path, *, case_mutator=None):
    """Install the small frozen ledger into a real durable run store."""
    record = saved_record()
    record['locked_flow'] = {
        'flow_id': 'input-discovery-test', 'version': 1, 'label': '输入发现恢复测试',
        'nodes': [{'node_id': 'n_prep_plan', 'role': 'planner', 'label': '准备'}],
        'edges': [], 'finalizer_node_id': 'n_prep_plan', 'meta': {},
    }
    record.update({
        'version': 2,
        'strategy': 'stable', 'attempt': 1, 'status_reason': '',
        'flow_state': {'phase': 'paused', 'status': 'paused', 'reason': ''},
        'updated_at': '2026-10-08T00:00:00+00:00',
        'created_at': '2026-10-08T00:00:00+00:00',
        'started_at': '2026-10-08T00:00:00+00:00', 'completed_at': None,
        'paused_intervals': [], 'attempts': [],
        'stages': copy.deepcopy(record['node_states']), 'active_node_ids': [],
        'handoffs': [], 'events': [], 'loop_states': {}, 'children': {},
        'tools': {}, 'members': {}, 'node_attempts': {}, 'pipeline_runs': {},
        'node_expansions': {}, 'display_index': 0, 'last_seq': 0, 'legacy': False,
    })
    if case_mutator:
        case_mutator(record)
    store = ExpertRunStore(tmp_path / 'runs')
    # _write is the same durable snapshot path used by create/mutate; the
    # fixture is intentionally installed only in this temporary test store.
    with store._lock:
        store._write(record)
    return store, record['run_id']


def discovery_case(record):
    return record['node_states']['n_prep_plan']['recovery_case']


def test_saved_glob_receipt_reconciles_without_new_round_or_input_binding():
    record = saved_record()
    before_inputs = copy.deepcopy(record['run_inputs'])
    before_flow = copy.deepcopy(record['locked_flow'])
    case = discovery_case(record)
    checked = verify_input_discovery(record, 'n_prep_plan', case=case, case_id=case['case_id'])
    assert checked['callback_eligible']
    assert checked['relative_paths'] == [
        '前言 电力会计专项总结.pdf', '数学习题.pdf', '电机学.pdf',
        '第一章 电路的模型及其基本规律.pdf',
    ]
    assert checked['absolute_paths'][0].startswith('C:\\Users\\example-user\\Desktop\\sample-project\\')

    async def emit(_event):
        return None

    coordinator = CaseCoordinator(store=MemoryStore(record), run_id=record['run_id'],
        emit=emit, executor=None, autonomous=False)
    result = asyncio.run(coordinator._run(
        node=SimpleNamespace(node_id='n_prep_plan'),
        result=SimpleNamespace(error_kind='', text='原输入缺口'),
        policy=ExpertExecutionPolicy('stable', False, 0),
    ))
    assert result['action'] == 'continue_checkpoint'
    assert record['node_states']['n_prep_plan']['status'] == 'running'
    assert 'pending_recovery_case' not in record['node_states']['n_prep_plan']
    assert record['node_states']['n_prep_plan']['recovery_case']['status'] == 'resolved'
    assert len(record['node_states']['n_prep_plan']['recovery_case']['rounds']) == 1
    assert record['run_inputs'] == before_inputs
    assert record['locked_flow'] == before_flow
    message = record['node_states']['n_prep_plan']['assistance_resume_message']
    assert 'n_prep_plan__assist_8103c490a19c' in message and 'call_ed2fa41c3df6453b9fdd9a6a43f84101' in message
    assert '原节点仍须自行核对输入并提交 SubmitHandoff' in message


def test_real_continue_reconciles_saved_discovery_inside_store_transaction(tmp_path):
    store, run_id = expert_store_from_fixture(tmp_path)
    before = store.load(run_id)
    before_inputs = copy.deepcopy(before['run_inputs'])
    before_flow = copy.deepcopy(before['locked_flow'])
    before_tools = copy.deepcopy(
        before['node_states']['n_prep_plan__assist_8103c490a19c']['tools']
    )
    resumed = store.resume(run_id, strategy='stable')
    stage = resumed['node_states']['n_prep_plan']
    assert resumed['status'] == 'running'
    assert stage['status'] == 'running'
    assert stage['recovery_case']['status'] == 'resolved'
    assert len(stage['recovery_case']['rounds']) == len(before['node_states']['n_prep_plan']['recovery_case']['rounds'])
    assert stage['recovery_case']['rounds'] == before['node_states']['n_prep_plan']['recovery_case']['rounds']
    assert resumed['run_inputs'] == before_inputs
    assert resumed['locked_flow'] == before_flow
    assert resumed['node_states']['n_prep_plan__assist_8103c490a19c']['tools'] == before_tools
    assert '原节点仍须自行核对输入并提交 SubmitHandoff' in stage['assistance_resume_message']
    # The planner is resumed at its checkpoint; the saved helper handoff is
    # evidence only and is never rewritten as a completed planner handoff.
    assert stage['status'] != 'completed'


def test_real_continue_rejects_other_pending_authority_without_partial_reconcile(tmp_path):
    def add_authority(record):
        authority = copy.deepcopy(discovery_case(record))
        authority.update(
            case_id='authority-pending-case', status='reported',
            pending={'kind': 'elevation'}, report={'root_cause': 'authority_gap'},
            events=['open:authority'],
        )
        save_case(record, authority)

    store, run_id = expert_store_from_fixture(tmp_path, case_mutator=add_authority)
    before = store.load(run_id)
    with pytest.raises(ValueError, match='恢复案件需要具体决定'):
        store.resume(run_id, strategy='stable')
    assert store.load(run_id) == before


@pytest.mark.parametrize('mutate', [
    lambda record: record['node_states']['n_prep_plan'].update(input_revision='stale-revision'),
    lambda record: (
        discovery_case(record).update(closed_by_user=True),
        save_case(record, discovery_case(record)),
    ),
])
def test_real_continue_rejects_stale_or_closed_saved_discovery_without_write(tmp_path, mutate):
    store, run_id = expert_store_from_fixture(tmp_path, case_mutator=mutate)
    before = store.load(run_id)
    with pytest.raises(ValueError, match='恢复案件需要具体决定'):
        store.resume(run_id, strategy='stable')
    assert store.load(run_id) == before


@pytest.mark.parametrize('root, pending_kind', [
    ('authority_gap', 'elevation'),
    ('business_decision', 'business_decision'),
    ('unknown_effect', 'incident'),
])
def test_current_authority_business_or_unknown_case_cannot_use_old_glob_receipt(
        tmp_path, root, pending_kind):
    def mutate(record):
        case = discovery_case(record)
        case['report']['root_cause'] = root
        case['pending'] = {'kind': pending_kind, 'question': '当前案件仍待决定。'}
        save_case(record, case)

    store, run_id = expert_store_from_fixture(tmp_path, case_mutator=mutate)
    record = store.load(run_id)
    case = record['node_states']['n_prep_plan']['recovery_case']
    checked = verify_input_discovery(record, 'n_prep_plan', case=case, case_id=case['case_id'])
    assert not checked['callback_eligible']
    assert any('当前案件不是输入缺口报告' in reason for reason in checked['reasons'])
    before = copy.deepcopy(record)
    with pytest.raises(ValueError, match='恢复案件需要具体决定'):
        store.resume(run_id, strategy='stable')
    assert store.load(run_id) == before


def test_current_typed_input_or_authority_pending_is_not_treated_as_informational_incident():
    record = saved_record()
    case = discovery_case(record)
    case['pending'] = {'kind': 'input', 'questions': [{'name': 'book_paths'}]}
    checked = verify_input_discovery(record, 'n_prep_plan', case=case, case_id=case['case_id'])
    assert not checked['callback_eligible']
    assert any('待决项' in reason for reason in checked['reasons'])


def _set_executing_discovery_round(record, *, add_current_receipt):
    case = discovery_case(record)
    case.update(status='executing', pending={})
    case['plan']['round'] = 2
    stage = record['node_states']['n_prep_plan']
    if add_current_receipt:
        request_id = digest([case['case_id'], 2])[:32]
        previous_receipt = copy.deepcopy(stage['assistance_receipts'][0])
        previous_receipt['request_id'] = request_id
        stage['assistance_receipts'].append(previous_receipt)
        previous_helper = record['node_states']['n_prep_plan__assist_8103c490a19c']
        current_helper = copy.deepcopy(previous_helper)
        current_helper['request_id'] = request_id
        record['node_states']['n_prep_plan__assist_' + request_id[:12]] = current_helper
    save_case(record, case)


def test_executing_discovery_uses_current_round_receipt_only(tmp_path):
    store, run_id = expert_store_from_fixture(
        tmp_path, case_mutator=lambda record: _set_executing_discovery_round(
            record, add_current_receipt=False))
    record = store.load(run_id)
    case = record['node_states']['n_prep_plan']['recovery_case']
    stale = verify_input_discovery(record, 'n_prep_plan', case=case, case_id=case['case_id'])
    assert not stale['callback_eligible']
    assert any('当前代际已接受的协助回执' in reason for reason in stale['reasons'])

    store, run_id = expert_store_from_fixture(
        tmp_path / 'current',
        case_mutator=lambda record: _set_executing_discovery_round(
            record, add_current_receipt=True))
    record = store.load(run_id)
    case = record['node_states']['n_prep_plan']['recovery_case']
    current_ref = digest([case['case_id'], 2])[:32]
    checked = verify_input_discovery(record, 'n_prep_plan', case=case, case_id=case['case_id'])
    assert checked['callback_eligible']
    assert checked['executor_receipt_ref'] == current_ref
    assert checked['helper_node_id'] == 'n_prep_plan__assist_' + current_ref[:12]


def test_interrupted_input_discovery_execution_uses_deterministic_request_receipt():
    record = saved_record()
    stage = record['node_states']['n_prep_plan']
    case = discovery_case(record)
    case.update(status='executing', rounds=[], pending={}, events=['open:1', 'start:1'])
    case['plan']['round'] = 1
    save_case(record, case)
    request_id = digest([case['case_id'], 1])[:32]
    receipt = stage['assistance_receipts'][0]
    receipt['request_id'] = request_id
    old_helper_id = 'n_prep_plan__assist_8103c490a19c'
    helper = record['node_states'].pop(old_helper_id)
    record['node_states']['n_prep_plan__assist_' + request_id[:12]] = helper
    checked = verify_input_discovery(record, 'n_prep_plan', case=case, case_id=case['case_id'])
    assert checked['callback_eligible']

    async def emit(_event):
        return None

    coordinator = CaseCoordinator(store=MemoryStore(record), run_id=record['run_id'],
        emit=emit, executor=None, autonomous=False)
    result = asyncio.run(coordinator._run(
        node=SimpleNamespace(node_id='n_prep_plan'),
        result=SimpleNamespace(error_kind='', text='中断'),
        policy=ExpertExecutionPolicy('stable', False, 0),
    ))
    assert result['action'] == 'continue_checkpoint'
    assert record['node_states']['n_prep_plan']['recovery_case']['status'] == 'resolved'
    assert len(record['node_states']['n_prep_plan']['recovery_case']['rounds']) == 0


def test_incident_report_keeps_real_missing_material_cause_when_future_field_exists():
    record = saved_record()
    case = discovery_case(record)
    report = incident_report(record, case, conclusion=case['report']['reason'], root_cause='input_defect')
    assert 'book_paths' in report['reason']
    assert '没有需要立即提交的输入' not in report['reason']
    assert '已有路径清单可复用；点击上方继续任务' in report['reason']
    assert '无需追加轮数或填写未来批大小' in report['reason']
    assert report['input_discovery']['status'] == 'saved_host_verified'
    assert report['deferred_inputs'][0]['name'] == 'batch_size'

def test_incident_report_keeps_plan_cause_when_saved_discovery_needs_revalidation():
    record = saved_record()
    record['node_states']['n_prep_plan__assist_8103c490a19c']['tools']['call_ed2fa41c3df6453b9fdd9a6a43f84101']['output'] = 'No files matched the pattern.'
    case = discovery_case(record)
    report = incident_report(record, case, conclusion=case['report']['reason'], root_cause='input_defect')
    assert 'book_paths' in report['reason']
    assert '没有需要立即提交的输入' not in report['reason']
    assert report['input_discovery']['status'] == 'needs_revalidation'

def test_explicit_current_gap_is_not_replaced_by_future_field_without_plan():
    record = saved_record()
    case = discovery_case(record)
    case['plan'] = {}
    report = incident_report(record, case, conclusion='原准备规划缺少 book_paths，不能继续。', root_cause='input_defect')
    assert 'book_paths' in report['reason']
    assert '没有需要立即提交的输入' not in report['reason']


@pytest.mark.parametrize('mutate', [
    lambda r: r['node_states']['n_prep_plan__assist_8103c490a19c']['tools']['call_ed2fa41c3df6453b9fdd9a6a43f84101'].update(output='No files matched the pattern.'),
    lambda r: r['node_states']['n_prep_plan__assist_8103c490a19c']['tools']['call_ed2fa41c3df6453b9fdd9a6a43f84101'].update(output='a.pdf\n… (output truncated)'),
    lambda r: r['node_states']['n_prep_plan__assist_8103c490a19c']['tools']['call_ed2fa41c3df6453b9fdd9a6a43f84101'].update(output='..\\outside.pdf'),
    lambda r: r['node_states']['n_prep_plan']['recovery_case']['plan']['options'][0]['verification']['arguments'].update(path=r'C:\Other'),
    lambda r: r['node_states']['n_prep_plan__assist_8103c490a19c']['tools']['call_ed2fa41c3df6453b9fdd9a6a43f84101'].update(args={'pattern': '**/*.txt', 'path': r'C:\Users\example-user\Desktop\sample-project'}),
    lambda r: r['node_states']['n_prep_plan__assist_8103c490a19c']['handoff']['inline_outputs'][0].update(content='C:\\Other\\wrong.pdf'),
    lambda r: r['node_states']['n_prep_plan']['recovery_case']['plan']['options'][0]['strategy'].update(tools=['Glob', 'Bash']),
])
def test_saved_glob_rejects_incomplete_or_mismatched_evidence(mutate):
    record = saved_record()
    mutate(record)
    case = discovery_case(record)
    checked = verify_input_discovery(record, 'n_prep_plan', case=case, case_id=case['case_id'])
    assert not checked['callback_eligible']
    assert checked['reasons']


def test_saved_glob_rejects_stale_revision_and_unrelated_blockers():
    record = saved_record()
    # Keep the helper receipt unchanged but make the original checkpoint stale.
    record['node_states']['n_prep_plan']['input_revision'] = 'new-revision'
    case = discovery_case(record)
    checked = verify_input_discovery(record, 'n_prep_plan', case=case, case_id=case['case_id'])
    assert not checked['callback_eligible'] and any('版本' in reason for reason in checked['reasons'])

    record = saved_record()
    record['node_states']['n_prep_plan']['pending_permission'] = {'tool_id': 'permission'}
    case = discovery_case(record)
    checked = verify_input_discovery(record, 'n_prep_plan', case=case, case_id=case['case_id'])
    assert not checked['callback_eligible'] and checked['reasons']


def test_execution_node_does_not_get_generic_glob_recovery_exemption():
    record = saved_record()
    for node in record['locked_flow']['nodes']:
        if node.get('node_id') == 'n_prep_plan':
            node['role'] = 'executor'
    case = discovery_case(record)
    checked = verify_input_discovery(record, 'n_prep_plan', case=case, case_id=case['case_id'])
    assert not checked['callback_eligible']
    assert any('规划节点' in reason for reason in checked['reasons'])


def test_reconcile_event_rechecks_current_source_and_case_version():
    record = saved_record()
    case = discovery_case(record)
    checked = verify_input_discovery(record, 'n_prep_plan', case=case, case_id=case['case_id'])
    forged = {**checked, 'absolute_paths': ['C:\\Other\\foreign.pdf'], 'evidence_digest': 'invented'}
    with pytest.raises(ValueError, match='来源不一致'):
        apply_event(record, case['case_id'], {
            'kind': 'reconcile', 'identity': 'forged-source',
            'case_digest': digest(case), 'evidence': forged,
        })
    assert record['node_states']['n_prep_plan']['recovery_case']['status'] == 'reported'
    with pytest.raises(ValueError, match='当前案件版本'):
        apply_event(record, case['case_id'], {
            'kind': 'reconcile', 'identity': 'forged-version',
            'case_digest': 'wrong', 'evidence': checked,
        })
