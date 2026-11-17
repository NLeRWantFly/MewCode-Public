"""Host-fixed content, queue advancement and bounded no-progress recovery."""
import asyncio
import copy

import pytest

from mewcode.expert_group import ExpertExecutionPolicy, ExpertRoleResult
from mewcode.expert_runs import ExpertRunStore
from mewcode.experts.loop_progress import artifact_versions, round_progress
from mewcode.experts.loop_runtime import run_bounded_loop
from mewcode.kernel import collaboration as ledger
from mewcode.kernel.acceptance import Acceptance
from mewcode.orchestration.convert import to_expert_flow
from mewcode.orchestration.loops import MAX_FOREACH_ITEMS, LoopContract
from mewcode.orchestration.spec import OrchestrationSpec
from tests.test_collaboration_mode import PROFILE, RUN_ID, core, passing_report
from tests.test_generic_loops import report, spec_for


def fixed_record(tmp_path, text='actual bytes'):
    (tmp_path / 'business.txt').write_text(text, encoding='utf-8')
    value = core()
    attempt = ledger.dispatch(value, 'work')
    result = passing_report(value)
    result['artifacts'] = Acceptance(tmp_path).freeze({'artifacts': [{'path': 'business.txt'}],
        'inline_outputs': [{'name': 'explanation', 'content': 'narrative'}]})
    ledger.complete(value, attempt['id'], result)
    return {'collaboration': value}


def test_round_versions_require_current_host_certificate_and_ignore_inline_narrative(tmp_path):
    record = fixed_record(tmp_path)
    versions = artifact_versions(record, 'work')
    assert len(versions) == 1 and versions[0]['path'] == 'business.txt'
    assert versions[0]['sha256'] == Acceptance(tmp_path).freeze({'artifacts': [{'path': 'business.txt'}]})[0]['sha256']
    record['collaboration']['certificates']['work']['artifacts'][-1]['content'] = 'another explanation'
    assert artifact_versions(record, 'work') == versions
    ledger.invalidate(record['collaboration'], ['work'], 'source changed')
    assert artifact_versions(record, 'work') is None
    assert artifact_versions({'node_states': {'work': {'handoff': {'artifacts': [
        {'path': 'business.txt', 'sha256': 'invented'}]}}}}, 'work') is None


async def run_loop(flow, contract, states, runner, *, emit=None, recover=None):
    async def ignore(_):
        pass
    return await run_bounded_loop(contract=contract, flow=flow, saved=states, run_node=runner,
        emit=emit or ignore, policy_for=lambda _: ExpertExecutionPolicy('stable', False, 0),
        incoming=[], recover_node=recover)


@pytest.mark.asyncio
@pytest.mark.parametrize('diagnose', [False, True])
async def test_unchanged_files_stop_despite_unique_review_prose_and_only_one_diagnosis(tmp_path, diagnose):
    raw = spec_for().as_dict()
    raw['meta']['loops'][0]['control_mode'] = 'team'
    flow = to_expert_flow(OrchestrationSpec.from_value(raw))
    contract = LoopContract.from_value(flow.meta['loops'][0])
    versions = artifact_versions(fixed_record(tmp_path), 'work')
    calls, diagnoses, states = [], [], {}
    async def runner(node, incoming, policy, prompt):
        calls.append(node.node_id)
        if node.node_id == 'work':
            return ExpertRoleResult(text='same file, new explanation ' + str(len(calls)),
                work_progress={'artifact_versions': versions})
        verdict = report(policy.loop_context, 'FAIL')
        verdict['checks'][0].update(evidence='rewritten review ' + str(len(calls)), location='business.txt')
        return ExpertRoleResult(text='review ' + str(len(calls)), loop_decision=verdict)
    async def recover(**kwargs):
        diagnoses.append(kwargs['host_facts'])
        return {'action': 'diagnose_path', 'chosen': {'reason': 'inspect existing issue'}}
    _, result = await run_loop(flow, contract, states, runner, recover=recover if diagnose else None)
    assert result.error_kind == 'no_progress'
    assert calls.count('check') == (5 if diagnose else 3)
    assert len(diagnoses) == int(diagnose)
    assert states['improve']['stalled'] >= contract.max_stalled_rounds
    assert result.status != 'completed'


@pytest.mark.asyncio
async def test_new_fixed_producer_bytes_allow_unchanged_coarse_score_to_continue(tmp_path):
    raw = spec_for().as_dict()
    raw['meta']['loops'][0]['control_mode'] = 'team'
    flow = to_expert_flow(OrchestrationSpec.from_value(raw))
    contract = LoopContract.from_value(flow.meta['loops'][0])
    calls = []
    async def runner(node, incoming, policy, prompt):
        calls.append(node.node_id)
        if node.node_id == 'work':
            versions = artifact_versions(fixed_record(tmp_path, str(calls.count('work'))), 'work')
            return ExpertRoleResult(text='new content', work_progress={'artifact_versions': versions})
        return ExpertRoleResult(text='located evidence', loop_decision=report(policy.loop_context,
            'PASS' if calls.count('check') == 5 else 'FAIL'))
    states = {}
    _, result = await run_loop(flow, contract, states, runner)
    assert result.status == 'completed' and calls.count('work') == 5
    assert states['improve']['stalled'] == 0


