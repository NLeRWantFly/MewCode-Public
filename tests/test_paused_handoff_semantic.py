from __future__ import annotations

import json
from pathlib import Path

import pytest

from mewcode.orchestration.paused_handoff_index import PausedHandoffIndex
from mewcode.orchestration.paused_handoff_review import ReviewLedger, classify_with_receipts, preflight
from mewcode.orchestration.paused_handoff_semantic import (
    review_core_with_receipts, summarize_semantic_holds,
)
from mewcode.permissions.review_outcome import ReviewContextTooLarge


def _index(tmp_path: Path, values: list[dict], *, fragment_bytes: int = 8192) -> PausedHandoffIndex:
    tmp_path.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "chat.jsonl"
    log.write_bytes(b"".join((json.dumps(value, ensure_ascii=False) + "\n").encode()
                             for value in values))
    return PausedHandoffIndex.build(
        log_path=log, root=tmp_path / "private", run_id="run-a",
        conversation_id="chat-a", contract_fingerprint="contract-v1", start=0,
        fragment_bytes=fragment_bytes,
    )


class Judge:
    model = "typed-review"

    def __init__(self, *, choice="unchanged", fail=False, cross=0.0) -> None:
        self.choice, self.fail, self.cross = choice, fail, cross
        self.calls = []

    async def evaluate(self, state, questions):
        self.calls.append((state, questions))
        if self.fail:
            raise RuntimeError("remote call ended without a result")
        return {"answers": {
            "checkpoint_impact": {"choice": self.choice, "confidence": 0.98},
            "cross_record": {"noul": self.cross},
            "unknown_side_effect": {"noul": 0.0},
            "signoff_or_review": {"noul": 0.0},
        }, "usage": {"input_tokens": 11, "output_tokens": 4}}


@pytest.mark.asyncio
async def test_core_user_review_is_bounded_and_resume_still_separate(tmp_path: Path) -> None:
    index = _index(tmp_path, [{"type": "user", "content": "Keep the original target."}],
                   fragment_bytes=12)
    ledger = ReviewLedger(index)
    await classify_with_receipts(index, ledger, None, task_contract="Original", remaining_tokens=0)
    judge = Judge()
    assert (await review_core_with_receipts(index, ledger, judge,
                                            task_contract="Original", remaining_tokens=None))["status"] == "budget_unknown"
    assert judge.calls == []
    result = await review_core_with_receipts(index, ledger, judge,
                                             task_contract="Original", remaining_tokens=10000)
    assert result["status"] == "reviewed"
    assert len(judge.calls) == 1
    assert ledger.progress()["reviewed"] == len(index.manifest["records"][0]["fragments"])
    assert ledger.progress()["usage"] == {"input_tokens": 11, "output_tokens": 4}
    assert (await review_core_with_receipts(index, ReviewLedger(index), judge,
                                             task_contract="Original", remaining_tokens=10000))["status"] == "reviewed"
    assert len(judge.calls) == 1


@pytest.mark.asyncio
async def test_unknown_tool_result_is_hard_hold_without_model(tmp_path: Path) -> None:
    index = _index(tmp_path, [{"type": "tool_result", "tool_use_id": "orphan",
                               "content": "Looks okay", "is_error": False}])
    ledger = ReviewLedger(index)
    await classify_with_receipts(index, ledger, None, task_contract="Original", remaining_tokens=0)
    judge = Judge()
    result = await review_core_with_receipts(index, ledger, judge,
                                             task_contract="Original", remaining_tokens=10000)
    assert result["status"] == "review_hold"
    assert result["reason"] == "write_error_or_unknown_result"
    assert judge.calls == []


@pytest.mark.asyncio
async def test_hard_hold_does_not_hide_later_deterministic_records(tmp_path: Path) -> None:
    index = _index(tmp_path, [
        {"type": "tool_result", "tool_use_id": "orphan-1", "content": "unknown"},
        {"type": "tool_result", "tool_use_id": "orphan-2", "content": "unknown"},
    ])
    ledger = ReviewLedger(index)
    await classify_with_receipts(index, ledger, None, task_contract="Original", remaining_tokens=0)
    judge = Judge()
    result = await review_core_with_receipts(index, ledger, judge,
                                             task_contract="Original", remaining_tokens=0)
    assert result["status"] == "review_hold"
    assert len(result["holds"]) == 2
    assert result["progress"]["reviewed"] == 2
    assert judge.calls == []


