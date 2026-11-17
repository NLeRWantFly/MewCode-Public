"""Verify a paused graph and active node replies against an older host view."""
import asyncio
import copy
import json
from pathlib import Path
import socket
import sys
import threading

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from playwright.async_api import async_playwright, expect
import uvicorn

from mewcode.orchestration.task_view import finish
from mewcode.workflow_core.activity import observe_view
from mewcode.workflow_core.projection import view
from mewcode.workflow_core.runtime import MemoryBackend
from mewcode.workflow_core.state import command
from tests.workflow_core.test_display import make_paused_graph


async def main(directory):
    output = Path(directory).resolve()
    output.mkdir(parents=True, exist_ok=True)
    repo = Path(__file__).resolve().parents[2]
    backend = MemoryBackend(await make_paused_graph())
    original = backend.read()
    calls, errors = [], []
    cid, rid = 'status-browser', 'status-run'

    def projected():
        shown = finish(observe_view(view(dict(run_id=rid, conversation_id=cid, workflow_core=backend.read())),
            live=dict(scheduler_active=True, active_node_ids=['content/check'])))
        # Emulate the still-running host from before the display fix.
        old = copy.deepcopy(shown)
        for name in ('workflow_status', 'node_dialogue', 'paused_branches'):
            old['task'].pop(name)
        for group in old['plan']:
            for step in group['steps']:
                step['detail'].pop('node_dialogue')
            group['status'] = '进行中'
        if old['task']['execution_status'] == 'running':
            old['task'].update(status='进行中', controls=['pause', 'cancel'], current_activity='正在推进当前计划')
        return old

    app = FastAPI()
    app.mount('/static', StaticFiles(directory=repo / 'frontend/static'))

    @app.get('/', response_class=HTMLResponse)
    async def page():
        return '''<!doctype html><html lang="zh-CN" data-theme="light"><head><meta charset="utf-8">
<link rel="stylesheet" href="/static/workbench/workbench.css"></head><body>
<button id="workbench-toggle">工作台</button><div id="workbench-resize"></div><div id="workbench-backdrop"></div>
<aside id="task-workbench"></aside><script type="module" src="/fixture.js"></script></body></html>'''

    @app.get('/fixture.js')
    async def script():
        scope = json.dumps(dict(conversationId=cid, project=str(output)))
        return Response('window.MewWorkbenchHost={context:()=>(' + scope + ')};'
                        "await import('/static/workbench/index.js');window.MewWorkbench.open();",
                        media_type='text/javascript')

    @app.get('/api/workbench/view')
    async def read_view():
        return projected()

    @app.get('/api/workbench/tasks')
    async def tasks():
        return dict(tasks=[projected()['task']])

    @app.post('/api/workbench/run-action')
    async def action(request: Request):
        body = await request.json()
        calls.append(body)
        backend.change(lambda saved: command(saved, body))
        return dict(view=projected())

    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    host = uvicorn.Server(uvicorn.Config(app, log_level='warning'))
    thread = threading.Thread(target=host.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    while not host.started:
        await asyncio.sleep(.05)
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={'width': 1600, 'height': 1000})
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.goto(f'http://127.0.0.1:{port}/')
            await expect(page.locator('.wb-run-state')).to_have_text('已暂停')
            await expect(page.locator('.wb-run-button')).to_have_text('▶ 继续')
            await page.get_by_role('button', name='任务', exact=True).click()
            await expect(page.locator('.wb-activity')).to_contain_text('节点对话：1 条进行中')
            await expect(page.get_by_role('button', name='暂停节点对话', exact=True)).to_be_visible()
            labels = await page.locator('.wb-milestone-summary').all_text_contents()
            assert labels == ['任务计划已暂停', '循环体：pilot已完成', '循环体：content已暂停', '循环体：layout未开始'], labels
            assert backend.read() == original and not calls
            await page.screenshot(path=str(output / 'aligned.png'), full_page=True)
            await page.get_by_role('button', name='暂停节点对话', exact=True).click()
            await expect(page.get_by_role('button', name='暂停节点对话', exact=True)).to_have_count(0)
            await expect(page.locator('.wb-run-button')).to_have_text('▶ 继续')
            assert [body['operation'] for body in calls] == ['pause']
            assert backend.read()['attempts'] == original['attempts']
            assert not errors, errors
            await browser.close()
    finally:
        host.should_exit = True
        await asyncio.to_thread(thread.join, 5)
    result = dict(passed=True, calls=calls, errors=errors, attempts_preserved=True)
    (output / 'browser-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else 'outputs/workflow-status-alignment-20261111'))
