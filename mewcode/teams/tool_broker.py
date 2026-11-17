"""Scoped host tools. Retrieved text is data, never an authority source.

Adapters are registered by trusted Python host code, not imported from a task
or selected through a model-supplied executable/URL. Candidate production stays
in the immutable handoff protocol; workspace publication uses its own journal.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from mewcode.mcp.coord_protocol import canonical_json, now_iso
from .orchestration_contracts import ResourceGrant, ToolInvocation
from .runtime_protocol import LeaseToken, byte_hash
from .spec import TaskTemplate
from .store_v2 import TeamStateError, _digest


@dataclass(frozen=True)
class HostTool:
    handler: Callable
    kind: str
    action: str = 'read'
    mutating: bool = False
    description: str = ''


class ToolBroker:
    max_result_bytes = 128 * 1024

    def __init__(self, runtime, workspace):
        self.runtime, self.db = runtime, runtime.db
        self.workspace = Path(workspace).resolve(strict=True)
        self.tools: dict[str, HostTool] = {}
        self.sources: dict[str, Callable] = {}
        self.register('read_text', HostTool(self._read_text, 'workspace', description='Read one granted UTF-8 file; path, offset, limit.'))
        self.register('list_files', HostTool(self._list_files, 'workspace', description='List safe relative file names under the granted directory.'))
        self.register('search_text', HostTool(self._search_text, 'workspace', description='Literal search in granted text files; query, limit.'))
        self.register('read_artifact', HostTool(self._read_artifact, 'artifact', description='Read the exact granted immutable artifact version.'))
        self.register('retrieve_source', HostTool(self._retrieve_source, 'source', description='Query a host-registered evidence source; query.'))

    def register(self, name, tool: HostTool):
        # No model-facing registration operation exists.
        if not isinstance(tool, HostTool) or not name or tool.kind not in {'workspace', 'artifact', 'source'}:
            raise ValueError('invalid trusted tool registration')
        if tool.mutating and tool.kind == 'workspace':
            raise ValueError('workspace mutations must use the explicit artifact publication journal')
        if tool.mutating and tool.action not in {'write_candidate', 'publish'}:
            raise ValueError('a mutating tool must require an explicit write or publication action')
        self.tools[name] = tool

    def register_source(self, source_id, handler):
        if not source_id or not callable(handler):
            raise ValueError('a source ID and trusted handler are required')
        self.sources[source_id] = handler

    @staticmethod
    def _denied(path):
        for part in path.parts:
            name = part.casefold()
            if (name in {'.git', '.codex', '.agents', '.mewcode', '.ssh', '.aws', '.azure', 'node_modules', '.venv'}
                    or name == '.env' or name.startswith('.env.') or name in {'credentials', 'credentials.json', 'id_rsa', 'id_ed25519'}
                    or name.endswith(('.pem', '.key', '.pfx', '.p12'))):
                return True
        return False

    def _path(self, locator, relative='.'):
        base = Path(locator)
        base = base if base.is_absolute() else self.workspace / base
        rel = Path(relative)
        if rel.is_absolute() or '..' in rel.parts or ':' in str(rel):
            raise TeamStateError('RESOURCE_PATH_DENIED', 'only safe relative paths are accepted')
        lexical = base / rel
        try:
            lexical_relative = lexical.relative_to(self.workspace)
            if '..' in lexical_relative.parts or self._denied(lexical_relative):
                raise ValueError('sensitive or parent path')
            current = self.workspace
            for part in lexical_relative.parts:
                current /= part
                info = current.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    raise ValueError('links and reparse points are not granted')
            resolved, resolved_base = lexical.resolve(strict=True), base.resolve(strict=True)
            if not resolved.is_relative_to(resolved_base) or not resolved.is_relative_to(self.workspace):
                raise ValueError('outside grant')
            if self._denied(resolved.relative_to(self.workspace)):
                raise ValueError('sensitive path')
            return resolved
        except (OSError, ValueError) as exc:
            raise TeamStateError('RESOURCE_PATH_DENIED', 'path is absent, sensitive, linked or outside the exact grant') from exc

    def _check_open_file(self, handle, path):
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
            raise TeamStateError('RESOURCE_PATH_DENIED', 'only regular non-hard-linked files can be read')
        # On Windows, validate the path of the OPEN HANDLE as well as the name.
        # This catches a junction/path substitution between checking and opening.
        if os.name == 'nt':
            import ctypes
            import msvcrt
            from ctypes import wintypes
            function = ctypes.windll.kernel32.GetFinalPathNameByHandleW
            function.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
            function.restype = wintypes.DWORD
            buffer = ctypes.create_unicode_buffer(32768)
            size = function(msvcrt.get_osfhandle(handle.fileno()), buffer, len(buffer), 0)
            if not size or size >= len(buffer):
                raise TeamStateError('RESOURCE_PATH_DENIED', 'could not attest the open file path')
            final = buffer.value
            if final.startswith('\\\\?\\'):
                final = final[4:]
            if os.path.normcase(final) != os.path.normcase(str(path)):
                raise TeamStateError('RESOURCE_PATH_DENIED', 'open handle escaped the checked path')

    def grant(self, actor, run_id, *, grant, expected_version=0, control_epoch, command_id):
        grant = ResourceGrant.model_validate(grant)
        def action():
            team = self.runtime._editable(actor, run_id, control_epoch)
            policy = self.runtime._compiled(run_id).effective_policy
            if grant.resource_id not in policy.allowed_resources:
                raise TeamStateError('RESOURCE_NOT_GRANTED', 'resource IDs must already belong to the compiled grant')
            if any(name not in self.tools or self.tools[name].kind != grant.kind for name in grant.tools):
                raise TeamStateError('TOOL_NOT_REGISTERED', 'grant names an unavailable tool')
            if any(self.tools[name].action not in policy.allowed_actions for name in grant.tools):
                raise TeamStateError('PERMISSION_REQUIRED', 'tool action is outside the compiled policy')
            if grant.writable and grant.kind == 'workspace':
                raise TeamStateError('PUBLICATION_REQUIRED', 'models cannot modify original workspace files through the broker')
            if grant.kind == 'workspace':
                self._path(grant.locator)
            elif grant.kind == 'source' and grant.locator not in self.sources:
                raise TeamStateError('SOURCE_NOT_REGISTERED', 'register the source through trusted host configuration')
            elif grant.kind == 'artifact':
                self.runtime._artifact_for_run(grant.locator, run_id)
            current = self.db.execute('SELECT version FROM team_v2_resource_grants WHERE team_run_id=? AND resource_id=?', (run_id, grant.resource_id)).fetchone()
            if type(expected_version) is not int or expected_version != (current[0] if current else 0):
                raise TeamStateError('STATE_VERSION_CONFLICT', grant.resource_id)
            self.db.execute('INSERT INTO team_v2_resource_grants VALUES(?,?,?,?) ON CONFLICT(team_run_id,resource_id) DO UPDATE SET grant_json=excluded.grant_json,version=excluded.version',
                            (run_id, grant.resource_id, grant.model_dump_json(), expected_version + 1))
            return dict(team_id=team['team_id'], team_run_id=run_id, grant=grant.model_dump(), version=expected_version + 1)
        return self.runtime.repo._write(actor, 'team_resource_granted', command_id,
                                        dict(run_id=run_id, grant=grant.model_dump(), expected_version=expected_version, epoch=control_epoch),
                                        lambda: self.runtime._owner(actor, run_id, epoch=control_epoch), action)

    def list_grants(self, actor, run_id):
        with self.runtime.lock:
            self.runtime._owner(actor, run_id)
            return [dict(resource_id=r['resource_id'], version=r['version'], **{k: v for k, v in json.loads(r['grant_json']).items() if k != 'resource_id'}) for r in self.db.execute(
                'SELECT * FROM team_v2_resource_grants WHERE team_run_id=? ORDER BY resource_id', (run_id,))]

    def revoke(self, actor, run_id, *, resource_id, expected_version, control_epoch, command_id):
        def action():
            team = self.runtime._editable(actor, run_id, control_epoch)
            row = self.db.execute('SELECT version FROM team_v2_resource_grants WHERE team_run_id=? AND resource_id=?', (run_id, resource_id)).fetchone()
            if not row or type(expected_version) is not int or row[0] != expected_version:
                raise TeamStateError('STATE_VERSION_CONFLICT', resource_id)
            # Keep a tombstone version so revoke/regrant cannot reuse an old
            # attestation while a tool is in flight.
            self.db.execute('UPDATE team_v2_resource_grants SET grant_json=?,version=version+1 WHERE team_run_id=? AND resource_id=?',
                            (canonical_json(dict(revoked=True)), run_id, resource_id))
            return dict(team_id=team['team_id'], team_run_id=run_id, resource_id=resource_id, revoked=True, version=expected_version + 1)
        return self.runtime.repo._write(actor, 'team_resource_revoked', command_id,
                                        dict(run_id=run_id, resource_id=resource_id, expected_version=expected_version, epoch=control_epoch),
                                        lambda: self.runtime._owner(actor, run_id, epoch=control_epoch), action)

    def _authorize(self, actor, token, invocation):
        task, attempt = self.runtime._lease(actor, token)
        contract = TaskTemplate.model_validate_json(task['contract_json'])
        row = self.db.execute('SELECT * FROM team_v2_resource_grants WHERE team_run_id=? AND resource_id=?', (task['team_run_id'], invocation.resource_id)).fetchone()
        policy = self.runtime._compiled(task['team_run_id']).effective_policy
        if not row or json.loads(row['grant_json']).get('revoked') or invocation.resource_id not in contract.input_scope or invocation.resource_id not in policy.allowed_resources:
            raise TeamStateError('RESOURCE_NOT_GRANTED', invocation.resource_id)
        grant = ResourceGrant.model_validate_json(row['grant_json'])
        tool = self.tools.get(invocation.name)
        if not tool or invocation.name not in grant.tools or tool.kind != grant.kind:
            raise TeamStateError('TOOL_NOT_GRANTED', invocation.name)
        if tool.action not in contract.required_actions or tool.action not in policy.allowed_actions or (tool.mutating and not grant.writable):
            raise TeamStateError('PERMISSION_REQUIRED', 'tool action is outside this task contract')
        if grant.kind == 'workspace':
            self._path(grant.locator)
        return task, grant, tool, row['version']

    def catalog_for_task(self, actor, task_id):
        with self.runtime.lock:
            task = self.runtime._task_access(actor, task_id)
            contract = TaskTemplate.model_validate_json(task['contract_json'])
            result = []
            for row in self.db.execute('SELECT * FROM team_v2_resource_grants WHERE team_run_id=?', (task['team_run_id'],)):
                if json.loads(row['grant_json']).get('revoked'):
                    continue
                grant = ResourceGrant.model_validate_json(row['grant_json'])
                if grant.kind == 'source' and grant.locator not in self.sources:
                    # A persisted grant is not proof that this host (or the
                    # legacy CLI worker) has the trusted source installed.
                    continue
                if grant.resource_id in contract.input_scope:
                    result.extend(dict(name=name, resource_id=grant.resource_id, description=self.tools[name].description,
                                       arguments='JSON object; only the named host tool interprets it')
                                  for name in grant.tools if name in self.tools and self.tools[name].action in contract.required_actions)
            return result

    def _resource_key(self, grant):
        if grant.kind == 'workspace':
            # Conservatively serialize mutations across all overlapping grants
            # in one workspace; concurrent reads remain allowed.
            return 'workspace:' + os.path.normcase(str(self.workspace))
        return grant.kind + ':' + grant.locator

    async def invoke(self, actor, token, invocation):
        token, invocation = LeaseToken.model_validate(token), ToolInvocation.model_validate(invocation)
        request = invocation.model_dump(mode='json')
        if len(canonical_json(request).encode()) > 32768:
            raise TeamStateError('TOOL_ARGUMENTS_TOO_LARGE', invocation.call_id)
        digest = _digest(request)
        captured = {}
        def start():
            task, grant, tool, version = self._authorize(actor, token, invocation)
            old = self.db.execute('SELECT * FROM team_v2_tool_calls WHERE attempt_id=? AND call_id=?', (token.attempt_id, invocation.call_id)).fetchone()
            if old:
                if old['request_hash'] != digest:
                    raise TeamStateError('TOOL_CALL_ID_CONFLICT', invocation.call_id)
                if old['state'] != 'completed':
                    raise TeamStateError('TOOL_OUTCOME_UNKNOWN', 'an interrupted tool is not blindly replayed')
                captured['replay'] = json.loads(old['result_json'])
                return dict(team_id=task['team_id'], task_id=token.task_id, replay=True)
            if self.db.execute("SELECT 1 FROM team_v2_tool_calls WHERE attempt_id=? AND state='running' LIMIT 1", (token.attempt_id,)).fetchone():
                raise TeamStateError('TOOL_IN_FLIGHT', 'one isolated instance dispatches tools sequentially')
            key = self._resource_key(grant)
            leases = self.db.execute('SELECT * FROM team_v2_resource_leases WHERE resource_key=?', (key,)).fetchall()
            if leases and (tool.mutating or any(r['mode'] == 'write' for r in leases)):
                raise TeamStateError('RESOURCE_BUSY', invocation.resource_id)
            self.db.execute('INSERT OR IGNORE INTO team_v2_resource_leases VALUES(?,?,?,?,?,?)',
                            (actor.project_id, key, token.attempt_id, token.task_id, 'write' if tool.mutating else 'read', now_iso()))
            self.db.execute('INSERT INTO team_v2_tool_calls VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)',
                            (token.attempt_id, invocation.call_id, token.task_id, token.revision, digest, invocation.name,
                             invocation.resource_id, 'running', canonical_json(request), None, now_iso()))
            captured.update(grant=grant, tool=tool, version=version, key=key, team_id=task['team_id'])
            return dict(team_id=task['team_id'], task_id=token.task_id, attempt_id=token.attempt_id,
                        call_id=invocation.call_id, tool=invocation.name, resource_id=invocation.resource_id, state='running')
        # Replay is checked in the tool journal, not skipped by a cached command
        # before the current lease and grant have been revalidated.
        from .runtime import _id
        self.runtime.repo._write(actor, 'team_tool_started', _id('tool-start'), request,
                                 lambda: self._authorize(actor, token, invocation), start)
        if 'replay' in captured:
            return captured['replay']
        error = None
        try:
            handler = captured['tool'].handler
            args = (actor, captured['grant'], dict(invocation.arguments))
            async with asyncio.timeout(30):
                result = await handler(*args) if inspect.iscoroutinefunction(handler) else await asyncio.to_thread(handler, *args)
            encoded = canonical_json(result)
            if len(encoded.encode()) > self.max_result_bytes:
                raise TeamStateError('TOOL_RESULT_TOO_LARGE', invocation.name)
            result = dict(data_only=True, authority='none', call_id=invocation.call_id, tool=invocation.name,
                          resource_id=invocation.resource_id, content_hash=byte_hash(encoded.encode()), result=result)
        except BaseException as exc:
            error = exc
            result = dict(data_only=True, authority='none', call_id=invocation.call_id, tool=invocation.name,
                          error=exc.code if isinstance(exc, TeamStateError) else type(exc).__name__)
        state = 'failed' if error else 'completed'
        def finish():
            nonlocal state, error
            try:
                _, _, _, version = self._authorize(actor, token, invocation)
                if version != captured['version']:
                    raise TeamStateError('RESOURCE_GRANT_CHANGED', invocation.resource_id)
            except TeamStateError as exc:
                state, error = 'discarded', exc
            if error and captured['tool'].mutating:
                state = 'effect_unknown'
            saved = result if state == 'completed' else dict(data_only=True, authority='none', error=state)
            self.db.execute('UPDATE team_v2_tool_calls SET state=?,result_json=?,finished_at=? WHERE attempt_id=? AND call_id=?',
                            (state, canonical_json(saved), now_iso(), token.attempt_id, invocation.call_id))
            if state != 'effect_unknown':
                self.db.execute('DELETE FROM team_v2_resource_leases WHERE attempt_id=? AND resource_key=?', (token.attempt_id, captured['key']))
            return dict(team_id=captured['team_id'], task_id=token.task_id, attempt_id=token.attempt_id,
                        call_id=invocation.call_id, tool=invocation.name, state=state, result=saved)
        # Finishing a stale tool can only quarantine its result, never apply it.
        self.runtime.repo._write(actor, 'team_tool_finished', _id('tool-finish'),
                                 dict(attempt_id=token.attempt_id, call_id=invocation.call_id, state=state),
                                 lambda: actor.require('team.execute'), finish)
        if error:
            raise error
        return result

    def _read_bytes(self, grant, relative, offset=0, limit=32768):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 65536:
            raise ValueError('offset >= 0 and 1 <= limit <= 65536 are required')
        path = self._path(grant.locator, relative)
        with path.open('rb') as handle:
            self._check_open_file(handle, path)
            before = os.fstat(handle.fileno())
            handle.seek(offset)
            content = handle.read(limit)
            after = os.fstat(handle.fileno())
            self._check_open_file(handle, path)
        if self._path(grant.locator, relative) != path or (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
            raise TeamStateError('SOURCE_CHANGED', 'file changed during the read')
        return dict(text=content.decode('utf-8', errors='strict'), source_ref=path.relative_to(self.workspace).as_posix(),
                    source_version=f'{after.st_mtime_ns}:{after.st_size}', source_hash=byte_hash(content),
                    offset=offset, bytes=len(content), total_bytes=after.st_size, truncated=offset + len(content) < after.st_size)

    def _read_text(self, actor, grant, arguments):
        if set(arguments) - {'path', 'offset', 'limit'}:
            raise ValueError('unknown read_text argument')
        return self._read_bytes(grant, str(arguments.get('path', '.')), arguments.get('offset', 0), arguments.get('limit', 32768))

    def _files(self, grant):
        root = self._path(grant.locator)
        if root.is_file():
            yield '.'
            return
        count = 0
        for current, directories, files in os.walk(root, followlinks=False):
            directories[:] = [name for name in sorted(directories) if not self._denied(Path(name))
                              and not (getattr((Path(current) / name).lstat(), 'st_file_attributes', 0) & 0x400)
                              and not (Path(current) / name).is_symlink()]
            for name in sorted(files):
                relative = (Path(current) / name).relative_to(root)
                if self._denied(relative):
                    continue
                try:
                    self._path(grant.locator, str(relative))
                except TeamStateError:
                    continue
                count += 1
                if count > 500:
                    return
                yield relative.as_posix()

    def _list_files(self, actor, grant, arguments):
        if arguments:
            raise ValueError('list_files has no arguments')
        return dict(files=list(self._files(grant)), limit=500)

    def _search_text(self, actor, grant, arguments):
        if set(arguments) - {'query', 'limit'} or not isinstance(arguments.get('query'), str) or not arguments['query']:
            raise ValueError('a nonempty literal query is required')
        limit = arguments.get('limit', 30)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('limit must be 1..100')
        matches = []
        for path in self._files(grant):
            try:
                evidence = self._read_bytes(grant, path)
            except (UnicodeError, OSError, TeamStateError):
                continue
            for index, line in enumerate(evidence['text'].splitlines(), 1):
                if arguments['query'] in line:
                    matches.append(dict(path=path, line=index, text=line[:1000], source_version=evidence['source_version'], source_hash=evidence['source_hash']))
                    if len(matches) >= limit:
                        return dict(matches=matches, limited=True)
        return dict(matches=matches, limited=False, per_file_bytes=32768)

    def _read_artifact(self, actor, grant, arguments):
        if arguments:
            raise ValueError('read_artifact uses the exact granted version, not a caller-supplied version')
        return self.runtime.artifact(actor, grant.locator)

    async def _retrieve_source(self, actor, grant, arguments):
        handler = self.sources.get(grant.locator)
        if not handler:
            raise TeamStateError('SOURCE_NOT_REGISTERED', grant.locator)
        if set(arguments) != {'query'} or not isinstance(arguments['query'], str) or len(arguments['query']) > 4000:
            raise ValueError('source queries must be bounded text, not executable requests')
        value = await handler(arguments['query']) if inspect.iscoroutinefunction(handler) else await asyncio.to_thread(handler, arguments['query'])
        if not isinstance(value, dict) or not {'content', 'source_ref', 'source_version'} <= value.keys():
            raise TeamStateError('SOURCE_PROVENANCE_REQUIRED', grant.locator)
        return dict(content=value['content'], source_ref=value['source_ref'], source_version=value['source_version'],
                    source_hash=byte_hash(canonical_json(value['content']).encode()), data_only=True, authority='none')

    def calls(self, actor, run_id, *, after=0, limit=100):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('invalid tool cursor')
        with self.runtime.lock:
            self.runtime._owner(actor, run_id)
            rows = self.db.execute('SELECT c.rowid AS cursor,c.* FROM team_v2_tool_calls c JOIN team_v2_tasks t USING(task_id) WHERE t.team_run_id=? AND c.rowid>? ORDER BY c.rowid LIMIT ?', (run_id, after, limit)).fetchall()
            return [dict(cursor=r['cursor'], attempt_id=r['attempt_id'], call_id=r['call_id'], task_id=r['task_id'],
                         revision=r['revision'], name=r['name'], resource_id=r['resource_id'], state=r['state'],
                         request=json.loads(r['request_json']), result=json.loads(r['result_json']) if r['result_json'] else None,
                         started_at=r['started_at'], finished_at=r['finished_at']) for r in rows]

    def reconcile(self, actor, run_id, *, attempt_id, call_id, evidence, control_epoch, command_id):
        from .store_v2 import _text
        evidence = _text(evidence)
        def action():
            team = self.runtime._owner(actor, run_id, epoch=control_epoch)
            row = self.db.execute('SELECT c.* FROM team_v2_tool_calls c JOIN team_v2_tasks t USING(task_id) WHERE t.team_run_id=? AND c.attempt_id=? AND c.call_id=?', (run_id, attempt_id, call_id)).fetchone()
            if not row or row['state'] not in {'running', 'effect_unknown'}:
                raise TeamStateError('TOOL_NOT_RECONCILABLE', call_id)
            attempt = self.db.execute('SELECT * FROM team_v2_attempts WHERE attempt_id=?', (attempt_id,)).fetchone()
            if attempt and attempt['state'] == 'running':
                raise TeamStateError('ATTEMPT_STILL_RUNNING', 'fence or reap the old attempt before reconciling effects')
            self.db.execute("UPDATE team_v2_tool_calls SET state='reconciled',result_json=?,finished_at=? WHERE attempt_id=? AND call_id=?",
                            (canonical_json(dict(evidence=evidence, reconciled_by=actor.principal_ref)), now_iso(), attempt_id, call_id))
            if not self.db.execute("SELECT 1 FROM team_v2_tool_calls WHERE attempt_id=? AND state IN ('running','effect_unknown') LIMIT 1", (attempt_id,)).fetchone():
                self.db.execute('DELETE FROM team_v2_resource_leases WHERE attempt_id=?', (attempt_id,))
            return dict(team_id=team['team_id'], team_run_id=run_id, attempt_id=attempt_id, call_id=call_id, state='reconciled')
        def authorize():
            actor.require('team.recover')
            self.runtime._owner(actor, run_id, epoch=control_epoch)
        return self.runtime.repo._write(actor, 'team_tool_reconciled', command_id,
                                        dict(run_id=run_id, attempt_id=attempt_id, call_id=call_id, evidence=evidence, epoch=control_epoch), authorize, action)