@pytest.mark.asyncio
async def test_cross_record_or_failed_remote_review_stays_held(tmp_path: Path) -> None:
    index = _index(tmp_path, [{"type": "user", "content": "As above, continue."}])
    ledger = ReviewLedger(index)
    await classify_with_receipts(index, ledger, None, task_contract="Original", remaining_tokens=0)
    dependent = await review_core_with_receipts(index, ledger, Judge(cross=0.3),
                                                task_contract="Original", remaining_tokens=10000)
    assert dependent["status"] == "review_hold"
    assert ledger.progress()["reviewed"] == 1

    second = _index(tmp_path / "failure", [{"type": "user", "content": "Continue."}])
    receipt = ReviewLedger(second)
    await classify_with_receipts(second, receipt, None, task_contract="Original", remaining_tokens=0)
    result = await review_core_with_receipts(second, receipt, Judge(fail=True),
                                             task_contract="Original", remaining_tokens=10000)
    assert result["status"] == "outcome_unknown"
    assert (await review_core_with_receipts(second, ReviewLedger(second), Judge(),
                                            task_contract="Original", remaining_tokens=10000))["status"] == "outcome_unknown"


@pytest.mark.asyncio
async def test_real_first_call_probes_usage_before_admitting_next_call(tmp_path: Path) -> None:
    index = _index(tmp_path, [
        {"type": "user", "content": "First unchanged note."},
        {"type": "user", "content": "Second unchanged note."},
    ])
    ledger = ReviewLedger(index)
    await classify_with_receipts(index, ledger, None,
                                 task_contract="Original", remaining_tokens=0)

    class HighUsageJudge(Judge):
        async def evaluate(self, state, questions):
            self.calls.append((state, questions))
            return {"answers": {
                "checkpoint_impact": {"choice": "unchanged", "confidence": 0.98},
                "cross_record": {"noul": 0.0},
                "unknown_side_effect": {"noul": 0.0},
                "signoff_or_review": {"noul": 0.0},
            }, "usage": {"input_tokens": 9_900, "output_tokens": 100}}

    judge = HighUsageJudge()
    result = await review_core_with_receipts(index, ledger, judge,
                                             task_contract="Original",
                                             remaining_tokens=13_000)
    assert result["status"] == "insufficient_budget"
    assert len(judge.calls) == 1
    first = index.manifest["records"][0]["fragments"][0]
    start = next(event for event in ledger.events
                 if event["phase"] == "review_started" and event["fragment_id"] == first["id"])
    assert start["reserved_tokens"] == start["base_reserved_tokens"]
    assert ledger.progress()["reviewed"] == 1


@pytest.mark.asyncio
async def test_probe_overrun_is_reported_and_stops_dispatch(tmp_path: Path) -> None:
    index = _index(tmp_path, [{"type": "user", "content": "First unchanged note."}])
    ledger = ReviewLedger(index)
    await classify_with_receipts(index, ledger, None,
                                 task_contract="Original", remaining_tokens=0)

    class HighUsageJudge(Judge):
        async def evaluate(self, state, questions):
            self.calls.append((state, questions))
            return {"answers": {
                "checkpoint_impact": {"choice": "unchanged", "confidence": 0.98},
                "cross_record": {"noul": 0.0},
                "unknown_side_effect": {"noul": 0.0},
                "signoff_or_review": {"noul": 0.0},
            }, "usage": {"input_tokens": 9_900, "output_tokens": 100}}

    judge = HighUsageJudge()
    result = await review_core_with_receipts(index, ledger, judge,
                                             task_contract="Original",
                                             remaining_tokens=3_000)
    assert result["status"] == "actual_budget_overrun"
    assert result["spent_tokens"] == 10_000
    assert len(judge.calls) == 1


@pytest.mark.asyncio
async def test_ambiguous_judgment_reports_reasoning_escalation_not_generic_hold(tmp_path: Path) -> None:
    index = _index(tmp_path, [{"type": "user", "content": "This may refer to the earlier change."}])
    ledger = ReviewLedger(index)
    await classify_with_receipts(index, ledger, None,
                                 task_contract="Original", remaining_tokens=0)
    result = await review_core_with_receipts(index, ledger, Judge(choice="ambiguous"),
                                             task_contract="Original", remaining_tokens=10_000)
    assert result["status"] == "review_hold"
    assert result["reason"] == "semantic_uncertain"
    assert result["holds"][0]["reason"] == "semantic_uncertain"
    repeated = await review_core_with_receipts(
        index, ReviewLedger(index), Judge(), task_contract="Original",
        remaining_tokens=10_000,
    )
    assert repeated["reason"] == "semantic_uncertain"
    assert repeated["hold_reasons"] == {"semantic_uncertain": 1}


