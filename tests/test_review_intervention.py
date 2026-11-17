import copy

import pytest

from tests.test_automatic_review_repair import audit_result, review_flow
from tests.test_collaboration_integration import orchestrator, run
from tests.test_collaboration_mode import RUN_ID, delivery
from tests.test_iterative_collaboration import retry_store


def investigation_plan():
    return {'action': 'investigate', 'diagnosis': 'Inspect the actual receipt before deciding it is unavailable.',
        'evidence_refs': ['issue:R-1'], 'investigations': [{
            'label': 'Inspect saved receipts', 'instructions': 'Read original evidence and distinguish missing report fields from missing service evidence.',
            'input_refs': ['work'], 'deliverables': ['Receipt findings with source references'],
            'forbidden_changes': ['Do not modify the original output or call the business service again.'],
            'verification': 'Every finding cites an existing receipt; unknown identity remains unknown.'}]}


@pytest.mark.asyncio
async def test_intervention_supersedes_unanswered_card_investigates_and_rechecks_without_producer_replay(tmp_path):
    spec, calls, plans = review_flow(), [], []
    store = retry_store(tmp_path, spec)
    async def execute(node, model, prompt, policy, *_):
        calls.append(node.node_id)
        if '::review-' in node.node_id:
            assert policy.parallel_readonly and '@review_intervention' in policy.stage_inputs
            assert not node.capabilities['write_paths'] and not node.capabilities['command_execution']
            return delivery('Original receipt contains the required response identifier; source: saved receipt 1.')
        if node.node_id == 'audit':
            result = audit_result(calls.count('audit') > 1)
            if result.verdict['issues']:
                result.verdict['issues'][0]['resolution'] = 'capability_gap'
            else:
                assert any('::review-' in key for key in policy.stage_inputs)
                assert 'work' in policy.stage_inputs
            return result
        return delivery('unchanged producer output')
    async def review(*_):
        return {'passed': True, 'evidence': ['checked fixed evidence']}
    host = orchestrator(store, tmp_path, execute, review)
    assert (await run(host, store, spec)).status == 'blocked'
    before = store.load(RUN_ID)
    old_card = before['node_states']['audit']['pending_interaction']
    async def coordinate(model, payload, system):
        plans.append(payload)
        if 'intervention_plan' in payload:
            return {'passed': True, 'evidence': ['issue:R-1'], 'reason': 'Read-only investigation stays in the original scope.'}
        return investigation_plan()
    host.collaboration_coordinate = coordinate
    result = await run(host, store, spec)
    assert result.status == 'completed', result.final_summary
    after = store.load(RUN_ID)
    assert calls.count('work') == 1 and calls.count('audit') == 2 and calls.count('deliver') == 1
    assert len([key for key in calls if '::review-' in key]) == 1
    assert len(plans) == 2
    assert after['collaboration']['certificates']['work'] == before['collaboration']['certificates']['work']
    assert after['collaboration']['entries'] == before['collaboration']['entries']
    assert after.get('authorizations') == before.get('authorizations')
    history = after['node_states']['audit']['interaction_history']
    assert any(row['pending'] == old_card and row['status'] == 'superseded' and not row['answered'] for row in history)
    assert after['locked_flow'] == before['locked_flow']
    assert any(row['kind'] == 'review_intervention' for row in after['collaboration']['graph_publications'].values())


@pytest.mark.asyncio
async def test_rejected_intervention_does_not_publish_or_invalidate_existing_work(tmp_path):
    spec, calls = review_flow(), []
    store = retry_store(tmp_path, spec)
    async def execute(node, *_):
        calls.append(node.node_id)
        if node.node_id == 'audit':
            result = audit_result(False)
            result.verdict['issues'][0]['resolution'] = 'capability_gap'
            return result
        return delivery()
    async def review(*_):
        pytest.fail('no downstream integration')
    host = orchestrator(store, tmp_path, execute, review)
    await run(host, store, spec)
    before = copy.deepcopy(store.read_collaboration(RUN_ID))
    proposals = []
    async def coordinate(model, payload, system):
        if 'intervention_plan' in payload:
            return {'passed': False, 'evidence': ['issue:R-1'], 'reason': 'No new investigation value.'}
        proposals.append(payload)
        return investigation_plan()
    host.collaboration_coordinate = coordinate
    assert (await run(host, store, spec)).status == 'blocked'
    assert (await run(host, store, spec)).status == 'blocked'
    after = store.read_collaboration(RUN_ID)
    assert calls == ['work', 'audit'] and not after.get('graph_publications')
    assert after['contracts'] == before['contracts'] and after['certificates'] == before['certificates']
    assert len(proposals) == 2 and proposals[1]['revision_feedback']['review']['passed'] is False


