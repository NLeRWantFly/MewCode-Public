from __future__ import annotations

import json
import random
import string
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import IO, Any

from mewcode.client import scheduled_client_stream
from mewcode.conversation import (
    ConversationManager,
    Message,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    repair_tool_call_chain,
)
from mewcode.teams.fsio import atomic_write_text, file_lock

SESSIONS_DIR = ".mewcode/sessions"
DEFAULT_MAX_AGE_DAYS = 30
TITLE_MAX_LENGTH = 50

SESSION_SUMMARY_PROMPT = (
    "你是一个对话摘要助手。请根据下面的对话内容，用一句话总结这个会话的主要内容。"
    "只输出摘要文本，不要加任何前缀或标点符号外的修饰。不要调用任何工具。"
)


# ---------------------------------------------------------------------------
# RecordType & SessionRecord
# ---------------------------------------------------------------------------


class RecordType(str, Enum):
    SYSTEM_PROMPT = "system_prompt"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL_RESULT = "tool_result"
    COMPRESSION = "compression"
    # Layer-2 compact 标记。auto_compact 压缩对话记录时写入。
    # 内容为结构化载荷（参见 make_compact_boundary / parse_compact_boundary），
    # 包含摘要文本和原样保留的 keep 尾部（以序列化 record 形式内联），
    # 使 resume 可以仅凭此标记重建压缩后的状态，无需重放标记之前的原始前缀。
    COMPACT_BOUNDARY = "compact_boundary"
    # Display-only expert timeline reference; records_to_messages ignores it.
    EXPERT_RUN = "expert_run"


def _host_observation_snapshot(value: Any) -> dict[str, Any] | list[Any] | None:
    """Copy structured host metadata without coercing claims or arbitrary objects.

    Missing or malformed metadata stays unknown. This only preserves observations;
    it does not establish their provenance or authorize reconciliation/execution.
    """
    if not isinstance(value, (dict, list)):
        return None

    def is_json(item: Any) -> bool:
        if isinstance(item, dict):
            return all(isinstance(key, str) and is_json(nested)
                       for key, nested in item.items())
        if isinstance(item, list):
            return all(is_json(nested) for nested in item)
        return item is None or type(item) in (str, bool, int, float)

    try:
        if not is_json(value):
            return None
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, RecursionError):
        return None


