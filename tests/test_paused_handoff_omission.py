from __future__ import annotations

import json
from pathlib import Path

import pytest

from mewcode.orchestration.paused_handoff_gate import audit_semantic_risk
from mewcode.orchestration.paused_handoff_index import PausedHandoffIndex
from mewcode.orchestration.paused_handoff_omission import audit_low_attention_with_receipts
from mewcode.orchestration.paused_handoff_review import ReviewLedger, classify_with_receipts
from mewcode.orchestration.paused_handoff_semantic import review_core_with_receipts


class Judge:
    model = "typed-audit"

    def __init__(self, *, omission="safe", fail=False) -> None:
        self.omission, self.fail = omission, fail
        self.calls: list[str] = []

    async def evaluate(self, _state, questions):
        if "intent" in questions:
            self.calls.append("classify")
            return {"answers": {"intent": {"choice": "routine", "confidence": 0.99},
                                "contract_change": {"noul": 0.0},
                                "state_change": {"noul": 0.0},
                                "cross_fragment": {"noul": 0.0}},
                    "usage": {"input_tokens": 4, "output_tokens": 2}}
        if "checkpoint_impact" in questions:
            self.calls.append("core")
            return {"answers": {"checkpoint_impact": {"choice": "unchanged", "confidence": 0.99},
                                "cross_record": {"noul": 0.0},
                                "unknown_side_effect": {"noul": 0.0},
                                "signoff_or_review": {"noul": 0.0}},
                    "usage": {"input_tokens": 4, "output_tokens": 2}}
        self.calls.append("omission")
        if self.fail:
            raise RuntimeError("remote outcome unknown")
        return {"answers": {"omission": {"choice": self.omission, "confidence": 0.99},
                            "contract_risk": {"noul": 0.0},
                            "state_risk": {"noul": 0.0},
                            "context_risk": {"noul": 0.0}},
                "usage": {"input_tokens": 5, "output_tokens": 2}}


def _index(tmp_path: Path) -> PausedHandoffIndex:
    tmp_path.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "chat.jsonl"
    log.write_bytes(b"".join((json.dumps(item) + "\n").encode() for item in [
        {"type": "user", "content": "Keep the original target."},
        {"type": "assistant", "content": "Routine navigation only."},
    ]))
    return PausedHandoffIndex.build(
        log_path=log, root=tmp_path / "private", run_id="run-a",
        conversation_id="chat-a", contract_fingerprint="contract-v1", start=0,
    )


@pytest.mark.asyncio
async def test_low_attention_needs_independent_source_bound_audit(tmp_path: Path) -> None:
    index = _index(tmp_path)
    ledger = ReviewLedger(index)
    judge = Judge()
    await classify_with_receipts(index, ledger, judge, task_contract="Original",
                                 remaining_tokens=10000)
    await review_core_with_receipts(index, ledger, judge, task_contract="Original",
                                    remaining_tokens=10000)
    assert not audit_semantic_risk(index, ledger)["passed"]
    result = await audit_low_attention_with_receipts(index, ledger, judge,
                                                     task_contract="Original",
                                                     remaining_tokens=10000)
    assert result["status"] == "audited"
    assert judge.calls == ["classify", "core", "omission"]
    assert audit_semantic_risk(index, ledger)["passed"]
    restarted = ReviewLedger(index)
    again = await audit_low_attention_with_receipts(index, restarted, judge,
                                                    task_contract="Original",
                                                    remaining_tokens=10000)
    assert again["status"] == "audited"
    assert judge.calls == ["classify", "core", "omission"]


@pytest.mark.asyncio
async def test_uncertain_omission_and_remote_unknown_fail_closed(tmp_path: Path) -> None:
    index = _index(tmp_path)
    ledger = ReviewLedger(index)
    judge = Judge(omission="retain")
    await classify_with_receipts(index, ledger, judge, task_contract="Original",
                                 remaining_tokens=10000)
    result = await audit_low_attention_with_receipts(index, ledger, judge,
                                                     task_contract="Original",
                                                     remaining_tokens=10000)
    assert result["status"] == "omission_hold"
    assert not audit_semantic_risk(index, ledger)["passed"]

    second = _index(tmp_path / "unknown")
    second_ledger = ReviewLedger(second)
    failed = Judge(fail=True)
    await classify_with_receipts(second, second_ledger, failed,
                                 task_contract="Original", remaining_tokens=10000)
    outcome = await audit_low_attention_with_receipts(second, second_ledger, failed,
                                                      task_contract="Original",
                                                      remaining_tokens=10000)
    assert outcome["status"] == "outcome_unknown"
    assert (await audit_low_attention_with_receipts(
        second, ReviewLedger(second), Judge(), task_contract="Original",
        remaining_tokens=10000,
    ))["status"] == "outcome_unknown"


@pytest.mark.asyncio
async def test_append_generation_reuses_completed_omission_without_rebilling(tmp_path: Path) -> None:
    index = _index(tmp_path)
    ledger = ReviewLedger(index)
    judge = Judge()
    await classify_with_receipts(index, ledger, judge, task_contract="Original",
                                 remaining_tokens=10000)
    await audit_low_attention_with_receipts(index, ledger, judge,
                                            task_contract="Original", remaining_tokens=10000)
    spent = ledger.progress()["usage"]
    assert judge.calls == ["classify", "omission"]
    log = tmp_path / "chat.jsonl"
    with log.open("ab") as handle:
        handle.write(b'{"type":"user","content":"New message"}\n')
    next_index = PausedHandoffIndex.build(
        log_path=log, root=tmp_path / "private", run_id="run-a",
        conversation_id="chat-a", contract_fingerprint="contract-v1", start=0,
        generation=2, parent_source_id=index.manifest["id"],
    )
    next_ledger = ReviewLedger(next_index)
    assert next_ledger.import_verified_prefix(index, ledger) > 0
    await classify_with_receipts(next_index, next_ledger, judge,
                                 task_contract="Original", remaining_tokens=10000)
    result = await audit_low_attention_with_receipts(
        next_index, next_ledger, judge, task_contract="Original", remaining_tokens=10000,
    )
    assert result["status"] == "audited"
    assert judge.calls == ["classify", "omission"]
    assert next_ledger.progress()["usage"] == {"input_tokens": 0, "output_tokens": 0}
    assert spent == {"input_tokens": 9, "output_tokens": 4}
