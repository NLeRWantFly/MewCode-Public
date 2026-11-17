import copy
from dataclasses import replace

import pytest

from mewcode.expert_group import ExpertExecutionPolicy
from mewcode.expert_web import _role_registry
from mewcode.experts.completion import rejected_handoff_correction
from mewcode.experts.delivery import prior_handoff_source
from mewcode.experts.review_reports import WriteReviewReport, report_scope
from mewcode.tools.write_file import WriteFile, Params
from tests.test_expert_children import FakeSession, make_registry


def contract():
    return {'inputs': ['runs/<document_id>/fused.jsonl：待核验', 'pages/<document_id>/：原图'],
            'deliverables': ['runs/<document_id>/review.jsonl：意见', 'runs/<document_id>/review.jsonl 内逐页检查点']}


@pytest.mark.asyncio
@pytest.mark.parametrize('target,allowed', [('runs/doc/review.jsonl', True),
    ('runs/doc/fused.jsonl', False), ('pages/doc/review.jsonl', False),
    ('runs/doc/extra/review.jsonl', False), ('runs/.git/review.jsonl', False), ('../review.jsonl', False)])
async def test_only_the_declared_report_can_be_written(tmp_path, target, allowed):
    tool = WriteReviewReport(WriteFile(), work_dir=tmp_path, scope=report_scope(contract()), sources={})
    path = tmp_path / target
    result = await tool.execute(Params(file_path=str(path), content='actual opinion\n'))
    assert result.is_error is not allowed
    assert path.exists() is allowed
    if allowed:
        assert path.read_text() == 'actual opinion\n'


@pytest.mark.asyncio
async def test_report_scope_cannot_overwrite_a_reviewed_input_or_bypass_delegate(tmp_path):
    path = tmp_path / 'runs/doc/review.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text('original')
    scope = report_scope(contract())
    protected = WriteReviewReport(WriteFile(), work_dir=tmp_path, scope=scope,
        sources={'upstream': {'fixed_artifacts': [{'path': str(path)}]}})
    assert (await protected.execute(Params(file_path=str(path), content='changed'))).is_error
    assert path.read_text() == 'original'
    scope['inputs'] += ('runs/<document_id>/review.jsonl',)
    protected = WriteReviewReport(WriteFile(), work_dir=tmp_path, scope=scope, sources={})
    assert (await protected.execute(Params(file_path=str(path), content='changed'))).is_error
    from mewcode.tools.file_state_cache import FileStateCache
    guarded = WriteReviewReport(WriteFile(file_state_cache=FileStateCache()), work_dir=tmp_path,
                               scope=report_scope(contract()), sources={})
    assert (await guarded.execute(Params(file_path=str(path), content='changed'))).is_error
    assert path.read_text() == 'original'


@pytest.mark.asyncio
async def test_own_previous_report_can_be_revised_without_changing_frozen_input(tmp_path):
    path = tmp_path / 'runs/doc/review.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text('previous report')
    snapshot = tmp_path / 'frozen/report.jsonl'
    snapshot.parent.mkdir()
    snapshot.write_text('previous report')
    source = {'node_id': 'previous:review', 'handoff': {'artifacts': [
        {'path': str(path), 'source_path': str(path), 'snapshot_path': str(snapshot)}]}}
    tool = WriteReviewReport(WriteFile(), work_dir=tmp_path, scope=report_scope(contract()),
        node_id='review', sources={'previous:review': source})
    result = await tool.execute(Params(file_path=str(path), content='revised report'))
    assert not result.is_error and path.read_text() == 'revised report'
    assert snapshot.read_text() == 'previous report'
    # Even an explicitly declared output cannot overwrite a frozen snapshot.
    tool.scope['outputs'] += ('frozen/report.jsonl',)
    assert (await tool.execute(Params(file_path=str(snapshot), content='changed'))).is_error


