"""Append-only, fsynced state transactions; checkpoints are disposable caches.

Each committed event is an immutable file. A process dying during a write leaves
an uncommitted temporary file, never a partial event or an overwritten history.
The existing host write-lease lock serializes writers across processes.
"""
from __future__ import annotations

import copy
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading

from mewcode.orchestration.write_lease import WriteLeaseStore


# Only verified, immutable event prefixes are cached, never disk checkpoints.
# Instances are short-lived, so share a bounded cache across store operations.
_SCAN_CACHE = OrderedDict()
_SCAN_CACHE_LOCK = threading.Lock()
_SCAN_CACHE_LIMIT = 8
_SQL_CACHE = OrderedDict()

HEADER_FORMAT = 'mewcode.kernel.events'
COLLABORATION_V2 = 'mewcode.collaboration.v2'


def _journal_schema_version(state):
    """Schema 2 fences collaboration v2 state; classic and collaboration v1 stay schema 1."""
    collaboration = state.get('collaboration')
    if isinstance(collaboration, dict) and collaboration.get('format') == COLLABORATION_V2:
        return 2
    return 2 if state.get('format') == COLLABORATION_V2 else 1


def _file_stamp(path):
    stat = path.stat()
    return path.name, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def changes(before, after, path=()):
    if isinstance(before, dict) and isinstance(after, dict):
        result = [{'op': 'remove', 'path': [*path, key]} for key in sorted(before.keys() - after.keys())]
        for key in sorted(after):
            result.extend(changes(before.get(key), after[key], (*path, key)) if key in before else
                          [{'op': 'set', 'path': [*path, key], 'value': copy.deepcopy(after[key])}])
        return result
    return [] if before == after else [{'op': 'set', 'path': list(path), 'value': copy.deepcopy(after)}]


def reduce_changes(state, operations):
    return _apply_changes(copy.deepcopy(state), operations)


def _apply_changes(state, operations, *, copy_values=True):
    """Replay into a private accumulator without copying its whole history."""
    for operation in operations:
        path = operation['path']
        if not isinstance(path, list) or any(not isinstance(key, str) for key in path):
            raise ValueError('invalid kernel transaction path')
        if not path:
            if operation['op'] != 'set' or not isinstance(operation.get('value'), dict):
                raise ValueError('invalid root state transaction')
            state = copy.deepcopy(operation['value']) if copy_values else operation['value']
            continue
        target = state
        for key in path[:-1]:
            target = target[key]
        if operation['op'] == 'set':
            target[path[-1]] = copy.deepcopy(operation['value']) if copy_values else operation['value']
        elif operation['op'] == 'remove':
            del target[path[-1]]
        else:
            raise ValueError('invalid kernel transaction operation')
    return state


def _event_documents(paths):
    """Overlap at most four file reads, retaining transaction order and bounded memory."""
    def read(path):
        return json.loads(path.read_text(encoding='utf-8'))

    if len(paths) < 4:
        for path in paths:
            yield path, read(path)
        return
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix='kernel-event-read') as pool:
        for start in range(0, len(paths), 4):
            batch = paths[start:start + 4]
            yield from zip(batch, pool.map(read, batch))


