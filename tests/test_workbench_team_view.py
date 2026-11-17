"""T83: actual team host state, synthetic inference and read-only projection."""
import asyncio
import copy
import sqlite3
from unittest.mock import Mock

import httpx
import pytest

from frontend import server
from mewcode.kernel.events import digest
from mewcode.orchestration import task_view
from mewcode.teams.host import TeamHost, template
from mewcode.teams.interventions import InterventionService
from mewcode.teams.read_views import reader
from mewcode.teams.runtime_binding import GrokTeamExecutor
from mewcode.teams.runtime_protocol import ArtifactContent
from tests.test_task_design_entry import task_api_env
from test_team_binding_v2 import TeamInvoker


def create_team(root):
    root.mkdir(parents=True, exist_ok=True)
    host = TeamHost(root)
    host.enable()
    run = host.draft(definition=template('data'), objective='整理数据并交付', budget_ref='fixture-budget',
                     name='数据质量与交付团队', command_id='fixture-draft')['run']['team_run_id']
    artifact = host.runtime.register_input(host.actor, run, ArtifactContent.from_bytes(
        port_id='request', schema_ref='data-artifact/v1', media_type='text/csv', content=b'id,value\n1,10\n'),
        control_epoch=1, command_id='seed')
    active = host.runtime.activate(host.actor, run, bindings=host.activation_plan(run)['bindings'], inputs={'request': artifact['version_id']},
                                   budget_limit=10000, expected_version=1, control_epoch=1, command_id='activate')
    invoker = TeamInvoker()
    result = asyncio.run(GrokTeamExecutor(host.runtime, invoker, root=root, session_id='workbench-synthetic').run_once())[0]
    assert 'handoff_id' in result, result
    task = host.runtime.task(host.actor, active['task_ids']['prepare'])
    host.runtime.review(host.actor, result['handoff_id'], accepted=True,
        scores=[dict(criterion_id=c['criterion_id'], score=10000, evidence='人工核对夹具的固定五字节；不是实际模型证明') for c in task['contract']['acceptance']],
        expected_revision=1, control_epoch=1, command_id='review')
    publication = host.publisher.preview(host.actor, run, [{'relative_path': 'delivery.bin', 'version_id': result['artifacts'][0]['version_id']}])
    prepared = host.publisher.prepare(host.actor, run, [{k: v for k, v in row.items() if k != 'content_hash'} for row in publication],
                                       control_epoch=1, command_id='prepare-publication')
    from mewcode.teams.responsibility import MissionService
    MissionService(host.runtime).initialize(host.actor, run, contract=dict(objective='整理数据并交付', hard_constraints=['不改原始数据'],
        success_criteria=[dict(criterion_id='quality', description='保留可追溯的输入版本')]),
        assignments=[dict(task_key=key, owner_role='producer', goal_criteria=['quality']) for key in ('prepare', 'deliver')],
        control_epoch=1, command_id='mission')
    return host, run, invoker, prepared


@pytest.fixture
def team_case(tmp_path):
    value = create_team(tmp_path / 'team')
    yield value
    value[0].close()


@pytest.mark.parametrize('raw,expected', [('draft', '等待开始'), ('ready', '等待开始'), ('running', '进行中'),
    ('waiting', '等你处理'), ('verifying', '进行中'), ('completed', '已完成'), ('failed', '已停止'), ('cancelled', '已停止')])
def test_all_team_run_statuses(raw, expected):
    assert task_view.task_status(raw) == expected


@pytest.mark.parametrize('raw,expected', [('waiting_inputs', '未开始'), ('queued', '未开始'), ('claimed', '进行中'),
    ('running', '进行中'), ('verifying', '验收中'), ('completed', '已完成'), ('failed', '已停止'),
    ('blocked', '等你处理'), ('expired', '等你处理'), ('cancelled', '已停止')])
def test_all_team_step_statuses(raw, expected):
    assert task_view.step_status(raw) == expected


