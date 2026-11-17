"""Adopt maintenance into untouched future stages while preserving host receipts."""
from __future__ import annotations

import copy

from mewcode.orchestration.convert import to_expert_flow
from mewcode.orchestration.revisions import execution_fingerprint


def changed_execution_nodes(before, after):
    """Include actual scheduling parents when a visible refinement is edited."""
    old = before.as_dict() if hasattr(before, "as_dict") else before or {}
    new = after.as_dict() if hasattr(after, "as_dict") else after
    clean = lambda node: {key: value for key, value in node.items() if key != "position"}
    a = {node["node_id"]: clean(node) for node in old.get("nodes", [])}
    b = {node["node_id"]: clean(node) for node in new.get("nodes", [])}
    affected = {identity for identity in a.keys() | b.keys() if a.get(identity) != b.get(identity)}
    old_edges, new_edges = old.get("edges") or [], new.get("edges") or []
    affected.update(edge["to_node"] for edge in [*old_edges, *new_edges] if edge not in old_edges or edge not in new_edges)
    old_loops = {loop["loop_id"]: loop for loop in (old.get("meta") or {}).get("loops", [])}
    new_loops = {loop["loop_id"]: loop for loop in (new.get("meta") or {}).get("loops", [])}
    for identity in old_loops.keys() | new_loops.keys():
        if old_loops.get(identity) != new_loops.get(identity):
            affected.update((old_loops.get(identity) or {}).get("node_ids") or [])
            affected.update((new_loops.get(identity) or {}).get("node_ids") or [])
    return affected


def execution_direction(snapshot):
    """Only an applied host maintenance record may update the stage input."""
    adopted = {item.get("maintenance_id") for item in snapshot.get("maintenance_adoptions") or [] if isinstance(item, dict)}
    for item in reversed(snapshot.get("supplemental_requirements") or []):
        if not isinstance(item, dict):
            continue
        source, text = item.get("source"), item.get("text")
        if (isinstance(source, str) and source.startswith("maintenance:") and source[12:] in adopted
                and isinstance(text, str) and text.strip()):
            return text
    return ""


def _selected_run_actions(actions, confirmed_ids):
    from mewcode.orchestration.maintenance import confirmed_run_action_ids
    if isinstance(confirmed_ids, (tuple, set, frozenset)):
        confirmed_ids = list(confirmed_ids)
    selected = confirmed_run_action_ids(actions or [], confirmed_ids)
    return [item for item in actions or []
            if item.get("requires_confirmation") is not True or item.get("id") in selected]


def apply_spec_run_actions(spec, actions, confirmed_ids=()):
    """Apply only user-confirmed run actions that change the saved graph."""
    from mewcode.orchestration.spec import OrchestrationSpec
    value = spec.as_dict() if hasattr(spec, "as_dict") else copy.deepcopy(spec)
    nodes = {node["node_id"]: node for node in value.get("nodes", [])}
    for action in _selected_run_actions(actions, confirmed_ids):
        kind, node_id = action.get("action"), action.get("node_id")
        if kind == "set_node_tools":
            if node_id not in nodes or not isinstance(action.get("tools"), list):
                raise ValueError("确认的节点工具动作与预览不一致。")
            nodes[node_id]["tools"] = list(action["tools"])
        elif kind == "set_known_inputs":
            if node_id not in nodes or not isinstance(action.get("known_inputs"), list):
                raise ValueError("确认的节点已知输入动作与预览不一致。")
            brief = copy.deepcopy(nodes[node_id].get("plan_brief") or {})
            brief["known_inputs"] = list(action["known_inputs"])
            nodes[node_id]["plan_brief"] = brief
        elif kind == 'extend_authorization':
            if node_id not in nodes:
                raise ValueError('授权扩展节点不存在。')
            nodes[node_id]['capabilities'] = copy.deepcopy(action['capabilities'])
    return OrchestrationSpec.from_value(value)


def _receipt_fact(view, receipt_id):
    matches = []
    for fact in view.current_receipts:
        payload = fact.payload
        identities = {fact.fact_id, payload.get("request_id"), payload.get("receipt_id"), payload.get("id")}
        if receipt_id in identities:
            matches.append(fact)
    if len(matches) != 1:
        raise ValueError("维护动作引用的回执不存在、已归档或不唯一：" + str(receipt_id))
    return matches[0]


