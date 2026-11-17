"""A finished design turn must not leave the workbench claiming live work."""
from types import SimpleNamespace

import httpx
import pytest

from frontend import server
from mewcode.orchestration.task_design import TaskDesign
from tests.test_task_design_entry import task_api_env


def design_turn(owner):
    design = TaskDesign(owner['project'], owner['conversation_id'])
    design.start('plan')
    current = design.record_turn('整理扫描 PDF 的识别与校核流程')
    runtime = SimpleNamespace(work_dir=owner['project'], conversation_id=owner['conversation_id'],
                              execution_kind='chat')
    return design, current, runtime


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', [{}, {'interrupted': True}, {'error': True}],
                         ids=['finished', 'cancelled', 'failed'])
async def test_finished_design_turn_updates_header_list_and_notification(task_api_env, monkeypatch, outcome):
    _, _, _, owner, _ = task_api_env
    monkeypatch.setattr(server.SESSION, 'chat_busy', lambda *_: False)
    design, current, runtime = design_turn(owner)
    server.task_design_after_turn(runtime, **outcome)
    saved = design.read()
    assert saved['status'] == 'paused'
    assert saved['task_id'] == current['task_id'] and saved['goal'] == current['goal']
    assert saved['revision'] == current['revision'] + 1

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        view = (await client.get('/api/workbench/view', params=owner)).json()
        listed = (await client.get('/api/workbench/tasks', params=owner)).json()
    assert view['task']['status'] == '已暂停'
    assert view['task']['controls'] == ['resume']
    assert '正在' not in view['task']['current_activity']
    assert listed['tasks'][0]['status'] == '已暂停'
    notice = server.WORKBENCH_NOTIFIER.read(dict(kind='runtime', **owner))
    assert notice[0]['version'] == view['version']
    server.task_design_after_turn(runtime, **outcome)
    assert design.read()['revision'] == saved['revision'], 'duplicate completion must be a no-op'
    # An ordinary follow-up can continue the existing design.
    continued = design.record_turn('继续核对已有流程')
    assert continued['task_id'] == current['task_id'] and continued['status'] == 'designing'


@pytest.mark.asyncio
async def test_session_load_recovers_design_left_active_by_an_old_process(task_api_env, monkeypatch):
    _, _, _, owner, _ = task_api_env
    monkeypatch.setattr(server.SESSION, 'chat_busy', lambda *_: False)
    design, current, _ = design_turn(owner)
    server.recover_task_design_on_session_load(owner['project'], owner['conversation_id'])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        recovered = await client.get('/api/orchestration/task-design', params=owner)
        assert recovered.status_code == 200, recovered.text
        saved = recovered.json()['task_design']
        again = await client.get('/api/orchestration/task-design', params=owner)
        view = (await client.get('/api/workbench/view', params=owner)).json()
    assert saved['status'] == 'paused' and saved['revision'] == current['revision'] + 1
    assert again.json()['task_design']['revision'] == saved['revision']
    assert view['task']['status'] == '已暂停' and view['task']['controls'] == ['resume']
    assert design.read()['goal'] == current['goal']


@pytest.mark.asyncio
@pytest.mark.parametrize('worker', ['chat', 'graph'])
async def test_live_continuation_or_background_graph_is_not_paused(task_api_env, monkeypatch, worker):
    _, _, _, owner, _ = task_api_env
    observations = []
    def chat_busy(cid):
        observations.append(cid)
        return worker == 'chat' and cid == owner['conversation_id']
    monkeypatch.setattr(server.SESSION, 'chat_busy', chat_busy)
    monkeypatch.setattr(server, 'graph_generation_running',
                        lambda project, cid: worker == 'graph' and (project, cid) ==
                        (owner['project'], owner['conversation_id']))
    design, current, runtime = design_turn(owner)
    server.task_design_after_turn(runtime)
    server.recover_task_design_on_session_load(owner['project'], owner['conversation_id'])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        response = await client.get('/api/orchestration/task-design', params=owner)
    assert response.status_code == 200, response.text
    assert response.json()['task_design']['status'] == 'designing'
    assert design.read()['revision'] == current['revision']
    if worker == 'chat':
        assert observations and set(observations) == {owner['conversation_id']}


@pytest.mark.parametrize('status', ['needs_input', 'ready', 'failed', 'blocked', 'completed', 'paused'])
def test_design_completion_preserves_specific_outcomes(task_api_env, monkeypatch, status):
    _, _, _, owner, _ = task_api_env
    monkeypatch.setattr(server.SESSION, 'chat_busy', lambda *_: False)
    design, current, runtime = design_turn(owner)
    specific = design.transition(current, status, '保留已有结果')
    server.task_design_after_turn(runtime)
    assert design.read()['status'] == status
    assert design.read()['revision'] == specific['revision']