@dataclass
class SessionRecord:
    type: RecordType
    content: Any
    timestamp: datetime
    tool_use_id: str | None = None
    is_error: bool = False
    host_observation: dict[str, Any] | list[Any] | None = None
    message_host_observation: dict[str, Any] | list[Any] | None = None

    def to_jsonl(self) -> str:
        data: dict[str, Any] = {
            "type": self.type.value,
            "content": self.content,
            "timestamp": self.timestamp.isoformat(),
        }
        if self.tool_use_id is not None:
            data["tool_use_id"] = self.tool_use_id
        if self.type == RecordType.TOOL_RESULT:
            data["is_error"] = self.is_error
            observation = _host_observation_snapshot(self.host_observation)
            if observation is not None:
                data["host_observation"] = observation
            message_observation = _host_observation_snapshot(self.message_host_observation)
            if message_observation is not None:
                data["message_host_observation"] = message_observation
        elif self.type in {RecordType.USER, RecordType.ASSISTANT}:
            observation = _host_observation_snapshot(self.message_host_observation)
            if observation is not None:
                data["host_observation"] = observation
        return json.dumps(data, ensure_ascii=False)


    @classmethod
    def from_jsonl(cls, line: str) -> SessionRecord | None:
        try:
            data = json.loads(line)
            record_type = RecordType(data["type"])
            observation = _host_observation_snapshot(data.get("host_observation"))
            return cls(
                type=record_type,
                content=data["content"],
                timestamp=datetime.fromisoformat(data["timestamp"]),
                tool_use_id=data.get("tool_use_id"),
                is_error=data.get("is_error", False),
                host_observation=observation if record_type == RecordType.TOOL_RESULT else None,
                message_host_observation=(
                    _host_observation_snapshot(data.get("message_host_observation"))
                    if record_type == RecordType.TOOL_RESULT else observation
                ),
            )
        except (json.JSONDecodeError, KeyError, ValueError):
            return None

    @classmethod
    def from_message(cls, message: Message) -> list[SessionRecord]:
        now = datetime.now(timezone.utc)
        records: list[SessionRecord] = []

        if message.tool_results:
            for tr in message.tool_results:
                records.append(
                    cls(
                        type=RecordType.TOOL_RESULT,
                        content=tr.content,
                        timestamp=now,
                        tool_use_id=tr.tool_use_id,
                        is_error=tr.is_error,
                        host_observation=_host_observation_snapshot(tr.host_observation),
                    )
                )
        elif message.role == "assistant":
            if message.tool_uses or message.thinking_blocks:
                content_blocks: list[dict[str, Any]] = []
                for thinking in message.thinking_blocks:
                    content_blocks.append(
                        {
                            "type": "thinking",
                            "thinking": thinking.thinking,
                            "signature": thinking.signature,
                            "kind": thinking.kind,
                        }
                    )
                if message.content:
                    content_blocks.append({"type": "text", "text": message.content})
                for tu in message.tool_uses:
                    content_blocks.append(
                        {
                            "type": "tool_use",
                            "id": tu.tool_use_id,
                            "name": tu.tool_name,
                            "input": tu.arguments,
                        }
                    )
                records.append(
                    cls(type=RecordType.ASSISTANT, content=content_blocks, timestamp=now)
                )
            else:
                records.append(
                    cls(type=RecordType.ASSISTANT, content=message.content, timestamp=now)
                )
        else:
            content: Any = message.content
            if message.content_blocks:
                blocks: list[dict[str, Any]] = []
                if message.content:
                    blocks.append({"type": "text", "text": message.content})
                for block in message.content_blocks:
                    attachment_id = str(block.get("attachment_id") or "")
                    if attachment_id:
                        metadata = block.get("attachment")
                        item = {
                            "type": "attachment",
                            "attachment_id": attachment_id,
                            "name": str(block.get("name") or "attachment"),
                        }
                        if isinstance(metadata, dict):
                            item["attachment"] = {
                                key: metadata.get(key)
                                for key in (
                                    "id", "conversation_id", "name", "mime_type",
                                    "size", "kind", "extracted_chars", "truncated",
                                    "preview_url",
                                )
                                if key in metadata
                            }
                        blocks.append(item)
                    elif block.get("type") == "text":
                        blocks.append({"type": "text", "text": str(block.get("text", ""))})
                    elif block.get("type") == "image":
                        # Legacy non-attachment images cannot be restored from
                        # disk without embedding large base64 payloads.
                        blocks.append({"type": "text", "text": "[image omitted from persisted history]"})
                content = blocks
            records.append(
                cls(type=RecordType.USER, content=content, timestamp=now)
            )

        message_observation = _host_observation_snapshot(message.host_observation)
        if message_observation is not None and records:
            records[0].message_host_observation = message_observation

        return records


# ---------------------------------------------------------------------------
# Compact boundary 载荷（摘要 + 内联的 keep 尾部）
# ---------------------------------------------------------------------------


def _message_to_record_dicts(message: Message) -> list[dict[str, Any]]:
    """将单条 Message 序列化为与磁盘存储格式一致的 record-dict 列表。

    复用 SessionRecord.from_message，使内联的 keep 尾部与正常追加消息的持久化
    结果逐字节一致（assistant 的 tool_uses 变为 content-blocks 列表，每个
    tool_result 独立成一条 record）。这保证了 tool_use↔tool_result 配对的
    无损往返——不像纯 role+content 文本导出那样会丢失 tool call 的关联关系。
    """
    dicts: list[dict[str, Any]] = []
    for rec in SessionRecord.from_message(message):
        data: dict[str, Any] = {"type": rec.type.value, "content": rec.content}
        if rec.tool_use_id is not None:
            data["tool_use_id"] = rec.tool_use_id
        if rec.type == RecordType.TOOL_RESULT:
            data["is_error"] = rec.is_error
            if rec.host_observation is not None:
                data["host_observation"] = rec.host_observation
            if rec.message_host_observation is not None:
                data["message_host_observation"] = rec.message_host_observation
        elif rec.message_host_observation is not None:
            data["host_observation"] = rec.message_host_observation
        dicts.append(data)
    return dicts


def make_compact_boundary(summary: str, keep: list[Message]) -> SessionRecord:
    """构建一条 COMPACT_BOUNDARY record，内联摘要和原样保留的 keep 尾部。

    `keep` 是 auto_compact 原样保留的近期尾部消息。将其存储在 boundary record
    内部（而不是依赖它在文件中的物理位置），意味着 resume 可以仅凭 boundary
    重建压缩后的状态——boundary 之前的原始前缀保留在磁盘上但不会被重放。
    """
    keep_dicts: list[dict[str, Any]] = []
    for msg in keep:
        if msg.transient:
            continue
        keep_dicts.extend(_message_to_record_dicts(msg))
    payload = {"summary": summary, "keep": keep_dicts}
    return SessionRecord(
        type=RecordType.COMPACT_BOUNDARY,
        content=payload,
        timestamp=datetime.now(timezone.utc),
    )


