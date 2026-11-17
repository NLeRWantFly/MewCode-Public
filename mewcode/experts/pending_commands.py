"""Reconnect accepted, unstarted requests to the normal capability dispatcher."""
from __future__ import annotations

import copy
from dataclasses import replace

from mewcode.experts.command_receipts import request_id


def restore_pending_commands(node_id, input_revision, receipts, result):
    handoff = result.handoff or {}
    if (not input_revision or result.status != "needs_attention"
            or handoff.get("blocker_kind") != "capability_missing"
            or handoff.get("question") or handoff.get("command_requests")
            or handoff.get("file_requests")
            or result.error_kind not in {"", "capability_missing", "environment_fault"}):
        return result
    latest = {}
    for row in receipts:
        if row.get("node_id") == node_id and row.get("input_revision") == input_revision:
            latest[row.get("base_request_id") or row.get("request_id")] = row
    requests = []
    for identity, row in latest.items():
        request = row.get("request")
        if (row.get("status") in {"not_started", "not_dispatched"}
                and row.get("command_started") is False
                and row.get("evidence_source") in {"host_execution", "host_dispatch",
                                                    "host_readonly_retry", "host_effect_verification"}
                and isinstance(request, dict)
                and identity == request_id(node_id, input_revision, request)):
            requests.append(copy.deepcopy(request))
    if not requests:
        return result
    handoff = copy.deepcopy(handoff)
    handoff["command_requests"] = requests
    # This only restores the queue. Schema validation, admission, exact command
    # scope, review, and execution recovery remain owned by the normal dispatcher.
    return replace(result, handoff=handoff, error_kind="capability_missing")
