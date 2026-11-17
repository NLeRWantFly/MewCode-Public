"""Regression coverage for decision visibility, actionable rework and quota waits."""
import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from mewcode.agent import Agent, StreamRetryEvent
from mewcode.client import RateLimitError
from mewcode.conversation import ConversationManager
from mewcode.errors import is_transient_error
from mewcode.orchestration import ask_gateway
from mewcode.provider_cooldown import cooldown_remaining, record_cooldown, retry_seconds
from mewcode.tools import create_default_registry
from mewcode.tools.read_authorizations import ReadAuthorizations, ReadAuthorizationsParams
from tests.test_agent import MockLLMClient
from tests.test_automatic_review_repair import audit_result, review_flow
from tests.test_collaboration_integration import orchestrator, run, question
from tests.test_collaboration_mode import RUN_ID, core, accept, delivery
from tests.test_iterative_collaboration import retry_store


def decision_record():
    r = {'run_id': RUN_ID, 'orchestration_mode': 'collaborative', 'collaboration': core()}
    accept(r['collaboration'], 'work')
    r['collaboration']['certificates']['work']['artifacts'] = [
        {'kind': 'file', 'path': 'pilot.json', 'sha256': 'original', 'snapshot_path': 'first'}]
    return r


def test_decision_reuses_identical_content_not_attempt_envelope():
    r = decision_record()
    row = ask_gateway.prepare(r, 'deliver', question())
    ask_gateway.answer(r, row['id'], {'choice': 'a'})
    cert = r['collaboration']['certificates']['work']
    cert.update(attempt_id='new-attempt', report_hash='new-report', read_set={'new': 'bookkeeping'})
    cert['artifacts'][0]['snapshot_path'] = 'second'
    assert ask_gateway.prepare(r, 'deliver', question())['answer'] == {'choice': 'a'}
    cert['artifacts'][0]['sha256'] = 'new-bytes'
    fresh = ask_gateway.prepare(r, 'deliver', question())
    assert fresh['status'] == 'waiting' and fresh['id'] != row['id']
    assert ask_gateway.decision_freshness(r, row)['changed_fields'] == ['inputs']


@pytest.mark.parametrize('change', ['model', 'authorization', 'requirements', 'iteration'])
def test_decision_cannot_expand_to_changed_scope(change):
    r = decision_record()
    row = ask_gateway.prepare(r, 'deliver', question())
    ask_gateway.answer(r, row['id'], {'choice': 'a'})
    if change == 'model':
        r['locked_models'] = {'deliver': {'provider_key': 'other', 'model_id': 'different', 'effort': 'high'}}
    elif change == 'authorization':
        r['authorized_revision'] = 'different'
    elif change == 'iteration':
        r['collaboration']['contracts']['deliver']['iteration'] = {'current_item': 'new-page-batch'}
    else:
        r['collaboration']['entries']['goal']['version'] += 1
    assert not ask_gateway.decision_freshness(r, row)['current']
    assert ask_gateway.prepare(r, 'deliver', question())['status'] == 'waiting'


def test_decision_survives_manifest_reconciliation_without_new_authority():
    from mewcode.kernel.authorization_binding import reconcile_bindings
    r = decision_record()
    r['authorization_manifest'] = {'schema_version': 2, 'approved': True,
        'workspace': 'project', 'nodes': {}, 'scope_digest': 'compiled-scope'}
    row = ask_gateway.prepare(r, 'deliver', question())
    ask_gateway.answer(r, row['id'], {'choice': 'a'})
    r['authorization_manifest'].update(task_id='same-task', task_authorization=None, call_bindings={})
    reconcile_bindings(r)
    assert r['authorization_manifest']['scope_digest'] != 'compiled-scope'
    assert ask_gateway.decision_freshness(r, row)['current']
    assert ask_gateway.prepare(r, 'deliver', question())['answer'] == {'choice': 'a'}


