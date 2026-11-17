"""New-flow and legacy checkpoint regressions. No provider/network calls."""
import asyncio
import copy
from dataclasses import asdict, replace

import pytest
from pydantic import ValidationError

from mewcode.expert_group import ExpertExecutionPolicy, ExpertRoleResult
from mewcode.experts.loop_runtime import run_bounded_loop
from mewcode.experts.loop_recovery import unfinished_roots
from mewcode.orchestration.convert import to_expert_flow
from mewcode.orchestration.loops import LoopContract
from mewcode.tools.submit_handoff import SubmitHandoffParams
from tests.test_generic_loops import spec_for, report


def flow_case(team=True):
    flow = to_expert_flow(spec_for("unrelated new workflow"))
    flow.meta["loops"][0]["control_mode"] = "team" if team else "strict"
    return flow, LoopContract.from_value(flow.meta["loops"][0])


async def execute(flow, contract, states, runner, recover=None, events=None):
    async def emit(event):
        if events is not None:
            events.append(copy.deepcopy(event))
    return await run_bounded_loop(contract=contract, flow=flow, saved=states,
        run_node=runner, recover_node=recover, emit=emit, incoming=[],
        policy_for=lambda _: ExpertExecutionPolicy("stable", False, 0))


@pytest.mark.asyncio
@pytest.mark.parametrize("team", [True, False])
async def test_stale_running_checkpoint_dispatches_unfinished_nodes(team):
    flow, contract = flow_case(team)
    states = {"improve": dict(status="running", round=1, round_open=True, tokens=1200, elapsed=12,
        contract_hash=contract.contract_hash, interrupted_node="check", round_results={
            "work": asdict(ExpertRoleResult(text="", status="budget_exhausted", error_kind="segment_timeout")),
            "check": asdict(ExpertRoleResult(text="", status="budget_exhausted"))})}
    calls = []
    async def runner(node, incoming, policy, prompt):
        calls.append(node.node_id)
        if node.node_id == "work":
            assert policy.resume_messages
            return ExpertRoleResult(status="completed", text="actual delivery")
        assert incoming[0]["status"] == "completed"
        return ExpertRoleResult(text="", status="completed", loop_decision=report(policy.loop_context))
    _, result = await execute(flow, contract, states, runner)
    assert calls == ["work", "check"] and result.status == "completed"
    assert states["improve"]["tokens"] == 1200
    assert states["improve"]["elapsed"] >= 12


@pytest.mark.asyncio
async def test_completed_sibling_not_replayed_missing_verdict_is_reopened():
    flow, contract = flow_case()
    states = {"improve": dict(status="running", round=1, round_open=True,
        contract_hash=contract.contract_hash, round_results={
            "work": asdict(ExpertRoleResult(status="completed", text="retained")),
            "check": asdict(ExpertRoleResult(status="completed", text="no structured verdict"))})}
    calls = []
    async def runner(node, incoming, policy, prompt):
        calls.append(node.node_id)
        return ExpertRoleResult(text="", status="completed", loop_decision=report(policy.loop_context))
    await execute(flow, contract, states, runner)
    assert calls == ["check"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["permission_denied", "effect_unknown", "external_dependency"])
async def test_cached_hard_blocker_never_replayed(kind):
    flow, contract = flow_case()
    states = {"improve": dict(status="running", round=1, round_open=True,
        contract_hash=contract.contract_hash, round_results={
            "work": asdict(ExpertRoleResult(text="", status="needs_attention", error_kind=kind))})}
    async def runner(*args):
        pytest.fail("must not rerun a hard blocker")
    _, result = await execute(flow, contract, states, runner)
    assert result.status == "needs_attention" and states["improve"]["status"] == "blocked"


@pytest.mark.asyncio
async def test_cancelled_recovery_persists_latest_result_and_blocked_state():
    flow, contract = flow_case()
    states, events = {}, []
    async def runner(node, *args):
        return ExpertRoleResult(status="needs_attention", error_kind="external_dependency", text="real external failure")
    async def recover(**kwargs):
        assert states["improve"]["status"] == "blocked"
        assert states["improve"]["round_results"]["work"]["text"] == "real external failure"
        assert events[-1]["loop_state"]["status"] == "blocked"
        raise asyncio.CancelledError("scheduler_recovery_paused")
    with pytest.raises(asyncio.CancelledError):
        await execute(flow, contract, states, runner, recover, events)
    assert states["improve"]["failed_node_id"] == "work"


