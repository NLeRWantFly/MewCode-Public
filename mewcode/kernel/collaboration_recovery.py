"""Versioned host recovery gates; model proposals cannot answer these gates."""
from __future__ import annotations

import copy

from mewcode.kernel import collaboration as ledger
from mewcode.kernel import lineage_budget
from mewcode.kernel.events import digest

_BUDGET_KINDS = {"attempts", "revisions", "expansions"}
_BUDGET_SCOPES = {"root", "lineage"}
_COORDINATOR_ACTIONS = {"resume_coordinator", "switch_coordinator"}
_COORDINATOR_MODEL_FIELDS = {"provider_key", "model_id", "effort"}


def _validated_coordinator_model(model):
    """Strict coordinator binding from the user; never an executor model."""
    if not isinstance(model, dict) or set(model) != _COORDINATOR_MODEL_FIELDS:
        raise ValueError("换绑协调者需要严格的模型标识（provider_key、model_id、effort）")
    if not isinstance(model["provider_key"], str) or not model["provider_key"].strip():
        raise ValueError("协调者模型的服务商标识无效")
    if not isinstance(model["model_id"], str) or not model["model_id"].strip():
        raise ValueError("协调者模型的模型标识无效")
    if not isinstance(model["effort"], str):
        raise ValueError("协调者模型的推理设置无效")
    from mewcode.model_registry import ModelRef
    return ModelRef.from_value(model).as_dict()


def unresolved_calls(record, keys):
    return [call for call in record.get("kernel_calls", {}).values()
            if call.get("state") in {"dispatched", "unknown"}
            and any(call.get("node_id") == key or str(call.get("node_id", "")).startswith(key + "__")
                    or str(call.get("node_id", "")).startswith(key + "::") for key in keys)]


def latest_attempt(value, key):
    return max((row for row in value["attempts"].values() if row["contract_id"] == key),
               key=lambda row: row["number"], default=None)


def _supersede_unstarted_assistance(record, node_id, decision_id):
    """An explicit retry returns unstarted technical assistance to its owner.

    This records a user routing decision, not a successful helper receipt or
    a capability clearance. The original execution still checks its authority,
    current tool guards and budgets; started/unknown effects stay unresolved.
    """
    from mewcode.experts.node_ledger import ledger_for_run
    from mewcode.kernel.blockers import policy

    stage = record.get('node_states', {}).get(node_id, {})
    request = stage.get('assistance_request') or {}
    request_id = request.get('request_id')
    if (not request_id or request.get('status') != 'pending'
            or request.get('node_id') != node_id or request.get('origin') != 'node'
            or request.get('kind') not in {'capability', 'environment', 'diagnosis'}
            or request.get('scope_requests') or stage.get('pending_permission')
            or stage.get('pending_interaction') or record.get('pending_interaction')
            or unresolved_calls(record, [node_id])
            or (stage.get('assistance_checkpoint') or {}).get('request_id') == request_id
            or any(row.get('request_id') == request_id for row in stage.get('assistance_receipts', []))
            or node_id + '__assist_' + request_id[:12] in record.get('node_states', {})):
        return
    facts = ledger_for_run(record)
    view = facts.view(node_id)
    attempt = latest_attempt(ledger.state(record), node_id)
    if (request.get('epoch') != view.epoch or view.pending_question
            or any(policy(f.payload['kind']).category in {'authority', 'business'} for f in view.active_blockers)
            or not attempt or attempt.get('delivery', {}).get('error_kind') != 'assistance_requested'
            or not any(f.epoch == view.epoch and not f.superseded_by and f.source == 'node'
                       and f.kind == 'assistance' and f.payload.get('request_id') == request_id
                       and f.payload.get('status') == 'requested' for f in view.history)):
        return
    request.update(status='superseded', superseded_by_decision=decision_id)
    facts.record(node_id, 'assistance', {'request_id': request_id, 'status': 'superseded',
        'decision_id': decision_id, 'reason': 'explicit_node_retry'},
        identity=['assistance_retry', request_id, decision_id])
    stage['assistance_resume_message'] = (
        '用户已明确选择由原执行节点重试，尚未启动的技术协助请求已保留并替代。'
        '复用已保存的交付正文和工具证据，先核对当前状态，再完成未完成项。'
        '这不是写入成功或权限放行回执；每次操作仍须通过当前授权、工具检查和剩余预算。')


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _validated_budget(budget):
    """Host-derived metadata from ``BudgetExhausted``; never a model self-report."""
    if not isinstance(budget, dict) or set(budget) != {"kind", "scope", "lineage_id", "requested", "available"}:
        raise ValueError("预算耗尽的关口需要宿主提供的预算元数据")
    kind, scope = budget.get("kind"), budget.get("scope")
    lineage_id = budget.get("lineage_id")
    requested, available = budget.get("requested"), budget.get("available")
    if not isinstance(kind, str) or not isinstance(scope, str) or kind not in _BUDGET_KINDS or scope not in _BUDGET_SCOPES:
        raise ValueError("预算元数据的类型或范围无效")
    if not isinstance(lineage_id, str) or not lineage_id:
        raise ValueError("预算元数据的谱系标识无效")
    if not _is_int(requested) or requested <= 0 or not _is_int(available) or available < 0:
        raise ValueError("预算元数据的数量无效")
    return {"kind": kind, "scope": scope, "lineage_id": lineage_id,
            "requested": requested, "available": available}