def test_team_projection_keeps_reviews_publications_and_history(team_case):
    host, run, invoker, publication = team_case
    snapshot = host.snapshot(run, read_only=True)
    before = digest(snapshot)
    first = task_view.team_view(snapshot)
    assert task_view.team_view(snapshot) == first and digest(snapshot) == before
    assert first['task']['mode'] == 'team' and first['task']['scope'] == 'project'
    assert first['task']['conversation_id'] == ''
    assert len(first['plan']) == 1 and len(first['plan'][0]['steps']) == 2
    steps = {row['id']: row for row in first['plan'][0]['steps']}
    accepted = next(row for row in steps.values() if row['status'] == '已完成')
    assert accepted['acceptance']['passed'] and accepted['detail']['checks'][0]['accepted']
    assert accepted['detail']['history']['revisions'][0]['attempts'][0]['usage_tokens'] == 42
    assert first['deliverables'][0]['status'] == '已验收'
    assert first['deliverables'][0]['publication']['id'] == publication['publication_id']
    assert first['deliverables'][0]['name'] == 'delivery.bin'
    assert invoker.calls == 1  # Only fixture preparation called the synthetic invoker.
    assert first['structure']['edges'][0]['from'] == accepted['id']
    changed = copy.deepcopy(snapshot)
    changed['orchestration']['mission']['assignments'][1]['owner_role'] = 'editor'
    assert len(task_view.team_view(changed)['plan']) == 2


def test_intervention_inbox_and_changes_come_from_existing_host(team_case):
    host, run, _, _ = team_case
    service = InterventionService(host.runtime)
    received = service.submit(host.actor, run, request=dict(intent='pause', message='先暂停检查交付', expected_goal_version=1, expected_plan_revision=1),
                               control_epoch=1, command_id='pause-proposal')
    first = task_view.team_view(host.snapshot(run, read_only=True))
    assert first['task']['status'] == '等你处理'
    assert first['inbox'][0]['source_id'] == received['intervention_id']
    assert first['inbox'][0]['mandatory'] and first['inbox'][0]['deadline'] is None
    assert first['changes'][-1]['type'] == '要求修订' and first['changes'][-1]['time']
    service.dismiss(host.actor, received['intervention_id'], reason='继续原计划', control_epoch=1, command_id='dismiss')
    second = task_view.team_view(host.snapshot(run, read_only=True))
    assert second['inbox'] == [] and second['changes'][-1]['type'] == '修订草稿未采用'
    assert {row['type'] for row in second['changes']} <= task_view.CHANGE_LABELS


def test_revoked_delivery_remains_visible(team_case):
    host, run, _, _ = team_case
    snapshot = host.snapshot(run, read_only=True)
    version = snapshot['artifacts'][0]['version_id']
    host.runtime.revoke_artifact(host.actor, version, reason='重新检查交付', control_epoch=1, command_id='revoke')
    view = task_view.team_view(host.snapshot(run, read_only=True))
    assert view['deliverables'][0]['status'] == '需重新核验'
    assert next(row for row in view['plan'][0]['steps'] if row['id'] == snapshot['artifacts'][0]['producer_task_id'])['status'] == '需返工'


def test_team_plan_proposal_and_other_run_events_are_separated(team_case):
    host, run, _, _ = team_case
    snapshot = host.snapshot(run, read_only=True)
    snapshot['orchestration']['plan_proposals'] = [dict(proposal_id='proposal-current', state='proposed',
        reason='调整核验顺序', goal_version=1, base_plan_revision=1, impact={}, definition=snapshot['definition'])]
    snapshot['events'].extend([
        dict(sequence=1000, event_type='team_plan_proposed', payload=dict(team_run_id=run, proposal_id='proposal-current')),
        dict(sequence=1001, event_type='team_plan_dismissed', payload=dict(proposal_id='proposal-current')),
        dict(sequence=1002, event_type='team_plan_replaced', payload=dict(team_run_id='another-run', proposal_id='proposal-other')),
        dict(sequence=1003, event_type='team_intervention_dismissed', payload=dict(intervention_id='another-intervention')),
    ])
    view = task_view.team_view(snapshot)
    assert view['inbox'][0]['type'] == 'team_plan'
    assert [row['id'] for row in view['changes']] == ['1000', '1001']


