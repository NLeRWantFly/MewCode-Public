from __future__ import annotations

import json
import copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mewcode.orchestration.paused_handoff_final import (
    FinalDecisionJournal, decide_final_handoff,
)
from mewcode.orchestration.paused_handoff_index import PausedHandoffIndex
from mewcode.orchestration.paused_handoff_budget import authorize_handoff_review
from mewcode.orchestration.paused_handoff_review import ReviewLedger, classify_with_receipts
from mewcode.orchestration.paused_handoff_semantic import review_core_with_receipts
from mewcode.orchestration.takeover import (
    paused_handoff_boundaries, paused_handoff_contract_fingerprint,
)


class Judge:
    model = "jev-test"

    def __init__(self, *, final_choice="unrelated", fail_final=False,
                 on_final=None) -> None:
        self.final_choice, self.fail_final = final_choice, fail_final
        self.on_final = on_final
        self.final_calls = 0

    async def evaluate(self, state, questions):
        if "checkpoint_impact" in questions:
            return {"answers": {
                "checkpoint_impact": {"choice": "unchanged", "confidence": 0.99},
                "cross_record": {"noul": 0.0},
                "unknown_side_effect": {"noul": 0.0},
                "signoff_or_review": {"noul": 0.0},
            }, "usage": {"input_tokens": 9, "output_tokens": 2}}
        self.final_calls += 1
        if self.on_final is not None:
            self.on_final()
        if self.fail_final:
            raise RuntimeError("remote outcome unknown")
        assert "private reasoning" not in str(state)
        return {"answers": {
            "relation": {"choice": self.final_choice, "confidence": 0.99},
            "checkpoint_conflict": {"noul": 0.01},
        }, "usage": {"input_tokens": 20, "output_tokens": 5}}


async def _prepared(tmp_path: Path):
    log = tmp_path / "chat.jsonl"
    log.write_bytes((json.dumps({"type": "user", "content": [
        {"type": "thinking", "thinking": "private reasoning", "signature": "hidden"},
        {"type": "text", "text": "Keep the original work unchanged."},
    ]}) + "\n").encode())
    index = PausedHandoffIndex.build(
        log_path=log, root=tmp_path / "private", run_id="run-a",
        conversation_id="chat-a", contract_fingerprint="contract-v1", start=0,
    )
    ledger = ReviewLedger(index)
    judge = Judge()
    await classify_with_receipts(index, ledger, judge,
                                 task_contract="Original", remaining_tokens=10000)
    await review_core_with_receipts(index, ledger, judge,
                                    task_contract="Original", remaining_tokens=10000)
    return index, ledger


@pytest.mark.asyncio
async def test_final_typed_decision_is_tool_validated_and_idempotent(tmp_path: Path) -> None:
    index, ledger = await _prepared(tmp_path)
    journal = FinalDecisionJournal(index, job_id="a" * 32)
    judge = Judge()
    result = await decide_final_handoff(index, ledger, journal, judge,
                                        task_contract="Original", max_tokens=10000)
    assert result["status"] == "completed"
    assert result["decision"]["action"] == "resume"
    assert result["decision"]["relation"] == "unrelated"
    assert judge.final_calls == 1
    resumed = await decide_final_handoff(index, ledger,
                                         FinalDecisionJournal(index, job_id="a" * 32),
                                         judge, task_contract="Original", max_tokens=10000)
    assert resumed["status"] == "completed"
    assert judge.final_calls == 1


@pytest.mark.asyncio
async def test_final_uncertainty_holds_and_remote_failure_is_not_replayed(tmp_path: Path) -> None:
    index, ledger = await _prepared(tmp_path)
    held = await decide_final_handoff(
        index, ledger, FinalDecisionJournal(index, job_id="b" * 32),
        Judge(final_choice="needs_user"), task_contract="Original", max_tokens=10000,
    )
    assert held["decision"]["action"] == "hold"
    failed = await decide_final_handoff(
        index, ledger, FinalDecisionJournal(index, job_id="c" * 32),
        Judge(fail_final=True), task_contract="Original", max_tokens=10000,
    )
    assert failed["status"] == "outcome_unknown"
    retry_judge = Judge()
    retried = await decide_final_handoff(
        index, ledger, FinalDecisionJournal(index, job_id="c" * 32),
        retry_judge, task_contract="Original", max_tokens=10000,
    )
    assert retried["status"] == "outcome_unknown"
    assert retry_judge.final_calls == 0


