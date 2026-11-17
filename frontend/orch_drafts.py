"""Draft-first planning: source review and graph previews never start a run."""
from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import time
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse

from mewcode.client import LLMError
from mewcode.model_registry import ModelRef
from mewcode.runtime_scheduler import bind_runtime_scheduler, reset_runtime_scheduler
from mewcode.orchestration.draft_sources import advance_cursor, digest, estimate_chunks, next_chunk, read_sources, select_messages
from mewcode.orchestration.draft_store import DraftStore, now
from mewcode.orchestration.effective_history import (
    EFFECTIVE_SYSTEM, apply_history_patch, basis_view, effective_document,
    effective_sources, coverage_sources, freeze_basis, new_basis, prompt_basis,
)
from mewcode.orchestration.graph_autobuild import MAX_SECONDS, TIME_LIMIT_REASON, new_build_state, restart_build_state, run_autobuild
from mewcode.orchestration.graph_build_control import CONTROL_GUIDANCE, ModelStopped, SoftBuildControl
from mewcode.orchestration.graph_generation import GRAPH_SYSTEM, graph_coverage, graph_state, graph_task_state, graph_process_state, ready_graph_document
from mewcode.orchestration.generation_failure import generation_failure
from mewcode.orchestration.graph_inspection import READER_SERVICE, VALIDATOR_SERVICE, page as graph_page, validate_proposal
from mewcode.orchestration.graph_readiness import readiness
from mewcode.orchestration.repair_protocol import is_control_source
from mewcode.orchestration.extract import (
    collect_model_text, OrchestrationStreamTimeout, OrchestrationGenerationLimit,
)
from mewcode.orchestration.workflow_draft import (
    REPAIR_HINT, SYSTEM, DraftPatchError, apply_patch, project_graph, render_markdown,
)

CHUNK_ATTEMPTS = 3
class AutoBuildDeadline(Exception):
    """The whole extraction and completion budget has expired."""

SELECTED_SYSTEM = """
本次为用户点选消息的增量整理，不是完整历史扫描。只纳入 sources 所表达的建议或要求。
context 仅用于理解指代，不代表本次选中或新增授权；不得仅凭 context 新增其他条款。
每项 upsert 必须引用至少一个当前 sources。助手回复仍是建议，不能冒充用户确认。
若上下文不足，记录待确认问题，不扩展扫描范围或猜测要求。"""


def parse_patch(raw, chunk):
    clean = raw.strip()
    fence = chr(96) * 3
    if clean.startswith(fence):
        clean = clean[len(fence):].removeprefix("json").strip()
        if clean.endswith(fence):
            clean = clean[:-len(fence)].strip()
    try:
        return json.loads(clean)
    except ValueError as exc:
        raise DraftPatchError(f"第 {chunk} 块返回的不是合法 JSON（{exc.msg}）；只输出 JSON，不要加说明文字。") from exc


def graph_changes(before, after):
    before = before or {}
    a, b = ({n["node_id"]: n for n in spec.get("nodes", [])} for spec in (before, after))
    changes = [{"kind": "node", "id": id, "change": "新增" if id not in a else "修改",
                "title": b[id]["label"], "before": a.get(id), "after": b[id]}
               for id in b if a.get(id) != b[id]]
    changes += [{"kind": "node", "id": id, "change": "删除", "title": a[id]["label"], "before": a[id], "after": None}
                for id in a if id not in b]
    for key, label in (("goal", "业务总目标"), ("label", "流程名称"), ("edges", "执行依赖"), ("goal_contract", "目标与验收约束")):
        if before.get(key) != after.get(key):
            changes.append({"kind": key, "change": "修改", "title": label, "before": before.get(key), "after": after.get(key)})
    if (before.get("meta") or {}).get("loops") != (after.get("meta") or {}).get("loops"):
        changes.append({"kind": "loops", "change": "修改", "title": "返修循环", "before": (before.get("meta") or {}).get("loops"),
                        "after": (after.get("meta") or {}).get("loops")})
    for key, title in (("annotations", "节点专属要求与备注"), ("workflow_draft", "底稿来源与节点归属")):
        old, new = ((s.get("meta") or {}).get(key) for s in (before, after))
        if old != new:
            changes.append({"kind": key, "change": "修改", "title": title, "before": old, "after": new})
    return changes