def parse_compact_boundary(record: SessionRecord) -> tuple[str, list[Message]]:
    """make_compact_boundary 的逆操作：返回 (summary, keep_messages)。

    对遗留或格式异常的 payload 降级返回 ("", [])，确保单条损坏的 boundary
    不会导致 resume 崩溃。
    """
    content = record.content
    if not isinstance(content, dict):
        return "", []
    summary = content.get("summary", "")
    keep_raw = content.get("keep", [])
    keep_records: list[SessionRecord] = []
    for item in keep_raw if isinstance(keep_raw, list) else []:
        if not isinstance(item, dict) or "type" not in item:
            continue
        try:
            keep_records.append(
                SessionRecord(
                    type=RecordType(item["type"]),
                    content=item.get("content"),
                    timestamp=record.timestamp,
                    tool_use_id=item.get("tool_use_id"),
                    is_error=item.get("is_error", False),
                    host_observation=(
                        _host_observation_snapshot(item.get("host_observation"))
                        if item.get("type") == RecordType.TOOL_RESULT.value else None
                    ),
                    message_host_observation=_host_observation_snapshot(
                        item.get("message_host_observation")
                        if item.get("type") == RecordType.TOOL_RESULT.value
                        else item.get("host_observation")
                    ),
                )
            )
        except ValueError:
            continue
    return summary, records_to_messages(keep_records)


# ---------------------------------------------------------------------------
# Record ↔ Message 转换
# ---------------------------------------------------------------------------


def records_to_messages(records: list[SessionRecord]) -> list[Message]:
    messages: list[Message] = []
    pending_tool_results: list[ToolResultBlock] = []
    pending_message_observation: dict[str, Any] | list[Any] | None = None

    for record in records:
        if record.type == RecordType.TOOL_RESULT:
            pending_tool_results.append(
                ToolResultBlock(
                    tool_use_id=record.tool_use_id or "",
                    content=(
                        record.content
                        if isinstance(record.content, str)
                        else json.dumps(record.content)
                    ),
                    is_error=record.is_error,
                    host_observation=_host_observation_snapshot(record.host_observation),
                )
            )
            if pending_message_observation is None:
                pending_message_observation = _host_observation_snapshot(
                    record.message_host_observation
                )
            continue

        if pending_tool_results:
            messages.append(
                Message(role="user", content="", tool_results=pending_tool_results)
                if pending_message_observation is None
                else Message(role="user", content="", tool_results=pending_tool_results,
                             host_observation=pending_message_observation)
            )
            pending_tool_results = []
            pending_message_observation = None

        if record.type == RecordType.SYSTEM_PROMPT:
            continue

        if record.type == RecordType.COMPRESSION:
            messages.append(
                Message(
                    role="user",
                    content="本次会话延续自之前的对话，因上下文空间不足进行了压缩。以下是早期对话的摘要：\n\n" + (record.content or ""),
                )
            )
            continue

        if record.type == RecordType.COMPACT_BOUNDARY:
            # 内联展开：摘要作为 user 消息，后接原样保留的 keep 尾部。
            # resume() 通常已预裁剪到最后一个 boundary，所以这里只会处理
            # 权威的那一条；但在此展开可以保证 records_to_messages 对任何
            # 直接调用者都保持自洽。
            summary, keep_messages = parse_compact_boundary(record)
            messages.append(Message(role="user", content="本次会话延续自之前的对话，因上下文空间不足进行了压缩。以下是早期对话的摘要：\n\n" + summary))
            messages.extend(keep_messages)
            continue

        if record.type == RecordType.USER:
            if isinstance(record.content, list):
                text_parts: list[str] = []
                content_blocks: list[dict[str, Any]] = []
                for block in record.content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        text_parts.append(str(block.get("text", "")))
                    elif block.get("type") == "attachment":
                        content_blocks.append(dict(block))
                messages.append(Message(
                    role="user",
                    content="\n".join(part for part in text_parts if part),
                    content_blocks=content_blocks,
                    host_observation=_host_observation_snapshot(record.message_host_observation),
                ))
            else:
                messages.append(Message(role="user", content=record.content or "",
                    host_observation=_host_observation_snapshot(record.message_host_observation)))
        elif record.type == RecordType.ASSISTANT:
            if isinstance(record.content, list):
                text = ""
                tool_uses: list[ToolUseBlock] = []
                thinking_blocks: list[ThinkingBlock] = []
                for block in record.content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        text += block.get("text", "")
                    elif block.get("type") == "thinking":
                        thinking_blocks.append(
                            ThinkingBlock(
                                thinking=str(block.get("thinking", "")),
                                signature=str(block.get("signature", "")),
                                kind=str(block.get("kind", "auto")),
                            )
                        )
                    elif block.get("type") == "tool_use":
                        tool_uses.append(
                            ToolUseBlock(
                                tool_use_id=block.get("id", ""),
                                tool_name=block.get("name", ""),
                                arguments=block.get("input", {}),
                            )
                        )
                messages.append(
                    Message(
                        role="assistant",
                        content=text,
                        tool_uses=tool_uses,
                        thinking_blocks=thinking_blocks,
                        host_observation=_host_observation_snapshot(record.message_host_observation),
                    )
                )
            else:
                messages.append(
                    Message(role="assistant", content=record.content or "",
                        host_observation=_host_observation_snapshot(record.message_host_observation))
                )

    if pending_tool_results:
        messages.append(
            Message(role="user", content="", tool_results=pending_tool_results,
                    host_observation=pending_message_observation)
        )

    return messages


