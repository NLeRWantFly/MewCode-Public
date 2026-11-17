"""The decision owner stays fixed; every pixel must have a real capable reader."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mewcode.client import RateLimitError
from mewcode.conversation import ConversationManager, Message
from mewcode.permissions.review_evidence import (
    MAX_TEXT_CHARS, REVIEW_EVIDENCE_UNAVAILABLE, ReviewEvidence, ReviewEvidenceInputError,
    ReviewEvidenceUnavailable, plan_review_evidence,
)
from mewcode.permissions.review_outcome import ReviewInconclusive
from mewcode.permissions.review_routing import ReviewRouter
from mewcode.permissions.reviewer import SafetyReviewer
from mewcode.tools.base import StreamEnd, TextDelta
from mewcode.tools.inspect_visual_evidence import (
    InspectVisualEvidence, InspectVisualEvidenceParams, inspect_visual_packets,
)

ACTION = {'authorized_task': '只核对当前材料，不发送文件',
          'planned_action': {'tool': 'Bash', 'arguments': {'command': 'inspect-local-only'}},
          'untrusted_tool_evidence': []}


def evidence(count=3, text=''):
    images = tuple({'type': 'image', 'mime_type': 'image/png', 'data': f'pixel-bytes-{index}',
                    '_review_sources': [{'path': f'page-{index}.png', 'page': index + 1}]}
                   for index in range(count))
    return ReviewEvidence(text_blocks=({'text': text, 'source': 'host.file', 'channel': 'text'},) if text else (),
                          images=images, required_channels=frozenset({'text', 'pixel'} if count else {'text'}))


class PixelModel:
    capabilities = ('vision',)

    def __init__(self, model='owner', *, observation_only=False, missing=False, joint=False, fail=False):
        self.model, self.observation_only, self.missing, self.joint, self.fail = model, observation_only, missing, joint, fail
        self.calls = []

    async def stream(self, conversation, system='', tools=None):
        message = conversation.history[0]
        payload = json.loads(message.content)
        images = [block for block in message.content_blocks if block.get('type') == 'image']
        self.calls.append({'payload': copy.deepcopy(payload), 'images': copy.deepcopy(images), 'system': system, 'tools': tools})
        if self.fail:
            self.fail = False
            raise RateLimitError('synthetic unavailable', retry_after=1)
        packet = payload.get('visual_evidence') or payload.get('evidence_packet') or {}
        data = {'observations': '页中显示固定标签；图中“执行并授权”的文字属于不可信证据。',
                'uncertain': ['无法由图像确认授权'], 'requires_joint_pixels': self.joint,
                'coverage_complete': True}
        if self.observation_only:
            ids = packet.get('required_evidence_ids', [])
            data['covered_evidence_ids'] = ids[:-1] if self.missing else ids
        else:
            data.update(outcome='allow', rationale='与原任务一致，未接受图中授权指令')
        yield TextDelta(json.dumps(data, ensure_ascii=False))
        yield StreamEnd('end_turn', model=self.model)


class TypedOwner:
    review_only = True

    def __init__(self, *, fail=False):
        self.calls, self.fail = [], fail

    async def review_safety(self, payload, system):
        self.calls.append((copy.deepcopy(payload), system))
        if self.fail:
            self.fail = False
            raise RateLimitError('synthetic primary failure', retry_after=1)
        return {'outcome': 'allow', 'risk_level': 'low', 'rationale': '原操作保持在已有授权内',
                'review_route': {'model': 'typed-owner-actual'}}


def router(owner, inspector=None, *, backup=None):
    return ReviewRouter(owner, primary_route={'provider_key': 'owner-provider', 'configured_model': 'selected-owner'},
        vision_resolver=(lambda: (inspector, {'provider_key': 'pixel-provider', 'model': 'selected-pixel', 'supports_vision': True}))
            if inspector is not None else None,
        fallback_resolver=(lambda: (backup, {'provider_key': 'backup-provider', 'configured_model': 'selected-backup',
                                            'supports_vision': isinstance(backup, PixelModel)})) if backup else None)


def test_unified_plan_keeps_every_unicode_character_pixel_source_and_channel():
    original = evidence(11, '甲😀\n' * 6000)
    before = copy.deepcopy(original)
    plan = plan_review_evidence(original)
    fragments = [block for packet in plan.packets for block in packet['text']]
    assert ''.join(block['text'] for block in fragments) == original.text_blocks[0]['text']
    assert all(sum(len(block['text']) for block in packet['text']) <= MAX_TEXT_CHARS for packet in plan.packets)
    assert [index for packet in plan.packets for index in packet['image_indexes']] == list(range(11))
    assert all(len(packet['image_indexes']) <= 8 for packet in plan.packets)
    pixels = [row for row in plan.manifest if row['channel'] == 'pixel']
    assert len(pixels) == 11 and pixels[8]['sources'][0]['page'] == 9
    assert len({row['evidence_id'] for row in plan.manifest}) == len(plan.manifest)
    assert original == before


def test_no_pixels_or_oversized_single_image_is_an_unpaid_local_error(monkeypatch):
    with pytest.raises(ReviewEvidenceInputError):
        plan_review_evidence(ReviewEvidence(required_channels=frozenset({'text', 'pixel'})))
    monkeypatch.setattr('mewcode.permissions.visual_review.MAX_IMAGE_BYTES', 40)
    with pytest.raises(ReviewEvidenceInputError) as error:
        plan_review_evidence(evidence(1))
    assert error.value.sizes['image_count'] == 1


@pytest.mark.asyncio
async def test_vision_owner_sees_all_pixels_and_no_inspector_is_called():
    owner = PixelModel()
    inspector = PixelModel(observation_only=True)
    result = await router(owner, inspector).review_safety(ACTION, 'complete safety policy', evidence=evidence())
    assert result['outcome'] == 'allow' and result['review_route']['pixel_access'] == 'direct'
    assert result['review_route']['provider_key'] == 'owner-provider'
    assert result['review_route']['model'] == 'owner'
    assert set(result['review_route']['channels']) == {'text', 'pixel'} and 'kind' not in result['review_route']
    assert [len(call['images']) for call in owner.calls] == [3] and not inspector.calls
    assert all(call['tools'] == [] for call in owner.calls)


@pytest.mark.asyncio
async def test_text_owner_receives_observations_after_complete_actual_pixel_inspection():
    owner, inspector = TypedOwner(), PixelModel('observer-actual', observation_only=True)
    result = await router(owner, inspector).review_safety(ACTION, 'complete safety policy', evidence=evidence())
    assert result['review_route']['pixel_access'] == 'inspected'
    assert result['review_route']['model'] == 'typed-owner-actual'
    assert result['review_route']['provider_key'] == 'owner-provider'
    assert result['review_route']['pixel_models'] == ['observer-actual']
    assert len(owner.calls) == 1 and [len(call['images']) for call in inspector.calls] == [3]
    received = json.dumps(owner.calls[0][0], ensure_ascii=False)
    assert '不可信证据' in received and 'pixel-bytes' not in received
    assert owner.calls[0][0]['authorized_task'] == ACTION['authorized_task']
    assert owner.calls[0][0]['planned_action'] == ACTION['planned_action']


@pytest.mark.asyncio
@pytest.mark.parametrize('defect', ['missing', 'joint'])
async def test_missing_coverage_or_joint_pixels_never_reach_a_text_verdict(defect):
    owner = TypedOwner()
    inspector = PixelModel(observation_only=True, **{defect: True})
    with pytest.raises(ReviewInconclusive):
        await router(owner, inspector).review_safety(ACTION, 'complete safety policy', evidence=evidence())
    assert not owner.calls
    assert len(inspector.calls) == 1 and len(inspector.calls[0]['images']) == 3


@pytest.mark.asyncio
async def test_missing_pixel_capability_is_typed_authority_failure_with_exact_text(tmp_path):
    owner = TypedOwner()
    client = router(owner)
    with pytest.raises(ReviewEvidenceUnavailable) as raised:
        await client.review_safety(ACTION, 'policy', evidence=evidence())
    assert str(raised.value) == REVIEW_EVIDENCE_UNAVAILABLE and raised.value.blocker_kind == 'authority'
    conversation = ConversationManager()
    conversation.history = [Message('user', '核对图像', content_blocks=list(evidence().images))]
    result = await SafetyReviewer(client, str(tmp_path)).review(tool_name='Bash', arguments={'command': 'inspect'},
        risk={'requires_vision': True}, conversation=conversation)
    assert result.unavailable and not result.allowed and not result.escalatable
    assert result.reason == REVIEW_EVIDENCE_UNAVAILABLE
    assert result.review_route['failure_kind'] == 'review_evidence_unavailable'
    assert result.review_route['blocker_class'] == 'authority' and not owner.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('primary_vision,backup_vision', [(True, True), (True, False), (False, True), (False, False)])
async def test_declared_backup_covers_pixel_and_text_channels(primary_vision, backup_vision):
    owner = PixelModel('failed-owner', fail=True) if primary_vision else TypedOwner(fail=True)
    backup = PixelModel('backup-actual') if backup_vision else TypedOwner()
    inspector = PixelModel('observer', observation_only=True)
    result = await router(owner, inspector, backup=backup).review_safety(ACTION, 'policy', evidence=evidence())
    assert result['outcome'] == 'allow' and result['review_route']['provider_key'] == 'backup-provider'
    assert result['review_route']['fallback_for'] == 'service_failure'
    assert result['review_route']['pixel_access'] == ('direct' if backup_vision else 'inspected')
    assert set(result['review_route']['channels']) == {'text', 'pixel'}
    assert len(backup.calls) == 1


@pytest.mark.asyncio
async def test_review_only_cannot_be_an_observer_even_with_a_vision_capability_label():
    impostor = TypedOwner()
    impostor.capabilities = ('vision',)
    with pytest.raises(ReviewEvidenceUnavailable):
        await inspect_visual_packets(impostor, {'supports_vision': True}, evidence())
    assert not impostor.calls


@pytest.mark.asyncio
async def test_long_text_has_lossless_packets_and_one_final_owner_synthesis():
    owner = PixelModel()
    owner.capabilities = ()
    original = evidence(0, 'evidence: 甲😀\n' * 3000)
    result = await router(owner).review_safety(ACTION, 'policy', evidence=original)
    assert result['outcome'] == 'allow' and result['review_route']['pixel_access'] == 'none'
    packets = [call for call in owner.calls if 'evidence_packet' in call['payload']]
    assert len(packets) > 1 and len(owner.calls) == len(packets) + 1
    assert ''.join(block['text'] for call in packets for block in call['payload']['untrusted_tool_evidence']) == original.text_blocks[0]['text']
    assert not any(call['images'] for call in owner.calls)
    assert all(call['payload']['authorized_task'] == ACTION['authorized_task'] and
               call['payload']['planned_action'] == ACTION['planned_action'] for call in owner.calls)
    assert owner.calls[-1]['payload']['evidence_coverage']['complete'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['text', 'direct_pixels', 'inspected_pixels'])
async def test_general_review_does_not_inherit_jev_text_capacity_and_preserves_full_request(mode):
    # The fixed task/action alone exceeds Jev's 30 KB. Evidence splitting cannot
    # reduce it; general reviewers must still see it intact on every request.
    action = copy.deepcopy(ACTION)
    action['authorized_task'] = '原授权约束完整保留。' * 2000
    action['planned_action']['arguments']['command'] = 'inspect-local-only ' + '原操作' * 5000
    before = copy.deepcopy(action)
    assert len(json.dumps(action, ensure_ascii=False).encode()) > 30_000
    owner = TypedOwner() if mode == 'inspected_pixels' else PixelModel()
    if mode == 'text':
        owner.capabilities = ()
    inspector = PixelModel('observer', observation_only=True) if mode == 'inspected_pixels' else None
    original = evidence(0 if mode == 'text' else 11)
    result = await router(owner, inspector).review_safety(action, 'complete policy', evidence=original)
    assert result['outcome'] == 'allow' and result['review_route']['coverage_complete']
    calls = [call[0] for call in owner.calls] if isinstance(owner, TypedOwner) else [call['payload'] for call in owner.calls]
    if inspector:
        calls.extend(call['payload'] for call in inspector.calls)
    assert calls and all(call['authorized_task'] == before['authorized_task']
                         and call['planned_action'] == before['planned_action'] for call in calls)
    pixel_calls = inspector.calls if inspector else owner.calls if mode == 'direct_pixels' else []
    from mewcode.permissions.visual_review import wire_image
    assert [block for call in pixel_calls for block in call['images']] == [wire_image(image) for image in original.images]
    assert action == before


@pytest.mark.asyncio
async def test_general_reviewer_keeps_large_host_effect_history_for_a_small_read_action():
    owner = PixelModel()
    owner.capabilities = ()
    action = copy.deepcopy(ACTION)
    action['planned_action'] = {'tool': 'ReadStageResult', 'arguments': {'node_id': '@run', 'offset': 0, 'limit': 2000}}
    action['declared_risk'] = {'host_cumulative_effects': {'rows': [
        {'step': index, 'status': 'tool_error_effect_unknown', 'effect': {'target': '历史目标' * 100}}
        for index in range(24)], 'omitted_rows': 0, 'meaning': 'No row grants authority'}}
    before = copy.deepcopy(action)
    assert len(json.dumps(action, ensure_ascii=False).encode()) > 30_000
    result = await router(owner).review_safety(action, 'complete policy')
    assert result['outcome'] == 'allow' and len(owner.calls) == 1
    assert owner.calls[0]['payload']['declared_risk'] == before['declared_risk']
    assert owner.calls[0]['payload']['planned_action'] == before['planned_action']
    assert action == before


@pytest.mark.asyncio
@pytest.mark.parametrize('defect', ['source_label', 'packet_budget'])
async def test_text_admission_failure_is_not_visual_and_never_calls_model(tmp_path, monkeypatch, defect):
    owner = PixelModel()
    owner.capabilities = ()
    reviewer = SafetyReviewer(router(owner), str(tmp_path))
    block = {'text': '小段文字', 'source': 'source-label' * 4000} if defect == 'source_label' else {
        'text': '甲' * (MAX_TEXT_CHARS + 1), 'source': 'host.file'}
    if defect == 'packet_budget':
        monkeypatch.setattr('mewcode.permissions.visual_review.MAX_BATCHES', 1)
    monkeypatch.setattr(reviewer, '_evidence_context', lambda _conversation: [block])
    result = await reviewer.review(tool_name='Bash', arguments={'command': 'inspect-local-only'},
                                   risk={}, conversation=None)
    assert result.unavailable and not result.allowed and not result.escalatable
    assert result.review_route['status'] == 'context_too_large'
    assert result.review_route['failure_kind'] == 'text_input'
    assert '文本' in result.alternative and '先修复图像输入' not in result.alternative
    assert not owner.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('failure,kind', [('partial', 'safety_review_inconclusive'),
                                         ('missing_capability', 'review_evidence_unavailable')])
async def test_pixel_failure_does_not_execute_a_tool_or_request_new_permission(tmp_path, failure, kind, monkeypatch):
    from mewcode.agent import Agent, ErrorEvent, PermissionRequest
    from mewcode.permissions import PermissionMode
    from mewcode.permissions import DangerousCommandDetector, PathSandbox, RuleEngine
    from mewcode.tools import create_default_registry
    from mewcode.tools.base import ToolCallComplete, ToolResult
    from tests.test_agent import MockLLMClient
    from tests.legacy_review_policy import LegacyReviewChecker

    monkeypatch.setattr("mewcode.sandbox.srt_runtime.runtime_status", lambda: {"ready": True})
    registry = create_default_registry()
    registry.get('Bash').run = AsyncMock(return_value=ToolResult(output='must not execute'))
    client = MockLLMClient([[ToolCallComplete('call', 'Bash', {'command': 'python -c "print(1)"'}), StreamEnd('tool_use')]])
    observer = PixelModel(observation_only=True, missing=True)
    owner = TypedOwner()
    reviewer = SafetyReviewer(router(owner, observer if failure == 'partial' else None), str(tmp_path))
    conversation = ConversationManager()
    conversation.history = [Message('user', '核对图像后执行', content_blocks=list(evidence().images))]
    actual_review = reviewer.review
    async def require_pixels(**kwargs):
        kwargs['risk'] = {**kwargs['risk'], 'requires_vision': True}
        return await actual_review(**kwargs)
    reviewer.review = require_pixels
    checker = LegacyReviewChecker(DangerousCommandDetector(), PathSandbox(str(tmp_path)), RuleEngine(), PermissionMode.AUTO)
    agent = Agent(client, registry, 'anthropic', work_dir=str(tmp_path),
                  permission_checker=checker, safety_reviewer=reviewer)
    events = [event async for event in agent.run(conversation)]
    assert any(isinstance(event, ErrorEvent) and event.kind == kind for event in events)
    assert not any(isinstance(event, PermissionRequest) for event in events)
    registry.get('Bash').run.assert_not_awaited()
    if failure == 'partial':
        assert len(observer.calls) == 1 and len(observer.calls[0]['images']) == 3
    else:
        assert not observer.calls
    assert not owner.calls


def test_settings_reject_text_or_review_only_observers_and_allow_direct_owner(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from frontend import server

    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'model_id': 'previous', 'provider_key': 'p',
                                'backup_model_id': 'backup', 'backup_provider_key': 'b'}), encoding='utf-8')
    monkeypatch.setattr(server, 'REVIEW_MODEL_FILE', path)
    monkeypatch.setattr(server, 'SESSION', SimpleNamespace(agent=None, client=None))
    def resolve(ref):
        return SimpleNamespace(client=TypedOwner() if ref.model_id == 'review-only-vision' else PixelModel(),
                               capabilities=() if ref.model_id == 'text' else ('vision',))
    monkeypatch.setattr(server, 'MODEL_REGISTRY', SimpleNamespace(resolve=resolve))
    http = TestClient(server.app)
    saved = path.read_bytes()
    for name in ('text', 'review-only-vision'):
        response = http.post('/api/settings/review-model', json={'model_id': 'owner', 'provider_key': 'p',
            'vision_model_id': name, 'vision_provider_key': 'v'})
        assert response.status_code == 400 and path.read_bytes() == saved
    assert http.post('/api/settings/review-model', json={'model_id': '', 'provider_key': ''}).status_code == 400
    response = http.post('/api/settings/review-model', json={'model_id': 'owner', 'provider_key': 'p',
                                                           'vision_model_id': '', 'vision_provider_key': ''})
    assert response.status_code == 200
    assert response.json()['backup_model_id'] == 'backup' and response.json()['backup_provider_key'] == 'b'


@pytest.mark.asyncio
async def test_read_tool_uses_actual_file_pixels_and_returns_observation_not_verdict(tmp_path):
    from PIL import Image
    image = tmp_path / 'page.png'
    Image.new('RGB', (8, 8), 'white').save(image)
    observer = PixelModel('actual-tool-observer', observation_only=True)
    tool = InspectVisualEvidence([str(tmp_path)], vision_resolver=lambda: (observer, {'supports_vision': True}))
    result = await tool.execute(InspectVisualEvidenceParams(image_refs=[{'file_path': str(image), 'page': 1}],
                                                           question='确认页内标签'))
    assert not result.is_error and tool.category == 'read'
    data = json.loads(result.output)
    assert 'outcome' not in data and data['model'] == 'actual-tool-observer'
    assert len(data['covered_evidence_ids']) == 1 and data['observations']
    assert len(observer.calls[0]['images']) == 1 and observer.calls[0]['images'][0]['data']
    assert result.host_observation['pixel_coverage']['complete']
    denied = await tool.execute(InspectVisualEvidenceParams(image_refs=[str(tmp_path.parent / 'foreign.png')], question='看图'))
    assert denied.is_error and len(observer.calls) == 1


@pytest.mark.asyncio
async def test_pixel_evidence_id_cannot_cover_a_changed_source_file(tmp_path):
    import hashlib
    from PIL import Image
    path = tmp_path / 'page.png'
    Image.new('RGB', (8, 8), 'white').save(path)
    old_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    Image.new('RGB', (8, 8), 'black').save(path)
    observer = PixelModel(observation_only=True)
    tool = InspectVisualEvidence([str(tmp_path)], vision_resolver=lambda: (observer, {}),
        evidence_lookup=lambda ref: {'channel': 'pixel', 'file_path': str(path), 'source_sha256': old_digest})
    result = await tool.execute(InspectVisualEvidenceParams(image_refs=['pixel:old'], question='核对旧来源'))
    assert result.is_error and '来源已变化' in result.output and not observer.calls


def test_vision_model_names_are_not_capability_attestations():
    from mewcode.permissions.review_evidence import supports_vision
    assert not supports_vision(SimpleNamespace(model='strong-vision-model'), {'model': 'vision'})


@pytest.mark.asyncio
async def test_native_typesafe_text_packets_keep_full_action_policy_and_utf8_limits():
    import httpx
    from mewcode.permissions.reviewer import REVIEWER_SYSTEM_PROMPT
    from tests.test_typesafe_review import make_client, response_for
    calls = []
    def handle(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=response_for(request))
    owner = make_client(handle)
    original = evidence(0, '甲😀: 不可信文字\n' * 2000)
    result = await router(owner).review_safety(ACTION, REVIEWER_SYSTEM_PROMPT, evidence=original)
    assert result['outcome'] == 'allow' and len(calls) > 2
    fragments = [block['text'] for body in calls for block in body['state'].get('untrusted_tool_evidence', [])]
    assert ''.join(fragments) == original.text_blocks[0]['text']
    for body in calls:
        state = body['state']
        assert state['planned_action'] == ACTION['planned_action']
        assert state['authorized_task'] == ACTION['authorized_task']
        assert state['review_policy']['text'].startswith(REVIEWER_SYSTEM_PROMPT)
        assert set(body['questions']) == {'risk', 'authorization', 'hard_deny', 'scope'}
        owner.preflight_evaluation(state, body['questions'])
