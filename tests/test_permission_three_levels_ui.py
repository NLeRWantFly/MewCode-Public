"""Render the production permission menu and persist selections in a real session."""
import asyncio
import json
import socket
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from playwright.async_api import async_playwright, expect

from frontend import server
from tests.test_web_slash_commands import _session


@pytest.mark.asyncio
async def test_permission_menu_has_three_levels_and_plan(monkeypatch, tmp_path):
    session = _session(monkeypatch, tmp_path)
    source = (Path(__file__).resolve().parents[1] / "frontend/static/app.js").read_text(encoding="utf-8")
    helpers = source[source.index("  function showPopover("):source.index("  function projectMenuItem(")]
    menu = source[source.index('  $("perm-mode-btn").addEventListener'):source.index('  // 多 Agent 模式触发器：')]
    label = source[source.index("    const modeChoice = permissionModes.find("):source.index("    updateModelIntelligenceLabels();", source.index("    const modeChoice = permissionModes.find("))]
    script = """
const $=id=>document.getElementById(id), popover=$('popover');
const esc=v=>String(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let runtimeSettingsView=null, catalogRefreshTimer=null, projectContextReturnFocus=null;
let currentStatus={}, permissionModes=[];
const positionPopover=()=>{popover.style.left='40px';popover.style.bottom='100px';};
const openProjectPermissionGrants=()=>{};
async function refresh(){const data=await (await fetch('/state')).json();currentStatus=data.status;permissionModes=data.choices;LABEL}
async function send(message){await fetch('/mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(message)});await refresh();}
HELPERS
MENU
refresh();
""".replace("HELPERS", helpers).replace("MENU", menu).replace("LABEL", label)
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parents[1] / "frontend/static")), name="static")

    @app.get("/state")
    async def state():
        return {"status": session.status_payload(), "choices": server.PERMISSION_MODE_CHOICES}

    @app.post("/mode")
    async def mode(request: Request):
        error = session.set_permission_mode((await request.json())["mode"])
        return {"error": error}

    @app.get("/fixture.js")
    async def fixture_js():
        return Response(script, media_type="application/javascript")

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return '''<!doctype html><meta charset="utf-8"><link rel="icon" href="data:,"><link rel="stylesheet" href="/static/_tokens.css"><link rel="stylesheet" href="/static/style.css">
<style>body{padding:40px;overflow:auto}#popover{min-width:340px;max-width:430px}#perm-mode-btn{position:fixed;left:40px;bottom:40px}</style>
<h2>MewCode 权限级别</h2><p>计划为独立工作状态。</p><button id="perm-mode-btn" class="mf-click"><span id="perm-mode-label"></span></button><div id="popover" class="hidden"></div><script src="/fixture.js"></script>'''

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    host = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    task = asyncio.create_task(host.serve(sockets=[sock]))
    try:
        for _ in range(200):
            if host.started:
                break
            await asyncio.sleep(.01)
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page(viewport={"width": 960, "height": 680})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            await page.goto(f"http://127.0.0.1:{port}")
            for choice in server.PERMISSION_MODE_CHOICES:
                await page.locator("#perm-mode-btn").click()
                rows = page.locator("#popover .pop-item")
                assert await rows.count() == 5  # Three execution levels, Plan and grants.
                assert [await rows.nth(i).evaluate("el=>el.childNodes[0].textContent") for i in range(4)] == [item["label"] for item in server.PERMISSION_MODE_CHOICES]
                await rows.filter(has_text=choice["label"]).click()
                await expect(page.locator("#perm-mode-label")).to_have_text(choice["label"])
                assert session.permission_mode.value == choice["value"]
                saved = json.loads(server.STATE_FILE.read_text(encoding="utf-8"))
                assert saved["permission_mode"] == choice["value"]
                await page.reload()
                await expect(page.locator("#perm-mode-label")).to_have_text(choice["label"])
            session.set_permission_mode("default")
            session.execute_web_command("plan")
            await page.reload()
            await expect(page.locator("#perm-mode-label")).to_have_text("计划（Plan）")
            await page.locator("#perm-mode-btn").click()
            await expect(page.locator("#popover .current")).to_contain_text("计划（Plan）")
            await page.screenshot(path=str(tmp_path / "permission-three-levels.png"), full_page=True)
            assert not errors
            await browser.close()
    finally:
        host.should_exit = True
        await asyncio.wait_for(task, 10)
        session._close_session()
