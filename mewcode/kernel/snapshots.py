"""Scoped workspace snapshots, with private indexes and no branch/index edits."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import uuid

from mewcode.kernel.events import canonical, digest
from mewcode.tools.base import SKIP_DIRS
from mewcode.worktree.manager import WorktreeManager


class SnapshotError(RuntimeError):
    pass


class SnapshotManager:
    def __init__(self, workspace, *, max_file_bytes=50 * 1024 * 1024):
        self.workspace = Path(workspace).resolve(strict=True)
        self.control = self.workspace / '.mewcode'
        if self.control.exists() and (self.control.resolve() != self.control or self._linked(self.control)):
            raise SnapshotError('snapshot control directory must not be redirected')
        self.directory = self.control / 'kernel-snapshots'
        for internal in (self.directory, self.control / 'shadow.git', self.control / 'snapshot-worktrees'):
            if internal.exists() and (self._linked(internal) or internal.resolve().parent != self.control):
                raise SnapshotError('snapshot storage must stay inside its workspace control directory')
        self.directory.mkdir(parents=True, exist_ok=True)
        self.max_file_bytes = max_file_bytes
        self.git_dir = None
        probe = self._git(['rev-parse', '--absolute-git-dir'], check=False)
        top = self._git(['rev-parse', '--show-toplevel'], check=False)
        self.shadow = (probe.returncode != 0 or top.returncode != 0
                       or Path(top.stdout.decode('utf-8').strip()).resolve() != self.workspace)
        if self.shadow:
            self.git_dir = self.control / 'shadow.git'
            if not self.git_dir.exists():
                self._git(['init', '--bare', str(self.git_dir)])
        else:
            self.git_dir = Path(probe.stdout.decode('utf-8').strip())

    @staticmethod
    def _linked(path):
        info = path.lstat()
        return path.is_symlink() or bool(getattr(info, 'st_file_attributes', 0)
                                       & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024))

    def _git(self, args, *, data=None, index=None, check=True):
        prefix = ['git', '-c', 'core.autocrlf=false', '-c', 'core.longpaths=true', '-c', 'core.hooksPath=' + os.devnull]
        if self.git_dir is not None and args[0] != 'init':
            prefix += ['--git-dir=' + str(self.git_dir), '--work-tree=' + str(self.workspace)]
        env = {key: value for key, value in os.environ.items()
               if key not in {'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE'}}
        env.update(GIT_TERMINAL_PROMPT='0', GIT_ASKPASS='', GIT_AUTHOR_NAME='MewCode Snapshot',
                   GIT_AUTHOR_EMAIL='snapshot@localhost', GIT_COMMITTER_NAME='MewCode Snapshot',
                   GIT_COMMITTER_EMAIL='snapshot@localhost')
        if index is not None:
            env['GIT_INDEX_FILE'] = str(index)
        result = subprocess.run([*prefix, *args], input=data, cwd=self.workspace, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        if check and result.returncode:
            raise SnapshotError(result.stderr.decode('utf-8', errors='replace')[:500])
        return result

    def inventory(self):
        candidates = []
        for parent, directories, filenames in os.walk(self.workspace, followlinks=False):
            directories[:] = sorted(name for name in directories if name not in SKIP_DIRS
                                    and not name.startswith('.pytest-') and not self._linked(Path(parent) / name))
            candidates.extend(Path(parent) / name for name in sorted(filenames))
        if not candidates:
            return {}
        # Git's parser preserves negated patterns and nested .gitignore files.
        encoded = b'\0'.join(path.relative_to(self.workspace).as_posix().encode('utf-8') for path in candidates) + b'\0'
        ignored = self._git(['check-ignore', '--no-index', '-z', '--stdin'], data=encoded, check=False)
        if ignored.returncode not in {0, 1}:
            raise SnapshotError('cannot establish .gitignore snapshot scope')
        exclusions = {value.decode('utf-8') for value in ignored.stdout.split(b'\0') if value}
        rows = {}
        for path in candidates:
            relative = path.relative_to(self.workspace).as_posix()
            if relative in exclusions or relative == '.git':
                continue
            if self._linked(path):
                rows[relative] = {'sha256': digest(['link', os.readlink(path)]), 'covered': False, 'kind': 'link'}
                continue
            before = path.stat()
            if not stat.S_ISREG(before.st_mode):
                rows[relative] = {'sha256': digest(['unsupported', before.st_mode]), 'covered': False, 'kind': 'unsupported'}
                continue
            hasher = hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    hasher.update(block)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise SnapshotError('workspace changed while snapshotting: ' + relative)
            rows[relative] = {'sha256': hasher.hexdigest(), 'size': after.st_size,
                              'covered': after.st_size <= self.max_file_bytes,
                              'permissions': stat.S_IMODE(after.st_mode),
                              'mode': '100755' if after.st_mode & stat.S_IXUSR else '100644'}
        return rows

    def _capture(self, *, run_id, node_id, reason, event_sequence):
        identity = uuid.uuid4().hex
        index = self.directory / (identity + '.index')
        rows = self.inventory()
        try:
            self._git(['read-tree', '--empty'], index=index)
            entries = []
            for relative, row in rows.items():
                if not row['covered']:
                    continue
                data = (self.workspace / relative).read_bytes()
                if hashlib.sha256(data).hexdigest() != row['sha256']:
                    raise SnapshotError('workspace changed before snapshot commit')
                blob = self._git(['hash-object', '-w', '--stdin'], data=data).stdout.decode().strip()
                row['blob'] = blob
                entries.append(f"{row['mode']} {blob}\t{relative}".encode('utf-8'))
            if entries:
                self._git(['update-index', '-z', '--index-info'], data=b'\0'.join(entries) + b'\0', index=index)
            tree = self._git(['write-tree'], index=index).stdout.decode().strip()
            commit = self._git(['commit-tree', tree], data=canonical({'run_id': run_id, 'node_id': node_id,
                'reason': reason, 'event_sequence': event_sequence})).stdout.decode().strip()
            self._git(['update-ref', 'refs/mewcode/snapshots/' + identity, commit])
            current = self.inventory()
            if {key: value['sha256'] for key, value in current.items()} != {key: value['sha256'] for key, value in rows.items()}:
                raise SnapshotError('workspace changed while committing the snapshot')
            return {'schema_version': 1, 'snapshot_id': identity, 'workspace': str(self.workspace),
                    'run_id': run_id, 'node_id': node_id, 'reason': reason, 'event_sequence': event_sequence,
                    'commit': commit, 'shadow': self.shadow, 'files': rows}
        finally:
            index.unlink(missing_ok=True)
            Path(str(index) + '.lock').unlink(missing_ok=True)

    async def create(self, *, run_id, node_id, reason, event_sequence):
        snapshot = await asyncio.to_thread(self._capture, run_id=run_id, node_id=node_id,
                                          reason=reason, event_sequence=event_sequence)
        # Worktree setup is disabled: excluded configs, hooks and ignored files
        # must not be copied back into the isolated snapshot by a convenience hook.
        manager = WorktreeManager(str(self.git_dir) if self.shadow else str(self.workspace),
                                  worktree_dir=str(self.control / 'snapshot-worktrees'), post_creation_setup=False)
        worktree = await asyncio.to_thread(lambda: asyncio.run(manager.create(
            'snapshot-' + snapshot['snapshot_id'], snapshot['commit'], checkout=False)))
        snapshot['work_dir'] = worktree.path
        await asyncio.to_thread(self._restore_files, snapshot,
                                [path for path, row in snapshot['files'].items() if row['covered']],
                                destination=Path(worktree.path))
        payload = {**snapshot, 'sha256': digest(snapshot)}
        path = self.directory / (snapshot['snapshot_id'] + '.json')
        await asyncio.to_thread(self._save, path, payload)
        return snapshot

    async def archive(self, *, run_id, node_id, reason, event_sequence):
        """Retain immutable content without creating another nested worktree."""
        snapshot = await asyncio.to_thread(self._capture, run_id=run_id, node_id=node_id,
                                          reason=reason, event_sequence=event_sequence)
        snapshot['materialized'] = False
        await asyncio.to_thread(self._save, self.directory / (snapshot['snapshot_id'] + '.json'),
                                {**snapshot, 'sha256': digest(snapshot)})
        return snapshot

    @staticmethod
    def _save(path, payload):
        temporary = path.with_suffix('.pending')
        with temporary.open('wb') as stream:
            stream.write(canonical(payload))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    def load(self, identity):
        if len(identity) != 32 or any(char not in '0123456789abcdef' for char in identity):
            raise SnapshotError('invalid snapshot identity')
        snapshot = json.loads((self.directory / (identity + '.json')).read_text(encoding='utf-8'))
        proof = snapshot.pop('sha256')
        if digest(snapshot) != proof or snapshot['schema_version'] != 1 or snapshot['workspace'] != str(self.workspace):
            raise SnapshotError('snapshot integrity or workspace mismatch')
        expected_worktree = self.control / 'snapshot-worktrees' / ('snapshot-' + identity)
        if snapshot.get('materialized', True) and Path(snapshot['work_dir']).resolve() != expected_worktree.resolve():
            raise SnapshotError('snapshot worktree escaped its recorded directory')
        return snapshot

    def list(self):
        return [self.load(path.stem) for path in sorted(self.directory.glob('*.json'))]

    def diff(self, before, after):
        first, second = self.load(before)['files'], self.load(after)['files']
        return sorted(path for path in first.keys() | second.keys()
                      if any(first.get(path, {}).get(key) != second.get(path, {}).get(key)
                             for key in ('sha256', 'permissions', 'kind')))

    def covers(self, identity, paths):
        """Coverage is physical content, never a grant to change these paths."""
        snapshot = self.load(identity)
        for value in paths:
            try:
                path = Path(value)
                path = path if path.is_absolute() else self.workspace / path
                relative = path.resolve().relative_to(self.workspace).as_posix()
            except (ValueError, OSError):
                return False
            if any(part in SKIP_DIRS or part.startswith('.pytest-') for part in Path(relative).parts):
                return False
            if relative in snapshot['files'] and not snapshot['files'][relative]['covered']:
                return False
            ignored = self._git(['check-ignore', '--no-index', '--', relative], check=False)
            if ignored.returncode != 1:
                return False
        return True

    async def restore(self, identity, *, run_id, node_id, event_sequence):
        target = self.load(identity)
        backup = await self.create(run_id=run_id, node_id=node_id, reason='before_restore', event_sequence=event_sequence)
        changes = self.diff(identity, backup['snapshot_id'])
        if any(not row['covered'] for path in changes
               for row in (target['files'].get(path), backup['files'].get(path)) if row):
            raise SnapshotError('restore touches content outside snapshot coverage; backup retained')
        await asyncio.to_thread(self._restore_files, target, changes)
        return {'restored': identity, 'backup': backup['snapshot_id'], 'paths': changes}

    def _restore_files(self, snapshot, changes, *, destination=None):
        root = destination or self.workspace
        for relative in changes:
            target = root / relative
            target.resolve().relative_to(root)
            if target.exists() and self._linked(target):
                raise SnapshotError('restore cannot follow a redirected path')
            row = snapshot['files'].get(relative)
            if row is None:
                target.unlink(missing_ok=True)
            else:
                data = self._git(['cat-file', 'blob', row['blob']]).stdout
                if hashlib.sha256(data).hexdigest() != row['sha256']:
                    raise SnapshotError('snapshot blob integrity mismatch')
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(target.name + '.restore-' + uuid.uuid4().hex)
                with temporary.open('wb') as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                if row.get('permissions') is not None:
                    temporary.chmod(row['permissions'])
                os.replace(temporary, target)