@pytest.mark.asyncio
async def test_stop_persisted_before_missing_report_recovery_cancel():
    flow, contract = flow_case()
    # Missing check output with inactive edge reaches stop() directly.
    flow = replace(flow, edges=tuple(replace(edge, condition="fail") if edge.to_node == "check" else edge for edge in flow.edges))
    states = {"improve": dict(status="running", round=1, round_open=True,
        contract_hash=contract.contract_hash, round_results={"work": asdict(ExpertRoleResult(text="", status="completed"))})}
    async def runner(*args):
        pytest.fail("inactive reviewer should not dispatch")
    async def recover(**kwargs):
        assert states["improve"]["status"] == "blocked"
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await execute(flow, contract, states, runner, recover)


@pytest.mark.asyncio
async def test_new_queue_continues_past_64_segments_only_with_durable_progress():
    flow, contract = flow_case()
    states, calls = {}, []
    async def runner(node, incoming, policy, prompt):
        calls.append(node.node_id)
        if node.node_id == "check":
            return ExpertRoleResult(text="", status="completed", loop_decision=report(policy.loop_context))
        n = calls.count("work")
        if n == 72:
            return ExpertRoleResult(text="", status="completed")
        marker = lambda count: {"review_complete": 0, "review_recorded": 0,
                                "submitted_items": [str(i) for i in range(count)]}
        return ExpertRoleResult(text="", status="needs_attention", handoff={"status": "needs_attention",
            "blocker_kind": "internal_work", "open_items": ["next batch"]},
            work_progress={"before": marker(n-1), "after": marker(n)})
    _, result = await execute(flow, contract, states, runner)
    assert calls.count("work") == 72 and calls[-1] == "check"
    assert result.status == "completed"


@pytest.mark.asyncio
async def test_no_progress_claim_cannot_loop_forever():
    flow, contract = flow_case()
    calls = []
    async def runner(node, *args):
        calls.append(node.node_id)
        return ExpertRoleResult(text="", status="needs_attention", handoff={"status":"needs_attention",
            "blocker_kind":"internal_work", "open_items":["not evidence"]})
    _, result = await execute(flow, contract, {}, runner)
    assert calls == ["work", "work"] and result.status == "needs_attention"


@pytest.mark.asyncio
async def test_recovery_reduced_profile_is_applied_to_next_slice_only():
    flow, contract = flow_case(False)
    slices = []

    async def runner(node, incoming, policy, prompt):
        if node.node_id == "check":
            return ExpertRoleResult(text="", status="completed",
                                    loop_decision=report(policy.loop_context))
        slices.append((policy.max_iterations, policy.timeout_seconds,
                       policy.resume_messages))
        if len(slices) == 1:
            return ExpertRoleResult(status="budget_exhausted",
                                    error_kind="provider_interrupted", text="stream stalled")
        return ExpertRoleResult(status="completed", text="work finished")

    async def recover(**kwargs):
        assert "reduce_execution_profile" in kwargs["allowed"]
        return {"action": "reduce_execution_profile"}

    _, result = await execute(flow, contract, {}, runner, recover)
    assert result.status == "completed"
    assert len(slices) == 2
    assert slices[1][0] < slices[0][0]
    assert slices[1][1] < slices[0][1]
    assert slices[1][2] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("review_status", ["completed", "incomplete_audit"])
async def test_missing_structured_verdict_gets_only_one_correction(review_status):
    flow, contract = flow_case()
    calls = []
    async def runner(node, incoming, policy, prompt):
        calls.append(node.node_id)
        return ExpertRoleResult(status=review_status if node.node_id == "check" else "completed",
                                text="natural language is not a verdict")
    _, result = await execute(flow, contract, {}, runner)
    assert calls == ["work", "check", "check"]
    assert result.status == "needs_attention"


@pytest.mark.parametrize("extra", [{"question":"approve deletion"}, {"blockers":["credential absent"]}, {"status":"completed"}, {"open_items":[]}])
def test_internal_work_cannot_hide_real_blockers(extra):
    with pytest.raises(ValidationError):
        SubmitHandoffParams(**dict({"summary":"batch", "status":"needs_attention", "blocker_kind":"internal_work", "open_items":["next"]}, **extra))


def test_store_resume_repairs_legacy_running_loop_not_only_blocked(tmp_path):
    from tests.test_team_loop_control import legacy_poisoned_run, grant
    store, run_id, flow, contract, snapshot = legacy_poisoned_run(tmp_path)
    changed = grant(store, run_id, snapshot)
    def stale(value):
        value["status"] = "paused"
        value["loop_states"]["improve"].update(status="running", error_kind="", failed_node_id="")
    store.mutate(run_id, stale)
    restored = store.resume(run_id, strategy="stable")
    loop = restored["loop_states"]["improve"]
    assert list(loop["round_results"]) == ["retrieve"]
    assert loop["tokens"] == changed["loop_states"]["improve"]["tokens"]
    assert loop["next_node_id"] == "extract"


