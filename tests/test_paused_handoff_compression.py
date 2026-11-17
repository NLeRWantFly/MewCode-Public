from __future__ import annotations

import json
from pathlib import Path

import pytest

from mewcode.orchestration.paused_handoff_classify import classify_index
from mewcode.orchestration.paused_handoff_compression import (
    aggregate_repeated_reads, expand_visible_reference, verify_citation,
)
from mewcode.orchestration.paused_handoff_index import PausedHandoffIndex


def _line(kind: str, content, **extra) -> bytes:
    return (json.dumps({"type": kind, "content": content,
                        "timestamp": "2026-09-23T00:00:00+00:00", **extra},
                       ensure_ascii=False) + "\n").encode("utf-8")


def _index(tmp_path: Path, raw: bytes) -> PausedHandoffIndex:
    log = tmp_path / "chat.jsonl"
    log.write_bytes(raw)
    return PausedHandoffIndex.build(
        log_path=log, root=tmp_path / "private", run_id="run-a",
        conversation_id="chat-a", contract_fingerprint="contract-v1", start=0,
    )


@pytest.mark.asyncio
async def test_duplicate_reads_keep_every_ref_and_unique_failure(tmp_path: Path) -> None:
    lines = []
    for call_id, result, error in (("r1", "same", False), ("r2", "same", False),
                                   ("r3", "failed", True), ("r4", "different", False)):
        lines.append(_line("assistant", [{"type": "tool_use", "id": call_id,
                                         "name": "ReadFile", "input": {"file_path": "a.txt"}}]))
        lines.append(_line("tool_result", result, tool_use_id=call_id, is_error=error))
    index = _index(tmp_path, b"".join(lines))
    classified = await classify_index(index)
    groups = aggregate_repeated_reads(index, classified)
    result_groups = [group for group in groups if group["key"]["kind"] == "tool_result"]
    assert len(result_groups) == 1
    assert result_groups[0]["count"] == 2
    assert len(result_groups[0]["member_refs"]) == 2
    assert [index.read(ref["record_id"]) for ref in result_groups[0]["member_refs"]] == [lines[1], lines[3]]
    assert index.manifest["records"][5]["id"] not in {
        ref["record_id"] for group in groups for ref in group["member_refs"]
    }
    assert next(row for row in classified if row["record_id"] == index.manifest["records"][5]["id"])["level"] == "core"


class CitationJudge:
    model = "jev-test"

    def __init__(self, relation="supports", confidence=0.99):
        self.relation, self.confidence = relation, confidence
        self.calls = 0

    async def evaluate(self, state, questions):
        self.calls += 1
        assert "source_context" in state and "relation" in questions
        return {"answers": {"relation": {"choice": self.relation,
                                          "confidence": self.confidence}}}


@pytest.mark.asyncio
async def test_citation_requires_real_quote_and_semantic_support(tmp_path: Path) -> None:
    index = _index(tmp_path, _line("assistant", "验收仍要求独立复核。"))
    record_id = index.manifest["records"][0]["id"]
    judge = CitationJudge()
    missing = await verify_citation(index, record_id=record_id, quote="不存在的文字",
                                    claim="验收放宽了", judge=judge)
    assert missing["verdict"] == "fabricated" and judge.calls == 0
    good = await verify_citation(index, record_id=record_id, quote="仍要求独立复核",
                                 claim="独立复核仍然必需", judge=judge)
    assert good["verified"] and judge.calls == 1
    judge.relation = "contradicts"
    bad = await verify_citation(index, record_id=record_id, quote="仍要求独立复核",
                                claim="独立复核不再必需", judge=judge)
    assert bad["verdict"] == "contradicted" and not bad["verified"]
    judge.relation, judge.confidence = "supports", 0.4
    low = await verify_citation(index, record_id=record_id, quote="仍要求独立复核",
                                claim="独立复核仍然必需", judge=judge)
    assert not low["verified"]


def test_visible_expansion_is_bounded_and_never_exposes_thinking(tmp_path: Path) -> None:
    index = _index(tmp_path, _line("assistant", [
        {"type": "thinking", "thinking": "private chain", "signature": "hidden"},
        {"type": "text", "text": "Public evidence " * 20},
    ]))
    ref = index.manifest["records"][0]["id"]
    first = expand_visible_reference(index, ref, max_chars=30)
    chunks = [first["visible_text"]]
    cursor = first["next_view_offset"]
    while cursor is not None:
        page = expand_visible_reference(index, ref, view_offset=cursor, max_chars=30)
        chunks.append(page["visible_text"])
        cursor = page["next_view_offset"]
    joined = "".join(chunks)
    assert "Public evidence" in joined
    assert "private chain" not in joined
    assert "hidden" not in joined
    assert first["source_sha256"] == index.manifest["records"][0]["sha256"]
