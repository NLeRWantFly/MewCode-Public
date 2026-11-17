import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from mewcode.config import ProviderConfig
from mewcode.permissions.review_client import bind_safety_reviewer
from mewcode.permissions.reviewer import SafetyReviewer
from mewcode.plugins import ApplicationHost
from mewcode.plugins.runtime import AgentRuntimeRequest
from mewcode.plugins.services import AGENT_FACTORY, PROVIDERS
from mewcode.tools.base import ToolResult
from tests.test_permissions import MockLLMClient
from tests.test_typesafe_review import make_client, response_for
from tests.test_visual_review_packets import Vision, conversation_with_images


REQUEST = dict(tool_name='Bash', arguments={'command': 'python -c "print(1)"'},
               risk={}, conversation=None)


def write_selection(path, model='jev-latest', **extra):
    path.write_text(json.dumps({'model_id': model, 'provider_key': 'desktop:jev', **extra}), encoding='utf-8')


@pytest.mark.asyncio
async def test_selection_hot_switches_auto_review_for_parent_and_child(tmp_path, monkeypatch):
    from frontend import server
    from mewcode.permissions import review_client
    from mewcode.permissions import PermissionMode
    from mewcode.permissions.policy import build_permission_checker

    monkeypatch.setattr("mewcode.sandbox.windows.native_runtime_ready", lambda: False)
    monkeypatch.setattr("mewcode.sandbox.native_recovery.recover_native_runtime", lambda: None)

    settings = tmp_path / 'review-model.json'
    monkeypatch.setattr(review_client, 'review_model_path', lambda: settings)
    monkeypatch.setattr(server, 'REVIEW_MODEL_FILE', settings)
    calls = []
    def handler(request):
        calls.append(json.loads(request.content)['model'])
        return httpx.Response(200, json=response_for(request))

    host = ApplicationHost(str(tmp_path), discover_plugins=False)
    await host.astart()
    registry = host.context.get(PROVIDERS)
    def resolve(ref, *, usage_recorder=None):
        if ref.provider_key != 'desktop:jev':
            raise ValueError('Provider unavailable')
        client = make_client(handler)
        client.model = ref.model_id
        client.set_usage_recorder(usage_recorder)
        return SimpleNamespace(client=client, capabilities=('structured-review',))
    monkeypatch.setattr(registry, 'resolve', resolve)
    monkeypatch.setattr(server, 'MODEL_REGISTRY', registry)
    provider = ProviderConfig(name='executor', protocol='openai-compat',
                              model='executor-model', base_url='http://localhost', api_key='test-only')
    runtimes = []
    try:
        for label in ('parent', 'child'):
            work = tmp_path if label == 'parent' else tmp_path / label
            work.mkdir(exist_ok=True)
            checker = build_permission_checker(work, mode=PermissionMode.AUTO,
                parent_policy=runtimes[0].agent.permission_checker if runtimes else None)
            client = MockLLMClient([])
            runtime = await host.context.get(AGENT_FACTORY).acreate(AgentRuntimeRequest(
                provider=provider, work_dir=str(work), client=client, auxiliary_client=client,
                permission_mode=PermissionMode.AUTO, permission_checker=checker,
                enable_memory=False, enable_skills=False))
            runtime.agent.session_id = label
            runtime.registry.get('Bash').run = AsyncMock(return_value=ToolResult(output='1'))
            runtimes.append(runtime)
        # Both agents existed before the UI changed the model.
        monkeypatch.setattr(server, 'SESSION', SimpleNamespace(
            agent=runtimes[0].agent, client=runtimes[0].client))
        http = TestClient(server.app)
        assert http.post('/api/settings/review-model', json={
            'model_id': 'jev-latest', 'provider_key': 'desktop:jev'}).status_code == 200
        for runtime in runtimes:
            bash = runtime.registry.get('Bash')
            boundary = runtime.agent.permission_checker.check(bash, REQUEST['arguments'])
            if runtime.agent.session_id == 'child':
                assert (boundary.effect, boundary.source, boundary.subcondition) == (
                    'deny', 'native_boundary_unverified', 'native_boundary_not_attested')
            else:
                assert boundary.effect == 'review'
            assert runtime.agent.safety_reviewer is not None
            # Exercise the live binding directly; the planned Bash call is never run.
            result = await runtime.agent.safety_reviewer.review(**REQUEST)
            assert result.allowed and result.review_route['configured_model'] == 'jev-latest'
            assert runtime.agent.permission_checker.check(bash, REQUEST['arguments']).effect == boundary.effect
            bash.run.assert_not_awaited()
        assert calls == ['jev-latest', 'jev-latest']
        assert http.post('/api/settings/review-model', json={
            'model_id': 'jev-preview', 'provider_key': 'desktop:jev'}).status_code == 200
        # Existing parent and child reviewers pick up the selection without rebuild.
        for runtime in runtimes:
            result = await runtime.agent.safety_reviewer.review(**REQUEST)
            assert result.allowed and result.review_route['configured_model'] == 'jev-preview'
            runtime.registry.get('Bash').run.assert_not_awaited()
        assert calls == ['jev-latest', 'jev-latest', 'jev-preview', 'jev-preview']
    finally:
        for runtime in runtimes:
            await runtime.aclose()
        await host.aclose()


