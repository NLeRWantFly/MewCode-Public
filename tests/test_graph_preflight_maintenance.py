"""Applied model edits stay saved while unrelated dispatch blockers remain visible."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from playwright.async_api import async_playwright, expect

from frontend import orch_maintenance, server
from mewcode import expert_web
from mewcode.conversation import ConversationManager
from mewcode.expert_runs import ExpertRunStore
from mewcode.orchestration.consistent_save import save_consistently
from mewcode.orchestration.convert import to_expert_flow
from mewcode.orchestration.graph_preflight import graph_preflight_findings, graph_preflight_error_text
from mewcode.orchestration.maintenance import SCHEMA
from mewcode.orchestration.spec import OrchestrationSpec
from mewcode.pipeline.store import PipelineStore
from tests.test_expert_flow import _profile
from tests.test_maintenance_model_edits import BINDING, ROUTES
from tests.test_orchestration_maintenance import maintenance_env as maintenance_env, poll, spec_for, update_proposal
from tests.test_orchestration_maintenance_safety import paused_run
from tests.test_orchestration_maintenance_ui import install_chat_shell, start_host


@pytest.fixture(autouse=True)
def no_external_review_config(monkeypatch):
    monkeypatch.setattr('mewcode.permissions.review_client.load_review_model_settings', lambda: {})


def blocked_spec(base=None, *, input_type='text'):
    value = (base or spec_for()).as_dict()
    value['goal_contract'] = {'objective': value['goal'], 'run_inputs': [
        {'name': 'pdf_paths', 'type': input_type, 'example': 'one.pdf\ntwo.pdf\nthree.pdf\nfour.pdf'}]}
    return OrchestrationSpec.from_value(value)


def save_blocked_fixture(owner, base):
    pipeline = PipelineStore(owner['project'])
    _, saved, _, _ = save_consistently(owner['project'], spec=blocked_spec(base),
        graph=pipeline.get_pipeline('maintenance-test'), pipeline_store=pipeline,
        plan_store=server._conversation_plan_store(), flow_store=server._conversation_flow_store(),
        previous=server._conversation_plan_store().get(owner['project']))
    return saved


def test_shared_preflight_reports_concrete_unrelated_blocker_without_losing_binding():
    before = blocked_spec()
    original = copy.deepcopy(before.as_dict())
    calls = []
    registry = SimpleNamespace(resolve=lambda ref: calls.append(ref) or SimpleNamespace(capabilities=['vision']))
    findings = graph_preflight_findings(to_expert_flow(before), registry)
    errors = [finding for finding in findings if finding.severity == 'error']
    assert [finding.code for finding in errors] == ['multiline_text_input']
    assert len(calls) == 1  # Equal node routes do not construct repeated clients.
    text = graph_preflight_error_text(findings)
    assert 'run_inputs.pdf_paths.example' in text and 'type=list' in text
    assert before.as_dict() == original
    assert not graph_preflight_error_text(graph_preflight_findings(
        to_expert_flow(blocked_spec(input_type='list')), registry))


def test_legacy_four_pdf_defaults_pass_the_same_checks_as_runtime_without_data_changes():
    value = spec_for().as_dict()
    value['goal_contract'] = {'objective': value['goal'], 'run_inputs': [
        {'name': 'pdf_paths', 'required': True, 'default': 'one.pdf\ntwo.pdf\nthree.pdf\nfour.pdf'}]}
    before = copy.deepcopy(value)
    registry = SimpleNamespace(resolve=lambda _: SimpleNamespace(capabilities=['vision']))
    graph = OrchestrationSpec.from_value(value)
    findings = graph_preflight_findings(to_expert_flow(graph), registry)
    assert not [finding for finding in findings if finding.severity == 'error']
    assert graph.goal_contract.run_inputs == tuple(before['goal_contract']['run_inputs'])


@pytest.mark.parametrize('vision,observer,error', [(True, False, False), (False, False, True), (False, True, False)])
def test_shared_preflight_keeps_actual_vision_gate(monkeypatch, vision, observer, error):
    value = spec_for().as_dict()
    value['nodes'][1]['review_channels'] = ['text', 'pixel']
    registry = SimpleNamespace(resolve=lambda _: SimpleNamespace(capabilities=['vision'] if vision else []))
    if observer:
        monkeypatch.setattr('mewcode.permissions.review_client.load_review_model_settings',
            lambda: {'vision_model_id': 'observer', 'vision_provider_key': 'selected'})
        monkeypatch.setattr('mewcode.permissions.review_client.resolve_visual_evidence_client', lambda *_: (None, {}))
    findings = graph_preflight_findings(to_expert_flow(OrchestrationSpec.from_value(value)), registry)
    assert bool([f for f in findings if f.code == 'review_channel_capability']) is error


@pytest.mark.asyncio
async def test_dispatch_rejection_includes_details_and_never_creates_or_runs_task(tmp_path):
    frames = []
    runs = ExpertRunStore(tmp_path / 'runs')
    session = SimpleNamespace(work_dir=str(tmp_path), conversation_id='fixture', multi_agent=False,
        session=SimpleNamespace(), conversation=ConversationManager(), status_payload=lambda: {}, tasks_payload=lambda: [])
    registry = SimpleNamespace(resolve=lambda _: SimpleNamespace(capabilities=['vision']))
    await expert_web.run_expert_chat(SimpleNamespace(send_json=AsyncMock(side_effect=frames.append)),
        '保留已经应用的模型', session=session, registry_service=registry,
        profile_store=SimpleNamespace(get=lambda _: _profile()), run_store=runs,
        confirmed_new_flow=to_expert_flow(blocked_spec()))
    error, done = frames
    assert error['error_code'] == 'graph_lint_failed'
    assert 'pdf_paths' in error['text'] and 'type=list' in error['text']
    assert error['findings'][0]['code'] == 'multiline_text_input'
    assert done['rejected'] and done['error'] == error['text']
    assert not list(runs.root.glob('*.json'))


@pytest.mark.asyncio
async def test_fix_sees_startup_findings_and_saved_model_does_not_replace_unassociated_run(maintenance_env, monkeypatch):
    app, owner, _, _, base = maintenance_env
    original = save_blocked_fixture(owner, base)
    monkeypatch.setattr('mewcode.orchestration.chat_routing.dialogue_focus', lambda _: {'kind': 'dialogue'})
    monkeypatch.setattr('mewcode.orchestration.graph_models.available_model_routes', lambda _: ROUTES)
    old_id = paused_run(owner, base)
    old_bytes = (server.EXPERT_RUNS.root / (old_id + '.json')).read_bytes()

    async def model(_client, **kwargs):
        facts = json.loads(kwargs['user_text'])['runtime']['preflight_findings']
        assert [f['code'] for f in facts if f['severity'] == 'error'] == ['multiline_text_input']
        return json.dumps({'schema': SCHEMA, 'reply': '补绑模型，仍需修正 pdf_paths 的参数类型。',
                           'proposal': update_proposal(model=BINDING)})

    monkeypatch.setattr(orch_maintenance, 'collect_model_text', model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        state = (await client.post('/api/orchestration/maintenance/open', json={**owner, 'mode': 'fix'})).json()
        assert not state['run_id']
        await client.post('/api/orchestration/maintenance/send', json={**owner, 'id': state['id'],
            'revision': state['revision'], 'message': '未绑定节点补绑 deepseek-flash max'})
        ready = await poll(client, owner, state)
        response = await client.post('/api/orchestration/maintenance/apply', json={**owner, 'id': ready['id'],
            'revision': ready['revision'], 'proposal_id': ready['proposal']['id']})
        assert response.status_code == 200, response.text
        applied = response.json()
        assert applied['status'] == 'applied' and '本次未关联旧运行' in applied['run_notice']
        assert any(f['code'] == 'multiline_text_input' for f in applied['diagnostics']['preflight_findings'])
    saved = server._conversation_plan_store().get(owner['project']).spec
    assert saved.node_map()['work'].model.as_dict() == BINDING
    assert saved.goal_contract == original.goal_contract
    assert (server.EXPERT_RUNS.root / (old_id + '.json')).read_bytes() == old_bytes


@pytest.mark.asyncio
async def test_browser_shows_applied_model_and_remaining_startup_blocker(maintenance_env, monkeypatch, tmp_path):
    app, owner, _, _, base = maintenance_env
    save_blocked_fixture(owner, base)
    monkeypatch.setattr('mewcode.orchestration.graph_models.available_model_routes', lambda _: ROUTES)

    async def model(*_args, **_kwargs):
        return json.dumps({'schema': SCHEMA, 'reply': '模型差异已准备，pdf_paths 尚有类型问题。',
                           'proposal': update_proposal(model=BINDING)})

    monkeypatch.setattr(orch_maintenance, 'collect_model_text', model)
    install_chat_shell(app, owner)
    host, task, origin = await start_host(app)
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page(viewport={'width': 1400, 'height': 1000})
            await page.goto(origin)
            await page.get_by_text('原业务回复，等待核验。', exact=True).wait_for()
            await page.locator('#chat-input').fill('/fix 补绑 deepseek-flash max')
            await page.locator('#chat-input').press('Enter')
            pane = page.locator('.maintenance-thread')
            await pane.get_by_role('button', name='应用修订', exact=True).click()
            await expect(pane.get_by_text('已应用修复', exact=True)).to_be_visible()
            await expect(pane.get_by_text('待应用修复', exact=True)).to_have_count(0)
            await expect(pane.locator('.maintenance-status')).to_contain_text('仍有开跑检查问题')
            await expect(pane.locator('.maintenance-status')).to_contain_text('pdf_paths')
            await pane.get_by_role('button', name='查看修改', exact=True).click()
            await expect(page.locator('.maintenance-compare')).to_contain_text('deepseek-flash')
            await page.locator('#settings-dialog-close').click()
            await page.screenshot(path=str(tmp_path / 'applied-model-with-startup-blocker.png'))
            await browser.close()
    finally:
        host.should_exit = True
        await asyncio.wait_for(task, 10)
