"""Chat-driven progress reports for the orchestration canvas.

Orchestration runs write ``node_states`` and ``process_file_receipts`` into an
expert-run snapshot, and the canvas renders whatever ``/api/orchestration/
run-state`` returns for the active conversation.  Chat-driven work has no such
run, so the same canvas stayed blank while the real work advanced.

This module stores a bounded, per-conversation report so chat work can be
mirrored onto the same canvas without creating an expert run: nothing here is
resumable, schedulable, or counted as orchestration history.  The payload is
deliberately run-shaped (``node_states`` / ``process_files`` / ``status``) so
the canvas and the node-change explainer consume it unchanged.

Only the runtime boundary writes here, and every list is capped: a chat turn
can call ``report_step`` hundreds of times, and an unbounded log would grow
without limit next to the session transcripts.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

STORE_FILENAME = "chat-reports.json"
STORE_DIR = ".mewcode"
STORE_VERSION = 1

# The canvas badges read these; they are the same statuses an expert run uses.
NODE_STATUSES = {
    "pending", "running", "completed", "failed",
    "blocked", "needs_attention", "skipped",
}
ACTIVE_STATUSES = {"pending", "running", "blocked", "needs_attention"}

MAX_NODES = 200
MAX_EVENTS = 400
MAX_RECEIPTS = 200
MAX_CONVERSATIONS = 40

RUN_ID_PREFIX = "chat:"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def normalize_status(value: Any) -> str:
    status = str(value or "").strip().lower()
    return status if status in NODE_STATUSES else "running"


def store_path(work_dir: str | os.PathLike[str]) -> Path:
    return Path(work_dir) / STORE_DIR / STORE_FILENAME


def _empty_conversation(conversation_id: str) -> dict[str, Any]:
    return {
        "conversation_id": conversation_id,
        "run_id": RUN_ID_PREFIX + conversation_id,
        "task": "",
        "status": "running",
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "node_states": {},
        "process_files": [],
        "events": [],
    }


class ChatReportStore:
    """Append-only, bounded reports keyed by (project, conversation)."""

    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        self._root = Path(root) if root is not None else None

    # ---------- storage ----------

    def _path(self, work_dir: str | os.PathLike[str]) -> Path:
        base = self._root if self._root is not None else work_dir
        return store_path(base)

    def _load(self, work_dir: str | os.PathLike[str]) -> dict[str, Any]:
        path = self._path(work_dir)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"version": STORE_VERSION, "conversations": {}}
        if not isinstance(raw, dict):
            return {"version": STORE_VERSION, "conversations": {}}
        conversations = raw.get("conversations")
        if not isinstance(conversations, dict):
            conversations = {}
        return {"version": STORE_VERSION, "conversations": conversations}

    def _save(self, work_dir: str | os.PathLike[str], data: dict[str, Any]) -> None:
        path = self._path(work_dir)
        conversations = data.get("conversations") or {}
        if len(conversations) > MAX_CONVERSATIONS:
            ordered = sorted(
                conversations.items(),
                key=lambda item: str((item[1] or {}).get("updated_at") or ""),
                reverse=True,
            )[:MAX_CONVERSATIONS]
            data["conversations"] = dict(ordered)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic replace: a half-written report must never break the canvas poll.
            handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as target:
                    json.dump(data, target, ensure_ascii=False, indent=1)
                os.replace(tmp_name, path)
            finally:
                if os.path.exists(tmp_name):
                    try:
                        os.remove(tmp_name)
                    except OSError:
                        pass
        except OSError:
            log.warning("failed to persist chat report store at %s", path)

    # ---------- writes ----------

    def report(
        self,
        *,
        work_dir: str | os.PathLike[str],
        conversation_id: str,
        node_id: str,
        label: str = "",
        status: str = "running",
        summary: str = "",
        details: dict[str, Any] | None = None,
        task: str = "",
    ) -> dict[str, Any] | None:
        """Record one chat step against a canvas node."""

        conversation_id = str(conversation_id or "").strip()
        node_id = str(node_id or "").strip()
        if not conversation_id or not node_id:
            return None
        status = normalize_status(status)
        now = utc_now()
        data = self._load(work_dir)
        conversations = data.setdefault("conversations", {})
        entry = conversations.get(conversation_id)
        if not isinstance(entry, dict):
            entry = _empty_conversation(conversation_id)
            conversations[conversation_id] = entry
        if task and not entry.get("task"):
            entry["task"] = str(task)[:400]

        states = entry.setdefault("node_states", {})
        state = states.get(node_id)
        if not isinstance(state, dict):
            state = {"attempt": 1, "started_at": now}
            states[node_id] = state
        state.update({
            "status": status,
            "label": str(label or state.get("label") or "")[:200],
            "updated_at": now,
        })
        if summary:
            state["summary"] = str(summary)[:2000]
            state["message"] = str(summary)[:2000]
        if details:
            from mewcode.tools.report_step import report_fields
            state["work_report"] = report_fields(details)
        if status in {"completed", "failed", "skipped"}:
            state["finished_at"] = now

        if len(states) > MAX_NODES:
            ordered = sorted(
                states.items(),
                key=lambda item: str((item[1] or {}).get("updated_at") or ""),
                reverse=True,
            )[:MAX_NODES]
            entry["node_states"] = dict(ordered)

        events = entry.setdefault("events", [])
        events.append({
            "seq": len(events) + 1,
            "type": "chat_report",
            "node_id": node_id,
            "label": state.get("label") or "",
            "status": status,
            "summary": str(summary)[:400],
            **({"work_report": state.get("work_report", {})} if details else {}),
            "at": now,
        })
        del events[:-MAX_EVENTS]

        # Conversation status follows the newest activity: a fresh running step
        # means the work is in flight again, even if an earlier node failed.
        statuses = [
            str((value or {}).get("status") or "")
            for value in entry["node_states"].values()
        ]
        if any(value in ACTIVE_STATUSES for value in statuses):
            entry["status"] = "running"
        elif any(value == "failed" for value in statuses):
            entry["status"] = "failed"
        elif statuses and all(value in {"completed", "skipped"} for value in statuses):
            entry["status"] = "completed"
        else:
            entry["status"] = "running"
        entry["updated_at"] = now

        self._save(work_dir, data)
        return dict(entry)

    def record_file(
        self,
        *,
        work_dir: str | os.PathLike[str],
        conversation_id: str,
        record: dict[str, Any],
        node_id: str = "",
    ) -> bool:
        """Attach a process-file receipt so the canvas shows produced files."""

        conversation_id = str(conversation_id or "").strip()
        if not conversation_id or not isinstance(record, dict) or not record.get("id"):
            return False
        data = self._load(work_dir)
        conversations = data.setdefault("conversations", {})
        entry = conversations.get(conversation_id)
        if not isinstance(entry, dict):
            entry = _empty_conversation(conversation_id)
            conversations[conversation_id] = entry
        receipts = entry.setdefault("process_files", [])
        if any(str(item.get("id")) == str(record["id"]) for item in receipts):
            return False
        stored = dict(record)
        if node_id and not stored.get("node_id"):
            stored["node_id"] = node_id
        receipts.append(stored)
        del receipts[:-MAX_RECEIPTS]
        entry["updated_at"] = utc_now()
        self._save(work_dir, data)
        return True

    def clear(self, *, work_dir: str | os.PathLike[str], conversation_id: str) -> bool:
        data = self._load(work_dir)
        conversations = data.setdefault("conversations", {})
        if str(conversation_id) not in conversations:
            return False
        conversations.pop(str(conversation_id), None)
        self._save(work_dir, data)
        return True

    # ---------- reads ----------

    def preview(
        self,
        *,
        work_dir: str | os.PathLike[str],
        conversation_id: str,
        file_id: str,
    ) -> dict[str, Any] | None:
        """Immutable snapshot for one reported file (never a later disk read)."""

        entry = (self._load(work_dir).get("conversations") or {}).get(
            str(conversation_id or "").strip()
        )
        if not isinstance(entry, dict):
            return None
        for record in entry.get("process_files") or []:
            if str((record or {}).get("id") or "") == str(file_id or "").strip():
                return dict(record)
        return None

    def snapshot(
        self,
        *,
        work_dir: str | os.PathLike[str],
        conversation_id: str,
    ) -> dict[str, Any] | None:
        """Run-shaped payload for the canvas, or None when nothing was reported."""

        conversation_id = str(conversation_id or "").strip()
        if not conversation_id:
            return None
        entry = (self._load(work_dir).get("conversations") or {}).get(conversation_id)
        if not isinstance(entry, dict):
            return None
        states = entry.get("node_states") or {}
        receipts = entry.get("process_files") or []
        if not states and not receipts:
            return None
        status = str(entry.get("status") or "running")
        latest = ""
        for state in states.values():
            latest = max(latest, str((state or {}).get("updated_at") or ""))
        return {
            "run_id": str(entry.get("run_id") or RUN_ID_PREFIX + conversation_id),
            "conversation_id": conversation_id,
            "task": str(entry.get("task") or ""),
            # Metadata only: previews ride a separate endpoint so the canvas
            # poll never carries base64 samples.
            "process_files": [
                {key: value for key, value in dict(record or {}).items() if key != "preview"}
                for record in receipts
            ],
            "intervention_receipts": [],
            "events": list(entry.get("events") or []),
            "status": status,
            "active": status in ACTIVE_STATUSES,
            "source": "chat_report",
            "updated_at": str(entry.get("updated_at") or latest),
            "node_states": {
                str(node_id): {
                    key: value
                    for key, value in dict(state or {}).items()
                    if value not in (None, "")
                }
                for node_id, state in states.items()
            },
        }
