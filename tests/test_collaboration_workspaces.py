"""Real Git isolation/merge tests, with no provider calls or live task access."""
import asyncio
import copy
from pathlib import Path
import subprocess

import pytest

from mewcode.expert_group import ExpertRoleResult
from mewcode.expert_runs import ExpertRunStore
from mewcode.experts.flow import ExpertFlowSpec
from mewcode.kernel import collaboration as ledger
from mewcode.orchestration.collab_compile import compile_plan
from mewcode.orchestration.collab_workspaces import CollaborationWorkspaces
from tests.test_collaboration_mode import PROFILE, REVIEWER, RUN_ID, flow
from tests.test_collaboration_integration import orchestrator, run


def git(root, *args):
    result = subprocess.run(['git', '-c', 'core.autocrlf=false', '-c', 'core.hooksPath=',
        '-c', 'user.name=Test', '-c', 'user.email=test@localhost', *args],
        cwd=root, capture_output=True, check=True)
    return result.stdout


def isolated_flow():
    raw = flow().as_dict()
    keys = ['left', 'right', 'merge']
    template = raw['nodes'][0]
    raw['nodes'] = [{**copy.deepcopy(template), 'node_id': key, 'label': key,
        'tools': ['ReadFile', 'WriteFile'], 'capabilities': {'write_paths': ['result.txt']}}
        for key in keys]
    raw['edges'] = [{'from_node': key, 'to_node': 'merge'} for key in keys[:2]]
    raw['finalizer_node_id'] = 'merge'
    raw['meta']['node_plan_briefs'] = {key: {'known_inputs': ['目标'], 'outputs': ['result.txt'],
                                           'acceptance': ['内容与整合结果正确']} for key in keys}
    for key in keys:
        raw['meta']['collaboration_spec']['document']['entries'].append({
            'id': key, 'kind': 'node', 'body': '完成原始目标', 'state': 'active', 'data': {
                'contract': {'workspace_mode': 'merge' if key == 'merge' else 'worktree',
                    'checks': [{'id': key + '_file', 'kind': 'file_exists', 'version': 1, 'path': 'result.txt'}]}}})
    return ExpertFlowSpec.from_value(raw)


def setup(tmp_path, *, repository=True, spec=None):
    root = tmp_path / 'project'
    root.mkdir()
    content = ''.join(f'original {i}\n' for i in range(30))
    (root / 'result.txt').write_text(content, encoding='utf-8')
    if repository:
        git(root, 'init')
        git(root, 'add', 'result.txt')
        git(root, 'commit', '-m', 'baseline')
        (root / 'result.txt').write_text(content.replace('original 15\n', 'user staged 15\n'), encoding='utf-8')
        git(root, 'add', 'result.txt')
        (root / 'user-note.txt').write_text('untracked user work', encoding='utf-8')
    spec = spec or isolated_flow()
    store = ExpertRunStore(tmp_path / 'runs', storage='sqlite')
    store.create(run_id=RUN_ID, conversation_id='test', task='goal', attachments=[],
                 profile=PROFILE.as_dict(), strategy='stable', flow=spec)
    store.mutate(RUN_ID, lambda record: record.update(role_models={'roles': {'verifier': REVIEWER.as_dict()}}))
    return root, spec, store


def delivered():
    return ExpertRoleResult(text='实际文件已交付', handoff={'status': 'completed', 'artifacts': [{'path': 'result.txt'}]})


