"""Explicit, deterministic copies; the original classic run stays selectable."""
from __future__ import annotations

import copy

from mewcode.experts.flow import ExpertFlowSpec
from mewcode.kernel import collaboration as ledger
from mewcode.kernel.events import digest


def copy_run(store, source, *, flow, workspace, request_id):
    """Copy reviewed design plus evidence, with no model calls or business dispatch.

    The caller supplies a source-linked collaborative design. Unknown legacy
    requirements must first be mapped there; no model invents their authority.
    A copied delivery is evidence awaiting the new contract's acceptance.
    """
    if not request_id or source.get("orchestration_mode", "classic") != "classic":
        raise ValueError("复制需要明确请求身份及经典运行")
    selected = ExpertFlowSpec.from_value(flow)
    if ledger.flow_mode(selected.as_dict()) != "collaborative":
        raise ValueError("复制目标必须是含来源和验收标准的协作规格")
    source_hash = digest(source)
    target = digest(["copy-to-collaboration", source["run_id"], request_id])[:32]
    existing = store.load(target)
    if existing:
        previous_hash = existing.get("collaboration_import", {}).get("source_hash") or existing.get("migration", {}).get("source_hash")
        if previous_hash != source_hash:
            raise ValueError("源运行已变化，请为新的复制使用新的请求身份")
        if existing.get("collaboration_import"):
            return existing
    # Preserve a recoverable immutable source in the same host journal before
    # exposing the copy. Authorization budgets are not duplicated into it.
    result = existing or store.create(run_id=target, conversation_id=source["conversation_id"], task=source.get("task", ""),
        attachments=copy.deepcopy(source.get("attachments", [])), profile=copy.deepcopy(source.get("locked_profile", {})),
        strategy=source.get("strategy", "stable"), flow=selected,
        initial_state={"status": "paused", "migration": {"kind": "classic_to_collaborative", "source_run_id": source["run_id"], "source_hash": source_hash}})
    def import_evidence(record):
        from mewcode.kernel.migration import import_call
        from mewcode.kernel.capabilities import compile_manifest
        value = ledger.state(record)
        record["collaboration_import"] = {"source_hash": source_hash, "source_run_id": source["run_id"],
            "source": copy.deepcopy(source), "model_calls": 0}
        record["authorization_manifest"] = compile_manifest(selected, workspace=workspace, approved=False)
        record["status_reason"] = "经典运行已复制为暂停的协作运行；原成果作为待验收证据保留，请核对规格和授权后继续。"
        value["imported_deliveries"] = {key: copy.deepcopy(stage) for key, stage in source.get("node_states", {}).items()
                                       if stage.get("handoff") or stage.get("text")}
        unresolved = []
        for key, stage in source.get("node_states", {}).items():
            for receipt in stage.get("command_receipts", []):
                imported = import_call(record, key, receipt, source_hash)
                if receipt.get("status") in {"outcome_unknown", "in_flight", "effect_partial"}:
                    unresolved.append({"node_id": key, "receipt": copy.deepcopy(receipt), "mapped": imported})
        if unresolved:
            value["blocked"] = "历史调用效果尚未核对，复制不授权重复操作"
            value["unresolved_imports"] = unresolved
        ledger.event(value, "classic_copied", source_run_id=source["run_id"], source_hash=source_hash)
    return store.mutate(result["run_id"], import_evidence, durable=True)
