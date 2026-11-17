"""Retire the removed dialogue-slice review without approving any operation."""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone

POLICY_VERSION = "host-checkpoint-resume-v2"
LEGACY_NO_DECISION = "接力分析没有形成有效决定，原编排保持暂停。"


def _is_retired_review(pending) -> bool:
    if not isinstance(pending, dict) or pending.get("action") != "hold":
        return False
    # A real plan/user/acceptance decision is not an incomplete model review.
    if (pending.get("relation") == "plan_changed"
            or pending.get("accepted_plan_fingerprint")
            or pending.get("invalidated_completed_node_ids")):
        return False
    progress = pending.get("handoff_review_progress")
    if isinstance(progress, dict):
        return (pending.get("evidence_incomplete") is True
                or bool(progress.get("error_kind"))
                or progress.get("review_status") != "reviewed")
    return (pending.get("summary") == LEGACY_NO_DECISION
            or (pending.get("retryable") is True
                and pending.get("failure_kind") in {"interrupted", "model_error", "no_decision"}))


def retire_paused_handoff_review(snapshot: dict) -> bool:
    """Archive only removed review state; keep host, business and user holds.

    Original sources and history remain readable. Archival never marks old
    records reconciled, moves semantic credits into the run, or changes tool
    outcomes, approval state, models, retries or the execution checkpoint.
    """
    pending = snapshot.get("pending_dialogue_handoff")
    retired = _is_retired_review(pending)
    grant = snapshot.get("handoff_review_grant")
    payload = {}
    if retired:
        payload["decision"] = copy.deepcopy(pending)
    if isinstance(grant, dict):
        payload["grant"] = copy.deepcopy(grant)
    changed = snapshot.get("paused_resume_policy") != POLICY_VERSION
    if payload:
        migration_id = hashlib.sha256(json.dumps(
            [snapshot.get("run_id"), payload], sort_keys=True, ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        archives = snapshot.setdefault("retired_paused_handoff_reviews", [])
        if not isinstance(archives, list) or any(not isinstance(item, dict) for item in archives):
            raise ValueError("旧暂停审阅归档格式异常，请恢复有效检查点后重试。")
        if not any(item.get("migration_id") == migration_id for item in archives):
            archives.append({"migration_id": migration_id, "policy_version": POLICY_VERSION,
                "status": "retired_not_approved", "reason": "dialogue_slice_review_removed",
                "at": datetime.now(timezone.utc).isoformat(), **payload})
        if retired:
            snapshot.pop("pending_dialogue_handoff")
        if isinstance(grant, dict):
            snapshot.pop("handoff_review_grant")
        changed = True
    snapshot["paused_resume_policy"] = POLICY_VERSION
    return changed
