from __future__ import annotations

import json
from pathlib import Path

from mewcode.external_agents import (
    ExternalConversation,
    ExternalMessage,
    _parent_folder,
    parse_claude_transcript,
    parse_codex_transcript,
)


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    return path


def test_external_conversation_exposes_parent_folder_without_full_path_grouping():
    conversation = ExternalConversation(
        source="claude-code",
        external_id="session-1",
        path="C:/Users/test/.claude/projects/demo/session.jsonl",
        project_path="C:/work/demo",
        messages=[ExternalMessage(id="1", role="user", text="hello")],
    )
    value = conversation.as_dict(include_messages=False)
    assert value["source_group"] == "claude-code"
    assert value["parent_folder"] == "demo"
    assert value["project_path"] == "C:/work/demo"


def test_claude_transcript_keeps_speech_and_structures_tools(tmp_path: Path):
    path = _write_jsonl(tmp_path / "claude.jsonl", [
        {
            "type": "user",
            "cwd": "D:/work/demo",
            "sessionId": "s1",
            "timestamp": "2026-08-28T10:00:00.000Z",
            "uuid": "u1",
            "message": {"role": "user", "content": "请补全 agent-test 场景"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-08-28T10:00:01.000Z",
            "uuid": "a1",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "secret scratchpad"},
                    {"type": "text", "text": "我先看目录结构。"},
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls -la"}},
                ],
            },
        },
        {
            "type": "user",
            "timestamp": "2026-08-28T10:00:02.000Z",
            "uuid": "u2",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": "total 41261\ndrwxr-xr-x 1 lenovo 197121 0 Jul 21 11:59 ./",
                    }
                ],
            },
        },
    ])
    conversation = parse_claude_transcript(path)
    assert conversation is not None
    speech = [item for item in conversation.messages if item.kind == "message"]
    tools = [item for item in conversation.messages if item.kind in {"tool_use", "tool_result", "thinking"}]
    assert [item.text for item in speech] == ["请补全 agent-test 场景", "我先看目录结构。"]
    assert conversation.title.startswith("请补全")
    assert "total 41261" not in conversation.title
    assert "secret scratchpad" not in "\n".join(item.text for item in speech)
    assert conversation.as_dict(include_messages=False)["message_count"] == 2
    assert any(item.kind == "tool_use" and "Bash" in item.text for item in tools)
    assert any(item.kind == "tool_result" for item in tools)
    assert all(len(item.text) <= 200 for item in tools)


def test_codex_transcript_strips_environment_context_from_title_and_body(tmp_path: Path):
    path = _write_jsonl(tmp_path / "codex.jsonl", [
        {
            "type": "session_meta",
            "timestamp": "2026-08-28T10:00:00.000Z",
            "payload": {"session_id": "c1", "cwd": "C:/Users/example-user/proj", "timestamp": "2026-08-28T10:00:00.000Z"},
        },
        {
            "type": "response_item",
            "timestamp": "2026-08-28T10:00:01.000Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{
                    "type": "input_text",
                    "text": "<environment_context>\n <cwd>C:\\Users\\example-user\\proj</cwd>\n <shell>powershell</shell>\n</environment_context>\n\n帮我修登录页",
                }],
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-08-28T10:00:02.000Z",
            "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "好的，我先看登录组件。"}]},
        },
    ])
    conversation = parse_codex_transcript(path)
    assert conversation is not None
    user = next(item for item in conversation.messages if item.role == "user")
    assert user.text == "帮我修登录页"
    assert "<environment_context>" not in user.text
    assert conversation.title == "帮我修登录页"


def test_title_falls_back_to_source_project_time_when_no_speech(tmp_path: Path):
    path = _write_jsonl(tmp_path / "empty-speech.jsonl", [
        {
            "type": "user",
            "cwd": "D:/work/demo",
            "sessionId": "s2",
            "timestamp": "2026-08-28T12:34:00.000Z",
            "uuid": "u1",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "content": "total 41261\n./"}],
            },
        },
    ])
    conversation = parse_claude_transcript(path)
    assert conversation is not None
    assert conversation.title == "Claude Code · demo · 08-28 12:34"
    assert all(item.kind != "message" for item in conversation.messages)


def test_parent_folder_skips_codex_date_segments_without_cwd(tmp_path: Path):
    transcript = tmp_path / "sessions" / "2026" / "08" / "28" / "rollout.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n", encoding="utf-8")
    assert _parent_folder("", str(transcript)) != "28"
    assert _parent_folder("", str(transcript)) != "08"
    assert _parent_folder("", str(transcript)) != "2026"
    assert _parent_folder("D:/projfiles/MewCode", str(transcript)) == "MewCode"


