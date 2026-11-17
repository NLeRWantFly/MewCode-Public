"""Host-owned collaboration facts, persisted by ExpertRunStore/EventJournal.

There is deliberately no file store here. DraftStore owns the design; after
binding, this state in the run journal owns revisions, dispatch and acceptance.
Model output is always a proposal, never authority or a completed delivery.
"""
from __future__ import annotations

import copy

from mewcode.kernel import lineage_budget
from mewcode.kernel.events import digest

FORMAT = "mewcode.collaboration.v1"
RUNTIME_FORMAT = "mewcode.collaboration.v2"
CAPABILITIES = frozenset({"lineage_budget", "elastic_budget", "context_projection", "rolling_plan", "runtime_graph", "scoped_contracts", "iterative_delivery", "isolated_workspaces"})
MODES = frozenset({"classic", "collaborative"})


class AttemptBudgetExhausted(ValueError):
    """A human decision or adopted specification revision can reopen work."""


def failed_attempts(value, contract_id):
    from mewcode.kernel.work_continuation import checkpoint_matches
    generation = value.get("retry_generations", {}).get(contract_id, 0)
    return [row for row in value["attempts"].values()
            if row["contract_id"] == contract_id and row["status"] == "rejected"
            and row.get("spec_version") == value["spec_version"]
            and row.get("retry_generation", 0) == generation and not checkpoint_matches(row)]


def retry_exhausted(value, contract_id):
    # In elastic mode the coordinator assesses progress; a local count does
    # not stop useful retries while the shared root allowance remains.
    return (not lineage_budget.elastic(value)
            and len(failed_attempts(value, contract_id)) >= value["policy"]["max_attempts"])


def mode(value=None):
    value = "classic" if value is None else value
    if not isinstance(value, str) or value not in MODES:
        raise ValueError("编排模式无效，请选择经典编排或协作编排。")
    return value


def flow_mode(flow):
    meta = flow.get("meta") or {}
    selected = mode(meta.get("orchestration_mode", (meta.get("task_design") or {}).get("orchestration_mode")))
    design_mode = (meta.get("task_design") or {}).get("orchestration_mode")
    if design_mode is not None and selected != mode(design_mode):
        raise ValueError("任务与编排图的模式不一致")
    if (meta.get("collaboration") or meta.get("collaboration_spec")) and selected != "collaborative":
        raise ValueError("协作规格不能通过经典编排执行。")
    return selected


def required_capabilities(value):
    declared = value.get("required_capabilities")
    if "required_capabilities" not in value:
        return []
    if (not isinstance(declared, list) or any(not isinstance(item, str) for item in declared)
            or len(set(declared)) != len(declared) or set(declared) - CAPABILITIES):
        raise ValueError("协作运行能力声明无效，未回退到旧协议。")
    return list(declared)


def _runtime_capabilities(plan):
    declared = set(required_capabilities(plan)) | {"lineage_budget", "context_projection", "iterative_delivery"}
    if plan.get("planning_mode") == "rolling":
        declared.add("rolling_plan")
    if any(row.get('workspace_mode') for row in plan.get('contracts', {}).values()):
        declared.add('isolated_workspaces')
    return sorted(declared)


def state(record):
    value = record.get("collaboration")
    if not isinstance(value, dict) or value.get("format") not in {FORMAT, RUNTIME_FORMAT}:
        raise ValueError("缺少有效协作账本，未回退到经典执行。")
    required_capabilities(value)
    return value


def event(value, kind, **data):
    events = value.setdefault("timeline", [])
    events.append({"sequence": len(events) + 1, "kind": kind, **copy.deepcopy(data)})


def _upgrade(value, capabilities):
    from mewcode.kernel import lineage_budget

    upgraded = value.get("format") != RUNTIME_FORMAT
    value["format"] = RUNTIME_FORMAT
    value["required_capabilities"] = sorted(set(required_capabilities(value)) | set(capabilities))
    lineage_budget.ensure(value)
    if upgraded:
        event(value, "protocol_upgraded", capabilities=list(capabilities), format=RUNTIME_FORMAT)


