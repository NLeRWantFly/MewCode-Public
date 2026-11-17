"""EventJournal v2 write isolation: schema 1 fencing of collaboration v2 state.

Classic and collaboration v1 records stay on schema 1. A collaboration v2 state
must be fenced to schema 2 *before* any new event is appended, without touching
the existing immutable history, so an old schema-1 program fails closed.
"""
import json

import pytest

from mewcode.kernel.events import EventJournal, changes, digest


def _header(journal):
    return json.loads((journal.directory / 'header.json').read_text(encoding='utf-8'))


def _seed_v1(journal, state):
    """Simulate on-disk schema-1 history, as the previous program would leave it."""
    journal.directory.mkdir(parents=True, exist_ok=True)
    journal._append_file(journal.directory / 'header.json',
                         {'schema_version': 1, 'run_id': journal.run_id, 'format': 'mewcode.kernel.events'})
    event = {'sequence': 1, 'previous_hash': '', 'kind': 'state_changed', 'changes': changes({}, state)}
    journal._append_file(journal.directory / '0000000000000001.json', {**event, 'sha256': digest(event)})


def test_classic_state_keeps_schema_one(tmp_path):
    journal = EventJournal(tmp_path, 'a' * 32)
    journal.commit({'status': 'paused'})
    assert _header(journal) == {'schema_version': 1, 'run_id': 'a' * 32, 'format': 'mewcode.kernel.events'}


def test_collaboration_v1_keeps_schema_one(tmp_path):
    journal = EventJournal(tmp_path, 'b' * 32)
    journal.commit({'collaboration': {'format': 'mewcode.collaboration.v1'}})
    assert _header(journal)['schema_version'] == 1


def test_collaboration_v2_first_commit_writes_schema_two_header(tmp_path):
    identity = 'b' * 32
    journal = EventJournal(tmp_path, identity)
    journal.commit({'collaboration': {'format': 'mewcode.collaboration.v2', 'members': []}})
    assert _header(journal)['schema_version'] == 2
    reloaded = EventJournal(tmp_path, identity).load()
    assert reloaded['collaboration']['format'] == 'mewcode.collaboration.v2'


def test_existing_v1_is_upgraded_without_touching_history(tmp_path):
    identity = 'c' * 32
    journal = EventJournal(tmp_path, identity)
    journal.commit({'status': 'paused'})
    first = journal.directory / '0000000000000001.json'
    original = first.read_bytes()
    previous_hash = json.loads(original)['sha256']
    assert _header(journal)['schema_version'] == 1

    upgraded = {'status': 'paused', 'collaboration': {'format': 'mewcode.collaboration.v2'},
                'kernel_revision': 1}
    assert journal.commit(upgraded) == 2

    assert _header(journal)['schema_version'] == 2
    assert first.read_bytes() == original  # immutable history preserved byte-for-byte
    second = json.loads((journal.directory / '0000000000000002.json').read_text(encoding='utf-8'))
    assert second['previous_hash'] == previous_hash  # prefix hash chain intact


def test_v2_state_upgrades_header_even_without_operations(tmp_path):
    identity = 'd' * 32
    journal = EventJournal(tmp_path, identity)
    _seed_v1(journal, {'collaboration': {'format': 'mewcode.collaboration.v2'}})
    loaded = journal.load()
    assert loaded['kernel_revision'] == 1
    assert _header(journal)['schema_version'] == 1

    assert journal.commit(loaded) == 1  # no operations, yet the fence must still move

    assert _header(journal)['schema_version'] == 2
    assert journal.load() == loaded
    assert (journal.directory / '0000000000000001.json').is_file()


def test_header_upgrade_survives_event_append_failure_and_retries(tmp_path, monkeypatch):
    identity = 'e' * 32
    journal = EventJournal(tmp_path, identity)
    journal.commit({'status': 'paused'})
    before = journal.load()
    target = {'status': 'paused', 'collaboration': {'format': 'mewcode.collaboration.v2'},
              'kernel_revision': before['kernel_revision']}

    def fail(*args, **kwargs):
        raise OSError('disk full')

    monkeypatch.setattr(journal, '_append_file', fail)
    with pytest.raises(OSError):
        journal.commit(target)
    monkeypatch.undo()

    # The schema-2 header sticks; history and sequence are unchanged and reloadable.
    assert _header(journal)['schema_version'] == 2
    recovered = EventJournal(tmp_path, identity)
    assert recovered.load() == before

    assert recovered.commit(target) == 2
    assert recovered.load()['collaboration']['format'] == 'mewcode.collaboration.v2'


def test_schema_two_is_never_downgraded(tmp_path):
    identity = 'a1' * 16
    journal = EventJournal(tmp_path, identity)
    journal.commit({'collaboration': {'format': 'mewcode.collaboration.v2'}})
    assert _header(journal)['schema_version'] == 2

    journal.commit({'status': 'done', 'kernel_revision': 1})
    assert _header(journal)['schema_version'] == 2


@pytest.mark.parametrize('header', [
    {'schema_version': 3, 'run_id': 'f' * 32, 'format': 'mewcode.kernel.events'},
    {'schema_version': 2, 'run_id': 'f' * 32, 'format': 'mewcode.kernel.events', 'extra': True},
    {'schema_version': 1, 'run_id': 'f' * 32},
])
def test_unknown_or_extended_header_is_rejected(tmp_path, header):
    identity = 'f' * 32
    journal = EventJournal(tmp_path, identity)
    journal.commit({'status': 'paused'})
    (journal.directory / 'header.json').write_text(json.dumps(header), encoding='utf-8')
    with pytest.raises(ValueError, match='migration'):
        EventJournal(tmp_path, identity).load()
