from __future__ import annotations

import pytest
import httpx
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from mewcode.expert_group import ExpertExecutionPolicy, ExpertRoleResult
from mewcode.experts.loop_runtime import run_bounded_loop
from mewcode.orchestration.convert import to_expert_flow
from mewcode.orchestration.loops import LoopContract, LoopWatchdog
from mewcode.orchestration.token_window import (
    advance_token_window, append_window_change, initial_token_window,
    token_bucket_delay,
)
from tests.test_generic_loops import report, spec_for
from tests.test_loop_unlimited_budget import budget_case


def test_productive_receipt_grows_only_soft_window_and_forecasts_from_real_samples():
    state = initial_token_window(spent_tokens=0, floor=4_000, ceiling=30_000)
    state, first = advance_token_window(state, segment_id="one", spent_tokens=3_000,
        durable_progress=True, progress_items=2, remaining_items=10)
    assert first["signal"] == "durable_ack"
    assert first["next_cwnd"] == 8_000 and first["issued_until"] == 11_000
    assert first["forecast_remaining_tokens"] == 18_750
    assert first["increases_total_budget"] is False
    repeated, same = advance_token_window(state, segment_id="one", spent_tokens=3_000,
        durable_progress=True, progress_items=2, remaining_items=10)
    assert repeated == state and same == first
    with pytest.raises(ValueError, match="conflicting"):
        advance_token_window(state, segment_id="one", spent_tokens=3_001,
            durable_progress=True, progress_items=2, remaining_items=10)


def test_loss_probe_then_replan_never_buys_more_for_empty_progress():
    state = initial_token_window(spent_tokens=0, floor=2_000, ceiling=20_000)
    state, ack = advance_token_window(state, segment_id="ack", spent_tokens=1_000,
        durable_progress=True, progress_items=1)
    assert ack["next_cwnd"] == 4_000
    state, first = advance_token_window(state, segment_id="loss", spent_tokens=2_500,
        durable_progress=False)
    assert first["next_cwnd"] == 2_000 and first["action"] == "continue"
    state, second = advance_token_window(state, segment_id="timeout", spent_tokens=3_000,
        durable_progress=False)
    assert second["action"] == "replan" and second["issued_until"] == 3_000
    assert state["no_progress_streak"] == 2


def test_empty_cancelled_segment_does_not_increase_or_strike():
    state = initial_token_window(spent_tokens=100, floor=2_000, ceiling=10_000)
    state, receipt = advance_token_window(state, segment_id="empty", spent_tokens=100,
        durable_progress=False)
    assert receipt["signal"] == "no_sample"
    assert state["cwnd"] == 2_000 and state["no_progress_streak"] == 0


def test_measured_token_bucket_debt_survives_resume_without_duplicate_charge():
    state = initial_token_window(spent_tokens=0, floor=2_000, ceiling=20_000,
        rate_per_minute=60, burst_tokens=2_000, now=100)
    state, receipt = advance_token_window(state, segment_id="over-burst",
        spent_tokens=2_500, durable_progress=True, progress_items=1, now=100)
    assert receipt["token_bucket_wait_seconds"] == 500
    assert state["token_bucket"]["available"] == -500
    repeated, same = advance_token_window(state, segment_id="over-burst",
        spent_tokens=2_500, durable_progress=True, progress_items=1, now=100)
    assert repeated == state and same == receipt
    updated, wait = token_bucket_delay(state, spent_tokens=2_500, now=350)
    assert wait == 250
    restored, wait = token_bucket_delay(updated, spent_tokens=2_500, now=600)
    assert wait == 0 and restored["token_bucket"]["available"] == 0
    assert restored["token_bucket"]["last_settled_tokens"] == 2_500


def test_token_bucket_rejects_invalid_receipts_and_clock_rollback_does_not_refill():
    state = initial_token_window(spent_tokens=0, floor=2_000, ceiling=10_000,
        rate_per_minute=60, now=100)
    state, _ = advance_token_window(state, segment_id="a", spent_tokens=2_500,
        durable_progress=False, now=100)
    updated, wait = token_bucket_delay(state, spent_tokens=2_500, now=90)
    assert wait == 500 and updated["token_bucket"]["last_refill_at"] == 100
    updated["token_bucket"]["available"] = float("nan")
    with pytest.raises(ValueError, match="bucket"):
        token_bucket_delay(updated, spent_tokens=2_500, now=101)
    with pytest.raises(ValueError, match="rate"):
        initial_token_window(spent_tokens=0, floor=2_000, ceiling=10_000,
                             rate_per_minute=600_001)


