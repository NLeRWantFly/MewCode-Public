"""Read latency must not scale with unrelated execution histories."""
import copy
import time
from types import SimpleNamespace

import pytest

from mewcode.agent import Agent, ToolExecutionStartEvent, ToolResultEvent
from mewcode.conversation import ConversationManager
from mewcode.expert_runs import ExpertRunStore
from mewcode.experts.recovery_context import context_sections
from mewcode.kernel.sqlite_state import RunDatabase
from mewcode.tools import create_default_registry
from mewcode.tools.base import StreamEnd, TextDelta, ToolCallComplete, ToolResult
from mewcode.tools.read_recovery_context import ReadRecoveryContext, ReadRecoveryContextParams
from tests.test_agent import MockLLMClient
from tests.test_recovery_context_tools import seed
from tests.test_recovery_case_e2e import harness, RUN


class UnrelatedHistory(list):
    def __deepcopy__(self, memo):
        raise AssertionError('read/checkpoint copied unrelated execution history')


@pytest.mark.asyncio
@pytest.mark.parametrize('count', [1, 2])
async def test_execution_clock_excludes_durable_start_callback(monkeypatch, tmp_path, count):
    now = [100.0]
    monkeypatch.setattr('mewcode.agent.time', SimpleNamespace(monotonic=lambda: now[0], time=time.time))
    client = MockLLMClient([
        [*[ToolCallComplete(str(i), 'ReadFile', {'file_path': 'missing.txt'}) for i in range(count)],
         StreamEnd('tool_use', input_tokens=1, output_tokens=1)],
        [TextDelta('done'), StreamEnd('end_turn', input_tokens=1, output_tokens=1)],
    ])
    agent = Agent(client, create_default_registry(), 'anthropic', work_dir=str(tmp_path))
    async def execute(*args):
        now[0] += 0.5
        return ToolResult('read')
    monkeypatch.setattr(agent, '_run_recorded_tool', execute)
    conversation = ConversationManager()
    conversation.add_user_message('read')
    results = []
    async for event in agent.run(conversation):
        if isinstance(event, ToolExecutionStartEvent):
            now[0] += 27.0  # Durable host callback, before the actual tool.
        if isinstance(event, ToolResultEvent):
            results.append(event)
    assert len(results) == count
    assert all(row.elapsed == 0.5 and row.dispatch_seconds == 27.0 for row in results)


def new_store(tmp_path):
    store = ExpertRunStore(tmp_path, storage='sqlite')
    run = 'e' * 32
    store.create(run_id=run, conversation_id='chat', task='read', attachments=[], profile={}, strategy='stable')
    return store, run


def test_unchanged_stream_does_not_copy_or_commit_history(tmp_path):
    store, run = new_store(tmp_path)
    args = dict(text='text', reasoning='', messages=[{'role': 'user', 'content': 'same'}],
                children={}, safety_review_state={}, return_snapshot=False)
    store.flush_stream(run, 'executor', **args)
    state = store._live[run]
    revision = state['kernel_revision']
    state['kernel_attempt_archives'] = UnrelatedHistory(range(5000))
    assert store.flush_stream(run, 'executor', **args) == {}
    assert store._live[run] is state and state['kernel_revision'] == revision
    assert RunDatabase(tmp_path, run).revision()[0] == revision


def test_stream_change_and_json_type_change_are_persisted(tmp_path):
    store, run = new_store(tmp_path)
    args = dict(text='text', reasoning='', messages=[{'flag': True}], return_snapshot=False)
    store.flush_stream(run, 'executor', **args)
    revision = store._live[run]['kernel_revision']
    store.flush_stream(run, 'executor', **dict(args, messages=[{'flag': 1}]))
    assert store._live[run]['kernel_revision'] == revision + 1
    persisted = RunDatabase(tmp_path, run).load()['node_states']['executor']['messages']
    assert type(persisted[0]['flag']) is int


def test_unchanged_tool_review_does_not_copy_or_commit_history(tmp_path):
    store, run = new_store(tmp_path)
    store.record_reviewed_tool_start(run, 'executor', 'first', 'ReadFile', {'file_path': 'a'})
    state = store._live[run]
    revision = state['kernel_revision']
    state['kernel_attempt_archives'] = UnrelatedHistory(range(5000))
    state['node_states']['executor']['messages'] = UnrelatedHistory(range(5000))
    store.record_reviewed_tool_start(run, 'executor', 'second', 'ReadFile', {'file_path': 'b'})
    assert store._live[run] is state and state['kernel_revision'] == revision


