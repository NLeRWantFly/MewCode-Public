import copy
import json

import pytest

from mewcode.kernel.events import EventJournal
from mewcode.expert_runs import ExpertRunStore
from mewcode.orchestration.write_lease import WriteLeaseError


def test_state_replays_without_checkpoint_and_temporary_writes_are_not_events(tmp_path):
    journal = EventJournal(tmp_path, 'a' * 32)
    value = {'run_id': 'a' * 32, 'status': 'paused', 'nodes': {'N03': {'blocked': True}}}
    journal.commit(value)
    value['nodes']['N03']['blocked'] = False
    journal.commit(value)
    (journal.directory / '.pending-interrupted').write_text('incomplete', encoding='utf-8')
    assert journal.load() == value
    assert EventJournal(tmp_path, 'a' * 32).load() == value
    stale = copy.deepcopy(value)
    stale['kernel_revision'] = 1
    with pytest.raises(ValueError, match='stale'):
        journal.commit(stale)


def test_event_tampering_is_not_repaired_during_read(tmp_path):
    journal = EventJournal(tmp_path, 'a' * 32)
    journal.commit({'status': 'paused'})
    path = journal.directory / '0000000000000001.json'
    event = json.loads(path.read_text(encoding='utf-8'))
    event['kind'] = 'tampered'
    path.write_text(json.dumps(event), encoding='utf-8')
    with pytest.raises(ValueError, match='integrity'):
        journal.load()


def test_live_store_recovers_all_mutations_after_checkpoint_deleted(tmp_path):
    identity = 'a' * 32
    store = ExpertRunStore(tmp_path)
    store.create(run_id=identity, conversation_id='fixture', task='task', attachments=[], profile={}, strategy='stable')
    store.mutate(identity, lambda record: record.update(status_reason='durable'), durable=True)
    store.append_event(identity, {'type': 'expert_step', 'label': 'recorded'})
    expected = store.load(identity)
    (tmp_path / f'{identity}.json').unlink()
    restored = ExpertRunStore(tmp_path).load(identity)
    assert restored == expected
    public = ExpertRunStore(tmp_path).public_for_conversation('fixture')
    assert len(public) == 1
    assert public[0]['status_reason'] == 'durable'
    assert ExpertRunStore(tmp_path).list_for_conversation('fixture') == [expected]
    assert ExpertRunStore(tmp_path).list_recent() == [expected]


@pytest.mark.parametrize('error', [OSError('disk full'), WriteLeaseError('lease_store_lock_failed')])
@pytest.mark.parametrize('operation', ['append', 'mutate'])
def test_failed_event_transaction_does_not_publish_memory_state(tmp_path, monkeypatch, error, operation):
    identity = 'a' * 32
    store = ExpertRunStore(tmp_path)
    store.create(run_id=identity, conversation_id='fixture', task='task', attachments=[], profile={}, strategy='stable')
    previous = store.load(identity)
    monkeypatch.setattr(EventJournal, 'commit', lambda *args, **kwargs: (_ for _ in ()).throw(error))
    with pytest.raises(type(error), match=str(error)):
        if operation == 'append':
            store.append_event(identity, {'type': 'expert_step', 'label': 'uncommitted'})
        else:
            store.mutate(identity, lambda state: state.update(status_reason='uncommitted'), durable=True)
    assert store.load(identity) == previous
    assert ExpertRunStore(tmp_path).load(identity) == previous


def test_new_instances_only_decode_new_events_and_return_isolated_state(tmp_path, monkeypatch):
    from pathlib import Path
    identity = 'b' * 32
    journal = EventJournal(tmp_path, identity)
    value = {'nodes': {'a': {'text': 'first'}}}
    journal.commit(value)
    journal.load()  # Verify the prefix once.
    read = Path.read_text
    decoded = []
    def track(path, *args, **kwargs):
        if path.parent == journal.directory and path.stem.isdigit():
            decoded.append(path.name)
        return read(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', track)
    returned = EventJournal(tmp_path, identity).load()
    returned['nodes']['a']['text'] = 'caller mutation'
    value['nodes']['a']['text'] = 'second'
    EventJournal(tmp_path, identity).commit(value)
    assert EventJournal(tmp_path, identity).load() == value
    assert decoded == ['0000000000000002.json']


@pytest.mark.parametrize('change', ['rewrite', 'remove', 'header'])
def test_verified_prefix_cache_invalidates_on_history_changes(tmp_path, change):
    identity = 'c' * 32
    journal = EventJournal(tmp_path, identity)
    value = {'status': 'paused'}
    journal.commit(value)
    value['status'] = 'running'
    journal.commit(value)
    journal.load()
    first = journal.directory / '0000000000000001.json'
    if change == 'rewrite':
        event = json.loads(first.read_text(encoding='utf-8'))
        event['kind'] = 'tampered'
        first.write_text(json.dumps(event), encoding='utf-8')
    elif change == 'remove':
        first.unlink()
    else:
        (journal.directory / 'header.json').write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='integrity|migration'):
        EventJournal(tmp_path, identity).load()


def test_failed_append_does_not_cache_uncommitted_state(tmp_path, monkeypatch):
    journal = EventJournal(tmp_path, 'd' * 32)
    value = {'status': 'paused'}
    journal.commit(value)
    previous = journal.load()
    value['status'] = 'running'
    monkeypatch.setattr(journal, '_append_file', lambda *args: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError):
        journal.commit(value)
    assert EventJournal(tmp_path, 'd' * 32).load() == previous


def test_public_sidecar_cannot_hide_new_kernel_state(tmp_path):
    identity = 'a' * 32
    first = ExpertRunStore(tmp_path)
    first.create(run_id=identity, conversation_id='fixture', task='task', attachments=[], profile={}, strategy='stable')
    viewer = ExpertRunStore(tmp_path)
    viewer.public_for_conversation('fixture')
    first.append_event(identity, {'type': 'expert_step', 'label': 'new fact'})
    summary = viewer.public_for_conversation('fixture')[0]
    assert any(item.get('label') == 'new fact' for item in summary['events'])
