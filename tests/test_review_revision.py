"""Repair current outputs with complete findings, without inheriting approval."""
import copy
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from mewcode.kernel import collaboration as ledger
from mewcode.kernel.acceptance import Acceptance, review_binding
from mewcode.kernel.events import digest
from mewcode.orchestration.review_revision import (
    artifact_changes, evidence_basis, refresh_previous_delivery, review_revision,
)
from mewcode.orchestration.task_context import _latest_failed_checks
from tests.test_collaboration_integration import orchestrator, run
from tests.test_collaboration_mode import RUN_ID, REVIEWER, core, delivery
from tests.test_iterative_collaboration import checked_flow, retry_store


CHECK = {'id': 'facts', 'kind': 'review', 'version': 1, 'blocking': True,
         'criteria': ['Facts must be supported; preserve unknowns and all original requirements.']}


def finding(identity, description='Unsupported conclusion'):
    return {'issue_id': identity, 'criterion_id': 'facts', 'node_id': 'work', 'file': '结果',
            'blocking': True, 'origin': 'introduced', 'resolution': 'producer_fix',
            'description': description, 'acceptance_test': 'Quote the actual source or retain UNKNOWN; do not invent evidence.'}


@pytest.mark.asyncio
async def test_three_revisions_use_latest_delivery_and_reviewer_still_catches_new_defect(tmp_path):
    store = retry_store(tmp_path, checked_flow(CHECK))
    executed, reviews = [], []
    async def execute(node, model, prompt, policy, *_):
        if node.node_id != 'work':
            return delivery('final')
        number = len(executed) + 1
        if number > 1:
            previous = policy.stage_inputs['previous:work']
            assert previous['handoff']['inline_outputs'][0]['content'] == f'revision {number - 1}'
            assert previous['report']['checks'][0]['evidence']['issues'][0]['issue_id'] == f'R{number - 1}'
            assert previous['same_dependencies']
            assert finding('x')['acceptance_test'] in prompt
            if number == 3:
                assert previous['attempt_id'] != executed[1]['attempt_id']
        executed.append(policy.stage_inputs.get('previous:work') or {})
        result = delivery(f'revision {number}')
        result.handoff['inline_outputs'].append({'name': 'stable evidence', 'content': 'verified source bytes'})
        return result
    async def review(model, payload, context):
        if payload['contract']['id'] != 'work':
            return {'passed': True, 'evidence': ['Final original requirements checked.']}
        number = len(reviews) + 1
        revision = payload.get('revision_context')
        if number == 1:
            assert revision is None
        else:
            assert revision['baseline_attempt_id'] == reviews[-1]['attempt_id']
            assert revision['sources_unchanged']
            assert revision['previous_check']['issues'][0]['issue_id'] == f'R{number - 1}'
            assert {row['name']: row['status'] for row in revision['artifact_changes']} == {
                '结果': 'changed', 'stable evidence': 'unchanged'}
            assert f'-revision {number - 1}' in next(row['inline_diff'] for row in revision['artifact_changes'] if row['name'] == '结果')
            assert payload['check'] == CHECK and payload['host_execution_evidence'] is not None
        reviews.append(context)
        # A new defect in revision 2 must trigger a third delivery and review.
        return {'passed': number == 3, 'evidence': ['Independent source inspection'],
                'issues': [finding(f'R{number}', 'Evidence: ' + 'detail ' * 500)] if number < 3 else []}
    result = await run(orchestrator(store, tmp_path, execute, review), store)
    assert result.status == 'completed', result.final_summary
    assert len(executed) == len(reviews) == 3
    attempts = ledger.attempts_for(store.read_collaboration(RUN_ID), 'work')
    assert [row['status'] for row in attempts] == ['rejected', 'rejected', 'accepted']
    assert [row['delivery']['handoff']['inline_outputs'][0]['content'] for row in attempts] == [
        'revision 1', 'revision 2', 'revision 3']


@pytest.mark.asyncio
async def test_unchanged_delivery_stops_even_when_reviewer_rewords_and_renumbers_findings(tmp_path):
    spec = checked_flow(CHECK)
    store = retry_store(tmp_path, spec)
    executions, reviews = [], []
    repaired = False
    async def execute(node, *_):
        executions.append(node.node_id)
        return delivery('fixed bytes' if repaired else 'unchanged bytes')
    async def review(model, payload, context):
        if payload['contract']['id'] != 'work':
            assert repaired
            return {'passed': True, 'evidence': ['Full final quality checked']}
        reviews.append(context['attempt_id'])
        if repaired:
            return {'passed': True, 'evidence': ['Original defect independently checked and fixed']}
        return {'passed': False, 'evidence': ['source checked'],
                'reason': f'Rephrased review {len(reviews)}',
                'issues': [finding(str(len(reviews)), f'Different wording {len(reviews)}')]}
    host = orchestrator(store, tmp_path, execute, review)
    result = await run(host, store, spec)
    assert result.status == 'blocked' and executions == ['work', 'work'] and len(reviews) == 2
    assert not ledger.valid_certificate(store.read_collaboration(RUN_ID), 'work')
    await run(host, store, spec)
    assert executions == ['work', 'work'] and len(reviews) == 2
    # An explicit targeted retry remains available; the guard is not a deadlock.
    repaired = True
    store.transition(RUN_ID, 'blocked')
    # The web adapter normally projects the returned node status; this offline
    # runner only journals events, so supply that real boundary state here.
    store.mutate(RUN_ID, lambda row: row.setdefault('node_states', {}).setdefault('work', {}).update(
        status='needs_attention', error_kind='collaboration_acceptance'), durable=True)
    store.resume(RUN_ID, strategy='stable', retry_node_id='work')
    assert (await run(host, store, spec)).status == 'completed'
    assert executions == ['work', 'work', 'work', 'deliver'] and len(reviews) == 3


