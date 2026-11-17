from __future__ import annotations

import json
from pathlib import Path

import pytest

from mewcode.orchestration.paused_handoff_index import PausedHandoffIndex


def _line(kind: str, content, **extra) -> bytes:
    return (json.dumps({"type": kind, "content": content,
                        "timestamp": "2026-09-23T00:00:00+00:00", **extra},
                       ensure_ascii=False) + "\n").encode("utf-8")


def test_large_single_record_is_fully_indexed_and_utf8_safe(tmp_path: Path) -> None:
    log = tmp_path / "chat.jsonl"
    huge = "路径与验收。" * 120_000
    raw = _line("user", huge)
    assert len(raw) > 2_000_000
    log.write_bytes(_line("assistant", "before") + raw + _line("tool_result", "after"))
    index = PausedHandoffIndex.build(
        log_path=log, root=tmp_path / "private", run_id="run-a",
        conversation_id="chat-a", contract_fingerprint="contract-v1",
        start=0, fragment_bytes=4096,
    )

    report = index.verify()
    assert report["bytes"] == log.stat().st_size
    assert report["records"] == 3
    assert report["fragments"] > 500
    middle = index.manifest["records"][1]
    assert index.read(middle["id"]) == raw
    assert b"".join(index.read(part["id"]) for part in middle["fragments"]) == raw
    assert all(index.read(part["id"]).decode("utf-8") for part in middle["fragments"])
    assert all(part["next_id"] == middle["fragments"][i + 1]["id"]
               for i, part in enumerate(middle["fragments"][:-1]))


def test_oversized_record_is_indexed_but_not_semantically_assumed(tmp_path: Path) -> None:
    log = tmp_path / "chat.jsonl"
    raw = _line("user", "中" * 2_700_000)
    assert len(raw) > 8_000_000
    log.write_bytes(raw)
    index = PausedHandoffIndex.build(
        log_path=log, root=tmp_path / "private", run_id="run-a",
        conversation_id="chat-a", contract_fingerprint="contract-v1", start=0,
    )
    record = index.manifest["records"][0]
    assert record["parse_error"] == "record_metadata_too_large"
    assert sum(part["end"] - part["start"] for part in record["fragments"]) == len(raw)
    assert index.read(record["id"]) == raw
    assert index.verify()["complete"]


def test_append_is_new_generation_and_original_change_fails_closed(tmp_path: Path) -> None:
    log = tmp_path / "chat.jsonl"
    first = _line("user", "保持原验收")
    log.write_bytes(first)
    kwargs = dict(log_path=log, root=tmp_path / "private", run_id="run-a",
                  conversation_id="chat-a", contract_fingerprint="contract-v1", start=0)
    initial = PausedHandoffIndex.build(**kwargs)
    with log.open("ab") as writer:
        writer.write(_line("user", "新增要求"))
    assert initial.verify()["bytes"] == len(first)
    next_generation = PausedHandoffIndex.build(**kwargs)
    assert next_generation.manifest_path != initial.manifest_path
    assert next_generation.verify()["records"] == 2

    changed = bytearray(log.read_bytes())
    changed[15] = ord("X")
    log.write_bytes(changed)
    with pytest.raises(ValueError, match="digest mismatch"):
        initial.verify()
    assert initial.read(initial.manifest["records"][0]["id"]) == first


def test_path_ids_are_private_and_do_not_merge_different_targets(tmp_path: Path) -> None:
    log = tmp_path / "chat.jsonl"
    raw = _line("assistant", [
        {"type": "tool_use", "id": "a", "name": "WriteFile",
         "input": {"file_path": r"C:\work\one\result.txt", "content": "A"}},
        {"type": "tool_use", "id": "b", "name": "WriteFile",
         "input": {"file_path": r"C:\work\two\result.txt", "content": "B"}},
    ])
    log.write_bytes(raw)
    index = PausedHandoffIndex.build(
        log_path=log, root=tmp_path / "private", run_id="run-a",
        conversation_id="chat-a", contract_fingerprint="contract-v1", start=0,
    )
    record = index.manifest["records"][0]
    assert {call["call_id"] for call in record["calls"]} == {"a", "b"}
    assert len(record["path_ids"]) == 2
    assert len(index.manifest["path_map"]) == 2
    assert index.verify()["complete"]


def test_known_read_only_calls_do_not_create_write_hold(tmp_path: Path) -> None:
    log = tmp_path / "chat.jsonl"
    read_tools = ("ReadReviewQueue", "WebFetch", "WebSearch", "ToolSearch",
                  "InspectGitState", "TaskList")
    log.write_bytes(_line("assistant", [
        {"type": "tool_use", "id": f"read-{index}", "name": name, "input": {}}
        for index, name in enumerate(read_tools)
    ]))
    index = PausedHandoffIndex.build(
        log_path=log, root=tmp_path / "private", run_id="run-a",
        conversation_id="chat-a", contract_fingerprint="contract-v1", start=0,
    )
    calls = index.manifest["records"][0]["calls"]
    assert [call["tool"] for call in calls] == list(read_tools)
    assert all(call["may_mutate"] is False for call in calls)
    assert index.verify()["complete"]


