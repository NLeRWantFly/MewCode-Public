"""Exercise the unchanged workbench shell with declarative input/output blocks."""
import asyncio
import base64
import json
from pathlib import Path
import socket
import sys
import threading

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from playwright.async_api import async_playwright, expect
import uvicorn

from mewcode.workflow_core.presentation import publish
from mewcode.workflow_core.projection import view, node_detail
from mewcode.workflow_core.state import fresh, start_attempt, finish_attempt
from tests.workflow_core.test_runtime import graph


async def main(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    repo = Path(__file__).resolve().parents[2]
    state = fresh(graph([dict(id='chapter',kind='work',label='行测章节 · 布局验证',
        task='仅验证输入输出组件的布局和刷新，不代表业务试跑结论。',config=dict(presentation=True))]))
    state['status'] = 'running'
    attempt = start_attempt(state,'chapter',{},[])
    state['attempts'][attempt['id']]['artifacts'] = [dict(id='image-fixture',name='fixture.png')]
    blocks = [
        dict(block_id='material',slot='input',kind='text',title='输入材料',text='行测章节：365 页、5,195 个源块。这里仅检查界面展示。'),
        dict(block_id='image',slot='output',kind='image',title='图片组件验证',artifact_id='image-fixture'),
        dict(block_id='table',slot='output',kind='table',title='表格组件验证',columns=['类型','展示'],rows=[['文字','原样显示'],['图表','根据数据绘制']]),
        dict(block_id='structure',slot='output',kind='diagram',title='关系图组件验证',nodes=[dict(id='a',label='源材料'),dict(id='b',label='章节结构'),dict(id='c',label='输出索引')],edges=[dict(source='a',target='b'),dict(source='b',target='c')]),
        dict(block_id='chart',slot='output',kind='chart',title='图表组件验证',labels=['文字','表格','图像'],series=[dict(name='布局示例',values=[3,2,1])])]
    for block in blocks:
        publish(state,attempt['id'],**block)
    finish_attempt(state,attempt['id'],dict(text='布局验证记录'))
    state['status'] = 'completed'
    record = dict(run_id='visual-run',conversation_id='visual-chat',workflow_core=state)
    document = view(record)
    app = FastAPI()
    app.mount('/static',StaticFiles(directory=repo/'frontend/static'))

    @app.get('/',response_class=HTMLResponse)
    async def page(theme='light'):
        theme = 'dark' if theme == 'dark' else 'light'
        return '<!doctype html><html lang="zh-CN" data-theme="'+theme+'"><head><meta charset="utf-8"><link rel="stylesheet" href="/static/workbench/workbench.css"></head><body><button id="workbench-toggle">工作台</button><div id="workbench-resize"></div><div id="workbench-backdrop"></div><aside id="task-workbench"></aside><script type="module" src="/fixture.js"></script></body></html>'

    @app.get('/fixture.js')
    async def fixture():
        return Response('window.MewWorkbenchHost={context:()=>({conversationId:"visual-chat",project:"visual"})};await import("/static/workbench/index.js");window.MewWorkbench.open();',media_type='text/javascript')

    @app.get('/api/workbench/view')
    async def read_view():
        return document

    @app.get('/api/workbench/tasks')
    async def task_list():
        return dict(tasks=[document['task']])

    @app.get('/api/workbench/node')
    async def read_node():
        return node_detail(record,'chapter')

    @app.get('/api/workbench/artifact')
    async def image():
        return Response(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jhVQAAAAASUVORK5CYII='),media_type='image/png')

    sock = socket.socket()
    sock.bind(('127.0.0.1',0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app,log_level='warning'))
    thread = threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True)
    thread.start()
    while not server.started:
        await asyncio.sleep(.02)
    errors = []
    try:
        async with async_playwright() as api:
            browser = await api.chromium.launch(headless=True)
            for theme in ('light','dark'):
                page = await browser.new_page(viewport={'width':1600,'height':1000})
                page.on('pageerror',lambda error:errors.append(str(error)))
                await page.goto(f'http://127.0.0.1:{port}/?theme={theme}')
                card = page.locator('.wb-workflow-card')
                await expect(card).to_have_count(1)
                await expect(card.locator('.wb-io-group')).to_have_count(2)
                await expect(card.locator('svg[role="img"]')).to_have_count(1)
                chart_box = await card.locator('svg[role="img"]').bounding_box()
                output_box = await card.locator('.wb-io-group[data-slot="output"]').bounding_box()
                assert chart_box['y'] >= output_box['y'], '图表不能覆盖节点标题'
                assert chart_box['y'] + chart_box['height'] <= output_box['y'] + output_box['height'] + 1
                await page.screenshot(path=str(output/(theme+'-canvas.png')),full_page=True)
                await card.dblclick()
                dialog = page.get_by_role('dialog', name='行测章节 · 布局验证 · 节点对话', exact=True)
                await expect(dialog).to_be_visible()
                await expect(dialog.locator('.wb-io-block')).to_have_count(5)
                await expect(dialog.locator('svg[role="img"]')).to_have_count(2)
                await expect(dialog.locator('img')).to_have_js_property('complete', True)
                assert await dialog.locator('img').evaluate('(image)=>image.naturalWidth>0')
                await page.screenshot(path=str(output/(theme+'-detail.png')),full_page=True)
                await page.reload()
                await expect(page.locator('.wb-workflow-card .wb-io-group')).to_have_count(2)
                await page.close()
            await browser.close()
        assert not errors, errors
    finally:
        server.should_exit = True
        await asyncio.to_thread(thread.join,5)
    result = dict(passed=True,themes=['light','dark'],blocks=5,refresh_preserved=True,errors=errors)
    (output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(main(sys.argv[1]))