@pytest.mark.asyncio
@pytest.mark.parametrize('conflict', ['other_node', 'wrong_identity', 'missing_owner', 'upstream', 'input'])
async def test_previous_report_exception_cannot_relax_other_protections(tmp_path, conflict):
    path = tmp_path / 'runs/doc/review.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text('original')
    source = {'node_id': 'previous:review', 'handoff': {'artifacts': [{'path': str(path)}]}}
    sources, owner, scope = {'previous:review': source}, 'review', report_scope(contract())
    if conflict == 'other_node':
        sources = {'previous:other': dict(source, node_id='previous:other')}
    elif conflict == 'wrong_identity':
        source['node_id'] = 'previous:other'
    elif conflict == 'missing_owner':
        owner = ''
    elif conflict == 'upstream':
        sources['upstream'] = {'fixed_artifacts': [{'source_path': str(path)}]}
    else:
        scope['inputs'] += ('runs/<document_id>/review.jsonl',)
    tool = WriteReviewReport(WriteFile(), work_dir=tmp_path, scope=scope, sources=sources, node_id=owner)
    assert (await tool.execute(Params(file_path=str(path), content='changed'))).is_error
    assert path.read_text() == 'original'


@pytest.mark.asyncio
async def test_report_path_cannot_alias_another_file(tmp_path):
    original = tmp_path / 'original.txt'
    original.write_text('protected input')
    target = tmp_path / 'runs/doc/review.jsonl'
    target.parent.mkdir(parents=True)
    target.hardlink_to(original)
    tool = WriteReviewReport(WriteFile(), work_dir=tmp_path, scope=report_scope(contract()), sources={})
    result = await tool.execute(Params(file_path=str(target), content='overwritten'))
    assert result.is_error and original.read_text() == 'protected input'


@pytest.mark.parametrize('invalid', ['../source', '/absolute', 'C:/input', '*.json', '.mewcode/authority.json'])
def test_ambiguous_or_unsafe_input_scope_does_not_enable_report_writing(invalid):
    value = contract()
    value['inputs'].append(invalid)
    assert report_scope(value) is None


@pytest.mark.parametrize('role,overrides,allowed', [('executor', {}, True), ('auditor', {}, False),
    ('executor', {'parallel_readonly': True}, False), ('executor', {'audit_contract': {'id': 'audit'}}, False),
    ('executor', {'deny_tools': ('WriteFile',)}, False), ('executor', {'node_tools': ('ReadFile',)}, False),
    ('executor', {'review_report_scope': None}, False)])
def test_existing_readonly_and_explicit_tool_restrictions_remain(tmp_path, role, overrides, allowed):
    parent = make_registry('ReadFile', 'EditFile', 'Bash')
    parent.register(WriteFile())
    policy = replace(ExpertExecutionPolicy('stable', False, 0, work_dir=str(tmp_path),
        loop_context={'reviewer': True}, review_report_scope=report_scope(contract())), **overrides)
    session = FakeSession(parent)
    session.work_dir = str(tmp_path)
    registry = _role_registry(session, role, policy)
    assert registry.is_enabled('WriteFile') is allowed
    assert not registry.is_enabled('EditFile') and not registry.is_enabled('Bash')
    if allowed:
        assert isinstance(registry.get('WriteFile'), WriteReviewReport)
        assert registry.get('WriteFile').category == 'write'


def pending_stage():
    value = {'summary': 'Prepared review', 'status': 'needs_attention', 'blocker_kind': 'capability_missing',
        'inline_outputs': [{'name': 'report', 'content': 'Complete retained report'}],
        'file_requests': [{'output_name': 'report', 'path': 'runs/doc/review.jsonl'}]}
    return {'status': 'needs_attention', 'input_revision': 'v1', 'handoff_input_revision': 'v1',
        'node_attempt': 2, 'epoch': 0,
        'handoff': {'status': 'needs_attention', 'blocker_kind': 'delivery_format'},
        'tools': {'submitted': {'name': 'SubmitHandoff', 'status': 'completed', 'is_error': False,
                               'node_attempt': 2, 'epoch': 0, 'declared_at': 1, 'args': value}}}


