"""Large histories keep exact provenance and the existing bounded read budget."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mewcode.orchestration import draft_sources
from mewcode.orchestration.draft_store import DraftStore
from mewcode.orchestration.history_scan import history_lines
from tests.test_workflow_blocks import make_blocks, node_entry, record, user_sources, write_history
from tests.test_workflow_blocks_snapshots import binding


def noise(size=3000):
    return {"type": "tool_result", "tool_use_id": "ordinary", "content": "x" * size}


def test_segment_boundaries_preserve_sources_ids_questions_and_all_old_requirements(tmp_path, monkeypatch):
    path = tmp_path / "history.jsonl"
    question = {"type": "assistant", "content": [{"type": "tool_use", "name": "AskUserQuestion",
        "id": "q", "input": {"question": "范围？"}}]}
    answer = {"type": "tool_result", "tool_use_id": "q",
              "content": json.dumps({"status": "answered", "answer": "全部页"})}
    write_history(path, [record("user", "最早要求：保留原件。"), question, *[noise() for _ in range(5)],
        {"type": "compact_boundary", "content": {"summary": "不能替代早期要求"}}, answer, answer,
        record("assistant", [{"type": "thinking", "thinking": "private"}, {"type": "text", "text": "建议核验"}]),
        record("user", "最后要求：全部输出页码。")])
    original = path.read_bytes()
    expected, warnings, baseline = draft_sources.read_sources(path)
    monkeypatch.setattr(draft_sources, "MAX_HISTORY_BYTES", 4096)
    sources, actual_warnings, scan = draft_sources.read_sources(path)
    assert sources == expected and actual_warnings == warnings == []
    assert scan["snapshot"] == baseline["snapshot"]
    assert scan["history_digest"] == hashlib.sha256(original).hexdigest()
    assert scan["complete"] and scan["bytes_read"] == len(original)
    assert scan["segments"] >= 4 and scan["max_segment_bytes"] == 4096
    assert sources[0]["text"] == "最早要求：保留原件。"
    assert sources[-1]["text"] == "最后要求：全部输出页码。"
    assert len([row for row in sources if row["kind"] == "human_answer"]) == 1
    assert path.read_bytes() == original


def test_actual_history_over_100_mib_reads_completely_without_raising_limit(tmp_path):
    blocks = make_blocks(tmp_path / "project", rows=[])
    path = blocks.source_path
    limit = draft_sources.MAX_HISTORY_BYTES
    assert limit == 100 * 1024 * 1024
    first = (json.dumps(record("user", "原始硬要求"), ensure_ascii=False) + "\n").encode()
    last = (json.dumps(record("user", "最新限制"), ensure_ascii=False) + "\n").encode()
    padding = (json.dumps(noise(1024 * 1024 - 100)) + "\n").encode()
    expected_hash = hashlib.sha256()
    with path.open("wb") as stream:
        for row in [first, *[padding] * 101, last]:
            stream.write(row)
            expected_hash.update(row)
    sources, warnings, scan = draft_sources.read_sources(path)
    assert not warnings and [source["text"] for source in sources] == ["原始硬要求", "最新限制"]
    assert scan["bytes"] > limit and scan["segments"] == 2
    assert scan["max_segment_bytes"] == limit and scan["bytes_read"] == path.stat().st_size
    assert scan["history_digest"] == expected_hash.hexdigest()
    assert draft_sources.MAX_HISTORY_BYTES == limit
    meta, _ = blocks.read_page()
    item = node_entry("bounded_save", "保存明确要求", "保留原始硬要求和最新限制", sources)
    receipt = blocks.submit(**binding(meta), upserts=[item], ignored=[])
    assert receipt["saved_entry_ids"] == ["bounded_save"] and not receipt["pending"]
    assert path.stat().st_size == scan["bytes"]


@pytest.mark.parametrize("ending", [b"\n", b""])
def test_oversized_single_record_is_rejected_not_split_or_truncated(tmp_path, ending):
    path = tmp_path / "history.jsonl"
    path.write_bytes(b"x" * 65 + ending)
    scan = {}
    with pytest.raises(ValueError, match="单条历史记录超过"):
        list(history_lines(path, limit=64, scan=scan))
    assert not scan["complete"]


def test_retained_sources_and_human_context_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(draft_sources, "MAX_HISTORY_BYTES", 1024)
    path = tmp_path / "history.jsonl"
    write_history(path, [record("user", "u" * 400)] * 4)
    with pytest.raises(ValueError, match="整理结果超过"):
        draft_sources.read_sources(path)
    write_history(path, [{"type": "assistant", "content": [{"type": "tool_use", "name": "AskUserQuestion",
        "id": str(index), "input": {"question": "q" * 400}}]} for index in range(4)])
    with pytest.raises(ValueError, match="整理结果超过"):
        draft_sources.read_sources(path)


def test_malformed_and_partial_records_remain_visible_across_segments(tmp_path, monkeypatch):
    monkeypatch.setattr(draft_sources, "MAX_HISTORY_BYTES", 4096)
    path = tmp_path / "history.jsonl"
    write_history(path, [record("user", "早期要求"), noise(), noise()])
    with path.open("ab") as stream:
        stream.write(b'broken\n{"type":"user"')
    sources, warnings, scan = draft_sources.read_sources(path)
    assert sources[0]["text"] == "早期要求" and len(warnings) == 2
    assert "无法解析" in warnings[0] and "尚未完整落盘" in warnings[1]
    assert scan["lines"] == 5 and scan["segments"] == 2


@pytest.mark.parametrize("change", ["rewrite", "truncate", "replace", "rewrite_and_append"])
def test_source_mutations_during_scan_never_publish_a_mixed_snapshot(tmp_path, monkeypatch, change):
    path = tmp_path / "history.jsonl"
    original = b"old\n" + b"tail\n" * 50
    path.write_bytes(original)
    scan = {}
    lines = history_lines(path, limit=64, scan=scan)
    assert next(lines) == b"old\n"
    if change == "replace":
        # Windows forbids renaming this open handle. Model a replaced pathname
        # with a different file ID so the identity check is exercised there too.
        stat = path.stat()
        replacement = SimpleNamespace(st_dev=stat.st_dev, st_ino=stat.st_ino + 1,
            st_size=stat.st_size, st_mtime_ns=stat.st_mtime_ns)
        original_stat = Path.stat
        monkeypatch.setattr(Path, "stat", lambda target, *args, **kwargs:
            replacement if target == path else original_stat(target, *args, **kwargs))
    else:
        changed = b"new\n" + original[4:]
        if change == "truncate":
            changed = b"new\n"
        if change == "rewrite_and_append":
            changed += b"append\n"
        path.write_bytes(changed)
    with pytest.raises(ValueError, match="变化|截短"):
        list(lines)
    assert not scan["complete"]


def test_append_verifies_whole_prefix_and_preserves_fixed_snapshot(tmp_path):
    path = tmp_path / "history.jsonl"
    original = b"first\n" + b"next\n" * 50
    path.write_bytes(original)
    scan = {}
    lines = history_lines(path, limit=64, scan=scan)
    first = next(lines)
    with path.open("ab") as stream:
        stream.write(b"later\n")
    assert b"".join([first, *lines]) == original
    assert scan["complete"] and scan["verified_prefix_bytes"] == len(original)
    assert scan["history_digest"] == hashlib.sha256(original).hexdigest()
    later_scan = {}
    assert b"".join(history_lines(path, limit=64, scan=later_scan)).endswith(b"later\n")


def test_segmented_save_uses_full_sources_and_rejects_stale_or_incomplete_history(tmp_path, monkeypatch):
    monkeypatch.setattr(draft_sources, "MAX_HISTORY_BYTES", 8192)
    blocks = make_blocks(tmp_path / "project", rows=[record("user", "保留早期要求"),
        *[noise(6000) for _ in range(4)], record("user", "最新要求：核对全部页码")])
    original = blocks.source_path.read_bytes()
    meta, _ = blocks.read_page()
    users = user_sources(blocks)
    item = node_entry("verify", "核对页码", "核对全部页码并保留早期要求", users)
    receipt = blocks.submit(**binding(meta), upserts=[item], ignored=[])
    assert receipt["saved_entry_ids"] == ["verify"] and not receipt["pending"]
    assert blocks.source_path.read_bytes() == original
    saved = DraftStore.read_state(blocks.project, "chat")
    meta, _ = blocks.read_page()
    # A prefix rewrite with exactly the same file size still invalidates CAS.
    changed = original.replace("早期".encode(), "最早".encode())
    assert len(changed) == len(original)
    blocks.source_path.write_bytes(changed)
    with pytest.raises(ValueError, match="STALE"):
        blocks.submit(**binding(meta), upserts=[item], ignored=[])
    assert DraftStore.read_state(blocks.project, "chat") == saved
    fresh, _ = blocks.read_page()
    write_history(blocks.source_path, [record("user", "追加的业务要求")], append=True)
    with pytest.raises(ValueError, match="STALE"):
        blocks.submit(**binding(fresh), upserts=[item], ignored=[])
    current, _ = blocks.read_page()
    with blocks.source_path.open("ab") as stream:
        stream.write(b'{"type":"user"')
    with pytest.raises(ValueError, match="来源尚未完整保存"):
        blocks.submit(**binding(current), upserts=[item], ignored=[])
    assert DraftStore.read_state(blocks.project, "chat") == saved


def test_io_reads_never_exceed_budget_or_use_unbounded_readline(tmp_path, monkeypatch):
    path = tmp_path / "history.jsonl"
    path.write_bytes(b"row\n" * 100)
    original_open = Path.open
    reads = []
    class BoundedFile:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def fileno(self):
            return self.stream.fileno()
        def read(self, size=-1):
            assert 0 < size <= 64
            reads.append(size)
            return self.stream.read(size)
    monkeypatch.setattr(Path, "open", lambda target, *args, **kwargs: BoundedFile(original_open(target, *args, **kwargs)))
    scan = {}
    assert len(list(history_lines(path, limit=64, scan=scan))) == 100
    assert sum(reads) == 400 and scan["max_segment_bytes"] == 64