def test_bucket_paces_unsettled_recovery_decision_tokens_once():
    state = initial_token_window(spent_tokens=0, floor=2_000, ceiling=10_000,
        rate_per_minute=60, now=100)
    state, wait = token_bucket_delay(state, spent_tokens=2_500, now=100,
                                     charge_unsettled=True)
    assert wait == 500 and state["token_bucket"]["last_settled_tokens"] == 2_500
    repeated, wait = token_bucket_delay(state, spent_tokens=2_500, now=100,
                                         charge_unsettled=True)
    assert wait == 500 and repeated == state


def test_run_audit_ledger_mirrors_one_identical_receipt_and_rejects_conflict():
    state = initial_token_window(spent_tokens=0, floor=2_000, ceiling=10_000)
    state, receipt = advance_token_window(state, segment_id="seg", spent_tokens=500,
        durable_progress=True, progress_items=1)
    loop_state = {"loop_resource_policy": {"mode": "adaptive", "limit": 10_000},
                  "token_window_receipts": [receipt]}
    snapshot = {}
    assert append_window_change(snapshot, loop_state_key="loop", loop_id="loop",
                                loop_state=loop_state)
    assert not append_window_change(snapshot, loop_state_key="loop", loop_id="loop",
                                    loop_state=loop_state)
    assert len(snapshot["loop_budget_changes"]) == 1
    changed = {**loop_state, "token_window_receipts": [{**receipt, "spent_tokens": 501}]}
    with pytest.raises(ValueError, match="conflicting"):
        append_window_change(snapshot, loop_state_key="loop", loop_id="loop",
                             loop_state=changed)


def test_total_ceiling_and_actual_overrun_have_distinct_actions():
    state = initial_token_window(spent_tokens=0, floor=2_000, ceiling=10_000)
    state, limit = advance_token_window(state, segment_id="at-limit", spent_tokens=10_000,
        durable_progress=True, progress_items=1, remaining_items=2)
    assert limit["action"] == "request_total_cap"
    assert limit["forecast_remaining_tokens"] == 25_000
    state, overrun = advance_token_window(state, segment_id="overrun", spent_tokens=10_050,
        durable_progress=False)
    assert overrun["action"] == "reconcile_overrun"
    assert overrun["issued_until"] == 10_000


def test_eighty_percent_warning_does_not_grant_a_higher_total():
    state = initial_token_window(spent_tokens=0, floor=2_000, ceiling=10_000)
    state, warning = advance_token_window(state, segment_id="near", spent_tokens=8_000,
        durable_progress=True, progress_items=2, remaining_items=1)
    assert warning["near_ceiling"] is True
    assert warning["upward_warning"] == "approaching_authorized_ceiling"
    assert warning["action"] == "continue"
    assert warning["issued_until"] == 10_000


def test_team_mode_remains_unlimited_even_with_stale_adaptive_policy():
    raw = {**spec_for().meta["loops"][0], "control_mode": "team"}
    contract = LoopContract.from_value(raw)
    state = LoopWatchdog(contract).state
    state["loop_resource_policy"] = {"mode": "adaptive", "limit": 12_000,
        "floor": 2_000, "authorized_by": "user", "change_id": "grant-1",
        "contract_hash": contract.contract_hash}
    state["token_window"] = initial_token_window(spent_tokens=0, floor=2_000,
                                                   ceiling=12_000)
    watchdog = LoopWatchdog(contract, state)
    assert watchdog.token_limit is None
    assert watchdog.settle_token_window(segment_id="seg-1", durable_progress=True) is None
    assert watchdog.contract.contract_hash == contract.contract_hash


