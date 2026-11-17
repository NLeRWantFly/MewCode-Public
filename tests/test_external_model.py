from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mewcode.mcp.external_model import (
    ExternalModelError,
    GrokCLIInvoker,
    ModelCallStatus,
    ModelInvocationRequest,
)


def _request(tmp_path: Path, **overrides: object) -> ModelInvocationRequest:
    values: dict[str, object] = {
        "task_id": "task-audit",
        "agent_id": "grok-executor-01",
        "model_id": "grok-4.6",
        "prompt": "Read-only audit",
        "cwd": tmp_path,
        "allowed_tools": ("Read", "Grep"),
        "allowed_paths": ("mewcode/mcp",),
        "forbidden_paths": (".git",),
        "write_mode": "read_only",
        "max_turns": 1,
    }
    values.update(overrides)
    return ModelInvocationRequest(**values)


def test_grok_command_is_bounded_and_pins_model(tmp_path: Path) -> None:
    command = GrokCLIInvoker(project_root=tmp_path).build_command(_request(tmp_path))
    assert command[0] == "grok"
    assert "--model" in command
    assert command[command.index("--model") + 1] == "grok-4.6"
    assert "--no-subagents" in command
    assert command[command.index("--max-turns") + 1] == "1"
    assert command[command.index("--permission-mode") + 1] == "plan"
    assert command[command.index("--tools") + 1] == "read_file,grep"
    assert "--allow" not in command
    assert "--always-approve" not in command
    assert "allowed_paths=[mewcode/mcp]" in command[command.index("--rules") + 1]
    assert "forbidden_paths=[.git]" in command[command.index("--rules") + 1]
    assert "--single" in command


def test_empty_tool_allowlist_does_not_emit_empty_tools_flag(tmp_path: Path) -> None:
    command = GrokCLIInvoker(project_root=tmp_path).build_command(
        _request(tmp_path, allowed_tools=())
    )
    assert "--tools" not in command
    assert "--allow" not in command
    assert "--always-approve" not in command


def test_terminal_tool_adds_bash_approval(tmp_path: Path) -> None:
    command = GrokCLIInvoker(project_root=tmp_path).build_command(
        _request(tmp_path, allowed_tools=("Read", "run_terminal_cmd"))
    )

    assert command[command.index("--tools") + 1] == "read_file,run_terminal_cmd"
    assert command[command.index("--allow") + 1] == "Bash"
    assert "--always-approve" in command


def test_terminal_tool_gets_at_least_two_turns_without_raising_global_cap(tmp_path: Path) -> None:
    command = GrokCLIInvoker(project_root=tmp_path).build_command(
        _request(tmp_path, allowed_tools=("run_terminal_cmd",), max_turns=1)
    )

    assert command[command.index("--max-turns") + 1] == "2"


def test_terminal_tool_respects_global_turn_cap(tmp_path: Path) -> None:
    with pytest.raises(ExternalModelError, match="cannot exceed 3"):
        GrokCLIInvoker(project_root=tmp_path).build_command(
            _request(tmp_path, allowed_tools=("run_terminal_cmd",), max_turns=4)
        )


def test_from_task_normalizes_terminal_turns(tmp_path: Path) -> None:
    request = ModelInvocationRequest.from_task(
        {
            "task_id": "task-terminal",
            "target_agent_id": "grok-executor-01",
            "allowed_tools": ["run_terminal_cmd"],
            "max_turns": 1,
        },
        prompt="Run the bounded command",
        cwd=tmp_path,
    )

    assert request.max_turns == 2


def test_other_builtin_tools_do_not_add_bash_approval(tmp_path: Path) -> None:
    command = GrokCLIInvoker(project_root=tmp_path).build_command(
        _request(tmp_path, allowed_tools=("Read", "Grep"))
    )

    assert "--allow" not in command
    assert "--always-approve" not in command


def test_mcp_coordination_tools_are_not_passed_as_builtin_allowlist(tmp_path: Path) -> None:
    command = GrokCLIInvoker(project_root=tmp_path).build_command(
        _request(tmp_path, allowed_tools=("mewcode-coordination__coord_get_capabilities", "coord_protocol"))
    )
    assert "--tools" not in command


