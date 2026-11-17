"""Host-bound incremental planning tools, also usable before a graph exists."""
from __future__ import annotations

import asyncio
import json

from pydantic import BaseModel, ConfigDict, Field

from mewcode.tools.base import Tool, ToolResult, ToolRiskProfile


class ReadWorkflowBlocksParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entry_ids: list[str] = Field(default_factory=list)
    query: str = ""
    source_ids: list[str] = Field(default_factory=list)
    snapshot_id: str = ""
    cursor: str = ""
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=8000, ge=1, le=9000)


class PendingWithdrawal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    diagnostic_id: str = Field(pattern=r"^pd_[a-f0-9]{24}$")
    reason: str = Field(min_length=1, max_length=1000)


class SubmitWorkflowBlocksParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_sequence: int = Field(ge=0)
    source_hash: str
    base_graph_hash: str
    snapshot_id: str = ""
    requirement_fingerprint: str = ""
    upserts: list[dict] = Field(default_factory=list)
    ignored: list[dict] = Field(default_factory=list)
    withdraw_pending: list[PendingWithdrawal] = Field(default_factory=list, max_length=200)
    use_jev: bool = True


class ReadWorkflowBlocks(Tool):
    name = "ReadWorkflowBlocks"
    description = ("Query this conversation's current workflow nodes, adjacent dependencies, requirements, missing fields, "
                   "source index and complete entry contract. The first header contains task identity/revision/status and missing fields. "
                   "Use section_ranges to jump directly to material_refresh.pending for previous/current/proposed conflicts, "
                   "or projection repair sections, before reading full entry copies. Pass that section's cursor and read only "
                   "until its end offset. more/next_cursor cover the whole snapshot: stop at the chosen section's end even "
                   "when more is true. Jumping sections does not confirm all requirements; save/build still require the current "
                   "snapshot basis and CAS checks. Complete source_index/sources remain accessible; use source_ids for exact cited messages "
                   "only when needed, then jump to the returned sources section. Continue pages with next_cursor or snapshot_id plus next_offset; "
                   "offset alone cannot resume a live read. Supply snapshot_id/requirement_fingerprint on save/build; "
                   "reuse stable node IDs when refining across turns. No execution or unrelated conversations.")
    params_model = ReadWorkflowBlocksParams
    category = "read"
    is_concurrency_safe = True

    def __init__(self, blocks):
        self.blocks = blocks

    async def execute(self, params):
        try:
            header, content = await asyncio.to_thread(self.blocks.read_page, **params.model_dump())
            return ToolResult(json.dumps(header, ensure_ascii=False) + "\n" + content)
        except (ValueError, OSError, TypeError) as exc:
            return ToolResult(str(exc), is_error=True)


