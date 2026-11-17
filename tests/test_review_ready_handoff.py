"""A reviewable delivery is not a quality pass or a new execution permission."""
import copy
from dataclasses import replace

import pytest

from mewcode.expert_group import ExpertExecutionPolicy, ExpertRoleResult
from mewcode.expert_web import _role_registry
from mewcode.kernel.acceptance import Acceptance
from mewcode.kernel.contracts import handoff_check_policy
from mewcode.tools.submit_handoff import HandoffHolder, SubmitHandoff, SubmitHandoffParams
from tests.test_expert_children import FakeSession, make_registry


CHECK = {'id': 'identity', 'version': 2, 'kind': 'review',
         'criteria': ['核对请求身份与内容；服务未提供的字段如实保留未知']}
FINDING = {'check_id': 'identity', 'version': 2, 'reason': '已有三页草稿；服务端档位证明尚未取得'}


def draft(**kwargs):
    return SubmitHandoffParams(**({'summary': '草稿已保存，未判通过', 'status': 'ready_for_review',
        'unmet_checks': [FINDING], 'inline_outputs': [{'name': '草稿', 'content': '可供独立审阅的既有内容。'}],
        'open_items': ['原质量阈值与全量放行条件尚待核验。']} | kwargs))


@pytest.mark.asyncio
async def test_review_ready_keeps_unknown_evidence_without_claiming_a_pass(tmp_path):
    (tmp_path / 'draft.json').write_text('{"pilot_pass": false}', encoding='utf-8')
    holder = HandoffHolder()
    tool = SubmitHandoff(holder, work_dir=tmp_path, handoff_checks=[CHECK])
    result = await tool.execute(draft(artifacts=[{'path': 'draft.json'}]))
    assert not result.is_error
    handoff = holder.read()
    assert handoff['status'] == 'completed' and handoff['readiness'] == 'ready_for_review'
    assert handoff['acceptance_status'] == 'pending' and handoff['unmet_checks'] == [FINDING]
    assert not handoff['blockers'] and not handoff['blocker_kind']
    assert (tmp_path / 'draft.json').read_text() == '{"pilot_pass": false}'

    reviewed = []
    async def review(payload, context):
        reviewed.append(payload['handoff']['unmet_checks'])
        return {'passed': False, 'evidence': ['仍未核验该项']}
    acceptance = Acceptance(tmp_path, review=review)
    delivery = ExpertRoleResult('草稿未通过', handoff=handoff)
    report = await acceptance.check({'checks': [CHECK]}, delivery, context={'phase': 'handoff'})
    assert report['ready'] and not report['passed'] and not reviewed
    final = await acceptance.check({'checks': [CHECK]}, delivery, context={'phase': 'final'})
    assert final['passed'] is False and reviewed == [[FINDING]]


@pytest.mark.asyncio
@pytest.mark.parametrize('findings', [[], [FINDING]])
async def test_advisory_evidence_cannot_be_promoted_to_execution_blocker(findings):
    holder = HandoffHolder()
    tool = SubmitHandoff(holder, handoff_checks=[CHECK])
    params = draft(status='needs_attention', blocker_kind='insufficient_evidence',
                   blockers=['没有服务端证明，无法交给审阅'], unmet_checks=findings)
    result = await tool.execute(params)
    assert result.is_error and holder.read() is None
    assert ('unmet_checks' if not findings else 'ready_for_review') in result.output


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['unknown', 'stale', 'duplicate', 'required', 'permission', 'empty', 'missing_file'])
async def test_review_ready_rejects_missing_inputs_and_invalid_boundaries(tmp_path, change):
    checks, params = [copy.deepcopy(CHECK)], draft().model_dump()
    if change == 'unknown': params['unmet_checks'][0]['check_id'] = 'invented'
    elif change == 'stale': params['unmet_checks'][0]['version'] = 1
    elif change == 'duplicate': params['unmet_checks'].append(copy.deepcopy(params['unmet_checks'][0]))
    elif change == 'required': checks[0]['blocking'] = True
    elif change == 'permission': params.update(blocker_kind='user_input', question='允许外发更多页面？')
    elif change == 'empty': params['inline_outputs'] = []
    else: params['artifacts'] = [{'path': 'missing.json'}]
    holder = HandoffHolder()
    result = await SubmitHandoff(holder, work_dir=tmp_path, handoff_checks=checks).execute(SubmitHandoffParams(**params))
    assert result.is_error and holder.read() is None


@pytest.mark.asyncio
async def test_real_required_evidence_remains_blocked():
    holder = HandoffHolder()
    result = await SubmitHandoff(holder, handoff_checks=[{**CHECK, 'blocking': True}]).execute(
        draft(status='needs_attention', blocker_kind='insufficient_evidence', blockers=['缺少下游运行必需输入']))
    assert not result.is_error and holder.read()['status'] == 'needs_attention'


