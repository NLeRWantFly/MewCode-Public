"""Host UI endpoints for the network policy; never inferred from model text."""
from __future__ import annotations

import asyncio
from fastapi import Request
from fastapi.responses import JSONResponse

from mewcode.permissions.network_policy import NetworkPolicyStore
from mewcode.sandbox.srt_runtime import runtime_status


def register_network_routes(app, get_session, run_control):
    async def state():
        policy = NetworkPolicyStore().read()
        runtime = await asyncio.to_thread(runtime_status)
        session = get_session()
        checker = getattr(getattr(session, 'agent', None), 'permission_checker', None)
        native = checker is None or checker.uses_native_commands()
        conversation = getattr(getattr(session, 'session', None), 'session_id', '')
        return {'conversation_id': conversation, 'policy': policy.as_dict(), 'runtime': runtime,
                'effective': {'structured_tools_enabled': policy.enabled,
                    'execution_backend': 'native' if native else 'host_reviewed',
                    'command_network_ready': policy.enabled and (not native or runtime['ready'])}}

    @app.get('/api/chat/sandbox-network')
    async def read_network():
        try:
            return JSONResponse(await state(), headers={'Cache-Control': 'no-store'})
        except (ValueError, OSError, RuntimeError) as exc:
            return JSONResponse({'error': str(exc)}, status_code=400)

    @app.post('/api/chat/sandbox-network')
    async def write_network(request: Request):
        if request.headers.get('X-Mew-Sandbox-Control') != '1':
            return JSONResponse({'error': '请从沙箱设置保存网络策略'}, status_code=403)
        try:
            data = await request.json()
            session = get_session()
            cid = getattr(getattr(session, 'session', None), 'session_id', '')
            if not isinstance(data, dict) or data.get('conversation_id') != cid:
                raise ValueError('当前对话已变化，请刷新设置')
            NetworkPolicyStore().save(data, expected_revision=data.get('revision', ''))
            # Policy stores are shared by path. Existing and future children
            # read the new version; abandoned operations are never resubmitted.
            return JSONResponse(await state())
        except (ValueError, TypeError, OSError, RuntimeError) as exc:
            return JSONResponse({'error': str(exc)}, status_code=409 if '已变化' in str(exc) else 400)

    @app.post('/api/chat/sandbox-network/configure')
    async def configure_network(request: Request):
        if request.headers.get('X-Mew-Sandbox-Control') != '1':
            return JSONResponse({'error': '请从沙箱设置配置联网运行器'}, status_code=403)
        try:
            data = await request.json()
            session = get_session()
            cid = getattr(getattr(session, 'session', None), 'session_id', '')
            if not isinstance(data, dict) or data.get('conversation_id') != cid:
                raise ValueError('当前对话已变化，请刷新设置')
            from mewcode.sandbox.srt_setup import configure_and_verify
            outcome = await run_control(configure_and_verify)
            return JSONResponse({**await state(), 'configuration': outcome})
        except (ValueError, TypeError, OSError, RuntimeError) as exc:
            return JSONResponse({'error': str(exc)}, status_code=400)
