"""Maintenance uses the production chat shell, renderer, composer and dialogs."""
import asyncio
import json
import os
import socket
from pathlib import Path

import pytest
import uvicorn
from fastapi import WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from playwright.async_api import async_playwright, expect

from frontend import orch_maintenance, server
from mewcode.orchestration.maintenance import SCHEMA, command_result
from tests.test_orchestration_maintenance import maintenance_env as maintenance_env, update_proposal


ROOT = Path(__file__).resolve().parents[1]


def screenshot_path(tmp_path, name):
    folder = Path(os.environ.get("MEWCODE_UI_SCREENSHOT_DIR") or tmp_path)
    folder.mkdir(parents=True, exist_ok=True)
    return str(folder / name)


def install_chat_shell(app, owner):
    """Real app.js and HTML; only business/model transports use test data."""
    frames, connections = [], []
    status = {"status": {"conversation_id": owner["conversation_id"], "work_dir": owner["project"],
        "conversation_title": "编排维护展示", "project_name": "MewCode", "scope": "project",
        "provider_key": "selected", "model": "selected-model", "execution_kind": "orchestration"},
        "running_conversations": [], "commands": [{"name": "fix", "usage": "/fix [问题描述]"},
                                                   {"name": "update", "usage": "/update [要求]"}]}
    app.mount("/static", StaticFiles(directory=ROOT / "frontend/static"))

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return (ROOT / "frontend/static/index.html").read_text(encoding="utf-8")

    @app.get("/api/state")
    async def state():
        return status

    @app.get("/api/conversations/project/{identity}")
    async def conversation(identity: str):
        return {"id": identity, "entries": [{"role": "user", "text": "原业务要求：保留验收成果"},
                                            {"role": "assistant", "text": "原业务回复，等待核验。"}]}

    @app.websocket("/ws")
    async def websocket(ws: WebSocket):
        await ws.accept()
        connections.append(ws)
        try:
            while True:
                frame = await ws.receive_json()
                frames.append(frame)
                if frame.get("type") == "command" and frame.get("name") in ("fix", "update"):
                    await ws.send_json({"type": "command_result", "conversation_id": owner["conversation_id"],
                        **command_result(owner["conversation_id"], frame["name"], frame.get("args", ""))})
        except WebSocketDisconnect:
            pass

    @app.api_route("/api/{rest:path}", methods=["GET", "POST"])
    async def background(rest: str):
        if rest == "workspace-tree":
            return {"machines": [], "work_dir": owner["project"]}
        if rest == "orchestration/current":
            return {"spec": server._conversation_plan_store().get(owner["project"]).spec.as_dict()}
        if rest == "models/catalog":
            return {"models": []}
        return {}

    return frames, connections, status