# ---------------------------------------------------------------------------
# 消息链校验
# ---------------------------------------------------------------------------


def validate_message_chain(records: list[SessionRecord]) -> int:
    last_valid = 0
    pending_tool_uses: set[str] = set()

    for i, record in enumerate(records):
        if record.type == RecordType.ASSISTANT and isinstance(record.content, list):
            for block in record.content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_id = block.get("id", "")
                    if tool_id:
                        pending_tool_uses.add(tool_id)

        if record.type == RecordType.TOOL_RESULT and record.tool_use_id:
            pending_tool_uses.discard(record.tool_use_id)

        if not pending_tool_uses:
            last_valid = i + 1

    return last_valid


# ---------------------------------------------------------------------------
# SessionMeta
# ---------------------------------------------------------------------------


@dataclass
class SessionMeta:
    id: str
    title: str = ""
    summary: str = ""
    message_count: int = 0
    total_tokens: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_active: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    dialogue_focus: dict = field(default_factory=dict)
    continuous_task: dict = field(default_factory=dict)

    def save(self, path: Path) -> None:
        with file_lock(path):
            self._save(path)

    def _save(self, path: Path) -> None:
        data = {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "message_count": self.message_count,
            "total_tokens": self.total_tokens,
            "dialogue_focus": self.dialogue_focus,
            "continuous_task": self.continuous_task,
            "created_at": self.created_at.isoformat(),
            "last_active": self.last_active.isoformat(),
        }
        # Meta is written on every append; a crash mid-write would truncate it
        # and list()/resume() would silently drop the whole conversation.
        atomic_write_text(
            path, json.dumps(data, ensure_ascii=False, indent=2)
        )

    @classmethod
    def load(cls, path: Path) -> SessionMeta | None:
        try:
            with file_lock(path):
                data = json.loads(path.read_text(encoding="utf-8"))
            return cls(
                id=data["id"],
                title=data.get("title", ""),
                summary=data.get("summary", ""),
                message_count=data.get("message_count", 0),
                total_tokens=data.get("total_tokens", 0),
                dialogue_focus=data.get("dialogue_focus") if isinstance(data.get("dialogue_focus"), dict) else {},
                continuous_task=data.get("continuous_task") if isinstance(data.get("continuous_task"), dict) else {},
                created_at=datetime.fromisoformat(data["created_at"]),
                last_active=datetime.fromisoformat(data["last_active"]),
            )
        except (json.JSONDecodeError, KeyError, ValueError):
            return None


# ---------------------------------------------------------------------------
# Session（活跃会话句柄）
# ---------------------------------------------------------------------------


class SessionAppendMetadataError(OSError):
    """The log append succeeded; callers must not replay the append operation."""

    log_appended = True

    def __init__(self, path: Path, cause: OSError) -> None:
        super().__init__(
            f"消息日志已写入，但会话元数据保存失败；请勿重复发送或重放消息。"
            f"原元数据和日志已保留，需核对后仅重试元数据保存。目标：{path}；原因：{cause}"
        )


