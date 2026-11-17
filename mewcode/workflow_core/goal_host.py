"""Own scoped goal runs without replacing another live run in the conversation."""
from __future__ import annotations

import asyncio
import copy
from pathlib import Path
import uuid

from .exploration import GoalCore, PROFILE
from .recipes import RecipeStore
from .runtime import LIVE
from .schema import walk
from .store import ensure
from .web import AgentExecutor, envelope, resolve_model

SESSIONS = {}
TASKS = {}
CHANGES = {}


def changed(rid):
    prior = CHANGES.get(rid)
    CHANGES[rid] = asyncio.Event()
    if prior is not None:
        prior.set()


def source_runtime(srv, cid):
    runtime = srv.SESSION.runtime_for(cid) or getattr(srv.SESSION, 'conversation_runs', {}).get(cid)
    if runtime is None:
        if getattr(getattr(srv.SESSION, 'session', None), 'session_id', '') != cid:
            raise ValueError('请在原任务对话中启动或继续目标')
        runtime = srv.SESSION.capture_conversation_runtime()
    return runtime


async def create(srv, parent, graph, inputs, *, bindings=None, runtime=None, run_id=None):
    if parent is None:
        from mewcode.orchestration.chat_routing import dialogue_focus
        if runtime is None:
            raise ValueError('缺少当前普通对话')
        focus = dialogue_focus(runtime)
        if focus.get('kind') != 'chat' or getattr(runtime, 'cancel_requested', False):
            raise ValueError('当前对话未处于可执行状态')
        cid = runtime.conversation_id
        scope = dict(schema='mewcode.execution-scope.v1', conversation_id=cid,
                     task_id=focus.get('task_id'), workspace=str(Path(runtime.work_dir).resolve()))
    else:
        cid = parent['conversation_id']
        runtime = runtime or source_runtime(srv, cid)
        scope = parent.get('host_execution_scope') or {}
    rid = run_id or uuid.uuid4().hex
    if (scope.get('conversation_id') != cid or not scope.get('task_id') or
            getattr(runtime.agent.permission_checker, 'task_id', '') != scope['task_id']):
        raise ValueError('缺少当前任务真实的执行身份，未创建试跑')
    root = Path(runtime.work_dir).resolve()
    for _, node in walk(graph):
        workspace = Path(node.get('config', {}).get('workspace') or root).resolve()
        if not workspace.is_relative_to(root):
            raise ValueError('试跑输出目录需要位于当前任务工作目录内')
        srv.MODEL_REGISTRY.resolve(resolve_model(node, {}))
    profile = {'executor': copy.deepcopy(graph['nodes'][0]['model'])}
    flow = envelope(graph, {'meta': {'task_design': {'task_id': scope['task_id']}}})
    srv.EXPERT_RUNS.create(run_id=rid, conversation_id=cid, task=graph['goal'], attachments=[],
        profile=profile, strategy='stable', flow=flow)
    backend = ensure(srv.EXPERT_RUNS, rid, graph)
    backend.change(lambda state: state.update(status='running', stop_requested=False,
        inputs=copy.deepcopy(inputs), bindings=copy.deepcopy(bindings or {})))
    parent_id = parent['run_id'] if parent else ''
    srv.EXPERT_RUNS.mutate(rid, lambda record: record.update(parent_run_id=parent_id, chat_origin=parent is None,
        host_execution_scope=dict(scope, run_id=rid, parent_run_id=parent_id)), durable=True, return_snapshot=False)
    await launch(srv, srv.EXPERT_RUNS.load(rid), runtime=runtime)
    return rid