@pytest.mark.asyncio
@pytest.mark.parametrize('repository', [True, False])
async def test_parallel_worktrees_merge_and_preserve_user_checkout(tmp_path, repository):
    root, spec, store = setup(tmp_path, repository=repository)
    original = (root / 'result.txt').read_bytes()
    index = (root / '.git/index').read_bytes() if repository else None
    head = git(root, 'rev-parse', 'HEAD') if repository else None
    entered, ready, paths = [], asyncio.Event(), {}
    async def runner(node, model, prompt, policy, *_):
        target = Path(policy.work_dir) / 'result.txt'
        paths[node.node_id] = policy.work_dir
        assert policy.collaboration_workspace and policy.expansion_holder is None
        if node.node_id != 'merge':
            entered.append(node.node_id)
            if len(entered) == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), 15)
            number = 1 if node.node_id == 'left' else 28
            target.write_text(target.read_text(encoding='utf-8').replace(f'original {number}\n',
                f'{node.node_id} changed\n'), encoding='utf-8')
        else:
            text = target.read_text(encoding='utf-8')
            assert 'left changed' in text and 'right changed' in text
        return delivered()
    async def review(*_):
        return {'passed': True, 'evidence': ['checked actual merged delivery']}
    host = orchestrator(store, root, runner, review)
    host.archive_root = tmp_path / 'archive'
    result = await run(host, store, spec)
    assert result.status == 'completed', result.final_summary
    assert len(set(paths.values())) == 3
    assert (root / 'result.txt').read_bytes() == original
    if repository:
        assert (root / '.git/index').read_bytes() == index
        assert git(root, 'rev-parse', 'HEAD') == head
        assert (root / 'user-note.txt').read_text() == 'untracked user work'
    else:
        assert not (root / '.git').exists()
    value = store.read_collaboration(RUN_ID)
    assert all(value['attempts'][cert['attempt_id']]['workspace']['status'] == 'sealed'
               for cert in value['certificates'].values())
    assert any(row['kind'] == 'workspace_merge_checked' for row in value['timeline'])
    invoked = len(entered)
    assert (await run(host, store, spec)).status == 'completed'
    assert len(entered) == invoked  # Accepted worktrees are reused, never reexecuted.


@pytest.mark.asyncio
async def test_conflict_retains_both_branches_and_never_invokes_merge_executor(tmp_path):
    root, spec, store = setup(tmp_path)
    original = (root / 'result.txt').read_bytes()
    invoked = []
    async def runner(node, model, prompt, policy, *_):
        invoked.append(node.node_id)
        assert node.node_id != 'merge'
        target = Path(policy.work_dir) / 'result.txt'
        target.write_text(target.read_text(encoding='utf-8').replace('original 1\n', node.node_id + '\n'), encoding='utf-8')
        return delivered()
    async def review(*_):
        return {'passed': True, 'evidence': ['checked']}
    host = orchestrator(store, root, runner, review)
    host.archive_root = tmp_path / 'archive'
    result = await run(host, store, spec)
    value = store.read_collaboration(RUN_ID)
    assert result.status == 'blocked', result.final_summary
    assert sorted(invoked) == ['left', 'right']
    assert set(value['certificates']) == {'left', 'right'}
    merge = ledger.attempts_for(value, 'merge')[-1]['workspace']
    assert merge['status'] == 'conflict' and merge['conflicts'] == ['result.txt']
    assert (Path(merge['path']) / 'result.txt').exists()
    assert (root / 'result.txt').read_bytes() == original


def test_workspace_contract_rejects_unsafe_or_missing_merge():
    for change in ['shared', 'no_merge', 'external', 'escape', 'tools']:
        spec = isolated_flow().as_dict()
        entries = spec['meta']['collaboration_spec']['document']['entries']
        if change == 'shared':
            entries[-1]['data']['contract'].pop('workspace_mode')
        elif change == 'no_merge':
            entries[-1]['data']['contract']['workspace_mode'] = 'worktree'
        elif change == 'external':
            spec['nodes'][0]['capabilities']['network_hosts'] = ['example.com']
        elif change == 'escape':
            spec['nodes'][0]['capabilities']['write_paths'] = ['../outside']
        else:
            spec['nodes'][0]['tools'] = ['*']
        with pytest.raises(ValueError):
            compile_plan(ExpertFlowSpec.from_value(spec))


@pytest.mark.asyncio
async def test_out_of_scope_write_cannot_be_certified(tmp_path):
    root, spec, store = setup(tmp_path)
    async def runner(node, model, prompt, policy, *_):
        (Path(policy.work_dir) / 'forbidden.txt').write_text('outside contract')
        return delivered()
    async def review(*_):
        return {'passed': True, 'evidence': ['checked']}
    host = orchestrator(store, root, runner, review)
    host.archive_root = tmp_path / 'archive'
    result = await run(host, store, spec)
    assert result.status == 'blocked'
    assert not store.read_collaboration(RUN_ID)['certificates']
    assert not (root / 'forbidden.txt').exists()


