"""Durable, provider-neutral turn events for the web conversation pipeline.

The chat renderer used to infer order from several independent websocket
messages.  This module records the ordered facts once at the runtime boundary
so live clients, reconnects, and historical views can use the same projection.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Iterator

log = logging.getLogger(__name__)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class PipelineEventStore:
    """Append-only event log next to a session's normal message log."""

    path: Path | None = None
    _seq: int = 0
    _events: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def for_session(cls, session: Any) -> "PipelineEventStore":
        sessions_dir = getattr(session, "_sessions_dir", None)
        session_id = str(getattr(session, "session_id", "") or "")
        path = (
            Path(sessions_dir) / f"{session_id}.pipeline.jsonl"
            if sessions_dir and session_id
            else None
        )
        store = cls(path=path)
        if path is not None:
            store._load()
        return store

    @classmethod
    def from_path(cls, path: Path) -> "PipelineEventStore":
        store = cls(path=path)
        store._load()
        return store

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            with self.path.open(encoding="utf-8") as source:
                for line in source:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    try:
                        seq = int(event.get("seq") or 0)
                    except (TypeError, ValueError):
                        continue
                    self._events.append(event)
                    self._seq = max(self._seq, seq)
        except OSError:
            log.warning("failed to read pipeline events from %s", self.path)

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        self._seq += 1
        record = {"type": "pipeline_item", "seq": self._seq, **event}
        self._events.append(record)
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as target:
                    target.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError:
                # The in-memory event remains available to the live websocket.
                log.warning("failed to persist pipeline event to %s", self.path)
        return record

    def events(self) -> list[dict[str, Any]]:
        return [dict(event) for event in self._events]


@dataclass
class _OpenItem:
    item_id: str
    role: str
    kind: str
    name: str
    started_at: str
    started_monotonic: float


PublishEvent = Callable[[dict[str, Any]], Awaitable[None]]

# Streaming deltas are extremely fine-grained: a single long thinking block can
# arrive as tens of thousands of separate chunks.  Recording each one as its own
# durable line is what turned one conversation's log into 1.02M lines / 475 MB
# and made the history view unopenable.  Buffering keeps the durable log
# proportional to the number of *items* rather than to the number of tokens,
# while the renderer still reconstructs identical text because chunks are
# concatenated in arrival order.
CHUNK_FLUSH_CHARS = 2048
CHUNK_FLUSH_SECONDS = 1.0