async def launch(srv, record, *, runtime=None):
    from .trace import executable
    executable(record['workflow_core'])
    rid, cid = record['run_id'], record['conversation_id']
    if rid in LIVE:
        LIVE[rid].wake.set()
        return True
    if rid in TASKS and not TASKS[rid].done():
        return True
    if not record.get('parent_run_id') and not record.get('chat_origin'):
        raise ValueError('该运行没有当前任务来源绑定')
    base = runtime or source_runtime(srv, cid)
    scope = record.get('host_execution_scope') or {}
    if (scope.get('run_id') != rid or scope.get('conversation_id') != cid or
            scope.get('task_id') != getattr(base.agent.permission_checker, 'task_id', '') or
            Path(scope.get('workspace') or '').resolve() != Path(base.work_dir).resolve()):
        raise ValueError('保存的目标与当前任务身份不一致，未派发')
    session = copy.copy(base)
    session.expert_run_id = rid
    session.permission_future, session.permission_request = None, {}
    session.ask_user_request = session.human_oncall_request = None
    session.cancel_requested = False
    SESSIONS[rid] = session
    owned = getattr(base, '_goal_run_tasks', None)
    if owned is None:
        owned = base._goal_run_tasks = {}

    async def emit(event):
        changed(rid)
        await srv.WS_EVENTS.send_json(dict(event, run_id=rid, conversation_id=cid))

    async def execute():
        backend = ensure(srv.EXPERT_RUNS, rid)
        try:
            saved = backend.read()
            if saved['status'] != 'running' or saved.get('stop_requested'):
                return
            graph = saved['graph']
            from mewcode.runtime_scheduler import RuntimeScheduler
            lanes = int(graph.get('concurrency', 2))
            session.goal_runtime_scheduler = RuntimeScheduler(limit=lanes + 4,
                kind_limits=dict(model=lanes, tool=4, usage=1))
            saved = backend.read()
            if saved['status'] != 'running' or saved.get('stop_requested'):
                return
            executor = AgentExecutor(session, srv.MODEL_REGISTRY, srv.EXPERT_RUNS, rid,
                                     copy.deepcopy(record['locked_profile']), emit)
            core = GoalCore(backend, executor, concurrency=lanes, run_id=rid, emit=emit,
                recipe_store=RecipeStore(base.work_dir) if graph.get('execution_profile') == PROFILE else None)
            while True:
                await core.run()
                # A chat can append work while the previous batch is finalizing.
                # Re-enter only if that explicit update reopened this same run.
                if backend.query(lambda state: state['status'] != 'running' or state.get('stop_requested')):
                    break
        except asyncio.CancelledError:
            def paused(state):
                state.update(status='terminated' if state['status'] == 'terminated' else 'paused', stop_requested=True)
            backend.change(paused)
            raise
        except Exception as exc:
            reason = str(exc)
            backend.change(lambda state: state.update(status='blocked', runtime_fault=dict(reason=reason, node_ids=[])))
        finally:
            changed(rid)
    task = asyncio.create_task(execute(), name='goal:' + rid)
    session.task = task
    TASKS[rid] = owned[rid] = task
    task.add_done_callback(lambda done: finish(base, rid, done))
    return True


def finish(base, rid, task):
    """Release ownership even when cancelled before execute() starts."""
    owned = getattr(base, '_goal_run_tasks', {})
    if owned.get(rid) is task:
        owned.pop(rid, None)
        changed(rid)
        base.owner._continue_conversation_queue(base)


async def refresh_concurrency(store, rid):
    """Apply saved limits without cancelling work already in flight."""
    lanes = ensure(store, rid).query(lambda state: int(state['graph'].get('concurrency', 2)))
    core = LIVE.get(rid)
    if core is not None:
        core.concurrency = lanes
    scheduler = getattr(SESSIONS.get(rid), 'goal_runtime_scheduler', None)
    if scheduler is not None:
        await scheduler.configure(limit=lanes + 4, kind_limits=dict(model=lanes, tool=4, usage=1))
    if core is not None:
        core.wake.set()


def interrupt(store, conversation_id, action='pause'):
    interrupted = 0
    for rid, session in tuple(SESSIONS.items()):
        task = TASKS.get(rid)
        if session.conversation_id != conversation_id or task is None or task.done():
            continue
        interrupted += 1
        if rid in LIVE:
            LIVE[rid].control(dict(operation='stop' if action == 'terminate' else 'pause', request_id=uuid.uuid4().hex))
        else:
            ensure(store, rid).change(lambda state: state.update(
                status='terminated' if action == 'terminate' else 'paused', stop_requested=True))
            if not task.cancelling():
                task.cancel()
    return interrupted
