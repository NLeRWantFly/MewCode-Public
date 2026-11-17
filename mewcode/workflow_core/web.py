"""Normal Agent execution for workflow nodes, without legacy completion protocols."""
from __future__ import annotations

import asyncio
import copy
import json
import time
import uuid
from pathlib import Path

from . import files, node_tools, revisions
from .migration import graph_from_flow
from .runtime import Core
from .schema import digest, walk
from .store import ensure
from .continuation import context_note, restart_context

NODE_PROMPT = '''完成当前节点的任务，使用提供的背景和输入自主工作，完成后直接向用户交付结果。
单个工具报错、超时或结果未知不是结束本节点的理由；根据反馈修正调用或换用可行方法，并继续不受影响的工作。
可能已经生效的外部操作先检查实际进程、文件或服务，避免盲目重放。保留已有成果和错误记录，从未完成部分继续。
取得实质进展时，简短说明发现、修改结果或下一步；不要逐条复述工具日志。
WriteFile 成功生成的文件由宿主自动登记为产物；其他方式生成的已有文件使用 ReportArtifact 登记。
普通最终回复就是本节点交付，不需要交接证书或额外审批。
需要用户提供的信息或真实工具权限仍使用现有交互工具。不要自动添加质量评审、完美性目标或返修循环。
发现上游交付缺漏时，用 SendNodeMessage 向原节点追问或请求补交，带上版本、文件或页码，先继续不受影响的工作。
回复会在工具回合结束后送达；确实没有其他可做的工作时再用 WaitForNodeReply 让出执行名额。收到新版本后只修正受影响部分。
输入产物的 path 是本次绑定的固定版本，source_path 只用于追溯；不要转而读取可能已被改写的原始路径。
'''


def envelope(graph, original=None):
    """Compatibility envelope for existing conversation/run registries."""
    from mewcode.experts.flow import ExpertFlowSpec
    original = original.as_dict() if hasattr(original, 'as_dict') else original or {}
    nodes = [dict(node_id=node['id'], role='executor', label=node['label'], responsibility=node['task'],
                  model=node.get('model')) for node in graph['nodes']]
    if not nodes:
        raise ValueError('请先添加至少一个节点')
    meta = {key: copy.deepcopy(value) for key, value in (original.get('meta') or {}).items()
            if key in {'task_design', 'run_input_contract', 'goal_contract'}}
    meta.update(workflow_core=graph, orchestration_mode='classic')
    return ExpertFlowSpec.from_value(dict(flow_id=original.get('flow_id') or 'workflow-' + uuid.uuid4().hex[:12],
        version=graph['revision'], label=graph['label'], nodes=nodes,
        edges=[dict(**{'from': e['source'], 'to': e['target']}, condition=e['port']) for e in graph['edges']],
        finalizer_node_id=nodes[-1]['node_id'], meta=meta))


def resolve_model(node, profile):
    from mewcode.model_registry import ModelRef
    selected = node.get('model') or profile.get('executor')
    if not selected:
        raise ValueError(f'节点 {node["label"]} 未配置执行模型')
    return ModelRef.from_value(selected)


