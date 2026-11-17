import asyncio
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from mewcode.expert_group import ExpertExecutionPolicy, ExpertRoleResult
from mewcode.expert_runs import ExpertRunStore
from mewcode.experts.flow import ExpertFlowSpec
from mewcode.experts.waits import BranchWaitPaused
from mewcode.kernel.escalation import KernelScheduler
from mewcode.kernel.events import EventJournal
from mewcode.kernel.migration import migrate


@pytest.mark.asyncio
async def test_resource_boundary_stops_without_spending_recovery_opportunities(tmp_path):
    store, run_id = ExpertRunStore(tmp_path / 'runs'), 'd' * 32
    store.create(run_id=run_id, conversation_id='fixture', task='fixture', attachments=[], profile={}, strategy='stable')
    before = store.load(run_id)
    handler = KernelScheduler(session=SimpleNamespace(), registry=None, store=store, run_id=run_id,
        emit=None, before_resume=lambda: asyncio.sleep(0), task='fixture')
    decision = await handler(node=SimpleNamespace(node_id='N03'), result=ExpertRoleResult('fixture'),
        policy=ExpertExecutionPolicy('stable', False, 0), allowed=['continue_checkpoint'],
        host_facts={'hard_boundary': '全局运行时限已耗尽'})
    assert decision['action'] == 'stop' and decision['source'] == 'resource_boundary'
    assert store.load(run_id) == before


@pytest.mark.asyncio
async def test_sanitized_n03_reaches_human_within_six_host_attempts_and_replay_is_identical(tmp_path):
    trace = json.loads((Path(__file__).parent / 'fixtures/paradigm/capability_missing_scheduler_hold.json').read_text(encoding='utf-8'))
    source = {'run_id': trace['source']['run_snapshot']['run_id'], 'conversation_id': 'fixture',
              'status': 'paused', 'locked_flow': trace['locked_flow'], 'node_states': copy.deepcopy(trace['node_states'])}
    store = ExpertRunStore(tmp_path / 'runs')
    record = migrate(store, source, workspace=str(tmp_path))
    run_id, seen = record['run_id'], []
    session = SimpleNamespace(work_dir=str(tmp_path))
    async def emit(event):
        if event['type'] == 'ask_user':
            seen.append(event)
            session._ask_user_request.future.set_result({'direction': 'pause'})
    async def catalog(**kwargs):
        return {'models': []}
    handler = KernelScheduler(session=session, registry=SimpleNamespace(catalog=catalog), store=store,
        run_id=run_id, emit=emit, before_resume=lambda: asyncio.sleep(0), task='sanitized fixture')
    flow = ExpertFlowSpec.from_value(trace['locked_flow'])
    for attempt in range(1, 7):
        store.mutate(run_id, lambda value, count=attempt: value['node_states']['N03'].update(node_attempt=count))
        try:
            await handler(node=flow.node_map['N03'], result=ExpertRoleResult('fixture', status='needs_attention',
                error_kind='capability_missing'), policy=ExpertExecutionPolicy('stable', False, 0),
                allowed=['continue_checkpoint', 'diagnose_path'], host_facts={'trigger': 'capability_missing'})
        except BranchWaitPaused:
            break
    else:
        pytest.fail('N03 exceeded six bounded host attempts')
    assert seen and attempt <= 6
    first = EventJournal(store.root, run_id).load()
    assert first == EventJournal(store.root, run_id).load()
    assert not any(row.get('state') == 'unknown' for row in first.get('kernel_calls', {}).values())


@pytest.mark.asyncio
async def test_killed_writer_preserves_fsynced_events_and_definite_receipt_prevents_duplicate_execution(tmp_path):
    code = '''
import sys,time
from pathlib import Path
from mewcode.kernel.events import EventJournal
from mewcode.kernel.calls import propose,transition
root=Path(sys.argv[1]); journal=EventJournal(root,'a'*32)
state={'node_states':{'N03':{}},'run_id':'a'*32}
row=propose(state,'N03','revision',{'tool':'Bash','arguments':{'command':'echo fixture'}})
transition(row,'reviewed');transition(row,'dispatched',evidence={'executor_accepted':True})
transition(row,'succeeded');row['result']={'output':'definite fixture','is_error':False}
journal.commit(state)
def partial(target,data):
    (journal.directory/'.pending-killed-test').write_bytes(b'{partial')
    print('mid-write',flush=True)
    time.sleep(3600)
journal._append_file=partial
state['unfinished_change']=True
journal.commit(state)
'''
    process = subprocess.Popen([sys.executable, '-c', code, str(tmp_path)], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env={**os.environ, 'MEWCODE_USER_DATA_DIR': str(tmp_path / 'isolated')},
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=1) as pool:
            try:
                assert pool.submit(process.stdout.readline).result(timeout=30).strip() == 'mid-write'
            except BaseException:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)
                raise
        process.kill()
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
        process.stdout.close()
        process.stderr.close()
    journal = EventJournal(tmp_path, 'a' * 32)
    state = journal.load()
    assert state['kernel_revision'] == 1 and 'unfinished_change' not in state
    from unittest.mock import AsyncMock
    from mewcode.agent import Agent
    from mewcode.kernel.calls import CallGateway
    class JournalStore:
        def load(self, run_id):
            return journal.load()
        def mutate(self, run_id, callback, **kwargs):
            current = journal.load()
            callback(current)
            journal.commit(current)
            return current
    agent = Agent.__new__(Agent)
    agent._safety_call_ref = lambda *args: 'fixture'
    agent._prepare_rollback_evidence = lambda *args: None
    agent.kernel_gateway = CallGateway(JournalStore(), 'a' * 32, 'N03', 'revision')
    tool = SimpleNamespace(category='command', run=AsyncMock())
    tc = SimpleNamespace(tool_name='Bash', arguments={'command': 'echo fixture'}, tool_id='restarted')
    result = await agent._run_recorded_tool(tool, {}, tc)
    assert result.output.startswith('KERNEL_RECEIPT_REUSED:')
    assert result.output.endswith('definite fixture')
    assert result.host_observation['reused'] is True
    assert result.host_observation['command_execution']['command_started'] is False
    assert result.host_observation['kernel_receipt_reuse']['call_id'] == next(iter(state['kernel_calls']))
    tool.run.assert_not_awaited()
