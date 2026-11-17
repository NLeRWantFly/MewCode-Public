import copy
import json
import pytest
from mewcode.experts.authorization_ledger import record_authorization, covers, authorization_block, valid_request
from mewcode.tools.read_authorizations import ReadAuthorizations, ReadAuthorizationsParams
from mewcode.experts.case_runtime import answer_case
from tests.test_recovery_case import record, waiting_case
from tests.test_recovery_case_e2e import AUTH


def add(r, scope, *, kind='elevation_answer', expires_at=None):
    return record_authorization(r, kind=kind, scope=scope, quote='explicit scope', source_ref=str(len(r.get('authorizations', {}).get('entries', []))),
        host_verified=kind not in {'denial', 'user_statement'}, expires_at=expires_at)


@pytest.mark.parametrize('scope,wanted,expected', [
    ({'paths': ['C:/allowed']}, {'paths': ['C:/allowed/child/file']}, 'covered'),
    ({'paths': ['C:/allowed']}, {'paths': ['C:/allowed-other/file']}, 'partially'),
    ({'command_prefix': ['python', '-m', 'pytest']}, {'command_prefix': ['python', '-m', 'pytest', 'tests']}, 'covered'),
    ({'command_prefix': ['python', '-m', 'pytest']}, {'command_prefix': ['python', '-c', 'danger']}, 'partially'),
    ({'hosts': ['example.test']}, {'hosts': ['evil.test']}, 'partially'),
])
def test_scope_boundaries(scope, wanted, expected):
    operation = 'write_outside_workspace' if 'paths' in scope else 'run_command_prefix' if 'command_prefix' in scope else 'network'
    r = {}
    add(r, {**scope, 'operation_classes': [operation]})
    assert covers(r, {'operation_class': operation, 'scope': wanted}).status == expected


def test_expiry_revocation_denial_and_statements_never_override_checks():
    r = {}
    scope = {**AUTH['scope'], 'operation_classes': ['external_send']}
    add(r, scope, expires_at=10)
    assert covers(r, AUTH, now=11).status == 'uncovered'
    add(r, scope, kind='user_statement')
    assert covers(r, AUTH, now=11).status == 'uncovered'
    add(r, scope)
    assert covers(r, AUTH, now=11).status == 'covered'
    r['authorizations']['entries'][-1]['revoked_at'] = 1
    assert covers(r, AUTH, now=11).status == 'uncovered'
    add(r, scope)
    add(r, {**scope, 'epoch': 0, 'node_ids': ['work']}, kind='denial')
    assert covers(r, {**AUTH, 'scope': {**AUTH['scope'], 'epoch': 0, 'node_ids': ['work']}}).status == 'denied'
    assert covers(r, {**AUTH, 'scope': {**AUTH['scope'], 'epoch': 1, 'node_ids': ['work']}}).status == 'covered'


def test_once_approval_never_leaks_other_case_round_or_node():
    r = record()
    c = waiting_case(r)
    c['pending']['authority'] = [copy.deepcopy(AUTH)]
    from mewcode.experts.recovery_case import save_case
    save_case(r, c)
    answer_case(r, c['case_id'], round=0, choice='approve', remember_scope='once')
    assert covers(r, AUTH).status != 'covered'
    wanted = {**AUTH, 'scope': {**AUTH['scope'], 'case_id': c['case_id'], 'round': 1, 'epoch': 0, 'node_ids': ['work']}}
    assert covers(r, wanted).status == 'covered'
    assert covers(r, {**wanted, 'scope': {**wanted['scope'], 'round': 2}}).status != 'covered'
    assert covers(r, {**wanted, 'scope': {**wanted['scope'], 'node_ids': ['other']}}).status != 'covered'
    assert covers(r, {**wanted, 'scope': {**wanted['scope'], 'epoch': 1}}).status != 'covered'


def test_arbitrary_or_wildcard_authority_rejected():
    for request in ['allow everything', {'operation_class': 'unknown', 'scope': {}},
                    {'operation_class': 'network', 'scope': {'hosts': ['*.test']}},
                    {'operation_class': 'external_send', 'scope': {'model_routes': ['test/model']}}]:
        with pytest.raises(ValueError):
            valid_request(request)


@pytest.mark.asyncio
async def test_compact_pinned_summary_full_pagination_keeps_every_fact():
    r = {}
    for n in range(55):
        record_authorization(r, kind='user_statement', scope={}, quote='用户表态' * 60,
            source_ref=f'message:{n}')
    assert len(authorization_block(r)) <= 1200
    tool, offset, seen = ReadAuthorizations(lambda: r), 0, []
    while offset < 55:
        output = await tool.execute(ReadAuthorizationsParams(offset=offset, limit=50))
        value = json.loads(output.output)
        seen.extend(value['entries'])
        assert value['next_offset'] > offset
        offset = value['next_offset']
    assert len(seen) == 55 and seen[-1]['source_ref'] == 'message:54'
