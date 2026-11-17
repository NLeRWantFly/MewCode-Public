"""One judge with host-attested evidence channels and compatible item records."""
from __future__ import annotations

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from mewcode.orchestration.convert import from_expert_flow, to_expert_flow
from mewcode.orchestration.graph_lint import lint_graph, review_channels_for_node
from mewcode.orchestration.graph_completion import apply_completion
from mewcode.orchestration.pipeline_bridge import from_pipeline_graph, to_pipeline_graph
from mewcode.orchestration.spec import OrchestrationNode, OrchestrationSpec, OrchestrationError
from mewcode.orchestration.workflow_draft import seed_document, project_graph, validate_entry, DraftPatchError
from mewcode.tools.inspect_visual_evidence import InspectVisualEvidence, InspectVisualEvidenceParams
from mewcode.tools.review_work_queue import ReviewWorkQueue, ReviewWorkQueueParams
from tests.test_review_unified import PixelModel
from tests.test_review_work_queue import setup_queue


def spec(nodes):
    return OrchestrationSpec.from_value({'spec_id': 'channels', 'label': 'channels', 'goal': 'review',
        'nodes': nodes, 'edges': []})


def node(node_id='audit', **extra):
    return {'node_id': node_id, 'kind': 'audit', 'label': '审查', 'goal': '核对本次材料', **extra}


@pytest.mark.parametrize('facts,observer,expected', [({}, False, True),
    ({'audit': {'supports_vision': True}}, False, False),
    ({'audit': {'supports_vision': True, 'review_only': True}}, False, True),
    ({}, True, False)])
def test_l12_requires_attested_pixel_capability_or_configured_observer(facts, observer, expected, monkeypatch):
    monkeypatch.setattr('mewcode.permissions.review_client.load_review_model_settings',
                        lambda *_: pytest.fail('a pure linter read user settings'))
    graph = spec([node(review_channels=['text', 'pixel'])])
    findings = lint_graph(graph, model_capabilities=facts, visual_evidence_available=observer)
    errors = [item for item in findings if item.code == 'review_channel_capability']
    assert bool(errors) is expected
    if expected:
        assert errors[0].severity == 'error' and errors[0].entry_ids == ('audit',)
        assert errors[0].message == '节点 audit 需要查看图像，但所用模型不支持图片且未设置看图能力；请为节点选择支持图片的模型，或在设置中配置看图能力。'


def test_l13_rejects_channel_split_but_accepts_one_owner_or_distinct_business_objects():
    split = spec([node('visual', label='视觉审查', audit_target='work'),
                  node('text', label='文本审查', audit_target='work')])
    findings = [item for item in lint_graph(split) if item.code == 'split_review_by_channel']
    assert len(findings) == 1 and findings[0].entry_ids == ('visual', 'text')
    merged = spec([node(review_channels=['text', 'pixel'], audit_target='work')])
    assert not [item for item in lint_graph(merged, visual_evidence_available=True) if item.code == 'split_review_by_channel']
    distinct = spec([node('visual', label='视觉审查', audit_target='layout'),
                     node('text', label='文本审查', audit_target='wording')])
    assert not [item for item in lint_graph(distinct) if item.code == 'split_review_by_channel']


def test_channels_infer_from_actual_steps_and_roundtrip_with_audit_target():
    raw = node(plan_brief={'steps': ['核对 PDF 版式'], 'acceptance': []})
    assert set(review_channels_for_node(OrchestrationNode.from_value(raw))) == {'text', 'pixel'}
    raw['review_channels'] = ['pixel', 'command']
    raw['audit_target'] = 'work'
    graph = spec([{'node_id': 'work', 'kind': 'implement', 'label': 'work'}, raw])
    restored = from_expert_flow(to_expert_flow(graph)).node_map()['audit']
    assert restored.review_channels == ('text', 'pixel', 'command')
    assert restored.audit_target == 'work'
    assert restored.as_dict()['review_channels'] == ['text', 'pixel', 'command']
    canvas_restored = from_pipeline_graph(to_pipeline_graph(graph)).node_map()['audit']
    assert canvas_restored.review_channels == restored.review_channels
    assert canvas_restored.audit_target == restored.audit_target
    with pytest.raises(OrchestrationError, match='review_channels'):
        OrchestrationNode.from_value(node(review_channels=['ocr']))


