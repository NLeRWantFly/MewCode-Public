import subprocess
from pathlib import Path

import pytest

from mewcode.kernel.snapshots import SnapshotError, SnapshotManager


def git(root, *args):
    result = subprocess.run(['git', '-c', 'core.hooksPath=NUL', *args], cwd=root,
                            capture_output=True, check=True)
    return result.stdout


@pytest.mark.asyncio
async def test_git_snapshot_preserves_index_dirty_files_and_ignored_scope(tmp_path):
    workspace = tmp_path / 'project'
    workspace.mkdir()
    git(workspace, 'init')
    (workspace / 'tracked.txt').write_text('first', encoding='utf-8')
    (workspace / '.gitignore').write_text('private.txt\n', encoding='utf-8')
    git(workspace, 'add', 'tracked.txt', '.gitignore')
    git(workspace, '-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '-m', 'fixture')
    (workspace / 'tracked.txt').write_text('staged', encoding='utf-8')
    git(workspace, 'add', 'tracked.txt')
    index_before = (workspace / '.git/index').read_bytes()
    (workspace / 'tracked.txt').write_text('dirty content', encoding='utf-8')
    (workspace / 'new.txt').write_text('untracked', encoding='utf-8')
    (workspace / 'private.txt').write_text('ignored', encoding='utf-8')
    (workspace / '.venv').mkdir()
    (workspace / '.venv/environment').write_text('excluded', encoding='utf-8')
    manager = SnapshotManager(workspace)
    snapshot = await manager.create(run_id='fixture', node_id='N03', reason='before_auto_continue', event_sequence=7)
    tree = Path(snapshot['work_dir'])
    assert not snapshot['shadow']
    assert (workspace / '.git/index').read_bytes() == index_before
    assert (tree / 'tracked.txt').read_text(encoding='utf-8') == 'dirty content'
    assert (tree / 'new.txt').read_text(encoding='utf-8') == 'untracked'
    assert not (tree / 'private.txt').exists()
    assert not (tree / '.venv').exists()
    assert manager.covers(snapshot['snapshot_id'], ['tracked.txt', 'new-output.txt'])
    assert not manager.covers(snapshot['snapshot_id'], ['private.txt'])


@pytest.mark.asyncio
async def test_non_git_snapshot_restore_keeps_pre_restore_snapshot(tmp_path):
    workspace = tmp_path / '项目'
    workspace.mkdir()
    source = workspace / 'file.txt'
    source.write_text('before', encoding='utf-8')
    manager = SnapshotManager(workspace)
    first = await manager.create(run_id='fixture', node_id='N03', reason='before', event_sequence=1)
    assert first['shadow'] and (workspace / '.mewcode/shadow.git').is_dir()
    assert not (workspace / '.git').exists()
    source.write_text('after', encoding='utf-8')
    (workspace / 'created.txt').write_text('later', encoding='utf-8')
    second = await manager.create(run_id='fixture', node_id='N03', reason='after', event_sequence=2)
    assert manager.diff(first['snapshot_id'], second['snapshot_id']) == ['created.txt', 'file.txt']
    restored = await manager.restore(first['snapshot_id'], run_id='fixture', node_id='N03', event_sequence=3)
    assert source.read_text(encoding='utf-8') == 'before'
    assert not (workspace / 'created.txt').exists()
    assert manager.load(restored['backup'])['reason'] == 'before_restore'
    assert len(manager.list()) == 3


@pytest.mark.asyncio
async def test_large_files_are_digest_only_and_cannot_be_auto_restored(tmp_path):
    source = tmp_path / 'large.txt'
    source.write_text('large content', encoding='utf-8')
    manager = SnapshotManager(tmp_path, max_file_bytes=4)
    snapshot = await manager.create(run_id='fixture', node_id='N03', reason='limit', event_sequence=1)
    row = snapshot['files']['large.txt']
    assert row['sha256'] and not row['covered'] and 'blob' not in row
    assert not manager.covers(snapshot['snapshot_id'], ['large.txt'])
    source.write_text('changed large content', encoding='utf-8')
    with pytest.raises(SnapshotError, match='outside snapshot coverage'):
        await manager.restore(snapshot['snapshot_id'], run_id='fixture', node_id='N03', event_sequence=2)
    assert source.read_text(encoding='utf-8') == 'changed large content'
    assert len(manager.list()) == 2


@pytest.mark.asyncio
async def test_restore_cannot_destroy_large_current_content_with_an_incomplete_backup(tmp_path):
    source = tmp_path / 'file.txt'
    source.write_text('tiny', encoding='utf-8')
    manager = SnapshotManager(tmp_path, max_file_bytes=4)
    first = await manager.create(run_id='fixture', node_id='N03', reason='small', event_sequence=1)
    source.write_text('new large content', encoding='utf-8')
    with pytest.raises(SnapshotError, match='outside snapshot coverage'):
        await manager.restore(first['snapshot_id'], run_id='fixture', node_id='N03', event_sequence=2)
    assert source.read_text(encoding='utf-8') == 'new large content'
