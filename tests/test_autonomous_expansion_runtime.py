"""Real collaboration scheduler expands, accepts and joins versioned contracts."""
import copy

import pytest

from mewcode.expert_group import ExpertRoleResult
from mewcode.kernel import collaboration as ledger
from mewcode.kernel.collaboration_graph import effective_flow
from tests.test_autonomous_fragment import _sample_flow, _proposal, ALLOWED_TOOLS
from tests.test_autonomous_context_runtime import retry_store, orchestrate, run_flow
from tests.test_collaboration_mode import RUN_ID, delivery


@pytest.mark.asyncio
async def test_nested_subcontracts_execute_once_and_join_without_business_model(tmp_path):
    flow = _sample_flow()
    store = retry_store(tmp_path, flow)
    invocations, reviews, proposals = [], [], []

    async def runner(node, model, prompt, policy, member=None):
        invocations.append(node.node_id)
        contract = store.load(RUN_ID)["collaboration"]["contracts"][node.node_id]
        if node.node_id == "work" or contract.get("expansion_depth") == 1 and node.node_id.endswith("::first"):
            assert policy.collaborative_expansion
            assert policy.authorization_node_id == "work"
            paths = {} if node.node_id == "work" else dict(first_paths=["work/a/x"], second_paths=["work/a/y"])
            proposal = _proposal(node.node_id, effective_flow(store.load(RUN_ID)).version, **paths)
            policy.expansion_holder.set_allowed_tools(set(ALLOWED_TOOLS))
            validated = policy.expansion_holder.submit(proposal)
            proposals.append(node.node_id)
            return ExpertRoleResult(text="提出细分合同", status="node_expansion", node_expansion=validated.as_dict())
        if contract.get("expansion_depth") == 2:
            assert policy.expansion_holder is None
            assert policy.expansion_write_paths
        return delivery("固定成果：" + node.node_id)

    async def review(model, payload, context):
        reviews.append((payload["contract"]["id"], payload["check"]["id"]))
        return {"passed": True, "evidence": ["固定交付满足完整合同"]}

    async def coordinate(model, payload, system):
        assert "fragment" in payload
        assert payload["fragment"]["contracts"][payload["parent_contract"]["id"]]["execution_kind"] == "join"
        return {"passed": True, "reason": "需求、写入范围和父级验收保留", "evidence": ["parent_contract", "fragment"]}

    result = await run_flow(orchestrate(store, tmp_path, runner, review, coordinate), flow, store)
    assert result.status == "completed", result.final_summary
    record = store.load(RUN_ID)
    value = ledger.state(record)
    assert len(proposals) == 2 and len(invocations) == len(set(invocations))
    assert value["plan_version"] == 3 and record["locked_flow"]["version"] == 1
    assert value["lineage_budget"]["lineages"]["work"]["used"]["expansions"] == 2
    assert all(ledger.valid_certificate(value, key) for key in value["contracts"])
    for key in proposals:
        assert any(node == key and check.startswith("join") for node, check in reviews)
        accepted = value["attempts"][value["certificates"][key]["attempt_id"]]
        assert accepted["delivery"]["handoff"]["child_certificates"]
        assert invocations.count(key) == 1
    before = copy.deepcopy(invocations)
    replay = await run_flow(orchestrate(store, tmp_path, runner, review, coordinate), flow, store)
    assert replay.status == "completed", replay.final_summary
    assert invocations == before


@pytest.mark.asyncio
async def test_rejected_expansion_never_dispatches_children(tmp_path):
    flow = _sample_flow()
    store = retry_store(tmp_path, flow)
    invocations = []

    async def runner(node, model, prompt, policy, member=None):
        invocations.append(node.node_id)
        policy.expansion_holder.set_allowed_tools(set(ALLOWED_TOOLS))
        validated = policy.expansion_holder.submit(_proposal())
        return ExpertRoleResult(text="请求划分", status="node_expansion", node_expansion=validated.as_dict())

    async def coordinate(*args):
        return {"passed": False, "reason": "未覆盖父合同", "evidence": ["缺少完整结果"]}

    result = await run_flow(orchestrate(store, tmp_path, runner, None, coordinate), flow, store)
    assert result.status == "blocked"
    value = ledger.state(store.load(RUN_ID))
    assert invocations == ["work"]
    assert set(value["contracts"]) == {"work", "deliver"}
    assert value["plan_holds"] == {}
    assert value["lineage_budget"]["root"]["used"]["expansions"] == 0
    assert any(gate["kind"] == "expansion_rejected" for gate in value["recovery_gates"].values())


