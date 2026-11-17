"""One derived current-state diagnosis for task UI, tools and safety review.

The draft remains the source of truth. No report, historical failure, runtime
fingerprint or suggested recovery step grants permission to save or execute.
"""
from __future__ import annotations

import copy
from functools import lru_cache
import json

from .draft_sources import digest


@lru_cache(maxsize=32)
def _validate(document_json, base_json, validator_revision):
    del validator_revision  # Changing loaded validators invalidates the cache.
    from .graph_readiness import readiness
    try:
        return readiness(json.loads(document_json), json.loads(base_json),
                         conversation_id="diagnostic", revision=0)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        return {"ready": False, "diagnostics": [{"code": "validation_unavailable", "path": "$",
            "entry_ids": [], "message": "当前结构无法完成校验：" + str(exc)[:400]}],
            "skipped": ["structure"], "skipped_reason": "校验未完成，不能视为通过。"}


def _validation(document, base, validator_revision):
    check = copy.deepcopy(_validate(json.dumps(document, sort_keys=True, ensure_ascii=False),
                                    json.dumps(base, sort_keys=True, ensure_ascii=False), validator_revision))
    rows = check.pop("diagnostics", [])
    blockers = [row for row in rows if row.get("severity") != "warning"]
    warnings = [row for row in rows if row.get("severity") == "warning"]
    shown = blockers + warnings
    return {**check, "blocking_count": len(blockers), "warning_count": len(warnings),
        "diagnostic_count": len(rows), "diagnostics": [{
            **{key: row[key] for key in ("code", "path", "severity", "entry_ids", "actual", "expected") if key in row},
            "message": str(row.get("message") or "")[:500]} for row in shown[:20]],
        "omitted_count": max(0, len(rows) - 20),
        "scope": "Full deterministic structure checks; not semantic coverage, permission or business acceptance."}


def design_health(state, *, base=None, runtime=None):
    from .workflow_blocks import _baseline
    from . import graph_readiness  # Load the actual validators before fingerprinting.
    design = state.get("task_design") or {}
    materials = state.get("materials") or {}
    candidate = state.get("candidate") or {}
    if base is None:
        base = candidate.get("base_snapshot") or materials.get("base_spec")
    document = _baseline(state, base)
    if runtime is None:
        from mewcode.runtime_revision import runtime_revision
        runtime = runtime_revision()
    current = _validation(document, base, runtime["fingerprint"])
    entries = {entry["id"]: entry for entry in document.get("entries", [])}
    previous_entries = {entry["id"]: entry for entry in (candidate.get("document") or {}).get("entries", [])}
    nodes = {entry["id"] for entry in entries.values() if entry.get("kind") == "node" and entry.get("state") != "retired"}
    for problem in current["diagnostics"]:
        if problem["code"] != "input_collection_target_missing":
            continue
        entry_id = problem["entry_ids"][0]
        prior = previous_entries.get(entry_id) or {}
        target = (prior.get("data") or {}).get("collect_at")
        if (prior.get("kind") == "input" and prior.get("state") != "retired" and target in nodes
                and prior["data"].get("name") == entries[entry_id]["data"].get("name")):
            problem["repair_hint"] = {"value": target, "basis": "same_input_in_previous_candidate",
                "requires_current_validation": True, "adopted": False}
    pending_count = len(materials.get("pending") or [])
    if pending_count:
        current["ready"] = False
        current["blocking_count"] += 1
        current["diagnostic_count"] += 1
        current["diagnostics"].insert(0, {"code": "pending_material_changes", "path": "materials/pending",
            "entry_ids": [], "message": f"还有 {pending_count} 项素材变更未解决，未视为已采用。"})
    request = design.get("build_request") or {}
    material_sequence = materials.get("sequence", 0)
    identity = {"task_id": design.get("task_id"), "task_revision": design.get("revision"),
        "material_sequence": material_sequence, "document_hash": digest(document), "base_hash": digest(base),
        "candidate_id": candidate.get("id"), "candidate_sequence": candidate.get("sequence"),
        "candidate_document_hash": digest(candidate["document"]) if candidate.get("document") else None}
    failure = {"present": request.get("state") == "failed", "status": "none"}
    if failure["present"]:
        same_sequence = request.get("sequence") == material_sequence if "sequence" in request else None
        same_base = request.get("base_graph_hash") == identity["base_hash"] if request.get("base_graph_hash") else None
        prior_evidence = request.get("diagnosis_identity") or {}
        same_document = prior_evidence.get("document_hash") == identity["document_hash"] if prior_evidence else None
        failure.update(status="historical" if False in (same_sequence, same_base, same_document) else
                       "current_basis" if same_sequence is True and same_base is True else "unverified_basis",
            build_id=request.get("id"), material_sequence=request.get("sequence"),
            reason_code=(request.get("failure_evidence") or {}).get("reason") or request.get("failure_kind"),
            retryable=request.get("retryable"), automatically_cleared=False)
    runtime_summary = {key: copy.deepcopy(runtime[key]) for key in
        ("schema", "fingerprint", "status", "changed_symbols", "unverified_symbols")}
    candidate_differs = bool(candidate.get("document") and
                             identity["candidate_document_hash"] != identity["document_hash"])
    if runtime["status"] == "restart_required":
        next_action = "reload_runtime"
        summary = "运行中的诊断或审查代码与磁盘不同，需加载修复后重新诊断。"
    elif current["blocking_count"]:
        next_action = "repair_current_materials"
        summary = f"当前素材有 {current['blocking_count']} 项结构阻塞；先修正这些字段，再重新校验。"
    elif failure["present"] and failure["status"] != "historical":
        next_action = "inspect_current_failure"
        summary = "当前结构校验通过；已有失败仍需按其证据处理，不能以结构通过代替审查。"
    elif candidate_differs or failure["status"] == "historical":
        next_action = "review_current_basis"
        summary = "当前素材结构校验通过，候选或失败记录属于不同版本；应以当前证据重新审查构图交接。"
    else:
        next_action = "continue_normal_checks"
        summary = "当前素材结构校验通过；后续覆盖审查、保存和执行仍分别核验。"
    report = {"schema": "mewcode.design-health.v1", "identity": identity,
        "current_validation": current, "candidate_differs": candidate_differs,
        "previous_failure": failure, "runtime": runtime_summary, "next_action": next_action,
        "summary": summary, "execution_authorized": False,
        "retry_authorized": False, "read_only": True}
    report["fingerprint"] = digest(report)
    return report


def compact_health(report):
    if not report:
        return None
    return {key: copy.deepcopy(report[key]) for key in ("schema", "fingerprint", "identity",
        "current_validation", "candidate_differs", "previous_failure", "runtime", "next_action", "summary",
        "read_only", "execution_authorized", "retry_authorized")}
