"""Bind task design to existing conversation, graph and execution services."""
from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

from fastapi import Request
from fastapi.responses import JSONResponse

from mewcode.orchestration.draft_store import DraftStore
from mewcode.orchestration.draft_sources import digest
from mewcode.orchestration.task_design import (
    TASK_BUILD_TRANSPORT_FAILURE_LIMIT, TaskBuildError, TaskDesign, design_view, execution_scope_error,
)
from mewcode.permissions import PermissionMode


class DesignModeChanged(ValueError):
    pass


class LocalRequest:
    def __init__(self, body, path=""):
        self.body = body
        self.url = SimpleNamespace(path=path)

    async def json(self):
        return self.body


def response_value(response):
    if isinstance(response, dict):
        return response
    data = json.loads(response.body)
    if response.status_code >= 400:
        raise ValueError(data.get("error") or "任务步骤未完成")
    return data


def install(app, srv):
    jobs = {}

    def design_busy(project, cid):
        job = jobs.get((project, cid))
        graph_running = getattr(srv, "graph_generation_running", lambda *_: False)
        return (bool(job and not job.done()) or graph_running(project, cid)
                or srv.SESSION.chat_busy(cid))

    def scope(body):
        project, cid = srv.SESSION.work_dir, srv._active_conversation_id()
        if (not cid or body.get("conversation_id") != cid or
                os.path.normcase(os.path.abspath(str(body.get("project") or ""))) !=
                os.path.normcase(os.path.abspath(project))):
            raise ValueError("项目或对话已切换，请回到任务所属对话。")
        return project, cid

    def require_design_mode(project, cid):
        scope({"project": project, "conversation_id": cid})
        if (not srv.SESSION.orchestration_planning or
                srv.SESSION.permission_mode == PermissionMode.PLAN):
            raise DesignModeChanged("已退出编排设计，规格已保留，未自动启动。")

    def pause_for_mode_change(project, cid):
        """Revoke pending design work without interrupting the ordinary chat."""
        current = design_view(DraftStore.read_state(project, cid) or {})
        if current and current.get("status") in {"designing", "queued", "building", "needs_input", "ready"}:
            TaskDesign(project, cid).control("pause", task_id=current["task_id"], revision=current["revision"])
        job = jobs.get((project, cid))
        if job:
            job.cancel()

    def recover_on_session_load(project, cid):
        """Cold-session lifecycle only; reads must never recover or start work."""
        from mewcode.orchestration.collab_service import bound_record
        current = design_view(DraftStore.read_state(project, cid) or {})
        if not current or design_busy(project, cid):
            return current
        design = TaskDesign(project, cid)
        if (current.get('orchestration_mode') == 'collaborative' and not current.get('run_id')
                and TaskDesign.execution_run_id(current)):
            run_id = TaskDesign.execution_run_id(current)
            existing = srv.EXPERT_RUNS.load(run_id)
            if existing:
                # Validate the conversation/task before recovering a saved claim.
                bound_record(srv.EXPERT_RUNS, {**current, 'run_id': run_id}, cid)
                current = design.bind_run(task_id=current['task_id'],
                    claim_id=current['build_request']['execution_claim_id'], run_id=run_id)
            else:
                design.record_execution_not_started(TaskDesign.execution_token(current),
                    '启动进程已结束且没有对应运行记录；保留规格，等待明确继续。', restart_recovery=True)
                current = design.read()
                if current.get('status') != 'paused':
                    current = design.control('pause', task_id=current['task_id'], revision=current['revision'],
                        reason='启动进程在创建运行前中断，尚未执行业务。')
        if current.get('orchestration_mode') == 'collaborative' and current.get('run_id'):
            bound = bound_record(srv.EXPERT_RUNS, current, cid)
            if (bound.get('status') == 'running' and not (bound.get('collaboration') or {}).get('attempts')
                    and not bound.get('active_node_ids')):
                srv.EXPERT_RUNS.transition(current['run_id'], 'paused', reason='启动绑定已恢复，等待明确继续。')
                current = design.transition(current, 'paused', '启动绑定已恢复，等待明确继续。')
            elif bound.get('status') == 'paused' and current.get('status') == 'running':
                current = design.transition(current, 'paused', bound.get('status_reason') or '运行已暂停，等待明确继续。')
        if (current.get('status') in {'queued', 'building'} or
                current.get('status') == 'designing' and current.get('goal')) and not current.get('run_id'):
            current = design.control('pause', task_id=current['task_id'], revision=current['revision'],
                reason=('对话已结束，设计进度已保存，可继续。' if current['status'] == 'designing'
                        else '构图进程已中断，原请求和规格已保留，可续接。'))
        return current

    @app.get("/api/orchestration/task-design")
    async def get_design(request: Request):
        try:
            project, cid = scope(dict(request.query_params))
            current = design_view(DraftStore.read_state(project, cid) or {})
            if current and current.get("orchestration_mode") == "collaborative" and current.get("run_id"):
                from mewcode.orchestration.collab_service import bound_projection
                current["collaboration"] = bound_projection(srv.EXPERT_RUNS, current, cid)
                if current["collaboration"].get("recovery_gates"):
                    from mewcode.kernel.models import RoleModels
                    catalog = await srv.MODEL_REGISTRY.catalog(local_only=True)
                    current["collaboration"]["recovery_models"] = [ref.as_dict() for ref in RoleModels(catalog).candidates("executor")]
                    current["collaboration"]["verification_models"] = [ref.as_dict() for ref in RoleModels(catalog).candidates("verifier")]
                    current["collaboration"]["coordinator_models"] = [ref.as_dict() for ref in RoleModels(catalog).candidates("coordinator")]
            return {"project": project, "conversation_id": cid,
                    "task_design": current}
        except (ValueError, OSError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    @app.post("/api/orchestration/task-design")
    async def change_design(request: Request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("任务请求必须是对象。")
            project, cid = scope(body)
            design = TaskDesign(project, cid)
            action = body.get("action")
            retry_node_id = body.get('retry_node_id', '')
            if not isinstance(retry_node_id, str) or (retry_node_id and action != 'resume'):
                raise ValueError('指定节点重试只能通过继续当前任务提交。')
            continuation = None
            if action == "start":
                if body.get("intent") not in {"plan", "execute"}:
                    raise ValueError("任务意图无效")
                if srv.SESSION.chat_busy():
                    raise ValueError("当前对话仍在运行，请完成或暂停后发起任务对话。")
                error = srv.SESSION.set_expert_mode(False) or srv.SESSION.set_orchestration_planning(True)
                if error:
                    raise ValueError(error)
                value = design.start(body["intent"], task_id=body.get("expected_task_id"),
                                     revision=body.get("expected_revision"), orchestration_mode=body.get("orchestration_mode"))
            elif action == "set_intent":
                current = design.read()
                if not current or (current["task_id"], current["revision"]) != (
                        body.get("expected_task_id"), body.get("expected_revision")):
                    raise ValueError("任务规格已变化，请重新读取；未采用旧决定。")
                if body.get("intent") != "plan":
                    raise ValueError("此入口仅支持修正为仅设计保存。")
                if current.get("status") == "running":
                    await srv.request_chat_interrupt(cid, action="pause")
                    current = design.read()
                    if current.get("status") == "running":
                        current = design.transition(current, "paused", "执行已暂停，正在修正任务范围。")
                value = design.set_intent("plan", task_id=current["task_id"], revision=current["revision"])
                job = jobs.get((project, cid))
                if job:
                    job.cancel()
                continuation = {"kind": "scope", "message": value["reason"]}
            else:
                current = design.read()
                if action in {"resume", "confirm_execute"} and srv.SESSION.chat_busy(cid):
                    raise ValueError("当前对话仍在运行，请等待完成或先暂停后再继续任务。")
                if action == "confirm_execute":
                    if body.get("confirm_execution") is not True:
                        raise ValueError("请明确确认设计并执行，未改变任务意图。")
                    if srv.SESSION.permission_mode == PermissionMode.PLAN:
                        raise ValueError("当前处于只读 Plan 模式，请先选择允许执行的权限模式。")
                    if (project, cid) in jobs or srv.SESSION.chat_busy():
                        raise ValueError("当前仍有设计工作运行，请暂停后再确认执行。")
                    value = design.confirm_execution(task_id=body.get("expected_task_id"),
                                                     revision=body.get("expected_revision"))
                elif current and current.get("status") == "running" and action == "pause":
                    if (current["task_id"], current["revision"]) != (body.get("expected_task_id"), body.get("expected_revision")):
                        raise ValueError("任务版本已变化，请刷新后重试。")
                    await srv.request_chat_interrupt(cid, action="pause")
                    latest = design.read()
                    value = latest if latest.get("status") == "completed" else design.transition(
                        latest, "paused", "任务已暂停，执行检查点已保留。")
                else:
                    value = design.control(action, task_id=body.get("expected_task_id"), revision=body.get("expected_revision"))
                if action == "pause":
                    job = jobs.get((project, cid))
                    if job:
                        job.cancel()
                    # Pause the design chat as well, including a pending question.
                    if srv.SESSION.chat_busy(cid):
                        await srv.request_chat_interrupt(cid, action="pause")
                else:
                    try:
                        request_record = value.get("build_request") or {}
                        if value.get("status") == "queued":
                            error = srv.SESSION.set_expert_mode(False) or srv.SESSION.set_orchestration_planning(True)
                            if error:
                                raise ValueError(error)
                            require_design_mode(project, cid)
                            schedule(project, cid, value)
                            continuation = {"kind": "build", "message": "已续接原构图请求，正在完成校验与保存。"}
                        elif value.get("intent") == "plan" and request_record.get("state") == "saved":
                            continuation = {"kind": "saved", "message": "编排图已保存，可查看阶段图。"}
                        elif value.get("intent") == "execute" and (value.get("run_id") or request_record.get("state") == "saved"):
                            scope_error = execution_scope_error(value)
                            if scope_error:
                                raise ValueError(scope_error)
                            plan = srv._conversation_plan_store(cid).get(project)
                            if not plan or not plan.spec:
                                raise ValueError("当前编排不存在，请在对话中核对后续接。")
                            value = design.transition(value, "ready", "正在续接已有编排检查点。")
                            if not value.get("run_id"):
                                value = design.claim_execution(value)
                            result = response_value(await srv.api_orchestration_confirm(LocalRequest({
                                "spec": plan.spec.as_dict(), "conversation_id": cid, "activate": True,
                                "run_inputs": execution_inputs(project, cid, value, plan.spec.as_dict()),
                                **({'retry_node_id': retry_node_id} if retry_node_id else {}),
                                "task_design_dispatch": design.execution_token(value)})))
                            if not result.get("started"):
                                reason = result.get("clarify_prompt") or "恢复条件尚未满足。"
                                if not value.get("run_id"):
                                    value = design.reject_execution(value, reason)
                                raise ValueError(reason)
                            execution = dict(value.get("build_request") or {})
                            if execution:
                                execution["execution_state"] = "started"
                            value = design.transition(value, "running", "正在续接已有编排检查点。",
                                                      **({"build_request": execution} if execution else {}))
                            continuation = {"kind": "execution", "message": "已继续执行已有编排。"}
                        else:
                            error = srv.SESSION.set_expert_mode(False) or srv.SESSION.set_orchestration_planning(True)
                            if error:
                                raise ValueError(error)
                            if value.get("goal"):
                                await continue_in_chat(project, cid, value,
                                    ("用户在任务卡明确重新确认了设计并执行；宿主已记录本次版本化选择。该选择只更新是否执行，其他约束和权限仍有效。"
                                     if action == "confirm_execute" else "用户点击了继续任务。") +
                                    "读取已有任务规格、素材和候选图，接着完成尚未完成的设计、校验与保存。"
                                    "复用已有成果；可调查的技术问题自行修复，只有必要的业务取舍才提问。"
                                    "收敛后使用最新版本调用 AdvanceTaskDesign(build)。保持已保存的任务意图和原有授权。",
                                    marker="task-design-resume")
                                continuation = {"kind": "design", "message": "已继续设计，主模型正在处理剩余步骤。"}
                            else:
                                continuation = {"kind": "input", "message": "任务已恢复，请在对话中填写目标。"}
                    except (ValueError, OSError, TypeError, RuntimeError) as exc:
                        try:
                            design.transition(value, "paused", str(exc))
                        except ValueError:
                            pass  # A newer user decision owns the state.
                        raise
            return {"project": project, "conversation_id": cid, "task_design": value,
                    "resumed": bool(continuation), "continuation": continuation}
        except (ValueError, OSError, TypeError, RuntimeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    @app.post("/api/orchestration/task-design/revision")
    async def revise_collaboration(request: Request):
        try:
            body = await request.json()
            project, cid = scope(body)
            design = TaskDesign(project, cid)
            current = design.read()
            if not current or current["task_id"] != body.get("expected_task_id"):
                raise ValueError("任务已变化，请刷新后重试")
            from mewcode.orchestration.collab_service import bound_record, user_revision
            from mewcode.orchestration.task_view import collaboration_projection as projection
            record = bound_record(srv.EXPERT_RUNS, current, cid)
            if record is None:
                raise ValueError("当前任务还没有绑定协作运行")
            if body.get('expected_run_id') and body['expected_run_id'] != record['run_id']:
                raise ValueError('任务运行已变化，请刷新后重试')
            updated = user_revision(srv.EXPERT_RUNS, record, entry_id=body.get("entry_id"), body=body.get("body"),
                expected_version=body.get("expected_version"), request_id=body.get("request_id"), field=body.get("field", "body"))
            revision_pending = any(contract.get("held") for contract in updated["collaboration"]["contracts"].values())
            if revision_pending and srv.SESSION.chat_busy(cid):
                await srv.request_chat_interrupt(cid, action="pause")
            latest = design.read()
            if revision_pending and latest and latest["task_id"] == current["task_id"] and latest.get("status") != "paused":
                design.transition(latest, "paused", "要求修订已保存，继续任务时核对并采用新规格。")
            return {"project": project, "conversation_id": cid, "collaboration": projection(updated)}
        except (ValueError, OSError, TypeError, RuntimeError) as exc:
            return JSONResponse({"error": "要求已被更新，请在最新版本上重新修改" if str(exc) == "提议基准已过期" else str(exc)}, status_code=409)

    @app.post("/api/orchestration/task-design/recovery")
    async def recover_collaboration(request: Request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("恢复请求必须是对象")
            project, cid = scope(body)
            design = TaskDesign(project, cid)
            current = design.read()
            if not current or current["task_id"] != body.get("expected_task_id"):
                raise ValueError("任务已变化，请刷新后重试")
            if srv.SESSION.chat_busy(cid):
                raise ValueError("当前运行仍在处理，请暂停后再决定恢复方式")
            from mewcode.orchestration.collab_service import bound_record, user_recovery
            from mewcode.orchestration.task_view import collaboration_projection as projection
            record = bound_record(srv.EXPERT_RUNS, current, cid)
            if record is None:
                raise ValueError("当前任务还没有绑定协作运行")
            catalog = (await srv.MODEL_REGISTRY.catalog(local_only=True)
                       if body.get("action") in {"switch_model", "switch_verifier", "switch_coordinator", "resume_coordinator"} else {})
            scope(body)
            latest = design.read()
            if not latest or latest["task_id"] != current["task_id"] or latest.get("run_id") != current.get("run_id"):
                raise ValueError("任务已变化，请刷新后重试")
            if srv.SESSION.chat_busy(cid):
                raise ValueError("当前运行仍在处理，请暂停后再决定恢复方式")
            record = bound_record(srv.EXPERT_RUNS, latest, cid)
            updated = user_recovery(srv.EXPERT_RUNS, record, gate_id=body.get("gate_id"),
                expected_version=body.get("expected_version"), request_id=body.get("request_id"),
                action=body.get("action"), reason=body.get("reason"), model=body.get("model"),
                budget_amount=body.get("budget_amount"), work_dir=project, catalog=catalog)
            latest = design.read()
            if latest and latest["task_id"] == current["task_id"] and latest.get("status") != "paused":
                design.transition(latest, "paused", updated.get("status_reason", "用户决定已保存"))
            return {"project": project, "conversation_id": cid, "collaboration": projection(updated)}
        except (ValueError, OSError, TypeError, RuntimeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    async def publish(project, cid, value):
        await srv.WS_EVENTS.send_json({"type": "task_design_state", "project": project,
                                      "conversation_id": cid, "task_design": value})

    async def continue_in_chat(project, cid, value, guidance, *, marker):
        """Resume the same main model without inventing another user message."""
        require_design_mode(project, cid)
        if srv.SESSION.chat_busy():
            raise ValueError("当前对话仍在运行，未重复启动设计。")
        from mewcode.orchestration.revisions import replace_plan_reminder
        runtime = srv.SESSION.capture_conversation_runtime()
        replace_plan_reminder(runtime.conversation, marker, guidance)
        ws = srv.ConversationWebSocket(srv.WS_EVENTS, cid)
        runtime.execution_kind = "chat"
        srv.SESSION.begin_conversation_turn(runtime)
        try:
            await ws.send_json({"type": "turn_started", "text": "", "attachments": [],
                                "queued": True, "expert_mode": False, "client_echoed": True})
            require_design_mode(project, cid)
            latest = TaskDesign(project, cid).read() or {}
            if (latest.get("task_id"), latest.get("revision")) != (value["task_id"], value["revision"]):
                raise ValueError("任务在续接前发生变化，未重复启动。")
            task = asyncio.create_task(srv.run_chat(ws, "", record_user=False, runtime=runtime))
            srv.SESSION.attach_conversation_task(runtime, task)
        except BaseException:
            runtime.starting = False
            # A failed handoff must also release the composer's running state.
            try:
                await ws.send_json({"type": "done", "rejected": True,
                                    "interrupted": False, "elapsed": 0})
            except Exception:
                pass
            raise

    async def repair_in_chat(project, cid, design, value, reason):
        request = value.get("build_request") or {}
        transport = request.get("failure_kind") == "transport" and request.get("retryable") is True
        limit_reached = (request.get("transport_failure_count", 0) >= TASK_BUILD_TRANSPORT_FAILURE_LIMIT
                         if transport else request.get("repair_attempts", 0) >= 2)
        if value.get("status") in {"failed", "blocked"} or limit_reached or srv.SESSION.chat_busy():
            return
        guidance = ("宿主记录本次构图或独立审查因可重试的服务错误中断，已完成的来源块与候选保留。"
                    "请读取最新规格、快照及诊断，保持当前业务要求和授权，使用最新版本重新提交构图；"
                    "不要为连接错误删改需求或重新处理已完成来源。\n" if transport else
                    "宿主构图检查未完成。请读取最新规格并修正以下问题，技术问题自行处理；"
                    "只有用户独有信息或真正业务取舍才使用 AskUserQuestion。修正后重新提交构图，"
                    "不得删掉需求或通过弱化校验掩盖问题。\n")
        await continue_in_chat(project, cid, value,
            guidance + reason[:18000], marker="task-design-build-feedback")

    def execution_inputs(project, cid, value, spec):
        from frontend.workbench_startup import execution_values
        return execution_values(srv, project, cid, value, spec)

    async def drive(project, cid):
        key = (project, cid)
        design = TaskDesign(project, cid)
        value = design.read()
        if not value or value.get("status") != "queued":
            return
        try:
            require_design_mode(project, cid)
            if srv.SESSION.chat_busy():
                return
            value = design.claim_build(value)
            expected = {**value["build_request"], "task_id": value["task_id"], "task_revision": value["revision"],
                        "build_id": value["build_request"]["id"], "build_epoch": value["build_request"]["epoch"]}
            await publish(project, cid, value)
            require_design_mode(project, cid)
            design.check_build(expected)
            # Same public services and version checks as the existing canvas UI.
            await srv.task_design_build_graph(project, cid, expected=expected)
            require_design_mode(project, cid)
            design.check_build(expected)
            if srv.SESSION.chat_busy():
                raise ValueError("对话有新的工作，候选图已保留，等待最新规格再保存。")
            await srv.task_design_check_build(project, cid, expected)
            require_design_mode(project, cid)
            design.check_build(expected)
            preview = await srv.task_design_preview_graph(project, cid, expected=expected)
            require_design_mode(project, cid)
            design.check_build(expected)
            # The apply service checks graph/source/candidate versions again.
            saved = await srv.task_design_apply_graph(project, cid, preview["id"], expected=expected)
            require_design_mode(project, cid)
            fresh = design.read() or {}
            if (fresh.get("build_request") or {}).get("state") == "saved":
                value = design.saved_build(expected)
            else:
                # Alternative hosts may keep their existing apply implementation;
                # persist the receipt with the same CAS after it returns.
                value = design.finish_build(expected, "ready", "编排图已保存。",
                    graph_version=(saved.get("spec", {}).get("meta") or {}).get("pipeline_graph_version"),
                    saved_spec_hash=digest(saved.get("spec")), preview_id=preview["id"])
            if value["intent"] == "execute":
                scope({"project": project, "conversation_id": cid})
                scope_error = execution_scope_error(value)
                if scope_error:
                    value = design.transition(value, "blocked", scope_error)
                    await publish(project, cid, value)
                    return
                value = design.claim_execution(value)
                result = response_value(await srv.api_orchestration_confirm(LocalRequest({
                    "spec": saved["spec"], "conversation_id": cid, "activate": True,
                    "run_inputs": execution_inputs(project, cid, value, saved['spec']),
                    "task_design_dispatch": design.execution_token(value)})))
                if not result.get("started"):
                    reason = result.get("clarify_prompt") or "启动条件尚未满足，请核对现有问题。"
                    value = design.reject_execution(value, reason)
                    await publish(project, cid, value)
                    return
                execution = dict(value["build_request"])
                execution["execution_state"] = "started"
                value = design.transition(value, "running", "按已保存编排执行，当前授权和运行检查继续生效。",
                                          build_request=execution)
            await publish(project, cid, value)
        except DesignModeChanged:
            pause_for_mode_change(project, cid)
        except asyncio.CancelledError:
            # The pause endpoint has already persisted the user's decision.
            return
        except (ValueError, OSError, TypeError, RuntimeError) as exc:
            try:
                if "expected" not in locals():
                    return  # Claim failed: another control/callback owns the request.
                fresh = design.read() or {}
                if (fresh.get("build_request") or {}).get("state") == "saved":
                    # Saving succeeded; a failed/uncertain execution handoff must
                    # never send the saved graph through generation again.
                    value = design.transition(value, "blocked", str(exc))
                else:
                    request_record = value.get("build_request") or {}
                    previous = request_record.get("previous_failure") or {}
                    same_basis = all(previous.get("basis", {}).get(k) == request_record.get(k, "")
                                     for k in ("sequence", "base_graph_hash", "user_source_hash", "requirement_fingerprint"))
                    repeated = same_basis and previous.get("fingerprint") == digest(str(exc))
                    repairs = previous.get("repair_attempts", 0) if same_basis else 0
                    transport_count = previous.get("transport_failure_count", 0) if same_basis else 0
                    if type(transport_count) is not int or transport_count < 0:
                        transport_count = 0
                    transport = isinstance(exc, TaskBuildError) and exc.retryable is True
                    if transport:
                        transport_count += 1
                        status = "failed" if transport_count >= TASK_BUILD_TRANSPORT_FAILURE_LIMIT else "needs_input"
                    else:
                        status = "failed" if repeated or repairs >= 2 else "needs_input"
                        repairs += 1
                    value = design.finish_build(expected, status, str(exc), failure_fingerprint=digest(str(exc)),
                        repair_attempts=repairs, failure_origin="host_build",
                        failure_kind="transport" if transport else "non_retryable", retryable=transport,
                        transport_failure_count=transport_count,
                        failure_evidence=exc.diagnostic if isinstance(exc, TaskBuildError) else {})
                await publish(project, cid, value)
                await repair_in_chat(project, cid, design, value, str(exc))
            except (ValueError, OSError, TypeError, RuntimeError):
                pass  # A newer turn/intent owns the state; never overwrite it.
        finally:
            if jobs.get(key) is asyncio.current_task():
                jobs.pop(key, None)

    def after_turn(runtime, *, interrupted=False, error=False):
        key = (runtime.work_dir, runtime.conversation_id)
        raw = DraftStore.read_state(*key) or {}
        value = design_view(raw)
        if not value:
            return
        design = TaskDesign(*key)
        if value.get("status") == "designing" and not value.get("run_id") and not design_busy(*key):
            design.control("pause", task_id=value["task_id"], revision=value["revision"],
                           reason="对话已停止，设计进度已保存，可继续。" if interrupted or error else
                                  "本轮对话已结束，设计进度已保存，可继续。")
            return
        if value.get("status") == "queued" and (interrupted or error):
            design.control("pause", task_id=value["task_id"], revision=value["revision"],
                           reason="对话已停止，原构图请求和规格已保存，未自动启动。")
            return
        if value.get("status") in {"running", "paused"} and getattr(runtime, "execution_kind", "") == "orchestration":
            # The runner's evidence-backed terminal state owns completion.
            run_id = getattr(runtime, "last_expert_run_id", "") or getattr(runtime, "expert_run_id", "")
            run = srv.EXPERT_RUNS.load(run_id) if run_id else None
            run_design = (((run or {}).get("locked_flow") or {}).get("meta") or {}).get("task_design") or {}
            if run and run.get("conversation_id") == key[1] and run_design.get("task_id") == value["task_id"]:
                status = "completed" if run.get("status") == "completed" else "paused"
                design.transition(value, status, "任务执行已结束。" if status == "completed" else "执行检查点已保存。", run_id=run_id)
            else:
                design.transition(value, "paused", "执行未完成，请在当前对话核对错误后续接。")
            return
        if value.get("status") == "queued":
            try:
                require_design_mode(*key)
            except ValueError:
                pause_for_mode_change(*key)
                return
            schedule(*key, value)

    def schedule(project, cid, value):
        key = (project, cid)
        request_record = value.get("build_request") or {}
        token = (value["task_id"], value["revision"], request_record.get("id"), request_record.get("epoch"))
        existing = jobs.get(key)
        if existing and not existing.done():
            if getattr(existing, "_task_design_token", None) == token:
                return
            if not existing.cancelling():
                existing.cancel()
        task = asyncio.create_task(drive(*key), name="task-design:" + cid)
        task._task_design_token = token
        jobs[key] = task
        def cleanup(done):
            if jobs.get(key) is done:
                jobs.pop(key, None)
        task.add_done_callback(cleanup)

    srv.task_design_after_turn = after_turn
    srv.pause_task_design_for_mode_change = pause_for_mode_change
    srv.recover_task_design_on_session_load = recover_on_session_load
    from frontend.task_restart import install as install_restart
    install_restart(app, srv, scope)
    from frontend.task_models import install as install_models
    install_models(app, srv, scope)
