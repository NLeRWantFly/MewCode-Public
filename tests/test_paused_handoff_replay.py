from __future__ import annotations

import json
from pathlib import Path

import pytest

from mewcode.orchestration.paused_handoff_replay import replay_paused_handoff


@pytest.mark.asyncio
async def test_replay_keeps_hidden_middle_change_and_unique_failure(tmp_path: Path) -> None:
    records = [
        {"type": "assistant", "content": [{"type": "tool_use", "id": "r1",
            "name": "ReadFile", "input": {"file_path": "x.txt"}}]},
        {"type": "tool_result", "tool_use_id": "r1", "content": "same", "is_error": False},
        {"type": "user", "content": "中间新增验收条件：必须独立复核"},
        {"type": "assistant", "content": [{"type": "tool_use", "id": "r2",
            "name": "ReadFile", "input": {"file_path": "x.txt"}}]},
        {"type": "tool_result", "tool_use_id": "r2", "content": "failed", "is_error": True},
        {"type": "assistant", "content": [{"type": "tool_use", "id": "r3",
            "name": "ReadFile", "input": {"file_path": "x.txt"}}]},
        {"type": "tool_result", "tool_use_id": "r3", "content": "same", "is_error": False},
    ]
    raw = b"".join((json.dumps(row, ensure_ascii=False) + "\n").encode()
                   for row in records)
    log = tmp_path / "chat.jsonl"
    log.write_bytes(raw)
    report = await replay_paused_handoff(
        log_path=log, run_id="run-a", conversation_id="chat-a",
        contract_fingerprint="contract-v1", start=0, end=len(raw),
        critical_record_ordinals=[2, 4], fragment_bytes=32,
    )
    assert report["mechanical_coverage_complete"]
    assert report["critical_events_checked"] == 2
    assert report["critical_events_missed"] == 0
    assert report["critical_recall"] == 1.0
    assert report["levels"]["core"] > 0
    assert report["potential_view_bytes_avoided"] >= 0
    assert report["hard_hold_tool_actions"] == {"ReadFile:-": 1}
    assert report["real_resume_attempted"] is False
    assert report["counterfactual_savings"] is None
    assert report["actual_token_savings"] is None
    assert report["actual_time_savings"] is None
    assert report["actual_cost_savings"] is None
    assert not list(tmp_path.glob("**/*.source"))


@pytest.mark.asyncio
async def test_replay_rejects_invalid_label_and_does_not_change_source(tmp_path: Path) -> None:
    raw = b'{"type":"user","content":"hello"}\n'
    log = tmp_path / "chat.jsonl"
    log.write_bytes(raw)
    with pytest.raises(ValueError, match="outside frozen source"):
        await replay_paused_handoff(log_path=log, run_id="run-a",
                                    conversation_id="chat-a",
                                    contract_fingerprint="contract-v1",
                                    start=0, end=len(raw),
                                    critical_record_ordinals=[5])
    assert log.read_bytes() == raw
