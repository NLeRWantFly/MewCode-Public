from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mewcode.orchestration.paused_handoff_budget import (
    authorize_handoff_review, authorize_handoff_review_escalation,
    diagnose_handoff_review_blocker,
    suggest_handoff_review_budget,
)
from mewcode.orchestration.paused_handoff_index import PausedHandoffIndex
from mewcode.orchestration.paused_handoff_review import ReviewLedger, preflight
from mewcode.orchestration.takeover import (
    paused_handoff_boundaries, paused_handoff_contract_fingerprint,
    prepare_paused_handoff_review,
)


SOURCE_ID = "a" * 64


def _snapshot() -> dict:
    return {"status": "paused", "conversation_id": "chat-a",
            "pending_dialogue_handoff": {"handoff_review_progress": {
                "source_id": SOURCE_ID,
            }}}


def test_explicit_grant_is_source_and_route_bound_and_only_increases_total() -> None:
    snapshot = _snapshot()
    grant = authorize_handoff_review(
        snapshot, source_id=SOURCE_ID, provider_key="typesafe-main",
        model_id="jev", max_tokens=1000,
    )
    assert grant["basis"] == "explicit_user_review_token_cap"
    snapshot["handoff_review_grant"] = grant
    increased = authorize_handoff_review(
        snapshot, source_id=SOURCE_ID, provider_key="typesafe-main",
        model_id="jev", max_tokens=2000,
    )
    assert increased["grant_id"] == grant["grant_id"]
    assert increased["max_tokens"] == 2000
    with pytest.raises(ValueError, match="exceed"):
        authorize_handoff_review(snapshot, source_id=SOURCE_ID,
                                 provider_key="typesafe-main", model_id="jev",
                                 max_tokens=1000)
    with pytest.raises(ValueError, match="separate review"):
        authorize_handoff_review(snapshot, source_id=SOURCE_ID,
                                 provider_key="other", model_id="jev",
                                 max_tokens=2000)


def test_secondary_route_is_explicit_distinct_and_preserves_total_cap() -> None:
    snapshot = _snapshot()
    snapshot["handoff_review_grant"] = authorize_handoff_review(
        snapshot, source_id=SOURCE_ID, provider_key="typesafe-main",
        model_id="jev", max_tokens=1000,
    )
    valid_grant = snapshot["handoff_review_grant"]
    snapshot["handoff_review_grant"] = {**valid_grant, "max_tokens": True}
    with pytest.raises(ValueError):
        authorize_handoff_review_escalation(
            snapshot, source_id=SOURCE_ID,
            provider_key="typesafe-second", model_id="jev-preview",
        )
    snapshot["handoff_review_grant"] = valid_grant
    with pytest.raises(ValueError):
        authorize_handoff_review_escalation(
            snapshot, source_id=SOURCE_ID,
            provider_key="typesafe-main", model_id="jev",
        )
    upgraded = authorize_handoff_review_escalation(
        snapshot, source_id=SOURCE_ID,
        provider_key="typesafe-second", model_id="jev-preview",
    )
    assert upgraded["grant_id"] == snapshot["handoff_review_grant"]["grant_id"]
    assert upgraded["max_tokens"] == 1000
    assert upgraded["escalation_route"]["basis"] == "explicit_user_secondary_review_route"
    snapshot["handoff_review_grant"] = upgraded
    with pytest.raises(ValueError):
        authorize_handoff_review_escalation(
            snapshot, source_id=SOURCE_ID,
            provider_key="another", model_id="jev-next",
        )
    raised = authorize_handoff_review(
        snapshot, source_id=SOURCE_ID, provider_key="typesafe-main",
        model_id="jev", max_tokens=2000,
    )
    assert raised["escalation_route"] == upgraded["escalation_route"]


@pytest.mark.parametrize("cap", [0, -1, True, 2_000_001, 1.5])
def test_invalid_budget_cannot_be_granted(cap) -> None:
    with pytest.raises(ValueError):
        authorize_handoff_review(_snapshot(), source_id=SOURCE_ID,
                                 provider_key="typesafe-main", model_id="jev",
                                 max_tokens=cap)


