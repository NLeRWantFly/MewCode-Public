"""Exercise the real workbench's manual input entry with an offline Core."""
import asyncio
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
from mewcode.workflow_core.projection import view, node_detail
from mewcode.workflow_core.runtime import Core, MemoryBackend
from mewcode.workflow_core.state import fresh, start_attempt, finish_attempt, command, edit, apply_pending
from tests.workflow_core.test_runtime import graph


async def main(directory, stale_wait=False):
    output = Path(directory).resolve()
    output.mkdir(parents=True, exist_ok=True)
    repo = Path(__file__).resolve().parents[2]
    cid, rid = 'manual-wait-browser', 'manual-wait-run'
    state = fresh(graph([dict(id='sample', label='样章制作'),
        dict(id='confirm', label='样章人工确认', kind='judge', task='请查看样章并选择通过或不通过', config={'manual': True}),
        dict(id='batch', label='批量准备')], [('sample', 'confirm', 'always'), ('confirm', 'batch', 'pass')]))
    sample = start_attempt(state, 'sample', {}, [])
    finish_attempt(state, sample['id'], dict(status='completed', text='样章已交付'))
    check = start_attempt(state, 'confirm', {}, [])
    finish_attempt(state, check['id'], dict(status='needs_input', reason='请查看样章并选择通过或不通过'))
    state['status'] = 'blocked'
    backend, calls, errors, executed = MemoryBackend(state), [], [], []
    original = backend.read()

    def record():
        return dict(run_id=rid, conversation_id=cid, workflow_core=backend.read())

    def projected():
        return finish(observe_view(view(record()), live=dict(scheduler_active=False)))

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

    @app.get('/api/workbench/node')
    async def detail(node_id: str):
        return node_detail(record(), node_id)

    @app.post('/api/workbench/run-action')
    async def action(request: Request):
        body = await request.json()
        calls.append(body)
        backend.change(lambda saved: command(saved, body))
        async def execute(ctx):
            executed.append(ctx.node_id)
            return dict(text='批量准备已完成')
        await Core(backend, execute).run()
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
            page = await browser.new_page(viewport={'width': 1440, 'height': 980})
            page.on('pageerror', lambda exc: errors.append(str(exc)))
            await page.goto(f'http://127.0.0.1:{port}/')
            await expect(page.locator('.wb-run-state')).to_have_text('等你处理')
            await expect(page.locator('.wb-run-button')).to_have_text('处理等待')
            if stale_wait:
                def saved_change(saved):
                    edit(saved, {'action': 'upsert', 'kind': 'node', 'id': 'confirm',
                        'fields': {'label': '自动采用样章', 'config': {'manual': False, 'expression': {'value': True}}}},
                        expected_revision=saved['graph']['revision'])
                    command(saved, dict(operation='apply', request_id='saved-auto-confirm',
                                        expected_revision=saved['draft']['revision']))
                    apply_pending(saved)
                    command(saved, dict(operation='pause', request_id='hold-saved-change'))
                backend.change(saved_change)
            await page.locator('.wb-run-button').click()
            if not stale_wait:
                await expect(page.get_by_role('button', name='通过', exact=True)).to_be_visible()
                await expect(page.locator('.wb-task-panel')).to_contain_text('请查看样章并选择通过或不通过')
                assert not calls and backend.read() == original
                await page.screenshot(path=str(output / 'waiting.png'), full_page=True)
                await page.get_by_role('button', name='通过', exact=True).click()
            await expect(page.locator('.wb-run-state')).to_have_text('已完成')
            assert [body['operation'] for body in calls] == ['resume' if stale_wait else 'judge_answer']
            assert executed == ['batch']
            assert all(backend.read()['attempts'][key] == value for key, value in original['attempts'].items())
            assert not errors, errors
            await page.screenshot(path=str(output / 'continued.png'), full_page=True)
            await browser.close()
    finally:
        host.should_exit = True
        await asyncio.to_thread(thread.join, 5)
    result = dict(passed=True, calls=calls, executed=executed, errors=errors, original_attempts_preserved=True)
    (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else 'outputs/manual-wait-fix-20261111',
                     stale_wait='--stale-wait' in sys.argv))