def initialize(record, plan):
    if plan.get("format") not in {FORMAT, RUNTIME_FORMAT}:
        raise ValueError("不支持的协作规格版本")
    capabilities = _runtime_capabilities(plan)
    if record.get("collaboration"):
        current = state(record)
        plan_hash = digest(plan)
        legacy_order = False
        from mewcode.orchestration.parallel_revision import adopted_plan_hash
        if (plan_hash not in {current["initial_plan_hash"], current.get("canonical_initial_plan_hash")}
                and not adopted_plan_hash(record, plan_hash)):
            # Older compilers derived this set-like list from dict insertion
            # order, which canonical journal serialization does not preserve.
            # Recover only that ordering and require the ENTIRE original hash.
            # Never compare against mutable runtime contracts or relax sources.
            legacy = copy.deepcopy(plan)
            for key, entry in legacy.get("entries", {}).items():
                prior = current.get("entries", {}).get(key, {}).get("adopted_fields")
                fields = entry.get("adopted_fields")
                if (isinstance(prior, list) and isinstance(fields, list)
                        and all(isinstance(field, str) for field in prior + fields)
                        and len(set(prior)) == len(prior) and sorted(prior) == fields):
                    entry["adopted_fields"] = copy.deepcopy(prior)
            if digest(legacy) != current["initial_plan_hash"]:
                raise ValueError("此运行已绑定另一版协作规格")
            legacy_order = True
        upgraded = copy.deepcopy(current)
        if legacy_order:
            # Keep the historical hash and live entries unchanged. The proven
            # canonical identity also survives later adopted runtime revisions.
            upgraded["canonical_initial_plan_hash"] = plan_hash
            event(upgraded, "initial_plan_order_normalized",
                  initial_plan_hash=current["initial_plan_hash"], canonical_plan_hash=plan_hash)
        _upgrade(upgraded, capabilities)
        if upgraded != current:
            record["collaboration"] = upgraded
            current = upgraded
        return current
    value = copy.deepcopy(plan)
    value.update(initial_plan_hash=digest(plan), run_id=record["run_id"], spec_version=1, plan_version=1,
                 coordinator_epoch=1, proposals={}, attempts={}, certificates={}, questions={},
                 timeline=[], calls={}, optional_questions={}, blocked=None)
    _upgrade(value, capabilities)
    event(value, "initialized", task_id=value["task_id"], source_hash=value["source_hash"])
    record["orchestration_mode"] = "collaborative"
    record["collaboration"] = value
    return value


def dependencies(value, contract_id):
    contract = value["contracts"][contract_id]
    keys = contract.get("requirement_ids", []) + contract.get("constraint_ids", [])
    basis = {"contract": contract,
                   "entries": {key: value["entries"].get(key) for key in sorted(set(keys))},
                   "inputs": {key: value["certificates"].get(key) for key in contract.get("bound_dependencies", contract.get("depends_on", []))}}
    if contract.get('parallel_group'):
        # Failure/absence is also an input to the fusion decision. A later
        # repaired branch must invalidate the earlier reduced-input fusion.
        basis['parallel_outcomes'] = {key: {
            'attempt_id': latest.get('id'), 'status': latest.get('status'),
            'delivery': latest.get('delivery'), 'report': latest.get('report'),
        } for key in contract['parallel_group']['node_ids']
            for latest in [(attempts_for(value, key) or [{}])[-1]]}
    return digest(basis)


def semantic_basis(value, contract_id):
    contract = value["contracts"][contract_id]
    return digest({"contract": {key: item for key, item in contract.items()
                               if key not in {"iteration", "bound_dependencies", "held"}},
                   "entries": {key: value["entries"][key] for key in
                               contract["requirement_ids"] + contract["constraint_ids"]}})


