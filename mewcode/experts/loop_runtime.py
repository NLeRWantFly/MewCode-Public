"""Execute structured loop regions without replaying unrelated DAG progress."""
from __future__ import annotations

import asyncio
import copy
import json
import time
import uuid
from dataclasses import asdict, replace

from mewcode.experts.flow import edge_condition_matches, normalize_edge_condition
from mewcode.orchestration.loops import LOOP_PROMPT, TEAM_LOOP_PROMPT, LoopWatchdog, compile_loops
from mewcode.experts.loop_recovery import affected_nodes, unfinished_roots, invalidate_results, invalidate_nested_inputs, reusable_result
from mewcode.experts.delivery import handoff_error_kind, recovery_action


async def run_bounded_loop(*, contract, flow, saved, run_node, emit, policy_for, incoming, control_policy=None,
                           recover_node=None):
    # Imported lazily: ExpertOrchestrator calls this module.
    from mewcode.expert_group import ExpertRoleResult

    _, contracts = compile_loops(flow.nodes, flow.edges, flow.meta.get("loops"))
    states = saved
    meter_clocks = {}
    result_fields = ExpertRoleResult.__dataclass_fields__

    def unpack(raw):
        return ExpertRoleResult(**{key: value for key, value in raw.items() if key in result_fields})

    def packed(result):
        return asdict(result)

    def passed(result):
        if result.status != "completed":
            return False
        if result.loop_decision:
            return result.loop_decision.get("decision") == "EXIT"
        if result.verdict:
            return result.verdict.get("verdict") == "PASS"
        return True

    async def notify(watchdog, key, *, durable=False):
        await emit({"type": "expert_flow_state", "phase": "loop", "loop_id": watchdog.contract.loop_id,
                    "durable_checkpoint": durable,
                    "loop_state_key": key, "node_id": (watchdog.state.get("active_node") or watchdog.state.get("next_node_id")
                                                        or watchdog.state.get("failed_node_id") or watchdog.contract.check_node_id),
                    "loop_state": copy.deepcopy(watchdog.state), "loop_contract": watchdog.contract.as_dict()})

    async def run(loop, key, external, ancestors=()):
        try:
            saved_state = dict(states.get(key) or {})
            if control_policy is not None:
                saved_state["loop_control_policy"] = {**control_policy, "contract_hash": loop.contract_hash}
            watchdog = LoopWatchdog(loop, saved_state)
        except ValueError as exc:
            return {}, ExpertRoleResult(text=str(exc), status="needs_attention")
        state = watchdog.state
        state['contract_snapshot'] = loop.as_dict()
        if loop.mode == "foreach":
            state.setdefault("execution_method", {"kind": "host_work_queue",
                "reason": "finite_declared_items", "parallelism": 1,
                "contract_hash": loop.contract_hash})
        states[key] = state
        chain = (*ancestors, (watchdog, key))
        body = set(loop.node_ids)
        directed_repair = loop.repair_dispatch == "after_decision"
        from mewcode.orchestration.parallel import parse_groups
        groups = parse_groups(loop.parallel_groups)
        branches = {node_id: group for group in groups for node_id in group.node_ids}
        joins = {group.join_node_id: group for group in groups}
        descendants = [item for item in contracts if set(item.node_ids) < body]
        children = [item for item in descendants if not any(
            set(item.node_ids) < set(parent.node_ids) < body for parent in descendants
        )]
        child_entries = {item.entry_node_id: item for item in children}
        nested_ids = {node_id for item in children for node_id in item.node_ids}

        async def stop(reason, local=None, failure=None, decision_checked=False):
            kind = (failure.error_kind if failure else state.get("error_kind")) or watchdog.stop_kind(reason)
            question = ((failure.handoff or {}).get("question") if failure else None)
            if question and question not in reason:
                reason += "\n需要你回答：" + str(question)
            failed_node = (failure.failed_node_id if failure else state.get("failed_node_id")) or state.get("next_node_id") or loop.check_node_id
            if watchdog.team_mode and not failure and not state.get("error_kind"):
                kind = "no_progress" if state.get("stalled", 0) >= loop.max_stalled_rounds else "awaiting_input"
            # Persist before the interaction can suspend/cancel this coroutine.
            state.update(status="blocked", reason=reason, active_node="", active_nodes=[], active_at=None,
                         error_kind=kind, failed_node_id=failed_node)
            if kind == "provider_interrupted":
                state["interrupted_node"] = failed_node
            await notify(watchdog, key)
            if recover_node is not None and not decision_checked:
                # Total-budget/contract/watchdog boundaries are still owned by
                # the host. Explain and wait durably; never mint another round.
                await recover_node(node=flow.node_map[failed_node],
                    result=failure or ExpertRoleResult(text=reason, status="needs_attention", error_kind=kind),
                    policy=policy_for(flow.node_map[failed_node]), allowed=[],
                    host_facts={"trigger": kind, "loop_id": loop.loop_id, "hard_boundary": reason,
                        "token_window_receipt": (state.get("token_window") or {}).get("last_receipt")},
                    account=None)
            action = (recovery_action(kind) if watchdog.team_mode else
                      "可明确继续中断节点，已完成节点和用量保留。" if kind == "provider_interrupted" else "需处理对应阻塞原因；未通过验收。")
            result = ExpertRoleResult(text=f"协作 {loop.loop_id} 等待处理：{reason}。{action}",
                                      status="needs_attention", error_kind=kind, failed_node_id=failed_node)
            await emit({"type": "expert_stage", "node_id": failed_node,
                        "role": flow.node_map[failed_node].role,
                        "label": flow.node_map[failed_node].label,
                        "status": "needs_attention", "text": result.text,
                        "reason": kind,
                        "loop_id": loop.loop_id, "loop_round": state["round"]})
            return local or {}, result

        if state.get("status") == "blocked":
            return await stop(state.get("reason") or "等待处理上次看门狗停止原因")
        if state.get("status") == "completed":
            local = {node_id: unpack(value) for node_id, value in state.get("round_results", {}).items()}
            if loop.check_node_id not in local:
                return await stop("完成检查点缺少判定结果，不自动重跑")
            return local, replace(local[loop.check_node_id], restored=True)

        # A process crash has no finally block. Charge the uncertain in-flight
        # interval conservatively; a resume never gets a fresh time budget.
        if state.get("active_at"):
            state["elapsed"] += max(0, time.time() - state["active_at"])
            state["active_at"] = None
            state["active_nodes"] = []

        while True:
            if not state.get("round_open"):
                reason = watchdog.remaining()
                if reason:
                    return await stop(reason)
                state.update(round=state["round"] + 1, item_round=state.get("item_round", 0) + 1,
                             round_open=True, round_results=state.pop("carry_results", {}), status="running",
                             protocol_corrections={})
                await notify(watchdog, key)
            local = {node_id: unpack(value) for node_id, value in state.get("round_results", {}).items()}
            # Older checkpoints stored unfinished outputs in round_results.
            # Never feed them downstream or treat their presence as success.
            roots = unfinished_roots(flow, loop, state)
            if roots:
                for root in roots:
                    old = local[root]
                    from mewcode.experts.recovery_state import HARD_KINDS
                    if old.error_kind in HARD_KINDS or handoff_error_kind(old.handoff) in HARD_KINDS or (old.handoff or {}).get("blockers"):
                        return await stop(old.text or "检查点仍有未决阻塞", local,
                                          replace(old, failed_node_id=root))
                affected, _ = invalidate_results(flow, loop, state, roots, reason="unfinished_checkpoint")
                invalidate_nested_inputs(flow, {item.loop_id: item for item in contracts}, states, key, affected, roots)
                state["interrupted_nodes"] = sorted(set(state.get("interrupted_nodes") or []) | set(roots))
                local = {node_id: unpack(value) for node_id, value in state["round_results"].items()}
                await notify(watchdog, key)
            skipped = set()
            async def halt(reason, local=None, failure=None, decision_checked=False):
                return (reason, local, failure, decision_checked)

            async def execute_stage(node_id, group=None, lease_tokens=None):
                stage_recover = None if group else recover_node
                child_state = states.get(f"{key}/{state['round']}/{child_entries[node_id].loop_id}") if node_id in child_entries else None
                child_pending = child_state is not None and child_state.get("status") != "completed"
                if node_id not in body or (node_id in local and not child_pending):
                    return None
                if node_id in nested_ids and node_id not in child_entries:
                    return None
                if (directed_repair and node_id in loop.repair_node_ids
                        and node_id not in state.get("dispatch_roots", [])):
                    skipped.add(node_id)
                    return None
                node = flow.node_map[node_id]
                edges = [edge for edge in flow.edges if edge.to_node == node_id and edge.from_node in body]
                def matches(edge):
                    source = local.get(edge.from_node)
                    if source is None:
                        return False
                    if source.status == "review_pending":
                        # This is a dispatch edge, never an acceptance PASS.
                        return (reusable_result(edge.from_node, packed(source), loop)
                            and (source.verification_request or {}).get("reviewer_node_id") == node_id
                            and normalize_edge_condition(edge.condition) in {"always", "pass"})
                    return edge_condition_matches(edge.condition, passed=passed(source), status=source.status)
                join_group = joins.get(node_id)
                joined = None
                if join_group:
                    from mewcode.orchestration.parallel import settlement
                    joined = settlement(join_group, local)
                    if not joined['settled'] or len(joined['usable_node_ids']) < join_group.min_success:
                        return await halt("并发组没有足够的有效成果供审计融合", local,
                            ExpertRoleResult(text="各分支状态已保留，需要补齐有效成果", status="needs_attention",
                                             error_kind="parallel_quorum_missing", failed_node_id=node_id))
                    edges = [edge for edge in edges if edge.from_node not in join_group.node_ids
                             or edge.from_node in joined['usable_node_ids']]
                active = [edge for edge in edges if matches(edge)]
                always = [edge for edge in edges if normalize_edge_condition(edge.condition) == "always"]
                branches = [edge for edge in edges if normalize_edge_condition(edge.condition) != "always"]
                ready = (not edges or bool(active)) and all(
                    edge.from_node in skipped or edge in active for edge in always
                ) and (not branches or any(edge in active for edge in branches))
                if not ready:
                    skipped.add(node_id)
                    if node_id in child_entries:
                        skipped.update(child_entries[node_id].node_ids)
                    return None
                node_incoming = [{"from_node": edge.from_node, "to_node": node_id,
                                  "label": flow.node_map[edge.from_node].label,
                                  "text": local[edge.from_node].text, "status": local[edge.from_node].status,
                                  "handoff": local[edge.from_node].handoff}
                                 for edge in active]
                if node_id == loop.entry_node_id:
                    node_incoming += external
                if joined:
                    node_incoming.append({"from_node": "@parallel:" + join_group.group_id,
                        "to_node": node_id, "parallel_group": joined, "status": "settled",
                        "text": json.dumps(joined, ensure_ascii=False), "handoff": None})
                if node_id in child_entries:
                    child = child_entries[node_id]
                    nested, result = await run(child, f"{key}/{state['round']}/{child.loop_id}", node_incoming, chain)
                    local.update(nested)
                    state["round_results"].update({nid: packed(value) for nid, value in nested.items()})
                    await notify(watchdog, key)
                    if result.status != "completed":
                        return await halt(result.text, local, result)
                    return None

                token_limits = [w.token_limit - w.state["tokens"] for w, _ in chain if w.token_limit is not None]
                tokens_left = min(token_limits) if token_limits else None
                if lease_tokens is not None:
                    tokens_left = min(tokens_left, lease_tokens) if tokens_left is not None else lease_tokens
                time_limits = [w.time_limit - w.state["elapsed"] for w, _ in chain if w.time_limit is not None]
                base_seconds = policy_for(node).timeout_seconds or 900
                seconds_left = min(time_limits) if time_limits else base_seconds
                state["next_node_id"] = node_id
                if tokens_left is not None and tokens_left < 1024:
                    return await halt("达到循环 Token 预算", local)
                if seconds_left <= 0:
                    return await halt("达到循环时间上限", local)
                reviewer = node_id in loop.reviewer_node_ids
                context = {**loop.as_dict(), "contract_hash": loop.contract_hash,
                           "round": state["round"], "reviewer": reviewer,
                           "previous_scores": state.get("scores"), "acting_node_id": node_id}
                if directed_repair:
                    context["dispatch_instruction"] = (
                        "首次不执行返修。全部复核完成后，由 check_node_id 的 CONTINUE 明确给出 repair_node_ids："
                        "返修只选合同声明的返修节点，下一项首次处理只选 entry_node_id，两者不能混选。"
                        "宿主仅重跑选中根及其下游复核/判定；UNKNOWN、BLOCKED 和预算门禁不变。")
                context["effective_token_budget"] = watchdog.token_limit
                context["effective_control_mode"] = "team" if watchdog.team_mode else "strict"
                if watchdog.team_mode:
                    context["control_mode"] = "team"
                    for advisory in ("token_budget", "max_iterations", "timeout_seconds"):
                        context.pop(advisory, None)
                if watchdog.token_limit is None:
                    context["token_budget_authorization"] = "用户授权本循环不限 Token；累计计量、父循环限制、时间和验收条件仍有效"
                if loop.mode == "foreach":
                    context.update(current_item=loop.items[state.get("item_index", 0)],
                                   item_round=state["item_round"], completed_items=state.get("completed_items", []))
                previous_reports = state.get("reports") or {}
                from mewcode.experts.segment_context import project_loop_context
                try:
                    prompt_context, context_receipt = project_loop_context(context)
                except ValueError as exc:
                    return await halt(str(exc), local, ExpertRoleResult(
                        text=str(exc), status="needs_attention", error_kind="context_limit",
                        failed_node_id=node_id))
                state.setdefault("segment_context_receipts", {})[node_id] = context_receipt
                prompt = (TEAM_LOOP_PROMPT if watchdog.team_mode else LOOP_PROMPT) + "\n当前任务及验收条件：\n" + json.dumps(prompt_context, ensure_ascii=False)
                prompt += ("\n【节点边界】本节点内部未完成的已授权工作继续分批处理；不要把内部待办伪装成外部依赖。"
                           "冻结合同明确交由后续独立用户 gate 的签认，由该 gate 执行，不提前阻塞当前技术工作；"
                           "这不构成用户已确认，不得省略该 gate，也不得忽略本节点真正的前置授权或验收证据。")
                prompt += ("\n【能力不足上报】若当前做法因单次上下文、输出窗口、分段时间或推理能力不足而无法按原标准继续，"
                           "先说明实际信号、来源/工作游标、现有上限及所需的最小执行能力。原授权内可无损分批的工作继续做，"
                           "不要把它伪装成用户阻塞；确需提高累计硬上限、切未声明模型或用户签认时，"
                           "用 SubmitHandoff 的 needs_attention + user_input + 具体 question 向上汇报。"
                           "上报不构成授权，模型自述进展不算宿主 ACK，不得降低验收标准或把截断结果报完成。")
                if joined:
                    from mewcode.orchestration.parallel import FUSION_PROMPT
                    prompt += "\n" + FUSION_PROMPT
                if state.get("recovery_note"):
                    prompt += "\n当前恢复状态（优先于旧交接中的运行状态）：" + state["recovery_note"]
                if previous_reports:
                    prompt += "\n上轮逐项证据（仅处理未通过项）：\n" + json.dumps(previous_reports, ensure_ascii=False)
                if reviewer:
                    prompt += "\n本节点使用 SubmitLoopDecision，不使用 SubmitVerdict。只读核验，不改写被核验产物。"
                if group:
                    from mewcode.orchestration.parallel import branch_policy
                    prompt += "\n并发独立分支：只处理共同输入，不预读其他分支答案；只写本节点声明的路径，完整保存每页结果与实际请求身份。"
                    prompt += ("\n批量处理：先读一次页清单和本路检查点，复用已成功且版本一致的页。"
                        "用 ReadAuditArtifact.file_paths 每次读取2～4张原图（合计≤8MiB），逐页完整转写后批量落盘；"
                        "仅对影响正文、数字、公式、表格或关键结论的疑点补读局部。原图已足够明确时不反复裁剪；"
                        "装饰、水印、模糊批注保留原图引用与未知状态，交审计定向核验，不猜填也不拖住整批。"
                        "不重复已完成的授权查询、技能发现或环境准备；实际权限变化仍按原入口处理。")
                last_usage, last_clock = 0, time.monotonic()
                branch_spent = 0

                async def meter(total):
                    nonlocal last_usage, last_clock, branch_spent
                    delta = max(0, int(total) - last_usage)
                    now = time.monotonic()
                    branch_spent += delta
                    for w, state_key in chain:
                        w.state["tokens"] += delta
                        w.state["elapsed"] += max(0, now - meter_clocks.get(state_key, last_clock))
                        meter_clocks[state_key] = now
                        w.state["active_at"] = time.time()
                        await notify(w, state_key)
                    last_usage = max(last_usage, int(total))
                    last_clock = now

                interrupted = set(state.get("interrupted_nodes") or [])
                resume = node_id in interrupted or state.get("interrupted_node") == node_id or state.get("active_node") == node_id
                for w, state_key in chain:
                    if not w.state.get("active_nodes"):
                        meter_clocks[state_key] = time.monotonic()
                    w.state["active_nodes"] = sorted(set(w.state.get("active_nodes") or []) | {node_id})
                    w.state.update(active_node=w.state["active_nodes"][0], active_at=time.time())
                    await notify(w, state_key)
                execution_policy = replace(policy_for(node), allow_delegation=False, max_direct_agents=0,
                                           allow_recursive_delegation=False,
                                           max_iterations=loop.reviewer_max_iterations if reviewer else loop.node_max_iterations,
                                           timeout_seconds=seconds_left, resume_messages=resume,
                                           loop_context=context, token_budget=tokens_left, usage_callback=meter)
                if group:
                    execution_policy = branch_policy(execution_policy, node, group)
                decision_checked = False
                segment, allowance = -1, 64 if watchdog.team_mode else (3 if stage_recover else 1)
                pending_diagnostic_slice = False
                while allowance:
                    segment += 1
                    allowance -= 1
                    result = None
                    # The token bucket is a pacing lease, not a second
                    # authority boundary. Actual usage is settled after
                    # each slice; an over-burst slice creates debt here.
                    while True:
                        pace_at = time.time()
                        delays = [(w, state_key, w.pace_token_bucket(now=pace_at))
                                  for w, state_key in chain]
                        wait_seconds = max((delay for _, _, delay in delays), default=0.0)
                        if wait_seconds <= 0:
                            for w, state_key, _ in delays:
                                if w.state.pop("token_pace_pending", None) is not None:
                                    await notify(w, state_key, durable=True)
                            break
                        for w, state_key, delay in delays:
                            if delay <= 0:
                                continue
                            if not w.state.get("token_pace_pending"):
                                w.state["token_pace_pending"] = {
                                    "queued_at": pace_at,
                                    "not_before": pace_at + delay,
                                    "rate_per_minute": (w.state.get("token_window") or {}).get(
                                        "token_bucket", {}).get("rate_per_minute"),
                                    "reason": "measured_token_debt",
                                }
                            await notify(w, state_key, durable=True)
                        remaining_time = [w.time_limit - w.state["elapsed"] for w, _ in chain
                                          if w.time_limit is not None]
                        if remaining_time and min(remaining_time) <= 0:
                            return await halt("达到循环时间上限", local)
                        await asyncio.sleep(min(wait_seconds, 30.0,
                                                min(remaining_time) if remaining_time else 30.0))
                        await meter(0)
                    from mewcode.experts.adaptive_segment_budget import choose_segment_budget
                    segment_time_limits = [w.time_limit - w.state["elapsed"] for w, _ in chain
                                           if w.time_limit is not None]
                    if segment_time_limits and min(segment_time_limits) <= 0:
                        return await halt("达到循环时间上限", local)
                    budget = choose_segment_budget(
                        base_iterations=loop.reviewer_max_iterations if reviewer else loop.node_max_iterations,
                        base_seconds=base_seconds,
                        remaining_total_seconds=min(segment_time_limits) if segment_time_limits else None,
                        history=(state.get("segment_budget_history") or {}).get(node_id) or [],
                    )
                    if state.setdefault("pending_reduced_profiles", {}).pop(node_id, False):
                        from mewcode.experts.adaptive_segment_budget import reduce_execution_slice
                        budget = reduce_execution_slice(budget)
                    if pending_diagnostic_slice:
                        budget = {**budget, "max_iterations": min(budget["max_iterations"], 6),
                                  "reason": "bounded_diagnostic_slice"}
                        pending_diagnostic_slice = False
                    state.setdefault("segment_budget_receipts", {}).setdefault(node_id, []).append(budget)
                    state["segment_budget_receipts"][node_id] = state["segment_budget_receipts"][node_id][-64:]
                    execution_policy = replace(execution_policy,
                        max_iterations=budget["max_iterations"],
                        timeout_seconds=budget["timeout_seconds"],
                        soft_grace_seconds=budget["soft_grace_seconds"])
                    if lease_tokens is not None:
                        remaining_lease = lease_tokens - branch_spent
                        if remaining_lease < 1024:
                            return await halt("达到并发分支预留 Token 预算", local,
                                ExpertRoleResult(text="该分支的预留预算已用完", status="budget_exhausted",
                                                 error_kind="budget_exhausted", failed_node_id=node_id))
                        execution_policy = replace(execution_policy, token_budget=remaining_lease)
                    last_usage, last_clock = 0, time.monotonic()
                    for w, state_key in chain:
                        if not w.state.get("active_nodes"):
                            meter_clocks[state_key] = last_clock
                        w.state["active_nodes"] = sorted(set(w.state.get("active_nodes") or []) | {node_id})
                        w.state.update(active_at=time.time(), active_node=w.state["active_nodes"][0])
                    segment_started = time.monotonic()
                    try:
                        result = await run_node(node, node_incoming, execution_policy, prompt)
                    except asyncio.CancelledError:
                        state["interrupted_nodes"] = sorted(set(state.get("interrupted_nodes") or []) | {node_id})
                        if not group:
                            state["interrupted_node"] = node_id
                        raise
                    except Exception as exc:
                        from mewcode.experts.waits import BranchWaitPaused
                        if isinstance(exc, BranchWaitPaused):
                            raise
                        result = ExpertRoleResult(text=f"{type(exc).__name__}: {exc}", status="failed",
                                                  error_kind="node_failure", failed_node_id=node_id)
                    finally:
                        await meter(0 if result and result.restored else
                                    (result.billable_tokens if result.billable_tokens is not None
                                     else result.input_tokens + result.output_tokens) if result else last_usage)
                        for w, state_key in chain:
                            w.state["active_nodes"] = [n for n in w.state.get("active_nodes", []) if n != node_id]
                            w.state.update(active_at=time.time() if w.state["active_nodes"] else None,
                                           active_node=next(iter(w.state["active_nodes"]), ""))
                            await notify(w, state_key)
                    # Save the most recent segment before any recovery
                    # callback can wait or cancel; never resurrect an older
                    # segment on the next Continue.
                    state["round_results"][node_id] = packed(result)
                    await notify(watchdog, key)
                    if result.status == "review_pending" and reusable_result(node_id, packed(result), loop):
                        break  # Dispatch the frozen reviewer, not a retry or user question.
                    handoff_kind = handoff_error_kind(result.handoff)
                    protocol_retry = False
                    if (reviewer and not result.loop_decision
                            and result.status in {"completed", "incomplete_audit"}
                            and not result.error_kind and not handoff_kind):
                        corrections = state.setdefault("protocol_corrections", {})
                        protocol_retry = corrections.get(node_id, 0) < 1
                        corrections[node_id] = corrections.get(node_id, 0) + 1
                        if protocol_retry:
                            allowance += 1  # One correction within the same remaining resource budget.
                        result = replace(result, status="budget_exhausted" if protocol_retry else "needs_attention",
                                         error_kind="handoff_format" if protocol_retry else "audit_protocol_missing")
                    internal_work = (handoff_kind == "internal_work" and (result.handoff or {}).get("open_items")
                                     and not (result.handoff or {}).get("blockers")
                                     and not (result.handoff or {}).get("question")
                                     and result.error_kind in {"", "internal_work"})
                    if internal_work and result.status in {"completed", "needs_attention", "continuing"}:
                        result = replace(result, status="continuing", error_kind="internal_work")
                    if (result.handoff or {}).get("status") in {"blocked", "needs_attention"} or (result.handoff or {}).get("blockers"):
                        result = replace(result,
                            status="needs_attention" if result.status == "completed" else result.status,
                            error_kind=result.error_kind or handoff_kind)
                    format_retry = (result.error_kind == "handoff_format" and result.status == "needs_attention"
                                    and node.role in {"manager", "planner"}
                                    and not (result.handoff or {}).get("question"))
                    recoverable = (result.status in {"continuing", "budget_exhausted", "stalled"} or format_retry)
                    from mewcode.experts.recovery_state import HARD_KINDS
                    if (result.error_kind in HARD_KINDS or ((result.handoff or {}).get("blockers") and not format_retry)
                            or (result.handoff or {}).get("question")):
                        recoverable = False
                    # More than one continuation requires host-persisted
                    # item progress, not a model's claim of progress.
                    progress = result.review_progress or {}
                    from mewcode.experts.review_evidence import advance_cursor
                    cursors = state.setdefault("review_cursors", {})
                    prior_review_cursor = cursors.get(node_id)
                    advanced, cursor = advance_cursor(progress, prior_review_cursor)
                    if cursor is not None:
                        cursors[node_id] = cursor
                    from mewcode.experts.recovery_state import advance_work_cursor
                    work = result.work_progress or {}
                    from mewcode.kernel.collaboration_faults import scheduled_yield
                    # The collaboration host projects current prerequisites
                    # for completed and partial work alike. Raw historical
                    # denials remain available as evidence, not a new stop.
                    work_blockers = work.get('continuation_blockers', work.get('blockers'))
                    if work_blockers:
                        from mewcode.kernel.collaboration_faults import continuation_fault
                        kind = continuation_fault(work_blockers, result.error_kind)
                        result = replace(result, status="needs_attention", error_kind=kind)
                        recoverable, protocol_retry = False, False
                    work_cursors = state.setdefault("work_cursors", {})
                    prior_work_cursor = work_cursors.get(node_id) or work.get("before") or {}
                    advanced_work, work_cursor = advance_work_cursor(work, work_cursors.get(node_id))
                    if work_cursor is not None:
                        work_cursors[node_id] = work_cursor
                    from mewcode.experts.adaptive_segment_budget import observed_segment
                    samples = state.setdefault("segment_budget_history", {}).setdefault(node_id, [])
                    samples.append(observed_segment(iterations=result.iterations_used,
                        wall_seconds=time.monotonic() - segment_started,
                        progressed=advanced or advanced_work))
                    del samples[:-8]
                    review_units = (max(0, cursor["complete"] - (prior_review_cursor or {}).get("complete", 0))
                                    if advanced and cursor else 0)
                    work_units = (max(0, int(work_cursor.get("review_complete") or 0)
                                      - int(prior_work_cursor.get("review_complete") or 0))
                                  if advanced_work and work_cursor else 0)
                    if advanced_work and work_cursor:
                        work_units = max(work_units, len(set(work_cursor.get("submitted_items") or [])
                            - set(prior_work_cursor.get("submitted_items") or [])))
                    remaining_items = (progress.get("total") - progress.get("complete")
                                       if type(progress.get("total")) is int
                                       and type(progress.get("complete")) is int
                                       and 0 <= progress["complete"] <= progress["total"]
                                       else None)
                    segment_receipt_id = uuid.uuid4().hex
                    for window_watchdog, window_key in chain:
                        window_receipt = window_watchdog.settle_token_window(
                            segment_id=segment_receipt_id,
                            durable_progress=advanced or advanced_work,
                            progress_items=max(review_units, work_units),
                            remaining_items=remaining_items,
                        )
                        if window_receipt is not None:
                            await notify(window_watchdog, window_key)
                    advancing_queue = (advanced or advanced_work) and result.status in {"continuing", "budget_exhausted"} and recoverable
                    if advancing_queue and (watchdog.team_mode or scheduled_yield(result)):
                        # At most two monotonic improvements per frozen item.
                        # Large finite queues must not hit an arbitrary 64-chat wall.
                        allowance += 1
                    parent_stopped = any(w.remaining() or (w.token_limit is not None and w.token_limit - w.state["tokens"] < 1024)
                                         for w, _ in chain if not w.team_mode)
                    selected_recovery = None
                    if stage_recover is not None and result.status != "completed" and not advancing_queue and not protocol_retry:
                        remaining_tokens = [w.token_limit - w.state["tokens"] for w, _ in chain if w.token_limit is not None]
                        remaining_seconds = [w.time_limit - w.state["elapsed"] for w, _ in chain if w.time_limit is not None]
                        async def account_decision(tokens, seconds):
                            for w, state_key in chain:
                                w.state["tokens"] += tokens
                                w.state["elapsed"] += seconds
                                await notify(w, state_key)
                        actions = [] if parent_stopped else ["continue_checkpoint", "diagnose_path"]
                        if actions:
                            actions.append("reduce_execution_profile")
                        if (actions and getattr(node, "backup_models", ())
                                and result.error_kind in {"provider_interrupted", "model_stream_stalled",
                                                          "provider_rate_limited", "provider_temporarily_unavailable",
                                                          "semantic_uncertain", "reasoning_insufficient",
                                                          "review_low_confidence"}):
                            actions.append("switch_declared_route")
                        state["round_results"][node_id] = packed(result)
                        state.update(status="blocked", failed_node_id=node_id,
                                     error_kind=result.error_kind or result.status,
                                     reason="节点未完成，等待调度恢复")
                        state["interrupted_node"] = node_id
                        await notify(watchdog, key)
                        selected_recovery = await stage_recover(node=node, result=result,
                            policy=replace(execution_policy,
                                token_budget=min(remaining_tokens) if remaining_tokens else None,
                                timeout_seconds=min(remaining_seconds) if remaining_seconds else seconds_left),
                            allowed=actions,
                            host_facts={"trigger": result.error_kind or result.status, "loop_id": loop.loop_id,
                                "round": state["round"], "control_mode": "team" if watchdog.team_mode else "strict",
                                "hard_boundary": "父循环资源或收敛约束已到达" if parent_stopped else "",
                                "remaining_tokens": min(remaining_tokens) if remaining_tokens else None,
                                "remaining_seconds": min(remaining_seconds) if remaining_seconds else None,
                                "token_window_receipt": (state.get("token_window") or {}).get("last_receipt"),
                                "completed_nodes": list(state.get("round_results", {})),
                                "progress": result.review_progress or {}}, account=account_decision)
                        decision_checked = True
                        # Recheck after decision costs; no synthetic budget extension.
                        parent_stopped = any(w.remaining() or (w.token_limit is not None and w.token_limit - w.state["tokens"] < 1024)
                                             for w, _ in chain if not w.team_mode)
                        if not selected_recovery or selected_recovery.get("action") not in actions or parent_stopped:
                            break
                        state.update(status="running", failed_node_id="", error_kind="", reason="")
                        await notify(watchdog, key)
                    if selected_recovery:
                        recoverable = True
                    if not ((protocol_retry or (watchdog.team_mode and (segment == 0 or advancing_queue))
                             or (scheduled_yield(result) and advancing_queue)) and allowance > 0
                            and recoverable and not parent_stopped) and not selected_recovery:
                        break
                    if advancing_queue:
                        # A productive host slice is a scheduling handoff,
                        # not another failed-method recovery opportunity.
                        from mewcode.experts.auto_card import auto_card
                        from mewcode.experts.recovery_diagnostics import _cursor_delta
                        next_items = (result.handoff or {}).get("open_items") or []
                        continuation = {"decision_id": "continuation-" + segment_receipt_id,
                            "action": "continue_checkpoint", "source": "host_progress_checkpoint",
                            "trigger": result.error_kind or result.status,
                            "host_check": "本批新增进展已保存；按当前循环合同继续未完成项",
                            "chosen": {"next_step": str(next_items[0]) if next_items else "按台账处理下一批未完成项"}}
                        continuation["presentation"] = auto_card(continuation,
                            {"handoff": result.handoff, "input_revision": execution_policy.input_revision},
                            health={"segment_id": segment_receipt_id,
                                "work_delta": _cursor_delta(prior_work_cursor, work_cursor or {})})
                        await emit({"type": "expert_step", "node_id": node_id, "role": node.role,
                            "status": "running", "label": "批次进展已保存，接续下一批",
                            "recovery_decision": continuation,
                            "summary": (f"已提交候选 {len((work_cursor or {}).get('submitted_items', []))} 项；继续处理本节点待办。"
                                if not progress else f"完成 {progress.get('complete', 0)}/{progress.get('total', 0)}；"
                                       f"未处理 {progress.get('pending', 0)}，待返修 {progress.get('needs_repair', 0)}，"
                                       f"未知 {progress.get('unknown', 0)}。"), "review_progress": progress})
                    state.setdefault("recovery_history", []).append({"round": state["round"], "roots": [node_id],
                        "reason": "reviewed_scheduler_recovery" if selected_recovery else "handoff_format_correction" if format_retry else "automatic_checkpoint_continuation",
                        "results": {node_id: packed(result)}})
                    state["round_results"][node_id] = packed(result)
                    state["interrupted_node"] = node_id
                    await notify(watchdog, key)
                    remaining_tokens = [w.token_limit - w.state["tokens"] for w, _ in chain if w.token_limit is not None]
                    remaining_seconds = [w.time_limit - w.state["elapsed"] for w, _ in chain if w.time_limit is not None]
                    execution_policy = replace(execution_policy, resume_messages=True,
                        token_budget=min(remaining_tokens) if remaining_tokens else None,
                        timeout_seconds=min(remaining_seconds) if remaining_seconds else seconds_left,
                        max_iterations=min(execution_policy.max_iterations or 6, 6)
                            if selected_recovery and selected_recovery["action"] == "diagnose_path" else execution_policy.max_iterations)
                    if selected_recovery:
                        pending_diagnostic_slice = selected_recovery["action"] == "diagnose_path"
                        if selected_recovery["action"] == "reduce_execution_profile":
                            state.setdefault("pending_reduced_profiles", {})[node_id] = True
                        prompt += "\n宿主恢复路径（不增加权限、不改变验收）：" + json.dumps(selected_recovery, ensure_ascii=False)
                    if protocol_retry:
                        prompt += "\n【一次审查判定补交】复用当前证据，调用 SubmitLoopDecision 提交真实判定；缺证据不能填 PASS，不重跑已完成操作。"
                    prompt += ("\n【一次交付形式补交】复用本节点已保存的分析，只核对并补交 SubmitHandoff.inline_outputs。"
                               "不重复执行检索、写文件或命令。任务未要求独立文件时，内联交付即可；若确实要求文件且缺能力，"
                               "如实提交 capability_missing，不得把未交付文件标为完成。"
                               if format_retry else
                               "\n当前节点已自动接续一次：复用已保存的工具结果和产物，核对当前状态，不重复已完成操作；完成后提交实际交接。")
                handoff_status = (result.handoff or {}).get("status")
                if result.status == "completed" and handoff_status in {"blocked", "needs_attention"}:
                    result = replace(result, status="needs_attention",
                                     error_kind=result.error_kind or handoff_error_kind(result.handoff) or "node_failure",
                                     text=(result.handoff or {}).get("summary") or result.text)
                if state.get("interrupted_node") == node_id:
                    state.pop("interrupted_node", None)
                interrupted = set(state.get("interrupted_nodes") or [])
                if not reviewer and result.status in {"continuing", "budget_exhausted", "stalled"}:
                    interrupted.add(node_id)
                else:
                    interrupted.discard(node_id)
                state["interrupted_nodes"] = sorted(interrupted)
                local[node_id] = result
                state["round_results"][node_id] = packed(result)
                await notify(watchdog, key)
                if result.status != "completed" and not (result.status == "review_pending"
                        and reusable_result(node_id, packed(result), loop)):
                    failure = replace(result, failed_node_id=result.failed_node_id or node_id,
                                      error_kind=result.error_kind or ("budget_exhausted" if any(
                                          w.remaining() and w.stop_kind(w.remaining()) == "budget_exhausted" for w, _ in chain)
                                          else "recovery_needed" if result.status in {"continuing", "budget_exhausted", "stalled"} else "node_failure"))
                    detail = str((result.handoff or {}).get("question") or result.text).strip()
                    if len(detail) > 1600:
                        detail = "…" + detail[-1600:]
                    return await halt(f"节点 {node.label} 未完成：{detail}", local, failure, decision_checked=decision_checked)
                return None

            handled_groups = set()
            for batch in flow.topological_batches():
                for node_id in batch:
                    group = branches.get(node_id)
                    if group is not None:
                        if group.group_id in handled_groups:
                            continue
                        handled_groups.add(group.group_id)
                        from mewcode.experts.parallel_runtime import run_parallel_group
                        await run_parallel_group(group=group, local=local, state=state, chain=chain,
                            execute_stage=execute_stage, notify=lambda: notify(watchdog, key, durable=True))
                        continue
                    outcome = await execute_stage(node_id)
                    if outcome is not None:
                        return await stop(*outcome)

            reports = {node_id: local[node_id].loop_decision for node_id in loop.reviewer_node_ids
                       if node_id in local and local[node_id].loop_decision}
            if len(reports) != len(set(loop.reviewer_node_ids)):
                return await stop("未执行全部审查节点或缺少结构化判定", local)
            state["reports"] = reports
            state["skipped_ids"] = sorted(skipped)
            from mewcode.experts.loop_progress import round_progress
            work_progress = round_progress(local, loop, reports)
            status, reason = watchdog.observe(list(reports.values()), work_progress=work_progress)
            if status == "completed":
                missing = [n for n in loop.required_node_ids if n not in local or not passed(local[n])]
                if missing:
                    status, reason = "blocked", "判定不能覆盖未通过或未完成的必需阶段：" + "、".join(missing)
            # An otherwise passing response must not conceal a breached budget.
            if watchdog.token_limit is not None and state["tokens"] > watchdog.token_limit:
                status, reason = "blocked", "达到循环 Token 预算"
            if watchdog.time_limit is not None and state["elapsed"] > watchdog.time_limit:
                status, reason = "blocked", "达到循环时间上限"
            if status == "continue" and directed_repair:
                decision = reports[loop.check_node_id]
                roots = decision.get("repair_node_ids") or []
                allowed = {loop.entry_node_id, *loop.repair_node_ids}
                if (decision.get("decision") != "CONTINUE" or not isinstance(roots, list) or not roots
                        or any(not isinstance(n, str) or n not in allowed for n in roots)
                        or len(set(roots)) != len(roots)
                        or loop.entry_node_id in roots and len(roots) != 1):
                    status, reason = "blocked", "判定后调度需要最终判定明确选择首次入口或已声明返修节点，不能混选或仅靠复核者建议"
                else:
                    # Closing a round and choosing the next dispatch are ONE
                    # durable checkpoint. A crash between two notifications
                    # must not restart first reads or reuse an older repair plan.
                    affected = (affected_nodes(flow, loop, roots) | set(loop.reviewer_node_ids)
                                | set(loop.repair_node_ids))
                    state["dispatch_roots"] = list(roots)
                    state["carry_results"] = {node_id: packed(result) for node_id, result in local.items() if node_id not in affected}
            state.update(status=status, reason=reason, round_open=False, active_node="")
            await notify(watchdog, key)
            if status == "blocked":
                # Team convergence guards are recoverable only through a
                # bounded reviewed change of path, never a silent fixed retry.
                # Strict round/time/token contracts are never reset here.
                signature = state.get('progress_signature')
                diagnosed = state.setdefault('convergence_diagnoses', [])
                if (recover_node is not None and watchdog.team_mode and state.get("stalled", 0) >= loop.max_stalled_rounds
                        and signature not in diagnosed
                        and not any(w.remaining() for w, _ in chain if not w.team_mode)):
                    # A bounded path diagnosis may run once for these exact
                    # producer versions. Its own prose cannot replenish it.
                    diagnosed.append(signature)
                    del diagnosed[:-20]
                    await notify(watchdog, key, durable=True)
                    async def account_stall(tokens, seconds):
                        for w, state_key in chain:
                            w.state["tokens"] += tokens
                            w.state["elapsed"] += seconds
                            await notify(w, state_key)
                    limits = [w.token_limit - w.state["tokens"] for w, _ in chain if w.token_limit is not None]
                    clocks = [w.time_limit - w.state["elapsed"] for w, _ in chain if w.time_limit is not None]
                    selected = await recover_node(node=flow.node_map[loop.check_node_id],
                        result=ExpertRoleResult(text=reason, status="needs_attention", error_kind="no_progress"),
                        policy=replace(policy_for(flow.node_map[loop.check_node_id]),
                            token_budget=min(limits) if limits else None, timeout_seconds=min(clocks) if clocks else None),
                        allowed=["diagnose_path"], host_facts={"trigger": "no_progress", "loop_id": loop.loop_id,
                            "round": state["round"], "progress": {"stalled_rounds": state["stalled"]},
                            "scope": "bounded path diagnosis, frozen acceptance unchanged"}, account=account_stall)
                    if selected and selected.get("action") == "diagnose_path" and not any(
                            w.remaining() for w, _ in chain if not w.team_mode):
                        roots = [n for r in reports.values() for n in r.get("repair_node_ids", []) if n in body]
                        if directed_repair:
                            # A no-progress diagnostic is not a repair dispatch
                            # grant. Re-evaluate the final decision, keeping work.
                            roots = [loop.check_node_id]
                        affected = affected_nodes(flow, loop, roots or [loop.entry_node_id]) | set(loop.reviewer_node_ids)
                        state.update(status="running", reason="", stalled=0,
                            carry_results={n: packed(r) for n, r in local.items() if n not in affected},
                            recovery_note="审核选择有限路径诊断；不修改验收条件：" + json.dumps(selected.get("chosen", {}), ensure_ascii=False))
                        state.setdefault("recovery_history", []).append({"round": state["round"],
                            "reason": "reviewed_no_progress_diagnosis", "roots": sorted(affected), "decision": selected})
                        await notify(watchdog, key)
                        continue
                    return await stop(reason, local, decision_checked=True)
                return await stop(reason, local, decision_checked=signature in diagnosed)
            if status == "continue" and watchdog.team_mode and not directed_repair:
                roots = [node_id for report in reports.values() for node_id in report.get("repair_node_ids") or []]
                roots = [node_id for node_id in roots if node_id in body and node_id not in loop.reviewer_node_ids]
                affected = affected_nodes(flow, loop, roots or [loop.entry_node_id]) | set(loop.reviewer_node_ids)
                state["carry_results"] = {node_id: packed(result) for node_id, result in local.items() if node_id not in affected}
                await notify(watchdog, key)
            if status == "completed":
                if loop.mode == "foreach":
                    index = state.get("item_index", 0)
                    state.setdefault("completed_items", []).append(loop.items[index]["item_id"])
                    state.setdefault("item_results", {})[loop.items[index]["item_id"]] = dict(state["round_results"])
                    if index + 1 < len(loop.items):
                        state.update(item_index=index + 1, item_round=0, scores=None, score=0, stalled=0,
                                     history=[], reports={}, interrupted_nodes=[], status="running", round_results={})
                        state.pop("dispatch_roots", None)
                        await notify(watchdog, key)
                        continue
                for node_id in loop.reviewer_node_ids:
                    local[node_id] = replace(local[node_id], verdict={"verdict": "PASS"})
                    state["round_results"][node_id] = packed(local[node_id])
                if loop.mode == "foreach":
                    queue_results, artifacts = {}, {}
                    for item_id, outputs in state.get("item_results", {}).items():
                        queue_results[item_id] = {}
                        for output_id, output in outputs.items():
                            handoff = output.get("handoff") or {}
                            queue_results[item_id][output_id] = {
                                "summary": str(handoff.get("summary") or output.get("text") or "")[:1200],
                                "handoff": handoff, "loop_decision": output.get("loop_decision")}
                            for artifact in handoff.get("artifacts") or []:
                                if isinstance(artifact, dict) and artifact.get("path"):
                                    artifacts[artifact["path"]] = artifact
                    summary = f"队列 {loop.loop_id} 已完成 {len(state['completed_items'])}/{len(loop.items)} 项；每项均通过冻结条件。"
                    check = local[loop.check_node_id]
                    local[loop.check_node_id] = replace(check, text=summary + "\n" + check.text,
                        handoff={"summary": summary, "artifacts": list(artifacts.values()), "open_items": [],
                                 "queue_results": queue_results})
                    state["round_results"][loop.check_node_id] = packed(local[loop.check_node_id])
                    await notify(watchdog, key)
                for node_id in skipped:
                    await emit({"type": "expert_node_completed", "node_id": node_id,
                                "role": flow.node_map[node_id].role, "label": flow.node_map[node_id].label,
                                "status": "skipped", "reason": "branch_not_taken"})
                for node_id, result in local.items():
                    await emit({"type": "expert_node_completed", "node_id": node_id,
                                "role": flow.node_map[node_id].role, "label": flow.node_map[node_id].label,
                                "status": result.status, "text": result.text, "verdict": result.verdict,
                                "loop_decision": result.loop_decision, "loop_id": loop.loop_id,
                                "loop_round": state["round"], "passed": passed(result)})
                return local, local[loop.check_node_id]

    from mewcode.experts.waits import BranchWaitPaused
    try:
        return await run(contract, contract.loop_id, incoming)
    except BranchWaitPaused as exc:
        return {}, ExpertRoleResult(text=exc.reason or '本循环分支等待用户决定，其他独立分支继续。', status='paused',
                                    error_kind=exc.error_kind, failed_node_id=exc.node_id)
