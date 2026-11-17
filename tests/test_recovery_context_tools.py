import copy
import json
from unittest.mock import AsyncMock

import pytest

from mewcode.experts.authorization_ledger import record_authorization
from mewcode.experts.node_ledger import ledger_for_run
from mewcode.experts.recovery_case import cases, open_case
from mewcode.experts.recovery_context import verify_checkpoint
from mewcode.tools.read_recovery_context import ReadRecoveryContext, ReadRecoveryContextParams
from mewcode.tools.verify_recovery_checkpoint import VerifyRecoveryCheckpoint, VerifyRecoveryCheckpointParams
from tests.test_recovery_case_e2e import harness, RUN, option, run


def seed(h):
    def prepare(record):
        record['locked_models'] = {'work': {'provider_key': 'test', 'model_id': 'vision', 'effort': 'medium'}}
        case = open_case(record, 'work', capability='vision')
        record_authorization(record, kind='elevation_answer', scope={'operation_classes': ['external_send'],
            'model_routes': ['test/vision'], 'content_classes': ['page_image'], 'cost_cap_tokens': 200000},
            quote='approved', source_ref='approved:vision', host_verified=True)
        record['node_states']['work']['tools'] = {'seen-page': {'name': 'InspectVisualEvidence', 'epoch': 0,
            'status': 'completed', 'is_error': False, 'args': {'image_refs': [{'file_path': 'book.pdf', 'page': 183}], 'question': 'observe'},
            'output': 'page 183, printed page 13, questions 3–6', 'host_observation': {
                'pixel_coverage': {'complete': True, 'model': 'vision', 'evidence_ids': ['pixel:page183'], 'plan_digest': 'pixels'}}}}
        return case
    h.store.mutate(RUN, prepare)
    case = cases(h.store.load(RUN))[0]
    args = VerifyRecoveryCheckpointParams(evidence_tool_id='seen-page', case_id=case['case_id'], epoch=0, input_revision='rev')
    return case, args


@pytest.mark.asyncio
async def test_read_tools_are_scoped_pageable_and_never_change_state_or_call_model(tmp_path):
    h = harness(tmp_path)
    case, args = seed(h)
    before = h.store.load(RUN)
    read = ReadRecoveryContext(lambda: h.store.load(RUN), RUN, 'work', epoch=0, input_revision='rev')
    chunks, offset = [], 0
    while True:
        result = await read.execute(ReadRecoveryContextParams(section='observations', offset=offset, limit=99))
        header, body = result.output.split('\n\n', 1)
        fields = dict(line.split(': ', 1) for line in header.splitlines())
        chunks.append(body)
        total = int(fields['TOTAL_CHARS']); offset = int(fields['NEXT_OFFSET'])
        if offset == total:
            break
    assert json.loads(''.join(chunks))[0]['telemetry_state'] == 'legacy_missing'
    accounting = await read.execute(ReadRecoveryContextParams(section='accounting'))
    account = json.loads(accounting.output.split('\n\n', 1)[1])['authorization_limits'][0]
    assert account['remaining_tokens'] == 0 and account['has_estimates']
    verify = VerifyRecoveryCheckpoint(lambda: h.store.load(RUN), RUN, 'work', epoch=0, input_revision='rev')
    result = await verify.execute(args)
    checked = result.host_observation['recovery_checkpoint']
    assert checked['callback_eligible'] and not checked['business_complete']
    assert checked['telemetry_gaps'] == ['route', 'authorization', 'usage']
    assert (await read.execute(ReadRecoveryContextParams(node_id='foreign'))).is_error
    assert h.store.load(RUN) == before and not h.calls
    h.store.mutate(RUN, lambda record: record['node_states']['work'].update(input_revision='new'))
    assert (await verify.execute(args)).is_error