class Session:
    def __init__(
        self,
        session_id: str,
        file: IO[str],
        meta: SessionMeta,
        sessions_dir: Path,
    ) -> None:
        self.session_id = session_id
        self._file = file
        self.meta = meta
        self._sessions_dir = sessions_dir
        self._lock = file_lock(sessions_dir / f"{session_id}.meta")

    def append(self, message: Message) -> None:
        with self._lock:
            self._append(message)

    def _append(self, message: Message) -> None:
        if message.transient:
            return
        records = SessionRecord.from_message(message)
        for record in records:
            self._file.write(record.to_jsonl() + "\n")
        self._file.flush()

        self.meta.message_count += 1
        self.meta.last_active = datetime.now(timezone.utc)

        if not self.meta.title and message.role == "user" and message.content:
            self.meta.title = message.content[:TITLE_MAX_LENGTH]
        elif not self.meta.title and message.role == "user" and message.content_blocks:
            first = message.content_blocks[0]
            self.meta.title = (str(first.get("name") or "附件对话"))[:TITLE_MAX_LENGTH]

        self._save_after_append()

    def append_record(self, record: SessionRecord) -> None:
        """追加一条原始 SessionRecord（例如 compact_boundary 标记）。

        与 append() 不同，此方法不会更新 message_count/title——boundary 是
        结构性标记而非对话轮次。last_active 仍会更新，以保证 session 按最近
        使用排序。
        """
        with self._lock:
            self._file.write(record.to_jsonl() + "\n")
            self._file.flush()
            self.meta.last_active = datetime.now(timezone.utc)
            self._save_after_append()

    def _save_after_append(self) -> None:
        path = self._sessions_dir / f"{self.session_id}.meta"
        try:
            self.meta.save(path)
        except OSError as exc:
            raise SessionAppendMetadataError(path, exc) from exc

    def append_expert_run(
        self,
        run_id: str,
        *,
        status: str = "running",
        seq: int = 0,
        display_index: int | None = None,
    ) -> None:
        self.append_record(SessionRecord(
            type=RecordType.EXPERT_RUN,
            content={
                "run_id": run_id, "status": status, "last_seq": seq,
                "display_index": (
                    int(display_index)
                    if display_index is not None
                    else self.meta.message_count
                ),
            },
            timestamp=datetime.now(timezone.utc),
        ))


    def save_meta(self) -> None:
        """立即持久化 meta。

        append() 只在追加消息时顺带存 meta；对于消息之外的 meta 变更
        （如一轮结束后回写 total_tokens），若不显式保存会滞后一轮，
        会话在此期间关闭则直接丢失。"""
        with self._lock:
            self.meta.save(self._sessions_dir / f"{self.session_id}.meta")

    def close(self) -> None:
        with self._lock:
            if self._file and not self._file.closed:
                self._file.flush()
                self._file.close()


# ---------------------------------------------------------------------------
# ResumeResult
# ---------------------------------------------------------------------------


@dataclass
class ResumeResult:
    session: Session
    messages: list[Message]
    last_active: datetime
    expert_runs: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Session 摘要生成
# ---------------------------------------------------------------------------


async def generate_session_summary(
    client: Any, conversation: ConversationManager, protocol: str
) -> str:
    from mewcode.tools.base import StreamEnd, TextDelta

    recent = conversation.history[-10:]
    if not recent:
        return ""

    summary_conv = ConversationManager()
    summary_conv.history = [Message(role="user", content=SESSION_SUMMARY_PROMPT)]
    for msg in recent:
        summary_conv.history.append(msg)
    summary_conv.history.append(
        Message(role="user", content="请用一句话总结上面的对话内容。不要调用工具。")
    )

    collected = ""
    try:
        async for event in scheduled_client_stream(
            client, summary_conv, system=SESSION_SUMMARY_PROMPT
        ):
            if isinstance(event, TextDelta):
                collected += event.text
            elif isinstance(event, StreamEnd):
                pass
    except Exception:
        return ""

    return collected.strip()


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------


def _generate_session_id() -> str:
    now = datetime.now()
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"session_{now.strftime('%Y%m%d_%H%M%S')}_{suffix}"


