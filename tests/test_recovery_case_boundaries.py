"""Host budget, authorization-version and checkpoint boundaries."""
import copy
import json
from dataclasses import replace, asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mewcode.expert_group import ExpertRoleResult
from mewcode.experts.authorization_ledger import covers, record_authorization
from mewcode.experts.capability_broker import BindingResult
from mewcode.experts.gate_dialogue import confirmation_valid
from mewcode.experts.human_gate import await_gate_decision
from mewcode.experts.problem_dossier import build_dossier, dossier_summary
from mewcode.experts.recovery_case import open_case, apply_event, CaseRound, cases
from mewcode.experts.recovery_case import digest
from mewcode.experts.waits import BranchWaitPaused
from mewcode.tools.read_case_dossier import ReadCaseDossier, ReadCaseDossierParams
from mewcode.tools.submit_recovery_plan import SubmitRecoveryPlanParams
from tests.test_acceptance_completion import old_false_completion, branch_flow
from tests.test_recovery_case import record
from tests.test_recovery_case_e2e import RUN, AUTH, harness, option, plan, run


def test_sixth_attempt_is_not_rejected_by_legacy_default_counter():
    r = record()
    c = open_case(r, 'work')
    for n in range(1, 6):
        c = apply_event(r, c['case_id'], {'kind': 'finish', 'identity': f'finish:{n}',
            'round': asdict(CaseRound(n, str(n), f'method:{n}', 'transient', verification={'result': 'changed'}))})
    assert c['status'] == 'open' and len(c['rounds']) == 5
    current = apply_event(r, c['case_id'], {'kind': 'start', 'identity': 'six', 'plan': {}})
    assert current['status'] == 'executing' and len(current['rounds']) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize('changing', [False, True])
async def test_gate_grants_are_explicit_and_bound_to_displayed_version(tmp_path, changing):
    store, run_id = old_false_completion(tmp_path)
    flow, session, visible = branch_flow(), SimpleNamespace(), []
    flow.meta['authorization_grants'] = {'handoff': [copy.deepcopy(AUTH)]}
    async def emit(event):
        if event['type'] == 'ask_user':
            session._ask_user_request.future.set_result({'requirements': '保留原始成果'})
        elif event['type'] == 'human_oncall':
            visible.append(event)
            if changing:
                flow.meta['authorization_grants']['handoff'][0]['scope']['model_routes'] = ['other/vision']
            session._human_oncall_request.future.set_result({'choice': 'continue'})
    approved = await await_gate_decision(node=flow.node_map['handoff'], flow=flow, session=session,
        store=store, run_id=run_id, emit=emit, proposal_builder=AsyncMock(return_value='仅批准所列范围'))
    assert approved is not changing
    snapshot = store.load(run_id)
    assert covers(snapshot, AUTH).status == ('uncovered' if changing else 'covered')
    assert confirmation_valid(flow.node_map['handoff'], flow, snapshot) is not changing
    assert 'test/vision' in json.dumps(visible, ensure_ascii=False)
    if not changing:
        flow.meta['authorization_grants']['handoff'][0]['scope']['cost_cap_tokens'] += 1
        assert not confirmation_valid(flow.node_map['handoff'], flow, snapshot)


def test_covered_ledger_never_overrides_hard_permission_rule(tmp_path):
    from mewcode.permissions import PermissionChecker, PermissionMode, DangerousCommandDetector, PathSandbox, RuleEngine
    from mewcode.tools.bash import Bash
    rule = tmp_path / 'rules.yaml'
    rule.write_text('- rule: "Bash(pytest *)"\n  effect: deny\n', encoding='utf-8')
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(tmp_path)),
        RuleEngine(project_rules_path=rule), PermissionMode.AUTO)
    request = {'operation_class': 'run_command_prefix', 'scope': {'command_prefix': ['pytest']}}
    r = {}
    record_authorization(r, kind='elevation_answer', scope={**request['scope'], 'operation_classes': ['run_command_prefix']},
        quote='批准具体命令', source_ref='card:1', host_verified=True)
    assert covers(r, request).status == 'covered'
    assert checker.check(Bash(), {'command': 'pytest -q'}).effect == 'deny'


@pytest.mark.asyncio
async def test_dossier_determinism_and_lossless_pagination():
    r = record()
    c = open_case(r, 'work')
    for n in range(75):
        record_authorization(r, kind='user_statement', scope={}, quote='表态', source_ref=f'input:{n}')
    first = build_dossier(r, c)
    assert first == build_dossier(r, c) and len(dossier_summary(first)) <= 3000
    reader, offset, chunks = ReadCaseDossier(first), 0, []
    while True:
        output = (await reader.execute(ReadCaseDossierParams(offset=offset, limit=90))).output
        headers, chunk = output.split('\n', 2)[:2], output.split('\n', 2)[2]
        total, offset = [int(h.split(': ')[1]) for h in headers]
        chunks.append(chunk)
        if offset >= total:
            break
    assert json.loads(''.join(chunks)) == json.loads(json.dumps(first))


@pytest.mark.asyncio
async def test_legacy_total_round_limit_does_not_stop_goal_progress(tmp_path):
    h = harness(tmp_path)
    h.store.mutate(RUN, lambda r: r.update(recovery_round_limit=0))
    await run(h)
    assert h.calls and cases(h.store.load(RUN))[0]['status'] == 'resolved'


