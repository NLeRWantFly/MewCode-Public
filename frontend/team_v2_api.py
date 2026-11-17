"""Desktop-only v2 routes. Actor, project and workspace are host-attested.

Only the explicit coordinate endpoint starts a bounded host execution job.
No endpoint accepts an actor, executable, credential or workspace override.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from mewcode.teams.host import DOMAINS, template
from mewcode.teams.store_v2 import TeamStateError


def install_team_v2_api(app, host, *, authorize, static_dir: Path):
    async def guard(request: Request):
        if not authorize(request):
            raise HTTPException(403, 'Desktop authorization required')
        if request.method not in {'GET', 'HEAD'}:
            if request.headers.get('x-team-v2') != '1':
                raise HTTPException(403, 'Explicit team command header required')
            origin = request.headers.get('origin')
            if (origin and origin != str(request.base_url).rstrip('/')) or request.headers.get('sec-fetch-site') == 'cross-site':
                raise HTTPException(403, 'Cross-origin team commands are forbidden')

    router = APIRouter(prefix='/api/team-v2', dependencies=[Depends(guard)])

    async def body(request):
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 28 * 1024 * 1024:
                raise HTTPException(413, 'Team command is too large')
            chunks.append(chunk)
        try:
            value = json.loads(b''.join(chunks))
            if not isinstance(value, dict):
                raise ValueError('object required')
            return value
        except (ValueError, UnicodeError):
            raise HTTPException(422, 'A JSON object is required') from None

    def call(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except TeamStateError as exc:
            raise HTTPException(409, dict(code=exc.code, message=str(exc))) from None
        except (ValueError, TypeError, KeyError) as exc:
            raise HTTPException(422, str(exc)[:2000]) from None

    @router.get('/status')
    def status():
        return call(host.status)

    @router.post('/enable')
    async def enable(request: Request):
        values = await body(request)
        if values != {'enabled': True}:
            raise HTTPException(422, 'Only explicit enable is supported; close/cancel existing runs before disabling the deployment')
        return call(host.enable)

    @router.get('/templates')
    def templates():
        return call(host.templates)

    @router.get('/models')
    def models():
        return call(lambda: host.responsibility.pool.available(host.actor))

    @router.get('/runs')
    def runs():
        return call(host.list_runs)

    @router.post('/validate')
    async def validate(request: Request):
        values = await body(request)
        return call(lambda: host.service.validate_spec(**values).model_dump(mode='json'))

    @router.post('/drafts')
    async def drafts(request: Request):
        return call(host.draft, **await body(request))

    @router.post('/goal-drafts')
    async def goal_drafts(request: Request):
        return call(host.draft_goal, **await body(request))

    @router.post('/compose')
    async def compose(request: Request):
        return call(host.compose, **await body(request))

    @router.get('/runs/{run_id}')
    def snapshot(run_id: str):
        return call(host.snapshot, run_id)

    @router.get('/runs/{run_id}/activation-plan')
    def activation_plan(run_id: str):
        return call(host.activation_plan, run_id)

    @router.post('/runs/{run_id}/commands')
    async def command(run_id: str, request: Request):
        values = await body(request)
        return call(host.command, run_id, **values)

    @router.post('/runs/{run_id}/coordinate', status_code=202)
    async def coordinate(run_id: str, request: Request):
        return call(host.responsibility.start, run_id, **await body(request))

    @router.post('/runs/{run_id}/coordinate/stop')
    async def stop_coordinate(run_id: str, request: Request):
        try:
            return await host.responsibility.stop(run_id, **await body(request))
        except TeamStateError as exc:
            raise HTTPException(409, dict(code=exc.code, message=str(exc))) from None
        except (ValueError, TypeError, KeyError) as exc:
            raise HTTPException(422, str(exc)[:2000]) from None

    @router.post('/runs/{run_id}/interpret')
    async def interpret(run_id: str, request: Request):
        from mewcode.teams.intervention_interpreter import InterventionInterpreter
        try:
            return await InterventionInterpreter(host.responsibility.coordinator).interpret(run_id, **await body(request))
        except TeamStateError as exc:
            raise HTTPException(409, dict(code=exc.code, message=str(exc))) from None
        except (ValueError, TypeError, KeyError) as exc:
            raise HTTPException(422, str(exc)[:2000]) from None

    @router.get('/runs/{run_id}/events')
    def events(run_id: str, after: int = 0, limit: int = 100):
        return call(host.responsibility.views.events, host.actor, run_id, after=after, limit=limit)

    @router.get('/tasks/{task_id}/history')
    def history(task_id: str, before_revision: int | None = None, limit: int = 10):
        return call(host.responsibility.views.task_history, host.actor, task_id, before_revision=before_revision, limit=limit)

    @router.get('/tasks/{task_id}/impact')
    def impact(task_id: str):
        return call(lambda: host.runtime.impact(host.actor, task_id))

    @router.get('/artifacts/{version_id}')
    def artifact(version_id: str):
        return call(lambda: host.runtime.artifact(host.actor, version_id))

    @router.post('/runs/{run_id}/publications/preview')
    async def publication_preview(run_id: str, request: Request):
        values = await body(request)
        return call(lambda: host.publisher.preview(host.actor, run_id, **values))

    @router.post('/runs/{run_id}/publications/prepare')
    async def publication_prepare(run_id: str, request: Request):
        values = await body(request)
        return call(lambda: host.publisher.prepare(host.actor, run_id, **values))

    @router.post('/publications/{publication_id}/{action}')
    async def publication_action(publication_id: str, action: str, request: Request):
        values = await body(request)
        if action == 'commit':
            return call(lambda: host.publisher.commit(host.actor, publication_id, **values))
        if action in {'finish', 'rollback'}:
            return call(lambda: host.publisher.reconcile(host.actor, publication_id, action=action, **values))
        raise HTTPException(422, 'Unknown publication action')

    app.include_router(router)
    app.state.team_v2_host = host
    app.state.team_v2_authorize = authorize

    @app.get('/teams')
    def page():
        return RedirectResponse('/?workbench=tasks', status_code=307)
