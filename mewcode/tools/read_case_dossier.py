import json
from pydantic import BaseModel, Field
from mewcode.tools.base import Tool, ToolResult
from mewcode.permissions.reviewer import redact_sensitive


class ReadCaseDossierParams(BaseModel):
    section: str = ''
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=8000, ge=1, le=9000)


class ReadCaseDossier(Tool):
    name = 'ReadCaseDossier'
    description = 'Read the frozen host dossier of this recovery case with lossless character pagination.'
    category = 'read'
    params_model = ReadCaseDossierParams
    consumes_runtime_slot = False

    def __init__(self, dossier):
        self.dossier = dossier
        self.description += (' Available sections: ' + ', '.join(dossier) +
            '. Start with blockers, command_receipts or rounds; progress contains the full business history.')

    async def execute(self, params):
        if params.section and params.section not in self.dossier:
            return ToolResult('档案分节不存在；可用分节：' + ', '.join(self.dossier), is_error=True)
        text = redact_sensitive(json.dumps(self.dossier.get(params.section) if params.section else self.dossier, ensure_ascii=False))
        end = min(len(text), params.offset + params.limit)
        return ToolResult(f'TOTAL_CHARS: {len(text)}\nNEXT_OFFSET: {end}\n' + text[params.offset:end])