def _register_effect_verification(record, node_id, receipt, *, maintenance_id):
    from mewcode.experts.node_ledger import ledger_for_run, project_node_view
    ledger = ledger_for_run(record)
    receipt_id = receipt.payload.get("request_id") or receipt.fact_id
    ledger.record(node_id, "blocker", {"kind": "effect_unknown", "receipt_id": receipt.fact_id,
        "request_id": receipt_id, "reason": "维护请求了独立效果核验；核验完成前不得重放此调用"},
        source="host", identity=["maintenance-effect-unknown", maintenance_id, receipt.fact_id])
    ledger.record(node_id, "assistance", {"kind": "effect_verification", "receipt_id": receipt.fact_id,
        "request_id": receipt_id, "status": "requested", "maintenance_id": maintenance_id},
        source="host", identity=["maintenance-effect-verification", maintenance_id, receipt.fact_id])
    ledger.set_phase(node_id, "escalated", reason="maintenance_effect_verification_pending")
    project_node_view(record, node_id)
    record.setdefault("node_states", {}).setdefault(node_id, {})["status"] = "needs_attention"


def _fallback_reclassify_receipt(record, node_id, fact, *, maintenance_id):
    """Narrow host-only fallback while the shared receipt resolver is unavailable."""
    from mewcode.experts.node_ledger import ledger_for_run, project_node_view
    payload = copy.deepcopy(dict(fact.payload))
    if (payload.get("effect_class") == "readonly"
            and payload.get("status") in {"outcome_unknown", "in_flight"}):
        payload.update(status="not_started", command_started=False, retry_reserved=True, retry_count=1,
            evidence_source="host_effect_reclassification",
            reason="宿主 effect_class=readonly；回执重分类确认无写入副作用，允许一次同代际重试")
        stage = record.setdefault("node_states", {}).setdefault(node_id, {})
        rows = stage.setdefault("command_receipts", [])
        request_id = payload.get("request_id")
        for index, old in enumerate(rows):
            if old.get("request_id") == request_id or old.get("receipt_id") == fact.fact_id:
                rows[index] = copy.deepcopy(payload)
                break
        else:
            rows.append(copy.deepcopy(payload))
        ledger = ledger_for_run(record)
        ledger.record(node_id, "receipt", payload, source="host",
            identity=["maintenance-reclassify", maintenance_id, fact.fact_id])
        from mewcode.experts.blocking_policy import refresh_clearances
        refresh_clearances(record, node_id)
        project_node_view(record, node_id)
        return {"status": "reclassified", "receipt_id": fact.fact_id, "effect_class": "readonly"}
    _register_effect_verification(record, node_id, fact, maintenance_id=maintenance_id)
    return {"status": "verification_required", "receipt_id": fact.fact_id}


def _resolve_receipt_action(record, node_id, receipt_id, *, mode, maintenance_id):
    from mewcode.experts.node_ledger import ledger_for_run, project_node_view, sync_node_facts
    sync_node_facts(record, node_id, source="host", include_legacy_blockers=True)
    fact = _receipt_fact(ledger_for_run(record).view(node_id), receipt_id)
    if mode == "verify":
        _register_effect_verification(record, node_id, fact, maintenance_id=maintenance_id)
        return {"status": "verification_required", "receipt_id": fact.fact_id}
    if mode != "reclassify":
        raise ValueError("回执处理模式无效。")
    # The shared resolver owns T7 policy and may be supplied by the central
    # execution-recovery integration. Never infer effect from command text here.
    resolver = None
    for module_name in ("mewcode.experts.command_receipts",):
        try:
            module = __import__(module_name, fromlist=["resolve_unknown_receipt"])
        except ImportError:
            continue
        resolver = getattr(module, "resolve_unknown_receipt", None)
        if callable(resolver):
            break
        resolver = None
    if resolver is not None:
        result = resolver(record, node_id, receipt_id, verification=None)
        project_node_view(record, node_id)
        return result if isinstance(result, dict) else {"status": "reclassified", "receipt_id": fact.fact_id}
    return _fallback_reclassify_receipt(record, node_id, fact, maintenance_id=maintenance_id)


