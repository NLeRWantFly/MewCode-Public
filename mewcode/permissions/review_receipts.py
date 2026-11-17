"""Content-free receipts for the actual host evidence envelope used in review."""
from __future__ import annotations

import copy

from .decision_envelope import canonical_digest


def workflow_receipt(scope):
    health = (scope or {}).get("workflow_health")
    if not isinstance(health, dict) or health.get("schema") != "mewcode.design-health.v1":
        return None
    validation = health.get("current_validation") or {}
    return {"schema": "mewcode.workflow-review-basis.v1", "health_fingerprint": health["fingerprint"],
        "identity": copy.deepcopy(health["identity"]),
        "runtime_fingerprint": health["runtime"]["fingerprint"],
        "runtime_status": health["runtime"]["status"],
        "structural_ready": validation.get("ready") is True,
        "blocking_count": validation.get("blocking_count"), "warning_count": validation.get("warning_count"),
        "execution_authorized": False}


def input_receipt(payload):
    result = {"schema": "mewcode.review-input-receipt.v1", "envelope_digest": canonical_digest(payload),
        "context_digest": canonical_digest(payload.get("authorized_task", "")),
        "arguments_digest": payload["planned_action"]["raw_arguments_digest"],
        "scope": "Host-prepared review input; not proof of model understanding, permission or execution."}
    basis = payload.get("declared_risk", {}).get("host_workflow_diagnosis")
    if basis:
        result["workflow"] = copy.deepcopy(basis)
    return result