class PipelineRecorder:
    """Translate Agent events into DSH-style item lifecycle events."""

    def __init__(
        self,
        *,
        conversation_id: str,
        store: PipelineEventStore,
        publish: PublishEvent | None = None,
        flush_chars: int = CHUNK_FLUSH_CHARS,
        flush_seconds: float = CHUNK_FLUSH_SECONDS,
        task_id: str = "",
        observe: PublishEvent | None = None,
    ) -> None:
        self.conversation_id = conversation_id
        self.store = store
        self.publish = publish
        self.observe = observe
        self.task_id = task_id
        self.turn_id = uuid.uuid4().hex
        self._open: dict[str, _OpenItem] = {}
        self._active_assistant: str | None = None
        self._item_counter = 0
        self._finished = False
        self._flush_chars = max(1, int(flush_chars))
        self._flush_seconds = max(0.0, float(flush_seconds))
        # item_id -> text streamed but not yet written to the durable log
        self._pending: dict[str, str] = {}
        self._pending_meta: dict[str, dict[str, Any]] = {}
        self._last_flush = time.monotonic()

    async def _emit(self, **payload: Any) -> dict[str, Any]:
        record = self.store.append({
            "conversation_id": self.conversation_id,
            "turn_id": self.turn_id,
            "task_id": self.task_id,
            "timestamp": _timestamp(),
            **payload,
        })
        if self.observe is not None:
            try:
                await self.observe(record)
            except Exception:
                log.exception("pipeline activity observer failed")
        if self.publish is not None:
            try:
                await self.publish(record)
            except Exception:
                # A disconnected renderer must never cancel the Agent.
                log.debug("pipeline websocket publish failed", exc_info=True)
        return record

    async def begin_turn(
        self, text: str, *, attachments: list[dict[str, Any]] | None = None
    ) -> None:
        now = _timestamp()
        await self._emit(
            item_id=f"user-{self.turn_id}",
            parent_id=None,
            role="user",
            kind="message",
            phase="complete",
            status="completed",
            started_at=now,
            completed_at=now,
            duration_ms=0,
            name="user",
            text=text,
            meta={"attachments": attachments or []},
        )

    async def lifecycle(self, status: str, text: str) -> None:
        """Persist each transition separately so waits/resumes survive replay."""
        self._item_counter += 1
        now = _timestamp()
        await self._emit(item_id=f"state-{self.turn_id}-{self._item_counter}",
                         parent_id=None, role="system", kind="lifecycle", name="执行状态",
                         phase="complete", status=status, text=text,
                         started_at=now, completed_at=now, duration_ms=0)

    async def _close_assistant(self) -> None:
        if self._active_assistant is not None:
            await self.complete(self._active_assistant)
            self._active_assistant = None

    async def _ensure_assistant(self, kind: str) -> str:
        if self._active_assistant is not None:
            current = self._open.get(self._active_assistant)
            if current is not None and current.kind == kind:
                return current.item_id
            await self._close_assistant()
        self._item_counter += 1
        item_id = f"assistant-{self.turn_id}-{self._item_counter}"
        await self.start(
            item_id=item_id,
            role="assistant",
            kind=kind,
            name="assistant",
        )
        self._active_assistant = item_id
        return item_id

    async def assistant_text(self, text: str) -> None:
        item_id = await self._ensure_assistant("message")
        await self.chunk(item_id, text=text)

    async def assistant_thinking(self, text: str) -> None:
        item_id = await self._ensure_assistant("thinking")
        await self.chunk(item_id, text=text)

    async def tool_start(
        self, tool_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> None:
        await self._close_assistant()
        await self.start(
            item_id=tool_id,
            role="tool",
            kind="call",
            name=tool_name,
            text="",
            meta={"arguments": arguments},
        )

    async def tool_result(
        self,
        tool_id: str,
        output: str,
        *,
        is_error: bool,
        elapsed: float,
    ) -> None:
        await self.chunk(
            tool_id,
            text=output[:4000],
            meta={
                "is_error": is_error,
                "execution_duration_ms": round(max(0.0, elapsed) * 1000),
            },
        )
        await self.complete(
            tool_id,
            status="failed" if is_error else "completed",
            duration_ms=round(max(0.0, elapsed) * 1000),
        )

    async def start(
        self,
        *,
        item_id: str,
        role: str,
        kind: str,
        name: str = "",
        text: str = "",
        meta: dict[str, Any] | None = None,
    ) -> None:
        if item_id in self._open:
            return
        self._item_counter += 1
        now = _timestamp()
        self._open[item_id] = _OpenItem(
            item_id=item_id,
            role=role,
            kind=kind,
            name=name,
            started_at=now,
            started_monotonic=time.monotonic(),
        )
        await self._emit(
            item_id=item_id,
            parent_id=None,
            role=role,
            kind=kind,
            phase="start",
            status="running",
            started_at=now,
            completed_at=None,
            duration_ms=None,
            name=name,
            text=text,
            meta=meta or {},
        )

    async def chunk(
        self,
        item_id: str,
        *,
        text: str = "",
        meta: dict[str, Any] | None = None,
    ) -> None:
        item = self._open.get(item_id)
        if item is None:
            return
        if meta:
            pending_meta = self._pending_meta.setdefault(item.item_id, {})
            pending_meta.update(meta)
        if text:
            self._pending[item.item_id] = (
                self._pending.get(item.item_id, "") + str(text)
            )
        await self._flush_if_due(item)

    def _take_pending(self, item_id: str) -> tuple[str, dict[str, Any]]:
        """Drain buffered delta text/meta for one item."""

        text = self._pending.pop(item_id, "")
        meta = self._pending_meta.pop(item_id, {})
        return text, meta

    async def _flush_if_due(self, item: _OpenItem) -> None:
        """Persist buffered text once it is worth a durable line."""

        buffered = self._pending.get(item.item_id, "")
        if not buffered:
            return
        elapsed = time.monotonic() - self._last_flush
        if len(buffered) < self._flush_chars and elapsed < self._flush_seconds:
            return
        await self._flush_chunk(item)

    async def _flush_chunk(self, item: _OpenItem) -> None:
        text, meta = self._take_pending(item.item_id)
        if not text and not meta:
            return
        self._last_flush = time.monotonic()
        await self._emit(
            item_id=item.item_id,
            parent_id=None,
            role=item.role,
            kind=item.kind,
            phase="chunk",
            status="running",
            started_at=item.started_at,
            completed_at=None,
            duration_ms=None,
            name=item.name,
            text=text,
            meta=meta,
        )

    async def complete(
        self,
        item_id: str,
        *,
        status: str = "completed",
        duration_ms: int | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        item = self._open.pop(item_id, None)
        if item is None:
            # Never leak buffered text for an item that already closed.
            self._take_pending(item_id)
            return
        if duration_ms is None:
            duration_ms = round(max(0.0, time.monotonic() - item.started_monotonic) * 1000)
        # Fold the unflushed tail into the terminal record instead of emitting a
        # separate chunk line: the renderer concatenates text regardless of
        # phase, so the reconstructed item is identical and cheaper to store.
        tail_text, tail_meta = self._take_pending(item.item_id)
        merged_meta = {**tail_meta, **(meta or {})}
        await self._emit(
            item_id=item.item_id,
            parent_id=None,
            role=item.role,
            kind=item.kind,
            phase="complete",
            status=status,
            started_at=item.started_at,
            completed_at=_timestamp(),
            duration_ms=duration_ms,
            name=item.name,
            text=tail_text,
            meta=merged_meta,
        )

    async def finish(self, *, interrupted: bool = False, error: str | None = None) -> None:
        if self._finished:
            return
        self._finished = True
        await self._close_assistant()
        status = "failed" if error else "interrupted" if interrupted else "completed"
        for item_id in list(self._open):
            await self.complete(item_id, status=status)


def load_pipeline_events(sessions_dir: str | Path, session_id: str) -> list[dict[str, Any]]:
    path = Path(sessions_dir) / f"{session_id}.pipeline.jsonl"
    return PipelineEventStore.from_path(path).events()


# ---------------------------------------------------------------------------
# Folding / compaction
#
# The durable log records every streaming delta as its own line, so a single
# long thinking block can own tens of thousands of lines.  Clients only ever
# need each item's final projection, because the renderer rebuilds an item by
# accumulating `text` and letting later values win for the scalar fields.  The
# functions below reproduce that projection while collapsing the log to one
# record per item, which keeps the history view proportional to the number of
# items instead of the number of tokens.
# ---------------------------------------------------------------------------

# Above this size a session's log is folded and rewritten on first read.
PIPELINE_COMPACT_BYTES = 32 * 1024 * 1024

_SCALAR_LAST_KEYS = ("turn_id", "task_id", "role", "kind", "name", "phase", "status")


def _iter_log_records(path: Path) -> Iterator[dict[str, Any]]:
    """Yield usable records, skipping blank and corrupt lines."""

    try:
        with path.open(encoding="utf-8") as source:
            for line in source:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    yield record
    except OSError:
        log.warning("failed to read pipeline events from %s", path)


def fold_pipeline_items(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse an append-only log into one record per item.

    The result replays to exactly the same item state a client would build from
    the full log: text is concatenated in arrival order, ``started_at`` keeps its
    first value, ``completed_at``/``duration_ms``/``seq`` keep their last
    non-null value, ``meta`` is shallow-merged in order, and the remaining
    scalars take their last truthy value.
    """

    items: dict[str, dict[str, Any]] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("type") not in (None, "pipeline_item"):
            continue
        item_id = str(event.get("item_id") or event.get("id") or "")
        if not item_id:
            continue
        item = items.get(item_id)
        if item is None:
            item = {
                "type": "pipeline_item",
                "conversation_id": event.get("conversation_id") or "",
                "item_id": item_id,
                "parent_id": event.get("parent_id", None),
                "text": "",
                "meta": {},
                "started_at": event.get("started_at") or event.get("timestamp"),
                "completed_at": None,
                "duration_ms": None,
                "seq": 0,
                "synthetic": bool(event.get("synthetic")),
            }
            items[item_id] = item
        if "parent_id" in event:
            item["parent_id"] = event["parent_id"]
        for key in _SCALAR_LAST_KEYS:
            value = event.get(key)
            if value:
                item[key] = value
        if event.get("conversation_id"):
            item["conversation_id"] = event["conversation_id"]
        started = event.get("started_at") or event.get("timestamp")
        if started and not item.get("started_at"):
            item["started_at"] = started
        if event.get("completed_at"):
            item["completed_at"] = event["completed_at"]
        if event.get("duration_ms") is not None:
            item["duration_ms"] = event["duration_ms"]
        if event.get("seq") is not None:
            item["seq"] = event["seq"]
        text = event.get("text")
        if text:
            item["text"] += str(text)
        meta = event.get("meta")
        if isinstance(meta, dict) and meta:
            item["meta"] = {**item["meta"], **meta}
    # The renderer ignores records whose seq is not strictly increasing, so the
    # payload has to be ordered by that same key.
    return sorted(items.values(), key=lambda item: item.get("seq") or 0)


def read_pipeline_items(path: Path) -> list[dict[str, Any]]:
    """Fold a log straight from disk without materialising its raw lines."""

    if not path.exists():
        return []
    return fold_pipeline_items(_iter_log_records(path))


def compact_pipeline_log(path: str | Path, *, keep_backup: bool = True) -> dict[str, Any]:
    """Rewrite an oversized append-only log in folded form.

    Folding is lossless for every consumer of this log: the client-side
    projection rebuilt from the compacted file is identical to the one rebuilt
    from the original.  The original is kept beside it unless ``keep_backup`` is
    false, and the swap is a same-volume rename so an interrupted run cannot
    destroy the only copy.
    """

    path = Path(path)
    stats: dict[str, Any] = {
        "path": str(path),
        "before_bytes": 0,
        "after_bytes": 0,
        "before_lines": 0,
        "after_items": 0,
        "compacted": False,
        "backup": None,
    }
    if not path.exists():
        return stats
    stats["before_bytes"] = path.stat().st_size

    before_lines = 0

    def counted(records: Iterator[dict[str, Any]]) -> Iterator[dict[str, Any]]:
        nonlocal before_lines
        for record in records:
            before_lines += 1
            yield record

    folded = fold_pipeline_items(counted(_iter_log_records(path)))
    stats["before_lines"] = before_lines
    stats["after_items"] = len(folded)
    if not folded:
        return stats

    tmp_path = path.with_name(path.name + ".compact-tmp")
    with tmp_path.open("w", encoding="utf-8") as target:
        for record in folded:
            target.write(json.dumps(record, ensure_ascii=False) + "\n")
    stats["after_bytes"] = tmp_path.stat().st_size

    backup_path: Path | None = None
    if keep_backup:
        backup_path = path.with_name(path.name + ".bak")
        if backup_path.exists():
            backup_path = path.with_name(
                f"{path.name}.bak-{datetime.now(timezone.utc):%Y%m%d%H%M%S}"
            )
    try:
        if backup_path is not None:
            path.replace(backup_path)
        tmp_path.replace(path)
    except OSError:
        # Put the original back rather than leaving a half-swapped pair.
        if backup_path is not None and backup_path.exists() and not path.exists():
            backup_path.replace(path)
        tmp_path.unlink(missing_ok=True)
        log.warning("failed to compact pipeline log %s", path, exc_info=True)
        return stats

    stats["compacted"] = True
    stats["backup"] = str(backup_path) if backup_path is not None else None
    return stats


def load_pipeline_items(
    sessions_dir: str | Path,
    session_id: str,
    *,
    compact_bytes: int = PIPELINE_COMPACT_BYTES,
) -> list[dict[str, Any]]:
    """Read a conversation's trajectory in folded form.

    Oversized logs are compacted in place before reading so a single runaway
    conversation cannot keep the history view from opening.
    """

    path = Path(sessions_dir) / f"{session_id}.pipeline.jsonl"
    if not path.exists():
        return []
    try:
        oversized = path.stat().st_size >= compact_bytes
    except OSError:
        oversized = False
    if oversized:
        compact_pipeline_log(path)
    return read_pipeline_items(path)


def legacy_entries_to_pipeline(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build a zero-duration projection for sessions created before the log.

    This projection is intentionally not written back to disk: it has no timing
    facts, but lets the new UI remain useful while old sessions are migrated by
    their next live turn.
    """
    events: list[dict[str, Any]] = []
    seq = 0
    current_turn_id: str | None = None

    def add(
        *,
        turn_id: str,
        item_id: str,
        role: str,
        kind: str,
        phase: str,
        status: str,
        name: str,
        text: str = "",
        meta: dict[str, Any] | None = None,
    ) -> None:
        nonlocal seq
        seq += 1
        now = "legacy"
        events.append({
            "type": "pipeline_item",
            "synthetic": True,
            "seq": seq,
            "conversation_id": "",
            "turn_id": turn_id,
            "item_id": item_id,
            "parent_id": None,
            "role": role,
            "kind": kind,
            "phase": phase,
            "status": status,
            "started_at": now,
            "completed_at": now if phase == "complete" else None,
            "duration_ms": 0 if phase == "complete" else None,
            "name": name,
            "text": text,
            "meta": meta or {},
        })

    for index, entry in enumerate(entries):
        role = str(entry.get("role") or "")
        if role == "user":
            current_turn_id = f"legacy-turn-{index}"
            add(
                turn_id=current_turn_id,
                item_id=f"legacy-user-{index}",
                role="user",
                kind="message",
                phase="complete",
                status="completed",
                name="user",
                text=str(entry.get("text") or ""),
                meta={"attachments": entry.get("attachments") or []},
            )
            continue
        if role != "assistant":
            continue
        turn_id = current_turn_id or f"legacy-turn-{index}"
        current_turn_id = turn_id
        text = str(entry.get("text") or "")
        if text:
            item_id = f"legacy-assistant-{index}"
            add(turn_id=turn_id, item_id=item_id, role="assistant", kind="message", phase="start", status="running", name="assistant")
            add(turn_id=turn_id, item_id=item_id, role="assistant", kind="message", phase="chunk", status="running", name="assistant", text=text)
            add(turn_id=turn_id, item_id=item_id, role="assistant", kind="message", phase="complete", status="completed", name="assistant")
        for tool_index, tool in enumerate(entry.get("tools") or []):
            item_id = f"legacy-tool-{index}-{tool_index}"
            name = str(tool.get("name") or "tool")
            add(
                turn_id=turn_id,
                item_id=item_id,
                role="tool",
                kind="call",
                phase="start",
                status="running",
                name=name,
                meta={"arguments": tool.get("args") or {}},
            )
            result = str(tool.get("result") or "")
            if result:
                add(turn_id=turn_id, item_id=item_id, role="tool", kind="call", phase="chunk", status="running", name=name, text=result)
            add(
                turn_id=turn_id,
                item_id=item_id,
                role="tool",
                kind="call",
                phase="complete",
                status="failed" if tool.get("is_error") else "completed",
                name=name,
            )
    return events