def test_crash_before_window_settlement_still_detects_actual_overrun():
    contract = LoopContract.from_value(spec_for().meta["loops"][0])
    state = LoopWatchdog(contract).state
    state["loop_resource_policy"] = {"mode": "adaptive", "limit": 10_000,
        "floor": 2_000, "authorized_by": "user", "change_id": "grant",
        "contract_hash": contract.contract_hash}
    state["token_window"] = initial_token_window(spent_tokens=0, floor=2_000,
                                                   ceiling=10_000)
    state["tokens"] = 10_100
    watchdog = LoopWatchdog(contract, state)
    assert watchdog.remaining() == "Token 用量超过获批总额，须先对账"
    assert watchdog.stop_kind(watchdog.remaining()) == "token_accounting_overrun"


def test_store_grant_is_finite_cas_bound_and_does_not_reset_progress(tmp_path):
    runs, run_id, _, contract, before = budget_case(tmp_path)
    changed = runs.set_loop_token_policy(run_id, loop_id="improve",
        expected_updated_at=before["updated_at"], command_id="adaptive-1",
        reason="Explicit finite ceiling", ceiling=400_000)
    state = changed["loop_states"]["improve"]
    assert state["tokens"] == 320_920
    assert LoopWatchdog(contract, state).token_limit == 337_304
    assert state["budget_resume_pending"] is True
    assert "token_bucket" not in state["token_window"]
    assert changed["locked_flow"] == before["locked_flow"]
    assert len(changed["loop_budget_changes"]) == 1
    repeated = runs.set_loop_token_policy(run_id, loop_id="improve",
        expected_updated_at=before["updated_at"], command_id="adaptive-1",
        reason="Explicit finite ceiling", ceiling=400_000)
    assert len(repeated["loop_budget_changes"]) == 1
    with pytest.raises(ValueError):
        runs.set_loop_token_policy(run_id, loop_id="improve",
            expected_updated_at=before["updated_at"], command_id="adaptive-2",
            reason="stale", ceiling=400_000)


def test_store_explicit_bucket_rate_is_bound_to_user_grant(tmp_path):
    runs, run_id, _, contract, before = budget_case(tmp_path)
    changed = runs.set_loop_token_policy(run_id, loop_id="improve",
        expected_updated_at=before["updated_at"], command_id="paced-1",
        reason="Bounded rate", ceiling=400_000, rate_per_minute=12_000,
        burst_tokens=20_000)
    state = changed["loop_states"]["improve"]
    assert state["token_window"]["token_bucket"]["rate_per_minute"] == 12_000
    assert state["token_window"]["token_bucket"]["burst_tokens"] == 20_000
    assert LoopWatchdog(contract, state).token_limit == 337_304
    with pytest.raises(ValueError, match="突发容量需要"):
        runs.set_loop_token_policy(run_id, loop_id="improve",
            expected_updated_at=before["updated_at"], command_id="bad",
            reason="bad", ceiling=400_000, burst_tokens=20_000)


def test_corrupt_spent_usage_cannot_be_truncated_into_a_new_grant(tmp_path):
    runs, run_id, _, _, before = budget_case(tmp_path)
    changed = runs.mutate(run_id, lambda value: value["loop_states"]["improve"].update(
        tokens=320_920.5))
    with pytest.raises(ValueError, match="须先对账"):
        runs.set_loop_token_policy(run_id, loop_id="improve",
            expected_updated_at=changed["updated_at"], command_id="bad-usage",
            reason="do not round usage", ceiling=400_000)


@pytest.mark.asyncio
async def test_runtime_advances_window_only_after_host_progress_receipt():
    flow = to_expert_flow(spec_for())
    contract = LoopContract.from_value(flow.meta["loops"][0])
    state = LoopWatchdog(contract).state
    state["loop_resource_policy"] = {"mode": "adaptive", "limit": 30_000,
        "floor": 2_000, "authorized_by": "user", "change_id": "grant",
        "contract_hash": contract.contract_hash}
    state["token_window"] = initial_token_window(spent_tokens=0, floor=2_000,
                                                   ceiling=30_000)
    calls = []

    async def emit(event):
        pass

    async def runner(node, incoming, policy, prompt):
        calls.append((node.node_id, policy.token_budget))
        if node.node_id == "work":
            before = {"review_complete": 0, "review_recorded": 0,
                      "submitted_items": [], "write_receipts": []}
            return ExpertRoleResult(text="saved", billable_tokens=1_500,
                work_progress={"before": before, "after": {**before,
                    "write_receipts": ["durable-r1"]}})
        return ExpertRoleResult(text="checked", billable_tokens=1_500,
                                loop_decision=report(policy.loop_context))

    saved = {"improve": state}
    _, result = await run_bounded_loop(contract=contract, flow=flow, saved=saved,
        run_node=runner, emit=emit,
        policy_for=lambda _: ExpertExecutionPolicy("stable", False, 0), incoming=[])
    assert result.status == "completed"
    assert calls == [("work", 2_000), ("check", 4_000)]
    receipts = saved["improve"]["token_window_receipts"]
    assert receipts[0]["signal"] == "durable_ack"
    assert receipts[1]["signal"] == "loss_probe"
    assert saved["improve"]["tokens"] == 3_000


