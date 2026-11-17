import asyncio
import base64
import copy
import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from mewcode.agent import AskUserRequest
from mewcode.experts.model_receipts import audit_sources, bind_recorder, request_receipt
from mewcode.experts.waits import wait_for_interaction
from mewcode.model_registry import ModelRef
from mewcode.orchestration import ask_gateway
from mewcode.tools.submit_verdict import SubmitVerdictParams, SubmitVerdictTool
from mewcode.usage.ledger import UsageLedger
from mewcode.usage.models import RequestUsage
from tests.test_automatic_review_repair import audit_result, review_flow
from tests.test_collaboration_integration import orchestrator, run
from tests.test_collaboration_mode import RUN_ID, delivery
from tests.test_iterative_collaboration import retry_store


def decision_issue():
    return {'issue_id': 'evidence-1', 'resolution': 'user_decision', 'blocking': True,
        'description': 'Pilot evidence requires a scoped choice.', 'acceptance_test': 'Review the saved choice.',
        'decision': {'question': 'Accept the specified evidence for these three pilot pages?',
            'scope': 'Only the current pilot; no full upload, permission grant, or factual verification.',
            'options': [{'value': 'pilot_only', 'label': 'Accept for the pilot', 'description': 'Review this pilot only.', 'effect': 'review'},
                        {'value': 'revise', 'label': 'Request a revision', 'description': 'Use the requirements revision flow.', 'effect': 'request_revision'},
                        {'value': 'defer', 'label': 'Defer', 'description': 'Keep the current unresolved state.', 'effect': 'defer'}]}}


@pytest.mark.asyncio
async def test_verdict_requires_current_host_sources_and_answerable_decision():
    tool = SubmitVerdictTool()
    tool.required_sources = {'@audit_requirements': 'req-v1', '@model_receipts': 'receipts-v1'}
    issue = decision_issue()
    params = SubmitVerdictParams(verdict='NEEDS_ATTENTION', issues=[issue])
    assert (await tool.execute(params)).is_error
    params = params.model_copy(update={'reviewed_sources': dict(tool.required_sources)})
    assert not (await tool.execute(params)).is_error
    assert tool._holder.read()['reviewed_sources'] == tool.required_sources
    del issue['decision']
    params = SubmitVerdictParams(verdict='NEEDS_ATTENTION', issues=[issue], reviewed_sources=tool.required_sources)
    assert (await tool.execute(params)).is_error
    assert tool._holder.read() is None


@pytest.mark.asyncio
@pytest.mark.parametrize('selection', ['pilot_only', 'revise', 'defer'])
async def test_real_question_answer_rechecks_only_audit_without_granting_authority(tmp_path, selection):
    spec, calls, prompts = review_flow(), [], []
    store = retry_store(tmp_path, spec)
    original = store.load(RUN_ID)
    producer = {}
    async def execute(node, *_):
        calls.append(node.node_id)
        if node.node_id == 'audit':
            producer.setdefault('certificate', copy.deepcopy(store.read_collaboration(RUN_ID)['certificates']['work']))
            result = audit_result(calls.count('audit') > 1)
            if result.verdict['verdict'] != 'PASS':
                result.verdict['issues'] = [decision_issue()]
            return result
        return delivery('fixed original output')
    async def review(*_):
        return {'passed': True, 'evidence': ['checked']}
    host = orchestrator(store, tmp_path, execute, review)
    async def prepare_choices(**_):
        pytest.fail('host-bound review outcomes must not be rewritten by the candidate generator')
    session = SimpleNamespace(interaction_choice_preparer=prepare_choices)
    async def decide(node, request_id, questions):
        request = AskUserRequest(request_id, 'ReviewDecision', questions, asyncio.get_running_loop().create_future())
        async def emit(event):
            if event['type'] == 'ask_user':
                prompts.append(event)
                assert questions[0]['allow_other'] is False and questions[0]['requires_user_answer'] is True
                # Exercise the actual durable host wait. The test user chooses
                # the underlying source value, just as an unbundled UI does.
                request.future.set_result({questions[0]['name']: selection})
        answer = await wait_for_interaction(request, kind='ask_user', session=session, emit=emit,
            store=store, run_id=RUN_ID, stage_key=node.node_id, role=node.role)
        return {'question_id': request.collaboration_question_id, 'answer': answer}
    host.collaboration_decide = decide
    result = await run(host, store, spec)
    assert len(prompts) == 1
    assert calls == (['work', 'audit', 'audit', 'deliver'] if selection == 'pilot_only' else ['work', 'audit'])
    assert result.status == ('completed' if selection == 'pilot_only' else 'blocked')
    saved = store.load(RUN_ID)
    assert saved['collaboration']['certificates']['work'] == producer['certificate']
    assert saved['collaboration']['entries'] == original['collaboration']['entries']
    for field in ('authorizations', 'authorization_manifest', 'locked_models'):
        assert saved.get(field) == original.get(field)
    choices = list(saved['collaboration']['review_decision_answers'].values())
    assert len(choices) == 1 and choices[0]['answer'] == {next(iter(choices[0]['answer'])): selection}
    if selection != 'pilot_only':
        await run(host, store, spec)
        assert len(prompts) == 1 and calls == ['work', 'audit']