def apply_run_actions(snapshot, actions, confirmed_ids=(), *, maintenance_id):
    """Apply host-owned run facts on a candidate copy; caller persists by CAS."""
    from mewcode.experts.node_ledger import ledger_for_run, project_node_view, sync_node_facts, bump_run_epoch
    selected = _selected_run_actions(actions, confirmed_ids)
    candidate = copy.deepcopy(snapshot)
    all_nodes = set(candidate.get("node_states") or {}) | set(candidate.get("stages") or {})
    rebase_actions = []
    receipt_nodes = set()
    touched_nodes = set()
    outcomes = []
    preserved_rebases = []
    from mewcode.experts.node_ledger import NodeLedger
    existing_ledger = NodeLedger(copy.deepcopy(candidate.get("node_ledgers") or {}))
    for action in selected:
        kind = action.get("action")
        node_id = action.get("node_id")
        if node_id:
            if node_id not in all_nodes:
                raise ValueError("运行维护动作引用了不属于当前运行的节点：" + str(node_id))
            if kind in {"rebase_node", "bump_epoch"}:
                stage = (candidate.get("node_states") or candidate.get("stages") or {}).get(node_id) or {}
                view = existing_ledger.view(node_id)
                has_evidence = bool(view.artifacts or any(
                    fact.kind == "handoff" and fact.payload.get("status") == "completed" for fact in view.history))
                if view.phase == "done" or stage.get("status") == "completed" or stage.get("passed") is True:
                    preserved_rebases.append({"id": action["id"], "node_id": node_id,
                        "status": "preserved", "reason": "已完成节点的成果和成功回执保持原样。" if has_evidence else "已完成节点不自动重置。"})
                    continue
            sync_node_facts(candidate, node_id, source="host", include_legacy_blockers=True)
            touched_nodes.add(node_id)
        if kind == "set_run_inputs":
            from mewcode.orchestration.run_inputs import resolve_run_inputs
            flow = candidate.get("locked_flow") or {}
            declarations = (flow.get("meta") or {}).get("run_input_contract") or []
            values = {**(candidate.get("run_inputs") or {}), **action.get("values", {})}
            candidate["run_inputs"] = resolve_run_inputs(declarations, values)
            outcomes.append({"id": action["id"], "status": "applied"})
        elif kind in {'adjudicate_unknown', 'extend_authorization'}:
            from mewcode.kernel.repairs import adjudicate, extend_manifest
            if kind == 'adjudicate_unknown':
                outcome = adjudicate(candidate, node_id=node_id, call_id=action['call_id'],
                    disposition=action['disposition'], source_ref=maintenance_id + ':' + action['id'])
            else:
                outcome = extend_manifest(candidate, node_id=node_id, capabilities=action['capabilities'],
                    source_ref=maintenance_id + ':' + action['id'])
            outcomes.append({'id': action['id'], **outcome})
        elif kind == "resolve_receipts":
            receipt_nodes.add(node_id)
            for receipt_id in action["receipt_ids"]:
                outcome = _resolve_receipt_action(candidate, node_id, receipt_id,
                    mode=action["mode"], maintenance_id=maintenance_id)
                outcomes.append({"id": action["id"], **outcome})
        elif kind in {"close_case", "extend_rounds"}:
            from mewcode.experts.case_runtime import answer_case
            from mewcode.experts.recovery_case import get_case, save_case
            case = get_case(candidate, action['case_id'])
            if not case or case['node_id'] != node_id:
                raise ValueError('案件不属于当前节点')
            case['pending'] = {'kind': 'incident', 'options': [{'value': 'close'}, {'value': 'retry'}]}
            save_case(candidate, case)
            answer_case(candidate, case['case_id'], round=action['round'],
                choice='close' if kind == 'close_case' else 'retry', answer=action.get('direction', ''))
            outcomes.append({'id': action['id'], 'node_id': node_id, 'status': 'applied'})
        elif kind in {"rebase_node", "bump_epoch"}:
            rebase_actions.append(action)

    rebased = set()
    deferred = list(preserved_rebases)
    for action in rebase_actions:
        node_id = action["node_id"]
        view = ledger_for_run(candidate).view(node_id)
        stage = (candidate.get("node_states") or candidate.get("stages") or {}).get(node_id) or {}
        has_evidence = bool(view.artifacts or any(
            fact.kind == "handoff" and fact.payload.get("status") == "completed" for fact in view.history))
        from mewcode.experts.blocking_policy import BLOCKER_CLASS
        authority = [fact for fact in view.active_blockers
                     if BLOCKER_CLASS.get(str(fact.payload.get("kind") or ""), "business") == "authority"]
        if authority or view.pending_question is not None:
            deferred.append({"id": action["id"], "node_id": node_id,
                "status": "deferred", "reason": "现有权限或用户问题未解决，保持节点阻塞。"})
            continue
        if view.phase not in {"escalated", "waiting_human", "failed"} and not view.active_blockers and stage.get("status") not in {
                "blocked", "needs_attention", "failed", "paused", "stalled", "budget_exhausted", "cancelled"}:
            raise ValueError("仅可为卡住或有活动阻塞的节点开启新代际：" + node_id)
        epoch = bump_run_epoch(candidate, node_id, reason="maintenance_applied",
            source_ref=maintenance_id)
        rebased.add(node_id)
        outcomes.append({"id": action["id"], "node_id": node_id, "status": "rebased", "epoch": epoch})
    for node_id in touched_nodes | rebased | receipt_nodes:
        project_node_view(candidate, node_id)
    return {"snapshot": candidate, "rebased_node_ids": sorted(rebased),
            "receipt_action_node_ids": sorted(receipt_nodes), "outcomes": outcomes + deferred,
            "deferred_rebases": deferred}