def intervention_fixture(tmp_path, coordinate):
    from mewcode.kernel import collaboration as ledger
    from mewcode.orchestration.collab_runtime import CollaborationRuntime
    from tests.test_collaboration_mode import CATALOG, passing_report
    spec = review_flow()
    store = retry_store(tmp_path, spec)
    runtime = CollaborationRuntime(store=store, run_id=RUN_ID, flow=spec, catalog=CATALOG,
        work_dir=tmp_path, coordinate=coordinate)
    def settle(record):
        value = ledger.state(record)
        for key in ['work', 'audit']:
            attempt = ledger.dispatch(value, key)
            value['attempts'][attempt['id']]['delivery'] = {'status': 'completed', 'text': key}
            ledger.complete(value, attempt['id'], passing_report(value, key))
    store.mutate(RUN_ID, settle, durable=True)
    issues = audit_result(False).verdict['issues']
    issues[0]['resolution'] = 'capability_gap'
    return runtime, issues


@pytest.mark.asyncio
async def test_investigation_result_reaches_coordinator_then_targeted_repair(tmp_path):
    from mewcode.kernel import collaboration as ledger
    from mewcode.orchestration.review_intervention import intervene
    from tests.test_collaboration_mode import passing_report
    payloads = []
    async def coordinate(model, payload, system):
        if 'intervention_plan' in payload:
            return {'passed': True, 'evidence': ['issue:R-1'], 'reason': 'Within the existing task.'}
        payloads.append(payload)
        if len(payloads) == 1:
            return investigation_plan()
        result = next(row['investigation_result'] for row in payload['evidence_index'].values()
                      if isinstance(row, dict) and 'investigation_result' in row)
        assert result['text'] == 'The saved source proves the report omitted a field.'
        assert payload['previous_interventions'][0]['status'] == 'applied'
        return {'action': 'repair', 'diagnosis': 'Copy the existing verified field.',
            'evidence_refs': ['issue:R-1'], 'repairs': [{'node_id': 'work',
                'instructions': 'Copy the existing verified field, without rerunning the service.',
                'verification': 'Compare against the saved original receipt.'}]}
    runtime, issues = intervention_fixture(tmp_path, coordinate)
    assert await intervene(runtime, runtime.flow.node_map['audit'], issues) == 'scheduled'
    original = copy.deepcopy(runtime.read()['entries'])
    def complete_investigation(record):
        value = ledger.state(record)
        child = next(key for key in value['contracts'] if '::review-' in key)
        for key in [child, 'audit']:
            attempt = ledger.dispatch(value, key)
            value['attempts'][attempt['id']]['delivery'] = {'status': 'completed', 'text': 'The saved source proves the report omitted a field.'}
            ledger.complete(value, attempt['id'], passing_report(value, key))
    runtime.store.mutate(RUN_ID, complete_investigation, durable=True)
    assert await intervene(runtime, runtime.flow.node_map['audit'], issues) == 'scheduled'
    value = runtime.read()
    assert value['entries'] == original and not ledger.valid_certificate(value, 'work')
    assert 'saved original receipt' in value['rework_instructions']['work']['instructions']
    assert value['retry_generations']['work'] == 1
    assert len(payloads) == 2