def test_parallel_cases_are_not_limited_by_legacy_shared_counter(tmp_path):
    h = harness(tmp_path)
    ids = []
    def opening(r):
        r['recovery_round_limit'] = 1
        ids.extend([open_case(r, 'work')['case_id'], open_case(r, 'other')['case_id']])
    h.store.mutate(RUN, opening)
    h.coordinator.update(ids[0], {'kind': 'start', 'identity': 'parallel:first', 'plan': {}})
    h.coordinator.update(ids[1], {'kind': 'start', 'identity': 'parallel:second', 'plan': {}})
    assert [c['status'] for c in cases(h.store.load(RUN))] == ['executing', 'executing']


@pytest.mark.asyncio
async def test_unknown_interrupted_execution_is_not_replayed(tmp_path):
    h = harness(tmp_path)
    def interrupted(r):
        c = open_case(r, 'work')
        apply_event(r, c['case_id'], {'kind': 'start', 'identity': 'crash:1', 'plan': {}})
    h.store.mutate(RUN, interrupted)
    with pytest.raises(BranchWaitPaused):
        await run(h)
    assert not h.calls and cases(h.store.load(RUN))[0]['report']['root_cause'] == 'unknown_effect'


@pytest.mark.asyncio
async def test_persisted_executor_receipt_resumes_only_host_verification(tmp_path):
    from mewcode.experts.assistance import AssistanceReceipt
    h = harness(tmp_path)
    def interrupted(r):
        c = open_case(r, 'work')
        p = plan({'case_id': c['case_id'], 'round': 1}, [option()])
        apply_event(r, c['case_id'], {'kind': 'start', 'identity': 'start:1', 'plan': p})
        request_id = digest([c['case_id'], 1])[:32]
        receipt = AssistanceReceipt(request_id, 'work', 0, (), (), (), 'resume', None, 'saved receipt')
        r['node_states']['work']['assistance_receipts'] = [asdict(receipt)]
    h.store.mutate(RUN, interrupted)
    assert (await run(h))['action'] == 'continue_checkpoint'
    assert not h.calls and len(cases(h.store.load(RUN))[0]['rounds']) == 1


@pytest.mark.asyncio
async def test_probe_user_wait_is_not_swallowed_or_completed(tmp_path):
    h = harness(tmp_path)
    h.coordinator.probe = AsyncMock(side_effect=BranchWaitPaused('work'))
    with pytest.raises(BranchWaitPaused):
        await run(h)
    assert len(h.calls) == 2 and cases(h.store.load(RUN))[0]['status'] == 'executing'
    assert h.store.load(RUN)['node_states']['work']['status'] != 'completed'


@pytest.mark.asyncio
async def test_remaining_node_budget_is_not_replenished_by_assistance(tmp_path):
    h = harness(tmp_path)
    h.policy = replace(h.policy, token_budget=1025)
    with pytest.raises(BranchWaitPaused):
        await run(h)
    assert len(h.calls) == 1 and h.calls[0][2].token_budget == 1025
    assert not any('__assist_' in call[0] for call in h.calls)


@pytest.mark.asyncio
async def test_binding_absence_is_one_round_without_execution(tmp_path):
    h = harness(tmp_path, make_options=lambda n: [option(n, vision=True)])
    h.coordinator.capability_bind = lambda node, selected: BindingResult('capability_absent')
    with pytest.raises(BranchWaitPaused):
        await run(h)
    c = cases(h.store.load(RUN))[0]
    assert len(c['rounds']) == 1 and c['report']['root_cause'] == 'capability_absent'
    assert len(h.calls) == 1


@pytest.mark.asyncio
async def test_changes_to_acceptance_require_fix_not_executor(tmp_path):
    def options(n):
        o = option(n)
        o['changes_acceptance'] = True
        return [o]
    h = harness(tmp_path, make_options=options)
    with pytest.raises(BranchWaitPaused):
        await run(h)
    assert len(h.calls) == 1 and '改变验收' in cases(h.store.load(RUN))[0]['report']['reason']


@pytest.mark.asyncio
@pytest.mark.parametrize('restored', [False, True])
async def test_missing_executor_receipt_is_not_success_but_host_replay_can_restore(tmp_path, restored):
    h = harness(tmp_path, outcomes=[{'passed': True, 'operation_restored': restored, 'evidence_refs': ['host:probe']}]*2)
    original = h.coordinator.executor.dispatch
    async def missing_receipt(helper, prompt, policy):
        if policy.recovery_analysis:
            return await original(helper, prompt, policy)
        return ExpertRoleResult('looks complete', status='completed')
    h.coordinator.executor.dispatch = missing_receipt
    if restored:
        assert (await run(h))['action'] == 'continue_checkpoint'
    else:
        with pytest.raises(BranchWaitPaused):
            await run(h)
    c = cases(h.store.load(RUN))[0]
    receipts = h.store.load(RUN)['node_states']['work']['assistance_receipts']
    assert c['status'] == ('resolved' if restored else 'reported') and len(c['rounds']) == (1 if restored else 2)
    assert all(r['verification']['result'] == ('resolved' if restored else 'unchanged') for r in c['rounds'])
    assert h.store.load(RUN)['node_states']['work']['status'] != 'completed'
    assert all('executor_incomplete' in r['notes'] for r in receipts)


@pytest.mark.parametrize('variant', ['zero', 'six', 'probe'])
def test_schema_rejects_option_count_and_missing_probe(variant):
    p = plan({'case_id': 'case', 'round': 1}, [option()])
    if variant == 'zero':
        p['options'] = []
    elif variant == 'six':
        p['options'] = [option(n) for n in range(6)]
    else:
        p['options'][0]['verification'] = {'expected_evidence': 'missing probe'}
    with pytest.raises(ValueError):
        SubmitRecoveryPlanParams(**p)
