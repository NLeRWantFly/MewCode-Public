"""Explicit, bounded adoption of a saved plan into a stopped run."""
from __future__ import annotations

import copy

from mewcode.orchestration.revisions import execution_fingerprint
from mewcode.orchestration.loops import LoopContract, LoopWatchdog


def revision_preview(snapshot, flow, *, repair_loop_id=""):
    new = flow.as_dict() if hasattr(flow, "as_dict") else flow
    old = snapshot.get("locked_flow") or {}
    pending = snapshot.get("pending_dialogue_handoff") or {}
    if snapshot.get("status") not in {"paused", "blocked", "needs_attention"}:
        raise ValueError("请先暂停原任务")
    if (not repair_loop_id and pending.get("relation") != "plan_changed") or pending.get("invalidated_completed_node_ids"):
        raise ValueError("当前不是可接纳的未来阶段修订，或已完成成果需要重新确认")
    if snapshot.get("pending_recovery_revision") or snapshot.get("recovery_locked"):
        raise ValueError("请先处理现有审查修订")
    if not new or old.get("flow_id") != new.get("flow_id"):
        raise ValueError("新旧计划不属于同一执行图，不能接纳到原任务")
    for key in ("conversation_id", "pipeline_graph_id"):
        if (old.get("meta") or {}).get(key) != (new.get("meta") or {}).get(key):
            raise ValueError("保存计划的对话或画布归属发生变化")
    before = {n["node_id"]: n for n in old["nodes"]}
    after = {n["node_id"]: n for n in new["nodes"]}
    repair_extension = None
    if repair_loop_id:
        from mewcode.orchestration.repair_revision import validate_repair_extension
        repair_extension = validate_repair_extension(old, new, repair_loop_id)
    elif before.keys() != after.keys() or old.get("edges") != new.get("edges"):
        raise ValueError("节点或连线已变化，需要定向迁移")
    for key in ("finalizer_node_id", "finalizer_plugin"):
        if old.get(key) != new.get(key):
            raise ValueError("最终验收入口已变化，需要定向迁移")
    clean = lambda node: {k: v for k, v in node.items() if k != "position"}
    affected = {key for key in after if key not in before or clean(before[key]) != clean(after[key])}
    old_meta, new_meta = old.get("meta") or {}, new.get("meta") or {}
    # Only node plans and loop acceptance may change through this narrow path.
    for key in ("audit_rubrics", "shared_constraints", "constraint_pipelines", "node_constraints",
                "schedule_skipped_node_ids", "node_kinds", "parallel"):
        if key == "node_kinds" and repair_extension:
            continue  # Validated as a strict extension, not a change to existing roles.
        if old_meta.get(key) != new_meta.get(key):
            raise ValueError("共享约束或审查合同变化，需单独修订：" + key)
    old_loops = {v["loop_id"]: LoopContract.from_value(v) for v in old_meta.get("loops", [])}
    new_loops = {v["loop_id"]: LoopContract.from_value(v) for v in new_meta.get("loops", [])}
    if old_loops.keys() != new_loops.keys():
        raise ValueError("循环结构变化，需要定向迁移")
    changed_loops = []
    for key, previous in old_loops.items():
        current = new_loops[key]
        if previous.contract_hash == current.contract_hash:
            continue
        a, b = previous.as_dict(), current.as_dict()
        if key == repair_loop_id:
            affected.update(current.node_ids)
            changed_loops.append(key)
            continue
        if {k: v for k, v in a.items() if k not in {"exit_condition", "criteria"}} != {
                k: v for k, v in b.items() if k not in {"exit_condition", "criteria"}}:
            raise ValueError("循环结构、队列或限额变化，不能随验收修订隐式修改")
        affected.update(current.node_ids)
        changed_loops.append(key)
    if not affected:
        raise ValueError("未发现可接纳的执行差异，请核对当前保存计划")
    states = snapshot.get("node_states") or snapshot.get("stages") or {}
    completed = {key for key, state in states.items() if state.get("status") == "completed"}
    from mewcode.expert_runs import successors_after_node
    for key in tuple(affected):
        affected.update(successors_after_node(new, key))
    if affected & completed:
        raise ValueError("修订影响已完成节点，需明确重跑范围：" + "、".join(sorted(affected & completed)))
    reset_loops = sorted(set(changed_loops) | {
        key for key, contract in new_loops.items() if set(contract.node_ids) & affected})
    limits = []
    for key, state in (snapshot.get("loop_states") or {}).items():
        loop_id = key.split("/")[-1]
        if loop_id not in reset_loops:
            continue
        if "/" in key or state.get("completed_items") or state.get("status") == "completed":
            raise ValueError("已有循环完成结果或嵌套循环，需要定向迁移")
        watchdog = LoopWatchdog(old_loops[loop_id], state)
        if watchdog.remaining():
            limits.append(loop_id + "：" + watchdog.remaining())
    return {"fingerprint": execution_fingerprint(new), "expected_updated_at": snapshot.get("updated_at"),
            "repair_extension": repair_extension,
            "affected_node_ids": sorted(affected), "changed_loop_ids": changed_loops,
            "reset_loop_ids": reset_loops, "remaining_blockers": limits,
            "loop_changes": [{"loop_id": key, "before": old_loops[key].as_dict(),
                              "after": new_loops[key].as_dict()} for key in changed_loops],
            "node_changes": [{"node_id": key, "before": clean(before.get(key, {})), "after": clean(after[key])}
                             for key in sorted(affected) if clean(before.get(key, {})) != clean(after[key])],
            "from_version": old.get("version"), "to_version": new.get("version"),
            "summary": "接纳已保存计划；保留前置成果与累计预算，受影响阶段重新核验。接纳后仍保持暂停。"}


