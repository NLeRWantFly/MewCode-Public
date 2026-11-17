import asyncio
import json

import pytest

from mewcode.teams.execution_pool import ExecutionPool, ModelReply
from mewcode.teams.responsibility import MissionService
from mewcode.teams.runtime_protocol import ArtifactContent, HandoffSubmission
from mewcode.teams.store_v2 import TeamStateError
from mewcode.teams.tool_broker import ToolBroker
from test_team_deployment_v2 import deployment_case, deploy
from test_team_responsibility_v2 import initialize


def candidate(context):
    contract = context['contract']
    return json.dumps(dict(action='candidate', submission=HandoffSubmission(
        summary='Synthetic candidate for independent validation',
        artifacts=tuple(ArtifactContent.from_bytes(port_id=p['port_id'], schema_ref=p['schema_ref'], media_type='text/plain', content=b'fixture result')
                        for p in contract['outputs']),
        evidence=[dict(check_id=contract['acceptance'][0]['criterion_id'], status='observed', detail='Host must independently verify')]).model_dump()))


class SyntheticAdapter:
    def __init__(self, handler=None):
        self.contexts = []
        self.handler = handler

    async def complete(self, context, **options):
        self.contexts.append(context)
        return await self.handler(context) if self.handler else ModelReply(candidate(context), 20)


def pool(case, tmp_path, adapter):
    runtime, owner, registry = case[:3]
    result = ExecutionPool(runtime, ToolBroker(runtime, tmp_path))
    descriptor = registry._catalog(owner.project_id)['fixture/model-a']
    result.register(owner, descriptor, adapter, command_id='install-adapter')
    return result


@pytest.mark.asyncio
async def test_real_coroutines_overlap_with_isolated_same_model_contexts(deployment_case, tmp_path):
    runtime, owner = deployment_case[:2]
    _, active, _ = deploy(deployment_case)
    arrived, release = 0, asyncio.Event()
    async def both(context):
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            release.set()
        await release.wait()
        assert runtime.db.execute("SELECT COUNT(*) FROM team_v2_attempts WHERE state='running'").fetchone()[0] == 2
        await asyncio.sleep(0)
        return ModelReply(candidate(context), 20)
    adapter = SyntheticAdapter(both)
    executor = pool(deployment_case, tmp_path, adapter)
    result = await asyncio.wait_for(executor.run_ready(owner, active['team_run_id']), 8)
    assert len(result['attempts']) == 2
    assert {r['state'] for r in result['attempts']} == {'verifying'}
    assert len({c['attempt_id'] for c in adapter.contexts}) == 2
    assert all(c['exchanges'] == [] for c in adapter.contexts)
    assert all(c['task_id'] != adapter.contexts[1-i]['task_id'] for i, c in enumerate(adapter.contexts))
    assert runtime._runtime_run(active['team_run_id'])['state'] != 'completed'


@pytest.mark.asyncio
async def test_unknown_provider_usage_is_held_not_read_from_text(deployment_case, tmp_path):
    runtime, owner = deployment_case[:2]
    _, active, _ = deploy(deployment_case, dict(mode='fixed', candidates=['fixture/model-a']))
    async def unknown(context):
        return ModelReply(candidate(context), None)
    executor = pool(deployment_case, tmp_path, SyntheticAdapter(unknown))
    result = await executor.run_ready(owner, active['team_run_id'])
    task = runtime.task(owner, result['attempts'][0]['task_id'])
    assert task['held_tokens'] == 300 and task['spent_tokens'] == 0
    assert task['state'] == 'verifying'


@pytest.mark.asyncio
async def test_model_cannot_forge_usage_or_completion(deployment_case, tmp_path):
    runtime, owner = deployment_case[:2]
    _, active, _ = deploy(deployment_case, dict(mode='fixed', candidates=['fixture/model-a']))
    async def forged(context):
        return ModelReply(json.dumps(dict(action='completed', usage_tokens=0, passed=True)), 23)
    executor = pool(deployment_case, tmp_path, SyntheticAdapter(forged))
    result = await executor.run_ready(owner, active['team_run_id'])
    task = runtime.task(owner, result['attempts'][0]['task_id'])
    assert task['state'] == 'blocked' and task['spent_tokens'] == 23
    assert not task['handoffs']


@pytest.mark.asyncio
async def test_stale_response_cannot_replace_cancelled_work(deployment_case, tmp_path):
    runtime, owner = deployment_case[:2]
    _, active, _ = deploy(deployment_case, dict(mode='fixed', candidates=['fixture/model-a']))
    started, release = asyncio.Event(), asyncio.Event()
    async def slow(context):
        started.set()
        await release.wait()
        return ModelReply(candidate(context), 20)
    executor = pool(deployment_case, tmp_path, SyntheticAdapter(slow))
    pending = asyncio.create_task(executor.run_ready(owner, active['team_run_id']))
    await started.wait()
    runtime.cancel_run(owner, active['team_run_id'], control_epoch=1, reason='Explicit test cancellation', command_id='cancel')
    release.set()
    result = await pending
    assert result['attempts'][0]['state'] == 'discarded'
    assert not runtime.task(owner, active['task_ids']['produce'])['handoffs']


@pytest.mark.asyncio
async def test_missing_adapter_does_not_start_or_fallback(deployment_case, tmp_path):
    runtime, owner = deployment_case[:2]
    _, active, _ = deploy(deployment_case)
    executor = ExecutionPool(runtime, ToolBroker(runtime, tmp_path))
    result = await executor.run_ready(owner, active['team_run_id'])
    assert not result['attempts'] and len(result['unavailable']) == 2
    assert runtime.db.execute('SELECT COUNT(*) FROM team_v2_attempts').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_internal_question_is_not_an_automatic_user_escalation(deployment_case, tmp_path):
    runtime, owner = deployment_case[:2]
    _, active, _ = deploy(deployment_case, dict(mode='fixed', candidates=['fixture/model-a']))
    async def question(context):
        return ModelReply(json.dumps(dict(action='question', detail='Which exact source unit should I use?')), 20)
    executor = pool(deployment_case, tmp_path, SyntheticAdapter(question))
    result = await executor.run_ready(owner, active['team_run_id'])
    assert result['attempts'][0]['state'] == 'blocked'
    assert runtime.task(owner, active['task_ids']['produce'])['question'] is None
