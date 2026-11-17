"""Rejected proposals can be withdrawn without hiding accepted-spec defects."""
from __future__ import annotations

import copy
import json
from unittest.mock import AsyncMock

import pytest

from mewcode.orchestration.draft_store import DraftStore
from mewcode.orchestration.task_design import TaskDesign
from mewcode.orchestration.workflow_blocks import WorkflowBlocks
from mewcode.tools.workflow_blocks import SubmitWorkflowBlocks, SubmitWorkflowBlocksParams
from tests.test_workflow_blocks import make_blocks, node_entry, record, submit, user_sources


def binding(view):
    return {key: view[key] for key in ['source_hash', 'base_graph_hash', 'requirement_fingerprint']} | {
        'expected_sequence': view['sequence']}


def withdraw(blocks, view, ids=None):
    return blocks.submit(**binding(view), upserts=[], ignored=[], withdraw_pending=[
        {'diagnostic_id': key, 'reason': '撤回历史错误提案，保留已保存的有效要求。'}
        for key in (ids if ids is not None else [row['diagnostic_id'] for row in view['pending']])])


def valid_materials(tmp_path):
    blocks = make_blocks(tmp_path, rows=[record('user', '整理原始材料并保留来源。')])
    source = user_sources(blocks)[0]
    submit(blocks, blocks.read(), [node_entry('work', '整理材料', '整理原始材料并保留来源。', [source])])
    return blocks, source


def test_legacy_anonymous_diagnostics_withdraw_without_losing_valid_document(tmp_path):
    blocks, source = valid_materials(tmp_path / 'project')
    design = TaskDesign(blocks.project, 'chat')
    design.start('plan')
    design.record_turn(source['text'])
    store = DraftStore(blocks.project, 'chat')
    with store.db() as db:
        state = store._get(db)
        state['materials']['pending'] = [
            {'entry_id': None, 'path': 'sources', 'code': 'invalid_source'},
            {'entry_id': '', 'path': 'entry', 'code': 'unsupported_entry_field'},
            {'entry_id': '', 'path': 'ignored[0]', 'code': 'invalid_ignored'},
            {'entry_id': '', 'path': 'ignored[0]', 'code': 'invalid_ignored'},
            {'entry_id': 'old_duplicate', 'path': 'data.work_key', 'code': 'duplicate_work_key'}]
        state['materials']['pending_upserts'] = [{'body': '无 ID 的旧提案'}, {'id': 'old_duplicate'}]
        state['candidate'] = {'id': 'saved-candidate', 'status': 'needs_review', 'document': {'entries': []}, 'processed_ids': []}
        store._put(db, state)
    before = store.get()
    view = blocks.read()
    assert len(view['pending']) == 4
    duplicate = next(row for row in view['pending'] if row['code'] == 'invalid_ignored')
    assert duplicate['occurrences'] == 2
    assert blocks.read()['pending'] == view['pending']
    result = withdraw(blocks, view)
    after = store.get()
    assert result['pending'] == []
    assert after['materials']['pending_upserts'] == []
    assert after['materials']['document'] == before['materials']['document']
    assert after['task_design'] == before['task_design']
    assert after['candidate'] == before['candidate']
    assert after['materials']['sequence'] == before['materials']['sequence'] + 1
    history = after['materials']['pending_history'][-1]
    assert history['action'] == 'withdraw_pending'
    assert sum(row['occurrences'] for row in history['removed']) == 5
    assert history['withdrawn_upserts'] == before['materials']['pending_upserts']
    queued = design.queue_build(task_id=view['task_design']['task_id'], revision=view['task_design']['revision'],
        sequence=result['sequence'], source_hash=blocks.read()['source_hash'], base_graph_hash=view['base_graph_hash'])
    assert queued['status'] == 'queued' and not queued.get('run_id')


def test_repeated_bad_ignored_coalesces_and_preserves_attempt_history(tmp_path):
    blocks, _ = valid_materials(tmp_path / 'project')
    for trial in range(3):
        view = blocks.read()
        result = blocks.submit(**binding(view), upserts=[], ignored=[{'id': 'wrong', 'reason': str(trial)}])
        assert len(result['pending']) == 1
        assert result['pending'][0]['occurrences'] == trial + 1
    assert len(DraftStore(blocks.project, 'chat').get()['materials']['pending_history']) == 4
    assert withdraw(blocks, blocks.read())['pending'] == []


def test_correcting_entry_keeps_anonymous_diagnostics_and_archives_replaced_error(tmp_path):
    blocks, source = valid_materials(tmp_path / 'project')
    bad = node_entry('bad', '错误节点', '错误节点', [source])
    bad['data']['work_key'] = 7
    submit(blocks, blocks.read(), [bad])
    store = DraftStore(blocks.project, 'chat')
    with store.db() as db:
        state = store._get(db)
        state['materials']['pending'].append({'entry_id': None, 'path': 'sources', 'code': 'invalid_source'})
        store._put(db, state)
    bad['data']['work_key'] = 'different-work'
    result = submit(blocks, blocks.read(), [bad])
    assert [row['entry_id'] for row in result['pending']] == [None]
    history = store.get()['materials']['pending_history'][-1]
    assert history['removed'][0]['entry_id'] == 'bad'


def test_partial_withdrawal_preserves_other_pending_proposals(tmp_path):
    blocks, source = valid_materials(tmp_path / 'project')
    bad = node_entry('bad', '坏提案', '坏提案', [source])
    bad['data']['work_key'] = False
    submit(blocks, blocks.read(), [bad])
    view = blocks.read()
    blocks.submit(**binding(view), upserts=[], ignored=[{'id': 'wrong', 'reason': 'wrong'}])
    view = blocks.read()
    ignored = next(row for row in view['pending'] if row['code'] == 'invalid_ignored')
    result = withdraw(blocks, view, [ignored['diagnostic_id']])
    assert [row['entry_id'] for row in result['pending']] == ['bad']
    assert DraftStore(blocks.project, 'chat').get()['materials']['pending_upserts'] == [bad]