@pytest.mark.asyncio
async def test_runtime_queues_next_slice_for_measured_bucket_debt():
    flow = to_expert_flow(spec_for())
    contract = LoopContract.from_value(flow.meta["loops"][0])
    state = LoopWatchdog(contract).state
    state["loop_resource_policy"] = {"mode": "adaptive", "limit": 30_000,
        "floor": 2_000, "authorized_by": "user", "change_id": "paced",
        "contract_hash": contract.contract_hash, "rate_per_minute": 60_000,
        "burst_tokens": 2_000}
    state["token_window"] = initial_token_window(spent_tokens=0, floor=2_000,
        ceiling=30_000, rate_per_minute=60_000, burst_tokens=2_000)
    events = []

    async def emit(event):
        events.append(event)

    async def runner(node, incoming, policy, prompt):
        if node.node_id == "work":
            before = {"review_complete": 0, "review_recorded": 0,
                      "submitted_items": [], "write_receipts": []}
            return ExpertRoleResult(text="saved", billable_tokens=2_100,
                work_progress={"before": before, "after": {**before,
                    "write_receipts": ["durable-r1"]}})
        return ExpertRoleResult(text="checked", billable_tokens=100,
                                loop_decision=report(policy.loop_context))

    started = time.monotonic()
    saved = {"improve": state}
    _, result = await run_bounded_loop(contract=contract, flow=flow, saved=saved,
        run_node=runner, emit=emit,
        policy_for=lambda _: ExpertExecutionPolicy("stable", False, 0), incoming=[])
    assert result.status == "completed"
    assert time.monotonic() - started >= 0.07
    assert any(row.get("durable_checkpoint") and
               (row.get("loop_state") or {}).get("token_pace_pending")
               for row in events)
    assert "token_pace_pending" not in saved["improve"]


@pytest.mark.asyncio
async def test_finite_window_api_requires_owner_pause_and_exact_checkpoint(tmp_path, monkeypatch):
    from frontend import server
    runs, run_id, _, _, before = budget_case(tmp_path)
    monkeypatch.setattr(server, "EXPERT_RUNS", runs)
    session = SimpleNamespace(session=SimpleNamespace(session_id="c"), chat_busy=lambda _: False)
    monkeypatch.setattr(server, "SESSION", session)
    publish = AsyncMock()
    monkeypatch.setattr(server, "WS_EVENTS", SimpleNamespace(send_json=publish))
    body = {"conversation_id": "c", "expected_updated_at": before["updated_at"],
            "loop_id": "improve", "ceiling": 400_000, "command_id": "ui-finite"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),
                                 base_url="http://test") as client:
        url = f"/api/expert-runs/{run_id}/loop-token-policy"
        assert (await client.post(url, json={**body, "conversation_id": "other"})).status_code == 409
        assert (await client.post(url, json={**body, "expected_updated_at": "stale"})).status_code == 409
        result = await client.post(url, json=body)
        assert result.status_code == 200, result.text
        snapshot = result.json()["snapshot"]
        assert snapshot["status"] == "blocked"
        assert snapshot["loop_states"]["improve"]["loop_resource_policy"]["limit"] == 400_000
        assert publish.call_count == 1
        paced = await client.post(url, json={**body,
            "expected_updated_at": snapshot["updated_at"], "command_id": "ui-paced",
            "ceiling": 450_000, "rate_per_minute": 12_000,
            "burst_tokens": 20_000})
        assert paced.status_code == 200, paced.text
        bucket = paced.json()["snapshot"]["loop_states"]["improve"]["token_window"]["token_bucket"]
        assert bucket["rate_per_minute"] == 12_000 and bucket["burst_tokens"] == 20_000