def test_missing_middle_fragment_is_not_accepted(tmp_path: Path) -> None:
    log = tmp_path / "chat.jsonl"
    log.write_bytes(_line("user", "中间变更" * 2000))
    index = PausedHandoffIndex.build(
        log_path=log, root=tmp_path / "private", run_id="run-a",
        conversation_id="chat-a", contract_fingerprint="contract-v1",
        start=0, fragment_bytes=100,
    )
    index.manifest["records"][0]["fragments"].pop(1)
    with pytest.raises(ValueError, match="integrity|coverage gap"):
        index.verify()


def test_exact_prior_call_in_same_session_can_link_a_later_result(tmp_path: Path) -> None:
    log = tmp_path / "chat.jsonl"
    prior = b"".join([
        _line("assistant", [{"type": "tool_use", "id": "unrelated", "name": "WriteFile",
                             "input": {"file_path": "private-unused.txt"}}]),
        _line("assistant", [{"type": "tool_use", "id": "read-1", "name": "ReadFile",
                             "input": {"file_path": "wanted.txt", "record_revision": "r1"}}]),
    ])
    log.write_bytes(prior + _line("tool_result", "read output", tool_use_id="read-1",
                                exit_code="0"))
    index = PausedHandoffIndex.build(
        log_path=log, root=tmp_path / "private", run_id="run-a",
        conversation_id="chat-a", contract_fingerprint="contract-v1",
        start=len(prior),
    )
    result = index.manifest["records"][0]
    assert result["tool"] == "ReadFile"
    assert result["call_provenance"] == "matched_prior_session_record"
    assert len(index.manifest["prior_call_refs"]) == 1
    assert len(index.manifest["path_map"]) == 1
    assert "wanted.txt" in str(index.manifest["path_map"])
    assert "private-unused.txt" not in str(index.manifest["path_map"])
    path_id = result["path_ids"][0]
    assert index.find_records(offset=result["start"], call_id="read-1",
                              path_id=path_id, input_revision="r1",
                              result_code="0")[0]["record_id"] == result["id"]
    assert index.verify()["complete"]
    tampered = log.read_bytes().replace(b"ReadFile", b"BadDFile", 1)
    log.write_bytes(tampered)
    with pytest.raises(ValueError, match="prior handoff call provenance changed"):
        index.verify()


def test_duplicate_prior_call_ids_remain_ambiguous(tmp_path: Path) -> None:
    log = tmp_path / "chat.jsonl"
    prior = b"".join(_line("assistant", [{"type": "tool_use", "id": "same",
                                                   "name": "ReadFile", "input": {}}])
                     for _ in range(2))
    log.write_bytes(prior + _line("tool_result", "result", tool_use_id="same"))
    index = PausedHandoffIndex.build(
        log_path=log, root=tmp_path / "private", run_id="run-a",
        conversation_id="chat-a", contract_fingerprint="contract-v1",
        start=len(prior),
    )
    assert index.manifest["records"][0]["call_provenance"] == "ambiguous_prior"
    assert index.manifest["unmatched_tool_results"] == 1


def test_duplicate_calls_inside_frozen_range_do_not_silently_choose_one(tmp_path: Path) -> None:
    log = tmp_path / "chat.jsonl"
    log.write_bytes(b"".join([
        _line("assistant", [{"type": "tool_use", "id": "same", "name": "ReadFile",
                             "input": {"file_path": "a.txt"}}]),
        _line("assistant", [{"type": "tool_use", "id": "same", "name": "WriteFile",
                             "input": {"file_path": "b.txt"}}]),
        _line("tool_result", "ambiguous", tool_use_id="same"),
    ]))
    index = PausedHandoffIndex.build(
        log_path=log, root=tmp_path / "private", run_id="run-a",
        conversation_id="chat-a", contract_fingerprint="contract-v1", start=0,
    )
    result = index.manifest["records"][2]
    assert result["call_provenance"] == "ambiguous_within_frozen_range"
    assert not result.get("tool")


def test_manifest_metadata_tamper_fails_even_if_source_bytes_unchanged(tmp_path: Path) -> None:
    log = tmp_path / "chat.jsonl"
    log.write_bytes(_line("assistant", [{"type": "tool_use", "id": "call-1",
                                         "name": "WriteFile", "input": {"file_path": "x.txt"}}]))
    index = PausedHandoffIndex.build(
        log_path=log, root=tmp_path / "private", run_id="run-a",
        conversation_id="chat-a", contract_fingerprint="contract-v1", start=0,
    )
    manifest = json.loads(index.manifest_path.read_text(encoding="utf-8"))
    manifest["records"][0]["calls"][0]["tool"] = "ReadFile"
    index.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="integrity"):
        PausedHandoffIndex.load(index.manifest_path)
    index.manifest["records"][0]["calls"][0]["tool"] = "ReadFile"
    with pytest.raises(ValueError, match="integrity"):
        index.verify()
