import copy
import hashlib
import json

import pytest

from mewcode.kernel.sqlite_state import RunDatabase, pointer


def seed(tmp_path):
    database = RunDatabase(tmp_path, 'a' * 32)
    value = {'nested': [[{'s/~': [None, False, i]}] for i in range(24)],
             'events': [{'seq': i, 'text': str(i)} for i in (2, 10, 90)],
             'empty': {}, 'list': [], 'locked_flow': {'nodes': [{'id': 'original'}]}}
    database.commit(value, previous={}, kind='seed')
    return database, value


def test_streamed_tree_preserves_nested_lists_numeric_order_and_scoped_roots(tmp_path):
    database, expected = seed(tmp_path)
    assert database.load() == expected
    with database.connection() as db:
        for parts in [('nested',), ('nested', 10), ('nested', 10, 0, 's/~'),
                      ('events',), ('events', 10), ('empty',), ('list',), ('locked_flow',)]:
            value = expected
            for part in parts:
                if isinstance(value, list) and value and isinstance(value[0], dict) and 'seq' in value[0]:
                    value = next(row for row in value if row['seq'] == part)
                else:
                    value = value[part]
            assert database._read(db, 'state', parts) == value


def test_cells_are_consumed_incrementally_without_fetchall(tmp_path):
    database, expected = seed(tmp_path)
    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor
        def __iter__(self):
            return iter(self.cursor)
        def fetchall(self):
            raise AssertionError('read buffered every cell')
    class Connection:
        def __init__(self, connection):
            self.connection = connection
        def execute(self, *args):
            return Cursor(self.connection.execute(*args))
    with database.connection() as db:
        actual = database._read(Connection(db), 'state')
    expected = copy.deepcopy(expected)
    expected.pop('kernel_revision')
    assert actual == expected


@pytest.mark.parametrize('corruption', ['missing_parent', 'wrong_parent', 'wrong_key', 'list_gap',
                                       'object_key', 'atom_children', 'unknown_kind'])
def test_stream_reader_rejects_malformed_trees(tmp_path, corruption):
    database, _ = seed(tmp_path)
    path = pointer(('nested', 0, 0))
    with database.connection(write=True) as db:
        if corruption == 'missing_parent':
            db.execute('DELETE FROM cells WHERE run_id=? AND space=? AND path=?',
                       (database.run_id, 'state', path))
        elif corruption == 'list_gap':
            database._remove(db, 'state', ('nested', 0))
        elif corruption == 'atom_children':
            db.execute('UPDATE cells SET kind=?, value=?, sha256=? WHERE run_id=? AND space=? AND path=?',
                       ('atom', b'1', hashlib.sha256(b'1').hexdigest(), database.run_id, 'state', path))
        else:
            field, value = {'wrong_parent': ('parent', pointer(('missing',))),
                'wrong_key': ('item_key', json.dumps('wrong')),
                'object_key': ('item_key', '1'), 'unknown_kind': ('kind', 'invalid')}[corruption]
            if corruption == 'object_key':
                path = pointer(('nested', 0, 0, 's/~'))
            db.execute(f'UPDATE cells SET {field}=? WHERE run_id=? AND space=? AND path=?',
                       (value, database.run_id, 'state', path))
    with pytest.raises(ValueError):
        database.load()
