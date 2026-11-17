import asyncio
import json
import subprocess
from dataclasses import replace

import pytest

from mewcode.mcp.external_model import GrokCLIInvoker, ExternalModelError
from mewcode.teams.runtime_binding import GrokTeamExecutor, result_packet
from test_coord_worker import FakeInvoker
from test_team_runtime_v2 import runtime_case, review_candidate


class TeamInvoker(FakeInvoker):
    def data_only_preflight(self, cwd):
        pass

    async def invoke_async(self, request):
        self.data_only_preflight(request.cwd)
        result = await super().invoke_async(request)
        context = json.loads(request.prompt.split('\n', 1)[1])
        contract = context['contract']
        packet = dict(summary='Candidate only', artifacts=[dict(port_id=p['port_id'], schema_ref=p['schema_ref'],
                      media_type='application/octet-stream', content_b64='AAECA/8=') for p in contract['outputs']],
                      evidence=[dict(check_id='valid', status='observed', detail='Five exact bytes; host must inspect')])
        return replace(result, parsed_output=dict(result=json.dumps(packet), usage=dict(total_tokens=42)))


def test_fresh_grok_turn_only_has_bound_inputs(runtime_case, tmp_path):
    _, runtime, owner, worker, active, schema = runtime_case
    invoker = TeamInvoker()
    executor = GrokTeamExecutor(runtime, invoker, root=tmp_path, session_id='fresh')
    results = asyncio.run(executor.run_once())
    assert len(results) == 1 and results[0]['handoff_id']
    task = runtime.task(owner, active['task_ids']['produce'])
    assert task['state'] == 'verifying' and task['spent_tokens'] == 42
    request = invoker.requests[0]
    assert request.data_only and not request.allowed_tools and request.model_id == 'grok-4.6'
    context = json.loads(request.prompt.split('\n', 1)[1])
    assert 'handoffs' not in context and len(context['inputs']) == 1
    assert asyncio.run(executor.run_once()) == []
    review_candidate(runtime, owner, results[0])
    artifact = runtime.artifact(owner, results[0]['artifacts'][0]['version_id'])
    assert artifact['size'] == 5


def test_missing_safety_preflight_blocks_without_charging(runtime_case, tmp_path):
    runtime, owner, active = runtime_case[1], runtime_case[2], runtime_case[4]
    class Unavailable(TeamInvoker):
        def data_only_preflight(self, cwd):
            raise ExternalModelError('DATA_ONLY_UNAVAILABLE')
    invoker = Unavailable()
    result = asyncio.run(GrokTeamExecutor(runtime, invoker, root=tmp_path, session_id='fresh').run_once())[0]
    task = runtime.task(owner, active['task_ids']['produce'])
    assert result['reason'] == 'adapter_unavailable' and invoker.calls == 0
    assert task['held_tokens'] == task['spent_tokens'] == 0


def test_model_usage_claim_does_not_release_reservation(runtime_case, tmp_path):
    runtime, owner = runtime_case[1:3]
    invoker = TeamInvoker()
    result = asyncio.run(invoker.invoke_async(GrokTeamExecutor(runtime, invoker, root=tmp_path, session_id='x').request(runtime_case[3], runtime_case[4]['task_ids']['produce'])))
    body = json.loads(result.parsed_output['result'])
    body['usage'] = {'total_tokens': 0}
    assert result_packet(replace(result, parsed_output=body))[1] is None


def test_cli_safety_requires_empty_extension_manifest(tmp_path):
    calls = []
    def runner(command, **kwargs):
        calls.append(command)
        output = '--tools --deny --no-memory --disable-web-search --no-subagents' if '--help' in command else json.dumps(dict(rules=[], skills=[], hooks=[], plugins=[], mcp_servers=[]))
        return subprocess.CompletedProcess(command, 0, output, '')
    invoker = GrokCLIInvoker(runner=runner)
    invoker.data_only_preflight(tmp_path)
    assert len(calls) == 2
    invoker._runner = lambda command, **kwargs: subprocess.CompletedProcess(command, 0, '{}', '')
    with pytest.raises(ExternalModelError, match='DATA_ONLY_UNAVAILABLE'):
        invoker.data_only_preflight(tmp_path)


def test_real_adapter_uses_empty_cwd_outside_user_repository(tmp_path):
    from mewcode.mcp.external_model import ModelInvocationRequest
    calls = []
    def runner(command, **kwargs):
        context = __import__('pathlib').Path(kwargs['cwd'])
        assert context != tmp_path and context.name.startswith('mewcode-team-context-')
        assert list(context.iterdir()) == []
        calls.append((command, context))
        if '--help' in command:
            output = '--tools --deny --no-memory --disable-web-search --no-subagents'
        elif 'inspect' in command:
            output = json.dumps(dict(rules=[], skills=[], hooks=[], plugins=[], mcp_servers=[]))
        else:
            assert '--tools=' in command and '--no-memory' in command and '--no-subagents' in command
            output = '{"result":"{}"}'
        return subprocess.CompletedProcess(command, 0, output, '')
    invoker = GrokCLIInvoker(runner=runner, project_root=tmp_path)
    request = ModelInvocationRequest(task_id='task', agent_id='grok-executor-01', model_id='grok-4.6', prompt='Only supplied data', cwd=tmp_path, data_only=True)
    result = invoker.invoke(request)
    assert result.status.value == 'completed' and len(calls) == 3
    assert not calls[0][1].exists()