@pytest.mark.asyncio
async def test_specific_business_choice_replaces_old_unanswered_card_and_resumes_without_replanning(tmp_path):
    from mewcode.orchestration.review_intervention import intervene
    from tests.test_review_evidence_decisions import decision_issue
    calls = []
    async def coordinate(model, payload, system):
        calls.append(payload)
        if 'intervention_plan' in payload:
            return {'passed': True, 'evidence': ['issue:R-1'], 'reason': 'The remaining choice belongs to the user.'}
        return {'action': 'user_decision', 'diagnosis': 'The service cannot supply historical telemetry.',
            'evidence_refs': ['issue:R-1'], 'decision': decision_issue()['decision']}
    runtime, issues = intervention_fixture(tmp_path, coordinate)
    assert await runtime.resolve_review_decisions(runtime.flow.node_map['audit'], issues) == 'user_decision'
    before = runtime.store.load(RUN_ID)
    outcome = await intervene(runtime, runtime.flow.node_map['audit'], issues)
    assert await runtime.resolve_review_decisions(runtime.flow.node_map['audit'], outcome['issues']) == 'user_decision'
    again = await intervene(runtime, runtime.flow.node_map['audit'], issues)
    assert again == outcome and len(calls) == 2
    after = runtime.store.load(RUN_ID)
    assert after['node_states']['audit']['pending_interaction']['questions'][0]['question'] == decision_issue()['decision']['question']
    assert after['node_states']['audit']['interaction_history'][-1]['answered'] is False
    assert after['collaboration']['certificates'] == before['collaboration']['certificates']
    assert after['collaboration']['entries'] == before['collaboration']['entries']
    assert not after['collaboration'].get('review_decision_answers')


@pytest.mark.asyncio
@pytest.mark.parametrize('guard', ['stale', 'budget', 'pending', 'review', 'answered'])
async def test_investigation_publication_is_atomic_at_host_boundaries(tmp_path, guard):
    from mewcode.kernel import collaboration as ledger
    from mewcode.orchestration.review_intervention import intervene, publish_investigation
    from mewcode.orchestration import ask_gateway
    async def coordinate(model, payload, system):
        if 'intervention_plan' in payload:
            return {'passed': True, 'evidence': ['issue:R-1'], 'reason': 'A new investigation is useful.'}
        return investigation_plan()
    runtime, issues = intervention_fixture(tmp_path, coordinate)
    # Save a fully reviewed record just before atomic publication.
    saved = []
    mutate = runtime.store.mutate
    def capture(run_id, callback, **kwargs):
        record = runtime.store.load(run_id)
        items = record['collaboration'].get('review_interventions', {})
        if any(row['status'] == 'reviewed' for row in items.values()):
            saved.append(record)
            raise ValueError('test interruption before publication')
        return mutate(run_id, callback, **kwargs)
    runtime.store.mutate = capture
    with pytest.raises(ValueError, match='test interruption'):
        await intervene(runtime, runtime.flow.node_map['audit'], issues)
    record = saved[0]
    value = ledger.state(record)
    identity = next(iter(value['review_interventions']))
    if guard == 'stale':
        value['coordinator_epoch'] += 1
    elif guard == 'budget':
        value['lineage_budget']['limits']['root']['attempts'] = value['lineage_budget']['root']['used']['attempts']
    elif guard == 'pending':
        value.setdefault('plan_holds', {})['audit'] = 'other-publication'
    elif guard == 'review':
        value['review_interventions'][identity]['review']['passed'] = False
    else:
        from mewcode.orchestration.review_decisions import question_for, prepare_interaction, source_basis
        question, _ = question_for(issues[0])
        prepare_interaction(record, node_id='audit', issue_id='R-1', source=source_basis(record, 'audit'), question=question)
        row = next(row for row in value['questions'].values() if row['status'] == 'waiting')
        ask_gateway.answer(record, row['id'], {question['name']: 'defer'})
    before = copy.deepcopy(record)
    with pytest.raises(ValueError):
        publish_investigation(record, identity, work_dir=tmp_path)
    assert record == before


def test_investigator_registry_removes_writes_network_delegation_and_questions():
    from types import SimpleNamespace
    from mewcode.orchestration.review_intervention import READ_TOOLS, restrict_tools
    names = {*READ_TOOLS, 'SubmitHandoff', 'report_step', 'WriteFile', 'Bash', 'Agent', 'RequestAssistance',
             'AskUserQuestion', 'human_oncall', 'WebSearch', 'LoadSkill', 'ProposeNodeExpansion'}
    registry = SimpleNamespace(list_tools=lambda: [SimpleNamespace(name=name) for name in names],
                               unregister=names.remove)
    restrict_tools(registry, SimpleNamespace(node_tools=sorted(READ_TOOLS), deny_tools=['ReadFile']))
    assert names == READ_TOOLS - {'ReadFile'} | {'SubmitHandoff', 'report_step'}