@pytest.mark.asyncio
async def test_unresolved_after_answer_does_not_reask_or_loop(tmp_path):
    spec, calls, asked = review_flow(), [], []
    store = retry_store(tmp_path, spec)
    async def execute(node, *_):
        calls.append(node.node_id)
        if node.node_id != 'audit':
            return delivery()
        result = audit_result(False)
        result.verdict['issues'] = [decision_issue()]
        return result
    async def review(*_):
        pytest.fail('must not reach downstream')
    host = orchestrator(store, tmp_path, execute, review)
    async def decide(node, request_id, questions):
        answer = {questions[0]['name']: 'pilot_only'}
        rows = []
        def save(record):
            row = ask_gateway.prepare(record, node.node_id, questions)
            ask_gateway.answer(record, row['id'], answer)
            rows.append(row)
        store.mutate(RUN_ID, save, durable=True)
        asked.append(request_id)
        return {'question_id': rows[0]['id'], 'answer': answer}
    host.collaboration_decide = decide
    result = await run(host, store, spec)
    assert result.status == 'blocked' and calls == ['work', 'audit', 'audit'] and len(asked) == 1
    await run(host, store, spec)
    assert calls == ['work', 'audit', 'audit'] and len(asked) == 1


@pytest.mark.asyncio
async def test_saved_question_resumes_without_repeating_producer(tmp_path):
    spec, calls = review_flow(), []
    store = retry_store(tmp_path, spec)
    async def execute(node, *_):
        calls.append(node.node_id)
        if node.node_id == 'audit':
            result = audit_result(calls.count('audit') > 1)
            if result.verdict['issues']:
                result.verdict['issues'] = [decision_issue()]
            return result
        return delivery()
    async def review(*_):
        return {'passed': True, 'evidence': ['checked']}
    host = orchestrator(store, tmp_path, execute, review)
    first = await run(host, store, spec)
    assert first.status == 'blocked' and calls == ['work', 'audit']
    pending = store.load(RUN_ID)['node_states']['audit']['pending_interaction']
    assert pending['tool'] == 'ReviewDecision'
    assert len([q for q in store.read_collaboration(RUN_ID)['questions'].values() if q['status'] == 'waiting']) == 1
    # Simulate a newly attached host callback consuming that same persisted question.
    async def decide(node, request_id, questions):
        rows = []
        answer = {questions[0]['name']: 'pilot_only'}
        def save(record):
            from mewcode.experts.recovery_state import finish_interaction
            row = ask_gateway.prepare(record, node.node_id, questions)
            ask_gateway.answer(record, row['id'], answer)
            finish_interaction(record, 'node_states', node.node_id, request_id)
            rows.append(row)
        store.mutate(RUN_ID, save, durable=True)
        return {'question_id': rows[0]['id'], 'answer': answer}
    host.collaboration_decide = decide
    assert (await run(host, store, spec)).status == 'completed'
    assert calls == ['work', 'audit', 'audit', 'deliver']


