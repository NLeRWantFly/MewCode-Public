"""Workbench preparation preserves the original host source and explicit action."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from frontend import workbench_answers
from frontend.prompt_dispatch import PromptError


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['ask_user', 'human_oncall', 'run_inputs', 'recovery_case', 'design_choice'])
async def test_scoped_prepare_uses_host_basis_and_never_answers(monkeypatch, kind):
    app = FastAPI()
    card = dict(id='card', type=kind, task_id='task', run_id='' if kind == 'design_choice' else 'run',
                context=dict(request_id='request', node_id='step', case_id='case', pending={}, choice_bundle_id='bundle', candidate_id='candidate', sequence=4),
                choice_bundle=dict(questions=[dict(choices=[dict(kind='action', action='research')])]))
    cached = dict(bundle=dict(id='bundle'), spec={'meta': {'task_design': {'task_id': 'task'}}})
    def scope(body):
        if body.get('conversation_id') != 'c' or body.get('task_id') != 'task':
            raise PromptError()
        return ('project', 'c')
    host = SimpleNamespace(srv=SimpleNamespace(app=app), scope=scope, view=lambda _: dict(inbox=[card]), startup_choices=lambda *_: [cached])
    workbench_answers.install_answers(app, host)
    route = AsyncMock(return_value={'ok': True})
    monkeypatch.setattr(workbench_answers, 'route', route)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
        data = dict(conversation_id='c', task_id='task', run_id='forged', request_id='forged', spec={'forged': True},
                    action='research', input=['保留已有材料', '补充来源'])
        response = await client.post('/api/workbench/inbox/card/prepare', json=data, headers={'X-Mew-Workbench': '1'})
        assert response.status_code == 200
        values = route.call_args.args[3]
        assert values['request_id'] == ('startup' if kind == 'run_inputs' else 'case' if kind == 'recovery_case' else 'graph-choices' if kind == 'design_choice' else 'request')
        assert values['run_id'] == ('' if kind in {'run_inputs', 'design_choice'} else 'run')
        if kind == 'design_choice':
            assert (values['candidate_id'], values['sequence']) == ('candidate', 4)
        assert values['action_input'] == '保留已有材料\n补充来源'
        assert values.get('spec') == (cached['spec'] if kind == 'run_inputs' else None)
        assert route.call_args.args[1] == '/api/orchestration/interaction-choices/prepare'
        for change, headers, status in [({}, {}, 403), ({'conversation_id': 'other'}, {'X-Mew-Workbench': '1'}, 409),
                                        ({'action': 'configure'}, {'X-Mew-Workbench': '1'}, 400),
                                        ({}, {'X-Mew-Workbench': '1', 'Origin': 'https://foreign.invalid'}, 403)]:
            response = await client.post('/api/workbench/inbox/card/prepare', json={**data, **change}, headers=headers)
            assert response.status_code == status
        assert route.await_count == 1
