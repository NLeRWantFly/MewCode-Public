"""Evidence-based review/repair loop used by routed desktop audit nodes."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from mewcode.experts.audit_protocol import (
    bump_rubric, content_hash, decide_candidate, drop_incompatible_assessment,
    freeze_rubric, set_flow_audit_rubric, validate_assessment, vector,
)
from mewcode.experts.audit_evidence import (
    DuplicateEvidenceLedger,
    bind_evidence_ledger,
    build_repair_progress,
    charge_repair_budget,
    infer_repair_step,
    remaining_repair_budget,
    repair_instructions,
    reset_evidence_ledger,
    reusable_baseline,
    split_delivery_and_scratch,
    stall_without_progress,
)
from mewcode.experts.audit_workspace import AuditWorkspace, ensure_no_partial_promotion, manifest
from mewcode.experts.recovery_classify import (
    BLOCKER_COMMAND,
    CHANGE_METHOD,
    COMMAND_RETRY_EXHAUSTED,
    looks_like_command_error,
    looks_like_provider_failure,
)
from mewcode.experts.recovery_planner import PLANNER_EXHAUSTED, resolve_stall
from mewcode.experts.repair_domain import repair_domain
from mewcode.experts.candidate_verification import environment_info
from mewcode.experts.attempt_memory import summarize_attempt, attempt_prompt
from mewcode.experts.waits import BranchWaitPaused


async def run_quantified_audit(
    *, node: Any, flow: Any, source: Path, archive: Path, saved: dict,
    run_node: Any, emit: Any, policy: Any, max_iterations: int,
    timeout_seconds: float, recovery_handler=None,
) -> Any:
    # Import at execution time: expert_group owns the runner result type.
    from mewcode.expert_group import ExpertRoleResult

    state: dict = {"rubric": {}, "round": 0, "no_progress": 0, "seen_candidates": []}
    state.update(saved or {})
    current = ExpertRoleResult(text="", status="needs_attention")
    transaction: AuditWorkspace | None = None
    repair_ids: list[str] = []
    repair_results: dict[str, Any] = {}
    ledger = DuplicateEvidenceLedger(
        state.get("delivered_evidence"),
        state.get("image_probes"),
    )
    ledger_token = None
    domain = repair_domain(node.responsibility)

    async def event(phase: str, reason: str, **extra: Any) -> None:
        state["delivered_evidence"] = ledger.as_list()
        state["image_probes"] = ledger.probes()
        payload = {
            "type": "expert_flow_state", "phase": phase, "node_id": node.node_id,
            "reason": reason, "audit_protocol": dict(state), **extra,
        }
        if state.get("pending_recovery_revision"):
            payload["pending_recovery_revision"] = state["pending_recovery_revision"]
        if extra.get("activated_rubric"):
            payload["activated_rubric"] = extra["activated_rubric"]
        await emit(payload)

    def apply_method_revision(proposal: dict[str, Any]) -> dict[str, Any]:
        patch = proposal.get("rubric_patch") or []
        new_rubric = bump_rubric(
            state["rubric"], patch, change_kind=CHANGE_METHOD, approved=False,
        )
        drop_incompatible_assessment(state)
        state["rubric"] = new_rubric
        set_flow_audit_rubric(flow, node.node_id, new_rubric["criteria"])
        state["recovery_decision"] = "auto_method"
        state.pop("pending_recovery_revision", None)
        state["no_progress"] = 0
        return new_rubric

    async def stop_for_stall(repaired: Any) -> Any:
        from mewcode.kernel.escalation import decide_and_record
        candidate = repaired or current
        identity = [state.get("round"), state.get("no_progress"), candidate.error_kind]
        if getattr(policy, "recovery_decider", None) is not None:
            choice = policy.recovery_decider(trigger="audit_stall", made_progress=False, identity=identity)
        else:
            record = state.setdefault("controller_record", {"node_states": {node.node_id: {}}, "node_ledgers": {}})
            choice = decide_and_record(record, node.node_id, trigger="audit_stall", identity=str(identity))
        state["controller_decision"] = asdict(choice)
        await event("audit_recovery", choice.reason, controller=asdict(choice))
        if choice.action in {"progress_continue", "self_diagnose"}:
            return None
        if recovery_handler is not None:
            selected = await recovery_handler(node=node, result=candidate, policy=policy,
                allowed=["continue_checkpoint", "diagnose_path"], host_facts={"trigger": "audit_stall"}, account=None)
            if selected and selected.get("action") in {"continue_checkpoint", "diagnose_path"}:
                return None
        return await stop(choice.reason, candidate)

    async def finish(result: Any, reason: str) -> Any:
        state["completed"] = result.status == "completed"
        if state["completed"]:
            state.pop("pending_repair", None)
            state.pop("pending_review", None)
        for node_id in repair_ids:
            repaired = repair_results.get(node_id)
            if repaired is None:
                continue
            # Keep execution errors intact. A completed candidate is only a
            # resumable checkpoint once the audit has accepted it.
            accepted = repaired.status == "completed" and result.status == "completed"
            status = ("completed" if accepted else "needs_attention") if repaired.status == "completed" else repaired.status
            await emit({"type": "expert_node_completed", "node_id": node_id,
                        "role": "executor", "status": status, "passed": accepted,
                        "execution_status": repaired.status,
                        "reason": "audit_accepted" if accepted else "candidate_not_accepted",
                        "transcript": repaired.text})
        await emit({
            "type": "expert_node_completed", "node_id": node.node_id,
            "role": node.role, "label": node.label, "status": result.status,
            "passed": result.status == "completed", "verdict": result.verdict,
            "transcript": result.text,
        })
        await event("audit_accepted" if result.status == "completed" else "audit_stopped",
                    reason, status=result.status)
        return result

    async def stop(reason: str, interrupted: Any = None) -> Any:
        result = interrupted or current
        return await finish(replace(result, status="blocked" if result.status == "blocked" else "needs_attention", text=(
            result.text.rstrip() + "\n\n自动审查已停止：" + reason
        )), reason)

    def review_prompt(contract: dict) -> str:
        return (
            "【冻结验收协议】\n只按以下清单逐项审查，不得增加维度或改变阈值。"
            + ("使用 ReadAuditArtifact 读取 DOCX/PDF/图片证据；只提取文字不等于已验证排版，无法读取或无图像能力时标 UNKNOWN。"
               if domain in {"document", "visual"} else
               "读取相关源文件和执行者的 verification_records；核对测试确实在候选目录运行，退出码和导入路径可追溯。审计者不执行测试，缺日志时标 UNKNOWN。")
            + "每项给出 PASS/PARTIAL/FAIL/UNKNOWN、可核验证据及文件位置。"
            "使用 SubmitVerdict.assessment；rubric_hash、artifact_hash 必须原样引用。"
            "有 baseline 时在同一轮阅读旧稿和新稿，提交 baseline_hash 与完整 baseline_checks。"
            "按旧稿已有证据重新评价，不要把本轮才发现的旧问题称作新稿退化；"
            "旧稿评分与先前不同会停止自动接收，不能隐瞒分歧。"
            "全部检查项通过且 issues 为空才允许 PASS，缺证据必须 UNKNOWN。"
            "每个未通过项必须关联稳定 issue_id、criterion_id；"
            "问题 acceptance_test 必须原样引用该冻结验收项的 acceptance_test；"
            "超出本节点范围的问题记录为 out_of_scope，留给后续阶段。"
            "所有文件只读。复审报告中的文件位置使用项目相对路径。\n"
            + json.dumps(contract, ensure_ascii=False)
        )

    try:
        state["rubric"] = freeze_rubric(node, flow, state.get("rubric"))
        rubric = state["rubric"]
        if node.members:
            return await stop("固定验收需要单个审计者提交同一份检查向量，请移除审计节点的并行成员")
        ensure_no_partial_promotion(archive)
        resume_repair = bool(state.get("pending_repair"))
        resume_review = bool(state.get("pending_review"))
        # Old checkpoints retained the transaction but did not mark the
        # interrupted phase. An unsealed, unaccepted candidate after a repair
        # round is the same resumable state; reviewed/rejected versions aren't.
        if state.get("transaction_dir") and not state.get("completed"):
            retained = Path(state["transaction_dir"])
            if retained.resolve().parent == archive.resolve():
                record = json.loads((retained / "transaction.json").read_text(encoding="utf-8"))
                # A crash can land between sealing the durable transaction and
                # persisting the phase event. Never run repair on a sealed copy.
                resume_review = resume_review or record.get("state") == "reviewing"
                resume_repair = not resume_review and (resume_repair or (
                    record.get("state") in {"active", "unaccepted"} and record.get("after") is None))
        if int(state["round"]) >= node.failure_policy.max_attempts and not (resume_repair or resume_review):
            return await stop("修复轮次预算已耗尽；请处理阻塞后明确重试该节点，普通继续不会重复基线审查")
        decision = str(state.get("recovery_decision") or "")
        if decision in {"auto_method", "supply_info", "approve"}:
            state["no_progress"] = 0
            state.pop("recovery_decision", None)
        elif state.get("pending_recovery_revision") or decision in {"reject", "defer"}:
            return await stop(
                "有待处理的计划修订，保持暂停并保留已完成工作。"
                if state.get("pending_recovery_revision")
                else "修订已拒绝或推迟，保持暂停并保留已完成工作。"
            )
        elif int(state["no_progress"]) >= 2:
            halted = await stop_for_stall(None)
            if halted is not None:
                return halted
            # The shared controller granted the next bounded diagnostic segment.
            # Candidate acceptance and the configured repair budget still apply.
        transaction = (AuditWorkspace.resume(source, archive, state["transaction_dir"], sealed=resume_review)
                       if resume_repair or resume_review else AuditWorkspace(source, archive))
        repair_node = flow.node_map.get(node.failure_policy.target_node_id)
        domain = repair_domain(node.responsibility + "\n" + str(getattr(repair_node, "responsibility", "")), transaction.before)
        state["task_domain"] = domain
        state["verification_environment"] = environment_info(source, transaction.candidate)
        contract = {"rubric": rubric, "artifact_hash": transaction.baseline_hash,
                    "artifact_dir": str(transaction.baseline)}
        review_policy = replace(
            policy, allow_delegation=False, max_direct_agents=0,
            allow_recursive_delegation=False, resume_messages=False,
            max_iterations=max_iterations, timeout_seconds=timeout_seconds,
            work_dir=str(transaction.baseline), audit_contract=contract,
        )
        # A retained assessment is usable after resume() validates the exact
        # source and baseline manifests. Matching hashes skip a fresh baseline
        # model call; the candidate is still reviewed afresh before promotion.
        result_fields = ExpertRoleResult.__dataclass_fields__

        def unpack_result(raw: dict[str, Any] | None, fallback: str) -> ExpertRoleResult:
            payload = dict(raw or {})
            payload.setdefault("text", fallback)
            return ExpertRoleResult(**{key: value for key, value in payload.items() if key in result_fields})

        reused = reusable_baseline(state, transaction.baseline_hash)
        if resume_review:
            baseline_result = reused or state.get("baseline_result")
        elif resume_repair:
            baseline_result = reused
        else:
            baseline_result = None
        if baseline_result:
            current = unpack_result(baseline_result, "已冻结基线审查")
        else:
            await event("audit_baseline", "冻结验收清单，核验当前文件版本")
            current = await run_node(node, review_prompt(contract), review_policy)
        if current.status != "completed":
            return await stop("基线审查未完成")
        accepted = validate_assessment(rubric, current.verdict, transaction.baseline_hash)
        if manifest(source) != transaction.before or manifest(transaction.baseline) != transaction.before:
            return await stop("审查期间原稿或基线发生变化，请重新核验")
        state["accepted"] = accepted
        state["baseline_result"] = {"text": current.text, "verdict": current.verdict}
        state["score"] = sum(vector(rubric, accepted["checks"]).values())
        # UNKNOWN is an unmet acceptance item, not proof that the configured
        # repair path is impossible. Give that path its bounded opportunity to
        # locate evidence/correct unsupported claims. The candidate gate still
        # rejects UNKNOWN; missing evidence must never turn into a silent PASS.
        if any(check["state"] == "UNKNOWN" for check in accepted["checks"]):
            await event("audit_evidence_needed", "证据不足，按已配置的失败分支补证据或修订", target_node_id=node.failure_policy.target_node_id)
        if state["score"] == 0:
            transaction.discard("baseline_passed")
            return await finish(current, "全部固定验收项通过")
        repair = flow.node_map[node.failure_policy.target_node_id]
        if repair.kind != "role" or repair.role != "executor" or repair.members:
            return await stop("当前候选协议仅支持单执行者修复节点，请调整修复节点配置")
        # A longer explicit recovery path must not be shortened by this gate.
        queue = [(repair.node_id, [repair.node_id])]
        seen = set()
        while queue:
            target, path = queue.pop(0)
            if target == node.node_id:
                if len(path) > 2:
                    return await stop("修复路径含中间节点，当前候选协议不允许跳过这些节点")
                break
            if target in seen:
                continue
            seen.add(target)
            queue.extend((edge.to_node, path + [edge.to_node]) for edge in flow.edges
                         if edge.from_node == target and edge.to_node not in seen)
        repair_ids.append(repair.node_id)
        # Resuming an interrupted phase is the SAME repair round, including
        # the last allowed round; it must not consume a second round's budget.
        first_round = max(0, int(state["round"]) - int(resume_repair or resume_review))
        for round_index in range(first_round, node.failure_policy.max_attempts):
            state["round"] = round_index + 1
            state["transaction_dir"] = str(transaction.root)
            ledger_token = None
            if not resume_review:
                iter_left, time_left = remaining_repair_budget(
                    state,
                    max_iterations=max_iterations,
                    timeout_seconds=timeout_seconds,
                    resume=resume_repair,
                )
                if iter_left <= 0 or time_left <= 0:
                    return await stop(
                        "本轮修复预算已耗尽；普通继续不会重新补充。"
                        "已保留已有发现和候选文件。请明确重试该节点以获得新预算。"
                    )
                state["pending_repair"] = True
                ledger = DuplicateEvidenceLedger(
                    state.get("delivered_evidence") if resume_repair else None,
                    state.get("image_probes") if resume_repair else None,
                )
                ledger_token = bind_evidence_ledger(ledger)
                await event("audit_repair", "接力保留的候选稿与执行上下文" if resume_repair else "在候选目录中定向修复",
                            recovery_round=state["round"])
            else:
                iter_left, time_left = max_iterations, timeout_seconds
            repair_policy = replace(
                policy, allow_delegation=False, max_direct_agents=0,
                allow_recursive_delegation=False, resume_messages=False,
                max_iterations=iter_left, timeout_seconds=time_left,
                work_dir=str(transaction.candidate), audit_contract=None,
                candidate_context={"source": str(source), "candidate": str(transaction.candidate), "baseline": str(transaction.baseline)},
            )
            changed = False
            try:
                after_manifest = manifest(transaction.candidate)
                changed = after_manifest != transaction.before
                state["changed_files"] = [
                    path for path in sorted(set(transaction.before) | set(after_manifest))
                    if transaction.before.get(path) != after_manifest.get(path)
                ]
            except (TypeError, ValueError, OSError):
                changed = bool(ledger.as_list() or state.get("changed_files"))
            issues = (current.verdict or {}).get("issues", [])
            progress = state.get("repair_progress") if isinstance(state.get("repair_progress"), dict) else {}
            delivery, scratch = split_delivery_and_scratch(
                [str(item) for item in (state.get("changed_files") or [])],
                accepted=accepted,
                issues=issues,
            )
            step = infer_repair_step(
                state, changed=changed,
                delivery_changed=bool(progress.get("edited_delivery") or delivery),
            )
            state["repair_step"] = step
            state["delivered_evidence"] = ledger.as_list()
            instructions = repair_instructions(
                candidate_dir=str(transaction.candidate),
                original_dir=str(source),
                rubric=rubric,
                accepted=accepted,
                issues=issues,
                step=step,
                delivered_evidence=ledger.as_list(),
                previous_attempt=state.get("last_decision", {}),
                max_iterations=iter_left,
                changed_files=[str(item) for item in (state.get("changed_files") or [])],
                notes=str(progress.get("text") or (state.get("repair_result") or {}).get("text") or ""),
                resume=resume_repair,
                delivery_files=list(progress.get("delivery_files") or delivery),
                scratch_files=list(progress.get("scratch_files") or scratch),
                located_files=list(progress.get("located_files") or []),
                remaining=str(progress.get("remaining") or ""),
                edited_delivery=list(progress.get("edited_delivery") or []),
                domain=domain,
                verification_environment=environment_info(source, transaction.candidate) if domain == "code" else None,
            )
            if state.get("repair_result"):
                instructions += attempt_prompt([summarize_attempt(state["repair_result"])], attempt=state["round"])
            repaired = None
            repair_started = time.monotonic()
            try:
                repaired = (
                    unpack_result(state.get("repair_result"), "已固定候选稿，修复报告未留存；请直接核验文件证据")
                    if resume_review else await run_node(repair, instructions, repair_policy)
                )
            finally:
                state["delivered_evidence"] = ledger.as_list()
                state["image_probes"] = ledger.probes()
                if not resume_review:
                    charge_repair_budget(
                        state,
                        elapsed_seconds=time.monotonic() - repair_started,
                        iterations_used=int(getattr(repaired, "iterations_used", 0) or 0) if repaired else 0,
                        exhausted=bool(repaired is not None and repaired.status == "budget_exhausted"),
                    )
                if ledger_token is not None:
                    reset_evidence_ledger(ledger_token)
                    ledger_token = None
            repair_results[repair.node_id] = repaired
            if repaired is not None:
                state["repair_result"] = asdict(repaired)
            changed_after = changed
            try:
                after_manifest = manifest(transaction.candidate)
                changed_after = after_manifest != transaction.before
                state["changed_files"] = [
                    path for path in sorted(set(transaction.before) | set(after_manifest))
                    if transaction.before.get(path) != after_manifest.get(path)
                ]
            except (TypeError, ValueError, OSError):
                changed_after = changed or bool(ledger.as_list() or state.get("changed_files"))
            state["repair_progress"] = build_repair_progress(
                repaired=repaired,
                changed_files=state.get("changed_files") or [],
                accepted=accepted,
                issues=issues,
                previous=progress,
            )
            state["delivery_files"] = state["repair_progress"]["delivery_files"]
            state["scratch_files"] = state["repair_progress"]["scratch_files"]
            if repaired.status != "completed":
                state["repair_step"] = infer_repair_step(
                    state, changed=changed_after,
                    delivery_changed=bool(state["repair_progress"].get("edited_delivery")),
                )
                state["no_progress"] = int(state.get("no_progress") or 0) + 1
                iter_left, time_left = remaining_repair_budget(state,
                    max_iterations=max_iterations, timeout_seconds=timeout_seconds, resume=True)
                if iter_left > 0 and time_left > 0 and state["round"] < node.failure_policy.max_attempts:
                    halted = await stop_for_stall(repaired)
                    if halted is not None:
                        return halted
                    resume_repair = True
                    continue
                return await stop("候选修复预算已耗尽，原稿未合入候选修改", repaired)
            state["repair_step"] = "verify"
            state["repair_result"] = asdict(repaired)
            candidate_hash = content_hash(transaction.after) if resume_review else transaction.seal()
            state.pop("pending_repair", None)
            state["pending_review"] = True
            resume_repair = False
            if not resume_review and candidate_hash in state["seen_candidates"]:
                state.pop("pending_review", None)
                transaction.discard()
                return await stop("重复提交同一候选版本，停止无效重试")
            if candidate_hash not in state["seen_candidates"]:
                state["seen_candidates"].append(candidate_hash)
            contract = {
                "rubric": rubric, "artifact_hash": candidate_hash,
                "artifact_dir": str(transaction.candidate),
                "baseline_hash": transaction.baseline_hash,
                "baseline_dir": str(transaction.baseline), "previous_assessment": accepted,
                "repair_evidence": repaired.text,
                "verification_records": repaired.verification_records,
            }
            await event("audit_comparing", "按同一验收清单配对复审旧稿与候选稿")
            reviewed = await run_node(node, review_prompt(contract), replace(
                review_policy, work_dir=str(transaction.candidate), audit_contract=contract,
            ))
            if reviewed.status != "completed":
                return await stop("配对复审未完成，候选稿已保留；继续时只接续复审", reviewed)
            report = validate_assessment(rubric, reviewed.verdict, candidate_hash, transaction.baseline_hash)
            decision = decide_candidate(rubric, accepted, report)
            state["last_decision"] = asdict(decision)
            state.pop("pending_review", None)
            resume_review = False
            await event("audit_decision", decision.reason, decision=asdict(decision))
            if decision.outcome in {"improved", "complete"}:
                changed = transaction.promote()
                current = reviewed
                accepted = report
                state.update(accepted=accepted, score=decision.after, no_progress=0,
                             changed_files=changed)
                state["baseline_result"] = {"text": current.text, "verdict": current.verdict}
                await event("audit_promoted", "候选稿已通过检查并合入", decision=asdict(decision))
                if decision.outcome == "complete":
                    return await finish(current, "全部固定验收项通过")
            else:
                transaction.discard()
                if decision.outcome != "plateau":
                    return await stop(decision.reason)
                state["no_progress"] += 1
                await event("audit_rejected", decision.reason, decision=asdict(decision))
                if state["no_progress"] >= 2:
                    halted = await stop_for_stall(current)
                    if halted is not None:
                        return halted
            if state["round"] < node.failure_policy.max_attempts:
                transaction = AuditWorkspace(source, archive)
                if transaction.baseline_hash != accepted["artifact_hash"]:
                    return await stop("两轮修复之间原稿发生变化，需重新核验")
        return await stop("修复轮次预算已耗尽，仍有未通过的验收项")
    except BranchWaitPaused:
        await event("audit_paused", "该审查分支等待业务答复，候选稿已保留", status="paused")
        return replace(current, status="paused", error_kind="branch_wait")
    except asyncio.CancelledError:
        state["delivered_evidence"] = ledger.as_list()
        state["image_probes"] = ledger.probes()
        await event("audit_paused", "审查已暂停，候选版本与原稿均保留", status="paused")
        raise
    except (ValueError, OSError, KeyError) as exc:
        return await stop(str(exc))
    finally:
        state["delivered_evidence"] = ledger.as_list()
        state["image_probes"] = ledger.probes()
        if ledger_token is not None:
            reset_evidence_ledger(ledger_token)
        if transaction is not None and transaction.state in {"active", "preparing", "reviewing", "unaccepted"}:
            transaction.discard("unaccepted")
