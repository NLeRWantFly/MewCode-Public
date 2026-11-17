"""Explicitly scoped canvas read models and node requirement attachment routes."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import File, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from mewcode.orchestration.activity import ActivityStore
from mewcode.orchestration.work_reports import fact_text, group_key, public_report
from mewcode.orchestration import chat_canvas
from mewcode.orchestration.revisions import CANVAS_NODES_MARKER
from mewcode.orchestration.constraints import attachment_owner, attachment_store
from mewcode.pipeline.store import VersionConflictError
from mewcode.attachments import AttachmentError, MAX_FILE_BYTES


def graph_nodes(graph):
    import json
    payload = json.loads(graph.variables.get("__orch.payload__", "{}"))
    return [{"id": n.id, "label": n.label, "title": n.title,
             **((payload.get("nodes", {}).get(n.id) or {}).get("plan_brief") or {})}
            for n in graph.nodes if n.id not in {"__orch_start__", "__orch_end__"}]


def check_scope(srv, project, conversation_id, graph_id):
    if not project or Path(project).resolve() != Path(srv.SESSION.work_dir).resolve():
        raise ValueError("当前项目已切换，请重新打开画布")
    if conversation_id != srv._active_conversation_id():
        raise ValueError("当前对话已切换，请重新打开画布")
    graph = srv.PipelineStore(srv.SESSION.work_dir).get_pipeline(graph_id)
    if graph is None or not srv.is_orchestration_graph(graph):
        raise ValueError("此项目没有该编排画布")
    if chat_canvas.is_chat_canvas(graph):
        chat_canvas.validate(graph, conversation_id)
    else:
        # Unclaimed legacy graphs open read-only; only a foreign owner rejects.
        owner = str(chat_canvas.metadata(graph).get("conversation_id") or "")
        if owner and owner != conversation_id:
            raise ValueError(f"画布属于另一个对话（{owner[:12]}…），不能叠加本任务的进度或修改归属")
    return graph


async def canvas_activity(srv, *, project, conversation_id, graph_id, run_id=""):
    graph = check_scope(srv, project, conversation_id, graph_id)
    if not str(chat_canvas.metadata(graph).get("conversation_id") or ""):
        # Unclaimed legacy canvas: openable, but nothing overlays until claimed.
        return {"project": srv.SESSION.work_dir, "conversation_id": conversation_id,
                "graph_id": graph_id, "run": None, "runs": [],
                "backfill_pending": False, "unclaimed": True}
    activity = ActivityStore(srv.SESSION.work_dir)
    nodes = graph_nodes(graph)
    runtime = srv.SESSION.runtime_for(conversation_id)
    task = getattr(runtime, "task", None)
    alive = bool(getattr(runtime, "starting", False) or (task is not None and not task.done()))
    durable = getattr(runtime, "session", None) or getattr(srv.SESSION, "session", None)
    sessions_dir = getattr(durable, "_sessions_dir", None)
    replay = {"pending": False}
    if sessions_dir:
        pipeline_log = Path(sessions_dir) / f"{conversation_id}.pipeline.jsonl"
        if await asyncio.to_thread(activity.replay_pending, pipeline_log):
            replay = await asyncio.to_thread(activity.replay_pipeline,
                pipeline_log, conversation_id, limit=400)
    if chat_canvas.is_chat_canvas(graph):
        raise ValueError("此旧进度视图已停用，请重新打开编排画布以恢复原图和箭头")
    runs = srv.EXPERT_RUNS.list_for_conversation(conversation_id)
    matching = []
    for run in runs:
        locked = run.get("locked_flow") or {}
        if (locked.get("meta") or {}).get("pipeline_graph_id") == graph_id:
            matching.append(run)
            locked_nodes = [{"id": n["node_id"], "label": n.get("label", ""),
                             **((locked.get("meta", {}).get("node_plan_briefs") or {}).get(n["node_id"]) or {})}
                            for n in locked.get("nodes") or []]
            activity.bind(conversation_id, graph_id, run["run_id"], nodes=locked_nodes,
                          version=locked.get("version", graph.version), title=run.get("task", ""))
    history_run = activity.chat_view_run(conversation_id)
    focus = getattr(srv, "dialogue_focus", lambda _: {})(runtime or srv.SESSION)
    if run_id == history_run or (not run_id and (not matching or focus.get("kind") == "chat")):
        activity.bind_chat_history(conversation_id, graph_id, nodes=nodes, version=graph.version)
        run_id = history_run
    bindings = activity.bindings(conversation_id, graph_id)
    wanted = run_id or (matching[0]["run_id"] if matching else (bindings[0]["run_id"] if bindings else ""))
    binding = next((b for b in bindings if b["run_id"] == wanted), None)
    if not binding:
        return {"project": srv.SESSION.work_dir, "conversation_id": conversation_id, "graph_id": graph_id,
                "run": None, "runs": [], "backfill_pending": replay["pending"], "unclaimed": False}
    expert = next((r for r in matching if r["run_id"] == wanted), None)
    if expert:
        await asyncio.to_thread(activity.replay_expert, expert.get("events") or [], conversation_id, wanted)
    check_scope(srv, project, conversation_id, graph_id)
    recent = activity.page(conversation_id, graph_id, wanted, limit=100)
    overview = activity.overview(conversation_id, graph_id, wanted)
    states = srv._orchestration_run_node_states(expert) if expert else {}
    for event in overview["latest"]:
        node = event["node_id"]
        if not node:
            continue
        state = states.setdefault(node, {"status": "pending", "attempt": event.get("attempt", 1)})
        state["current_action"] = fact_text(event)
        state["updated_at"] = event.get("updated_at") or event.get("at")
        state["attribution"] = event["attribution"]
        if not expert and event["attribution"] in {"explicit", "exact_label", "manual"}:
            state["attempt"] = event.get("attempt") or 1
            state["status"] = event["status"] if event.get("status") in {"running", "waiting", "paused", "failed"} else "recorded"
            state["message"] = "已记录活动；完成情况待确认"
    for report in overview.get("reports", []):
        node = report.get("node_id")
        latest = next((e for e in reversed(overview["latest"]) if e.get("node_id") == node), None)
        if node in states and latest and group_key(report) == group_key(latest):
            states[node]["latest_report"] = {"text": fact_text(report), "at": report.get("at"),
                                             "event_id": report.get("id"), "role": report.get("role"),
                                             "run_id": report.get("run_id"), "attempt": int(report.get("attempt") or 1),
                                             "source": "model_report", "details": public_report(report)}
            if states[node].get("status") not in {"failed", "waiting", "paused", "blocked"}:
                states[node]["current_action"] = fact_text(report)
    if expert:
        status = expert.get("status", "pending")
        files = expert.get("process_file_receipts") or []
    else:
        turn = next((e for e in overview["latest"] if e["kind"] in {"turn", "lifecycle"}), None)
        status = turn.get("status", "pending") if turn else "needs_attention"
        if not alive:
            if status in {"running", "waiting", "pending"}:
                status = "needs_attention"
            for state in states.values():
                if state.get("status") == "running":
                    state.update(status="needs_attention", message="没有仍在执行的任务；上次操作是否结束需要核实")
        elif wanted == history_run and status not in {"waiting", "paused"}:
            status = "running"
        files = [dict(e["file"], node_id=e["node_id"], activity_snapshot=e["kind"] == "artifact") for e in overview["files"]]
    run = {"run_id": wanted, "conversation_id": conversation_id, "graph_id": graph_id,
           "graph_version": (expert or {}).get("locked_flow", {}).get("version") or binding["graph_version"], "source": "expert" if expert else "activity",
           "node_states": states, "status": status, "active": status in {"running", "pending", "waiting"},
           "process_files": files, "activity_cursor": recent["cursor"], "recent_activity": recent["items"],
           "activity_count": overview["activity_count"], "chat_history": wanted == history_run,
           "last_activity_at": max((str(e.get("updated_at") or e.get("at") or "") for e in overview["latest"]), default="") or None,
           "unassigned_count": activity.count_unassigned(conversation_id, wanted),
           "intervention_receipts": (expert or {}).get("intervention_receipts") or [],
           "takeover_state": (expert or {}).get("takeover_state"),
           "plan_revision_pending": bool((expert or {}).get("plan_revision_pending")),
           "applied_constraints": {k: v.get("applied_constraints", []) for k, v in ((expert or {}).get("node_states") or {}).items()}}
    return {"project": srv.SESSION.work_dir, "conversation_id": conversation_id, "graph_id": graph_id,
            "run": run, "runs": [{k: b[k] for k in ("run_id", "title", "graph_version")} for b in bindings],
            "backfill_pending": replay["pending"], "unclaimed": False}


def bind_open_canvas(srv, graph):
    """Opening explicitly associates only the current chat task, not future chats."""
    conv = srv._active_conversation_id()
    if not conv:
        return
    # Merely opening a project plan is not evidence that this chat executes it.
    if chat_canvas.metadata(graph).get("conversation_id") != conv or chat_canvas.is_chat_canvas(graph):
        return
    activity = ActivityStore(srv.SESSION.work_dir)
    runtime = srv.SESSION.runtime_for(conv) or srv.SESSION
    focus = srv.dialogue_focus(runtime)
    if focus.get("kind") == "orchestration":
        return
    run_id = "chat:" + str(focus["task_id"]) if focus.get("task_id") else activity.latest_chat_run(conv) or "chat:legacy:"+conv
    activity.bind(conv, graph.graph_id, run_id, nodes=graph_nodes(graph), version=graph.version,
                  title=focus.get("task", "历史对话记录"))
    # The conversation may already be mid-turn — the canvas was created while
    # its task was running, so the turn-start node list never reached it. Queue
    # the reminder; it flushes at the next tool-result boundary.
    conversation = getattr(runtime, "conversation", None)
    if conversation is not None:
        try:
            conversation.queue_marker_reminder(
                CANVAS_NODES_MARKER, srv._canvas_nodes_text(runtime))
        except Exception:
            pass


def install(app, srv):
    from frontend.orch_reports import install as install_reports
    install_reports(app, srv, check_scope)

    @app.post("/api/orchestration/node-supplement")
    async def node_supplement(request: Request):
        try:
            if not srv._chat_queue_header_valid(request):
                return JSONResponse({"error": "需要从补充要求入口明确提交"}, status_code=403)
            body = await request.json()
            graph = check_scope(srv, body["project"], body["conversation_id"], body["graph_id"])
            node = next((n for n in graph_nodes(graph) if n["id"] == body["node_id"]), None)
            if node is None:
                raise ValueError("目标节点不存在")
            runtime = srv.SESSION._active_queue_runtime(body["conversation_id"])
            if runtime.expert_run_id != body["run_id"]:
                raise ValueError("当前运行已切换，请重新打开节点")
            snapshot = srv.EXPERT_RUNS.load(body["run_id"])
            if ((snapshot or {}).get("locked_flow", {}).get("meta") or {}).get("pipeline_graph_id") != body["graph_id"]:
                raise ValueError("运行不属于此画布")
            text = str(body["text"]).strip()
            if not text or len(text) > 7000:
                raise ValueError("补充要求应为 1～7000 字")
            result = await srv._accept_expert_intervention(runtime, body["queue_id"],
                f"针对节点 [{node['id']}] {node['label']} 的补充要求：\n{text}\n完成节点不得自动重跑。")
            def tag(value):
                from mewcode.orchestration.interventions import update_receipts
                for item in value.get("interventions") or []:
                    if item["id"] == body["queue_id"]:
                        item["target_node_id"] = node["id"]
                update_receipts(value)
            srv.EXPERT_RUNS.mutate(body["run_id"], tag, durable=True)
            return result
        except (ValueError, LookupError, RuntimeError, KeyError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    @app.post("/api/orchestration/claim-canvas")
    async def claim_canvas(request: Request):
        try:
            # The header is the point: only the canvas claim button sends it, so
            # no model or background job can claim a legacy graph on its own.
            if request.headers.get("X-Mew-Canvas-Claim") != "1":
                return JSONResponse({"error": "需要从画布的认领按钮明确确认"}, status_code=403)
            body = await request.json()
            project, conversation_id, graph_id = body["project"], body["conversation_id"], body["graph_id"]
            graph = check_scope(srv, project, conversation_id, graph_id)
            owner = str(chat_canvas.metadata(graph).get("conversation_id") or "")
            if owner == conversation_id:
                return {"claimed": True, "already": True, "graph_id": graph_id,
                        "graph_version": graph.version, "conversation_id": conversation_id}
            if owner:
                return JSONResponse({"error": f"此图属于另一个对话（{owner[:12]}…），不能认领"}, status_code=409)
            payload = json.loads(graph.variables.get("__orch.payload__", "{}"))
            payload["meta"] = {**chat_canvas.metadata(graph), "conversation_id": conversation_id}
            claimed = graph.model_copy(update={"variables": {
                **graph.variables, "__orch.payload__": json.dumps(payload, ensure_ascii=False)}})
            saved = srv.PipelineStore(srv.SESSION.work_dir).update_pipeline(
                claimed, expected_version=graph.version)
            if saved is None:
                raise LookupError("画布已删除，无法认领")
            # The project-level plan that produced this graph moves with it.
            plan = srv.get_plan_session_store().get(project)
            if (plan is not None and plan.spec is not None
                    and str((plan.spec.meta or {}).get("pipeline_graph_id") or "") == graph_id
                    and "conversation_id" not in (plan.spec.meta or {})):
                srv._conversation_plan_store(conversation_id).put(project, plan)
            runtime = srv.SESSION.runtime_for(conversation_id) or srv.SESSION
            conversation = getattr(runtime, "conversation", None)
            if conversation is not None:
                try:
                    conversation.queue_marker_reminder(
                        CANVAS_NODES_MARKER, srv._canvas_nodes_text(runtime))
                except Exception:
                    pass
            return {"claimed": True, "graph_id": graph_id, "graph_version": saved.version,
                    "conversation_id": conversation_id}
        except VersionConflictError as exc:
            return JSONResponse({"error": "画布已被更新，请刷新后重试",
                                 "current_version": exc.current_version}, status_code=409)
        except (ValueError, LookupError, RuntimeError, KeyError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    @app.get("/api/orchestration/node-history")
    async def node_history(project: str, conversation_id: str, graph_id: str, run_id: str,
                           node_id: str | None = None, after: int = 0, before: int = 0, limit: int = 50, attempt: int = 0):
        try:
            graph = check_scope(srv, project, conversation_id, graph_id)
            if chat_canvas.is_chat_canvas(graph):
                raise ValueError("此旧进度视图已停用，请重新打开编排画布")
            from mewcode.orchestration.work_reports import local_cards
            page = ActivityStore(project).page(conversation_id, graph_id, run_id, node_id=node_id,
                after=after, before=before, limit=limit, attempt=attempt)
            page["report_cards"] = local_cards(page["items"])
            for event in page["items"]:
                event["report_card"] = next(iter(local_cards([event])), None)
            return page
        except (ValueError, LookupError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    @app.post("/api/orchestration/history-attribution")
    async def history_attribution(request: Request):
        try:
            body = await request.json()
            graph = check_scope(srv, body["project"], body["conversation_id"], body["graph_id"])
            if chat_canvas.is_chat_canvas(graph):
                raise ValueError("此旧进度视图已停用，请重新打开编排画布")
            return ActivityStore(body["project"]).assign(body["conversation_id"], body["graph_id"],
                body["run_id"], body["event_id"], body.get("node_id"))
        except (ValueError, LookupError, KeyError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    @app.get("/api/orchestration/activity-file/{file_id}")
    async def activity_file(file_id: str, project: str, conversation_id: str, graph_id: str, run_id: str):
        try:
            graph = check_scope(srv, project, conversation_id, graph_id)
            if chat_canvas.is_chat_canvas(graph):
                return {"file": chat_canvas.read_file(project, conversation_id, graph, run_id, file_id)}
            activity = ActivityStore(project)
            activity.page(conversation_id, graph_id, run_id, limit=1)
            if run_id == activity.chat_view_run(conversation_id):
                with activity.db() as db:
                    row = db.execute("SELECT run_id FROM artifact WHERE id=? AND conversation_id=? AND run_id LIKE 'chat:%'",
                                     (file_id, conversation_id)).fetchone()
                if not row:
                    raise LookupError("找不到此对话的文件快照")
                run_id = row[0]
            return {"file": activity.read_artifact(conversation_id, run_id, file_id)}
        except (ValueError, LookupError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    @app.post("/api/orchestration/constraint-attachments")
    async def upload_constraint(project: str, conversation_id: str, graph_id: str,
                                files: list[UploadFile] = File(...)):
        saved = []
        assets = attachment_store(project)
        try:
            check_scope(srv, project, conversation_id, graph_id)
            if not 1 <= len(files) <= 8:
                raise ValueError("请选择 1～8 个附件")
            for upload in files:
                data = await upload.read(MAX_FILE_BYTES + 1)
                ref = assets.save(attachment_owner(graph_id), upload.filename or "file", data, upload.content_type or "")
                saved.append(ref.as_dict())
            return {"attachments": saved}
        except (ValueError, AttachmentError, OSError) as exc:
            for ref in saved:
                assets.delete(attachment_owner(graph_id), ref["id"])
            return JSONResponse({"error": str(exc)}, status_code=400)
        finally:
            for upload in files:
                await upload.close()

    @app.get("/api/orchestration/constraint-attachments/{attachment_id}")
    async def get_constraint(attachment_id: str, project: str, conversation_id: str, graph_id: str):
        try:
            check_scope(srv, project, conversation_id, graph_id)
            assets = attachment_store(project)
            ref = assets.get(attachment_owner(graph_id), attachment_id)
            return FileResponse(assets.asset_path(ref), media_type=ref.mime_type,
                headers={"X-Content-Type-Options": "nosniff"})
        except (ValueError, AttachmentError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