class EventJournal:
    def __init__(self, root, run_id, *, storage='legacy'):
        if not re.fullmatch(r'[a-f0-9]{32}', run_id):
            raise ValueError('invalid kernel run identity')
        self.directory = Path(root) / 'kernel-events' / run_id
        self.run_id = run_id
        self.root = Path(root)
        self.storage = storage

    @property
    def exists(self):
        return (self.directory / 'header.json').is_file()

    def _lease(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        return WriteLeaseStore(self.directory / 'writer.json', sandbox=None)._locked()

    def _header_document(self, version):
        return {'schema_version': version, 'run_id': self.run_id, 'format': HEADER_FORMAT}

    def _read_header(self):
        """Schema 3 explicitly fences SQLite runs from older journal writers."""
        header = json.loads((self.directory / 'header.json').read_text(encoding='utf-8'))
        if header == self._header_document(1):
            return 1
        if header == self._header_document(2):
            return 2
        if header == self._header_document(3):
            return 3
        raise ValueError('kernel journal requires explicit version migration')

    @property
    def sqlite(self):
        return self.exists and self._read_header() == 3

    def _database(self):
        from mewcode.kernel.sqlite_state import RunDatabase
        return RunDatabase(self.root, self.run_id)

    def _sql_load(self, *, detached=True):
        database = self._database()
        revision, proof = database.revision()
        stamp = database.stamp()
        key = (str(self.root.resolve()), self.run_id)
        with _SCAN_CACHE_LOCK:
            cached = _SQL_CACHE.get(key)
            # Every run shares this database and WAL. Another run's commit
            # changes the file stamp but cannot change this immutable revision.
            # The run's own revision/hash fence is the cache authority.
            if cached and cached[:2] == (revision, proof):
                return copy.deepcopy(cached[3]) if detached else cached[3]
        value = database.load()
        with _SCAN_CACHE_LOCK:
            _SQL_CACHE[key] = (revision, proof, stamp, copy.deepcopy(value))
            _SQL_CACHE.move_to_end(key)
            while len(_SQL_CACHE) > _SCAN_CACHE_LIMIT:
                _SQL_CACHE.popitem(last=False)
        return value

    def migrate_sqlite(self, *, expected_revision, expected_hash, models=None):
        """One stopped-writer migration; retain all legacy transactions verbatim."""
        with self._lease():
            if self.sqlite:
                return dict(already_migrated=True, revision=self._database().revision()[0])
            value, sequence, proof = self._scan()
            if (sequence, proof) != (expected_revision, expected_hash):
                raise ValueError('migration source changed')
            value['kernel_revision'] = sequence
            report = self._database().import_state(value, source_hash=proof,
                report={'legacy_header': self._read_header(), 'verified_transactions': sequence}, models=models)
            # The routing marker changes only after complete-state equality and a
            # durable database commit. Older binaries reject this schema.
            self._replace_header(self._header_document(3))
            return report

    def _atomic_write(self, target, data):
        handle, name = tempfile.mkstemp(prefix='.pending-', dir=self.directory)
        try:
            with os.fdopen(handle, 'wb') as stream:
                stream.write(canonical(data))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, target)
            if os.name != 'nt':
                directory = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            Path(name).unlink(missing_ok=True)

    def _append_file(self, target, data):
        if target.exists():
            raise ValueError('kernel history is immutable')
        self._atomic_write(target, data)

    def _replace_header(self, data):
        """Atomic in-place header rewrite used only for a schema upgrade."""
        self._atomic_write(self.directory / 'header.json', data)

    def _scan(self):
        if not self.exists:
            return {}, 0, ''
        if self._read_header() == 3:
            value = self._sql_load()
            sequence, proof = self._database().revision()
            value.pop('kernel_revision', None)
            return value, sequence, proof
        paths = sorted(self.directory.glob('[0-9]*.json'))
        stamps = tuple(_file_stamp(path) for path in paths)
        key = os.path.normcase(os.path.abspath(self.directory))
        header_stamp = _file_stamp(self.directory / 'header.json')
        with _SCAN_CACHE_LOCK:
            cached = _SCAN_CACHE.get(key)
            if cached is not None:
                _SCAN_CACHE.move_to_end(key)
        state, sequence, previous_hash = {}, 0, ''
        if (cached is not None and cached[0] == header_stamp
                and stamps[:len(cached[1])] == cached[1]):
            state, sequence, previous_hash = copy.deepcopy(cached[2]), cached[3], cached[4]
        for path, event in _event_documents(paths[sequence:]):
            proof = event.pop('sha256')
            if (event['sequence'] != sequence + 1 or event['previous_hash'] != previous_hash
                    or digest(event) != proof or path.name != f"{event['sequence']:016d}.json"):
                raise ValueError('kernel event integrity or sequence mismatch')
            # Decoded event values are private to this scan. Their hash was
            # checked above, and the verified cache below still owns a copy.
            state = _apply_changes(state, event['changes'], copy_values=False)
            sequence, previous_hash = event['sequence'], proof
        with _SCAN_CACHE_LOCK:
            _SCAN_CACHE[key] = (header_stamp, stamps, copy.deepcopy(state), sequence, previous_hash)
            _SCAN_CACHE.move_to_end(key)
            while len(_SCAN_CACHE) > _SCAN_CACHE_LIMIT:
                _SCAN_CACHE.popitem(last=False)
        return state, sequence, previous_hash

    def load(self):
        if not self.exists:
            return None
        with self._lease():
            state, sequence, _ = self._scan()
            if not sequence:
                return None
            state['kernel_revision'] = sequence
            return state

    def commit(self, state, *, kind='state_changed', models=None,
               previous=None, previous_models=None, cache_owned=False):
        with self._lease():
            if (self.directory / 'closed.json').exists():
                raise OSError('kernel journal belongs to a deleted conversation')
            if self.sqlite or (not self.exists and self.storage == 'sqlite'):
                if not self.exists:
                    # Fence old writers before creating authoritative SQL state.
                    # A failed first transaction leaves an empty, retryable run.
                    with self._database().connection(write=True):
                        pass
                    self._append_file(self.directory / 'header.json', self._header_document(3))
                prior_revision = self._database().revision(missing_ok=True)
                # The delta writer only reads this committed cache entry.
                # Public loads stay detached; the new state is copied into
                # the cache only after the SQLite transaction succeeds.
                incremental = previous is not None
                if previous is None:
                    previous = self._sql_load(detached=False) if prior_revision is not None else {}
                elif previous.get('kernel_revision', 0) != state.get('kernel_revision', 0):
                    raise ValueError('incremental previous revision mismatch')
                sequence = self._database().commit(state, previous=previous, kind=kind,
                    models=models, previous_models=previous_models)
                key = (str(self.root.resolve()), self.run_id)
                with _SCAN_CACHE_LOCK:
                    revision, proof = self._database().revision()
                    if incremental and not cache_owned:
                        # The store owns shared COW subtrees; do not expose them
                        # through the journal cache or clone historical context.
                        _SQL_CACHE.pop(key, None)
                    else:
                        _SQL_CACHE[key] = (revision, proof, self._database().stamp(),
                                           state if cache_owned else copy.deepcopy(state))
                        _SQL_CACHE.move_to_end(key)
                    while len(_SQL_CACHE) > _SCAN_CACHE_LIMIT:
                        _SQL_CACHE.popitem(last=False)
                return sequence
            previous, sequence, previous_hash = self._scan()
            if state.get('kernel_revision', 0) != sequence:
                raise ValueError('kernel writer is stale; refresh before mutating')
            clean = {key: value for key, value in state.items() if key != 'kernel_revision'}
            required = _journal_schema_version(clean)
            # Fence collaboration v2 before any new event: the old program must fail closed.
            # Upgrade an existing header even when the commit carries no operations, and
            # never downgrade a journal that is already schema 2.
            if self.exists and required > self._read_header():
                self._replace_header(self._header_document(required))
            operations = changes(previous, clean)
            if not operations:
                return sequence
            if not self.exists:
                self._append_file(self.directory / 'header.json', self._header_document(required))
            event = {'sequence': sequence + 1, 'previous_hash': previous_hash, 'kind': kind, 'changes': operations}
            self._append_file(self.directory / f'{sequence + 1:016d}.json', {**event, 'sha256': digest(event)})
            state['kernel_revision'] = sequence + 1
            return sequence + 1

    def delete(self):
        """Explicit conversation deletion; never used by replay or recovery."""
        import shutil
        if not self.directory.exists():
            return
        expected_parent = self.directory.parent.parent.resolve() / 'kernel-events'
        resolved = self.directory.resolve()
        if resolved.parent != expected_parent or self.directory.is_symlink():
            raise ValueError('journal deletion target escaped its run directory')
        # No file/lease handles stay open across the removal on Windows.
        with self._lease():
            if not (self.directory / 'closed.json').exists():
                self._append_file(self.directory / 'closed.json', {'reason': 'conversation_deleted'})
            self._database().delete()
            with _SCAN_CACHE_LOCK:
                _SQL_CACHE.pop((str(self.root.resolve()), self.run_id), None)
                _SCAN_CACHE.pop(os.path.normcase(os.path.abspath(self.directory)), None)
        shutil.rmtree(resolved)
