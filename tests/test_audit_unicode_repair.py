from __future__ import annotations

import copy
import hashlib
import json

import pytest

from mewcode.agent import Agent
from mewcode.conversation import ConversationManager, Message, ToolResultBlock, ToolUseBlock
from mewcode.expert_runs import ExpertRunStore, deserialize_messages, serialize_messages
from mewcode.serialization import build_messages
from mewcode.tools.read_audit_artifact import ReadAuditArtifact, ReadAuditArtifactParams
from mewcode.unicode_text import utf8_safe_json, utf8_safe_text


def test_unicode_repair_preserves_chinese_math_emoji_and_visible_bad_code_units():
    text = "中文 α≤β 😀\ud83d\ude00 x\ud800y\udfffz"
    repaired = utf8_safe_text(text)
    assert repaired == "中文 α≤β 😀😀 x\\ud800y\\udfffz"
    assert utf8_safe_text(repaired) == repaired
    repaired.encode("utf-8")


@pytest.mark.asyncio
async def test_pdf_bad_font_mapping_cannot_poison_audit_request(tmp_path, monkeypatch):
    pdf = tmp_path / "evidence.pdf"
    pdf.write_bytes(b"mock PDF bytes must remain untouched")
    original = pdf.read_bytes()
    bad = "图5 中文 α≤β \ud800\ud801\udfff\udfff"
    monkeypatch.setattr("mewcode.tools.pdf_pixels.extract_page_text", lambda path, page, **kwargs: (bad, 1))
    tool = ReadAuditArtifact([str(tmp_path)])
    result = await tool.execute(ReadAuditArtifactParams(file_path=str(pdf)))
    assert not result.is_error
    assert hashlib.sha256(original).hexdigest() in result.output
    assert "1\t图5 中文 α≤β" in result.output
    assert "异常 Unicode" in result.output and "核对原页" in result.output
    assert "\\ud800" in result.output
    assert pdf.read_bytes() == original
    result.output.encode("utf-8")
    conversation = ConversationManager()
    conversation.add_assistant_message("", tool_uses=[ToolUseBlock("read", tool.name, {})])
    conversation.add_tool_results_message([ToolResultBlock("read", result.output)])
    for protocol in ("openai", "openai-compat", "anthropic"):
        payload = build_messages(conversation.get_messages(), protocol)
        # This is the same UTF-8 hashing path that runs before an LLM request.
        for item in payload:
            assert Agent._hash_text(json.dumps(item, ensure_ascii=False))


@pytest.mark.parametrize("protocol", ["openai", "openai-compat", "anthropic"])
def test_old_tool_checkpoints_remain_usable_without_replaying_tools(protocol):
    history = [Message(role="assistant", content="", tool_uses=[ToolUseBlock("read", "ReadAuditArtifact", {})]),
               Message(role="user", content="", tool_results=[ToolResultBlock("read", "旧证据\ud800 中文 😀")])]
    before = copy.deepcopy(history)
    restored = deserialize_messages(serialize_messages(history))
    payload = build_messages(restored, protocol)
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    assert b"\\\\ud800" in encoded
    assert history == before
    assert restored[1].tool_results[0].content == before[1].tool_results[0].content
    assert len(restored) == 2


def test_existing_progress_and_bad_unicode_roundtrip_safely_in_run_store(tmp_path):
    store = ExpertRunStore(tmp_path)
    run_id = "f" * 32
    store.create(run_id=run_id, conversation_id="c", task="task", attachments=[], profile={}, strategy="stable")
    store.mutate(run_id, lambda value: value.update(status="paused", node_states={
        "executor": {"status": "completed", "text": "实验已完成\ud800", "tools": {}},
        "auditor": {"status": "needs_attention", "text": "编码错误\udfff", "tools": {}},
    }))
    saved = store.load(run_id)
    assert saved["node_states"]["executor"]["text"] == "实验已完成\ud800"
    assert saved["node_states"]["executor"]["status"] == "completed"
    json.dumps(store.public(run_id), ensure_ascii=False).encode("utf-8")
    store.resume(run_id, strategy="stable")
    assert store.load(run_id)["node_states"]["executor"]["status"] == "completed"


def test_utf8_json_copy_does_not_mutate_source():
    original = {"content": [{"text": "bad\ud800"}], "image": "abc123==", "counter": 5}
    fixed = utf8_safe_json(original)
    assert original["content"][0]["text"] == "bad\ud800"
    assert fixed["content"][0]["text"] == "bad\\ud800"
    assert fixed["image"] == original["image"]
    assert fixed["counter"] == 5