@pytest.mark.asyncio
async def test_switch_invalidates_old_denial_and_failure_never_uses_executor(tmp_path):
    settings = tmp_path / 'review.json'
    calls, receipts = [], []
    fallback = SimpleNamespace(model='grok-4.6', review_safety=AsyncMock())
    def resolve(ref, *, usage_recorder=None):
        if ref.model_id == 'unavailable':
            raise ValueError('unavailable')
        def handler(request):
            calls.append(ref.model_id)
            overrides = {'scope': 'no'} if ref.model_id == 'deny-model' else {}
            return httpx.Response(200, json=response_for(request, overrides=overrides))
        client = make_client(handler)
        client.set_usage_recorder(usage_recorder)
        return SimpleNamespace(client=client)
    reviewer = SafetyReviewer(fallback, str(tmp_path))
    agent = SimpleNamespace(safety_reviewer=reviewer, auxiliary_client=fallback,
                            client=SimpleNamespace(get_usage_recorder=lambda: receipts.append))
    bind_safety_reviewer(agent, SimpleNamespace(resolve=resolve), settings_path=settings)
    write_request = {
        'tool_name': 'WriteFile',
        'arguments': {'file_path': str(tmp_path / 'draft.txt'), 'content': 'synthetic draft'},
        'risk': {},
        'conversation': None,
    }
    write_selection(settings, 'deny-model')
    assert not (await reviewer.review(**write_request)).allowed
    assert (await reviewer.review(**write_request)).cache_hit
    write_selection(settings, 'jev-latest')
    assert (await reviewer.review(**write_request)).allowed
    assert calls == ['deny-model', 'jev-latest'] and len(receipts) == 2
    write_selection(settings, 'unavailable')
    result = await reviewer.review(**REQUEST)
    assert result.unavailable and result.review_route['configured_model'] == 'unavailable'
    settings.write_text('{broken', encoding='utf-8')
    assert (await reviewer.review(**REQUEST)).unavailable
    fallback.review_safety.assert_not_called()


