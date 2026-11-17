"""A fixed review toolchain can recheck a saved delivery on explicit Continue."""
import copy
from dataclasses import asdict

import pytest

from mewcode.kernel import collaboration as ledger
from mewcode.kernel.acceptance import review_binding, retry_malformed_review
from mewcode.kernel.events import digest
from tests.test_collaboration_mode import RUN_ID, core, delivery
from tests.test_collaboration_recovery import run, store_for
from tests.test_collaboration_integration import orchestrator


@pytest.mark.asyncio
async def test_continue_rechecks_malformed_acceptance_without_replaying_producer(tmp_path):
    store = store_for(tmp_path)
    producers, reviews = [], []

    async def execute(node, *_):
        producers.append(node.node_id)
        return delivery('The saved original delivery')

    async def review(model, payload, context):
        reviews.append(context['acceptance_round'])
        return 'No submission tool' if len(reviews) == 1 else {'passed': True, 'evidence': ['Saved output checked']}

    host = orchestrator(store, tmp_path, execute, review)
    assert (await run(host, store)).status == 'blocked'
    before = store.read_collaboration(RUN_ID)
    pending = next(row for row in before['attempts'].values() if row['contract_id'] == 'work')
    assert pending['status'] == 'checking' and pending['acceptance_fault']['execution_error'] == 'acceptance_format'
    assert (await run(host, store)).status == 'completed'
    after = store.read_collaboration(RUN_ID)
    assert producers == ['work', 'deliver'] and reviews[:2] == [0, 1]
    assert after['attempts'][pending['id']]['delivery'] == pending['delivery']
    assert all(after['calls'][key] == call for key, call in before['calls'].items())


@pytest.mark.parametrize('change', ['', 'same_host', 'missing_call', 'changed_handoff', 'waiting',
                                   'unknown_effect', 'permission_fault', 'quality_result', 'already_advanced'])
def test_only_explicit_resume_of_proven_malformed_review_advances_round(change):
    value = core()
    check = {'id': 'review', 'kind': 'review', 'version': 1, 'blocking': True, 'criteria': ['Check the saved output']}
    value['contracts']['work']['checks'] = [check]
    attempt = value['attempts'][ledger.dispatch(value, 'work')['id']]
    attempt.update(status='checking', delivery=asdict(delivery()), acceptance_fault={
        'execution_error': 'acceptance_format', 'artifacts': [], 'checks': []})
    context = dict(node_id='work', attempt_id=attempt['id'], acceptance_round=0)
    identity = digest(['acceptance', attempt['id'], 0, check, []])
    binding = {**review_binding(context, check), 'call_id': identity,
               'handoff_hash': digest(attempt['delivery']['handoff'])}
    value['calls'][identity] = dict(kind='acceptance', status='finished', binding=binding,
        result={'passed': False, 'execution_error': 'acceptance_format'})
    record = {'attempt': 2, 'collaboration': value}
    if change == 'same_host': record['attempt'] = 1
    elif change == 'missing_call': value['calls'].clear()
    elif change == 'changed_handoff': attempt['delivery']['handoff']['summary'] = 'Changed delivery'
    elif change == 'waiting': value['calls'][identity]['status'] = 'waiting'
    elif change == 'unknown_effect': record['kernel_calls'] = {'c': {'node_id': 'work', 'state': 'unknown'}}
    elif change == 'permission_fault': attempt['acceptance_fault']['execution_error'] = 'permission_required'
    elif change == 'quality_result': value['calls'][identity]['result'] = {'passed': False, 'evidence': ['Quality defect']}
    elif change == 'already_advanced': value['acceptance_rounds'] = {attempt['id']: 1}
    calls, saved = copy.deepcopy(value['calls']), copy.deepcopy(attempt['delivery'])
    retry_malformed_review(record, attempt)
    once = copy.deepcopy(record)
    retry_malformed_review(record, attempt)
    assert record == once
    assert value.get('acceptance_rounds', {}).get(attempt['id'], 0) == int(change in {'', 'already_advanced'})
    assert value['calls'] == calls and attempt['delivery'] == saved
