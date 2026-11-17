from types import SimpleNamespace
from pathlib import Path

import pytest

from mewcode.kernel.shadow_policy import bind_shadow_checker, command_in_scope
from mewcode.kernel.snapshots import SnapshotManager
from mewcode.permissions.checker import Decision
from mewcode.permissions.task_authorization import TaskAuthorization
from mewcode.tools.write_file import WriteFile, Params


@pytest.mark.asyncio
async def test_source_hard_denies_and_original_workspace_write_boundary_survive_promotion(tmp_path):
    manager = SnapshotManager(tmp_path)
    snapshot = await manager.create(run_id='fixture', node_id='N03', reason='test', event_sequence=1)
    authorization = TaskAuthorization.from_user_scope({'read_directories': [str(tmp_path)],
        'write_directories': [str(tmp_path)], 'allowed_tools': ['ReadFile', 'WriteFile'],
        'allowed_behaviors': ['isolated_command'], 'network_hosts': ['example.test']}, task_id='fixture')
    def source_check(tool, args):
        return Decision('deny' if Path(args.get('file_path', '')).name == 'protected.txt' else 'allow', '', 'source')
    child = SimpleNamespace(check=lambda *args: Decision('allow', '', 'child'),
        set_task_authorization=lambda value, **kwargs: setattr(child, 'task_authorization', value))
    parent = SimpleNamespace(check=source_check, task_authorization=authorization)
    bind_shadow_checker(child, parent, snapshot)
    tool = SimpleNamespace(name='WriteFile', category='write')
    assert child.check(tool, {'file_path': str(tmp_path / 'source.txt')}).effect == 'deny'
    assert child.check(tool, {'file_path': 'protected.txt'}).effect == 'deny'
    assert child.check(tool, {'file_path': 'allowed.txt'}).effect == 'allow'
    assert child.task_authorization.write_directories == (snapshot['work_dir'],)
    assert child.task_authorization.network_hosts == authorization.network_hosts
    assert child.task_authorization.expires_at == authorization.expires_at
    assert child.requires_command_isolation


def test_scoped_commands_cannot_add_opaque_code_or_an_extra_statement():
    requests = [{'operation_class': 'run_command_prefix', 'scope': {'command_prefix': ['git', 'status']}}]
    assert command_in_scope('git status --short', requests)
    assert not command_in_scope('git status; echo extra', requests)
    assert not command_in_scope('python -c "print(1)"', requests)


@pytest.mark.asyncio
async def test_existing_write_gateway_publishes_exact_binary_bytes(tmp_path):
    data = b'\x00\xff\r\n\x80'
    import base64
    tool = WriteFile()
    result = await tool.run(Params(file_path='artifact.bin', content=base64.b64encode(data).decode('ascii'), encoding='base64'),
                            work_dir=str(tmp_path))
    assert not result.is_error
    assert (tmp_path / 'artifact.bin').read_bytes() == data
