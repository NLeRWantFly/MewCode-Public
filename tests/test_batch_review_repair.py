import copy
import hashlib
import json
from dataclasses import replace

import pytest

from mewcode.tools.review_work_queue import ReviewWorkQueue, ReviewWorkQueueParams as Params, ReadReviewQueue, ReadReviewQueueParams
from mewcode.tools.read_audit_artifact import ReadAuditArtifact, ReadAuditArtifactParams
from mewcode.orchestration.convert import to_expert_flow, from_expert_flow
from mewcode.orchestration.pipeline_bridge import from_pipeline_graph, to_pipeline_graph
from mewcode.orchestration.loops import LoopContract, LoopWatchdog, compile_loops
from mewcode.orchestration.spec import OrchestrationError
from tests.test_generic_loops import spec_for
from tests.test_review_work_queue import setup_queue


async def receipts(tool, root, item):
    reader = ReadAuditArtifact([str(root)])
    reader.image_observer = tool.observe_image
    assert not (await reader.execute(ReadAuditArtifactParams(
        file_path=str(root / item['image_path']), criterion_id=item['item_id']))).is_error
    result = []
    for name in ('left.json', 'right.json'):
        read = await tool.execute(Params(action='evidence', item_id=item['item_id'], source_path=name,
                                        source_selector={'page_idx': item['identity']['pdf_page'] - 1}))
        assert not read.is_error, read.output
        result.append(json.loads(read.output)['evidence_id'])
    return result


@pytest.mark.asyncio
async def test_typed_receipt_separates_review_text_digest_from_raw_file_digest(tmp_path):
    tool, init, _, _ = setup_queue(tmp_path)
    await tool.execute(init)
    source = tmp_path / 'crlf.md'
    raw = b'alpha\r\nbeta\r\n'
    source.write_bytes(raw)
    item = json.loads((await tool.execute(Params(action='next', limit=1))).output)['items'][0]

    result = await tool.execute(Params(
        action='evidence', item_id=item['item_id'], source_path='crlf.md'
    ))

    assert not result.is_error
    receipt = json.loads(result.output)['source']
    assert receipt['sha256'] == hashlib.sha256(b'alpha\nbeta\n').hexdigest()
    assert receipt['digest_kind'] == 'utf8_text_universal_newline_lf_v1'
    assert receipt['file_sha256'] == hashlib.sha256(raw).hexdigest()
    assert receipt['file_digest_kind'] == 'raw_file_sha256_v1'
    assert receipt['sha256'] != receipt['file_sha256']


@pytest.mark.asyncio
async def test_legacy_typed_receipts_gain_digest_semantics_without_changing_review_revision(tmp_path):
    tool, init, store, run_id = setup_queue(tmp_path)
    await tool.execute(init)
    for name in ('left.json', 'right.json'):
        (tmp_path / name).write_text(json.dumps([{'page_idx': 0, 'text': name}]), encoding='utf-8')
    item = json.loads((await tool.execute(Params(action='next', limit=1))).output)['items'][0]
    refs = await receipts(tool, tmp_path, item)
    assert not (await tool.execute(Params(
        action='record', item_id=item['item_id'], state='needs_repair',
        summary='verified issue', evidence_ids=refs,
    ))).is_error
    revision = tool.progress()['revision']

    def make_legacy(snapshot):
        queue = snapshot['review_queues']['check']
        rows = list(queue['records'][item['item_id']]['evidence'])
        rows += list(queue['text_receipts'][item['item_id']].values())
        for receipt in rows:
            receipt.pop('digest_kind', None)
            receipt.pop('file_sha256', None)
            receipt.pop('file_digest_kind', None)
    store.mutate(run_id, make_legacy, durable=True)

    worker = ReadReviewQueue(store, run_id, ['check'], 'frozen', tmp_path)
    work = json.loads((await worker.execute(ReadReviewQueueParams(
        reviewer_node_id='check'
    ))).output)
    exposed = work['items'][0]['record']['evidence'][0]
    assert exposed['digest_kind'] == 'utf8_text_universal_newline_lf_v1'
    assert exposed['file_digest_kind'] == 'raw_file_sha256_v1'
    assert exposed['file_sha256']

    assert not (await tool.execute(Params(action='summary'))).is_error
    saved = store.load(run_id)['review_queues']['check']['records'][item['item_id']]['evidence'][0]
    assert saved['digest_kind'] == 'utf8_text_universal_newline_lf_v1'
    assert saved['file_digest_kind'] == 'raw_file_sha256_v1'
    assert tool.progress()['revision'] == revision

    (tmp_path / 'left.json').write_text(json.dumps([{'page_idx': 0, 'text': 'changed'}]), encoding='utf-8')
    stale = json.loads((await worker.execute(ReadReviewQueueParams(
        reviewer_node_id='check'
    ))).output)['items'][0]['record']
    assert stale['evidence_stale'] is True
    changed_receipt = next(row for row in stale['evidence'] if row['path'] == 'left.json')
    assert changed_receipt['file_fingerprint_reason'] == 'text_evidence_changed'


