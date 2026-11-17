"""Keep the desktop available while saved state recovers; fence task access until ready."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import logging

from fastapi.responses import HTMLResponse, JSONResponse


@dataclass
class RecoveryState:
    phase: str = 'restoring'


def phase(app) -> str:
    recovery = getattr(app.state, 'startup_recovery', None)
    return recovery.phase if recovery is not None else 'ready'


def blocked_response(app, path: str, *, wants_html: bool):
    current = phase(app)
    if current == 'ready' or path in {'/api/health', '/desktop-boot'}:
        return None
    text = ('正在恢复已保存的任务，完成后将自动打开。' if current == 'restoring'
            else '恢复已保存任务时遇到错误。请查看应用启动日志后重新打开。')
    headers = {'Cache-Control': 'no-store', 'Retry-After': '3'}
    if wants_html:
        refresh = '<meta http-equiv="refresh" content="3">' if current == 'restoring' else ''
        return HTMLResponse(
            '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            + refresh + '<title>恢复任务</title></head><body><p>' + text + '</p></body></html>',
            status_code=503, headers=headers,
        )
    return JSONResponse({'error': text, 'error_code': 'startup_' + current,
                         'retryable': current == 'restoring'}, status_code=503, headers=headers)


def install(app):
    """Wrap the composed lifespan so plugin startup/shutdown retain their ordering."""
    original = app.router.lifespan_context

    @asynccontextmanager
    async def responsive_lifespan(instance):
        recovery = RecoveryState()
        instance.state.startup_recovery = recovery
        stop = asyncio.Event()

        async def restore():
            entered = False
            try:
                async with original(instance):
                    entered = True
                    recovery.phase = 'ready'
                    await stop.wait()
            except Exception:
                recovery.phase = 'failed'
                logging.exception('Saved-state startup recovery failed')
                if entered:
                    raise

        worker = asyncio.create_task(restore(), name='saved-state-startup-recovery')
        try:
            yield
        finally:
            stop.set()
            # Do not abandon a thread that may still own a recovery transaction.
            # If startup fails, the original context manager unwinds normally.
            await worker

    app.router.lifespan_context = responsive_lifespan
