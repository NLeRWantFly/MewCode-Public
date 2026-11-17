"""A replacement reuses real approvals, not new answers or broader grants."""
import copy
import hashlib
from types import SimpleNamespace
import time

import pytest

from mewcode.experts.authorization_ledger import carry_forward, record_authorization
from mewcode.experts.flow import ExpertFlowSpec
from mewcode.experts.gate_dialogue import await_dialogue, confirmation_valid, digest
from mewcode.experts.human_gate import gate_scope_hash
from mewcode.experts.restart_authorizations import reuse_gate
from mewcode.kernel.collaboration import dependencies, valid_certificate
from mewcode.orchestration.convert import to_expert_flow
from tests.test_graph_lint import _node, _spec


@pytest.fixture
def gate_run(tmp_path):
    nodes = [_node('prep', outputs=['pilot.json']),
             _node('gate', kind='gate', goal='Approve exactly this upload scope'), _node('work')]
    spec = _spec(nodes, [{'from_node': 'prep', 'to_node': 'gate'},
                         {'from_node': 'gate', 'to_node': 'work'}],
        meta={'task_design': {'task_id': 'new-task'}}, run_inputs=[
            {'name': 'scope', 'label': 'Already answered scope', 'required': False, 'default': ''}])
    flow = to_expert_flow(spec)
    original = copy.deepcopy(flow.as_dict())
    original['meta']['task_design']['task_id'] = 'old-task'
    source_flow = ExpertFlowSpec.from_value(original)
    file = tmp_path / 'pilot.json'
    file.write_text('Actual verified pilot evidence', encoding='utf-8')
    artifacts = [{'kind': 'file', 'path': 'pilot.json', 'sha256': hashlib.sha256(file.read_bytes()).hexdigest()}]
    def collaboration():
        value = {'contracts': {'prep': {'depends_on': []}, 'gate': {'depends_on': ['prep']}},
                 'entries': {}, 'certificates': {}}
        for key in ('prep', 'gate'):
            value['certificates'][key] = {'dependencies': dependencies(value, key),
                'artifacts': copy.deepcopy(artifacts) if key == 'prep' else [], 'quality_passed': True}
        return value
    proposal = 'Upload only the previously named pages to the previously selected service.'
    source = {'run_id': 'a' * 32, 'conversation_id': 'conversation', 'status': 'terminated',
        'task': 'Original business task', 'run_inputs': {'scope': 'known-scope'},
        'active_node_ids': [], 'locked_flow': original, 'collaboration': collaboration(),
        'gate_dialogues': {'gate': {'proposal': proposal, 'inputs_hash': 'historical-inputs'}},
        'gate_confirmations': {'gate': {'decision': 'continue', 'request_id': 'real-user-answer',
            'scope_hash': gate_scope_hash(source_flow.node_map['gate'], source_flow.meta),
            'proposal_hash': digest(proposal), 'inputs_hash': 'historical-inputs'}}}
    record_authorization(source, kind='gate_answer', scope={'operation_classes': ['other'],
        'node_ids': ['gate'], 'operation_digest': digest(proposal)}, quote=proposal,
        source_ref='gate:real-user-answer', host_verified=True)
    record = {'run_id': 'b' * 32, 'conversation_id': 'conversation', 'status': 'running',
        'task': source['task'], 'run_inputs': dict(source['run_inputs']),
        'locked_flow': flow.as_dict(), 'collaboration': collaboration(),
        'node_states': {'prep': {'status': 'completed', 'handoff': {
            'summary': 'Reused original verified pilot', 'artifacts': copy.deepcopy(artifacts)}}},
        'task_restart': {'source_run_id': source['run_id'], 'source_task_id': 'old-task', 'request_id': 'explicit-rebuild'}}
    inherited = carry_forward(record, source=source, conversation_id='conversation', task=source['task'],
        source_conversation_id='conversation', source_task=source['task'])
    record['authorization_inheritance'] = {**inherited, 'source_run_id': source['run_id'],
                                         'same_task_basis': 'host_task_restart'}
    def read(run_id, *fields):
        value = source if run_id == source['run_id'] else record
        return copy.deepcopy({key: value[key] for key in fields if key in value})
    def certificate(run_id, node_id):
        value = source if run_id == source['run_id'] else record
        collab = value['collaboration']
        return {'certificate': copy.deepcopy(collab['certificates'][node_id])} if valid_certificate(collab, node_id) else None
    def mutate(run_id, apply, **kwargs):
        assert run_id == record['run_id']
        candidate = copy.deepcopy(record)
        apply(candidate)
        record.clear()
        record.update(candidate)
        return copy.deepcopy(record)
    store = SimpleNamespace(read_execution_fields=read, read_current_certificate=certificate,
        read_execution_flow=lambda run_id: ExpertFlowSpec.from_value(source['locked_flow']),
        mutate=mutate, load=lambda run_id: copy.deepcopy(record))
    return flow, source, record, store, file


