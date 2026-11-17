from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

from mewcode.config import RedisConfig
from mewcode.conversation import Message, ThinkingBlock, ToolResultBlock, ToolUseBlock


log = logging.getLogger(__name__)
MAX_CONTEXT_PAYLOAD_BYTES = 4_000_000


class RedisContextCache:
    """Optional Redis acceleration for ephemeral context and recall results."""

    def __init__(self, config: RedisConfig, project_root: str) -> None:
        self.config = config
        self._project_id = hashlib.sha256(
            str(Path(project_root).resolve()).encode("utf-8")
        ).hexdigest()[:16]
        self._client: Any = None
        self._retry_after = 0.0

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def _key(self, category: str, identifier: str) -> str:
        return f"{self.config.key_prefix}:{category}:{self._project_id}:{identifier}"

    def _mark_failure(self, exc: Exception) -> None:
        self._retry_after = time.monotonic() + 30.0
        self._client = None
        log.debug("Redis cache unavailable, falling back to durable storage: %s", exc)

    def _get_client(self) -> Any | None:
        if not self.enabled or time.monotonic() < self._retry_after:
            return None
        if self._client is not None:
            return self._client
        try:
            from redis import Redis

            self._client = Redis.from_url(
                self.config.url,
                decode_responses=True,
                socket_connect_timeout=self.config.socket_timeout_seconds,
                socket_timeout=self.config.socket_timeout_seconds,
                health_check_interval=30,
            )
            self._client.ping()
            return self._client
        except Exception as exc:
            self._mark_failure(exc)
            return None

    def health(self) -> bool:
        client = self._get_client()
        if client is None:
            return False
        try:
            return bool(client.ping())
        except Exception as exc:
            self._mark_failure(exc)
            return False

    @staticmethod
    def _serialize_message(message: Message) -> dict[str, Any]:
        return {
            "role": message.role,
            "content": message.content,
            "tool_uses": [
                {
                    "tool_use_id": block.tool_use_id,
                    "tool_name": block.tool_name,
                    "arguments": block.arguments,
                }
                for block in message.tool_uses
            ],
            "tool_results": [
                {
                    "tool_use_id": block.tool_use_id,
                    "content": block.content,
                    "is_error": block.is_error,
                }
                for block in message.tool_results
            ],
            "thinking_blocks": [
                {
                    "thinking": block.thinking,
                    "signature": block.signature,
                    "kind": block.kind,
                }
                for block in message.thinking_blocks
            ],
        }

    @staticmethod
    def _deserialize_message(data: dict[str, Any]) -> Message:
        return Message(
            role=str(data.get("role", "user")),
            content=str(data.get("content", "")),
            tool_uses=[
                ToolUseBlock(
                    tool_use_id=str(block.get("tool_use_id", "")),
                    tool_name=str(block.get("tool_name", "")),
                    arguments=block.get("arguments", {}),
                )
                for block in data.get("tool_uses", [])
                if isinstance(block, dict)
            ],
            tool_results=[
                ToolResultBlock(
                    tool_use_id=str(block.get("tool_use_id", "")),
                    content=str(block.get("content", "")),
                    is_error=bool(block.get("is_error", False)),
                )
                for block in data.get("tool_results", [])
                if isinstance(block, dict)
            ],
            thinking_blocks=[
                ThinkingBlock(
                    thinking=str(block.get("thinking", "")),
                    signature=str(block.get("signature", "")),
                    kind=str(block.get("kind", "auto")),
                )
                for block in data.get("thinking_blocks", [])
                if isinstance(block, dict)
            ],
        )

    def _bounded_messages(self, messages: list[Message]) -> list[Message]:
        if len(messages) <= self.config.max_messages:
            return messages
        tail = messages[-self.config.max_messages :]
        # Avoid starting a restored conversation with orphaned tool results.
        for index, message in enumerate(tail):
            if message.role == "user" and message.content and not message.tool_results:
                return tail[index:]
        return tail

    def store_context(self, session_id: str, messages: list[Message]) -> bool:
        client = self._get_client()
        if client is None:
            return False
        messages = [message for message in messages if not message.transient]
        payload = {
            "version": 1,
            "saved_at": time.time(),
            "messages": [
                self._serialize_message(message)
                for message in self._bounded_messages(messages)
            ],
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_CONTEXT_PAYLOAD_BYTES:
            return False
        try:
            client.set(
                self._key("context", session_id),
                encoded,
                ex=self.config.context_ttl_seconds,
            )
            return True
        except Exception as exc:
            self._mark_failure(exc)
            return False

    def load_context(self, session_id: str) -> tuple[list[Message], float] | None:
        client = self._get_client()
        if client is None:
            return None
        try:
            raw = client.get(self._key("context", session_id))
            if not raw:
                return None
            payload = json.loads(raw)
            messages = payload.get("messages", [])
            if payload.get("version") != 1 or not isinstance(messages, list):
                return None
            restored = [
                self._deserialize_message(message)
                for message in messages
                if isinstance(message, dict)
            ]
            return restored, float(payload.get("saved_at", 0.0))
        except (AttributeError, json.JSONDecodeError, TypeError, ValueError):
            return None
        except Exception as exc:
            self._mark_failure(exc)
            return None

    def delete_context(self, session_id: str) -> None:
        client = self._get_client()
        if client is None:
            return
        try:
            client.delete(self._key("context", session_id))
        except Exception as exc:
            self._mark_failure(exc)

    def _recall_key(self, query: str) -> str:
        query_hash = hashlib.sha256(query.strip().casefold().encode("utf-8")).hexdigest()
        return self._key("recall", query_hash)

    def get_recall(self, query: str) -> tuple[str, list[str]] | None:
        client = self._get_client()
        if client is None:
            return None
        try:
            raw = client.get(self._recall_key(query))
            if not raw:
                return None
            payload = json.loads(raw)
            reminder = payload.get("reminder", "")
            memory_ids = payload.get("memory_ids", [])
            if not isinstance(reminder, str) or not isinstance(memory_ids, list):
                return None
            return reminder, [item for item in memory_ids if isinstance(item, str)]
        except (AttributeError, json.JSONDecodeError, TypeError):
            return None
        except Exception as exc:
            self._mark_failure(exc)
            return None

    def set_recall(self, query: str, reminder: str, memory_ids: list[str]) -> bool:
        client = self._get_client()
        if client is None:
            return False
        try:
            client.set(
                self._recall_key(query),
                json.dumps(
                    {"reminder": reminder, "memory_ids": memory_ids},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                ex=self.config.recall_ttl_seconds,
            )
            return True
        except Exception as exc:
            self._mark_failure(exc)
            return False
