"""Real browser checks for node conversations; isolated state and no model calls."""
import asyncio
import copy
import json
from pathlib import Path
import socket
import sys
import threading
import time

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from playwright.async_api import async_playwright, expect
import uvicorn

from mewcode.workflow_core import dialogue
from mewcode.workflow_core.activity import observe_node_detail, observe_view
from mewcode.workflow_core.projection import node_detail, view
from mewcode.workflow_core.state import finish_attempt, fresh, start_attempt
from tests.workflow_core.test_runtime import graph


def create_app():
    state = fresh(graph([dict(id='prepare', label='清点交付与准备 ElegantBook 环境'),
                         dict(id='structure', label='确定内容关系规则与样章范围')],
                        [('prepare', 'structure', 'always')]))
    state['graph']['layout'] = {'prepare': {'x': 30, 'y': 40}, 'structure': {'x': 450, 'y': 40}}
    primary = start_attempt(state, 'prepare', {}, [])
    finish_attempt(state, primary['id'], dict(text='首次执行已经完成。', artifacts=[]))
    consumer = start_attempt(state, 'structure', {}, [])
    request = dialogue.enqueue(state, dict(node_id='prepare', kind='revise', message='补充三个代表单元的固定快照。',
                                           request_id='need-samples'), sender=consumer)
    _, reply, _ = dialogue.start_request(state, state['node_requests'][request['id']])
    reply = state['attempts'][reply['id']]
    now = time.time()
    reply.update(progress=[dict(text='正在整理三个代表单元，保留已有交付。', at=now - 8)],
        phase_events=[dict(phase='reasoning', at=now - 15), dict(phase='tool_running', at=now - 10)],
        execution=dict(phase='reasoning', text='模型正在思考', active=True, at=now),
        pending_tools={'read': dict(name='ReadFile', arguments={'file_path': '材料清单.json'},
                                   status='completed', started_at=now - 10, ended_at=now - 8)},
        diagnostics=[dict(kind='tool_result', name='ReadFile', tool_id='read', output='已读取 3 个代表单元。',
                          is_error=False, at=now - 8)])
    dialogue.wait_for(state, consumer['id'], request['id'])
    consumer.update(status='suspended', progress=[dict(text='规则已经保存，等待补交材料。', at=now - 4)])
    state['status'] = 'running'
    record = dict(run_id='conversation-browser', workflow_core=state)
    calls = []

    def live():
        return dict(scheduler_active=True, active_node_ids=['prepare'], active_attempt_ids=[reply['id']],
                    attempt_executions={reply['id']: reply['execution']})

    def projected():
        return observe_view(view(record), live=live())

    app = FastAPI()
    repo = Path(__file__).resolve().parents[2]
    app.mount('/static', StaticFiles(directory=repo / 'frontend/static'))

    @app.get('/', response_class=HTMLResponse)
    async def page():
        return '''<!doctype html><html lang="zh-CN" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>节点对话验证</title>
<link rel="stylesheet" href="/static/workbench/workbench.css"></head><body>
<aside id="task-workbench"><div id="canvas" style="display:flex;flex-direction:column;flex:1;min-height:0"></div></aside>
<script type="module" src="/fixture.js"></script></body></html>'''

    @app.get('/fixture.js')
    async def script():
        return Response('''import {mountWorkflowCanvas} from '/static/workbench/workflow-canvas.js';
import {createAPI} from '/static/workbench/api.js';
const api=createAPI();let canvas;
const store={generation:1,state:{scope:{conversation_id:'fixture',project:'fixture'}},
 accept(value){this.state.view=value;canvas.update(value);},async refresh(){this.accept(await api.get('/fixture/view'));}};
canvas=mountWorkflowCanvas(document.querySelector('#canvas'),{api,store,onMutate:async()=>{throw Error('Unexpected graph edit');}});
window.fixtureStore=store;window.fixtureCanvas=canvas;await store.refresh();
setInterval(()=>store.refresh(),1500);''', media_type='text/javascript')

    @app.get('/fixture/view')
    async def read_view():
        return projected()

    @app.get('/api/workbench/node')
    async def read_node(request: Request):
        return observe_node_detail(node_detail(record, request.query_params['node_id']), live=live())

    @app.post('/api/workbench/node-message')
    async def send(request: Request):
        body = await request.json()
        calls.append(copy.deepcopy(body))
        result = dialogue.enqueue(state, body)
        if len(calls) == 1:
            return JSONResponse({'error': '测试：响应丢失，请重试'}, status_code=503)
        return dict(view=projected(), request=result)

    @app.post('/fixture/progress')
    async def progress():
        reply['progress'].append(dict(at=time.time(), text='新进展已到达。'))
        return dict(ok=True)

    @app.post('/fixture/block')
    async def block():
        consumer.update(status='blocked', result={'reason': '模型服务连接中断：connection reset'})
        state['nodes']['structure']['status'] = 'blocked'
        return dict(ok=True)

    @app.post('/api/workbench/run-action')
    async def action(request: Request):
        calls.append(await request.json())
        return dict(view=projected())

    return app, calls


