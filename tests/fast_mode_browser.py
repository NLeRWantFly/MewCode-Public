"""Exercise the model speed setting with a local provider and no paid calls."""
import asyncio
import json
from pathlib import Path
import socket
import sys
import threading
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from playwright.async_api import async_playwright, expect
import uvicorn

from frontend.fast_mode import FastModeSettings, install
from tests.test_fast_mode import MODEL, ref, registry


async def main(directory):
    output = Path(directory).resolve()
    output.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[1]
    providers, slot = registry()
    settings = FastModeSettings(output / 'speed.json', providers)
    live = providers.resolve(ref()).client
    def status():
        return dict(conversation_id='speed-browser', provider_key=slot.key, model=MODEL,
                    effort=live.effort, **settings.status(slot, live))
    server = SimpleNamespace(FAST_MODE_SETTINGS=settings, SESSION=SimpleNamespace(slot=slot, status_payload=status),
                             _active_conversation_id=lambda: 'speed-browser')
    app = FastAPI()
    app.mount('/static', StaticFiles(directory=root / 'frontend/static'))
    install(app, server)

    @app.get('/', response_class=HTMLResponse)
    async def page():
        return '''<!doctype html><html lang="zh-CN" data-theme="light"><head><meta charset="utf-8">
<link rel="stylesheet" href="/static/_tokens.css"><link rel="stylesheet" href="/static/_primitives.css">
<link rel="stylesheet" href="/static/style.css"></head><body style="display:block;padding:32px;max-width:900px">
<main class="runtime-settings-panel"><h2>模型与推理</h2><p id="model">gpt-6.1-sol · max</p>
<div class="runtime-settings-meta"><div id="speed"></div></div></main>
<script src="/static/fast-mode.js"></script><script>
let state=''' + json.dumps(status()) + ''';
const control=window.MewFastMode.mount(document.getElementById('speed'),{getStatus:()=>state,
onChange:value=>{state={...state,...value};document.getElementById('model').textContent=state.model+' · '+state.effort+(state.fast_mode?' · Fast':'');}});
</script></body></html>'''

    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    host = uvicorn.Server(uvicorn.Config(app, log_level='warning'))
    thread = threading.Thread(target=host.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    while not host.started:
        await asyncio.sleep(.05)
    errors = []
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={'width': 1100, 'height': 500})
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.goto(f'http://127.0.0.1:{port}/')
            toggle = page.get_by_role('switch', name='Fast 模式')
            await expect(toggle).to_have_attribute('aria-checked', 'false')
            await toggle.click()
            await expect(toggle).to_have_attribute('aria-checked', 'true')
            await expect(page.locator('#model')).to_have_text('gpt-6.1-sol · max · Fast')
            assert live.service_tier == 'fast' and live.effort == 'max'
            await page.screenshot(path=str(output / 'fast-enabled.png'), full_page=True)
            await page.reload()
            await expect(toggle).to_have_attribute('aria-checked', 'true')
            await toggle.click()
            await expect(toggle).to_have_attribute('aria-checked', 'false')
            assert live.service_tier == 'default' and live.effort == 'max'
            assert FastModeSettings(settings.path, providers).state(slot.key, MODEL)['enabled'] is False
            assert not errors, errors
            await browser.close()
    finally:
        host.should_exit = True
        await asyncio.to_thread(thread.join, 5)
    result = dict(passed=True, reasoning_effort='max', model=MODEL, errors=errors)
    (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else 'outputs/fast-mode-20261112'))