@pytest.mark.parametrize('guard', ['case', 'epoch', 'revision', 'model', 'pixel', 'permission', 'question', 'unknown_effect', 'terminated', 'business'])
def test_checkpoint_rejects_real_blockers_and_unrelated_evidence(tmp_path, guard):
    h = harness(tmp_path)
    case, args = seed(h)
    record = h.store.load(RUN)
    stage = record['node_states']['work']
    values = args.model_dump()
    if guard == 'case': values['case_id'] = 'foreign-case'
    elif guard == 'epoch': values['epoch'] = 1
    elif guard == 'revision': values['input_revision'] = 'foreign-revision'
    elif guard == 'model': stage['tools']['seen-page']['host_observation']['pixel_coverage']['model'] = 'other'
    elif guard == 'pixel': stage['tools']['seen-page']['host_observation']['pixel_coverage']['complete'] = False
    elif guard == 'permission': stage['pending_permission'] = {'tool_id': 'pending'}
    elif guard == 'question': stage['pending_interaction'] = {'question': 'real question'}
    elif guard == 'unknown_effect': stage['tools']['unsettled-write'] = {'status': 'outcome_unknown', 'name': 'WriteFile'}
    elif guard == 'terminated': record['status'] = 'terminated'
    else: ledger_for_run(record).record('work', 'blocker', {'kind': 'business_ambiguity'})
    values.pop('node_id')
    tool_id = values.pop('evidence_tool_id')
    result = verify_checkpoint(record, 'work', tool_id, **values)
    assert not result['callback_eligible'] and result['reasons'] and not result['business_complete']


@pytest.mark.asyncio
@pytest.mark.parametrize('business_blocker', [False, True])
async def test_host_verified_pixels_callback_despite_telemetry_handoff_but_not_business_blocker(tmp_path, business_blocker):
    from mewcode.expert_group import ExpertRoleResult
    from mewcode.experts.waits import BranchWaitPaused
    def options(n):
        selected = option(n)
        selected['strategy']['tools'] = ['VerifyRecoveryCheckpoint']
        selected['verification'].update(tool='VerifyRecoveryCheckpoint', arguments=args.model_dump())
        return [selected]
    h = harness(tmp_path, make_options=options)
    case, args = seed(h)
    h.coordinator.executor.before_resume = AsyncMock()
    original = h.coordinator.executor.dispatch
    async def dispatch(helper, prompt, policy):
        if policy.recovery_analysis:
            return await original(helper, prompt, policy)
        policy.assistance_holder.update(next_step='resume', notes='one existing pixel receipt', clearances=[])
        handoff = {'status': 'needs_attention', 'blockers': ['missing legacy telemetry'],
            'blocker_kind': 'business_ambiguity' if business_blocker else 'capability_missing', 'question': ''}
        h.store.mutate(RUN, lambda record: record['node_states'].setdefault(helper.node_id, {}).update(
            status='needs_attention', handoff=copy.deepcopy(handoff), tools={}))
        return ExpertRoleResult('saved observation', status='needs_attention', handoff=handoff)
    h.coordinator.executor.dispatch = dispatch
    h.coordinator.analyst.dispatch = dispatch
    async def probe(node, selected):
        checked = verify_checkpoint(h.store.load(RUN), 'work', 'seen-page', case_id=case['case_id'], epoch=0, input_revision='rev')
        return {'passed': checked['callback_eligible'], 'checkpoint': checked, 'evidence_refs': ['seen-page']}
    h.coordinator.probe = probe
    if business_blocker:
        with pytest.raises(BranchWaitPaused):
            await run(h)
        assert cases(h.store.load(RUN))[0]['status'] == 'reported'
    else:
        result = await run(h)
        assert result['action'] == 'continue_checkpoint'
        current = cases(h.store.load(RUN))[0]
        assert current['status'] == 'resolved' and len(current['rounds']) == 1
        assert current['rounds'][0]['verification']['callback_source'] == 'host_pixel_checkpoint'
    assert h.store.load(RUN)['node_states']['work']['status'] != 'completed'
    assert h.store.load(RUN)['node_states']['work']['tools']['seen-page']['output'].startswith('page 183')


def test_invalid_probe_is_rejected_at_plan_submission():
    from mewcode.tools.submit_recovery_plan import SubmitRecoveryPlanParams
    from tests.test_recovery_case_e2e import plan
    for name in ['', 'ReadStageResult', 'ToolSearch']:
        selected = option()
        selected['verification']['tool'] = name
        with pytest.raises(ValueError):
            SubmitRecoveryPlanParams(**plan({'case_id': 'case', 'round': 1}, [selected]))


