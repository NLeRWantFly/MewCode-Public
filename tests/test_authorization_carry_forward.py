"""A confirmed scope survives the next run of the same task; nothing wider does."""
import copy

from mewcode.experts.authorization_ledger import carry_forward, covers, record_authorization
from mewcode.experts.visual_authorization_usage import JOURNAL, authorization_account

AUTH = {'operation_class': 'external_send', 'scope': {
    'model_routes': ['desktop:vision/deepseek-flash'], 'content_classes': ['page_image']}}


def granted(record, *, source_ref='answer:1'):
    return record_authorization(record, kind='elevation_answer',
        scope={**copy.deepcopy(AUTH['scope']), 'operation_classes': ['external_send']},
        quote='确认页图外发', source_ref=source_ref, host_verified=True)


def visual_granted(record, *, source_ref, cap=100):
    return record_authorization(record, kind='elevation_answer',
        scope={**copy.deepcopy(AUTH['scope']), 'operation_classes': ['external_send'],
               'cost_cap_tokens': cap}, quote='确认页图外发', source_ref=source_ref,
        host_verified=True)


def test_same_task_reuses_the_confirmed_external_send_scope():
    source = {}
    granted(source)
    target = {}
    result = carry_forward(target, source=source, conversation_id='c1', task='从四书建库',
                           source_conversation_id='c1', source_task='从四书建库')
    assert result['carried'] == 1
    assert len(target['authorizations']['entries']) == 1
    assert covers(target, AUTH).status == 'covered'


def test_a_different_task_never_inherits_a_scope():
    source = {}
    granted(source)
    target = {}
    result = carry_forward(target, source=source, conversation_id='c1', task='把四书里的一道题讲清楚',
                           source_conversation_id='c1', source_task='从四书建库')
    assert result['carried'] == 0
    assert covers(target, AUTH).status != 'covered'


def test_a_different_conversation_never_inherits_a_scope():
    source = {}
    granted(source)
    target = {}
    result = carry_forward(target, source=source, conversation_id='c2', task='从四书建库',
                           source_conversation_id='c1', source_task='从四书建库')
    assert result['carried'] == 0
    assert covers(target, AUTH).status != 'covered'


def test_revoked_expired_and_non_enforceable_entries_are_not_inherited():
    source = {}
    granted(source, source_ref='answer:revoked')
    source['authorizations']['entries'][-1]['revoked_at'] = 1
    granted(source, source_ref='answer:expired')
    source['authorizations']['entries'][-1]['expires_at'] = 10
    record_authorization(source, kind='user_statement', scope={'operation_classes': ['other']},
        quote='口头表态', source_ref='message:2')
    target = {}
    result = carry_forward(target, source=source, conversation_id='c1', task='t',
                           source_conversation_id='c1', source_task='t', now=1000)
    assert result['carried'] == 0
    assert covers(target, AUTH, now=1000).status != 'covered'


def test_carried_entries_keep_their_original_evidence_and_gain_provenance():
    source = {}
    original = granted(source)
    target = {}
    carry_forward(target, source=source, conversation_id='c1', task='t',
                  source_conversation_id='c1', source_task='t')
    entry = target['authorizations']['entries'][0]
    assert entry['auth_id'] == original['auth_id']
    assert entry['source_ref'] == original['source_ref']
    assert entry['kind'] == original['kind']
    assert entry['enforceable'] is True
    assert entry['carried_from']['source_ref'] == original['source_ref']
    # The scope itself is copied, so widening the new run cannot rewrite the grant.
    assert entry['scope'] == original['scope']


def test_carrying_twice_is_idempotent():
    source = {}
    granted(source)
    target = {}
    first = carry_forward(target, source=source, conversation_id='c1', task='t',
                          source_conversation_id='c1', source_task='t')
    second = carry_forward(target, source=source, conversation_id='c1', task='t',
                           source_conversation_id='c1', source_task='t')
    assert first['carried'] == 1
    assert second['carried'] == 0
    assert len(target['authorizations']['entries']) == 1


