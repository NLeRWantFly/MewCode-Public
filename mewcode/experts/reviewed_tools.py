"""Host-directed recovery tools through the ordinary Agent execution gateway."""
from __future__ import annotations

import asyncio
import copy
import time
import uuid

from mewcode.agent import AskUserRequest, HumanOnCallRequest, PermissionRequest, ToolExecutionStartEvent
from mewcode.tools.base import ToolCallComplete, ToolResult
from mewcode.experts.waits import wait_for_human, wait_for_interaction


async def invoke_reviewed_tool(agent, name, arguments, *, session, store, run_id, node_id, emit, tool_id=None):
    """Keep global policy, Auto review, cancellation and durable tool receipts."""
    tool_id = tool_id or 'environment_' + uuid.uuid4().hex
    row = {'name': name, 'tool_id': tool_id, 'args': copy.deepcopy(arguments), 'status': 'queued',
           'declared_at': time.time()}
    def save(record):
        stage = record.setdefault('node_states', {}).setdefault(node_id, {})
        row['epoch'] = stage.get('epoch', 0)
        stage.setdefault('tools', {})[tool_id] = copy.deepcopy(row)
    store.mutate(run_id, save, durable=True)
    await emit({'type': 'expert_tool', 'node_id': node_id, 'role': 'executor',
                'id': tool_id, 'name': name, 'args': arguments})
    outcome = ToolResult('Recovery tool did not produce a result.', is_error=True)
    stream = agent._execute_tool(ToolCallComplete(tool_id, name, arguments))
    try:
        async for event in stream:
            if isinstance(event, tuple):
                outcome, elapsed, _ = event
                outcome.host_observation = {**(outcome.host_observation or {}), 'recovery_tool_id': tool_id}
                row.update(status='completed', output=outcome.output, is_error=outcome.is_error,
                           host_observation=outcome.host_observation, elapsed=elapsed, completed_at=time.time())
            elif isinstance(event, ToolExecutionStartEvent):
                row.update(status='running', started_at=time.time())
                from mewcode.experts.blocking_policy import record_reviewed_tool_start
                store.mutate(run_id, lambda r: record_reviewed_tool_start(r, node_id, tool_id, name, arguments), durable=True)
            elif isinstance(event, PermissionRequest):
                pending = {'tool_id': tool_id, 'name': name, 'arguments': copy.deepcopy(arguments),
                           'description': event.description, 'reason': event.reason, 'source': event.source}
                row['status'] = 'waiting_permission'
                store.mutate(run_id, lambda r: r['node_states'][node_id].update(pending_permission=pending), durable=True)
                lock = getattr(session, '_expert_permission_lock', None)
                if lock is None:
                    lock = session._expert_permission_lock = asyncio.Lock()
                async with lock:
                    session._perm_future = event.future
                    session.permission_request_future = event.future
                    from mewcode.orchestration.inbox import permission_identity
                    session.permission_request = permission_identity(session, {'role': 'executor',
                        'tool': name, 'tool_id': tool_id, 'description': event.description, 'reason': event.reason,
                        'source': event.source, 'directory_scope': event.directory_scope,
                        'directory_ttl_seconds': event.directory_ttl_seconds}, run_id=run_id, node_id=node_id, epoch=row.get('epoch', 0))
                    pending.update({key: session.permission_request[key] for key in ('request_id', 'request_epoch', 'node_epoch')})
                    store.mutate(run_id, lambda r: r['node_states'][node_id].update(pending_permission=copy.deepcopy(pending)), durable=True)
                    try:
                        await emit({'type': 'permission', **session.permission_request})
                        response = await wait_for_human(event.future, kind='permission', emit=emit, store=store,
                            run_id=run_id, stage_key=node_id, wait_id=tool_id, role='executor')
                    finally:
                        session._perm_future, session.permission_request = None, {}
                from mewcode.experts.blocking_policy import record_permission_response
                def answered(record):
                    record_permission_response(record, node_id, tool_id, response)
                    record['node_states'][node_id].pop('pending_permission', None)
                store.mutate(run_id, answered, durable=True)
                row['status'] = 'queued'
            elif isinstance(event, (AskUserRequest, HumanOnCallRequest)):
                await wait_for_interaction(event, kind='ask_user' if isinstance(event, AskUserRequest) else 'human_oncall',
                    session=session, emit=emit, store=store, run_id=run_id, stage_key=node_id, role='executor')
            store.mutate(run_id, save, durable=True)
    finally:
        await stream.aclose()
        if row['status'] in {'queued', 'waiting_permission'}:
            row.update(status='interrupted_before_execution')
        elif row['status'] == 'running':
            row.update(status='outcome_unknown')
        store.mutate(run_id, save, durable=True)
    await emit({'type': 'expert_tool_result', 'node_id': node_id, 'role': 'executor', 'id': tool_id,
        'name': name, 'output': outcome.output, 'is_error': outcome.is_error})
    return outcome
