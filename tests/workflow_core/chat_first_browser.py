"""Ordinary chat remains usable with an empty or unavailable workflow view."""
import asyncio
import json
from pathlib import Path
import socket
import sys
import threading

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from playwright.async_api import async_playwright, expect
import uvicorn


async def main(directory):
    output = Path(directory).resolve()
    output.mkdir(parents=True, exist_ok=True)
    repo = Path(__file__).resolve().parents[2]
    app, calls, errors = FastAPI(), [], []
    app.mount('/static', StaticFiles(directory=repo / 'frontend/static'))

    @app.get('/', response_class=HTMLResponse)
    async def page():
        return '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<link rel="stylesheet" href="/static/workbench/workbench.css"></head><body>
<button id="workbench-toggle">工作台</button><div id="workbench-resize"></div><div id="workbench-backdrop"></div>
<section id="conversation-pane"><textarea id="chat-input"></textarea></section><aside id="task-workbench"></aside>
<script type="module" src="/fixture.js"></script></body></html>'''

    @app.get('/fixture.js')
    async def script():
        return Response("window.MewWorkbenchHost={context:()=>({conversationId:'chat',project:'test'})};"
                        "await import('/static/workbench/index.js');window.MewWorkbench.open();",
                        media_type='text/javascript')

    @app.api_route('/api/{path:path}', methods=['GET', 'POST'])
    async def api(path: str):
        calls.append(path)
        if path == 'workbench/view':
            return None
        if path == 'workbench/tasks':
            return dict(tasks=[])
        return JSONResponse({'error': 'unexpected design request'}, status_code=503)

    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    host = uvicorn.Server(uvicorn.Config(app, log_level='warning'))
    thread = threading.Thread(target=host.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    for _ in range(100):
        if host.started:
            break
        await asyncio.sleep(.05)
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={'width': 1440, 'height': 980})
            page.on('pageerror', lambda exc: errors.append(str(exc)))
            page.on('pageerror', lambda exc: print(str(exc), flush=True))
            page.on('console', lambda msg: print(msg.text, flush=True) if msg.type == 'error' else None)
            page.on('requestfailed', lambda request: print(request.url, request.failure, flush=True))
            await page.goto(f'http://127.0.0.1:{sock.getsockname()[1]}/')
            control = page.locator('.wb-run-button')
            await expect(control).to_have_text('继续对话', timeout=30000)
            await expect(control).to_be_enabled()
            await expect(page.locator('.wb-compose-button')).to_be_hidden()
            await control.click()
            await expect(page.locator('#chat-input')).to_be_focused()
            assert not any('task-design' in path or 'revision' in path for path in calls), calls
            # Failed graph reads must not disable the ordinary conversation entry.
            await page.route('**/api/workbench/view*', lambda route: route.fulfill(status=503, body='{}'))
            await page.evaluate('window.MewWorkbench.store.refresh()')
            await expect(page.locator('.wb-view-message')).to_contain_text('读取')
            await expect(control).to_be_enabled()
            await control.click()
            await expect(page.locator('#chat-input')).to_be_focused()
            assert not errors, errors
            await page.screenshot(path=output / 'chat-first.png')
            await browser.close()
    finally:
        host.should_exit = True
        thread.join(timeout=5)
    print(json.dumps(dict(errors=errors, design_requests=0, chat_available=True)))


if __name__ == '__main__':
    asyncio.run(main(sys.argv[1]))
