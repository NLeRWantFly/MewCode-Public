from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from mewcode.pipeline_events import (
    PipelineEventStore,
    PipelineRecorder,
    compact_pipeline_log,
    fold_pipeline_items,
    legacy_entries_to_pipeline,
    load_pipeline_items,
)


def _project(events: list[dict]) -> dict[str, dict]:
    """Mirror frontend/static/app.js `pipelineItemFromEvent`."""

    items: dict[str, dict] = {}
    last_seq = 0
    for event in events:
        if event.get("type") != "pipeline_item":
            continue
        seq = 0 if event.get("synthetic") else int(event.get("seq") or 0)
        if seq and seq <= last_seq:
            continue
        if seq:
            last_seq = seq
        item_id = str(event["item_id"])
        item = items.setdefault(item_id, {
            "item_id": item_id,
            "text": "",
            "meta": {},
            "phase": event.get("phase"),
            "status": event.get("status"),
            "started_at": event.get("started_at"),
            "completed_at": None,
            "duration_ms": None,
            "seq": event.get("seq"),
        })
        if event.get("phase"):
            item["phase"] = event["phase"]
        if event.get("status"):
            item["status"] = event["status"]
        if event.get("started_at") and not item["started_at"]:
            item["started_at"] = event["started_at"]
        if event.get("completed_at"):
            item["completed_at"] = event["completed_at"]
        if event.get("duration_ms") is not None:
            item["duration_ms"] = event["duration_ms"]
        if event.get("seq") is not None:
            item["seq"] = event["seq"]
        if event.get("text"):
            item["text"] += str(event["text"])
        if event.get("meta"):
            item["meta"] = {**item["meta"], **event["meta"]}
    return items


def test_pipeline_recorder_persists_order_and_duration(tmp_path) -> None:
    session = SimpleNamespace(session_id="session-a", _sessions_dir=tmp_path)
    received: list[dict] = []

    async def publish(event: dict) -> None:
        received.append(event)

    async def run() -> None:
        recorder = PipelineRecorder(
            conversation_id="session-a",
            store=PipelineEventStore.for_session(session),
            publish=publish,
        )
        await recorder.begin_turn("inspect repo")
        await recorder.assistant_text("I will inspect it")
        await recorder.tool_start("tool-1", "ReadFile", {"path": "README.md"})
        await recorder.tool_result("tool-1", "contents", is_error=False, elapsed=0.25)
        await recorder.assistant_text("Done")
        await recorder.finish()

    asyncio.run(run())

    assert [event["role"] for event in received if event["phase"] == "start"] == [
        "assistant", "tool", "assistant",
    ]
    assert [event["seq"] for event in received] == list(range(1, len(received) + 1))
    tool_complete = next(
        event for event in received
        if event["item_id"] == "tool-1" and event["phase"] == "complete"
    )
    assert tool_complete["duration_ms"] == 250
    assert tool_complete["status"] == "completed"

    path = tmp_path / "session-a.pipeline.jsonl"
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == len(received)
    assert [line["seq"] for line in lines] == list(range(1, len(lines) + 1))


def test_pipeline_store_skips_corrupt_lines_and_continues_sequence(tmp_path) -> None:
    path = tmp_path / "broken.pipeline.jsonl"
    path.write_text(
        '{"type":"pipeline_item","seq":4,"role":"user"}\nnot-json\n',
        encoding="utf-8",
    )
    store = PipelineEventStore.from_path(path)
    event = store.append({"conversation_id": "x", "role": "assistant"})

    assert event["seq"] == 5
    assert len(store.events()) == 2


def test_legacy_entries_project_to_user_assistant_and_tool() -> None:
    events = legacy_entries_to_pipeline([
        {"role": "user", "text": "read it"},
        {"role": "assistant", "text": "done", "tools": [{
            "name": "ReadFile", "args": {"path": "a.py"}, "result": "ok",
        }]},
    ])

    assert [event["role"] for event in events if event["phase"] == "complete"] == [
        "user", "assistant", "tool",
    ]
    assert all(event["duration_ms"] == 0 for event in events if event["phase"] == "complete")


