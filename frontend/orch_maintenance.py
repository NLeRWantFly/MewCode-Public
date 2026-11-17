"""Conversation-owned maintenance endpoints, independent of business turns."""
from __future__ import annotations

import asyncio
import copy
import inspect
import json
import logging
import sqlite3
import time
from dataclasses import replace
from types import SimpleNamespace

from fastapi import Request
from fastapi.responses import JSONResponse

from mewcode.orchestration.consistent_save import recover_pending_save, save_consistently
from mewcode.orchestration.convert import to_expert_flow
from mewcode.orchestration.draft_sources import digest
from mewcode.orchestration.extract import collect_model_text
from mewcode.orchestration.maintenance import (
    MaintenanceStore, now, project_key, resolve_targets,
)
from mewcode.orchestration.maintenance_revision import (
    apply_run_actions, apply_spec_run_actions, changed_execution_nodes, future_revision,
)
from mewcode.orchestration.maintenance_agent import MaintenanceTurn, build_fix_agent, build_maintenance_agent
from mewcode.orchestration.spec import OrchestrationSpec
from mewcode.orchestration.validate import validate_spec
from mewcode.runtime_scheduler import bind_runtime_scheduler, reset_runtime_scheduler


def install(app, srv):
    jobs, progress, maintenance_turns = {}, {}, {}
    preflight_cache = {}

    def pending_request(identity):
        turn = maintenance_turns.get(identity)
        if turn and turn.pending is not None and not turn.pending.future.done():
            return copy.deepcopy(turn.info.get('pending'))
        return None
    srv.workbench_maintenance_pending = pending_request

    def workbench_owner(project, cid):
        key = scope(dict(project=project, conversation_id=cid))
        spec, _ = current(key)
        return spec.spec_id, (run_for(key, spec) or {}).get('run_id', '')
    srv.workbench_maintenance_owner = workbench_owner

    def ledger():
        value = getattr(srv, "MAINTENANCE_STORE", None)
        if value is None:
            value = srv.MAINTENANCE_STORE = MaintenanceStore()
        return value

    def scope(body):
        project, cid = srv.SESSION.work_dir, srv._active_conversation_id()
        if (not isinstance(body, dict) or not cid or body.get("conversation_id") != cid
                or not isinstance(body.get("project"), str) or project_key(body["project"]) != project_key(project)):
            raise ValueError("项目或对话已切换，请重新打开当前编排的维护面板。")
        durable = getattr(srv.SESSION, "session", None)
        if durable is None or getattr(durable, "session_id", "") != cid:
            raise ValueError("当前对话的持久化身份未就绪。")
        return project, cid

    def current(key):
        recover_pending_save(key[0], plan_store=srv._conversation_plan_store(key[1]),
                             flow_store=srv._conversation_flow_store(key[1]))
        spec, previous = srv._orchestration_spec_for_project()
        if str(spec.meta.get("conversation_id") or "") != key[1]:
            raise ValueError("当前编排缺少可核对的对话归属。")
        return spec, previous

    def owned_maintenance_spec(key, state):
        """Complete a background maintenance turn in its owner after navigation."""
        plans, flows = srv._conversation_plan_store(key[1]), srv._conversation_flow_store(key[1])
        recover_pending_save(key[0], plan_store=plans, flow_store=flows)
        saved = plans.get(key[0])
        if saved is not None and saved.spec is not None:
            spec = saved.spec
        else:
            from mewcode.orchestration.pipeline_bridge import from_pipeline_graph
            from mewcode.pipeline.store import PipelineStore
            graph = PipelineStore(key[0]).get_pipeline(state["base"].get("meta", {}).get("pipeline_graph_id", ""))
            if graph is None:
                raise ValueError("原编排已经移除，本次调查记录仍保留。")
            spec = from_pipeline_graph(graph)
        if spec.meta.get("conversation_id") != key[1] or spec.spec_id != state["base"]["spec_id"]:
            raise ValueError("原编排归属已变化，维护结果未应用。")
        return spec

    def runtime(key):
        existing = srv.SESSION.runtime_for(key[1])
        if existing is not None:
            return existing
        # After reopening a stopped conversation there may be no live runner.
        # Bind only the verified active durable session and its host agent.
        return SimpleNamespace(owner=srv.SESSION, conversation_id=key[1], work_dir=key[0],
            session=srv.SESSION.session, agent=getattr(srv.SESSION, "agent", None))

    def run_for(key, spec):
        from mewcode.orchestration.chat_routing import dialogue_focus
        focus = dialogue_focus(runtime(key))
        identity = str(focus.get("run_id") or "") if focus.get("kind") == "orchestration" else ""
        snapshot = srv.EXPERT_RUNS.load(identity) if identity else None
        if snapshot and (snapshot.get("conversation_id") != key[1]
                or (snapshot.get("locked_flow") or {}).get("flow_id") != spec.spec_id):
            raise ValueError("当前运行不属于本对话的这份编排。")
        return snapshot

    def facts(key, spec):
        from mewcode.kernel.repairs import current_blocker_actions
        from dataclasses import asdict
        from mewcode.orchestration.graph_models import available_model_routes
        from mewcode.orchestration.graph_preflight import graph_preflight_findings
        snapshot = run_for(key, spec)
        checker = getattr(getattr(runtime(key), "agent", None), "permission_checker", None)
        expected = (snapshot or {}).get("host_execution_scope") or {}
        from mewcode.sandbox.task_identity import require_task_identity
        task_error = ""
        try:
            if snapshot:
                require_task_identity(expected.get("task_id") or getattr(checker, "task_id", None))
                if expected.get("task_id") and getattr(checker, "task_id", None) != expected["task_id"]:
                    task_error = "执行器当前任务身份与原运行不一致，请暂停后检修执行环境。"
        except ValueError as exc:
            task_error = str(exc)
        try:
            warnings = validate_spec(spec, require_bindings=False)
            graph_error = ""
        except ValueError as exc:
            graph_error, warnings = str(exc), []
        from mewcode.expert_group import ExpertProfile
        preflight_findings = []
        try:
            profile = (ExpertProfile.from_value(snapshot['locked_profile']) if snapshot and snapshot.get('locked_profile')
                       else srv.EXPERT_PROFILES.get(key[0]))
            signature = digest({'spec': spec.as_dict(), 'profile': profile.as_dict() if profile else None})
            cached = preflight_cache.get(key)
            if cached and cached['signature'] == signature and time.monotonic() - cached['at'] < 5:
                preflight_findings = copy.deepcopy(cached['findings'])
            else:
                preflight_findings = [asdict(f) for f in graph_preflight_findings(
                    to_expert_flow(spec), srv.MODEL_REGISTRY, profile=profile)]
                preflight_cache[key] = {'signature': signature, 'at': time.monotonic(),
                                        'findings': copy.deepcopy(preflight_findings)}
        except (ValueError, KeyError) as exc:
            graph_error = graph_error or str(exc)
        from mewcode.sandbox.native_recovery import diagnose_native_runtime
        from mewcode.sandbox.project_python import inspect_project_python
        from mewcode.sandbox.transaction_registry import workspace_write_block
        from pathlib import Path
        states = (snapshot or {}).get("node_states") or (snapshot or {}).get("stages") or {}
        return {"available_model_routes": available_model_routes(srv.MODEL_REGISTRY),
            "run_id": (snapshot or {}).get("run_id", ""), "status": (snapshot or {}).get("status", "未运行"),
            "stage_cursor": (snapshot or {}).get("stage_cursor", ""),
            "status_reason": str((snapshot or {}).get("status_reason") or "")[:4000],
            "graph_error": graph_error, "warnings": warnings, "task_identity_error": task_error,
            "preflight_findings": preflight_findings,
            'current_blocker_actions': [action for node_id in states for action in current_blocker_actions(snapshot, node_id)],
            "host_execution_scope": copy.deepcopy(expected), "bound_task_id": getattr(checker, "task_id", ""),
            "native_runtime": diagnose_native_runtime(), "project_python": inspect_project_python(key[0]),
            "workspace_blocker": workspace_write_block(Path(key[0])),
            "node_statuses": {identity: {field: state.get(field) for field in ("status", "passed", "status_reason")}
                              for identity, state in states.items() if isinstance(state, dict)}}

    def public(key, state):
        spec, _ = current(key)
        result = ledger().public(state)
        agent = result.get("agent")
        if agent and agent.get("task_id"):
            turn = maintenance_turns.get(state["id"])
            manager = turn.manager if turn else getattr(srv.SESSION, "task_manager", None)
            task = manager.get(agent["task_id"]) if manager else None
            if task:
                agent.update(task_status=task.status, cancellation_pending=task.cancellation_pending,
                    cleanup_failed=task.lifecycle_finalized and not task.cleanup_confirmed)
                if task.status != "running":
                    agent.update(status=task.status, pending=None)
        result.update(stale=state["base_hash"] != digest(spec.as_dict()), diagnostics=facts(key, spec),
            progress=copy.deepcopy(progress.get(state["id"], {})),
            business_busy=srv.SESSION.chat_busy(key[1]),
            nodes=[{"id": n.node_id, "label": n.label} for n in spec.nodes])
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    async def body_of(request):
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("请求必须为对象。")
        return body

    def error(exc):
        return JSONResponse({"error": str(exc)}, status_code=409)

    def repair_host_runtime(key, spec, snapshot, maintenance_id):
        receipt, backup = None, None
        scope_fields = ('run_id', 'conversation_id', 'task_id', 'permission_scope_bound', 'flow_fingerprint')
        before_scope = {field: (snapshot or {}).get('host_execution_scope', {}).get(field) for field in scope_fields}
        if snapshot:
            if snapshot.get("status") == "running" or snapshot.get("active_node_ids"):
                raise ValueError("原编排仍在执行，请先暂停。")
            backup = srv.EXPERT_RUNS.checkpoint_archive.save(snapshot,
                turn_key="before-runtime-maintenance:" + maintenance_id, kind="before_runtime_maintenance")
            from mewcode.orchestration.execution_scope import prepare_orchestration_scope
            receipt = prepare_orchestration_scope(runtime(key), task=str(snapshot.get("task") or spec.goal),
                run_id=snapshot["run_id"], checkpoint=snapshot)
        # This fixed host operation uses the existing opt-in, not model-written
        # commands or a new permission grant.
        from mewcode.sandbox.native_recovery import recover_native_runtime
        report = recover_native_runtime()
        if snapshot:
            receipt_reconciliation = {"corrected": 0, "unresolved": []}
            def bind(value):
                value["host_execution_scope"] = receipt
                from mewcode.experts.command_receipts import reconcile_run_command_receipts
                receipt_fields = {node_id: "command_receipts" in stage
                    for node_id, stage in (value.get("node_states") or {}).items() if isinstance(stage, dict)}
                receipt_reconciliation.update(reconcile_run_command_receipts(value))
                # Reconciliation may visit nodes with no command evidence. Do
                # not create empty receipt projections on completed stages.
                for node_id, stage in (value.get("node_states") or {}).items():
                    if (isinstance(stage, dict) and not receipt_fields.get(node_id, False)
                            and not stage.get("command_receipts")):
                        stage.pop("command_receipts", None)
            snapshot = srv.EXPERT_RUNS.mutate_stopped(snapshot["run_id"], bind, conversation_id=key[1],
                                                      expected_updated_at=snapshot.get("updated_at"))
            bound = receipt["permission_scope_bound"]
            receipt_blockers = receipt_reconciliation["unresolved"]
            native_ready = report.get("ready") is True
            report = {**report, "native_ready": native_ready,
                "changed": (report.get('changed') is True or receipt_reconciliation['corrected'] > 0
                            or before_scope != {field: receipt.get(field) for field in scope_fields}),
                "task_identity_restored": bound,
                "command_receipt_blockers": receipt_blockers,
                "command_receipt_reconciliation": receipt_reconciliation,
                "ready": native_ready and bound and not receipt_blockers,
                "checkpoint_id": backup["checkpoint_id"]}
            if not bound:
                report["reason"] = "当前执行器的任务权限检查器尚未就绪；原任务身份已保存，执行保持暂停。"
            elif receipt_blockers:
                counts = {}
                for item in receipt_blockers:
                    counts[item["node_id"]] = counts.get(item["node_id"], 0) + 1
                details = "、".join(f"{node_id}（{count} 条）" for node_id, count in counts.items())
                report["reason"] = "宿主环境已就绪，但原运行仍有未解决命令回执，执行保持暂停：" + details
            from mewcode.sandbox.project_python import inspect_project_python
            python_environment = inspect_project_python(key[0])
            report["project_python"] = python_environment
            current_states = snapshot.get("node_states") or snapshot.get("stages") or {}
            missing_python = any(
                row.get("error_code") == "python_runtime_missing" and row.get("command_started") is False
                for state in current_states.values() if isinstance(state, dict)
                and state.get("status") not in {"completed", "passed", "skipped"}
                for row in (state.get("command_receipts") or []) if isinstance(row, dict)
            )
            if missing_python and not python_environment.get("available"):
                report.update(ready=False, project_environment_blocked=True)
                report["reason"] = (str(report.get("reason") or "") +
                    " 项目 Python 环境仍缺失或不可用；请在已授权维护范围内准备环境并验证依赖，"
                    "宿主边界就绪不代表原探测已完成。执行保持暂停。").strip()
        return snapshot, report

    @app.post("/api/orchestration/maintenance/open")
    async def open_panel(request: Request):
        try:
            body = await body_of(request)
            key = scope(body)
            spec, _ = current(key)
            snapshot = run_for(key, spec)
            ids, message = resolve_targets(spec, body.get("mode"), body.get("request", ""),
                body.get("node_ids"), (snapshot or {}).get("stage_cursor", ""))
            state = (ledger().get(*key, body["id"]) if body.get("id") else
                ledger().open(*key, spec.as_dict(), mode=body.get("mode"), node_ids=ids,
                    run_id=(snapshot or {}).get("run_id", ""), fresh=body.get("fresh") is True))
            if state["status"] == "running" and state["id"] not in jobs:
                state = ledger().fail(*key, state["id"], call_id=state.get("call_id"),
                                      error="上次维护请求已中断，消息保留，可重新发送。")
            response = public(key, state)
            data = json.loads(response.body)
            data["initial_message"] = message
            return JSONResponse(data)
        except (ValueError, TypeError, KeyError, OSError) as exc:
            return error(exc)

    @app.get("/api/orchestration/maintenance/state")
    async def panel_state(request: Request):
        try:
            body = dict(request.query_params)
            key = scope(body)
            return public(key, ledger().get(*key, body.get("id")))
        except (ValueError, TypeError, KeyError, OSError) as exc:
            return error(exc)

    @app.post("/api/orchestration/maintenance/send")
    async def send(request: Request):
        try:
            body = await body_of(request)
            key = scope(body)
            spec, _ = current(key)
            identity = body.get("id")
            if identity in jobs and not jobs[identity].done():
                raise ValueError("维护 AI 仍在处理，请等待或停止。")
            context = srv._orchestration_attempt_context()
            state = ledger().begin(*key, identity, revision=body.get("revision"),
                                   message=body.get("message"), base=spec.as_dict())
            progress[identity] = {"model": context["model"], "effort": context.get("requested_effort"),
                                  "started_at": now(),
                                  "detail": "维护 AI 正在调查" if state["mode"] == "fix" else "维护 AI 正在更新编排"}
            async def converse():
                token = bind_runtime_scheduler(srv.APP_RUNTIME_SCHEDULER)
                client = None
                turn = None

                try:
                    effort = context.get("requested_effort", "")
                    client = srv._new_orchestration_client(context, "" if effort == "auto" else effort)
                    scope(body)
                    builder = build_fix_agent if state["mode"] == "fix" else build_maintenance_agent
                    agent, conversation = builder(host=srv.SESSION, provider_context=context,
                        client=client, state=state, snapshot=run_for(key, spec), diagnostics=facts(key, spec))
                    turn = MaintenanceTurn(ledger(), state, agent, conversation)
                    agent._maintenance_turn = turn
                    turn.manager = srv.SESSION.task_manager
                    maintenance_turns[identity] = turn
                    task_name = turn.name
                    try:
                        task_id = turn.manager.launch(agent, state["messages"][-1]["text"], name=task_name,
                            owner_session_id=key[1], owner_store_dir=srv.SESSION._conversation_store_dir(),
                            owner_scope=getattr(srv.SESSION, "scope", "project"),
                            owner_task_id="maintenance:" + identity, owner_task_title=task_name,
                            origin="maintenance", runner=turn.run)
                    except BaseException:
                        await agent._cordis_runtime.aclose()
                        raise
                    turn.info.update(task_id=task_id, model=context["model"], effort=effort or "auto")
                    turn.save()
                    await turn.manager.settle_tasks([task_id])
                    task = turn.manager.get(task_id)
                    if task.status != "completed":
                        raise ValueError("本次维护已停止。" if task.status == "cancelled" else task.result)
                    raw = task.result
                    if state["mode"] == "fix":
                        latest = owned_maintenance_spec(key, state)
                    else:
                        scope(body)
                        latest, _ = current(key)
                    clean = raw.strip()
                    if clean.startswith("```") and clean.endswith("```"):
                        clean = clean[3:-3].removeprefix("json").strip()
                    response = json.loads(clean)
                    from mewcode.orchestration.graph_models import available_model_routes
                    ledger().complete(*key, identity, call_id=state["call_id"],
                                      response=response, base=latest.as_dict(), run_snapshot=run_for(key, latest),
                                      model_routes=available_model_routes(srv.MODEL_REGISTRY))
                    if turn is not None:
                        task.result = response["reply"]
                        turn.manager.mark_notified([task.id])
                except asyncio.CancelledError:
                    if turn is not None and turn.info.get("task_id"):
                        await turn.manager.settle_tasks([turn.info["task_id"]], cancel=True)
                    ledger().fail(*key, identity, call_id=state["call_id"], error="这次维护 AI 已停止，业务运行和图保持原样。")
                except Exception as exc:
                    ledger().fail(*key, identity, call_id=state["call_id"], error="维护未完成：" + str(exc))
                finally:
                    reset_runtime_scheduler(token)
                    try:
                        if client is not None and callable(getattr(client, "aclose", None)):
                            closing = client.aclose()
                            if inspect.isawaitable(closing):
                                await closing
                    except Exception:
                        logging.getLogger(__name__).exception("Maintenance client cleanup failed")
                    finally:
                        maintenance_turns.pop(identity, None)
                        progress.pop(identity, None)
                        jobs.pop(identity, None)
            jobs[identity] = asyncio.create_task(converse())
            return public(key, state)
        except (ValueError, TypeError, KeyError, OSError) as exc:
            return error(exc)

    @app.post("/api/orchestration/maintenance/stop")
    async def stop(request: Request):
        try:
            body = await body_of(request)
            key = scope(body)
            state = ledger().get(*key, body.get("id"))
            if type(body.get("revision")) is not int or body["revision"] != state["revision"]:
                raise ValueError("维护会话已变化，请刷新后再停止。")
            task = jobs.get(state["id"])
            turn = maintenance_turns.get(state["id"])
            if turn is not None and turn.info.get("task_id"):
                await turn.manager.settle_tasks([turn.info["task_id"]], cancel=True, timeout=1)
                return public(key, ledger().get(*key, state["id"]))
            elif task:
                task.cancel()
            state = ledger().fail(*key, state["id"], call_id=state.get("call_id"), error="这次维护 AI 已停止。")
            return public(key, state)
        except (ValueError, TypeError, KeyError, OSError) as exc:
            return error(exc)

    @app.post("/api/orchestration/maintenance/answer")
    async def answer(request: Request):
        try:
            body = await body_of(request)
            key = scope(body)
            state = ledger().get(*key, body.get("id"))
            turn = maintenance_turns.get(state["id"])
            if turn is None or state.get("call_id") != turn.state["call_id"] or state["status"] != "running":
                raise ValueError("维护已停止或重启，当前没有待回答的问题。")
            turn.answer(body)
            return public(key, ledger().get(*key, state["id"]))
        except (ValueError, TypeError, KeyError, OSError) as exc:
            return error(exc)

    @app.post("/api/orchestration/maintenance/apply")
    async def apply(request: Request):
        try:
            body = await body_of(request)
            key = scope(body)
            state = ledger().get(*key, body.get("id"))
            before, previous = current(key)
            proposal = state.get("proposal")
            if (type(body.get("revision")) is not int or state["revision"] != body["revision"]
                    or state["status"] != "ready" or not proposal or proposal["id"] != body.get("proposal_id")
                    or state["base_hash"] != digest(before.as_dict())):
                raise ValueError("差异或图版本已变化，请重新打开；未应用修订。")
            if srv.SESSION.chat_busy(key[1]):
                raise ValueError("请先暂停业务运行再应用修订，维护讨论可以独立继续。")
            snapshot = run_for(key, before)
            if state["run_id"] != (snapshot or {}).get("run_id", ""):
                raise ValueError("维护预览所属运行已变化，请按当前运行重新打开；未应用到其他任务。")
            if snapshot and snapshot.get("status") == "running":
                raise ValueError("原编排仍在执行，请先暂停再应用。")
            actions = proposal.get("run_actions") or []
            confirmed_ids = body.get("confirmed_run_action_ids", [])
            from mewcode.orchestration.maintenance import confirmed_run_action_ids
            selected_ids = confirmed_run_action_ids(actions, confirmed_ids)
            selected_actions = [action for action in actions if not action.get("requires_confirmation")
                                or action.get("id") in selected_ids]
            runtime_action_selected = any(action.get("action") in {"set_run_inputs", "rebase_node", "resolve_receipts", "close_case", "extend_rounds", "bump_epoch"}
                                          for action in selected_actions)
            if any(action.get("action") in {"set_run_inputs", "rebase_node", "resolve_receipts", "close_case", "extend_rounds", "bump_epoch"}
                   for action in selected_actions) and snapshot is None:
                raise ValueError("此运行动作需要当前暂停中的原运行；未应用修订。")
            candidate = apply_spec_run_actions(OrchestrationSpec.from_value(proposal["spec"]),
                                               actions, confirmed_ids)
            graph = srv._stored_orchestration_graph(srv.PipelineStore(key[0]))
            if graph is None or str(candidate.meta.get("pipeline_graph_id") or graph.graph_id) != graph.graph_id:
                raise ValueError("当前画布已变化或不存在，请先保存当前编排图。")
            if before.meta.get("pipeline_graph_version", before.version) != graph.version:
                raise ValueError("画布与方案版本不一致，请重新打开画布同步当前版本后再维护；未覆盖画布。")
            graph_action_selected = any(action.get("action") in {"set_node_tools", "set_known_inputs"}
                                         and action.get("id") in selected_ids for action in actions)
            graph_changed = bool(proposal.get("graph_changed", True) or graph_action_selected)
            from mewcode.orchestration.graph_models import available_model_routes
            from mewcode.orchestration.maintenance_models import validate_changed_models
            validate_changed_models(before, candidate, available_model_routes(srv.MODEL_REGISTRY))
            run_action_result = None
            if snapshot and runtime_action_selected:
                # Preflight all host-owned effects on an isolated snapshot before saving any graph.
                run_action_result = apply_run_actions(snapshot, actions, confirmed_ids,
                                                      maintenance_id=state["id"])
            run_candidate = (run_action_result or {}).get("snapshot", snapshot)
            affected = set(changed_execution_nodes(to_expert_flow(before), to_expert_flow(candidate)))
            for action in selected_actions:
                if action.get("action") == "set_run_inputs":
                    affected.update(node.node_id for node in candidate.nodes)
                elif action.get("node_id"):
                    affected.add(action["node_id"])
            from mewcode.expert_runs import successors_after_node
            for identity in tuple(affected):
                affected.update(successors_after_node(to_expert_flow(candidate).as_dict(), identity))
            if graph_changed:
                # User applies a concrete preview. No maintenance prose enters business history.
                from mewcode.orchestration.spec import SpecAction
                version = max(before.version + 1, graph.version + 1)
                bindings = copy.deepcopy(candidate.meta.get("maintenance_run_bindings") or {})
                if state["run_id"]:
                    bindings[state["run_id"]] = {"maintenance_id": state["id"],
                                                  "affected_node_ids": sorted(affected)}
                candidate = replace(candidate, version=version, actions=(*candidate.actions, SpecAction(
                    action_id="maintenance:" + state["id"], revision=version, kind=state["mode"],
                    payload={"maintenance_id": state["id"], "focus_node_ids": state["node_ids"] if state["mode"] == "fix" else [],
                             "node_ids": sorted(affected)},
                    actor="user", reason=proposal["summary"], created_at=now())),
                    meta={**candidate.meta, **({"maintenance_run_bindings": bindings} if bindings else {})})
                graph, candidate, flow, plan = save_consistently(key[0], spec=candidate, graph=graph,
                    pipeline_store=srv.PipelineStore(key[0]), plan_store=srv._conversation_plan_store(key[1]),
                    flow_store=srv._conversation_flow_store(key[1]), previous=previous,
                    note="/" + state["mode"] + "：" + proposal["summary"])
            else:
                from mewcode.orchestration.plan_session import build_plan_session
                candidate, flow, plan = before, to_expert_flow(before), previous or build_plan_session(before)
            result = {"graph_id": graph.graph_id, "spec": candidate.as_dict(), "flow": flow.as_dict(),
                      "control_plan": plan.control_plan.as_dict(), "run_notice": "", "run_adopted": False,
                      "graph_changed": graph_changed}
            if graph_changed and snapshot is None and srv.EXPERT_RUNS.latest_resumable(key[1]):
                result['run_notice'] = '当前图修订已保存；本次未关联旧运行，原检查点未替换。'
            if run_action_result:
                result["run_action_outcomes"] = run_action_result["outcomes"]
            if proposal.get("runtime_actions"):
                try:
                    snapshot, report = repair_host_runtime(key, before, snapshot, state["id"])
                    result.update(runtime_repair=report, runtime_repaired=report.get("ready") is True and report.get('changed') is True,
                        run_notice="未改变任何东西；当前阻塞仍待处理。" if report.get('ready') and not report.get('changed') else
                                   "宿主检修完成；原运行保持暂停。" if report.get("ready") else
                                   "宿主检修后仍有阻塞：" + str(report.get("reason") or report.get("action") or "请查看宿主诊断"))
                    if snapshot:
                        result["run_snapshot"] = srv.EXPERT_RUNS._public_value(snapshot, include_details=False)
                    if not report.get("ready"):
                        result["warning"] = result["run_notice"]
                except (ValueError, KeyError, OSError) as exc:
                    if not graph_changed:
                        raise
                    result["warning"] = "新图已保存，宿主检修未完成，原运行未自动启动：" + str(exc)
                    # Reload the exact run; a partial repair must never be
                    # mistaken for the pre-repair CAS snapshot below.
                    snapshot = srv.EXPERT_RUNS.load(state["run_id"]) if state["run_id"] else None
            if proposal.get("runtime_actions") and snapshot and runtime_action_selected:
                # Host repair persisted first; reapply the already-preflighted
                # candidate actions to its fresh stopped snapshot to preserve
                # the restored identity and receipt evidence.
                run_action_result = apply_run_actions(snapshot, actions, confirmed_ids,
                                                      maintenance_id=state["id"])
                run_candidate = run_action_result["snapshot"]
                result["run_action_outcomes"] = run_action_result["outcomes"]
            if graph_changed and snapshot and snapshot.get("run_id") == state["run_id"]:
                try:
                    revision = future_revision(run_candidate, candidate, affected=sorted(affected),
                        maintenance_id=state["id"], base_spec=before,
                        direction=candidate.goal if candidate.goal != before.goal else "",
                        rebased_node_ids=(run_action_result or {}).get("rebased_node_ids", ()))
                    backup = srv.EXPERT_RUNS.checkpoint_archive.save(snapshot,
                        turn_key="before-maintenance:" + state["id"], kind="before_maintenance")
                    revision["maintenance_adoptions"][-1]["checkpoint_id"] = backup["checkpoint_id"]
                    def adopt(value):
                        if value.get("updated_at") != snapshot.get("updated_at"):
                            raise ValueError("原运行状态已变化，图已保存，原运行未改动。")
                        value.clear()
                        value.update(revision)
                    changed_run = srv.EXPERT_RUNS.mutate_stopped(state["run_id"], adopt, conversation_id=key[1],
                                                expected_updated_at=snapshot.get("updated_at"))
                    result.update(run_adopted=True, run_notice="原运行已接纳未来阶段修订，仍保持暂停。" + result["run_notice"])
                    result["run_snapshot"] = srv.EXPERT_RUNS._public_value(changed_run, include_details=False)
                except (ValueError, KeyError, OSError) as exc:
                    result["run_notice"] = "新图已保存；原运行未接纳图修订，原有进度与回执保留：" + str(exc)
                    if run_candidate is not None and runtime_action_selected:
                        # Chosen host actions remain separately auditable while graph adoption awaits a choice.
                        run_candidate.setdefault("maintenance_run_actions", []).append({
                            "maintenance_id": state["id"], "outcomes": copy.deepcopy((run_action_result or {}).get("outcomes", []))})
                        backup = srv.EXPERT_RUNS.checkpoint_archive.save(snapshot,
                            turn_key="before-maintenance-actions:" + state["id"], kind="before_maintenance")
                        run_candidate.setdefault("maintenance_checkpoints", []).append(backup["checkpoint_id"])
                        changed_run = srv.EXPERT_RUNS.mutate_stopped(state["run_id"], lambda value: (value.clear(), value.update(run_candidate)),
                            conversation_id=key[1], expected_updated_at=snapshot.get("updated_at"))
                        result["run_snapshot"] = srv.EXPERT_RUNS._public_value(changed_run, include_details=False)
                        result["run_actions_applied"] = True
                    from mewcode.orchestration.maintenance_revision import require_adopted_maintenance
                    adoption = require_adopted_maintenance(snapshot, flow)
                    if adoption.get("status") == "choice_required":
                        result["run_adoption"] = adoption
            elif not graph_changed and snapshot and run_candidate is not None and runtime_action_selected:
                if run_candidate != snapshot:
                    run_candidate.setdefault("maintenance_run_actions", []).append({
                        "maintenance_id": state["id"], "outcomes": copy.deepcopy((run_action_result or {}).get("outcomes", []))})
                    backup = srv.EXPERT_RUNS.checkpoint_archive.save(snapshot,
                        turn_key="before-maintenance-actions:" + state["id"], kind="before_maintenance")
                    run_candidate.setdefault("maintenance_checkpoints", []).append(backup["checkpoint_id"])
                    changed_run = srv.EXPERT_RUNS.mutate_stopped(state["run_id"], lambda value: (value.clear(), value.update(run_candidate)),
                        conversation_id=key[1], expected_updated_at=snapshot.get("updated_at"))
                    result.update(run_actions_applied=True, run_adopted=True,
                        run_notice="已按所选动作更新暂停中的原运行；运行仍保持暂停。")
                    result["run_snapshot"] = srv.EXPERT_RUNS._public_value(changed_run, include_details=False)
            def finish(value):
                blocked = not graph_changed and not result.get("runtime_repaired") and not result.get("run_actions_applied")
                value.update(status="no_effect" if blocked else "applied", applied_at="" if blocked else now(),
                    applied_version=candidate.version, run_notice=result["run_notice"],
                    error=result.get("warning", ""), runtime_repair=result.get("runtime_repair"),
                    run_adoption=copy.deepcopy(result.get("run_adoption")))
            try:
                changed = ledger().mutate(*key, state["id"], finish)
            except (OSError, sqlite3.Error) as exc:
                changed = copy.deepcopy(state)
                finish(changed)
                result["warning"] = ("编排图已保存" if graph_changed else "宿主检修已处理") + "，维护会话记录写回失败；请重新打开核对当前版本和诊断：" + str(exc)
            if changed.get('status') == 'no_effect' and snapshot:
                from mewcode.kernel.escalation import decide_and_record
                from mewcode.experts.node_ledger import ledger_for_run
                def escalate_unchanged(value):
                    for node_id in value.get('node_states', {}):
                        if ledger_for_run(value).view(node_id).active_blockers:
                            decide_and_record(value, node_id, trigger='fix_no_effect',
                                identity='fix_no_effect:' + state['id'] + ':' + node_id)
                latest_unchanged = srv.EXPERT_RUNS.load(snapshot['run_id'])
                srv.EXPERT_RUNS.mutate_stopped(snapshot['run_id'], escalate_unchanged, conversation_id=key[1],
                    expected_updated_at=latest_unchanged['updated_at'])
            try:
                await srv.WS_EVENTS.send_json({"type": "orchestration_saved", "project": key[0],
                                              "conversation_id": key[1], **result})
                if result.get("run_snapshot"):
                    await srv.WS_EVENTS.send_json({"type": "expert_checkpoint_restored", "project": key[0],
                        "conversation_id": key[1], "run_id": state["run_id"], "snapshot": result["run_snapshot"]})
            except Exception as exc:
                result["warning"] = (result.get("warning", "") + " 维护动作已处理，界面同步未完成，请重新打开画布和诊断：" + str(exc)).strip()
            try:
                diagnostics = facts(key, candidate)
            except (ValueError, TypeError, KeyError, OSError) as exc:
                diagnostics = {'graph_error': '修订已保存；更新宿主诊断失败：' + str(exc)}
            return JSONResponse({**ledger().public(changed), "diagnostics": diagnostics, "result": result})
        except (ValueError, TypeError, KeyError, OSError) as exc:
            return error(exc)

    @app.post("/api/orchestration/maintenance/choose-adoption")
    async def choose_adoption(request: Request):
        try:
            body = await body_of(request)
            key = scope(body)
            state = ledger().get(*key, body.get("id"))
            spec, _ = current(key)
            snapshot = srv.EXPERT_RUNS.load(state.get("run_id", "")) if state.get("run_id") else None
            choice = body.get("choice")
            adoption = state.get("run_adoption") or {}
            if (type(body.get("revision")) is not int or body["revision"] != state["revision"]
                    or state.get("status") != "applied" or adoption.get("status") != "choice_required"
                    or not snapshot or snapshot.get("conversation_id") != key[1]
                    or state.get("proposal", {}).get("id") != body.get("proposal_id")):
                raise ValueError("维护接纳选择已过期或不属于当前暂停运行，请重新打开维护面板。")
            if srv.SESSION.chat_busy(key[1]) or snapshot.get("status") == "running":
                raise ValueError("请先暂停业务运行，再选择维护接纳方式。")
            from mewcode.experts.maintenance_adoption import apply_maintenance_adoption_choice
            selected = apply_maintenance_adoption_choice(snapshot, to_expert_flow(spec).as_dict(), choice,
                node_id=body.get("node_id"))
            if selected.get("new_run"):
                import uuid
                fresh_id = uuid.uuid4().hex
                fresh = selected["run_snapshot"]
                flow_value = selected["flow"]
                created = srv.EXPERT_RUNS.create(run_id=fresh_id, conversation_id=key[1],
                    task=str(fresh.get("task") or snapshot.get("task") or ""),
                    attachments=copy.deepcopy(fresh.get("attachments") or snapshot.get("attachments") or []),
                    profile=copy.deepcopy(fresh.get("profile") or snapshot.get("locked_profile") or {}),
                    strategy=str(fresh.get("strategy") or snapshot.get("strategy") or "stable"), flow=flow_value)
                run_inputs = copy.deepcopy(fresh.get("run_inputs") or {})
                if run_inputs:
                    created = srv.EXPERT_RUNS.mutate(fresh_id,
                        lambda value: value.update(run_inputs=run_inputs), durable=True)
                try:
                    started = await srv.launch_expert_resume(created)
                except BaseException:
                    srv.EXPERT_RUNS.transition(fresh_id, "paused", reason="maintenance_new_run_not_started")
                    raise
                summary = {"status": "adopted", "choice": choice, "new_run_id": fresh_id,
                           "label": "已用新版新开运行；原运行保持暂停并保留。"}
                run_snapshot = srv.EXPERT_RUNS.load(fresh_id)
            else:
                run_snapshot = selected["run_snapshot"]
                backup = srv.EXPERT_RUNS.checkpoint_archive.save(snapshot,
                    turn_key="before-maintenance-choice:" + state["id"] + ":" + choice,
                    kind="before_maintenance")
                run_snapshot.setdefault("maintenance_checkpoints", []).append(backup["checkpoint_id"])
                def persist(value):
                    if value.get("updated_at") != snapshot.get("updated_at"):
                        raise ValueError("原运行状态已变化，维护选择未写入。")
                    value.clear()
                    value.update(run_snapshot)
                changed_run = srv.EXPERT_RUNS.mutate_stopped(state["run_id"], persist,
                    conversation_id=key[1], expected_updated_at=snapshot.get("updated_at"))
                run_snapshot = changed_run
                labels = {item.get("id"): item.get("label") for item in adoption.get("choices", [])}
                summary = {"status": "adopted", "choice": choice, "label": labels.get(choice, choice)}
                started = None
            def save_choice(value):
                value.update(run_adoption=summary,
                    run_notice=summary["label"], adoption_result=summary)
            changed = ledger().mutate(*key, state["id"], save_choice)
            public_snapshot = srv.EXPERT_RUNS._public_value(run_snapshot, include_details=False)
            try:
                await srv.WS_EVENTS.send_json({"type": "expert_checkpoint_restored", "project": key[0],
                    "conversation_id": key[1], "run_id": run_snapshot.get("run_id"), "snapshot": public_snapshot})
            except Exception:
                logging.getLogger(__name__).exception("Maintenance adoption UI broadcast failed")
            return JSONResponse({**ledger().public(changed), "result": {
                "run_adoption": summary, "run_snapshot": public_snapshot,
                "flow": selected["flow"], "plan": selected["plan"],
                "started": started, "new_run": bool(selected.get("new_run"))}})
        except (ValueError, TypeError, KeyError, OSError) as exc:
            return error(exc)

    @app.post("/api/orchestration/maintenance/repair-runtime")
    async def repair_runtime(request: Request):
        try:
            body = await body_of(request)
            key = scope(body)
            state = ledger().get(*key, body.get("id"))
            if state["mode"] != "fix" or srv.SESSION.chat_busy(key[1]):
                raise ValueError("请在修理工模式暂停业务运行后检修执行环境。")
            spec, _ = current(key)
            snapshot = run_for(key, spec)
            if (type(body.get("revision")) is not int or body["revision"] != state["revision"]
                    or state["base_hash"] != digest(spec.as_dict())
                    or state["run_id"] != (snapshot or {}).get("run_id", "")):
                raise ValueError("维护基准或原运行已变化，请按当前图重新打开修理工；未检修其他运行。")
            if snapshot and snapshot.get("status") == "running":
                raise ValueError("编排仍在执行，请先暂停。")
            snapshot, report = repair_host_runtime(key, spec, snapshot, state["id"])
            changed = ledger().mutate(*key, state["id"], lambda value: value.update(runtime_repair=report))
            return public(key, changed)
        except (ValueError, TypeError, KeyError, OSError) as exc:
            return error(exc)
