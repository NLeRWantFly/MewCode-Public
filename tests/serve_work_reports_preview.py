"""Isolated UI acceptance fixture using real canvas assets and report routes.

Run: .venv/Scripts/python.exe -m tests.serve_work_reports_preview <fresh-dir> [port]
Only synthetic, explicitly labelled data is used. No real provider calls/stores.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from frontend import orch_activity, orch_reports
from mewcode.orchestration import OrchestrationSpec, to_pipeline_graph
from mewcode.orchestration.activity import ActivityStore
from mewcode.pipeline.store import PipelineStore


def create_preview(root: Path) -> FastAPI:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError("Preview requires an empty, dedicated directory")
    conv, graph_id = "report-ui-fixture", "g_report_ui_fixture"
    spec = OrchestrationSpec.from_value({"label": "工作汇报验收 · 隔离测试数据", "nodes": [
        {"node_id": "len_intro", "label": "len_intro", "kind": "implement", "goal": "拆分引言中的过长句子，并保留原有条件范围。",
         "position": {"x": 100, "y": 120}, "plan_brief": {"summary": "拆分长句并保持原意", "known_inputs": ["此页所有论文内容均为 UI 验收的虚构测试数据。"]}},
        {"node_id": "audit_intro", "label": "核对修改前后的含义", "kind": "audit", "goal": "核对句子拆分后的条件范围是否保持一致。", "position": {"x": 550, "y": 120}},
        {"node_id": "tables", "label": "人工指定名称", "kind": "implement", "goal": "核对实验表格中的数据。", "position": {"x": 550, "y": 480}}],
        "edges": [{"from": "len_intro", "to": "audit_intro"}, {"from": "audit_intro", "to": "tables"}],
        "meta": {"conversation_id": conv, "manual_node_labels": {"tables": "人工指定名称"}}})
    graph = PipelineStore(root).create_pipeline(to_pipeline_graph(spec, graph_id=graph_id))
    store = ActivityStore(root)
    run_id = "chat:ui-fixture"
    store.bind(conv, graph_id, run_id, nodes=orch_activity.graph_nodes(graph))
    base = {"node_id": "len_intro", "kind": "call", "role": "assistant", "status": "completed", "source": "runtime", "attempt": 1}
    for index in range(2):
        store.record(conv, run_id, f"read-{index}", {**base, "name": "ReadFile", "arguments": {"path": "paper/intro.md"},
                     "at": f"2026-09-12T14:00:0{index}Z", "text": "已读取引言第二段（虚构测试数据）。"})
    store.record(conv, run_id, "edit", {**base, "name": "EditFile", "arguments": {"path": "paper/intro.md"},
                 "at": "2026-09-12T14:01:00Z", "text": "成功拆分两处长句，保留段落顺序（虚构测试数据）。"})
    store.artifact(conv, run_id, {"id": "intro-snapshot", "node_id": "len_intro", "name": "引言修改快照.md", "path": "paper/intro.md",
        "created_at": "2026-09-12T14:01:02Z", "preview_type": "text", "preview": "UI 验收虚构数据：引言第二段已拆分两处长句；条件范围仍在审查。"})
    store.record(conv, run_id, "report", {**base, "status": "running", "name": "report_step", "at": "2026-09-12T14:02:00Z",
        "arguments": {"node": "len_intro", "summary": "我已拆分引言第二段中的两处长句。审查发现一处条件范围可能改变，正在返修。",
          "work_object": "论文引言第二段", "purpose": "让句子更容易理解，同时保持原意", "changes": "已将两处长句拆开，保留原段落顺序。",
          "verification": "审查尚未通过，一处条件范围需要修正。", "next_step": "逐项核对原句和改写句的条件范围。"}})
    store.record(conv, run_id, "tables", {**base, "node_id": "tables", "name": "report_step", "at": "2026-09-12T13:59:00Z",
        "arguments": {"node": "tables", "summary": "我已读取实验表格，正在核对第三列的数据来源。", "work_object": "实验表格第三列"}})
    runtime = SimpleNamespace(starting=True, task=None, session=None)
    srv = SimpleNamespace(SESSION=SimpleNamespace(work_dir=str(root), session=None, runtime_for=lambda _: runtime),
        PipelineStore=PipelineStore, EXPERT_RUNS=SimpleNamespace(list_for_conversation=lambda _: []),
        _active_conversation_id=lambda: conv, is_orchestration_graph=lambda _: True, dialogue_focus=lambda _: {},
        _orchestration_attempt_context=lambda: {"provider_key": "fixture", "model": "fixture-no-network", "requested_effort": "high"},
        _new_orchestration_client=lambda context, effort: object())

    async def complete(client, *, system, user_text, **kwargs):
        await asyncio.sleep(1)
        inputs = json.loads(user_text)
        if "nodes" in inputs:
            return json.dumps({"labels": {n["id"]: "拆分引言中的过长句子" for n in inputs["nodes"]}})
        # Same strict output contract as the provider; historical dates and refs
        # are still validated and projected by the real implementation.
        return json.dumps({"cards": [{"text": (e.get("report") or {}).get("summary") or
            ("我已保存引言修改快照，检查尚未结束。" if (e.get("file") or {}).get("name") else
             "我已阅读引言第二段，记录中的长句修改仍需要审查。"), "evidence_refs": [e["id"]]} for e in inputs["events"]]})

    orch_reports.collect_model_text = complete
    app = FastAPI()
    orch_activity.install(app, srv)

    @app.post("/api/orchestration/open-canvas")
    async def open_canvas():
        return {"graph": graph.model_dump(mode="json", by_alias=True), "graph_id": graph_id,
                "project": str(root), "conversation_id": conv}

    @app.get("/api/orchestration/run-state")
    async def state():
        return await orch_activity.canvas_activity(srv, project=str(root), conversation_id=conv, graph_id=graph_id)

    @app.post("/fixture/advance")
    async def advance():
        store.record(conv, run_id, "revised", {**base, "name": "report_step", "status": "running",
            "at": "2026-09-12T14:03:00Z", "arguments": {"node": "len_intro", "summary": "我已修正条件范围，正在重新检查改写句是否保留原意。",
              "changes": "已恢复原句中的限定条件。", "next_step": "提交第二轮审查。"}})
        return {"ok": True}

    @app.post("/fixture/stop")
    async def stop():
        app.state.server.should_exit = True
        return {"ok": True}

    @app.get("/api/{path:path}")
    async def empty_api(path: str):
        return {"models": [], "kinds": [], "files": [], "pending_grants": [], "lessons": []}

    app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parents[1] / "frontend/static"))
    return app


if __name__ == "__main__":
    preview = create_preview(Path(sys.argv[1]))
    server = uvicorn.Server(uvicorn.Config(preview, host="127.0.0.1", port=int(sys.argv[2]) if len(sys.argv) > 2 else 18780))
    preview.state.server = server
    server.run()