def test_round_progress_uses_queue_counts_and_excludes_reviewer_file_rewrites():
    contract = LoopContract.from_value(spec_for().meta['loops'][0])
    decisions = {'check': report({'contract_hash': contract.contract_hash}, 'FAIL')}
    local = {'work': ExpertRoleResult(text='work', work_progress={'artifact_versions': [
        {'path': 'business.txt', 'sha256': 'a' * 64}]}),
        'check': ExpertRoleResult(text='check', work_progress={'artifact_versions': [
            {'path': 'review.txt', 'sha256': 'b' * 64}]})}
    first = round_progress(local, contract, decisions)
    local['check'].work_progress['artifact_versions'][0]['sha256'] = 'c' * 64
    decisions['check']['checks'][0]['evidence'] = 'different prose'
    assert round_progress(local, contract, decisions) == first
    decisions['check']['checks'][0]['state'] = 'PARTIAL'
    assert round_progress(local, contract, decisions) != first
    local['check'].review_progress = {'manifest_sha256': 'frozen', 'complete': 8, 'total': 16}
    assert round_progress(local, contract, decisions)['check']['complete'] == 8


@pytest.mark.asyncio
async def test_135_frozen_batches_advance_and_resume_without_repeating_finished_items(tmp_path):
    raw = spec_for().as_dict()
    items = [{'item_id': f'batch-{i:03}', 'goal': f'Process the frozen pages of batch {i}'} for i in range(135)]
    raw['meta']['loops'][0].update(mode='foreach', items=items, control_mode='team')
    flow = to_expert_flow(OrchestrationSpec.from_value(raw))
    contract = LoopContract.from_value(flow.meta['loops'][0])
    store = ExpertRunStore(tmp_path / 'runs', storage='sqlite')
    store.create(run_id=RUN_ID, conversation_id='queue', task='process every batch', attachments=[],
                 profile=PROFILE.as_dict(), strategy='stable', flow=flow)
    states, calls, crash = {}, [], True
    async def emit(event):
        if crash and (event.get('loop_state') or {}).get('completed_items') == ['batch-000']:
            store.mutate(RUN_ID, lambda record: record.update(status='paused', loop_states=copy.deepcopy(states)), durable=True)
            raise asyncio.CancelledError()
    async def runner(node, incoming, policy, prompt):
        item = policy.loop_context['current_item']['item_id']
        assert len(policy.loop_context['items']) == 135
        assert '"item_count": 135' in prompt and '"items": [' not in prompt
        calls.append((node.node_id, item))
        return ExpertRoleResult(text='actual processing complete, quality unverified',
            handoff={'status': 'completed', 'inline_outputs': [{'name': 'quality',
                'content': '{"quality_passed":false,"reference_state":"missing"}'}]},
            loop_decision=report(policy.loop_context) if node.node_id == 'check' else None)
    with pytest.raises(asyncio.CancelledError):
        await run_loop(flow, contract, states, runner, emit=emit)
    assert calls == [('work', 'batch-000'), ('check', 'batch-000')]
    crash = False
    loaded = ExpertRunStore(tmp_path / 'runs', storage='sqlite').load(RUN_ID)
    states = loaded['loop_states']
    _, result = await run_loop(flow, contract, states, runner)
    assert result.status == 'completed' and len(calls) == 270
    assert calls[2] == ('work', 'batch-001')
    assert states['improve']['completed_items'] == [item['item_id'] for item in items]
    assert len(result.handoff['queue_results']) == 135
    for item in result.handoff['queue_results'].values():
        assert '"quality_passed":false' in item['work']['handoff']['inline_outputs'][0]['content']


def test_queue_capacity_is_finite_and_draft_uses_the_same_limit():
    from mewcode.orchestration.workflow_draft import validate_entry
    raw = spec_for().meta['loops'][0]
    items = [{'item_id': str(i), 'goal': 'item'} for i in range(MAX_FOREACH_ITEMS)]
    assert len(LoopContract.from_value({**raw, 'mode': 'foreach', 'items': items}).items) == MAX_FOREACH_ITEMS
    entry = {'id': 'queue', 'kind': 'loop', 'title': 'queue', 'body': 'queue', 'state': 'active',
             'data': {'mode': 'foreach', 'items': items}, 'sources': []}
    assert len(validate_entry(entry)['data']['items']) == MAX_FOREACH_ITEMS
    for parse in (lambda rows: LoopContract.from_value({**raw, 'mode': 'foreach', 'items': rows}),
                  lambda rows: validate_entry({**entry, 'data': {'mode': 'foreach', 'items': rows}})):
        with pytest.raises(ValueError, match='foreach'):
            parse(items + [{'item_id': 'too-many', 'goal': 'overflow'}])
