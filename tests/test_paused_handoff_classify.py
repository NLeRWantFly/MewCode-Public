from __future__ import annotations

import json
from pathlib import Path

import pytest

from mewcode.orchestration.paused_handoff_classify import (
    classify_fragment, classify_index, hard_review_hold,
)
from mewcode.orchestration.paused_handoff_index import PausedHandoffIndex


def _record(kind: str, content, **extra) -> bytes:
    return (json.dumps({"type": kind, "content": content,
                        "timestamp": "2026-09-23T00:00:00+00:00", **extra},
                       ensure_ascii=False) + "\n").encode("utf-8")


def _index(tmp_path: Path, raw: bytes) -> PausedHandoffIndex:
    tmp_path.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "chat.jsonl"
    log.write_bytes(raw)
    return PausedHandoffIndex.build(
        log_path=log, root=tmp_path / "private", run_id="run-a",
        conversation_id="chat-a", contract_fingerprint="contract-v1", start=0,
    )


class FakeJudge:
    model = "jev-test"

    def __init__(self, choice="unrelated", confidence=0.99, **risks):
        self.choice, self.confidence, self.risks = choice, confidence, risks
        self.calls = []

    async def evaluate(self, state, questions):
        self.calls.append((state, questions))
        return {"answers": {
            "intent": {"choice": self.choice, "confidence": self.confidence},
            **{name: {"noul": self.risks.get(name, 0.0)}
               for name in ("contract_change", "state_change", "cross_fragment")},
        }}


@pytest.mark.asyncio
async def test_host_core_and_error_cannot_be_downgraded(tmp_path: Path) -> None:
    raw = b"".join([
        _record("user", "验收必须增加独立复核"),
        _record("assistant", [{"type": "tool_use", "id": "w1", "name": "WriteFile",
                               "input": {"file_path": "out.txt", "content": "x"}}]),
        _record("tool_result", "failed", tool_use_id="w1", is_error=True),
    ])
    index = _index(tmp_path, raw)
    judge = FakeJudge()
    rows = await classify_index(index, judge, task_contract="原验收标准")
    assert [row["level"] for row in rows] == ["core", "core", "core"]
    assert judge.calls == []


@pytest.mark.asyncio
async def test_read_result_is_supporting_not_assumed_routine(tmp_path: Path) -> None:
    raw = b"".join([
        _record("assistant", [{"type": "tool_use", "id": "r1", "name": "ReadFile",
                               "input": {"file_path": "source.txt"}}]),
        _record("tool_result", "unique important fact", tool_use_id="r1", is_error=False),
    ])
    index = _index(tmp_path, raw)
    rows = await classify_index(index, FakeJudge())
    assert [row["level"] for row in rows] == ["supporting", "supporting"]


@pytest.mark.asyncio
async def test_stateful_read_named_tool_is_core_even_for_read_action(tmp_path: Path) -> None:
    index = _index(tmp_path, b"".join([
        _record("assistant", [{"type": "tool_use", "id": "q1",
                               "name": "ReviewWorkQueue", "input": {"action": "read"}}]),
        _record("tool_result", "review queue", tool_use_id="q1"),
        _record("assistant", [{"type": "tool_use", "id": "b1",
                               "name": "BackgroundTasks", "input": {"acknowledge": True}}]),
        _record("tool_result", "task acknowledged", tool_use_id="b1"),
    ]))
    assert [row.get("may_mutate") for row in index.manifest["records"]] == [
        None, True, None, True,
    ]
    assert [row["level"] for row in await classify_index(index, FakeJudge())] == [
        "core", "core", "core", "core",
    ]


@pytest.mark.asyncio
async def test_unacknowledged_background_task_lookup_remains_supporting(tmp_path: Path) -> None:
    index = _index(tmp_path, b"".join([
        _record("assistant", [{"type": "tool_use", "id": "b1",
                               "name": "BackgroundTasks", "input": {"task_id": "t1"}}]),
        _record("tool_result", "task status", tool_use_id="b1"),
    ]))
    assert [row["level"] for row in await classify_index(index, FakeJudge())] == [
        "supporting", "supporting",
    ]