@pytest.mark.asyncio
async def test_scoped_sections_match_full_evidence_without_copying_history(tmp_path):
    h = harness(tmp_path)
    seed(h)
    baseline = h.store.load(RUN)
    expected = context_sections(baseline, 'work')
    with h.store._lock:
        h.store._enqueue_write(RUN, durable=True)
    state = h.store._live[RUN]
    before_ledger = copy.deepcopy(state['node_ledgers'])
    state['kernel_attempt_archives'] = UnrelatedHistory(range(5000))
    state['node_states']['work']['messages'] = UnrelatedHistory(range(5000))
    for section, value in expected.items():
        actual = h.store.read_recovery_section(RUN, 'work', section, epoch=0, input_revision='rev')
        assert actual == value
    def forbidden():
        raise AssertionError('full reader must not run')
    read = ReadRecoveryContext(forbidden, RUN, 'work', epoch=0, input_revision='rev',
                               section_reader=h.store.read_recovery_section)
    assert not (await read.execute(ReadRecoveryContextParams(section='commands'))).is_error
    assert (await read.execute(ReadRecoveryContextParams(node_id='foreign'))).is_error
    with pytest.raises(ValueError, match='代际'):
        h.store.read_recovery_section(RUN, 'work', 'summary', epoch=1, input_revision='rev')
    with pytest.raises(ValueError, match='输入'):
        h.store.read_recovery_section(RUN, 'work', 'summary', epoch=0, input_revision='changed')
    assert state['node_ledgers'] == before_ledger


def test_timing_receipt_survives_sql_reload(tmp_path):
    store, run = new_store(tmp_path)
    store.append_sequenced_events(run, [dict(type='expert_tool_result', seq=1, node_id='executor',
        id='read', name='ReadFile', is_error=False, output='ok', elapsed=0.01, dispatch_seconds=27.0)])
    tool = RunDatabase(tmp_path, run).load()['node_states']['executor']['tools']['read']
    assert tool['execution_seconds'] == 0.01 and tool['dispatch_seconds'] == 27.0


def test_superseded_stream_cannot_overwrite_a_newer_checkpoint(tmp_path):
    store, run = new_store(tmp_path)
    store.flush_stream(run, 'executor', text='final', reasoning='', messages=['final'], return_snapshot=False)
    state = store._live[run]
    revision = state['kernel_revision']
    store.flush_stream(run, 'executor', text='stale', reasoning='', messages=['stale'],
                       return_snapshot=False, is_current=lambda: False)
    assert state['kernel_revision'] == revision
    assert state['node_states']['executor']['messages'] == ['final']


def test_review_start_still_persists_matching_clearance_only(tmp_path):
    from mewcode.experts.node_ledger import ledger_for_run
    store, run = new_store(tmp_path)
    def prepare(record):
        record['node_states']['executor'] = {'tools': {
            'old': {'name': 'Bash', 'args': {'command': 'git status --short'}, 'status': 'failed'},
            'different': {'name': 'Bash', 'args': {'command': 'git push'}, 'status': 'failed'}}}
        ledger = ledger_for_run(record)
        for kind, ref in [('legacy_safety_review', 'old'), ('legacy_safety_review', 'different'), ('effect_unknown', 'old')]:
            ledger.record('executor', 'blocker', {'kind': kind, 'ref': ref})
    store.mutate(run, prepare)
    store.record_reviewed_tool_start(run, 'executor', 'new', 'Bash', {'command': 'git status --short'})
    persisted = RunDatabase(tmp_path, run).load()
    assert {(b.payload['kind'], b.payload['ref']) for b in ledger_for_run(persisted).view('executor').active_blockers} == {
        ('safety_review', 'different'), ('unknown_effect', 'old')}


def test_commit_cache_is_read_only_and_public_loads_remain_detached(tmp_path):
    from mewcode.kernel.events import EventJournal
    journal = EventJournal(tmp_path, 'a' * 32, storage='sqlite')
    state = {'data': {'items': ['original']}}
    journal.commit(state)
    borrowed = journal._sql_load(detached=False)
    state['data']['items'].append('next')
    journal.commit(state)
    assert borrowed['data']['items'] == ['original']
    public = journal.load()
    public['data']['items'].append('caller-only')
    assert journal.load()['data']['items'] == ['original', 'next']
