"""Exercise exploration controls and extraction in the rendered workbench."""
import asyncio
import json
import re
from pathlib import Path
import socket
import sys
import threading

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from playwright.async_api import async_playwright, expect
import uvicorn

from mewcode.workflow_core import trace
from mewcode.workflow_core.blueprint import documents, extract
from mewcode.workflow_core.chat_tools import UpdateParams
from mewcode.workflow_core.projection import view, node_detail
from mewcode.workflow_core.recipes import crystallize
from mewcode.workflow_core.state import fresh


async def main(directory):
    output = Path(directory).resolve()
    output.mkdir(parents=True, exist_ok=True)
    repo = Path(__file__).resolve().parents[2]
    state = fresh(dict(execution_profile=trace.PROFILE, label='资料整理与校核', goal='按资料类型整理，核对引用并交付。'))
    template = dict(model={'provider_key': 'local', 'model_id': 'gpt-6.1-sol', 'effort': 'max'}, config={})
    trace.update(state, UpdateParams(request_id='draw', intent='execute', items=[
        dict(id='read', label='识别资料类型', task='读取资料并判断处理路线', kind='branch',
             config={'source': 'inputs.type', 'cases': [{'equals': 'text', 'port': 'text'}], 'default': 'image'}),
        dict(id='text', label='整理文本资料', task='保留引用，按主题整理。'),
        dict(id='image', label='识别图片资料', task='必要时识别图片中的文字。'),
        dict(id='check', label='逐轮核对', task='核对实际采用的引用', kind='loop', config={'mode': 'count', 'count': 2,
            'graph': {'nodes': [{'id': 'citation', 'label': '核对引用', 'task': '对照来源并记录结论'}], 'edges': []}}),
        dict(id='parallel', label='并发整理附件', task='子 Agent 独立整理附件索引。'),
        dict(id='deliver', label='合并交付', task='等待引用核对和附件索引，合并交付。')], edges=[
            dict(id='text-route', source='read', target='text', port='text'),
            dict(id='image-route', source='read', target='image', port='image'),
            dict(id='text-check', source='text', target='check'),
            dict(id='image-check', source='image', target='check'),
            dict(id='check-deliver', source='check', target='deliver'),
            dict(id='parallel-deliver', source='parallel', target='deliver')], observations=[
                dict(node_id='read', status='completed', text='本次资料可以直接读取文本。', result={'value': 'text'}),
                dict(node_id='text', status='completed', text='已按主题整理并保留来源。'),
                dict(node_id='image', status='skipped', text='本次无需图片识别。'),
                dict(node_id='check', status='running', text='正在核对第二轮引用。'),
                dict(node_id='check/citation', status='completed', actor='chat', iteration=[1], text='第二轮引用核对结束。'),
                dict(node_id='parallel', status='failed', actor='agent-7', text='初次索引工具失败；主对话继续处理。')]), template)
    record = dict(run_id='e'*32, conversation_id='chat', workflow_core=state, kernel_revision=1,
                  locked_profile={'executor': template['model']}, locked_flow={})
    app, requests, errors = FastAPI(), [], []
    app.mount('/static', StaticFiles(directory=repo/'frontend/static'))

    @app.get('/', response_class=HTMLResponse)
    async def page():
        return '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<link rel="stylesheet" href="/static/workbench/workbench.css"></head><body>
