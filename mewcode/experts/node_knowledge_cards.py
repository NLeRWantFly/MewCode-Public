"""Small, fingerprint-bound host facts for a single expert node.

No model self-report can become a verified card. At present the extractor only
learns successful local read routes and a repeated, host-checked page offset.
Semantic rubrics require a separate evidence-backed mechanism.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

POLICY_VERSION = "node-knowledge-v1"
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_OBSERVATIONS = 128
MAX_CARDS = 12
MAX_READ_OBSERVATIONS_PER_SEGMENT = 8


def _source(root: Path, raw: Any) -> tuple[str, str] | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        path = (root / raw).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            return None
        relative = path.relative_to(root).as_posix()
        return relative, hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, ValueError):
        return None


def _segment_for(timestamp: Any, history: list[dict[str, Any]]) -> str:
    if not isinstance(timestamp, (int, float)):
        return ""
    for row in reversed(history):
        start, end = row.get("started_at"), row.get("ended_at")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)) and start <= timestamp <= end:
            return str(row.get("segment_id") or "")
    return ""


def observe_knowledge(stage: dict[str, Any], *, root: str | Path,
                      queue: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return updated bounded observations/cards without modifying the stage."""
    work_dir = Path(root).resolve()
    revision = str(stage.get("input_revision") or "")
    if not revision:
        return {"observations": [], "cards": []}
    history = [row for row in stage.get("segment_health_history") or [] if isinstance(row, dict)]
    old = [row for row in stage.get("knowledge_observations") or []
           if isinstance(row, dict) and row.get("input_revision") == revision]
    observations = {(row.get("key"), row.get("segment_id")): row for row in old
                    if row.get("key") and row.get("segment_id")}
    tools = stage.get("tools") or {}
    rows = tools.values() if isinstance(tools, dict) else tools if isinstance(tools, list) else []
    queue_items = {str(row.get("item_id")): row for row in (queue or {}).get("items") or []
                   if isinstance(row, dict) and row.get("item_id")}
    inspected_reads = 0
    for row in list(rows)[-32:]:
        if not isinstance(row, dict) or row.get("status") != "completed" or row.get("is_error"):
            continue
        segment_id = _segment_for(row.get("declared_at") or row.get("started_at"), history)
        if history and segment_id != history[-1].get("segment_id"):
            continue
        args = row.get("args")
        if not segment_id or not isinstance(args, dict):
            continue
        if row.get("name") not in {"ReadFile", "ReadAuditArtifact", "ReviewWorkQueue"}:
            continue
        inspected_reads += 1
        if inspected_reads > MAX_READ_OBSERVATIONS_PER_SEGMENT:
            break
        kind = ""
        details: dict[str, Any] = {}
        if row.get("name") in {"ReadFile", "ReadAuditArtifact"}:
            source = _source(work_dir, args.get("path") or args.get("file_path"))
            if source:
                kind = "read_route"
                details = {"path": source[0], "file_sha256": source[1],
                           "tool": row["name"]}
        elif row.get("name") == "ReviewWorkQueue" and args.get("action") == "evidence":
            item = queue_items.get(str(args.get("item_id") or ""))
            page = (item or {}).get("pdf_page")
            selector = args.get("source_selector") or {}
            source = _source(work_dir, args.get("source_path"))
            if (source and isinstance(page, int) and page > 0 and isinstance(selector, dict)
                    and selector.get("page_idx") == page - 1):
                kind = "page_zero_offset"
                details = {"path": source[0], "file_sha256": source[1],
                           "offset": -1}
        if not kind:
            continue
        key = hashlib.sha256(json.dumps([kind, details, revision], sort_keys=True,
                        ensure_ascii=False).encode()).hexdigest()
        observations[(key, segment_id)] = {"key": key, "kind": kind,
            "details": details, "input_revision": revision,
            "segment_id": segment_id, "tool_id": str(row.get("tool_id") or "")}
    bounded = list(observations.values())[-MAX_OBSERVATIONS:]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in bounded:
        grouped.setdefault(row["key"], []).append(row)
    cards = []
    for key, group in grouped.items():
        if len({row["segment_id"] for row in group}) < 2:
            continue
        sample = group[-1]
        kind, details = sample["kind"], sample["details"]
        if kind == "read_route":
            text = f"当前输入版本中，{details['tool']} 已成功读取 {details['path']}。"
        else:
            text = f"当前输入版本中，{details['path']} 的 page_idx = 物理页 - 1 已由两段证据读取核对。"
        cards.append({"schema_version": 1, "policy_version": POLICY_VERSION,
                      "key": key, "kind": kind, "text": text,
                      "details": details, "input_revision": revision,
                      "source_tool_ids": [row["tool_id"] for row in group[-4:]],
                      "verified": True})
    return {"observations": bounded, "cards": cards[-MAX_CARDS:]}


def select_cards(stage: dict[str, Any], *, input_revision: str,
                 root: str | Path) -> list[dict[str, Any]]:
    """Revalidate file bytes on every injection; stale cards silently expire."""
    work_dir = Path(root).resolve()
    selected = []
    for card in (stage.get("knowledge_cards") or [])[-MAX_CARDS:]:
        if not isinstance(card, dict) or not card.get("verified") or card.get("input_revision") != input_revision:
            continue
        details = card.get("details") or {}
        source = _source(work_dir, details.get("path"))
        if source and source[1] == details.get("file_sha256"):
            selected.append(card)
    return selected