def test_successful_pending_submission_survives_a_later_missing_handoff():
    stage = pending_stage()
    source = prior_handoff_source('review', stage, input_revision='v1')
    assert source['handoff']['file_requests'] == stage['tools']['submitted']['args']['file_requests']
    assert source['record_kind'] == 'previous_attempt_not_current_acceptance'
    assert source['status'] == source['handoff']['status'] == 'needs_attention'
    source['handoff']['inline_outputs'][0]['content'] = 'changed copy'
    assert stage['tools']['submitted']['args']['inline_outputs'][0]['content'] == 'Complete retained report'


def archived_retry():
    from mewcode.kernel.briefs import begin_stage_attempt
    record = {'status': 'running', 'attempt': 3, 'node_states': {'review': pending_stage()}}
    begin_stage_attempt(record, {'node_id': 'review', 'node_attempt': 3, 'input_revision': 'v1'})
    return record


@pytest.mark.parametrize('extra_retry', [False, True])
def test_actual_retry_archive_preserves_pending_body_without_reexecuting_work(extra_retry):
    record = archived_retry()
    if extra_retry:
        from mewcode.kernel.briefs import begin_stage_attempt
        begin_stage_attempt(record, {'node_id': 'review', 'node_attempt': 4, 'input_revision': 'v1'})
    stage = record['node_states']['review']
    assert stage['tools'] == {} and stage['node_attempt'] == (4 if extra_retry else 3)
    source = prior_handoff_source('review', stage, input_revision='v1', archives=record['kernel_attempt_archives'])
    assert source['handoff']['inline_outputs'][0]['content'] == 'Complete retained report'
    assert source['record_kind'] == 'previous_attempt_not_current_acceptance'
    assert stage['handoff']['blocker_kind'] == 'delivery_format'


@pytest.mark.parametrize('change', ['node', 'epoch', 'revision', 'attempt', 'new_submission', 'cycle'])
def test_archived_retry_cannot_cross_identity_or_supersede_a_new_submission(change):
    record = archived_retry()
    stage = record['node_states']['review']
    archives = record['kernel_attempt_archives']
    archived = archives[stage['last_archive_id']]
    if change == 'node':
        archived['node_id'] = 'unrelated'
    elif change == 'epoch':
        archived['data']['epoch'] = 1
    elif change == 'revision':
        archived['data']['handoff_input_revision'] = 'old'
    elif change == 'attempt':
        archived['data']['node_attempt'] = 4
    elif change == 'new_submission':
        stage['tools'] = {'failed': {'name': 'SubmitHandoff', 'is_error': True}}
    else:
        archived['data'] = copy.deepcopy(stage)
    assert prior_handoff_source('review', stage, input_revision='v1', archives=archives) is None


@pytest.mark.parametrize('change', ['input', 'attempt', 'epoch', 'rejected', 'new_rejected'])
def test_invalid_or_superseded_submission_is_not_exposed_as_previous_delivery(change):
    stage = pending_stage()
    row = stage['tools']['submitted']
    if change == 'input':
        row['input_revision'] = 'old'
    elif change == 'attempt':
        row['node_attempt'] = 1
    elif change == 'epoch':
        row['epoch'] = 1
    elif change == 'rejected':
        row.update(status='failed', is_error=True)
    else:
        stage['tools']['later'] = dict(copy.deepcopy(row), declared_at=2, status='failed', is_error=True)
    assert prior_handoff_source('review', stage, input_revision='v1') is None


def test_forgotten_submission_gets_bounded_correction_without_fabricating_a_handoff():
    state = {}
    for _ in range(2):
        assert rejected_handoff_correction(None, {}, state, input_revision='v1', node_attempt=1)
    assert not rejected_handoff_correction(None, {}, state, input_revision='v1', node_attempt=1)
    assert not rejected_handoff_correction({'status': 'needs_attention'}, {}, {}, input_revision='v1', node_attempt=1)
