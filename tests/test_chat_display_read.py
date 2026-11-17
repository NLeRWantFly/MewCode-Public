"""Chat opens promptly without hiding execution receipts or blocking dispatch."""
import asyncio
import copy
import json
import threading
from types import SimpleNamespace

import pytest

from frontend import server
from mewcode.expert_runs import ExpertRunStore
from mewcode.kernel.chat_view import STAGE_HISTORY, compact
from mewcode.kernel.sqlite_state import RunDatabase


RUN = 'a' * 32


def test_chat_reads_skip_diagnostic_trees_and_restore_recent_tools(tmp_path, monkeypatch):
    store = ExpertRunStore(tmp_path, storage='sqlite')
    store.create(run_id=RUN, conversation_id='chat', task='task', attachments=[], profile={}, strategy='stable')
    def seed(row):
        row['status'] = 'running'
        row['locked_flow']['meta'] = {'workflow_draft': {'evidence': 'internal source' * 1000},
                                      'loops': [{'loop_id': 'review', 'label': '检查'}]}
        row['node_states'] = {'executor': dict(role='executor', status='running', attempt=1, node_attempt=2,
            partial_text='执行正文完整保留。' * 300, stream_activity={'phase': 'processing', 'updated_at': 123},
            **{key: [{'notes': 'internal diagnostic' * 100}] * 20 for key in STAGE_HISTORY})}
    store.mutate(RUN, seed)
    store.append_sequenced_events(RUN, [
        dict(type='expert_tool', seq=1, node_id='executor', role='executor', attempt=1, node_attempt=2,
             id='read-1', name='ReadFile', args={'path': 'report.txt'}),
        dict(type='expert_tool_result', seq=2, node_id='executor', role='executor', attempt=1, node_attempt=2,
             id='read-1', name='ReadFile', output='执行记录', status='completed'),
    ], return_snapshot=False)
    database = RunDatabase(tmp_path, RUN)
    before, revision = database.load(), database.revision()
    public = database.read('summary')
    original = RunDatabase._read
    def guarded(self, connection, space, parts=()):
        assert space != 'state'
        assert not any(key in STAGE_HISTORY for key in parts)
        assert not (space == 'summary' and parts in [(), ('node_states',), ('stages',), ('events',)])
        return original(self, connection, space, parts)
    with monkeypatch.context() as patch:
        patch.setattr(RunDatabase, '_read', guarded)
        assert store.read_owner(RUN) == 'chat'
        result = store.chat_for_conversation('chat')[0]
        patch.setattr(RunDatabase, '_read', lambda *args: pytest.fail('Task enumeration loaded a display tree'))
        assert store.query_run_ids('chat') == [RUN]
    assert database.load() == before and database.revision() == revision
    assert result['locked_flow']['meta'] == {key: value for key, value in public['locked_flow'].get('meta', {}).items()
                                           if key != 'workflow_draft'}
    assert result['node_states']['executor']['stream_activity'] == {'phase': 'processing', 'updated_at': 123}
    assert not (STAGE_HISTORY & result['node_states']['executor'].keys())
    assert [e['text'] for e in result['events'] if e['type'] == 'expert_delta'] == [
        e['text'] for e in public['events'] if e['type'] == 'expert_delta']
    tools = [e for e in result['events'] if e['type'] == 'expert_tool_result']
    assert tools and tools[-1]['output'] == '执行记录' and tools[-1]['node_attempt'] == 2
    assert len(json.dumps(result)) < len(json.dumps(public)) / 3
    store.append_event(RUN, dict(type='expert_tool_start', node_id='executor', role='executor',
                                attempt=1, node_attempt=2, id='unfinished', name='ReadFile'))
    store.mutate(RUN, lambda row: row.update(status='paused'))
    stopped = store.chat_for_conversation('chat')[0]
    assert any(event.get('id') == 'unfinished' and event.get('status') == 'outcome_unknown'
               for event in stopped['events'])


def test_legacy_chat_compaction_leaves_original_unchanged():
    source = {'locked_flow': {'meta': {'workflow_draft': {'private': 'large'}, 'loops': []}},
              'node_states': {'work': {'status': 'running', 'segment_health_history': ['large']}},
              'events': [{'type': 'expert_delta', 'text': '完整正文'},
                         {'type': 'expert_stage', 'status': 'running', 'interaction_history': ['large']}]}
    before = copy.deepcopy(source)
    result = compact(source)
    assert source == before and result['locked_flow']['meta'] == {'loops': []}
    assert result['events'] == [{'type': 'expert_delta', 'text': '完整正文'}, {'type': 'expert_stage', 'status': 'running'}]
    assert result['node_states'] == {'work': {'status': 'running'}}


@pytest.mark.asyncio
async def test_navigation_keeps_session_switch_on_loop_and_display_in_worker(monkeypatch, tmp_path):
    owner = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    sessions_dir = tmp_path / 'original'
    calls = []
    def switch(scope, cid, *, include_display=True):
        assert threading.get_ident() == owner and not include_display
        return dict(id=cid, scope=scope, entries=[{'role': 'user', 'text': 'current'}])
    def pipeline(scope, cid, entries, **kwargs):
        assert threading.get_ident() != owner
        assert kwargs['sessions_dir'] == sessions_dir
        entered.set()
        assert release.wait(3)
        return []
    def runs(cid):
        calls.append(cid)
        assert threading.get_ident() != owner
        return []
    fake = SimpleNamespace(load_conversation=switch, pipeline_events=pipeline,
                           session_manager=lambda _: SimpleNamespace(_sessions_dir=sessions_dir))
    monkeypatch.setattr(server, 'SESSION', fake)
    monkeypatch.setattr(server, 'EXPERT_RUNS', SimpleNamespace(chat_for_conversation=runs))
    pending = asyncio.create_task(server.api_load_conversation('normal', 'chat'))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert not pending.done()
        # A later project switch cannot redirect the in-flight history read.
        fake.session_manager = lambda _: SimpleNamespace(_sessions_dir=tmp_path / 'other')
    finally:
        release.set()
    response = await asyncio.wait_for(pending, 3)
    assert response.status_code == 200 and calls == ['chat']
    assert json.loads(response.body)['scope'] == 'user'