def valid_certificate(value, contract_id):
    certificate = value["certificates"].get(contract_id)
    if not certificate or certificate.get("invalidated"):
        return False
    contract = value["contracts"].get(contract_id)
    if not contract or certificate["dependencies"] != dependencies(value, contract_id):
        return False
    if certificate.get("accepted_by") == "user":
        decision = value.get("human_decisions", {}).get(certificate.get("human_decision_id"), {})
        if (decision.get("source") != "user" or decision.get("attempt_id") != certificate["attempt_id"]
                or decision.get("dependencies") != certificate["dependencies"]
                or decision.get("report_hash") != certificate["report_hash"]):
            return False
    return all(valid_certificate(value, key) for key in contract.get("bound_dependencies", contract.get("depends_on", [])))


def affected_contracts(value, entry_ids):
    changed = set(entry_ids)
    direct = {key for key, contract in value["contracts"].items()
              if changed & set(contract.get("requirement_ids", []) + contract.get("constraint_ids", []) + [key])}
    return dependent_contracts(value, direct)


def dependent_contracts(value, contract_ids):
    affected = set(contract_ids)
    while True:
        more = {key for key, contract in value["contracts"].items()
                if set(contract.get("depends_on", [])) & affected}
        if more <= affected:
            break
        affected |= more
    return affected


def revision_pending(value, contract_id):
    return any(item["status"] in {"proposed", "needs_attention"}
               and contract_id in affected_contracts(value, item["payload"]["changes"])
               for item in value["proposals"].values())


def invalidate(value, contract_ids, reason):
    affected = dependent_contracts(value, contract_ids)
    for key in sorted(affected):
        cert = value["certificates"].get(key)
        if cert:
            cert["invalidated"] = reason
        for attempt in value["attempts"].values():
            if attempt["contract_id"] == key and attempt["status"] in {"dispatched", "checking"}:
                attempt["superseded"] = True
    event(value, "acceptance_invalidated", contracts=sorted(affected), reason=reason,
          dependency_policy="declared_dependencies",
          dependency_reason="按相关语义要求及声明依赖保守失效；尚无足够证据排除后继影响")
    return affected


def attempts_for(value, contract_id):
    """Attempt numbers, not JSON object order, define a contract's chronology."""
    return sorted((row for row in value["attempts"].values() if row["contract_id"] == contract_id),
                  key=lambda row: row["number"])


def dispatch(value, contract_id, *, run_attempt=1):
    contract = value["contracts"][contract_id]
    if (value.get("blocked") or contract.get("held") or value.get("plan_holds", {}).get(contract_id)
            or revision_pending(value, contract_id)):
        raise ValueError("协作规格有待处理修订，不能派发")
    if valid_certificate(value, contract_id):
        return None
    if any(not valid_certificate(value, key) for key in contract.get("bound_dependencies", contract.get("depends_on", []))):
        raise ValueError("上游尚无有效验收交接")
    previous = attempts_for(value, contract_id)
    if any(a["status"] in {"dispatched", "checking", "unknown"} for a in previous):
        raise ValueError("前次派发结果尚未核对，不能重复执行业务操作")
    if retry_exhausted(value, contract_id):
        raise AttemptBudgetExhausted("该合同已达到当前规格的无进展尝试上限，已保留现有产物和未满足的必需条件。")
    number = len(previous) + 1
    identity = digest([value["run_id"], contract_id, number])
    lineage_id = contract.get("lineage_id") or contract_id
    from mewcode.kernel.collaboration_work_budget import dispatch_reservation
    reservation_id = dispatch_reservation(value, contract_id) or "attempt:" + identity
    attempt = {"id": identity, "contract_id": contract_id, "number": number, "status": "dispatched",
               "lineage_id": lineage_id, "budget_reservation_id": reservation_id,
               "run_attempt": run_attempt, "iteration": copy.deepcopy(contract.get("iteration")),
               "retry_generation": value.get("retry_generations", {}).get(contract_id, 0),
               "semantic_basis": semantic_basis(value, contract_id),
               "spec_version": value["spec_version"], "plan_version": value["plan_version"],
               "coordinator_epoch": value["coordinator_epoch"], "dependencies": dependencies(value, contract_id)}
    lineage_budget.reserve(value, reservation_id, lineage_id=lineage_id, kind="attempts")
    value["attempts"][identity] = attempt
    if previous:
        from mewcode.kernel.work_continuation import checkpoint_matches
        if checkpoint_matches(previous[-1]):
            attempt["continued_from"] = previous[-1]["id"]
    event(value, "dispatched", contract_id=contract_id, attempt_id=identity)
    return copy.deepcopy(attempt)