@pytest.mark.parametrize('change', ['scope', 'approval', 'task_authorization', 'grant', 'binding'])
def test_decision_rejects_actual_authority_change_even_if_digest_is_stale(change):
    r = decision_record()
    r['authorization_manifest'] = {'approved': True, 'workspace': 'project', 'nodes': {}, 'scope_digest': 'unchanged'}
    row = ask_gateway.prepare(r, 'deliver', question())
    ask_gateway.answer(r, row['id'], {'choice': 'a'})
    manifest = r['authorization_manifest']
    if change == 'scope':
        manifest['nodes']['deliver'] = {'network_hosts': ['new.example']}
    elif change == 'approval':
        manifest['approved'] = False
    elif change == 'task_authorization':
        manifest.update(task_id='same-task', task_authorization={'version': 'v2'})
    elif change == 'grant':
        r['authorizations'] = {'version': 2, 'entries': [{'auth_id': 'revoked', 'revoked_at': 1}]}
    else:
        manifest['call_bindings'] = {'call': {'auth_id': 'grant', 'node_id': 'deliver'}}
    assert ask_gateway.decision_freshness(r, row)['changed_fields'] == ['authorization']
    assert ask_gateway.prepare(r, 'deliver', question())['status'] == 'waiting'


@pytest.mark.asyncio
async def test_legacy_user_answers_are_visible_as_history_without_granting_authority():
    r = decision_record()
    key = ask_gateway._legacy_identity(r, 'deliver', question())
    row = {'id': key, 'node_id': 'deliver', 'questions': question(), 'status': 'answered',
           'spec_version': 1, 'answer_source': 'user', 'answer': {'choice': 'a'}}
    r['collaboration']['questions'][key] = row
    r['collaboration']['certificates']['work']['report_hash'] = 'changed'
    original = copy.deepcopy(r)
    result = json.loads((await ReadAuthorizations(lambda: r).execute(ReadAuthorizationsParams())).output)
    fact = result['entries'][0]
    assert fact['kind'] == 'host_user_decision' and not fact['enforceable']
    assert not fact['freshness']['current'] and '从未回答' in fact['freshness']['reason']
    detail = json.loads((await ReadAuthorizations(lambda: r).execute(ReadAuthorizationsParams(decision_id=key))).output)
    assert json.loads(detail['text'])['answer'] == {'choice': 'a'}
    assert r == original


def test_auditor_context_keeps_relevant_host_decisions_visible():
    from mewcode.experts.user_decisions import decision_context
    r = decision_record()
    q = [{'name': 'evidence', 'question': 'Accept this pilot evidence?', 'options': ['accept_pilot_only', 'decline']}]
    row = ask_gateway.prepare(r, 'work', q)
    ask_gateway.answer(r, row['id'], {'evidence': 'accept_pilot_only'})
    text = decision_context(r, 'deliver')
    assert row['id'] in text and 'accept_pilot_only' in text and '不涵盖其他模型或全量' in text
    assert decision_context(r, 'unrelated') == ''
    assert len(text) <= 2400


def test_review_requires_direct_input_decisions_without_credential_answers():
    from mewcode.experts.user_decisions import review_decisions
    r = decision_record()
    q = [{'name': 'evidence', 'question': 'Accept this pilot evidence?', 'options': ['accept_pilot_only', 'decline']}]
    row = ask_gateway.prepare(r, 'work', q)
    ask_gateway.answer(r, row['id'], {'evidence': 'accept_pilot_only'})
    secret = ask_gateway.prepare(r, 'work', [{'name': 'credential', 'question': 'Provide API key'}])
    ask_gateway.answer(r, secret['id'], {'credential': 'synthetic-secret-value'})
    original = copy.deepcopy(r)
    assert set(review_decisions(r, 'deliver')) == {row['id']}
    assert review_decisions(r, 'unrelated') == {}
    assert r == original