def test_work_high_water_mark_never_regresses():
    from mewcode.experts.recovery_state import advance_work_cursor
    original = {"review_complete": 4, "review_recorded": 8, "submitted_items": ["a"]}
    regressed = {"review_complete": 0, "review_recorded": 1, "submitted_items": ["b"]}
    advanced, cursor = advance_work_cursor({"after": regressed}, original)
    assert advanced and cursor == {"schema_version": 2, "review_complete": 4, "review_recorded": 8,
                                   "submitted_items": ["a", "b"], "write_receipts": []}
    advanced, cursor = advance_work_cursor({"after": original}, cursor)
    assert not advanced and cursor["submitted_items"] == ["a", "b"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["permission", "unknown_effect", "human_question"])
async def test_real_progress_never_bypasses_host_blocker(kind):
    flow, contract = flow_case()
    calls = []
    async def runner(node, *args):
        calls.append(node.node_id)
        return ExpertRoleResult(text="partial", status="budget_exhausted", work_progress={
            "before": {"review_complete": 0, "review_recorded": 0, "submitted_items": []},
            "after": {"review_complete": 1, "review_recorded": 1, "submitted_items": ["a"]},
            "blockers": [{"kind": kind, "ref": "unresolved"}]})
    _, result = await execute(flow, contract, {}, runner)
    assert calls == ["work"] and result.status == "needs_attention"


@pytest.mark.asyncio
@pytest.mark.parametrize("kinds,original,expected", [
    (["environment_fault"], "environment_fault", "environment_fault"),
    (["capability_missing"], "", "capability_missing"),
    (["provider_error"], "provider_cooldown", "provider_cooldown"),
    (["safety_review_unavailable"], "", "safety_review_unavailable"),
    (["environment_fault", "human_question"], "environment_fault", "awaiting_input"),
    (["environment_fault", "missing_authorization"], "environment_fault", "missing_authorization"),
    (["environment_fault", "unknown_effect"], "environment_fault", "effect_unknown"),
])
async def test_loop_preserves_typed_continuation_blocker(kinds, original, expected):
    flow, contract = flow_case()
    states, calls, recoveries = {}, [], []

    async def runner(node, *args):
        calls.append(node.node_id)
        return ExpertRoleResult(text="preflight failed", status="needs_attention", error_kind=original,
            work_progress={"continuation_blockers": [{"kind": kind} for kind in kinds]})

    async def recover(**kwargs):
        recoveries.append(kwargs["result"].error_kind)

    _, result = await execute(flow, contract, states, runner, recover)
    assert calls == ["work"]
    assert result.error_kind == states["improve"]["error_kind"] == expected
    assert recoveries and set(recoveries) == {expected}
    assert ("请回答具体问题" in result.text) == (expected == "awaiting_input")


@pytest.mark.asyncio
async def test_protocol_correction_preserves_strict_token_limit():
    flow, contract = flow_case(False)
    calls = []
    async def runner(node, incoming, policy, prompt):
        calls.append(node.node_id)
        return ExpertRoleResult(text="no verdict", status="completed",
                                billable_tokens=contract.token_budget if node.node_id == "check" else 0)
    _, result = await execute(flow, contract, {}, runner)
    assert calls == ["work", "check"] and result.status == "needs_attention"


def test_domain_progress_retains_unresolved_effects():
    from mewcode.experts.recovery_state import work_progress
    value = work_progress({"node_states": {"work": {"tools": {
        "cmd": {"name": "Bash", "status": "outcome_unknown"}}}}}, "work")
    assert value["blockers"][0]["kind"] == "unknown_effect"


@pytest.mark.asyncio
async def test_direct_legacy_recovery_invalidates_nested_input():
    from tests.test_team_loop_control import nested_flow, report as chain_report
    flow, contract = nested_flow()
    states = {}
    async def first(node, incoming, policy, prompt):
        if node.node_id == "rewrite":
            return ExpertRoleResult(text="paused", status="needs_attention", error_kind="awaiting_input")
        return ExpertRoleResult(text="old " + node.node_id, billable_tokens=10,
            loop_decision=chain_report(policy.loop_context) if node.node_id == "analyze" else None)
    await execute(flow, contract, states, first)
    states["improve"].update(status="running", error_kind="", failed_node_id="")
    states["improve"]["round_results"]["retrieve"]["status"] = "budget_exhausted"
    old_tokens = states["improve/1/inner"]["tokens"]
    calls = []
    async def second(node, incoming, policy, prompt):
        calls.append(node.node_id)
        if incoming:
            assert incoming[0]["text"].startswith("new ")
        return ExpertRoleResult(text="new " + node.node_id, billable_tokens=10,
            loop_decision=chain_report(policy.loop_context) if node.node_id in {"analyze", "check"} else None)
    _, result = await execute(flow, contract, states, second)
    assert result.status == "completed"
    assert calls == ["retrieve", "extract", "analyze", "rewrite", "check"]
    assert states["improve/1/inner"]["tokens"] == old_tokens + 20
