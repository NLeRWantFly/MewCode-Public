"""Independent review dispatch without Agent or fabricated acceptance."""
import asyncio
import json
from dataclasses import asdict, replace

import pytest

from mewcode.expert_group import ExpertRoleResult
from mewcode.experts.loop_recovery import reusable_result
from mewcode.tools.repair_work_batch import RepairWorkBatchParams, revision
from mewcode.tools.request_verification import RequestVerification, VerificationParams
from tests.test_generic_loops import report
from tests.test_loop_checkpoint_liveness import execute, flow_case
from tests.test_repair_work_batch import candidates, fixture, freeze


async def submitted(root):
    _, worker, store, run_id, ids = await fixture(root)
    batch = await freeze(worker, ids[:1])
    payload = candidates(root, ids[:1])
    result = await worker.execute(RepairWorkBatchParams(action="submit", reviewer_node_id="check",
        batch_id=batch["batch_id"], candidates=payload))
    assert not result.is_error
    tool = RequestVerification(store=store, run_id=run_id, owner="repair", reviewers=["check"],
                               contract_hash="frozen", root=root)
    params = VerificationParams(reviewer_node_id="check", batch_ids=[batch["batch_id"]],
                                summary="one batch ready", remaining_work=["remaining pages"])
    return tool, params, store, run_id, ids, payload


@pytest.mark.asyncio
async def test_request_persists_versioned_batch_not_pass(tmp_path):
    tool, params, store, run_id, ids, _ = await submitted(tmp_path)
    result = await tool.execute(params)
    assert not result.is_error, result.output
    assert json.loads(result.output)["status"] == "review_pending"
    request = tool.revalidate()
    assert set(request["candidate_revisions"]) == {ids[0]}
    assert store.load(run_id)["node_states"]["repair"]["verification_request"] == request
    assert all(r["state"] == "needs_repair" for r in store.load(run_id)["review_queues"]["check"]["records"].values())
    assert not (await tool.execute(params)).is_error
    assert tool.revalidate() == request


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["owner", "reviewer", "stale", "already_reviewed", "unsubmitted", "contract"])
async def test_no_self_review_unsubmitted_stale_or_foreign_receipts(tmp_path, failure):
    tool, params, store, run_id, ids, payload = await submitted(tmp_path)
    if failure == "owner":
        tool.owner = "other"
    elif failure == "reviewer":
        params.reviewer_node_id = "repair"
    elif failure == "stale":
        (tmp_path / payload[0]["candidate"]["path"]).write_text("changed")
    elif failure == "already_reviewed":
        def mutate(s):
            queue = s["review_queues"]["check"]
            queue["records"][ids[0]]["candidate_revision"] = queue["repair_candidates"][ids[0]]["candidate_revision"]
        store.mutate(run_id, mutate)
    elif failure == "unsubmitted":
        store.mutate(run_id, lambda s: s["review_queues"]["check"]["repair_batches"][params.batch_ids[0]].update(status="frozen"))
    else:
        tool.contract_hash = "other"
    assert (await tool.execute(params)).is_error
    assert tool.value is None


@pytest.mark.asyncio
async def test_same_turn_late_write_invalidates_request(tmp_path):
    tool, params, _, _, _, payload = await submitted(tmp_path)
    assert not (await tool.execute(params)).is_error
    (tmp_path / payload[0]["candidate"]["path"]).write_text("late write")
    with pytest.raises(ValueError):
        tool.revalidate()


def pending(contract):
    receipt = {"version": 1, "owner": "work", "reviewer_node_id": "check",
        "contract_hash": contract.contract_hash, "batch_ids": ["batch"],
        "candidate_revisions": {"p15": "a" * 64}, "summary": "batch ready", "remaining_work": ["190 remain"]}
    receipt["request_id"] = revision(receipt)
    return ExpertRoleResult(text="queued", status="review_pending", verification_request=receipt,
                            handoff={"status": "review_pending", "summary": "ready"})


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["always", "pass"])
async def test_pending_batch_dispatches_review_and_resume_skips_worker(condition):
    flow, contract = flow_case()
    flow = replace(flow, edges=tuple(replace(e, condition=condition) if e.to_node == "check" else e for e in flow.edges))
    states, calls = {}, []
    async def runner(node, incoming, policy, prompt):
        calls.append(node.node_id)
        if node.node_id == "work":
            return pending(contract)
        assert incoming[0]["status"] == "review_pending"
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await execute(flow, contract, states, runner)
    assert calls == ["work", "check"]
    assert reusable_result("work", states["improve"]["round_results"]["work"], contract)
    async def resume(node, incoming, policy, prompt):
        calls.append(node.node_id)
        assert node.node_id == "check"
        return ExpertRoleResult(text="independent", loop_decision=report(policy.loop_context))
    _, result = await execute(flow, contract, states, resume)
    assert result.status == "completed" and calls == ["work", "check", "check"]


@pytest.mark.asyncio
async def test_review_continue_returns_to_worker_not_user_question():
    flow, contract = flow_case()
    calls = []
    async def runner(node, incoming, policy, prompt):
        calls.append(node.node_id)
        if node.node_id == "work":
            return pending(contract) if calls.count("work") == 1 else ExpertRoleResult(text="remaining work delivered")
        verdict = report(policy.loop_context)
        if calls.count("check") == 1:
            verdict = {**report(policy.loop_context, "FAIL"), "repair_node_ids": ["work"]}
        return ExpertRoleResult(text="independent", loop_decision=verdict)
    _, result = await execute(flow, contract, {}, runner)
    assert calls == ["work", "check", "work", "check"] and result.status == "completed"


@pytest.mark.parametrize("change", [{"owner": "other"}, {"reviewer_node_id": "work"}, {"contract_hash": "stale"}])
def test_pending_checkpoint_does_not_accept_wrong_binding(change):
    _, contract = flow_case()
    result = asdict(pending(contract))
    result["verification_request"].update(change)
    assert not reusable_result("work", result, contract)