class SessionManager:
    def __init__(self, work_dir: str) -> None:
        self._sessions_dir = Path(work_dir) / SESSIONS_DIR
        self._sessions_dir.mkdir(parents=True, exist_ok=True)


    def create(self) -> Session:
        session_id = _generate_session_id()
        jsonl_path = self._sessions_dir / f"{session_id}.jsonl"
        meta = SessionMeta(id=session_id)
        meta.save(self._sessions_dir / f"{session_id}.meta")

        file = open(jsonl_path, "a", encoding="utf-8")  # noqa: SIM115
        return Session(
            session_id=session_id,
            file=file,
            meta=meta,
            sessions_dir=self._sessions_dir,
        )


    def list(self) -> list[SessionMeta]:
        metas: list[SessionMeta] = []
        for meta_path in self._sessions_dir.glob("*.meta"):
            meta = SessionMeta.load(meta_path)
            if meta is not None:
                metas.append(meta)
        metas.sort(key=lambda m: m.last_active, reverse=True)
        return metas

    def resume(
        self,
        session_id: str,
        cached_context: tuple[list[Message], float] | None = None,
    ) -> ResumeResult | None:
        jsonl_path = self._sessions_dir / f"{session_id}.jsonl"
        meta_path = self._sessions_dir / f"{session_id}.meta"

        if not jsonl_path.exists():
            return None

        meta = SessionMeta.load(meta_path)
        if meta is None:
            return None

        cached_messages: list[Message] | None = None
        if cached_context is not None:
            candidate_messages, cached_at = cached_context
            # A previous append may have reached JSONL before meta replacement
            # failed. Never hide those durable messages behind a stale cache.
            if (candidate_messages and cached_at >= meta.last_active.timestamp()
                    and jsonl_path.stat().st_mtime_ns <= meta_path.stat().st_mtime_ns):
                cached_messages = candidate_messages

        records: list[SessionRecord] = []
        if cached_messages is None:
            with open(jsonl_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    record = SessionRecord.from_jsonl(line)
                    if record is not None:
                        records.append(record)

        expert_runs = [
            dict(record.content)
            for record in records
            if record.type == RecordType.EXPERT_RUN
            and isinstance(record.content, dict)
        ]
        if cached_messages is not None:
            with open(jsonl_path, encoding="utf-8") as source:
                for line in source:
                    record = SessionRecord.from_jsonl(line.strip()) if line.strip() else None
                    if (
                        record is not None
                        and record.type == RecordType.EXPERT_RUN
                        and isinstance(record.content, dict)
                    ):
                        expert_runs.append(dict(record.content))

        # 重建压缩后的状态：仅从最后一个 compact_boundary 开始重放。
        # 该标记之前的 record 是已被摘要过的原始前缀——保留在磁盘上供审计，
        # 但不再重放。标记本身内联了摘要 + 原样 keep 尾部，标记之后追加的
        # 普通消息（续写）照常重放。没有 boundary 则全量重放（兼容旧 session）。
        if cached_messages is None:
            last_boundary = -1
            for i, rec in enumerate(records):
                if rec.type == RecordType.COMPACT_BOUNDARY:
                    last_boundary = i
            if last_boundary >= 0:
                records = records[last_boundary:]

            # Preserve later user turns and repair interrupted tool batches in
            # memory. Older versions truncated the entire tail at the first
            # missing result, which lost valid follow-up messages.
            messages = records_to_messages(records)
            messages, _ = repair_tool_call_chain(messages)
        else:
            messages, _ = repair_tool_call_chain(cached_messages)

        restored_conversation = ConversationManager()
        restored_conversation.restore_history(messages)
        messages = restored_conversation.history

        file = open(jsonl_path, "a", encoding="utf-8")  # noqa: SIM115
        session = Session(
            session_id=session_id,
            file=file,
            meta=meta,
            sessions_dir=self._sessions_dir,
        )

        return ResumeResult(
            session=session,
            messages=messages,
            last_active=meta.last_active,
            expert_runs=expert_runs,
        )

    def delete(self, session_id: str) -> bool:
        jsonl_path = self._sessions_dir / f"{session_id}.jsonl"
        meta_path = self._sessions_dir / f"{session_id}.meta"

        deleted = False
        if jsonl_path.exists():
            jsonl_path.unlink()
            deleted = True
        if meta_path.exists():
            meta_path.unlink()
            deleted = True
        return deleted

    def cleanup(self, max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        removed = 0

        for meta_path in list(self._sessions_dir.glob("*.meta")):
            meta = SessionMeta.load(meta_path)
            if meta is not None and meta.last_active < cutoff:
                self.delete(meta.id)
                removed += 1

        return removed