@pytest.mark.asyncio
async def test_live_visual_selection_and_inflight_review_are_isolated(tmp_path):
    settings = tmp_path / 'review.json'
    entered, release = asyncio.Event(), asyncio.Event()
    text = SimpleNamespace(review_only=True)
    async def review_safety(*args, **kwargs):
        entered.set()
        await release.wait()
        return {'outcome': 'allow', 'risk_level': 'low', 'review_route': {'model': 'jev-actual'}}
    text.review_safety = review_safety
    visual = Vision()
    calls = []
    def resolve(ref, **kwargs):
        calls.append(ref.model_id)
        return SimpleNamespace(client=text if ref.model_id.startswith('jev') else visual,
                               capabilities=('vision',))
    reviewer = SafetyReviewer(None, str(tmp_path))
    agent = SimpleNamespace(safety_reviewer=reviewer, client=None)
    bind_safety_reviewer(agent, SimpleNamespace(resolve=resolve), settings_path=settings)
    write_selection(settings, vision_model_id='visual-a', vision_provider_key='vision')
    pending = asyncio.create_task(reviewer.review(**REQUEST))
    await asyncio.wait_for(entered.wait(), 2)
    write_selection(settings, 'jev-preview', vision_model_id='visual-b', vision_provider_key='vision')
    release.set()
    result = await pending
    assert result.review_route['configured_model'] == 'jev-latest'
    conversation = conversation_with_images(1, root=tmp_path)
    visual_request = {
        'tool_name': 'ReadAuditArtifact',
        'arguments': {'file_path': str(tmp_path / 'p0.png')},
        'risk': {'requires_vision': True},
        'conversation': conversation,
    }
    result = await reviewer.review(**visual_request)
    assert result.allowed and result.review_route['provider_key'] == 'desktop:jev'
    assert result.review_route['pixel_access'] == 'inspected'
    assert result.review_route['channels'] == ['pixel', 'text']
    assert calls[-2:] == ['jev-preview', 'visual-b']
    assert result.review_route['model'] == 'jev-actual'
    assert result.review_route['pixel_models'] == ['selected-vision']
    assert [call['phase'] for call in visual.calls] == [1]
    packet = visual.calls[0]
    assert packet['payload']['planned_action']['tool'] == 'ReadAuditArtifact'
    assert packet['payload']['planned_action']['arguments']['file_path'] == str(tmp_path / 'p0.png')
    assert packet['images'] == [{'type': 'image', 'mime_type': 'image/png', 'data': 'pixels-0'}]
    pixel_row = next(row for row in packet['payload']['visual_evidence']['manifest'] if row['channel'] == 'pixel')
    assert pixel_row['sources'][0]['label'].startswith('FILE: p0.png')
    assert 'visual-a' not in calls and 'visual-b' in calls


def test_invalid_ui_selection_does_not_replace_valid_settings(tmp_path, monkeypatch):
    from frontend import server
    settings = tmp_path / 'review.json'
    write_selection(settings)
    monkeypatch.setattr(server, 'REVIEW_MODEL_FILE', settings)
    def resolve(*args, **kwargs):
        raise ValueError('Provider unavailable')
    monkeypatch.setattr(server, 'MODEL_REGISTRY', SimpleNamespace(resolve=resolve))
    saved = settings.read_bytes()
    client = TestClient(server.app)
    for body in ({'model_id': 'jev-latest'}, {'provider_key': 'missing', 'model_id': 'jev-latest'}):
        assert client.post('/api/settings/review-model', json=body).status_code == 400
        assert settings.read_bytes() == saved


@pytest.mark.asyncio
async def test_invalid_verdict_still_records_received_usage():
    records = []
    def handler(request):
        result = response_for(request)
        result['answers'].pop('scope')
        return httpx.Response(200, json=result)
    client = make_client(handler)
    client.set_usage_recorder(records.append)
    with pytest.raises(ValueError):
        await client.review_safety({}, 'policy')
    assert len(records) == 1 and records[0].tokens.input_tokens == 120


@pytest.mark.asyncio
async def test_selected_visual_capable_reviewer_can_handle_images_itself(tmp_path):
    settings = tmp_path / 'review.json'
    write_selection(settings, 'user-selected-vision-model')
    visual = Vision()
    resolved_models = []
    def resolve(ref, **kwargs):
        resolved_models.append(ref.model_id)
        return SimpleNamespace(client=visual, capabilities=('vision',))
    registry = SimpleNamespace(resolve=resolve)
    reviewer = SafetyReviewer(None, str(tmp_path))
    bind_safety_reviewer(SimpleNamespace(safety_reviewer=reviewer, client=None), registry, settings_path=settings)
    conversation = conversation_with_images(1, root=tmp_path)
    result = await reviewer.review(tool_name='ReadAuditArtifact',
        arguments={'file_path': str(tmp_path / 'p0.png')},
        risk={'requires_vision': True}, conversation=conversation)
    assert result.allowed and result.review_route['provider_key'] == 'desktop:jev'
    assert result.review_route['pixel_access'] == 'direct'
    assert result.review_route['model'] == 'selected-vision'
    assert resolved_models == ['user-selected-vision-model']
    assert [call['phase'] for call in visual.calls] == [1]
    assert visual.calls[0]['images'] == [{'type': 'image', 'mime_type': 'image/png', 'data': 'pixels-0'}]