def install(app, srv):
    jobs = {}
    progress = {}

    def generation_running(project, cid):
        job = jobs.get((project, cid))
        return bool(job and not job.done())

    srv.graph_generation_running = generation_running

    def graph_request(request):
        return request.url.path.startswith(("/api/orchestration/graph-generation",
                                            "/api/orchestration/graph-autobuild"))

    def autobuild_request(request):
        return request.url.path.startswith("/api/orchestration/graph-autobuild")

    def scope(body):
        project, cid = srv.SESSION.work_dir, srv._active_conversation_id()
        if (not cid or body.get("conversation_id") != cid
                or os.path.normcase(os.path.abspath(str(body.get("project") or ""))) != os.path.normcase(os.path.abspath(project))):
            raise ValueError("项目或对话已切换，请重新打开当前底稿")
        return project, cid, body.get("draft_id", "main")

    def current_spec(project, cid):
        plan = srv._conversation_plan_store(cid).get(project)
        if plan and plan.spec:
            return plan.spec.as_dict()
        flow = srv._conversation_flow_store(cid).get(project)
        if flow:
            return srv.from_expert_flow(flow).as_dict()
        graph = srv._stored_orchestration_graph(srv.PipelineStore(project))
        return srv.from_pipeline_graph(graph).as_dict() if graph else None

    def source_path(project, cid):
        session = getattr(srv.SESSION, "session", None)
        if not session or getattr(session, "session_id", "") != cid or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", cid):
            raise ValueError("当前会话没有可核对的持久化历史")
        directory = getattr(session, "_sessions_dir", None)
        return Path(directory if directory is not None else Path(project) / ".mewcode" / "sessions") / (cid + ".jsonl")

    source_cache = {}

    async def read_candidate_graph(*, project, conversation_id, ticket=None, snapshot_id=None, cursor=0):
        # Called by the model tool and the read-only HTTP route. Never take the
        # conversation from a model argument or silently fall back to a canvas.
        key = scope({"project": project, "conversation_id": conversation_id})
        state = DraftStore.read_state(*key)
        if not state or not state.get("candidate"):
            raise ValueError("当前对话没有候选图；未读取已保存画布或其他对话。")
        return graph_page(state, current_spec(*key[:2]), conversation_id=key[1], draft_id=key[2],
                          ticket=ticket, snapshot_id=snapshot_id, cursor=cursor)

    srv.APP_CONTEXT.provide(READER_SERVICE, read_candidate_graph, replace=True)

    async def validate_candidate_graph(*, project, conversation_id, proposal, snapshot_id):
        key = scope({"project": project, "conversation_id": conversation_id})
        state = DraftStore.read_state(*key)
        if not state or not state.get("candidate"):
            raise ValueError("当前对话没有候选图")
        return validate_proposal(state, current_spec(*key[:2]), conversation_id=key[1],
                                 proposal=proposal, snapshot_id=snapshot_id)

    srv.APP_CONTEXT.provide(VALIDATOR_SERVICE, validate_candidate_graph, replace=True)

    @app.get("/api/orchestration/graph-generation/snapshot")
    async def candidate_snapshot(request: Request):
        try:
            params = dict(request.query_params)
            result = await read_candidate_graph(project=params.get("project"), conversation_id=params.get("conversation_id"),
                ticket=params.get("ticket"), snapshot_id=params.get("snapshot_id"), cursor=int(params.get("cursor", 0)))
            return result
        except (ValueError, OSError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    async def refresh(store, path):
        # stat marker is taken before the read: a mid-read write then fails the
        # next stat comparison and forces one extra read instead of serving
        # stale cached content.
        try:
            st = path.stat()
            marker = (st.st_size, st.st_mtime_ns)
        except OSError:
            marker = None
        cached = source_cache.get(str(path)) if marker else None
        if cached and cached[0] == marker:
            sources, warnings, snapshot = cached[1]
        else:
            sources, warnings, snapshot = await asyncio.to_thread(read_sources, path)
            if marker:
                source_cache[str(path)] = (marker, (sources, warnings, snapshot))
        from mewcode.orchestration.workflow_blocks import with_material_sources
        state = store.get()
        sources = with_material_sources(sources, state)
        snapshot = {**snapshot, "source_count": len(sources), "snapshot": digest([s["id"] for s in sources])}
        if state["snapshot"] == snapshot and state.get("warnings", []) == warnings:
            return state
        return await asyncio.to_thread(store.refresh, sources, warnings, snapshot)

    def work_sources(store, candidate):
        sources = store.sources()
        if candidate and "history_source_ids" in candidate:
            frozen = candidate["history_source_ids"]
            if [s["id"] for s in sources][:len(frozen)] != frozen:
                raise ValueError("本轮构图的原始来源发生变化，请核对后重新扫描")
            sources = sources[:len(frozen)]
        if candidate and candidate.get("mode") == "selected":
            values = {s["id"]: s for s in sources}
            if not set(candidate["source_ids"]).issubset(values):
                raise ValueError("选定来源已变化，请刷新对话后重新选择")
            return [values[id] for id in candidate["source_ids"]]
        return sources

    def coverage(store, state, *, graph=False):
        candidate = state.get("candidate")
        processed = candidate["processed_ids"] if candidate else state["processed_ids"]
        reused = candidate.get("reused_ids", []) if candidate else []
        if graph and not candidate:
            reused = state.get("selected_ids", [])
        if graph:
            state["coverage"] = graph_coverage(state)
        state["coverage"]["chunks_estimate"] = 0 if state["coverage"].get("history_changed") else estimate_chunks(
            work_sources(store, candidate), processed, source_limit=(candidate or {}).get("chunk_source_limit"), reused_ids=reused)

    def public_state(store, key, *, graph=False):
        state = store.get()
        coverage(store, state, graph=graph)
        if graph:
            c = state.get("candidate") or {}
            basis = c.get("history_basis") or state.get("history_basis")
            frozen = c.get("history_source_ids", state["source_ids"])
            sources = store.sources()[:len(frozen)]
            # Compatibility reads do not introduce another confirmation step.
            if not basis and state["document"]["entries"]:
                basis = new_basis(state["document"], state["source_ids"])
            state["basis"] = basis_view(basis, sources, c.get("document", state["document"]))
            state["history_hash"] = digest(state["source_ids"])
        if graph and (state.get("repair_preview") or {}).get("local"):
            from mewcode.orchestration.graph_repair import require_current
            try:
                require_current(state, current_spec(*key[:2]), state["repair_preview"]["ticket"])
                state["local_repair_preview"] = {k: v for k, v in state["repair_preview"].items() if k != "document"}
            except ValueError:
                state["local_repair_preview"] = None
        state["progress"] = copy.deepcopy(progress.get(key))
        state["versions"] = store.versions()
        state["running"] = key in jobs and not jobs[key].done()
        materials = state.get("materials") or {}
        state["material_progress"] = {"sequence": materials.get("sequence", 0),
            "entries": len((materials.get("document") or {}).get("entries", [])),
            "pending": len(materials.get("pending", [])),
            "new_materials_available": materials.get("sequence", 0) >
                ((state.get("candidate") or {}).get("materials_snapshot") or {}).get("sequence", 0)}
        state.update(project=key[0], conversation_id=key[1], draft_id=key[2])
        if graph:
            result = graph_state(state, current_spec(*key[:2]))
            result["material_progress"] = state["material_progress"]
            # Keep the legacy read-only claim path reachable through the shared
            # workspace without treating an unclaimed graph as this chat's base.
            unclaimed = (not result["can_open_canvas"]
                         and srv._single_unclaimed_graph(srv.PipelineStore(key[0])) is not None)
            result["canvas_claim_required"] = bool(unclaimed)
            result["can_open_canvas"] = result["can_open_canvas"] or bool(unclaimed)
            return result
        state.pop("source_ids", None)
        state.pop("processed_ids", None)
        state.pop("selected_ids", None)
        if state.get("candidate"):
            state["candidate"].pop("processed_ids", None)
            state["candidate"].pop("selection_context", None)
            state["candidate"].pop("base_processed_ids", None)
            state["candidate"].pop("reused_ids", None)
        if state.get("local_repair_run"):
            state["local_repair_run"] = {k: v for k, v in state["local_repair_run"].items() if k != "work"}
        return state

    async def body_of(request):
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("请求必须是对象")
        return body

    @app.get("/api/orchestration/draft")
    @app.get("/api/orchestration/graph-generation")
    @app.get("/api/orchestration/graph-autobuild")
    async def get_draft(request: Request):
        try:
            body = dict(request.query_params)
            key = scope(body)
            store = DraftStore(*key)
            await refresh(store, source_path(*key[:2]))
            scope(body)
            return public_state(store, key, graph=graph_request(request))
        except (ValueError, OSError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    @app.get("/api/orchestration/draft/progress")
    @app.get("/api/orchestration/graph-generation/progress")
    @app.get("/api/orchestration/graph-autobuild/progress")
    async def get_draft_progress(request: Request):
        # Cheap poll: sqlite draft state plus in-memory progress; never rereads history.
        try:
            key = scope(dict(request.query_params))
            while True:
                job = jobs.get(key)
                running = job is not None and not job.done()
                state = await asyncio.to_thread(lambda: DraftStore(*key).get(include_changes=False))
                scope(dict(request.query_params))
                current_job = jobs.get(key)
                if job is current_job and running == (current_job is not None and not current_job.done()):
                    break
            c = state.get("candidate")
            if graph_request(request):
                state["coverage"] = graph_coverage(state)
                if not state["coverage"]["pending"] or state["coverage"].get("history_changed"):
                    state["coverage"]["chunks_estimate"] = 0
            task_state = graph_task_state(state, running, progress.get(key))
            build = {k: c["autobuild"].get(k) for k in ("phase", "status", "reason", "model_calls", "call_attempts")} if c and c.get("autobuild") else None
            if build and task_state["kind"] != "local_repair" and (c["status"] in {"failed", "paused"} or task_state["status"] == "interrupted"):
                build.update(status=task_state["status"], reason=task_state["error"] or build["reason"])
            cached = source_cache.get(str(source_path(*key[:2])))
            try:
                stat = source_path(*key[:2]).stat()
                history_updated = not cached or cached[0] != (stat.st_size, stat.st_mtime_ns)
            except OSError:
                history_updated = True
            return {
                "running": running, "task": task_state,
                "process": graph_process_state(state, running, progress.get(key)),
                "new_history_available": history_updated,
                "progress": copy.deepcopy(progress.get(key)) if running else None,
                "coverage": state["coverage"],
                "revision": state["revision"],
                "candidate": {**{k: c[k] for k in ("id", "sequence", "status", "error", "log", "at") if k in c},
                              "autobuild": build} if c else None,
                "project": key[0], "conversation_id": key[1], "draft_id": key[2],
            }
        except (ValueError, OSError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    @app.get("/api/orchestration/draft/source/{source_id}")
    @app.get("/api/orchestration/graph-generation/source/{source_id}")
    @app.get("/api/orchestration/graph-autobuild/source/{source_id}")
    async def get_source(source_id: str, request: Request):
        try:
            key = scope(dict(request.query_params))
            return DraftStore(*key).source(source_id)
        except (ValueError, OSError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    @app.get("/api/orchestration/draft/export")
    async def export_draft(request: Request):
        try:
            key = scope(dict(request.query_params))
            state = DraftStore(*key).get()
            return PlainTextResponse(render_markdown((state.get("candidate") or {}).get("document", state["document"]), state["revision"]),
                                     headers={"Content-Disposition": 'attachment; filename="workflow-draft.md"'})
        except (ValueError, OSError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    @app.post("/api/orchestration/draft/update")
    @app.post("/api/orchestration/graph-generation/update")
    @app.post("/api/orchestration/graph-autobuild/update")
    async def update_draft(request: Request):
        try:
            body = await body_of(request)
            direct = graph_request(request)
            auto = autobuild_request(request)
            retry_build = body.get("retry_build", False)
            if not isinstance(retry_build, bool) or retry_build and (
                    not auto or body.get("rebuild") is True or "selection" in body):
                raise ValueError("从当前图重新修复只能用于自动构图，不能同时重读历史或改变选择范围")
            rebase_current_canvas = body.get("rebase_current_canvas", False)
            if not isinstance(rebase_current_canvas, bool):
                raise ValueError("rebase_current_canvas 必须是布尔值")
            if rebase_current_canvas and retry_build:
                raise ValueError("rebase_current_canvas 不能与 retry_build 同时使用")
            if rebase_current_canvas and (not direct or body.get("rebuild") is True or "selection" in body):
                raise ValueError("从当前画布 rebase 只能用于构图更新，不能同时重读历史或改变选择范围")
            key = scope(body)
            if key in jobs and not jobs[key].done():
                return JSONResponse({"error": "此底稿正在整理，请等待或停止后再试"}, status_code=409)
            store = DraftStore(*key)
            path = source_path(*key[:2])
            state = await refresh(store, path)
            scope(body)
            if key in jobs and not jobs[key].done():
                raise ValueError("此底稿正在整理，请等待或停止后再试")
            if not state["source_ids"]:
                raise ValueError("尚无可整理的原始对话；未使用压缩摘要冒充完整历史")
            base = current_spec(*key[:2])
            existing_build = (state.get("candidate") or {}).get("autobuild")
            new_history = len(state["source_ids"]) > len((state.get("candidate") or {}).get("history_source_ids", state["source_ids"]))
            if (existing_build and existing_build.get("phase") == "waiting_user" and not rebase_current_canvas
                    and body.get("rebuild") is not True
                    and not new_history and not body.get("include_new")):
                return JSONResponse({"waiting_user": True, **public_state(store, key, graph=direct)})
            if retry_build and (not existing_build or existing_build.get("phase") != "done"
                                or existing_build.get("status") == "ready"):
                raise ValueError("当前没有已结束且需要审查的构图；进行中的构图请继续或停止")
            if not auto and body.get("rebuild") is not True and existing_build and existing_build.get("phase") != "done":
                raise ValueError("自动构图尚未完成，请从自动构图入口继续或停止")
            selection, nearby = None, []
            if "selection" in body:
                chosen, nearby = select_messages(store.sources(), body["selection"])
                if any(is_control_source(s) for s in chosen):
                    raise ValueError("这段回复是图修订单，请点击「预览 AI 修订」，不要再次纳入阶段图；未改变候选或调用模型。")
                selection = [s["id"] for s in chosen]
            if rebase_current_canvas:
                current = state.get("candidate")
                if not current:
                    raise ValueError("当前没有可 rebase 的候选图")
                expected_revision = body.get("expected_revision", body.get("revision"))
                if (type(expected_revision) is not int or expected_revision < 0
                        or ("expected_revision" in body and "revision" in body
                            and body.get("expected_revision") != body.get("revision"))):
                    raise ValueError("rebase 需要当前的 revision")
                candidate_id, sequence = body.get("candidate_id"), body.get("sequence")
                if not isinstance(candidate_id, str) or type(sequence) is not int:
                    raise ValueError("rebase 需要当前候选的 candidate_id 和 sequence")
                base_hash = body.get("base_graph_hash")
                if not isinstance(base_hash, str) or base_hash != digest(base):
                    raise ValueError("画布已被并发修改，请刷新后重新 rebase")
                candidate = store.rebase({"id": candidate_id, "sequence": sequence}, base,
                                         expected_revision=expected_revision,
                                         expected_base_graph_hash=base_hash)
                state = store.get()
            capacity_expected = getattr(request, "_capacity_resume", None)
            capacity_resume = capacity_expected is not None
            if capacity_resume:
                from mewcode.orchestration.task_design import TaskDesign
                from mewcode.orchestration.draft_sources import digest as capacity_digest
                import copy as capacity_copy

                if (not direct or not auto or retry_build or rebase_current_canvas
                        or body.get("include_new") or body.get("rebuild") or selection is not None):
                    raise ValueError("容量恢复不能与重读历史、重建或改变选择范围混用。")
                await check_task_build(*key[:2], capacity_expected)
                task = state.get("task_design") or {}
                TaskDesign._assert_build(task, capacity_expected)
                task_request = task.get("build_request") or {}
                receipt = task_request.get("capacity_revalidation") or {}
                candidate = capacity_copy.deepcopy(state.get("candidate") or {})
                build = candidate.get("autobuild") or {}
                preflight = receipt.get("preflight") or {}
                recovery_history = build.get("capacity_recovery_history") or []
                if (not receipt or receipt != capacity_expected.get("capacity_revalidation")
                        or candidate.get("id") != receipt.get("candidate_id")
                        or type(receipt.get("candidate_sequence")) is not int
                        or candidate.get("sequence") != receipt["candidate_sequence"] + 1
                        or candidate.get("status") != "ready"
                        or capacity_digest(candidate.get("document")) != receipt.get("document_hash")
                        or candidate.get("base_graph_hash") != capacity_digest(base)
                        or capacity_digest(candidate.get("base_snapshot")) != capacity_digest(base)
                        or capacity_digest(candidate.get("history_basis")) != (preflight.get("identity") or {}).get("history_basis_hash")
                        or capacity_digest(candidate.get("history_source_ids")) != preflight.get("frozen_source_ids_hash")
                        or (candidate.get("materials_snapshot") or {}).get("sequence") != task_request.get("sequence")
                        or (state.get("materials") or {}).get("sequence") != task_request.get("sequence")
                        or not recovery_history or recovery_history[-1].get("capacity_revalidation") != receipt
                        or build.get("phase") not in {"review", "final_review"}
                        or build.get("status") != "working" or build.get("review_stale") is not True
                        or preflight.get("ready") is not True):
                    raise ValueError("容量恢复回执或冻结候选已变化，未扩展来源或重新抽取。")
            else:
                candidate = store.begin(base, rebuild=body.get("rebuild") is True, selection=selection,
                                        context=[s["id"] for s in nearby], graph_first=direct,
                                        include_new=body.get("include_new") is True or new_history and existing_build and existing_build.get("phase") == "waiting_user")
            if candidate is None:
                return JSONResponse({"already_included": True, **public_state(store, key, graph=direct)})
            all_sources = store.sources()[:len(candidate.get("history_source_ids", state["source_ids"]))]
            history_extended = candidate.get("history_source_ids") != (state.get("candidate") or {}).get("history_source_ids")
            if direct and not capacity_resume and ("materials_snapshot" not in candidate or body.get("include_new") is True or history_extended):
                from mewcode.orchestration.workflow_blocks import freeze_materials
                frozen = freeze_materials(candidate, store.get(), sources=all_sources,
                                          force=body.get("include_new") is True or history_extended)
                candidate = store.freeze_materials(candidate, frozen)
            if not auto and candidate.get("autobuild") and candidate["autobuild"].get("phase") != "done":
                raise ValueError("自动构图尚未完成，请从自动构图入口继续或停止")
            sources = work_sources(store, candidate)
            if direct and not candidate.get("history_basis"):
                candidate = store.checkpoint(candidate, candidate["document"], candidate["processed_ids"],
                    candidate["status"], history_basis=new_basis(candidate["document"], [s["id"] for s in all_sources], state.get("history_basis")))
            reused = candidate.get("reused_ids", [])
            history_basis = candidate.get("history_basis") or {}
            basis_work = bool(history_basis.get("dirty") or history_basis.get("pending"))
            has_batch = bool(next_chunk(sources, candidate["processed_ids"],
                                        source_limit=candidate.get("chunk_source_limit"), reused_ids=reused) or history_basis.get("pending"))
            if capacity_resume and (basis_work or has_batch):
                raise ValueError("冻结候选仍有未完成抽取，不能作为容量恢复续接；未重置已有预算。")
            if retry_build:
                candidate = store.checkpoint(candidate, candidate["document"], candidate["processed_ids"],
                    autobuild=restart_build_state(candidate["document"], candidate["autobuild"], now=time.time()))
            if auto and (has_batch or basis_work or not candidate.get("autobuild")):
                started = time.time()
                prior = candidate.get("autobuild") or {}
                extracting = ({**prior, "status": "working"} if prior.get("phase") == "extracting"
                              and "deadline_at" in prior and "started_at" in prior else
                              {"phase": "extracting", "status": "working", "model_calls": 0, "call_attempts": 0,
                               "started_at": started, "deadline_at": started + MAX_SECONDS,
                               "previous_attempts": prior.get("previous_attempts", [])})
                candidate = store.checkpoint(candidate, candidate["document"], candidate["processed_ids"],
                    autobuild=extracting)
            if not has_batch and not basis_work and (not auto or candidate["autobuild"].get("phase") == "done"):
                cursor = advance_cursor(sources, candidate["processed_ids"], reused_ids=reused)
                completed = candidate.get("autobuild") or {}
                if completed.get("phase") == "done" and completed.get("status") == "ready":
                    from mewcode.orchestration.planning_issues import pending_question_gate
                    reason, deferred = pending_question_gate(candidate["document"],
                        plan_only=(store.get().get("task_design") or {}).get("intent") == "plan")
                    checked = {**completed, "deferred_capability_question_ids": deferred}
                    if reason:
                        checked.update(status="needs_review", reason=reason)
                    if checked != completed:
                        candidate = store.checkpoint(candidate, candidate["document"], cursor,
                            "ready", autobuild=checked)
                if candidate["status"] != "ready" or candidate["processed_ids"] != cursor:
                    store.checkpoint(candidate, candidate["document"], cursor, "ready")
                return JSONResponse({"already_included": True, **public_state(store, key, graph=direct)})
            context = srv._orchestration_attempt_context(str(body.get("orchestration_effort") or ""))
            selected_mode = candidate.get("mode") == "selected"
            context_sources = {s["id"]: s for s in store.sources()} if selected_mode else {}
            saved_ids = set(state["processed_ids"]) | set(state.get("selected_ids", []))

            async def run_scoped():
                current = candidate
                reused = list(candidate.get("reused_ids", []))
                log = list(current.get("log") or [])
                effort = context.get("requested_effort", "")
                model_label = " · ".join(filter(None, [str(context.get("model") or ""), f"effort {effort}" if effort else ""]))
                job_started_at = now()
                batch, n, batch_chars, chunk_started_at = [], 0, 0, ""
                chunk_t0, chunk_active = 0.0, False
                source_limit = current.get("chunk_source_limit")
                extraction_control = SoftBuildControl((current.get("autobuild") or {}).get("control"))

                async def save_extraction_control(control):
                    nonlocal current
                    build = {**current["autobuild"], "control": control}
                    current = store.checkpoint(current, current["document"], current["processed_ids"],
                                               log=log, autobuild=build)

                async def ask_extraction_control(system, user):
                    nonlocal current
                    build = {**current["autobuild"], "call_attempts": (current["autobuild"].get("call_attempts") or 0) + 1}
                    current = store.checkpoint(current, current["document"], current["processed_ids"], autobuild=build, log=log)
                    started_request = time.monotonic()
                    result = await collect_model_text(client, system=system, user_text=user,
                        total_timeout=None, max_generated_chars=None, progress=on_progress)
                    extraction_control.add_request(result, time.monotonic() - started_request)
                    build = {**current["autobuild"], "model_calls": (current["autobuild"].get("model_calls") or 0) + 1}
                    current = store.checkpoint(current, current["document"], current["processed_ids"], autobuild=build, log=log)
                    return result

                def on_progress(update):
                    entry = progress.get(key)
                    if entry is None or not isinstance(update, dict):
                        return
                    subset = {k: update[k] for k in (
                        "stage", "detail", "thinking_chars", "thinking_summary_chars",
                        "output_chars", "output_chunks", "provider_events", "stalled_phase",
                    ) if k in update}
                    preview = str(update.get("output_preview") or "")
                    if preview:
                        subset["output_preview"] = preview[-600:]
                    subset["updated_at"] = now()
                    subset["waiting"] = bool(update.get("waiting"))
                    if update.get("provider_event"):
                        subset["last_event_at"] = subset["updated_at"]
                    if update.get("request_admitted"):
                        entry["request_started_at"] = subset["updated_at"]
                    entry["model"] = {**(entry.get("model") or {}), **subset}

                def unfinished_entry(status, error="", diagnostic=None):
                    model = (progress.get(key) or {}).get("model") or {}
                    return {"chunk": n, "sources": len(batch), "chars": batch_chars,
                            "first_line": batch[0].get("line") if batch else None,
                            "last_line": batch[-1].get("line") if batch else None,
                            "started_at": chunk_started_at or now(),
                            "duration_ms": int((time.monotonic() - chunk_t0) * 1000) if chunk_t0 else 0,
                            "upserts": 0, "ignored": 0, "status": status, "error": str(error)[:300],
                            "thinking_chars": int(model.get("thinking_chars") or 0),
                            "output_chars": int(model.get("output_chars") or 0),
                            "attempt": attempt, "request_chars": len(prompt) + len(repair),
                            "model_route": {key: str(context.get(key) or "") for key in
                                ("provider_key", "provider_name", "model", "requested_effort")},
                            **({"diagnostic": diagnostic} if diagnostic else {})}

                def batch_prompt(batch):
                    position = len(current["processed_ids"])
                    # The graph, not a rolling replay of old raw text, carries
                    # prior requirements. Legacy draft callers keep their context.
                    nearby = [] if direct else sources[max(0, position - 2):position]
                    if selected_mode:
                        ids = dict.fromkeys(id for s in batch for id in current.get("selection_context", {}).get(s["id"], []))
                        covered = saved_ids | set(current["processed_ids"]) | set(current.get("base_processed_ids", []))
                        covered.update(s["id"] for s in batch)
                        nearby = [context_sources[id] for id in ids if id in context_sources and (not direct or id not in covered)]
                    # Keep requirements and stable source IDs; only execution
                    # batch size changes, never historical coverage or scope.
                    ledger = current["history_basis"]["document"] if direct else current["document"]
                    prompt_doc = {"entries": [{**e, "sources": [r["source_id"] for r in e["sources"]]}
                                              for e in ledger["entries"]]}
                    nearby = [s for s in nearby if not is_control_source(s)]
                    from mewcode.orchestration.graph_models import available_model_routes
                    payload = {"document": prompt_doc, "context": nearby,
                                         "sources": [s for s in batch if not is_control_source(s)],
                                         "available_model_routes":available_model_routes(srv.MODEL_REGISTRY)}
                    from mewcode.orchestration.workflow_draft import entry_contract
                    payload["entry_contract"] = entry_contract()
                    payload["workflow_materials"] = {"sequence": (current.get("materials_snapshot") or {}).get("sequence", 0),
                        "instruction": "已保存素材是可复用的 AI 设计；完善时复用条目 ID，仍逐项核对用户要求。"}
                    if direct:
                        payload["history_basis"] = prompt_basis(current["history_basis"], all_sources, current["processed_ids"])
                    prompt = json.dumps(payload, ensure_ascii=False)
                    if not direct and len(prompt) > 160000:
                        raise ValueError("底稿和当前块超过安全输入上限，未截断要求；请拆分独立流程")
                    return prompt, nearby, sum(len(json.dumps(s, ensure_ascii=False)) for s in batch)

                try:
                    client = None
                    while True:
                        if srv._active_conversation_id() == key[1]:
                            base_now = current_spec(*key[:2])
                            if digest(base_now) != current["base_graph_hash"]:
                                if direct:
                                    raise ValueError("画布已被修改，已保存生成进度，未覆盖；请按新图重新生成")
                                current = store.rebase(current, base_now)
                        pending = (current.get("history_basis") or {}).get("pending", [])
                        replaying = bool(pending)
                        if replaying:
                            values = {s["id"]: s for s in all_sources}
                            replay = []
                            for id in dict.fromkeys(item["source_id"] for item in pending):
                                if id not in values:
                                    raise ValueError("待恢复内容的来源已变化，未复用错误来源")
                                spans = [item["quote"] for item in pending if item["source_id"] == id]
                                text = "\n".join(dict.fromkeys(spans)) if all(spans) else values[id]["text"]
                                replay.append({**values[id], "text": text})
                            batch = next_chunk(replay, [], source_limit=source_limit)
                        else:
                            batch = next_chunk(sources, current["processed_ids"], source_limit=source_limit, reused_ids=reused)
                        if not batch:
                            cursor = advance_cursor(sources, current["processed_ids"], reused_ids=reused)
                            current = store.checkpoint(current, current["document"], cursor,
                                                       "working" if auto else "ready", log=log)
                            break
                        prompt, nearby, batch_chars = batch_prompt(batch)
                        document, patch, repair = None, None, ""
                        done_chunks = sum(1 for e in log if e["status"] == "ok")
                        n = done_chunks + 1
                        attempt = 0
                        while True:
                            attempt += 1
                            raw = ""
                            chunk_started_at = now()
                            chunk_active = False
                            progress[key] = {
                                "phase": "preparing", "chunk": n, "attempt": attempt, "attempts": CHUNK_ATTEMPTS,
                                "chunks_estimate": done_chunks + estimate_chunks(sources, current["processed_ids"],
                                    source_limit=source_limit, reused_ids=reused),
                                "batch": {"count": len(batch), "chars": batch_chars,
                                          "first_line": batch[0].get("line"), "last_line": batch[-1].get("line")},
                                "job_started_at": job_started_at, "chunk_started_at": chunk_started_at,
                                "model_label": model_label, "model": None}
                            progress[key]["phase"] = "model"
                            chunk_t0 = time.monotonic()
                            chunk_active = True
                            try:
                                controls = [{"source_id": s["id"], "reason": "修图协议单独预览采用，不是业务要求；原文仍可回读"}
                                            for s in batch if is_control_source(s)]
                                # Budget counts reasoning too: thinking models burn 45k chars
                                # of hidden reasoning on one chunk before emitting any JSON.
                                if len(controls) == len(batch):
                                    patch = {"upserts": [], "ignored": controls}
                                else:
                                    if client is None:
                                        client = srv._new_orchestration_client(context, "" if effort == "auto" else effort)
                                    if auto:
                                        build = {**current["autobuild"], "call_attempts": (current["autobuild"].get("call_attempts") or 0) + 1}
                                        current = store.checkpoint(current, current["document"], current["processed_ids"], autobuild=build, log=log)
                                    from mewcode.orchestration.planning_issues import PREPARATION_QUESTION_GUIDANCE
                                    request_model = collect_model_text(
                                        client, system=(GRAPH_SYSTEM + EFFECTIVE_SYSTEM if direct else SYSTEM) + (SELECTED_SYSTEM if selected_mode else "")
                                        + "\n" + PREPARATION_QUESTION_GUIDANCE + (CONTROL_GUIDANCE if auto else ""),
                                        user_text=prompt + repair, total_timeout=None if auto else 900,
                                        max_generated_chars=None, progress=on_progress)
                                    raw = await request_model
                                    if auto:
                                        extraction_control.add_request(raw, time.monotonic() - chunk_t0)
                                        build = {**current["autobuild"], "model_calls": (current["autobuild"].get("model_calls") or 0) + 1,
                                                 "control": copy.deepcopy(extraction_control.state)}
                                        current = store.checkpoint(current, current["document"], current["processed_ids"], autobuild=build, log=log)
                                    patch = parse_patch(raw, n)
                                    if isinstance(patch, dict) and isinstance(patch.get("ignored"), list):
                                        patch["ignored"].extend(controls)
                                if key in progress:
                                    progress[key]["phase"] = "applying"
                                if direct:
                                    next_basis = apply_history_patch(current["history_basis"], patch, batch, all_sources,
                                                                     context=nearby, selected_only=selected_mode, partial=True)
                                    if next_basis.get("fragment_issues"):
                                        partial_document, _ = effective_document(next_basis)
                                        covered = next_basis.get("fragment_coverage", {}).get("covered_source_ids", [])
                                        reused = list(dict.fromkeys(reused + covered))
                                        current = store.checkpoint(current, partial_document, current["processed_ids"],
                                            history_basis=next_basis, reused_ids=reused, log=log)
                                        issue = next_basis["fragment_issues"][0]
                                        raise DraftPatchError(issue.get("message", "部分条目需修订；有效素材已保存"),
                                            entry_id=issue.get("entry_id", ""), path=issue.get("path", ""),
                                            code=issue.get("code", "invalid_entry"), details=issue)
                                    if replaying:
                                        completed = {s["id"] for s in batch}
                                        restored = [r for r in pending if r["source_id"] in completed and r.get("kind") == "content"]
                                        next_basis["pending"] = [r for r in pending if r["source_id"] not in completed]
                                        next_basis["document"]["ignored"] = [r for r in next_basis["document"].get("ignored", [])
                                            if not any(r["source_id"] == item["source_id"] and
                                                       (not item.get("quote") or r.get("quote") == item["quote"]) for item in restored)]
                                    document, _ = effective_document(next_basis)
                                else:
                                    document = apply_patch(current["document"], patch, batch, context=nearby, selected_only=selected_mode)
                                from mewcode.orchestration.planning_issues import triage_preparation_questions
                                document = triage_preparation_questions(document)
                                if direct:
                                    next_basis["document"] = copy.deepcopy(document)
                            except (DraftPatchError, LLMError, OrchestrationStreamTimeout,
                                    OrchestrationGenerationLimit) as exc:
                                diagnostic = generation_failure(exc, context)
                                # A mistyped quote or a dropped connection must not throw
                                # away minutes of work: retry this chunk with the reason.
                                if (not auto and attempt == CHUNK_ATTEMPTS) or (auto and not isinstance(exc, DraftPatchError)) or (isinstance(exc, OrchestrationStreamTimeout)
                                                                 and exc.phase == "queue") or (
                                        isinstance(exc, LLMError) and not diagnostic["retryable"]):
                                    raise
                                repairable = isinstance(exc, DraftPatchError)
                                interrupted = isinstance(exc, (OrchestrationStreamTimeout, OrchestrationGenerationLimit))
                                message = diagnostic["summary"]
                                log = log + [unfinished_entry("retry", message, diagnostic)]
                                repair = REPAIR_HINT.format(reason=message) if repairable else ""
                                chunk_active = False
                                if interrupted and len(batch) > 1:
                                    previous_size = len(batch)
                                    source_limit = max(1, previous_size // 2)
                                    batch = batch[:source_limit]
                                    log[-1]["error"] += f"；旧请求已结束，批次从 {previous_size} 缩小为 {source_limit} 个来源，原文和已完成游标不变"
                                    log[-1]["next_source_limit"] = source_limit
                                    prompt, nearby, batch_chars = batch_prompt(batch)
                                # Persist retry decisions as well as successful
                                # chunks, so stop/reopen never restarts a huge block.
                                current = store.checkpoint(current, current["document"], current["processed_ids"],
                                    log=log, source_limit=source_limit)
                                if auto:
                                    decision = await extraction_control.assess(raw, ask_model=ask_extraction_control,
                                        stalled=True, feedback={"phase": "extracting", "validation_error": message,
                                            "processed_sources": len(current["processed_ids"]), "pending_batch": [s["id"] for s in batch]},
                                        checkpoint=save_extraction_control)
                                    if decision:
                                        repair += "\n本轮后续修法：" + decision["next_step"]
                                if not repairable and not interrupted:
                                    await asyncio.sleep(2)
                                continue
                            break
                        log = log + [{"chunk": n, "sources": len(batch), "chars": batch_chars,
                                      "first_line": batch[0].get("line"), "last_line": batch[-1].get("line"),
                                      "started_at": chunk_started_at,
                                      "duration_ms": int((time.monotonic() - chunk_t0) * 1000),
                                      "phase": "history" if direct else "draft", "restoring": replaying,
                                      "upserts": len(patch.get("upserts") or []),
                                      "ignored": len(patch.get("ignored") or []), "status": "ok"}]
                        if replaying:
                            reused = list(dict.fromkeys(reused + [s["id"] for s in batch]))
                        cursor = advance_cursor(sources, current["processed_ids"],
                                                [] if replaying else [s["id"] for s in batch], reused_ids=reused)
                        current = store.checkpoint(current, document, cursor, log=log,
                                                  **({"history_basis": next_basis, "reused_ids": reused} if direct else {}))
                        if auto and raw:
                            await extraction_control.assess(raw, ask_model=ask_extraction_control,
                                feedback={"phase": "extracting", "processed_sources": len(cursor),
                                          "total_sources": len(sources)}, checkpoint=save_extraction_control)
                        chunk_active = False
                    if direct and not capacity_resume:
                        frozen_basis = freeze_basis(current["history_basis"])
                        current = store.checkpoint(current, current["document"], current["processed_ids"],
                                                   "working" if auto else "ready", log=log, history_basis=frozen_basis)
                    if auto:
                        build = current.get("autobuild") or {}
                        if build.get("phase") == "extracting":
                            build = {**new_build_state(current["document"], now=build["started_at"]),
                                     "deadline_at": build["deadline_at"],
                                     "call_attempts": build.get("call_attempts") or 0,
                                     "retired_extraction_calls": max(0, (build.get("model_calls") or 0) - extraction_control.state["model_calls"]),
                                     "control": copy.deepcopy(extraction_control.state),
                                     "previous_attempts": build.get("previous_attempts", [])}
                            current = store.checkpoint(current, current["document"], current["processed_ids"],
                                                       autobuild=build, log=log)

                        completion_client = None
                        review_client = None
                        review_route = None
                        review_context = {}

                        def start_model_progress(request_kind, label):
                            section = "gap_fill" if request_kind == "gap_fill" else "autofix"
                            rounds = (current["autobuild"].get(section) or {}).get("rounds", [])
                            progress[key] = {"phase": current["autobuild"]["phase"],
                                "job_started_at": job_started_at, "request_started_at": now(),
                                "request_kind": request_kind, "request_round": len(rounds) + 1,
                                "model_label": label, "model": {"stage": "preparing",
                                    "detail": "正在准备本次模型请求。", "provider_events": 0,
                                    "thinking_chars": 0, "output_chars": 0, "output_chunks": 0}}

                        async def ask_completion(system, user):
                            nonlocal completion_client
                            phase = current["autobuild"]["phase"]
                            rounds = (current["autobuild"].get("autofix") or {}).get("rounds", [])
                            start_model_progress("gap_fill" if phase == "gap_fill" else
                                                 "repair" if rounds else "completion", model_label)
                            if completion_client is None:
                                completion_client = srv._new_orchestration_client(
                                    context, "" if effort == "auto" else effort)
                            from mewcode.orchestration.graph_models import available_model_routes
                            payload = json.loads(user)
                            progress[key]["focus_entry_ids"] = list({i for d in payload.get("diagnostics", []) for i in d.get("entry_ids", [])})
                            payload["available_model_routes"] = available_model_routes(srv.MODEL_REGISTRY)
                            user = json.dumps(payload,ensure_ascii=False)
                            return await collect_model_text(completion_client, system=system, user_text=user,
                                                            total_timeout=None, max_generated_chars=None,
                                                            progress=on_progress)

                        async def ask_independent_review(system, user):
                            nonlocal review_client, review_route
                            start_model_progress(current["autobuild"]["phase"], model_label)
                            if review_client is None:
                                review_route = srv._load_review_model()
                                if review_route.get("model_id"):
                                    review_context.update(provider_key=review_route.get("provider_key", ""),
                                                          model=review_route["model_id"])
                                    resolved = srv.MODEL_REGISTRY.resolve(ModelRef(
                                        review_route["provider_key"], review_route["model_id"]))
                                    review_context.update(provider=getattr(resolved, "config", None),
                                        provider_name=getattr(getattr(resolved, "provider", None), "label", ""),
                                        requested_effort=getattr(resolved, "effort", ""))
                                    review_client = resolved.client
                                    if context.get("usage_service") is not None:
                                        context["usage_service"].bind_client(review_client)
                                else:
                                    review_context.update(context)
                                    review_client = srv._new_orchestration_client(
                                        context, "" if effort == "auto" else effort)
                            progress[key]["model_label"] = review_route.get("model_id") or model_label
                            return await collect_model_text(review_client, system=system, user_text=user,
                                                            total_timeout=None, max_generated_chars=None,
                                                            progress=on_progress)

                        async def save_build(build_state, document):
                            nonlocal current
                            current = store.checkpoint(current, document, current["processed_ids"],
                                                       "working", log=log, autobuild=build_state)
                            previous_progress = progress.get(key) or {}
                            previous_model = previous_progress.get("model") or {}
                            if previous_model.get("stage") in {"received", "parsing"}:
                                previous_model = {**previous_model, "stage": "validating",
                                    "detail": "模型提案已接收，正在校验结构并保存候选图。"}
                            progress[key] = {**previous_progress, "phase": build_state["phase"],
                                             "model_calls": build_state["model_calls"],
                                             "job_started_at": job_started_at,
                                             "model": previous_model or {"stage": "checkpointing",
                                                 "detail": "正在保存构图进度，随后继续下一阶段。"}}

                        review_sources = coverage_sources(current["history_basis"], all_sources,
                            start_source_id=(store.get().get("task_design") or {}).get("source_boundary"))
                        if capacity_resume:
                            from mewcode.orchestration.graph_review import _with_decisions
                            if capacity_digest(_with_decisions(review_sources, current["document"])) != preflight.get("review_sources_hash"):
                                raise ValueError("实际审查来源与容量预检不一致，未派发模型请求。")
                        result = await run_autobuild(current["document"], base, review_sources,
                            conversation_id=key[1], revision=current["base_revision"] + 1,
                            ask_model=ask_completion, ask_review=ask_independent_review,
                            state=build, on_checkpoint=save_build, autonomous=True, review_context=review_context,
                            plan_only=(store.get().get("task_design") or {}).get("intent") == "plan")
                        check = readiness(result["document"], base, conversation_id=key[1], revision=current["base_revision"] + 1)
                        if not check["ready"] and result["build"]["status"] == "ready":
                            result["build"].update(status="needs_review", reason="structural_not_ready", remaining=check["diagnostics"])
                        current = store.checkpoint(current, result["document"], current["processed_ids"],
                                                   "ready", log=log, autobuild=result["build"], readiness_result=check)
                except ModelStopped as exc:
                    build = {**(current.get("autobuild") or {}), "status": "needs_review", "reason": exc.code,
                             "stop_summary": exc.reason, "control": copy.deepcopy(extraction_control.state)}
                    try:
                        store.checkpoint(current, current["document"], current["processed_ids"], "paused",
                                         "AI 已保存当前进度并收束，可继续构图。", log=log, autobuild=build)
                    except ValueError:
                        pass
                except AutoBuildDeadline:
                    build = {**new_build_state(current["document"], now=current["autobuild"]["started_at"]),
                             "deadline_at": current["autobuild"]["deadline_at"],
                             "previous_attempts": current["autobuild"].get("previous_attempts", [])}
                    check = readiness(current["document"], base, conversation_id=key[1], revision=current["base_revision"] + 1)
                    build.update(phase="done", status="needs_review", reason=TIME_LIMIT_REASON,
                                 remaining=check["diagnostics"])
                    try:
                        current = store.checkpoint(current, current["document"], current["processed_ids"],
                                                   "ready", log=log, autobuild=build, readiness_result=check)
                    except ValueError:
                        pass
                except asyncio.CancelledError:
                    try:
                        paused = unfinished_entry("paused", "已停止，成功的块已保留，可继续整理") if chunk_active else None
                        store.checkpoint(current, current["document"], current["processed_ids"], "paused",
                                         "已停止，成功的块已保留，可继续整理",
                                         log=log + [paused] if paused else log)
                    except ValueError:
                        pass
                except Exception as exc:
                    try:
                        diagnostic = generation_failure(exc, context)
                        saved = sum(row.get("status") == "ok" for row in log)
                        retained = f"已保存 {saved} 块，原始对话保留" if saved else "尚无成功块，原始对话已保留"
                        message = diagnostic["summary"] + "；" + retained + "。"
                        failed = unfinished_entry("failed", message, diagnostic) if chunk_active else None
                        build = current.get("autobuild")
                        if build:
                            build = {**build, "status": "failed", "reason": "model_error",
                                     "failure": diagnostic}
                        store.checkpoint(current, current["document"], current["processed_ids"], "failed", message,
                                         log=log + [failed] if failed else log,
                                         **({"autobuild": build} if build else {}))
                    except ValueError:
                        pass
                finally:
                    progress.pop(key, None)
                    if jobs.get(key) is asyncio.current_task():
                        jobs.pop(key, None)

            async def run():
                token = bind_runtime_scheduler(srv.APP_RUNTIME_SCHEDULER)
                try:
                    await run_scoped()
                finally:
                    reset_runtime_scheduler(token)

            jobs[key] = asyncio.create_task(run(), name="workflow-draft:" + key[1])
            return JSONResponse({"accepted": True, **public_state(store, key, graph=direct)}, status_code=202)
        except (ValueError, OSError, TypeError, RuntimeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    @app.post("/api/orchestration/draft/action")
    @app.post("/api/orchestration/graph-generation/action")
    @app.post("/api/orchestration/graph-autobuild/action")
    async def draft_action(request: Request):
        try:
            body = await body_of(request)
            key = scope(body)
            store, action = DraftStore(*key), body.get("action")
            running = key in jobs and not jobs[key].done()
            if action == "stop":
                if running:
                    local_job = (progress.get(key) or {}).get("request_kind") == "local_repair"
                    jobs[key].cancel()
                    try:
                        await jobs[key]
                    except asyncio.CancelledError:
                        candidate = store.get().get("candidate")
                        if candidate and not local_job:
                            store.checkpoint(candidate, candidate["document"], candidate["processed_ids"], "paused", "已停止，可继续整理")
                return public_state(store, key, graph=graph_request(request))
            if running:
                raise ValueError("请先停止整理，再编辑或确认候选底稿")
            if graph_request(request) and action in {"exclude_content", "restore_content", "select_plan"}:
                await refresh(store, source_path(*key[:2]))
                scope(body)
                if key in jobs and not jobs[key].done():
                    raise ValueError("当前仍在构建，请先停止再修改构图依据")
                base = current_spec(*key[:2])
                if body.get("base_graph_hash") != digest(base):
                    raise ValueError("画布已变化，请刷新后再修改构图依据")
                store.change_history(base, action, candidate_id=body.get("candidate_id"), sequence=body.get("sequence"),
                    expected_revision=body.get("expected_revision"), source_hash=body.get("source_hash"),
                    entry_id=body.get("entry_id"), source_ids=body.get("source_ids"), quote=body.get("quote"))
                return public_state(store, key, graph=True)
            if graph_request(request) and action != "edit":
                raise ValueError("直接生成图无需确认文字底稿；请在图上编辑并保存")
            if action == "edit":
                store.edit(body.get("candidate_id"), body.get("sequence"), body.get("entry_id"),
                           body.get("changes"), body.get("keep_previous") is True)
            elif action == "confirm":
                await refresh(store, source_path(*key[:2]))
                scope(body)
                store.confirm(body.get("candidate_id"), body.get("sequence"))
            elif action == "restore":
                store.restore(body.get("revision"), body.get("expected_revision"))
            else:
                raise ValueError("未知底稿操作")
            return public_state(store, key, graph=graph_request(request))
        except (ValueError, OSError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    @app.post("/api/orchestration/graph-autobuild/discussion")
    async def graph_discussion(request: Request):
        from mewcode.orchestration.graph_discussion import DISCUSSION_SYSTEM
        binding = None
        task = None
        try:
            body = await body_of(request)
            key = scope(body)
            store = DraftStore(*key)
            await refresh(store, source_path(*key[:2]))
            scope(body)
            if key in jobs and not jobs[key].done():
                raise ValueError("当前构建或讨论仍在处理中，请等待或停止后继续。")
            base = current_spec(*key[:2])
            action = body.get("action", "send")
            identity = {"candidate_id": body.get("candidate_id"), "sequence": body.get("sequence")}
            if action in {"adopt", "discard", "continue", "choose"}:
                selections, prepared_options = body.get("selections"), None
                if action == "choose" and body.get("choice_bundle_id"):
                    from frontend.interaction_choices import resolve_graph_selections
                    resolved = resolve_graph_selections(srv, selections,
                        str(body["choice_bundle_id"]), str(body.get("choice_version") or ""),
                        conversation_id=key[1], **identity)
                    selections = resolved['selections']
                    prepared_options = resolved['prepared_options']
                store.discuss(base, action, **identity, proposal_id=body.get("proposal_id"),
                              selections=selections, prepared_options=prepared_options, choice_notes=body.get('notes'))
                return public_state(store, key, graph=True)
            if action != "send":
                raise ValueError("未知构图讨论操作。")
            context = srv._orchestration_attempt_context(str(body.get("orchestration_effort") or ""))
            binding, prompt = store.discuss(base, "begin", **identity, message=body.get("message"))
            from mewcode.orchestration.workflow_blocks import WorkflowBlocks, WORKFLOW_BLOCK_GUIDANCE
            from mewcode.orchestration.graph_models import available_model_routes
            blocks = WorkflowBlocks(key[0], key[1], source_path=source_path(*key[:2]), base_spec=lambda: base,
                                    guard=lambda: srv._active_conversation_id() == key[1] and srv.SESSION.work_dir == key[0],
                                    model_routes=lambda: available_model_routes(srv.MODEL_REGISTRY))
            material_view = await asyncio.to_thread(blocks.read)
            payload = json.loads(prompt)
            payload["workflow_materials"] = {k: material_view[k] for k in (
                "sequence", "source_hash", "base_graph_hash", "document", "source_index", "contract", "pending",
                "available_model_routes", "available_tool_services", "pending_contract")}
            prompt = json.dumps(payload, ensure_ascii=False)
            async def converse():
                token = bind_runtime_scheduler(srv.APP_RUNTIME_SCHEDULER)
                try:
                    effort = context.get("requested_effort", "")
                    client = srv._new_orchestration_client(context, "" if effort == "auto" else effort)
                    progress[key] = {"phase": "discussion", "request_started_at": now(),
                                     "model_label": context.get("model", ""), "model": {}}
                    def update(value):
                        progress[key]["model"] = {k: value[k] for k in (
                            "stage", "detail", "thinking_chars", "output_chars", "provider_events") if k in value}
                    material_receipt = None
                    view = material_view
                    saved_ids = []
                    current_prompt = prompt
                    for repair_attempt in range(CHUNK_ATTEMPTS):
                        raw = await collect_model_text(client, system=DISCUSSION_SYSTEM + "\n" + WORKFLOW_BLOCK_GUIDANCE,
                            user_text=current_prompt, total_timeout=900, max_generated_chars=None, progress=update)
                        scope(body)
                        response = parse_patch(raw, 1)
                        material_patch = response.pop("materials", None) if isinstance(response, dict) else None
                        if material_patch is None:
                            break
                        if (not isinstance(material_patch, dict) or not {"upserts", "ignored"} <= set(material_patch)
                                or set(material_patch) - {"upserts", "ignored", "withdraw_pending"}
                                or not isinstance(material_patch["upserts"], list) or not isinstance(material_patch["ignored"], list)):
                            raise ValueError("节点素材应使用 upserts/ignored 增量协议；原图未改动。")
                        receipt = await asyncio.to_thread(blocks.submit,
                            expected_sequence=view["sequence"], source_hash=view["source_hash"],
                            base_graph_hash=view["base_graph_hash"], **material_patch, turn_id=binding["turn_id"])
                        saved_ids = list(dict.fromkeys(saved_ids + receipt["saved_entry_ids"]))
                        material_receipt = {"sequence": receipt["sequence"], "saved_entry_ids": saved_ids,
                                            "pending": receipt["pending"], "withdrawn_pending_ids": receipt.get("withdrawn_pending_ids", [])}
                        if not receipt["pending"] or repair_attempt + 1 == CHUNK_ATTEMPTS:
                            break
                        view = await asyncio.to_thread(blocks.read)
                        repair_payload = {**payload, "workflow_materials": {k: view[k] for k in (
                            "sequence", "source_hash", "base_graph_hash", "document", "source_index", "contract", "pending", "pending_contract")},
                            "repair": {"diagnostics": receipt["pending"], "pending_upserts": view["pending_upserts"],
                                "instruction": "本轮独立有效素材已经保存。只修订错误条目及其依赖，引用真实来源；不要新增用户授权或改业务要求。"}}
                        current_prompt = json.dumps(repair_payload, ensure_ascii=False)
                    store.discuss(current_spec(*key[:2]), "complete", binding=binding, response=response,
                                  material_receipt=material_receipt)
                    await refresh(store, source_path(*key[:2]))
                except BaseException as exc:
                    detail = str(exc) if isinstance(exc, ValueError) else "讨论回复未完成，已保存你的消息；可继续讨论。"
                    try:
                        store.discuss(base, "fail", binding=binding, error=detail)
                    except ValueError:
                        pass
                    raise
                finally:
                    progress.pop(key, None)
                    if jobs.get(key) is asyncio.current_task():
                        jobs.pop(key, None)
                    reset_runtime_scheduler(token)
            task = asyncio.create_task(converse(), name="graph-discussion:" + key[1])
            jobs[key] = task
            await task
            return public_state(store, key, graph=True)
        except asyncio.CancelledError:
            return JSONResponse({"error": "讨论已停止，已保存消息和原图。"}, status_code=409)
        except (ValueError, OSError, TypeError, LLMError, OrchestrationStreamTimeout, OrchestrationGenerationLimit) as exc:
            detail = str(exc) if isinstance(exc, ValueError) else f"讨论请求失败（{type(exc).__name__}）；消息和原图保留。"
            return JSONResponse({"error": detail}, status_code=409)

    @app.post("/api/orchestration/graph-generation/repair")
    async def graph_repair(request: Request):
        try:
            body = await body_of(request)
            key = scope(body)
            if key in jobs and not jobs[key].done():
                raise ValueError("请先等待生成结束，再交给 AI 修订；未中断当前生成。")
            store = DraftStore(*key)
            action = body.get("action")
            if action == "local":
                from mewcode.orchestration.graph_build_control import ModelStopped
                # The job is registered before the preparation task is
                # awaited.  The run is created only after request identity
                # and the current canvas pass their CAS checks.
                repair_run = None
                progress[key] = {"phase": "repair", "request_kind": "local_repair", "request_started_at": now(),
                                 "model": {"stage": "preparing"}}
                def report(update):
                    if key not in progress:
                        return
                    if "request_round" in update:
                        progress[key]["request_round"] = update["request_round"]
                    if "focus_entry_ids" in update:
                        progress[key]["focus_entry_ids"] = update["focus_entry_ids"]
                    subset = {k: update[k] for k in ("stage", "detail", "thinking_chars", "thinking_summary_chars",
                              "output_chars", "output_chunks", "provider_events", "stalled_phase", "waiting") if k in update}
                    subset["updated_at"] = now()
                    if update.get("provider_event"):
                        subset["last_event_at"] = subset["updated_at"]
                        subset["waiting"] = False
                    if update.get("request_admitted"):
                        progress[key]["request_started_at"] = subset["updated_at"]
                    progress[key]["model"] = {**progress[key]["model"], **subset}
                async def generate_local():
                    nonlocal repair_run
                    # Register the job before any await, including source refresh,
                    # so an early Stop cancels preparation as well as the model.
                    await refresh(store, source_path(*key[:2]))
                    scope(body)
                    base_now = current_spec(*key[:2])
                    state_now = store.get()
                    candidate_now = state_now.get("candidate")
                    # Current clients send the exact identity.  Derive it for
                    # older callers that predate this CAS contract.
                    requested_revision = body.get("revision", state_now["revision"])
                    expected_hash = body.get("base_graph_hash", digest(base_now))
                    requested_id = body.get("candidate_id", candidate_now.get("id") if candidate_now else None)
                    requested_sequence = body.get("sequence", candidate_now.get("sequence") if candidate_now else None)
                    candidate = store.prepare_local_repair(
                        base_now, candidate_id=requested_id, sequence=requested_sequence,
                        expected_revision=requested_revision, expected_base_graph_hash=expected_hash)
                    repair_run = store.start_local_repair(
                        candidate["id"], candidate["sequence"],
                        expected_revision=requested_revision,
                        expected_base_graph_hash=expected_hash)
                    from mewcode.orchestration.draft_keys import duplicate_groups
                    if candidate and duplicate_groups(candidate["document"]):
                        return store.preview_graph_dedupe(current_spec(*key[:2]))
                    state_now = store.get()
                    seed = None
                    if body.get("preview_id"):
                        seed = state_now.get("repair_preview")
                        if not seed or seed.get("id") != body["preview_id"]:
                            raise ValueError("修订版本已变化，请重新打开当前图。")
                        if not seed.get("local") or seed.get("dedupe"):
                            seed = None  # Wider chat changes retain their separate explicit adoption scope.
                    resume = (state_now.get("local_repair_run") or {}).get("work")
                    req = state_now.get("repair_request") or {}
                    if resume and resume.get("ticket") == req.get("id") and req.get("local"):
                        from mewcode.orchestration.graph_repair import require_current
                        require_current(state_now, base_now, req["id"])
                        packet = {"ticket": req["id"], "prompt": resume["prompt"]}
                    else:
                        resume = None
                        packet = store.request_local_graph_repair(base_now)
                    from mewcode.orchestration.graph_design_provenance import unsupported_designs
                    from mewcode.orchestration.graph_local_repair import SCHEMA as LOCAL_SCHEMA, validate_document
                    if candidate and unsupported_designs(candidate["document"], candidate.get("autobuild") or {}):
                        check = validate_document(candidate["document"], base_now, conversation_id=key[1], revision=store.get()["revision"] + 1)
                        if check["valid"]:
                            return store.preview_graph_repair(base_now, {"schema": LOCAL_SCHEMA,
                                "ticket": packet["ticket"], "summary": "当前图结构已通过校验，以下仅整理旧版未采用提案留下的说明。",
                                "upserts": []})
                    context = srv._orchestration_attempt_context(str(body.get("orchestration_effort") or ""))
                    local_effort = context.get("requested_effort") or "auto"
                    progress[key]["model_label"] = str(context.get("model") or "")
                    token = bind_runtime_scheduler(srv.APP_CONTEXT.get("runtime_scheduler"))
                    try:
                        client = srv._new_orchestration_client(context, "" if local_effort == "auto" else local_effort)
                        from mewcode.orchestration.graph_repair_build import build_repair
                        async def ask(system, user):
                            progress[key]["request_started_at"] = now()
                            progress[key]["model"] = {"stage": "preparing", "provider_events": 0,
                                                      "thinking_chars": 0, "output_chars": 0, "output_chunks": 0}
                            return await collect_model_text(client, system=system, user_text=user,
                                total_timeout=None, max_generated_chars=None, progress=report)
                        def guard():
                            scope(body)
                            from mewcode.orchestration.graph_repair import require_current
                            require_current(store.get(), current_spec(*key[:2]), packet["ticket"])
                        async def checkpoint(work):
                            guard()
                            work["ticket"] = packet["ticket"]
                            store.checkpoint_local_repair(repair_run, work)
                        result = await build_repair(store.get(), base_now, packet, ask_model=ask,
                            checkpoint=checkpoint, report=report, guard=guard, resume=resume, seed=seed)
                        guard()
                        return store.save_local_repair_preview(current_spec(*key[:2]), result)
                    finally:
                        reset_runtime_scheduler(token)
                task = asyncio.create_task(generate_local())
                jobs[key] = task
                try:
                    result = await task
                    if repair_run:
                        store.finish_local_repair(repair_run, "completed")
                except ModelStopped as exc:
                    stopped = exc.code == "model_stopped"
                    message = exc.reason if stopped else "AI 暂未形成可采用的修订，当前图和修复进度已保留，可继续完善。"
                    if repair_run:
                        store.finish_local_repair(repair_run, "needs_review" if stopped else "failed", message)
                    if not stopped:
                        return JSONResponse({"error": message}, status_code=409)
                    result = {"local": True, "repair_status": "model_stopped", "summary": message,
                              "changes": [], "validation": {"valid": False}, "adoption": {"allowed": False}}
                except asyncio.CancelledError:
                    if repair_run:
                        store.finish_local_repair(repair_run, "paused", "修复已停止；当前图和已完成进度保留。")
                    return JSONResponse({"error": "修复已停止，当前图和修复进度已保留。"}, status_code=409)
                except Exception as exc:
                    detail = (str(exc) if isinstance(exc, ValueError) else
                              "模型连接或输出未完成，当前图和修复进度已保留，可继续。" if isinstance(exc, (OrchestrationStreamTimeout, OrchestrationGenerationLimit)) else
                              "修复请求未完成，当前图和修复进度已保留，可继续。")
                    if repair_run:
                        store.finish_local_repair(repair_run, "failed", detail)
                    return JSONResponse({"error": detail}, status_code=409)
                finally:
                    if jobs.get(key) is task:
                        jobs.pop(key, None)
                        progress.pop(key, None)
            else:
                await refresh(store, source_path(*key[:2]))
                scope(body)
                if key in jobs and not jobs[key].done():
                    raise ValueError("当前正在生成，请稍后再修订。")
                base = current_spec(*key[:2])
                if action == "request":
                    result = store.request_graph_repair(base, body.get("entry_id"))
                elif action == "preview":
                    result = store.preview_graph_repair(base, body.get("proposal"))
                elif action == "dedupe_preview":
                    result = store.preview_graph_dedupe(base, body.get("choices"))
                elif action == "adopt":
                    store.adopt_graph_repair(base, body.get("preview_id"))
                    result = {"adopted": True, **public_state(store, key, graph=True)}
                else:
                    raise ValueError("未知修订操作。")
            return {**result, "project": key[0], "conversation_id": key[1]}
        except (ValueError, OSError, TypeError, LLMError, OrchestrationStreamTimeout, OrchestrationGenerationLimit) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    @app.post("/api/orchestration/draft/graph")
    @app.post("/api/orchestration/graph-generation/graph")
    @app.post("/api/orchestration/graph-autobuild/graph")
    async def draft_graph(request: Request):
        try:
            body = await body_of(request)
            key = scope(body)
            if body.get("task_design_expected"):
                await check_task_build(*key[:2], body["task_design_expected"])
            store = DraftStore(*key)
            direct = graph_request(request)
            state = await refresh(store, source_path(*key[:2])) if direct else store.get()
            scope(body)
            if body.get("task_design_expected"):
                await check_task_build(*key[:2], body["task_design_expected"])
            if key in jobs and not jobs[key].done():
                raise ValueError("生成尚未结束，请等待或停止；未保存未完成的图")
            if not direct and (not state["revision"] or state.get("candidate")):
                raise ValueError("请先核对并确认底稿版本，再生成流程图")
            candidate = state.get("candidate")
            base = current_spec(*key[:2])
            if body.get("action") == "apply":
                preview = state.get("graph_candidate")
                if not preview or preview["id"] != body.get("preview_id") or preview["revision"] != state["revision"]:
                    raise ValueError("预览已过期，请重新生成并核对差异")
                if preview.get("applied_hash") == digest(base):
                    return {"spec": base, "project": key[0], "conversation_id": key[1], "already_applied": True}
                if direct:
                    ready_graph_document(state, candidate, base, conversation_id=key[1])
                    if ((preview.get("source_hash") != digest(state["source_ids"])
                            and not (body.get("task_design_expected") or {}).get("requirement_fingerprint"))
                            or (candidate and (preview.get("candidate_id"), preview.get("sequence")) != (candidate["id"], candidate["sequence"]))):
                        raise ValueError("来源或候选图已变化，请重新查看图后保存")
                if digest(base) != preview["base_hash"]:
                    raise ValueError("画布已被修改，未覆盖；请重新生成差异预览")
                if body.get("task_design_expected"):
                    await check_task_build(*key[:2], body["task_design_expected"])
                if srv.SESSION.chat_busy():
                    raise ValueError("当前任务仍在执行，可保存底稿和预览；请在任务空闲后应用流程")
                preserved_runs = []
                for run in srv.EXPERT_RUNS.list_for_conversation(key[1]):
                    if run.get("status") in {"running", "pending"}:
                        raise ValueError("当前编排仍在执行或等待启动，请停止执行后再保存新版阶段图")
                    if run.get("status") in {"paused", "waiting", "blocked"}:
                        preserved_runs.append({"run_id": run.get("run_id") or run.get("id"), "status": run["status"]})
                # Reuse the version-checked canvas writer, not just the plan
                # cache: old open editors must conflict with this new snapshot.
                class CanvasRequest:
                    async def json(self):
                        return {"spec": preview["spec"], "mode": "replace", "conversation_id": key[1],
                                "task_design_expected": body.get("task_design_expected"),
                                "expected_version": ((base or {}).get("meta") or {}).get("pipeline_graph_version")}
                response = await srv.api_orchestration_open_canvas(CanvasRequest())
                if response.status_code >= 400:
                    return response
                saved = json.loads(response.body)
                store.mark_graph_applied(preview["id"], saved["spec"])
                # Saving a plan never rewrites a run's immutable locked_flow.
                # Paused/history snapshots remain available for explicit recovery.
                saved["preserved_runs"] = preserved_runs
                return saved
            document = ready_graph_document(state, candidate, base, conversation_id=key[1]) if direct else state["document"]
            if direct and candidate and candidate["base_graph_hash"] != digest(base):
                raise ValueError("画布已被修改，未覆盖；请按新图重新生成")
            spec = project_graph(document, base, conversation_id=key[1], revision=state["revision"] + bool(candidate))
            design = state.get("task_design")
            if design:
                from mewcode.orchestration.task_design import graph_identity
                spec.meta["task_design"] = graph_identity(design, state)
                from mewcode.orchestration.collab_compile import attach_design
                from mewcode.orchestration.workflow_blocks import block_context_sources
                attach_design(spec, design, state, sources=block_context_sources(store.sources(), state))
            changes = graph_changes(base, spec.as_dict())
            preview = store.save_graph_preview(spec.as_dict(), base, state["revision"], changes, candidate=candidate)
            return {**preview, "changes": changes, "conversation_id": key[1], "project": key[0]}
        except (ValueError, OSError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    async def check_task_build(project, cid, expected):
        from mewcode.orchestration.workflow_blocks import WorkflowBlocks
        from mewcode.orchestration.task_design import TaskDesign
        scope({"project": project, "conversation_id": cid})
        if not expected:
            raise ValueError("缺少构图规格版本，请重新读取并提交；未保存或启动。")
        design = (DraftStore.read_state(project, cid) or {}).get("task_design") or {}
        if (expected.get("task_id") and (design.get("task_id"), design.get("revision")) !=
                (expected["task_id"], expected.get("task_revision"))):
            raise ValueError("任务已暂停或规格版本变化，未采用旧构图请求。")
        if expected.get("build_id") or expected.get("build_epoch"):
            TaskDesign(project, cid).check_build(expected)
        view = WorkflowBlocks(project, cid, source_path=source_path(project, cid),
                              base_spec=lambda: current_spec(project, cid)).current_basis()
        if (view["warnings"] or expected.get("sequence") != view["sequence"]
                or expected.get("base_graph_hash") != view["base_graph_hash"]
                or (expected.get("requirement_fingerprint") != view["requirement_fingerprint"]
                    if expected.get("requirement_fingerprint") else expected.get("user_source_hash") != view["user_source_hash"])):
            raise ValueError("提交构图后的素材、用户要求或画布已经变化，请重新读取规格并提交；未保存或启动。")

    async def build_for_task(project, cid, *, expected):
        from frontend.task_design import LocalRequest, response_value
        body = {"project": project, "conversation_id": cid, "include_new": True}
        key = scope(body)
        await check_task_build(project, cid, expected)
        state = DraftStore.read_state(project, cid) or {}
        current_request = (state.get("task_design") or {}).get("build_request") or {}
        receipt = current_request.get("capacity_revalidation")
        if receipt != expected.get("capacity_revalidation"):
            raise ValueError("容量恢复回执已变化，未续接旧构图请求。")
        if receipt:
            body["include_new"] = False
        request = LocalRequest(body, "/api/orchestration/graph-autobuild/update")
        if receipt:
            request._capacity_resume = expected
        response_value(await update_draft(request))
        pending = jobs.get(key)
        if pending:
            await pending
        scope(body)
        await check_task_build(project, cid, expected)
        state = DraftStore(*key).get()
        candidate = state.get("candidate") or {}
        build = candidate.get("autobuild") or {}
        if build.get("status") != "ready":
            from mewcode.orchestration.task_design import TaskBuildError
            review = build.get("final_review") or build.get("review") or {}
            failure = build.get("failure") or review.get("failure") or {}
            retryable = (build.get("reason") in {"model_error", "review_error"} and isinstance(failure, dict)
                         and failure.get("retryable") is True)
            error = (failure.get("summary") or candidate.get("error") or review.get("error")
                     or build.get("reason") or "构图检查未完成，当前成果已保留")
            raise TaskBuildError("构图尚未通过：" + json.dumps({"error": error,
                "reason": build.get("reason"), "remaining": build.get("remaining"),
                "readiness": candidate.get("readiness")}, ensure_ascii=False), retryable=retryable,
                diagnostic={"reason": build.get("reason"), "candidate_id": candidate.get("id"), "failure": failure})

    async def preview_for_task(project, cid, *, expected=None):
        from frontend.task_design import LocalRequest, response_value
        if expected is not None:
            await check_task_build(project, cid, expected)
        return response_value(await draft_graph(LocalRequest({"project": project, "conversation_id": cid,
            "task_design_expected": expected},
            "/api/orchestration/graph-autobuild/graph")))

    async def apply_for_task(project, cid, preview_id, *, expected):
        from frontend.task_design import LocalRequest, response_value
        await check_task_build(project, cid, expected)
        return response_value(await draft_graph(LocalRequest({"project": project, "conversation_id": cid,
            "action": "apply", "preview_id": preview_id,
            "task_design_expected": expected}, "/api/orchestration/graph-autobuild/graph")))

    srv.task_design_build_graph = build_for_task
    srv.task_design_preview_graph = preview_for_task
    srv.task_design_apply_graph = apply_for_task
    srv.task_design_check_build = check_task_build
    from frontend.workflow_design import install as install_workflow_design
    install_workflow_design(srv, check_task_build, current_spec)
