"""Bounded loop prompt projection; the complete frozen contract stays host-side."""
from __future__ import annotations

import hashlib
import json
from typing import Any

POLICY_VERSION = "segment-context-v1"
DEFAULT_MAX_CHARS = 32000


def project_loop_context(context: dict[str, Any], *, max_chars: int = DEFAULT_MAX_CHARS
                         ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Omit other queue items only; never omit criteria or authorization fields.

    This is a prompt projection, not a contract mutation. If indispensable
    fields alone exceed the cap, fail closed instead of cutting acceptance.
    """
    if max_chars < 1024:
        raise ValueError("context cap too small for a frozen node contract")
    projected = dict(context)
    items = projected.get("items")
    omissions: list[str] = []
    if projected.get("mode") == "foreach" and isinstance(items, list):
        if not isinstance(projected.get("current_item"), dict):
            raise ValueError("current queue item missing")
        projected.pop("items")
        projected["item_count"] = len(items)
        projected["item_ids_sha256"] = hashlib.sha256(json.dumps(
            [row.get("item_id") for row in items], ensure_ascii=False,
            separators=(",", ":")).encode()).hexdigest()
        omissions.append("other_queue_items")
    encoded = json.dumps(projected, ensure_ascii=False, sort_keys=True, default=str)
    if len(encoded) > max_chars:
        raise ValueError("frozen node contract exceeds context cap; no acceptance text was truncated")
    receipt = {"schema_version": 1, "policy_version": POLICY_VERSION,
               "chars": len(encoded), "max_chars": max_chars,
               "omissions": omissions, "contract_hash": str(context.get("contract_hash") or ""),
               "criteria_preserved": projected.get("criteria") == context.get("criteria"),
               "authorization_preserved": projected.get("token_budget_authorization") ==
                                          context.get("token_budget_authorization")}
    return projected, receipt
