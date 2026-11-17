import copy
import json
import re

import pytest

from mewcode.agent import Agent, ErrorEvent
from mewcode.client import LLMClient
from mewcode.context.working_set import CATALOG_KIND, OutputArchive, WorkingSet
from mewcode.conversation import (
    ConversationManager, ToolResultBlock, ToolUseBlock, estimate_tokens,
)
from mewcode.serialization import build_openai_input
from mewcode.tools import ToolRegistry
from mewcode.tools.base import StreamEnd, TextDelta
from mewcode.tools.read_file import ReadFile


def conversation(count=100):
    value = ConversationManager()
    value.add_user_message("Keep the public API compatible. Never change the schema.")
    for i in range(count):
        value.add_assistant_message("", [
            ToolUseBlock(f"call-{i}", "ReadFile", {"file_path": "same.py", "offset": i * 10})
        ])
        text = f"version-{i}\n" + ("source content here\n" * 500)
        value.add_tool_results_message([
            ToolResultBlock(f"call-{i}", text, host_observation={"version": i})
        ])
    return value


def prepare(working, conv, **kwargs):
    return working.prepare(
        conv, context_window=1_050_000, output_reserve=8_192,
        fixed_tokens=kwargs.get("fixed_tokens", 0),
        can_archive=lambda call: call.tool_name == "ReadFile",
    )


def test_watermarks_preserve_original_hot_pairs_and_restore_checkpoint(tmp_path):
    conv = conversation()
    original = copy.deepcopy(conv.history)
    working = WorkingSet(tmp_path, "session-a")
    view = prepare(working, conv)
    assert working.last_report["replaced_results"] > 0
    assert estimate_tokens(view.history) <= 101_000
    assert conv.history == original
    assert view.history[0] == original[0]
    assert view.history[-16:] == original[-16:]
    assert view.history[2].tool_results[0].host_observation == {"version": 0}
    wire = build_openai_input(view.get_messages())
    assert sum(x.get("type") == "function_call" for x in wire) == 100
    assert sum(x.get("type") == "function_call_output" for x in wire) == 100
    restarted = WorkingSet(tmp_path, "session-a")
    assert build_openai_input(restarted.project(conv).history) == wire
    conv.add_user_message("Continue with the same constraints.")
    appended = prepare(working, conv)
    assert build_openai_input(appended.history)[:-1] == wire


def test_under_high_water_keeps_full_text(tmp_path):
    conv = conversation(20)
    assert prepare(WorkingSet(tmp_path, "small"), conv) is conv
    assert not list(tmp_path.rglob("*.json"))


def test_smaller_model_window_rechecks_previous_unshrinkable_context(tmp_path):
    conv = conversation(20)
    working = WorkingSet(tmp_path, "model-switch")
    prepare(working, conv, fixed_tokens=220_000)
    assert working.last_report["target_reached"] is False
    earlier_high = working.last_report["high_water"]
    working.prepare(
        conv, context_window=128_000, output_reserve=8_192,
        fixed_tokens=80_000, can_archive=lambda call: True,
    )
    assert working.last_report["high_water"] < earlier_high


def test_missing_archive_keeps_original_result(tmp_path):
    conv = conversation()
    working = WorkingSet(tmp_path, "missing")
    prepare(working, conv)
    key, item = next(iter(working._replacements.items()))
    working.archive._path(item["ref_id"]).unlink()
    view = working.project(conv)
    original_result = next(
        result for message in conv.history for result in message.tool_results
        if working._key(result.tool_use_id, result.content) == key
    )
    actual = next(
        result for message in view.history for result in message.tool_results
        if result.tool_use_id == original_result.tool_use_id
    )
    assert actual == original_result
    assert original_result.tool_use_id not in working.protected_result_ids(conv)


def test_checkpoint_survives_legacy_result_budget(tmp_path):
    from mewcode.context.manager import apply_tool_result_budget, create_replacement_state

    conv = conversation()
    working = WorkingSet(tmp_path, "legacy")
    view = prepare(working, conv)
    state = create_replacement_state()
    state.replacements["call-0"] = "An obsolete preview that hides the archive reference"
    budgeted, _ = apply_tool_result_budget(
        view, tmp_path, state, cache_stable=True,
        protected_result_ids=working.protected_result_ids(conv),
    )
    assert budgeted.history == view.history


def test_fixed_input_budget_counts_and_errors_are_not_dehydrated(tmp_path):
    conv = conversation(25)
    conv.history[2].tool_results[0].is_error = True
    conv.history[4].tool_results[0].content_blocks = [{"type": "image", "data": "preserve"}]
    conv.history[6].tool_results[0].content = "outcome unknown\n" + "x" * 10_000
    original = copy.deepcopy(conv.history)
    view = prepare(WorkingSet(tmp_path, "fixed"), conv, fixed_tokens=220_000)
    assert view.history[:7] == original[:7]
    assert any("[archived tool output:" in r.content for m in view.history for r in m.tool_results)
    assert conv.history == original


