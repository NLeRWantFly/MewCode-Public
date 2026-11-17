"""Explicit host re-selection after plan-only/revoked design, without business IO."""
import asyncio
import copy

import httpx
import pytest

from frontend import server
from mewcode.orchestration.task_design import TaskDesign, execution_scope_error
from mewcode.permissions import PermissionMode
from tests.test_task_design_entry import task_api_env
from tests.test_task_design_resume import _mock_main_chat_dispatch, _settle
from tests.test_task_design_spec import _queued_design, _expected_build


@pytest.mark.parametrize('saved', [False, True])
def test_confirmation_preserves_design_and_fences_prior_callbacks(tmp_path, saved):
    design, queued = _queued_design(tmp_path)
    building = design.claim_build(queued)
    expected = _expected_build(building)
    if saved:
        with design.commit_build(expected) as receipt:
            receipt['spec'] = {'meta': {'pipeline_graph_version': 2}}
        current = design.read()
    else:
        current = design.control('pause', task_id=building['task_id'], revision=building['revision'])
    before = design.store.get()
    confirmed = design.confirm_execution(task_id=current['task_id'], revision=current['revision'])
    assert confirmed['task_id'] == current['task_id']
    assert confirmed['goal'] == current['goal'] and confirmed['orchestration_mode'] == current['orchestration_mode']
    assert confirmed['intent'] == 'execute' and confirmed['status'] == 'designing'
    assert confirmed['specification'] == current['specification']
    assert design.store.get()['materials'] == before['materials']
    assert confirmed['build_request']['state'] == 'superseded'
    assert confirmed['build_request']['epoch'] > current['build_request']['epoch']
    assert confirmed['intent_confirmations'][0]['previous_authority'] == current['intent_authority']
    assert confirmed['intent_authority']['confirmed_revision'] == current['revision']
    assert not execution_scope_error(confirmed)
    with pytest.raises(ValueError):
        design.check_build(expected)
    with pytest.raises(ValueError):
        design.claim_execution(confirmed)
    with pytest.raises(ValueError):
        design.confirm_execution(task_id=current['task_id'], revision=current['revision'])


def test_plain_continue_cannot_restore_revoked_intent_but_host_confirmation_can(tmp_path):
    design = TaskDesign(tmp_path, 'revoked')
    design.start('execute')
    restricted = design.record_turn('只设计保存，不执行，不上传资料。')
    design.record_turn('继续', control='continue')
    assert execution_scope_error(design.read())
    confirmed = design.confirm_execution(task_id=restricted['task_id'], revision=restricted['revision'])
    assert not execution_scope_error(confirmed)
    assert confirmed['intent_confirmations'][0]['previous_authority']['revoked'] is True
    assert '不上传资料' in confirmed['goal']
    with pytest.raises(ValueError, match='无需重复确认'):
        design.confirm_execution(task_id=confirmed['task_id'], revision=confirmed['revision'])
    assert execution_scope_error(design.record_turn('不要执行'))


@pytest.mark.parametrize('extra', [
    {'status': 'queued'}, {'status': 'building'}, {'status': 'running'}, {'status': 'completed'},
    {'run_id': 'old-run'}, {'build_request': {'execution_state': 'dispatching'}},
    {'build_request': {'execution_state': 'started'}}, {'build_request': {'execution_claim_id': 'old-claim'}},
])
def test_confirmation_cannot_duplicate_or_replace_active_work(tmp_path, extra):
    design = TaskDesign(tmp_path, 'in-flight')
    value = design.start('plan')
    value = design.transition(value, extra.get('status', 'paused'), 'fixture',
                              **{key: val for key, val in extra.items() if key != 'status'})
    with pytest.raises(ValueError):
        design.confirm_execution(task_id=value['task_id'], revision=value['revision'])
    assert design.read() == value


