import json
from pydantic import BaseModel, Field
from mewcode.tools.base import Tool, ToolResult
from mewcode.experts.authorization_ledger import entries
from mewcode.experts.visual_authorization_usage import authorization_accounts


class ReadAuthorizationsParams(BaseModel):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=50)
    decision_id: str = Field(default='', description='Read an exact host user decision, including its full original questions and answer.')
    text_offset: int = Field(default=0, ge=0)
    text_limit: int = Field(default=6000, ge=1, le=7000)


class ReadAuthorizations(Tool):
    name = 'ReadAuthorizations'
    description = ('Page host-verified grants, statements and denials with visual quota usage, reservations and '
                   'unknown calls, plus host-recorded user decisions with their original scope and freshness. '
                   'Use decision_id and text_offset to read a complete decision. '
                   'Detailed accounting is pageable via ReadRecoveryContext. Decisions and statements are not permission.')
    category = 'read'
    params_model = ReadAuthorizationsParams
    consumes_runtime_slot = False

    def __init__(self, read_record, grant_store=None):
        self.read_record, self.grant_store = read_record, grant_store

    async def execute(self, params):
        record = self.read_record()
        if params.decision_id:
            from mewcode.orchestration.ask_gateway import decision_freshness
            from mewcode.experts.user_decisions import public_decision
            row = (record.get('collaboration') or {}).get('questions', {}).get(params.decision_id)
            if not row or row.get('status') != 'answered' or row.get('answer_source') != 'user':
                return ToolResult('没有该宿主用户决定。', is_error=True)
            body = json.dumps({**public_decision(row), 'freshness': decision_freshness(record, row), 'enforceable': False}, ensure_ascii=False, sort_keys=True)
            end = min(len(body), params.text_offset + params.text_limit)
            def page(last):
                return json.dumps({'decision_id': params.decision_id, 'total_chars': len(body),
                    'next_offset': last, 'text': body[params.text_offset:last]}, ensure_ascii=False)
            output = page(end)
            while len(output) > 9000:
                end -= max(1, (len(output) - 9000 + 1) // 2)
                output = page(end)
            return ToolResult(output)
        accounts = {row['auth_id']: {
            **{key: value for key, value in row.items() if key not in {'calls', 'legacy_receipts', 'unknown_call_ids'}},
            'unknown_call_count': len(row['unknown_call_ids']), 'details_tool': 'ReadRecoveryContext(section=accounting)'}
            for row in authorization_accounts(record, self.grant_store)}
        rows = entries(record, self.grant_store)
        from mewcode.experts.user_decisions import decisions
        rows = decisions(record) + rows
        for row in rows:
            if row['auth_id'] in accounts:
                row['accounting'] = accounts[row['auth_id']]
        selected = []
        for row in rows[params.offset:params.offset + params.limit]:
            if len(json.dumps(selected + [row], ensure_ascii=False)) > 9000:
                break
            selected.append(row)
        return ToolResult(json.dumps({'total': len(rows), 'next_offset': params.offset + len(selected), 'entries': selected}, ensure_ascii=False))
