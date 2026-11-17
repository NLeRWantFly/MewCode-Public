import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest

from frontend import startup_recovery


@pytest.mark.asyncio
async def test_health_and_loading_page_respond_while_task_operations_are_fenced():
    release = asyncio.Event()
    entered = asyncio.Event()
    order = []

    @asynccontextmanager
    async def recover(app):
        order.append('recovery_started')
        entered.set()
        await release.wait()
        order.append('plugin_started')
        try:
            yield
        finally:
            order.append('plugin_stopped')

    app = FastAPI(lifespan=recover)
    startup_recovery.install(app)
    writes = []

    @app.middleware('http')
    async def gate(request, next_handler):
        response = startup_recovery.blocked_response(app, request.url.path, wants_html=request.url.path == '/')
        return response if response is not None else await next_handler(request)

    @app.get('/api/health')
    async def health():
        return {'startup_state': startup_recovery.phase(app)}

    @app.post('/api/task')
    async def change_task():
        writes.append('task_called')
        return {'ok': True}

    @app.get('/desktop-boot')
    async def boot():
        return {'normal_handoff': True}

    async with app.router.lifespan_context(app):
        await entered.wait()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.get('/api/health')).json() == {'startup_state': 'restoring'}
            loading = await client.get('/')
            assert loading.status_code == 503 and 'http-equiv="refresh"' in loading.text
            assert (await client.get('/desktop-boot')).status_code == 200
            blocked = await client.post('/api/task')
            assert blocked.status_code == 503 and blocked.json()['error_code'] == 'startup_restoring'
            assert not writes and order == ['recovery_started']
            release.set()
            for _ in range(20):
                if startup_recovery.phase(app) == 'ready':
                    break
                await asyncio.sleep(0)
            assert (await client.get('/api/health')).json() == {'startup_state': 'ready'}
            assert (await client.post('/api/task')).status_code == 200
    assert writes == ['task_called']
    assert order == ['recovery_started', 'plugin_started', 'plugin_stopped']


@pytest.mark.asyncio
async def test_failed_recovery_stays_closed_and_does_not_retry_or_start_plugins():
    failure_seen = asyncio.Event()
    attempts = []

    @asynccontextmanager
    async def recover(app):
        attempts.append('once')
        failure_seen.set()
        raise ValueError('fixture integrity failure')
        yield

    app = FastAPI(lifespan=recover)
    startup_recovery.install(app)
    async with app.router.lifespan_context(app):
        await failure_seen.wait()
        assert startup_recovery.phase(app) == 'failed'
        blocked = startup_recovery.blocked_response(app, '/api/task', wants_html=False)
        assert blocked.status_code == 503 and b'"retryable":false' in blocked.body
        page = startup_recovery.blocked_response(app, '/', wants_html=True)
        assert b'http-equiv="refresh"' not in page.body
    assert attempts == ['once']


@pytest.mark.asyncio
async def test_shutdown_waits_for_recovery_then_unwinds_original_lifespan():
    release, entered = asyncio.Event(), asyncio.Event()
    order = []

    @asynccontextmanager
    async def recover(app):
        entered.set()
        await release.wait()
        order.append('started')
        try:
            yield
        finally:
            order.append('stopped')

    app = FastAPI(lifespan=recover)
    startup_recovery.install(app)
    context = app.router.lifespan_context(app)
    await context.__aenter__()
    await entered.wait()
    closing = asyncio.create_task(context.__aexit__(None, None, None))
    await asyncio.sleep(0)
    assert not closing.done() and not order
    release.set()
    await closing
    assert order == ['started', 'stopped']


@pytest.mark.asyncio
async def test_server_authentication_and_websocket_remain_closed_during_recovery(monkeypatch):
    from frontend import server
    monkeypatch.setattr(server, 'DESKTOP_TOKEN', 'fixture-secret')
    monkeypatch.setattr(server.app.state, 'startup_recovery', startup_recovery.RecoveryState(), raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        assert (await client.get('/api/state')).status_code == 401
        headers = {'Cookie': server.DESKTOP_COOKIE + '=fixture-secret'}
        blocked = await client.post('/api/orchestration/task-design', json={}, headers=headers)
        assert blocked.status_code == 503 and blocked.json()['error_code'] == 'startup_restoring'
        assert (await client.get('/api/health')).json()['ready'] is False
    closes = []
    async def close(**kwargs):
        closes.append(kwargs['code'])
    socket = SimpleNamespace(cookies={server.DESKTOP_COOKIE: 'fixture-secret'}, close=close)
    await server.ws_endpoint(socket)
    assert closes == [1013]
    socket.cookies = {}
    await server.ws_endpoint(socket)
    assert closes == [1013, 4401]


def test_missing_lifespan_marker_preserves_direct_test_and_embedding_behavior():
    app = FastAPI()
    assert startup_recovery.phase(app) == 'ready'
    assert startup_recovery.blocked_response(app, '/api/task', wants_html=False) is None
