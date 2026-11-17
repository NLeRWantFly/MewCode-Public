"""Offline srt pilot policy tests; these do not install or run the backend."""
from __future__ import annotations

import os
import json
import subprocess
from pathlib import Path

import pytest

from mewcode.sandbox.srt_windows import (
    SrtPilotUnavailable,
    build_srt_pilot_settings,
    srt_pilot_argv,
)


def test_pilot_policy_is_offline_and_narrow(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    output = workspace / "output"
    output.mkdir(parents=True)
    profile = tmp_path / "profile"
    settings = build_srt_pilot_settings(
        workspace,
        read_directories=[workspace],
        write_directories=[output],
        env={"USERPROFILE": str(profile)},
    )
    assert settings["network"] == {
        "allowedDomains": [], "deniedDomains": ["*"], "allowLocalBinding": False,
    }
    assert settings["enableWeakerNestedSandbox"] is False
    assert settings["enableWeakerNetworkIsolation"] is False
    filesystem = settings["filesystem"]
    assert filesystem["allowRead"] == [str(workspace)]
    assert filesystem["allowWrite"] == [str(output)]
    assert str(workspace / ".mewcode") in filesystem["denyRead"]
    assert str(workspace / ".mewcode") in filesystem["denyWrite"]
    assert str(workspace / ".git") in filesystem["denyWrite"]
    assert str(profile / ".codex" / "auth.json") in filesystem["denyRead"]
    assert str(profile) in filesystem["denyRead"]


def test_pilot_rejects_undeclared_or_external_write_scope(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    with pytest.raises(SrtPilotUnavailable, match="workspace root"):
        build_srt_pilot_settings(workspace, read_directories=[workspace], write_directories=[workspace])
    with pytest.raises(SrtPilotUnavailable, match="inside the workspace"):
        build_srt_pilot_settings(workspace, read_directories=[workspace], write_directories=[external])
    with pytest.raises(SrtPilotUnavailable, match="explicit read scope"):
        build_srt_pilot_settings(workspace, read_directories=[])


def test_pilot_rejects_host_authority_write_scope(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    authority = workspace / ".mewcode"
    authority.mkdir(parents=True)
    with pytest.raises(SrtPilotUnavailable, match="host authority"):
        build_srt_pilot_settings(
            workspace, read_directories=[workspace], write_directories=[authority],
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows pilot argv")
def test_pilot_argv_requires_explicit_host_selected_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    node = tmp_path / "node.exe"
    cli = tmp_path / "cli.js"
    settings = tmp_path / "settings.json"
    for path in (node, cli):
        path.write_text("synthetic", encoding="utf-8")
    settings.write_text(json.dumps(build_srt_pilot_settings(
        workspace, read_directories=[workspace], env={"USERPROFILE": str(tmp_path / "profile")},
    )), encoding="utf-8")
    argv = srt_pilot_argv(
        "Write-Output probe", work_dir=workspace, settings_path=settings,
        node_exe=node, srt_cli=cli, read_directories=[workspace],
        env={"USERPROFILE": str(tmp_path / "profile")},
    )
    assert argv == [str(node), str(cli), "--settings", str(settings), "-c", "Write-Output probe"]
    rogue = workspace / "cli.js"
    rogue.write_text("synthetic", encoding="utf-8")
    with pytest.raises(SrtPilotUnavailable, match="outside the workspace"):
        srt_pilot_argv(
            "Write-Output probe", work_dir=workspace, settings_path=settings,
            node_exe=node, srt_cli=rogue, read_directories=[workspace],
            env={"USERPROFILE": str(tmp_path / "profile")},
        )
    settings.write_text("{}", encoding="utf-8")
    with pytest.raises(SrtPilotUnavailable, match="differ from"):
        srt_pilot_argv(
            "Write-Output probe", work_dir=workspace, settings_path=settings,
            node_exe=node, srt_cli=cli, read_directories=[workspace],
            env={"USERPROFILE": str(tmp_path / "profile")},
        )


@pytest.mark.skipif(
    os.name != "nt" or os.environ.get("MEWCODE_TEST_SRT_SANDBOX") != "1",
    reason="explicit opt-in after Windows srt installation",
)
def test_real_srt_can_run_synthetic_read_only_command(tmp_path: Path) -> None:
    """Only a live smoke test; no production boundary is enabled by this pass."""
    root = tmp_path / "workspace"
    profile = tmp_path / "synthetic-profile"
    root.mkdir()
    profile.mkdir()
    settings = tmp_path / "settings.json"
    synthetic_env = {"USERPROFILE": str(profile)}
    settings.write_text(json.dumps(build_srt_pilot_settings(
        root, read_directories=[root], env=synthetic_env,
    )), encoding="utf-8")
    node = Path(os.environ["MEWCODE_SRT_NODE_EXE"])
    cli = Path(os.environ["MEWCODE_SRT_CLI"])
    argv = srt_pilot_argv(
        "cmd.exe /c echo SRT_SYNTHETIC_READ_OK", work_dir=root,
        settings_path=settings, node_exe=node, srt_cli=cli,
        read_directories=[root], env=synthetic_env,
    )
    result = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert "SRT_SYNTHETIC_READ_OK" in result.stdout