def reuse(fixture):
    flow, _, record, store, file = fixture
    return reuse_gate(store, record['run_id'], node=flow.node_map['gate'], flow=flow,
                      snapshot=copy.deepcopy(record), project=file.parent)


def test_gate_uses_the_old_answer_and_files_without_creating_an_approval(gate_run):
    flow, source, record, _, _ = gate_run
    original = copy.deepcopy(source)
    authorizations = copy.deepcopy(record['authorizations'])
    assert reuse(gate_run)
    assert source == original and record['authorizations'] == authorizations
    assert not record.get('gate_confirmations')
    assert record['gate_dialogues']['gate'] == source['gate_dialogues']['gate']
    assert confirmation_valid(flow.node_map['gate'], flow, record)
    record['node_states']['prep']['handoff']['summary'] = 'Upstream changed after reuse'
    assert not confirmation_valid(flow.node_map['gate'], flow, record)


def inline_handoff(fixture):
    _, _, record, _, _ = fixture
    report = {'kind': 'inline', 'name': 'Current readback', 'content': 'Source files unchanged.'}
    record['collaboration']['certificates']['prep']['artifacts'] = [copy.deepcopy(report)]
    record['node_states']['prep']['handoff']['artifacts'] = [report]


def test_host_checks_original_file_versions_when_current_handoff_omits_reused_files(gate_run):
    flow, source, record, _, file = gate_run
    inline_handoff(gate_run)
    before = copy.deepcopy(source), copy.deepcopy(record['authorizations'])
    assert reuse(gate_run)
    proof = record['gate_reuses']['gate']
    assert proof['upstream_file_versions']['prep'] == [
        {'path': 'pilot.json', 'sha256': hashlib.sha256(file.read_bytes()).hexdigest()}]
    assert confirmation_valid(flow.node_map['gate'], flow, record)
    assert (source, record['authorizations']) == before
    assert not record.get('gate_confirmations')
    file.write_text('Changed after reuse', encoding='utf-8')
    assert not confirmation_valid(flow.node_map['gate'], flow, record)


def test_inline_audit_does_not_turn_prior_scope_consent_into_a_new_quality_approval(gate_run):
    flow, source, record, _, _ = gate_run
    inline_handoff(gate_run)
    source['collaboration']['certificates']['prep']['artifacts'] = [
        {'kind': 'inline', 'name': 'Prior audit', 'content': 'Known unresolved service attestation.',
         'sha256': digest('Known unresolved service attestation.')}]
    source['collaboration']['certificates']['prep']['quality_passed'] = False
    source['collaboration']['certificates']['gate']['dependencies'] = dependencies(source['collaboration'], 'gate')
    record['collaboration']['certificates']['prep']['quality_passed'] = False
    pending_gate(gate_run)
    grants = copy.deepcopy(record['authorizations'])
    assert reuse(gate_run)
    assert confirmation_valid(flow.node_map['gate'], flow, record)
    assert not record['collaboration']['certificates']['prep']['quality_passed']
    assert record['authorizations'] == grants and not record.get('gate_confirmations')
    assert record['gate_dialogues']['gate']['proposal'] == source['gate_dialogues']['gate']['proposal']
    assert record['gate_reuses']['gate']['upstream_file_versions'] == {'prep': []}
    assert record['gate_reuses']['gate']['upstream_inline_versions']['prep']


def test_missing_upstream_evidence_cannot_reuse_scope_consent(gate_run):
    _, source, record, _, _ = gate_run
    inline_handoff(gate_run)
    source['collaboration']['certificates']['prep']['artifacts'] = []
    source['collaboration']['certificates']['gate']['dependencies'] = dependencies(source['collaboration'], 'gate')
    before = copy.deepcopy(record)
    assert not reuse(gate_run) and record == before


@pytest.mark.parametrize('fault', ['changed_file', 'conflicting_certificate', 'outside_workspace'])
def test_inline_report_cannot_substitute_for_host_verified_unchanged_files(gate_run, fault):
    _, source, record, _, file = gate_run
    inline_handoff(gate_run)
    if fault == 'changed_file':
        file.write_text('Changed bytes', encoding='utf-8')
    elif fault == 'conflicting_certificate':
        record['collaboration']['certificates']['prep']['artifacts'].append(
            {'kind': 'file', 'path': 'pilot.json', 'sha256': '0' * 64})
    else:
        original = source['collaboration']['certificates']['prep']['artifacts'][0]
        outside = file.parent.parent / ('outside-' + file.parent.name + '.json')
        original['path'] = '../' + outside.name
        source['collaboration']['certificates']['gate']['dependencies'] = dependencies(source['collaboration'], 'gate')
        outside.write_text(file.read_text(encoding='utf-8'), encoding='utf-8')
    before = copy.deepcopy(record)
    assert not reuse(gate_run)
    assert record == before


