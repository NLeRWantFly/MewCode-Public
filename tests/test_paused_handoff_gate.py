from __future__ import annotations

import json
from pathlib import Path

import pytest

from mewcode.orchestration.paused_handoff_gate import (
    SEMANTIC_AUDIT_VERSION,
    audit_semantic_risk, build_final_handoff_view, evaluate_handoff_gate,
)
from mewcode.orchestration.paused_handoff_compression import supporting_source_references
from mewcode.orchestration.paused_handoff_index import PausedHandoffIndex
from mewcode.orchestration.paused_handoff_review import ReviewLedger, classify_with_receipts
from mewcode.orchestration.paused_handoff_semantic import review_core_with_receipts
from mewcode.orchestration.takeover import paused_handoff_boundaries


class Judge:
    model = "typed-review"

    async def evaluate(self, _state, _questions):
        return {"answers": {
            "checkpoint_impact": {"choice": "unchanged", "confidence": 0.99},
            "cross_record": {"noul": 0.0},
            "unknown_side_effect": {"noul": 0.0},
            "signoff_or_review": {"noul": 0.0},
        }, "usage": {"input_tokens": 4, "output_tokens": 2}}


def _index(tmp_path: Path) -> PausedHandoffIndex:
    log = tmp_path / "chat.jsonl"
    log.write_bytes((json.dumps({"type": "user", "content": "原标准继续"},
                                ensure_ascii=False) + "\n").encode("utf-8"))
    return PausedHandoffIndex.build(
        log_path=log, root=tmp_path / "private", run_id="run-a",
        conversation_id="chat-a", contract_fingerprint="contract-v1", start=0,
    )


def _snapshot() -> dict:
    return {"run_id": "run-a", "conversation_id": "chat-a",
            "contract_fingerprint": "contract-v1", "plan_revision": "plan-1",
            "node_id": "node-1", "permission_revision": "permission-1",
            "ledger_fingerprint": "ledger-1", "unknown_side_effects": False,
            "independent_review_intact": True, "acceptance_intact": True,
            "user_signoff_required": False}


@pytest.mark.asyncio
async def test_gate_requires_core_review_and_current_boundaries(tmp_path: Path) -> None:
    index = _index(tmp_path)
    ledger = ReviewLedger(index)
    await classify_with_receipts(index, ledger, None, task_contract="Original", remaining_tokens=1)
    snapshot = _snapshot()
    audit = audit_semantic_risk(index, ledger)
    assert audit["passed"]
    pending = evaluate_handoff_gate(index, ledger, frozen_snapshot=snapshot,
                                    current_snapshot=snapshot, final_decision="resume",
                                    semantic_audit=audit)
    assert pending["decision"] == "hold"
    assert "core_review_incomplete" in pending["reasons"]
    await review_core_with_receipts(index, ledger, Judge(), task_contract="Original",
                                    remaining_tokens=5000)
    current = {**snapshot, "permission_revision": "permission-2"}
    changed = evaluate_handoff_gate(index, ledger, frozen_snapshot=snapshot,
                                    current_snapshot=current, final_decision="resume",
                                    semantic_audit=audit)
    assert changed["decision"] == "hold"
    assert "permission_revision_changed" in changed["reasons"]
    ready = evaluate_handoff_gate(index, ledger, frozen_snapshot=snapshot,
                                  current_snapshot=snapshot, final_decision="resume",
                                  semantic_audit=audit)
    assert ready["decision"] == "resume"
    view = build_final_handoff_view(index, ledger)
    assert view["status"] == "ready"
    assert "原标准继续" in view["core"][0]["visible_json"]


@pytest.mark.asyncio
async def test_gate_fails_on_append_citation_or_missing_semantic_audit(tmp_path: Path) -> None:
    index = _index(tmp_path)
    ledger = ReviewLedger(index)
    await classify_with_receipts(index, ledger, None, task_contract="Original", remaining_tokens=1)
    await review_core_with_receipts(index, ledger, Judge(), task_contract="Original",
                                    remaining_tokens=5000)
    snapshot = _snapshot()
    log = Path(index.manifest["source"]["log_path"])
    with log.open("ab") as writer:
        writer.write(b'{"type":"user","content":"new"}\n')
    result = evaluate_handoff_gate(index, ledger, frozen_snapshot=snapshot,
                                   current_snapshot=snapshot, final_decision="resume",
                                   citation_receipts=[{"verified": False, "verdict": "fabricated"}])
    assert result["decision"] == "hold"
    assert "new_dialogue_requires_incremental_generation" in result["reasons"]
    assert "citation_unverified" in result["reasons"]
    assert "semantic_risk_not_cleared" in result["reasons"]


@pytest.mark.asyncio
async def test_semantic_audit_separates_coverage_from_low_attention_risk(tmp_path: Path) -> None:
    index = _index(tmp_path)
    ledger = ReviewLedger(index)
    await classify_with_receipts(index, ledger, None, task_contract="Original", remaining_tokens=0)
    audit = audit_semantic_risk(index, ledger)
    assert audit["critical_events_checked"] == 1
    assert audit["critical_events_missed"] == 0
    assert audit["passed"]
    assert audit["semantic_completeness_proven"] is False