@pytest.mark.asyncio
async def test_failed_investigation_cannot_be_skipped_to_open_audit_or_delivery(tmp_path):
    from mewcode.expert_group import ExpertRoleResult
    spec, calls = review_flow(), []
    store = retry_store(tmp_path, spec)
    async def execute(node, *_):
        calls.append(node.node_id)
        if '::review-' in node.node_id:
            return ExpertRoleResult(text='Saved receipt source is unavailable.', status='blocked')
        if node.node_id == 'audit':
            result = audit_result(False)
            result.verdict['issues'][0]['resolution'] = 'capability_gap'
            return result
        return delivery()
    async def review(*_):
        pytest.fail('failed investigation must keep downstream closed')
    async def coordinate(model, payload, system):
        if 'intervention_plan' in payload:
            return {'passed': True, 'evidence': ['issue:R-1'], 'reason': 'Read existing sources.'}
        return investigation_plan()
    host = orchestrator(store, tmp_path, execute, review, coordinate)
    result = await run(host, store, spec)
    assert result.status == 'blocked' and calls.count('work') == calls.count('audit') == 1
    assert len([key for key in calls if '::review-' in key]) == 1 and 'deliver' not in calls


@pytest.mark.asyncio
async def test_intervention_uses_configured_independent_reviewer_and_caches_wait(tmp_path):
    from mewcode.orchestration.review_intervention import intervene
    from tests.test_collaboration_mode import EXECUTOR, REVIEWER
    calls = []
    async def coordinate(model, payload, system):
        calls.append(model)
        if 'intervention_plan' in payload:
            return {'passed': True, 'evidence': ['issue:R-1'], 'reason': 'The missing service evidence cannot be recreated.'}
        return {'action': 'wait', 'diagnosis': 'Await historical provider telemetry.', 'evidence_refs': ['issue:R-1']}
    runtime, issues = intervention_fixture(tmp_path, coordinate)
    runtime.store.mutate(RUN_ID, lambda record: record.setdefault('locked_models', {}).update(audit=EXECUTOR.as_dict()), durable=True)
    assert await intervene(runtime, runtime.flow.node_map['audit'], issues) == 'deferred'
    assert await intervene(runtime, runtime.flow.node_map['audit'], issues) == 'wait'
    assert calls == [EXECUTOR, REVIEWER]
    assert not runtime.read().get('graph_publications')


@pytest.mark.asyncio
async def test_relevant_new_host_evidence_releases_wait_without_repeating_accepted_work(tmp_path):
    from mewcode.orchestration.review_intervention import intervene
    from tests.test_review_evidence_decisions import decision_issue
    calls = []
    sources = {'@model_receipts': {'ledger_available': True, 'capture_coverage': {'work': {'examined_calls': 1}}},
               '@model_receipts:work': {'calls': [{'event_id': 'historical'}]}}
    async def coordinate(model, payload, system):
        calls.append(payload)
        if 'intervention_plan' in payload:
            return {'passed': True, 'evidence': ['@model_receipts'], 'reason': 'Independent check of the scoped next step.'}
        known = payload['evidence_index']['@model_receipts'].get('recording_contract')
        return ({'action': 'user_decision', 'diagnosis': 'Saved historical capture is unavailable; only the user may accept it.',
                 'evidence_refs': ['@model_receipts'], 'decision': decision_issue()['decision']} if known else
                {'action': 'wait', 'diagnosis': 'Await host capture coverage.', 'evidence_refs': ['@model_receipts']})
    runtime, issues = intervention_fixture(tmp_path, coordinate)
    async def context(*_):
        return copy.deepcopy(sources)
    runtime.context_callback = context
    before = copy.deepcopy(runtime.read()['certificates'])
    assert await intervene(runtime, runtime.flow.node_map['audit'], issues) == 'deferred'
    # The reviewer's own activity and a later session capture are not new facts
    # about the completed producer, and must not trigger more coordination calls.
    sources['@model_receipts']['first_saved_request_summary_at'] = 'later'
    sources['@model_receipts']['capture_coverage']['audit'] = {'examined_calls': 20}
    sources['@model_receipts:audit'] = {'calls': [{'event_id': 'new-audit'}]}
    assert await intervene(runtime, runtime.flow.node_map['audit'], issues) == 'wait'
    assert len(calls) == 2
    sources['@model_receipts']['recording_contract'] = {'unrecorded_history_reconstructable': False}
    result = await intervene(runtime, runtime.flow.node_map['audit'], issues)
    assert result['outcome'] == 'user_decision' and len(calls) == 4
    assert (await intervene(runtime, runtime.flow.node_map['audit'], issues)) == result
    assert len(calls) == 4 and runtime.read()['certificates'] == before
    assert not runtime.read().get('graph_publications') and not runtime.read()['questions']


