"""Real process death at each durable design-to-run startup boundary."""
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import AsyncMock

import httpx
import pytest

from frontend import server
from mewcode.expert_runs import ExpertRunStore
from mewcode.orchestration.task_design import TaskDesign
from tests.test_task_design_entry import task_api_env


CRASH_WORKER = r'''
import json, os, sys
from pathlib import Path
from mewcode.expert_runs import ExpertRunStore
from mewcode.experts.flow import ExpertFlowSpec
from mewcode.orchestration.task_design import TaskDesign
from tests.test_collaboration_mode import flow, PROFILE
project, cid, store_path, phase, receipt_path = sys.argv[1:]
design = TaskDesign(project, cid)
created = design.start('execute', orchestration_mode='collaborative')
ready = design.transition(created, 'ready', build_request={'id': 'build', 'epoch': 1, 'state': 'saved'})
claim = design.claim_execution(ready)
run_id = design.execution_run_id(claim)
Path(receipt_path).write_text(json.dumps({'run_id': run_id, 'claim': claim}), encoding='utf-8')
if phase == 'after_claim':
    os._exit(73)
raw = flow().as_dict()
raw['meta']['task_design'] = {'task_id': claim['task_id'], 'orchestration_mode': 'collaborative'}
raw['meta']['collaboration_spec']['task_id'] = claim['task_id']
store = ExpertRunStore(store_path)
store.create(run_id=run_id, conversation_id=cid, task='交付测试结果', attachments=[],
             profile=PROFILE.as_dict(), strategy='stable', flow=ExpertFlowSpec.from_value(raw))
if phase == 'after_run':
    os._exit(73)
design.bind_run(task_id=claim['task_id'], claim_id=claim['build_request']['execution_claim_id'], run_id=run_id)
os._exit(73)
'''


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['after_claim', 'after_run', 'after_bind'])
async def test_crash_recovers_one_binding_without_dispatch(task_api_env, tmp_path, monkeypatch, phase):
    _, _, _, owner, _ = task_api_env
    runs = tmp_path / 'crash-runs'
    receipt = tmp_path / 'crash.json'
    process = subprocess.run([sys.executable, '-c', CRASH_WORKER, owner['project'],
        owner['conversation_id'], str(runs), phase, str(receipt)],
        cwd=Path(__file__).resolve().parents[1], env=dict(os.environ, MEWCODE_OFFLINE_TEST='1'),
        capture_output=True, timeout=40, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    assert process.returncode == 73, process.stderr.decode(errors='replace')
    claimed = json.loads(receipt.read_text(encoding='utf-8'))
    store = ExpertRunStore(runs)
    monkeypatch.setattr(server, 'EXPERT_RUNS', store)
    monkeypatch.setattr(server.SESSION, 'chat_busy', lambda *_: False)
    business = AsyncMock(side_effect=AssertionError('read recovery cannot execute business'))
    monkeypatch.setattr(server, 'run_expert_chat', business)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        first = await client.get('/api/orchestration/task-design', params=owner)
        assert first.status_code == 200, first.text
        again = await client.get('/api/orchestration/task-design', params=owner)
        assert again.status_code == 200, again.text
    current = TaskDesign(owner['project'], owner['conversation_id']).read()
    assert current['status'] == 'paused' and current['intent'] == 'execute'
    assert again.json()['task_design']['revision'] == first.json()['task_design']['revision']
    records = store.list_for_conversation(owner['conversation_id'])
    if phase == 'after_claim':
        assert records == [] and not current.get('run_id')
        assert current['build_request']['execution_not_started']['claim_id'] == claimed['claim']['build_request']['execution_claim_id']
        resumed = TaskDesign(owner['project'], owner['conversation_id']).control('resume',
            task_id=current['task_id'], revision=current['revision'])
        assert resumed['status'] == 'ready'
        reclaimed = TaskDesign(owner['project'], owner['conversation_id']).claim_execution(resumed)
        assert TaskDesign.execution_run_id(reclaimed) == claimed['run_id']
    else:
        assert len(records) == 1 and current['run_id'] == claimed['run_id']
        record = store.load(claimed['run_id'])
        assert record['status'] == 'paused'
        assert not record['collaboration']['attempts'] and not record.get('kernel_calls')
    business.assert_not_called()

    # Explicit continuation finishes the recovered binding, then the real
    # scheduler executes each contract once. Reload never starts a second run.
    from mewcode.expert_group import ExpertGroupOrchestrator
    from mewcode.experts.flow import ExpertFlowSpec
    from tests.test_collaboration_mode import CATALOG, PROFILE, REVIEWER, EXECUTOR, delivery
    design = TaskDesign(owner['project'], owner['conversation_id'])
    if not records:
        from tests.test_collaboration_mode import flow
        raw = flow().as_dict()
        raw['meta']['task_design'] = {'task_id': current['task_id'], 'orchestration_mode': 'collaborative'}
        raw['meta']['collaboration_spec']['task_id'] = current['task_id']
        selected = ExpertFlowSpec.from_value(raw)
        store.create(run_id=claimed['run_id'], conversation_id=owner['conversation_id'], task='交付测试结果',
            attachments=[], profile=PROFILE.as_dict(), strategy='stable', flow=selected)
        bound = design.bind_run(task_id=current['task_id'],
            claim_id=reclaimed['build_request']['execution_claim_id'], run_id=claimed['run_id'])
    else:
        selected = ExpertFlowSpec.from_value(store.load(claimed['run_id'])['locked_flow'])
        bound = design.control('resume', task_id=current['task_id'], revision=current['revision'])
        store.resume(claimed['run_id'], strategy='stable')
    design.transition(bound, 'running')
    store.mutate(claimed['run_id'], lambda row: row.update(role_models={'roles': {
        'verifier': REVIEWER.as_dict(), 'reviewer': REVIEWER.as_dict(),
        'executor': EXECUTOR.as_dict(), 'planner': EXECUTOR.as_dict()}}), durable=True)
    executed = []
    async def runner(node, *args):
        executed.append(node.node_id)
        return delivery()
    async def review(*args):
        return {'passed': True, 'evidence': ['固定合成交付满足原始目标']}
    host = ExpertGroupOrchestrator(runner=None, node_runner=runner, run_store=store,
        work_dir=tmp_path, archive_root=tmp_path / 'archive', collaboration_review=review)
    for _ in range(2):
        result = await host.run('交付测试结果', PROFILE, CATALOG, strategy='stable', run_id=claimed['run_id'],
            flow=selected, checkpoint=store.load(claimed['run_id']))
        assert result.status == 'completed'
    assert executed == ['work', 'deliver']
    assert len(store.list_for_conversation(owner['conversation_id'])) == 1