def test_reads_do_not_open_responsibility_register_models_or_write_db(team_case, monkeypatch):
    host, run, invoker, _ = team_case
    host.close()
    assert host._store is None and host._responsibility is None
    db_path = host.state / 'coordination.sqlite3'
    before = db_path.read_bytes()
    constructor = Mock(side_effect=AssertionError('view must never configure models'))
    monkeypatch.setattr('mewcode.teams.orchestration_host.ResponsibilityHost', constructor)
    one = task_view.team_view(host.snapshot(run, read_only=True))
    assert task_view.team_view(host.snapshot(run, read_only=True)) == one
    assert host.workbench_runs()[0]['run']['team_run_id'] == run
    assert db_path.read_bytes() == before and host._store is None and host._responsibility is None
    assert constructor.call_count == 0 and invoker.calls == 1
    with reader(host) as repository:
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            repository._conn.execute('DELETE FROM team_v2_events')


def test_disabled_or_uninitialized_team_read_creates_nothing(tmp_path):
    host = TeamHost(tmp_path)
    before = list(tmp_path.iterdir())
    assert host.workbench_runs() == [] and list(tmp_path.iterdir()) == before


def test_draft_team_does_not_initialize_runtime_when_read(tmp_path):
    host = TeamHost(tmp_path)
    host.enable()
    created = host.draft(definition=template('data'), objective='等待开始', budget_ref='draft-budget', name='草稿团队', command_id='draft')
    run = created['run']['team_run_id']
    host.close()
    before = (host.state / 'coordination.sqlite3').read_bytes()
    snapshot = host.snapshot(run, read_only=True)
    view = task_view.team_view(snapshot)
    assert view['task']['status'] == '等待开始' and len(view['plan'][0]['steps']) == 2
    assert host.workbench_runs()[0]['run']['team_run_id'] == run
    assert (host.state / 'coordination.sqlite3').read_bytes() == before


@pytest.mark.asyncio
async def test_team_api_selection_is_project_and_desktop_scoped(task_api_env, monkeypatch):
    from pathlib import Path
    _, _, _, owner, _ = task_api_env
    # Fixture construction invokes asyncio.run; do it outside this event loop.
    host, run, _, _ = await asyncio.to_thread(create_team, Path(owner['project']))
    monkeypatch.setattr(server.app.state, 'team_v2_host', host)
    monkeypatch.setattr(server.app.state, 'team_v2_authorize', lambda request: True)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
            params = dict(conversation_id=owner['conversation_id'], run_id=run)
            first = await client.get('/api/workbench/view', params=params)
            assert first.status_code == 200, first.text
            head = first.json()['task']
            tasks = (await client.get('/api/workbench/tasks', params=owner)).json()['tasks']
            assert any(row['id'] == head['id'] and row['mode'] == 'team' for row in tasks)
            by_task = await client.get('/api/workbench/view', params=dict(conversation_id=owner['conversation_id'], task_id=head['id']))
            assert by_task.json() == first.json()
            stale = await client.get('/api/workbench/view', params=dict(params, task_id='team:other'))
            assert stale.status_code == 409
            monkeypatch.setattr(server.app.state, 'team_v2_authorize', lambda request: False)
            assert (await client.get('/api/workbench/view', params=params)).status_code == 404
            assert not any(row['mode'] == 'team' for row in (await client.get('/api/workbench/tasks', params=owner)).json()['tasks'])
            monkeypatch.setattr(server.app.state, 'team_v2_authorize', lambda request: True)
            monkeypatch.setattr(host, 'root', host.root / 'another-project')
            assert (await client.get('/api/workbench/view', params=params)).status_code == 404
    finally:
        host.close()