class AgentExecutor:
    cooperative_cancel = True
    def __init__(self, session, registry, store, run_id, profile, emit, content_blocks=None):
        self.session, self.registry, self.store = session, registry, store
        self.run_id, self.profile, self.emit = run_id, profile, emit
        self.content_blocks = content_blocks or []
        self.interaction_lock = asyncio.Lock()

    async def interaction(self, ctx, request):
        from mewcode.agent import PermissionRequest, HumanOnCallRequest
        from mewcode.orchestration.inbox import permission_identity
        async with self.interaction_lock:
            if isinstance(request, PermissionRequest):
                session = self.session
                session._perm_future = request.future
                session.permission_request_future = request.future
                pending = dict(tool_id=request.tool_id, tool=request.tool_name, name=request.tool_name,
                               description=request.description, reason=request.reason, source=request.source,
                               arguments=copy.deepcopy(request.arguments))
                session.permission_request = permission_identity(session, pending, run_id=self.run_id,
                    node_id=ctx.node_id, epoch=0)
                self.store.mutate(self.run_id, lambda record: record.setdefault('node_states', {}).setdefault(
                    ctx.node_id, {}).update(pending_permission=copy.deepcopy(session.permission_request)))
                try:
                    await self.emit(dict(type='permission', **session.permission_request))
                    await request.future
                finally:
                    session._perm_future = None
                    session.permission_request = {}
                    self.store.mutate(self.run_id, lambda record: record['node_states'][ctx.node_id].pop('pending_permission', None))
            else:
                from mewcode.experts.waits import _show_interaction
                kind = 'human_oncall' if isinstance(request, HumanOnCallRequest) else 'ask_user'
                # Show the actual question directly; do not run another planning model to rewrite it.
                await _show_interaction(request, kind=kind, session=self.session, emit=self.emit,
                    store=self.store, run_id=self.run_id, stage_key=ctx.node_id, role='executor',
                    bucket='node_states', resumed=False, scope_hash=ctx.attempt_id)

    async def __call__(self, ctx):
        from mewcode.agent import (StreamText, ThinkingText, ToolUseEvent, ToolExecutionStartEvent,
            ToolResultEvent, ErrorEvent, LoopComplete, TurnComplete, StreamRetryEvent, UsageEvent,
            PermissionRequest, AskUserRequest, HumanOnCallRequest)
        from mewcode.expert_group import ExpertExecutionPolicy
        from mewcode.expert_web import _build_role_agent
        from mewcode.expert_runs import serialize_messages, deserialize_messages
        from mewcode.tools.report_step import report_fields
        from .context import NodeContext, configure_tool_context
        from .judgment import instructions, parse as parse_judgment, repair_message
        node = ctx.node
        if node['kind'] == 'judge' and node['config'].get('manual') and not ctx.request:
            return dict(status='needs_input', text='', reason=node['task'] or '等待人工判断', manual_judgment=True)
        if node['config'].get('legacy_pipeline'):
            return await self.pipeline(ctx)
        model = resolve_model(node, self.profile)
        revision_root, revision_paths = None, {}
        if ctx.request and ctx.request['kind'] != 'ask':
            revision_root, revision_paths = await asyncio.to_thread(revisions.prepare, self.session.work_dir,
                self.run_id, ctx.request, ctx.inputs.get('previous_delivery') or {}, self.store.root)
        policy = ExpertExecutionPolicy('stable', False, 0, max_iterations=0,
            work_dir=str(revision_root) if revision_root else node['config'].get('workspace'),
            stage_inputs=ctx.inputs, input_revision=ctx.attempt_id,
            node_tools=tuple(node['tools']) if node.get('tools') is not None else None,
            deny_tools=tuple(node.get('deny_tools') or ()), execution_node_id=ctx.node_id,
            authorization_reader=lambda: self.store.load(self.run_id) or {},
            parallel_readonly=ctx.request.get('kind') == 'ask',
            role_model_bindings={'reviewer': model.as_dict()} if node['config'].get('goal_execution') else None,
            expansion_write_paths=(str(revision_root),) if revision_root else None)
        agent, _, runtime = _build_role_agent(self.session, self.registry, 'executor', model, policy)
        if node['config'].get('goal_execution'):
            agent.auxiliary_client = agent.client
            configure_tool_context(agent)
            if getattr(self.session, 'goal_runtime_scheduler', None) is not None:
                agent.runtime_scheduler = self.session.goal_runtime_scheduler
        if node['config'].get('service_tier') is not None:
            if not hasattr(agent.client, 'service_tier'):
                raise ValueError('当前模型客户端不支持所选服务档位')
            agent.client.service_tier = node['config']['service_tier']
        # These are quality/coordination mechanisms, not the existing tool permission checker.
        agent.critic = None
        agent.stall_policy = None
        agent.completion_gate = None
        # Long reasoning and file output are normal node work. Neither elapsed
        # request time nor silence should cancel and resubmit that work.
        # Actual transport failures and explicit user cancellation still apply.
        agent.provider_watchdog = False
        agent.workflow_long_running = True
        agent.client.workflow_long_running = True
        agent.workflow_context = NodeContext(ctx)
        for name in ('SubmitHandoff', 'SubmitVerdict', 'SubmitLoopDecision', 'RequestAssistance', 'ReadWorkflowContext'):
            agent.registry.unregister(name)
        allowed = (Path(self.store.root) / self.run_id).resolve()
        from .scoped_io import install as install_scoped_io
        install_scoped_io(agent, node['config'], allowed)
        def grant_reads(value):
            revisions.grant_reads(agent, value, allowed)
        node_tools.install(agent, ctx, on_result=grant_reads)
        if ctx.request.get('kind') == 'ask':
            for tool in agent.registry.list_tools():
                if not tool.is_read_only:
                    agent.registry.unregister(tool.name)
        if revision_root:
            agent.work_dir = str(revision_root)
        grant_reads(ctx.inputs)
        agent.set_review_authorization_context(ctx.background + '\n当前节点：' + node['task'])
        conversation = runtime.conversation
        fresh_context = restart_context(ctx.checkpoint, ctx.recovery)
        if ctx.checkpoint.get('messages') and not fresh_context:
            conversation.restore_history(deserialize_messages(ctx.checkpoint['messages']))
            conversation.repair_incomplete_tool_turns()
        elif fresh_context:
            conversation.add_system_reminder(context_note(ctx.checkpoint))
        prior_tools = copy.deepcopy(ctx.checkpoint.get('pending_tools') or {})
        unresolved = {key: value for key, value in prior_tools.items()
                      if value.get('status') in {'running', 'unknown', 'outcome_unknown'}}
        for value in unresolved.values():
            value['status'] = 'unknown'
        prompt = NODE_PROMPT + '\n背景：\n' + ctx.background + '\n当前节点：\n' + node['task']
        if node['config'].get('prompt'):
            prompt += '\n节点补充指令：\n' + node['config']['prompt']
        if node['config'].get('constraints'):
            prompt += '\n节点约束：\n' + json.dumps(node['config']['constraints'], ensure_ascii=False)
        prompt += '\n输入：\n' + json.dumps(ctx.inputs, ensure_ascii=False, default=str)
        if node['config'].get('exploration_owner'):
            prompt += ('\n当前是滚动目标主对话：先处理眼前已知事项，使用 UpdateWorkflowTasks 提交可独立执行的工作项和真实依赖。'
                '工作项可以执行中继续补充，不必预先制定整张流程或固定批次。主对话维护目标、事实和用户后续要求。'
                '无其他有用工作时调用 WaitForWorkflowTasks，保存本轮并释放名额；恢复后读取输入 workflow_tasks 或 ReadWorkflowTasks。'
                '需要固化复用的汇总文件也作为实际工作项安排，主对话最后索引真实交付。'
                '技术中断不算交付，不要静默放弃工作项；已完成工作直接复用。所有必要工作正常交付后再给最终答复。')
        if node['config'].get('presentation'):
            prompt += ('\n可使用 PublishWorkflowBlock 选择有用的输入/输出表达：文字、表格、图表、关系图或已登记图片。'
                '由你决定表达的业务内容，使用真实观察与实际产物，明确区分计划和结果；稳定 block_id 用于更新。'
                '绘图使用结构化 nodes/edges 或 labels/series，图片先登记产物。展示不是交付门槛，不要为绘图重复模型调用。')
        if getattr(ctx, 'recovery', None):
            prefix = ('\n系统正在接续本节点的技术中断，沿用原任务和授权。' if ctx.recovery.get('automatic_continuation')
                      else '\n用户已要求检查阻塞、修复并继续。')
            prompt += (prefix + '接续当前节点保存的对话、文件与工具回执，'
                       '先诊断下列当前问题，在既有任务权限内自行修复并完成未完成部分。'
                       '不要重新执行已完成的业务，不把历史错误或未知回执当作需要额外确认的门槛；'
                       '结果未知的操作先核查实际效果。需要环境适配时使用现有诊断、环境准备及文件工具。'
                       '只在确实缺少无法自行获得的信息、权限或执行条件时说明具体缺口。\n'
                       + json.dumps(ctx.recovery, ensure_ascii=False, default=str))
        if ctx.request:
            prompt += ('\n这是原节点的一轮定向' + ('追问，只需解释，不修改业务产物。' if ctx.request['kind'] == 'ask'
                else '补交。保留既有成果，只处理本次要求的变化；普通回复和产物登记即完成，不另行审批或验收。')
                + '\n请求：\n' + json.dumps(ctx.request, ensure_ascii=False, default=str))
            if revision_root:
                prompt += ('\n本轮可写的修订目录：' + str(revision_root) +
                    ('\n这是回复通知，旧产物通过输入中的固定版本路径只读访问；修订目录不预复制旧产物。'
                     '确需修改时，仅将相关文件复制到此目录并登记新版本；无需为确认收到回复而重新交付文件。'
                     if ctx.request['kind'] == 'update' else
                     '\n已有文件的副本已按原相对目录放入此处。只在此目录修改/新增并登记产物，旧版文件保持可读。') +
                    '\n本轮不重新执行原任务中已经完成的部署、上传或其他无关操作。')
        if unresolved:
            prompt += ('\n恢复说明：以下历史工具调用的实际结果未确认。由你在当前节点内检查进程、文件或服务状态，'
                       '复用已有结果并继续任务；不要直接重放可能仍在执行或已产生副作用的操作。'
                       '这些记录不是需要用户解除的编排门槛；只有真实缺少输入或权限时才请求用户。\n'
                       + json.dumps(unresolved, ensure_ascii=False, default=str))
        if node['inputs']:
            prompt += '\n输入说明：\n' + json.dumps(node['inputs'], ensure_ascii=False)
        if node['outputs']:
            prompt += '\n期望产出：\n' + json.dumps(node['outputs'], ensure_ascii=False)
        if node['kind'] == 'judge' and ctx.request.get('kind') != 'ask':
            prompt += ('\n判断条件：' + json.dumps(node['config'].get('criteria') or [node['task']], ensure_ascii=False)
                       + '\n' + instructions(node))
        conversation.add_user_message(prompt, content_blocks=self.content_blocks if not ctx.checkpoint.get('messages') else [])
        agent.workflow_context.restore(conversation)
        tools, fragments, public_messages, usage = prior_tools, [], [], {}
        completed, error, final_response, decision = False, None, '', None
        async def checkpoint():
            await ctx.publish('checkpoint', messages=serialize_messages(conversation.history),
                              pending_tools=tools, usage=usage, partial_text=''.join(fragments))
        async def flush_public():
            text = ''.join(fragments).strip()
            fragments.clear()
            if text:
                public_messages.append(text)
                await ctx.publish('progress', text=text)
        async def receive_messages():
            if not ctx.channel:
                return
            messages = ctx.poll_messages(ack=False)
            if messages:
                ctx.inputs.update(ctx.channel('inputs'))
                grant_reads(messages)
                grant_reads(ctx.inputs)
                conversation.add_user_message('节点消息已到达，请保留既有工作并处理相关部分：\n' +
                    json.dumps(messages, ensure_ascii=False, default=str))
                await checkpoint()
                ctx.channel('ack', notification_ids=[message['id'] for message in messages])
        try:
            await receive_messages()
            while not completed:
                if ctx.cancelled.is_set():
                    raise asyncio.CancelledError()
                error = None
                await ctx.publish('activity', activity='waiting_model')
                stream = agent.run(conversation)
                try:
                    async for item in stream:
                        if ctx.cancelled.is_set() and not isinstance(item, ToolResultEvent) and not any(
                                t.get('status') == 'running' for t in tools.values()):
                            raise asyncio.CancelledError()
                        if isinstance(item, ThinkingText):
                            await ctx.publish('activity', activity='reasoning')
                            continue
                        if isinstance(item, StreamText):
                            await ctx.publish('activity', activity='output')
                            fragments.append(item.text)
                        elif isinstance(item, StreamRetryEvent):
                            await ctx.publish('activity', activity='retrying', retry_attempt=item.attempt,
                                              retry_max=item.max_attempts, retry_wait=item.wait)
                            if item.discard_chars:
                                text = ''.join(fragments)
                                fragments[:] = [text[:-item.discard_chars]]
                        elif isinstance(item, ToolUseEvent):
                            await flush_public()
                            tools[item.tool_id] = dict(name=item.tool_name, arguments=item.arguments, status='queued',
                                                       queued_at=time.time())
                            await ctx.publish('activity', activity='tool_queued', tool=item.tool_name, tool_id=item.tool_id)
                            await checkpoint()
                        elif isinstance(item, ToolExecutionStartEvent):
                            tools.setdefault(item.tool_id, dict(name=item.tool_name)).update(status='running', started_at=time.time())
                            await ctx.publish('activity', activity='tool_running', tool=item.tool_name, tool_id=item.tool_id,
                                              tools=[t['name'] for t in tools.values() if t.get('status') == 'running'])
                            await checkpoint()
                        elif isinstance(item, ToolResultEvent):
                            entry = tools.setdefault(item.tool_id, dict(name=item.tool_name))
                            entry.update(status='failed' if item.is_error else 'completed', ended_at=time.time())
                            observed = item.host_observation or {}
                            command = observed.get('command_execution') or {} if isinstance(observed, dict) else {}
                            if command.get('status') in {'outcome_unknown', 'timed_out', 'cancelled'} and command.get('command_started') is not False:
                                entry['status'] = 'unknown'
                            running = [t['name'] for t in tools.values() if t.get('status') == 'running']
                            await ctx.publish('activity', activity='tool_running' if running else {
                                'failed': 'tool_error', 'unknown': 'tool_unknown'}.get(entry['status'], 'tool_returned'),
                                tool=item.tool_name, tool_id=item.tool_id, tools=running, outcome=entry['status'])
                            await ctx.publish('tool_result', tool_id=item.tool_id, name=item.tool_name,
                                              is_error=item.is_error, output=item.output)
                            if not item.is_error and item.tool_name == 'report_step':
                                report = report_fields(entry.get('arguments') or {})
                                text = report.get('summary') or (entry.get('arguments') or {}).get('label')
                                if text:
                                    await ctx.publish('progress', text=text)
                            if not item.is_error and item.tool_name in {'WriteFile', 'EditFile', 'ReportArtifact'}:
                                args = entry.get('arguments') or {}
                                path = args.get('file_path') or args.get('path')
                                if path:
                                    target = Path(path)
                                    if not target.is_absolute():
                                        target = Path(revision_root or self.session.work_dir) / target
                                    try:
                                        artifact = await asyncio.to_thread(files.capture, self.store.root, self.run_id,
                                            ctx.attempt_id, target, sandbox=agent.permission_checker.sandbox)
                                        origin = Path(revision_root or self.session.work_dir).resolve()
                                        resolved_target = target.resolve()
                                        artifact['logical_path'] = revision_paths.get(str(resolved_target)) or (
                                            str(resolved_target.relative_to(origin)) if resolved_target.is_relative_to(origin)
                                            else str(resolved_target))
                                        await ctx.publish('artifact', artifact=artifact)
                                    except (ValueError, OSError) as exc:
                                        await ctx.publish('artifact_error', path=str(target), reason=str(exc))
                            await checkpoint()
                        elif isinstance(item, (PermissionRequest, AskUserRequest, HumanOnCallRequest)):
                            await ctx.publish('activity', activity='waiting_permission' if isinstance(item, PermissionRequest)
                                              else 'waiting_input')
                            await self.interaction(ctx, item)
                            await ctx.publish('activity', activity='tool_running',
                                              tools=[t['name'] for t in tools.values() if t.get('status') == 'running'])
                        elif isinstance(item, UsageEvent):
                            usage.update(input_tokens=item.input_tokens, output_tokens=item.output_tokens,
                                         billable_tokens=item.billable_tokens)
                        elif isinstance(item, ErrorEvent):
                            error = item
                        elif isinstance(item, TurnComplete):
                            await flush_public()
                            await checkpoint()
                            await receive_messages()
                            ctx.cooperate()
                            await ctx.publish('activity', activity='waiting_model')
                        elif isinstance(item, LoopComplete):
                            # A wait tool may be the last action before a provider's
                            # final reply; it must still yield instead of completing.
                            if ctx.channel and ctx.channel('waiting'):
                                await checkpoint()
                                from .runtime import YieldExecution
                                raise YieldExecution()
                            final_response = ''.join(fragments).strip()
                            completed = True
                            await ctx.publish('activity', activity='finishing')
                    await flush_public()
                    await checkpoint()
                finally:
                    await stream.aclose()
                if error and error.kind in {'max_iterations', 'output_window_limit'}:
                    conversation.add_system_reminder('接着当前检查点继续完成本节点；保留已有成果，输出较长时分块交付。')
                    await checkpoint()
                    continue
                if error or not completed:
                    return dict(status='blocked', text=public_messages[-1] if public_messages else '',
                                reason=('模型对话已中断，节点尚未交付：' + error.message) if error
                                else '模型对话已结束，节点尚未交付。')
                if final_response and node['kind'] == 'judge' and ctx.request.get('kind') != 'ask':
                    try:
                        decision = parse_judgment(final_response, node)
                    except (ValueError, TypeError) as exc:
                        await ctx.publish('judgment_format_error', reason=str(exc), text=final_response)
                        conversation.add_system_reminder(repair_message(node, exc))
                        agent.workflow_context.restore(conversation)
                        completed, final_response = False, ''
                        await checkpoint()
            if not final_response:
                return dict(status='blocked', text=public_messages[-1] if public_messages else '',
                            reason='模型对话已结束，尚未收到节点的最终交付。')
            final = final_response
            result = dict(status='completed', text=final, tokens=usage.get('billable_tokens') or
                          usage.get('input_tokens', 0) + usage.get('output_tokens', 0))
            if decision is not None:
                result.update(decision)
            return result
        finally:
            for pending in tools.values():
                if pending.get('status') == 'running':
                    pending['status'] = 'unknown'
            await checkpoint()
            await runtime.aclose()

    async def pipeline(self, ctx):
        # Explicit old pipeline nodes need their referenced graph, never a guessed substitute.
        from mewcode.pipeline.executor import PipelineExecutor
        from mewcode.pipeline.models import PipelineGraph
        from mewcode.pipeline.store import PipelineStore
        reference = ctx.node['config']['legacy_pipeline']['pipeline']
        store = PipelineStore()
        graph = store.get(reference['graph_id'])
        if graph is None:
            return dict(status='needs_input', text='', reason='引用的流水线不存在：' + reference['graph_id'])
        return dict(status='needs_input', text='', reason='请将引用流水线展开为执行节点后继续：' + reference['graph_id'])