def _budget_projection(value, budget):
    """Only the counters that the gate's own scope and kind can move.

    A lineage gate must not be invalidated by an unrelated branch's usage.
    """
    book = lineage_budget.ensure(copy.deepcopy(value))
    kind = budget["kind"]
    limits = book.get("limits", {})
    grants = book.get("grants", {})
    if budget["scope"] == "root":
        usage = book.get("root", {})
        return {"limit": limits.get("root", {}).get(kind),
                "used": usage.get("used", {}).get(kind, 0),
                "reserved": usage.get("reserved", {}).get(kind, 0),
                "grants": {gid: copy.deepcopy(row) for gid, row in grants.items()
                           if row.get("kind") == kind and row.get("lineage_id") is None}}
    lineage = book.get("lineages", {}).get(budget["lineage_id"]) or {}
    return {**({'allocation_mode': 'elastic'} if lineage_budget.elastic(value) else {}),
            "limit": limits.get("lineage", {}).get(kind),
            "used": lineage.get("used", {}).get(kind, 0),
            "reserved": lineage.get("reserved", {}).get(kind, 0),
            "grants": {gid: copy.deepcopy(row) for gid, row in grants.items()
                       if row.get("kind") == kind and row.get("lineage_id") == budget["lineage_id"]}}


def _gate_basis_data(value, scope, keys, budget=None, *, execution_evidence_fingerprint=None):
    basis = {"spec_version": value["spec_version"], "epoch": value["coordinator_epoch"],
        "dependencies": {key: ledger.dependencies(value, key) for key in keys},
        "attempts": {key: latest_attempt(value, key) for key in keys},
        "integration": value.get("integrations", {}).get(scope),
        "scope_generation": value.get("integration_generations", {}).get(scope, 0),
        "retry_generations": {key: value.get("retry_generations", {}).get(key, 0) for key in keys}}
    if budget is not None:
        basis["budget"] = {"meta": copy.deepcopy(budget), "projection": _budget_projection(value, budget)}
    if execution_evidence_fingerprint is not None:
        basis["execution_evidence_fingerprint"] = execution_evidence_fingerprint
    return basis


def gate_basis(value, scope, keys, budget=None, *, execution_evidence_fingerprint=None):
    return digest(_gate_basis_data(value, scope, keys, budget,
        execution_evidence_fingerprint=execution_evidence_fingerprint))


def gate_current(value, gate):
    return (gate["status"] == "waiting"
            and gate["basis"] == gate_basis(value, gate["scope"], gate["node_ids"], gate.get("budget"),
                execution_evidence_fingerprint=gate.get("execution_evidence_fingerprint"))
            and not any(value["contracts"][key].get("held") for key in gate["node_ids"]))