class SubmitWorkflowBlocks(Tool):
    name = "SubmitWorkflowBlocks"
    description = ("Save an incremental node/edge/loop planning patch in this conversation. ReadWorkflowBlocks first and supply "
                   "its sequence/source/canvas hashes and snapshot_id/requirement_fingerprint. Assistant progress observations do not "
                   "invalidate the semantic basis; new requirements/material/canvas edits do. Preserve IDs and source quotes; designs remain AI proposals. Independently "
                   "valid fragments are saved; pending diagnostics must be repaired in this turn. Never edits saved canvas, starts "
                   "business execution, confirms user choices or grants permission. To withdraw rejected proposals, submit only "
                   "withdraw_pending=[{diagnostic_id,reason}] using the current pending IDs. This keeps accepted entries, records history "
                   "and revalidates the document. ignored={source_id,reason} excludes source content; it never deletes diagnostics.")
    params_model = SubmitWorkflowBlocksParams
    category = "write"
    consumes_runtime_slot = False

    def __init__(self, blocks, *, turn_id=None, judgment_runtime=None):
        self.blocks = blocks
        self.turn_id = turn_id or (lambda: "")
        self.judgment_runtime = judgment_runtime

    def permission_risk(self, arguments):
        auxiliary = bool(not arguments.get("withdraw_pending") and arguments.get("use_jev", True) and self.judgment_runtime)
        return ToolRiskProfile(side_effect="workspace", network=auxiliary, cost=auxiliary,
                               target_type="workflow_planning_state")

    async def _advice(self, params):
        runtime = self.judgment_runtime
        if params.withdraw_pending:
            return {"status": "main_model", "reason": "diagnostic_withdrawal_no_model_call"}
        if runtime is None or not params.use_jev:
            return {"status": "main_model", "reason": "optional_judgment_unavailable"}
        from mewcode.judgments.spec import JudgmentRequest, JudgmentSpec, Question
        from mewcode.plugins.services import JUDGMENT_SPEC_SLOT
        try:
            await asyncio.to_thread(self.blocks.validate_snapshot, expected_sequence=params.expected_sequence,
                source_hash=params.source_hash, base_graph_hash=params.base_graph_hash, snapshot_id=params.snapshot_id,
                requirement_fingerprint=params.requirement_fingerprint)
            view = await asyncio.to_thread(self.blocks.read)
            context = getattr(runtime, "context", None)
            if context is None:
                return {"status": "main_model", "reason": "judgment_context_unavailable"}
            spec_name = "workflow-material-match"
            if spec_name not in (context.slots.resolve(JUDGMENT_SPEC_SLOT) or {}):
                disposable = context.slots.register(JUDGMENT_SPEC_SLOT,
                    JudgmentSpec(name=spec_name, mode="advisory", failure_policy="preserve"), item_key=spec_name)
                context.effect(disposable.dispose)
            entries = view["document"]["entries"]
            existing = [e for e in entries if e["kind"] == "node" and e["state"] != "retired"]
            questions, candidates = {}, {}
            for index, entry in enumerate(params.upserts):
                if entry.get("kind") != "node" or any(e["id"] == entry.get("id") for e in existing):
                    continue
                options = existing[:8]
                if not options:
                    continue
                key = f"node_{index}"
                candidates[key] = {"proposed": entry, "existing": options,
                                   "candidate_list_complete": len(options) == len(existing)}
                questions[key] = Question(type="choice", instructions=(
                    f"Does candidates.{key}.proposed describe the SAME business step as an existing candidate? "
                    "Same name alone is insufficient; compare responsibility, scope and inputs/outputs. "
                    "Select none for different stages, unknown evidence or missing candidates. Never authorize execution."),
                    criteria={**{e["id"]: e["title"] for e in options}, "none": "No proven equivalent in these candidates"})
            if not questions:
                return {"status": "main_model", "reason": "no_matching_question_needed"}
            result = await runtime.judge(JudgmentRequest(spec=spec_name,
                issue="workflow-" + str(params.expected_sequence),
                state={"requirements": [e for e in entries if e["kind"] in {"goal", "constraint"}],
                       "candidates": candidates, "source_hash": params.source_hash}, questions=questions),
                expected_scope=runtime.scope, guard=self.blocks.guard)
            return {**result, "advisory_only": True, "next_action": "main_model_reviews_reuse_before_next_revision"}
        except Exception as exc:  # noqa: BLE001 -- optional advice must never interrupt planning.
            return {"status": "main_model", "reason": "judgment_unavailable", "error_type": type(exc).__name__}

    async def execute(self, params):
        try:
            advice = await self._advice(params)
            value = await asyncio.to_thread(self.blocks.submit, **params.model_dump(exclude={"use_jev"}), turn_id=self.turn_id())
            # Avoid the ordinary tool-output cap dropping the useful repair diagnostic.
            detail = {k: v for k, v in value.items() if k not in {"changes", "contract"}}
            detail["changes"] = [{"entry_id": c["entry_id"], "changed_fields": [k for k in ("title", "body", "data", "state")
                if (c["before"] or {}).get(k) != c["after"].get(k)]} for c in value["changes"]]
            detail["next_action"] = "repair_pending_fields" if value["pending"] else "continue_discussion_or_build"
            detail["judgment"] = advice
            return ToolResult(json.dumps(detail, ensure_ascii=False), host_observation={"workflow_materials": detail})
        except (ValueError, OSError, TypeError) as exc:
            return ToolResult(str(exc), is_error=True)
