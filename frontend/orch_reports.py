"""Scoped, on-demand readable names and history. No execution tools or graph writes."""
from __future__ import annotations

import asyncio
import json
import re

from fastapi import Request
from fastapi.responses import JSONResponse

from mewcode.orchestration.activity import ActivityStore
from mewcode.orchestration.extract import collect_model_text
from mewcode.orchestration.work_reports import (
    EXPLICIT_ATTRIBUTIONS, JOBS, NAME_SYSTEM, REPORT_SYSTEM, eligible_name,
    local_cards, model_input, parse_result, public_report, validate_cards,
)


def name_nodes(graph):
    payload = json.loads(graph.variables.get("__orch.payload__", "{}"))
    manual = (payload.get("meta") or {}).get("manual_node_labels") or {}
    nodes = []
    for node in graph.nodes:
        if node.id in {"__orch_start__", "__orch_end__"}:
            continue
        meta = (payload.get("nodes") or {}).get(node.id) or {}
        nodes.append({"id": node.id, "label": node.label, "goal": node.goal,
                      "summary": (meta.get("plan_brief") or {}).get("summary", ""),
                      "label_source": "manual" if manual.get(node.id) == node.label else meta.get("label_source", "")})
    return nodes


def install(app, srv, check_scope):
    @app.post("/api/orchestration/readable-report")
    async def readable_report(request: Request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("请求格式无效")
            scope = {key: body[key] for key in ("project", "conversation_id", "graph_id")}
            graph = check_scope(srv, **scope)
            mode = body.get("mode", "history")
            if mode not in {"history", "names"}:
                raise ValueError("未知整理类型")
            scope["mode"] = mode
            activity = ActivityStore(scope["project"])
            nodes = name_nodes(graph)
            try:
                context = srv._orchestration_attempt_context()
            except (ValueError, RuntimeError):
                context = None
            # Cache identity includes the selected provider/model, never credentials.
            scope["provider"] = {k: context.get(k) for k in ("provider_key", "model", "requested_effort")} if context else None

            async def complete(system, inputs):
                if not context:
                    raise ValueError("当前模型未就绪")
                # Freeze the original route/effort; do not use planner recovery or
                # silently choose a different model when the selected one fails.
                effort = context.get("requested_effort", "")
                client = srv._new_orchestration_client(context, "" if effort == "auto" else effort)
                raw = await collect_model_text(client, system=system,
                    user_text=json.dumps(inputs, ensure_ascii=False), total_timeout=360,
                    max_generated_chars=30000)
                return parse_result(raw)

            if mode == "names":
                inputs = [n for n in nodes if eligible_name(n)]
                if not inputs:
                    return {"status": "ready", "labels": {}}
                fallback = {"labels": {}, "based_on": {}}

                async def generate():
                    labels = {}
                    # Bound provider inputs even for large imported graphs.
                    for start in range(0, len(inputs), 50):
                        batch = inputs[start:start + 50]
                        prompt_nodes = [{**n, "goal": str(n.get("goal") or "")[:1800],
                                         "summary": str(n.get("summary") or "")[:800]} for n in batch]
                        result = (await complete(NAME_SYSTEM, {"nodes": prompt_nodes})).get("labels")
                        if not isinstance(result, dict) or set(result) != {n["id"] for n in batch}:
                            raise ValueError("名称整理未覆盖所选节点")
                        for key, label in result.items():
                            if not isinstance(label, str) or not 2 <= len(label.strip()) <= 32 or not re.search(r"[\u4e00-\u9fff]", label):
                                raise ValueError("名称格式不符合要求")
                            labels[key] = label.strip()
                    return {"labels": labels, "based_on": {n["id"]: {k: n.get(k) for k in ("label", "goal")} for n in inputs}}
            else:
                node_id = body.get("node_id")
                if not node_id or node_id not in {n["id"] for n in nodes}:
                    raise ValueError("请选择此画布中的具体节点")
                ids = body.get("event_ids")
                if not isinstance(ids, list) or not 1 <= len(ids) <= 100 or any(not isinstance(e, str) for e in ids):
                    raise ValueError("每次只能整理 1～100 条记录")
                scope.update(node_id=node_id, run_id=body["run_id"], attempt=max(0, int(body.get("attempt") or 0)))
                page = await asyncio.to_thread(activity.page, scope["conversation_id"], scope["graph_id"],
                    scope["run_id"], node_id=node_id, attempt=scope["attempt"], event_ids=ids, limit=100)
                events = page["items"]
                if {e["id"] for e in events} != set(ids) or any(
                        e.get("attribution") not in EXPLICIT_ATTRIBUTIONS or "thinking" in str(e.get("kind") or "") for e in events):
                    raise ValueError("记录已变更或不属于此节点，请刷新后重试")
                # Stable ordering and revisions invalidate edited or re-attributed facts.
                events.sort(key=lambda e: e["id"])
                inputs = events
                fallback = {"cards": local_cards(events), "revisions": {e["id"]: e["revision"] for e in events}}
                legacy = [e for e in events if not public_report(e)]
                reported = [e for e in events if public_report(e)]
                if not legacy:
                    return {**fallback, "status": "ready"}

                async def generate():
                    # Report-step records already contain intentional public prose.
                    # Summarize only legacy facts, and preserve their actual run/attempt.
                    result = await complete(REPORT_SYSTEM, model_input(legacy))
                    cards = validate_cards(result, legacy) + local_cards(reported)
                    cards.sort(key=lambda c: str(c.get("at") or ""), reverse=True)
                    return {"cards": cards, "revisions": fallback["revisions"]}

            result = await JOBS.request(activity=activity, scope=scope, inputs=inputs,
                                        fallback=fallback, generate=generate, retry=body.get("retry") is True)
            check_scope(srv, body["project"], body["conversation_id"], body["graph_id"])
            return result
        except (ValueError, LookupError, KeyError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