@pytest.mark.asyncio
async def test_gate_does_not_ignore_unknown_side_effect_or_signoff(tmp_path: Path) -> None:
    index = _index(tmp_path)
    ledger = ReviewLedger(index)
    await classify_with_receipts(index, ledger, None, task_contract="Original", remaining_tokens=1)
    await review_core_with_receipts(index, ledger, Judge(), task_contract="Original",
                                    remaining_tokens=5000)
    snapshot = _snapshot()
    current = {**snapshot, "unknown_side_effects": True,
               "user_signoff_required": True, "user_signoff_granted": False}
    result = evaluate_handoff_gate(index, ledger, frozen_snapshot=snapshot,
                                   current_snapshot=current, final_decision="resume",
                                   semantic_audit={"passed": True, "source_id": index.manifest["id"],
                                                   "policy_version": SEMANTIC_AUDIT_VERSION,
                                                   "critical_events_checked": 1,
                                                   "critical_events_missed": 0})
    assert result["decision"] == "hold"
    assert "unknown_side_effects" in result["reasons"]
    assert "user_signoff_required" in result["reasons"]


@pytest.mark.asyncio
async def test_forged_semantic_audit_cannot_clear_unclassified_source(tmp_path: Path) -> None:
    log = tmp_path / "chat.jsonl"
    log.write_bytes(b"".join((json.dumps(item, ensure_ascii=False) + "\n").encode("utf-8")
                             for item in [
                                 {"type": "user", "content": "原标准继续"},
                                 {"type": "assistant", "content": "尚未完成语义归类"},
                             ]))
    index = PausedHandoffIndex.build(
        log_path=log, root=tmp_path / "private", run_id="run-a",
        conversation_id="chat-a", contract_fingerprint="contract-v1", start=0,
    )
    ledger = ReviewLedger(index)
    await classify_with_receipts(index, ledger, None, task_contract="Original", remaining_tokens=1)
    await review_core_with_receipts(index, ledger, Judge(), task_contract="Original",
                                    remaining_tokens=5000)
    snapshot = _snapshot()
    forged = {"passed": True, "source_id": index.manifest["id"],
              "policy_version": SEMANTIC_AUDIT_VERSION,
              "critical_events_checked": 1, "critical_events_missed": 0,
              "unclassified_fragments": 0, "suspicious_low_attention_fragments": 0,
              "unverified_low_attention_fragments": 0}
    result = evaluate_handoff_gate(index, ledger, frozen_snapshot=snapshot,
                                   current_snapshot=snapshot, final_decision="resume",
                                   semantic_audit=forged)
    assert result["decision"] == "hold"
    assert "semantic_audit_stale_or_forged" in result["reasons"]
    assert "semantic_risk_not_cleared" in result["reasons"]


def test_stateful_queue_lookup_marks_handoff_effect_unknown(tmp_path: Path) -> None:
    log = tmp_path / "chat.jsonl"
    log.write_bytes((json.dumps({"type": "assistant", "content": [{
        "type": "tool_use", "id": "q1", "name": "ReviewWorkQueue",
        "input": {"action": "summary"},
    }]}) + "\n").encode("utf-8"))
    index = PausedHandoffIndex.build(
        log_path=log, root=tmp_path / "private", run_id="run-a",
        conversation_id="chat-a", contract_fingerprint="contract-v1", start=0,
    )
    boundaries = paused_handoff_boundaries({
        "run_id": "run-a", "conversation_id": "chat-a", "task": "Original",
        "locked_flow": {}, "node_states": {},
    }, index=index)
    assert boundaries["unknown_side_effects"] is True


@pytest.mark.asyncio
async def test_supporting_reference_is_lossless_navigation_not_business_claim(tmp_path: Path) -> None:
    log = tmp_path / "chat.jsonl"
    log.write_bytes(b"".join((json.dumps(item, ensure_ascii=False) + "\n").encode("utf-8") for item in [
        {"type": "user", "content": "按原标准继续"},
        {"type": "assistant", "content": [{"type": "tool_use", "id": "read-1",
          "name": "ReadFile", "input": {"file_path": "ledger.txt"}}]},
        {"type": "tool_result", "tool_use_id": "read-1", "content": "42"},
    ]))
    index = PausedHandoffIndex.build(
        log_path=log, root=tmp_path / "private", run_id="run-a",
        conversation_id="chat-a", contract_fingerprint="contract-v1", start=0,
    )
    ledger = ReviewLedger(index)
    await classify_with_receipts(index, ledger, None, task_contract="Original", remaining_tokens=1)
    await review_core_with_receipts(index, ledger, Judge(), task_contract="Original",
                                    remaining_tokens=5000)
    refs = supporting_source_references(index, ledger.classifications)
    assert len(refs) == 2
    assert all(item["kind"] == "source_reference_no_content_claim" for item in refs)
    snapshot = _snapshot()
    audit = audit_semantic_risk(index, ledger)
    missing = evaluate_handoff_gate(index, ledger, frozen_snapshot=snapshot,
                                    current_snapshot=snapshot, final_decision="resume",
                                    semantic_audit=audit)
    assert "supporting_evidence_unrepresented" in missing["reasons"]
    ready = evaluate_handoff_gate(index, ledger, frozen_snapshot=snapshot,
                                  current_snapshot=snapshot, final_decision="resume",
                                  semantic_audit=audit, supporting_references=refs)
    assert ready["decision"] == "resume"
    forged = [{**refs[0], "tool": "WriteFile"}, refs[1]]
    rejected = evaluate_handoff_gate(index, ledger, frozen_snapshot=snapshot,
                                     current_snapshot=snapshot, final_decision="resume",
                                     semantic_audit=audit, supporting_references=forged)
    assert "supporting_reference_invalid" in rejected["reasons"]
