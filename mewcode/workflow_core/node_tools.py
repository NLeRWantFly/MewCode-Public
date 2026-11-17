"""Tools bound to one node execution; authorship comes from the host context."""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field

from mewcode.tools.base import Tool, ToolResult
from .exploration import WorkflowTask
from .presentation import PresentationBlock


class MessageParams(BaseModel):
    node_id: str = Field(description='Target node ID from workflow inputs or ListWorkflowNodes')
    message: str
    kind: Literal['ask', 'revise'] = Field(default='ask', description='ask explains; revise requests a corrected/supplemental delivery')
    references: list[str] = Field(default_factory=list, description='Relevant files, pages or record IDs')
    target_attempt_id: str = Field(default='', description='Optional historical execution; otherwise the consumed upstream instance')
    base_delivery_id: str = ''
    request_id: str = Field(default='', description='Optional stable ID; identical repeated requests otherwise reuse a receipt')


class RequestParams(BaseModel):
    request_id: str = Field(default='', description='Request receipt ID; empty reads all requests sent by this node')


class DeliveryParams(BaseModel):
    delivery_id: str


class EmptyParams(BaseModel):
    pass


class CheckpointParams(BaseModel):
    attempt_id: str = Field(default='', description='Saved attempt ID; empty selects the previous checkpoint')
    section: Literal['summary','messages','tools'] = 'summary'
    index: int = Field(default=0, ge=0, description='Message or tool record index')
    offset: int = Field(default=0, ge=0, description='Character offset inside the selected record')
    limit: int = Field(default=6000, ge=1, le=12000)


class TaskUpdateParams(BaseModel):
    request_id: str = Field(min_length=1, max_length=100, description='Stable receipt ID for this task update')
    items: list[WorkflowTask] = Field(min_length=1, max_length=100)


class TaskReadParams(BaseModel):
    node_ids: list[str] = Field(default_factory=list, description='Empty selects all current work items')


class TaskWaitParams(BaseModel):
    node_ids: list[str] = Field(min_length=1, description='Wait only when no useful independent work remains')


class NodeTool(Tool):
    category = 'read'
    is_system_tool = True
    consumes_runtime_slot = False
    is_concurrency_safe = False

    def __init__(self, ctx, on_result=None):
        self.ctx = ctx
        self.on_result = on_result

    def result(self, action, **fields):
        try:
            value = self.ctx.channel(action, **fields)
            if self.on_result:
                self.on_result(value)
            return ToolResult(json.dumps(value, ensure_ascii=False, default=str))
        except (ValueError, KeyError) as exc:
            return ToolResult(str(exc), is_error=True)


class SendNodeMessage(NodeTool):
    name = 'SendNodeMessage'
    description = ('Send an asynchronous question or revision request to an existing workflow node. '
        'Its context and model are retained. Returns a receipt immediately; continue unaffected work. '
        'No graph rewind, downstream cancellation, approval or extra review is added.')
    params_model = MessageParams

    async def execute(self, params):
        if self.ctx.request.get('kind') == 'ask' and params.kind != 'ask':
            return ToolResult('当前回合只回答追问，不把它升级为修改产物的请求。', is_error=True)
        return self.result('send', **params.model_dump())


class ReadNodeReplies(NodeTool):
    name = 'ReadNodeReplies'
    description = 'Read replies/status for requests sent by this node without waiting or stopping other work.'
    params_model = RequestParams

    async def execute(self, params):
        return self.result('read', request_id=params.request_id)


class WaitForNodeReply(NodeTool):
    name = 'WaitForNodeReply'
    description = ('Use only when there is no other useful work before a requested reply. '
        'Saves this complete tool turn and releases the execution slot. The node resumes automatically '
        'when the reply arrives; other nodes continue. Do not poll in a sleep loop.')
    params_model = RequestParams

    async def execute(self, params):
        return self.result('wait', request_id=params.request_id)


