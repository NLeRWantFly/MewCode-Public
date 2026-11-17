"""Isolated browser fixture; real production UI/API and synthetic models only."""
import argparse
import asyncio
import copy
import socket
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
import uvicorn

from frontend.team_v2_api import install_team_v2_api
from mewcode.teams.disputes import DisputeService
from mewcode.teams.dynamic_plans import PlanService
from mewcode.teams.interventions import InterventionService
from mewcode.teams.responsibility import MissionService
from mewcode.teams.runtime_protocol import byte_hash
from team_responsibility_fixture import FixtureAdapter, make_host, make_run


def fixture_app(root):
    root=Path(root).resolve();repository=Path(__file__).resolve().parents[1]
    if not root.is_relative_to(repository) or not root.name.startswith('.tmp-team-v2-render'):
        raise ValueError('isolated .tmp-team-v2-render workspace required')
    root.mkdir(exist_ok=False)
    host,_=make_host(root,adapter=FixtureAdapter(use_tools=True));run,raw=make_run(host,third=True)
    info=InterventionService(host.runtime).submit(host.actor,run,request=dict(intent='information',message='补充资料：以 fixture-document 的 v7 为核对依据。',expected_goal_version=1,expected_plan_revision=1),control_epoch=1,command_id='fixture-info')
    first=asyncio.run(host.responsibility.coordinator.step(run,control_epoch=1,command_id='first-step'))
    assert not first.get('reason'),first
    asyncio.run(host.responsibility.pool.run_ready(host.actor,run))
    view=host.snapshot(run);current=next(t for t in view['tasks'] if t['task_key']=='deliver')
    disputes=DisputeService(host.runtime)
    d=disputes.open(host.actor,run,submission=dict(task_id=current['task_id'],revision=1,criterion_id='correct',owner_role='producer',issue_key='source-format',problem='来源字段需要附带可读版本，不能只保留正文。',reproduction='查看当前候选的来源字段和独立检查结果。'),command_id='dispute')
    e=disputes.add_evidence(host.actor,d['dispute_id'],evidence=dict(kind='test',source_ref='fixture-check:source-format',source_hash=byte_hash(b'fixture observed missing source field'),detail='测试夹具观察：此条记录用于检验争议界面与稳定归属。',verification='本地夹具检查记录，不代表真实模型缺陷。',task_revision=1),expected_version=d['version'],command_id='evidence')
    disputes.verify_evidence(host.actor,d['dispute_id'],evidence_id=e['evidence_id'],expected_version=disputes.get(host.actor,d['dispute_id'])['version'],control_epoch=1,command_id='verify-evidence')
    replacement=copy.deepcopy(raw);replacement['version']=2;replacement['tasks'][-1]['task_key']='publish_package'
    replacement['tasks'][-1]['objective']='保留已核验上游，替换末端交付步骤并检查来源版本。'
    for edge in replacement['connections']:
        for side in ('source','target'):
            if edge[side].get('task_key')=='archive':edge[side]['task_key']='publish_package'
    assignments=MissionService(host.runtime).view(host.actor,run)['assignments']
    assignments=[dict(a,task_key='publish_package') if a['task_key']=='archive' else a for a in assignments]
    PlanService(host.service).propose(host.actor,run,definition=replacement,assignments=assignments,reason='替换末端分支，复用已核验的上游；先展示影响再由用户确认。',expected_goal_version=1,expected_plan_revision=1,control_epoch=1,command_id='proposal')
    app=FastAPI();static=repository/'frontend'/'static'
    install_team_v2_api(app,host,authorize=lambda request:request.client.host in {'127.0.0.1','::1'},static_dir=static)
    app.mount('/static',StaticFiles(directory=static),name='static')
    @app.post('/__test__/prepare-root-review')
    async def prepare_root_review(request: Request):
        if request.headers.get('x-team-v2')!='1':raise HTTPException(403)
        return await host.responsibility.coordinator.run_until_idle(run,control_epoch=1,max_steps=4)
    return app,host


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',required=True);args=parser.parse_args()
    app,host=fixture_app(args.root);sock=socket.socket();sock.bind(('127.0.0.1',0))
    print('TEAM_RESPONSIBILITY_RENDER_PORT='+str(sock.getsockname()[1]),flush=True)
    try:uvicorn.Server(uvicorn.Config(app,log_level='warning')).run(sockets=[sock])
    finally:host.close()