def test_unknown_stale_and_cross_conversation_withdrawals_are_atomic(tmp_path):
    blocks, source = valid_materials(tmp_path / 'project')
    blocks.submit(**binding(blocks.read()), upserts=[], ignored=[{'id': 'wrong', 'reason': 'wrong'}])
    view = blocks.read()
    store = DraftStore(blocks.project, 'chat')
    before = store.get()
    with pytest.raises(ValueError, match='STALE'):
        withdraw(blocks, view, ['pd_' + '0' * 24])
    assert store.get() == before
    other = WorkflowBlocks(blocks.project, 'other', source_path=blocks.source_path)
    other.submit(**binding(other.read()), upserts=[], ignored=[{'id': 'wrong', 'reason': 'wrong'}])
    assert other.read()['pending'][0]['diagnostic_id'] != view['pending'][0]['diagnostic_id']
    with pytest.raises(ValueError, match='STALE'):
        withdraw(other, other.read(), [view['pending'][0]['diagnostic_id']])
    submit(blocks, blocks.read(), [node_entry('another', '另一工作', '另一工作', [source])])
    newer = store.get()
    with pytest.raises(ValueError, match='STALE'):
        withdraw(blocks, view)
    assert store.get() == newer


@pytest.mark.parametrize('defect', ['source', 'binding', 'duplicate', 'field', 'top_field'])
def test_withdrawal_revalidates_current_document_and_still_blocks_build(tmp_path, defect):
    blocks, source = valid_materials(tmp_path / 'project')
    design = TaskDesign(blocks.project, 'chat')
    design.start('plan')
    design.record_turn(source['text'])
    store = DraftStore(blocks.project, 'chat')
    with store.db() as db:
        state = store._get(db)
        node = state['materials']['document']['entries'][0]
        if defect == 'source':
            node['sources'][0]['quote'] = '不是原文'
        elif defect == 'binding':
            node['data']['input_bindings'] = {'原始材料': 'does_not_exist'}
        elif defect == 'duplicate':
            node['data']['work_key'] = 'same'
            another = copy.deepcopy(node)
            another['id'] = 'duplicate'
            state['materials']['document']['entries'].append(another)
        elif defect == 'top_field':
            node['unexpected_field'] = '不支持的字段'
        else:
            node['data']['work_key'] = False
        state['materials']['pending'] = [{'entry_id': '', 'path': 'ignored[0]', 'code': 'invalid_ignored'}]
        store._put(db, state)
    view = blocks.read()
    result = withdraw(blocks, view)
    assert result['pending'] and all(row['origin'] == 'document' for row in result['pending'])
    with pytest.raises(ValueError, match='待修正字段'):
        design.queue_build(task_id=view['task_design']['task_id'], revision=view['task_design']['revision'],
            sequence=result['sequence'], source_hash=view['source_hash'], base_graph_hash=view['base_graph_hash'])
    repeated = withdraw(blocks, blocks.read())
    assert [row['diagnostic_id'] for row in repeated['pending']] == [row['diagnostic_id'] for row in result['pending']]


@pytest.mark.asyncio
async def test_tool_withdrawal_never_calls_auxiliary_model_and_exposes_protocol(tmp_path):
    blocks, _ = valid_materials(tmp_path / 'project')
    blocks.submit(**binding(blocks.read()), upserts=[], ignored=[{'id': 'wrong', 'reason': 'wrong'}])
    view = blocks.read()
    runtime = type('Runtime', (), {'judge': AsyncMock(side_effect=AssertionError('no model call'))})()
    tool = SubmitWorkflowBlocks(blocks, judgment_runtime=runtime)
    params = SubmitWorkflowBlocksParams(**binding(view), withdraw_pending=[{
        'diagnostic_id': view['pending'][0]['diagnostic_id'], 'reason': '撤回错误提交'}])
    assert tool.permission_risk(params.model_dump()).network is False
    result = await tool.execute(params)
    assert not result.is_error
    assert json.loads(result.output)['pending'] == []
    runtime.judge.assert_not_awaited()
    header, _ = blocks.read_page()
    assert 'pending_contract' in header['section_ranges']
    assert 'pending_history' in header['section_ranges']


def test_withdrawal_retry_is_idempotent_and_invalid_requests_add_no_diagnostics(tmp_path):
    blocks, _ = valid_materials(tmp_path / 'project')
    blocks.submit(**binding(blocks.read()), upserts=[], ignored=[{'id': 'wrong', 'reason': 'wrong'}])
    view = blocks.read()
    store = DraftStore(blocks.project, 'chat')
    before = store.get()
    with pytest.raises(ValueError, match='非空 reason'):
        blocks.submit(**binding(view), upserts=[], ignored=[], withdraw_pending=[{
            'diagnostic_id': view['pending'][0]['diagnostic_id'], 'reason': ' '}])
    assert store.get() == before
    result = withdraw(blocks, view)
    assert withdraw(blocks, view) == {**result, 'cached': True}


def test_rejected_unhashable_entry_id_remains_addressable(tmp_path):
    blocks, source = valid_materials(tmp_path / 'project')
    bad = node_entry('bad', '坏提案', '坏提案', [source])
    bad['id'] = ['不是有效的 ID']
    result = submit(blocks, blocks.read(), [bad])
    assert result['pending']
    assert withdraw(blocks, blocks.read())['pending'] == []
    assert DraftStore(blocks.project, 'chat').get()['materials']['pending_upserts'] == []