def resume_dispatch(record, contract_id, *, resume_segment=False):
    """An explicit host resume may continue known effects, never replay an unknown call."""
    value = state(record)
    pending = [row for row in value["attempts"].values()
               if row["contract_id"] == contract_id and row["status"] in {"dispatched", "checking"}]
    if len(pending) != 1:
        return None
    attempt = pending[0]
    if int(record.get("attempt", 1)) <= int(attempt.get("run_attempt", 1)):
        if not resume_segment:
            return None
        from mewcode.kernel.work_continuation import segment_checkpoint
        saved = attempt.get('segment_checkpoint')
        if not saved or saved != segment_checkpoint(record, attempt):
            return None
        attempt['segment_progress_cursor'] = copy.deepcopy(saved['progress_cursor'])
        attempt.pop('segment_checkpoint')
        event(value, 'execution_segment_resumed', attempt_id=attempt['id'],
              run_attempt=record.get('attempt', 1))
        return copy.deepcopy(attempt)
    unknown = [call for call in record.get("kernel_calls", {}).values()
               if (call.get("node_id") == contract_id or str(call.get("node_id", "")).startswith(contract_id + "__")
                   or str(call.get("node_id", "")).startswith(contract_id + "::"))
               and call.get("state") in {"dispatched", "unknown"}]
    if unknown:
        raise ValueError("此尝试仍有结果不明的操作，请先使用宿主回执核对入口处理")
    try:
        check_attempt(value, attempt["id"])
    except ValueError:
        # A user-approved revision may replace an interrupted contract only
        # after an explicit resume and after all effects have known receipts.
        attempt["status"] = "superseded_closed"
        reservation = attempt.get('budget_reservation_id') or 'attempt:' + attempt['id']
        permit = (value.get('lineage_budget') or {}).get('reservations', {}).get(reservation, {})
        if not attempt.get('budget_started') and permit.get('status') == 'reserved':
            lineage_budget.release(value, reservation)
        restarted = dispatch(value, contract_id, run_attempt=record["attempt"])
        if restarted:
            value["attempts"][restarted["id"]]["resumed_from"] = attempt["id"]
            event(value, "dispatch_revised", previous_attempt=attempt["id"], attempt_id=restarted["id"])
            return copy.deepcopy(value["attempts"][restarted["id"]])
        return None
    attempt["run_attempt"] = record["attempt"]
    attempt["retry_generation"] = value.get("retry_generations", {}).get(contract_id, 0)
    event(value, "dispatch_resumed", attempt_id=attempt["id"], run_attempt=record["attempt"])
    return copy.deepcopy(attempt)


def check_attempt(value, attempt_id):
    attempt = value["attempts"][attempt_id]
    if (attempt.get("superseded") or attempt["coordinator_epoch"] != value["coordinator_epoch"]
            or attempt["dependencies"] != dependencies(value, attempt["contract_id"])):
        raise ValueError("合同、依赖或协调者代际已变化；旧回执只能作为历史证据")
    return attempt


def mark_started(value, attempt_id):
    """The real start boundary: settle this attempt's reservation exactly once."""
    attempt = check_attempt(value, attempt_id)
    if attempt["status"] not in {"dispatched", "checking", "accepted"}:
        raise ValueError("当前尝试不接受开始标记")
    # settle 自身会迁移旧账本；预留未知或预算不足时账本保持原样。
    lineage_budget.settle(value, attempt.get("budget_reservation_id") or "attempt:" + attempt_id)
    if attempt.get("patch"):
        lineage_budget.settle(value, attempt['patch']['reservation_id'])
    attempt["budget_started"] = True
    return copy.deepcopy(attempt)


