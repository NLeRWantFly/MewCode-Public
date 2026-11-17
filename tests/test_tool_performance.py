"""Compatibility and durability boundaries for the tool hot path."""
import asyncio
import copy
import hashlib
import json
import re
import shutil
import threading
from pathlib import Path

import pytest

from mewcode.cache import FileCache
from mewcode.expert_runs import ExpertRunStore
from mewcode.kernel import events, run_views
from mewcode.kernel.async_commit import durable_call
from mewcode.kernel.sqlite_state import RunDatabase
from mewcode.runtime_workers import io_call
from mewcode.tools import grep as grep_module
from mewcode.tools import read_file as read_module
from mewcode.tools.file_state_cache import FileStateCache

RUN = 'a' * 32


def running_store(root, *, status='running'):
    store = ExpertRunStore(root, storage='sqlite')
    store.create(run_id=RUN, conversation_id='chat', task='read files', attachments=[],
                 profile={}, strategy='stable')
    def seed(row):
        row['status'] = status
        row['node_states'].setdefault('executor', {}).update(status='running', tools={})
    store.mutate(RUN, seed)
    return store


def receipt(seq, kind='expert_tool', **fields):
    return dict(seq=seq, type=kind, node_id='executor', role='executor', id='t',
                name='ReadFile', args={'file_path': 'one.txt'}, **fields)


def assert_views_match(root):
    db = RunDatabase(root, RUN)
    state = db.load()
    expected = run_views.build(state)
    with db.connection() as connection:
        for space in expected:
            assert db._read(connection, space) == expected[space], space
    return state


@pytest.mark.parametrize('status', ['running', 'paused'])
def test_tool_event_views_match_full_rebuild_and_keep_receipts(tmp_path, status):
    store = running_store(tmp_path, status=status)
    store.append_sequenced_events(RUN, [dict(seq=i, type='expert_step', label=str(i))
                                       for i in range(1, 58)], return_snapshot=False)
    for event in [receipt(58), receipt(59, 'expert_tool_start', started_at=1),
                  receipt(60, 'expert_tool_result', completed_at=2, output='1\tone',
                          is_error=False, host_observation={'file_state': {'text_sha256': 'proof'}})]:
        store.append_sequenced_events(RUN, [event], return_snapshot=False)
        state = assert_views_match(tmp_path)
    assert state['node_states']['executor']['tools']['t']['status'] == 'completed'
    assert state['stages'] == state['node_states']
    assert store.history_page(RUN, limit=100)['source_revision'] == state['kernel_revision']
    # A late duplicate start must not reopen a completed operation.
    store.append_sequenced_events(RUN, [receipt(61, 'expert_tool_start', started_at=3)], return_snapshot=False)
    assert RunDatabase(tmp_path, RUN).load()['node_states']['executor']['tools']['t']['status'] == 'completed'


def test_incremental_commit_uses_committed_tree_and_rolls_back_atomically(tmp_path, monkeypatch):
    store = running_store(tmp_path)
    external_tools = {}
    store.mutate(RUN, lambda row: row['node_states']['executor'].update(tools=external_tools))
    external_tools['uncommitted'] = {'output': 'must not become a receipt'}
    before = RunDatabase(tmp_path, RUN).load()
    original = RunDatabase._models
    def fail(*args, **kwargs):
        raise OSError('injected write failure')
    monkeypatch.setattr(RunDatabase, '_models', fail)
    with pytest.raises(OSError, match='injected'):
        store.append_sequenced_events(RUN, [receipt(1)], return_snapshot=False)
    assert RunDatabase(tmp_path, RUN).load() == before
    assert events.EventJournal(tmp_path, RUN)._sql_load() == before
    monkeypatch.setattr(RunDatabase, '_models', original)
    store.append_sequenced_events(RUN, [receipt(1)], return_snapshot=False)
    state = assert_views_match(tmp_path)
    assert 't' in state['node_states']['executor']['tools']
    assert 'uncommitted' not in state['node_states']['executor']['tools']
    external_tools['after_commit'] = {'output': 'still private'}
    store._live[RUN]['node_states']['executor']['tools']['t']['args']['file_path'] = 'changed locally'
    assert events.EventJournal(tmp_path, RUN)._sql_load() == state
    assert RunDatabase(tmp_path, RUN).load() == state
    count = state['kernel_revision']
    store.append_sequenced_events(RUN, [receipt(1)], return_snapshot=False)
    assert RunDatabase(tmp_path, RUN).revision()[0] == count


def test_warm_tool_commit_does_not_copy_historical_context_or_rebuild_views(tmp_path, monkeypatch):
    store = running_store(tmp_path)
    class Uncopyable(list):
        def __deepcopy__(self, memo):
            raise AssertionError('historical context was copied')
    committed = events.EventJournal(tmp_path, RUN)._sql_load(detached=False)
    committed['node_states']['executor']['messages'] = Uncopyable()
    store._live[RUN]['node_states']['executor']['messages'] = Uncopyable()
    def forbidden(*args):
        raise AssertionError('full display rebuild')
    monkeypatch.setattr(run_views, 'build', forbidden)
    store.append_sequenced_events(RUN, [receipt(1)], return_snapshot=False)
    assert RunDatabase(tmp_path, RUN).load()['node_states']['executor']['tools']['t']['status'] == 'queued'