def test_changed_source_cannot_spend_existing_grant() -> None:
    snapshot = _snapshot()
    snapshot["pending_dialogue_handoff"]["handoff_review_progress"]["source_id"] = "b" * 64
    with pytest.raises(ValueError, match="not current"):
        authorize_handoff_review(snapshot, source_id=SOURCE_ID,
                                 provider_key="typesafe-main", model_id="jev",
                                 max_tokens=1000)


def test_host_generates_finite_total_but_never_changes_grant() -> None:
    budget = {"estimated_semantic_tokens_upper": 214_641}
    suggestion = suggest_handoff_review_budget(
        budget, spent_tokens=0, unknown_reserved_tokens=0,
    )
    assert suggestion["suggested_total_tokens"] == 215_000
    assert suggestion["requires_explicit_authorization"] is True
    snapshot = _snapshot()
    assert "handoff_review_grant" not in snapshot

    corrected = suggest_handoff_review_budget(
        {"estimated_semantic_tokens_upper": 230_100},
        spent_tokens=18_300, unknown_reserved_tokens=1_200,
        approved_total_tokens=240_000,
    )
    assert corrected["suggested_total_tokens"] == 250_000
    assert corrected["additional_tokens_needed"] == 10_000
    assert corrected["approved_total_tokens"] == 240_000
    assert "handoff_review_grant" not in snapshot
    covered = suggest_handoff_review_budget(
        {"estimated_semantic_tokens_upper": 49_100},
        spent_tokens=200_000, unknown_reserved_tokens=0,
        approved_total_tokens=249_500,
    )
    assert covered["suggested_total_tokens"] == 250_000
    assert covered["additional_tokens_needed"] == 0
    assert covered["requires_explicit_authorization"] is False


def test_suggestion_above_supported_max_does_not_silently_clip() -> None:
    suggestion = suggest_handoff_review_budget(
        {"estimated_semantic_tokens_upper": 2_100_000},
        spent_tokens=1, unknown_reserved_tokens=0,
    )
    assert suggestion["suggested_total_tokens"] is None
    assert suggestion["exceeds_supported_max"] is True
    with pytest.raises(ValueError):
        suggest_handoff_review_budget(
            {"estimated_semantic_tokens_upper": 100},
            spent_tokens=-1, unknown_reserved_tokens=0,
        )


def test_blocker_triage_requests_capacity_or_reasoning_without_auto_grant() -> None:
    recommendation = suggest_handoff_review_budget(
        {"estimated_semantic_tokens_upper": 249_000},
        spent_tokens=1_000, unknown_reserved_tokens=0,
        approved_total_tokens=200_000,
    )
    capacity = diagnose_handoff_review_blocker(
        {"status": "insufficient_budget", "record_id": "record-a"},
        recommendation, has_semantic_route=True,
    )
    assert capacity["action"] == "request_explicit_total_cap"
    assert capacity["suggested_total_tokens"] == 250_000
    assert capacity["additional_tokens_needed"] == 50_000
    assert capacity["can_auto_increase_total"] is False
    assert capacity["can_auto_resume"] is False
    mixed = diagnose_handoff_review_blocker(
        {"status": "semantic_route_unavailable"}, recommendation,
        has_semantic_route=True, hard_hold_records=706,
    )
    assert mixed["action"] == "reconcile_hard_holds_before_paid_review"
    assert mixed["hard_hold_records"] == 706
    assert mixed["suggested_total_tokens"] == 250_000

    deeper = diagnose_handoff_review_blocker(
        {"status": "review_hold", "reason": "semantic_uncertain"},
        recommendation, has_semantic_route=True,
    )
    assert deeper["action"] == "request_declared_deeper_review"
    assert deeper["can_auto_resume"] is False
    unknown = diagnose_handoff_review_blocker(
        {"status": "outcome_unknown"}, recommendation,
        has_semantic_route=True,
    )
    assert unknown["action"] == "reconcile_before_more_calls"
    hard = diagnose_handoff_review_blocker(
        {"status": "review_hold", "reason": "write_error_or_unknown_result"},
        recommendation, has_semantic_route=True,
    )
    assert hard["action"] == "inspect_source_and_reconcile"
    context = diagnose_handoff_review_blocker(
        {"status": "context_window_insufficient", "context_sizes": {"state_bytes": 31_000}},
        recommendation, has_semantic_route=True,
    )
    assert context["action"] == "request_larger_context_or_lossless_split"
    assert context["can_auto_increase_total"] is False