def test_archive_failure_preserves_context(tmp_path, monkeypatch):
    working = WorkingSet(tmp_path, "broken")
    monkeypatch.setattr(working.archive, "save", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    conv = conversation()
    assert prepare(working, conv) is conv
    assert working.last_report["replaced_results"] == 0


def test_manifest_failure_does_not_publish_replacements(tmp_path, monkeypatch):
    import mewcode.context.working_set as module
    original = module._atomic_json
    def fail_manifest(path, data):
        if path.name == "replacements.json":
            raise OSError("disk full")
        return original(path, data)
    monkeypatch.setattr(module, "_atomic_json", fail_manifest)
    working = WorkingSet(tmp_path, "broken")
    conv = conversation()
    assert prepare(working, conv) is conv
    assert working.last_report["replaced_results"] == 0


def test_refs_preserve_versions_and_do_not_cross_sessions(tmp_path):
    first = OutputArchive(tmp_path, "first")
    ref = first.save("same-call", "old version")
    newer = first.save("same-call", "new version")
    assert ref != newer
    assert first.load(ref)["text"] == "old version"
    assert first.load(newer)["text"] == "new version"
    with pytest.raises(FileNotFoundError):
        OutputArchive(tmp_path, "second").inspect(ref)
    with pytest.raises(ValueError):
        first.inspect("../../outside")
    first._path(ref).write_text('{"version": 1, "tool_id": "same-call", "text": "tampered"}')
    with pytest.raises(ValueError, match="integrity"):
        first.inspect(ref)


def test_literal_search_and_long_line_pagination(tmp_path):
    archive = OutputArchive(tmp_path, "session")
    ref = archive.save("log", "header\n" + "a" * 15_000 + "\nFAILED [case]\ntail\n")
    page = json.loads(archive.inspect(ref, start_line=2))
    assert len(page["text"]) <= 7_000
    assert page["next_line"] == 2
    assert page["next_char_offset"] > 0
    next_page = json.loads(archive.inspect(
        ref, start_line=page["next_line"], char_offset=page["next_char_offset"]
    ))
    assert next_page["next_char_offset"] > page["next_char_offset"]
    match = json.loads(archive.inspect(ref, contains="[case]"))
    assert match["text"] == "3: FAILED [case]\n"


class Client(LLMClient):
    async def stream(self, conversation, system="", tools=None):
        yield TextDelta("Done")
        yield StreamEnd("end_turn", input_tokens=10, output_tokens=1)


def agent(tmp_path, client=None):
    registry = ToolRegistry()
    registry.register(ReadFile())
    return Agent(client or Client(), registry, "openai", str(tmp_path), max_iterations=3)


def test_raw_output_saved_before_preview_and_storage_failure_keeps_original(tmp_path, monkeypatch):
    runner = agent(tmp_path)
    runner.session_id = "raw"
    text = "prefix\n" + "body\n" * 5_000 + "important tail"
    preview = runner._maybe_persist_or_truncate("raw-call", text)
    ref = re.search(r'ref_id="([0-9a-f]{64})"', preview)[1]
    assert len(preview) < len(text)
    assert runner._get_working_set().archive.load(ref)["text"] == text
    monkeypatch.setattr(runner._get_working_set().archive, "save", lambda *a: (_ for _ in ()).throw(OSError()))
    assert runner._maybe_persist_or_truncate("other", text) == text


def test_fallback_medium_result_preserves_complete_unicode_and_model_tail(tmp_path):
    runner = agent(tmp_path)
    runner.registry.disable("InspectArchivedOutput")
    text = json.dumps({"padding": "文" * 11_000, "models": [{"model_id": "grok-4.7"}]}, ensure_ascii=False)
    preview = runner._maybe_persist_or_truncate("metadata-call", text)
    assert "<persisted-output>" in preview
    path = re.search(r"完整内容已保存到：\n([^\n]+)", preview)[1]
    from pathlib import Path
    assert Path(path).read_text(encoding="utf-8") == text
    assert len(preview) < 10_000 and not preview.endswith("… (output truncated)")


def test_fallback_output_archive_failure_retains_original(tmp_path, monkeypatch):
    runner = agent(tmp_path)
    runner.registry.disable("InspectArchivedOutput")
    text = "x" * 10_001 + "required tail"
    def fail(*args):
        raise OSError("synthetic write failure")
    monkeypatch.setattr("mewcode.context.manager.persist_tool_result", fail)
    assert runner._maybe_persist_or_truncate("metadata-call", text) == text


def test_fallback_result_versions_do_not_overwrite_or_escape_session(tmp_path):
    from pathlib import Path
    runner = agent(tmp_path)
    runner.registry.disable("InspectArchivedOutput")
    contents = ["a" * 10_001, "b" * 10_001]
    paths = []
    for content in contents:
        preview = runner._maybe_persist_or_truncate("../../same-call", content)
        path = Path(re.search(r"完整内容已保存到：\n([^\n]+)", preview)[1])
        assert path.parent == runner.session_dir
        paths.append(path)
    assert paths[0] != paths[1]
    assert [path.read_text(encoding="utf-8") for path in paths] == contents
    assert runner._maybe_persist_or_truncate("small", "x" * 10_000) == "x" * 10_000


def test_fallback_archive_preserves_crlf_and_refuses_incorrect_existing_bytes(tmp_path):
    from pathlib import Path
    runner = agent(tmp_path)
    runner.registry.disable("InspectArchivedOutput")
    text = "header\r\n" + "正文\r\n" * 3_000 + "tail\r\n"
    preview = runner._maybe_persist_or_truncate("windows-lines", text)
    path = Path(re.search(r"完整内容已保存到：\n([^\n]+)", preview)[1])
    assert path.read_bytes() == text.encode("utf-8")
    path.write_bytes(b"synthetic incomplete archive")
    assert runner._maybe_persist_or_truncate("windows-lines", text) == text
    assert path.read_bytes() == b"synthetic incomplete archive"


def test_catalog_is_appended_once_and_changes_do_not_rewrite_prefix(tmp_path):
    runner = agent(tmp_path)
    deferred = ReadFile()
    deferred.name = "DeferredRead"
    deferred.should_defer = True
    runner.registry.register(deferred)
    conv = ConversationManager()
    conv.add_user_message("<system-reminder>User-authored constraint must survive.</system-reminder>")
    runner._announce_deferred_tools(conv)
    before = copy.deepcopy(conv.history)
    for _ in range(20):
        runner._announce_deferred_tools(conv)
    assert conv.history == before
    assert sum(m.context_kind == CATALOG_KIND for m in conv.history) == 1
    other = ReadFile()
    other.name = "OtherDeferred"
    other.should_defer = True
    runner.registry.register(other)
    runner._announce_deferred_tools(conv)
    assert conv.history[:-1] == before
    assert conv.history[-1].transient


@pytest.mark.asyncio
@pytest.mark.parametrize("completion", [False, True])
async def test_real_agent_paths_send_bounded_view_before_summary(tmp_path, monkeypatch, completion):
    sent = []
    class Capturing(Client):
        async def stream(self, conversation, system="", tools=None):
            sent.append(copy.deepcopy(conversation.history))
            assert any(t["name"] == "InspectArchivedOutput" for t in tools)
            async for event in super().stream(conversation, system, tools):
                yield event
    async def no_summary(view, *args, **kwargs):
        assert estimate_tokens(view.history) < 120_000
        return None
    monkeypatch.setattr("mewcode.agent.auto_compact", no_summary)
    runner = agent(tmp_path, Capturing())
    runner.context_window = 1_050_000
    runner.session_id = "integration"
    conv = conversation()
    if completion:
        await runner.run_to_completion("", conv)
    else:
        events = [e async for e in runner.run(conv)]
        assert not [e for e in events if isinstance(e, ErrorEvent)]
    assert len(sent) == 1
    assert any("[archived tool output:" in r.content for m in sent[0] for r in m.tool_results)
    assert not any("[archived tool output:" in r.content for m in conv.history for r in m.tool_results)


@pytest.mark.asyncio
async def test_recall_tool_reads_current_session_only(tmp_path):
    runner = agent(tmp_path)
    runner.session_id = "session-a"
    ref = runner._get_working_set().archive.save("call", "original evidence")
    tool = runner.registry.get("InspectArchivedOutput")
    result = await tool.execute(tool.params_model(ref_id=ref))
    assert not result.is_error and "original evidence" in result.output
    runner.session_id = "session-b"
    result = await tool.execute(tool.params_model(ref_id=ref))
    assert result.is_error and "original evidence" not in result.output


@pytest.mark.asyncio
async def test_inherited_recall_tool_is_rebound_to_child_scope(tmp_path):
    parent = agent(tmp_path)
    parent.session_id = "parent"
    ref = parent._get_working_set().archive.save("parent-call", "parent-only evidence")
    child_registry = ToolRegistry()
    child_registry.register(parent.registry.get("InspectArchivedOutput"))
    child = Agent(Client(), child_registry, "openai", str(tmp_path))
    child.session_id = "child"
    tool = child.registry.get("InspectArchivedOutput")
    result = await tool.execute(tool.params_model(ref_id=ref))
    assert result.is_error and "parent-only evidence" not in result.output