def test_draft_seed_projection_preserves_channels_and_rejects_unknown_channels():
    graph = spec([node(review_channels=['text', 'pixel', 'command'])])
    draft = seed_document(graph.as_dict())
    seeded = next(e for e in draft['entries'] if e['id'] == 'audit')
    assert seeded['data']['review_channels'] == ['text', 'pixel', 'command']
    assert validate_entry(seeded)['data']['review_channels'] == ['text', 'pixel', 'command']
    restored = project_graph(draft, graph.as_dict(), conversation_id='test', revision=1)
    assert restored.node_map()['audit'].review_channels == graph.node_map()['audit'].review_channels
    seeded['data']['review_channels'] = ['ocr']
    with pytest.raises(DraftPatchError, match='review_channels'):
        validate_entry(seeded)


def test_legacy_flow_does_not_gain_empty_channel_metadata():
    graph = spec([node()])
    meta = to_expert_flow(graph).meta
    assert 'node_review_channels' not in meta
    assert 'node_audit_targets' not in meta


def test_completion_cannot_weaken_saved_review_channels():
    graph = spec([node(review_channels=['text', 'pixel'])])
    draft = seed_document(graph.as_dict())
    changed, applied, rejected = apply_completion(draft, {'upserts': [{
        'id': 'audit', 'data': {'review_channels': ['text']}, 'rationale': '不能省略像素要求'}]})
    assert changed == draft and not applied
    assert rejected[0]['code'] == 'locked_content'


@pytest.mark.asyncio
async def test_inspector_pixels_are_recorded_in_one_queue_with_pass_and_channel_ids(tmp_path):
    reviewer, init, store, run_id = setup_queue(tmp_path)
    assert not (await reviewer.execute(init)).is_error
    item = json.loads((await reviewer.execute(ReviewWorkQueueParams(action='next'))).output)['items'][0]
    observer = PixelModel(observation_only=True)
    inspector = InspectVisualEvidence([str(tmp_path)], vision_resolver=lambda: (observer, {}),
        image_observer=reviewer.observe_image, evidence_lookup=reviewer.lookup_evidence)
    result = await inspector.execute(InspectVisualEvidenceParams(image_refs=[{
        'file_path': str(tmp_path / item['image_path']), 'criterion_id': item['item_id']}], question='核对标签'))
    assert not result.is_error and len(observer.calls[0]['images']) == 1
    pixel_ids = json.loads(result.output)['covered_evidence_ids']
    assert all(reference.startswith('pixel:') for reference in pixel_ids)
    text_ids = []
    for name in ('left.md', 'right.md'):
        result = await reviewer.execute(ReviewWorkQueueParams(action='evidence', item_id=item['item_id'], source_path=name))
        text_ids.append(json.loads(result.output)['evidence_id'])
    result = await reviewer.execute(ReviewWorkQueueParams(action='record', item_id=item['item_id'], state='pass',
        summary='审阅者核对了图像和双侧文本', evidence_ids=[*pixel_ids, *text_ids]))
    assert not result.is_error, result.output
    record = store.load(run_id)['review_queues']['check']['records'][item['item_id']]
    assert record['state'] == 'pass' and record['channels'] == ['pixel', 'text']
    assert set(record['evidence_ids']) == set(pixel_ids + text_ids)
    assert reviewer.progress()['pass'] == reviewer.progress()['complete'] == 1
    assert not (await reviewer.execute(ReviewWorkQueueParams(action='read', state_filter='complete'))).is_error


@pytest.mark.asyncio
async def test_joint_or_partial_inspection_cannot_register_queue_pixel_receipts(tmp_path):
    reviewer, init, store, run_id = setup_queue(tmp_path)
    assert not (await reviewer.execute(init)).is_error
    item = json.loads((await reviewer.execute(ReviewWorkQueueParams(action='next'))).output)['items'][0]
    for model in (PixelModel(observation_only=True, joint=True), PixelModel(observation_only=True, missing=True)):
        inspector = InspectVisualEvidence([str(tmp_path)], vision_resolver=lambda: (model, {}), image_observer=reviewer.observe_image)
        result = await inspector.execute(InspectVisualEvidenceParams(image_refs=[{
            'file_path': str(tmp_path / item['image_path']), 'criterion_id': item['item_id']}], question='确认相关图'))
        assert result.is_error
        assert store.load(run_id)['review_queues']['check']['image_receipts'] == {}