def complete(value, attempt_id, report):
    from mewcode.kernel.acceptance import report_ready
    from mewcode.kernel.contracts import blocking_check

    attempt = check_attempt(value, attempt_id)
    if attempt["status"] == "accepted":
        cert = value["certificates"][attempt["contract_id"]]
        if cert["report_hash"] != digest(report):
            raise ValueError("同一尝试不能提交不同验收结果")
        return cert
    if attempt["status"] not in {"dispatched", "checking"}:
        raise ValueError("当前尝试不接受验收结果")
    contract = value["contracts"][attempt["contract_id"]]
    ready = report_ready(contract, report)
    if report.get("ready") is True and not ready:
        raise ValueError("交接报告未满足当前必需检查或固定产物条件")
    if ready:
        required = contract["checks"]
        actual = report.get("checks", [])
        mandatory = {row["id"] for row in required if blocking_check(row) or report.get("passed") is True}
        if (len(required) != len(actual)
                or {(c["id"], c["version"]) for c in required} != {(c.get("id"), c.get("version")) for c in actual}
                or any(not c.get("evidence") or (c["id"] in mandatory and c.get("passed") is not True) for c in actual)):
            raise ValueError("验收报告没有逐项覆盖当前合同检查及版本")
    mark_started(value, attempt_id)
    attempt["report"] = copy.deepcopy(report)
    attempt["status"] = "accepted" if ready else "rejected"
    event(value, ("accepted" if report.get("passed") is True else "delivery_ready") if ready else "rejected",
          contract_id=attempt["contract_id"], attempt_id=attempt_id, report_hash=digest(report))
    if not ready:
        return None
    certificate = {"attempt_id": attempt_id, "contract_version": value["contracts"][attempt["contract_id"]]["version"],
                   "dependencies": attempt["dependencies"], "report_hash": digest(report),
                   "artifacts": copy.deepcopy(report.get("artifacts", [])),
                   "quality_passed": report.get("passed") is True,
                   "checks": [{key: copy.deepcopy(row[key]) for key in ("id", "version", "passed", "deferred") if key in row}
                              for row in report["checks"]]}
    if "read_set" in attempt:
        certificate["read_set"] = copy.deepcopy(attempt["read_set"])
    value["certificates"][attempt["contract_id"]] = certificate
    return certificate


def final_ready(value):
    """A current reviewed draft can be complete while retaining quality findings."""
    from mewcode.kernel.acceptance import report_ready
    final = value.get("integrations", {}).get("final") or {}
    return bool(not value.get("blocked") and final.get("contract")
        and report_ready(final["contract"], final.get("report") or {})
        and final.get("spec_version") == value["spec_version"]
        and final.get("coordinator_epoch") == value["coordinator_epoch"]
        and final.get("certificates")
        and value["final_contract_id"] in final["certificates"]
        and not any(contract.get("held") for contract in value["contracts"].values())
        and all(valid_certificate(value, key) and digest(value["certificates"][key]) == fingerprint
                for key, fingerprint in final["certificates"].items()))


