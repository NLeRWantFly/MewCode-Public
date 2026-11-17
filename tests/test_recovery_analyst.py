from mewcode.experts.root_cause import validate_plan
from mewcode.experts.recovery_case import open_case
from mewcode.experts.problem_dossier import build_dossier
from mewcode.tools.submit_recovery_plan import SubmitRecoveryPlanParams
from tests.test_recovery_case_e2e import option, plan
from tests.test_recovery_case import record
from tests.test_graph_lint import _node, _spec, _codes
import pytest


def test_model_cannot_claim_fundamental_defect_without_host_evidence():
    r = record()
    case = open_case(r, 'work')
    dossier = build_dossier(r, case, {'models': []})
    p = plan({'case_id': case['case_id'], 'round': 1}, [option()], 'capability_absent')
    with pytest.raises(ValueError, match='不完整'):
        validate_plan(p, case, dossier)
    p['root_cause']['class'] = 'contract_defect'
    with pytest.raises(ValueError, match='宿主'):
        validate_plan(p, case, dossier)


def test_schema_rejects_free_text_authority_duplicate_options_and_missing_probe():
    p = plan({'case_id': 'case', 'round': 1}, [option()])
    p['options'][0]['needs']['authority'] = ['full permission']
    with pytest.raises(ValueError):
        SubmitRecoveryPlanParams(**p)
    p = plan({'case_id': 'case', 'round': 1}, [option(), option()])
    with pytest.raises(ValueError):
        SubmitRecoveryPlanParams(**p)


def test_authority_schema_exposes_supported_fields_and_preserves_scope_validation():
    from mewcode.tools.submit_recovery_plan import RecoveryAuthority
    schema = SubmitRecoveryPlanParams.model_json_schema()['$defs']['RecoveryAuthority']
    assert 'external_send' in schema['properties']['operation_class']['enum']
    assert schema['properties']['scope']['type'] == 'object'
    assert schema['additionalProperties'] is False
    root = SubmitRecoveryPlanParams.model_json_schema()['$defs']['RootCause']['properties']
    assert 'active host blocker' in root['class']['description']
    assert 'root_evidence_refs' in root['evidence_refs']['description']
    valid = {'operation_class': 'external_send', 'scope': {
        'model_routes': ['test/vision'], 'content_classes': ['page_image']}, 'covered_by': 'AUTH-01'}
    assert RecoveryAuthority(**valid).model_dump(exclude_none=True) == valid
    for invalid in [
        {'type': 'other', 'scope': 'Only approved retry rounds'},
        {'operation_class': 'other', 'scope': 'Only approved retry rounds'},
        {'operation_class': 'recovery_round_budget', 'scope': {'case_id': 'case'}},
        {'operation_class': 'external_send', 'scope': {'tools': ['ReadFile']}},
        {'operation_class': 'other', 'scope': {'tools': ['*']}},
    ]:
        with pytest.raises(ValueError):
            RecoveryAuthority(**invalid)


@pytest.mark.asyncio
@pytest.mark.parametrize('deadline', [None, 0.3, 360])
async def test_analyst_inherits_only_explicit_deadlines(tmp_path, deadline):
    from dataclasses import replace
    from mewcode.expert_group import ExpertRoleResult
    from mewcode.experts.recovery_analyst import RecoveryAnalyst
    from tests.test_recovery_case_e2e import harness, RUN
    h = harness(tmp_path)
    h.store.mutate(RUN, lambda r: open_case(r, 'work'))
    case = h.store.load(RUN)['node_states']['work']['recovery_case']
    async def dispatch(helper, prompt, policy):
        assert policy.timeout_seconds == deadline
        assert 'root_evidence_refs' in prompt and 'contract_defect 或 graph_invalid' in prompt
        policy.recovery_analysis['holder'].update(plan(policy.recovery_analysis, [option()]))
        return ExpertRoleResult('valid plan')
    analyst = RecoveryAnalyst(dispatch=dispatch, store=h.store, run_id=RUN)
    await analyst.analyze(case, build_dossier(h.store.load(RUN), case), node=h.node,
        policy=replace(h.policy, timeout_seconds=deadline))


@pytest.mark.asyncio
async def test_failed_submission_reports_current_schema_error_without_old_failure(tmp_path):
    from mewcode.expert_group import ExpertRoleResult
    from mewcode.experts.recovery_analyst import RecoveryAnalyst
    from tests.test_recovery_case_e2e import harness, RUN
    h = harness(tmp_path)
    h.store.mutate(RUN, lambda r: open_case(r, 'work'))
    case = h.store.load(RUN)['node_states']['work']['recovery_case']
    async def dispatch(helper, prompt, policy):
        h.store.mutate(RUN, lambda r: r['node_states'].setdefault(helper.node_id, {}).update(
            started_at=20, tools={
                'old': {'name': 'SubmitRecoveryPlan', 'started_at': 10, 'status': 'failed', 'output': 'obsolete error'},
                'new': {'name': 'SubmitRecoveryPlan', 'started_at': 21, 'status': 'failed', 'output': 'scope must be an object'},
            }))
        return ExpertRoleResult('no plan')
    analyst = RecoveryAnalyst(dispatch=dispatch, store=h.store, run_id=RUN)
    with pytest.raises(ValueError, match='scope must be an object') as error:
        await analyst.analyze(case, build_dossier(h.store.load(RUN), case), node=h.node, policy=h.policy)
    assert 'obsolete error' not in str(error.value)


