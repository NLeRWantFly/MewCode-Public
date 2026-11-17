"""One host-owned transition from the main design conversation to graph validation."""
from __future__ import annotations

import asyncio
import copy
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from mewcode.orchestration.task_design import TaskDesign
from mewcode.orchestration.draft_store import DraftStore
from mewcode.orchestration.task_design import design_view
from .base import Tool, ToolResult, ToolRiskProfile


class AdvanceTaskDesignParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["read", "diagnose", "build"] = "read"
    expected_revision: int | None = Field(default=None, ge=1)
    expected_sequence: int | None = Field(default=None, ge=0)
    source_hash: str = ""
    base_graph_hash: str = ""
    snapshot_id: str = ""
    requirement_fingerprint: str = ""


def review_request_preflight(blocks, state):
    from mewcode.orchestration.draft_sources import digest, read_sources
    from mewcode.orchestration.draft_store import DraftStore
    from mewcode.orchestration.effective_history import coverage_sources
    from mewcode.orchestration.graph_review import (
        _requirements, _review_batches, _with_decisions, build_review_request,
    )
    from mewcode.orchestration.workflow_blocks import with_material_sources

    candidate = (state or {}).get("candidate") or {}
    task = (state or {}).get("task_design") or {}
    document = candidate.get("document")
    basis = candidate.get("history_basis")
    result = {
        "identity": {"task_id": task.get("task_id"), "task_revision": task.get("revision"),
            "material_sequence": ((state or {}).get("materials") or {}).get("sequence"),
            "candidate_id": candidate.get("id"), "candidate_sequence": candidate.get("sequence"),
            "candidate_document_hash": digest(document), "history_basis_hash": digest(basis)},
        "ready": False, "max_chars": 250000, "model_calls": 0, "read_only": True,
        "retry_authorized": False,
        "scope": "Request capacity and lossless encoding only; not semantic approval or permission.",
    }
    try:
        blocks._check()
        frozen = candidate.get("history_source_ids")
        if not document or not basis or not candidate.get("id"):
            raise ValueError("候选或冻结历史基准缺失。")
        if (not isinstance(frozen, list) or not frozen
                or any(not isinstance(item, str) for item in frozen) or len(frozen) != len(set(frozen))):
            raise ValueError("冻结来源清单缺失、重复或格式无效。")
        raw, warnings, scan = read_sources(blocks.source_path)
        if warnings or scan.get("complete") is not True:
            raise ValueError("原始来源读取不完整，不能生成容量结论。")
        indexed = {row["id"]: row for row in with_material_sources(raw, state)}
        if any(item not in indexed for item in frozen):
            raise ValueError("冻结来源缺失，不能以来源子集预检。")
        sources = _with_decisions(coverage_sources(basis, [indexed[item] for item in frozen],
            start_source_id=task.get("source_boundary")), document)
        requirements = _requirements(sources)
        texts = {row["source_id"]: row["text"] for row in requirements}
        if not texts or len(texts) != len(requirements):
            raise ValueError("用户要求为空或来源身份重复。")
        result.update(frozen_source_ids_hash=digest(frozen), review_sources_hash=digest(sources),
            requirement_count=len(texts), history_scan_complete=True)
        batches = _review_batches(sources, document, result["max_chars"])
        sizes, ranges = [], {item: [] for item in texts}
        for batch in batches:
            original = json.loads(build_review_request(batch, document))
            request = build_review_request(batch, document, max_chars=result["max_chars"])
            decoded = json.loads(request)
            if decoded.pop("history_text_refs", False):
                texts_table = decoded.pop("history_texts", {})

                def expand_text(item):
                    if isinstance(item, dict):
                        if set(item) == {"graph_text_path"}:
                            literal = decoded["graph"]
                            for part in item["graph_text_path"]:
                                literal = literal[part]
                            if not isinstance(literal, str):
                                raise ValueError("历史文字引用没有指向完整字符串。")
                            return literal
                        if set(item) == {"history_text_id"}:
                            return texts_table[item["history_text_id"]]
                        return {key: expand_text(child) for key, child in item.items()}
                    if isinstance(item, list):
                        return [expand_text(child) for child in item]
                    return item

                for key in ("history_context", "history_context_tables", "history_citations", "history_quote_prefixes"):
                    if key in decoded:
                        decoded[key] = expand_text(decoded[key])
            if "history_context_tables" in decoded:
                decoded["history_context"] = [
                    {**dict(zip(table["columns"], row)), **table.get("shared", {})}
                    for table in decoded.pop("history_context_tables") for row in table["rows"]
                ]
            if decoded.pop("history_field_sources_grouped", False):
                for row in decoded.get("history_context", []):
                    if "field_source_groups" in row:
                        row["field_sources"] = {
                            field: copy.deepcopy(group["references"])
                            for group in row.pop("field_source_groups") for field in group["fields"]
                        }
            citations = decoded.pop("history_citations", {})
            prefixes = decoded.pop("history_quote_prefixes", {})
            for citation in citations.values():
                quote = citation.get("quote")
                if isinstance(quote, dict) and set(quote) == {"prefix_id", "suffix"}:
                    citation["quote"] = prefixes[quote["prefix_id"]] + quote["suffix"]
            for row in decoded.get("history_context", []):
                fields = row.get("field_sources") or {}
                for refs in [row.get("source_refs", []), *fields.values()]:
                    for index, reference in enumerate(refs):
                        if set(reference) == {"citation_id"}:
                            refs[index] = citations[reference["citation_id"]]
            if decoded != original or len(request) > result["max_chars"]:
                raise ValueError("请求编码不完整或仍然超限。")
            sizes.append(len(request))
            for requirement in original["requirements"]:
                item = requirement["source_id"]
                text = texts[item]
                chunk = requirement.get("chunk") or {"start": 0, "end": len(text)}
                start, end = chunk["start"], chunk["end"]
                if (type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(text)
                        or requirement["text"] != text[start:end]):
                    raise ValueError("来源分片与原文不一致。")
                ranges[item].append((start, end))
        for item, parts in ranges.items():
            covered = 0
            for start, end in sorted(parts):
                if start > covered:
                    raise ValueError("来源分片存在覆盖缺口。")
                covered = max(covered, end)
            if not parts or covered != len(texts[item]):
                raise ValueError("来源分片未覆盖全部原文。")
        latest = DraftStore.read_state(blocks.project, blocks.conversation_id)
        if digest(latest) != digest(state):
            raise ValueError("候选或规格在预检期间变化，结果失效。")
        blocks._check()
        result.update(ready=True, batch_count=len(sizes), max_request_chars=max(sizes),
            min_request_chars=min(sizes), total_request_chars=sum(sizes),
            lossless_roundtrip=True, all_requirement_ranges_covered=True)
    except (ValueError, OSError, TypeError, KeyError, AttributeError) as exc:
        result.update(error_type=type(exc).__name__, error=str(exc)[:500])
    return result


