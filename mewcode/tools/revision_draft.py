"""Main-chat-only proposal tool. It has no confirmation or execution operation."""
import json

from pydantic import BaseModel, ConfigDict, Field

from .base import Tool, ToolResult, ToolRiskProfile


class RevisionDraftParams(BaseModel):
    model_config = ConfigDict(extra='forbid')
    message_id: str = Field(min_length=1, max_length=100)


class SubmitRevisionDraft(Tool):
    name = 'SubmitRevisionDraft'
    description = ('Submit an existing user message as a proposed change to the current collaborative task. '
        'Use a user message source ID from ReadWorkflowBlocks. The host coordinator prepares a draft for explicit '
        'user confirmation in the task workbench. This does not adopt requirements or grant operation permission.')
    params_model = RevisionDraftParams
    category = 'harness'

    def __init__(self, submit):
        self.submit = submit

    def permission_risk(self, arguments):
        return ToolRiskProfile(side_effect='workspace', network=True, cost=True, reversible=True, target_type='model')

    async def execute(self, params):
        try:
            draft = await self.submit(params.message_id)
            return ToolResult(json.dumps(dict(draft=draft, requirements_changed=False), ensure_ascii=False))
        except (ValueError, OSError, TypeError, RuntimeError) as exc:
            return ToolResult(str(exc), is_error=True)