@pytest.mark.asyncio
async def test_text_command_queue_requires_actual_host_receipt_and_preserves_legacy_complete(tmp_path):
    reviewer, _, store, run_id = setup_queue(tmp_path)
    manifest = tmp_path / 'text-manifest.json'
    manifest.write_text('[{"id":"one"}]', encoding='utf-8')
    reviewer = ReviewWorkQueue(store, run_id, 'text-audit', tmp_path, 'text-contract', review_channels=['text', 'command'])
    initialized = await reviewer.execute(ReviewWorkQueueParams(action='initialize', manifest_path=manifest.name,
        manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(), expected_count=1, key_fields=['id']))
    assert not initialized.is_error, initialized.output
    item = json.loads((await reviewer.execute(ReviewWorkQueueParams(action='next'))).output)['items'][0]
    text = await reviewer.execute(ReviewWorkQueueParams(action='evidence', item_id=item['item_id'], source_path='left.md'))
    reference = json.loads(text.output)['evidence_id']
    arguments = {'command': 'git status'}
    assert reviewer.observe_command('unverified', arguments, json.dumps({'command': 'git status', 'status': 'PASS'})) is None
    attempted = ReviewWorkQueueParams(action='record', item_id=item['item_id'], state='pass', summary='检查已完成', evidence_ids=[reference])
    assert (await reviewer.execute(attempted)).is_error
    command = reviewer.observe_command('host-result', arguments, json.dumps({'command': 'git status', 'exit_code': 0, 'status': 'PASS'}))
    assert command['evidence_id'].startswith('command:')
    assert not (await reviewer.execute(attempted.model_copy(update={'evidence_ids': [reference, command['evidence_id']]}))).is_error
    assert not reviewer.exit_blocker()
    before = copy.deepcopy(store.load(run_id))
    def legacy(snapshot):
        queue = snapshot['review_queues']['text-audit']
        queue['records'][item['item_id']]['state'] = 'complete'
        queue['visual_reviewed_count'] = 1
    store.mutate(run_id, legacy, durable=True)
    assert reviewer.progress()['pass'] == reviewer.progress()['complete'] == 1
    assert not reviewer.exit_blocker()
    assert store.load(run_id)['review_queues']['text-audit']['visual_reviewed_count'] == 1
    assert before['review_queues']['text-audit']['records'][item['item_id']]['state'] == 'pass'


def test_role_inspector_is_read_only_and_respects_explicit_tools_and_denies(tmp_path):
    from mewcode.expert_group import ExpertExecutionPolicy
    from mewcode.expert_web import _role_registry
    from mewcode.permissions import PermissionMode
    from mewcode.permissions.policy import build_permission_checker
    from mewcode.permissions.rules import Rule
    from mewcode.tools import create_default_registry
    session = SimpleNamespace(agent=SimpleNamespace(registry=create_default_registry()), work_dir=str(tmp_path), context=None)
    registry = _role_registry(session, 'auditor', ExpertExecutionPolicy('stable', False, 0), vision_ok=False)
    inspector = registry.get('InspectVisualEvidence')
    assert inspector.category == 'read' and not inspector.is_system_tool
    assert registry.is_discovered('InspectVisualEvidence')
    checker = build_permission_checker(tmp_path, mode=PermissionMode.AUTO)
    checker.rule_engine.append_session_rule(Rule('InspectVisualEvidence', '*', 'deny'))
    assert checker.check(inspector, {'image_refs': ['page.png'], 'question': '核对'}).effect == 'deny'
    for policy in (ExpertExecutionPolicy('stable', False, 0, node_tools=()),
                   ExpertExecutionPolicy('stable', False, 0, deny_tools=('InspectVisualEvidence',))):
        assert _role_registry(session, 'auditor', policy, vision_ok=False).get('InspectVisualEvidence') is None
    assert _role_registry(session, 'auditor', ExpertExecutionPolicy('stable', False, 0), vision_ok=True).get('InspectVisualEvidence') is not None


@pytest.mark.asyncio
async def test_audit_pass_is_blocked_until_unified_queue_is_complete(tmp_path):
    from mewcode.tools.submit_verdict import SubmitVerdictParams, SubmitVerdictTool, VerdictHolder
    reviewer, init, store, run_id = setup_queue(tmp_path)
    assert not (await reviewer.execute(init)).is_error
    holder = VerdictHolder(value={'verdict': 'PASS'})
    verdict = SubmitVerdictTool(holder)
    verdict.completion_guard = reviewer.exit_blocker
    result = await verdict.execute(SubmitVerdictParams(verdict='PASS'))
    assert result.is_error and holder.value is None