@pytest.mark.asyncio
async def test_changed_host_evidence_during_independent_review_cannot_publish_plan(tmp_path):
    from mewcode.orchestration.review_intervention import intervene
    sources = {'@model_receipts:work': {'calls': [{'event_id': 'old'}]}}
    async def coordinate(model, payload, system):
        if 'intervention_plan' in payload:
            sources['@model_receipts:work']['calls'].append({'event_id': 'new'})
            return {'passed': True, 'evidence': ['issue:R-1'], 'reason': 'Stale review.'}
        return investigation_plan()
    runtime, issues = intervention_fixture(tmp_path, coordinate)
    async def context(*_):
        return copy.deepcopy(sources)
    runtime.context_callback = context
    before = copy.deepcopy(runtime.read()['certificates'])
    with pytest.raises(ValueError, match='宿主回执依据已经变化'):
        await intervene(runtime, runtime.flow.node_map['audit'], issues)
    assert runtime.read()['certificates'] == before and not runtime.read().get('graph_publications')


@pytest.mark.asyncio
@pytest.mark.parametrize('wrapping', ['fence', 'prefix', 'malformed_then_repair', 'legacy_decode_error'])
async def test_model_response_wrappers_and_one_format_repair_remain_reviewed(tmp_path, wrapping):
    import json
    from mewcode.orchestration.review_intervention import intervene
    calls = []
    async def coordinate(model, payload, system):
        calls.append(payload)
        result = ({'passed': True, 'evidence': ['issue:R-1'], 'reason': 'Independent verification.'}
                  if 'intervention_plan' in payload else investigation_plan())
        if len(calls) == 1 and wrapping == 'malformed_then_repair':
            return 'Not JSON yet'
        if len(calls) == 1 and wrapping == 'legacy_decode_error':
            return {'action': 'wait', 'passed': False, 'reason': 'Expecting value', 'error': 'JSONDecodeError'}
        raw = json.dumps(result)
        return '```json\n' + raw + '\n```' if wrapping == 'fence' else 'Proposal:\n' + raw
    runtime, issues = intervention_fixture(tmp_path, coordinate)
    assert await intervene(runtime, runtime.flow.node_map['audit'], issues) == 'scheduled'
    assert len(calls) == (3 if wrapping in {'malformed_then_repair', 'legacy_decode_error'} else 2)
    saved = next(iter(runtime.read()['review_interventions'].values()))
    assert saved['status'] == 'applied' and saved['review']['passed'] is True


@pytest.mark.asyncio
async def test_review_feedback_returns_to_coordinator_before_publishing_corrected_plan(tmp_path):
    from mewcode.orchestration.review_intervention import intervene
    from tests.test_review_evidence_decisions import decision_issue
    calls = []
    async def coordinate(model, payload, system):
        calls.append(payload)
        if 'intervention_plan' in payload:
            accepted = payload['intervention_plan']['action'] == 'investigate'
            return {'passed': accepted, 'reason': 'Read candidate receipts before asking the user again.', 'evidence': ['issue:R-1']}
        if 'revision_feedback' in payload:
            assert payload['revision_feedback']['plan']['action'] == 'user_decision'
            return investigation_plan()
        return {'action': 'user_decision', 'diagnosis': 'The service must lack evidence.',
            'evidence_refs': ['issue:R-1'], 'decision': decision_issue()['decision']}
    runtime, issues = intervention_fixture(tmp_path, coordinate)
    assert await intervene(runtime, runtime.flow.node_map['audit'], issues) == 'scheduled'
    item = next(iter(runtime.read()['review_interventions'].values()))
    assert len(calls) == 4 and item['proposal_round'] == 1 and item['status'] == 'applied'
    assert item['review_history'][0]['review']['passed'] is False
    assert item['plan']['action'] == 'investigate'
