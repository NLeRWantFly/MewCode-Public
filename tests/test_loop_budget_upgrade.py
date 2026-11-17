import copy

import pytest

from mewcode.agent import UsageEvent
from mewcode.expert_group import ExpertGroupOrchestrator, ExpertRoleResult, ExpertProfile
from mewcode.model_registry import ModelRef
from mewcode.orchestration.convert import to_expert_flow
from mewcode.orchestration.loops import LoopContract, LoopWatchdog, fingerprint
from mewcode.usage_budget import BudgetTokenMeter
from tests.test_generic_loops import spec_for, report


def test_cache_budget_counts_misses_and_output_not_repeated_full_context():
    meter = BudgetTokenMeter()
    for i in range(1, 7):
        meter.observe(UsageEvent(input_tokens=i * 1000, output_tokens=i * 1500,
            request_input_tokens=10000, request_cache_usage_available=True,
            request_cache_miss_tokens=1000, request_cache_read_tokens=9000))
    assert meter.tokens == 15000


def test_unknown_cache_uses_positive_prompt_growth_and_handles_provider_switch():
    meter = BudgetTokenMeter()
    for i, prompt in enumerate([10000, 10500, 10500, 4000, 4500], 1):
        meter.observe(UsageEvent(input_tokens=i * 10000, output_tokens=i * 100,
                                 request_input_tokens=prompt))
    assert meter.tokens == 11500
    meter.observe(UsageEvent(input_tokens=51000, output_tokens=600, request_input_tokens=6000,
        request_cache_usage_available=True, request_cache_miss_tokens=1000, request_cache_creation_tokens=500))
    assert meter.tokens == 13100


def test_serialized_legacy_contract_keeps_hash_and_spent_budget():
    # Exact shape written by the previous implementation (including old defaults).
    old = dict(loop_id="old", entry_node_id="work", check_node_id="check", node_ids=["work", "check"],
        exit_condition="verified", criteria=[{"criterion_id": "a", "acceptance_test": "verified"}],
        reviewer_node_ids=["check"], max_iterations=4, token_budget=60000,
        timeout_seconds=900.0, max_stalled_rounds=2)
    contract = LoopContract.from_value(old)
    assert contract.as_dict() == old and contract.contract_hash == fingerprint(old)
    assert LoopWatchdog(contract, {"contract_hash": fingerprint(old), "tokens": 55000}).state["tokens"] == 55000
    assert LoopContract.from_value({k: v for k, v in old.items() if k != "token_budget"}).token_budget == 300000
    changed = LoopContract.from_value({**old, "node_max_iterations": 60})
    assert changed.contract_hash != contract.contract_hash
    with pytest.raises(ValueError):
        LoopWatchdog(changed, {"contract_hash": contract.contract_hash})


@pytest.mark.parametrize("field,value", [("node_max_iterations", 3), ("node_max_iterations", 201),
                                        ("reviewer_max_iterations", 3), ("reviewer_max_iterations", 201)])
def test_iteration_limits_are_validated(field, value):
    raw = spec_for().meta["loops"][0]
    with pytest.raises(ValueError):
        LoopContract.from_value({**raw, field: value})


@pytest.mark.asyncio
async def test_incomplete_executor_is_resumed_before_review(tmp_path):
    calls, states = [], {}

    async def runner(node, _model, _prompt, policy, member=None):
        calls.append(node.node_id)
        if node.node_id == "work":
            first = calls.count("work") == 1
            assert policy.max_iterations == 40 and policy.resume_messages is not first
            await policy.usage_callback(500)
            return ExpertRoleResult("work", status="budget_exhausted" if first else "completed",
                                    input_tokens=100000, billable_tokens=500)
        if node.node_id == "check":
            assert policy.max_iterations == 12
            return ExpertRoleResult("check", loop_decision=report(policy.loop_context), billable_tokens=100)
        return ExpertRoleResult(node.node_id)

    def sink(event):
        if event.get("loop_state"):
            states[event["loop_state_key"]] = copy.deepcopy(event["loop_state"])

    ref = ModelRef("test", "test", "")
    orchestrator = ExpertGroupOrchestrator(runner=lambda *_: None, node_runner=runner,
                                         archive_root=tmp_path, event_sink=sink)
    raw = spec_for().as_dict()
    raw['meta']['loops'][0]['control_mode'] = 'team'
    from mewcode.orchestration.spec import OrchestrationSpec
    result = await orchestrator.run("task", ExpertProfile(ref, ref, ref), {}, strategy="stable", flow=to_expert_flow(OrchestrationSpec.from_value(raw)))
    assert result.status == "completed"
    assert calls == ["prepare", "work", "work", "check", "done"]
    assert states["improve"]["tokens"] == 1100
    assert states["improve"]["interrupted_nodes"] == []