def require_adopted_maintenance(snapshot, flow):
    """Compatibility wrapper for the structured adoption-choice contract."""
    from mewcode.experts.maintenance_adoption import require_adopted_maintenance as require_choice
    return require_choice(snapshot, flow)


def future_revision(snapshot, spec, *, affected, maintenance_id, base_spec=None, direction="",
                    rebased_node_ids=()):
    if snapshot.get("status") not in {"paused", "blocked", "needs_attention"} or snapshot.get("active_node_ids"):
        raise ValueError("原运行仍在执行，请先暂停后接纳修订。")
    if snapshot.get("pending_recovery_revision") or snapshot.get("recovery_locked"):
        raise ValueError("原运行已有待审查修订，请先处理。")
    flow = to_expert_flow(spec)
    old, new = snapshot.get("locked_flow") or {}, flow.as_dict()
    if base_spec is not None and execution_fingerprint(to_expert_flow(base_spec)) != execution_fingerprint(old):
        raise ValueError("原运行与维护预览的基准已有其他执行差异，未暗中接纳其它修订。")
    if old.get("flow_id") != new["flow_id"]:
        raise ValueError("原运行和修订图不属于同一流程。")
    for key in ("conversation_id", "pipeline_graph_id"):
        if (old.get("meta") or {}).get(key) != new.get("meta", {}).get(key):
            raise ValueError("原运行和修订图的所属对话或画布不同。")
    if {n["node_id"] for n in old.get("nodes", [])} != {n["node_id"] for n in new["nodes"]}:
        raise ValueError("原运行节点集合不同，需要单独迁移。")
    affected = set(affected) | changed_execution_nodes(old, new)
    from mewcode.expert_runs import successors_after_node
    for identity in tuple(affected):
        affected.update(successors_after_node(old, identity))
    rebased_node_ids = set(rebased_node_ids or ())
    if rebased_node_ids - affected:
        raise ValueError("代际重开节点不属于本次维护影响范围。")
    states = snapshot.get("node_states") or snapshot.get("stages") or {}
    from mewcode.experts.node_ledger import NodeLedger, successful_receipt
    ledger = NodeLedger(copy.deepcopy(snapshot.get("node_ledgers") or {}))
    for identity in affected:
        state = states.get(identity) or {}
        view = ledger.view(identity)
        completed_with_evidence = (view.phase == "done" and bool(
            view.artifacts or any(successful_receipt(fact.payload) for fact in view.current_receipts)
            or any(fact.kind == "handoff" and fact.payload.get("status") == "completed" for fact in view.history)))
        if state.get("status") == "completed" or state.get("passed") is True or completed_with_evidence:
            raise ValueError("修订影响已完成成果，未改动原运行：" + identity)
        from mewcode.experts.blocking_policy import BLOCKER_CLASS
        if view.pending_question or any(BLOCKER_CLASS.get(str(fact.payload.get("kind") or ""), "business") == "authority"
                                        for fact in view.active_blockers):
            raise ValueError("受影响节点仍有未解决的权限阻塞或用户问题，原运行保持原样：" + identity)
        if identity not in rebased_node_ids and (state.get("tool_calls") or state.get("handoff") or state.get("command_receipts")
                or (snapshot.get("pipeline_runs") or {}).get(identity)
                or (snapshot.get("command_receipts") or {}).get(identity)
                or (snapshot.get("review_queues") or {}).get(identity)
                or (snapshot.get("review_queue_refs") or {}).get(identity)):
            raise ValueError("受影响阶段已有执行记录，须先核对结果，未重置或重放：" + identity)
    from mewcode.orchestration.loops import LoopContract
    old_loops = {loop["loop_id"]: LoopContract.from_value(loop) for loop in (old.get("meta") or {}).get("loops", [])}
    new_loops = {loop["loop_id"]: LoopContract.from_value(loop) for loop in (new.get("meta") or {}).get("loops", [])}
    if old_loops.keys() != new_loops.keys():
        raise ValueError("原运行与修订图的循环集合不同，需要定向迁移。")
    reset_loops = []
    for key, state in (snapshot.get("loop_states") or {}).items():
        identity = key.split("/")[-1]
        members = (set(old_loops[identity].node_ids) | set(new_loops[identity].node_ids)) if identity in old_loops else set()
        if not members & affected:
            continue
        if ("/" in key or state.get("status") == "completed" or any(state.get(field) for field in (
                "round", "tokens", "elapsed", "history", "completed_items", "scores", "evidence_history"))):
            raise ValueError("受影响循环已有执行进度，需定向迁移，预算和成果保持原样。")
        reset_loops.append(key)
    candidate = copy.deepcopy(snapshot)
    for identity in reset_loops:
        state, contract = candidate["loop_states"][identity], new_loops[identity]
        # Only a loop with no execution/evidence can rebind here. Keep its
        # counters, approved ceilings, token window and loaded input queue.
        state.update(contract_hash=contract.contract_hash, contract_snapshot=contract.as_dict(),
                     status="blocked", reason="维护修订已接纳，等待继续任务")
        for field in ("loop_control_policy", "loop_resource_policy"):
            if state.get(field):
                state[field]["contract_hash"] = contract.contract_hash
    # Changed bindings for untouched future nodes must override both node and
    # legacy role locks; otherwise the saved model edit would never execute.
    old_nodes = {node["node_id"]: node for node in old.get("nodes", [])}
    for node in new["nodes"]:
        if node.get("model") != old_nodes[node["node_id"]].get("model"):
            candidate.setdefault("locked_models", {})[node["node_id"]] = copy.deepcopy(node["model"])
    # Keep original observations, budgets, other model locks and approvals.
    # Only pending/failed stages with no execution receipts can adopt this revision.
    for field in ("node_states", "stages"):
        for identity in affected:
            state = candidate.setdefault(field, {}).get(identity)
            if isinstance(state, dict):
                state.update(status="pending", passed=False, status_reason="维护修订后待核验")
    from mewcode.expert_runs import expert_flow_fingerprint
    candidate.update(locked_flow=new, flow_version=new["version"], flow_id=new["flow_id"],
        flow_fingerprint=expert_flow_fingerprint(flow), saved_plan_fingerprint=execution_fingerprint(flow),
        status="paused", active_node_ids=[], status_reason="维护修订已接纳，等待继续任务")
    candidate.pop("flow_state", None)
    if direction:
        from mewcode.orchestration.maintenance import now
        candidate.setdefault("supplemental_requirements", []).append({
            "attempt": int(candidate.get("attempt") or 1), "text": "已应用的当前业务方向：" + direction,
            "created_at": now(), "source": "maintenance:" + maintenance_id})
    candidate.setdefault("maintenance_adoptions", []).append({"maintenance_id": maintenance_id,
        "affected_node_ids": sorted(affected), "from_version": old.get("version"), "to_version": new["version"]})
    return candidate