def test_request_receipt_has_actual_parameters_and_pixel_hashes_but_no_secrets():
    image = b'synthetic image bytes'
    client = SimpleNamespace(effort='high', context_window=100000, max_output_tokens=4000)
    args = {'model': 'route-alias', 'reasoning': {'effort': 'high'}, 'max_output_tokens': 2000,
        'input': [{'role': 'user', 'content': [{'type': 'input_image',
            'image_url': 'data:image/png;base64,' + base64.b64encode(image).decode()},
            {'type': 'input_text', 'text': 'private document content'}]}],
        'extra_headers': {'Authorization': 'Bearer synthetic-secret'}}
    result = request_receipt(args, client)
    assert result['sent_effort'] == 'high'
    assert result['image_inputs'][0]['sha256'] == hashlib.sha256(image).hexdigest()
    assert result['limits']['sent_output_tokens'] == 2000
    assert not result['limits']['provider_maximum_verified']
    assert 'private document' not in json.dumps(result) and 'synthetic-secret' not in json.dumps(result)
    del args['reasoning']; del args['max_output_tokens']
    omitted = request_receipt(args, client)
    assert omitted['configured_effort'] == 'high' and omitted['sent_effort'] is None
    assert omitted['limits']['sent_output_tokens'] is None


def test_scoped_receipts_and_legacy_candidates_stay_distinct(tmp_path):
    ledger = UsageLedger(tmp_path)
    now = datetime.now(timezone.utc)
    record = {'run_id': 'run-a', 'conversation_id': 'session', 'attempt': 4,
        'collaboration': {'spec_version': 1, 'entries': {}, 'contracts': {'audit': {'depends_on': ['work']}}},
        'node_states': {'work': {'input_revision': 'input-v1', 'model_spans': [
            {'started_at': now.timestamp() - 1, 'completed_at': now.timestamp() + 1}]}}}
    def usage(**kwargs):
        return RequestUsage(session_id='session', occurred_at=now, configured_model='route', response_model='returned', **kwargs)
    recorder = bind_recorder(ledger.append, record, 'work', 'input-v1', ModelRef('p', 'route', 'high'))
    recorder(usage())
    ledger.append(usage())  # Historic record lacks an execution binding.
    ledger.append(usage(metadata={'execution_scope': {'run_id': 'other-run', 'node_id': 'work'}}))
    ledger.append(usage(metadata={'execution_scope': {'run_id': 'run-a', 'node_id': 'unrelated'}}))
    before = copy.deepcopy(record)
    sources = audit_sources(record, 'audit', ledger)
    calls = sources['@model_receipts:work']['calls']
    assert [c['binding'] for c in calls] == ['exact_scope', 'legacy_candidate']
    assert all(c['response_model'] == 'returned' for c in calls)
    assert len(sources['@model_receipts']['nodes']) == 1
    assert record == before
    assert 'credential_fingerprint' not in json.dumps(sources)


def test_historical_capture_gap_is_not_confused_with_later_recording_capability(tmp_path):
    ledger = UsageLedger(tmp_path)
    now = datetime.now(timezone.utc)
    record = {'run_id': 'run-a', 'conversation_id': 'session',
        'collaboration': {'entries': {}, 'contracts': {'audit': {'depends_on': ['work']}}},
        'node_states': {'work': {'input_revision': 'old-input', 'model_spans': [
            {'started_at': now.timestamp() - 1, 'completed_at': now.timestamp() + 1}]}}}
    ledger.append(RequestUsage(session_id='session', occurred_at=now, configured_model='old-route'))
    later = RequestUsage(session_id='session', configured_model='new-route', metadata={
        'execution_scope': {'run_id': 'run-a', 'node_id': 'audit', 'input_revision': 'new-input'},
        'model_call': {'request': {'sdk_request_sha256': 'a' * 64}}})
    ledger.append(later)
    sources = audit_sources(record, 'audit', ledger)
    summary = sources['@model_receipts']
    coverage = summary['capture_coverage']['work']
    assert coverage['availability'] == 'not_recorded_in_examined_host_records'
    assert coverage['legacy_candidates'] == coverage['examined_calls'] == 1
    assert coverage['saved_request_summaries'] == coverage['current_input_bound_summaries'] == 0
    assert summary['first_saved_request_summary_at'] is not None
    assert summary['recording_contract']['unrecorded_history_reconstructable'] is False
    assert summary['recording_contract']['remote_provider_records'] == 'not_queried'
    assert sources['@model_receipts:work']['calls'][0]['binding'] == 'legacy_candidate'