@pytest.mark.asyncio
async def test_missing_independent_reviewer_releases_prepared_hold_and_exposes_recovery(tmp_path):
    from tests.test_collaboration_mode import EXECUTOR
    flow = _sample_flow()
    store = retry_store(tmp_path, flow)
    store.mutate(RUN_ID, lambda record: record['role_models']['roles'].update(reviewer=EXECUTOR.as_dict()), durable=True)
    called = []

    async def runner(node, model, prompt, policy, member=None):
        called.append(node.node_id)
        policy.expansion_holder.set_allowed_tools(set(ALLOWED_TOOLS))
        proposal = policy.expansion_holder.submit(_proposal())
        return ExpertRoleResult(text='请求展开', status='node_expansion', node_expansion=proposal.as_dict())

    result = await run_flow(orchestrate(store, tmp_path, runner, None, None), flow, store)
    value = ledger.state(store.load(RUN_ID))
    assert result.status == 'blocked' and called == ['work']
    assert value['plan_holds'] == {}
    assert all(row['status'] == 'rejected' for row in value['graph_publications'].values())
    assert value['lineage_budget']['root']['reserved']['expansions'] == 0
    assert any(row['kind'] == 'collaboration_checkpoint' and row['status'] == 'waiting'
               for row in value['recovery_gates'].values())


@pytest.mark.asyncio
async def test_join_failure_preserves_children_and_only_rechecks_parent_on_retry(tmp_path):
    from mewcode.kernel.collaboration_recovery import resolve_gate
    flow = _sample_flow()
    store = retry_store(tmp_path, flow)
    invoked, allow_join = [], []
    async def runner(node, model, prompt, policy, member=None):
        invoked.append(node.node_id)
        if node.node_id == 'work':
            policy.expansion_holder.set_allowed_tools(set(ALLOWED_TOOLS))
            proposal = policy.expansion_holder.submit(_proposal())
            return ExpertRoleResult(text='展开', status='node_expansion', node_expansion=proposal.as_dict())
        return delivery(node.node_id)
    async def review(model, payload, context):
        passed = payload['contract']['id'] != 'work' or bool(allow_join)
        return {'passed': passed, 'evidence': ['父级完整检查' if passed else '子结果尚未组合成完整交付']}
    async def coordinate(model, payload, system):
        if 'fragment' in payload:
            return {'passed': True, 'evidence': ['父目标与范围保留']}
        return {'action': 'wait', 'reason': '需要核对父返回'}
    host = orchestrate(store, tmp_path, runner, review, coordinate)
    result = await run_flow(host, flow, store)
    value = ledger.state(store.load(RUN_ID))
    children = value['contracts']['work']['join_children']
    assert result.status == 'blocked'
    assert all(ledger.valid_certificate(value, key) for key in children)
    assert not ledger.valid_certificate(value, 'work')
    assert 'deliver' not in invoked
    gate = next(row for row in value['recovery_gates'].values() if row['status'] == 'waiting')
    store.mutate(RUN_ID, lambda record: resolve_gate(record, gate_id=gate['id'], action='retry',
        expected_version=value['spec_version'], request_id='join-retry', reason='核对父返回', actor='user'), durable=True)
    allow_join.append(True)
    resumed = await run_flow(host, flow, store)
    value = ledger.state(store.load(RUN_ID))
    assert resumed.status == 'completed', resumed.final_summary
    assert invoked.count('work') == 1
    assert all(invoked.count(key) == 1 for key in children)
    assert ledger.valid_certificate(value, 'work')
