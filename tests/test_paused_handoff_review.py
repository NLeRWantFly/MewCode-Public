from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from mewcode.orchestration.paused_handoff_index import PausedHandoffIndex
from mewcode.orchestration.paused_handoff_review import (
    ReviewLedger, classify_with_receipts, preflight,
)
from mewcode.orchestration.paused_handoff_semantic import review_core_with_receipts
from mewcode.permissions.review_outcome import ReviewContextTooLarge


def _index(tmp_path: Path, messages: list[tuple[str, str]]) -> PausedHandoffIndex:
    raw = b"".join(
        (json.dumps({"type": kind, "content": content}, ensure_ascii=False) + "\n").encode()
        for kind, content in messages
    )
    log = tmp_path / "chat.jsonl"
    log.write_bytes(raw)
    return PausedHandoffIndex.build(
        log_path=log, root=tmp_path / "private", run_id="run-a",
        conversation_id="chat-a", contract_fingerprint="contract-v1", start=0,
    )


class Judge:
    model = "test-judge"

    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    async def evaluate(self, _state, _questions):
        self.calls += 1
        if self.fail:
            raise RuntimeError("service unavailable")
        return {"answers": {
            "intent": {"choice": "supporting", "confidence": 0.99},
            "contract_change": {"noul": 0.0},
            "state_change": {"noul": 0.0},
            "cross_fragment": {"noul": 0.0},
        }, "usage": {"input_tokens": 7, "output_tokens": 3}}


@pytest.mark.asyncio
async def test_receipts_survive_reload_without_repeating_completed_judgment(tmp_path: Path) -> None:
    index = _index(tmp_path, [("assistant", "Plain observation"), ("user", "Keep the original scope")])
    judge = Judge()
    ledger = ReviewLedger(index)
    estimate = preflight(index, remaining_tokens=0, remaining_seconds=None,
                         task_contract_bytes=len("Original"))
    assert estimate["shortage"] == "insufficient_budget"
    assert estimate["estimated_omission_audit_tokens_upper"] > 0
    assert estimate["estimated_seconds"] is None
    assert estimate["estimated_cost"] is None
    assert estimate["cost_estimate_basis"] == "model_tariff_unavailable"
    result = await classify_with_receipts(index, ledger, judge, task_contract="Original", remaining_tokens=5000)
    assert result["status"] == "classified"
    assert judge.calls == 1
    reloaded = ReviewLedger(index)
    assert reloaded.progress()["classified"] == 2
    assert reloaded.progress()["usage"] == {"input_tokens": 7, "output_tokens": 3}
    result = await classify_with_receipts(index, reloaded, judge, task_contract="Original", remaining_tokens=5000)
    assert result["status"] == "classified"
    assert judge.calls == 1
    first = index.manifest["records"][0]["fragments"][0]
    reloaded.mark_reviewed(first, decision="retain", evidence_refs=[first["id"]], model="test-judge")
    assert ReviewLedger(index).progress()["reviewed"] == 1


@pytest.mark.asyncio
async def test_started_receipt_is_outcome_unknown_after_interruption(tmp_path: Path) -> None:
    index = _index(tmp_path, [("assistant", "Plain observation")])
    ledger = ReviewLedger(index)
    fragment = index.manifest["records"][0]["fragments"][0]
    ledger.append(fragment, "started", model="test-judge", reserved_tokens=100)
    result = await classify_with_receipts(index, ReviewLedger(index), Judge(),
                                          task_contract="Original", remaining_tokens=5000)
    assert result["status"] == "outcome_unknown"


@pytest.mark.asyncio
async def test_missing_approved_budget_cannot_start_model_call(tmp_path: Path) -> None:
    index = _index(tmp_path, [("assistant", "Plain observation")])
    judge = Judge()
    ledger = ReviewLedger(index)
    assert preflight(index, remaining_tokens=None, remaining_seconds=None)["shortage"] == "budget_unknown"
    result = await classify_with_receipts(index, ledger, judge,
                                          task_contract="Original", remaining_tokens=None)
    assert result["status"] == "budget_unknown"
    assert judge.calls == 0
    assert ledger.progress()["classified"] == 0