async def run_chat(ws, task, *, session, registry_service, profile_store, run_store,
                   confirmed_new_flow=None, resume_run_id='', content_blocks=None,
                   task_design_dispatch=None, supplemental='', retry_node_id='', resume_after_node='', **kwargs):
    if not confirmed_new_flow and not resume_run_id:
        await ws.send_json(dict(type='done', rejected=True, interrupted=False,
            error_code='unconfirmed_flow_start', error='请先保存并启动工作流'))
        return
    from mewcode.expert_group import ExpertProfile
    from mewcode.conversation import Message
    from mewcode.expert_web import _attachment_refs
    from mewcode.orchestration.task_design import TaskDesign
    store = run_store
    started = time.monotonic()
    run_id = resume_run_id
    backend = None
    interrupted = False
    async def emit(event):
        await ws.send_json(dict(event, run_id=run_id, conversation_id=session.conversation_id))
    try:
        record = store.load(run_id) if run_id else None
        if run_id and (not record or record.get('conversation_id') != session.conversation_id):
            raise ValueError('该运行不属于当前会话')
        if record is None:
            if confirmed_new_flow is None:
                raise ValueError('请先保存并启动工作流')
            profile = profile_store.get(session.work_dir)
            if profile is None:
                raise ValueError('请先配置执行模型')
            profile = profile.as_dict() if hasattr(profile, 'as_dict') else profile
            graph = graph_from_flow(confirmed_new_flow, goal=task)
            # Resolve exact configured models before creating any paid execution.
            for _, node in walk(graph):
                if node['kind'] == 'work' or node['kind'] == 'judge' and not node['config'].get('manual') and 'expression' not in node['config']:
                    registry_service.resolve(resolve_model(node, profile))
            from mewcode.orchestration.draft_sources import digest as task_digest
            run_id = (task_digest([task_design_dispatch['task_id'], task_design_dispatch['claim_id']])[:32]
                      if task_design_dispatch else uuid.uuid4().hex)
            if store.load(run_id):
                raise ValueError('该启动请求已有运行记录，请继续原运行')
            record = store.create(run_id=run_id, conversation_id=session.conversation_id, task=task,
                attachments=_attachment_refs(content_blocks or []), profile=profile, strategy='stable',
                flow=envelope(graph, confirmed_new_flow))
            backend = ensure(store, run_id, graph)
            run_inputs = copy.deepcopy(getattr(session, 'orchestration_run_inputs', {}) or {})
            run_inputs['attachments'] = _attachment_refs(content_blocks or [])
            backend.change(lambda state: state.update(inputs=run_inputs))
            if task_design_dispatch:
                TaskDesign(session.work_dir, session.conversation_id).bind_run(task_id=task_design_dispatch['task_id'],
                    claim_id=task_design_dispatch['claim_id'], run_id=run_id)
            if session.conversation is not None:
                session.conversation.add_user_message(task, content_blocks=content_blocks or [])
            if session.session is not None:
                session.session.append(Message(role='user', content=task))
                if hasattr(session.session, 'append_expert_run'):
                    session.session.append_expert_run(run_id, status='running', seq=0, display_index=0)
        else:
            profile = copy.deepcopy(record['locked_profile'])
            executor_model = ((record.get('role_models') or {}).get('roles') or {}).get('executor')
            if executor_model:
                profile['executor'] = copy.deepcopy(executor_model)
            backend = ensure(store, run_id)
        session.expert_run_id = session.last_expert_run_id = run_id
        from mewcode.orchestration.execution_scope import prepare_orchestration_scope
        host_scope = prepare_orchestration_scope(session, task=record.get('task') or task,
                                                 run_id=run_id, checkpoint=dict(record, workflow_core=backend.read()))
        await asyncio.to_thread(store.mutate, run_id, lambda value: value.update(host_execution_scope=host_scope),
                                durable=True, return_snapshot=False)
        if supplemental:
            backend.change(lambda state: state['graph'].update(goal=state['graph']['goal'] + '\n' + supplemental))
        executor = AgentExecutor(session, registry_service, store, run_id, profile, emit, content_blocks)
        async def report(attempt):
            artifact = await asyncio.to_thread(files.write_report, store.root, run_id, attempt)
            backend.change(lambda state: state['attempts'][attempt['id']].update(report=artifact))
        graph = backend.read()['graph']
        if graph.get('execution_profile') in {'bookflow.parallel.v1', 'goal.open.v1', 'goal.recipe.v1'}:
            scheduler = getattr(getattr(session, 'agent', None), 'runtime_scheduler', None)
            if scheduler is not None:
                lanes = max(1, min(int(graph.get('concurrency', 8)), 16))
                limits = dict(scheduler.kind_limits)
                limits['model'] = max(limits.get('model', 1), lanes)
                await scheduler.configure(limit=max(scheduler.limit, lanes + 4), kind_limits=limits)
        core_options = dict(concurrency=graph.get('concurrency', 4), run_id=run_id, emit=emit, report=report)
        if graph.get('execution_profile') == 'goal.open.v1':
            from .exploration import GoalCore
            from .recipes import RecipeStore
            core = GoalCore(backend, executor, recipe_store=RecipeStore(session.work_dir), **core_options)
        else:
            core = Core(backend, executor, **core_options)
        if retry_node_id or resume_after_node:
            core.control(dict(operation='continue_from', node_id=retry_node_id or resume_after_node, request_id=uuid.uuid4().hex))
        elif backend.read()['status'] != 'running':
            core.control(dict(operation='resume', request_id=uuid.uuid4().hex))
        await emit(dict(type='expert_run_state', status='running', workflow_core=True))
        result = await core.run()
        final = '\n\n'.join(row['result'].get('text', '') for path, row in result['nodes'].items()
                            if '/' not in path and row['status'] == 'completed' and row.get('result', {}).get('text'))
        if not final:
            final = '本次执行已结束，产物和进度已保存在工作流中。' if result['status'] == 'completed' else '进度已保存，可在节点面板继续处理。'
        if session.conversation is not None:
            session.conversation.add_assistant_message(final)
        if session.session is not None:
            session.session.append(Message(role='assistant', content=final))
        await emit(dict(type='expert_summary', text=final, status=result['status']))
        await emit(dict(type='expert_run_state', status=result['status']))
    except asyncio.CancelledError:
        interrupted = True
        if backend:
            status = 'terminated' if getattr(session, 'expert_control_action', '') == 'terminate' else 'paused'
            backend.change(lambda state: state.update(status=status, stop_requested=True))
    except Exception as exc:
        if backend:
            backend.change(lambda state: state.update(status='blocked'))
        await emit(dict(type='error', text=str(exc)))
    finally:
        session.expert_run_id = ''
        await emit(dict(type='done', interrupted=interrupted, elapsed=time.monotonic() - started,
                        status=session.status_payload(), tasks=session.tasks_payload()))
