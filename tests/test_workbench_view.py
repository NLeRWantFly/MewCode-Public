"""T82 view contracts: stable reads, scope isolation and acceptance evidence."""
from __future__ import annotations

import copy
from unittest.mock import AsyncMock

import httpx
import pytest

from frontend import server
from mewcode.kernel import collaboration as ledger
from mewcode.kernel.events import digest
from mewcode.orchestration import task_view
from mewcode.orchestration.collab_service import user_revision
from tests.test_collaboration_mode import RUN_ID, store_for, passing_report
from tests.test_task_design_entry import task_api_env


@pytest.mark.parametrize('raw,expected', [
    ('pending', '未开始'), ('queued', '未开始'), ('planned', '未开始'), ('ready', '未开始'),
    ('running', '进行中'), ('dispatched', '进行中'), ('working', '进行中'), ('continuing', '进行中'),
    ('checking', '验收中'), ('reviewing', '验收中'), ('review_pending', '验收中'),
    ('waiting_permission', '等你处理'), ('awaiting_user', '等你处理'), ('needs_input', '等你处理'),
    ('blocked', '等你处理'), ('needs_attention', '等你处理'), ('unknown', '等你处理'), ('held', '等你处理'),
    ('rejected', '需返工'), ('invalidated', '需返工'), ('incomplete_audit', '需返工'),
    ('completed', '已完成'), ('accepted', '已完成'), ('skipped', '已跳过'), ('branch_not_taken', '已跳过'),
    ('paused', '已暂停'), ('terminated', '已停止'), ('failed', '已停止'), ('cancelled', '已停止'),
    ('canceled', '已停止'), ('stopped', '已停止'), ('budget_exhausted', '已停止'), ('stalled', '已停止'),
    ('superseded_closed', '已停止'), ('unrecognized-future-state', '已停止'),
])
def test_step_status_contract(raw, expected):
    assert task_view.step_status(raw) == expected


@pytest.mark.parametrize('raw,expected', [
    ('designing', '设计中'), ('building', '设计中'), ('ready', '等待开始'), ('queued', '等待开始'),
    ('pending', '等待开始'), ('planned', '等待开始'), ('running', '进行中'), ('working', '进行中'),
    ('continuing', '进行中'), ('reviewing', '进行中'), ('checking', '进行中'),
    ('needs_input', '等你处理'), ('needs_attention', '等你处理'), ('awaiting_user', '等你处理'),
    ('waiting_permission', '等你处理'), ('blocked', '等你处理'), ('intervening', '等你处理'),
    ('paused', '已暂停'), ('held', '已暂停'), ('completed', '已完成'), ('complete', '已完成'),
    ('terminated', '已停止'), ('cancelled', '已停止'), ('canceled', '已停止'), ('failed', '已停止'),
    ('stopped', '已停止'), ('budget_exhausted', '已停止'), ('unknown', '已停止'), ('stalled', '已停止'),
    ('future-state', '已停止'),
])
def test_task_status_contract(raw, expected):
    assert task_view.task_status(raw) == expected


def test_classic_has_single_group_and_never_fabricates_acceptance(tmp_path):
    store = store_for(tmp_path, selected='classic')
    def seed(record):
        record['node_states'] = {'work': {'status': 'completed', 'attempt': 1,
            'acceptance_checks': [{'id': 'old-check', 'passed': True}]}, 'deliver': {'status': 'running', 'attempt': 2}}
    store.mutate(RUN_ID, seed, durable=True)
    record = store.load(RUN_ID)
    before = digest(record)
    one, two = task_view.from_record(record), task_view.from_record(record)
    assert one == two and digest(record) == before
    assert len(one['plan']) == 1 and one['plan'][0]['steps'][0]['acceptance'] is None
    assert one['plan'][0]['steps'][1]['attempts'] == 2
    assert one['task']['current_activity'] == '正在进行：deliver'
    assert one['legacy'] is False and one['task']['mode'] == 'classic'


def accepted_artifact(record, text='first'):
    value = record['collaboration']
    attempt = ledger.dispatch(value, 'work')
    report = passing_report(value)
    report['artifacts'] = [{'kind': 'inline', 'name': '资料清单', 'content': text, 'sha256': digest(text)}]
    ledger.complete(value, attempt['id'], report)


@pytest.mark.parametrize('accepted_upstream', [False, True])
def test_paused_dispatch_is_saved_progress_and_keeps_certificates_and_future_steps(tmp_path, accepted_upstream):
    record = store_for(tmp_path).load(RUN_ID)
    if accepted_upstream:
        accepted_artifact(record)
    active = 'deliver' if accepted_upstream else 'work'
    ledger.dispatch(record['collaboration'], active)
    running = task_view.from_record(record)
    record.update(status='paused', status_reason='专家角色配置失效：旧模型不可用')
    before = digest(record)
    paused = task_view.from_record(record)
    steps = {step['id']: step for group in paused['plan'] for step in group['steps']}
    assert steps[active]['status'] == '已暂停'
    assert steps[active]['status_reason'] == record['status_reason']
    assert steps['work' if accepted_upstream else 'deliver']['status'] == ('已完成' if accepted_upstream else '未开始')
    assert next(node for node in paused['structure']['nodes'] if node['id'] == active)['status'] == '已暂停'
    assert paused['plan'][0]['status'] == '已暂停'
    assert paused['structure']['versions'] == running['structure']['versions']
    assert paused['task']['current_activity'] == '已保留进度，任务暂停中'
    assert task_view.finish(paused) == paused and digest(record) == before
    if accepted_upstream:
        assert paused['deliverables'][0]['status'] == '已验收'


