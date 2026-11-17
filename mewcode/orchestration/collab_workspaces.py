"""Durable, opt-in worktrees and merge gates for collaboration contracts.

Git only mutates host-owned checkouts and refs. The user's checkout/index is
never a merge target. Permissions and business acceptance remain separate.
"""
from __future__ import annotations

import asyncio
import copy
from functools import wraps
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import subprocess
import stat
import threading

from mewcode.kernel import collaboration as ledger
from mewcode.kernel.acceptance import Acceptance
from mewcode.kernel.events import digest
from mewcode.kernel.snapshots import SnapshotManager, SnapshotError
from mewcode.worktree.manager import WorktreeError, WorktreeManager


LOCAL_TOOLS = frozenset({'ReadFile', 'Glob', 'Grep', 'WriteFile', 'EditFile', 'Bash',
    'ReadStageResult', 'ReadRunArchive', 'InspectGitState', 'ReadAuditArtifact',
    'InspectVisualEvidence', 'ReportArtifact', 'ReportStep', 'SubmitHandoff', 'SubmitVerdict'})


def workspace_io(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (OSError, SnapshotError, WorktreeError, subprocess.SubprocessError) as exc:
            raise ValueError('隔离工作区操作未完成，已有文件和检查点保留：' + str(exc)) from exc
    return guarded


def relative_scope(value):
    if (not isinstance(value, str) or not value or '\\' in value or '\x00' in value
            or PureWindowsPath(value).drive or PurePosixPath(value).is_absolute()
            or '..' in PurePosixPath(value).parts
            or any(part.casefold() in {'.git', '.mewcode'} for part in PurePosixPath(value).parts)):
        raise ValueError('隔离节点写入范围必须是项目内相对路径，不能包含控制目录')
    return value


def validate_workspace_contracts(contracts):
    uses_isolation = any(row.get('workspace_mode') for row in contracts.values())
    for key, contract in contracts.items():
        mode = contract.get('workspace_mode')
        isolated_inputs = [parent for parent in contract.get('depends_on', [])
                           if contracts[parent].get('workspace_mode')]
        if not mode:
            if isolated_inputs:
                raise ValueError('隔离产物的下游必须声明 worktree 或 merge：' + key)
            if uses_isolation:
                capabilities = contract.get('capabilities')
                if (not isinstance(capabilities, dict) or capabilities.get('write_paths')
                        or capabilities.get('command_execution')):
                    raise ValueError('同一隔离计划中的本地写入节点必须声明 worktree：' + key)
            continue
        if not isinstance(mode, str) or mode not in {'worktree', 'merge'}:
            raise ValueError('协作工作区模式无效')
        if len(isolated_inputs) > 1 and mode != 'merge':
            raise ValueError('多个隔离分支必须交给 merge 节点：' + key)
        if mode == 'merge' and len(isolated_inputs) < 2:
            raise ValueError('merge 节点必须至少依赖两个隔离节点：' + key)
        capabilities = contract.get('capabilities')
        if not isinstance(capabilities, dict):
            raise ValueError('隔离节点必须声明能力及写入范围：' + key)
        if capabilities.get('external_targets') or capabilities.get('network_hosts'):
            raise ValueError('worktree 不能隔离外部服务副作用：' + key)
        for path in capabilities.get('write_paths', []):
            relative_scope(path)
        if contract.get('tools') is None or not set(contract['tools']) <= LOCAL_TOOLS:
            raise ValueError('隔离节点必须显式声明本地工具集合：' + key)
        if contract.get('iteration') or contract.get('execution_kind') == 'join':
            raise ValueError('隔离节点需要独立的工作与合并合同：' + key)


class WorkspaceConflict(ValueError):
    def __init__(self, row):
        self.workspace = copy.deepcopy(row)
        super().__init__('合并存在冲突，双方交付和合并工作区已保留：' + '、'.join(row['conflicts']))


class WorkspaceSnapshots(SnapshotManager):
    def inventory(self):
        rows = super().inventory()
        if self.shadow:
            return rows
        # Git still tracks files covered by a later ignore rule (and sometimes
        # vendor/build directories). Never turn those into accidental deletes.
        tracked = self._git(['ls-files', '--cached', '--stage', '-z']).stdout
        for encoded in tracked.split(b'\0'):
            if not encoded:
                continue
            metadata, encoded_path = encoded.split(b'\t', 1)
            mode, _, stage = metadata.decode().split()
            if stage != '0':
                raise SnapshotError('cannot snapshot unresolved Git index')
            relative = encoded_path.decode('utf-8')
            parts = Path(relative).parts
            if relative in rows:
                if os.name == 'nt' and mode in {'100644', '100755'}:
                    rows[relative]['mode'] = mode
                continue
            if any(part.casefold() in {'.git', '.mewcode'} for part in parts):
                continue
            path = self.workspace / relative
            if not path.exists() and not path.is_symlink():
                continue  # An actual working-copy deletion.
            if (any(self._linked(parent) for parent in (path, *path.parents) if parent.is_relative_to(self.workspace))
                    or not path.is_file()):
                rows[relative] = {'sha256': digest(['uncovered', relative]), 'covered': False, 'kind': 'unsupported'}
                continue
            before = path.stat()
            if before.st_size > self.max_file_bytes:
                rows[relative] = {'sha256': digest([before.st_size, before.st_mtime_ns]), 'covered': False, 'kind': 'oversized'}
                continue
            fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise SnapshotError('workspace changed while capturing tracked file: ' + relative)
            rows[relative] = {'sha256': fingerprint, 'size': before.st_size, 'covered': True,
                             'permissions': stat.S_IMODE(before.st_mode),
                             'mode': mode if os.name == 'nt' else '100755' if before.st_mode & stat.S_IXUSR else '100644'}
        return rows


class WorkspaceAcceptance(Acceptance):
    """Check logical paths in the checkout, retain root-relative evidence."""
    def __init__(self, source, checkout, **kwargs):
        super().__init__(source, **kwargs)
        self.checkout = Path(checkout).resolve(strict=True)
        self.checkout.relative_to(self.work_dir)

    def _path(self, name):
        path = Path(name)
        # Upstream fixed artifacts are immutable host evidence in the source
        # archive. Business file paths are always resolved in this checkout.
        internal = ('.mewcode/collaboration/', '.mewcode/collaboration-worktrees/')
        if path.is_absolute() or path.as_posix().startswith(internal):
            return super()._path(name)
        resolved = (self.checkout / path).resolve()
        if not resolved.is_relative_to(self.checkout):
            raise ValueError('验收产物超出节点工作区')
        return resolved


class CollaborationWorkspaces:
    def __init__(self, runtime):
        self.runtime = runtime
        self.source = runtime.acceptance.work_dir
        self.directory = self.source / '.mewcode' / 'collaboration-worktrees' / digest(runtime.run_id)[:20]
        self._lock = threading.RLock()

    @staticmethod
    def git(path, *args, check=True, data=None):
        env = {key: val for key, val in os.environ.items() if not key.startswith('GIT_')}
        env.update(GIT_TERMINAL_PROMPT='0', GIT_ASKPASS='', GIT_AUTHOR_NAME='MewCode Collaboration',
                   GIT_AUTHOR_EMAIL='collaboration@localhost', GIT_COMMITTER_NAME='MewCode Collaboration',
                   GIT_COMMITTER_EMAIL='collaboration@localhost')
        result = subprocess.run(['git', '-c', 'core.hooksPath=' + os.devnull,
            '-c', 'core.autocrlf=false', '-c', 'core.longpaths=true', '-c', 'commit.gpgSign=false',
            *args], cwd=path, env=env, input=data, capture_output=True, timeout=120)
        if check and result.returncode:
            raise ValueError(result.stderr.decode('utf-8', errors='replace')[:1200])
        return result

    def save(self, attempt_id, row, event=None):
        def update(value):
            attempt = ledger.check_attempt(value, attempt_id)
            attempt['workspace'] = copy.deepcopy(row)
            if event:
                ledger.event(value, event, contract_id=attempt['contract_id'],
                             attempt_id=attempt_id, workspace=copy.deepcopy(row))
        self.runtime.mutate(update)

    def baseline(self):
        saved = self.runtime.read().get('workspace_baseline')
        manager = WorkspaceSnapshots(self.source)
        if saved:
            return manager, manager.load(saved['snapshot_id'])
        snapshot = asyncio.run(manager.archive(run_id=self.runtime.run_id, node_id='workspace_baseline',
            reason='collaboration_worktrees', event_sequence=0))
        if any(not row['covered'] for row in snapshot['files'].values()):
            raise ValueError('项目快照存在未覆盖文件，不能宣称完整隔离；原目录未改变')
        self.runtime.mutate(lambda value: value.update(workspace_baseline={
            'snapshot_id': snapshot['snapshot_id'], 'commit': snapshot['commit']}))
        return manager, snapshot

    def verify_location(self, row):
        target = Path(row['path'])
        expected = self.directory / row['name']
        if (target != expected or target.resolve(strict=True) != expected
                or self.directory.resolve(strict=True) != self.directory):
            raise ValueError('节点工作区位置或归属已变化')
        if not (target / '.git').is_file():
            raise ValueError('节点 Git 工作区已丢失；保留检查点，不重建覆盖')
        reference = (target / '.git').read_text(encoding='utf-8').strip()
        if not reference.startswith('gitdir: '):
            raise ValueError('节点 Git 管理目录无效')
        admin = Path(reference[len('gitdir: '):])
        admin = (target / admin).resolve()
        common = (admin / (admin / 'commondir').read_text(encoding='utf-8').strip()).resolve()
        if (str(common) != row['git_common_dir']
                or (admin / 'HEAD').read_text(encoding='utf-8').strip() != 'ref: refs/heads/worktree-' + row['name']):
            raise ValueError('节点 Git 归属或分支被修改')
        return target

    def verify_sealed(self, row):
        try:
            path = self.verify_location(row)
            head = self.git(path, 'rev-parse', 'HEAD').stdout.decode().strip()
            snapshot = WorkspaceSnapshots(path).load(row['snapshot_id'])
            files = WorkspaceSnapshots(path).inventory()
            expected = {key: (val['sha256'], val.get('mode')) for key, val in snapshot['files'].items()}
            actual = {key: (val['sha256'], val.get('mode')) for key, val in files.items()}
            return row.get('status') == 'sealed' and head == row['commit'] and expected == actual
        except (ValueError, OSError, SnapshotError, subprocess.SubprocessError):
            return False

    def current_commit(self, row):
        try:
            return (row.get('status') == 'sealed'
                    and WorktreeManager.read_worktree_head_sha(str(self.verify_location(row))) == row['commit'])
        except (OSError, ValueError):
            return False

    def inputs(self, contract, value):
        inputs = {}
        for key in contract.get('bound_dependencies', contract['depends_on']):
            if not value['contracts'][key].get('workspace_mode'):
                continue
            cert = value['certificates'][key]
            row = value['attempts'][cert['attempt_id']].get('workspace')
            if not row or not self.verify_sealed(row):
                raise ValueError('上游隔离交付已变化：' + key)
            inputs[key] = row['commit']
        return inputs

    @workspace_io
    def prepare(self, attempt_id):
        # Shared baseline creation and Git worktree registration must not race.
        with self._lock:
            value = self.runtime.read()
            attempt = ledger.check_attempt(value, attempt_id)
            contract = value['contracts'][attempt['contract_id']]
            inputs = self.inputs(contract, value)
            row = copy.deepcopy(attempt.get('workspace'))
            if row:
                if row['inputs'] != inputs:
                    raise ValueError('工作区输入版本已变化，不能复用旧合并')
                if row['status'] != 'preparing':
                    self.verify_location(row)
                    if row['status'] == 'sealing':
                        return self.finish_seal(attempt_id, row)
                    if row['status'] == 'conflict':
                        raise WorkspaceConflict(row)
                    return row
            manager, baseline = self.baseline()
            commits = list(dict.fromkeys(inputs.values())) or [baseline['commit']]
            if len(commits) > 1 and contract['workspace_mode'] != 'merge':
                raise ValueError('多分支交付缺少合并节点')
            if not row:
                name = 'node-' + digest([self.runtime.run_id, attempt_id])[:24]
                previous = [item for item in ledger.attempts_for(value, attempt['contract_id'])
                    if item['id'] != attempt_id and item['number'] < attempt['number']
                    and not item.get('superseded')
                    and item.get('dependencies') == attempt['dependencies']
                    and item.get('workspace', {}).get('status') in {'active', 'sealing', 'sealed'}]
                if previous:
                    prior = self.seal(previous[-1]['id'])
                    commits = [prior['commit']]
                common = manager._git(['rev-parse', '--git-common-dir']).stdout.decode('utf-8').strip()
                row = {'name': name, 'path': str(self.directory / name), 'source': str(self.source),
                       'base': commits[0], 'inputs': inputs, 'mode': contract['workspace_mode'],
                       'git_common_dir': str((self.source / common).resolve()),
                       'status': 'preparing', 'merged': [], 'conflicts': []}
                if previous:
                    row['continued_from'] = previous[-1]['id']
                self.save(attempt_id, row)
            if self.directory.exists() and self.directory.resolve() != self.directory:
                raise ValueError('工作区目录不能重定向')
            wt = WorktreeManager(str(manager.git_dir) if manager.shadow else str(self.source),
                                 worktree_dir=str(self.directory), post_creation_setup=False)
            asyncio.run(wt.create(row['name'], row['base']))
            path = self.verify_location(row)
            for commit in commits[1:]:
                if commit in row['merged']:
                    continue
                pending = self.git(path, 'rev-parse', '-q', '--verify', 'MERGE_HEAD', check=False)
                if pending.returncode == 0:
                    if pending.stdout.decode().strip() != commit:
                        raise ValueError('合并中断记录与固定输入不一致')
                    result = pending
                else:
                    result = self.git(path, 'merge', '--no-commit', '--no-ff', '--no-edit', commit, check=False)
                conflicts = self.git(path, 'diff', '--name-only', '--diff-filter=U', '-z').stdout
                if conflicts:
                    row.update(status='conflict', conflicts=[item.decode('utf-8') for item in conflicts.split(b'\0') if item])
                    self.save(attempt_id, row, 'workspace_merge_conflict')
                    raise WorkspaceConflict(row)
                if result.returncode:
                    raise ValueError('Git 合并检查未完成：' + result.stderr.decode('utf-8', errors='replace')[:800])
                merge_head = self.git(path, 'rev-parse', '-q', '--verify', 'MERGE_HEAD', check=False)
                if merge_head.returncode == 0:
                    self.git(path, 'commit', '--no-verify', '--no-gpg-sign', '-m', 'MewCode merge input ' + commit)
                row['merged'].append(commit)
                self.save(attempt_id, row)
            row.update(status='active', execution_base=self.git(path, 'rev-parse', 'HEAD').stdout.decode().strip())
            self.save(attempt_id, row, 'workspace_merge_checked' if row['mode'] == 'merge' else 'workspace_prepared')
            return row

    @workspace_io
    def seal(self, attempt_id):
        with self._lock:
            value = self.runtime.read()
            attempt = ledger.check_attempt(value, attempt_id)
            row = copy.deepcopy(attempt['workspace'])
            if row['status'] == 'sealing':
                return self.finish_seal(attempt_id, row)
            if row['status'] == 'sealed':
                if not self.verify_sealed(row):
                    raise ValueError('节点固定提交已变化，不能沿用验收')
                return row
            if row['status'] != 'active':
                raise ValueError('节点工作区尚未通过合并检查')
            path = self.verify_location(row)
            if self.git(path, 'rev-parse', 'HEAD').stdout.decode().strip() != row['execution_base']:
                raise ValueError('节点不得修改宿主 Git 提交或分支')
            manager = WorkspaceSnapshots(path)
            snapshot = asyncio.run(manager.archive(run_id=self.runtime.run_id, node_id=attempt['contract_id'],
                reason='node_delivery', event_sequence=0))
            if any(not item['covered'] for item in snapshot['files'].values()):
                raise ValueError('节点交付存在无法固定的文件')
            changed = self.git(path, 'diff', '--name-only', '--no-renames', '-z',
                               row['execution_base'], snapshot['commit']).stdout
            changed = [item.decode('utf-8') for item in changed.split(b'\0') if item]
            scopes = value['contracts'][attempt['contract_id']]['capabilities'].get('write_paths', [])
            if any(not any((path / item).is_relative_to(path / relative_scope(scope)) for scope in scopes)
                   for item in changed):
                raise ValueError('隔离节点改动超出原合同写入范围')
            tree = self.git(path, 'rev-parse', snapshot['commit'] + '^{tree}').stdout.decode().strip()
            commit = self.git(path, 'commit-tree', tree, '-p', row['execution_base'],
                              data=('MewCode node delivery ' + attempt_id).encode()).stdout.decode().strip()
            row.update(status='sealing', commit=commit, snapshot_id=snapshot['snapshot_id'], changed_paths=changed)
            self.save(attempt_id, row)
            return self.finish_seal(attempt_id, row)

    def finish_seal(self, attempt_id, row):
        path = self.verify_location(row)
        head = self.git(path, 'rev-parse', 'HEAD').stdout.decode().strip()
        if head == row['execution_base']:
            self.git(path, 'update-ref', 'HEAD', row['commit'], row['execution_base'])
        elif head != row['commit']:
            raise ValueError('固定交付中断后 Git 提交发生变化，不能覆盖')
        self.git(path, 'read-tree', row['commit'])
        row.update(status='sealed')
        if not self.verify_sealed(row):
            raise ValueError('固定交付时文件发生变化，保留工作区等待核对')
        self.save(attempt_id, row, 'workspace_sealed')
        return row

    def acceptance(self, row):
        return WorkspaceAcceptance(self.source, self.verify_location(row),
            review=self.runtime.review, command=self.runtime.acceptance.command)

    @workspace_io
    def public_delivery(self, result, report, row):
        from dataclasses import replace
        snapshot = WorkspaceSnapshots(row['path']).load(row['snapshot_id'])
        for item in report['artifacts']:
            if item['kind'] != 'file':
                continue
            target = (self.source / item['path']).resolve()
            try:
                relative = target.relative_to(Path(row['path'])).as_posix()
            except ValueError as exc:
                raise ValueError('隔离节点文件交付必须来自本节点固定提交') from exc
            if snapshot['files'].get(relative, {}).get('sha256') != item['sha256']:
                raise ValueError('交付文件未包含在固定提交中；忽略文件不能作为已合并结果')
        handoff = copy.deepcopy(result.handoff or {})
        handoff['artifacts'] = [{'path': item['path']} for item in report['artifacts'] if item['kind'] == 'file']
        handoff['workspace'] = {key: copy.deepcopy(row[key]) for key in ('path', 'commit', 'inputs', 'mode', 'changed_paths')}
        return replace(result, handoff=handoff)

    def execution_policy(self, policy, row, contract):
        from dataclasses import replace
        sources = copy.deepcopy(policy.stage_inputs or {})
        for source in sources.values():
            for artifact in source.get('fixed_artifacts', []):
                if artifact.get('kind') == 'file':
                    for field in ('path', 'snapshot_path'):
                        if artifact.get(field):
                            artifact[field] = str(self.source / artifact[field])
        return replace(policy, work_dir=row['path'], collaboration_workspace=copy.deepcopy(row),
            stage_inputs=sources, expansion_holder=None, collaborative_expansion=False,
            allow_delegation=False, max_direct_agents=0, allow_recursive_delegation=False,
            expansion_write_paths=tuple(str(Path(row['path']) / relative_scope(path))
                for path in contract['capabilities'].get('write_paths', [])))

    @staticmethod
    def prompt(row):
        return ('\n【隔离工作区】只在下列节点目录内完成原合同。上游提交已由宿主合入；'
                'Git 元数据由宿主管理，不执行提交、切分支、重置或修改其他工作区。'
                '使用相对路径交付文件，并完成合并后的合同检查；工作区隔离不增加任何权限。\n'
                + json.dumps({key: row[key] for key in ('path', 'inputs', 'mode')}, ensure_ascii=False))


def repair_marker(attempt, failed):
    """A new checkout address is not progress on the same failed delivery."""
    artifacts = copy.deepcopy((attempt.get('report') or {}).get('artifacts', []))
    workspace = attempt.get('workspace')
    if workspace:
        for row in artifacts:
            if row.get('kind') == 'file':
                target = Path(workspace['source']) / row['path']
                row['path'] = target.relative_to(Path(workspace['path'])).as_posix()
    # A differently worded review is not a changed delivery. Infrastructure
    # faults are handled before this path; these are still-failing prerequisites.
    failed = [{'id': row['id'], 'version': row['version']} for row in failed]
    return digest([failed, artifacts])
