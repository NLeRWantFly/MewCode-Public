import copy
import json
import sqlite3

import pytest

from mewcode.kernel.events import EventJournal, _SQL_CACHE
from mewcode.kernel.sqlite_state import RunDatabase, RunDatabaseError, delta, encoded, fingerprint


RUN = 'e' * 32


def test_disk_full_reports_storage_cause_and_preserves_committed_checkpoint(tmp_path):
    journal = EventJournal(tmp_path, RUN, storage='sqlite')
    checkpoint = dict(run_id=RUN, status='paused', delivered=['kept'])
    journal.commit(checkpoint)
    database = RunDatabase(tmp_path, RUN)
    with pytest.raises(RunDatabaseError, match='空间不足') as caught:
        with database.connection(write=True) as db:
            db.execute('CREATE TABLE storage_pressure (payload BLOB)')
            pages = db.execute('PRAGMA page_count').fetchone()[0]
            db.execute('PRAGMA max_page_count=' + str(pages))
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT INTO storage_pressure VALUES (zeroblob(1048576))')
            db.commit()
    assert caught.value.storage_full
    assert caught.value.sqlite_errorcode & 255 == sqlite3.SQLITE_FULL
    assert caught.value.sqlite_errorname == 'SQLITE_FULL'
    assert caught.value.database_path == tmp_path/'runs.sqlite3'
    assert isinstance(caught.value.__cause__, sqlite3.Error)
    _SQL_CACHE.clear()
    assert journal.load() == checkpoint


def test_database_error_keeps_sqlite_detail_for_non_storage_failures(tmp_path):
    with pytest.raises(RunDatabaseError, match='transaction failed') as caught:
        with RunDatabase(tmp_path, RUN).connection(write=True) as db:
            db.execute('INVALID SQL')
    assert not caught.value.storage_full
    assert caught.value.sqlite_errorcode == sqlite3.SQLITE_ERROR
    assert 'syntax error' in str(caught.value)


def state(count=0):
    return dict(run_id=RUN, conversation_id='c', status='paused',
                events=[dict(seq=i + 1, type='expert_step', label='step ' + str(i)) for i in range(count)],
                grants={'a': {'status': 'denied'}}, certificates={'done': {'hash': 'unchanged'}})


def written(root):
    with sqlite3.connect(root / 'runs.sqlite3') as db:
        return db.execute('SELECT logical_bytes FROM transactions ORDER BY revision DESC LIMIT 1').fetchone()[0]


@pytest.mark.parametrize('count', [1000, 5000, 10000])
def test_one_step_writes_less_than_ten_kib_regardless_of_history(tmp_path, count):
    journal = EventJournal(tmp_path, RUN, storage='sqlite')
    value = state(count)
    journal.commit(value)
    value['events'].append(dict(seq=count + 1, type='expert_step', label='next'))
    journal.commit(value, kind='host_event')
    assert written(tmp_path) < 10 * 1024
    _SQL_CACHE.clear()
    assert journal.load() == value


def test_total_write_volume_is_linear_in_step_count(tmp_path):
    totals = []
    for count in (1000, 2000, 5000):
        root = tmp_path / str(count)
        journal = EventJournal(root, RUN, storage='sqlite')
        value = state()
        journal.commit(value)
        for start in range(0, count, 100):
            value['events'].extend(dict(seq=i + 1, type='expert_step', label='fixed text')
                                   for i in range(start, start + 100))
            journal.commit(value)
        with sqlite3.connect(root / 'runs.sqlite3') as db:
            totals.append(db.execute('SELECT sum(logical_bytes) FROM transactions').fetchone()[0])
    assert 1.7 < totals[1] / totals[0] < 2.3
    assert 4.5 < totals[2] / totals[0] < 5.8


def test_rolling_event_window_does_not_rewrite_other_rows(tmp_path):
    journal = EventJournal(tmp_path, RUN, storage='sqlite')
    value = state(5000)
    journal.commit(value)
    value['events'] = value['events'][1:] + [dict(seq=5001, type='expert_step', label='next')]
    journal.commit(value)
    assert written(tmp_path) < 10 * 1024
    _SQL_CACHE.clear()
    assert journal.load() == value


def test_list_replacement_empty_values_and_json_types_round_trip(tmp_path):
    journal = EventJournal(tmp_path, RUN, storage='sqlite')
    value = dict(a=[1, False, None, {'s/~': '原文\ud800'}], empty={}, empty_list=[], flag=True)
    journal.commit(value)
    value.update(a=[2, True], empty={'x': []}, empty_list={}, flag=1)
    journal.commit(value)
    _SQL_CACHE.clear()
    assert journal.load() == value
    assert type(journal.load()['flag']) is int


