"""Production composer/canvas and recovery service, with an offline node executor."""
import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import threading
from types import SimpleNamespace
import uuid


async def main(output):
    repo = Path(__file__).resolve().parents[2]
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    root = repo / ('.tmp-chat-queue-integration-recovery-' + uuid.uuid4().hex[:10])
    isolated = repo / '.tmp' / ('recovery-browser-' + uuid.uuid4().hex[:10])
    Path.home = classmethod(lambda cls: isolated / 'home')
    os.environ.update(MEWCODE_USER_DATA_DIR=str(isolated / 'userdata'), MEWCODE_DESKTOP_PROFILE_ISOLATED='1')
    connect = socket.socket.connect
    def local_only(sock, address):
        if isinstance(address, tuple) and address[0] not in {'127.0.0.1', 'localhost', '::1'}:
            raise OSError('Browser fixture forbids external connections')
        return connect(sock, address)
    socket.socket.connect = local_only

    from fastapi import WebSocket, WebSocketDisconnect, Request
    from playwright.async_api import async_playwright, expect
    import uvicorn
    from frontend import server
    from frontend.workbench_api import install
    from frontend.workflow_recovery import handle_chat
    from mewcode.conversation import ConversationManager
    from mewcode.expert_runs import ExpertRunStore
    from mewcode.orchestration.chat_routing import set_dialogue_focus
    from mewcode.permissions import PermissionMode
    from mewcode.workflow_core.runtime import Core
    from mewcode.workflow_core.state import start_attempt, finish_attempt
    from mewcode.workflow_core.store import ensure
    from mewcode.workflow_core.web import envelope
    from tests.chat_queue_render_server import fixture_app, CONVERSATION_ID as cid, FIXTURE_TOKEN
    from tests.workflow_core.test_runtime import graph

    app = fixture_app(root)
    project, rid = root / 'project', 'b' * 32
    store = ExpertRunStore(root / 'runs', storage='sqlite')
    flow = graph([dict(id='source', label='读取资料'), dict(id='work', label='处理样章'), dict(id='deliver', label='交付结果')],
                 [('source', 'work', 'always'), ('work', 'deliver', 'always')])
    store.create(run_id=rid, conversation_id=cid, task='保留已完成资料，续接样章处理', attachments=[],
                 profile={}, strategy='stable', flow=envelope(flow))
    backend = ensure(store, rid, flow)
    def seed(state):
        source = start_attempt(state, 'source', {}, [])
        finish_attempt(state, source['id'], dict(status='completed', text='原资料已交付'))
        work = start_attempt(state, 'work', {}, [])
        work['messages'] = [dict(role='assistant', content='样章前半段已经保存')]
        finish_attempt(state, work['id'], dict(status='blocked', reason='模型连接中断，样章尚未交付'))
        state['status'] = 'blocked'
    backend.change(seed)
    original = backend.read()
    calls, frames, clients, history = [], [], set(), []
    durable = SimpleNamespace(session_id=cid, meta=SimpleNamespace(dialogue_focus={}),
                              save_meta=lambda: None, append=lambda message: history.append(message))
    runtime = SimpleNamespace(conversation_id=cid, work_dir=str(project), session=durable,
        conversation=ConversationManager(), task=None, execution_kind='orchestration', expert_run_id=rid,
        agent=None, tasks_payload=lambda: [])
    base_state = next(route.endpoint for route in app.routes if getattr(route, 'path', '') == '/api/state')
    def busy(*_):
        return runtime.task is not None and not runtime.task.done()
    def status():
        value = base_state()['status']
        value.update(execution_kind='orchestration', expert_mode=True, conversation_title='对话诊断与续跑验证')
        return value
    runtime.status_payload = status
    owner = SimpleNamespace(work_dir=str(project), session=durable, permission_mode=PermissionMode.AUTO,
        capture_conversation_runtime=lambda: runtime, runtime_for=lambda _: runtime, chat_busy=busy)
    set_dialogue_focus(runtime, 'orchestration', task='样章处理', run_id=rid)
    async def broadcast(event):
        for client in list(clients):
            try:
                await client.send_json(dict(event, conversation_id=cid))
            except Exception:
                clients.discard(client)
    srv = SimpleNamespace(app=app, SESSION=owner, EXPERT_RUNS=store, _active_conversation_id=lambda: cid,
        ConversationWebSocket=server.ConversationWebSocket, WS_EVENTS=SimpleNamespace(send_json=broadcast))
    async def launch(record):
        await broadcast(dict(type='turn_started', expert_mode=True, client_echoed=True, resume_run_id=rid))
        async def execute(ctx):
            calls.append(ctx.node_id)
            if ctx.node_id == 'work':
                assert ctx.checkpoint['messages'][0]['content'] == '样章前半段已经保存'
                assert ctx.recovery['request'] == '检查为什么卡住并继续'
            await ctx.publish('progress', text='正在从保存的进度接续')
            await asyncio.sleep(.3)
            return dict(text='未完成部分已接续交付')
        async def run():
            await broadcast(dict(type='expert_run_state', status='running', run_id=rid))
            result = await Core(backend, execute, run_id=rid, emit=broadcast).run()
            await broadcast(dict(type='expert_run_state', status=result['status'], run_id=rid))
            await broadcast(dict(type='done', interrupted=False, elapsed=1, status=status(), tasks=[]))
        runtime.task = asyncio.create_task(run())
    srv.launch_expert_resume = launch
    install(app, srv)
    app.router.routes[:] = [route for route in app.routes if getattr(route, 'path', '') != '/ws']
    @app.websocket('/ws')
    async def ws(client: WebSocket):
        await client.accept()
        clients.add(client)
        await client.send_json(dict(type='state', **dict(base_state(), status=status())))
        try:
            while True:
                frame = await client.receive_json()
                frames.append(frame)
                if frame.get('type') == 'chat':
                    result = await handle_chat(srv, cid=cid, text=frame['text'], request_id=frame['request_id'],
                                             context=frame.get('workbench_context'))
                    assert result and result['handled']
        except WebSocketDisconnect:
            pass
        finally:
            clients.discard(client)
    @app.post('/api/orchestration/interventions')
    async def intervention(request: Request):
        frame = await request.json()
        frames.append(frame)
        return await handle_chat(srv, cid=cid, text=frame['text'], request_id=frame['queue_id'],
                                 context=frame.get('workbench_context'), client_echoed=False)
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
            page = await browser.new_page(viewport={'width': 1600, 'height': 1000})
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.goto(f'http://127.0.0.1:{port}/desktop-boot?session={FIXTURE_TOKEN}&next=/?workbench=structure')
            await expect(page.locator('.wb-workflow-card[data-node-id="work"]')).to_be_visible(timeout=60000)
            await expect(page.locator('#btn-send')).to_be_enabled(timeout=60000)
            await page.locator('#chat-input').fill('检查为什么卡住，先不要继续')
            await page.locator('#btn-send').click()
            await expect(page.locator('#messages')).to_contain_text('检查结果：', timeout=60000)
            assert backend.read()['nodes'] == original['nodes'] and not calls
            await expect(page.locator('#btn-send')).to_be_enabled(timeout=60000)
            await page.locator('#chat-input').fill('检查为什么卡住并继续')
            await page.locator('#btn-send').click()
            await expect(page.locator('.wb-run-state')).to_have_text('已完成', timeout=120000)
            await page.screenshot(path=str(output / 'recovery-completed.png'), full_page=True)
            assert calls == ['work', 'deliver']
            assert all(frame.get('workbench_context', {}).get('run_id') == rid for frame in frames if frame.get('type') == 'chat')
            assert all(backend.read()['attempts'][key] == value for key, value in original['attempts'].items())
            assert not errors, errors
            result = dict(passed=True, executor='offline fixture', calls=calls, errors=errors,
                          retained_attempts=len(original['attempts']), requests=len(frames), root=str(root))
            (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(result, ensure_ascii=False), flush=True)
            await browser.close()
    finally:
        host.should_exit = True
        await asyncio.to_thread(thread.join, 5)
        srv.WORKBENCH_NOTIFIER.close()


if __name__ == '__main__':
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else 'outputs/workflow-chat-recovery-20261111'))