def test_visual_usage_moves_with_authorization_without_binding_id_collisions_or_overwriting(tmp_path):
    source = {}
    original = visual_granted(source, source_ref='answer:source')
    source[JOURNAL] = {'version': 1, 'calls': {'shared-call': {
        'call_id': 'shared-call', 'auth_id': original['auth_id'], 'state': 'settled',
        'usage': {'reported_total_tokens': 90}, 'reserved_tokens': 0}}}

    target = {}
    local = visual_granted(target, source_ref='answer:target')
    target[JOURNAL] = {'version': 1, 'calls': {'shared-call': {
        'call_id': 'shared-call', 'auth_id': local['auth_id'], 'state': 'settled',
        'usage': {'reported_total_tokens': 7}, 'reserved_tokens': 0}}}

    first = carry_forward(target, source=source, conversation_id='c1', task='t',
                          source_conversation_id='c1', source_task='t')
    inherited_id = first['auth_ids'][0]
    inherited = next(row for row in target['authorizations']['entries']
                     if row['auth_id'] == inherited_id)
    assert inherited_id != local['auth_id']
    assert authorization_account(target, inherited)['used_tokens'] == 90
    assert authorization_account(target, inherited)['remaining_tokens'] == 10
    assert authorization_account(target, local)['used_tokens'] == 7
    assert target[JOURNAL]['calls']['shared-call']['auth_id'] == local['auth_id']
    assert len(authorization_account(target, inherited)['calls']) == 1

    # A new call recorded in the destination after the first carry remains intact.
    target[JOURNAL]['calls']['destination-call'] = {
        'call_id': 'destination-call', 'auth_id': inherited_id, 'state': 'settled',
        'usage': {'reported_total_tokens': 3}, 'reserved_tokens': 0}
    second = carry_forward(target, source=source, conversation_id='c1', task='t',
                           source_conversation_id='c1', source_task='t')
    inherited = next(row for row in target['authorizations']['entries']
                     if row['auth_id'] == inherited_id)
    account = authorization_account(target, inherited)
    assert second['carried'] == 0
    assert account['used_tokens'] == 93 and account['remaining_tokens'] == 7
    assert target[JOURNAL]['calls']['destination-call']['usage'] == {'reported_total_tokens': 3}


def test_visual_reservation_is_carried_and_legacy_usage_is_charged_once():
    source = {}
    original = visual_granted(source, source_ref='answer:source')
    source[JOURNAL] = {'version': 1, 'calls': {'pending': {
        'call_id': 'pending', 'auth_id': original['auth_id'], 'state': 'started',
        'usage': None, 'reserved_tokens': 35}}}
    target = {}
    carry_forward(target, source=source, conversation_id='c1', task='t',
                  source_conversation_id='c1', source_task='t')
    inherited = next(row for row in target['authorizations']['entries']
                     if row['source_ref'] == original['source_ref'])
    account = authorization_account(target, inherited)
    assert account['reserved_tokens'] == 35 and account['remaining_tokens'] == 65
    assert len(account['unknown_call_ids']) == 1

    legacy_source = {}
    legacy_grant = visual_granted(legacy_source, source_ref='answer:legacy')
    legacy_source['node_states'] = {'work': {'tools': {'inspect': {
        'name': 'InspectVisualEvidence', 'host_observation': {'visual_call': {
            'authorization': {'auth_id': legacy_grant['auth_id']},
            'usage': {'reported_total_tokens': 90}}}}}}}
    legacy_target = {}
    carry_forward(legacy_target, source=legacy_source, conversation_id='c1', task='t',
                  source_conversation_id='c1', source_task='t')
    inherited_legacy = next(row for row in legacy_target['authorizations']['entries']
                            if row['source_ref'] == legacy_grant['source_ref'])
    assert authorization_account(legacy_target, inherited_legacy)['remaining_tokens'] == 10
    carry_forward(legacy_target, source=legacy_source, conversation_id='c1', task='t',
                  source_conversation_id='c1', source_task='t')
    assert authorization_account(legacy_target, inherited_legacy)['used_tokens'] == 90


