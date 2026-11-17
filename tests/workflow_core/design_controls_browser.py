"""Check the real workbench's pre-run Continue/Pause buttons without model calls."""
import asyncio
import copy
import json
from pathlib import Path
import socket
import sys
import threading

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from playwright.async_api import async_playwright, expect
import uvicorn

from mewcode.orchestration import task_view
from mewcode.orchestration.task_design import design_view
from mewcode.orchestration.workflow_draft import seed_document
from mewcode.workflow_core.planning import design_projection, spec_for_graph


async def main(directory):
    output = Path(directory).resolve()
    output.mkdir(parents=True, exist_ok=True)
    repo = Path(__file__).resolve().parents[2]
    cid, project = 'design-control-browser', str(output)
    spec = spec_for_graph(dict(label='续接设计', nodes=[dict(id='work', kind='work', task='整理材料')])).as_dict()
    state = dict(task_design=dict(task_id='design', revision=1, status='paused', intent='plan', goal='整理材料'),
                 materials=dict(document=seed_document(spec), base_spec=spec))
    calls, errors = [], []

    def view():
        design = design_view(state)
        result = task_view.design_view(design, conversation_id=cid, entries=design['specification']['entries'])
        return task_view.finish(design_projection(result, state, spec, cid))

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
        scope = json.dumps(dict(conversationId=cid, project=project))
        return Response('window.MewWorkbenchHost={context:()=>(' + scope + ')};'
                        "await import('/static/workbench/index.js');window.MewWorkbench.open();",
                        media_type='text/javascript')

    @app.get('/api/workbench/view')
    async def read_view():
        return view()

    @app.get('/api/workbench/tasks')
    async def tasks():
        return dict(tasks=[view()['task']])

    @app.get('/api/orchestration/task-design')
    async def design():
        return dict(conversation_id=cid, task_design=design_view(state))

    @app.post('/api/orchestration/task-design')
    async def control(request: Request):
        body = await request.json()
        calls.append(copy.deepcopy(body))
        if len(calls) == 1:
            return JSONResponse({'error': '暂时无法续接，请重试'}, status_code=409)
        assert body['expected_task_id'] == 'design'
        assert body['expected_revision'] == state['task_design']['revision']
        state['task_design'].update(revision=state['task_design']['revision'] + 1,
            status='designing' if body['action'] == 'resume' else 'paused')
        return dict(conversation_id=cid, resumed=True, task_design=design_view(state))

    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
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
            await page.goto(f'http://127.0.0.1:{port}/')
            button = page.locator('.wb-run-button')
            await expect(button).to_have_text('▶ 继续')
            await button.click()
            await expect(page.get_by_text('暂时无法续接，请重试', exact=True)).to_be_visible()
            await expect(button).to_be_enabled()
            # Refresh returns the exact same view version after this failure.
            await page.wait_for_timeout(500)
            await expect(button).to_be_enabled()
            await button.click()
            await expect(button).to_have_text('Ⅱ 暂停')
            await expect(button).to_be_enabled()
            await button.click()
            await expect(button).to_have_text('▶ 继续')
            await expect(button).to_be_enabled()
            assert [c['action'] for c in calls] == ['resume', 'resume', 'pause'], calls
            assert [c['expected_revision'] for c in calls] == [1, 1, 2], calls
            assert all('run_id' not in c for c in calls), calls
            assert not errors, errors
            await page.screenshot(path=output / 'continue-controls.png')
            await browser.close()
    finally:
        host.should_exit = True
        thread.join(timeout=5)
        result = dict(actions=calls, errors=errors, business_started=False)
        (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(dict(actions=[c['action'] for c in calls], errors=errors, retry_enabled=True)))


if __name__ == '__main__':
    asyncio.run(main(sys.argv[1]))