@pytest.mark.asyncio
async def test_classification_context_limit_is_detected_before_start_receipt(tmp_path: Path) -> None:
    index = _index(tmp_path, [("assistant", "Plain observation")])
    ledger = ReviewLedger(index)

    class NarrowJudge(Judge):
        def preflight_evaluation(self, state, questions):
            raise ReviewContextTooLarge(
                state_bytes=31_000, largest_question_bytes=100,
                total_question_bytes=200, field_bytes={"excerpt": 30_000},
            )

        async def evaluate(self, state, questions):
            raise AssertionError("no remote classification call is allowed")

    result = await classify_with_receipts(
        index, ledger, NarrowJudge(), task_contract="Original",
        remaining_tokens=100,
    )
    assert result["status"] == "context_window_insufficient"
    assert not any(event["phase"] == "started" for event in ledger.events)


@pytest.mark.asyncio
async def test_no_route_does_not_freeze_speculative_text_classification(tmp_path: Path) -> None:
    index = _index(tmp_path, [("assistant", "Potentially relevant observation"),
                              ("user", "Keep original scope")])
    ledger = ReviewLedger(index)
    initial = await classify_with_receipts(index, ledger, None,
                                           task_contract="Original", remaining_tokens=None)
    assert initial["status"] == "semantic_route_unavailable"
    assert initial["progress"]["classified"] == 1
    judge = Judge()
    approved = await classify_with_receipts(index, ledger, judge,
                                            task_contract="Original", remaining_tokens=5000)
    assert approved["status"] == "classified"
    assert judge.calls == 1
    assert ledger.progress()["classified"] == 2


@pytest.mark.asyncio
async def test_review_started_cannot_be_silently_replayed_or_marked_done(tmp_path: Path) -> None:
    index = _index(tmp_path, [("user", "Keep original scope")])
    ledger = ReviewLedger(index)
    await classify_with_receipts(index, ledger, None, task_contract="Original", remaining_tokens=0)
    fragment = index.manifest["records"][0]["fragments"][0]
    ledger.append(fragment, "review_started", model="reviewer")
    reloaded = ReviewLedger(index)
    assert reloaded.progress()["outcome_unknown"] == [fragment["id"]]
    with pytest.raises(ValueError, match="outcome is unknown"):
        reloaded.mark_reviewed(fragment, decision="retain",
                               evidence_refs=[fragment["id"]], model="reviewer")


@pytest.mark.asyncio
async def test_service_failure_is_recorded_but_not_reviewed(tmp_path: Path) -> None:
    index = _index(tmp_path, [("assistant", "Plain observation")])
    ledger = ReviewLedger(index)
    result = await classify_with_receipts(index, ledger, Judge(fail=True),
                                          task_contract="Original", remaining_tokens=5000)
    assert result["status"] == "outcome_unknown"
    assert ledger.progress()["classified"] == 0
    assert ledger.progress()["reviewed"] == 0
    assert ledger.progress()["outcome_unknown"] == [
        index.manifest["records"][0]["fragments"][0]["id"]
    ]


@pytest.mark.asyncio
async def test_missing_usage_stops_before_next_paid_fragment(tmp_path: Path) -> None:
    class MissingUsage(Judge):
        async def evaluate(self, state, questions):
            result = await super().evaluate(state, questions)
            result.pop("usage")
            return result

    index = _index(tmp_path, [("assistant", "First observation"),
                              ("assistant", "Second observation")])
    ledger = ReviewLedger(index)
    judge = MissingUsage()
    result = await classify_with_receipts(index, ledger, judge,
                                          task_contract="Original", remaining_tokens=10000)
    assert result["status"] == "outcome_unknown"
    assert judge.calls == 1
    assert ledger.progress()["classified"] == 0
    assert (await classify_with_receipts(index, ReviewLedger(index), judge,
                                         task_contract="Original",
                                         remaining_tokens=10000))["status"] == "outcome_unknown"
    assert judge.calls == 1