def test_cold_tool_commit_updates_legacy_alias_and_checkpoint_stays_durable(tmp_path):
    store = running_store(tmp_path)
    store.append_sequenced_events(RUN, [receipt(1)], return_snapshot=False)
    events._SQL_CACHE.clear()
    cold = ExpertRunStore(tmp_path, storage='sqlite')
    cold.append_sequenced_events(RUN, [receipt(2, 'expert_tool_result', output='done')], return_snapshot=False)
    assert_views_match(tmp_path)
    cold.mutate(RUN, lambda row: row['node_states']['executor'].update(status='completed'))
    cold.save_turn_checkpoint(RUN, turn_key='tool-performance', kind='node_end', node_id='executor')
    state = RunDatabase(tmp_path, RUN).load()
    assert state['last_checkpoint']
    assert state['node_states'] == state['stages']


def test_member_receipts_and_history_outlive_the_display_window(tmp_path):
    store = running_store(tmp_path)
    store.mutate(RUN, lambda row: row['members'].update({'executor::helper': {'tools': {}}}))
    batch = [dict(seq=i, type='expert_node_tool', node_id='executor', id=f'old-{i}')
             for i in range(1, 2502)]
    batch += [receipt(2502, member_id='helper'),
              receipt(2503, 'expert_tool_start', member_id='helper', started_at=1),
              receipt(2504, 'expert_tool_result', member_id='helper', output='done')]
    store.append_sequenced_events(RUN, batch, return_snapshot=False)
    db = RunDatabase(tmp_path, RUN)
    state = db.load()
    assert len(state['events']) == 2500
    assert state['members']['executor::helper']['tools']['t']['status'] == 'completed'
    assert store.history_page(RUN, limit=3)['items'][0]['seq'] == 1
    expected = run_views.build(state)
    with db.connection() as connection:
        for space in ('public', 'summary', 'workbench', 'aux', 'design'):
            assert db._read(connection, space) == expected[space]


def test_recovery_card_uses_full_projection_when_tools_change(tmp_path, monkeypatch):
    store = running_store(tmp_path)
    def seed(row):
        row['node_states']['executor']['scheduler_recovery'] = {
            'decisions': [{'action': 'human', 'decision_id': 'recover'}]}
    store.mutate(RUN, seed)
    original = run_views.build
    rebuilt = []
    def tracked(row, **kwargs):
        rebuilt.append(row['kernel_revision'])
        return original(row, **kwargs)
    monkeypatch.setattr(run_views, 'build', tracked)
    store.append_sequenced_events(RUN, [receipt(1, 'expert_tool_result', is_error=True,
                                              output='tool unavailable')], return_snapshot=False)
    assert rebuilt
    assert_views_match(tmp_path)


@pytest.mark.asyncio
async def test_read_output_digest_and_cache_share_one_open(tmp_path, monkeypatch):
    path = tmp_path / 'one.txt'
    raw = b'\xef\xbb\xbfone\r\ntwo\rthree\n'
    path.write_bytes(raw)
    cache, state = FileCache(), FileStateCache()
    cache.put(str(path.resolve()), 'stale cached text')
    original = Path.open
    opened = []
    def tracked(self, *args, **kwargs):
        if self == path:
            opened.append(args[0] if args else kwargs.get('mode'))
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', tracked)
    result = await read_module.ReadFile(cache, state).execute(read_module.Params(
        file_path=str(path), fingerprint_mode='both'))
    payload = json.loads(result.output)
    assert opened == ['rb']
    assert payload['content'] == '1\t\ufeffone\n2\ttwo\n3\tthree'
    assert payload['fingerprints']['raw_sha256'] == hashlib.sha256(raw).hexdigest()
    assert payload['fingerprints']['text_sha256'] == hashlib.sha256(b'one\ntwo\nthree\n').hexdigest()
    assert cache.get(str(path.resolve())) == '\ufeffone\ntwo\nthree\n'
    assert state.check(str(path.resolve()))[0]


@pytest.mark.asyncio
async def test_cancelled_read_does_not_publish_cache(tmp_path, monkeypatch):
    path = tmp_path / 'one.txt'
    path.write_text('one')
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    original = read_module._read_snapshot
    def paused(params, cancelled):
        entered.set()
        release.wait(3)
        result = original(params, threading.Event())
        exited.set()
        return result
    monkeypatch.setattr(read_module, '_read_snapshot', paused)
    cache, state = FileCache(), FileStateCache()
    task = asyncio.create_task(read_module.ReadFile(cache, state).execute(
        read_module.Params(file_path=str(path))))
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    assert await asyncio.to_thread(exited.wait, 2)
    await asyncio.sleep(0)
    assert len(cache) == 0 and not state.check(str(path.resolve()))[0]


