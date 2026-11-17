"""Opt-in integration: isolated browser, local HTML only, no model requests."""
import asyncio
import os
import re
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.mark.skipif(os.environ.get("MEWCODE_LIVE_BROWSER") != "1", reason="requires installed browser and Playwright MCP")
@pytest.mark.asyncio
async def test_real_browser_navigate_type_click_snapshot(tmp_path):
    from mewcode.mcp.manager import MCPManager
    from mewcode.mcp.playwright_browser import build_playwright_config
    from mewcode.tools import ToolRegistry
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'<html><body><input aria-label="Acceptance name"><button onclick="document.getElementById(\'result\').textContent=\'Acceptance PASS\'">Verify</button><p id="result">Waiting</p></body></html>'
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    manager, registry = MCPManager(), ToolRegistry()
    browser_env = {key: value for key, value in os.environ.items() if key.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}}
    config = build_playwright_config("ephemeral", env=browser_env)
    config.args.extend(["--output-dir", str(tmp_path)])
    manager.load_configs([config])
    async def call(name, **args):
        tool = registry.get(name)
        assert tool is not None, name
        result = await tool.execute(tool.params_model(**args))
        assert not result.is_error, result.output
        output = result.output
        for reference in re.findall(r'\[Snapshot\]\(([^)]+\.yml)\)', output):
            path = Path(reference).resolve()
            assert path.is_relative_to(tmp_path.resolve()), path
            output += "\n" + path.read_text(encoding="utf-8")
        return output
    try:
        errors = await asyncio.wait_for(manager.register_all_tools(registry), 60)
        assert not errors, errors
        snapshot = await call("browser_navigate", url=f"http://127.0.0.1:{server.server_port}")
        field = re.search(r'textbox "Acceptance name".*?\[ref=([^\]]+)\]', snapshot)
        assert field, snapshot
        await call("browser_type", element="Acceptance name", target=field.group(1), text="local smoke test")
        snapshot = await call("browser_snapshot")
        button = re.search(r'button "Verify".*?\[ref=([^\]]+)\]', snapshot)
        assert button, snapshot
        await call("browser_click", element="Verify", target=button.group(1))
        assert "Acceptance PASS" in await call("browser_snapshot")
        await call("browser_take_screenshot", type="png", scale="css", filename=str(tmp_path / "acceptance.png"))
        assert (tmp_path / "acceptance.png").stat().st_size > 0
    finally:
        await manager.shutdown()
        server.shutdown()
        server.server_close()
