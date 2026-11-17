"""Updating the current display window must not scan archived receipts."""
import copy

import pytest

from mewcode.kernel.sqlite_state import RunDatabase, pointer


def event(seq):
    return dict(seq=seq, node_id='n', type='expert_step', text='receipt',
                detail={'ok': True, 'count': seq})


def seed(root, count, run_id='a' * 32):
    database = RunDatabase(root, run_id)
    state = {'run_id': run_id, 'tick': 1}
    database.commit(state, previous={}, kind='seed',
                    models={'history': {'events': [event(i) for i in range(1, count + 1)]}})
    return database, state


@pytest.mark.parametrize('window_size', [1, 5])
def test_projecting_fixed_window_has_bounded_sql_work(tmp_path, window_size):
    costs = []
    for count in (10, 5000):
        database, state = seed(tmp_path / str(count), count)
        window = [event(i) for i in range(count - window_size + 2, count + 2)]
        with database.connection(write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            ticks = []
            db.set_progress_handler(lambda: ticks.append(1) or 0, 100)
            database._models(db, {'history': {'events': window}}, 2, state)
            db.set_progress_handler(None, 0)
            costs.append(len(ticks) * 100)
            db.rollback()
    assert costs[1] < 10000, costs
    assert costs[1] <= costs[0] + 1000, costs


def test_append_after_5000_receipts_writes_only_a_few_kib(tmp_path):
    database, before = seed(tmp_path, 5000)
    after = dict(before, tick=2)
    database.commit(after, previous=before, kind='append',
                    models={'history': {'events': [event(5000), event(5001)]}})
    with database.connection() as db:
        written = db.execute('SELECT logical_bytes FROM transactions WHERE run_id=? AND revision=2',
                             (database.run_id,)).fetchone()[0]
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert written < 8192
    assert database.page(after=4999)['items'] == [event(5000), event(5001)]
    assert database.page(limit=1)['items'] == [event(1)]


def test_total_projection_writes_grow_linearly(tmp_path):
    totals = []
    for count in (100, 200):
        database = RunDatabase(tmp_path / str(count), 'a' * 32)
        before = {}
        # Fixed batches keep durable fsync work small in the test while the
        # total number of persisted receipts doubles. A single append after
        # 5,000 receipts is checked separately above.
        for i in range(10, count + 1, 10):
            after = dict(before, tick=i)
            window = [event(seq) for seq in range(i - 9, i + 1)]
            database.commit(after, previous=before, kind='append',
                            models={'history': {'events': window}})
            before = after
        with database.connection() as db:
            totals.append(db.execute('SELECT sum(logical_bytes) FROM transactions').fetchone()[0])
            assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert 1.8 < totals[1] / totals[0] < 2.2, totals


def test_empty_and_advanced_windows_preserve_old_receipts_and_other_spaces(tmp_path):
    database, before = seed(tmp_path, 3)
    other, _ = seed(tmp_path, 2, 'b' * 32)
    for tick, events in ((2, []), (3, [event(3), event(4)])):
        after = dict(before, tick=tick)
        database.commit(after, previous=before, kind='advance',
                        models={'history': {'events': events}, 'summary': {'tick': tick}})
        before = after
    assert database.page()['items'] == [event(i) for i in range(1, 5)]
    assert other.page()['items'] == [event(1), event(2)]
    assert database.read('summary') == {'tick': 3}


def test_existing_current_receipt_can_be_updated_without_duplicate_index(tmp_path):
    database, before = seed(tmp_path, 3)
    changed = dict(event(3), text='corrected projection')
    after = dict(before, tick=2)
    database.commit(after, previous=before, kind='projection_update',
                    models={'history': {'events': [changed, event(4)]}})
    assert database.page()['items'] == [event(1), event(2), changed, event(4)]
    assert database.page(node_id='n', limit=2)['next_cursor'] == 2


def test_projection_failure_rolls_back_state_receipts_and_index(tmp_path):
    database, before = seed(tmp_path, 3)
    with database.connection(write=True) as db:
        path = pointer(('events', 4, 'text')).replace("'", "''")
        db.execute("CREATE TRIGGER refuse_projection BEFORE INSERT ON cells "
                   "WHEN NEW.space='history' AND NEW.path='" + path + "' "
                   "BEGIN SELECT RAISE(ABORT, 'injected projection failure'); END")
    after = dict(before, tick=2)
    with pytest.raises(OSError, match='transaction failed'):
        database.commit(after, previous=before, kind='must_rollback',
                        models={'history': {'events': [event(3), event(4)]}})
    assert database.load() == before
    assert database.revision()[0] == 1
    assert database.page()['items'] == [event(i) for i in range(1, 4)]


def test_nonstandard_history_model_retains_generic_projection_semantics(tmp_path):
    database, before = seed(tmp_path, 2)
    model = {'events': [event(3)], 'extra': {'source': 'legacy'}}
    after = dict(before, tick=2)
    database.commit(after, previous=before, kind='generic', models={'history': copy.deepcopy(model)})
    assert database.page()['items'] == [event(1), event(2), event(3)]
    with database.connection() as db:
        assert database._read(db, 'history', ('extra',)) == model['extra']
