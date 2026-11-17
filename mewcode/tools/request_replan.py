"""Explicit executor request, consumed only after the current tool batch closes."""
from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field

from mewcode.experts.recovery_state import blockers, digest, tool_rows
from mewcode.tools.base import Tool, ToolResult


class RequestReplanParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    reason: str = Field(min_length=1, max_length=800)
    evidence_refs: list[str] = Field(min_length=1, max_length=8)


class RequestReplan(Tool):
    name = "RequestReplan"
    category = "read"
    is_system_tool = True
    consumes_runtime_slot = False
    params_model = RequestReplanParams
    description = ("Request a bounded discussion with the primary model when durable evidence "
        "invalidates the current method. Cite existing tool result IDs. This cannot change goals, "
        "acceptance, models, budgets, tools or permissions. Stop issuing new work after requesting; "
        "the host consumes it after the current tool batch has settled. Not a completion signal.")

    def __init__(self, store, run_id, node_id):
        self.store, self.run_id, self.node_id = store, run_id, node_id

    async def execute(self, params):
        def save(value):
            stage = value["node_states"][self.node_id]
            if blockers(stage) or stage.get("pending_interaction"):
                raise ValueError("Resolve the existing host boundary before requesting a new method")
            refs = {str(t.get("tool_id") or digest(t)) for t in tool_rows(stage)
                    if t.get("status") in {"completed", "failed"}}
            if not set(params.evidence_refs) <= refs:
                raise ValueError("Replan evidence must reference durable tool receipts")
            request = {**params.model_dump(), "input_revision": stage.get("input_revision", ""),
                       "node_attempt": stage.get("node_attempt")}
            request_id = digest(request)
            existing = stage.get("adaptive_request")
            if existing and existing.get("status") == "pending" and existing["request_id"] != request_id:
                raise ValueError("A pending replan request already exists")
            if existing and existing.get("request_id") == request_id:
                return
            stage["adaptive_request"] = {**request, "request_id": request_id, "status": "pending"}
        try:
            self.store.mutate(self.run_id, save, durable=True)
        except ValueError as exc:
            return ToolResult(output=str(exc), is_error=True)
        return ToolResult(output=json.dumps({"status": "recorded", "instruction":
            "Stop new work; await the host's safe-boundary method consultation."}))
