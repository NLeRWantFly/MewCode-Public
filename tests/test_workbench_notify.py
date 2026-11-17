"""T85 post-commit hints and bounded, authoritative refresh notifications."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from frontend import server
from frontend.workbench_notify import Coalescer, WorkbenchNotifier
from mewcode.orchestration import task_view
from mewcode.orchestration.draft_store import DraftStore
from mewcode.orchestration.task_design import TaskDesign
from mewcode.orchestration.workbench_events import subscribe, changed
from tests.test_collaboration_mode import RUN_ID, store_for
from tests.test_task_design_entry import task_api_env


class Clock:
    def __init__(self):
        self.now, self.queue, self.coroutines = 0, [], []

    def later(self, delay, callback):
        handle = SimpleNamespace(cancelled=False)
        handle.cancel = lambda: setattr(handle, 'cancelled', True)
        self.queue.append((self.now + delay, callback, handle))
        return handle

    async def advance(self, end):
        while self.queue and min(row[0] for row in self.queue) <= end + 1e-9:
            row = min(self.queue, key=lambda row: row[0])
            self.queue.remove(row)
            self.now = row[0]
            if not row[2].cancelled:
                row[1]()
            while self.coroutines:
                await self.coroutines.pop(0)
        self.now = end


@pytest.mark.asyncio
async def test_one_hundred_real_writes_in_one_second_have_at_most_five_notifications(tmp_path):
    store = store_for(tmp_path, selected='classic')
    clock, sent, reads = Clock(), [], []
    def read(event):
        reads.append(clock.now)
        view = task_view.from_record(store.load(event['run_id']))
        return [dict(conversation_id='conversation', task_id=view['task']['id'], run_id=RUN_ID, version=view['version'])]
    async def send(payload):
        sent.append((clock.now, payload))
    queue = Coalescer(read, send, call_later=clock.later, spawn=clock.coroutines.append)
    stop = subscribe(lambda event: queue.touch(RUN_ID, event) if event['kind'] == 'run' and event['root'] == str(store.root) else None)
    try:
        for index in range(100):
            await clock.advance(index / 100)
            store.mutate(RUN_ID, lambda row: row.update(status_reason='进展 ' + str(index)), durable=True)
        await clock.advance(1.25)
        assert 1 <= len(sent) <= 5 and len(reads) == len(sent)
        assert all(b[0] - a[0] >= .25 - 1e-9 for a, b in zip(sent, sent[1:]))
        assert sent[-1][1]['version'] == task_view.from_record(store.load(RUN_ID))['version']
    finally:
        stop()
        queue.close()


@pytest.mark.asyncio
async def test_same_version_is_not_sent_again_and_failed_read_does_not_publish():
    clock, send = Clock(), AsyncMock()
    values = [dict(conversation_id='c', task_id='t', run_id='r', version='v1')]
    queue = Coalescer(lambda event: values, send, call_later=clock.later, spawn=clock.coroutines.append)
    for at in (0, .3, .6):
        await clock.advance(at)
        queue.touch('task', {})
    await clock.advance(1)
    assert send.await_count == 1
    queue.read = lambda event: (_ for _ in ()).throw(ValueError('unavailable'))
    queue.touch('task', {})
    await clock.advance(1.3)
    assert send.await_count == 1


def test_draft_signal_occurs_after_commit_and_never_after_rollback(tmp_path):
    seen = []
    def receive(event):
        if event['kind'] == 'draft':
            seen.append((event, DraftStore.read_state(tmp_path, 'c')))
    stop = subscribe(receive)
    try:
        design = TaskDesign(tmp_path, 'c')
        result = design.start('plan')
        assert seen[-1][0]['task_id'] == result['task_id']
        assert seen[-1][1]['task_design']['task_id'] == result['task_id']
        count = len(seen)
        with pytest.raises(ValueError):
            with design.store.db() as db:
                design.store._put(db, {'task_design': {'task_id': 'rolled-back'}})
                raise ValueError('rollback')
        assert len(seen) == count
    finally:
        stop()


def test_team_writes_emit_once_but_read_and_duplicate_commands_do_not(tmp_path):
    from mewcode.teams.host import TeamHost, template
    host, events = TeamHost(tmp_path), []
    host.enable()
    stop = subscribe(lambda event: events.append(event) if event['kind'] == 'team' else None)
    try:
        result = host.draft(definition=template('data'), objective='检查数据', budget_ref='budget', name='数据任务', command_id='draft')
        assert events and all(row['db_path'] == str(host.state / 'coordination.sqlite3') for row in events)
        count = len(events)
        host.snapshot(result['run']['team_run_id'], read_only=True)
        host.draft(definition=template('data'), objective='检查数据', budget_ref='budget', name='数据任务', command_id='draft')
        assert len(events) == count
    finally:
        stop()
        host.close()


@pytest.mark.asyncio
async def test_notifications_and_reconnect_read_share_current_digest(task_api_env, tmp_path, monkeypatch):
    _, _, _, owner, _ = task_api_env
    store = store_for(tmp_path / 'run', selected='classic')
    cid = owner['conversation_id']
    store.mutate(RUN_ID, lambda record: record.update(conversation_id=cid), durable=True)
    monkeypatch.setattr(server, 'EXPERT_RUNS', store)
    runtime = SimpleNamespace(expert_run_id=RUN_ID, conversation_id=cid, work_dir=owner['project'])
    monkeypatch.setattr(server.SESSION, 'runtime_for', lambda identity: runtime if identity == cid else None)
    send = AsyncMock()
    notifier = WorkbenchNotifier(server.WORKBENCH)
    notifier.coalescer.send = send
    notifier.attach_loop()
    try:
        # The first state after a reconnect invalidates, even if intermediate hints were lost.
        notifier.observe({'type': 'state', 'conversation_id': cid})
        await asyncio.sleep(0)
        await notifier.coalescer.flush((cid, RUN_ID))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
            view = (await client.get('/api/workbench/view', params=dict(owner, run_id=RUN_ID))).json()
        assert send.call_args.args[0]['version'] == view['version']
        from mewcode.agent import AskUserRequest
        runtime.ask_user_request = AskUserRequest('wait', 'AskUserQuestion', [dict(name='x', question='补充范围')], asyncio.get_running_loop().create_future())
        notifier.observe({'type': 'ask_user', 'conversation_id': cid})
        await asyncio.sleep(0)
        await notifier.coalescer.flush((cid, RUN_ID))
        waiting_version = send.call_args.args[0]['version']
        assert waiting_version != view['version']
        runtime.ask_user_request.future.set_result({'x': '指定范围'})
        changed('runtime', conversation_id=cid)
        await asyncio.sleep(0)
        await notifier.coalescer.flush((cid, RUN_ID))
        assert send.call_args.args[0]['version'] == view['version']
    finally:
        notifier.close()


@pytest.mark.asyncio
async def test_failed_mutation_and_foreign_store_do_not_notify(tmp_path, monkeypatch):
    store = store_for(tmp_path / 'own', selected='classic')
    monkeypatch.setattr(server, 'EXPERT_RUNS', store)
    notifier = WorkbenchNotifier(server.WORKBENCH)
    try:
        notifier.schedule(dict(kind='run', root=str(tmp_path / 'other'), conversation_id='conversation', run_id=RUN_ID))
        assert not notifier.coalescer.pending
        events = []
        stop = subscribe(events.append)
        try:
            with pytest.raises(ValueError):
                store.mutate(RUN_ID, lambda record: (_ for _ in ()).throw(ValueError('invalid')))
            assert not events
        finally:
            stop()
    finally:
        notifier.close()