@pytest.mark.asyncio
async def test_host_acceptance_revalidates_current_check_version(tmp_path):
    holder = HandoffHolder()
    assert not (await SubmitHandoff(holder, handoff_checks=[CHECK]).execute(draft())).is_error
    for check in ({**CHECK, 'version': 3}, {**CHECK, 'blocking': True}):
        result = await Acceptance(tmp_path).check({'checks': [check]},
            ExpertRoleResult('draft', handoff=holder.read()), context={'phase': 'handoff'})
        assert not result['passed'] and not result.get('ready') and not result['artifacts']
    forged = {**holder.read(), 'acceptance_status': 'passed'}
    result = await Acceptance(tmp_path).check({'checks': [CHECK]},
        ExpertRoleResult('claimed success', handoff=forged), context={'phase': 'handoff'})
    assert not result['passed'] and not result.get('ready')


@pytest.mark.asyncio
async def test_real_registry_passes_checks_only_to_business_node():
    policy = ExpertExecutionPolicy('stable', False, 0, handoff_checks=(CHECK,), handoff_holder=HandoffHolder())
    session = FakeSession(make_registry('ReadFile'))
    registry = _role_registry(session, 'executor', policy)
    assert not (await registry.get('SubmitHandoff').execute(draft())).is_error
    for changes in ({'assistance_session': True}, {'recovery_analysis': {'enabled': True}}, {'handoff_checks': None}):
        holder = HandoffHolder()
        registry = _role_registry(session, 'executor', replace(policy, handoff_holder=holder, **changes))
        assert (await registry.get('SubmitHandoff').execute(draft())).is_error
        assert holder.read() is None


@pytest.mark.asyncio
async def test_real_scheduler_hands_draft_to_consumer_and_keeps_failed_quality(tmp_path):
    from mewcode.kernel import collaboration as ledger
    from mewcode.orchestration.task_context import build_task_context
    from tests.test_iterative_collaboration import checked_flow, retry_store
    from tests.test_collaboration_integration import orchestrator, run
    from tests.test_collaboration_mode import RUN_ID, delivery

    spec = checked_flow(CHECK)
    store = retry_store(tmp_path, spec)
    invoked = []
    async def execute(node, model, prompt, policy, member=None):
        invoked.append(node.node_id)
        if node.node_id == 'work':
            assert policy.handoff_checks == (CHECK,)
            context = build_task_context(store.read_collaboration(RUN_ID), 'work')
            assert context['objective']['handoff_policy'] == handoff_check_policy([CHECK])
            assert 'ready_for_review' in prompt
            holder = HandoffHolder()
            registry = _role_registry(FakeSession(make_registry('ReadFile')), 'executor', replace(policy, handoff_holder=holder))
            first = draft(status='needs_attention', blocker_kind='insufficient_evidence', blockers=['证明未取得'])
            assert (await registry.get('SubmitHandoff').execute(first)).is_error
            assert not (await registry.get('SubmitHandoff').execute(draft())).is_error
            return ExpertRoleResult('仅交接草稿，未通过质量验收', handoff=holder.read())
        source = policy.stage_inputs['work']
        assert source['handoff']['unmet_checks'] == [FINDING]
        assert source['acceptance_certificate']['quality_passed'] is False
        return delivery('已审阅草稿，保留缺口')
    async def review(*_):
        return {'passed': False, 'evidence': ['仍有质量待复核项'], 'reason': '不能宣称达标'}
    result = await run(orchestrator(store, tmp_path, execute, review), store, spec)
    assert invoked == ['work', 'deliver'] and result.status == 'completed'
    value = store.load(RUN_ID)['collaboration']
    assert ledger.valid_certificate(value, 'work')
    assert value['certificates']['work']['quality_passed'] is False
    assert value['integrations']['final']['report']['passed'] is False
    assert not value.get('recovery_gates')


def test_empty_visual_route_list_is_explicitly_scoped_not_a_model_failure(tmp_path):
    from mewcode.experts.recovery_context import context_sections
    from tests.test_recovery_context_tools import harness, seed, RUN
    h = harness(tmp_path)
    seed(h)
    record = h.store.load(RUN)
    before = copy.deepcopy(record)
    model = context_sections(record, 'work', section='model')['model']
    assert model['observed_routes'] == [] and model['declared']['model_id'] == 'vision'
    assert 'InspectVisualEvidence' in model['observed_routes_scope']
    assert set(model['transport_evidence'].values()) == {'not_exposed_by_this_interface'}
    assert record == before and not h.calls