@pytest.mark.parametrize('status,label', [('blocked', '等你处理'), ('failed', '已停止'), ('budget_exhausted', '已停止')])
def test_suspended_classic_runtime_does_not_advertise_active_nodes(tmp_path, status, label):
    record = store_for(tmp_path, selected='classic').load(RUN_ID)
    record.update(status=status, node_states={'work': {'status': 'running'}, 'deliver': {'status': 'pending'}})
    view = task_view.from_record(record)
    assert [step['status'] for step in view['plan'][0]['steps']] == [label, '未开始']


def test_waiting_task_can_still_show_parallel_execution():
    view = {'task': {'id': 't', 'run_id': 'r', 'status': '等你处理', 'execution_status': 'running'},
            'plan': [{'status': '等你处理', 'steps': [{'id': 'a', 'status': '等你处理'},
                                                   {'id': 'b', 'status': '进行中'}]}]}
    assert task_view.finish(view)['plan'][0]['steps'][1]['status'] == '进行中'


def test_old_view_overlay_uses_only_matching_design_reason_without_mutating_cache(tmp_path):
    from mewcode.kernel.run_views import overlay
    record = store_for(tmp_path).load(RUN_ID)
    ledger.dispatch(record['collaboration'], 'work')
    cached = task_view.from_record(record)
    cached['task'].pop('execution_status')
    cached['task']['status'] = '已暂停'
    before = digest(cached)
    runtime = {'run_id': RUN_ID, 'status': 'paused'}
    design = {'task_id': cached['task']['id'], 'run_id': RUN_ID, 'status': 'paused',
              'revision': 9, 'reason': '专家角色配置失效：旧模型不可用'}
    view = task_view.finish(overlay(cached, design, runtime=runtime))
    assert view['task']['reason'] == design['reason'] and view['task']['design_revision'] == 9
    assert view['plan'][0]['steps'][0]['status'] == '已暂停'
    assert view['plan'][0]['steps'][0]['status_reason'] == design['reason']
    assert digest(cached) == before
    other = overlay(cached, {**design, 'task_id': 'other'}, runtime=runtime)
    assert other['task']['reason'] != design['reason']
    design['reason'] = '模型配置已变化，请检查后继续'
    refreshed = task_view.finish(overlay(view, design, runtime=runtime))
    assert refreshed['plan'][0]['steps'][0]['status_reason'] == design['reason']


def test_collaborative_reads_are_pure_and_keep_invalidated_deliveries(tmp_path):
    store = store_for(tmp_path)
    store.mutate(RUN_ID, accepted_artifact, durable=True)
    record = store.load(RUN_ID)
    before = digest(record)
    accepted = task_view.from_record(record)
    assert accepted == task_view.from_record(record) and digest(record) == before
    assert accepted['deliverables'][0]['status'] == '已验收'
    revised = user_revision(store, record, entry_id='goal', body='补充来源说明', expected_version=1, request_id='revision')
    changed = task_view.from_record(revised)
    assert changed['version'] != accepted['version']
    assert changed['deliverables'][0]['status'] == '需重新核验'
    assert changed['deliverables'][0]['inline_content'] == 'first'
    assert {row['label'] for row in changed['changes']} == {'要求修订'}
    assert changed['requirements'][0]['revision']['status'] == '已记录'
    assert changed['plan'][0]['steps'][0]['status'] == '等你处理'


def test_replacement_certificate_does_not_erase_old_delivery_and_versions(tmp_path):
    store = store_for(tmp_path)
    store.mutate(RUN_ID, accepted_artifact, durable=True)
    record = user_revision(store, store.load(RUN_ID), entry_id='goal', body='新的要求', expected_version=1, request_id='revision')
    def adopt(saved):
        value = saved['collaboration']
        proposal = next(iter(value['proposals']))
        ledger.adopt(value, proposal, review={'passed': True, 'evidence': ['checked']}, proposer_vendor='a', reviewer_vendor='b')
        accepted_artifact(saved, 'second')
    store.mutate(RUN_ID, adopt, durable=True)
    view = task_view.from_record(store.load(RUN_ID))
    assert [row['status'] for row in view['deliverables']] == ['需重新核验', '已验收']
    assert [row['version'] for row in view['structure']['versions']] == [1, 2]
    versions = view['structure']['versions']
    assert versions[0]['nodes'][0]['requirements_hash'] != versions[1]['nodes'][0]['requirements_hash']
    assert versions[0]['nodes'][0]['requirements'] != versions[1]['nodes'][0]['requirements']
    assert any(row['body'] == '新的要求' for row in versions[1]['nodes'][0]['requirements'])


