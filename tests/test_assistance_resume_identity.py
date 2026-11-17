"""Completed assistance is host subwork, not a missing business graph node."""
import copy

import pytest

from mewcode.experts.node_ledger import NodeLedger
from mewcode.orchestration.revisions import can_resume_confirmed_flow
from tests.test_collaboration_mode import flow


def checkpoint():
    spec = flow()
    request_id = 'a' * 64
    helper = 'work__assist_' + request_id[:12]
    receipt = dict(request_id=request_id, node_id='work', epoch=0, next_step='resume', notes='诊断已返回')
    record = {'locked_flow': spec.as_dict(), 'stage_cursor': helper,
        'node_states': {'work': {'status': 'blocked', 'assistance_receipts': [receipt]},
                        helper: {'node_id': helper, 'role': 'executor', 'status': 'completed'}},
        'collaboration': {'format': 'mewcode.collaboration.v2'}, 'node_ledgers': {}}
    NodeLedger(record['node_ledgers']).record('work', 'assistance', receipt, identity=['receipt', request_id])
    return record, spec, helper


def test_assistance_receipt_restores_parent_identity_without_changing_or_clearing_progress():
    record, spec, _ = checkpoint()
    before = copy.deepcopy(record)
    assert can_resume_confirmed_flow(record, spec)
    assert record == before and record['node_states']['work']['status'] == 'blocked'


@pytest.mark.parametrize('change', ['missing_fact', 'model_fact', 'changed_receipt', 'wrong_epoch',
                                   'wrong_parent', 'wrong_role', 'lookalike', 'removed_parent', 'real_node'])
def test_unproven_assistance_cannot_hide_removed_business_progress(change):
    record, spec, helper = checkpoint()
    current = spec.as_dict()
    receipt = record['node_states']['work']['assistance_receipts'][0]
    if change == 'missing_fact': record['node_ledgers'].clear()
    elif change == 'model_fact': record['node_ledgers']['work']['facts'][0]['source'] = 'node'
    elif change == 'changed_receipt': receipt['request_id'] = 'b' * 64
    elif change == 'wrong_epoch': receipt['epoch'] = 1
    elif change == 'wrong_parent': receipt['node_id'] = 'deliver'
    elif change == 'wrong_role': record['node_states'][helper]['role'] = 'planner'
    elif change == 'lookalike': record['node_states']['work__assist_unproven'] = {'status': 'completed'}
    elif change == 'removed_parent':
        current['nodes'] = [node for node in current['nodes'] if node['node_id'] != 'work']
        current['edges'] = []
    else: record['locked_flow']['nodes'].append({**record['locked_flow']['nodes'][0], 'node_id': helper})
    before = copy.deepcopy(record)
    assert not can_resume_confirmed_flow(record, current)
    assert record == before
