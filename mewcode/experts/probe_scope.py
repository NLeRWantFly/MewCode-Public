"""Capability-reduced trials: local reads and exclusive new candidate files.

This is not a subprocess sandbox. Commands, plugins, delegation, installations
and arbitrary edits are absent from the trial registry. SQL claims coordinate
trial writers; ordinary/external writers are not covered by these claims.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PureWindowsPath

from mewcode.experts.replan_ledger import LedgerConflict
from mewcode.tools import ToolRegistry
from mewcode.tools.base import Tool, ToolResult
from mewcode.tools.write_file import Params


def candidate_path(project, candidate_root, value):
    project = Path(project).resolve()
    relative = Path(value)
    parts = value.replace('\\', '/').split('/')
    reserved = {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$', 'CLOCK$',
                *(f'COM{i}' for i in '0123456789¹²³'), *(f'LPT{i}' for i in '0123456789¹²³')}
    protected = {'.git', '.agents', '.codex', '.mewcode', '.venv'}
    if (not value or relative.is_absolute() or PureWindowsPath(value).drive
            or any(not p or p in {'.', '..'} or ':' in p or p.rstrip(' .') != p
                   or any(ord(c) < 32 or c in '<>"|?*' for c in p)
                   or p.split('.')[0].rstrip(' ').upper() in reserved or p.lower() in protected for p in parts)):
        raise ValueError('Trial candidate must be an unambiguous project-relative file path')
    target = project.joinpath(*parts)
    current = project
    for part in parts:
        current = current / part
        if current.is_symlink() or (current.exists() and getattr(current.stat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('Trial paths cannot traverse symbolic links or reparse points')
    root = project / candidate_root
    if not target.is_relative_to(root) or target == root:
        raise ValueError('Candidate is outside the frozen candidate directory')
    return target


def normalize_scope(project, candidate_root, paths):
    if not paths:
        return []
    if not project or not candidate_root:
        raise ValueError('Write trials require a frozen project candidate_root')
    # Apply the same lexical/link rules to the configured directory itself.
    marker = candidate_path(project, '', candidate_root)
    if not marker.is_dir():
        raise ValueError('The authorized candidate directory must already exist')
    result = []
    for value in paths:
        target = candidate_path(project, candidate_root, value)
        if not target.parent.is_dir():
            raise ValueError('Trial output parent directory must already exist')
        if target.exists():
            raise ValueError('Trial candidates must be new files, never overwrite existing work')
        result.append(os.path.normcase(str(target)))
    if len(set(result)) != len(result):
        raise ValueError('Duplicate or equivalent candidate paths')
    return result


class ProbeFiles:
    def __init__(self, ledger):
        self.ledger = ledger
        with ledger.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS probe_paths ('
                       'path TEXT PRIMARY KEY, run TEXT, episode TEXT, receipt TEXT)')

    def claim(self, run, episode, paths):
        with self.ledger.transaction() as db:
            existing = db.execute('SELECT path,run,episode FROM probe_paths').fetchall()
            for path in paths:
                for held, other_run, other_episode in existing:
                    owner = db.execute('SELECT body FROM episodes WHERE run=? AND id=?',
                                       (other_run, other_episode)).fetchone()
                    if owner and json.loads(owner[0])['status'] in {'applied', 'candidate_ready', 'rejected', 'stale', 'needs_input'}:
                        continue
                    if (other_run, other_episode) != (run, episode) and (
                            path == held or Path(path).is_relative_to(held) or Path(held).is_relative_to(path)):
                        raise LedgerConflict('Another trial owns an overlapping candidate path')
            for path in paths:
                owner = db.execute('SELECT run,episode FROM probe_paths WHERE path=?', (path,)).fetchone()
                if owner != (run, episode):
                    db.execute('INSERT OR REPLACE INTO probe_paths VALUES (?,?,?,NULL)', (path, run, episode))

    def receipts(self, run, episode):
        with self.ledger.transaction() as db:
            return {path: json.loads(value) if value else None for path, value in db.execute(
                'SELECT path,receipt FROM probe_paths WHERE run=? AND episode=?', (run, episode))}

    def reconcile_written(self, run, episode, fence):
        """After stream closure, recover exact file receipts without another write.

        Missing/different bytes stay uncertain. No claim is released here, no
        stage/tool is declared successful, and no model request is reissued.
        """
        for path, receipt in self.receipts(run, episode).items():
            if not receipt or receipt['status'] != 'writing' or not self.matches(path, receipt):
                continue
            try:
                # Windows FlushFileBuffers requires a writable handle. r+b
                # neither truncates nor creates; recovery never calls write().
                with open(path, 'r+b') as source:
                    os.fsync(source.fileno())
                    if hashlib.sha256(source.read(receipt['bytes'] + 1)).hexdigest() != receipt['sha256']:
                        continue
            except OSError:
                continue
            with self.ledger.transaction() as db:
                current = db.execute('SELECT revision,body FROM episodes WHERE run=? AND id=?', (run, episode)).fetchone()
                if not current or current[0] != fence or json.loads(current[1])['status'] != 'probing':
                    raise LedgerConflict('Cannot reconcile an obsolete trial')
                saved = db.execute('SELECT receipt FROM probe_paths WHERE path=? AND run=? AND episode=?',
                                   (path, run, episode)).fetchone()
                if saved and json.loads(saved[0]) == receipt:
                    recovered = {**receipt, 'status': 'written', 'recovered_from_file': True}
                    db.execute('UPDATE probe_paths SET receipt=? WHERE path=? AND run=? AND episode=?',
                               (json.dumps(recovered), path, run, episode))
        return self.receipts(run, episode)

    def write(self, run, episode, fence, path, content):
        raw = content.encode('utf-8')
        if not raw or len(raw) > 1_000_000:
            raise ValueError('Trial file must contain 1..1000000 UTF-8 bytes')
        sha = hashlib.sha256(raw).hexdigest()
        with self.ledger.transaction() as db:
            row = db.execute('SELECT revision,body FROM episodes WHERE run=? AND id=?', (run, episode)).fetchone()
            if not row or row[0] != fence or json.loads(row[1])['status'] != 'probing':
                raise LedgerConflict('Trial write fencing revision is stale')
            owned = db.execute('SELECT receipt FROM probe_paths WHERE path=? AND run=? AND episode=?',
                               (path, run, episode)).fetchone()
            if owned is None:
                raise LedgerConflict('Path is not claimed by this trial')
            if owned[0]:
                saved = json.loads(owned[0])
                if saved['sha256'] == sha and saved['status'] == 'written' and self.matches(path, saved):
                    return saved
                raise LedgerConflict('Existing write intent cannot be overwritten or replayed')
            intent = {'status': 'writing', 'sha256': sha, 'bytes': len(raw), 'fence': fence}
            db.execute('UPDATE probe_paths SET receipt=? WHERE path=?', (json.dumps(intent), path))
        # Exclusive create protects against ordinary writers creating the target
        # after preflight. Never truncate, delete or roll back a candidate.
        with open(path, 'xb') as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        with self.ledger.transaction() as db:
            intent['status'] = 'written'
            db.execute('UPDATE probe_paths SET receipt=? WHERE path=? AND run=? AND episode=?',
                       (json.dumps(intent), path, run, episode))
        return intent

    @staticmethod
    def matches(path, receipt):
        target = Path(path)
        if type(receipt.get('bytes')) is not int or not 0 < receipt['bytes'] <= 1_000_000:
            return False
        try:
            for part in (target, *target.parents):
                if part.is_symlink() or getattr(part.stat(), 'st_file_attributes', 0) & 0x400:
                    return False
            if not target.is_file() or target.stat().st_size != receipt['bytes']:
                return False
            with target.open('rb') as source:
                return hashlib.sha256(source.read(receipt['bytes'] + 1)).hexdigest() == receipt['sha256']
        except OSError:
            return False


class CandidateWriter(Tool):
    name = 'WriteFile'
    description = 'Trial-only WriteFile: create a declared new candidate once. No overwrite, editing or directory creation.'
    category = 'write'
    params_model = Params

    def __init__(self, files, run, episode, fence, project, root, allowed, *, binding_valid=lambda: True):
        self.files, self.run_id, self.episode, self.fence = files, run, episode, fence
        self.project, self.root, self.allowed = project, root, set(allowed)
        self.binding_valid = binding_valid

    async def execute(self, params):
        try:
            if not self.binding_valid():
                raise ValueError('Trial contract or input revision changed before candidate write')
            target = candidate_path(self.project, self.root, params.file_path)
            path = os.path.normcase(str(target))
            if path not in self.allowed:
                raise ValueError('File is not declared in the frozen trial scope')
            receipt = self.files.write(self.run_id, self.episode, self.fence, path, params.content)
            return ToolResult(output=json.dumps({'candidate': path, **receipt, 'business_acceptance': False}))
        except (ValueError, OSError) as exc:
            return ToolResult(output=str(exc), is_error=True)


def trial_registry(original, writer=None):
    from mewcode.tools.read_file import ReadFile
    from mewcode.tools.glob import Glob
    from mewcode.tools.grep import Grep
    registry = ToolRegistry()
    # Exact built-in types: a plugin's read category/name is not isolation.
    for cls in (ReadFile, Glob, Grep):
        tool = original.get(cls.name)
        if type(tool) is cls and original.is_enabled(cls.name):
            registry.register(tool)
    if writer is not None and original.is_enabled('WriteFile'):
        registry.register(writer)
    return registry