@pytest.mark.asyncio
async def test_stale_final_journal_cannot_start_second_paid_decision(tmp_path: Path) -> None:
    index, _ledger = await _prepared(tmp_path)
    first = FinalDecisionJournal(index, job_id="d" * 32)
    stale = FinalDecisionJournal(index, job_id="d" * 32)
    first.append("started", model="jev-test", reserved_tokens=100)
    with pytest.raises(ValueError, match="advanced concurrently"):
        stale.append("started", model="jev-test", reserved_tokens=100)
    assert FinalDecisionJournal(index, job_id="d" * 32).phase == "started"


@pytest.mark.asyncio
@pytest.mark.parametrize("append_on_return,mutate_before_save,append_before_save", [
    (False, False, False), (True, False, False),
    (False, True, False), (False, False, True),
])
async def test_retired_segmented_runtime_uses_checkpoint_version_gate(tmp_path: Path,
                                                                      monkeypatch,
                                                                      append_on_return: bool,
                                                                      mutate_before_save: bool,
                                                                      append_before_save: bool) -> None:
    from frontend import server

    run_id = "a" * 32
    log = tmp_path / "chat.jsonl"
    log.write_bytes((json.dumps({"type": "user", "content": "Unrelated chat."}) + "\n").encode())
    snapshot = {"run_id": run_id, "conversation_id": "chat-a", "status": "paused",
                "task": "Original", "updated_at": "v1", "attempt": 1,
                "stage_cursor": "work", "locked_flow": {}, "locked_profile": {},
                "node_states": {"work": {"status": "paused"}},
                "pause_dialogue_anchor": {"record_offset": 0}}
    root = tmp_path / "run-store"
    index = PausedHandoffIndex.build(
        log_path=log, root=root / "handoff-reviews", run_id=run_id,
        conversation_id="chat-a",
        contract_fingerprint=paused_handoff_contract_fingerprint(snapshot),
        start=0, frozen_boundaries=paused_handoff_boundaries(snapshot, work_dir=tmp_path),
    )
    snapshot["pending_dialogue_handoff"] = {"action": "hold", "evidence_incomplete": True,
        "handoff_review_progress": {"source_id": index.manifest["id"]}}
    snapshot["handoff_review_grant"] = authorize_handoff_review(
        snapshot, source_id=index.manifest["id"],
        provider_key="typesafe-main", model_id="jev-test", max_tokens=10000,
    )

    class Store:
        def __init__(self):
            self.root = root
            self.value = copy.deepcopy(snapshot)

        def load(self, identity):
            return copy.deepcopy(self.value) if identity == run_id else None

        def mutate(self, identity, fn, **_kwargs):
            assert identity == run_id
            if mutate_before_save:
                self.value["updated_at"] = "v2"
            if append_before_save:
                with log.open("ab") as writer:
                    writer.write(b'{"type":"user","content":"new at commit"}\n')
            fn(self.value)
            return self.load(identity)

    with log.open("a", encoding="utf-8") as handle:
        session = SimpleNamespace(_file=handle, _sessions_dir=tmp_path)
        runtime = SimpleNamespace(session=session, conversation_id="chat-a",
                                  work_dir=tmp_path, status_payload=lambda: {},
                                  tasks_payload=lambda: [])
        store = Store()
        def on_event(event):
            if append_on_return and event.get("phase") == "returning":
                handle.write(json.dumps({"type": "user", "content": "New requirement"}) + "\n")
                handle.flush()
        def forbidden_resolve(_ref):
            pytest.fail("retired review must not resolve or call a provider")
        registry = SimpleNamespace(
            resolve=forbidden_resolve,
            records=lambda: [],
        )
        monkeypatch.setattr(server, "EXPERT_RUNS", store)
        monkeypatch.setattr(server, "MODEL_REGISTRY", registry)
        monkeypatch.setattr("mewcode.orchestration.takeover.read_paused_dialogue_window",
                            lambda *_: {"text": "truncated preview", "count": 1,
                                        "truncated": True, "start_offset": 0,
                                        "end_offset": log.stat().st_size})
        resume = AsyncMock()
        monkeypatch.setattr(server, "run_expert_chat", resume)
        ws = SimpleNamespace(send_json=AsyncMock(side_effect=on_event))
        await server._resume_expert_after_paused_dialogue(ws, runtime, snapshot)
    if mutate_before_save:
        resume.assert_not_awaited()
        assert store.value["pending_dialogue_handoff"]["action"] == "hold"
        assert "retired_paused_handoff_reviews" not in store.value
    else:
        resume.assert_awaited_once()
        assert store.value.get("pending_dialogue_handoff") is None
        assert store.value["retired_paused_handoff_reviews"][0]["status"] == "retired_not_approved"
        assert store.value["locked_profile"] == snapshot["locked_profile"]
        assert store.value["node_states"] == snapshot["node_states"]