@pytest.mark.asyncio
@pytest.mark.parametrize('resolution', ['user_decision', 'capability_gap', 'unclassified'])
async def test_unfixable_findings_preserve_producer_and_stop_rework(tmp_path, resolution):
    spec = review_flow()
    store = retry_store(tmp_path, spec)
    calls = []
    async def execute(node, *_):
        calls.append(node.node_id)
        if node.node_id != 'audit':
            return delivery()
        result = audit_result(False)
        result.verdict['issues'][0]['resolution'] = resolution
        return result
    async def review(*_):
        pytest.fail('blocked audit must not reach downstream')
    result = await run(orchestrator(store, tmp_path, execute, review), store, spec)
    assert result.status == 'blocked' and calls == ['work', 'audit']
    value = store.read_collaboration(RUN_ID)
    assert not value.get('integration_reworks')
    assert value['review_followups']['review:audit']['issues'][0]['route'] == resolution
    assert not value['certificates']['work'].get('invalidated')


@pytest.mark.asyncio
async def test_nonblocking_downstream_work_advances_and_reaches_its_target(tmp_path):
    spec = review_flow()
    store = retry_store(tmp_path, spec)
    calls = []
    async def execute(node, model, prompt, *_):
        calls.append(node.node_id)
        if node.node_id == 'audit':
            result = audit_result(True)
            result.verdict['issues'] = [{'issue_id': 'later', 'description': 'next-stage-formula',
                'resolution': 'downstream', 'blocking': False, 'origin': 'out_of_scope', 'target_node_id': 'deliver'}]
            return result
        if node.node_id == 'deliver':
            assert 'next-stage-formula' in prompt
        return delivery()
    async def review(*_):
        return {'passed': True, 'evidence': ['checked']}
    result = await run(orchestrator(store, tmp_path, execute, review), store, spec)
    assert result.status == 'completed' and calls == ['work', 'audit', 'deliver']


@pytest.mark.asyncio
@pytest.mark.parametrize('headless', [False, True])
async def test_long_rate_limit_makes_one_request_and_no_short_sleep(tmp_path, monkeypatch, headless):
    class Client(MockLLMClient):
        calls = 0
        async def stream(self, *_args, **_kwargs):
            self.calls += 1
            raise RateLimitError('Rate limited. Retry after 1440902s.', retry_after=1440902)
            yield
    client = Client([])
    agent = Agent(client, create_default_registry(), 'anthropic', work_dir=str(tmp_path))
    conversation = ConversationManager()
    conversation.add_user_message('continue')
    with pytest.raises(RateLimitError):
        if headless:
            await agent.run_to_completion('continue', conversation)
        else:
            async for event in agent.run(conversation):
                assert not isinstance(event, StreamRetryEvent)
    assert client.calls == 1


def test_cooldown_is_route_bound_persistent_and_accepts_http_dates():
    model = {'provider_key': 'p', 'model_id': 'm', 'effort': 'max'}
    r = {}
    record_cooldown(r, model, 1440902, now=100)
    r = json.loads(json.dumps(r))
    assert cooldown_remaining(r, model, now=101) == 1440901
    assert cooldown_remaining(r, {**model, 'effort': 'high'}, now=101) == 1440901
    assert cooldown_remaining(r, {**model, 'model_id': 'gemini'}, now=101) == 0
    assert cooldown_remaining(r, model, now=1500000) == 0
    assert retry_seconds('Thu, 01 Jan 1970 00:02:00 GMT', now=100) == 20
    assert not is_transient_error(RateLimitError('rate limited', retry_after=86400))


def test_user_decision_projection_keeps_choices_but_hides_credential_answers():
    from mewcode.experts.user_decisions import public_decision
    secret = 'synthetic-credential-material-for-test'
    row = {'questions': [{'name': 'choice', 'question': '请提供完整 API key',
                         'options': [{'value': 'provide_key'}]}], 'answer': {'choice': secret + ' key是这个'}}
    safe = public_decision(row)
    assert secret not in json.dumps(safe) and safe['sensitive_details_redacted']
    assert row['answer']['choice'].startswith(secret)
    row['answer']['choice'] = 'provide_key'
    assert public_decision(row)['answer']['choice'] == 'provide_key'
