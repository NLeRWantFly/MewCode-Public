"""Parent-owned, bounded executor assistance for missing local capabilities."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import time
from dataclasses import replace
from pathlib import Path, PureWindowsPath


def _request_body(handoff):
    value = handoff or {}
    file_requests = list(value.get("file_requests") or [])
    requested_names = {
        str(item.get("output_name") or "")
        for item in file_requests
        if isinstance(item, dict)
    }
    return {
        "file_requests": file_requests,
        "file_contents": [
            {"name": str(item.get("name") or ""), "content": str(item.get("content") or "")}
            for item in (value.get("inline_outputs") or [])
            if isinstance(item, dict) and str(item.get("name") or "") in requested_names
        ],
        "command_requests": list(value.get("command_requests") or []),
    }


def _request_hash(handoff):
    encoded = json.dumps(_request_body(handoff), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _route_key(value):
    if not isinstance(value, dict):
        return ""
    provider = str(value.get("provider") or value.get("provider_key") or "").strip()
    model = str(value.get("model") or value.get("model_id") or "").strip()
    diagnostic = str(value.get("request_error") or "")
    if not provider:
        match = re.search(r"Provider=([^;\n]+)", diagnostic)
        provider = match.group(1).strip() if match else ""
    if not model:
        match = re.search(r"model=([^;\n]+)", diagnostic)
        model = match.group(1).strip() if match else ""
    # Saved diagnostics use the display provider while ModelRef persists the
    # registry-qualified key (for example ``cc-switch:cc-switch-claude``).
    provider = provider.rsplit(":", 1)[-1]
    return provider + "/" + model if provider or model else ""


def _same_failed_escalation(current, prior, *, executor_route, previous_helper_state):
    receipt = (prior or {}).get("escalation_receipt") or {}
    if receipt.get("status") != "needs_attention":
        return None
    current_body, prior_body = _request_body(current), _request_body(prior)
    if current_body["command_requests"] and prior_body["command_requests"]:
        from mewcode.experts.command_receipts import request_id
        same_request = (sorted(request_id("", "", item) for item in current_body["command_requests"])
                        == sorted(request_id("", "", item) for item in prior_body["command_requests"]))
    else:
        same_request = current_body == prior_body
    if not same_request:
        return None
    current_route = _route_key(executor_route)
    previous_route = _route_key(receipt.get("executor_route")) or _route_key(previous_helper_state)
    if current_route != previous_route:
        return None
    return receipt


def _verification_evidence(receipt):
    evidence = []
    for item in receipt.verification_records or []:
        if not isinstance(item, dict) or item.get("name") != "Bash":
            continue
        args = item.get("args") if isinstance(item.get("args"), dict) else {}
        evidence.append({
            "kind": "command",
            "ref": str(args.get("command") or item.get("command") or "")[:2000],
            "result": str(item.get("output") or item.get("status") or "")[:4000],
        })
    return evidence


async def _resolve_capability_requests(
    node,
    result,
    policy,
    *,
    work_dir,
    dispatch,
    emit,
    prior_handoff=None,
    executor_route=None,
    previous_helper_state=None,
    allow_command_followup=True,
    prior_command_receipts=(),
    command_admission=None,
    verification_revision="",
):
    handoff = result.handoff or {}
    file_requests = handoff.get("file_requests") or []
    command_requests = handoff.get("command_requests") or []
    if (not (file_requests or command_requests)
            or handoff.get("blocker_kind") != "capability_missing"
            or handoff.get("question")
            or result.error_kind not in {"", "capability_missing"}
            or result.status not in {"blocked", "needs_attention", "completed"}):
        return result

    from mewcode.experts.command_receipts import request_receipts, observe_requests
    # The exact-command gateway below requires native discard publication;
    # this is a host execution contract, never the model's read-only claim.
    rows = request_receipts(node.node_id, policy.input_revision, command_requests,
                            epoch=policy.node_epoch, policy={'publication': 'discard'})
    if verification_revision:
        for row in rows:
            row["base_request_id"] = row["request_id"]
            row["verification_revision"] = verification_revision
            row["request_id"] = hashlib.sha256((row["request_id"] + ":" + verification_revision).encode()).hexdigest()

    async def stop_commands(reason, *, status="not_dispatched", error_kind="capability_missing"):
        nonlocal rows
        rows = [{**row, "status": status, "reason": reason,
                 "command_started": False if status in {"not_started", "not_dispatched"} else row.get("command_started")}
                for row in rows]
        merged = copy.deepcopy(handoff)
        merged["escalation_receipt"] = {"status": "needs_attention", "kind": "command",
            "request_hash": _request_hash(handoff), "input_revision": policy.input_revision,
            "executor_route": copy.deepcopy(executor_route or {}), "error": reason,
            "text": reason, "verification_records": [], "command_receipts": rows}
        await emit({"phase": "capability_escalation", "node_id": node.node_id,
                    "status": "needs_attention", "reason": reason,
                    "command_receipts": rows, "durable_checkpoint": True})
        return replace(result, status="needs_attention", error_kind=error_kind,
                       handoff=merged, command_receipts=rows, text=result.text + "\n" + reason)

    restricted = (policy.parallel_readonly or policy.audit_contract
                  or (policy.loop_context or {}).get("reviewer"))
    if restricted or node.role not in {"manager", "planner", "executor"}:
        return await stop_commands("只读审查或角色约束禁止命令转派") if command_requests else result
    if file_requests and node.role not in {"manager", "planner"}:
        return result

    # Validate even custom runners that bypass SubmitHandoff's schema.
    from mewcode.tools.submit_handoff import SubmitHandoffParams
    try:
        parsed = SubmitHandoffParams.model_validate(handoff)
        if work_dir is None:
            raise ValueError("缺少已授权的工作目录")
        root = Path(work_dir).resolve()
        deliveries, paths = [], set()
        for request in parsed.file_requests:
            raw = request.path.replace("\\", "/")
            path = (root / raw).resolve()
            if (PureWindowsPath(raw).drive or raw.startswith("/") or ":" in raw
                    or ".." in raw.split("/") or not path.is_relative_to(root)
                    or path == root or path in paths
                    or any(part.lower() in {".git", ".codex", ".agents"} for part in raw.split("/"))):
                raise ValueError("文件转派路径必须是工作区内唯一的普通相对路径")
            paths.add(path)
            content = next(item.content for item in parsed.inline_outputs if item.name == request.output_name)
            deliveries.append((path, content, raw))
        commands = tuple(request.command.strip() for request in parsed.command_requests)
        if commands and not allow_command_followup:
            raise ValueError("同一节点一次运行只允许一轮命令能力回填；请合并只读探测命令")
    except (ValueError, OSError) as exc:
        if command_requests:
            return await stop_commands(f"转派请求无效：{exc}")
        return replace(result, status="needs_attention", text=result.text + f"\n转派请求无效：{exc}")

    kind = "command" if commands else "file"
    current_hash = _request_hash(handoff)
    from mewcode.experts.command_receipts import reconcile_helper_receipts
    previous = {row.get("base_request_id") or row.get("request_id"): row for row in reconcile_helper_receipts(prior_command_receipts, previous_helper_state)
                if row.get("node_id") == node.node_id and row.get("input_revision") == policy.input_revision}
    protected = {"completed", "in_flight", "outcome_unknown"}
    reused = [previous.get(row["request_id"], row) for row in rows]
    for row in rows:
        prior = previous.get(row["request_id"]) or {}
        if prior.get("recovery_attempt"):
            row["recovery_attempt"] = copy.deepcopy(prior["recovery_attempt"])
    if commands and any(row["status"] in protected for row in reused):
        rows = copy.deepcopy(reused)
        merged = copy.deepcopy(handoff)
        merged["escalation_receipt"] = copy.deepcopy((prior_handoff or {}).get("escalation_receipt") or {})
        merged["escalation_receipt"]["command_receipts"] = rows
        complete = all(row.get("successful") for row in rows)
        if complete:
            merged["command_requests"] = []
            merged["capability_followup_required"] = True
            merged["capability_results"] = {"command_receipts": rows,
                "summary": "复用与本节点输入版本一致的命令回执", "evidence": [], "verification_records": []}
        await emit({"phase": "capability_escalation", "node_id": node.node_id,
                    "status": "completed" if complete else "needs_attention", "deduplicated": True,
                    "command_receipts": rows, "durable_checkpoint": True})
        return replace(result, status="needs_attention", handoff=merged, command_receipts=rows,
                       error_kind="capability_resolved" if complete else "capability_missing",
                       text=result.text + "\n已有完成、在途或结果未知的宿主回执；未重复派发命令。")
    prior_receipt = _same_failed_escalation(
        handoff,
        prior_handoff,
        executor_route=executor_route,
        previous_helper_state=previous_helper_state,
    )
    if prior_receipt is not None and not (commands and previous):
        merged = copy.deepcopy(handoff)
        merged["escalation_receipt"] = copy.deepcopy(prior_receipt)
        reason = str(prior_receipt.get("text") or prior_receipt.get("error")
                     or "同一执行器的上次转派未完成")
        if commands:
            rows = [{**row, "status": "outcome_unknown", "command_started": None,
                     "reason": "复用历史失败回执；缺少逐命令宿主观察：" + reason} for row in rows]
            merged["escalation_receipt"]["command_receipts"] = rows
        await emit({"phase": "capability_escalation", "node_id": node.node_id,
                    "status": "needs_attention", "reason": reason, "deduplicated": True,
                    **({"command_receipts": rows, "durable_checkpoint": True} if commands else {})})
        return replace(
            result,
            status="needs_attention",
            handoff=merged,
            command_receipts=rows,
            error_kind="capability_missing",
            text=result.text + "\n检测到相同能力请求和相同 executor 的失败回执，未原样重试：" + reason,
        )

    # The helper does not bypass normal permissions. It receives only the
    # exact capability required by this structured request.
    used = result.billable_tokens if result.billable_tokens is not None else result.input_tokens + result.output_tokens
    remaining = None if policy.token_budget is None else policy.token_budget - used
    if remaining is not None and remaining < 1024:
        if commands:
            return await stop_commands("剩余预算不足，命令未派发", error_kind="budget_exhausted")
        return replace(result, status="needs_attention", error_kind="budget_exhausted")
    original = copy.deepcopy(handoff)
    sources = {node.node_id: {"node_id": node.node_id, "handoff": original}}

    async def meter(total):
        if policy.usage_callback:
            await policy.usage_callback(used + total)

    from mewcode.tools import expand_tool_aliases
    allowed = {"ReadFile", "Glob", "Grep", "Bash"} if commands else {
        "ReadFile", "WriteFile", "Glob", "Grep"
    }
    if policy.node_tools is not None:
        explicit = expand_tool_aliases(set(policy.node_tools))
        if "*" not in explicit:
            allowed &= explicit
    denied = expand_tool_aliases(set(policy.deny_tools))
    required_tool = "Bash" if commands else "WriteFile"
    if required_tool not in allowed or required_tool in denied or "*" in denied:
        if commands:
            return await stop_commands(f"显式工具限制禁止 {required_tool}；转派不能绕过该限制。")
        return replace(result, status="needs_attention",
            text=result.text + f"\n上级已接收请求，但显式工具限制禁止 {required_tool}；转派不能绕过该限制。")
    helper_iterations = max(8, min(24, len(commands) * 2 + 2))
    import uuid
    receipt_context = {"dispatch_id": uuid.uuid4().hex, "records": [],
                       "source_node_id": node.node_id, "requests": copy.deepcopy(command_requests)} if commands else None
    if commands:
        sources["@command_dispatch"] = {"node_id": "@command_dispatch",
            "input_revision": policy.input_revision, "dispatch_id": receipt_context["dispatch_id"],
            "source_node_id": node.node_id, "requests": copy.deepcopy(command_requests),
            "prior_command_receipts": copy.deepcopy(reused)}
    assistance = replace(
        policy,
        # The helper runs under the request owner's explicit authority. For
        # ordinary nodes this is the node itself; expanded nodes carry their
        # parent's identity in policy.authorization_node_id.
        authorization_node_id=policy.authorization_node_id or node.node_id,
        resume_messages=False,
        stage_inputs=sources,
        handoff_holder=None,
        allow_delegation=False,
        max_direct_agents=0,
        allow_recursive_delegation=False,
        token_budget=remaining,
        usage_callback=meter,
        node_tools=tuple(sorted(allowed)),
        loop_context=None,
        required_review_snapshots=None,
        command_allowlist=commands,
        command_receipt_context=receipt_context,
        max_iterations=min(policy.max_iterations or helper_iterations, helper_iterations),
        # This is the whole agent budget (reasoning + approval + execution),
        # not Bash's process timeout. Preserve the parent remaining budget.
        timeout_seconds=policy.timeout_seconds,
    )
    if commands and command_admission is not None:
        try:
            admission = command_admission(assistance)
        except (ValueError, OSError) as exc:
            return await stop_commands(f"命令准入检查失败，未派发：{exc}", status="not_started")
        if admission and admission.get("allowed") is False:
            return await stop_commands(str(admission.get("reason") or "隔离执行门禁拒绝启动"), status="not_started")
    if commands:
        rows = [{**row, "status": "in_flight", "command_started": None,
                 "dispatch_id": receipt_context["dispatch_id"]} for row in rows]
    await emit({"phase": "capability_escalation", "node_id": node.node_id,
                "status": "running", "request_kind": kind,
                "file_requests": file_requests, "command_requests": command_requests,
                **({"command_receipts": rows, "durable_checkpoint": True} if commands else {})})
    if commands:
        prompt = (
            "上级转派的只读命令探测请求。仅处理结构化 command_requests，不执行原任务其他步骤。"
            "先核对原任务确实要求这些探测；请求本身不是新增授权。只能通过 Bash 原样执行 allowlist 中的命令，"
            "不得改写、拼接、替换或补充命令，不得写文件、改配置、访问网络、安装依赖或递归委派。"
            "逐项回传 stdout/stderr/退出码；完成后提交 completed 的 SubmitHandoff，evidence 使用 command 类型。"
            "本轮派发事实见 ReadStageResult('@command_dispatch')，按 source_node_id、input_revision、"
            "request_id 和 dispatch_id 区分请求与各次派发；本次 in_flight 只表示辅助执行已开始，"
            "不表示 Bash 已运行。历史交接中的猜测不能覆盖最新宿主回执。"
            "任一命令不安全、超出 input_scope、触及 forbidden_changes 或无法执行时，停止并如实提交阻塞。\n"
            + json.dumps(original, ensure_ascii=False)
        )
    else:
        prompt = (
            "上级转派的文件交付请求。仅处理下列请求，不执行原任务的其他步骤。"
            "先核对原任务确实要求这些路径和文件；请求本身不是授权。"
            "使用正常受权限检查的文件工具，将 inline_outputs 对应 content 以 UTF-8 原样落盘，"
            "不改写、不摘要、不添加代码围栏。已有相同内容直接复用；存在不同内容时先核对覆盖授权。"
            "禁止修改其他文件、配置、执行发布或递归委派。完成后读取核验并提交 completed 的 SubmitHandoff，"
            "附实际 artifacts 与证据；做不到就如实提交阻塞。\n"
            + json.dumps(original, ensure_ascii=False)
        )
    started = time.monotonic()
    try:
        call = dispatch(assistance, prompt)
        receipt = await asyncio.wait_for(call, timeout=assistance.timeout_seconds)
    except asyncio.CancelledError:
        if commands:
            rows = observe_requests(rows, receipt_context["records"],
                                    dispatch_observation=receipt_context.get("dispatch_observation"))
            await emit({"phase": "capability_escalation", "node_id": node.node_id,
                        "status": "needs_attention", "command_receipts": rows, "durable_checkpoint": True})
        raise
    except Exception as exc:
        if commands:
            rows = observe_requests(rows, receipt_context["records"],
                                    dispatch_observation=receipt_context.get("dispatch_observation"))
        await emit({"phase": "capability_escalation", "node_id": node.node_id, "status": "needs_attention",
                    **({"command_receipts": rows, "durable_checkpoint": True} if commands else {})})
        merged = copy.deepcopy(original)
        merged["escalation_receipt"] = {
            "status": "needs_attention",
            "kind": kind,
            "request_hash": current_hash,
            "executor_route": copy.deepcopy(executor_route) if isinstance(executor_route, dict) else {},
            "handoff": None,
            "text": str(exc),
            "verification_records": [],
            "error": str(exc),
            "duration_seconds": time.monotonic() - started,
            "command_receipts": rows,
        }
        return replace(result, status="needs_attention", handoff=merged, command_receipts=rows,
                       error_kind="capability_missing",
                       text=result.text + f"\n上级转派未完成：{exc}")
    error = ""
    if commands:
        rows = observe_requests(rows, receipt.verification_records,
                                dispatch_observation=receipt_context.get("dispatch_observation"))
    from mewcode.experts.completion import handoff_blocker
    try:
        if receipt.status != "completed" or (receipt.handoff or {}).get("status") != "completed":
            error = "executor 未提交完成回执"
        else:
            error = handoff_blocker(receipt.handoff, work_dir=root)
        artifacts = []
        if not error and commands:
            missing = [row["request"]["command"] for row in rows if not row.get("successful")]
            if missing:
                error = "executor 回执缺少命令执行证据：" + "；".join(missing)
        if not error and not commands:
            for path, content, raw in deliveries:
                if (not path.resolve().is_relative_to(root)
                        or path.stat().st_size != len(content.encode("utf-8"))
                        or path.read_bytes() != content.encode("utf-8")):
                    error = f"文件与请求正文不一致：{raw}"
                    break
                artifacts.append({"path": raw, "kind": "file", "note": "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()})
    except (ValueError, OSError) as exc:
        error = f"转派产物核验失败：{exc}"
    merged = copy.deepcopy(original)
    receipt_info = {
        "status": "needs_attention" if error else "completed",
        "kind": kind,
        "request_hash": current_hash,
        "executor_route": copy.deepcopy(executor_route) if isinstance(executor_route, dict) else {},
        "handoff": receipt.handoff,
        "text": receipt.text,
        "verification_records": list(receipt.verification_records or []),
        "error": error,
        "duration_seconds": time.monotonic() - started,
        "input_revision": policy.input_revision,
        "command_receipts": rows,
    }
    merged["escalation_receipt"] = receipt_info
    if not error and commands:
        evidence = list((receipt.handoff or {}).get("evidence") or [])
        evidence.extend(_verification_evidence(receipt))
        merged["command_requests"] = []
        merged["capability_followup_required"] = True
        merged["capability_results"] = {
            "summary": str((receipt.handoff or {}).get("summary") or receipt.text or "")[:2000],
            "evidence": evidence[:16],
            "verification_records": list(receipt.verification_records or []),
            "command_receipts": rows,
        }
    elif not error:
        merged.update(status="completed", blockers=[], blocker_kind="", file_requests=[])
        merged["artifacts"] = list(merged.get("artifacts") or []) + artifacts
        error = handoff_blocker(merged, work_dir=root)
        if error:
            merged.update(status=original["status"], blockers=original.get("blockers", []),
                          blocker_kind="capability_missing", file_requests=file_requests)
            merged["escalation_receipt"].update(status="needs_attention", error=error)
    await emit({"phase": "capability_escalation", "node_id": node.node_id,
                "status": "needs_attention" if error else "completed",
                "request_kind": kind, "reason": error,
                **({"command_receipts": rows, "durable_checkpoint": True} if commands else {})})
    if error:
        status, error_kind = "needs_attention", "capability_missing"
        suffix = f"\n上级转派未完成：{error}"
    elif commands:
        status, error_kind = "needs_attention", "capability_resolved"
        suffix = "\n上级已安排 executor 完成只读命令探测并取得执行证据；结果将回填本节点后自动续跑一次。"
    else:
        status, error_kind = "completed", ""
        suffix = "\n上级已安排 executor 完成文件交付，正文一致性核验通过。"
    return replace(
        result,
        status=status,
        handoff=merged,
        command_receipts=rows,
        error_kind=error_kind,
        text=result.text + suffix,
        verification_records=list(result.verification_records or []) + list(receipt.verification_records or []),
        input_tokens=result.input_tokens + receipt.input_tokens,
        output_tokens=result.output_tokens + receipt.output_tokens,
        billable_tokens=used + (receipt.billable_tokens if receipt.billable_tokens is not None else receipt.input_tokens + receipt.output_tokens),
    )


async def resolve_capability_requests(node, result, policy, *, recover_execution=False, **kwargs):
    """Resolve the request, then offer one separately reviewed recovery transaction."""
    from mewcode.experts.execution_recovery import recover_capability_execution
    if recover_execution:
        prior = [row for row in kwargs.get("prior_command_receipts", ())
                 if row.get("node_id") == node.node_id and row.get("input_revision") == policy.input_revision]
        interrupted = [row for row in prior if (row.get("recovery_attempt") or {}).get("status")
                       in {"running", "interrupted", "verifying"}]
        if interrupted:
            handoff = copy.deepcopy(result.handoff or {})
            handoff["execution_recovery"] = copy.deepcopy(interrupted[-1]["recovery_attempt"])
            return replace(result, status="needs_attention", error_kind="effect_unknown", handoff=handoff,
                command_receipts=prior, text=result.text + "\n上次执行条件修复未确认结束，需核对其工具回执后接续。")
    started = time.monotonic()
    initial = await _resolve_capability_requests(node, result, policy, **kwargs)
    if not recover_execution:
        return initial
    final = await recover_capability_execution(node, result, initial, policy,
        resolve=_resolve_capability_requests, elapsed=time.monotonic() - started, **kwargs)
    if final.error_kind == "capability_missing" and final.command_receipts:
        from mewcode.sandbox.execution_conditions import classify_execution_condition
        latest = {row.get("base_request_id") or row["request_id"]: row for row in final.command_receipts}
        conditions = [classify_execution_condition(row) for row in latest.values()
                      if not row.get("successful") and row.get("evidence_source") == "host_execution"]
        kinds = {item["kind"] for item in conditions}
        typed = ("effect_unknown" if "effect_unknown" in kinds else
                 "permission_denied" if "permission_denied" in kinds else
                 "environment_fault" if "environment_unavailable" in kinds else
                 "command_failed" if "execution_failed" in kinds else "")
        if typed:
            handoff = copy.deepcopy(final.handoff or {})
            handoff["execution_conditions"] = conditions
            final = replace(final, error_kind=typed, handoff=handoff)
    return final


async def resolve_file_requests(node, result, policy, **kwargs):
    """Backward-compatible entry point for existing integrations and tests."""
    return await resolve_capability_requests(node, result, policy, **kwargs)