@pytest.mark.asyncio
async def test_busy_io_workers_cannot_starve_durable_commit():
    release = threading.Event()
    entered = [threading.Event() for _ in range(4)]
    def block(flag):
        flag.set()
        release.wait(4)
    tasks = [asyncio.create_task(io_call(block, flag)) for flag in entered]
    try:
        for flag in entered:
            assert await asyncio.to_thread(flag.wait, 2)
        assert await asyncio.wait_for(durable_call(lambda: 'committed'), 1) == 'committed'
    finally:
        release.set()
        await asyncio.gather(*tasks)


@pytest.mark.parametrize('pattern', ['needle', 'n.edle', 'nee(?=dle)', '中文'])
def test_grep_native_and_fallback_match_unusual_text(tmp_path, monkeypatch, pattern):
    (tmp_path / 'a.txt').write_bytes(b'needle\rno\r\nnee\xffdle\nnee\xc3\xa9dle\n\x00needle\n')
    (tmp_path / 'b.txt').write_text('中文 needle\n', encoding='utf-8')
    (tmp_path / '.hidden.txt').write_text('needle\n')
    config = tmp_path / 'rg-config'
    config.write_text('--invert-match\n--max-filesize=1\n--path-separator=/\n')
    monkeypatch.setenv('RIPGREP_CONFIG_PATH', str(config))
    regex = re.compile(pattern)
    native = grep_module._grep_files(tmp_path, regex, '**/*.txt', threading.Event())
    monkeypatch.setattr(shutil, 'which', lambda _: None)
    fallback = grep_module._grep_files(tmp_path, regex, '**/*.txt', threading.Event())
    assert native == fallback


@pytest.mark.asyncio
async def test_parallel_results_stream_early_but_model_order_is_stable(tmp_path, monkeypatch):
    from mewcode.agent import Agent, ToolExecutionStartEvent, ToolResultEvent
    from mewcode.conversation import ConversationManager
    from mewcode.tools import create_default_registry
    from mewcode.tools.base import ToolCallComplete, StreamEnd, TextDelta, ToolResult
    from tests.test_agent import MockLLMClient
    release = asyncio.Event()
    client = MockLLMClient([
        [ToolCallComplete('slow', 'ReadFile', {'file_path': 'slow'}),
         ToolCallComplete('fast', 'ReadFile', {'file_path': 'fast'}), StreamEnd('tool_use')],
        [TextDelta('done'), StreamEnd('end_turn')]])
    agent = Agent(client, create_default_registry(), 'anthropic', work_dir=str(tmp_path))
    async def read(_tool, _params, tc):
        if tc.tool_id == 'slow':
            await release.wait()
        return ToolResult(tc.tool_id)
    monkeypatch.setattr(agent, '_run_recorded_tool', read)
    conversation = ConversationManager()
    conversation.add_user_message('read both')
    starts, results = [], []
    async with asyncio.timeout(5):
        async for event in agent.run(conversation):
            if isinstance(event, ToolExecutionStartEvent):
                starts.append(event.batch_pending)
            if isinstance(event, ToolResultEvent):
                results.append(event.tool_id)
                assert results != ['slow']
                release.set()
    assert starts == [True, False]
    assert results == ['fast', 'slow']
    messages = [message for message in conversation.history if message.tool_results]
    assert [result.tool_use_id for result in messages[-1].tool_results] == ['slow', 'fast']


@pytest.mark.asyncio
@pytest.mark.parametrize('count', [1, 2])
async def test_cancel_during_archival_preserves_completed_results(tmp_path, monkeypatch, count):
    from mewcode.agent import Agent
    from mewcode.conversation import ConversationManager
    from mewcode.tools import create_default_registry
    from mewcode.tools.base import ToolCallComplete, StreamEnd, ToolResult
    from tests.test_agent import MockLLMClient
    entered, release = threading.Event(), threading.Event()
    client = MockLLMClient([[*[ToolCallComplete(str(i), 'ReadFile', {'file_path': 'one'})
                              for i in range(count)], StreamEnd('tool_use')]])
    agent = Agent(client, create_default_registry(), 'anthropic', work_dir=str(tmp_path))
    async def read(_tool, _params, tc):
        return ToolResult('completed-' + tc.tool_id, host_observation={'receipt': tc.tool_id})
    def archive(_tool_id, output):
        entered.set()
        release.wait(3)
        return output
    monkeypatch.setattr(agent, '_run_recorded_tool', read)
    monkeypatch.setattr(agent, '_maybe_persist_or_truncate', archive)
    conversation = ConversationManager()
    conversation.add_user_message('read')
    async def consume():
        async for _ in agent.run(conversation):
            pass
    task = asyncio.create_task(consume())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    finally:
        release.set()
    results = [result for message in conversation.history for result in message.tool_results]
    assert len(results) == count
    for i, result in enumerate(results):
        assert result.content == 'completed-' + str(i)
        assert not result.is_error
        assert result.host_observation['receipt'] == str(i)