def test_completed_probe_receipt_corrects_future_budget_upward(tmp_path: Path) -> None:
    log = tmp_path / "chat.jsonl"
    log.write_bytes(b'{"type":"user","content":"First"}\n'
                    b'{"type":"user","content":"Second"}\n')
    index = PausedHandoffIndex.build(
        log_path=log, root=tmp_path / "reviews", run_id="d" * 32,
        conversation_id="chat-a", contract_fingerprint="contract-v1", start=0,
    )
    first = index.manifest["records"][0]["fragments"][0]
    ledger = ReviewLedger(index)
    ledger.append(first, "classified", classification={"level": "core"}, usage={})
    attempt = ledger.start_review(first, model="jev-latest", reserved_tokens=1000)
    ledger.finish_review(first, attempt_id=attempt, decision="retain",
                         evidence_refs=[first["id"]], model="jev-latest",
                         judgment={}, usage={"input_tokens": 2800, "output_tokens": 200})
    estimate = preflight(index, remaining_tokens=5000,
                         remaining_seconds=None, ledger=ledger)
    assert estimate["observed_token_ratio_samples"] == 1
    assert estimate["observed_token_ratio"] == 3
    assert estimate["estimated_final_decision_tokens_upper"] == 105_000
    assert estimate["estimated_semantic_tokens_upper"] == (
        estimate["base_remaining_tokens_upper"] * 3 + 105_000
    )
    assert estimate["shortage"] == "insufficient_budget"


class ReviewJudge:
    model = "jev"

    def __init__(self) -> None:
        self.calls = 0

    async def evaluate(self, _state, _questions):
        self.calls += 1
        return {"answers": {
            "checkpoint_impact": {"choice": "unchanged", "confidence": 0.99},
            "cross_record": {"noul": 0.0},
            "unknown_side_effect": {"noul": 0.0},
            "signoff_or_review": {"noul": 0.0},
        }, "usage": {"input_tokens": 9, "output_tokens": 2}}


@pytest.mark.asyncio
async def test_approved_review_uses_separate_idempotent_job(tmp_path: Path) -> None:
    log = tmp_path / "chat.jsonl"
    log.write_bytes((json.dumps({"type": "user", "content": "Proceed unchanged."}) + "\n").encode())
    snapshot = {"run_id": "b" * 32, "conversation_id": "chat-a", "status": "paused",
                "task": "Original", "attempt": 1,
                "pause_dialogue_anchor": {"record_offset": 0}}
    root = tmp_path / "reviews"
    index = PausedHandoffIndex.build(
        log_path=log, root=root, run_id=snapshot["run_id"],
        conversation_id="chat-a",
        contract_fingerprint=paused_handoff_contract_fingerprint(snapshot),
        start=0, frozen_boundaries=paused_handoff_boundaries(snapshot),
    )
    snapshot["pending_dialogue_handoff"] = {"handoff_review_progress": {
        "source_id": index.manifest["id"],
    }}
    grant = authorize_handoff_review(
        snapshot, source_id=index.manifest["id"],
        provider_key="typesafe-main", model_id="jev", max_tokens=5000,
    )
    with log.open("a", encoding="utf-8") as handle:
        session = SimpleNamespace(_file=handle, _sessions_dir=tmp_path)
        judge = ReviewJudge()
        first = await prepare_paused_handoff_review(session, snapshot,
                                                    root=root, judge=judge, grant=grant)
        assert first["status"]["status"] == "reviewed"
        assert first["progress"]["reviewed"] == 1
        assert first["budget"]["estimated_core_review_tokens_upper"] == 0
        assert first["budget_recommendation"]["spent_tokens"] == 11
        assert first["budget_recommendation"]["requires_explicit_authorization"] is True
        assert judge.calls == 1
        second = await prepare_paused_handoff_review(session, snapshot,
                                                     root=root, judge=judge, grant=grant)
        assert second["status"]["status"] == "reviewed"
        assert judge.calls == 1