def proposal(value, *, proposal_id, changes, evidence, actor, base_version, epoch):
    if actor not in {"user", "coordinator"}:
        raise ValueError("工作单元只能提交候选，不能修改有效规格")
    payload = {"changes": copy.deepcopy(changes), "evidence": copy.deepcopy(evidence), "actor": actor,
               "base_version": base_version, "epoch": epoch}
    previous = value["proposals"].get(proposal_id)
    if previous:
        if previous["payload"] != payload:
            raise ValueError("提议身份已被另一内容使用")
        return previous
    if base_version != value["spec_version"] or epoch != value["coordinator_epoch"]:
        raise ValueError("提议基准已过期")
    if not changes or set(changes) - set(value["entries"]):
        raise ValueError("修订必须引用现有规格条目")
    for key, replacement in changes.items():
        old = value["entries"][key]
        if not isinstance(replacement, dict) or replacement.get("id") != key or replacement.get("kind") != old["kind"]:
            raise ValueError("修订不能改变条目身份和类型")
        # Provenance and authority are host facts, not editable model fields.
        for field in ("origin", "adopted_fields", "field_sources", "strength", "scope", "sources"):
            if replacement.get(field) != old.get(field):
                raise ValueError("修订不能改写来源、强度或原作用范围")
        from mewcode.kernel.contracts import changed_fields
        if actor != "user" and (old.get("strength") == "hard" or old.get("origin") in {"user", "legacy_unknown", "host"}
                                or set(old.get("adopted_fields", [])) & changed_fields(old, replacement)):
            raise ValueError("此要求的修改需要用户明确决定")
        if old.get("origin") == "host":
            raise ValueError("宿主策略必须通过原权限入口修改")
    result = {"id": proposal_id, "payload": payload, "status": "proposed",
              "basis": {key: digest(value["entries"][key]) for key in changes}}
    if actor == "user":
        for old in value["proposals"].values():
            if old["status"] in {"proposed", "needs_attention"} and set(changes) & set(old["payload"]["changes"]):
                old.update(status="superseded", superseded_by=proposal_id)
    value["proposals"][proposal_id] = result
    if actor == "user":
        affected = {key for key, contract in value["contracts"].items()
                    if set(changes) & set(contract.get("requirement_ids", []) + contract.get("constraint_ids", []) + [key])}
        invalidate(value, affected, "用户修订已记录，等待安全切换")
        for key in affected:
            value["contracts"][key]["held"] = True
    event(value, "proposal_recorded", proposal_id=proposal_id, actor=actor)
    return result


def adopt(value, proposal_id, *, review, proposer_vendor, reviewer_vendor):
    """Adopt a revision transactionally: the ledger commits only if every step succeeds."""
    candidate = copy.deepcopy(value)
    result = _adopt(candidate, proposal_id, review=review,
                    proposer_vendor=proposer_vendor, reviewer_vendor=reviewer_vendor)
    if result:
        value.clear()
        value.update(candidate)
    return result