def test_streaming_deltas_are_coalesced_without_losing_text(tmp_path) -> None:
    """A long stream must not turn into one durable line per token.

    Regression: a single conversation once reached 1.02M lines / 475 MB because
    every delta was appended, which made the history view impossible to open.
    """

    session = SimpleNamespace(session_id="session-coalesced", _sessions_dir=tmp_path)
    published: list[dict] = []

    async def publish(event: dict) -> None:
        published.append(event)

    deltas = 4000

    async def run() -> None:
        recorder = PipelineRecorder(
            conversation_id="session-coalesced",
            store=PipelineEventStore.for_session(session),
            publish=publish,
            flush_chars=1024,
            flush_seconds=3600.0,
        )
        await recorder.begin_turn("stream a lot")
        for index in range(deltas):
            await recorder.assistant_thinking(f"d{index};")
        await recorder.finish()

    asyncio.run(run())

    path = tmp_path / "session-coalesced.pipeline.jsonl"
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == len(published)
    # Coalescing must be substantial, not a token-per-line log.
    assert len(lines) < deltas / 10, len(lines)

    live = _project(published)
    folded = _project(fold_pipeline_items(lines))
    assert live == folded

    thinking = next(item for item in live.values() if item["text"].startswith("d0;"))
    assert thinking["text"] == "".join(f"d{index};" for index in range(deltas))
    assert thinking["phase"] == "complete"


def test_fold_pipeline_items_reproduces_client_projection() -> None:
    events = [
        {"type": "pipeline_item", "seq": 1, "item_id": "a", "role": "assistant",
         "kind": "thinking", "phase": "start", "status": "running",
         "started_at": "t0", "text": "", "meta": {"step": 1}},
        {"type": "pipeline_item", "seq": 2, "item_id": "a", "role": "assistant",
         "kind": "thinking", "phase": "chunk", "status": "running",
         "started_at": "t0", "text": "hello ", "meta": {"step": 2}},
        {"type": "pipeline_item", "seq": 3, "item_id": "a", "role": "assistant",
         "kind": "thinking", "phase": "chunk", "status": "running",
         "started_at": "t0", "text": "world", "meta": {}},
        {"type": "pipeline_item", "seq": 4, "item_id": "a", "role": "assistant",
         "kind": "thinking", "phase": "complete", "status": "completed",
         "started_at": "t0", "completed_at": "t9", "duration_ms": 900,
         "text": "!", "meta": {}},
    ]

    folded = fold_pipeline_items(events)

    assert len(folded) == 1
    assert folded[0]["text"] == "hello world!"
    assert folded[0]["phase"] == "complete"
    assert folded[0]["status"] == "completed"
    assert folded[0]["started_at"] == "t0"
    assert folded[0]["completed_at"] == "t9"
    assert folded[0]["duration_ms"] == 900
    assert folded[0]["seq"] == 4
    assert folded[0]["meta"] == {"step": 2}
    assert _project(folded) == _project(events)


def test_compact_pipeline_log_shrinks_and_keeps_projection(tmp_path) -> None:
    path = tmp_path / "session-big.pipeline.jsonl"
    records: list[dict] = []
    seq = 0
    for index in range(3):
        seq += 1
        records.append({"type": "pipeline_item", "seq": seq, "item_id": f"item-{index}",
                        "role": "assistant", "kind": "thinking", "phase": "start",
                        "status": "running", "started_at": "t0", "text": ""})
        for chunk in range(50):
            seq += 1
            records.append({"type": "pipeline_item", "seq": seq, "item_id": f"item-{index}",
                            "role": "assistant", "kind": "thinking", "phase": "chunk",
                            "status": "running", "started_at": "t0", "text": f"c{chunk}"})
        seq += 1
        records.append({"type": "pipeline_item", "seq": seq, "item_id": f"item-{index}",
                        "role": "assistant", "kind": "thinking", "phase": "complete",
                        "status": "completed", "started_at": "t0",
                        "completed_at": "t1", "duration_ms": 1, "text": ""})
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )

    stats = compact_pipeline_log(path, keep_backup=False)
    compacted = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    assert stats["compacted"] is True
    assert stats["before_lines"] == len(records)
    assert stats["after_items"] == 3
    assert len(compacted) == 3
    assert stats["after_bytes"] < stats["before_bytes"]
    assert _project(compacted) == _project(records)
    assert not (tmp_path / "session-big.pipeline.jsonl.compact-tmp").exists()


def test_load_pipeline_items_compacts_oversized_log(tmp_path) -> None:
    path = tmp_path / "session-huge.pipeline.jsonl"
    records: list[dict] = []
    for seq in range(1, 41):
        records.append({"type": "pipeline_item", "seq": seq, "item_id": "only",
                        "role": "assistant", "kind": "thinking", "phase": "chunk",
                        "status": "running", "started_at": "t0", "text": "x" * 64})
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )

    items = load_pipeline_items(tmp_path, "session-huge", compact_bytes=1)

    assert len(items) == 1
    assert items[0]["text"] == "x" * 64 * 40
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1
    assert (tmp_path / "session-huge.pipeline.jsonl.bak").exists()


def test_load_pipeline_items_ignores_missing_log(tmp_path) -> None:
    assert load_pipeline_items(tmp_path, "nope") == []
