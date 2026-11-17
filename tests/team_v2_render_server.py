"""Isolated real HTTP fixture for the Electron v2 interaction test.

Only the model response is synthetic. API, SQLite state, leases, review,
receipts, budgets and production HTML/JS/CSS are real. No user store is opened.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
import uvicorn

from frontend.team_v2_api import install_team_v2_api
from mewcode.teams.host import TeamHost, template
from mewcode.teams.runtime_binding import GrokTeamExecutor
from mewcode.teams.runtime_protocol import ArtifactContent
from mewcode.teams.store_v2 import TeamActor
from test_team_binding_v2 import TeamInvoker


def fixture_app(root):
    root = Path(root).resolve()
    repository = Path(__file__).resolve().parents[1]
    if not root.is_relative_to(repository) or not root.name.startswith('.tmp-team-v2-render'):
        raise ValueError('renderer fixture must use a new .tmp-team-v2-render* workspace')
    root.mkdir(exist_ok=False)
    host = TeamHost(root)
    host.enable()
    raw = template('data')
    keys = ('extract', 'normalize', 'quality', 'deliver')
    goals = ('核对输入字段与数据来源，生成结构化清单。', '统一批次格式，并标记缺失值和异常记录。', '汇总质量核验依据，保留可追溯的输入版本。', '生成可复用交付包与简明质量说明。')
    original = raw['tasks'][0]
    raw['tasks'] = []
    for key, goal in zip(keys, goals):
        task = copy.deepcopy(original)
        task.update(task_key=key, objective=goal, token_budget=2000)
        raw['tasks'].append(task)
    raw['connections'] = [dict(source=dict(port_id='request'), target=dict(task_key=keys[0], port_id='request'))]
    raw['connections'] += [dict(source=dict(task_key=left, port_id='result'), target=dict(task_key=right, port_id='request')) for left, right in zip(keys, keys[1:])]
    raw['connections'].append(dict(source=dict(task_key=keys[-1], port_id='result'), target=dict(port_id='delivery')))
    run = host.draft(definition=raw, name='数据质量与交付团队', objective='清理供应的样例批次，核对质量证据，再交付可复用的结构化产物。所有步骤只使用固定输入，保留独立验收记录。', budget_ref='render-budget', command_id='render-draft')['run']['team_run_id']
    seed = host.runtime.register_input(host.actor, run, ArtifactContent.from_bytes(port_id='request', schema_ref='data-artifact/v1', media_type='text/csv', content=b'id,value\n1,10\n2,20\n'), control_epoch=1, command_id='seed')
    plan = host.activation_plan(run)
    active = host.runtime.activate(host.actor, run, bindings=plan['bindings'], inputs={'request': seed['version_id']}, budget_limit=10000, expected_version=1, control_epoch=1, command_id='activate')
    executor = GrokTeamExecutor(host.runtime, TeamInvoker(), root=root, session_id='renderer-synthetic-grok')
    first = asyncio.run(executor.run_once())[0]
    host.runtime.review(host.actor, first['handoff_id'], accepted=True,
                        scores=[dict(criterion_id=c['criterion_id'], score=10000, evidence='渲染夹具人工核对：五个预期字节；非真实模型试验') for c in raw['tasks'][0]['acceptance']],
                        expected_revision=1, control_epoch=1, command_id='review')
    host.runtime.accept_input(host.actor, active['task_ids']['normalize'], 'request', first['artifacts'][0]['version_id'], revision=1, accepted=True,
                              reason='字段契约一致，明确接收这个固定版本。', control_epoch=1, command_id='receipt')
    asyncio.run(executor.run_once())
    app = FastAPI()
    static = repository / 'frontend' / 'static'
    install_team_v2_api(app, host, authorize=lambda request: request.client.host in {'127.0.0.1', '::1'}, static_dir=static)
    app.mount('/static', StaticFiles(directory=static), name='static')

    @app.post('/__test__/tick')
    async def tick(request: Request):
        if request.headers.get('x-team-v2') != '1':
            raise HTTPException(403)
        return await executor.run_once()

    return app, host


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    app, host = fixture_app(args.root)
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    print('TEAM_V2_RENDER_PORT=' + str(sock.getsockname()[1]), flush=True)
    try:
        uvicorn.Server(uvicorn.Config(app, log_level='warning')).run(sockets=[sock])
    finally:
        host.close()