@pytest.mark.asyncio
async def test_typed_receipts_reject_images_and_cross_item_refs_and_only_invalidate_changed_page(tmp_path):
    tool, init, store, run_id = setup_queue(tmp_path)
    await tool.execute(init)
    for name in ('left.json', 'right.json'):
        (tmp_path / name).write_text(json.dumps([{'page_idx': p, 'text': f'value {p}'} for p in range(2)]))
    items = json.loads((await tool.execute(Params(action='next'))).output)['items']
    bad = await tool.execute(Params(action='evidence', item_id=items[0]['item_id'], source_path='page_0001.png'))
    assert bad.is_error and 'ReadAuditArtifact' in bad.output
    for item in items:
        refs = await receipts(tool, tmp_path, item)
        assert not (await tool.execute(Params(action='record', item_id=item['item_id'], state='complete',
                                              summary='pixels match both texts', evidence_ids=refs))).is_error
    assert not tool.exit_blocker()
    wrong = await tool.execute(Params(action='record', item_id=items[0]['item_id'], state='complete',
                                     summary='wrong references', evidence_ids=refs))
    assert wrong.is_error
    (tmp_path / 'right.json').write_text(json.dumps([{'page_idx': 0, 'text': 'changed'}, {'page_idx': 1, 'text': 'value 1'}]))
    assert '变化' in tool.exit_blocker()
    assert tool.progress()['complete'] == 1
    pending = json.loads((await tool.execute(Params(action='next'))).output)['items']
    assert [v['item_id'] for v in pending] == [items[0]['item_id']]
    old = store.load(run_id)['review_queues']['check']['records'][items[1]['item_id']]
    assert old['state'] == 'pass'


@pytest.mark.asyncio
async def test_review_pass_routes_unresolved_to_worker_then_rechecks_next_round(tmp_path):
    tool, init, store, run_id = setup_queue(tmp_path)
    await tool.execute(init)
    items = json.loads((await tool.execute(Params(action='next'))).output)['items']
    for item in items:
        assert not (await tool.execute(Params(action='record', item_id=item['item_id'], state='unknown',
                                              summary='need input mapping'))).is_error
    result = json.loads((await tool.execute(Params(action='next'))).output)
    assert result['items'] == [] and result['reviewed'] == 2 and result['complete'] == 0
    assert result['next_action'] == 'supply_evidence' and tool.exit_blocker()
    worker = ReadReviewQueue(store, run_id, ['check'], 'frozen')
    request = ReadReviewQueueParams(reviewer_node_id='check')
    assert len(json.loads((await worker.execute(request)).output)['items']) == 2
    assert (await worker.execute(request.model_copy(update={'reviewer_node_id': 'other'}))).is_error
    assert (await ReadReviewQueue(store, run_id, ['check'], 'new').execute(request)).is_error
    next_round = ReviewWorkQueue(store, run_id, 'check', tmp_path, 'frozen', review_round=1)
    assert len(json.loads((await next_round.execute(Params(action='next'))).output)['items']) == 2