def open_gate(record, *, scope, keys, label, reason, kind, budget=None, execution_evidence_fingerprint=None):
    value = ledger.state(record)
    if kind == "lineage_budget_exhausted":
        budget = _validated_budget(budget)
    elif budget is not None:
        raise ValueError("仅预算耗尽的关口可以携带预算元数据")
    if execution_evidence_fingerprint is not None and (kind not in {"execution_evidence_available", "infrastructure"}
            or not isinstance(execution_evidence_fingerprint, str) or len(execution_evidence_fingerprint) != 64
            or any(c not in "0123456789abcdef" for c in execution_evidence_fingerprint)):
        raise ValueError("执行回执重验需要有效的宿主证据指纹")
    if kind == "execution_evidence_available" and execution_evidence_fingerprint is None:
        raise ValueError("执行回执重验缺少宿主证据指纹")
    basis = gate_basis(value, scope, keys, budget, execution_evidence_fingerprint=execution_evidence_fingerprint)
    identity = digest([value["run_id"], scope, kind, basis])
    gates = value.setdefault("recovery_gates", {})
    if identity not in gates:
        for old in gates.values():
            if old["scope"] == scope and old["status"] == "waiting":
                old["status"] = "superseded"
        gates[identity] = {"id": identity, "scope": scope, "node_ids": list(keys), "label": label,
            "reason": reason, "kind": kind, "basis": basis, "spec_version": value["spec_version"], "status": "waiting"}
        if budget is not None:
            gates[identity]["budget"] = copy.deepcopy(budget)
        if execution_evidence_fingerprint is not None:
            gates[identity]["execution_evidence_fingerprint"] = execution_evidence_fingerprint
        ledger.event(value, "recovery_gate_opened", gate_id=identity, scope=scope, reason=reason)
    return copy.deepcopy(gates[identity])


def reconcile_attempt_order_gates(record):
    """Replace proven legacy ordering errors with fresh, unanswered gates.

    This is host reconciliation, never a user decision. The complete old basis
    must still match; unrelated stale gates, running work and unknown effects
    are left untouched. Old gate IDs remain unusable after reconciliation.
    """
    if (record.get("status") not in {"paused", "blocked", "needs_attention"}
            or record.get("active_node_ids") or not record.get("collaboration")):
        return []
    value, repaired = ledger.state(record), []
    for gate in list(value.get("recovery_gates", {}).values()):
        keys = gate.get("node_ids", [])
        if (gate.get("status") != "waiting" or gate.get("kind") != "infrastructure"
                or len(keys) != 1 or gate.get("scope") != "contract:" + keys[0]
                or gate_current(value, gate) or unresolved_calls(record, keys)
                or value["contracts"][keys[0]].get("held")):
            continue
        legacy = {key: next((row for row in reversed(list(value["attempts"].values()))
                             if row["contract_id"] == key), None) for key in keys}
        basis = _gate_basis_data(value, gate["scope"], keys, gate.get("budget"),
            execution_evidence_fingerprint=gate.get("execution_evidence_fingerprint"))
        current = basis["attempts"]
        if current == legacy:
            continue
        basis["attempts"] = legacy
        if digest(basis) != gate["basis"]:
            continue
        replacement_id = digest([value["run_id"], gate["scope"], gate["kind"],
                                 gate_basis(value, gate["scope"], keys)])
        if replacement_id in value.get("recovery_gates", {}):
            # Never resurrect or reinterpret an already recorded gate.
            continue
        replacement = open_gate(record, scope=gate["scope"], keys=keys, label=gate["label"],
            reason=gate["reason"], kind=gate["kind"])
        event = {"previous_gate_id": gate["id"], "gate_id": replacement["id"],
                 "previous_basis": gate["basis"], "basis": replacement["basis"],
                 "previous_attempt_ids": {key: row["id"] if row else None for key, row in legacy.items()},
                 "attempt_ids": {key: row["id"] if row else None for key, row in current.items()}}
        ledger.event(value, "recovery_gate_attempt_order_reconciled", **event)
        repaired.append(event)
    return repaired


def manual_candidate(value, gate):
    if gate["scope"].startswith("contract:"):
        attempt = latest_attempt(value, gate["node_ids"][0])
        if (not attempt or attempt["status"] not in {"rejected", "checking"}
                or (attempt.get("delivery") or {}).get("status") != "completed"):
            return None
        return attempt.get("acceptance_fault") if attempt["status"] == "checking" else attempt.get("report")
    integration = value.get("integrations", {}).get(gate["scope"])
    return integration.get("report") if integration else None