def revision_fixture(tmp_path):
    value = core()
    value['contracts']['work']['checks'] = [copy.deepcopy(CHECK)]
    original = delivery('Known fact\nOverstated claim\n')
    previous = ledger.dispatch(value, 'work')
    artifacts = Acceptance(tmp_path).freeze(original.handoff)
    evidence = {'passed': False, 'evidence': ['Original facts checked'], 'issues': [finding('R1')]}
    report = {'passed': False, 'ready': False, 'checks': [
        {'id': 'facts', 'version': 1, 'passed': False, 'evidence': evidence}],
        'artifacts': artifacts, 'artifacts_unchanged': True}
    value['attempts'][previous['id']]['delivery'] = asdict(original)
    ledger.complete(value, previous['id'], report)
    baseline_context = {'node_id': 'work', 'attempt_id': previous['id'],
                        'policy': SimpleNamespace(stage_inputs={'upstream': {'source_revision': 'v1'}})}
    record = {'collaboration': value, 'authorizations': {'scope': 'original'}}
    identity = digest(['acceptance', previous['id'], 0, CHECK, artifacts])
    binding = {**review_binding(baseline_context, CHECK), 'call_id': identity, 'handoff_hash': digest(original.handoff)}
    value['calls'][identity] = {'kind': 'acceptance', 'status': 'finished',
        'model': REVIEWER.as_dict(), 'binding': binding, 'result': copy.deepcopy(evidence),
        'review_evidence_basis': evidence_basis(record, baseline_context)}
    current = ledger.dispatch(value, 'work')
    context = {**baseline_context, 'attempt_id': current['id']}
    amended = delivery('Known fact\nUnverified claim: UNKNOWN\n')
    payload = {'check': CHECK, 'artifacts': Acceptance(tmp_path).freeze(amended.handoff)}
    return record, payload, context, previous['id']


@pytest.mark.parametrize('change', ['requirements', 'input_version', 'reviewer', 'call_binding',
                                   'call_result', 'fault', 'superseded', 'check_version', 'unfixed_artifacts'])
def test_revision_context_never_reuses_unbound_or_stale_findings(tmp_path, change):
    record, payload, context, previous = revision_fixture(tmp_path)
    value, reviewer = record['collaboration'], REVIEWER.as_dict()
    call = next(iter(value['calls'].values()))
    assert review_revision(value, payload, context, reviewer, evidence_basis(record, context))
    if change == 'requirements': value['entries']['goal']['body'] = 'Different requirements'
    elif change == 'input_version': value['attempts'][previous]['dependencies'] = 'other inputs'
    elif change == 'reviewer': reviewer = {**reviewer, 'effort': 'different'}
    elif change == 'call_binding': call['binding']['check_hash'] = 'other'
    elif change == 'call_result': call['result']['passed'] = True
    elif change == 'fault': value['attempts'][previous]['report']['execution_error'] = 'permission_required'
    elif change == 'superseded': value['attempts'][previous]['superseded'] = True
    elif change == 'check_version': payload['check'] = {**CHECK, 'version': 2}
    elif change == 'unfixed_artifacts': value['attempts'][previous]['report']['artifacts_unchanged'] = False
    assert review_revision(value, payload, context, reviewer, evidence_basis(record, context)) is None


@pytest.mark.parametrize('change', ['authorization', 'source', 'legacy_call'])
def test_changed_sources_preserve_diff_but_require_fresh_evidence_verification(tmp_path, change):
    record, payload, context, _ = revision_fixture(tmp_path)
    if change == 'authorization': record['authorizations'] = {'scope': 'changed'}
    elif change == 'source': context['policy'].stage_inputs['upstream']['source_revision'] = 'v2'
    else: next(iter(record['collaboration']['calls'].values())).pop('review_evidence_basis')
    result = review_revision(record['collaboration'], payload, context, REVIEWER.as_dict(), evidence_basis(record, context))
    assert result and result['sources_unchanged'] is False
    assert result['previous_check']['passed'] is False


def test_complete_actionable_findings_survive_compaction_and_restart(tmp_path):
    record, _, _, previous = revision_fixture(tmp_path)
    value = record['collaboration']
    issues = [finding(f'R{i}', 'Detailed facts ' * 160) for i in range(10)]
    report = value['attempts'][previous]['report']
    report['checks'][0]['evidence'].update(issues=issues, reason='Narration ' * 500)
    loaded = copy.deepcopy(value)  # The projection must be derivable from persisted state.
    source = refresh_previous_delivery(loaded, 'work', {'previous:work': {'attempt_id': 'stale'}})['previous:work']
    failed = _latest_failed_checks(loaded, 'work')
    assert failed['attempt_id'] == source['attempt_id'] == previous
    assert failed['checks'][0]['evidence']['issues'] == issues
    assert source['report'] == report


def test_diff_distinguishes_deletion_rename_and_changed_content_and_detects_corruption(tmp_path):
    freeze = Acceptance(tmp_path).freeze
    before = freeze({'inline_outputs': [{'name': 'old', 'content': 'identical'}, {'name': 'kept', 'content': 'a'}]})
    after = freeze({'inline_outputs': [{'name': 'renamed', 'content': 'identical'}, {'name': 'kept', 'content': 'b'}]})
    result = artifact_changes(before, after)
    assert {r['name']: r['status'] for r in result} == {'old': 'removed', 'renamed': 'added', 'kept': 'changed'}
    after[0]['content'] = 'changed after freeze'
    assert artifact_changes(before, after) is None
    assert artifact_changes(before, before + [before[0]]) is None
