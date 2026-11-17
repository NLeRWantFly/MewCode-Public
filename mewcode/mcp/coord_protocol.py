"""Shared protocol primitives for the Codex/Grok coordination broker."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

PROTOCOL_VERSION = "1"
HEARTBEAT_INTERVAL_SECONDS = 15
LEASE_SECONDS = 45
MAX_EXECUTION_TURNS = 3
MAX_RETRIES = 1
NO_PROGRESS_LIMIT = 2
APPROVAL_MODEL_ID = "codex-auto-review"
EXTERNAL_EXECUTOR_MODEL_ID = "grok-4.6"


class CoordinationError(Exception):
    """Expected protocol error returned as a structured MCP tool result."""


class TaskState(StrEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    EXPIRED = "expired"


class TaskKind(StrEnum):
    EXECUTION = "execution"
    VERIFICATION = "verification"


class WriteMode(StrEnum):
    READ_ONLY = "read_only"
    SCOPED_WRITE = "scoped_write"


TERMINAL_STATES = {
    TaskState.COMPLETED,
    TaskState.FAILED,
    TaskState.BLOCKED,
    TaskState.EXPIRED,
}

ROLE_DEFINITIONS: dict[str, dict[str, str]] = {
    "planner": {
        "agent_id": "codex-planner-01",
        "model_id": APPROVAL_MODEL_ID,
        "description": "由 Codex Auto Review 托管的需求拆解、任务创建和边界定义",
    },
    "executor": {
        "agent_id": "grok-executor-01",
        "model_id": EXTERNAL_EXECUTOR_MODEL_ID,
        "description": "范围明确的搜索、实现、测试和证据收集",
    },
    "verifier": {
        "agent_id": "codex-verifier-01",
        "model_id": APPROVAL_MODEL_ID,
        "description": "由 Codex Auto Review 托管的证据读取、验证任务创建和最终验收",
    },
}


def now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_capabilities(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return a stable, non-secret capability representation."""

    if not isinstance(manifest, dict):
        raise CoordinationError("capabilities manifest must be an object")

    def normalize_entries(key: str) -> list[dict[str, Any]]:
        raw = manifest.get(key, [])
        if not isinstance(raw, list):
            raise CoordinationError(f"capabilities.{key} must be a list")
        entries: list[dict[str, Any]] = []
        for item in raw:
            if isinstance(item, str):
                item = {"name": item}
            if not isinstance(item, dict) or not str(item.get("name", "")).strip():
                raise CoordinationError(f"capabilities.{key} entries need a name")
            entries.append(dict(item))
        entries.sort(key=lambda item: str(item.get("name", "")))
        return entries

    normalized: dict[str, Any] = {
        "agent_id": str(manifest.get("agent_id", "")),
        "model_id": str(manifest.get("model_id", "")),
        "role": str(manifest.get("role", "")),
        "tools": normalize_entries("tools"),
        "skills": normalize_entries("skills"),
        "mcp_servers": normalize_entries("mcp_servers"),
        "source": str(manifest.get("source", "runtime")),
    }
    if not normalized["agent_id"] or not normalized["model_id"]:
        raise CoordinationError("capabilities requires agent_id and model_id")
    if normalized["role"] not in ROLE_DEFINITIONS:
        raise CoordinationError(f"unknown agent role: {normalized['role']}")
    return normalized


def capabilities_hash(manifest: dict[str, Any]) -> str:
    return sha256_text(canonical_json(normalize_capabilities(manifest)))


def capability_names(manifest: dict[str, Any], key: str) -> set[str]:
    return {
        str(item.get("name"))
        for item in normalize_capabilities(manifest).get(key, [])
    }


def validate_write_mode(value: str) -> WriteMode:
    try:
        return WriteMode(value)
    except ValueError as exc:
        raise CoordinationError(
            f"write_mode must be one of {[item.value for item in WriteMode]}"
        ) from exc


def validate_task_limits(max_turns: int) -> int:
    if not isinstance(max_turns, int) or max_turns < 1:
        raise CoordinationError("max_turns must be a positive integer")
    if max_turns > MAX_EXECUTION_TURNS:
        raise CoordinationError(
            f"max_turns cannot exceed {MAX_EXECUTION_TURNS}"
        )
    return max_turns


@dataclass(frozen=True)
class MessageEnvelope:
    protocol_version: str
    message_id: str
    task_id: str
    sender_id: str
    recipient_id: str | None
    kind: str
    sequence: int
    correlation_id: str | None
    payload: dict[str, Any]
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "message_id": self.message_id,
            "task_id": self.task_id,
            "sender_id": self.sender_id,
            "recipient_id": self.recipient_id,
            "kind": self.kind,
            "sequence": self.sequence,
            "correlation_id": self.correlation_id,
            "payload": self.payload,
            "created_at": self.created_at,
        }