def verifier_switch_attempt(value, gate):
    """A verifier service failure may retry its fixed delivery, never the work."""
    if not gate.get("scope", "").startswith("contract:") or len(gate.get("node_ids", [])) != 1:
        return None
    attempt = latest_attempt(value, gate["node_ids"][0])
    if (not attempt or attempt.get("status") != "checking"
            or (attempt.get("delivery") or {}).get("status") != "completed"):
        return None
    from mewcode.kernel.collaboration_faults import TRANSIENT, REQUIRES_CLEARANCE, report_fault
    fault = report_fault(attempt.get("acceptance_fault") or {})
    return attempt if (fault in TRANSIENT | {'provider_cooldown'}
        and fault not in (REQUIRES_CLEARANCE - {'provider_cooldown'}) | {"safety_review_unavailable"}) else None


def execution_recheck_candidate(record, key):
    value = ledger.state(record)
    attempt = latest_attempt(value, key)
    if (not attempt or attempt.get("status") != "rejected" or value["contracts"][key].get("held")
            or (attempt.get("delivery") or {}).get("status") != "completed"
            or (attempt.get("report") or {}).get("passed") is not False
            or not (attempt.get("report") or {}).get("artifacts")
            or unresolved_calls(record, [key])):
        return None
    from mewcode.kernel.collaboration_faults import report_fault
    if report_fault(attempt["report"]):
        return None
    try:
        ledger.check_attempt(value, attempt["id"])
    except ValueError:
        return None
    from mewcode.kernel.execution_evidence import execution_evidence
    evidence = execution_evidence(record, attempt)
    if evidence["complete"] and evidence["fingerprint"] != attempt.get("acceptance_input_evidence_hash"):
        return evidence
    return None


def execution_recheck_gates(record):
    """Read-only offers; a decision persists the exact same versioned gate."""
    if record.get("status") not in {"paused", "blocked", "needs_attention"} or record.get("active_node_ids"):
        return []
    value = ledger.state(record)
    working, gates = {"run_id": record["run_id"], "collaboration": copy.deepcopy(value)}, []
    for key, contract in value["contracts"].items():
        if any(gate.get("status") == "waiting" and gate.get("scope") == "contract:" + key
               and gate.get("kind") != "coordination_wait"
               for gate in value.get("recovery_gates", {}).values()):
            continue
        evidence = execution_recheck_candidate(record, key)
        if evidence:
            gates.append(open_gate(working, scope="contract:" + key, keys=[key], label=contract["label"],
                reason="已有未交给上次验收的宿主执行回执，可复用原交付重新验收；旧结论保留。",
                kind="execution_evidence_available", execution_evidence_fingerprint=evidence["fingerprint"]))
    return gates


def unstarted_retry_candidate(record, key):
    """A stopped technical failure may offer a retry, never grant execution."""
    from mewcode.kernel.calls import unstarted_retry_calls

    if (record.get('status') not in {'paused', 'blocked', 'needs_attention'}
            or record.get('active_node_ids') or record.get('pending_interaction')):
        return None
    value = ledger.state(record)
    contract = value['contracts'].get(key)
    attempt = latest_attempt(value, key)
    stage = (record.get('node_states') or {}).get(key) or {}
    delivery = (attempt or {}).get('delivery') or {}
    if (not contract or contract.get('held') or not attempt
            or attempt.get('status') not in {'dispatched', 'rejected'}
            or delivery.get('status') not in {'blocked', 'needs_attention'}
            or (delivery.get('handoff') or {}).get('blocker_kind') not in {
                'capability_missing', 'environment_unready', 'service_unavailable'}
            or stage.get('pending_permission') or stage.get('pending_interaction')
            or stage.get('input_revision') != attempt.get('dependencies')
            or unresolved_calls(record, [key])):
        return None
    try:
        ledger.check_attempt(value, attempt['id'])
    except ValueError:
        return None
    grants = record.get('kernel_call_retry_authorizations') or {}
    calls, retries = {}, {}
    for cid in unstarted_retry_calls(record, key):
        call = record['kernel_calls'][cid]
        if call.get('input_revision') != attempt['dependencies']:
            continue
        grant = grants.get(cid)
        previous = record['kernel_calls'].get(digest([cid, 'user_retry', grant['decision_id']])) if grant else None
        if grant and not previous:
            continue  # The existing retry permission has not been consumed yet.
        calls[cid], retries[cid] = call, previous
    if not calls:
        return None
    return {'fingerprint': digest({'calls': calls, 'attempt_id': attempt['id'],
        'grants': {cid: grants.get(cid) for cid in calls}, 'retries': retries})}