def test_logical_coordination_tools_are_not_passed_as_builtin_allowlist(tmp_path: Path) -> None:
    command = GrokCLIInvoker(project_root=tmp_path).build_command(
        _request(tmp_path, allowed_tools=("ping", "coord_protocol"))
    )
    assert "--tools" not in command


def test_scoped_cwd_cannot_escape_project(tmp_path: Path) -> None:
    request = _request(tmp_path.parent)
    with pytest.raises(ExternalModelError, match="fixed project root"):
        GrokCLIInvoker(project_root=tmp_path).build_command(request)


def test_read_only_requires_plan_mode(tmp_path: Path) -> None:
    request = _request(tmp_path, permission_mode="acceptEdits")
    with pytest.raises(ExternalModelError, match="read_only"):
        GrokCLIInvoker(project_root=tmp_path).build_command(request)


def test_task_path_cannot_escape_fixed_project(tmp_path: Path) -> None:
    request = _request(tmp_path, allowed_paths=("..",))
    with pytest.raises(ExternalModelError, match="escapes"):
        GrokCLIInvoker(project_root=tmp_path).build_command(request)


def test_auth_failure_is_classified_without_raising(tmp_path: Path) -> None:
    def fake_runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args[0], 1, "", "Not signed in. Set XAI_API_KEY")

    result = GrokCLIInvoker(project_root=tmp_path, runner=fake_runner).invoke(_request(tmp_path))
    assert result.status == ModelCallStatus.AUTH_REQUIRED
    assert result.as_evidence()["model_id"] == "grok-4.6"


def test_auth_probe_detects_unauthenticated_model_catalog(tmp_path: Path) -> None:
    def fake_runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args[0][-1] == "models"
        return subprocess.CompletedProcess(args[0], 0, "You are not authenticated.", "")

    result = GrokCLIInvoker(project_root=tmp_path, runner=fake_runner).probe_auth()
    assert result.status == ModelCallStatus.AUTH_REQUIRED
    assert result.task_id == "preflight"


def test_success_output_is_parsed(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        return subprocess.CompletedProcess(args[0], 0, '{"answer":"ok"}', "")

    result = GrokCLIInvoker(project_root=tmp_path, runner=fake_runner).invoke(_request(tmp_path))
    assert result.status == ModelCallStatus.COMPLETED
    assert result.parsed_output == {"answer": "ok"}
    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "replace"


def test_invoker_uses_project_grok_home_without_user_home_detection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_home = tmp_path / ".mewcode" / "grok-home"
    project_home.mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "user-home-that-must-not-be-used"))
    monkeypatch.setenv("HOME", str(tmp_path / "user-home-that-must-not-be-used"))
    monkeypatch.delenv("GROK_HOME", raising=False)
    captured: dict[str, object] = {}

    def fake_runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        return subprocess.CompletedProcess(args[0], 0, '{"answer":"ok"}', "")

    GrokCLIInvoker(project_root=tmp_path, runner=fake_runner).invoke(_request(tmp_path))
    assert captured["env"]["GROK_HOME"] == str(project_home)


def test_invoker_explicit_grok_home_wins_over_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit_home = tmp_path / "explicit-home"
    monkeypatch.setenv("MEWCODE_GROK_HOME", str(tmp_path / "environment-home"))
    captured: dict[str, object] = {}

    def fake_runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        return subprocess.CompletedProcess(args[0], 0, '{"answer":"ok"}', "")

    GrokCLIInvoker(project_root=tmp_path, grok_home=explicit_home, runner=fake_runner).probe_auth()
    assert captured["env"]["GROK_HOME"] == str(explicit_home)


def test_auth_json_access_denied_is_auth_required_without_path_evidence(tmp_path: Path) -> None:
    def fake_runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args[0], 1, "", "Access is denied: C:\\secret\\auth.json")

    result = GrokCLIInvoker(project_root=tmp_path, runner=fake_runner).probe_auth()
    assert result.status is ModelCallStatus.AUTH_REQUIRED
    assert "auth.json" in result.stderr
    assert "C:\\secret" not in result.stderr