def test_checkpoint_probe_does_not_block_itself_while_running(tmp_path):
    h = harness(tmp_path)
    case, args = seed(h)
    record = h.store.load(RUN)
    record['node_states']['work']['tools']['checking'] = {'name': 'VerifyRecoveryCheckpoint', 'status': 'running'}
    assert verify_checkpoint(record, 'work', 'seen-page')['callback_eligible']


def test_exact_pixel_probe_reuses_success_and_preserves_blocked_or_unknown_call(tmp_path):
    from mewcode.experts.recovery_context import reuse_visual_observation
    h = harness(tmp_path)
    seed(h)
    record = h.store.load(RUN)
    arguments = record['node_states']['work']['tools']['seen-page']['args']
    assert reuse_visual_observation(record, 'work', arguments)['callback_eligible']
    assert reuse_visual_observation(record, 'work', {**arguments, 'question': 'different'}) is None
    record['node_states']['work']['pending_permission'] = {'tool_id': 'pending'}
    assert not reuse_visual_observation(record, 'work', arguments)['callback_eligible']
    record['node_states']['work']['tools']['seen-page']['status'] = 'outcome_unknown'
    assert reuse_visual_observation(record, 'work', arguments) is None


def test_unrelated_helper_and_actual_route_mismatch_are_not_accepted(tmp_path):
    h = harness(tmp_path)
    case, args = seed(h)
    record = h.store.load(RUN)
    observation = record['node_states']['work']['tools'].pop('seen-page')
    record['node_states']['work__assist_foreign'] = {'tools': {'foreign': observation}}
    assert not verify_checkpoint(record, 'work', 'foreign')['callback_eligible']
    record['node_states']['work']['tools']['seen-page'] = observation
    observation['host_observation']['visual_call'] = {'route': {'provider_key': 'test', 'effort': 'high'}}
    checked = verify_checkpoint(record, 'work', 'seen-page')
    assert not checked['callback_eligible'] and any('档位' in reason for reason in checked['reasons'])


def test_assistance_registry_does_not_advertise_an_unbound_assistance_tool(tmp_path):
    from types import SimpleNamespace
    from mewcode.expert_group import ExpertExecutionPolicy
    from mewcode.expert_web import _role_registry
    from mewcode.tools import ToolRegistry
    session = SimpleNamespace(work_dir=str(tmp_path), agent=SimpleNamespace(registry=ToolRegistry()))
    registry = _role_registry(session, 'executor', ExpertExecutionPolicy('stable', False, 0,
        assistance_session=True, assistance_holder={}, node_tools=('RequestAssistance',)))
    assert registry.get('RequestAssistance') is None


@pytest.mark.asyncio
async def test_pixel_request_records_reported_usage_without_inventing_missing_fields(monkeypatch):
    from mewcode.tools import inspect_visual_evidence as module
    from mewcode.tools.base import StreamEnd, TextDelta
    monkeypatch.setattr(module, 'preflight_review_text', lambda *args: None)
    async def stream(*args, **kwargs):
        yield TextDelta('{"observations":"visible page"}')
        yield StreamEnd('end_turn', input_tokens=100, output_tokens=30, cache_read=70, model='vision')
    monkeypatch.setattr(module, 'scheduled_client_stream', stream)
    response = await module._observation_request(object(), {}, 'observe', [], timeout_seconds=1, remaining=lambda: 1)
    assert response['model'] == 'vision'
    assert response['usage']['reported_total_tokens'] == 200
    assert 'billable_tokens' not in response['usage']
    async def missing(*args, **kwargs):
        yield TextDelta('{"observations":"visible page"}')
        yield StreamEnd('end_turn')
    monkeypatch.setattr(module, 'scheduled_client_stream', missing)
    response = await module._observation_request(object(), {}, 'observe', [], timeout_seconds=1, remaining=lambda: 1)
    assert response['usage'] is None and response['model'] == ''