def test_parent_folder_collapses_codex_documents_ephemeral_cwd():
    project = r"C:\Users\example-user\Documents\Codex\2026-08-28\23-23-23-23-22-x20-7"
    assert _parent_folder(project, r"C:\Users\example-user\.codex\sessions\2026\08\28\a.jsonl") == "未关联项目 · 08-28"


def test_title_strips_agents_instructions_preamble(tmp_path: Path):
    path = _write_jsonl(tmp_path / "agents-title.jsonl", [
        {
            "type": "session_meta",
            "timestamp": "2026-08-28T10:00:00.000Z",
            "payload": {"session_id": "c2", "cwd": "D:/projfiles/MewCode", "timestamp": "2026-08-28T10:00:00.000Z"},
        },
        {
            "type": "response_item",
            "timestamp": "2026-08-28T10:00:01.000Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{
                    "type": "input_text",
                    "text": (
                        "AGENTS.md instructions for D:\\projfiles\\MewCode "
                        "<INSTRUCTIONS>Global Agent Constraints</INSTRUCTIONS>\n\n"
                        "帮我修侧栏分组"
                    ),
                }],
            },
        },
    ])
    conversation = parse_codex_transcript(path)
    assert conversation is not None
    assert conversation.title == "帮我修侧栏分组"


def test_parent_folder_decodes_claude_encoded_project_dir(tmp_path: Path):
    transcript = tmp_path / "projects" / "-D-projfiles-MewCode" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n", encoding="utf-8")
    assert _parent_folder("", str(transcript)) == "MewCode"


def test_codex_prefers_native_title_and_skips_uuid_user_text(tmp_path: Path):
    path = _write_jsonl(tmp_path / "native-title.jsonl", [
        {
            "type": "session_meta",
            "timestamp": "2026-08-28T10:00:00.000Z",
            "payload": {
                "session_id": "c9",
                "cwd": "D:/work/demo",
                "title": "登录页重构",
                "timestamp": "2026-08-28T10:00:00.000Z",
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-08-28T10:00:01.000Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "550e8400-e29b-41d4-a716-446655440000"}],
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-08-28T10:00:02.000Z",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "好的"}],
            },
        },
    ])
    conversation = parse_codex_transcript(path)
    assert conversation is not None
    assert conversation.title == "登录页重构"
    assert conversation.as_dict(include_messages=False)["parent_folder"] == "demo"


def test_claude_summary_row_becomes_title(tmp_path: Path):
    path = _write_jsonl(tmp_path / "summary.jsonl", [
        {"type": "summary", "summary": "排查打包失败", "leafUuid": "x"},
        {
            "type": "user",
            "cwd": "D:/work/app",
            "sessionId": "s3",
            "timestamp": "2026-08-28T10:00:00.000Z",
            "uuid": "u1",
            "message": {"role": "user", "content": "42"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-08-28T10:00:01.000Z",
            "uuid": "a1",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "收到"}]},
        },
    ])
    conversation = parse_claude_transcript(path)
    assert conversation is not None
    assert conversation.title == "排查打包失败"


def test_codex_history_wrapper_recovers_user_intent(tmp_path: Path):
    path = _write_jsonl(tmp_path / "history-wrap.jsonl", [
        {
            "type": "session_meta",
            "timestamp": "2026-08-28T10:00:00.000Z",
            "payload": {
                "session_id": "c-hist",
                "cwd": r"C:\Users\example-user\Documents\Codex\2026-08-28\slug",
                "timestamp": "2026-08-28T10:00:00.000Z",
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-08-28T10:00:01.000Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{
                    "type": "input_text",
                    "text": "The following is the Codex agent history whose request a\n\n帮我修侧栏",
                }],
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-08-28T10:00:02.000Z",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "好的"}],
            },
        },
    ])
    conversation = parse_codex_transcript(path)
    assert conversation is not None
    assert conversation.title == "帮我修侧栏"
    assert conversation.sidebar_visible is True
    payload = conversation.as_dict(include_messages=False)
    assert payload["is_project"] is False
    assert payload["parent_folder"] == ""


def test_tool_only_conversation_hidden_from_sidebar(tmp_path: Path):
    path = _write_jsonl(tmp_path / "tool-only.jsonl", [
        {
            "type": "user",
            "cwd": "D:/work/demo",
            "sessionId": "s-tool",
            "timestamp": "2026-08-28T10:00:00.000Z",
            "uuid": "u1",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "content": "total 1"}],
            },
        },
    ])
    conversation = parse_claude_transcript(path)
    assert conversation is not None
    assert conversation.sidebar_visible is False