@pytest.mark.asyncio
async def test_dossier_sections_are_discoverable_and_summary_keeps_recent_delivery_failures():
    import json
    from mewcode.experts.problem_dossier import dossier_summary
    from mewcode.tools.read_case_dossier import ReadCaseDossier, ReadCaseDossierParams
    r = record()
    case = open_case(r, 'work')
    case['rounds'].append({'n': 1, 'option_id': 'analyst_failed', 'executor_receipt_ref': None,
        'verification': {'result': 'unchanged', 'reason': 'submit schema rejected'}})
    dossier = build_dossier(r, case)
    summary = json.loads(dossier_summary(dossier))
    assert summary['recent_rounds'][0]['reason'] == 'submit schema rejected'
    assert summary['recent_rounds'][0]['executor_receipt_ref'] is None
    tool = ReadCaseDossier(dossier)
    assert 'command_receipts' in tool.description
    result = await tool.execute(ReadCaseDossierParams(section='current_stop'))
    assert result.is_error and 'rounds' in result.output


def test_authority_before_use_blocks_late_gate_and_bypass_path():
    work = _node('work', steps=['交给外部模型识别页面'])
    gate = _node('gate', kind='gate', goal='确认内容外发授权')
    assert 'authority_before_use' in _codes(_spec([work, gate], [{'from_node': 'work', 'to_node': 'gate'}]))
    assert 'authority_before_use' not in _codes(_spec([gate, work], [{'from_node': 'gate', 'to_node': 'work'}]))
    root = _node('root')
    bypass = _spec([root, gate, work], [{'from_node': 'root', 'to_node': 'gate'}, {'from_node': 'gate', 'to_node': 'work'}, {'from_node': 'root', 'to_node': 'work'}])
    assert 'authority_before_use' in _codes(bypass)
    early = _spec([work], run_inputs=[{'name': 'external_permission', 'label': '内容外发授权', 'collect_at': 'start'}])
    assert 'authority_before_use' not in _codes(early)


def test_planning_pixel_requirement_cannot_escape_explicit_text_channel():
    from mewcode.orchestration.graph_lint import lint_graph
    findings = lint_graph(_spec([_node(kind='plan', steps=['观察冻结像素'], review_channels=['text'])]))
    assert 'review_channel_capability' in {f.code for f in findings}


@pytest.mark.asyncio
async def test_analyst_timeout_charges_current_checkpoint_without_recharging_old_usage(tmp_path):
    from mewcode.experts.recovery_analyst import RecoveryAnalyst
    from tests.test_recovery_case_e2e import harness, RUN
    h = harness(tmp_path)
    h.store.mutate(RUN, lambda r: open_case(r, 'work'))
    case = h.store.load(RUN)['node_states']['work']['recovery_case']
    dossier = build_dossier(h.store.load(RUN), case, {})
    async def interrupted(helper, prompt, policy):
        h.store.mutate(RUN, lambda r: r['node_states'].setdefault(helper.node_id, {}).update(
            started_at=123, billable_tokens=37, wait_seconds=2.))
        raise TimeoutError('paid dispatch interrupted')
    analyst = RecoveryAnalyst(dispatch=interrupted, store=h.store, run_id=RUN)
    with pytest.raises(TimeoutError):
        await analyst.analyze(case, dossier, node=h.node, policy=h.policy)
    assert analyst.last_tokens == 37 and analyst.last_wait_seconds == 2.
    # This failure occurs before a new role starts: old paid tokens are not new usage.
    with pytest.raises(TimeoutError):
        await analyst.analyze(case, dossier, node=h.node, policy=h.policy)
    assert analyst.last_tokens == 0 and analyst.last_wait_seconds == 0.


@pytest.mark.asyncio
@pytest.mark.parametrize('budget', [None, 1500, 90000])
async def test_analyst_inherits_explicit_model_budget_without_a_default_hard_cap(tmp_path, budget):
    from dataclasses import replace
    from mewcode.expert_group import ExpertRoleResult
    from mewcode.experts.recovery_analyst import RecoveryAnalyst
    from tests.test_recovery_case_e2e import harness, RUN
    h = harness(tmp_path)
    h.store.mutate(RUN, lambda r: open_case(r, 'work'))
    case = h.store.load(RUN)['node_states']['work']['recovery_case']
    seen = []
    spent = 60000 if budget is None else min(60000, budget)
    async def dispatch(helper, prompt, policy):
        seen.append(policy)
        context = policy.recovery_analysis
        context['holder'].update(plan(context, [option()]))
        return ExpertRoleResult('valid plan', billable_tokens=spent)
    analyst = RecoveryAnalyst(dispatch=dispatch, store=h.store, run_id=RUN)
    await analyst.analyze(case, build_dossier(h.store.load(RUN), case), node=h.node,
        policy=replace(h.policy, token_budget=budget))
    assert seen[0].token_budget == budget
    assert seen[0].max_iterations == h.policy.max_iterations and seen[0].timeout_seconds == h.policy.timeout_seconds
    assert analyst.last_tokens == spent  # Unlimited usage is still recorded.
