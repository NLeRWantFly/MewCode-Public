"""Ordinary chat owns a graph, not a second model posing as the chat owner."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from mewcode.orchestration.chat_routing import dialogue_focus
from mewcode.permissions import PermissionMode
from mewcode.tools.base import Tool, ToolResult
from . import goal_host
from .exploration import TASK_GRANULARITY, WorkflowTask, tasks, tasks_ready, update_items
from .recipes import RecipeStore, crystallize, instantiate
from .presentation import PresentationBlock, publish
from .schema import digest, normalize, walk
from .state import command, fresh
from .store import RunBackend, ensure
from .web import resolve_model
from . import trace

PROFILE = 'goal.chat.v1'
GUIDANCE = '''先完成用户任务，流程图只是执行过程的记录和后续复用方式，不是交付前置条件。
直接使用当前工具推进工作；不为构图另开规划、审图、确认执行或修订审批回合。
普通对话是主体，不必先制定完整编排或切换模式。
UpdateWorkflowTasks 只记录探索图，不启动管线。图不能限制主对话进度；节点问题交给主对话处理。
图记录公开计划、行动和结果，不记录隐藏推理。顺序用依赖，分支用 branch/cases 与条件出口，循环用 loop 的嵌套 graph 和退出条件；并发工作保留各自依赖、真实子 Agent ID 和汇合。
首次探索中的修改通过对话更新图，禁止为了按图执行而切换模式。需要子 Agent 时用 Agent/BackgroundTasks，或显式 DispatchWorkflowTasks；派发回执与图记录分开。
后台默认最多 2 路并发，优先合并相关工作；只有用户明确要求提高并发时才调高，不通过另建运行绕过限制。
仅预览图时 intent=propose，保存记录时 intent=execute（兼容旧参数名，但不执行节点）。简单问答直接处理。
工作项 task 写清输入范围、禁止修改、交付物和必要验证，inputs 携带相关澄清；节点不会自动继承全部聊天记录。
主对话继续处理其他事情，暂无独立工作时 WaitForWorkflowTasks 等结果，不轮询、不重复派发、不把后台运行当成已完成。
任务实际完成时用 completed=true 和 summary 记录公开交付摘要，不要求历史尝试全部成功；阶段回复不等于完成。
只有用户明确要求提取，且任务已完成，才用 SaveWorkflow；只有用户明确要求复用，才用 RunSavedWorkflow 绑定新输入执行。提取本身不执行。
画布展示、输出块或流程保存失败时，继续可独立完成的任务；如实说明展示问题，不重跑已交付工作。
复用时指定独立输出目录，不因已有同名文件就宣称本轮执行过。节点可使用 PublishWorkflowBlock 表达输入输出。
模型、Prompt、constraints 和 Fast 可按节点配置；未配置模型时使用当前 Provider 的 gpt-6.1-sol / max。
暂停后仅在用户要求继续时用 ControlWorkflow 恢复。旧运行不属于本轮任务时不要启动它。
''' + TASK_GRANULARITY


class DispatchParams(BaseModel):
    request_id: str = Field(min_length=1, max_length=100)
    intent: Literal['execute', 'propose']
    items: list[WorkflowTask] = Field(min_length=1, max_length=100)
    run_id: str = ''
    label: str = Field(default='', max_length=200)
    model: dict | None = None
    service_tier: Literal['fast', 'default', 'priority'] = 'fast'
    workspace: str = ''
    inputs: dict[str, str] = Field(default_factory=dict,
        description='Named input/output path parameters for later reuse; set when first creating this graph')
    concurrency: int = Field(default=2, ge=1, le=32,
        description='默认 2 路；已有工作流仅在显式传入时调整，不取消在途工作。提高并发需用户明确要求。')


class UpdateParams(DispatchParams):
    items: list[trace.TraceTask] = Field(default_factory=list, max_length=100)
    scope: str = Field(default='', description='Enclosing loop path; empty means root')
    edges: list[trace.TraceEdge] | None = Field(default=None, max_length=500,
        description='If supplied, replace edges in this scope, including branch ports. Otherwise depends_on updates incoming edges.')
    observations: list[trace.Observation] = Field(default_factory=list, max_length=100)
    completed: bool | None = Field(default=None, description='True only when the user task is actually delivered; old failures do not block completion')
    summary: str = Field(default='', max_length=16000, description='Public task requirements, decisions and delivery summary for later extraction')


class ReadParams(BaseModel):
    run_id: str = ''
    node_ids: list[str] = Field(default_factory=list)


class SaveParams(BaseModel):
    run_id: str = ''


class CatalogParams(BaseModel):
    recipe_id: str = ''
    version: int | None = Field(default=None, ge=1)


class NodeOverride(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model: dict | None = None
    task: str | None = None
    prompt: str | None = None
    constraints: list[str] | None = None
    service_tier: Literal['fast', 'default', 'priority'] | None = None


class RunParams(BaseModel):
    request_id: str = Field(min_length=1, max_length=100)
    recipe_id: str
    version: int | None = Field(default=None, ge=1)
    inputs: dict[str, str]
    overrides: dict[str, NodeOverride] = Field(default_factory=dict,
        description='Changes are saved as a new version before execution; old runs and versions remain intact')


class ControlParams(BaseModel):
    run_id: str = ''
    request_id: str = Field(min_length=1, max_length=100)
    operation: Literal['pause', 'resume', 'stop']


class BlockParams(PresentationBlock):
    run_id: str = ''
    node_id: str = Field(description='Existing work item whose input/output this block explains')


class ChatWorkflow:
    def __init__(self, srv, runtime):
        self.srv, self.runtime = srv, runtime
        self.cid = runtime.conversation_id
        self.task_id = dialogue_focus(runtime).get('task_id')

    def restore(self, conversation):
        """Rehydrate host facts once at a compact/reload boundary, without dispatch."""
        focus = dialogue_focus(self.runtime)
        # Reading the task snapshot does not need an execution permission binding.
        # The tool methods still guard every execution/read against that binding.
        if (focus.get('kind') != 'chat' or not self.task_id
                or focus.get('task_id') != self.task_id or self.runtime.conversation_id != self.cid):
            return
        self.sync(conversation)
        current = self.identity()
        others = list(dict.fromkeys(rid for rid in focus.get('workflow_run_ids', []) if rid != current))
        run_ids = [current, *others[-7:]]
        runs = []
        for rid in run_ids:
            record = self.srv.EXPERT_RUNS.read_execution_fields(rid, 'conversation_id',
                'host_execution_scope', 'chat_origin')
            if not record:
                continue
            scope = record.get('host_execution_scope') or {}
            if (not record.get('chat_origin') or record.get('conversation_id') != self.cid
                    or scope.get('task_id') != self.task_id
                    or Path(scope.get('workspace') or '').resolve() != Path(self.runtime.work_dir).resolve()):
                continue
            def project(state):
                counts, rows = {}, []
                for nid, node in state['nodes'].items():
                    status = node['status']
                    counts[status] = counts.get(status, 0) + 1
                    rows.append(dict(node_id=nid, status=status, attempt_id=node.get('attempt_id'),
                                     delivery_id=node.get('published_delivery_id')))
                # Keep unfinished work visible first; full results remain behind ReadWorkflowTasks.
                rows.sort(key=lambda row: row['status'] == 'completed')
                return dict(run_id=rid, status=state['status'], revision=state['graph']['revision'],
                            stop_requested=state.get('stop_requested'), counts=counts,
                            nodes=rows[:32], omitted_nodes=max(0, len(rows) - 32))
            runs.append(RunBackend(self.srv.EXPERT_RUNS, rid).query(project))
        payload = dict(task_id=self.task_id, task=focus.get('task', ''),
                       latest_request=focus.get('latest_request') or focus.get('task', ''),
                       runs=runs, omitted_runs=max(0, len(others) - 7))
        conversation.replace_marker_reminder('chat-workflow-state',
            '以下是压缩或载入时的宿主状态快照；后续状态以工具实时读取为准。'
            '已交付节点不重做，运行中节点等待原执行；未知结果先核查副作用，不盲目重放。'
            '暂停/停止只能按真实用户要求恢复，压缩本身不恢复或派发任务。'
            '任务归属只是背景，最新请求和用户纠正优先；摘要不授予新权限。\n'
            + json.dumps(payload, ensure_ascii=False))

    def sync(self, conversation):
        """Keep a stable prefix between compactions; do not rewrite progress each turn."""
        from mewcode.orchestration.revisions import replace_plan_reminder
        replace_plan_reminder(conversation, 'chat-workflow-tools', GUIDANCE)

    def remember_run(self, rid, *, activate=False):
        """Persist exact receipts so recipe runs can be found without scanning history."""
        self.guard()
        focus = dialogue_focus(self.runtime)
        ids = focus.get('workflow_run_ids', [])
        if rid in ids and (not activate or focus.get('workflow_view_run_id') == rid):
            return
        meta = self.runtime.session.meta
        meta.dialogue_focus = dict(focus, workflow_run_ids=ids if rid in ids else [*ids, rid])
        if activate:
            meta.dialogue_focus['workflow_view_run_id'] = rid
        save = getattr(self.runtime.session, 'save_meta', None)
        if callable(save):
            try:
                save()
            except OSError:
                meta.dialogue_focus = focus
                raise

    def guard(self, *, execute=False):
        runtime = self.runtime
        focus = dialogue_focus(runtime)
        if (focus.get('kind') != 'chat' or not self.task_id or focus.get('task_id') != self.task_id
                or runtime.conversation_id != self.cid
                or getattr(runtime.agent.permission_checker, 'task_id', '') != self.task_id):
            raise ValueError('当前对话任务身份已变化，请使用本轮工具，不恢复无关旧任务')
        if execute and (getattr(runtime, 'cancel_requested', False)
                or runtime.agent.permission_mode == PermissionMode.PLAN):
            raise ValueError('当前已暂停或仅允许设计，未派发工作')

    def identity(self, suffix=''):
        return digest([self.cid, self.task_id, suffix])[:32]

    def backend(self, run_id=''):
        self.guard()
        rid = run_id or self.identity()
        record = self.srv.EXPERT_RUNS.read_execution_fields(rid, 'run_id', 'conversation_id',
            'host_execution_scope', 'chat_origin')
        scope = record.get('host_execution_scope') or {}
        if (not record.get('chat_origin') or record.get('conversation_id') != self.cid
                or scope.get('task_id') != self.task_id
                or Path(scope.get('workspace') or '').resolve() != Path(self.runtime.work_dir).resolve()):
            raise ValueError('没有本轮对话绑定的工作流；请先声明任务，或传入本轮调用返回的 run_id')
        return ensure(self.srv.EXPERT_RUNS, rid)

    def display_backend(self, run_id=''):
        if not run_id:
            run_id = dialogue_focus(self.runtime).get('workflow_view_run_id', '')
        if not run_id and self.srv.EXPERT_RUNS.read_execution_fields(self.identity('trace'), 'run_id'):
            run_id = self.identity('trace')
        return self.backend(run_id)

    def validate_graph(self, graph):
        root = Path(self.runtime.work_dir).resolve()
        for _, node in walk(graph):
            model = node.get('model') or {}
            if not all(model.get(key) for key in ('provider_key', 'model_id', 'effort')):
                raise ValueError('节点需要明确 Provider、模型和推理档位')
            self.srv.MODEL_REGISTRY.resolve(resolve_model(node, {}))
            if not Path(node.get('config', {}).get('workspace') or root).resolve().is_relative_to(root):
                raise ValueError('节点输出工作目录必须位于当前项目内')

    async def update(self, params):
        self.guard()
        rid = params.run_id or self.identity('trace')
        def capture(node_id, artifact):
            from .files import capture as snapshot
            root = Path(self.runtime.work_dir).resolve()
            path = (root / artifact['path']).resolve()
            if not path.is_relative_to(root):
                raise ValueError('探索产物需位于当前项目内；外部材料请保留来源引用')
            return snapshot(self.srv.EXPERT_RUNS.root, rid, 'trace-' + digest(node_id)[:16], path,
                sandbox=getattr(self.runtime.agent, 'sandbox', None))
        locks = getattr(self.runtime, '_chat_workflow_locks', None)
        if locks is None:
            locks = self.runtime._chat_workflow_locks = {}
        async with locks.setdefault(rid, asyncio.Lock()):
            self.guard()
            existing = self.srv.EXPERT_RUNS.read_execution_fields(rid, 'run_id')
            backend = self.backend(rid) if existing else None
            if params.run_id and backend is None:
                raise ValueError('指定探索记录不存在')
            if backend:
                state = backend.read()
                if not trace.is_trace(state):
                    raise ValueError('这是独立执行的运行；请在当前探索图记录其回执，不改写执行状态')
                template = state['graph']['chat_defaults']
            else:
                template = dict(model=params.model or dict(provider_key=getattr(self.runtime.slot, 'key', ''),
                    model_id='gpt-6.1-sol', effort='max'), config=dict(workspace=str(Path(self.runtime.work_dir).resolve()),
                    goal_execution=True, presentation=True, service_tier=params.service_tier))
                state = fresh(dict(label=params.label or '探索过程', goal=dialogue_focus(self.runtime).get('task', ''),
                    execution_profile=trace.PROFILE, chat_defaults=template, concurrency=params.concurrency,
                    run_parameters=list(params.inputs), nodes=[], edges=[]))
                state.update(status='running', inputs=copy.deepcopy(params.inputs))
            if params.intent == 'propose':
                result = trace.update(state, params, template)
                return dict(result, workflow=state['graph'])
            if backend is None:
                result = trace.update(state, params, template, capture=capture)
                from .web import envelope
                self.srv.EXPERT_RUNS.create(run_id=rid, conversation_id=self.cid, task=state['graph']['goal'],
                    attachments=[], profile={'executor': template['model']}, strategy='stable',
                    flow=envelope(state['graph'], {'meta': {'task_design': {'task_id': self.task_id}}}))
                from .store import synchronize
                def persist(record):
                    record.update(workflow_core=state, chat_origin=True, host_execution_scope=dict(
                        schema='mewcode.execution-scope.v1', conversation_id=self.cid, task_id=self.task_id,
                        workspace=str(Path(self.runtime.work_dir).resolve()), run_id=rid, parent_run_id=''))
                    synchronize(record)
                self.srv.EXPERT_RUNS.mutate(rid, persist, durable=True, return_snapshot=False)
            else:
                result = backend.change(lambda value: trace.update(value, params, template, capture=capture))
            self.remember_run(rid, activate=True)
            goal_host.changed(rid)
            return dict(result, run_id=rid)

    async def dispatch(self, params):
        self.guard(execute=params.intent == 'execute')
        rid = params.run_id or self.identity()
        # A per-chat lock also serializes tools issued in the same parallel tool turn.
        locks = getattr(self.runtime, '_chat_workflow_locks', None)
        if locks is None:
            locks = self.runtime._chat_workflow_locks = {}
        async with locks.setdefault(rid, asyncio.Lock()):
            self.guard(execute=params.intent == 'execute')
            existing = self.srv.EXPERT_RUNS.read_execution_fields(rid, 'run_id')
            backend = self.backend(rid) if existing else None
            if backend is None and params.run_id:
                raise ValueError('指定运行不存在')
            if backend is None:
                model = params.model or dict(provider_key=getattr(self.runtime.slot, 'key', ''),
                    model_id='gpt-6.1-sol', effort='max')
                root = Path(self.runtime.work_dir)
                workspace = str((root / params.workspace).resolve()) if params.workspace else str(root.resolve())
                template = dict(model=model, config=dict(workspace=workspace, goal_execution=True,
                    presentation=True, service_tier=params.service_tier))
                graph = normalize(dict(label=params.label or '对话工作流',
                    goal=dialogue_focus(self.runtime).get('task') or params.label,
                    execution_profile=PROFILE, concurrency=params.concurrency,
                    run_parameters=list(params.inputs), chat_defaults=template, nodes=[], edges=[]))
                state = fresh(graph)
                state.update(status='running', inputs=copy.deepcopy(params.inputs))
            else:
                state = backend.read()
                if state['graph'].get('execution_profile') != PROFILE:
                    raise ValueError('保存流程的运行使用节点编辑；增量工作请在当前对话图声明')
                template = state['graph']['chat_defaults']
            if state.get('stop_requested') or state['status'] in {'paused', 'terminated'}:
                raise ValueError('工作流已暂停或停止，不通过任务增量自动恢复')
            def mutate(value):
                actor = dict(node_id='__chat__', session_id='chat:' + self.task_id)
                receipt_key = actor['session_id'] + ':' + params.request_id
                prior = value.get('task_updates', {}).get(receipt_key)
                concurrency = params.concurrency if 'concurrency' in params.model_fields_set else None
                if prior is not None and prior.get('concurrency') != concurrency:
                    raise ValueError('同一请求编号不能提交不同的并发设置')
                result = update_items(value, actor, template, request_id=params.request_id,
                                      items=[item.model_dump() for item in params.items])
                if prior is None and concurrency is not None and value['graph'].get('concurrency') != concurrency:
                    value['graph']['concurrency'] = concurrency
                    value['graph']['revision'] += 1
                    value['graph_history'].append(copy.deepcopy(value['graph']))
                    result['revision'] = value['graph']['revision']
                if prior is None:
                    value['task_updates'][receipt_key].update(concurrency=concurrency, result=copy.deepcopy(result))
                self.validate_graph(value['graph'])
                if result['added'] or result['revised']:
                    value['status'] = 'running'
                    value.pop('solidified_recipe', None)
                return result
            if params.intent == 'propose':
                result = mutate(state)
                return dict(result, workflow=state['graph'], execution_started=False)
            if backend is None:
                result = mutate(state)
                await goal_host.create(self.srv, None, state['graph'], state['inputs'],
                    bindings=state.get('bindings'), runtime=self.runtime, run_id=rid)
                backend = self.backend(rid)
                backend.change(lambda value: value.update(task_updates=state['task_updates']))
            else:
                result = backend.change(mutate)
                if backend.query(lambda value: value['status']) == 'running':
                    await goal_host.launch(self.srv, self.srv.EXPERT_RUNS.load(rid), runtime=self.runtime)
                await goal_host.refresh_concurrency(self.srv.EXPERT_RUNS, rid)
            goal_host.changed(rid)
            self.remember_run(rid)
            return dict(result, run_id=rid, execution_started=True)

    def read(self, params):
        backend = self.display_backend(params.run_id)
        return backend.query(lambda state: dict(run_id=backend.run_id, status=state['status'],
            tasks=tasks(state, params.node_ids or None), recipe=state.get('solidified_recipe')))

    async def wait(self, params):
        backend = self.display_backend(params.run_id)
        if backend.query(trace.is_trace):
            return dict(self.read(params), message='探索图没有调度器；主对话继续处理任务，子 Agent 使用派发回执等待。')
        # No model request, scheduler permit, deadline or fixed-interval polling while waiting.
        while True:
            self.guard()
            signal = goal_host.CHANGES.setdefault(backend.run_id, asyncio.Event())
            state = backend.query(lambda value: {key: value[key] for key in
                ('graph', 'nodes', 'attempts', 'automatic_continuations', 'status') if key in value})
            ids = params.node_ids or [path for path, _ in walk(state['graph'])]
            tasks(state, ids)  # Validate explicit IDs before waiting.
            if state['status'] != 'running' or (params.node_ids and
                    tasks_ready(state, dict(waiting_tasks=ids))):
                return self.read(params)
            task = goal_host.TASKS.get(backend.run_id)
            if task is None or task.done():
                return dict(self.read(params), needs_resume=True,
                            message='调度器未运行，检查状态后按用户要求恢复；未静默重跑')
            await signal.wait()

    async def save(self, params):
        backend = self.display_backend(params.run_id)
        saved = backend.read()
        store = RecipeStore(self.runtime.work_dir)
        prior = saved.get('solidified_recipe')
        if prior:
            return store.read(prior['id'], prior['version'])
        value = crystallize(saved, source_run_id=backend.run_id)
        value['id'] = saved.get('chat_recipe_id', '')
        recipe = await asyncio.to_thread(store.save, value)
        backend.change(lambda state: state.update(chat_recipe_id=recipe['id'],
            solidified_recipe={key: recipe[key] for key in ('id', 'version', 'name', 'digest')}))
        return recipe

    async def run_recipe(self, params):
        self.guard(execute=True)
        rid = self.identity('recipe:' + params.request_id)
        signature = digest(params.model_dump())
        prior = self.srv.EXPERT_RUNS.read_execution_fields(rid, 'run_id')
        if prior:
            backend = self.backend(rid)
            if backend.query(lambda state: state['graph'].get('chat_recipe_request')) != signature:
                raise ValueError('相同请求编号不能提交不同的流程调用')
            self.remember_run(rid, activate=True)
            return dict(self.read(ReadParams(run_id=rid)), reused_receipt=True)
        store = RecipeStore(self.runtime.work_dir)
        recipe = store.read(params.recipe_id, params.version)
        nodes = dict(walk(recipe['graph']))
        for key, patch in params.overrides.items():
            if key not in nodes:
                raise ValueError('保存流程中不存在节点：' + key)
            for field, value in patch.model_dump(exclude_none=True).items():
                target = nodes[key] if field in {'task', 'model'} else nodes[key]['config']
                target[field] = copy.deepcopy(value)
        graph, bindings = instantiate(recipe, params.inputs)
        self.validate_graph(graph)
        if params.overrides:
            # Synchronous short transaction: no interleaving before the durable run receipt exists.
            recipe = store.save(recipe)
            graph, bindings = instantiate(recipe, params.inputs)
        graph['chat_recipe_request'] = signature
        graph['label'] = recipe['name'] + ' · 复用 v' + str(recipe['version'])
        await goal_host.create(self.srv, None, graph, params.inputs, bindings=bindings,
                               runtime=self.runtime, run_id=rid)
        self.remember_run(rid, activate=True)
        return dict(run_id=rid, recipe=graph['recipe'], execution_started=True)

    async def control(self, params):
        self.guard(execute=params.operation == 'resume')
        backend = self.backend(params.run_id)
        backend.query(trace.executable)
        core = goal_host.LIVE.get(backend.run_id)
        body = params.model_dump(exclude={'run_id'})
        result = core.control(body) if core else backend.change(lambda state: command(state, body))
        if params.operation == 'resume':
            await goal_host.launch(self.srv, self.srv.EXPERT_RUNS.load(backend.run_id), runtime=self.runtime)
        goal_host.changed(backend.run_id)
        return dict(result, run_id=backend.run_id)

    def publish(self, params):
        backend = self.display_backend(params.run_id)
        def change(state):
            if trace.is_trace(state):
                return trace.publish(state, params.node_id, params.model_dump(exclude={'run_id', 'node_id'}))
            if state.get('stop_requested'):
                raise ValueError('工作流已暂停或停止，保留原表达')
            attempt_id = state['nodes'].get(params.node_id, {}).get('attempt_id')
            if not attempt_id:
                raise ValueError('节点尚未开始，待其建立执行记录后发布输入输出表达')
            return publish(state, attempt_id, from_chat=True,
                           **params.model_dump(exclude={'run_id', 'node_id'}))
        result = backend.change(change)
        goal_host.changed(backend.run_id)
        return result


class ChatTool(Tool):
    category = 'read'
    is_system_tool = True
    consumes_runtime_slot = False
    inline_schema_refs = True

    def __init__(self, bridge):
        self.bridge = bridge

    async def execute(self, params):
        try:
            value = await self.invoke(params)
            return ToolResult(json.dumps(value, ensure_ascii=False, default=str))
        except (ValueError, KeyError, OSError) as exc:
            return ToolResult(str(exc), is_error=True)


class UpdateWorkflowTasks(ChatTool):
    name = 'UpdateWorkflowTasks'
    description = ('Record the current conversation exploration map without executing nodes. Include sequence, '
        'branch ports, nested loops and parallel dependencies/joins. Observations record actual public results, '
        'selected paths, loop iterations and subagent IDs. Main chat owns progress; no graph state gates it. '
        'Use completed=true only after actual task delivery. propose previews; execute persists only.')
    params_model = UpdateParams

    async def invoke(self, params):
        return await self.bridge.update(params)


class DispatchWorkflowTasks(ChatTool):
    name = 'DispatchWorkflowTasks'
    description = ('Explicitly delegate authorized independent work to subagents, separately from recording the '
        'exploration map. Specify scope, forbidden changes, deliverables, verification and model/effort. '
        'This starts work; never call merely to draw nodes. Wait/read using the returned run_id.')
    params_model = DispatchParams

    async def invoke(self, params):
        return await self.bridge.dispatch(params)


class ReadWorkflowTasks(ChatTool):
    name = 'ReadWorkflowTasks'
    description = 'Read actual states, results, files and visual blocks of the current chat workflow. Does not start or resume execution.'
    params_model = ReadParams

    async def invoke(self, params):
        return self.bridge.read(params)


class WaitForWorkflowTasks(ChatTool):
    name = 'WaitForWorkflowTasks'
    description = 'Wait for named tasks (empty: all) when no useful independent work remains. Releases runtime slots; returns actual results in this same chat. Never starts another planner or restarts paused work.'
    params_model = ReadParams

    async def invoke(self, params):
        return await self.bridge.wait(params)


class SaveWorkflow(ChatTool):
    name = 'SaveWorkflow'
    description = 'Save normally completed work as a versioned executable workflow including node models, prompts, constraints, dependencies and declared parameters. Does not rerun work.'
    params_model = SaveParams

    async def invoke(self, params):
        return await self.bridge.save(params)


class ReadSavedWorkflows(ChatTool):
    name = 'ReadSavedWorkflows'
    description = 'Discover saved workflows and their required parameters in this project; supply recipe_id to read executable nodes and a specific version for customization/reuse.'
    params_model = CatalogParams

    async def invoke(self, params):
        self.bridge.guard()
        store = RecipeStore(self.bridge.runtime.work_dir)
        if params.recipe_id:
            return await asyncio.to_thread(store.read, params.recipe_id, params.version)
        values = await asyncio.to_thread(store.list)
        return dict(recipes=[{key: value[key] for key in
            ('id', 'version', 'name', 'parameters', 'source_run_id')} for value in values])


class RunSavedWorkflow(ChatTool):
    name = 'RunSavedWorkflow'
    description = 'Execute a saved workflow in this ordinary chat with explicit parameters and a stable request_id. Optional per-node model/Prompt/constraints/Fast overrides create a new version. Uses fresh attempts, preserving prior files and history; choose an independent output directory.'
    params_model = RunParams

    async def invoke(self, params):
        return await self.bridge.run_recipe(params)


class ControlWorkflow(ChatTool):
    name = 'ControlWorkflow'
    description = 'Pause/stop the current chat workflow, or resume its saved unfinished work only when the user requests continuation. Completed nodes are retained.'
    params_model = ControlParams

    async def invoke(self, params):
        return await self.bridge.control(params)


class PublishWorkflowBlock(ChatTool):
    name = 'PublishWorkflowBlock'
    description = 'Choose useful text, table, chart, diagram or registered image to explain an existing node input/output. Stable block_id updates its display. Does not execute work or mark delivery; use genuine facts and label proposals.'
    params_model = BlockParams

    async def invoke(self, params):
        return self.bridge.publish(params)


DESIGN_TOOLS = ('ReadWorkflowBlocks', 'SubmitWorkflowBlocks', 'AdvanceTaskDesign', 'SubmitRevisionDraft')


def remove_design_tools(agent):
    """The ordinary chat exposes execution tools, not a second design pipeline."""
    registry = getattr(agent, 'registry', None)
    remove = getattr(agent, '_remove_runtime_tool', None) or getattr(registry, 'unregister', None)
    if remove:
        for name in DESIGN_TOOLS:
            remove(name)


def install(srv, runtime):
    if getattr(runtime.agent, 'registry', None) is None:
        return None
    if not getattr(runtime, 'orchestration_planning', False):
        remove_design_tools(runtime.agent)
    bridge = ChatWorkflow(srv, runtime)
    for cls in (UpdateWorkflowTasks, DispatchWorkflowTasks, ReadWorkflowTasks, WaitForWorkflowTasks, SaveWorkflow,
                ReadSavedWorkflows, RunSavedWorkflow, ControlWorkflow, PublishWorkflowBlock):
        installer = getattr(runtime.agent, '_install_runtime_tool', runtime.agent.registry.register)
        installer(cls(bridge))
    runtime.agent.workflow_context = bridge
    bridge.sync(runtime.conversation)
    try:
        bridge.restore(runtime.conversation)
    except (OSError, ValueError, KeyError, RuntimeError):
        # Reading a canvas snapshot is advisory; execution tools retain their
        # own task/permission/storage checks before any work can be dispatched.
        logging.getLogger(__name__).warning('chat workflow snapshot unavailable', exc_info=True)
        runtime.conversation.replace_marker_reminder('chat-workflow-state',
            '流程图状态暂时无法读取。继续处理不依赖该状态的用户任务；'
            '已派发工作的状态需用 ReadWorkflowTasks 核实，不猜测完成、不重复派发。')
    return bridge