def test_git_environment_cannot_redirect_workspace(tmp_path, monkeypatch):
    root = tmp_path / 'repo'
    root.mkdir()
    git(root, 'init')
    monkeypatch.setenv('GIT_DIR', str(tmp_path / 'outside'))
    assert CollaborationWorkspaces.git(root, 'rev-parse', '--show-toplevel').stdout.decode().strip().replace('\\', '/') == root.as_posix()


def test_seal_interruption_recovers_same_commit_and_worktree(tmp_path, monkeypatch):
    from mewcode.orchestration.collab_runtime import CollaborationRuntime
    from tests.test_collaboration_mode import CATALOG
    root, spec, store = setup(tmp_path)
    runtime = CollaborationRuntime(store=store, run_id=RUN_ID, flow=spec, catalog=CATALOG, work_dir=root)
    attempt = runtime.mutate(lambda value: ledger.dispatch(value, 'left'))
    workspace = runtime.workspaces.prepare(attempt['id'])
    target = Path(workspace['path']) / 'result.txt'
    target.write_text('saved partial delivery', encoding='utf-8')
    real_git = runtime.workspaces.git
    def interrupted(path, *args, **kwargs):
        if args[0] == 'read-tree':
            raise OSError('simulated host interruption')
        return real_git(path, *args, **kwargs)
    monkeypatch.setattr(runtime.workspaces, 'git', interrupted)
    with pytest.raises(ValueError, match='interruption'):
        runtime.workspaces.seal(attempt['id'])
    saved = runtime.read()['attempts'][attempt['id']]['workspace']
    assert saved['status'] == 'sealing'
    restored = CollaborationRuntime(store=store, run_id=RUN_ID, flow=spec, catalog=CATALOG, work_dir=root)
    recovered = restored.workspaces.prepare(attempt['id'])
    assert recovered['commit'] == saved['commit'] and recovered['path'] == workspace['path']
    assert recovered['status'] == 'sealed' and target.read_text() == 'saved partial delivery'
    assert restored.workspaces.verify_sealed(recovered)


def test_tracked_files_survive_ignore_rules_and_ignored_outputs_are_not_merged(tmp_path):
    from mewcode.orchestration.collab_runtime import CollaborationRuntime
    from tests.test_collaboration_mode import CATALOG
    root, spec, store = setup(tmp_path)
    (root / 'script.sh').write_text('#!/bin/sh\necho ready\n', encoding='utf-8')
    git(root, 'add', 'script.sh')
    git(root, 'update-index', '--chmod=+x', 'script.sh')
    (root / '.gitignore').write_text('result.txt\nignored.bin\n', encoding='utf-8')
    runtime = CollaborationRuntime(store=store, run_id=RUN_ID, flow=spec, catalog=CATALOG, work_dir=root)
    attempt = runtime.mutate(lambda value: ledger.dispatch(value, 'left'))
    row = runtime.workspaces.prepare(attempt['id'])
    assert git(Path(row['path']), 'ls-files', '--stage', '--', 'script.sh').startswith(b'100755 ')
    assert (Path(row['path']) / 'result.txt').read_bytes() == (root / 'result.txt').read_bytes()
    (Path(row['path']) / 'ignored.bin').write_bytes(b'not committed')
    row = runtime.workspaces.seal(attempt['id'])
    artifacts = runtime.workspaces.acceptance(row).freeze({'artifacts': [{'path': 'ignored.bin'}]})
    with pytest.raises(ValueError, match='未包含在固定提交'):
        runtime.workspaces.public_delivery(delivered(), {'artifacts': artifacts}, row)


