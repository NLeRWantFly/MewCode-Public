"""Snapshot investigations: inherited execution, independent verification, guarded publication."""
import copy
import asyncio
import hashlib
from pathlib import Path

from mewcode.experts.authorization_ledger import covers, valid_request
from mewcode.kernel.events import digest
from mewcode.kernel.snapshots import SnapshotManager, SnapshotError


class InvestigationGate(RuntimeError):
    def __init__(self, kind, reason, requests=None):
        self.kind, self.reason, self.requests = kind, reason, requests
        super().__init__(reason)


class Investigations:
    def __init__(self, store, run_id, workspace, *, grant_store=None, arbitration=None):
        self.store, self.run_id = store, run_id
        self.snapshots = SnapshotManager(workspace)
        self.grant_store = grant_store
        self.arbitration = arbitration

    def _authorized(self, record, requests):
        return bool(requests) and all(covers(record, request, grant_store=self.grant_store).status == 'covered'
                                      for request in requests)

    def _save(self, identity, **values):
        def apply(record):
            record.setdefault('kernel_investigations', {}).setdefault(identity, {}).update(copy.deepcopy(values))
        return self.store.mutate(self.run_id, apply, durable=True)

    async def run(self, *, node_id, requests, execute, verify, merge, executor_provider, verifier_provider):
        """Callbacks use the ordinary reviewed Agent gateway, never raw shell writes.

        `execute` receives the isolated directory; `merge` receives only the
        verified byte changes and a completed mainline backup. Providers are
        supplied by the host's configured role binding, never by model output.
        """
        requests = [valid_request(request) for request in requests]
        record = self.store.load(self.run_id)
        from mewcode.experts.node_ledger import ledger_for_run
        epoch = ledger_for_run(record).view(node_id).epoch
        for request in requests:
            if request['scope'].get('node_ids', [node_id]) != [node_id] or request['scope'].get('epoch', epoch) != epoch:
                raise InvestigationGate('authorization_extension', '调查范围不能改为其他节点或代际。')
            request['scope'].update(node_ids=[node_id], epoch=epoch)
        revision = (record['node_states'].get(node_id) or {}).get('input_revision', '')
        identity = digest([node_id, revision, requests])
        previous = (record.get('kernel_investigations') or {}).get(identity)
        if previous:
            if previous.get('status') == 'merged':
                return previous
            raise InvestigationGate('irreversible', '上次调查尚未核对结束状态，不能自动重放。')
        if not self._authorized(record, requests):
            raise InvestigationGate('authorization_extension', '调查请求超出原有授权范围。')
        try:
            snapshot = await self.snapshots.create(run_id=self.run_id, node_id=node_id,
                reason='before_investigation', event_sequence=record['kernel_revision'])
        except (SnapshotError, OSError) as exc:
            raise InvestigationGate('business', '快照未建成，调查未启动：' + str(exc)) from exc
        self._save(identity, status='executing', node_id=node_id, requests=requests,
                   snapshot_id=snapshot['snapshot_id'], executor_provider=executor_provider)
        latest = self.store.load(self.run_id)
        if (not self._authorized(latest, requests) or latest['status'] != record['status']
                or (latest['node_states'].get(node_id) or {}).get('input_revision', '') != revision):
            self._save(identity, status='not_started')
            raise InvestigationGate('business', '建快照期间运行状态或授权发生变化，调查未启动。')
        # Executor policy/checker remains inherited; this object provides no
        # permission grant and no route for bypassing sensitive/hard-denied paths.
        try:
            await execute(snapshot['work_dir'])
        except asyncio.CancelledError:
            self._save(identity, status='interrupted')
            raise
        except Exception as exc:
            self._save(identity, status='interrupted', error=type(exc).__name__)
            raise InvestigationGate('business', '调查中断，改动保留在快照中，需先核对已有工具回执。') from exc
        result = self._result(snapshot)
        changed = sorted(path for path in snapshot['files'].keys() | result.keys()
                         if any(snapshot['files'].get(path, {}).get(key) != result.get(path, {}).get(key)
                                for key in ('sha256', 'permissions', 'kind')))
        if not changed:
            return self._save(identity, status='no_effect')['kernel_investigations'][identity]
        writable = [Path(path).resolve() for request in requests for path in request['scope'].get('paths', [])
                    if set(request['scope'].get('tools', [])) & {'WriteFile', 'EditFile', 'Bash'}]
        if any(not any((self.snapshots.workspace / path).resolve().is_relative_to(root) for root in writable)
               for path in changed):
            needed = [{'operation_class': 'other', 'scope': {'tools': ['WriteFile'],
                        'paths': [str(self.snapshots.workspace / path) for path in changed]}}]
            raise InvestigationGate('authorization_extension', '实际改动路径超出本次调查声明的写入范围。', needed)
        if any(not row.get('covered') for path in changed
               for row in (snapshot['files'].get(path), result.get(path)) if row):
            raise InvestigationGate('irreversible', '调查改动的内容未被快照完整保存。')
        if not executor_provider or not verifier_provider or executor_provider == verifier_provider:
            self._save(identity, status='awaiting_independent_verification', changed_paths=changed)
            raise InvestigationGate('business', '没有另一家厂商的核查模型，调查改动保留在快照中。')
        change_digest = digest({path: result.get(path) for path in changed})
        verdict = await verify(snapshot['work_dir'], changed, change_digest)
        if (not isinstance(verdict, dict) or verdict.get('passed') is not True
                or not str(verdict.get('reason') or '').strip() or verdict.get('change_digest') != change_digest):
            if self.arbitration is not None:
                decision = await self.arbitration.resolve(node_id, {'changed_paths': changed, 'change_digest': change_digest},
                    verdict if isinstance(verdict, dict) else {'reason': 'invalid_verdict'},
                    [{'id': change_digest, 'kind': 'host_diff', 'paths': changed}])
                self._save(identity, arbitration=decision)
            failed = SnapshotManager(snapshot['work_dir'], max_file_bytes=self.snapshots.max_file_bytes)
            archive = await failed.archive(run_id=self.run_id, node_id=node_id, reason='rejected_investigation',
                                          event_sequence=self.store.load(self.run_id)['kernel_revision'])
            await asyncio.to_thread(self.snapshots._restore_files, snapshot, changed, destination=Path(snapshot['work_dir']))
            self._save(identity, status='rejected', verification=verdict, changed_paths=changed,
                       rejected_snapshot=archive)
            reason = str((verdict or {}).get('reason') or '核查结论不完整') if isinstance(verdict, dict) else '核查结论不完整'
            def blocked(record):
                from mewcode.experts.node_ledger import ledger_for_run
                ledger_for_run(record).record(node_id, 'blocker', {'kind': 'business_ambiguity',
                    'ref': identity, 'reason': reason}, identity=['investigation_rejection', identity])
            self.store.mutate(self.run_id, blocked, durable=True)
            raise InvestigationGate('business', '独立核查没有通过：' + reason + '；改动已归档，调查目录已恢复。')
        if self._result(snapshot) != result:
            raise InvestigationGate('business', '核查后快照内容又有变化，请重新核查。')
        latest = self.store.load(self.run_id)
        if not self._authorized(latest, requests):
            raise InvestigationGate('authorization_extension', '原有授权不再覆盖调查改动。')
        if (latest['node_states'].get(node_id) or {}).get('input_revision', '') != revision:
            raise InvestigationGate('business', '节点输入已变化，不能并回旧调查。')
        current = self.snapshots.inventory()
        if any(any(current.get(path, {}).get(key) != snapshot['files'].get(path, {}).get(key)
                   for key in ('sha256', 'permissions', 'kind')) for path in changed):
            self._save(identity, status='conflict', verification=verdict, changed_paths=changed)
            raise InvestigationGate('business', '主线的同一文件已有改动，需由你选择如何处理冲突。')
        if not self.snapshots.covers(snapshot['snapshot_id'], changed):
            raise InvestigationGate('irreversible', '调查改动超出快照的内容覆盖范围。')
        backup = await self.snapshots.create(run_id=self.run_id, node_id=node_id,
            reason='before_investigation_merge', event_sequence=latest['kernel_revision'])
        if any(any(backup['files'].get(path, {}).get(key) != snapshot['files'].get(path, {}).get(key)
                   for key in ('sha256', 'permissions', 'kind')) for path in changed):
            raise InvestigationGate('business', '建备份期间主线发生冲突，自动并回已停止。')
        payload = {}
        for relative in changed:
            row = result.get(relative)
            data = (Path(snapshot['work_dir']) / relative).read_bytes() if row else None
            if row and hashlib.sha256(data).hexdigest() != row['sha256']:
                raise InvestigationGate('business', '待并回内容与已核查内容不一致。')
            payload[relative] = data
        self._save(identity, status='merging', verification=verdict, changed_paths=changed,
                   backup_id=backup['snapshot_id'], verifier_provider=verifier_provider)
        backup['merge_metadata'] = {path: copy.deepcopy(result.get(path)) for path in changed}
        receipt = await merge(payload, backup)
        if not isinstance(receipt, dict) or receipt.get('host_verified') is not True or receipt.get('change_digest') != change_digest:
            if isinstance(receipt, dict) and receipt.get('command_started') is False:
                self._save(identity, status='not_started', receipt=receipt)
                raise InvestigationGate('business', '并回调用未启动，当前改动仍保留在快照中。')
            self._save(identity, status='merge_unknown')
            raise InvestigationGate('irreversible', '并回入口没有确定回执，先核对主线效果。')
        applied = self.snapshots.inventory()
        if any(any(applied.get(path, {}).get(key) != result.get(path, {}).get(key)
                   for key in ('sha256', 'permissions', 'kind')) for path in changed):
            self._save(identity, status='merge_unknown', receipt=receipt)
            raise InvestigationGate('irreversible', '主线摘要与已核查改动不一致，先核对并回效果。')
        return self._save(identity, status='merged', receipt=receipt)['kernel_investigations'][identity]

    def _result(self, snapshot):
        root = Path(snapshot['work_dir'])
        manager = SnapshotManager(root, max_file_bytes=self.snapshots.max_file_bytes)
        result = manager.inventory()
        # A changed .gitignore cannot hide a previously covered file from review.
        for relative in snapshot['files']:
            path = root / relative
            if not snapshot['files'][relative]['covered'] and not path.exists():
                result[relative] = copy.deepcopy(snapshot['files'][relative])
                continue
            if path.is_file() and not manager._linked(path):
                if path.resolve().is_relative_to(root):
                    info = path.stat()
                    hasher = hashlib.sha256()
                    with path.open('rb') as stream:
                        for block in iter(lambda: stream.read(1024 * 1024), b''):
                            hasher.update(block)
                    result[relative] = {**snapshot['files'][relative], 'sha256': hasher.hexdigest(),
                        'size': info.st_size, 'permissions': info.st_mode & 0o777,
                        'covered': info.st_size <= self.snapshots.max_file_bytes}
        return result
