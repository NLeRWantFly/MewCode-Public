import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from mewcode.expert_runs import ExpertRunStore
from mewcode.runtime_scheduler import RuntimeScheduler
from mewcode.workflow_core import goal_host
from mewcode.workflow_core.exploration import build_goal
from mewcode.workflow_core.store import ensure
from mewcode.workflow_core.state import command
from mewcode.workflow_core.web import envelope
from tests.workflow_core.test_exploration import MODEL


@pytest.mark.asyncio
async def test_scoped_goal_preserves_parent_and_pause_before_dispatch_is_respected(tmp_path, monkeypatch):
    store = ExpertRunStore(tmp_path/'runs',storage='sqlite')
    flow = build_goal(label='行测章节',goal='处理数学材料',model=MODEL,workspace=tmp_path/'trial')
    parent_id = '2'*32
    store.create(run_id=parent_id,conversation_id='chat',task='原任务',attachments=[],profile={'executor':MODEL},
        strategy='stable',flow=envelope(flow))
    ensure(store,parent_id,flow)
    store.mutate(parent_id,lambda record:record.update(host_execution_scope=dict(
        schema='mewcode.execution-scope.v1',conversation_id='chat',run_id=parent_id,task_id='task',workspace=str(tmp_path))))
    parent = store.load(parent_id)
    before = copy.deepcopy(parent['workflow_core'])
    parent_scheduler = RuntimeScheduler(limit=1,kind_limits=dict(model=1))
    base = SimpleNamespace(work_dir=str(tmp_path),conversation_id='chat',
        agent=SimpleNamespace(permission_checker=SimpleNamespace(task_id='task'),runtime_scheduler=parent_scheduler),
        task=asyncio.get_running_loop().create_future(),owner=SimpleNamespace(_continue_conversation_queue=Mock()),
        expert_run_id=parent_id)
    host = SimpleNamespace(EXPERT_RUNS=store,MODEL_REGISTRY=SimpleNamespace(resolve=Mock()),
        SESSION=SimpleNamespace(runtime_for=lambda cid:base),WS_EVENTS=SimpleNamespace(send_json=AsyncMock()))
    invoked=[]
    async def execute(ctx):
        child_scheduler = factory.call_args.args[0].goal_runtime_scheduler
        assert child_scheduler is not parent_scheduler
        async with child_scheduler.slot(kind='model'):
            invoked.append(ctx.node_id)
            return dict(text='正常交付')
    factory=Mock(return_value=execute)
    monkeypatch.setattr(goal_host,'AgentExecutor',factory)
    monkeypatch.setattr(goal_host,'SESSIONS',{})
    monkeypatch.setattr(goal_host,'TASKS',{})
    child=await goal_host.create(host,parent,flow,{'source':'数学材料'})
    goal_host.interrupt(store,'chat','pause')
    await asyncio.gather(goal_host.TASKS[child],return_exceptions=True)
    assert not invoked and ensure(store,child).read()['status']=='paused'
    assert not base._goal_run_tasks
    base.owner._continue_conversation_queue.assert_called_once_with(base)
    assert base.expert_run_id==parent_id and base.task.cancelled() is False
    assert store.load(parent_id)['workflow_core']==before
    backend=ensure(store,child)
    backend.change(lambda state:command(state,dict(operation='resume',request_id='resume-child')))
    async with parent_scheduler.slot(kind='model',label='another task owns its model slot'):
        await goal_host.launch(host,store.load(child))
        await asyncio.wait_for(goal_host.TASKS[child],20)
    assert backend.read()['status']=='completed' and invoked==['goal']
    assert store.load(parent_id)['workflow_core']==before
    assert parent_scheduler.limit == 1 and parent_scheduler.kind_limits == dict(model=1)
