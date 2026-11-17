"""Reviewed Auto commands may run only through a bounded container."""
from __future__ import annotations

import os
import shutil

import pytest

from mewcode.permissions.task_authorization import TaskAuthorization
from mewcode.tools.command_sandbox import _mount_arguments, reviewed_auto_command, run_isolated_command


def test_mounts_include_only_explicit_scope(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    scope = TaskAuthorization.from_user_scope({
        "read_directories": [str(allowed)], "write_directories": [str(allowed)],
        "allowed_behaviors": ["isolated_command"],
    }, task_id="task")
    mounts = _mount_arguments(str(tmp_path), scope)
    joined = " ".join(mounts)
    assert str(allowed) in joined
    assert "target=/workspace/allowed" in joined
    assert "target=/workspace," not in joined


@pytest.mark.asyncio
async def test_no_task_scope_fails_closed(tmp_path):
    with reviewed_auto_command(None):
        result = await run_isolated_command("echo unsafe", work_dir=str(tmp_path), timeout=5)
    assert result.is_error and "AUTO_DENIED" in result.output


@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get("MEWCODE_TEST_DOCKER") != "1" or not shutil.which("docker"),
                    reason="opt-in Docker daemon integration test")
async def test_container_cannot_read_other_project_dir_or_inherit_secret(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    forbidden = tmp_path / "forbidden"
    allowed.mkdir(); forbidden.mkdir()
    (forbidden / "secret.txt").write_text("not visible", encoding="utf-8")
    monkeypatch.setenv("MEWCODE_TEST_SECRET", "hidden-host-secret")
    scope = TaskAuthorization.from_user_scope({
        "read_directories": [str(allowed)], "write_directories": [str(allowed)],
        "allowed_behaviors": ["isolated_command"],
    }, task_id="task")
    command = ("python -c \"from pathlib import Path; import os; "
               "Path('/workspace/allowed/out.txt').write_text('ok'); "
               "print(Path('/workspace/forbidden/secret.txt').exists()); "
               "print(os.getenv('MEWCODE_TEST_SECRET', 'absent'))\"")
    with reviewed_auto_command(scope):
        result = await run_isolated_command(command, work_dir=str(tmp_path), timeout=30)
    assert not result.is_error, result.output
    assert "False" in result.output and "absent" in result.output
    assert (allowed / "out.txt").read_text(encoding="utf-8") == "ok"