def pending_gate(fixture):
    from mewcode.experts.node_ledger import ledger_for_run
    flow, _, record, _, _ = fixture
    questions = [{'name': 'choice', 'question': 'Approve the unchanged upload?',
                  'options': ['continue', 'pause']}]
    scope = gate_scope_hash(flow.node_map['gate'], flow.meta)
    pending = {'request_id': 'redundant-question', 'kind': 'human_oncall', 'tool': 'human_oncall',
               'questions': questions, 'scope_hash': scope + ':current-proposal'}
    record['node_states']['gate'] = {'pending_interaction': pending}
    record['kernel_gates'] = {'redundant-question': {'node_id': 'gate', 'status': 'waiting'}}
    record['collaboration']['questions'] = {'duplicate': {
        'node_id': 'gate', 'status': 'waiting', 'questions': copy.deepcopy(questions)}}
    ledger = ledger_for_run(record)
    fact = ledger.record('gate', 'question', copy.deepcopy(pending), source='user')
    ledger.record('gate', 'blocker', {'kind': 'human_question', 'ref': pending['request_id']}, source='host')
    ledger.record('gate', 'blocker', {'kind': 'permission', 'ref': 'different-operation'}, source='host')
    pending['question_fact_id'] = fact.fact_id
    return pending, ledger, fact


def test_only_duplicate_unanswered_gate_is_retired_without_fabricating_an_answer(gate_run):
    from mewcode.experts.node_ledger import ledger_for_run
    _, source, record, _, _ = gate_run
    inline_handoff(gate_run)
    pending, _, _ = pending_gate(gate_run)
    grants = copy.deepcopy(record['authorizations'])
    assert reuse(gate_run)
    stage = record['node_states']['gate']
    assert 'pending_interaction' not in stage
    assert stage['interaction_history'][-1]['pending'] == pending
    assert stage['interaction_history'][-1]['answered'] is False
    assert record['kernel_gates']['redundant-question']['status'] == 'superseded'
    assert record['collaboration']['questions']['duplicate']['status'] == 'superseded'
    view = ledger_for_run(record).view('gate')
    assert not any(fact.kind == 'answer' for fact in view.history)
    assert [(fact.payload['kind'], fact.payload['ref']) for fact in view.active_blockers] == [
        ('permission', 'different-operation')]
    assert record['authorizations'] == grants and not record.get('gate_confirmations')
    assert record['gate_dialogues']['gate'] == source['gate_dialogues']['gate']


def test_retired_gate_stays_retired_after_durable_store_normalization_and_reload(gate_run):
    from mewcode.expert_runs import ExpertRunStore
    from mewcode.kernel import collaboration as ledger
    flow, source, record, _, file = gate_run
    inline_handoff(gate_run)
    raw = flow.as_dict()
    raw['meta']['orchestration_mode'] = 'collaborative'
    raw['meta']['collaboration_spec'] = {'format': ledger.FORMAT, 'task_id': 'new-task',
        'goal': record['task'], 'document': {'entries': [{'id': 'goal', 'kind': 'goal',
            'title': 'Original task', 'body': record['task'], 'state': 'active', 'data': {},
            'sources': [{'source_id': 'user-1', 'quote': record['task']}]}]}}
    for node in raw['nodes']:
        raw['meta']['node_plan_briefs'].setdefault(node['node_id'], {})['acceptance'] = [
            'Deliver the declared result within the original scope.']
    flow = ExpertFlowSpec.from_value(raw)
    source['locked_flow'] = copy.deepcopy(raw)
    source['locked_flow']['meta']['task_design']['task_id'] = 'old-task'
    source['gate_confirmations']['gate']['scope_hash'] = gate_scope_hash(flow.node_map['gate'], flow.meta)
    pending_gate((flow, source, record, None, file))
    store = ExpertRunStore(file.parent / 'durable-runs')
    store.create(run_id=record['run_id'], conversation_id=record['conversation_id'], task=record['task'],
                 attachments=[], profile={}, strategy='stable', flow=flow)
    def prepare(value):
        value.update(copy.deepcopy({key: item for key, item in record.items()
                                    if key not in {'collaboration', 'locked_flow'}}))
        collab = value['collaboration']
        collab['questions'] = copy.deepcopy(record['collaboration']['questions'])
        attempt = ledger.dispatch(collab, 'prep')
        contract = collab['contracts']['prep']
        ledger.complete(collab, attempt['id'], {'passed': True,
            'checks': [{'id': check['id'], 'version': check['version'], 'passed': True,
                        'evidence': {'host': 'Current files read back'}} for check in contract['checks']],
            'artifacts': copy.deepcopy(record['collaboration']['certificates']['prep']['artifacts'])})
    store.mutate(record['run_id'], prepare, durable=True)
    def value(run_id):
        return source if run_id == source['run_id'] else store.load(run_id)
    def read(run_id, *fields):
        return {key: item for key, item in value(run_id).items() if key in fields}
    def certificate(run_id, node_id):
        collab = value(run_id)['collaboration']
        return {'certificate': collab['certificates'][node_id]} if valid_certificate(collab, node_id) else None
    proxy = SimpleNamespace(read_execution_fields=read, read_current_certificate=certificate,
        read_execution_flow=lambda _: ExpertFlowSpec.from_value(source['locked_flow']), mutate=store.mutate)
    assert reuse_gate(proxy, record['run_id'], node=flow.node_map['gate'], flow=flow,
                      snapshot=store.load(record['run_id']), project=file.parent)
    reloaded = ExpertRunStore(file.parent / 'durable-runs').load(record['run_id'])
    assert 'pending_interaction' not in reloaded['node_states']['gate']
    assert reloaded['collaboration']['questions']['duplicate']['status'] == 'superseded'
    assert reloaded['kernel_gates']['redundant-question']['status'] == 'superseded'
    assert confirmation_valid(flow.node_map['gate'], flow, reloaded)
    assert not reloaded.get('gate_confirmations')