<button id="workbench-toggle">工作台</button><div id="workbench-resize"></div><div id="workbench-backdrop"></div>
<section id="conversation-pane"><p>当前对话正在处理用户任务。</p><textarea id="chat-input"></textarea>
<button id="continue-task-btn">补充当前任务</button></section><aside id="task-workbench"></aside>
<script type="module" src="/fixture.js"></script></body></html>'''

    @app.get('/fixture.js')
    async def script():
        return Response("window.MewWorkbenchHost={context:()=>({conversationId:'chat',project:'test'})};"
            "await import('/static/workbench/index.js');window.MewWorkbench.open();", media_type='text/javascript')

    @app.post('/test/complete')
    async def complete():
        state.update(status='completed', conversation_summary='保留引用与附件索引；原失败尝试保留，实际任务已经交付。')
        return {'ok': True}

    @app.api_route('/api/{path:path}', methods=['GET', 'POST'])
    async def api(path: str, request: Request):
        requests.append((request.method, path))
        if path == 'workbench/view':
            return view(record)
        if path == 'workbench/tasks':
            return {'tasks': [view(record)['task']]}
        if path == 'workbench/node':
            return node_detail(record, request.query_params['node_id'])
        if path == 'workbench/export':
            return documents(extract(record))
        if path == 'workbench/recipes' and request.method == 'POST':
            recipe = crystallize(state, source_run_id=record['run_id'])
            return dict(recipe, id='recipe', version=1)
        return JSONResponse({'error': 'unexpected request'}, status_code=503)

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
            page = await browser.new_page(viewport={'width': 1700, 'height': 1080})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('pageerror', lambda error: print(str(error), flush=True))
            page.on('console', lambda message: print(message.text, flush=True) if message.type == 'error' else None)
            await page.goto(f'http://127.0.0.1:{sock.getsockname()[1]}/')
            await expect(page.locator('[data-node-id="read"]')).to_be_visible(timeout=30000)
            await expect(page.locator('.wb-run-button')).to_be_hidden()
            await expect(page.locator('.wb-run-notice')).to_be_hidden()
            await expect(page.locator('#continue-task-btn')).to_be_hidden()
            await expect(page.get_by_role('button', name='应用并继续', exact=True)).to_be_hidden()
            await expect(page.locator('.wb-workflow-palette')).to_be_hidden()
            await expect(page.get_by_role('button', name='提取为可复用流程', exact=True)).to_be_hidden()
            await expect(page.locator('[data-route="selected"]')).to_have_count(1)
            await expect(page.locator('[data-route="not_selected"]')).to_have_count(1)
            assert await page.evaluate('window.MewWorkbench.chatContinuation()')
            await page.get_by_role('button', name='总览', exact=True).click()
            await page.locator('[data-node-id="parallel"]').click()
            await expect(page.locator('.wb-node-inspector')).to_contain_text('agent-7')
            await expect(page.locator('.wb-node-inspector').get_by_role('button', name='编辑', exact=True)).to_have_count(0)
            assert not any(method == 'POST' for method, _ in requests)
            await page.screenshot(path=output/'exploration.png')
            await page.request.post(page.url.rstrip('/') + '/test/complete')
            await page.evaluate('window.MewWorkbench.store.refresh({tasks:true})')
            extract_button = page.get_by_role('button', name='提取为可复用流程', exact=True)
            await expect(extract_button).to_be_visible()
            await expect(extract_button).to_be_enabled()
            assert await page.evaluate('window.MewWorkbench.chatContinuation()') is None
            await extract_button.click()
            await expect(page.locator('.wb-export-status')).to_contain_text('未启动执行')
            await expect(page.locator('.wb-export-preview')).to_have_value(re.compile('实际任务已经交付'))
            await page.get_by_label('提取格式').select_option('recipe')
            await expect(page.locator('.wb-export-preview')).to_have_value(re.compile('goal.recipe.v1'))
            assert [path for method, path in requests if method == 'POST'] == ['workbench/recipes']
            assert not errors, errors
            await page.screenshot(path=output/'extraction.png')
            await browser.close()
    finally:
        host.should_exit = True
        thread.join(timeout=5)
    (output/'browser-result.json').write_text(json.dumps(dict(errors=errors, requests=requests), ensure_ascii=False, indent=2), encoding='utf-8')
    print('exploration browser passed: readonly trace, selected branch, observations and explicit extraction')


if __name__ == '__main__':
    asyncio.run(main(sys.argv[1]))