async def main(directory):
    output = Path(directory).resolve()
    output.mkdir(parents=True, exist_ok=True)
    app, calls = create_app()
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
    errors = []
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={'width': 1600, 'height': 1000})
            page.on('pageerror', lambda exc: errors.append(str(exc)))
            try:
                await page.goto(f'http://127.0.0.1:{port}/')
                card = page.locator('.wb-workflow-card[data-node-id="prepare"]')
                await card.dblclick()
                dialog = page.get_by_role('dialog')
                await expect(dialog).to_be_visible()
                await expect(dialog.locator('.wb-conversation-log')).to_contain_text('正在整理三个代表单元')
                await expect(dialog.locator('.wb-conversation-log')).not_to_contain_text('首次执行已经完成')
                await dialog.get_by_text('思考 · 5.0 秒', exact=True).click()
                tool = dialog.locator('.wb-conversation-tool')
                await tool.locator('summary').click()
                await expect(tool).to_contain_text('材料清单.json')
                await expect(tool).to_contain_text('已读取 3 个代表单元')
                await page.request.post(f'http://127.0.0.1:{port}/fixture/progress')
                await expect(dialog.locator('.wb-conversation-log')).to_contain_text('新进展已到达')
                await expect(dialog.locator('.wb-conversation-tool')).to_have_attribute('open', '')
                text = dialog.get_by_role('textbox', name='给此节点的消息')
                await text.fill('请说明第一个单元的来源。')
                await dialog.get_by_role('button', name='首次执行').click()
                await expect(dialog.locator('.wb-conversation-log')).to_contain_text('首次执行已经完成')
                await expect(text).to_have_value('请说明第一个单元的来源。')
                await dialog.get_by_role('button', name='发送', exact=True).click()
                await expect(dialog.locator('.wb-conversation-notice')).to_contain_text('响应丢失')
                await expect(text).to_have_value('请说明第一个单元的来源。')
                await dialog.get_by_role('button', name='发送', exact=True).click()
                await expect(text).to_have_value('')
                assert calls[0]['request_id'] == calls[1]['request_id']
                assert not any(c.get('operation') in {'pause', 'rerun'} for c in calls)
                await page.keyboard.press('Escape')
                await expect(dialog).to_have_count(0)
                await page.locator('.wb-workflow-card[data-node-id="structure"]').dblclick()
                dialog = page.get_by_role('dialog')
                await expect(dialog.locator('.wb-conversation-banner')).to_contain_text('正在等待节点回复')
                await expect(dialog.locator('.wb-conversation-reasons')).to_contain_text('补充三个代表单元')
                await expect(dialog.get_by_role('button', name='继续此节点')).to_have_count(0)
                await page.screenshot(path=output / 'waiting-conversation.png')
                await dialog.get_by_role('button', name='查看等待节点的对话').click()
                await expect(page.get_by_role('dialog')).to_have_count(1)
                await expect(page.get_by_role('dialog').locator('h2')).to_have_text('清点交付与准备 ElegantBook 环境')
                await page.keyboard.press('Escape')
                await page.locator('.wb-workflow-card[data-node-id="structure"]').focus()
                await page.keyboard.press('Enter')
                await expect(page.get_by_role('dialog')).to_be_visible()
                await page.request.post(f'http://127.0.0.1:{port}/fixture/block')
                await expect(page.get_by_role('dialog').locator('.wb-conversation-banner')).to_contain_text('connection reset')
                await page.set_viewport_size({'width': 900, 'height': 820})
                await page.get_by_role('dialog').get_by_role('button', name='状态与原因', exact=True).click()
                await expect(page.locator('.wb-conversation-reasons')).to_be_visible()
                await expect(page.locator('.wb-conversation-reasons')).to_contain_text('connection reset')
                await page.screenshot(path=output / 'blocked-conversation.png')
                await page.get_by_role('dialog').get_by_role('button', name='关闭原因详情').click()
                await page.evaluate('window.fixtureStore.generation++;window.fixtureCanvas.update(window.fixtureStore.state.view)')
                await expect(page.get_by_role('dialog')).to_have_count(0)
                assert not errors, errors
            except BaseException:
                await page.screenshot(path=output / 'failure.png')
                raise
            finally:
                await browser.close()
    finally:
        host.should_exit = True
        thread.join(timeout=5)
        (output / 'result.json').write_text(json.dumps(dict(calls=calls, errors=errors), ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(dict(double_click=True, keyboard=True, current_reply=True, tool_details=True,
        waiting_reason=True, stopped_reason=True, retry_idempotent=True, scope_cleanup=True, errors=errors)))


if __name__ == '__main__':
    asyncio.run(main(sys.argv[1]))