def test_child_policy_checks_original_path_and_blocks_shared_writes(tmp_path):
    from mewcode.kernel.shadow_policy import bind_collaboration_worktree
    from mewcode.permissions.modes import PermissionMode
    from mewcode.permissions.policy import build_permission_checker
    from mewcode.permissions.rules import Rule
    from mewcode.tools.write_file import WriteFile
    root = tmp_path / 'project'
    checkout = root / '.mewcode' / 'collaboration-worktrees' / 'run' / 'node-test'
    checkout.mkdir(parents=True)
    parent = build_permission_checker(root, mode=PermissionMode.BYPASS)
    child = build_permission_checker(checkout, parent_policy=parent, profile='worktree')
    bind_collaboration_worktree(child, parent, {'source': str(root), 'path': str(checkout),
        'name': checkout.name, 'status': 'active'})
    tool = WriteFile()
    assert child.check(tool, {'file_path': str(checkout / 'result.txt'), 'content': 'x'}).effect == 'allow'
    for target in (root / 'result.txt', tmp_path / 'outside.txt', checkout / '.git'):
        assert child.check(tool, {'file_path': str(target), 'content': 'x'}).effect == 'deny'
    parent.rule_engine.append_session_rule(Rule('WriteFile', str(root / 'result.txt'), 'deny'))
    assert child.check(tool, {'file_path': str(checkout / 'result.txt'), 'content': 'x'}).effect == 'deny'
    assert child.requires_command_isolation


def test_task_grants_map_only_into_current_worktree(tmp_path):
    from mewcode.kernel.shadow_policy import bind_collaboration_worktree
    from mewcode.permissions.modes import PermissionMode
    from mewcode.permissions.policy import build_permission_checker
    from mewcode.permissions.task_authorization import TaskAuthorization
    from mewcode.tools.write_file import WriteFile
    root = tmp_path / 'project'
    checkout = root / '.mewcode' / 'collaboration-worktrees' / 'run' / 'node-test'
    checkout.mkdir(parents=True)
    parent = build_permission_checker(root, mode=PermissionMode.DEFAULT)
    authorization = TaskAuthorization.from_user_scope({'write_directories': [str(root / 'output')],
        'allowed_tools': ['WriteFile'], 'allowed_behaviors': ['git_write'], 'ttl_seconds': 3600}, task_id='task')
    parent.set_task_authorization(authorization, task_id='task')
    child = build_permission_checker(checkout, parent_policy=parent, profile='worktree')
    bind_collaboration_worktree(child, parent, {'source': str(root), 'path': str(checkout),
        'name': checkout.name, 'status': 'active'})
    assert child.task_authorization.write_directories == (str(checkout / 'output'),)
    assert child.task_authorization.allowed_behaviors == ()
    assert parent.task_authorization == authorization
    assert child.check(WriteFile(), {'file_path': str(checkout / 'output' / 'file.txt'), 'content': 'x'}).effect == 'allow'
    assert child.check(WriteFile(), {'file_path': str(checkout / 'elsewhere.txt'), 'content': 'x'}).effect != 'allow'


@pytest.mark.asyncio
async def test_merge_checks_run_against_combined_checkout_and_can_fail(tmp_path):
    raw = isolated_flow().as_dict()
    raw['meta']['collaboration_spec']['document']['entries'][-1]['data']['contract']['checks'].append(
        {'id': 'integration_test', 'kind': 'test', 'version': 1, 'command': 'python -m pytest'})
    spec = ExpertFlowSpec.from_value(raw)
    root, spec, store = setup(tmp_path, spec=spec)
    async def runner(node, model, prompt, policy, *_):
        if node.node_id != 'merge':
            target = Path(policy.work_dir) / 'result.txt'
            number = 1 if node.node_id == 'left' else 28
            target.write_text(target.read_text().replace(f'original {number}', node.node_id + ' changed'))
        return delivered()
    async def review(*_):
        return {'passed': True, 'evidence': ['checked']}
    inspected = []
    async def command(check, context):
        cwd = Path(context['policy'].work_dir)
        text = (cwd / 'result.txt').read_text()
        assert 'left changed' in text and 'right changed' in text
        inspected.append(str(cwd))
        return {'exit_code': 1, 'receipt_id': 'actual-test-failure-' + str(len(inspected)), 'command_started': True}
    host = orchestrator(store, root, runner, review)
    host.archive_root = tmp_path / 'archive'
    host.collaboration_command = command
    result = await run(host, store, spec)
    assert result.status == 'blocked' and inspected
    assert 'merge' not in store.read_collaboration(RUN_ID)['certificates']
    assert len(inspected) == 2  # Another checkout path is not evidence of improvement.
