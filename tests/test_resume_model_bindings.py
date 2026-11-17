"""Resume honors checkpoint choices rather than unrelated project defaults."""
import copy
from types import SimpleNamespace

import httpx
import pytest

from frontend import server
from mewcode.expert_group import ExpertProfile
from mewcode.expert_web import expert_resume_model_error
from mewcode.kernel import collaboration as ledger
from mewcode.model_registry import ModelRef
from mewcode.orchestration import OrchestrationSpec
from mewcode.orchestration.convert import to_expert_flow
from tests.test_collaboration_mode import RUN_ID, store_for, accept
from tests.test_orchestration_start_run import confirm_env, _bound_spec

GOOD = ModelRef('chosen', 'chosen-model', 'high')
OLD = ModelRef('removed', 'old-model', 'high')


def resolve(ref):
    if ref.provider_key == OLD.provider_key:
        raise ValueError('provider is unavailable: removed')
    return SimpleNamespace(ref=ref)


def forbidden(*args, **kwargs):
    raise AssertionError('resume must not read or rewrite the project profile')


@pytest.mark.parametrize('available', [True, False])
@pytest.mark.asyncio
async def test_confirm_resume_checks_saved_user_override_without_project_profile(confirm_env, monkeypatch, available):
    raw = _bound_spec('继续原任务').as_dict()
    target = next(node for node in raw['nodes'] if node.get('model'))
    target['model'] = OLD.as_dict()
    flow = to_expert_flow(OrchestrationSpec.from_value(raw))
    checkpoint = dict(run_id='f'*32, conversation_id='confirm-test', status='paused',
        locked_flow=flow.as_dict(), locked_profile=ExpertProfile(manager=GOOD, executor=GOOD, auditor=OLD).as_dict(),
        locked_models={target['node_id']: (GOOD if available else OLD).as_dict()}, node_states={})
    before = copy.deepcopy(checkpoint)
    monkeypatch.setattr(server.EXPERT_RUNS, 'latest_resumable', lambda _cid: checkpoint)
    monkeypatch.setattr(server, 'EXPERT_PROFILES', SimpleNamespace(get=forbidden, put=forbidden))
    monkeypatch.setattr(server.MODEL_REGISTRY, 'resolve', resolve)
    monkeypatch.setattr(server.SESSION, 'set_expert_mode', type(server.SESSION).set_expert_mode.__get__(server.SESSION))
    monkeypatch.setattr(server.SESSION, 'conversation_runs', {})
    started = []
    async def start(_runtime, _text, **kwargs):
        started.append(kwargs)
    monkeypatch.setattr(server, 'start_runtime_turn', start)
    previous = server._conversation_plan_store().get(server.SESSION.work_dir)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        response = await client.post('/api/orchestration/confirm', json={'spec': raw, 'activate': True})
    if available:
        assert response.status_code == 200, response.text
        assert response.json()['execution_mode'] == 'resume'
        assert started == [{'resume_run_id': checkpoint['run_id']}]
    else:
        assert response.status_code == 400 and 'provider is unavailable: removed' in response.json()['error']
        assert not started
        assert server._conversation_plan_store().get(server.SESSION.work_dir) == previous
    assert checkpoint == before


def test_collaborative_resume_uses_current_node_binding_and_preserves_saved_acceptance(tmp_path):
    record = store_for(tmp_path).load(RUN_ID)
    accept(record['collaboration'], 'work')
    record['locked_profile']['auditor'] = OLD.as_dict()
    record['locked_profile']['executor'] = OLD.as_dict()
    record['role_models']['roles']['executor'] = OLD.as_dict()
    record['role_models']['roles']['verifier'] = OLD.as_dict()
    record['locked_models'] = {'deliver': GOOD.as_dict()}
    record['collaboration']['acceptance_model_overrides'] = {'deliver': GOOD.as_dict()}
    before = copy.deepcopy(record)
    assert expert_resume_model_error(SimpleNamespace(resolve=resolve), record) == ''
    assert record == before and ledger.valid_certificate(record['collaboration'], 'work')
    record['locked_models'].clear()
    assert 'provider is unavailable: removed' in expert_resume_model_error(SimpleNamespace(resolve=resolve), record)


def test_resume_does_not_silently_reduce_selected_effort(tmp_path):
    record = store_for(tmp_path, selected='classic').load(RUN_ID)
    record['locked_models'] = {'work': GOOD.as_dict()}
    registry = SimpleNamespace(resolve=lambda ref: SimpleNamespace(ref=ModelRef(ref.provider_key, ref.model_id, 'low')))
    assert '未自动降档' in expert_resume_model_error(registry, record)


def test_checkpoint_cannot_activate_another_conversation(confirm_env, monkeypatch):
    monkeypatch.setattr(server, 'EXPERT_PROFILES', SimpleNamespace(get=forbidden, put=forbidden))
    method = type(server.SESSION).set_expert_mode.__get__(server.SESSION)
    before = server.SESSION.expert_mode
    assert '不属于当前对话' in method(True, resume_checkpoint={'run_id': 'r', 'conversation_id': 'other'})
    assert server.SESSION.expert_mode == before


def test_new_expert_activation_still_requires_its_project_models(confirm_env, monkeypatch):
    profile = ExpertProfile(manager=GOOD, executor=GOOD, auditor=OLD)
    monkeypatch.setattr(server, 'EXPERT_PROFILES', SimpleNamespace(get=lambda _project: profile, put=forbidden))
    monkeypatch.setattr(server, 'relocate_expert_profile', lambda _registry, value: value)
    monkeypatch.setattr(server.MODEL_REGISTRY, 'resolve', resolve)
    method = type(server.SESSION).set_expert_mode.__get__(server.SESSION)
    assert '专家角色配置已失效' in method(True)