def adopt_revision(snapshot, flow, expected_updated_at, fingerprint, *, repair_loop_id="", root=None):
    if str(snapshot.get("updated_at")) != expected_updated_at:
        raise ValueError("任务状态已变化，请重新核对修订")
    preview = revision_preview(snapshot, flow, repair_loop_id=repair_loop_id)
    if preview["fingerprint"] != fingerprint:
        raise ValueError("保存计划已变化，请重新核对修订")
    new = flow.as_dict() if hasattr(flow, "as_dict") else flow
    result = copy.deepcopy(snapshot)
    affected = set(preview["affected_node_ids"])
    extension = preview["repair_extension"]
    for node_id in affected:
        if extension and node_id == extension["reviewer_node_id"]:
            from mewcode.orchestration.repair_revision import migrate_review_queue
            preview["queue_migration"] = migrate_review_queue(result, node_id, extension, root)
            continue
        previous_queue = result.get("review_queues", {}).pop(node_id, None)
        if previous_queue:
            result.setdefault("review_queue_archives", []).append({"node_id": node_id, "queue": previous_queue})
    loops = {v["loop_id"]: LoopContract.from_value(v) for v in (new.get("meta") or {}).get("loops", [])}
    for key, state in list(result.get("loop_states", {}).items()):
        if key not in preview["reset_loop_ids"]:
            continue
        contract = loops[key]
        # Carry counters and explicit run policy; no old verdict may certify the new contract.
        fresh = LoopWatchdog(contract).state
        for field in ("round", "tokens", "elapsed", "stalled", "history"):
            if field in state:
                fresh[field] = copy.deepcopy(state[field])
        fresh["contract_snapshot"] = contract.as_dict()
        for field in ("loop_control_policy", "loop_resource_policy"):
            if state.get(field):
                fresh[field] = {**state[field], "contract_hash": contract.contract_hash}
        fresh.update(status="blocked", reason="计划修订已接纳，等待继续任务")
        result["loop_states"][key] = fresh
    for field in ("node_states", "stages"):
        for key in affected:
            state = result.setdefault(field, {}).setdefault(key, {})
            if state is not None:
                state.update(status="pending", passed=False, status_reason="计划修订后待重新核验")
                for name in ("verdict", "loop_decision", "handoff", "audit_snapshot"):
                    state.pop(name, None)
    for field in ("pipeline_runs", "gate_confirmations", "audit_protocols", "locked_models"):
        for key in affected:
            result.get(field, {}).pop(key, None)
    from mewcode.expert_runs import expert_flow_fingerprint
    result.update(locked_flow=copy.deepcopy(new), flow_id=new["flow_id"], flow_version=new["version"],
                  saved_plan_fingerprint=fingerprint,
                  flow_fingerprint=expert_flow_fingerprint(flow), active_node_ids=[],
                  status_reason="已接纳保存计划修订，可继续原任务")
    result.pop("flow_state", None)
    if extension:
        result.update(status="paused", stage_cursor=extension["repair_node_id"])
    decision = result.get("pending_dialogue_handoff") or {}
    result["pending_dialogue_handoff"] = {**decision, "action": "resume", "relation": "context_changed",
        "summary": "已核验并接纳保存计划修订", "accepted_plan_fingerprint": fingerprint}
    result.setdefault("saved_plan_adoptions", []).append(preview)
    return result