@pytest.mark.asyncio
@pytest.mark.parametrize('intent', ['plan', 'revoked'])
async def test_host_confirmation_resumes_same_design_once(task_api_env, monkeypatch, intent):
    _, _, _, owner, _ = task_api_env
    runtime, _, dispatches, attached = _mock_main_chat_dispatch(monkeypatch, owner)
    design = TaskDesign(owner['project'], owner['conversation_id'])
    design.start('execute' if intent == 'revoked' else 'plan', orchestration_mode='collaborative')
    value = design.record_turn('本次只设计保存，不上传资料。')
    payload = {**owner, 'action': 'confirm_execute', 'confirm_execution': True,
               'expected_task_id': value['task_id'], 'expected_revision': value['revision']}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        first = await client.post('/api/orchestration/task-design', json=payload)
        assert first.status_code == 200, first.text
        result = first.json()
        assert result['task_design']['task_id'] == value['task_id']
        assert result['task_design']['goal'] == value['goal']
        assert result['task_design']['orchestration_mode'] == 'collaborative'
        assert result['task_design']['intent_authority']['action'] == 'confirm_execute'
        assert result['continuation']['kind'] == 'design'
        await asyncio.sleep(0)
        assert (await client.post('/api/orchestration/task-design', json=payload)).status_code == 409
        await _settle(attached)
    assert len(dispatches) == 1 and dispatches[0]['record_user'] is False
    assert any('只更新是否执行' in message.content for message in runtime.conversation.history)
    assert design.read()['status'] == 'designing'


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['missing_confirmation', 'stale', 'foreign', 'busy', 'plan_mode'])
async def test_bad_confirmation_is_rejected_before_mutation(task_api_env, monkeypatch, fault):
    active, _, _, owner, _ = task_api_env
    _, _, dispatches, attached = _mock_main_chat_dispatch(monkeypatch, owner)
    design = TaskDesign(owner['project'], owner['conversation_id'])
    value = design.start('plan')
    payload = {**owner, 'action': 'confirm_execute', 'confirm_execution': True,
               'expected_task_id': value['task_id'], 'expected_revision': value['revision']}
    if fault == 'missing_confirmation': payload.pop('confirm_execution')
    if fault == 'stale': payload['expected_revision'] -= 1
    if fault == 'foreign': payload['conversation_id'] = 'other'
    if fault == 'busy': active['busy'] = True
    if fault == 'plan_mode': monkeypatch.setattr(server.SESSION, 'permission_mode', PermissionMode.PLAN)
    before = copy.deepcopy(design.store.get())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        response = await client.post('/api/orchestration/task-design', json=payload)
    assert response.status_code == 409, response.text
    assert design.store.get() == before
    assert not dispatches and not attached


def test_reviewer_sees_new_host_choice_and_retains_original_constraints(tmp_path):
    from tests.test_task_design_review_contract import _fixture
    agent, _, _, _, _, design = _fixture(tmp_path)
    old = agent._host_task_review_scope()
    value = design.read()
    design.confirm_execution(task_id=value['task_id'], revision=value['revision'])
    current = agent._host_task_review_scope()
    assert current['intent'] == 'execute'
    assert current['intent_authority']['action'] == 'confirm_execute'
    assert current['revision'] != old['revision']
    assert current['user_sources'] == old['user_sources']
    assert '不上传资料' in current['user_sources'][0]['text']


@pytest.mark.asyncio
async def test_confirmation_keeps_recoverable_pause_when_chat_handoff_fails(task_api_env, monkeypatch):
    _, _, _, owner, _ = task_api_env
    _mock_main_chat_dispatch(monkeypatch, owner)
    design = TaskDesign(owner['project'], owner['conversation_id'])
    design.start('plan')
    value = design.record_turn('整理已有文件，完成任务设计')
    def unavailable():
        raise RuntimeError('fixture runtime unavailable')
    monkeypatch.setattr(server.SESSION, 'capture_conversation_runtime', unavailable)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        response = await client.post('/api/orchestration/task-design', json={**owner,
            'action': 'confirm_execute', 'confirm_execution': True,
            'expected_task_id': value['task_id'], 'expected_revision': value['revision']})
    assert response.status_code == 409
    current = design.read()
    assert current['status'] == 'paused' and current['intent'] == 'execute'
    assert current['intent_authority']['action'] == 'confirm_execute'
    assert 'fixture runtime unavailable' in current['reason']
    assert current['task_id'] == value['task_id'] and not current.get('run_id')
