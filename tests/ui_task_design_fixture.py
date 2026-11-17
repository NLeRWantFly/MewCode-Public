"""Local browser acceptance fixture; production components/services, offline model.

Run: .venv/Scripts/python.exe -m tests.ui_task_design_fixture --port 50741
Open /qa. All data is disposable and lives in a fresh workspace directory.
The model response alone is scripted; graph building, review gates, preview,
save, pause and resume use the application's real services.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import socket
from types import SimpleNamespace
import uuid
from fastapi import Request


def main():
    args = argparse.ArgumentParser()
    args.add_argument("--port", type=int, default=50741)
    args.add_argument("--first-turn-delay", type=float, default=45)
    args.add_argument("--collaboration-preview", action="store_true")
    args.add_argument("--recovery-preview", action="store_true")
    options = args.parse_args()
    port = options.port
    root = Path(__file__).resolve().parents[1]
    workspace = root / ".tmp-kernel-audit" / ("ui-task-design-" + uuid.uuid4().hex[:10])
    workspace.mkdir(parents=True)
    Path.home = classmethod(lambda cls: workspace / "home")
    os.environ["MEWCODE_USER_DATA_DIR"] = str(workspace / "userdata")
    os.environ["MEWCODE_DESKTOP_PROFILE_ISOLATED"] = "1"
    os.environ.pop("MEWCODE_DESKTOP_TOKEN", None)
    original_connect = socket.socket.connect

    def offline_connect(sock, address):
        if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "::1", "localhost"}:
            raise OSError("Browser acceptance fixture forbids external connections")
        return original_connect(sock, address)

    socket.socket.connect = offline_connect
    import pytest
    import uvicorn
    from fastapi.responses import HTMLResponse, Response
    from frontend import orch_drafts, server
    from mewcode.conversation import ConversationManager
    from mewcode.orchestration.draft_store import DraftStore
    from mewcode.orchestration.draft_sources import digest
    from mewcode.orchestration.task_design import TaskDesign
    from mewcode.orchestration.workflow_blocks import WorkflowBlocks
    from mewcode.tools.task_design import AdvanceTaskDesign, AdvanceTaskDesignParams
    from mewcode.tools.workflow_blocks import SubmitWorkflowBlocks, SubmitWorkflowBlocksParams
    from tests.test_task_design_entry import task_api_env
    from tests.test_task_design_e2e import _complete_synthetic_material
    from tests.test_workflow_drafts import record, write_history

    patch = pytest.MonkeyPatch()
    current, _, history, owner, _ = task_api_env.__wrapped__(workspace, patch)
    write_history(history, [])
    runtime = SimpleNamespace(work_dir=owner["project"], conversation_id=owner["conversation_id"],
                              conversation=ConversationManager(), starting=False, chat_task=None,
                              execution_kind="chat")
    events, messages = [], []
    runs = 0

    async def event(payload):
        events.append(payload)

    def begin(value):
        value.starting = current["busy"] = True

    def attach(value, task):
        value.chat_task = task
        value.starting = False

    async def interrupt(_cid, **_kwargs):
        task = runtime.chat_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        runtime.starting = current["busy"] = False
        return {"interrupted": True}

    async def model(_client, **kwargs):
        payload = json.loads(kwargs["user_text"])
        if isinstance(payload.get("sources"), list):
            return json.dumps({"upserts": [], "ignored": [
                {"source_id": row["id"], "reason": "目标已记录在合成验收素材"}
                for row in payload["sources"]]}, ensure_ascii=False)
        if isinstance(payload.get("requirements"), list):
            return json.dumps({"gaps": [], "notes": "离线验收样例已覆盖"})
        return json.dumps({"upserts": [], "notes": "保留完整的三个样例节点"})

    async def run_chat(ws, text, *, record_user=False, runtime=runtime):
        nonlocal runs
        runs += 1
        interrupted = failed = False
        messages.append({"role": "assistant", "text": f"第 {runs} 次设计：读取已有目标，整理样例流程。"})
        try:
            # Leave time to exercise pause while the first turn is running.
            await asyncio.sleep(options.first_turn_delay if runs == 1 else 2)
            blocks = WorkflowBlocks(owner["project"], owner["conversation_id"], source_path=history,
                base_spec=lambda: None, guard=lambda: True, model_routes=lambda: [], tool_services=lambda: [])
            before = blocks.read()
            source_id = next(row["id"] for row in reversed(before["source_index"]) if row["role"] == "user")
            source = blocks.read(source_ids=[source_id])["sources"][0]
            rows = _complete_synthetic_material(source)
            rows[0]["body"] = rows[0]["title"] = TaskDesign(**owner).read()["goal"]
            submitted = await SubmitWorkflowBlocks(blocks, turn_id=lambda: "ui-main").execute(
                SubmitWorkflowBlocksParams(expected_sequence=before["sequence"], source_hash=before["source_hash"],
                    base_graph_hash=before["base_graph_hash"], upserts=rows, ignored=[], use_jev=False))
            if submitted.is_error:
                raise ValueError(submitted.output)
            fresh = blocks.read()
            task = TaskDesign(**owner).read()
            result = await AdvanceTaskDesign(blocks).execute(AdvanceTaskDesignParams(action="build",
                expected_revision=task["revision"], expected_sequence=fresh["sequence"],
                source_hash=fresh["source_hash"], base_graph_hash=fresh["base_graph_hash"]))
            if result.is_error:
                raise ValueError(result.output)
            messages.append({"role": "assistant", "text": "已提交 3 个阶段及依赖关系，正在构图、核对和保存。"})
        except asyncio.CancelledError:
            interrupted = True
            messages.append({"role": "assistant", "text": "设计已暂停，目标已保留。"})
            raise
        except Exception as exc:
            failed = True
            messages.append({"role": "assistant", "text": "验收错误：" + str(exc)})
            raise
        finally:
            runtime.starting = current["busy"] = False
            await ws.send_json({"type": "done", "interrupted": interrupted, "error": failed})
            server.task_design_after_turn(runtime, interrupted=interrupted, error=failed)

    async def never_execute(*_args, **_kwargs):
        raise AssertionError("This fixture only designs and saves; business execution is forbidden")

    patch.setattr(server.SESSION, "capture_conversation_runtime", lambda: runtime)
    patch.setattr(server.SESSION, "begin_conversation_turn", begin)
    patch.setattr(server.SESSION, "attach_conversation_task", attach)
    patch.setattr(server, "request_chat_interrupt", interrupt)
    patch.setattr(server, "run_chat", run_chat)
    patch.setattr(server.WS_EVENTS, "send_json", event)
    patch.setattr(server, "_load_review_model", lambda: {})
    patch.setattr(server, "api_orchestration_confirm", never_execute)
    patch.setattr(orch_drafts, "collect_model_text", model)

    if options.collaboration_preview or options.recovery_preview:
        from tests.test_collaboration_mode import store_for, RUN_ID, accept
        from mewcode.kernel import collaboration as ledger
        store = store_for(workspace)
        design = TaskDesign(**owner)
        task = design.start("execute", orchestration_mode="collaborative")
        design.transition(task, "paused", run_id=RUN_ID, goal="整理两份数据并核验合并结果",
                          reason="合成界面验收：第一阶段已通过，可在详情中修订要求。")
        def seed(row):
            row["conversation_id"] = owner["conversation_id"]
            row["collaboration"]["task_id"] = task["task_id"]
            accept(row["collaboration"])
            ledger.event(row["collaboration"], "integration_accepted", scope="milestone:milestone_1")
        store.mutate(RUN_ID, seed, durable=True)
        patch.setattr(server, "EXPERT_RUNS", store)
        if options.recovery_preview:
            from tests.test_collaboration_recovery import exhausted_gate
            from tests.test_collaboration_mode import CATALOG
            # Replace the synthetic accepted history with a fresh rejected unit.
            def reset(row):
                row["collaboration"]["attempts"] = {}
                row["collaboration"]["certificates"] = {}
            store.mutate(RUN_ID, reset, durable=True)
            exhausted_gate(store, Path(owner["project"]))
            async def recovery_catalog(**kwargs):
                return CATALOG
            patch.setattr(server.MODEL_REGISTRY, "catalog", recovery_catalog)

    @server.app.get("/qa")
    async def page():
        html = (root / "frontend/static/index.html").read_text(encoding="utf-8")
        html = html.replace('data-theme="dark"', 'data-theme="light"', 1)
        html = re.sub(r'<script src="/static/app.js[^\"]*"></script>', '<script src="/qa/driver.js"></script>', html)
        return HTMLResponse(html)

    @server.app.get("/qa/state")
    async def state():
        return {"owner": owner, "busy": current["busy"], "runs": runs, "messages": messages,
                "task_design": TaskDesign(**owner).read(), "events": events[-8:]}

    @server.app.post("/qa/message")
    async def message(request: Request):
        text = str((await request.json()).get("text", "")).strip()
        if current["busy"] or not text:
            return Response("Busy or empty", status_code=409)
        write_history(history, [record("user", text)], append=True)
        TaskDesign(**owner).record_turn(text)
        messages.append({"role": "user", "text": text})
        begin(runtime)
        attach(runtime, asyncio.create_task(run_chat(server.ConversationWebSocket(server.WS_EVENTS, owner["conversation_id"]), text)))
        return {"ok": True}

    @server.app.post("/qa/legacy")
    async def legacy():
        store = DraftStore(**owner)
        current_state = store.get()
        base = server._conversation_plan_store(owner["conversation_id"]).get(owner["project"]).spec.as_dict()
        store.prepare_local_repair(base, expected_revision=current_state["revision"],
                                   expected_base_graph_hash=digest(base))
        with store.db() as db:
            data = store._get(db)
            document = (data.get("candidate") or data)["document"]
            node = next(row for row in document["entries"] if row["kind"] == "node")
            node["data"]["capabilities"] = {"possible_blockers": ["OCR能力待调查（验收用旧数据）"]}
            store._put(db, data)
        return {"ok": True}

    @server.app.get("/qa/driver.js")
    async def driver():
        return Response(DRIVER, media_type="application/javascript")

    print(f"Isolated UI fixture: http://127.0.0.1:{port}/qa\nWorkspace: {workspace}", flush=True)
    uvicorn.run(server.app, host="127.0.0.1", port=port, lifespan="off", log_level="warning")


DRIVER = r"""
(async () => {
  const initial = await (await fetch('/qa/state')).json(), owner = initial.owner;
  let busy = false, messageSignature = '';
  document.getElementById('drawer').classList.add('hidden');
  const welcome = document.createElement('div');
  welcome.style.cssText = 'max-width:780px;margin:25px auto;padding:20px';
  welcome.textContent = '隔离验收会话 · 离线模型样例 · 使用真实构图与保存服务';
  const legacy = document.createElement('button'); legacy.textContent = '载入旧字段异常样例';
  legacy.onclick = async () => { await fetch('/qa/legacy', {method:'POST'}); openGraph(); };
  const theme = document.createElement('button'); theme.textContent = '切换明暗主题';
  theme.onclick = () => {document.documentElement.dataset.theme = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';};
  welcome.append(document.createElement('br'), legacy, theme);
  document.getElementById('messages').before(welcome);
  function openGraph() {
    return window.GraphGeneration.open({...owner, mode:'auto', workspace:true,
      isCurrent: () => true, onApplied: () => task.refresh()});
  }
  const task = window.TaskDesign.mount({getScope:()=>owner,getBusy:()=>busy,
    onStart:()=>document.getElementById('chat-input').focus(),onOpenGraph:openGraph});
  document.getElementById('btn-send').onclick = async () => {
    const input = document.getElementById('chat-input');
    if (!input.value.trim()) return;
    const response = await fetch('/qa/message',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:input.value})});
    if (response.ok) input.value = '';
    else welcome.append(document.createTextNode('发送失败：' + response.status + ' ' + await response.text()));
    await refresh(); await task.refresh();
  };
  async function refresh() {
    const state = await (await fetch('/qa/state')).json(); busy = state.busy;
    const signature = JSON.stringify(state.messages);
    if (signature !== messageSignature) {
      messageSignature = signature;
      const host = document.getElementById('messages'); host.replaceChildren();
      state.messages.forEach(row => {
        const node = document.createElement('article');
        node.className = 'msg ' + (row.role === 'user' ? 'user' : 'assistant');
        node.style.cssText = 'max-width:780px;margin:12px auto;padding:18px;border:1px solid var(--border);border-radius:16px;background:var(--bg-panel);white-space:pre-wrap';
        node.textContent = row.text; host.append(node);
      });
    }
  }
  await refresh(); setInterval(refresh, 700);
})();
"""


if __name__ == "__main__":
    main()