def _adopt(value, proposal_id, *, review, proposer_vendor, reviewer_vendor):
    item = value["proposals"][proposal_id]
    if item["status"] == "adopted":
        return False
    if item["status"] not in {"proposed", "needs_attention"}:
        raise ValueError("提议已被新的用户决定取代，旧结果不能生效")
    if (item["payload"]["epoch"] != value["coordinator_epoch"]
            or any(digest(value["entries"][key]) != fingerprint for key, fingerprint in item["basis"].items())):
        raise ValueError("相关规格或协调者已变化，需要重新复核")
    if (not proposer_vendor or not reviewer_vendor or proposer_vendor.casefold() == reviewer_vendor.casefold()
            or review.get("passed") is not True or not review.get("evidence")):
        raise ValueError("语义修订尚未获得有证据的独立复核")
    changed = set(item["payload"]["changes"])
    affected = {key for key, contract in value["contracts"].items()
                if changed & set(contract.get("requirement_ids", []) + contract.get("constraint_ids", []) + [key])}
    affected_lineages = sorted({value["contracts"][key].get("lineage_id") or key for key in affected})
    for lineage_id in affected_lineages:
        reservation = "revision:" + proposal_id + ":" + lineage_id
        lineage_budget.reserve(value, reservation, lineage_id=lineage_id, kind="revisions")
        lineage_budget.settle(value, reservation)
    invalidate(value, affected, "相关规格已修订")
    for key, replacement in item["payload"]["changes"].items():
        original = value["entries"][key]
        updated = {**copy.deepcopy(replacement), "version": original.get("version", 1) + 1}
        if item["payload"]["actor"] == "user":
            from mewcode.kernel.contracts import changed_fields
            fields = changed_fields(original, replacement)
            updated["adopted_fields"] = sorted(set(original.get("adopted_fields", [])) | fields)
            references = [{"source_id": identity, "quote": value["user_sources"][identity]["text"]}
                          for identity in item["payload"]["evidence"] if identity in value.get("user_sources", {})]
            if references:
                for field in fields:
                    updated.setdefault("field_sources", {})[field] = copy.deepcopy(references)
        value["entries"][key] = updated
    for key in affected:
        references = set(value["contracts"][key]["requirement_ids"] + value["contracts"][key]["constraint_ids"] + [key])
        waiting = any(other["id"] != proposal_id and other["status"] in {"proposed", "needs_attention"}
                      and other["payload"]["actor"] == "user" and references & set(other["payload"]["changes"])
                      for other in value["proposals"].values())
        if waiting:
            value["contracts"][key]["held"] = True
        else:
            value["contracts"][key].pop("held", None)
        value["contracts"][key]["version"] += 1
        if key in changed and value["entries"][key]["kind"] == "node":
            from mewcode.kernel.contracts import design_contract
            updates = design_contract(value["entries"][key].get("data", {}).get("contract", {}))
            original = value["contracts"][key]
            if any(updates[field] != original.get(field) for field in set(updates) - {"method", "forbidden_changes", "checks"}):
                raise ValueError("运行中合同修订仅支持方法、禁止变更和验收；拓扑变更需重新设计")
            if 'checks' in updates and updates['checks'] != original['checks']:
                from mewcode.kernel.contracts import revised_acceptance_goal
                original['goal'] = revised_acceptance_goal(original, updates['checks'])
            if "method" in updates and updates['method'] != original.get('method'):
                value["contracts"][key]["method_decision"] = proposal_id
            value["contracts"][key].update(copy.deepcopy(updates))
    value["spec_version"] += 1
    value["plan_version"] += 1
    from mewcode.kernel.collaboration_work_budget import release_obsolete
    release_obsolete(value)
    value["goal"] = "\n".join(row["body"] for row in value["entries"].values() if row["kind"] == "goal")
    item.update(status="adopted", review=copy.deepcopy(review), version=value["spec_version"],
                affected_lineages=affected_lineages)
    event(value, "proposal_adopted", proposal_id=proposal_id, version=value["spec_version"], plan_version=value["plan_version"])
    return True


def takeover(value, model, *, source_ref):
    candidate = copy.deepcopy(value)
    _takeover(candidate, model, source_ref=source_ref)
    value.clear()
    value.update(candidate)


def _takeover(value, model, *, source_ref):
    if not source_ref:
        raise ValueError("协调者换绑需要用户入口记录")
    previous = next((e for e in value["timeline"] if e["kind"] == "coordinator_changed" and e["source_ref"] == source_ref), None)
    if previous:
        if previous["model"] != model:
            raise ValueError("同一用户决定不能改写换绑模型")
        return
    value["coordinator"] = copy.deepcopy(model)
    value["coordinator_epoch"] += 1
    if value.get("coordinator_lease"):
        value["coordinator_lease"]["status"] = "superseded"
    from mewcode.kernel import lineage_budget
    for publication in value.get("graph_publications", {}).values():
        if publication["status"] == "prepared":
            from mewcode.kernel.collaboration_work_budget import release_unclaimed
            release_unclaimed(value, publication)
            lineage_budget.release(value, publication["reservation_id"])
            publication.update(status="rejected", reason="用户恢复协调服务后，旧代际候选需要重新核对")
            for key in publication["affected"]:
                if value.get("plan_holds", {}).get(key) == publication["id"]:
                    del value["plan_holds"][key]
    for plan in value.get("milestone_plans", {}).values():
        if plan["status"] == "planning":
            lineage_budget.release(value, plan["reservation_id"])
            plan.update(status="rejected", reason="用户恢复协调服务后，旧代际规划需要重新核对")
    event(value, "coordinator_changed", model=model, source_ref=source_ref, epoch=value["coordinator_epoch"])
