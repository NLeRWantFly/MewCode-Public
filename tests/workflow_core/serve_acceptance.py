"""Isolated real workbench/API/Core; deterministic offline OCR, real files and SQLite.

Run: python -m tests.workflow_core.serve_acceptance <output-directory> <port>
The business adapter is the only replacement. No user project or provider is used.
"""
import asyncio
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

from fastapi.responses import HTMLResponse, Response
import uvicorn

from frontend import server
from mewcode.expert_runs import ExpertRunStore
from mewcode.permissions import PermissionMode
from mewcode.workflow_core import files, revisions
from mewcode.workflow_core.runtime import Core, LIVE
from mewcode.workflow_core.store import ensure
from mewcode.workflow_core.web import envelope
from tests.workflow_core.test_runtime import graph

RUN = 'e' * 32
CHAT = 'workflow-acceptance'


def create_app(root):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    store = ExpertRunStore(root / 'runs', storage='sqlite')
    server.EXPERT_RUNS = store
    session = server.SESSION
    session.work_dir = str(root)
    session.session = SimpleNamespace(session_id=CHAT, _sessions_dir=root / 'sessions')
    session.permission_mode = PermissionMode.DEFAULT
    session.runtime_for = lambda _: SimpleNamespace()
    session.chat_busy = lambda *_: RUN in LIVE
    server._active_conversation_id = lambda: CHAT
    release, started, calls = asyncio.Event(), [], []
    nodes = [dict(id=key, label=title, task=task) for key, title, task in [
        ('environment', '构建环境', '准备 OCR 输出目录和资料库'),
        ('materials', '整理原材料', '拆分两份测试材料，分发到并行识别节点'),
        ('ocr_a', '识别材料 A', '读取材料 A，保存识别结果'),
        ('ocr_b', '识别材料 B', '读取材料 B，保存识别结果'),
        ('database', '汇合入库', '等待两份结果完成，将识别结果存入 SQLite'),
        ('independent', '生成材料索引', '独立整理材料目录，不等待 OCR')]]
    flow = graph(nodes, [('environment','materials','always'),('materials','ocr_a','always'),
        ('materials','ocr_b','always'),('ocr_a','database','always'),('ocr_b','database','always')])
    flow.update(label='OCR · 自由编排验收', goal='环境构建 → 材料处理 → 并行 OCR → 汇合入库')
    if not store.load(RUN):
        store.create(run_id=RUN, conversation_id=CHAT, task=flow['goal'], attachments=[], profile={},
                     strategy='stable', flow=envelope(flow))
    backend = ensure(store, RUN, flow)

    async def execute(ctx):
        key = ctx.node_id
        calls.append(key)
        if ctx.request:
            await ctx.publish('checkpoint', messages=[*(ctx.checkpoint.get('messages') or []),
                {'role': 'user', 'content': ctx.request['message']}])
            if ctx.request['kind'] == 'ask':
                text = '保留原节点上下文：识别结果来自本节点输入材料；原始交付和其他节点进度继续保留。'
                await ctx.publish('progress', text=text)
                await ctx.publish('checkpoint', messages=[*(ctx.checkpoint.get('messages') or []),
                    {'role': 'user', 'content': ctx.request['message']}, {'role': 'assistant', 'content': text}])
                return dict(text=text)
            if ctx.request['kind'] == 'update':
                return dict(text='已收到上游变更通知；离线样例保留既有结果和来源版本。')
            folder, mapping = revisions.prepare(root, RUN, ctx.request, ctx.inputs.get('previous_delivery') or {}, store.root)
            output = folder / (key + '.txt')
            output.write_text('PAGE ' + key[-1].upper() + ' · 第 53 页公式已补齐', encoding='utf-8')
            artifact = files.capture(store.root, RUN, ctx.attempt_id, output)
            artifact['logical_path'] = mapping.get(str(output.resolve())) or str(root / output.name)
            await ctx.publish('artifact', artifact=artifact)
            text = '已补齐第 53 页公式并登记新版本，其余材料沿用原交付。'
            await ctx.publish('progress', text=text)
            await ctx.publish('checkpoint', messages=[*(ctx.checkpoint.get('messages') or []),
                {'role': 'user', 'content': ctx.request['message']}, {'role': 'assistant', 'content': text}])
            return dict(text=text)
        await ctx.publish('progress', text=f'已开始{ctx.node["label"]}。')
        if key.startswith('ocr_') and not release.is_set():
            started.append(key)
            await release.wait()
        output = root / (key + ('.sqlite' if key == 'database' else '.txt'))
        if key == 'database':
            assert ctx.inputs['ocr_a']['text'] == 'PAGE A'
            assert ctx.inputs['ocr_b']['text'] == 'PAGE B'
            with sqlite3.connect(output) as db:
                db.execute('CREATE TABLE IF NOT EXISTS documents (name TEXT PRIMARY KEY, text TEXT)')
                for name in ('ocr_a', 'ocr_b'):
                    db.execute('INSERT OR REPLACE INTO documents VALUES (?, ?)', (name, ctx.inputs[name]['text']))
            text = '两份材料已汇合入库，可在节点面板下载数据库。'
        elif key.startswith('ocr_'):
            text = 'PAGE ' + key[-1].upper()
            output.write_text(text, encoding='utf-8')
            for index in range(6):
                await ctx.publish('progress', text=f'已核对样例第 {index+1} 行文字。')
        else:
            text = ctx.node['label'] + '已完成'
            output.write_text(text, encoding='utf-8')
        await ctx.publish('tool_result', name='OfflineOCRFixture', output='Diagnostic detail for acceptance')
        await ctx.publish('artifact', artifact=files.capture(store.root, RUN, ctx.attempt_id, output))
        await ctx.publish('progress', text='结果已保存，后续节点可读取本次产物。')
        await ctx.publish('checkpoint', messages=[{'role': 'user', 'content': ctx.node['task']},
                                                 {'role': 'assistant', 'content': text}])
        return dict(text=text)

    async def report(attempt):
        value = files.write_report(store.root, RUN, attempt)
        backend.change(lambda s: s['attempts'][attempt['id']].update(report=value))

    active = set()
    async def launch(_):
        task = asyncio.create_task(Core(backend, execute, concurrency=4, run_id=RUN, report=report).run())
        active.add(task)
        task.add_done_callback(active.discard)
        return dict(run_id=RUN)
    server.launch_expert_resume = launch

    @server.app.get('/workflow-acceptance', response_class=HTMLResponse)
    async def page():
        return '''<!doctype html><html lang="zh-CN" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>OCR 工作台验收</title>
<link rel="stylesheet" href="/static/workbench/workbench.css"></head><body>
<button id="workbench-toggle">工作台</button><div id="workbench-resize"></div><div id="workbench-backdrop"></div>
<aside id="task-workbench"></aside><script type="module" src="/workflow-acceptance.js"></script></body></html>'''

    @server.app.get('/workflow-acceptance.js')
    async def script():
        scope = json.dumps(dict(conversationId=CHAT, project=str(root)))
        return Response('window.MewWorkbenchHost={context:()=>(' + scope + ')};'
            "await import('/static/workbench/index.js');window.MewWorkbench.open();"
            'setInterval(()=>window.MewWorkbench.store.refresh(),500);', media_type='text/javascript')

    @server.app.get('/workflow-acceptance-state')
    async def state():
        return dict(state=backend.read(), started=started, calls=calls)

    @server.app.post('/workflow-acceptance-release')
    async def unblock():
        release.set()
        return dict(ok=True)

    return server.app


if __name__ == '__main__':
    uvicorn.run(create_app(sys.argv[1]), host='127.0.0.1', port=int(sys.argv[2]), lifespan='off', log_level='warning')