@pytest.mark.parametrize('fault', ['different_question', 'real_answer'])
def test_other_questions_and_real_answers_are_preserved(gate_run, fault):
    _, _, record, _, _ = gate_run
    inline_handoff(gate_run)
    pending, ledger, fact = pending_gate(gate_run)
    if fault == 'different_question':
        pending['scope_hash'] = 'different-gate:current-proposal'
    else:
        ledger.record('gate', 'answer', {'question_fact_id': fact.fact_id,
            'request_id': pending['request_id'], 'answer': 'pause'}, source='user')
    before = copy.deepcopy(record)
    assert not reuse(gate_run)
    assert record == before


@pytest.mark.parametrize('fault', ['scope', 'inputs', 'active_source', 'source_revoked', 'target_revoked',
    'source_certificate', 'current_certificate', 'physical_file', 'proposal', 'request', 'missing_restart'])
def test_a_changed_or_unproven_gate_needs_the_real_human_gate(gate_run, fault):
    flow, source, record, _, file = gate_run
    if fault == 'scope':
        flow.meta.setdefault('node_plan_briefs', {})['gate'] = {'steps': ['Send additional material']}
    elif fault == 'inputs':
        record['run_inputs']['scope'] = 'broader-scope'
    elif fault == 'active_source':
        source['active_node_ids'] = ['work']
    elif fault == 'source_revoked':
        source['authorizations']['entries'][0]['revoked_at'] = time.time()
    elif fault == 'target_revoked':
        record['authorizations']['entries'][0]['revoked_at'] = time.time()
    elif fault == 'source_certificate':
        source['collaboration']['certificates']['gate']['quality_passed'] = False
    elif fault == 'current_certificate':
        record['collaboration']['certificates']['prep']['invalidated'] = 'input changed'
    elif fault == 'physical_file':
        file.write_text('Different evidence', encoding='utf-8')
    elif fault == 'proposal':
        source['gate_dialogues']['gate']['proposal'] = 'A different proposal'
    elif fault == 'request':
        source['gate_confirmations']['gate']['request_id'] = 'no-such-user-answer'
    else:
        record.pop('task_restart')
    before = copy.deepcopy(record)
    assert not reuse(gate_run)
    assert record == before and not record.get('gate_reuses')


@pytest.mark.asyncio
async def test_runtime_reuses_the_original_approval_without_asking_again(gate_run):
    flow, _, record, store, file = gate_run
    emitted = []
    async def emit(event):
        emitted.append(event)
    async def forbidden_question(*args):
        pytest.fail('An unchanged, verified approval must not ask again')
    assert await await_dialogue(node=flow.node_map['gate'], flow=flow,
        session=SimpleNamespace(work_dir=str(file.parent)), store=store, run_id=record['run_id'],
        emit=emit, proposal_builder=forbidden_question)
    assert any('复用' in row.get('text', '') for row in emitted)
    assert confirmation_valid(flow.node_map['gate'], flow, record)
