"""Collaboration contracts around the existing authorized stage executor.

The graph scheduler remains a host primitive. Its completed text alone cannot
make a collaborative node ready: every handoff requires a current certificate.
"""
from __future__ import annotations

import copy
import asyncio
import json
import time
from dataclasses import asdict, replace

from mewcode.kernel import collaboration as ledger
from mewcode.kernel import lineage_budget
from mewcode.kernel.async_commit import durable_call
from mewcode.runtime_workers import io_call
from mewcode.kernel.acceptance import Acceptance, paused_review_binding, review_binding
from mewcode.kernel.events import digest
from mewcode.kernel.models import RoleModels
from mewcode.kernel.collaboration_faults import TRANSIENT, execution_fault, host_blocker
from mewcode.kernel.collaboration_recovery import open_gate, unresolved_calls
from mewcode.model_registry import ModelRef
from mewcode.orchestration.task_context import build_task_context, ContextBudgetExceeded
from mewcode.orchestration.forward_planning import FORWARD_PLANNING_RULES
from mewcode.orchestration.context_budget import model_context_budget


class CollaborationRuntime:
    def __init__(self, *, store, run_id, flow, catalog, work_dir, review=None, command=None, coordinate=None, publish=None, decide=None, context=None):
        self.store, self.run_id, self.flow = store, run_id, flow
        self.pool = RoleModels(catalog)
        self.rework_requested = set()
        self.review_callback = review
        self.publish_callback = publish
        self.decide_callback = decide
        self.context_callback = context
        from mewcode.orchestration.collab_coordinator import CollaborationCoordinator
        self.coordinator = CollaborationCoordinator(store, run_id, catalog, ask=coordinate)
        self.acceptance = Acceptance(work_dir, review=self.review, command=command)
        from mewcode.orchestration.collab_workspaces import CollaborationWorkspaces
        self.workspaces = CollaborationWorkspaces(self)
        if store is None:
            raise ValueError("协作编排必须使用持久化运行存储")
        from mewcode.orchestration.collab_compile import compile_plan
        from mewcode.experts.flow import ExpertFlowSpec
        from mewcode.kernel.collaboration_graph import effective_flow
        original = self.store.read_execution_fields(run_id, "locked_flow").get("locked_flow") or flow.as_dict()
        plan = compile_plan(ExpertFlowSpec.from_value(original))
        store.mutate(run_id, lambda record: ledger.initialize(record, plan), durable=True, return_snapshot=False)
        self.flow = self.store.read_execution_flow(run_id)

    def read(self, *fields):
        if fields:
            return self.store.read_collaboration_fields(self.run_id, *fields)
        return self.store.read_collaboration(self.run_id)

    def mutate(self, callback):
        return self.store.mutate_collaboration(self.run_id, callback)

    def accepted(self, node_id):
        current = self.store.read_current_certificate(self.run_id, node_id)
        if current is None:
            return False
        cert, workspace = current['certificate'], current['workspace']
        if workspace and not self.workspaces.current_commit(workspace):
            self.mutate(lambda current: ledger.invalidate(current, {node_id}, "隔离交付的固定提交或文件已变化"))
            return False
        if not self.acceptance.unchanged(cert["artifacts"]):
            self.mutate(lambda current: ledger.invalidate(current, {node_id}, "已验收产物内容发生变化"))
            return False
        return True

    def request_recovery(self, *, scope, keys, reason, kind, budget=None):
        # Missing decisions, unavailable models and host clearances belong in
        # the recovery inbox. Internal schemas and local retry counters
        # remain execution diagnostics with the existing resume control.
        if kind not in {'user_decision', 'infrastructure', 'lineage_budget_exhausted', 'execution_evidence_available'}:
            self.mutate(lambda value: ledger.event(value, 'execution_deferred',
                scope=scope, contracts=list(keys), reason=reason, error_kind=kind))
            return
        labels = [self.read('contracts')["contracts"][key]["label"] for key in keys]
        label = "最终整体验收" if scope == "final" else (
            "里程碑验收：" + self.read('milestones')["milestones"][scope.split(":", 1)[1]]["goal"]
            if scope.startswith("milestone:") else "、".join(labels))
        self.store.mutate(self.run_id, lambda record: open_gate(record, scope=scope, keys=keys,
            label=label, reason=reason, kind=kind, **({"budget": budget} if budget is not None else {})), durable=True, return_snapshot=False)

    def request_budget_recovery(self, node_id, exc):
        self.request_recovery(scope="contract:" + node_id, keys=[node_id],
            reason=str(exc), kind="lineage_budget_exhausted",
            budget={"kind": exc.kind, "scope": exc.scope, "lineage_id": exc.lineage_id,
                    "requested": exc.requested, "available": exc.available})

    def expansion_policy(self, node, policy, attempt):
        from mewcode.experts.node_expansion import NodeExpansionContext, NodeExpansionHolder
        from mewcode.permissions.sandbox import PathSandbox
        value = self.read()
        contract = value["contracts"][node.node_id]
        tools = contract.get("tools")
        allowed = contract.get("allow_expansion", tools is None or bool(set(tools) & {"*", "ProposeNodeExpansion"}))
        eligible = (allowed and node.role == "executor" and node.kind == "role" and not node.members
                    and not policy.loop_context and not policy.parallel_readonly
                    and not policy.parallel_isolated and not contract.get('workspace_mode')
                    and contract.get("execution_kind") != "join"
                    and not contract.get("held") and isinstance(contract.get("capabilities"), dict)
                    and contract.get("expansion_depth", 0) < value["policy"].get("max_expansion_depth", 2)
                    and not set(node.deny_tools) & {"*", "ProposeNodeExpansion"}
                    and (self.flow.meta.get("node_kinds") or {}).get(node.node_id) != "gate")
        holder = None
        if eligible:
            from mewcode.kernel.collaboration_work_budget import expansion_capacity
            holder = NodeExpansionHolder(NodeExpansionContext(
                parent_node_id=node.node_id, flow_version=self.flow.version,
                input_revision=ledger.dependencies(value, node.node_id), node_attempt=attempt["number"],
                work_dir=self.acceptance.work_dir, allowed_tools=frozenset(),
                available_input_refs=frozenset(contract.get("bound_dependencies", contract.get("depends_on", []))),
                sandbox=PathSandbox(str(self.acceptance.work_dir))),
                budget_capacity=lambda: expansion_capacity(self.read(), node.node_id))
        return replace(policy, expansion_holder=holder, collaborative_expansion=eligible,
            authorization_node_id=contract.get("authorization_node_id") or node.node_id,
            expansion_write_paths=tuple(contract["write_paths"]) if "write_paths" in contract else policy.expansion_write_paths,
            expansion_parent_contract=json.dumps(contract, ensure_ascii=False) if contract.get("expanded_from") else policy.expansion_parent_contract)

    def join_delivery(self, node_id):
        from mewcode.expert_group import ExpertRoleResult
        value = self.read()
        contract = value["contracts"][node_id]
        artifacts, inline, seen = [], [], set()
        for key in contract["join_children"]:
            if not self.accepted(key):
                raise ValueError("子合同交付尚未全部通过验收")
            for item in value["certificates"][key]["artifacts"]:
                identity = digest(item)
                if identity in seen:
                    continue
                seen.add(identity)
                if item["kind"] == "file":
                    artifacts.append({"path": item["path"]})
                else:
                    inline.append({"name": key + ": " + item["name"], "content": item["content"]})
        handoff = {"status": "completed", "artifacts": artifacts, "inline_outputs": inline,
                   "child_certificates": {key: value["certificates"][key]["report_hash"] for key in contract["join_children"]}}
        if contract.get("method"):
            handoff.update(method_id=contract["method"]["id"], method_decision=copy.deepcopy(contract.get("method_decision")))
        return ExpertRoleResult(text="宿主汇集了全部子合同的固定交付，等待父合同完整验收。", handoff=handoff)

    async def publish_expansion(self, node, model, policy, attempt, result):
        from mewcode.kernel import collaboration_graph as graph
        from mewcode.orchestration.collab_fragment import build_expansion_fragment
        from mewcode.orchestration.collab_coordinator import REVIEW_SYSTEM
        identity = digest(["expansion", attempt["id"], result.node_expansion])
        item = self.read('graph_publications').get("graph_publications", {}).get(identity)
        if not item:
            holder = policy.expansion_holder
            if holder is None:
                raise ValueError("此合同不允许运行时展开")
            fragment = build_expansion_fragment(self.read(), self.flow, node.node_id,
                result.node_expansion.get("proposal"), work_dir=self.acceptance.work_dir,
                sandbox=holder.context.sandbox, allowed_tools=holder.context.allowed_tools, node_attempt=attempt["number"])
            items = []
            (await durable_call(self.store.mutate, self.run_id, lambda record: items.append(graph.prepare(
                record, fragment, publication_id=identity, attempt_id=attempt["id"])), durable=True, return_snapshot=False))
            item = items[0]
        if item["status"] == "published":
            self.flow = (await io_call(self.store.read_execution_flow, self.run_id))
            return replace(result, status="expanded", error_kind="collaboration_expanded")
        if item["status"] != "prepared":
            raise ValueError("本次展开已被拒绝，请处理恢复关口后重试")
        record = (await io_call(self.store.load, self.run_id))
        roles = (record.get("role_models") or {}).get("roles") or {}
        override = ledger.state(record).get("acceptance_model_overrides", {}).get(node.node_id)
        reviewer = self.pool.require(override or roles.get("reviewer") or roles.get("verifier"), "reviewer")
        proposer = self.pool.require(model, "executor")
        if not self.pool.independent(proposer, reviewer):
            raise ValueError("子图发布缺少不同厂商的独立复核")
        current = self.read()
        parent = current["contracts"][node.node_id]
        payload = {"parent_contract": parent, "fragment": item["fragment"],
                   "entries": {key: current["entries"][key] for key in parent["requirement_ids"] + parent["constraint_ids"]},
                   "authority": "仅分解既有合同；目标、方法、验收和权限保持原约束；父合同必须重新完整验收"}
        review_budget = model_context_budget(self.pool, reviewer, current['policy'], system=REVIEW_SYSTEM)
        if (not review_budget.fits(json.dumps(payload, ensure_ascii=False))
                or any(item["fragment"]["contracts"][key]["requirement_ids"] != parent["requirement_ids"]
                       for key in item["fragment"]["child_ids"])):
            from mewcode.orchestration.context_partition import review_expansion
            review = await review_expansion(self, node_id=node.node_id, item=item,
                                           reviewer=reviewer, identity=identity, system=REVIEW_SYSTEM)
        else:
            review = await self.coordinator.call(kind="semantic_review", identity="graph:" + identity,
                payload=payload, model=reviewer, system=REVIEW_SYSTEM, related_contracts=item["affected"])
        if review.get("passed") is not True or not review.get("evidence"):
            (await durable_call(self.store.mutate, self.run_id, lambda record: graph.reject(record, identity,
                reason=str(review.get("reason") or "独立复核未通过")), durable=True, return_snapshot=False))
            (await durable_call(self.mutate, lambda value: value["attempts"][attempt["id"]].update(status="rejected")))
            (await durable_call(self.request_recovery, scope="contract:" + node.node_id, keys=[node.node_id],
                kind="expansion_rejected", reason=str(review.get("reason") or "子任务划分未通过独立复核")))
            return replace(result, status="blocked", error_kind="collaboration_expansion")
        (await durable_call(self.store.mutate, self.run_id, lambda record: graph.publish(record, identity, review=review,
            proposer_vendor=self.pool.vendor(proposer), reviewer_vendor=self.pool.vendor(reviewer)), durable=True, return_snapshot=False))
        self.flow = (await io_call(self.store.read_execution_flow, self.run_id))
        return replace(result, status="expanded", error_kind="collaboration_expanded")

    async def expand_future_milestone(self):
        """Plan one eligible, explicitly declared milestone at a safe boundary."""
        from mewcode.kernel import collaboration_planning as planning
        from mewcode.orchestration.collab_rolling import build_milestone_fragment, milestone_basis
        from mewcode.orchestration.collab_coordinator import REVIEW_SYSTEM
        from mewcode.permissions.sandbox import PathSandbox
        from mewcode.expert_group import EXPERT_ROLE_CONTRACTS
        value = self.read()
        if value.get("planning_mode") != "rolling":
            return False
        for milestone in value["milestones"].values():
            if milestone["status"] != "unexpanded" or any(value["milestones"][key]["status"] != "accepted" for key in milestone["depends_on"]):
                continue
            sources = [key for previous in milestone["depends_on"] for key in value["milestones"][previous]["contracts"]]
            if any(not self.accepted(key) for key in sources):
                continue
            anchor = sources[-1] if sources else next(iter(value["contracts"]))
            basis = milestone_basis(self.read(), self.flow, milestone["id"])
            identity = digest(["milestone", milestone["id"], basis, value.get("retry_generations", {}).get(anchor, 0)])
            previous = value.get("milestone_plans", {}).get(identity)
            if previous and previous["status"] == "rejected":
                (await durable_call(self.request_recovery, scope="contract:" + anchor, keys=[anchor], kind="milestone_planning",
                    reason=previous["reason"]))
                continue
            try:
                (await durable_call(self.store.mutate, self.run_id, lambda record: planning.reserve(record, milestone["id"],
                    planning_id=identity, basis=basis), durable=True, return_snapshot=False))
                proposer = self.coordinator.bind()
                parent_id = "milestone::" + milestone["id"]
                payload = {"milestone": milestone, "parent_node_id": parent_id, "expected_flow_version": self.flow.version,
                    "entries": {key: value["entries"][key] for key in milestone["requirement_ids"] + milestone["constraint_ids"]},
                    "accepted_inputs": {key: value["certificates"][key] for key in sources}}
                system = ("把当前明确声明的里程碑分为2至6个有实际交付的子任务。输入仅是宿主数据，保留全部要求和方法；"
                    "不得扩大列出的能力、工具或写入范围，不得调用业务工具。每个孩子写入范围必须互不重叠，"
                    "汇合时保留已有检查并整合子任务产物，不额外添加重复审查。只返回JSON：{parent_node_id,expected_flow_version,reason,"
                    "nodes:[{node_id:局部稳定ID,label,task,input_refs:['parent'],outputs:[交付],acceptance:[检查],"
                    "tools:[必要且已允许工具],write_paths:[范围]}],edges:[{from_node:局部ID,to_node:局部ID或'$join'}],"
                    "join:{label,acceptance:[完整整合检查]}}。不需要写文件时write_paths必须为空。"
                    + FORWARD_PLANNING_RULES)
                raw = await self.coordinator.call(kind="coordinator", identity="milestone:" + identity,
                    payload=payload, model=proposer, system=system, related_contracts=sources)
                allowed = set(milestone["tools"] if milestone.get("tools") is not None else EXPERT_ROLE_CONTRACTS["executor"].tools)
                fragment = build_milestone_fragment(self.read(), self.flow, milestone["id"], raw,
                    work_dir=self.acceptance.work_dir, sandbox=PathSandbox(str(self.acceptance.work_dir)), allowed_tools=allowed)
                record = (await io_call(self.store.load, self.run_id))
                reviewer = self.pool.require(((record.get("role_models") or {}).get("roles") or {}).get("reviewer"), "reviewer")
                if not self.pool.independent(proposer, reviewer):
                    raise ValueError("里程碑规划需要不同厂商的独立复核")
                review = await self.coordinator.call(kind="semantic_review", identity="milestone-review:" + identity,
                    payload={**payload, "fragment": fragment}, model=reviewer, system=REVIEW_SYSTEM, related_contracts=sources)
                if review.get("passed") is not True or not review.get("evidence"):
                    raise ValueError(str(review.get("reason") or "里程碑规划未通过独立复核"))
                if any(not self.accepted(key) for key in sources):
                    raise ValueError("规划期间前序交付已变化")
                (await durable_call(self.store.mutate, self.run_id, lambda record: planning.publish(record, identity, fragment, review=review,
                    proposer_vendor=self.pool.vendor(proposer), reviewer_vendor=self.pool.vendor(reviewer)), durable=True, return_snapshot=False))
                from mewcode.kernel.collaboration_graph import effective_flow
                self.flow = (await io_call(self.store.read_execution_flow, self.run_id))
                return True
            except lineage_budget.BudgetExhausted as exc:
                self.request_budget_recovery(anchor, exc)
            except ValueError as exc:
                pending = self.read('milestone_plans').get("milestone_plans", {}).get(identity)
                if pending and pending["status"] == "planning":
                    (await durable_call(self.store.mutate, self.run_id, lambda record, reason=str(exc):
                        planning.reject(record, identity, reason=reason), durable=True, return_snapshot=False))
                (await durable_call(self.request_recovery, scope="contract:" + anchor, keys=[anchor], kind="milestone_planning", reason=str(exc)))
        return False

    async def recover_fault(self, node, result, policy, *, identity, scope=None, keys=None):
        """Use the existing host ladder without spending a contract rejection."""
        from mewcode.experts.waits import BranchWaitPaused
        scope, keys = scope or "contract:" + node.node_id, keys or [node.node_id]
        current = self.read()
        generation = [current["spec_version"], current.get("retry_generations", {}).get(node.node_id, 0),
                      current.get("integration_generations", {}).get(scope, 0)]
        fault_id = digest([identity, generation])
        used = current.get("fault_recoveries", {}).get(fault_id, 0)
        host = getattr(self, "host", None)
        handler = getattr(host, "recovery_handler", None)
        kind = execution_fault(result) or result.error_kind
        from mewcode.kernel.collaboration_faults import MODEL_UNAVAILABLE, REQUIRES_CLEARANCE, ASSISTANCE
        if kind in REQUIRES_CLEARANCE:
            (await durable_call(self.request_recovery, scope=scope, keys=keys, kind="infrastructure",
                reason="执行或验收服务尚未恢复：" + result.text))
            return None
        if handler is None or used >= current["policy"].get("max_fault_recoveries", 3):
            (await durable_call(self.request_recovery, scope=scope, keys=keys,
                kind='infrastructure' if kind in MODEL_UNAVAILABLE | ASSISTANCE else 'service_unavailable', reason=result.text))
            return None
        def reserve(record):
            value = ledger.state(record)
            value.setdefault("fault_recoveries", {})[fault_id] = used + 1
            ledger.event(value, "infrastructure_recovery", contract_id=node.node_id, error_kind=kind, round=used + 1)
            if kind in TRANSIENT:
                from mewcode.experts.node_ledger import ledger_for_run
                findings = (record.get('stage_preflight') or {}).get(node.node_id)
                preflight_fault = (kind == 'environment_fault' and isinstance(findings, list)
                    and any(row.get('severity') == 'error' for row in findings)
                    and result.text == '当前节点环境预检未通过：' + json.dumps(findings, ensure_ascii=False))
                ledger_for_run(record).record(node.node_id, "blocker", {"kind": host_blocker(kind),
                    "ref": "collaboration-fault:" + fault_id,
                    "fault_origin": 'node_preflight' if preflight_fault else 'execution'},
                    identity=["collaboration-fault", fault_id, used])
        (await durable_call(self.store.mutate, self.run_id, reserve, durable=True, return_snapshot=False))
        try:
            decision = await handler(node=node, result=result, policy=policy,
                allowed=["continue_checkpoint", "diagnose_path"],
                host_facts={"trigger": kind or result.status, "boundary_type": "recovery", "hard_boundary": "",
                            "preserve_collaboration_contract": True, "defer_human": True}, account=None)
        except BranchWaitPaused as exc:
            decision = {"action": "human", "chosen": {"next_step": exc.reason}}
        assistance_pending = kind in ASSISTANCE and self.store.read_turn_boundary(
            self.run_id, node.node_id)['assistance_request'].get('status') in {'pending', 'running'}
        if (decision and decision.get("action") in {"continue_checkpoint", "diagnose_path"}
                and not assistance_pending
                and not unresolved_calls((await io_call(self.store.read_execution_fields, self.run_id, "kernel_calls")), keys)):
            return decision
        reason = str((decision or {}).get("chosen", {}).get("next_step") or result.text)
        (await durable_call(self.request_recovery, scope=scope, keys=keys,
            kind='infrastructure' if kind in MODEL_UNAVAILABLE | ASSISTANCE else 'service_unavailable', reason=reason))
        return None

    def clear_transient_faults(self, node_id):
        if not any(row.get('kind') == 'infrastructure_recovery' and row.get('contract_id') == node_id
                   for row in self.read('timeline').get('timeline', [])):
            return
        def clear(record):
            from mewcode.experts.node_ledger import ledger_for_run
            from mewcode.kernel.blockers import policy
            facts = ledger_for_run(record)
            for fact in facts.view(node_id).active_blockers:
                if not str(fact.payload.get("ref", "")).startswith("collaboration-fault:"):
                    continue
                evidence = facts.record(node_id, "receipt", {"status": "completed", "host_verified": True,
                    "request_id": fact.payload["ref"]}, identity=["collaboration-recovered", fact.fact_id])
                facts.record(node_id, "clearance", {"blocker_fact_id": fact.fact_id,
                    "clearance_type": policy(fact.payload["kind"]).clearance, "evidence_fact_id": evidence.fact_id},
                    identity=["collaboration-recovered-clearance", fact.fact_id])
        self.store.mutate(self.run_id, clear, durable=True, return_snapshot=False)

    async def review(self, payload, context):
        record = (await io_call(self.store.load, self.run_id))
        bindings = (record.get("role_models") or {}).get("roles") or {}
        override = ledger.state(record).get("acceptance_model_overrides", {}).get(context["node_id"])
        selected = override or (bindings.get("reviewer") if context.get("executor_role") == "auditor" else bindings.get("verifier"))
        if not selected or self.review_callback is None:
            raise ValueError("未配置独立验收模型，请在运行设置中选择或人工验收")
        reviewer = self.pool.require(selected, "verifier")
        executor = self.pool.require(context["executor_model"], "executor")
        if (not override and context.get("executor_role") == "auditor" and not self.pool.independent(executor, reviewer)
                and "reviewer" not in (record.get("role_models") or {}).get("explicit_roles", [])):
            # A default auditor cannot accept its own work. The already-bound
            # planner may verify that audit; no new provider route is invented.
            reviewer = self.pool.require(bindings.get("planner"), "verifier")
        if not self.pool.independent(executor, reviewer):
            raise ValueError("验收模型与执行模型厂商相同，不能作为独立验收")
        current = self.read()
        from mewcode.kernel.execution_evidence import execution_evidence
        source_attempt = current.get("attempts", {}).get(context["attempt_id"])
        host_evidence = execution_evidence(record, source_attempt) if source_attempt else None
        payload = {**copy.deepcopy(payload), "requirements": {
            key: copy.deepcopy(current["entries"][key]) for key in payload["contract"]["requirement_ids"]},
            "constraints": {key: copy.deepcopy(current["entries"][key]) for key in payload["contract"]["constraint_ids"]},
            "loop_context": context.get("loop_context"), "host_execution_evidence": host_evidence}
        from mewcode.orchestration.review_revision import evidence_basis, review_revision, compact_revision
        review_evidence_basis = evidence_basis(record, context)
        identity = digest(["acceptance", context["attempt_id"], context.get("acceptance_round", 0), payload["check"], payload["artifacts"]])
        previous = self.read('calls')["calls"].get(identity)
        from mewcode.kernel.collaboration_faults import REQUIRES_CLEARANCE
        binding = review_binding(context, payload["check"])
        binding.update(call_id=identity, handoff_hash=digest(payload.get("handoff") or {}))
        paused_binding = paused_review_binding(record, identity, previous) if previous else None
        interrupted = bool(paused_binding == binding and previous
            and previous.get("model") == reviewer.as_dict())
        resume_wait = bool(previous and (previous.get("status") == "waiting" or interrupted)
            and int(record.get("attempt", 1)) > int(previous.get("run_attempt", 1)))
        context = {**context, "resume_acceptance": resume_wait}
        if previous:
            if previous.get("result") is not None and not resume_wait:
                return previous["result"]
            if not resume_wait or unresolved_calls(record, [context["node_id"]]):
                raise ValueError("前次验收调用结果未确认，请核对后恢复")
        capacity = model_context_budget(self.pool, reviewer, current['policy'], tools=True)
        # Ordinary and integration acceptance share this boundary. Preserve the
        # complete check, requirements and evidence: independent partial PASSes
        # are not a substitute for an indivisible whole-contract check.
        # Legacy/offline catalogs have no model capacity to validate here.
        if capacity.unit == 'estimated_tokens' and not capacity.fits(json.dumps(payload, ensure_ascii=False)):
            raise ContextBudgetExceeded(capacity.exceeded())
        revision = review_revision(current, payload, context, reviewer.as_dict(), review_evidence_basis)
        if revision:
            for candidate in (revision, compact_revision(revision)):
                extended = {**payload, 'revision_context': candidate}
                if capacity.unit != 'estimated_tokens' or capacity.fits(json.dumps(extended, ensure_ascii=False)):
                    payload = extended
                    break
            else:
                notice = {**payload, 'revision_context_omitted': '历史差异超出上下文预算；按完整当前合同核验。'}
                if capacity.fits(json.dumps(notice, ensure_ascii=False)):
                    payload = notice
        def reserve(value):
            if context.get("integration_basis"):
                if self.integration_basis(value, context["integration_contracts"]) != context["integration_basis"]:
                    raise ValueError("集成验收的相关版本已变化")
            else:
                ledger.check_attempt(value, context["attempt_id"])
            if identity in value["calls"] and not resume_wait:
                raise ValueError("此验收调用已派发")
            if not resume_wait and sum(call["kind"] in {"acceptance", "semantic_review"} for call in value["calls"].values()) >= value["policy"]["max_review_calls"]:
                raise ValueError("独立验收调用预算已用尽")
            history = copy.deepcopy((previous or {}).get("wait_history", []))
            if resume_wait:
                if value["calls"].get(identity) != previous:
                    raise ValueError("验收调用状态已变化，请重新核对")
                history.append(copy.deepcopy(previous.get("result") or {
                    "execution_error": "user_paused", "recovery_node_id": binding["recovery_node_id"],
                    "source": "persisted_paused_review_stage"}))
            value["calls"][identity] = {"kind": "acceptance", "status": "dispatched", "model": reviewer.as_dict(),
                "run_attempt": record.get("attempt", 1), "wait_history": history, "binding": binding,
                'review_evidence_basis': review_evidence_basis,
                'revision_baseline': (payload.get('revision_context') or {}).get('baseline_attempt_id')}
            if source_attempt:
                value["attempts"][context["attempt_id"]]["acceptance_input_evidence_hash"] = host_evidence["fingerprint"]
        (await durable_call(self.mutate, reserve))
        started = time.monotonic()
        cancelled = None
        try:
            raw = await self.review_callback(reviewer, payload, context)
            try:
                result = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                result = None
            if (not isinstance(result, dict) or type(result.get("passed")) is not bool
                    or not isinstance(result.get("evidence"), (list, str))
                    or not result.get("evidence") and not result.get("execution_error")):
                result = {"passed": False, "evidence": [], "execution_error": "acceptance_format",
                          "reason": "独立验收输出无效，必须明确是否通过并给出证据"}
        except asyncio.CancelledError as exc:
            cancelled = exc
            result = {"passed": False, "evidence": [], "execution_error": "user_paused",
                      "reason": "验收已暂停，保留原交付并等待宿主继续。",
                      "recovery_node_id": binding["recovery_node_id"]}
        except Exception as exc:
            from mewcode.experts.waits import BranchWaitPaused
            result = {"passed": False, "evidence": [], "error": str(exc), "execution_error": "acceptance_unavailable"}
            if isinstance(exc, BranchWaitPaused):
                result.update(execution_error="user_input", reason=exc.reason,
                    recovery_node_id=exc.node_id, request_id=exc.request_id)
        def finish(value):
            value["calls"][identity].update(status="waiting" if result.get("execution_error") in REQUIRES_CLEARANCE else "finished", result=copy.deepcopy(result),
                                            duration_ms=round((time.monotonic() - started) * 1000))
        (await durable_call(self.mutate, finish))
        if cancelled is not None:
            raise cancelled
        return result

    @staticmethod
    def batch_deliveries(value, keys):
        latest = {}
        for attempt in value["attempts"].values():
            iteration = attempt.get("iteration") or {}
            key = attempt["contract_id"]
            if (key not in keys or attempt["status"] != "accepted" or not iteration.get("current_item")
                    or attempt.get("semantic_basis") != ledger.semantic_basis(value, key)):
                continue
            latest[digest([key, iteration["loop_id"], iteration["current_item"]])] = attempt
        return latest

    @staticmethod
    def integration_basis(value, keys):
        return digest({"contracts": {key: value["contracts"][key] for key in keys},
                       "certificates": {key: value["certificates"].get(key) for key in keys},
                       "batch_deliveries": CollaborationRuntime.batch_deliveries(value, keys),
                       "entries": value["entries"], "epoch": value["coordinator_epoch"]})

    @staticmethod
    def quality_scope(value, keys):
        return {'quality_scope': [dict(node_id=key, label=value['contracts'][key]['label'],
                    checks=copy.deepcopy(value['contracts'][key]['checks'])) for key in sorted(keys)],
                'milestone_checks': {key: copy.deepcopy(row['checks'])
                    for key, row in value['milestones'].items() if row.get('checks')}}

    def final_review_basis(self, value, keys):
        final = self.flow.finalizer_node_id
        return digest([self.integration_basis(value, sorted(key for key in keys if key != final)),
                       value['contracts'][final], self.quality_scope(value, keys)])

    def bound_final_verdict(self, value, keys, model, results):
        """Reuse an explicit final audit only for its host-bound, unchanged scope."""
        final = self.flow.finalizer_node_id
        if self.flow.node_map[final].role != 'auditor':
            return None
        attempt = value['attempts'][value['certificates'][final]['attempt_id']]
        verdict = (results.get(final).verdict or {}) if results.get(final) else {}
        if (attempt.get('final_review_basis') != self.final_review_basis(value, keys)
                or verdict.get('verdict') not in {'PASS', 'NEEDS_ATTENTION'} or not verdict.get('evidence')):
            return None
        for key in keys:
            if key == final or self.flow.node_map[key].role == 'auditor':
                continue
            producer = value['attempts'][value['certificates'][key]['attempt_id']].get('executor_model')
            if not producer or not self.pool.independent(ModelRef.from_value(producer), model):
                return None
        return {'passed': verdict['verdict'] == 'PASS', 'evidence': copy.deepcopy(verdict['evidence']),
                'issues': copy.deepcopy(verdict.get('issues') or []), 'source_attempt_id': attempt['id']}

    async def integrate(self, identity, keys, *, checks, model, policy, results):
        """Review a completed draft once; milestone checks are prerequisites."""
        from mewcode.expert_group import ExpertRoleResult
        from mewcode.kernel.acceptance import report_ready, execution_fault as acceptance_fault
        value = self.read()
        if not keys or any(not self.accepted(key) for key in keys):
            return False
        for key in keys:
            workspace = value['attempts'][value['certificates'][key]['attempt_id']].get('workspace')
            if workspace and not await io_call(self.workspaces.verify_sealed, workspace):
                await durable_call(self.mutate, lambda current, key=key: ledger.invalidate(
                    current, {key}, '隔离交付在集成检查前发生变化'))
                return False
        basis = self.integration_basis(value, keys)
        saved = value.get("integrations", {}).get(identity)
        if saved and saved["basis"] == basis:
            if report_ready(saved['contract'], saved['report']):
                return True
            from mewcode.kernel.collaboration_faults import REQUIRES_CLEARANCE, report_fault
            if (report_fault(saved["report"]) in REQUIRES_CLEARANCE
                    and (await io_call(self.store.read_execution_fields, self.run_id, "attempt")).get("attempt", 1) <= saved.get("run_attempt", 1)):
                return False
            if not acceptance_fault(saved['contract'], saved['report']):
                retained = {**copy.deepcopy(saved['report']), 'ready': True,
                            'artifacts_unchanged': self.acceptance.unchanged(saved['report'].get('artifacts', []))}
                if report_ready(saved['contract'], retained):
                    (await durable_call(self.mutate, lambda current: current['integrations'][identity].update(report=retained)))
                    return True
                await self.plan_integration_rework(identity, keys, saved["report"], basis)
                return False
        contracts = [value["contracts"][key] for key in keys]
        contract = {"id": identity, "version": value["spec_version"], "goal": value["goal"],
                    "requirement_ids": sorted({key for c in contracts for key in c["requirement_ids"]}),
                    "constraint_ids": sorted({key for c in contracts for key in c["constraint_ids"]}),
                    "checks": checks or [{"id": identity + "_integration", "version": 1, "kind": "review",
                        "criteria": ["集中核对本轮交付与原始目标，明确已实现内容、未确认事实和下一轮需修订的问题"]}]}
        if identity == 'final':
            # Each quality requirement appears here once, after useful work has
            # flowed through the graph. It is not another gate at every node.
            contract.update(self.quality_scope(value, keys))
        artifacts, inline = {}, []
        for key in keys:
            for row in value["certificates"][key]["artifacts"]:
                if row["kind"] == "file":
                    artifacts[row["path"]] = {"path": row["path"]}
                else:
                    inline.append({"name": key + "/" + row["name"], "content": row["content"]})
        batch = self.batch_deliveries(value, keys)
        for batch_id, delivery in batch.items():
            for row in delivery["report"]["artifacts"]:
                if row["kind"] == "file":
                    snapshot = row["snapshot_path"]
                    if not self.acceptance.unchanged([{**row, "path": snapshot}]):
                        raise ValueError("批处理历史产物的固定版本已变化")
                    artifacts[snapshot] = {"path": snapshot}
                else:
                    inline.append({"name": batch_id + "/" + row["name"], "content": row["content"]})
            inline.append({"name": batch_id + "/批次范围", "content": json.dumps(delivery["iteration"], ensure_ascii=False)})
        result = ExpertRoleResult(text="\n".join(results[key].text for key in keys),
            handoff={"status": "completed", "artifacts": list(artifacts.values()), "inline_outputs": inline})
        context = {"run_id": self.run_id, "node_id": keys[-1], "attempt_id": digest([identity, basis,
                    value.get("integration_generations", {}).get(identity, 0)]),
                   "executor_model": model.as_dict(), "executor_role": self.flow.node_map[keys[-1]].role,
                   "policy": policy, "integration_basis": basis, "integration_contracts": keys,
                   "phase": "review" if identity == 'final' else "handoff"}
        acceptance = self.acceptance
        final_workspace = value['attempts'][value['certificates'][keys[-1]]['attempt_id']].get('workspace')
        if final_workspace:
            policy = self.workspaces.execution_policy(policy, final_workspace, value['contracts'][keys[-1]])
            context['policy'] = policy
            acceptance = self.workspaces.acceptance(final_workspace)
        verdict = self.bound_final_verdict(value, keys, model, results) if identity == 'final' else None
        if verdict is not None:
            async def reuse_verdict(*_):
                return copy.deepcopy(verdict)
            acceptance = copy.copy(acceptance)
            acceptance.review = reuse_verdict
        while True:
            if unresolved_calls((await io_call(self.store.read_execution_fields, self.run_id, "kernel_calls")), keys):
                (await durable_call(self.request_recovery, scope=identity, keys=keys, kind="infrastructure",
                    reason="仍有结果不明的验收操作，请先核对宿主回执。"))
                return False
            if self.integration_basis(self.read(), keys) != basis:
                raise ValueError("集成验收的相关版本已变化")
            context["acceptance_round"] = self.read('acceptance_rounds').get("acceptance_rounds", {}).get(context["attempt_id"], 0)
            report = await acceptance.check(contract, result, context=context)
            host_attempt = (await io_call(self.store.read_execution_fields, self.run_id, "attempt")).get("attempt", 1)
            def finish(current):
                if self.integration_basis(current, keys) != basis or any(not ledger.valid_certificate(current, key) for key in keys):
                    raise ValueError("集成验收期间规格或交付已变化")
                current.setdefault("integrations", {})[identity] = {"basis": basis, "report": report,
                    "keys": list(keys), "contract": copy.deepcopy(contract),
                    "spec_version": current["spec_version"], "coordinator_epoch": current["coordinator_epoch"],
                    "run_attempt": host_attempt,
                    "certificates": {key: digest(current["certificates"][key]) for key in keys}}
                ledger.event(current, "integration_unavailable" if report.get("execution_error") else
                    "integration_accepted" if report["passed"] else "draft_reviewed" if report.get('ready') else "integration_rejected",
                    scope=identity, report_hash=digest(report))
            (await durable_call(self.mutate, finish))
            if not report.get("execution_error"):
                break
            fault = ExpertRoleResult(text=json.dumps(report, ensure_ascii=False), status="needs_attention", error_kind=report["execution_error"])
            if not await self.recover_fault(self.flow.node_map[keys[-1]], fault, policy,
                    identity=context["attempt_id"], scope=identity, keys=keys):
                return False
            (await durable_call(self.mutate, lambda current: current.setdefault("acceptance_rounds", {}).__setitem__(
                context["attempt_id"], context["acceptance_round"] + 1)))
        if not report["passed"] and (identity == 'final' or not report_ready(contract, report)):
            await self.plan_integration_rework(identity, keys, report, basis)
        return report_ready(contract, report)

    async def plan_integration_rework(self, scope, keys, report, basis):
        """Route concrete feedback straight to its producer, without a planner."""
        from mewcode.orchestration.review_feedback import issue_owners, issue_route
        value = self.read()
        generation = [value["spec_version"], value.get("integration_generations", {}).get(scope, 0)]
        history = value.get("integration_reworks", {}).get(scope, {})
        rounds = history.get("rounds", []) if history.get("generation") == generation else []
        instructions, followups = {}, []
        for check in report.get('checks', []):
            for issue in (check.get('evidence') or {}).get('issues', []):
                if not isinstance(issue, dict):
                    continue
                route = issue_route(issue)
                if route != 'producer_fix':
                    followups.append({**copy.deepcopy(issue), 'route': route})
                    continue
                description = issue.get('description')
                if isinstance(description, str) and description.strip():
                    for key in issue_owners(value, keys, issue):
                        instructions.setdefault(key, []).append(description + ('；验证：' + issue['acceptance_test']
                            if isinstance(issue.get('acceptance_test'), str) and issue['acceptance_test'] else ''))
        if followups:
            await self.save_review_followups(scope, keys, basis, followups)
        if not instructions:
            if any(row['route'] == 'user_decision' for row in followups):
                return 'user_decision'
            if any(row['route'] == 'capability_gap' for row in followups):
                return 'capability_gap'
            if followups and all(row['route'] in {'downstream', 'out_of_scope'} for row in followups):
                return 'downstream'
            return 'unresolved_owners'  # Do not invent a producer or a repair.
        marker = digest([instructions, [{k: row.get(k) for k in ('path', 'name', 'sha256')}
                                       for row in report.get('artifacts', [])]])
        if len(rounds) >= value['policy'].get('max_integration_reworks', 3):
            return 'limit'
        if any(row.get('marker') == marker for row in rounds):
            return 'unchanged'
        event_id = digest(['integration-rework', scope, basis, generation, len(rounds)])
        selected = set(instructions)
        for group in getattr(self, "loop_groups", []):
            if group & selected:
                selected |= group
        def apply(current):
            if self.integration_basis(current, keys) != basis:
                raise ValueError("返工提议对应的集成交付已经变化")
            current.setdefault("integration_reworks", {})[scope] = {"generation": generation,
                "rounds": rounds + [{"event_id": event_id, "basis": basis, 'marker': marker,
                                     'contract_ids': sorted(selected)}]}
            affected = ledger.invalidate(current, selected, "集中复核提出了可定位的修订项")
            for key in selected:
                current.setdefault("rework_instructions", {})[key] = {"scope": scope, "event_id": event_id,
                    "instructions": '\n'.join(instructions.get(key) or ['配合本轮相关修订，复用已完成结果。']),
                    "report_hash": digest(report)}
            ledger.event(current, "integration_rework", scope=scope, contracts=sorted(affected), reason="按具体复核问题定向修订")
            return affected
        self.rework_requested.update((await durable_call(self.mutate, apply)))
        return 'scheduled'

    async def save_review_followups(self, scope, keys, basis, issues):
        def apply(current):
            if self.integration_basis(current, keys) != basis:
                raise ValueError('复核问题对应的输入已经变化')
            current.setdefault('review_followups', {})[scope] = {
                'basis': basis, 'keys': list(keys), 'spec_version': current['spec_version'], 'issues': issues}
        await durable_call(self.mutate, apply)

    async def resolve_review_decisions(self, node, issues):
        from mewcode.orchestration.review_decisions import (
            question_for, prior_answer, source_basis, record_answer, prepare_interaction)
        from mewcode.experts.waits import BranchWaitPaused
        choices = [issue for issue in issues if issue.get('blocking', True)
                   and issue.get('resolution') in {'user_decision', 'capability_gap'}]
        if not choices:
            return None
        saved_answers = []
        for issue in choices:
            record = await io_call(self.store.load, self.run_id)
            issue_id = issue.get('issue_id') or digest(issue)
            prior = prior_answer(record, node.node_id, issue_id)
            if prior:
                if prior['effect'] != 'review':
                    return 'decision_recorded'
                if not prior.get('applied'):
                    saved_answers.append(prior)
                continue
            source = source_basis(record, node.node_id)
            question, effects = question_for(issue)
            request_ids = []
            await durable_call(self.store.mutate, self.run_id, lambda current: request_ids.append(prepare_interaction(
                current, node_id=node.node_id, issue_id=issue_id, source=source, question=question)), durable=True, return_snapshot=False)
            request_id = request_ids[0]
            if self.decide_callback is None:
                return 'user_decision'
            try:
                response = await self.decide_callback(node, request_id, [question])
            except BranchWaitPaused:
                return 'user_decision'
            saved_rows = []
            await durable_call(self.store.mutate, self.run_id, lambda current: saved_rows.append(record_answer(current,
                node_id=node.node_id, issue_id=issue_id, source=source, question_id=response['question_id'],
                answer=response['answer'], effects=effects)), durable=True, return_snapshot=False)
            saved = saved_rows[0]
            if saved['effect'] == 'request_revision':
                await durable_call(self.request_recovery, scope='contract:' + node.node_id, keys=[node.node_id],
                    kind='user_decision', reason='已记录要求修订意图。请通过现有要求修订入口提交具体变更；审核和外发范围尚未改变。')
                return 'decision_recorded'
            if saved['effect'] == 'defer':
                return 'decision_recorded'
            saved_answers.append(saved)
        if not saved_answers:
            return 'decision_recorded'
        affected_keys = []
        def recheck(record):
            value = ledger.state(record)
            for saved in saved_answers:
                if source_basis(record, node.node_id) != saved['source_basis']:
                    raise ValueError('用户答复后审核输入发生变化，未重新派发。')
                identity = digest([node.node_id, saved['issue_id'], saved['source_basis']])
                value['review_decision_answers'][identity]['applied'] = True
            affected = ledger.invalidate(value, {node.node_id}, '用户答复已保存，只重新核验受影响审核。')
            value.setdefault('retry_generations', {})[node.node_id] = value.get('retry_generations', {}).get(node.node_id, 0) + 1
            ledger.event(value, 'review_recheck_requested', node_id=node.node_id,
                         question_ids=[saved['question_id'] for saved in saved_answers])
            affected_keys.extend(affected)
        await durable_call(self.store.mutate, self.run_id, recheck, durable=True, return_snapshot=False)
        self.rework_requested.update(affected_keys)
        return 'scheduled'

    async def repair_intermediate_review(self, node, result):
        """Close a failed pass-only audit by repairing its existing producers."""
        from mewcode.experts.flow import normalize_edge_condition
        verdict = result.verdict or {}
        outgoing = [edge for edge in self.flow.edges if edge.from_node == node.node_id]
        if (node.role != 'auditor' or node.node_id == self.flow.finalizer_node_id
                or result.status != 'completed' or not self.accepted(node.node_id)
                or verdict.get('verdict') not in {'NEEDS_ATTENTION', 'PASS'} or not verdict.get('evidence')
                or not outgoing or any(normalize_edge_condition(edge.condition) != 'pass' for edge in outgoing)):
            return
        value = self.read()
        ancestors, pending = set(), list(value['contracts'][node.node_id].get('bound_dependencies', []))
        while pending:
            key = pending.pop()
            if key in ancestors or key == node.node_id:
                continue
            ancestors.add(key)
            contract = value['contracts'][key]
            pending.extend(contract.get('bound_dependencies', contract['depends_on']))
        keys = sorted(key for key in ancestors if self.flow.node_map[key].role != 'auditor'
                      and (self.flow.meta.get('node_kinds') or {}).get(key) != 'gate' and self.accepted(key))
        # Bind feedback to the audited inputs and preserve the original failed
        # verdict. Downstream pass/authorization edges remain closed throughout.
        report = {'checks': [{'evidence': {'issues': copy.deepcopy(verdict.get('issues') or [])}}],
                  'artifacts': [row for key in keys for row in value['certificates'][key]['artifacts']]}
        if verdict.get('verdict') == 'PASS':
            from mewcode.orchestration.review_feedback import issue_route
            followups = [{**copy.deepcopy(issue), 'route': issue_route(issue)} for issue in verdict.get('issues', [])
                         if isinstance(issue, dict) and issue_route(issue) in {'downstream', 'out_of_scope'}]
            if followups:
                await self.save_review_followups('review:' + node.node_id, keys, self.integration_basis(value, keys), followups)
            return
        outcome = await self.plan_integration_rework('review:' + node.node_id, keys, report, self.integration_basis(value, keys))
        if outcome in {'user_decision', 'capability_gap', 'unresolved_owners', 'unchanged', 'limit'}:
            from mewcode.orchestration.review_intervention import intervene
            try:
                intervention = await intervene(self, node, verdict.get('issues') or [])
            except lineage_budget.BudgetExhausted as exc:
                await durable_call(self.request_budget_recovery, node.node_id, exc)
                return 'limit'
            except ValueError as exc:
                await durable_call(self.mutate, lambda current, reason=str(exc): ledger.event(current, 'review_intervention_failed',
                    node_id=node.node_id, reason=reason))
                await durable_call(self.request_recovery, scope='contract:' + node.node_id, keys=[node.node_id],
                    reason=str(exc), kind='coordination_wait')
                return 'coordination_wait'
            if intervention == 'scheduled':
                return intervention
            if isinstance(intervention, dict) and intervention.get('outcome') == 'user_decision':
                return await self.resolve_review_decisions(node, intervention['issues']) or 'user_decision'
            if intervention is not None:
                return 'coordination_wait'
        if outcome in {'user_decision', 'capability_gap'}:
            return await self.resolve_review_decisions(node, verdict.get('issues') or []) or outcome
        return outcome

    def integration_reason(self, scope, report):
        label = "最终整体验收" if scope == "final" else "里程碑“" + self.read('milestones')["milestones"][scope.split(":", 1)[1]]["goal"] + "”验收"
        details = [str(row.get("evidence", {}).get("reason") or row.get("evidence", {}).get("error")
                       or row.get("evidence", {}).get("evidence") or row["id"])
                   for row in report.get("checks", []) if not row["passed"]]
        return label + "未通过：" + (report.get("reason") or "；".join(details) or "缺少完整的验收证据")

    def patch_context(self, attempt, policy, proposal=None):
        from mewcode.orchestration.collab_patch import PatchCandidate
        patch = PatchCandidate(self, attempt['id'])
        if proposal and not self.read('attempts')['attempts'][attempt['id']].get('patch'):
            patch.prepare(proposal['baseline_id'], proposal['decision'])
        transaction = patch.open()
        policy = replace(policy, work_dir=str(transaction.candidate), expansion_holder=None,
            collaborative_expansion=False, resume_messages=False,
            candidate_context={'source': str(transaction.source), 'candidate': str(transaction.candidate),
                               'baseline': str(transaction.baseline), 'collaboration_patch': True})
        acceptance = Acceptance(transaction.candidate, review=self.review, command=self.acceptance.command)
        return patch, policy, acceptance

    def retain_ready_delivery(self, node_id):
        """Reuse a fixed legacy delivery rejected only for advisory quality.

        This records a new host handoff receipt, keeps the original failed
        report, and performs no model, business-tool or command replay.
        """
        from mewcode.kernel.acceptance import report_ready
        from mewcode.kernel.collaboration_recovery import gate_current
        record = self.store.load(self.run_id)
        value = ledger.state(record)
        prior = ledger.attempts_for(value, node_id)
        if not prior or ledger.valid_certificate(value, node_id):
            return False
        old = prior[-1]
        contract = value['contracts'][node_id]
        if (old['status'] != 'rejected' or old.get('patch') or old.get('superseded')
                or (old.get('delivery') or {}).get('status') != 'completed'
                or (old.get('report') or {}).get('execution_error')
                or contract.get('held') or ledger.revision_pending(value, node_id)
                or value.get('rework_instructions', {}).get(node_id)
                or old.get('retry_generation', 0) != value.get('retry_generations', {}).get(node_id, 0)
                or (record.get('node_states', {}).get(node_id) or {}).get('pending_interaction')
                or unresolved_calls(record, [node_id])):
            return False
        try:
            ledger.check_attempt(value, old['id'])
        except ValueError:
            return False
        report = copy.deepcopy(old.get('report') or {})
        report.update(ready=True, artifacts_unchanged=True, reused_from=old['id'])
        if not report_ready(contract, report) or not self.acceptance.unchanged(report.get('artifacts', [])):
            return False
        gates = [gate['id'] for gate in value.get('recovery_gates', {}).values()
                 if gate.get('scope') == 'contract:' + node_id
                 and gate.get('kind') in {'coordination_wait', 'no_progress', 'attempts_exhausted', 'collaboration_checkpoint'}
                 and gate_current(value, gate)]
        def retain(current):
            source = ledger.check_attempt(current, old['id'])
            if source != old:
                raise ValueError('已保存交付在交接核对期间变化')
            identity = digest(['readiness', old['id'], ledger.dependencies(current, node_id)])
            receipt = {key: copy.deepcopy(item) for key, item in old.items()
                       if key not in {'report', 'acceptance_fault', 'execution_fault'}}
            receipt.update(id=identity, number=len(ledger.attempts_for(current, node_id)) + 1,
                           status='checking', reused_from=old['id'], run_attempt=record.get('attempt', 1))
            current['attempts'][identity] = receipt
            ledger.complete(current, identity, report)
            for gate_id in gates:
                current['recovery_gates'][gate_id].update(status='superseded', reason='现有产物满足交接条件，质量问题留待修订。')
            ledger.event(current, 'delivery_retained', contract_id=node_id, attempt_id=identity, previous_attempt=old['id'])
        self.mutate(retain)
        return True

    async def execute(self, node, model, prompt, policy, member, invoke, *, patch_proposal=None,
                      resume_unstarted_context=False):
        from mewcode.expert_group import ExpertRoleResult
        from mewcode.experts.waits import BranchWaitPaused
        if member is not None:
            return ExpertRoleResult(text="协作工作单元需独立合同，不能隐式委派成员。", status="blocked")
        patch, acceptance = None, self.acceptance
        workspace = None
        try:
            if patch_proposal is None:
                (await durable_call(self.retain_ready_delivery, node.node_id))
            value = self.read()
            contract = value["contracts"][node.node_id]
            from mewcode.orchestration.review_revision import repair_stall_basis
            prior = ledger.attempts_for(value, node.node_id)
            if patch_proposal is None and prior and prior[-1].get('repair_stalled'):
                saved = prior[-1]
                record = await io_call(self.store.load, self.run_id)
                if (saved['status'] == 'rejected'
                        and saved['repair_stalled'] == repair_stall_basis(record, node.node_id, policy)
                        and await io_call(self.acceptance.unchanged, saved.get('report', {}).get('artifacts', []))):
                    return replace(ExpertRoleResult(**saved['delivery']), status='needs_attention',
                        error_kind='collaboration_acceptance',
                        text='连续返修未改变交付，已保留产物与具体修订项；需要新的修复依据或定向重试。')
            # Verify physical upstream deliveries immediately before dispatch.
            bound_inputs = contract.get("bound_dependencies", contract["depends_on"])
            if any(not self.accepted(key) for key in bound_inputs):
                raise ValueError("上游验收交接已失效，等待协调者处理")
            pending = [a for a in ledger.attempts_for(value, node.node_id)
                       if a["status"] == "checking" and not a.get("superseded")
                       and a["coordinator_epoch"] == value["coordinator_epoch"]
                       and a["dependencies"] == ledger.dependencies(value, node.node_id)]
            if pending:
                attempt = pending[-1]
                result = ExpertRoleResult(**attempt["delivery"])
                if attempt.get('patch'):
                    patch, policy, acceptance = self.patch_context(attempt, policy)
                if contract.get('workspace_mode'):
                    workspace = await durable_call(self.workspaces.prepare, attempt['id'])
                    policy = self.workspaces.execution_policy(policy, workspace, contract)
                    acceptance = self.workspaces.acceptance(workspace)
                from mewcode.kernel.collaboration_faults import REQUIRES_CLEARANCE, report_fault
                fault = report_fault(attempt.get("acceptance_fault") or {})
                if (fault in REQUIRES_CLEARANCE and (await io_call(self.store.read_execution_fields, self.run_id, "attempt")).get("attempt", 1)
                        <= attempt.get("acceptance_fault_host_attempt", attempt.get("run_attempt", 1))):
                    return replace(result, status="needs_attention", error_kind=fault,
                        text="验收仍等待原用户问题、权限或效果核对；请处理后继续任务。")
            else:
                # Older hosts classified a yielded assistance request as a
                # rejected delivery. Settle that exact request before another
                # business attempt; otherwise every resume exits after turn 1.
                prior = ledger.attempts_for(value, node.node_id)
                request = self.store.read_turn_boundary(self.run_id, node.node_id)['assistance_request']
                if (request.get('status') == 'pending' and prior
                        and prior[-1].get('delivery', {}).get('error_kind') == 'assistance_requested'):
                    saved = ExpertRoleResult(**prior[-1]['delivery'])
                    decision = await self.recover_fault(node, saved, policy,
                        identity='assistance:' + request['request_id'])
                    if not decision:
                        return saved
                    # The helper's durable receipt, not its suggestion alone,
                    # proves the request has been settled.
                    if self.store.read_turn_boundary(self.run_id, node.node_id)['assistance_request'].get('status') == 'pending':
                        return saved
                    value = self.read()
                # Older hosts ended productive partial work as a rejection.
                # Revalidate its actual saved stage before dispatch; this does
                # not change the delivery, its status, or any acceptance report.
                from mewcode.kernel.work_continuation import retain_checkpoint
                prior = ledger.attempts_for(value, node.node_id)
                if prior and prior[-1]["status"] == "rejected":
                    (await durable_call(self.store.mutate, self.run_id, lambda record: retain_checkpoint(record, prior[-1]["id"]), durable=True, return_snapshot=False))
                resumed = []
                def resume(record):
                    if not resume_unstarted_context:
                        resumed.append(ledger.resume_dispatch(record, node.node_id, resume_segment=policy.resume_messages))
                        return
                    current = ledger.state(record)
                    candidates = [row for row in ledger.attempts_for(current, node.node_id)
                                  if row['status'] == 'dispatched' and row.get('context_partition_deferred')]
                    if len(candidates) != 1:
                        raise ValueError('缺少可串行接续的未启动上下文尝试')
                    saved = candidates[0]
                    permit = current['lineage_budget']['reservations'][saved['budget_reservation_id']]
                    if (saved.get('budget_started') or saved.get('delivery') or permit['status'] != 'reserved'
                            or saved.get('run_attempt') != record.get('attempt', 1)
                            or unresolved_calls(record, [node.node_id])):
                        raise ValueError('未启动上下文尝试已变化，不能重复执行')
                    try:
                        ledger.check_attempt(current, saved['id'])
                    except ValueError:
                        # A sibling's reviewed revision may obsolete this
                        # never-started slot. Close it, return its permit and
                        # dispatch the current contract; never reuse stale work.
                        saved.update(status='superseded_closed')
                        lineage_budget.release(current, saved['budget_reservation_id'])
                        replacement = ledger.dispatch(current, node.node_id, run_attempt=record.get('attempt', 1))
                        current['attempts'][replacement['id']]['resumed_from'] = saved['id']
                        ledger.event(current, 'dispatch_revised', previous_attempt=saved['id'], attempt_id=replacement['id'])
                        resumed.append(copy.deepcopy(current['attempts'][replacement['id']]))
                        return
                    saved.pop('context_partition_deferred')
                    resumed.append(copy.deepcopy(saved))
                (await durable_call(self.store.mutate, self.run_id, resume, durable=True, return_snapshot=False))
                attempt = resumed[0]
                if attempt and not resume_unstarted_context:
                    policy = replace(policy, resume_messages=True)
                elif not attempt:
                    host_attempt = (await io_call(self.store.read_execution_fields, self.run_id, "attempt")).get("attempt", 1)
                    attempt = (await durable_call(self.mutate, lambda current: ledger.dispatch(current, node.node_id, run_attempt=host_attempt)))
                if attempt is None:
                    cert = value["certificates"][node.node_id]
                    old = value["attempts"][cert["attempt_id"]]
                    return ExpertRoleResult(**{**old["delivery"], "restored": True})
                policy = replace(policy, allow_delegation=False, max_direct_agents=0,
                                 allow_recursive_delegation=False)
                policy = self.expansion_policy(node, policy, attempt)
                if patch_proposal or self.read('attempts')['attempts'][attempt['id']].get('patch'):
                    if contract.get('workspace_mode'):
                        raise ValueError('隔离工作区通过原节点修订，不混用旧候选发布事务')
                    patch, policy, acceptance = self.patch_context(attempt, policy, patch_proposal)
                if contract.get('workspace_mode'):
                    workspace = await durable_call(self.workspaces.prepare, attempt['id'])
                    policy = self.workspaces.execution_policy(policy, workspace, contract)
                    acceptance = self.workspaces.acceptance(workspace)
                    prompt += self.workspaces.prompt(workspace)
                # 每次真正派发前重建投影：协调过程中合同与规格可能已经更新。
                contract_header = ("\n\n【协作工作合同】下面是宿主固定的规格数据。必须保留全部要求；不得自行改合同、"
                                   "降低要求或更换指定方法。先完成可供下游使用的初稿，保留未确认事实和待改进项。"
                                   "仅缺少必需输入、无法执行或需要新授权时停止；一般质量问题不阻断交接。"
                                   "原范围内的返修直接修改现有产物并做相关小验证，不重复已完成工作。\n"
                                   + FORWARD_PLANNING_RULES + "\nTASK_CONTEXT_JSON\n")
                latest_recovery = ""
                if attempt.get("continued_from"):
                    latest_recovery = ("\n已有进展的分段接续：先读取本节点上次交接和 ReadRunArchive 中的真实回执，"
                        "复用已完成结果，只处理未完成项。旧未授权操作仍被拒绝，不得重放或间接执行；"
                        "具体新操作仍经过当前清单、普通安全审查及剩余预算。交接不等于验收通过。")
                while True:
                    ledger.check_attempt(self.read(), attempt["id"])
                    if unresolved_calls((await io_call(self.store.read_execution_fields, self.run_id, "kernel_calls")), [node.node_id]):
                        (await durable_call(self.request_recovery, scope="contract:" + node.node_id, keys=[node.node_id],
                            reason="仍有结果不明的操作，请先核对宿主回执。", kind="infrastructure"))
                        return ExpertRoleResult(text="仍有结果不明的操作，请先核对宿主回执。", status="blocked", error_kind="effect_unknown")
                    value = self.read()
                    contract = value["contracts"][node.node_id]
                    from mewcode.orchestration.stage_sources import retained_loop_decision
                    control = await retained_loop_decision(value, node.node_id,
                        policy.loop_context, model, attempt['id'])
                    if control is not None:
                        source, result = control
                        def reuse_control(current):
                            original = ledger.check_attempt(current, source['id'])
                            if original != source:
                                raise ValueError('已保存循环判定在重新验收前发生变化')
                            saved = ledger.check_attempt(current, attempt['id'])
                            saved.update(delivery=asdict(result), status='checking',
                                control_delivery_rechecked_from=source['id'],
                                executor_model=copy.deepcopy(source['executor_model']),
                                read_set=copy.deepcopy(source.get('read_set') or []),
                                execution_evidence=copy.deepcopy(source.get('execution_evidence')))
                            ledger.event(current, 'loop_decision_handoff_rechecked',
                                source_attempt_id=source['id'], attempt_id=attempt['id'])
                        await durable_call(self.mutate, reuse_control)
                        break
                    from mewcode.orchestration.parallel_revision import saved_branch_delivery
                    retained = await io_call(saved_branch_delivery, value, node.node_id, acceptance)
                    if retained is not None:
                        # Re-run the real host checks below against this current
                        # contract; reusing bytes is not reusing an old PASS.
                        result = ExpertRoleResult(**retained)
                        def reused(current):
                            source = current['parallel_delivery_rechecks'].pop(node.node_id)
                            current['attempts'][attempt['id']].update(rechecked_from=source['source_attempt_id'],
                                delivery=copy.deepcopy(retained), status='checking')
                            ledger.event(current, 'parallel_delivery_reused', node_id=node.node_id,
                                         source_attempt_id=source['source_attempt_id'], attempt_id=attempt['id'])
                        await durable_call(self.mutate, reused)
                        break
                    from mewcode.experts.stage_context import runtime_prompt
                    dispatch_prompt = runtime_prompt(run_id=self.run_id, flow=self.flow, node_id=node.node_id,
                        model=model, work_dir=policy.work_dir or str(self.acceptance.work_dir),
                        attempt=attempt.get('run_attempt', 1), contract=contract)
                    from mewcode.orchestration.review_revision import refresh_previous_delivery
                    policy = replace(policy, input_revision=ledger.dependencies(value, node.node_id),
                        stage_inputs=refresh_previous_delivery(value, node.node_id, policy.stage_inputs))
                    # 固定 wrapper 先占预算，剩余部分才交给投影；预算缺失或不足一律报错，绝不截断必需内容。
                    budget = model_context_budget(self.pool, model, value['policy'], tools=True)
                    patch_context = patch.execution_context(value) if patch else ""
                    final_scope, final_basis = '', None
                    if node.node_id == self.flow.finalizer_node_id and node.role == 'auditor':
                        scope_keys = [key for key in value['contracts'] if ledger.valid_certificate(value, key) or key == node.node_id]
                        final_basis = self.final_review_basis(value, scope_keys)
                        final_scope = ('\n本节点承担本轮集中复核。核对以下全部检查和各阶段固定产物，'
                            '用 SubmitVerdict 提交结论与证据；问题注明产物所属 node_id，供直接修订。\nFINAL_REVIEW_SCOPE\n'
                            + json.dumps(self.quality_scope(value, scope_keys), ensure_ascii=False))
                    remaining = budget.subtract(prompt + dispatch_prompt + contract_header + latest_recovery + patch_context + final_scope)
                    if remaining.limit <= 0:
                        raise ValueError("协作上下文预算不足以容纳固定合同说明、调用者提示与恢复说明。")
                    # 证据只取当前合同、当前 spec_version 下最新一次被拒交接；缺版本号的交接不算当前。
                    rejected = [item for item in ledger.attempts_for(value, node.node_id)
                                if item.get("status") == "rejected"
                                and not item.get("superseded")
                                and item.get("spec_version") == value.get("spec_version")]
                    # prior_failed_checks already contains the current repair
                    # findings. Keep an archive reference instead of another
                    # copy of the full report, handoff and artifact contents.
                    evidence = [{"attempt_id": rejected[-1]['id'], "source": "ReadStageResult",
                                 "summary": "当前失败项见 objective.prior_failed_checks"}] if rejected else []
                    if contract.get("execution_kind") == "join":
                        # No business-model request is made for a join. Its
                        # full original contract still goes through acceptance.
                        instructions = ""
                    else:
                        try:
                            projection = build_task_context(value, node.node_id, role="executor",
                                                            sources=policy.stage_inputs, evidence=evidence,
                                                            context_budget=remaining, compact=True)
                        except ContextBudgetExceeded:
                            if patch:
                                raise
                            if policy.parallel_readonly or policy.parallel_isolated:
                                # No model/tool start has occurred. Leave the
                                # exact attempt and permit for serial planning
                                # after its concurrent siblings have finished.
                                def defer(current):
                                    saved = ledger.check_attempt(current, attempt['id'])
                                    permit = current['lineage_budget']['reservations'][saved['budget_reservation_id']]
                                    if saved.get('budget_started') or saved.get('delivery') or permit['status'] != 'reserved':
                                        raise ContextBudgetExceeded('已开始的只读尝试上下文超限，保留真实回执等待串行恢复。')
                                    saved['context_partition_deferred'] = True
                                (await durable_call(self.mutate, defer))
                                return ExpertRoleResult(text='完整上下文需要串行规划分工，原尝试与预留已保留。',
                                    status='continuing', error_kind='context_partition_deferred', failed_node_id=node.node_id)
                            from mewcode.orchestration.context_partition import plan_overflow
                            return await plan_overflow(self, node, model, policy, attempt, remaining=remaining)
                        instructions = (prompt + dispatch_prompt + contract_header
                                        + json.dumps(projection, ensure_ascii=False, separators=(",", ":"))
                                        + latest_recovery + patch_context + final_scope)
                        if not budget.fits(instructions):
                            raise ContextBudgetExceeded(budget.exceeded())
                    def start(current):
                        ledger.mark_started(current, attempt['id'])
                        current['attempts'][attempt['id']].update(executor_model=model.as_dict(), final_review_basis=final_basis)
                    (await durable_call(self.mutate, start))
                    from mewcode.experts.review_reports import report_scope
                    policy = replace(policy, handoff_checks=tuple(copy.deepcopy(contract['checks'])),
                                     review_report_scope=report_scope(contract))
                    try:
                        result = (self.join_delivery(node.node_id) if contract.get("execution_kind") == "join"
                                  else await invoke(replace(node, responsibility=contract['goal']), model, instructions, policy, member))
                    except Exception as exc:
                        if isinstance(exc, BranchWaitPaused):
                            raise
                        result = ExpertRoleResult(text=str(exc), status="failed", error_kind="provider_error")
                    from mewcode.orchestration.stage_sources import loop_decision_handoff
                    result = await loop_decision_handoff(result, node.node_id, policy.loop_context)
                    if (node.role == 'auditor' and result.status == 'completed'
                            and (result.verdict or {}).get('verdict') in {'PASS', 'NEEDS_ATTENTION'}
                            and (result.verdict or {}).get('evidence')
                            and not any((result.handoff or {}).get(key) for key in ('artifacts', 'inline_outputs'))):
                        # Read-only auditors deliver their structured verdict.
                        # Persist those actual bytes, not a made-up file or PASS.
                        result = replace(result, handoff={**(result.handoff or {}), 'inline_outputs': [
                            {'name': '审阅结论', 'content': json.dumps(result.verdict, ensure_ascii=False)}]})
                    from mewcode.kernel.read_set import collect_stage_reads
                    completed_record = await io_call(self.store.load, self.run_id)
                    observed_reads = collect_stage_reads(completed_record,
                        node_id=node.node_id, node_attempt=attempt["number"],
                        input_revision=policy.input_revision, sources=policy.stage_inputs or {})
                    # Only host receipts count. Keep them even for a late return,
                    # which remains historical and cannot create a certificate.
                    from mewcode.kernel.execution_evidence import execution_evidence
                    observed_execution = execution_evidence(completed_record, {**attempt, "delivery": asdict(result)})
                    (await durable_call(self.mutate, lambda current: current["attempts"][attempt["id"]].update(
                        read_set=copy.deepcopy(observed_reads), execution_evidence=copy.deepcopy(observed_execution))))
                    from mewcode.kernel.collaboration_faults import scheduled_yield
                    if scheduled_yield(result):
                        from mewcode.kernel.work_continuation import retain_segment, segment_progress
                        (await durable_call(self.store.mutate, self.run_id,
                            lambda record: retain_segment(record, attempt['id'], asdict(result)), durable=True, return_snapshot=False))
                        record = await io_call(self.store.load, self.run_id)
                        return replace(result, work_progress=segment_progress(record, node.node_id))
                    if not execution_fault(result):
                        (await durable_call(self.clear_transient_faults, node.node_id))
                        break
                    def fault(current):
                        saved = ledger.check_attempt(current, attempt["id"])
                        saved.update(delivery=asdict(result), status="dispatched", execution_fault=execution_fault(result))
                    (await durable_call(self.mutate, fault))
                    if policy.parallel_readonly or policy.parallel_isolated:
                        await durable_call(self.mutate, lambda current: ledger.complete(current, attempt['id'], {
                            'passed':False, 'execution_error':execution_fault(result), 'checks':[], 'artifacts':[]}))
                        return result  # Settle this branch before any serial recovery.
                    decision = await self.recover_fault(node, result, policy, identity=attempt["id"] + ":execution")
                    if not decision:
                        return result
                    from mewcode.experts.continuation import decision_prompt
                    latest_recovery = decision_prompt(decision)
                    policy = replace(policy, resume_messages=True)
                    model = ModelRef.from_value(((await io_call(self.store.read_execution_fields, self.run_id, "locked_models")).get("locked_models") or {}).get(node.node_id) or model)
                def returned(current):
                    saved = current["attempts"][attempt["id"]]
                    saved["delivery"] = asdict(result)
                    try:
                        ledger.check_attempt(current, attempt["id"])
                    except ValueError:
                        saved["status"] = "superseded_returned"
                        ledger.event(current, "stale_delivery", attempt_id=attempt["id"])
                        return
                    saved["status"] = "checking" if result.status == "completed" or result.node_expansion else "rejected"
                (await durable_call(self.mutate, returned))
                if result.status == "continuing" and result.error_kind == "internal_work":
                    from mewcode.kernel.work_continuation import retain_checkpoint
                    (await durable_call(self.store.mutate, self.run_id, lambda record: retain_checkpoint(record, attempt["id"]), durable=True, return_snapshot=False))
                ledger.check_attempt(self.read(), attempt["id"])
            if result.node_expansion:
                return await self.publish_expansion(node, model, policy, attempt, result)
            if result.status != "completed":
                return result
            if patch:
                patch.seal()
            if workspace:
                workspace = await durable_call(self.workspaces.seal, attempt['id'])
            from mewcode.experts.gate_dialogue import confirmation_valid
            record = (await io_call(self.store.load, self.run_id))
            context = {"run_id": self.run_id, "node_id": node.node_id, "attempt_id": attempt["id"],
                       "phase": "review" if patch or attempt.get("evidence_recheck_artifacts") is not None else "handoff",
                       "executor_model": model.as_dict(), "policy": policy,
                       "executor_role": node.role, "loop_context": policy.loop_context,
                       "gate_confirmed": confirmation_valid(node, self.flow, record)}
            while True:
                ledger.check_attempt(self.read(), attempt["id"])
                if unresolved_calls((await io_call(self.store.read_execution_fields, self.run_id, "kernel_calls")), [node.node_id]):
                    (await durable_call(self.request_recovery, scope="contract:" + node.node_id, keys=[node.node_id], kind="infrastructure",
                        reason="仍有结果不明的验收操作，请先核对宿主回执。"))
                    return replace(result, status="blocked", error_kind="effect_unknown")
                context["acceptance_round"] = self.read('acceptance_rounds').get("acceptance_rounds", {}).get(attempt["id"], 0)
                report = await acceptance.check(contract, result, context=context)
                if workspace and not await io_call(self.workspaces.verify_sealed, workspace):
                    report.update(passed=False, ready=False, artifacts_unchanged=False,
                                  reason='集成检查期间隔离交付发生变化，不能交接')
                fixed_recheck = self.read('attempts')["attempts"][attempt["id"]].get("evidence_recheck_artifacts")
                if fixed_recheck is not None and report.get("artifacts") != fixed_recheck:
                    report.update(passed=False, ready=False, reason="原交付产物已变化，不能以宿主回执重验替代重新执行")
                if not report.get("execution_error"):
                    (await durable_call(self.clear_transient_faults, node.node_id))
                    break
                fault_result = replace(result, status="needs_attention", error_kind=report["execution_error"],
                    text="验收服务未完成：" + json.dumps(report, ensure_ascii=False))
                host_attempt = (await io_call(self.store.read_execution_fields, self.run_id, "attempt")).get("attempt", 1)
                (await durable_call(self.mutate, lambda current: current["attempts"][attempt["id"]].update(
                    acceptance_fault=copy.deepcopy(report), acceptance_fault_host_attempt=host_attempt)))
                if policy.parallel_readonly or policy.parallel_isolated:
                    return fault_result
                decision = await self.recover_fault(node, fault_result, policy, identity=attempt["id"] + ":acceptance")
                if not decision:
                    return fault_result
                (await durable_call(self.mutate, lambda current: current.setdefault("acceptance_rounds", {}).__setitem__(
                    attempt["id"], context["acceptance_round"] + 1)))
            if patch:
                from mewcode.orchestration.collab_patch import compare_reports
                saved_patch = patch.saved()
                comparison = compare_reports(contract, saved_patch['baseline_report'], report,
                    saved_patch['decision']['verification_refs'])
                report['improvement'] = comparison
                if comparison['publishable']:
                    certificate = await patch.publish(report, result, context)
                else:
                    report.update(passed=False, ready=False)
                    report['reason'] = comparison['reason']
                    patch.reject(report)
                    certificate = (await durable_call(self.mutate, lambda current: ledger.complete(current, attempt['id'], report)))
            else:
                if workspace:
                    report['workspace'] = copy.deepcopy(workspace)
                    result = await io_call(self.workspaces.public_delivery, result, report, workspace)
                    await durable_call(self.mutate, lambda current: current['attempts'][attempt['id']].update(delivery=asdict(result)))
                certificate = (await durable_call(self.mutate, lambda current: ledger.complete(current, attempt["id"], report)))
            if certificate:
                return result
            reasons = [str(c["evidence"].get("error") or c["evidence"].get("reason") or c["id"])
                       for c in report["checks"] if not c["passed"]]
            return replace(result, status="needs_attention", error_kind="collaboration_acceptance",
                           text=result.text + "\n宿主验收未通过：" + (report.get("reason") or "；".join(reasons)))
        except lineage_budget.BudgetExhausted as exc:
            self.request_budget_recovery(node.node_id, exc)
            return ExpertRoleResult(text=str(exc), status="blocked", error_kind="collaboration_exhausted", failed_node_id=node.node_id)
        except ledger.AttemptBudgetExhausted as exc:
            (await durable_call(self.request_recovery, scope="contract:" + node.node_id, keys=[node.node_id], reason=str(exc), kind="attempts_exhausted"))
            return ExpertRoleResult(text=str(exc), status="blocked", error_kind="collaboration_exhausted", failed_node_id=node.node_id)
        except ValueError as exc:
            from mewcode.orchestration.collab_workspaces import WorkspaceConflict
            if isinstance(exc, WorkspaceConflict):
                await durable_call(self.request_recovery, scope='contract:' + node.node_id,
                    keys=[node.node_id], reason=str(exc), kind='infrastructure')
                return ExpertRoleResult(text=str(exc), status='blocked', error_kind='workspace_merge_conflict',
                    failed_node_id=node.node_id, handoff={'workspace': exc.workspace})
            # A rejected candidate must not strand a hold. Keep prepared work
            # only while effect receipts are unresolved; explicit recovery then
            # resumes the same delivery without reexecuting its business tools.
            current_record = (await io_call(self.store.load, self.run_id))
            current = ledger.state(current_record)
            if node.node_id in current["contracts"]:
                if not unresolved_calls(current_record, [node.node_id]):
                    from mewcode.kernel import collaboration_graph as graph
                    for identity, publication in current.get("graph_publications", {}).items():
                        if publication["status"] == "prepared" and publication["fragment"]["parent_id"] == node.node_id:
                            (await durable_call(self.store.mutate, self.run_id, lambda record, key=identity, reason=str(exc):
                                graph.reject(record, key, reason=reason), durable=True, return_snapshot=False))
                            (await durable_call(self.mutate, lambda value, key=publication["attempt_id"]:
                                value["attempts"][key].update(status="rejected")))
                (await durable_call(self.mutate, lambda value, reason=str(exc): ledger.event(value, "execution_checkpoint_error",
                    contract_id=node.node_id, reason=reason)))
            return ExpertRoleResult(text=str(exc), status="blocked", error_kind="collaboration_checkpoint", failed_node_id=node.node_id)
        except BranchWaitPaused as exc:
            # Keep the existing attempt open. Explicit resume reuses its tool
            # receipts and conversation; independent contracts can continue.
            return ExpertRoleResult(text=exc.reason or "此工作等待用户回答，进度已保存。",
                                    status="needs_attention", error_kind=exc.error_kind, failed_node_id=node.node_id)

    async def run(self, host, *, task, profile, strategy, attempt=1, budget=None, supplemental=""):
        """Dispatch approved contracts, not the classic planner/recovery graph.

        Work always enters the existing node_runner (and hence its live
        permission/preflight checks). No model or tool is called on replay.
        Writes remain serial; a blocked contract does not cancel independent
        contracts in already active milestones.
        """
        from mewcode.expert_group import (ExpertProfile, ExpertRoleResult, ExpertRunResult, ExpertRunBudget,
                                         ExpertExecutionPolicy, REPORT_STYLE_CONTRACT, executor_max_iterations)
        from mewcode.experts.flow import edge_condition_matches, normalize_edge_condition
        from mewcode.experts.authorization_ledger import authorization_block
        from mewcode.tools.read_stage_result import ReadStageResult
        from mewcode.kernel.models import ALIASES
        self.host = host
        self.rework_requested = set()
        profile = ExpertProfile.from_value(profile)
        budget = ExpertRunBudget.from_value(budget)
        results, skipped, handled_loops = {}, set(), set()
        review_outcomes = {}
        started = time.monotonic()
        self.flow.validate()
        # Adopt pending user revisions before any further business dispatch.
        for identity, proposal in self.read('proposals')["proposals"].items():
            if proposal["status"] in {"proposed", "needs_attention"}:
                try:
                    await self.coordinator.review_proposal(identity)
                except lineage_budget.BudgetExhausted as exc:
                    keys = [key for key, contract in self.read('contracts')["contracts"].items()
                            if (contract.get("lineage_id") or key) == exc.lineage_id]
                    if not keys:
                        raise
                    self.request_budget_recovery(keys[0], exc)
                except ValueError as exc:
                    (await durable_call(self.mutate, lambda current, reason=str(exc): ledger.event(current, "revision_waiting", proposal_id=identity, reason=reason)))
                    key = next(iter(self.read('contracts')["contracts"]))
                    (await durable_call(self.request_recovery, scope="contract:" + key, keys=[key], reason=str(exc), kind="coordination_wait"))
        from mewcode.orchestration.loops import compile_loops
        _, loops = compile_loops(self.flow.nodes, self.flow.edges, self.flow.meta.get("loops"))
        roots = {loop.entry_node_id: loop for loop in loops if not any(set(loop.node_ids) < set(parent.node_ids) for parent in loops)}
        loop_nodes = {key for loop in loops for key in loop.node_ids}
        self.loop_groups = [set(loop.node_ids) for loop in loops]
        await host._emit({"type": "expert_flow_defined", "run_id": self.run_id, "flow": self.flow.as_dict(),
                          "flow_id": self.flow.flow_id, "flow_version": self.flow.version, "attempt": attempt,
                          "orchestration_mode": "collaborative"})

        def model_for(node):
            record = self.store.read_execution_fields(self.run_id, 'locked_models', 'role_models')
            binding = (record.get("locked_models") or {}).get(node.node_id) or node.model
            if binding is None:
                binding = ((record.get("role_models") or {}).get("roles") or {}).get(ALIASES.get(node.role))
            if binding is None:
                binding = {"executor": profile.executor, "auditor": profile.auditor,
                           "planner": profile.planner or profile.manager}.get(node.role, profile.manager)
            return ModelRef.from_value(binding)

        def policy_for(node):
            return ExpertExecutionPolicy(strategy=strategy, allow_delegation=False, max_direct_agents=0,
                max_iterations=executor_max_iterations() if node.role == "executor" else 50,
                work_dir=str(self.acceptance.work_dir), node_tools=node.tools, deny_tools=node.deny_tools,
                node_epoch=self.store.stage_epoch(self.run_id, node.node_id))

        async def observed_result(node_id, result):
            # Every handoff, including a restored completed one, crosses the
            # same host boundary. Historical denied operations remain denied;
            # they do not become a prerequisite for independent delivered work.
            progress = await io_call(self.store.read_segment_progress, self.run_id, node_id)
            return replace(result, work_progress=progress)

        def control_pass(key):
            result = results[key]
            if result.status != "completed" or not self.accepted(key):
                return False
            if result.loop_decision:
                return result.loop_decision.get("decision") == "EXIT"
            return not result.verdict or result.verdict.get("verdict") == "PASS"

        def readiness(node_id):
            edges = [edge for edge in self.flow.edges if edge.to_node == node_id]
            required_review = set(self.read('contracts')['contracts'][node_id].get('review_dependencies', []))
            if not edges:
                return "ready", []
            if any(edge.from_node not in results and edge.from_node not in skipped for edge in edges):
                return "waiting", []
            if any(key in skipped or not control_pass(key) for key in required_review):
                return "waiting", []
            # Investigation inputs are mandatory additions to the original
            # selected branch, never an alternate branch or a replacement.
            edges = [edge for edge in edges if edge.from_node not in required_review]
            selected = []
            from mewcode.orchestration.run_inputs import predicate_matches
            record = self.store.read_execution_fields(self.run_id, 'run_inputs')
            for edge in edges:
                if edge.from_node in skipped:
                    continue
                result = results[edge.from_node]
                if edge.input_when is not None:
                    values = record.get("run_inputs") or {}
                    if edge.branch_group:
                        choices = {item.input_when["equals"].strip() for item in self.flow.edges
                                   if item.branch_group == edge.branch_group and item.input_when is not None}
                        if values.get(edge.input_when["input"]) not in choices:
                            return "waiting", []
                    if not predicate_matches(edge.input_when, values):
                        continue
                if edge_condition_matches(edge.condition, passed=control_pass(edge.from_node), status=result.status):
                    selected.append(edge)
            always = [edge for edge in edges if normalize_edge_condition(edge.condition) == "always" and edge.from_node not in skipped]
            branches = [edge for edge in selected if normalize_edge_condition(edge.condition) != "always"]
            active = branches or ([edge for edge in selected if edge in always]
                                  if always and all(edge in selected and self.accepted(edge.from_node) for edge in always) else [])
            if not active:
                return ("waiting" if any(results[edge.from_node].status != "completed" for edge in edges if edge.from_node in results)
                        else "skipped"), []
            return "ready", [*[edge.from_node for edge in active], *sorted(required_review)]

        async def execute_one(node, incoming=(), override=None, extra_prompt="", *,
                              defer_recovery=False, prior_result=None):
            current = self.read()
            parents = [row["from_node"] for row in incoming if row.get("from_node") in current["contracts"]]
            contract = current['contracts'][node.node_id]
            if (not (override and override.loop_context) and not contract.get('iteration')
                    and sorted(set(parents)) == sorted(contract.get('bound_dependencies', contract['depends_on']))
                    and self.accepted(node.node_id)):
                saved = current['attempts'][current['certificates'][node.node_id]['attempt_id']]
                # Restoring accepted work is not a new node event. In
                # particular do not move the visible stage back to the entry.
                return await observed_result(node.node_id,
                    ExpertRoleResult(**{**saved['delivery'], 'restored': True}))
            model = model_for(node)
            policy = override or policy_for(node)
            settlements = [row['parallel_group'] for row in incoming if row.get('parallel_group')]
            def bind_inputs(value):
                contract = value["contracts"][node.node_id]
                iteration = None
                if policy.loop_context:
                    iteration = {key: copy.deepcopy(policy.loop_context.get(key)) for key in
                                 ("loop_id", "contract_hash", "round", "current_item", "item_round")}
                if contract.get("iteration") != iteration:
                    ledger.invalidate(value, {node.node_id}, "循环进入新的有界工作范围")
                    contract["iteration"] = iteration
                contract["bound_dependencies"] = sorted(set(parents))
            (await durable_call(self.mutate, bind_inputs))
            current = self.read()
            contract = current["contracts"][node.node_id]
            source_keys = ([key for key in current['certificates'] if ledger.valid_certificate(current, key)]
                           if node.node_id == self.flow.finalizer_node_id and node.role == 'auditor' else parents)
            # A loop can restore completed nodes without invoking loop_execute.
            # Certificates and their exact deliveries own handoff data for both
            # fresh and restored work; the scheduler cache only owns readiness.
            from mewcode.orchestration.stage_sources import accepted_sources
            sources = accepted_sources(current, source_keys)
            from mewcode.orchestration.review_revision import refresh_previous_delivery
            sources = refresh_previous_delivery(current, node.node_id, sources)
            for receipt in settlements:
                group = receipt['group']
                if current['contracts'][node.node_id].get('parallel_group') != group:
                    raise ValueError('并发汇合声明与执行合同不一致')
                sources['@parallel:' + group['group_id']] = {
                    'node_id': '@parallel:' + group['group_id'], 'status': 'settled',
                    'text': json.dumps(receipt, ensure_ascii=False), 'parallel_group': copy.deepcopy(receipt),
                    'instruction': '这是宿主记录的全部分支状态；失败不算通过，融合必须使用全部有效结果并保留缺口。'}
            # 上游记录按明确版本冻结：ReadStageResult 与合同看到同一版本，不得各自猜测摘要。
            for key, revision in ReadStageResult(sources).revisions.items():
                sources[key]["source_revision"] = revision
            policy = replace(policy, stage_inputs=sources, input_revision=ledger.dependencies(current, node.node_id))
            investigation = contract.get('review_intervention')
            if investigation:
                item = current['review_interventions'][investigation['id']]
                sources['@review_intervention'] = {key: copy.deepcopy(item.get(key))
                    for key in ('node_id', 'issues', 'plan', 'review')}
                policy = replace(policy, parallel_readonly=True, stage_inputs=sources)
            base_prompt = (REPORT_STYLE_CONTRACT + "\n" + supplemental + "\n" + extra_prompt
                           + "\n" + authorization_block((await io_call(self.store.load, self.run_id)), node.node_id))
            current = self.read()
            followups = [issue for saved in current.get('review_followups', {}).values()
                         if saved.get('spec_version') == current['spec_version']
                         and saved.get('basis') == self.integration_basis(current, saved['keys'])
                         for issue in saved.get('issues', [])
                         if issue.get('route') == 'downstream' and issue.get('target_node_id') == node.node_id]
            if followups:
                base_prompt += ('\n前序复核为本阶段保留的待办，按当前合同处理；不改变验收条件或权限：'
                                + json.dumps(followups, ensure_ascii=False))
            repair = self.read('rework_instructions').get("rework_instructions", {}).get(node.node_id)
            if repair:
                base_prompt += ("\n复核定向修订（不得修改原合同）：复用已完成结果，只修正下列具体问题。"
                    "不得为补遥测重复有效业务调用；不得改变模型、验收要求或外发授权。"
                    "现有能力无法取得的证据如实保留缺口，不能伪造通过。" + json.dumps(repair, ensure_ascii=False))

            async def invoke(active_node, ref, instructions, active_policy, member):
                current_attempts = [row for row in self.read('attempts')["attempts"].values() if row["contract_id"] == active_node.node_id]
                number = len(current_attempts)
                await host._emit({"type": "expert_node_started", "run_id": self.run_id, "node_id": active_node.node_id,
                                  "role": active_node.role, "label": active_node.label, "status": "running",
                                  "node_attempt": number, "provider": ref.provider_key, "model": ref.model_id,
                                  "started_at": time.time(), "input_revision": active_policy.input_revision})
                if active_node.kind == "pipeline":
                    if host.pipeline_runner is None:
                        return ExpertRoleResult(text="此流程执行服务不可用", status="blocked")
                    return await host.pipeline_runner(active_node, {"task": task, "supplemental": supplemental,
                        "instruction": instructions, "handoffs": list((active_policy.stage_inputs or {}).values()),
                        "inputs": active_policy.stage_inputs or {},
                        "expert": {"run_id": self.run_id, "flow_id": self.flow.flow_id, "flow_version": self.flow.version,
                                   "node_id": active_node.node_id}},
                        ((await io_call(self.store.read_execution_fields, self.run_id, "pipeline_runs")).get("pipeline_runs") or {}).get(active_node.node_id), host._emit)
                if host.node_runner is not None:
                    return await host.node_runner(active_node, ref, instructions, active_policy, member)
                return await host.runner(active_node.role, ref, instructions, active_policy)

            segment_started = time.monotonic()
            current = self.read()
            prior = ledger.attempts_for(current, node.node_id)
            previous = prior[-1] if prior else {}
            report = previous.get("report") or {}
            if prior_result is not None and prior_result.error_kind == 'context_partition_deferred':
                result = await self.execute(node, model, base_prompt, policy, None, invoke,
                                            resume_unstarted_context=True)
            elif prior_result is not None:
                result = prior_result
            else:
                result = await self.execute(node, model, base_prompt, policy, None, invoke)
            continuation_policy = policy
            from mewcode.kernel.collaboration_faults import scheduled_yield
            while not defer_recovery and not policy.loop_context and scheduled_yield(result):
                from mewcode.kernel.work_continuation import segment_checkpoint, remaining_segment_policy
                previous = ledger.attempts_for(self.read(), node.node_id)[-1]
                checkpoint = segment_checkpoint((await io_call(self.store.load, self.run_id)), previous)
                if not checkpoint or not checkpoint['host_observed_progress']:
                    break
                if budget.deadline_seconds is not None and time.monotonic() - started >= budget.deadline_seconds:
                    break
                continuation_policy = remaining_segment_policy(continuation_policy, result, time.monotonic() - segment_started)
                if continuation_policy is None:
                    break
                segment_started = time.monotonic()
                result = await self.execute(node, model, base_prompt,
                    replace(continuation_policy, resume_messages=True), None, invoke)
            while not defer_recovery and result.status == "continuing" and result.error_kind == "internal_work":
                from mewcode.kernel.work_continuation import checkpoint_matches, work_checkpoint, remaining_segment_policy
                previous = ledger.attempts_for(self.read(), node.node_id)[-1]
                if not checkpoint_matches(previous) or work_checkpoint((await io_call(self.store.load, self.run_id)), previous) is None:
                    break
                if budget.deadline_seconds is not None and time.monotonic() - started >= budget.deadline_seconds:
                    break
                continuation_policy = remaining_segment_policy(continuation_policy, result, time.monotonic() - segment_started)
                if continuation_policy is None:
                    break
                # A fresh bounded segment uses the same contract/model and
                # prior handoff/archive. Dispatch charges the existing lineage
                # attempt budget again; productive work creates no new quota.
                segment_started = time.monotonic()
                result = await self.execute(node, model, base_prompt, continuation_policy, None, invoke)
            # Failed checks return to the same unit with evidence. There is no
            # implicit route change or fresh budget; dispatch owns the limit.
            while not defer_recovery and (result.error_kind == "collaboration_acceptance" and result.status == "needs_attention"
                   or (result.handoff or {}).get("blocker_kind") in {"contract_conflict", "plan_revision_required"}):
                failures = ledger.attempts_for(self.read(), node.node_id)
                if ledger.retry_exhausted(self.read(), node.node_id):
                    (await durable_call(self.request_recovery, scope="contract:" + node.node_id, keys=[node.node_id],
                        reason=result.text, kind="attempts_exhausted"))
                    break
                correction = ""
                if result.error_kind == "collaboration_acceptance":
                    # The executor already has a concrete failed prerequisite.
                    # An ordinary repair needs no planning model or candidate
                    # publication protocol. Its tool calls retain normal review.
                    from mewcode.kernel.contracts import blocking_check
                    contract = self.read('contracts')['contracts'][node.node_id]
                    required = {row['id'] for row in contract['checks'] if blocking_check(row)}
                    failed = [row for row in (failures[-1].get('report') or {}).get('checks', [])
                              if row['id'] in required and row.get('passed') is not True]
                    from mewcode.orchestration.collab_workspaces import repair_marker
                    marker = repair_marker(failures[-1], failed)
                    previous_markers = [item.get('repair_marker') for item in failures[:-1]
                                        if item.get('dependencies') == failures[-1].get('dependencies')
                                        and item.get('retry_generation', 0) == failures[-1].get('retry_generation', 0)]
                    (await durable_call(self.mutate, lambda value: value['attempts'][failures[-1]['id']].update(repair_marker=marker)))
                    if marker in previous_markers:
                        from mewcode.orchestration.review_revision import repair_stall_basis
                        record = await io_call(self.store.load, self.run_id)
                        stalled = repair_stall_basis(record, node.node_id, policy)
                        await durable_call(self.mutate, lambda value: value['attempts'][failures[-1]['id']].update(
                            repair_stalled=stalled))
                        result = replace(result, text=result.text + '\n必需条件仍未满足，已保留现有产物和具体修订项。')
                        break
                    correction = ('\n修正当前未满足的交接条件，保留其余已完成结果；只运行相关小验证。'
                        '具体问题见 prior_failed_checks；ReadStageResult(previous:' + node.node_id + ') 是最新被退回的交付。'
                        '一次落实全部 acceptance_test，交付时逐项说明修正位置和证据。'
                        '已有证据只需收窄结论时，直接修订正文，不重复检索或引入新前提；未知仍如实保留。')
                elif self.coordinator.ask is not None:
                    try:
                        decision = await self.coordinator.decide(event_id=failures[-1]["id"], node_id=node.node_id,
                            evidence=failures[-1].get("report") or {"delivery": failures[-1].get("delivery")})
                        if decision["action"] == "revise":
                            changes = decision.get("entry_changes") or {}
                            identity = digest(["revision", failures[-1]["id"], changes])
                            (await durable_call(self.mutate, lambda current: ledger.proposal(current, proposal_id=identity, changes=changes,
                                evidence=decision.get("evidence_refs") or [failures[-1]["id"]], actor="coordinator",
                                base_version=current["spec_version"], epoch=current["coordinator_epoch"])))
                            if not (await self.coordinator.review_proposal(identity)).get("passed"):
                                break
                        elif decision["action"] == "wait" and not decision.get("error"):
                            result = replace(result, text=result.text + "\n" + decision["reason"])
                            (await durable_call(self.request_recovery, scope="contract:" + node.node_id, keys=[node.node_id],
                                kind="no_progress" if decision.get("no_progress") else "user_decision",
                                reason=decision["reason"]))
                            break
                        correction = "\n协调者在原合同内的处理建议：" + decision.get("instructions", "")
                    except lineage_budget.BudgetExhausted as exc:
                        self.request_budget_recovery(node.node_id, exc)
                        result = replace(result, text=result.text + "\n" + str(exc))
                        break
                    except ValueError as exc:
                        result = replace(result, text=result.text + "\n" + str(exc))
                        (await durable_call(self.mutate, lambda value, reason=str(exc): ledger.event(value, 'coordination_error',
                            contract_id=node.node_id, reason=reason)))
                        break
                elif (result.handoff or {}).get("blocker_kind") in {"contract_conflict", "plan_revision_required"}:
                    (await durable_call(self.request_recovery, scope="contract:" + node.node_id, keys=[node.node_id],
                        reason=result.text, kind="coordination_wait"))
                    break
                result = await self.execute(node, model, base_prompt + correction, policy, None, invoke)
            result = await observed_result(node.node_id, result)
            await host._emit({"type": "expert_node_completed", "run_id": self.run_id, "node_id": node.node_id,
                              "role": node.role, "label": node.label, "status": result.status,
                              "text": result.text, "transcript": result.text, "handoff": result.handoff,
                              "verdict": result.verdict, "loop_decision": result.loop_decision,
                              "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
                              "passed": self.accepted(node.node_id), "restored": result.restored,
                              "completed_at": time.time(), "error_kind": result.error_kind})
            return result

        active_milestones, completed_milestones = set(), set()
        from mewcode.orchestration.parallel import flow_groups, optional_failures
        parallel_groups = flow_groups(self.flow)
        def resolved_alternatives():
            return optional_failures(parallel_groups, results, self.accepted)
        def reset_reworked_results():
            affected = set(self.rework_requested)
            for key in affected:
                results.pop(key, None)
                skipped.discard(key)
            for identity, milestone in self.read('milestones')["milestones"].items():
                if set(milestone["contracts"]) & affected:
                    active_milestones.discard(identity)
                    completed_milestones.discard(identity)
                    self.mutate(lambda current, key=identity: current["milestones"][key].update(status="pending"))
            self.rework_requested.clear()

        async def finish_milestones():
            advanced = False
            for identity in list(active_milestones):
                milestone = self.read('milestones')["milestones"][identity]
                optional = resolved_alternatives()
                keys = [key for key in milestone["contracts"] if key not in skipped and key not in optional]
                if not all(key in results and results[key].status == "completed" and self.accepted(key) for key in keys):
                    continue
                last = self.flow.node_map[keys[-1]] if keys else None
                passed = not keys or not milestone['checks'] or await self.integrate("milestone:" + identity, keys, checks=milestone["checks"],
                    model=model_for(last), policy=policy_for(last), results=results)
                if passed:
                    active_milestones.remove(identity)
                    completed_milestones.add(identity)
                    (await durable_call(self.mutate, lambda current: current["milestones"][identity].update(status="accepted")))
                    advanced = True
            return advanced

        while True:
            while True:
                if (self.read('planning_mode').get("planning_mode") == "rolling"
                        and len(active_milestones) < self.read('policy')["policy"]["max_active_milestones"]
                        and await self.expand_future_milestone()):
                    await host._emit({"type": "expert_flow_defined", "run_id": self.run_id,
                        "flow": self.flow.as_dict(), "flow_id": self.flow.flow_id, "flow_version": self.flow.version,
                        "attempt": attempt, "orchestration_mode": "collaborative"})
                pending = [node for node in self.flow.nodes if node.node_id not in results and node.node_id not in skipped
                           and (node.node_id not in loop_nodes or node.node_id in roots)]
                if not pending:
                    break
                advanced = False
                for node in pending:
                    current = self.read('contracts', 'milestones', 'policy')
                    milestone_id = current["contracts"][node.node_id]["milestone_id"]
                    if milestone_id not in active_milestones:
                        if len(active_milestones) >= current["policy"]["max_active_milestones"]:
                            continue
                        dependencies = set(current["milestones"][milestone_id]["depends_on"])
                        if not dependencies <= completed_milestones:
                            continue
                        active_milestones.add(milestone_id)
                        (await durable_call(self.mutate, lambda value: value["milestones"][milestone_id].update(status="active")))
                    if budget.deadline_seconds is not None and time.monotonic() - started >= budget.deadline_seconds:
                        results[node.node_id] = ExpertRoleResult(text="运行时限已达到，进度已保存。", status="budget_exhausted")
                        continue
                    ready, parents = readiness(node.node_id)
                    if ready == "skipped":
                        skipped.add(node.node_id)
                        await host._emit({"type": "expert_node_completed", "run_id": self.run_id, "node_id": node.node_id,
                                          "role": node.role, "label": node.label, "status": "skipped", "reason": "branch_not_taken"})
                        advanced = True
                        continue
                    if ready != "ready":
                        continue
                    incoming = [{"from_node": key, "to_node": node.node_id, "text": results[key].text,
                                 "status": results[key].status, "handoff": results[key].handoff, "condition": "always"}
                                for key in parents]
                    if node.node_id in roots:
                        from mewcode.experts.loop_runtime import run_bounded_loop
                        loop = roots[node.node_id]
                        def prepare_loop(record):
                            value = ledger.state(record)
                            basis = digest({"contracts": {key: ledger.semantic_basis(value, key) for key in loop.node_ids},
                                            "inputs": {key: value["certificates"].get(key) for key in parents}})
                            state = record.setdefault("loop_states", {}).get(loop.loop_id)
                            previous = value.setdefault("loop_bases", {}).get(loop.loop_id)
                            if state and previous and (previous != basis or state.get("status") == "completed"
                                    and any(not ledger.valid_certificate(value, key) for key in state.get("round_results", {})
                                            if key not in optional_failures(parallel_groups, state.get('round_results', {}),
                                                lambda item: ledger.valid_certificate(value, item))
                                            if key in value["contracts"])):
                                from mewcode.experts.loop_recovery import invalidate_results
                                invalidate_results(self.flow, loop, state, [loop.entry_node_id], reason="collaboration_requirements_changed")
                                state.setdefault("recovery_history", []).append({"reason": "batch_requirements_changed",
                                    "item_results": copy.deepcopy(state.get("item_results", {}))})
                                state.update(status="running", reason="", error_kind="", round_open=True,
                                             item_index=0, item_round=0, completed_items=[], item_results={})
                                # Preserve cumulative time, tokens, rounds and attempt
                                # counters. A revision cannot buy another budget.
                            value["loop_bases"][loop.loop_id] = basis
                        (await durable_call(self.store.mutate, self.run_id, prepare_loop, durable=True, return_snapshot=False))
                        async def loop_execute(loop_node, loop_incoming, execution_policy, prompt):
                            result = await execute_one(loop_node, loop_incoming, execution_policy, prompt,
                                defer_recovery=execution_policy.parallel_readonly or execution_policy.parallel_isolated)
                            results[loop_node.node_id] = result
                            return result
                        async def loop_emit(event):
                            if event.get("loop_state") and event.get("loop_state_key"):
                                await durable_call(self.store.record_loop_state, self.run_id, event)
                            await host._emit({"run_id": self.run_id, **event})
                        loop_checkpoint = await io_call(self.store.read_execution_fields, self.run_id,
                            "loop_states", "loop_control_policy")
                        local, result = await run_bounded_loop(contract=loop, flow=self.flow,
                            saved=loop_checkpoint.get("loop_states") or {},
                            run_node=loop_execute, emit=loop_emit, policy_for=policy_for, incoming=incoming,
                            control_policy=loop_checkpoint.get("loop_control_policy"))
                        results.update(local)
                        results[loop.check_node_id] = result
                        handled_loops.add(loop.loop_id)
                        if result.status == "completed":
                            skipped.update(set(loop.node_ids) - set(results))
                    else:
                        # Only first executions of reviewed, independent local
                        # read-only siblings may overlap. Their attempts were
                        # reserved together at publication. Recoveries run after
                        # the batch, serially, so revisions never race a sibling.
                        current = self.read()
                        parent = (current['contracts'][node.node_id].get('expanded_from') or {}).get('contract_id')
                        readonly = {'ReadFile', 'Glob', 'Grep', 'ReadStageResult',
                                    'ReadAuditArtifact', 'InspectGitState', 'InspectVisualEvidence'}
                        siblings = []
                        isolated_batch = current['contracts'][node.node_id].get('workspace_mode') == 'worktree'
                        if isolated_batch:
                            basis = current['contracts'][node.node_id]['depends_on']
                            for candidate in pending:
                                contract = current['contracts'][candidate.node_id]
                                if (candidate.node_id in results or candidate.node_id in skipped
                                        or candidate.node_id in loop_nodes or candidate.kind != 'role'
                                        or candidate.role != 'executor' or candidate.members
                                        or contract.get('workspace_mode') != 'worktree' or contract.get('held')
                                        or contract['depends_on'] != basis
                                        or ledger.attempts_for(current, candidate.node_id)):
                                    continue
                                state, inputs = readiness(candidate.node_id)
                                if state == 'ready':
                                    siblings.append((candidate, inputs))
                        elif parent:
                            for candidate in pending:
                                contract = current['contracts'][candidate.node_id]
                                if (candidate.node_id in results or candidate.node_id in skipped
                                        or candidate.node_id in loop_nodes or candidate.kind != 'role'
                                        or candidate.role != 'executor' or candidate.members
                                        or contract.get('execution_kind') == 'join' or contract.get('held')
                                        or (contract.get('expanded_from') or {}).get('contract_id') != parent
                                        or contract.get('write_paths') != []
                                        or not isinstance(contract.get('tools'), list)
                                        or not set(contract['tools']) <= readonly
                                        or ledger.attempts_for(current, candidate.node_id)):
                                    continue
                                state, inputs = readiness(candidate.node_id)
                                if state == 'ready':
                                    siblings.append((candidate, inputs))
                        if any(candidate.node_id == node.node_id for candidate, _ in siblings) and len(siblings) > 1:
                            siblings = siblings[:2]
                            tasks, batch_inputs = [], {}
                            for candidate, inputs in siblings:
                                batch_inputs[candidate.node_id] = [
                                    {'from_node': key, 'to_node': candidate.node_id, 'text': results[key].text,
                                     'status': results[key].status, 'handoff': results[key].handoff, 'condition': 'always'}
                                    for key in inputs]
                                policy = replace(policy_for(candidate), parallel_readonly=not isolated_batch,
                                                 parallel_isolated=isolated_batch)
                                tasks.append(asyncio.create_task(execute_one(candidate,
                                    batch_inputs[candidate.node_id], policy, defer_recovery=True)))
                            try:
                                outcomes = await asyncio.gather(*tasks)
                            finally:
                                for pending_task in tasks:
                                    if not pending_task.done():
                                        pending_task.cancel()
                                await asyncio.gather(*tasks, return_exceptions=True)
                            for (candidate, _), outcome in zip(siblings, outcomes):
                                results[candidate.node_id] = outcome
                            for candidate, _ in siblings:
                                outcome = results[candidate.node_id]
                                if outcome.status != 'completed':
                                    outcome = await execute_one(candidate,
                                        batch_inputs[candidate.node_id], prior_result=outcome)
                                    if outcome.error_kind == 'collaboration_expanded':
                                        results.pop(candidate.node_id, None)
                                        await host._emit({'type': 'expert_flow_defined', 'run_id': self.run_id,
                                            'flow': self.flow.as_dict(), 'flow_id': self.flow.flow_id,
                                            'flow_version': self.flow.version, 'attempt': attempt,
                                            'orchestration_mode': 'collaborative'})
                                    else:
                                        results[candidate.node_id] = outcome
                            for candidate, _ in siblings:
                                outcome = results.get(candidate.node_id)
                                if outcome and outcome.status == 'completed' and not self.accepted(candidate.node_id):
                                    results.pop(candidate.node_id)
                            advanced = True
                            # Rebuild pending/readiness after the batch rather
                            # than visiting these siblings again in the old list.
                            break
                        executed = await execute_one(node, incoming)
                        if executed.error_kind == "collaboration_expanded":
                            results.pop(node.node_id, None)
                            await host._emit({"type": "expert_flow_defined", "run_id": self.run_id,
                                "flow": self.flow.as_dict(), "flow_id": self.flow.flow_id, "flow_version": self.flow.version,
                                "attempt": attempt, "orchestration_mode": "collaborative"})
                            advanced = True
                            break
                        results[node.node_id] = executed
                        review_outcomes[node.node_id] = await self.repair_intermediate_review(node, executed)
                        if self.rework_requested:
                            await host._emit({'type': 'expert_flow_defined', 'run_id': self.run_id,
                                'flow': self.flow.as_dict(), 'flow_id': self.flow.flow_id,
                                'flow_version': self.flow.version, 'attempt': attempt,
                                'orchestration_mode': 'collaborative'})
                            advanced = True
                            break
                    advanced = True
                advanced = await finish_milestones() or advanced
                if self.rework_requested:
                    reset_reworked_results()
                    advanced = True
                if not advanced:
                    break
            final = results.get(self.flow.finalizer_node_id)
            complete = bool(final and final.status == "completed" and self.accepted(self.flow.finalizer_node_id)
                            and all(key in skipped or key in results and results[key].status == "completed" and self.accepted(key)
                                    for key in self.read('contracts')["contracts"] if key not in loop_nodes))
            complete = complete and len(completed_milestones) == len(self.read('milestones')["milestones"])
            if complete:
                last = self.flow.node_map[self.flow.finalizer_node_id]
                optional = resolved_alternatives()
                keys = [node.node_id for node in self.flow.nodes if node.node_id in results
                        and node.node_id not in skipped and node.node_id not in optional]
                complete = await self.integrate("final", keys, checks=[], model=model_for(last), policy=policy_for(last), results=results)
            if self.rework_requested:
                reset_reworked_results()
                continue
            break
        status = "completed" if complete else "blocked"
        failures = [self.flow.node_map[key].label + '：\n' + result.text
                    for key, result in results.items() if result.status != "completed"]
        if not complete:
            for key, result in results.items():
                verdict = result.verdict or {}
                if result.status == 'completed' and verdict.get('verdict') == 'NEEDS_ATTENTION':
                    details = [issue['description'] for issue in verdict.get('issues', [])
                               if isinstance(issue, dict) and isinstance(issue.get('description'), str)]
                    reason = {'unresolved_owners': '未能将修订项定位到现有生产节点。',
                              'user_decision': '需要用户决定，已保留原产物，不向生产节点重复派发。',
                              'decision_recorded': '用户选择已保存；仍未满足的条件保持未通过，不重复提问或重做成功产物。',
                              'capability_gap': '需要补齐宿主或服务商能力，修改产物不能解决此问题。',
                              'downstream': '已记录后续阶段待办，当前审查结论仍需按本阶段验收条件核对。',
                              'limit': '本轮自动修订次数已达到现有上限。',
                              'unchanged': '自动修订后问题与产物未变化，已停止重复执行。'}.get(review_outcomes.get(key), '')
                    if review_outcomes.get(key) == 'coordination_wait':
                        interventions = [row for row in self.read('review_interventions').get('review_interventions', {}).values()
                                         if row['node_id'] == key]
                        latest = interventions[-1] if interventions else {}
                        reason = ('阻塞协调已保留调查记录。' + str(
                            (latest.get('review') or {}).get('reason') if latest.get('status') == 'rejected'
                            else (latest.get('plan') or {}).get('diagnosis') or '协调未完成，请查看恢复关口的具体原因。'))
                    failures.append(self.flow.node_map[key].label + '未通过：' + reason + '\n'
                                    + ('\n'.join(details) or result.text))
        failures.extend(self.integration_reason(scope, row["report"]) for scope, row in self.read('integrations').get("integrations", {}).items()
                        if row["report"].get("passed") is not True)
        final_report = self.read('integrations').get('integrations', {}).get('final', {}).get('report') or {}
        if complete and final_report.get('passed') is not True:
            summary = "初稿已完成，仍有待修订项。\n" + final.text + '\n' + self.integration_reason('final', final_report)
        else:
            summary = final.text if complete else "当前仍有必需条件未满足，已保留完成的产物。" + ("\n" + "\n".join(failures) if failures else "仍有依赖未就绪。")
        await host._emit({"type": "expert_run_done", "run_id": self.run_id, "status": status,
                          "final_summary": summary, "orchestration_mode": "collaborative"})
        return ExpertRunResult(run_id=self.run_id, status=status, strategy=strategy, manager_summary="",
            planner_summary="", executor_summary="", audit_summary="", final_summary=summary,
            node_summaries={key: result.text for key, result in results.items()})
