"""Browser acceptance for the real assistance picker and its backend writes."""
import asyncio
import json
import socket
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from playwright.async_api import async_playwright, expect

from frontend import server
from tests.test_capability_executor_slot import REF, api_env, run_env, protected


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [True, False])
async def test_picker_saves_future_and_paused_run_separately(api_env, tmp_path, configured):
    env = api_env
    source = (Path(__file__).resolve().parents[1] / "frontend/static/app.js").read_text(encoding="utf-8")
    picker = source[source.index("  function modelLabel("):source.index("  function createRouteEditor(")]
    slot = source[source.index('        const assistancePanel = document.createElement("section");'):
                  source.index("        const runInputForm = window.OrchestrationRunInputs.create();")]
    models = [dict(env.catalog["models"][0], provider_name="Chosen provider"),
              {"provider_key": "codex", "provider_name": "Codex", "model_id": "gpt-6.1-sol",
               "tools": True, "capabilities": ["tools"], "efforts": ["medium", "high"]},
              {"provider_key": "claude", "provider_name": "Claude", "model_id": "claude-sonnet-4.6",
               "tools": True, "capabilities": ["tools"], "efforts": ["medium"]},
              {"provider_key": "grok", "provider_name": "Grok", "model_id": "grok-4.6",
               "tools": True, "capabilities": ["tools"], "efforts": ["high"]},
              {"provider_key": "review", "model_id": "review-only", "review_only": True},
              {"provider_key": "typesafe", "model_id": "typed", "protocol": "typesafe"},
              {"provider_key": "text", "model_id": "no-tools", "tools": False}]
    original = env.store.load(env.run_id)
    roles = {name: REF.as_dict() for name in ("manager", "planner", "executor", "auditor")}
    if not configured:
        server.MODEL_REGISTRY.relocate_model_ref = lambda ref: ref
        env.session.expert_mode = False
    bootstrap = json.dumps({"conversation_id": "owner", "project": env.project,
                            "models": models, "configured": configured,
                            "run": env.store.public(env.run_id), "roles": roles}, ensure_ascii=False)
    script = """
const fixture=BOOTSTRAP;
const conversationId=fixture.conversation_id, orch={project:fixture.project};
let owner=conversationId;
const activeConversationId=()=>owner;
const currentStatus={work_dir:fixture.project};
const orchestrationProjectKey=value=>String(value||'').toLowerCase().replace(/\\\\/g,'/');
const state={configured:fixture.configured}, existing={};
const assistanceRun=fixture.run, models=fixture.models;
const seatControls=Object.fromEntries(Object.entries(fixture.roles).map(([k,v])=>[k,{get:()=>v}]));
const body=document.querySelector('main');
const esc=value=>String(value).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const notify=(message,kind)=>{document.querySelector('#notice').textContent=message;window.notices.push({message,kind});};
const renderExpertSnapshot=value=>{window.rendered=value;};
window.notices=[];
window.switchOwner=()=>{owner='other';};
""".replace("BOOTSTRAP", bootstrap) + picker + slot
    app = FastAPI()
    app.put("/api/expert-profile")(server.api_put_expert_profile)
    app.put("/api/expert-profile/capability-executor")(server.api_put_project_capability_executor)
    app.post("/api/expert-runs/{run_id}/capability-executor")(server.api_put_run_capability_executor)

    @app.get("/api/expert-runs/{run_id}")
    async def current_run(run_id: str):
        return env.store.public(run_id)

    app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parents[1] / "frontend/static"))

    @app.get("/fixture.js")
    async def fixture_js():
        return Response(script, media_type="application/javascript")

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return '''<!doctype html><meta charset="utf-8"><link rel="icon" href="data:,">
<link rel="stylesheet" href="/static/_tokens.css"><link rel="stylesheet" href="/static/style.css">
<style>body{overflow:auto;padding:40px}main{max-width:860px;margin:auto}#notice{margin-top:24px}</style>
<main><h2>专家团编排</h2><p>选择辅助执行器</p></main><p id="notice" role="status"></p>
<script src="/fixture.js"></script>'''

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    host = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    task = asyncio.create_task(host.serve(sockets=[sock]))
    requests = []
    try:
        for _ in range(200):
            if host.started:
                break
            await asyncio.sleep(.01)
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page(viewport={"width": 1200, "height": 850})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("request", lambda r: requests.append((r.method, r.url, r.post_data))
                    if r.method in {"POST", "PUT"} else None)
            await page.goto(f"http://127.0.0.1:{port}")
            panel = page.locator(".orch-capability-slot")
            await expect(panel).to_be_visible()
            await expect(panel.locator("summary")).to_contain_text("未配置")
            assert not requests and env.store.load(env.run_id) == original
            await panel.locator("summary").click()
            rows = await panel.locator(".model-picker-row").all_text_contents()
            assert any("gpt-6.1-sol" in row for row in rows)
            assert any("claude-sonnet-4.6" in row for row in rows)
            assert any("grok-4.6" in row for row in rows)
            assert not any(value in " ".join(rows) for value in ("review-only", "typed", "no-tools"))
            await panel.get_by_role("button", name="Chosen provider · chosen-model").click()
            await panel.locator(".expert-effort").select_option("medium")
            await panel.get_by_role("button", name="保存供以后使用", exact=True).click()
            await expect(page.get_by_role("status")).to_contain_text("已保存")
            assert env.profiles.get(env.project).capability_executor == REF
            assert env.store.load(env.run_id) == original
            assert len(requests) == 1 and requests[0][0] == "PUT"
            assert requests[0][1].endswith("/api/expert-profile/capability-executor" if configured else "/api/expert-profile")
            if not configured:
                assert json.loads(requests[0][2])["activate"] is False
            await panel.get_by_role("button", name="应用到当前暂停任务", exact=True).click()
            await expect(panel).to_contain_text("chosen-provider · chosen-model · medium")
            changed = env.store.load(env.run_id)
            assert protected(changed) == protected(original)
            assert changed["locked_profile"]["capability_executor"] == REF.as_dict()
            assert changed["status"] == "paused" and len(requests) == 2
            assert requests[1][1].endswith("/capability-executor")
            assert all("/confirm" not in url and "/resume" not in url for _, url, _ in requests)
            # Unavailable selection cannot fall back to the still-present Grok.
            await panel.locator("summary").click()
            await panel.get_by_role("button", name="Codex · gpt-6.1-sol").click()
            await panel.locator(".expert-effort").select_option("medium")
            await panel.get_by_role("button", name="应用到当前暂停任务", exact=True).click()
            await expect(page.get_by_role("status")).to_contain_text("未自动替换")
            assert env.store.load(env.run_id) == changed
            await page.evaluate("window.switchOwner()")
            prior = len(requests)
            await panel.get_by_role("button", name="保存供以后使用", exact=True).click()
            await panel.get_by_role("button", name="应用到当前暂停任务", exact=True).click()
            assert len(requests) == prior and not errors
            await page.screenshot(path=str(tmp_path / "capability-executor-slot.png"), full_page=True)
            await browser.close()
    finally:
        host.should_exit = True
        await asyncio.wait_for(task, 10)