class AdvanceTaskDesign(Tool):
    name = "AdvanceTaskDesign"
    description = ("Read the host's active task design or compile its authored nodes and connections into a native workflow "
                   "and save it after this chat turn. No additional model review. ReadWorkflowBlocks first and use its first-header revision, "
                   "sequence, hashes and snapshot_id/requirement_fingerprint. The user's host-selected "
                   "plan/execute intent cannot be changed by this tool. Use action=diagnose after a failure to read current "
                   "material/candidate identities, structural diagnostics, runtime freshness and the next recovery step. "
                   "Does not itself dispatch business tools or grant permission.")
    params_model = AdvanceTaskDesignParams
    category = "harness"
    risk_profile = ToolRiskProfile(side_effect="workspace", reversible=True)

    def permission_risk(self, arguments):
        return ToolRiskProfile(side_effect="workspace" if arguments.get("action") == "build" else "none")

    def __init__(self, blocks):
        self.blocks = blocks

    async def execute(self, params):
        try:
            self.blocks._check()
            state = await asyncio.to_thread(DraftStore.read_state, self.blocks.project, self.blocks.conversation_id)
            current = await asyncio.to_thread(design_view, state or {})
            self.blocks._check()
            if params.action == "build":
                if not current:
                    raise ValueError("当前没有已发起的任务对话，请使用一起完成任务入口。")
                if (current.get("health") or {}).get("next_action") == "reload_runtime":
                    return ToolResult(json.dumps({"error": "runtime_revision_changed",
                        "health": current["health"], "business_started": False}, ensure_ascii=False), is_error=True)
                arguments = {"expected_sequence": params.expected_sequence, "source_hash": params.source_hash,
                    "base_graph_hash": params.base_graph_hash, "snapshot_id": params.snapshot_id,
                    "requirement_fingerprint": params.requirement_fingerprint}
                view = await asyncio.to_thread(self.blocks.validate_snapshot, **arguments)
                design = TaskDesign(self.blocks.project, self.blocks.conversation_id)
                current = await asyncio.to_thread(design.queue_build, task_id=current["task_id"],
                    compiler='workflow_core',
                    revision=params.expected_revision, sequence=params.expected_sequence,
                    source_hash=view["source_hash"], base_graph_hash=params.base_graph_hash,
                    user_source_hash=view["user_source_hash"], requirement_fingerprint=view["requirement_fingerprint"],
                    basis_validator=lambda: self.blocks.validate_snapshot(**arguments),
                    capacity_validator=lambda current_state: review_request_preflight(self.blocks, current_state))
            if params.action == "diagnose":
                from mewcode.orchestration.workflow_blocks import compact_task_design
                current = compact_task_design(current)
                if current and (current.get("failure") or {}).get("exception_type") == "ReviewRequestTooLarge":
                    current["review_request_preflight"] = await asyncio.to_thread(
                        review_request_preflight, self.blocks, state)
                    self.blocks._check()
            return ToolResult(json.dumps({"task_design": current, "business_started": False}, ensure_ascii=False),
                host_observation={"design_health": current.get("health")} if current else None)
        except (ValueError, OSError, TypeError) as exc:
            return ToolResult(str(exc), is_error=True)
