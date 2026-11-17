"""A single receipt lookup/replacement must not scan unrelated run history."""
import copy

import pytest

from mewcode.kernel.sqlite_state import RunDatabase, pointer


def seed(root, count):
    database = RunDatabase(root, 'a' * 32)
    value = {'run_id': database.run_id, 'status': 'paused',
             'events': [{'seq': i, 'text': 'receipt'} for i in range(1, count + 1)]}
    database.commit(value, previous={}, kind='test_seed')
    return database, value


@pytest.mark.parametrize('operation', ['read', 'remove'])
def test_single_cell_sql_work_is_bounded_as_unrelated_history_grows(tmp_path, operation):
    costs = []
    for count in (10, 5000):
        database, _ = seed(tmp_path / str(count), count)
        with database.connection(write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            ticks = []
            db.set_progress_handler(lambda: ticks.append(1) or 0, 100)
            if operation == 'read':
                assert database._read(db, 'state', ('status',)) == 'paused'
            else:
                database._remove(db, 'state', ('status',))
            db.set_progress_handler(None, 0)
            costs.append(len(ticks) * 100)
            db.rollback()
    # VM work, not machine speed: an indexed point/subtree query stays small.
    assert costs[1] < 3000, costs
    assert costs[1] <= costs[0] + 500, costs


@pytest.mark.parametrize('key', ['a', 'a/b', 'a~b', '原文', '"quoted"', ''])
def test_subtree_replacement_preserves_prefix_siblings_other_runs_and_spaces(tmp_path, key):
    database = RunDatabase(tmp_path, 'a' * 32)
    other = RunDatabase(tmp_path, 'b' * 32)
    subtree = {'inner/~': [0, False, None, {'text': 'original'}]}
    value = {key: subtree, key + 'extra': {'keep': True}, 'elsewhere': [1, 2]}
    database.commit(value, previous={}, kind='seed')
    other_value = copy.deepcopy(value)
    other_value.pop('kernel_revision')
    other.commit(other_value, previous={}, kind='seed')
    with database.connection(write=True) as db:
        db.execute('BEGIN IMMEDIATE')
        database._set(db, 'history', (), {key: subtree})
        db.commit()
    with database.connection() as db:
        assert database._read(db, 'state', (key,)) == subtree
    updated = copy.deepcopy(value)
    updated[key] = {'new': 'replacement'}
    database.commit(updated, previous=value, kind='replace_subtree')
    assert database.load() == updated
    assert other.load() == other_value
    with database.connection() as db:
        assert database._read(db, 'history') == {key: subtree}
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_failed_descendant_delete_rolls_back_root_delete_and_revision(tmp_path):
    database = RunDatabase(tmp_path, 'a' * 32)
    before = {'selected': {'child': 'keep'}, 'elsewhere': 'unchanged'}
    database.commit(before, previous={}, kind='seed')
    revision = database.revision()
    with database.connection(write=True) as db:
        path = pointer(('selected', 'child')).replace("'", "''")
        db.execute("CREATE TRIGGER refuse_child BEFORE DELETE ON cells "
                   "WHEN OLD.space='state' AND OLD.path='" + path + "' "
                   "BEGIN SELECT RAISE(ABORT, 'injected delete failure'); END")
    after = copy.deepcopy(before)
    after['selected'] = 'replacement'
    with pytest.raises(OSError, match='transaction failed'):
        database.commit(after, previous=before, kind='should_rollback')
    assert database.revision() == revision
    assert database.load() == before


def test_root_read_and_delete_are_scoped_to_one_run_and_space(tmp_path):
    database, value = seed(tmp_path, 2)
    other = RunDatabase(tmp_path, 'b' * 32)
    saved = {'run_id': other.run_id, 'kept': True}
    other.commit(saved, previous={}, kind='seed')
    with database.connection(write=True) as db:
        db.execute('BEGIN IMMEDIATE')
        database._set(db, 'history', (), {'event': 'kept'})
        database._remove(db, 'state', ())
        with pytest.raises(LookupError):
            database._read(db, 'state')
        assert database._read(db, 'history') == {'event': 'kept'}
        assert other._read(db, 'state')['kept'] is True
        db.rollback()
    assert database.load() == value