async def start_host(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    host = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    task = asyncio.create_task(host.serve(sockets=[sock]))
    for _ in range(200):
        if host.started:
            break
        await asyncio.sleep(.01)
    return host, task, f"http://127.0.0.1:{sock.getsockname()[1]}"


async def visible_business_messages(page):
    source = (ROOT / "frontend/static/app.js").read_text(encoding="utf-8")
    helper = source[source.index("  function collectVisibleChatMessages() {"):source.index("  let activeOrchestrationProgress")]
    return await page.evaluate("() => {const messagesEl=document.getElementById('messages');" + helper +
                               "return collectVisibleChatMessages();}")


@pytest.mark.asyncio
async def test_fix_dialogue_shows_explanation_and_inline_question(maintenance_env, monkeypatch, tmp_path):
    from tests.test_maintenance_agent import interactive_builder
    app, owner, _, _, _ = maintenance_env
    made = interactive_builder(monkeypatch, Path(owner["project"]))
    from mewcode.tools.base import TextDelta
    original_builder = orch_maintenance.build_fix_agent
    def conversational_builder(**kwargs):
        agent, conversation = original_builder(**kwargs)
        agent.client._responses[0].insert(0, TextDelta(text="我已看过当前编排，先确认修复范围，再保留原验收进行修改。"))
        return agent, conversation
    monkeypatch.setattr(orch_maintenance, "build_fix_agent", conversational_builder)

    @app.get("/api/agent_tasks")
    async def agent_tasks():
        return server.SESSION.agent_tasks_payload()

    frames, _, _ = install_chat_shell(app, owner)
    host, task, origin = await start_host(app)
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page(viewport={"width": 1400, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(origin)
            await page.get_by_text("原业务回复，等待核验。", exact=True).wait_for()
            await page.locator("#chat-input").fill("/fix 先确认修复方式")
            await page.locator("#chat-input").press("Enter")
            pane = page.locator(".maintenance-thread")
            await expect(pane.get_by_text("需要你补充一下", exact=True)).to_be_visible()
            await expect(pane.get_by_text("我已看过当前编排，先确认修复范围，再保留原验收进行修改。", exact=True)).to_be_visible()
            assert len(made) == 1 and not (Path(owner["project"]) / "repair.txt").exists()
            await expect(pane.get_by_role("button", name="查看子 Agent 进度", exact=True)).to_have_count(0)
            assert not await pane.locator(".maintenance-activity").evaluate("node=>node.open")
            await page.screenshot(path=screenshot_path(tmp_path, "fix-conversation.png"))
            await pane.locator(".maintenance-activity-summary").click()
            await expect(pane.locator(".maintenance-agent-tools")).to_be_visible()
            await pane.locator(".maintenance-activity-summary").click()
            assert len(made) == 1
            await pane.get_by_role("radio", name="写入修复说明", exact=True).click()
            await pane.get_by_role("textbox", name="选择修复方式", exact=True).fill("写入说明，保留原验收")
            await page.screenshot(path=str(tmp_path / "fix-question.png"))
            await pane.get_by_role("button", name="提交回答并继续", exact=True).click()
            await expect(pane.get_by_text("修复说明已保存", exact=True)).to_be_visible()
            await expect(pane.get_by_text("修复说明已保存", exact=True)).to_have_count(1)
            await expect(pane.locator(".maintenance-questions")).to_be_empty()
            assert len(made) == 1 and (Path(owner["project"]) / "repair.txt").exists()
            assert "保留原验收" in await pane.inner_text()
            transcript = await pane.locator(".maintenance-messages").inner_text()
            assert transcript.index("我已看过当前编排") < transcript.index("写入说明，保留原验收") < transcript.index("修复说明已保存")
            assert not [frame for frame in frames if frame["type"] in ("chat", "ask_user_response", "interrupt")]
            assert [m["content"] for m in await visible_business_messages(page)] == ["原业务要求：保留验收成果", "原业务回复，等待核验。"]
            assert not errors, errors
            await browser.close()
    finally:
        host.should_exit = True
        await asyncio.wait_for(task, 10)


@pytest.mark.asyncio
async def test_chat_stop_reopen_preview_apply_and_scope_switch(maintenance_env, monkeypatch, tmp_path):
    from mewcode.agent import Agent
    from mewcode.client import LLMClient
    from mewcode.conversation import ConversationManager
    from mewcode.expert_runs import deserialize_messages
    from mewcode.tools import ToolRegistry
    from mewcode.tools.base import StreamEnd, TextDelta

    app, owner, current, durable, original = maintenance_env
    entered = asyncio.Event()
    calls = []
    partial = '{"schema":"' + SCHEMA + '","reply":"我正在核对当前节点的输入和验收要求。'
    final = json.dumps({"schema": SCHEMA, "reply": "**已定位**当前环节的核验要求，保留原验收。",
                        "proposal": update_proposal(goal="新增逐项核验")}, ensure_ascii=False)

    class TextEventClient(LLMClient):
        async def stream(self, conversation, system="", tools=None):
            calls.append([message.content for message in conversation.history if message.role == "user"])
            if len(calls) == 1:
                yield TextDelta(text=partial)
                entered.set()
                await asyncio.Event().wait()
            yield TextDelta(text=final)
            yield StreamEnd(stop_reason="end_turn", input_tokens=1, output_tokens=1)

    def build_update_agent(*, client, state, **_kwargs):
        conversation = ConversationManager()
        if state.get("agent_history"):
            conversation.restore_history(deserialize_messages(state["agent_history"]))
            conversation.repair_incomplete_tool_turns()
        conversation.add_user_message(state["messages"][-1]["text"])
        agent = Agent(TextEventClient(), ToolRegistry(), "anthropic", work_dir=owner["project"], stall_policy=False)
        agent.session_id = state["conversation_id"]
        return agent, conversation

    monkeypatch.setattr(orch_maintenance, "build_maintenance_agent", build_update_agent)
    frames, connections, shell_status = install_chat_shell(app, owner)
    host, task, origin = await start_host(app)
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page(viewport={"width": 1400, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(origin)
            await page.get_by_text("原业务回复，等待核验。", exact=True).wait_for()
            input_box = page.locator("#chat-input")
            await input_box.fill("/update work")
            await input_box.press("Enter")
            pane = page.locator("#messages .maintenance-thread")
            send = page.get_by_role("button", name="发送维护消息", exact=True)
            await expect(send).to_be_enabled()
            assert await page.locator("dialog.orch-maintenance").count() == 0
            assert await page.locator("textarea:visible").count() == 1
            await input_box.fill("修改当前环节，保留原稿")
            await input_box.press("Enter")
            await asyncio.wait_for(entered.wait(), 5)
            await expect(pane.get_by_text("我正在核对当前节点的输入和验收要求。", exact=True)).to_be_visible()
            assert SCHEMA not in await pane.inner_text()
            await page.screenshot(path=screenshot_path(tmp_path, "update-streaming-conversation.png"))
            await page.get_by_role("button", name="停止本次维护回复", exact=True).click()
            await expect(send).to_be_enabled()
            await expect(pane.get_by_text("我正在核对当前节点的输入和验收要求。", exact=True)).to_be_visible()
            assert server._conversation_plan_store().get(owner["project"]).spec.as_dict() == original.as_dict()
            await page.get_by_role("button", name="返回普通对话", exact=True).click()
            await pane.get_by_role("button", name="继续讨论", exact=True).click()
            await expect(send).to_be_enabled()
            await pane.get_by_text("修改当前环节，保留原稿", exact=True).wait_for()
            await input_box.fill("新增逐项核验")
            await send.click()
            apply = pane.get_by_role("button", name="应用修订", exact=True)
            await expect(apply).to_be_visible()
            assert len(calls) == 2
            assert calls[0][-1] == "修改当前环节，保留原稿"
            assert calls[1][-1] == "新增逐项核验"
            await expect(pane.locator(".msg.assistant .md strong")).to_have_text("已定位")
            assert await pane.locator(".msg-copy-btn").count() == 2
            assert await pane.locator(".msg-draft-btn").count() == 0
            await pane.get_by_role("button", name="查看修改", exact=True).click()
            popup = page.locator("#settings-overlay")
            await expect(popup).to_be_visible()
            assert await popup.locator(".maintenance-compare").inner_text() == "当前\n{\n  \"goal\": \"保留验收和成果\"\n}\n修订后\n{\n  \"goal\": \"新增逐项核验\"\n}"
            current["busy"] = True
            await expect(popup.get_by_role("button", name="应用修订", exact=True)).to_be_disabled(timeout=6000)
            current["busy"] = False
            await expect(popup.get_by_role("button", name="应用修订", exact=True)).to_be_enabled(timeout=6000)
            await page.locator("#settings-dialog-close").click()
            assert [m["content"] for m in await visible_business_messages(page)] == ["原业务要求：保留验收成果", "原业务回复，等待核验。"]
            await page.screenshot(path=str(tmp_path / "maintenance-chat-preview.png"))
            await apply.click()
            await expect(pane.get_by_role("button", name="已应用", exact=True)).to_be_visible()
            saved = server._conversation_plan_store().get(owner["project"]).spec
            assert saved.node_map()["work"].goal == "新增逐项核验"
            assert saved.node_map()["work"].model == original.node_map()["work"].model
            assert not [frame for frame in frames if frame["type"] in ("chat", "ask_user_response", "interrupt")]
            await page.get_by_role("button", name="返回普通对话", exact=True).click()
            await input_box.fill("普通聊天未发送草稿")
            await page.evaluate("options=>OrchestrationMaintenance.open(options)", {**owner, "mode": "update", "request": "work"})
            await expect(send).to_be_enabled()
            await input_box.fill("维护未发送草稿")
            await page.get_by_role("button", name="返回普通对话", exact=True).click()
            await expect(input_box).to_have_value("普通聊天未发送草稿")
            await pane.get_by_role("button", name="继续讨论", exact=True).click()
            await expect(input_box).to_have_value("维护未发送草稿")
            shell_status["status"]["conversation_id"] = "other"
            await connections[0].send_json({"type": "state", **shell_status})
            await expect(page.locator(".maintenance-mode")).to_have_count(0)
            assert len(calls) == 2
            assert not errors, errors
            await expect(input_box).to_have_value("")
            await input_box.fill("回到普通对话后发送的消息")
            await input_box.press("Enter")
            for _ in range(100):
                if any(frame["type"] == "chat" for frame in frames):
                    break
                await asyncio.sleep(.01)
            business = [frame for frame in frames if frame["type"] == "chat"]
            assert len(business) == 1 and business[0]["text"] == "回到普通对话后发送的消息"
            assert business[0]["conversation_id"] == "other"
            await browser.close()
    finally:
        host.should_exit = True
        await asyncio.wait_for(task, 10)


@pytest.mark.asyncio
async def test_fix_chat_uses_description_and_existing_option_dialogs(maintenance_env, monkeypatch, tmp_path):
    from tests.test_fix_description import DESCRIPTION, cross_node_repair
    app, owner, _, durable, original = maintenance_env
    calls = []

    async def model(client, **kwargs):
        prompt = json.loads(kwargs["user_text"])
        calls.append(prompt)
        assert prompt["focus_node_ids"] == ["work"]
        assert prompt["allowed_node_ids"] == ["first", "work", "done"]
        assert prompt["history"][-1]["text"] == DESCRIPTION
        return json.dumps({"schema": SCHEMA, "reply": "根据 work 的错误文本定位到上游执行说明，并修复汇总步骤。",
                           "proposal": cross_node_repair()})

    monkeypatch.setattr(orch_maintenance, "collect_model_text", model)
    frames, _, _ = install_chat_shell(app, owner)
    host, task, origin = await start_host(app)
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page(viewport={"width": 1400, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(origin)
            await page.get_by_text("原业务回复，等待核验。", exact=True).wait_for()
            await page.locator("#chat-input").fill("/fix " + DESCRIPTION)
            await page.locator("#chat-input").press("Enter")
            pane = page.locator("#messages .maintenance-thread")
            await pane.get_by_text("待应用修复", exact=True).wait_for()
            assert "节点仅作线索" in await pane.locator(".maintenance-thread-heading").inner_text()
            assert await page.locator("dialog[open]").count() == 0
            await page.get_by_role("button", name="范围与诊断", exact=True).click()
            popup = page.locator("#settings-overlay")
            await expect(popup).to_be_visible()
            await expect(popup.get_by_role("button", name="work · work", exact=True)).to_have_attribute("aria-pressed", "true")
            await page.locator("#settings-dialog-close").click()
            await pane.get_by_role("button", name="查看修改", exact=True).click()
            assert await popup.locator(".maintenance-change").count() == 2
            assert "first" in await popup.inner_text() and "done" in await popup.inner_text()
            assert '"steps"' in await popup.locator(".maintenance-compare").last.inner_text()
            assert '"outputs"' not in await popup.locator(".maintenance-compare").last.inner_text()
            for theme, width in (("light", 1400), ("dark", 1400), ("light", 650)):
                await page.set_viewport_size({"width": width, "height": 1000})
                await page.evaluate("theme=>document.documentElement.dataset.theme=theme", theme)
                geometry = await popup.locator(".settings-dialog").evaluate("e=>({scroll:e.scrollWidth,client:e.clientWidth})")
                assert geometry["scroll"] <= geometry["client"] + 2
                await page.screenshot(path=str(tmp_path / f"fix-chat-diff-{theme}-{width}.png"))
            await page.locator("#settings-dialog-close").click()
            await page.set_viewport_size({"width": 1400, "height": 1000})
            for theme in ("light", "dark"):
                await page.evaluate("theme=>document.documentElement.dataset.theme=theme", theme)
                await page.locator("#messages").evaluate("e=>e.scrollTop=e.scrollHeight")
                await page.screenshot(path=str(tmp_path / f"fix-chat-{theme}.png"))
                assert await page.locator(".maintenance-mode").evaluate("e=>e.scrollWidth<=e.clientWidth+2")
                contrast = await pane.locator(".msg.assistant .msg-bubble").first.evaluate("""e=>{
                    const style=getComputedStyle(e),lum=color=>{const v=color.match(/[0-9.]+/g).slice(0,3).map(n=>{
                        n=Number(n)/255;return n<=.04045?n/12.92:((n+.055)/1.055)**2.4;});return v[0]*.2126+v[1]*.7152+v[2]*.0722;};
                    const a=lum(style.color),b=lum(style.backgroundColor);return (Math.max(a,b)+.05)/(Math.min(a,b)+.05);}""")
                assert contrast >= 4.5
            assert len(calls) == 1
            await pane.get_by_role("button", name="应用修订", exact=True).click()
            await expect(pane.get_by_role("button", name="已应用", exact=True)).to_be_visible()
            saved = server._conversation_plan_store().get(owner["project"]).spec
            assert saved.node_map()["first"].goal != original.node_map()["first"].goal
            assert saved.node_map()["done"].plan_brief.steps != original.node_map()["done"].plan_brief.steps
            assert saved.node_map()["work"] == original.node_map()["work"]
            assert not [frame for frame in frames if frame["type"] == "chat"]
            assert not errors, errors
            await browser.close()
    finally:
        host.should_exit = True
        await asyncio.wait_for(task, 10)