def test_legacy_readonly_and_invalid_collaboration_cannot_fall_back(tmp_path):
    record = store_for(tmp_path, selected='classic').load(RUN_ID)
    record.pop('kernel_revision')
    record['node_states'] = {'work': {'status': 'completed'}}
    view = task_view.from_record(record)
    assert view['legacy'] and view['legacy_message'] == task_view.LEGACY_TEXT
    assert view['task']['controls'] == [] and view['inbox'] == [] and view['deliverables'] == []
    assert all(step['detail'] is None for step in view['plan'][0]['steps'])
    record['orchestration_mode'] = 'collaborative'
    with pytest.raises(ValueError, match='协作账本'):
        task_view.from_record(record)


def test_failure_and_invalid_certificate_never_display_completion(tmp_path):
    store = store_for(tmp_path)
    record = store.load(RUN_ID)
    record['status'] = 'completed'
    assert task_view.from_record(record)['task']['status'] != '已完成'
    record['status'] = 'failed'
    assert task_view.from_record(record)['task']['status'] == '已停止'


def test_collaboration_groups_and_constraint_editing_authority(tmp_path):
    from mewcode.expert_runs import ExpertRunStore
    from mewcode.experts.flow import ExpertFlowSpec
    from tests.test_collaboration_mode import flow, PROFILE
    raw = flow().as_dict()
    raw['meta']['workflow_draft'] = {'parents': {'work': 'research', 'deliver': 'delivery'}}
    raw['meta']['collaboration_spec']['document']['entries'].extend([
        {'id': 'research', 'kind': 'milestone', 'body': '整理资料', 'data': {}},
        {'id': 'delivery', 'kind': 'milestone', 'body': '核验与交付', 'data': {}},
    ])
    store = ExpertRunStore(tmp_path / 'groups')
    store.create(run_id=RUN_ID, conversation_id='conversation', task='交付', attachments=[],
                 profile=PROFILE.as_dict(), strategy='stable', flow=ExpertFlowSpec.from_value(raw))
    record = store.load(RUN_ID)
    record['collaboration']['entries'].update({
        'user_limit': {'id': 'user_limit', 'kind': 'constraint', 'body': '用户的硬约束', 'origin': 'user', 'strength': 'hard'},
        'policy': {'id': 'policy', 'kind': 'constraint', 'body': '宿主策略', 'origin': 'host', 'strength': 'hard'},
    })
    view = task_view.from_record(record)
    # Disk serialization can sort object keys; the plan still follows dependencies.
    import json
    view = task_view.from_record(json.loads(json.dumps(record, sort_keys=True)))
    assert [group['title'] for group in view['plan']] == ['整理资料', '核验与交付']
    assert [[step['id'] for step in group['steps']] for group in view['plan']] == [['work'], ['deliver']]
    requirements = {row['id']: row for row in view['requirements']}
    assert requirements['user_limit']['editable'] is True
    assert requirements['policy']['editable'] is False
    assert requirements['policy']['edit_reason'] == task_view.POLICY_TEXT


@pytest.mark.asyncio
async def test_api_checks_scope_and_reads_without_models_or_record_writes(task_api_env, tmp_path, monkeypatch):
    _, _, _, owner, _ = task_api_env
    store = store_for(tmp_path / 'view')
    store.mutate(RUN_ID, lambda row: row.update(conversation_id=owner['conversation_id']), durable=True)
    monkeypatch.setattr(server, 'EXPERT_RUNS', store)
    model = AsyncMock(side_effect=AssertionError('view must not call a model'))
    monkeypatch.setattr(server.MODEL_REGISTRY, 'catalog', model)
    before = digest(store.load(RUN_ID))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        first = await client.get('/api/workbench/view', params={'conversation_id': owner['conversation_id'], 'run_id': RUN_ID})
        second = await client.get('/api/workbench/view', params=owner)
        assert first.status_code == second.status_code == 200, first.text
        assert first.json() == second.json()
        listed = await client.get('/api/workbench/tasks', params=owner)
        assert listed.json()['tasks'][0]['run_id'] == RUN_ID
        wrong = await client.get('/api/workbench/view', params={**owner, 'conversation_id': 'other'})
        mismatch = await client.get('/api/workbench/view', params={**owner, 'run_id': RUN_ID, 'task_id': 'other'})
        assert wrong.status_code == mismatch.status_code == 409
        missing = await client.get('/api/workbench/view', params={**owner, 'run_id': 'b' * 32})
        assert missing.status_code == 404
    assert model.await_count == 0 and digest(store.load(RUN_ID)) == before


@pytest.mark.asyncio
async def test_empty_reads_do_not_create_draft_database(task_api_env, monkeypatch):
    from pathlib import Path
    _, _, _, owner, _ = task_api_env
    # api_env may already have unrelated graph storage; a read must leave every
    # file unchanged and must never create a task design to populate the panel.
    project = Path(owner['project'])
    before = {str(p): p.read_bytes() for p in project.rglob('*') if p.is_file()}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        assert (await client.get('/api/workbench/view', params=owner)).json() is None
    assert {str(p): p.read_bytes() for p in project.rglob('*') if p.is_file()} == before