@pytest.mark.asyncio
async def test_known_hard_hold_stops_new_paid_review_even_with_large_grant(tmp_path: Path) -> None:
    log = tmp_path / "chat.jsonl"
    log.write_bytes(b'{"type":"tool_result","tool_use_id":"unknown","content":"write?"}\n'
                    b'{"type":"user","content":"Continue unchanged."}\n')
    snapshot = {"run_id": "e" * 32, "conversation_id": "chat-a", "status": "paused",
                "task": "Original", "pause_dialogue_anchor": {"record_offset": 0}}
    root = tmp_path / "reviews"
    index = PausedHandoffIndex.build(
        log_path=log, root=root, run_id=snapshot["run_id"],
        conversation_id="chat-a",
        contract_fingerprint=paused_handoff_contract_fingerprint(snapshot),
        start=0, frozen_boundaries=paused_handoff_boundaries(snapshot),
    )
    snapshot["pending_dialogue_handoff"] = {"handoff_review_progress": {
        "source_id": index.manifest["id"],
    }}
    grant = authorize_handoff_review(
        snapshot, source_id=index.manifest["id"], provider_key="typesafe-main",
        model_id="jev", max_tokens=2_000_000,
    )
    judge = ReviewJudge()
    with log.open("a", encoding="utf-8") as handle:
        session = SimpleNamespace(_file=handle, _sessions_dir=tmp_path)
        result = await prepare_paused_handoff_review(
            session, snapshot, root=root, judge=judge, grant=grant,
        )
    assert result["status"]["status"] == "hard_reconciliation_required"
    assert result["budget"]["deterministic_hold_records"] == 1
    assert result["progress"]["usage"] == {"input_tokens": 0, "output_tokens": 0}
    assert judge.calls == 0


@pytest.mark.asyncio
async def test_append_only_generation_reuses_reviewed_prefix_without_model_replay(tmp_path: Path) -> None:
    log = tmp_path / "chat.jsonl"
    first_line = (json.dumps({"type": "user", "content": "First unrelated note."}) + "\n").encode()
    second_line = (json.dumps({"type": "user", "content": "Second unrelated note."}) + "\n").encode()
    log.write_bytes(first_line)
    snapshot = {"run_id": "c" * 32, "conversation_id": "chat-a", "status": "paused",
                "task": "Original", "attempt": 1, "stage_cursor": "work",
                "pause_dialogue_anchor": {"record_offset": 0}}
    root = tmp_path / "reviews"
    index = PausedHandoffIndex.build(
        log_path=log, root=root, run_id=snapshot["run_id"],
        conversation_id="chat-a",
        contract_fingerprint=paused_handoff_contract_fingerprint(snapshot),
        start=0, frozen_boundaries=paused_handoff_boundaries(snapshot),
    )
    snapshot["pending_dialogue_handoff"] = {"handoff_review_progress": {
        "source_id": index.manifest["id"], "source_lineage": [index.manifest["id"]],
    }}
    grant = authorize_handoff_review(
        snapshot, source_id=index.manifest["id"],
        provider_key="typesafe-main", model_id="jev", max_tokens=10000,
    )
    judge = ReviewJudge()
    with log.open("a", encoding="utf-8") as handle:
        session = SimpleNamespace(_file=handle, _sessions_dir=tmp_path)
        first = await prepare_paused_handoff_review(session, snapshot,
                                                    root=root, judge=judge, grant=grant)
        assert first["status"]["status"] == "reviewed"
        assert judge.calls == 1
        handle.write(second_line.decode())
        handle.flush()
        second = await prepare_paused_handoff_review(session, snapshot,
                                                     root=root, judge=judge, grant=grant)
        assert second["status"]["status"] == "reviewed"
        assert second["index"].manifest["source"]["generation"] == 2
        assert second["source_lineage"] == [first["index"].manifest["id"],
                                            second["index"].manifest["id"]]
        assert second["progress"]["reviewed"] == 2
        assert second["prior_usage"] == {"input_tokens": 9, "output_tokens": 2}
        assert second["progress"]["usage"] == {"input_tokens": 18, "output_tokens": 4}
        assert second["budget_recommendation"]["spent_tokens"] == 22
        assert judge.calls == 2
        first_ref = second["index"].manifest["records"][0]["fragments"][0]["id"]
        assert second["ledger"].latest[first_ref]["reused_from_source_id"] == first["index"].manifest["id"]
