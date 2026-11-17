"""Read-only adapters for Codex and Claude Code transcript files.

The adapters intentionally normalize only conversation metadata and text. They
never execute an agent, read credentials, or import external messages into a
MewCode session. A polling watcher is used instead of OS-specific file events
so the same implementation works on desktop and Android-hosted backends.
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from mewcode.workspace_tree import source_label

MAX_FILE_BYTES = 24 * 1024 * 1024
MAX_MESSAGES = 2000
MAX_TEXT = 120_000
MAX_TOOL_SUMMARY = 160
TITLE_LIMIT = 56
SPEECH_BLOCK_TYPES = {"text", "input_text", "output_text"}
_ENV_CONTEXT_RE = re.compile(r"<environment_context>[\s\S]*?</environment_context>", re.I)
_MARKDOWN_FENCE_RE = re.compile(r"```[\s\S]*?```")
_MARKDOWN_MARK_RE = re.compile(r"[#*_`>~]+")
_WHITESPACE_RE = re.compile(r"\s+")
_ISO_CLOCK_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})")
_UUID_LIKE_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.I,
)
_NUMERIC_TITLE_RE = re.compile(r"^\d{1,8}$")
_YEAR_DIR_RE = re.compile(r"^20\d{2}$")
_MONTH_DIR_RE = re.compile(r"^(0[1-9]|1[0-2])$")
_DAY_DIR_RE = re.compile(r"^(0[1-9]|[12]\d|3[01])$")
_HASHISH_DIR_RE = re.compile(r"^[0-9a-f]{8,}$", re.I)
_JUNK_DIR_NAMES = frozenset({
    "", ".", "\\", "/",
    "sessions", "projects", "history", "session",
    ".codex", ".claude", "codex", "claude",
})
# Codex often parks unbound chats under Documents/Codex/YYYY-MM-DD/<slug>.
_CODEX_EPHEMERAL_RE = re.compile(
    r"(?i)(?:^|[/\\])Documents[/\\]Codex[/\\](\d{4}-\d{2}-\d{2})(?:[/\\]|$)"
)
_INSTRUCTIONS_BLOCK_RE = re.compile(
    r"(?is)(?:AGENTS\.md\s+instructions\b.*?(?:</instructions>|$)|<instructions\b[^>]*>.*?</instructions>)"
)
_TITLE_NOISE_PREFIX_RE = re.compile(
    r"(?is)^(?:\s*"
    r"(?:The following is the Codex agent history[^\n]*|"
    r"<recommended\s+plugins\b[^>]*>[\s\S]*?(?:</recommended\s+plugins>|>)|"
    r"<local-command-caveat\b[^>]*>[\s\S]*?(?:</local-command-caveat>|>)|"
    r"Files mentioned by the user:\s*|"
    r"Context from my IDE setup:\s*|"
    r"Open tabs:\s*|"
    r"Active file:\s*"
    r"))+"
)
_UNBOUND_PROJECT_LABEL = "未关联项目"


def _is_junk_folder_name(name: str) -> bool:
    value = (name or "").strip()
    if value in _JUNK_DIR_NAMES:
        return True
    if _YEAR_DIR_RE.fullmatch(value) or _MONTH_DIR_RE.fullmatch(value) or _DAY_DIR_RE.fullmatch(value):
        return True
    if _HASHISH_DIR_RE.fullmatch(value):
        return True
    return False


def _decode_claude_encoded_dir(name: str) -> str:
    """Decode Claude Code project dir names like `-D-work-MewCode` → `MewCode`."""
    value = (name or "").strip()
    if not value.startswith("-") or len(value) < 4:
        return ""
    parts = [part for part in value.lstrip("-").split("-") if part]
    if not parts:
        return ""
    if len(parts[0]) == 1 and parts[0].isalpha():
        reconstructed = parts[0] + ":/" + "/".join(parts[1:])
        leaf = Path(reconstructed).name.strip()
        return leaf if leaf and not _is_junk_folder_name(leaf) else ""
    leaf = parts[-1].strip()
    return leaf if leaf and not _is_junk_folder_name(leaf) else ""


def _codex_ephemeral_label(path_value: str) -> str:
    match = _CODEX_EPHEMERAL_RE.search((path_value or "").replace("\\", "/"))
    if not match:
        return ""
    date = match.group(1)
    return f"{_UNBOUND_PROJECT_LABEL} · {date[5:]}"  # MM-DD


def _folder_label_from_path(path_value: str) -> str:
    if not path_value:
        return ""
    ephemeral = _codex_ephemeral_label(path_value)
    if ephemeral:
        return ephemeral
    candidate = Path(path_value).expanduser()
    name = candidate.name.strip()
    if _is_junk_folder_name(name):
        decoded = _decode_claude_encoded_dir(name)
        if decoded:
            return decoded
        name = candidate.parent.name.strip()
    else:
        decoded = _decode_claude_encoded_dir(name)
        if decoded:
            return decoded
    if _is_junk_folder_name(name):
        return ""
    return name


def _parent_folder(project_path: str, transcript_path: str) -> str:
    """Return a display-only project folder without exposing full paths."""
    label = _folder_label_from_path(project_path)
    if label:
        return label
    try:
        current = Path(transcript_path).expanduser()
        if not current.is_absolute():
            current = current.resolve()
    except (OSError, RuntimeError, ValueError):
        current = Path(transcript_path) if transcript_path else Path()
    for candidate in [current.parent, *list(current.parents)[:8]]:
        name = candidate.name.strip()
        if _is_junk_folder_name(name):
            decoded = _decode_claude_encoded_dir(name)
            if decoded:
                return decoded
            continue
        decoded = _decode_claude_encoded_dir(name)
        if decoded:
            return decoded
        return name
    return "未指定项目"


def _strip_injected(text: str) -> str:
    return html.unescape(_ENV_CONTEXT_RE.sub("", text or "")).strip()


def _leaf_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "input_text", "output_text", "thinking"):
            if key in value:
                result = _leaf_text(value[key])
                if result:
                    return result
        return ""
    if isinstance(value, list):
        parts = [_leaf_text(item) for item in value]
        return "\n".join(part for part in parts if part).strip()
    return ""


def _summarize(text: str) -> str:
    compact = _WHITESPACE_RE.sub(" ", text or "").strip()
    if len(compact) <= MAX_TOOL_SUMMARY:
        return compact
    return compact[:MAX_TOOL_SUMMARY].rstrip() + "…"


def _tool_input_summary(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("command", "query", "path", "file_path", "pattern", "description"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip()
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return ""
    return _leaf_text(value)


def _content_blocks(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, str):
        return [{"type": "text", "text": value}] if value.strip() else []
    if isinstance(value, dict):
        if value.get("type"):
            return [value]
        inner = value.get("content")
        if inner is not None:
            return _content_blocks(inner)
        text = _leaf_text(value)
        return [{"type": "text", "text": text}] if text else []
    if isinstance(value, list):
        blocks: list[dict[str, Any]] = []
        for item in value:
            blocks.extend(_content_blocks(item))
        return blocks
    return []


def _append_from_content(
    messages: list[ExternalMessage],
    *,
    content: Any,
    role: str,
    timestamp: str,
    base_id: str,
) -> None:
    speech: list[str] = []

    def flush_speech() -> None:
        if not speech:
            return
        messages.append(ExternalMessage(
            id=base_id if not any(item.id == base_id for item in messages) else f"{base_id}:speech:{len(messages)}",
            role=role,
            text="\n".join(speech)[:MAX_TEXT],
            timestamp=timestamp,
            kind="message",
        ))
        speech.clear()

    for index, block in enumerate(_content_blocks(content)):
        btype = str(block.get("type") or "")
        item_id = f"{base_id}:{index}"
        if btype in SPEECH_BLOCK_TYPES or not btype:
            text = _strip_injected(_leaf_text(block))
            if text:
                speech.append(text)
            continue
        flush_speech()
        if btype == "tool_use":
            name = str(block.get("name") or "tool")
            detail = _summarize(_tool_input_summary(block.get("input")))
            text = _summarize(f"{name} · {detail}".strip(" ·"))
            if text:
                messages.append(ExternalMessage(id=item_id, role="assistant", text=text, timestamp=timestamp, kind="tool_use"))
            continue
        if btype == "tool_result":
            text = _summarize(_leaf_text(block.get("content")) or _leaf_text(block))
            if text:
                messages.append(ExternalMessage(id=item_id, role="assistant", text=text, timestamp=timestamp, kind="tool_result"))
            continue
        if btype == "thinking":
            text = _summarize(_leaf_text(block))
            if text:
                messages.append(ExternalMessage(id=item_id, role="assistant", text=text, timestamp=timestamp, kind="thinking"))
    flush_speech()


def _clean_title_source(text: str) -> str:
    cleaned = _MARKDOWN_FENCE_RE.sub(" ", _strip_injected(text))
    cleaned = _INSTRUCTIONS_BLOCK_RE.sub(" ", cleaned)
    cleaned = _TITLE_NOISE_PREFIX_RE.sub(" ", cleaned)
    cleaned = _MARKDOWN_MARK_RE.sub(" ", cleaned)
    return _WHITESPACE_RE.sub(" ", cleaned).strip()


def _clip_title(text: str, limit: int = TITLE_LIMIT) -> str:
    clipped = text
    for sep in ("。", "！", "？", "\n", ". ", "! ", "? "):
        index = clipped.find(sep)
        if 8 <= index <= limit:
            clipped = clipped[:index]
            break
    clipped = clipped.strip()
    if len(clipped) <= limit:
        return clipped
    return clipped[:limit].rstrip() + "…"


def _format_title_time(value: str) -> str:
    match = _ISO_CLOCK_RE.match(value or "")
    if not match:
        return ""
    return f"{match.group(2)}-{match.group(3)} {match.group(4)}:{match.group(5)}"


def _is_weak_title(text: str) -> bool:
    cleaned = (text or "").strip()
    if len(cleaned) < 2:
        return True
    if cleaned.startswith("[Request interrupted"):
        return True
    lower = cleaned.lower()
    if lower.startswith("agents.md"):
        return True
    if lower.startswith("the following is the codex agent history"):
        return True
    if lower.startswith("files mentioned by the user"):
        return True
    if lower.startswith("context from my ide setup"):
        return True
    if lower.startswith("<recommended"):
        return True
    if lower.startswith("<local-command-caveat"):
        return True
    if _UUID_LIKE_RE.fullmatch(cleaned):
        return True
    if _NUMERIC_TITLE_RE.fullmatch(cleaned):
        return True
    return False


def _effective_user_title(messages: list[ExternalMessage], native_title: str = "") -> str:
    native = _clean_title_source(native_title)
    if native and not _is_weak_title(native):
        return _clip_title(native)
    for message in messages:
        if message.role != "user" or message.kind != "message":
            continue
        cleaned = _clean_title_source(message.text)
        if cleaned and not _is_weak_title(cleaned):
            return _clip_title(cleaned)
    return ""


def _is_real_project_path(project_path: str) -> bool:
    value = (project_path or "").strip()
    if not value:
        return False
    if _codex_ephemeral_label(value):
        return False
    return True


def _conversation_title(
    messages: list[ExternalMessage],
    *,
    source: str,
    project_path: str,
    path: str,
    updated_at: str,
    native_title: str = "",
) -> str:
    recovered = _effective_user_title(messages, native_title)
    if recovered:
        return recovered
    parts = [source_label(source), _parent_folder(project_path, path)]
    clock = _format_title_time(updated_at)
    if clock:
        parts.append(clock)
    return " · ".join(part for part in parts if part)


def _timestamp(value: Any) -> str:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            pass
    return ""


def _bounded_lines(path: Path) -> Iterable[dict[str, Any]]:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return []
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, UnicodeError):
        return []
    result: list[dict[str, Any]] = []
    for line in lines[-MAX_MESSAGES * 3 :]:
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            result.append(value)
    return result


@dataclass(frozen=True)
class ExternalMessage:
    id: str
    role: str
    text: str
    timestamp: str = ""
    kind: str = "message"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "text": self.text,
            "timestamp": self.timestamp,
            "kind": self.kind,
        }


@dataclass
class ExternalConversation:
    source: str
    external_id: str
    path: str
    project_path: str = ""
    title: str = ""
    created_at: str = ""
    updated_at: str = ""
    messages: list[ExternalMessage] = field(default_factory=list)
    controllable: bool = False
    sidebar_visible: bool = True
    native_title: str = ""

    def as_dict(self, *, include_messages: bool = True) -> dict[str, Any]:
        parent = _parent_folder(self.project_path, self.path)
        is_project = _is_real_project_path(self.project_path)
        value = {
            "source": self.source,
            "external_id": self.external_id,
            "path": self.path,
            "project_path": self.project_path,
            "parent_folder": parent if is_project else "",
            "is_project": is_project,
            "sidebar_visible": self.sidebar_visible,
            "source_group": self.source,
            "title": self.title or "未命名对话",
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "message_count": sum(1 for message in self.messages if message.kind == "message"),
            "controllable": self.controllable,
        }
        if include_messages:
            value["messages"] = [message.as_dict() for message in self.messages]
        return value


def _message_id(item: dict[str, Any], fallback: int) -> str:
    for key in ("id", "uuid", "messageId"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    payload = item.get("payload")
    if isinstance(payload, dict):
        value = payload.get("id")
        if isinstance(value, str) and value:
            return value
    return str(fallback)


def parse_codex_transcript(path: Path) -> ExternalConversation | None:
    rows = list(_bounded_lines(path))
    if not rows:
        return None
    session: dict[str, Any] = {}
    messages: list[ExternalMessage] = []
    for index, item in enumerate(rows):
        if item.get("type") == "session_meta" and isinstance(item.get("payload"), dict):
            session = item["payload"]
        payload = item.get("payload")
        if item.get("type") != "response_item" or not isinstance(payload, dict):
            continue
        if payload.get("type") != "message" or payload.get("role") not in {"user", "assistant"}:
            continue
        _append_from_content(
            messages,
            content=payload.get("content"),
            role=str(payload.get("role")),
            timestamp=_timestamp(item.get("timestamp")) or _timestamp(payload.get("timestamp")),
            base_id=_message_id(payload, index),
        )
    if not messages:
        return None
    stat = path.stat()
    created = _timestamp(session.get("timestamp")) or _timestamp(rows[0].get("timestamp"))
    updated = _timestamp(rows[-1].get("timestamp")) or datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
    project_path = str(session.get("cwd") or "")
    native_title = ""
    for key in ("title", "name", "session_title", "display_name"):
        value = session.get(key)
        if isinstance(value, str) and value.strip():
            native_title = value.strip()
            break
    kept = messages[-MAX_MESSAGES:]
    title = _conversation_title(
        kept,
        source="codex",
        project_path=project_path,
        path=str(path),
        updated_at=updated,
        native_title=native_title,
    )
    return ExternalConversation(
        source="codex",
        external_id=str(session.get("session_id") or session.get("id") or path.stem),
        path=str(path),
        project_path=project_path,
        title=title,
        created_at=created,
        updated_at=updated,
        messages=kept,
        controllable=False,
        sidebar_visible=bool(_effective_user_title(kept, native_title)),
        native_title=native_title,
    )


def parse_claude_transcript(path: Path) -> ExternalConversation | None:
    rows = list(_bounded_lines(path))
    if not rows:
        return None
    messages: list[ExternalMessage] = []
    project_path = ""
    native_title = ""
    for index, item in enumerate(rows):
        if item.get("isSidechain"):
            continue
        if not native_title and item.get("type") in {"summary", "session"}:
            for key in ("summary", "title", "display_name"):
                value = item.get(key)
                if isinstance(value, str) and value.strip() and not _is_weak_title(_clean_title_source(value)):
                    native_title = value.strip()
                    break
        role = ""
        message = item.get("message")
        if isinstance(message, dict):
            role = str(message.get("role") or "")
            content = message.get("content")
        else:
            content = item.get("content")
        if not role and item.get("type") in {"user", "assistant"}:
            role = str(item.get("type"))
        if role not in {"user", "assistant"}:
            continue
        if not project_path:
            project_path = str(item.get("cwd") or "")
        _append_from_content(
            messages,
            content=content,
            role=role,
            timestamp=_timestamp(item.get("timestamp")),
            base_id=_message_id(item, index),
        )
    if not messages:
        return None
    stat = path.stat()
    kept = messages[-MAX_MESSAGES:]
    updated = kept[-1].timestamp or datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
    title = _conversation_title(
        kept,
        source="claude-code",
        project_path=project_path,
        path=str(path),
        updated_at=updated,
        native_title=native_title,
    )
    return ExternalConversation(
        source="claude-code",
        external_id=str(next((item.get("sessionId") for item in rows if item.get("sessionId")), path.stem)),
        path=str(path),
        project_path=project_path,
        title=title,
        created_at=kept[0].timestamp,
        updated_at=updated,
        messages=kept,
        controllable=False,
        sidebar_visible=bool(_effective_user_title(kept, native_title)),
        native_title=native_title,
    )


def default_transcript_roots() -> dict[str, list[Path]]:
    home = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or ".")
    codex = Path(os.environ.get("MEWCODE_CODEX_DIR") or home / ".codex")
    claude = Path(os.environ.get("MEWCODE_CLAUDE_DIR") or home / ".claude")
    return {
        "codex": [codex / "sessions"],
        "claude-code": [claude / "projects", claude / "sessions"],
    }


class ExternalAgentStore:
    """Bounded polling store for external transcript snapshots."""

    def __init__(self, roots: dict[str, list[Path]] | None = None) -> None:
        self.roots = roots or default_transcript_roots()
        self._files: dict[tuple[str, str], tuple[int, int]] = {}
        self._conversations: dict[tuple[str, str], ExternalConversation] = {}
        self._refresh_lock = threading.Lock()

    def _paths(self) -> Iterable[tuple[str, Path]]:
        for source, roots in self.roots.items():
            for root in roots:
                try:
                    if not root.exists():
                        continue
                    for path in root.rglob("*.jsonl"):
                        if path.is_file():
                            yield source, path
                except OSError:
                    continue

    def refresh(self) -> list[dict[str, Any]]:
        with self._refresh_lock:
            return self._refresh()

    def _refresh(self) -> list[dict[str, Any]]:
        changed: list[dict[str, Any]] = []
        files = dict(self._files)
        conversations = dict(self._conversations)
        seen: set[tuple[str, str]] = set()
        for source, path in self._paths():
            try:
                stat = path.stat()
            except OSError:
                continue
            key = (source, str(path))
            seen.add(key)
            signature = (stat.st_mtime_ns, stat.st_size)
            if files.get(key) == signature:
                continue
            files[key] = signature
            conversation = parse_codex_transcript(path) if source == "codex" else parse_claude_transcript(path)
            if conversation is None:
                continue
            conv_key = (conversation.source, conversation.external_id)
            conversations[conv_key] = conversation
            changed.append(conversation.as_dict(include_messages=True))
        for key in set(files) - seen:
            files.pop(key, None)
        # Readers keep the previous complete mapping while a worker scans.
        self._files, self._conversations = files, conversations
        return changed

    def list(self, source: str = "", *, include_hidden: bool = False) -> list[dict[str, Any]]:
        values = []
        for conversation in self._conversations.values():
            if source and conversation.source != source:
                continue
            if not include_hidden and not conversation.sidebar_visible:
                continue
            values.append(conversation.as_dict(include_messages=False))
        return sorted(values, key=lambda value: value.get("updated_at", ""), reverse=True)

    def get(self, source: str, external_id: str) -> dict[str, Any] | None:
        conversation = self._conversations.get((source, external_id))
        return conversation.as_dict(include_messages=True) if conversation else None

    async def watch(self, callback: Callable[[dict[str, Any]], Awaitable[None]], stop: asyncio.Event) -> None:
        while not stop.is_set():
            changed = await asyncio.to_thread(self.refresh)
            if stop.is_set():
                return
            for conversation in changed:
                await callback({"type": "external_agent_update", "conversation": conversation})
            try:
                await asyncio.wait_for(stop.wait(), timeout=1.5)
            except asyncio.TimeoutError:
                continue


__all__ = [
    "ExternalAgentStore", "ExternalConversation", "ExternalMessage",
    "parse_codex_transcript", "parse_claude_transcript", "default_transcript_roots",
]