@pytest.mark.parametrize('tools,denied', [([], []), (['ReadFile'], []), (None, ['Bash', 'EditFile', 'WriteFile'])])
def test_repair_contract_rejects_unwritable_worker(tools, denied):
    flow = to_expert_flow(spec_for())
    loop = {**flow.meta['loops'][0], 'requires_repair': True, 'repair_node_ids': ['work']}
    nodes = [replace(n, tools=tuple(tools) if tools is not None else None, deny_tools=tuple(denied))
             if n.node_id == 'work' else n for n in flow.nodes]
    with pytest.raises(OrchestrationError):
        compile_loops(nodes, flow.edges, [loop])


def test_repair_contract_roundtrips_and_old_hash_stays_compatible():
    spec = spec_for()
    old = LoopContract.from_value(spec.meta['loops'][0])
    assert 'requires_repair' not in old.as_dict() and 'repair_node_ids' not in old.as_dict()
    assert LoopContract.from_value({**old.as_dict(), 'requires_repair': False, 'repair_node_ids': []}).contract_hash == old.contract_hash
    spec.meta['loops'][0].update(requires_repair=True, repair_node_ids=['work'])
    flow = to_expert_flow(spec)
    restored = to_expert_flow(from_pipeline_graph(to_pipeline_graph(from_expert_flow(flow))))
    assert restored.meta['loops'] == flow.meta['loops']


def test_batch_convergence_counts_work_not_reworded_summaries():
    loop = LoopContract.from_value({**to_expert_flow(spec_for()).meta['loops'][0], 'control_mode': 'team'})
    watch = LoopWatchdog(loop)
    progress = {'check': {'total': 10, 'complete': 7, 'needs_repair': 3}}
    report = dict(contract_hash=loop.contract_hash, decision='CONTINUE', checks=[
        dict(criterion_id='REQ-01', state='FAIL', location='host:queue', evidence='new words')])
    assert watch.observe([report], work_progress=progress)[0] == 'continue'
    report['checks'][0]['evidence'] = 'different words but same unresolved work'
    assert watch.observe([report], work_progress=progress)[0] == 'continue'
    report['checks'][0]['evidence'] = 'rewritten summary again'
    assert watch.observe([report], work_progress=progress)[0] == 'blocked'


def test_continuation_cursor_cannot_be_reset_by_oscillation_or_scope_change():
    from mewcode.experts.review_evidence import advance_cursor
    progress = dict(total=20, complete=8, pending=10, advanced=True, manifest_sha256='fixed')
    advanced, cursor = advance_cursor(progress)
    assert advanced
    assert not advance_cursor(progress, cursor)[0]
    assert not advance_cursor({**progress, 'complete': 7}, cursor)[0]
    assert not advance_cursor({**progress, 'total': 21}, cursor)[0]
    assert advance_cursor({**progress, 'complete': 9}, cursor)[0]


@pytest.mark.asyncio
async def test_finite_queue_continues_past_64_segments_without_rerunning_predecessors():
    from mewcode.expert_group import ExpertRoleResult
    from tests.test_team_loop_control import chain_flow, execute, report
    flow, contract = chain_flow()
    calls, states = [], {}
    async def runner(node, incoming, policy, prompt):
        calls.append(node.node_id)
        if node.node_id == 'check':
            completed = calls.count('check')
            progress = dict(total=75, complete=completed, pending=75-completed, advanced=True)
            if completed < 75:
                return ExpertRoleResult(text='saved', status='budget_exhausted', billable_tokens=1, review_progress=progress)
            return ExpertRoleResult(text='all verified', loop_decision=report(policy.loop_context),
                                    review_progress=progress, billable_tokens=1)
        return ExpertRoleResult(text='saved', billable_tokens=1)
    _, result = await execute(flow, contract, states, runner)
    assert result.status == 'completed'
    assert calls.count('check') == 75 and calls.count('extract') == 1