def test_repeated_flow_content_is_stored_once(tmp_path):
    journal = EventJournal(tmp_path, RUN, storage='sqlite')
    flow = {'nodes': [{'node_id': 'n' + str(i), 'body': 'x' * 100} for i in range(200)]}
    value = state()
    value['locked_flow'] = flow
    journal.commit(value)
    blobs = set((tmp_path / 'content-v1').rglob('*.json'))
    value['events'].append(dict(seq=1, type='expert_flow_defined', flow=flow))
    journal.commit(value)
    assert fingerprint(flow) in {p.stem for p in blobs}
    assert len(list((tmp_path / 'content-v1').rglob(f'{fingerprint(flow)}.json'))) == 1
    assert journal.load() == value


def test_migration_preserves_all_state_and_legacy_transaction_bytes(tmp_path):
    journal = EventJournal(tmp_path, RUN)
    value = state(100)
    value['pending_gate'] = {'id': 'g', 'status': 'awaiting_user'}
    journal.commit(value)
    value['status'] = 'running'
    journal.commit(value)
    originals = {p.name: p.read_bytes() for p in journal.directory.glob('[0-9]*.json')}
    _, revision, proof = journal._scan()
    result = journal.migrate_sqlite(expected_revision=revision, expected_hash=proof)
    assert result['state_sha256'] == fingerprint({k: v for k, v in value.items() if k != 'kernel_revision'})
    assert journal.load() == value
    assert originals == {p.name: p.read_bytes() for p in journal.directory.glob('[0-9]*.json')}
    value['status'] = 'paused'
    journal.commit(value)
    assert len(list(journal.directory.glob('[0-9]*.json'))) == 2
    assert journal.load() == value


def test_migration_failure_does_not_switch_the_authority(tmp_path, monkeypatch):
    journal = EventJournal(tmp_path, RUN)
    value = state(3)
    journal.commit(value)
    _, revision, proof = journal._scan()
    monkeypatch.setattr(RunDatabase, 'import_state', lambda *a, **k: (_ for _ in ()).throw(OSError('full')))
    with pytest.raises(OSError):
        journal.migrate_sqlite(expected_revision=revision, expected_hash=proof)
    assert not journal.sqlite and journal.load() == value


def test_stale_sql_writer_and_failed_projection_roll_back(tmp_path, monkeypatch):
    journal = EventJournal(tmp_path, RUN, storage='sqlite')
    value = state(3)
    journal.commit(value)
    stale = copy.deepcopy(value)
    value['status'] = 'running'
    journal.commit(value)
    with pytest.raises(ValueError, match='stale'):
        journal.commit(stale)
    def fail(*args, **kwargs):
        raise OSError('projection failure')
    monkeypatch.setattr(RunDatabase, '_models', fail)
    changed = copy.deepcopy(value)
    changed['status'] = 'paused'
    with pytest.raises(OSError):
        journal.commit(changed, models={'public': {'status': 'paused'}})
    _SQL_CACHE.clear()
    assert journal.load() == value


def test_corrupt_content_and_inline_cell_are_rejected(tmp_path):
    journal = EventJournal(tmp_path, RUN, storage='sqlite')
    value = state()
    value['text'] = '原始回执' * 5000
    journal.commit(value)
    digest = fingerprint(value['text'])
    path = tmp_path / 'content-v1' / digest[:2] / (digest + '.json')
    path.write_bytes(b'"damaged"')
    _SQL_CACHE.clear()
    with pytest.raises(ValueError, match='integrity'):
        journal.load()


def test_paged_delta_does_not_include_history():
    before, after = state(5000), state(5001)
    operations = list(delta(before, after))
    assert len(encoded(operations)) < 1024
    assert len(operations) == 1


def test_interleaved_runs_do_not_reload_unchanged_history(tmp_path, monkeypatch):
    first = EventJournal(tmp_path, RUN, storage='sqlite')
    second = EventJournal(tmp_path, 'b' * 32, storage='sqlite')
    a, b = state(100), dict(state(), run_id='b' * 32)
    first.commit(a)
    second.commit(b)
    reads = []
    load = RunDatabase.load
    def observed(database):
        reads.append(database.run_id)
        return load(database)
    monkeypatch.setattr(RunDatabase, 'load', observed)
    for number in range(3):
        b['events'].append(dict(seq=number + 1, type='expert_step', label='independent'))
        second.commit(b)
        assert first.load() == a
    assert reads == []
    a['status'] = 'running'
    first.commit(a)
    assert first.load()['status'] == 'running'
    _SQL_CACHE.clear()
    assert first.load() == a and reads == [RUN]