def unstarted_retry_gates(record):
    """Derive stable recovery offers without copying or mutating run history."""
    value = ledger.state(record)
    gates = []
    for key, contract in value['contracts'].items():
        scope = 'contract:' + key
        if any(row.get('scope') == scope and gate_current(value, row)
               for row in value.get('recovery_gates', {}).values()):
            continue
        evidence = unstarted_retry_candidate(record, key)
        if evidence:
            basis = gate_basis(value, scope, [key], execution_evidence_fingerprint=evidence['fingerprint'])
            gates.append({'id': digest([value['run_id'], scope, 'infrastructure', basis]),
                'scope': scope, 'node_ids': [key], 'label': contract['label'],
                'reason': '操作在执行前被阻止，尚未产生效果。可按原要求重试；重试仍需通过当前权限和工具检查。',
                'kind': 'infrastructure', 'basis': basis, 'spec_version': value['spec_version'],
                'status': 'waiting', 'execution_evidence_fingerprint': evidence['fingerprint']})
    return gates


def resolve_gate(record, *, gate_id, expected_version, request_id, action, reason, actor, model=None,
                 budget_amount=None):
    """Called only after the owning host UI validates its user request.

    The working copy is committed only when the whole decision succeeds, so a
    rejected request can never leave a half-written ledger behind.
    """
    working = copy.deepcopy(record)
    decision = _resolve_gate(working, gate_id=gate_id, expected_version=expected_version, request_id=request_id,
                             action=action, reason=reason, actor=actor, model=model, budget_amount=budget_amount)
    record.clear()
    record.update(working)
    return copy.deepcopy(decision)


def elastic_budget_available(value, gate):
    """A local quota may be replaced, never an exhausted task-wide allowance."""
    if lineage_budget.elastic(value):
        return False
    budget = gate.get('budget') or {}
    if gate.get('kind') == 'lineage_budget_exhausted' and budget.get('scope') == 'lineage':
        kind, requested = budget['kind'], budget['requested']
    elif gate.get('kind') == 'attempts_exhausted':
        kind, requested = 'attempts', 1
    else:
        return False
    book = lineage_budget.ensure(copy.deepcopy(value))
    total = book['limits']['root'][kind] + sum(row['amount'] for row in book['grants'].values()
        if row['kind'] == kind and row['lineage_id'] is None)
    return total - book['root']['used'][kind] - book['root']['reserved'][kind] >= requested