def test_inherited_reservation_tracks_settlement_monotonically_and_preserves_destination_calls():
    for settled_tokens in (90, 20):
        source = {}
        original = visual_granted(source, source_ref=f'answer:source:{settled_tokens}')
        source[JOURNAL] = {'version': 1, 'calls': {'pending': {
            'call_id': 'pending', 'auth_id': original['auth_id'], 'state': 'started',
            'usage': None, 'reserved_tokens': 35}}}
        target = {}

        first = carry_forward(target, source=source, conversation_id='c1', task='t',
                              source_conversation_id='c1', source_task='t')
        inherited_id = first['auth_ids'][0]
        inherited = next(row for row in target['authorizations']['entries']
                         if row['auth_id'] == inherited_id)
        assert authorization_account(target, inherited)['remaining_tokens'] == 65

        target[JOURNAL]['calls']['destination-call'] = {
            'call_id': 'destination-call', 'auth_id': inherited_id, 'state': 'settled',
            'usage': {'reported_total_tokens': 4}, 'reserved_tokens': 0}
        source[JOURNAL]['calls']['pending'].update(
            state='settled', usage={'reported_total_tokens': settled_tokens})

        second = carry_forward(target, source=source, conversation_id='c1', task='t',
                               source_conversation_id='c1', source_task='t')
        inherited = next(row for row in target['authorizations']['entries']
                         if row['auth_id'] == inherited_id)
        account = authorization_account(target, inherited)
        expected_inherited_charge = max(35, settled_tokens)
        assert second['usage_carried'] == 1
        assert account['known_used_tokens'] == settled_tokens + 4
        assert account['estimated_used_tokens'] == max(0, 35 - settled_tokens)
        assert account['used_tokens'] + account['reserved_tokens'] == expected_inherited_charge + 4
        assert account['remaining_tokens'] == 100 - expected_inherited_charge - 4
        assert target[JOURNAL]['calls']['destination-call']['usage'] == {'reported_total_tokens': 4}

        calls_after_sync = copy.deepcopy(target[JOURNAL]['calls'])
        third = carry_forward(target, source=source, conversation_id='c1', task='t',
                              source_conversation_id='c1', source_task='t')
        assert third['usage_carried'] == 0
        assert target[JOURNAL]['calls'] == calls_after_sync
        assert authorization_account(target, inherited)['remaining_tokens'] == 100 - expected_inherited_charge - 4


def test_live_runs_defer_capped_authorizations_until_static_finalization():
    cases = [
        ('running', [], 'settled'),
        ('paused', [], 'settled'),
        ('needs_attention', [], 'settled'),
        ('completed', ['work'], 'settled'),
        ('terminated', [], 'started'),
    ]
    for index, (status, active_nodes, call_state) in enumerate(cases):
        source = {}
        original = visual_granted(source, source_ref=f'answer:live:{index}')
        source.update(run_id=f'{index + 1:032x}', status=status, active_node_ids=active_nodes)
        source[JOURNAL] = {'version': 1, 'calls': {'pending': {
            'call_id': 'pending', 'auth_id': original['auth_id'], 'state': call_state,
            'usage': {'reported_total_tokens': 90} if call_state == 'settled' else None,
            'reserved_tokens': 35}}}
        target = {}

        result = carry_forward(target, source=source, conversation_id='c1', task='t',
                               source_conversation_id='c1', source_task='t')
        assert result['status'] == 'deferred'
        assert result['carried'] == result['usage_carried'] == 0
        assert result['auth_ids'] == []
        assert result['deferred_auth_ids'] == [original['auth_id']]
        assert result['deferred_reason']
        assert target['authorizations']['entries'] == []
        assert JOURNAL not in target

    source = {}
    original = visual_granted(source, source_ref='answer:acceptance-pending')
    source.update(run_id='e' * 32, status='completed', active_node_ids=[],
        locked_flow={'nodes': [{'node_id': 'audit', 'role': 'auditor'}]},
        node_states={'audit': {'status': 'needs_attention'}})
    target = {}
    projected = carry_forward(target, source=source, conversation_id='c1', task='t',
                              source_conversation_id='c1', source_task='t')
    assert projected['deferred_auth_ids'] == [original['auth_id']]

    # A completed, inactive run can transfer an unknown outcome as a conservative estimate.
    source = {}
    original = visual_granted(source, source_ref='answer:terminal-unknown')
    source.update(run_id='f' * 32, status='completed', active_node_ids=[])
    source[JOURNAL] = {'version': 1, 'calls': {'unknown': {
        'call_id': 'unknown', 'auth_id': original['auth_id'], 'state': 'outcome_unknown',
        'usage': None, 'reserved_tokens': 35}}}
    target = {}
    result = carry_forward(target, source=source, conversation_id='c1', task='t',
                           source_conversation_id='c1', source_task='t')
    inherited = next(row for row in target['authorizations']['entries']
                     if row['auth_id'] == result['auth_ids'][0])
    account = authorization_account(target, inherited)
    assert result['deferred_auth_ids'] == []
    assert account['estimated_used_tokens'] == 35 and account['remaining_tokens'] == 65
