"""Transactional current state and immutable content for explicitly migrated runs.

Legacy journals remain the authority until their schema marker is switched. A
SQLite run never replays that journal: state cells, transaction receipts and read
documents commit together. Only changed cells are written; lists with ordered
sequence numbers keep stable keys even when their display window advances.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile


SCHEMA = 1
ATOM_FIELDS = frozenset({'flow', 'locked_flow', 'initial_plan'})
INLINE_LIMIT = 4096


class RunDatabaseError(OSError):
    """Retain SQLite's cause and name the storage condition for recovery."""
    def __init__(self, path, cause):
        self.database_path = Path(path)
        self.sqlite_errorcode = getattr(cause, 'sqlite_errorcode', None)
        self.sqlite_errorname = getattr(cause, 'sqlite_errorname', 'SQLITE_ERROR')
        self.storage_full = ((self.sqlite_errorcode is not None and self.sqlite_errorcode & 255 == sqlite3.SQLITE_FULL)
                             or 'database or disk is full' in str(cause).lower())
        if self.storage_full:
            message = '运行数据盘空间不足，无法保存进度；数据库位置：' + str(self.database_path)
        else:
            message = f'run database transaction failed ({self.sqlite_errorname}): {cause}'
        super().__init__(message)


def encoded(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('ascii')


def fingerprint(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def pointer(parts):
    return ''.join('/' + json.dumps(key, ensure_ascii=True).replace('~', '~0').replace('/', '~1')
                   for key in parts)


def sequenced(value):
    return (isinstance(value, list) and bool(value)
            and all(isinstance(row, dict) and type(row.get('seq')) is int for row in value)
            and all(left['seq'] < right['seq'] for left, right in zip(value, value[1:])))


def shape(value, parts):
    if parts and parts[-1] in ATOM_FIELDS and isinstance(value, dict):
        return 'atom'
    if isinstance(value, dict):
        return 'dict'
    if isinstance(value, list):
        return 'seq' if sequenced(value) else 'list'
    return 'atom'


def children(value, kind):
    if kind == 'dict':
        return value
    if kind == 'seq':
        return {row['seq']: row for row in value}
    return dict(enumerate(value))


def delta(before, after, parts=()):
    """Typed changes; an append does not serialize the unchanged list prefix."""
    if before is after:
        return
    old_kind, new_kind = shape(before, parts), shape(after, parts)
    if old_kind != new_kind:
        yield 'set', parts, after
    elif new_kind == 'atom':
        if type(before) is not type(after) or before != after:
            yield 'set', parts, after
    else:
        old, new = children(before, old_kind), children(after, new_kind)
        for key in old.keys() - new.keys():
            yield 'remove', (*parts, key), None
        for key, value in new.items():
            if key in old:
                yield from delta(old[key], value, (*parts, key))
            else:
                yield 'set', (*parts, key), value


class RunDatabase:
    def __init__(self, root, run_id):
        self.root, self.run_id = Path(root), run_id
        self.path = self.root / 'runs.sqlite3'
        self.content = self.root / 'content-v1'
        self.logical_bytes = 0

    @contextmanager
    def connection(self, *, write=False):
        if write:
            self.root.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        else:
            if not self.path.is_file():
                raise FileNotFoundError('run read model has not been built')
            db = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True,
                                 timeout=5, isolation_level=None)
        try:
            db.execute('PRAGMA busy_timeout=5000')
            if write:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('PRAGMA synchronous=FULL')
                self._schema(db)
            elif db.execute('PRAGMA user_version').fetchone()[0] != SCHEMA:
                raise ValueError('unsupported run database schema')
            yield db
        except sqlite3.Error as exc:
            raise RunDatabaseError(self.path, exc) from exc
        finally:
            db.close()

    @staticmethod
    def _schema(db):
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if version not in {0, SCHEMA}:
            raise ValueError('unsupported run database schema')
        if version:
            return
        db.executescript('''
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, event_hash TEXT NOT NULL,
                conversation_id TEXT NOT NULL, updated_at TEXT NOT NULL, source TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS runs_conversation ON runs(conversation_id, updated_at DESC, run_id);
            CREATE TABLE IF NOT EXISTS run_views (
                run_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, conversation_id TEXT NOT NULL,
                updated_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS view_conversation ON run_views(conversation_id, updated_at DESC, run_id);
            CREATE TABLE IF NOT EXISTS cells (
                run_id TEXT NOT NULL, space TEXT NOT NULL, path TEXT NOT NULL,
                parent TEXT, item_key TEXT, ordinal INTEGER, kind TEXT NOT NULL, value BLOB, sha256 TEXT,
                PRIMARY KEY(run_id, space, path)) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS cells_parent ON cells(run_id, space, parent, item_key);
            CREATE INDEX IF NOT EXISTS cells_page ON cells(run_id, space, parent, ordinal);
            CREATE TABLE IF NOT EXISTS transactions (
                run_id TEXT NOT NULL, revision INTEGER NOT NULL, previous_hash TEXT NOT NULL,
                hash TEXT NOT NULL, kind TEXT NOT NULL, payload BLOB NOT NULL,
                logical_bytes INTEGER NOT NULL, PRIMARY KEY(run_id, revision)) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS migrations (
                run_id TEXT PRIMARY KEY, source_revision INTEGER NOT NULL,
                source_hash TEXT NOT NULL, state_hash TEXT NOT NULL, report BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS history_index (
                run_id TEXT NOT NULL, seq INTEGER NOT NULL, node_id TEXT NOT NULL, role TEXT NOT NULL,
                attempt INTEGER NOT NULL, node_attempt INTEGER NOT NULL, member_id TEXT NOT NULL,
                PRIMARY KEY(run_id,seq)) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS history_node ON history_index(run_id,node_id,attempt,node_attempt,member_id,seq);
            PRAGMA user_version=1;
            COMMIT;
        ''')

    def _put_blob(self, payload):
        digest = hashlib.sha256(payload).hexdigest()
        path = self.content / digest[:2] / (digest + '.json')
        if path.is_file():
            if path.read_bytes() != payload:
                raise ValueError('content hash collision or damaged content')
            return digest
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.pending-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            self.logical_bytes += len(payload)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return digest

    def _blob(self, digest):
        if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('invalid content reference')
        payload = (self.content / digest[:2] / (digest + '.json')).read_bytes()
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError('content integrity mismatch')
        return payload

    def _rows(self, space, value, parts=(), known=None):
        kind = shape(value, parts)
        payload = encoded(value) if kind == 'atom' else None
        digest = hashlib.sha256(payload).hexdigest() if payload is not None else None
        external = payload is not None and len(payload) > INLINE_LIMIT
        if external and (known or {}).get(pointer(parts)) != (kind, digest):
            self._put_blob(payload)
        yield (self.run_id, space, pointer(parts), pointer(parts[:-1]) if parts else None,
               json.dumps(parts[-1], ensure_ascii=True) if parts else None,
               parts[-1] if parts and type(parts[-1]) is int else None,
               kind, None if external else payload, digest)
        if kind != 'atom':
            for key, item in children(value, kind).items():
                yield from self._rows(space, item, (*parts, key), known)

    def _insert(self, db, rows):
        def measured():
            for row in rows:
                self.logical_bytes += len(row[2]) + len(row[6]) + len(row[8] or '') + len(row[7] or b'')
                yield row
        db.executemany('INSERT OR REPLACE INTO cells VALUES(?,?,?,?,?,?,?,?,?)', measured())

    def _set(self, db, space, parts, value):
        self._remove(db, space, parts)
        self._insert(db, self._rows(space, value, parts))

    def _remove(self, db, space, parts):
        key = pointer(parts)
        # Keep each predicate as a primary-key seek. Combining the point and
        # subtree with OR makes SQLite scan every cell in this run/space.
        # Both deletes stay inside the caller's existing write transaction.
        db.execute('DELETE FROM cells WHERE run_id=? AND space=? AND path=?',
                   (self.run_id, space, key))
        db.execute('DELETE FROM cells WHERE run_id=? AND space=? AND path>=? AND path<?',
                   (self.run_id, space, key + '/', key + '/\uffff'))

    def _read(self, db, space, parts=()):
        key = pointer(parts)
        rows = db.execute('SELECT path,parent,item_key,kind,value,sha256 FROM cells '
                          'WHERE run_id=? AND space=? AND path=? UNION ALL '
                          'SELECT path,parent,item_key,kind,value,sha256 FROM cells '
                          'WHERE run_id=? AND space=? AND path>=? AND path<? ORDER BY path',
                          (self.run_id, space, key, self.run_id, space,
                           key + '/', key + '/\uffff'))
        # A path sorts before all its children. Attach each row immediately;
        # retain only container locations, not every atom plus several copies
        # of its path/parent metadata. Lists keep numeric order separately.
        containers, found, result = {}, False, None
        for path, parent, item_key, kind, payload, digest in rows:
            if kind not in {'dict', 'list', 'seq', 'atom'}:
                raise ValueError('invalid state cell kind')
            if kind == 'atom':
                data = payload if payload is not None else self._blob(digest)
                if hashlib.sha256(data).hexdigest() != digest:
                    raise ValueError('state cell integrity mismatch')
                value = json.loads(data)
            else:
                value = {} if kind == 'dict' else []
            if path == key:
                result, found = value, True
            else:
                item = json.loads(item_key)
                owner = containers.get(parent)
                if owner is None or path != parent + pointer((item,)):
                    raise ValueError('invalid state cell parent')
                parent_kind, _, items = owner
                if parent_kind == 'dict' and not isinstance(item, str):
                    raise ValueError('invalid state object key')
                if parent_kind != 'dict' and type(item) is not int:
                    raise ValueError('invalid state list index')
                items[item] = value
            if kind != 'atom':
                containers[path] = (kind, value, value if kind == 'dict' else {})
        if not found:
            raise LookupError('run read model is not ready')
        for kind, value, items in containers.values():
            if kind == 'dict':
                continue
            indices = sorted(items)
            if kind == 'list' and indices != list(range(len(items))):
                raise ValueError('non-contiguous state list')
            value.extend(items[index] for index in indices)
        return result

    def revision(self, *, missing_ok=False):
        with self.connection() as db:
            row = db.execute('SELECT revision,event_hash FROM runs WHERE run_id=?', (self.run_id,)).fetchone()
            if row is None:
                if missing_ok:
                    return None
                raise ValueError('run database entry missing')
            return row

    def stamp(self):
        result = []
        for path in (self.path, Path(str(self.path) + '-wal')):
            try:
                stat = path.stat()
                result.append((stat.st_size, stat.st_mtime_ns))
            except FileNotFoundError:
                result.append(None)
        return tuple(result)

    def delete(self):
        """Remove this run and its projections; shared content stays untouched."""
        if not self.path.is_file():
            return
        with self.connection(write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            for table in ('history_index', 'cells', 'transactions', 'migrations', 'run_views', 'runs'):
                db.execute('DELETE FROM ' + table + ' WHERE run_id=?', (self.run_id,))
            db.commit()

    def load(self):
        with self.connection() as db:
            db.execute('BEGIN')
            row = db.execute('SELECT revision,event_hash FROM runs WHERE run_id=?', (self.run_id,)).fetchone()
            if row is None:
                raise ValueError('run database entry missing')
            result = self._read(db, 'state')
            result['kernel_revision'] = row[0]
            return result

    def repair_loop_checkpoint(self, archive, checkpoint_id, *, expected_revision):
        """Fill missing loop cells from an exact, verified stopped checkpoint.

        Existing cells must all match. This cannot restore older progress,
        change budgets, replace receipts, or edit authorizations.
        """
        meta, snapshot = archive.load(self.run_id, checkpoint_id)
        loops = snapshot.get('loop_states')
        if meta.get('kind') != 'turn_end' or meta.get('status') != 'paused' or not isinstance(loops, dict):
            raise ValueError('需要暂停时刻的完整循环归档')
        space = 'state'
        subtrees = {('loop_states',): loops}
        with self.connection(write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            revision, previous_hash = db.execute('SELECT revision,event_hash FROM runs WHERE run_id=?', (self.run_id,)).fetchone()
            if (revision != expected_revision or self._read(db,'state',('status',)) != 'paused'
                    or self._read(db,'state',('active_node_ids',))
                    or any(self._read(db,'state',(field,)) != snapshot[field]
                           for field in ('attempt','last_seq','conversation_id'))):
                raise ValueError('暂停状态或归档版本已变化，未修复')
            # The UI mirror can have a newer clock tick than the archive. Only
            # recover its identical budget receipts; preserve all other fields.
            mirror = ('flow_state', 'loop_state')
            if db.execute('SELECT 1 FROM cells WHERE run_id=? AND space=? AND path=?',
                          (self.run_id, space, pointer(mirror))).fetchone():
                loop_key = self._read(db, space, ('flow_state', 'loop_state_key'))
                source = loops.get(loop_key)
                if not isinstance(source, dict) or any(
                    self._read(db, space, mirror + (field,)) != source.get(field)
                    for field in ('contract_hash', 'round', 'tokens')
                ):
                    raise ValueError('循环镜像与归档身份不一致，不能覆盖')
                if 'segment_budget_receipts' in source:
                    subtrees[mirror + ('segment_budget_receipts',)] = source['segment_budget_receipts']
            expected, actual = {}, {}
            for parts, value in subtrees.items():
                key = pointer(parts)
                expected.update((row[2], row) for row in self._rows(space, value, parts))
                actual.update((row[2], row) for row in db.execute('SELECT * FROM cells WHERE run_id=? AND space=? '
                    'AND (path=? OR (path>=? AND path<?))', (self.run_id,space,key,key+'/',key+'/\uffff')))
            if any(path not in expected or row != expected[path] for path,row in actual.items()):
                raise ValueError('现存循环记录与归档不一致，不能覆盖')
            missing = sorted(expected.keys() - actual.keys())
            if not missing:
                return {'repaired':False,'missing_cells':0,'revision':revision}
            self._insert(db,(expected[path] for path in missing))
            if any(self._read(db,space,parts) != value for parts,value in subtrees.items()):
                raise ValueError('补齐后循环状态未通过完整性核验')
            event = {'sequence':revision+1,'previous_hash':previous_hash,'kind':'loop_checkpoint_cell_repair',
                'changes':[['set',list(parts),{'sha256':self._put_blob(encoded(value))}]
                           for parts,value in subtrees.items()],
                'checkpoint_id':checkpoint_id,'checkpoint_sha256':meta['sha256'],'added_paths':missing}
            payload=encoded(event);proof=hashlib.sha256(payload).hexdigest()
            db.execute('UPDATE runs SET revision=?,event_hash=? WHERE run_id=?',(revision+1,proof,self.run_id))
            db.execute('UPDATE run_views SET revision=? WHERE run_id=?',(revision+1,self.run_id))
            db.execute('INSERT INTO transactions VALUES(?,?,?,?,?,?,?)',
                (self.run_id,revision+1,previous_hash,proof,event['kind'],payload,len(payload)+self.logical_bytes))
            db.commit()
        return {'repaired':True,'missing_cells':len(missing),'revision':revision+1,'checkpoint_id':checkpoint_id}

    def read(self, space, parts=()):
        if space not in {'public', 'workbench', 'aux', 'summary'}:
            raise ValueError('not a public read model')
        with self.connection() as db:
            db.execute('BEGIN')
            return self._read(db, space, parts)

    def page(self, *, after=-1, limit=50, node_id='', attempt=0, node_attempt=0, member_id=None):
        if type(after) is not int or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid history cursor or page size')
        with self.connection() as db:
            db.execute('BEGIN')
            filters, args = ['run_id=?', 'seq>?'], [self.run_id, after]
            for name, value in [('node_id', node_id), ('attempt', attempt), ('node_attempt', node_attempt), ('member_id', member_id)]:
                if value or (name == 'member_id' and value is not None):
                    filters.append(name + '=?')
                    args.append(value)
            rows = db.execute('SELECT seq FROM history_index WHERE ' + ' AND '.join(filters)
                              + ' ORDER BY seq LIMIT ?', [*args, limit + 1]).fetchall()
            selected = rows[:limit]
            items = [self._read(db, 'history', ('events', row[0])) for row in selected]
            revision = db.execute('SELECT revision FROM run_views WHERE run_id=?', (self.run_id,)).fetchone()[0]
            return dict(items=items, next_cursor=selected[-1][0] if len(rows) > limit else None,
                        source_revision=revision)

    def summaries(self, conversation_id, *, limit=50, before=None, chat=False, ids_only=False):
        if not 1 <= limit <= 100:
            raise ValueError('invalid summary page size')
        with self.connection() as db:
            db.execute('BEGIN')
            rows = db.execute('SELECT run_id FROM run_views WHERE conversation_id=? '
                              'ORDER BY updated_at DESC, run_id LIMIT ?', (conversation_id, limit)).fetchall()
            if ids_only:
                return [{'run_id': row[0]} for row in rows]
            result = []
            for row in rows:
                original = self.run_id
                try:
                    self.run_id = row[0]
                    if chat:
                        from mewcode.kernel.chat_view import read
                        result.append(read(self, db))
                    else:
                        result.append(self._read(db, 'summary'))
                finally:
                    self.run_id = original
            return result

    def _model_signatures(self, db, space, model):
        # History retains receipts that have left the display window. Reading
        # every retained cell on each commit makes a fixed-size window cost
        # proportional to the entire run. Only the supplied event subtrees
        # can be updated here; older receipts are neither touched nor removed.
        if (space == 'history' and isinstance(model, dict) and set(model) == {'events'}
                and (model['events'] == [] or sequenced(model['events']))):
            known = {}
            for key in ('', pointer(('events',))):
                known.update((path, (kind, digest)) for path, kind, digest in db.execute(
                    'SELECT path,kind,sha256 FROM cells WHERE run_id=? AND space=? AND path=?',
                    (self.run_id, space, key)))
            for event in model['events']:
                key = pointer(('events', event['seq']))
                known.update((path, (kind, digest)) for path, kind, digest in db.execute(
                    'SELECT path,kind,sha256 FROM cells WHERE run_id=? AND space=? AND path=? UNION ALL '
                    'SELECT path,kind,sha256 FROM cells WHERE run_id=? AND space=? AND path>=? AND path<?',
                    (self.run_id, space, key, self.run_id, space, key + '/', key + '/\uffff')))
            return known
        return {path: (kind, digest) for path, kind, digest in db.execute(
            'SELECT path,kind,sha256 FROM cells WHERE run_id=? AND space=?', (self.run_id, space))}

    def _models(self, db, models, revision, state, *, previous_models=None):
        for space, model in models.items():
            if (space == 'history' and model == {'events': []} and db.execute(
                    'SELECT 1 FROM cells WHERE run_id=? AND space=? AND path=?',
                    (self.run_id, space, pointer(('events',)))).fetchone()):
                # An empty incremental batch cannot turn a retained sequence
                # into an ordinary empty list while keeping its old children.
                continue
            if space != 'history' and previous_models is not None and space in previous_models:
                for action, parts, value in delta(previous_models[space], model):
                    if action == 'remove':
                        self._remove(db, space, parts)
                    else:
                        self._set(db, space, parts, value)
                continue
            known = self._model_signatures(db, space, model)
            retained, updates = set(), []
            for row in self._rows(space, model, known=known):
                retained.add(row[2])
                if known.get(row[2]) != (row[6], row[8]):
                    updates.append(row)
            self._insert(db, updates)
            if space == 'history':
                # A display window advancing must not delete earlier receipts.
                db.executemany('INSERT INTO history_index VALUES(?,?,?,?,?,?,?) ON CONFLICT DO NOTHING',
                    [(self.run_id, event['seq'], str(event.get('node_id', '')), str(event.get('role', '')),
                      int(event.get('attempt') or 0), int(event.get('node_attempt') or 0), str(event.get('member_id', '')))
                     for event in model.get('events', []) if type(event.get('seq')) is int
                     and pointer(('events', event['seq'])) not in known])
            else:
                db.executemany('DELETE FROM cells WHERE run_id=? AND space=? AND path=?',
                               [(self.run_id, space, path) for path in known.keys() - retained])
        db.execute('INSERT INTO run_views VALUES(?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET '
                   'revision=excluded.revision,conversation_id=excluded.conversation_id,updated_at=excluded.updated_at',
                   (self.run_id, revision, str(state.get('conversation_id', '')), str(state.get('updated_at', ''))))

    def publish_models(self, state, models):
        """Legacy authority: publish a rebuildable projection after its commit."""
        with self.connection(write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT revision FROM run_views WHERE run_id=?', (self.run_id,)).fetchone()
            revision = state.get('kernel_revision', 0)
            if current and current[0] > revision:
                return
            self._models(db, models, revision, state)
            db.commit()

    def commit(self, state, *, previous, kind, models=None, previous_models=None):
        self.logical_bytes = 0
        expected = state.get('kernel_revision', 0)
        clean = {key: value for key, value in state.items() if key != 'kernel_revision'}
        old = {key: value for key, value in previous.items() if key != 'kernel_revision'}
        operations = list(delta(old, clean))
        if not operations:
            return expected
        with self.connection(write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT revision,event_hash FROM runs WHERE run_id=?', (self.run_id,)).fetchone()
            revision, previous_hash = row if row else (0, '')
            if revision != expected:
                raise ValueError('kernel writer is stale; refresh before mutating')
            if row is None:
                self._set(db, 'state', (), {})
            receipts = []
            for action, parts, value in operations:
                if action == 'remove':
                    self._remove(db, 'state', parts)
                    receipts.append([action, list(parts)])
                else:
                    self._set(db, 'state', parts, value)
                    data = encoded(value)
                    receipts.append([action, list(parts), {'sha256': self._put_blob(data)}
                                     if len(data) > INLINE_LIMIT else value])
            event = dict(sequence=revision + 1, previous_hash=previous_hash, kind=kind, changes=receipts)
            payload = encoded(event)
            proof = hashlib.sha256(payload).hexdigest()
            db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET '
                       'revision=excluded.revision,event_hash=excluded.event_hash,conversation_id=excluded.conversation_id,'
                       'updated_at=excluded.updated_at',
                       (self.run_id, revision + 1, proof, str(clean.get('conversation_id', '')),
                        str(clean.get('updated_at', '')), 'sqlite'))
            if models:
                self._models(db, models, revision + 1, clean, previous_models=previous_models)
            db.execute('INSERT INTO transactions VALUES(?,?,?,?,?,?,?)',
                       (self.run_id, revision + 1, previous_hash, proof, kind, payload,
                        len(payload) + self.logical_bytes))
            db.commit()
        state['kernel_revision'] = revision + 1
        return revision + 1

    def import_state(self, state, *, source_hash, report, models=None):
        revision = state['kernel_revision']
        clean = {key: value for key, value in state.items() if key != 'kernel_revision'}
        with self.connection(write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT source_hash,state_hash FROM migrations WHERE run_id=?',
                                  (self.run_id,)).fetchone()
            state_hash = fingerprint(clean)
            if existing and existing != (source_hash, state_hash):
                raise ValueError('migration source changed')
            if not existing:
                if db.execute('SELECT 1 FROM runs WHERE run_id=?', (self.run_id,)).fetchone():
                    raise ValueError('run already exists in database')
                self._set(db, 'state', (), clean)
                db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?)',
                           (self.run_id, revision, source_hash, str(clean.get('conversation_id', '')),
                            str(clean.get('updated_at', '')), 'legacy'))
                if models:
                    self._models(db, models, revision, clean)
                db.execute('INSERT INTO migrations VALUES(?,?,?,?,?)',
                           (self.run_id, revision, source_hash, state_hash, encoded(report)))
            restored = self._read(db, 'state')
            if fingerprint(restored) != state_hash:
                raise ValueError('migration did not preserve complete state')
            db.commit()
        return dict(revision=revision, source_hash=source_hash, state_sha256=state_hash)