@pytest.mark.asyncio
async def test_capability_probe_and_skill_discovery_are_read_only_but_skill_activation_is_not(tmp_path: Path) -> None:
    index = _index(tmp_path, b"".join([
        _record("assistant", [{"type": "tool_use", "id": "p1",
                               "name": "ProbeCapabilities", "input": {"target": "inventory"}}]),
        _record("tool_result", "inventory", tool_use_id="p1"),
        _record("assistant", [{"type": "tool_use", "id": "f1",
                               "name": "FindSkills", "input": {"query": "review"}}]),
        _record("tool_result", "skills", tool_use_id="f1"),
        _record("assistant", [{"type": "tool_use", "id": "s1",
                               "name": "LoadSkill", "input": {"name": "review"}}]),
        _record("tool_result", "skill activated", tool_use_id="s1"),
    ]))
    records = index.manifest["records"]
    assert [record.get("may_mutate") for record in records] == [
        None, False, None, False, None, True,
    ]
    assert [row["level"] for row in await classify_index(index, FakeJudge())] == [
        "supporting", "supporting", "supporting", "supporting", "core", "core",
    ]
    assert [hard_review_hold(record) for record in records] == [
        "", "", "", "", "write_review_or_unknown_call", "write_error_or_unknown_result",
    ]


@pytest.mark.asyncio
async def test_assistant_text_with_read_call_is_core_but_not_unreconciled_write(tmp_path: Path) -> None:
    index = _index(tmp_path, _record("assistant", [
        {"type": "text", "text": "Read showed a possible issue."},
        {"type": "tool_use", "id": "r1", "name": "ReadFile",
         "input": {"file_path": "source.txt"}},
    ]))
    record = index.manifest["records"][0]
    assert (await classify_index(index))[0]["level"] == "core"
    assert hard_review_hold(record) == ""


@pytest.mark.asyncio
async def test_nonzero_result_and_unknown_record_type_cannot_be_low_attention(tmp_path: Path) -> None:
    index = _index(tmp_path, b"".join([
        _record("assistant", [{"type": "tool_use", "id": "r1", "name": "ReadFile",
                               "input": {"file_path": "source.txt"}}]),
        _record("tool_result", "failure despite missing flag", tool_use_id="r1",
                exit_code=2),
        _record("developer", "Do not lower the acceptance gate"),
        _record("unrecognized_format", "potentially consequential"),
    ]))
    rows = await classify_index(index, FakeJudge())
    assert [row["level"] for row in rows] == ["supporting", "core", "core", "uncertain"]


@pytest.mark.asyncio
async def test_typed_semantics_cannot_treat_weak_or_risky_text_as_unrelated(tmp_path: Path) -> None:
    index = _index(tmp_path, _record("assistant", "A related observation."))
    record = index.manifest["records"][0]
    fragment = record["fragments"][0]
    weak = await classify_fragment(index, record, fragment,
                                   FakeJudge(confidence=0.4), task_contract="original")
    changed = await classify_fragment(index, record, fragment,
                                      FakeJudge(contract_change=0.3), task_contract="original")
    dependent = await classify_fragment(index, record, fragment,
                                        FakeJudge(cross_fragment=0.4), task_contract="original")
    assert weak["level"] == "uncertain"
    assert changed["level"] == "core"
    assert "contract_change" in changed["risk_flags"]
    assert dependent["level"] == "uncertain"


@pytest.mark.asyncio
async def test_secret_or_no_judge_stays_uncertain(tmp_path: Path) -> None:
    index = _index(tmp_path, _record("assistant", "Authorization: Bearer abcdefghij"))
    judge = FakeJudge()
    row = (await classify_index(index, judge))[0]
    assert row["level"] == "uncertain"
    assert judge.calls == []
    safe = _index(tmp_path / "safe", _record("assistant", "Plain observation"))
    assert (await classify_index(safe))[0]["confidence_source"] == "model_unavailable"


@pytest.mark.asyncio
async def test_semantic_route_never_receives_private_thinking(tmp_path: Path) -> None:
    raw = _record("assistant", [
        {"type": "thinking", "thinking": "private reasoning", "signature": "hidden-signature"},
        {"type": "text", "text": "Visible status only."},
    ])
    index = _index(tmp_path, raw)
    judge = FakeJudge(choice="supporting")
    row = (await classify_index(index, judge))[0]
    assert row["level"] == "supporting"
    assert len(judge.calls) == 1
    state = judge.calls[0][0]
    assert state["excerpt"] == "Visible status only."
    assert "private reasoning" not in str(state)