def test_saved_summary_with_wrong_input_never_becomes_current_evidence(tmp_path):
    ledger = UsageLedger(tmp_path)
    record = {'run_id': 'run-a', 'conversation_id': 'session',
        'collaboration': {'entries': {}, 'contracts': {'audit': {'depends_on': ['work']}}},
        'node_states': {'work': {'input_revision': 'current'}}}
    for revision in ('old', 'current'):
        ledger.append(RequestUsage(session_id='session', configured_model='route', metadata={
            'execution_scope': {'run_id': 'run-a', 'node_id': 'work', 'input_revision': revision},
            'model_call': {'request': {'sdk_request_sha256': 'a' * 64}}}))
        coverage = audit_sources(record, 'audit', ledger)['@model_receipts']['capture_coverage']['work']
        assert coverage['current_input_bound_summaries'] == int(revision == 'current')
        assert coverage['availability'] == ('available_for_review' if revision == 'current' else 'candidate_requires_binding')


def test_unreadable_receipt_store_is_not_reported_as_no_historical_evidence(tmp_path):
    path = tmp_path / 'corrupt.sqlite3'
    path.write_text('not a database')
    record = {'conversation_id': 'session', 'collaboration': {'entries': {}, 'contracts': {}}}
    summary = audit_sources(record, 'audit', SimpleNamespace(path=path))['@model_receipts']
    assert summary['ledger_available'] is False
    assert summary['error'] == 'DatabaseError' and summary['capture_coverage'] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['openai', 'openai-compat'])