@pytest.mark.asyncio
async def test_source_and_receipt_tamper_fail_closed(tmp_path: Path) -> None:
    index = _index(tmp_path, [("user", "original")])
    ledger = ReviewLedger(index)
    await classify_with_receipts(index, ledger, None, task_contract="Original", remaining_tokens=100)
    payload = ledger.path.read_bytes()
    ledger.path.write_bytes(payload.replace(b'"classified"', b'"reviewed"'))
    with pytest.raises(ValueError, match="chain is corrupt"):
        ReviewLedger(index)
    ledger.path.write_bytes(payload)
    Path(index.manifest["source"]["log_path"]).write_bytes(b"changed\n")
    with pytest.raises(ValueError, match="original handoff source"):
        await classify_with_receipts(index, ReviewLedger(index), None,
                                     task_contract="Original", remaining_tokens=100)


@pytest.mark.asyncio
async def test_concurrent_review_instances_cannot_duplicate_paid_judgment(tmp_path: Path) -> None:
    class SlowJudge(Judge):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def evaluate(self, state, questions):
            self.entered.set()
            await self.release.wait()
            return await super().evaluate(state, questions)

    index = _index(tmp_path, [("assistant", "Plain observation")])
    first, stale = ReviewLedger(index), ReviewLedger(index)
    judge = SlowJudge()
    running = asyncio.create_task(classify_with_receipts(
        index, first, judge, task_contract="Original", remaining_tokens=5000,
    ))
    await judge.entered.wait()
    with pytest.raises(ValueError, match="advanced concurrently"):
        await classify_with_receipts(index, stale, judge,
                                     task_contract="Original", remaining_tokens=5000)
    judge.release.set()
    assert (await running)["status"] == "classified"
    assert judge.calls == 1
    assert ReviewLedger(index).progress()["classified"] == 1


@pytest.mark.asyncio
async def test_preflight_exposes_source_bound_hard_hold_breakdown(tmp_path: Path) -> None:
    log = tmp_path / "chat.jsonl"
    log.write_bytes(b"".join((json.dumps(item) + "\n").encode() for item in [
        {"type": "assistant", "content": [{"type": "tool_use", "id": "q1",
           "name": "ReviewWorkQueue", "input": {"action": "read"}}]},
        {"type": "tool_result", "tool_use_id": "q1", "content": "queue"},
    ]))
    index = PausedHandoffIndex.build(
        log_path=log, root=tmp_path / "private", run_id="run-a",
        conversation_id="chat-a", contract_fingerprint="contract-v1", start=0,
    )
    budget = preflight(index, remaining_tokens=None, remaining_seconds=None)
    assert budget["deterministic_hold_records"] == 2
    assert budget["deterministic_hold_reasons"] == {
        "write_error_or_unknown_result": 1,
        "write_review_or_unknown_call": 1,
    }
    assert budget["first_hold_ref"] == index.manifest["records"][0]["id"]
    ledger = ReviewLedger(index)
    await classify_with_receipts(index, ledger, None,
                                 task_contract="Original", remaining_tokens=0)
    assert (await review_core_with_receipts(index, ledger, None,
                                            task_contract="Original",
                                            remaining_tokens=0))["status"] == "review_hold"
    after_review = preflight(index, remaining_tokens=0,
                             remaining_seconds=None, ledger=ledger)
    assert after_review["deterministic_hold_records"] == 2


@pytest.mark.asyncio
async def test_large_record_reuses_one_semantic_judgment_for_all_fragments(tmp_path: Path) -> None:
    index = _index(tmp_path, [("assistant", "One observation. " * 200)])
    # Re-index with a deliberately small fragment size to exercise record-wide
    # judgment without repeatedly billing the same visible text.
    log = tmp_path / "chat.jsonl"
    index = PausedHandoffIndex.build(
        log_path=log, root=tmp_path / "private-small", run_id="run-a",
        conversation_id="chat-a", contract_fingerprint="contract-v1",
        start=0, fragment_bytes=100,
    )
    judge = Judge()
    ledger = ReviewLedger(index)
    result = await classify_with_receipts(index, ledger, judge,
                                          task_contract="Original", remaining_tokens=10000)
    assert result["status"] == "classified"
    assert judge.calls == 1
    assert ledger.progress()["classified"] == len(index.manifest["records"][0]["fragments"])