def _resolve_gate(record, *, gate_id, expected_version, request_id, action, reason, actor, model=None,
                  budget_amount=None):
    if actor != "user":
        raise ValueError("只有用户可以决定重试或人工验收")
    if (not isinstance(request_id, str) or not 1 <= len(request_id) <= 120
            or not isinstance(reason, str) or not reason.strip() or len(reason) > 4000):
        raise ValueError("请填写有效请求身份及不超过 4000 字的处理依据")
    if action not in {"retry", "switch_model", "switch_verifier", "recheck_execution_evidence", "revise", "accept", "add_budget", "enable_elastic_budget"} | _COORDINATOR_ACTIONS:
        raise ValueError("恢复选项无效")
    if budget_amount is not None and action != "add_budget":
        raise ValueError("仅追加预算可以携带预算数量")
    if action == "switch_coordinator":
        model = _validated_coordinator_model(model)
    elif action == "resume_coordinator" and model is not None:
        raise ValueError("恢复协调服务不能携带执行模型")
    value = ledger.state(record)
    payload = {"gate_id": gate_id, "expected_version": expected_version, "action": action,
               "reason": reason.strip(), "model": copy.deepcopy(model)}
    if action == "add_budget":
        payload["budget_amount"] = budget_amount
    decisions = value.setdefault("human_decisions", {})
    identity = digest([value["run_id"], "human-recovery", request_id])
    if identity in decisions:
        if decisions[identity]["payload"] != payload:
            raise ValueError("同一用户决定身份不能改写内容")
        return copy.deepcopy(decisions[identity])
    gate = value.get("recovery_gates", {}).get(gate_id)
    if not gate or expected_version != value["spec_version"] or not gate_current(value, gate):
        raise ValueError("恢复关口或规格已经变化，请刷新后重新决定")
    keys = gate["node_ids"]
    if gate['kind'] == 'infrastructure' and gate.get('execution_evidence_fingerprint'):
        evidence = unstarted_retry_candidate(record, keys[0]) if len(keys) == 1 else None
        if not evidence or evidence['fingerprint'] != gate['execution_evidence_fingerprint']:
            raise ValueError('未执行操作的宿主证据已经变化，请刷新后重新决定')
        if action not in {'retry', 'switch_model', 'revise'}:
            raise ValueError('此关口只能重试、换模型重试或请求修改要求')
    if gate["kind"] == "execution_evidence_available" and action not in {"recheck_execution_evidence", "revise"}:
        raise ValueError("此关口只能复用原交付重验或请求修改要求")
    if gate["kind"] == "coordinator_unavailable":
        if action not in _COORDINATOR_ACTIONS and action != "revise":
            raise ValueError("协调服务不可用的关口只能恢复或换绑协调者")
    elif action in _COORDINATOR_ACTIONS:
        raise ValueError("当前关口不支持协调者恢复")
    if action in _COORDINATOR_ACTIONS or action == 'enable_elastic_budget':
        if unresolved_calls(record, list(value["contracts"])):
            raise ValueError("仍有结果不明的业务操作，请先使用宿主回执核对入口；协调者恢复不能替代效果核对")
    elif action != "revise" and unresolved_calls(record, keys):
        raise ValueError("仍有结果不明的操作，请先使用宿主回执核对入口；人工验收不能替代效果核对")
    if action == "switch_model" and not model:
        raise ValueError("请选择要重试的实际模型")
    if action == "switch_verifier":
        if not verifier_switch_attempt(value, gate):
            raise ValueError("当前关口没有可换绑验收模型的已完成交付")
        if not isinstance(model, dict) or set(model) != _COORDINATOR_MODEL_FIELDS:
            raise ValueError("换绑验收模型需要严格的 provider_key、model_id、effort")
        from mewcode.model_registry import ModelRef
        model = ModelRef.from_value(model).as_dict()
    if model and action not in {"switch_model", "switch_verifier", "switch_coordinator"}:
        raise ValueError("仅换模型重试可以修改执行模型")
    decision = {"id": identity, "source": "user", "payload": payload, "scope": gate["scope"],
                "basis": gate["basis"], "spec_version": value["spec_version"], "node_ids": list(keys)}
    if action == "recheck_execution_evidence":
        if gate["kind"] != "execution_evidence_available" or len(keys) != 1:
            raise ValueError("当前关口不支持补齐宿主回执后重验")
        evidence = execution_recheck_candidate(record, keys[0])
        if not evidence or evidence["fingerprint"] != gate.get("execution_evidence_fingerprint"):
            raise ValueError("宿主执行回执或原交付已经变化，请重新核对")
        pending = latest_attempt(value, keys[0])
        pending.setdefault("acceptance_history", []).append(copy.deepcopy(pending["report"]))
        pending.update(status="checking", execution_evidence=evidence,
                       evidence_recheck_artifacts=copy.deepcopy(pending["report"].get("artifacts", [])))
        value.setdefault("retry_generations", {})[keys[0]] = value.get("retry_generations", {}).get(keys[0], 0) + 1
        pending["retry_generation"] = value["retry_generations"][keys[0]]
        value.setdefault("acceptance_rounds", {})[pending["id"]] = value.get("acceptance_rounds", {}).get(pending["id"], 0) + 1
        decision["execution_evidence_fingerprint"] = evidence["fingerprint"]
        ledger.event(value, "execution_evidence_recheck", gate_id=gate_id, decision_id=identity,
                     attempt_id=pending["id"], reason=reason.strip(), execution_evidence_fingerprint=evidence["fingerprint"])
    elif action == "accept":
        report = manual_candidate(value, gate)
        if not report or not report.get("artifacts"):
            raise ValueError("没有可供人工判定的固定交付，请先重试或修改要求")
        from mewcode.kernel.collaboration_faults import ACCEPTANCE_BLOCKERS, report_fault
        if report_fault(report) in ACCEPTANCE_BLOCKERS:
            raise ValueError("人工验收不能替代模型服务恢复、权限、用户输入或操作效果核对")
        # Physical bytes are checked by the host service immediately before
        # this mutation. Failed automatic checks remain unchanged as evidence.
        decision["artifacts"] = copy.deepcopy(report["artifacts"])
        if gate["scope"].startswith("contract:"):
            key = keys[0]
            attempt = latest_attempt(value, key)
            ledger.check_attempt(value, attempt["id"])
            attempt.update(status="accepted", human_decision_id=identity, report=copy.deepcopy(report))
            certificate = {"attempt_id": attempt["id"], "contract_version": value["contracts"][key]["version"],
                "dependencies": ledger.dependencies(value, key), "report_hash": digest(report),
                "artifacts": copy.deepcopy(report["artifacts"]), "checks": copy.deepcopy(report.get("checks", [])),
                "accepted_by": "user", "human_decision_id": identity}
            # The certificate carries the host-observed read set, never the
            # delivery's own claim about which files it read.
            current_read_set = attempt.get("read_set")
            if current_read_set is not None:
                certificate["read_set"] = copy.deepcopy(current_read_set)
            value["certificates"][key] = certificate
            decision.update(attempt_id=attempt["id"], dependencies=certificate["dependencies"], report_hash=certificate["report_hash"])
        else:
            integration = value["integrations"][gate["scope"]]
            integration["automatic_report"] = copy.deepcopy(integration["report"])
            integration["report"] = {**integration["report"], "passed": True, "accepted_by": "user", "human_decision_id": identity}
            integration["human_decision_id"] = identity
        ledger.event(value, "human_acceptance", gate_id=gate_id, decision_id=identity, scope=gate["scope"], reason=reason.strip())
    elif action in {"retry", "switch_model", "switch_verifier"}:
        for key in keys:
            if action in {'retry', 'switch_model'} and gate['scope'] == 'contract:' + key:
                _supersede_unstarted_assistance(record, key, identity)
                from mewcode.kernel.calls import authorize_unstarted_retries
                authorized = authorize_unstarted_retries(record, key, identity)
                if authorized:
                    decision.setdefault('unstarted_call_retries', {})[key] = authorized
            value.setdefault("retry_generations", {})[key] = value.get("retry_generations", {}).get(key, 0) + 1
            pending = latest_attempt(value, key)
            if pending and pending["status"] in {"dispatched", "checking"}:
                pending["retry_generation"] = value["retry_generations"][key]
            if pending and pending.get("acceptance_fault"):
                from mewcode.kernel.collaboration_faults import REQUIRES_CLEARANCE
                if pending["acceptance_fault"].get("execution_error") not in REQUIRES_CLEARANCE:
                    value.setdefault("acceptance_rounds", {})[pending["id"]] = value.get("acceptance_rounds", {}).get(pending["id"], 0) + 1
            if action == "switch_verifier":
                before = copy.deepcopy(value.get("acceptance_model_overrides", {}).get(key))
                value.setdefault("acceptance_model_overrides", {})[key] = copy.deepcopy(model)
                ledger.event(value, "verifier_model_changed", node_id=key, before=before,
                             after=model, decision_id=identity, reason=reason.strip())
            elif model:
                before = copy.deepcopy(record.get("locked_models", {}).get(key))
                record.setdefault("locked_models", {})[key] = copy.deepcopy(model)
                record.setdefault("collaboration_model_changes", []).append({"node_id": key, "before": before,
                    "after": copy.deepcopy(model), "source": "user", "decision_id": identity})
        if not gate["scope"].startswith("contract:"):
            scope = gate["scope"]
            integration = value.get("integrations", {}).get(scope, {})
            fault = integration.get("report", {}).get("execution_error")
            if fault and action != "switch_model":
                from mewcode.kernel.collaboration_faults import REQUIRES_CLEARANCE
                if fault not in REQUIRES_CLEARANCE:
                    attempt_id = digest([scope, integration["basis"], value.get("integration_generations", {}).get(scope, 0)])
                    value.setdefault("acceptance_rounds", {})[attempt_id] = value.get("acceptance_rounds", {}).get(attempt_id, 0) + 1
            else:
                value.setdefault("integration_generations", {})[scope] = value.get("integration_generations", {}).get(scope, 0) + 1
                value.get("integration_reworks", {}).pop(scope, None)
                ledger.invalidate(value, keys, "用户明确要求集成交付重新返工")
        elif action == "switch_model":
            ledger.invalidate(value, keys, "用户选择新执行模型并明确重试")
        ledger.event(value, "retry_authorized", gate_id=gate_id, decision_id=identity, contracts=keys, reason=reason.strip())
    elif action == 'enable_elastic_budget':
        if not elastic_budget_available(value, gate):
            raise ValueError('当前关口无法通过共享总任务额度恢复')
        before = value['policy'].get('lineage_budget_mode', 'fixed')
        value['policy']['lineage_budget_mode'] = 'elastic'
        value['required_capabilities'] = sorted(set(value.get('required_capabilities', [])) | {'elastic_budget'})
        decision['budget_policy'] = {'before': before, 'after': 'elastic', 'scope': 'run'}
        replaced = []
        for old in value.get('recovery_gates', {}).values():
            local_quota = (old.get('kind') == 'attempts_exhausted' or
                old.get('kind') == 'lineage_budget_exhausted' and (old.get('budget') or {}).get('scope') == 'lineage')
            if old['id'] != gate_id and old.get('status') == 'waiting' and local_quota:
                old.update(status='superseded', superseded_by_decision=identity)
                replaced.append(old['id'])
        decision['superseded_local_budget_gates'] = replaced
        ledger.event(value, 'budget_policy_changed', gate_id=gate_id, decision_id=identity,
                     before=before, after='elastic', scope='run', reason=reason.strip(),
                     superseded_local_budget_gates=replaced)
    elif action == "add_budget":
        budget = gate.get("budget")
        if gate["kind"] != "lineage_budget_exhausted" or not budget:
            raise ValueError("当前关口不支持追加预算")
        budget = _validated_budget(budget)
        if not _is_int(budget_amount) or budget_amount <= 0:
            raise ValueError("追加预算必须是正整数")
        grant = lineage_budget.grant(value, identity, kind=budget["kind"], amount=budget_amount,
            lineage_id=None if budget["scope"] == "root" else budget["lineage_id"])
        decision["budget_grant"] = copy.deepcopy(grant)
        for key in keys:
            value.setdefault("retry_generations", {})[key] = value.get("retry_generations", {}).get(key, 0) + 1
            pending = latest_attempt(value, key)
            if pending and pending["status"] in {"dispatched", "checking"}:
                pending["retry_generation"] = value["retry_generations"][key]
        ledger.event(value, "budget_granted", gate_id=gate_id, decision_id=identity, grant=copy.deepcopy(grant))
        ledger.event(value, "retry_authorized", gate_id=gate_id, decision_id=identity, contracts=keys, reason=reason.strip())
    elif action in _COORDINATOR_ACTIONS:
        selected = model if action == "switch_coordinator" else value.get("coordinator")
        if not selected:
            raise ValueError("当前没有绑定可恢复的协调服务")
        ledger.takeover(value, selected, source_ref=identity)
        decisions = value.setdefault("human_decisions", {})
        gate = value["recovery_gates"][gate_id]
        decision["coordinator_model"] = copy.deepcopy(selected)
        ledger.event(value, "coordinator_recovery_authorized", action=action, decision_id=identity,
                     gate_id=gate_id, reason=reason.strip())
    else:
        ledger.event(value, "requirement_revision_requested", gate_id=gate_id, decision_id=identity, reason=reason.strip())
    decisions[identity] = decision
    gate.update(status="resolved", decision_id=identity, action=action)
    record.update(status="paused", status_reason="用户处理决定已保存，可继续任务。" if action != "revise" else "请提交具体要求修订后继续任务。")
    return copy.deepcopy(decision)