class UseNodeDelivery(NodeTool):
    name = 'UseNodeDelivery'
    description = ('Bind a notified immutable upstream revision at a saved work boundary. '
        'Returns its frozen files; preserve unaffected work and update only affected results.')
    params_model = DeliveryParams

    async def execute(self, params):
        result = self.result('adopt', delivery_id=params.delivery_id)
        if not result.is_error:
            self.ctx.inputs.update(self.ctx.channel('inputs'))
        return result


class ListWorkflowNodes(NodeTool):
    name = 'ListWorkflowNodes'
    description = 'List node IDs, current deliveries and execution instances available for a direct question.'
    params_model = EmptyParams

    async def execute(self, params):
        return self.result('nodes')


class ReadNodeCheckpoint(NodeTool):
    name = 'ReadNodeCheckpoint'
    description = ('Page the current node conversation\'s preserved checkpoint, messages and tool receipts. '
        'Use after context recovery to continue unfinished work without repeating completed operations. '
        'Returns at most limit characters and next_offset; old image evidence should be read through its original artifact.')
    params_model = CheckpointParams

    async def execute(self, params):
        values = params.model_dump()
        values['attempt_id'] = values['attempt_id'] or self.ctx.checkpoint.get('id') or self.ctx.attempt_id
        return self.result('checkpoint', **values)


class UpdateWorkflowTasks(NodeTool):
    name = 'UpdateWorkflowTasks'
    # Like SendNodeMessage, this updates the host-bound work queue. It grants
    # no tool permissions; each dispatched node retains its normal checker.
    inline_schema_refs = True
    description = ('Append coherent business deliverables to this live goal. Keep paging and tool calls inside a task '
        'unless they are independent deliverables. Independent tasks start immediately '
        'without fixed batches. Reuse stable task IDs and request_id to avoid duplicates. '
        'Unstarted work may be revised. Use a new ID and replaces for failed work, retaining old records and '
        'binding pending successors to the replacement. Never replace running/delivered work or depend on this ongoing goal conversation.')
    params_model = TaskUpdateParams

    async def execute(self, params):
        return self.result('update_tasks', **params.model_dump())


class ReadWorkflowTasks(NodeTool):
    name = 'ReadWorkflowTasks'
    description = 'Read actual work item states, public results, files and visual blocks for this goal.'
    params_model = TaskReadParams

    async def execute(self, params):
        return self.result('read_tasks', **params.model_dump())


class WaitForWorkflowTasks(NodeTool):
    name = 'WaitForWorkflowTasks'
    description = ('When no other useful work remains, save this conversation and release its slot until the named '
        'work items finish. Automatically resumes with their actual results; no polling or repeated model requests.')
    params_model = TaskWaitParams

    async def execute(self, params):
        return self.result('wait_tasks', **params.model_dump())


class PublishWorkflowBlock(NodeTool):
    name = 'PublishWorkflowBlock'
    inline_schema_refs = True
    description = ('Choose the meaningful input or output content to visualize in this node. '
        'Submit text, table columns/rows, chart labels/series, diagram nodes/edges, or an image artifact_id/registered artifact_path. '
        'Reuse block_id to update a block. This only saves presentation; it does not complete work or invent results. '
        'Use genuine observations and distinguish proposals from actual outputs. No HTML or JavaScript.')
    params_model = PresentationBlock

    async def execute(self, params):
        return self.result('presentation', **params.model_dump())


def install(agent, ctx, on_result=None):
    forbidden = set(ctx.node.get('deny_tools') or [])
    classes = [SendNodeMessage, ReadNodeReplies, WaitForNodeReply, UseNodeDelivery, ListWorkflowNodes, ReadNodeCheckpoint]
    if ctx.node.get('config', {}).get('presentation'):
        classes.append(PublishWorkflowBlock)
    if ctx.node.get('config', {}).get('exploration_owner'):
        classes.extend((UpdateWorkflowTasks, ReadWorkflowTasks, WaitForWorkflowTasks))
    for cls in classes:
        if cls.name not in forbidden and '*' not in forbidden:
            agent.registry.register(cls(ctx, on_result))