@pytest.mark.asyncio
async def test_secondary_route_can_recheck_one_uncertain_hold_without_erasing_history(
    tmp_path: Path,
) -> None:
    index = _index(tmp_path, [{"type": "user", "content": "Continue the same task."}],
                   fragment_bytes=12)
    ledger = ReviewLedger(index)
    await classify_with_receipts(index, ledger, None,
                                 task_contract="Original", remaining_tokens=0)

    class LowConfidence(Judge):
        model = "jev-latest"

        async def evaluate(self, state, questions):
            result = await super().evaluate(state, questions)
            result["answers"]["checkpoint_impact"]["confidence"] = 0.84
            return result

    first = LowConfidence()
    assert (await review_core_with_receipts(index, ledger, first,
                                            task_contract="Original",
                                            remaining_tokens=10000))["status"] == "review_hold"
    assert ledger.progress()["held"] == ledger.progress()["core_total"]
    assert ledger.progress()["retained"] == 0
    assert summarize_semantic_holds(index, ledger)["reasons"] == {
        "semantic_uncertain": 1,
    }
    estimate = preflight(index, remaining_tokens=20_000, remaining_seconds=None,
                         ledger=ledger, escalation_enabled=True)
    assert estimate["estimated_escalation_tokens_upper"] > 0

    class Secondary(Judge):
        model = "secondary-typed-route"

    second = Secondary()
    result = await review_core_with_receipts(
        index, ledger, first, escalation_judge=second,
        task_contract="Original", remaining_tokens=20000,
    )
    assert result["status"] == "reviewed"
    assert len(second.calls) == 1
    assert ledger.progress()["retained"] == ledger.progress()["core_total"]
    assert ledger.progress()["held"] == 0
    assert summarize_semantic_holds(index, ledger)["reasons"] == {}
    reloaded = ReviewLedger(index)
    assert reloaded.progress()["retained"] == reloaded.progress()["core_total"]
    assert (await review_core_with_receipts(index, reloaded, first,
                                             escalation_judge=second,
                                             task_contract="Original",
                                             remaining_tokens=20000))["status"] == "reviewed"
    assert len(second.calls) == 1
    assert any(event.get("escalation_from") for event in reloaded.events)


@pytest.mark.asyncio
async def test_hard_hold_never_upgrades_with_a_secondary_model(tmp_path: Path) -> None:
    index = _index(tmp_path, [{"type": "tool_result", "tool_use_id": "orphan",
                               "content": "Unknown write result"}])
    ledger = ReviewLedger(index)
    await classify_with_receipts(index, ledger, None,
                                 task_contract="Original", remaining_tokens=0)
    backup = Judge()
    backup.model = "secondary-typed-route"
    result = await review_core_with_receipts(
        index, ledger, Judge(), escalation_judge=backup,
        task_contract="Original", remaining_tokens=10000,
    )
    assert result["status"] == "review_hold"
    assert backup.calls == []
    assert ledger.progress()["held"] == 1


@pytest.mark.asyncio
async def test_local_context_window_limit_is_not_an_unknown_paid_call(tmp_path: Path) -> None:
    index = _index(tmp_path, [{"type": "user", "content": "Needs more context."}])
    ledger = ReviewLedger(index)
    await classify_with_receipts(index, ledger, None,
                                 task_contract="Original", remaining_tokens=0)

    class NarrowJudge(Judge):
        def preflight_evaluation(self, state, questions):
            raise ReviewContextTooLarge(
                state_bytes=31_000, largest_question_bytes=100,
                total_question_bytes=200, field_bytes={"excerpt": 30_000},
            )

        async def evaluate(self, state, questions):
            raise AssertionError("local preflight must stop before a paid request")

    result = await review_core_with_receipts(index, ledger, NarrowJudge(),
                                             task_contract="Original", remaining_tokens=1000)
    assert result["status"] == "context_window_insufficient"
    assert result["context_sizes"]["state_bytes"] == 31_000
    assert not any(event["phase"] == "review_started" for event in ledger.events)