async def test_stream_persists_submitted_parameters_and_raw_provider_claims(monkeypatch, protocol):
    import mewcode.client as client_module
    from mewcode.config import ProviderConfig
    from mewcode.conversation import ConversationManager
    from mewcode.tools.base import StreamEnd
    sent, receipts = {}, []

    async def create(**kwargs):
        sent.update(kwargs)
        async def events():
            if protocol == 'openai':
                yield SimpleNamespace(type='response.completed', response=SimpleNamespace(
                    id='response-1', model='returned-alias', status='completed', reasoning={'effort': 'high'},
                    usage=SimpleNamespace(input_tokens=10, output_tokens=3, input_tokens_details=None)))
            else:
                yield SimpleNamespace(id='response-1', model='returned-alias', choices=[SimpleNamespace(
                    index=0, delta=SimpleNamespace(content='done', tool_calls=None), finish_reason='length')], usage=None)
                yield SimpleNamespace(id='response-1', model='returned-alias', choices=[],
                    usage=SimpleNamespace(prompt_tokens=10, completion_tokens=3, prompt_tokens_details=None))
        return events()
    fake = SimpleNamespace(responses=SimpleNamespace(create=create),
                           chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(client_module, 'AsyncOpenAI', lambda **kwargs: fake)
    cls = client_module.OpenAIClient if protocol == 'openai' else client_module.OpenAICompatClient
    config = ProviderConfig(name='test-relay', protocol=protocol,
        base_url='https://relay.example/v1', model='gpt-5', api_key='synthetic-credential')
    client = cls(config)
    client.configure_usage_tracking(config, endpoint='/v1/responses' if protocol == 'openai' else '/v1/chat/completions')
    client.set_effort('high')
    client.set_usage_recorder(receipts.append)
    conversation = ConversationManager()
    conversation.add_user_message('private source text')
    # Tool JSON Schema can itself define a property named "type", whose
    # value is another schema object rather than a content-block discriminator.
    schema = {'type': 'function', 'name': 'Example', 'parameters': {'type': 'object',
        'properties': {'type': {'type': 'string'}, 'nested': {'type': 'object'}},
        'examples': [{'type': 'input_image', 'image_url': 'data:image/png;base64,bm90LXBpeGVscw=='}]}}
    events = [event async for event in client.stream(conversation, tools=[schema])]
    assert len([event for event in events if isinstance(event, StreamEnd)]) == len(receipts) == 1
    receipt = receipts[0]
    call = receipt.metadata['model_call']
    assert call['request'] == request_receipt(sent, client)
    assert call['request']['sent_effort'] == 'high'
    assert call['request']['image_inputs'] == []
    assert receipt.response_model == call['response']['model'] == 'returned-alias'
    assert call['response']['finish_reason'] == ('completed' if protocol == 'openai' else 'length')
    assert call['response']['effort'] == ('high' if protocol == 'openai' else None)
    assert not call['response']['effort_execution_verified']
    assert receipt.evidence.value == 'relay_reported'
    assert 'private source' not in json.dumps(call) and 'synthetic-credential' not in json.dumps(call)


def test_answer_cannot_recheck_after_frozen_requirements_change(tmp_path):
    from mewcode.orchestration.review_decisions import question_for, source_basis, record_answer
    record = retry_store(tmp_path, review_flow()).load(RUN_ID)
    issue = decision_issue()
    question, effects = question_for(issue)
    source = source_basis(record, 'audit')
    row = ask_gateway.prepare(record, 'audit', [question])
    answer = {question['name']: 'pilot_only'}
    ask_gateway.answer(record, row['id'], answer)
    contract = record['collaboration']['contracts']['audit']
    record['collaboration']['entries'][contract['requirement_ids'][0]]['version'] += 1
    with pytest.raises(ValueError, match='已经变化'):
        record_answer(record, node_id='audit', issue_id=issue['issue_id'], source=source,
            question_id=row['id'], answer=answer, effects=effects)
    assert not record['collaboration'].get('review_decision_answers')


@pytest.mark.asyncio
async def test_legacy_capability_gap_gets_safe_choices_without_replaying_cached_audit(tmp_path):
    spec, calls = review_flow(), []
    store = retry_store(tmp_path, spec)
    async def execute(node, *_):
        calls.append(node.node_id)
        if node.node_id != 'audit':
            return delivery()
        result = audit_result(False)
        result.verdict['issues'] = [{'issue_id': 'missing-receipt', 'resolution': 'capability_gap',
            'blocking': True, 'description': 'Provider does not expose the required receipt.'}]
        return result
    async def review(*_):
        pytest.fail('must not advance through a missing required receipt')
    host = orchestrator(store, tmp_path, execute, review)
    assert (await run(host, store, spec)).status == 'blocked'
    pending = store.load(RUN_ID)['node_states']['audit']['pending_interaction']
    assert pending['tool'] == 'ReviewDecision'
    assert {option['value'] for option in pending['questions'][0]['options']} == {'request_revision', 'defer'}
    certificate = copy.deepcopy(store.read_collaboration(RUN_ID)['certificates']['work'])
    assert (await run(host, store, spec)).status == 'blocked'
    assert calls == ['work', 'audit']
    assert store.read_collaboration(RUN_ID)['certificates']['work'] == certificate
    assert len(store.read_collaboration(RUN_ID)['questions']) == 1


@pytest.mark.asyncio
async def test_generated_legacy_card_is_superseded_before_real_wait_without_replay(tmp_path):
    from mewcode.experts.recovery_state import save_interaction
    from mewcode.kernel.events import digest
    from mewcode.orchestration.review_decisions import source_basis
    spec, calls, shown = review_flow(), [], []
    store = retry_store(tmp_path, spec)
    async def execute(node, *_):
        calls.append(node.node_id)
        if node.node_id != 'audit':
            return delivery()
        result = audit_result(False)
        result.verdict['issues'] = [decision_issue()]
        return result
    async def review(*_):
        pytest.fail('must not advance without a passing audit')
    host = orchestrator(store, tmp_path, execute, review)
    assert (await run(host, store, spec)).status == 'blocked'
    old = {}
    def legacy(record):
        stage = record['node_states']['audit']
        pending = stage.pop('pending_interaction')
        source = source_basis(record, 'audit')
        pending['request_id'] = 'review_' + digest([RUN_ID, 'audit', 'evidence-1', source])[:24]
        pending['source_questions'] = copy.deepcopy(pending['questions'])
        for key in ('type', 'allow_other', 'requires_user_answer'):
            pending['source_questions'][0].pop(key)
        pending['questions'] = copy.deepcopy(pending['source_questions'])
        pending['questions'][0]['options'].append({'value': 'manual', 'label': 'Other'})
        pending['choice_bundle'] = {'id': 'legacy-generated-card'}
        old.update(question=ask_gateway.prepare(record, 'audit', pending['questions']),
                   pending=save_interaction(record, 'node_states', 'audit', pending))
    store.mutate(RUN_ID, legacy, durable=True)
    before = store.load(RUN_ID)
    async def decide(node, request_id, questions):
        assert request_id != old['pending']['request_id']
        request = AskUserRequest(request_id, 'ReviewDecision', questions, asyncio.get_running_loop().create_future())
        async def emit(event):
            if event['type'] == 'ask_user':
                shown.append(request_id)
                assert len(questions[0]['options']) == 3 and not questions[0]['allow_other']
                request.future.set_result({questions[0]['name']: 'defer'})
        answer = await wait_for_interaction(request, kind='ask_user', session=SimpleNamespace(), emit=emit,
            store=store, run_id=RUN_ID, stage_key=node.node_id, role=node.role, resumed=True)
        return {'question_id': request.collaboration_question_id, 'answer': answer}
    host.collaboration_decide = decide
    assert (await run(host, store, spec)).status == 'blocked'
    assert (await run(host, store, spec)).status == 'blocked'
    after = store.load(RUN_ID)
    assert calls == ['work', 'audit'] and len(shown) == 1
    assert after['collaboration']['certificates'] == before['collaboration']['certificates']
    assert after['collaboration']['entries'] == before['collaboration']['entries']
    history = after['node_states']['audit']['interaction_history']
    assert any(row['status'] == 'superseded' and not row['answered'] and row['pending'] == old['pending'] for row in history)
    assert after['collaboration']['questions'][old['question']['id']]['status'] == 'superseded'
    with pytest.raises(ValueError, match='被替代'):
        ask_gateway.answer(after, old['question']['id'], {old['question']['questions'][0]['name']: 'manual'})


@pytest.mark.parametrize('conflict', ['different_source', 'already_answered', 'unrelated_request'])
def test_legacy_card_upgrade_preserves_conflicting_or_answered_questions(tmp_path, conflict):
    from mewcode.experts.recovery_state import save_interaction
    from mewcode.kernel.events import digest
    from mewcode.orchestration.review_decisions import question_for, source_basis, prepare_interaction
    record = retry_store(tmp_path, review_flow()).load(RUN_ID)
    issue = decision_issue()
    question, _ = question_for(issue)
    source = source_basis(record, 'audit')
    pending = {'request_id': 'review_' + digest([RUN_ID, 'audit', issue['issue_id'], source])[:24],
        'kind': 'ask_user', 'tool': 'ReviewDecision', 'questions': [copy.deepcopy(question)]}
    row = ask_gateway.prepare(record, 'audit', pending['questions'])
    if conflict == 'different_source':
        pending['questions'][0]['context'] = 'A different decision scope'
    elif conflict == 'already_answered':
        ask_gateway.answer(record, row['id'], {question['name']: 'defer'})
    else:
        pending['request_id'] = 'unrelated-request'
    save_interaction(record, 'node_states', 'audit', pending)
    before = copy.deepcopy(record)
    with pytest.raises(ValueError):
        prepare_interaction(record, node_id='audit', issue_id=issue['issue_id'], source=source, question=question)
    assert record == before
