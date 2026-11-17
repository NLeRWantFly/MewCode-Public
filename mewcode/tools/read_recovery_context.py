import asyncio
from typing import Literal
from pydantic import BaseModel, Field
from mewcode.experts.recovery_context import context_sections, page_context, scoped_record
from mewcode.tools.base import Tool, ToolResult


class ReadRecoveryContextParams(BaseModel):
    node_id: str = Field(default='', description='Empty or the bound ORIGINAL node ID; no unrelated nodes or runs.')
    section: Literal['summary', 'authorizations', 'observations', 'commands', 'accounting', 'blockers', 'tools', 'model'] = 'summary'
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=8000, ge=1, le=9000)


class ReadRecoveryContext(Tool):
    name = 'ReadRecoveryContext'
    description = 'Read focused host facts for the original recovery node: actual observations, current grants, model binding and UNKNOWN legacy usage. It does not invoke models or retry actions. Lossless pagination.'
    params_model = ReadRecoveryContextParams
    category = 'read'
    consumes_runtime_slot = False

    def __init__(self, read_record, run_id, node_id, *, epoch=None, input_revision=None, section_reader=None):
        self.read_record, self.run_id, self.node_id = read_record, run_id, node_id
        self.epoch, self.input_revision = epoch, input_revision
        self.section_reader = section_reader

    def record(self, node_id):
        if node_id and node_id != self.node_id:
            raise ValueError('只能读取本恢复调用绑定的原节点')
        return scoped_record(self.read_record, self.run_id, self.node_id, self.epoch, self.input_revision)

    async def execute(self, params):
        try:
            if params.node_id and params.node_id != self.node_id:
                raise ValueError('只能读取本恢复调用绑定的原节点')
            if self.section_reader is not None:
                value = await asyncio.to_thread(self.section_reader, self.run_id, self.node_id, params.section,
                                               epoch=self.epoch, input_revision=self.input_revision)
            else:
                value = context_sections(self.record(params.node_id), self.node_id, section=params.section)[params.section]
            return ToolResult(page_context(value, params.offset, params.limit))
        except ValueError as exc:
            return ToolResult(str(exc), is_error=True)
