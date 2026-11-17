"""Exercise the sandbox network settings in a local Microsoft Edge browser."""
import asyncio
from pathlib import Path
import socket
from unittest.mock import Mock

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from playwright.async_api import async_playwright

from frontend import sandbox_network
from mewcode.permissions.network_policy import NetworkPolicyStore


@pytest.mark.asyncio
async def test_network_settings_browser_states_and_mobile_layout(tmp_path, monkeypatch):
    store = NetworkPolicyStore(tmp_path / "sandbox-network.json")
    monkeypatch.setattr(sandbox_network, "NetworkPolicyStore", lambda: store)
    monkeypatch.setattr(sandbox_network, "runtime_status", lambda: {"ready": False, "state": "pending"})

    session = type("Session", (), {"session_id": "conversation-1"})()
    holder = type("Holder", (), {"session": session})()
    configure_calls = []

    async def mock_run_control(operation):
        # The browser test exercises the explicit configure action without ever
        # invoking the installation or verification function it receives.
        configure_calls.append(operation)
        return {"ready": False, "state": "pending", "mocked": True}

    app = FastAPI()
    sandbox_network.register_network_routes(app, lambda: holder, mock_run_control)
    static = Path(__file__).resolve().parents[1] / "frontend" / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return """<!doctype html><html lang="zh-CN" data-theme="light"><head>
        <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
        <link rel="icon" href="data:,"><link rel="stylesheet" href="/static/_tokens.css">
        <link rel="stylesheet" href="/static/style.css"></head><body>
        <main><button id="native-execution-btn" type="button">沙箱设置</button></main>
        <div id="settings-overlay" class="settings-overlay hidden" aria-hidden="true">
          <section class="settings-dialog" role="dialog" aria-modal="true" aria-labelledby="settings-title">
            <header class="settings-dialog-head"><div><h2 id="settings-title">沙箱设置与执行恢复</h2></div></header>
            <div id="settings-body" class="settings-dialog-body"></div>
            <footer class="settings-dialog-footer"></footer>
          </section>
        </div>
        <script src="/static/sandbox-network.js"></script><script>
        document.querySelector('#native-execution-btn').addEventListener('click', () => {
          document.querySelector('#settings-overlay').classList.remove('hidden');
          document.querySelector('#settings-overlay').setAttribute('aria-hidden', 'false');
          if (!document.querySelector('.sandbox-network-section')) {
            MewSandboxNetwork.mount(document.querySelector('#settings-body'));
          }
        });
        </script></body></html>"""

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    page_errors = []
    page_requests = []
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="msedge", headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1200, "height": 950})
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                page.on("request", lambda request: page_requests.append(request.url))
                await page.goto(f"http://127.0.0.1:{port}")
                await page.get_by_role("button", name="沙箱设置").click()
                enabled = page.get_by_role("checkbox", name="允许网络访问")
                status = page.get_by_role("status")
                save = page.get_by_role("button", name="保存网络设置")
                configure = page.get_by_role("button", name="配置联网运行器")
                await enabled.wait_for()
                assert await enabled.is_checked()
                await page.get_by_label("网络黑名单（每行一个域名或 IP）").fill(
                    "EXAMPLE.COM.\n*.example.org\n"
                )

                # Save the disabled setting and deny list using the real local API.
                await enabled.uncheck()
                await save.click()
                await page.wait_for_function(
                    "document.querySelector('[role=status]')?.textContent.includes('任务联网已关闭')"
                )
                assert store.read().enabled is False
                assert store.read().denied_domains == ("*.example.org", "example.com")

                # Re-enable networking and confirm the UI separates the policy
                # from the command runner's not-ready runtime state.
                await enabled.check()
                await save.click()
                await page.wait_for_function(
                    "document.querySelector('[role=status]')?.textContent.includes('命令联网运行器尚未就绪')"
                )
                assert store.read().enabled is True
                assert await configure.is_visible()

                from mewcode.sandbox import srt_setup
                configure_mock = Mock(return_value={"ready": False, "mocked": True})
                monkeypatch.setattr(srt_setup, "configure_and_verify", configure_mock)
                await configure.click()
                await page.wait_for_function(
                    "document.querySelector('[role=status]')?.textContent.includes('命令联网运行器尚未就绪')"
                )
                assert len(configure_calls) == 1
                assert configure_calls[0] is configure_mock
                configure_mock.assert_not_called()
                assert await configure.is_visible()

                output = Path(__file__).resolve().parents[1] / ".tmp"
                output.mkdir(exist_ok=True)
                await page.screenshot(path=str(output / "network-ui-desktop.png"), full_page=True)

                await page.set_viewport_size({"width": 390, "height": 844})
                geometry = await page.locator(".sandbox-network-section").evaluate(
                    "e => ({right: e.getBoundingClientRect().right, width: e.getBoundingClientRect().width, "
                    "scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth})"
                )
                assert geometry["right"] <= 390 and geometry["scroll"] <= geometry["client"] + 2, geometry
                await page.screenshot(path=str(output / "network-ui-390.png"), full_page=True)

                # Simulate another window saving first. The stale UI revision
                # must be rejected and surfaced without replacing local edits.
                current = store.read()
                store.save(
                    {"enabled": False, "denied_domains": list(current.denied_domains)},
                    expected_revision=current.revision,
                )
                await enabled.check()
                await page.get_by_label("网络黑名单（每行一个域名或 IP）").fill("stale.example")
                await save.click()
                await page.wait_for_function(
                    "document.querySelector('[role=status]')?.textContent.includes('网络设置已变化')"
                )
                assert await enabled.is_checked()
                assert await page.get_by_label("网络黑名单（每行一个域名或 IP）").input_value() == "stale.example"
                assert not store.read().enabled
                assert store.read().denied_domains == ("*.example.org", "example.com")
                assert not page_errors, page_errors
                assert all(
                    url.startswith(f"http://127.0.0.1:{port}/") or url.startswith("data:")
                    for url in page_requests
                ), page_requests
            finally:
                await browser.close()
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 5)
        listener.close()
