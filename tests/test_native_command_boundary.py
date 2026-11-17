"""P0 Windows command boundary: never turn a text match into host execution."""
from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from mewcode.sandbox import windows
from mewcode.permissions import DangerousCommandDetector, PathSandbox, PermissionChecker, PermissionMode, RuleEngine, Rule
from mewcode.permissions.task_authorization import TaskAuthorization
from mewcode.tools.base import ToolResult
from mewcode.tools.bash import Bash, Params, _child_env
from mewcode.tools.command_sandbox import (
    native_auto_command, native_auto_scope, run_native_command, execute_native_command,
)
from mewcode.sandbox.receipt import NativeExecutionReceipt
from mewcode.tools.run_verification import RunVerification, RunVerificationParams


def test_unattested_native_backend_never_unlocks_script_fastpath(tmp_path: Path, monkeypatch) -> None:
    # Pin the gate to an empty data root so this never depends on host state.
    monkeypatch.setenv("MEWCODE_USER_DATA_DIR", str(tmp_path))
    assert windows.native_fastpath_ready() is False
    assert windows.native_runtime_ready() is False


def test_explicit_command_rule_can_authorize_host_when_native_not_selected(tmp_path: Path) -> None:
    command = "python -m pytest"
    rules = RuleEngine()
    rules.append_session_rule(Rule("Bash", command, "allow"))
    checker = PermissionChecker(
        DangerousCommandDetector(), PathSandbox(str(tmp_path)), rules, PermissionMode.AUTO,
    )
    scope = TaskAuthorization.from_user_scope({
        "read_directories": [str(tmp_path)],
        "allowed_behaviors": ["isolated_command"],
    }, task_id="task")
    checker.set_task_authorization(scope, task_id="task")
    decision = checker.check(Bash(), {"command": command})
    assert (decision.effect, decision.source) == ("allow", "rule_engine")


def test_child_environment_drops_provider_and_proxy_values(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_ORG_ID", "synthetic-private")
    monkeypatch.setenv("MODEL_API_KEY", "synthetic-private")
    monkeypatch.setenv("HTTPS_PROXY", "http://synthetic-private@localhost")
    child = _child_env(None)
    assert "OPENAI_ORG_ID" not in child
    assert "MODEL_API_KEY" not in child
    assert "HTTPS_PROXY" not in child


@pytest.mark.asyncio
async def test_diagnostic_probe_needs_explicit_test_mode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("MEWCODE_TEST_NATIVE_SANDBOX", raising=False)
    with native_auto_command():
        result = await run_native_command(
            "Write-Output probe", work_dir=str(tmp_path), timeout=2,
            diagnostic_only=True,
        )
    assert result.is_error
    assert "diagnostic native probe is disabled" in result.output


@pytest.mark.skipif(os.name != "nt", reason="Windows native boundary")
def test_profile_denies_stable_sensitive_and_authority_paths(tmp_path: Path) -> None:
    env = {
        "USERPROFILE": str(tmp_path / "profile"),
        "APPDATA": str(tmp_path / "profile" / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(tmp_path / "profile" / "AppData" / "Local"),
    }
    override = windows.native_profile_override(tmp_path, env)
    assert 'extends=":workspace"' in override
    assert "network={enabled=false,allow_local_binding=false}" in override
    for suffix in (
        "/profile/.ssh", "/profile/.aws", "/profile/.codex/auth.json",
        "/AppData/Roaming/com.mewcode.desktop", "/.mewcode/rules",
        "/.mewcode/permissions.grants.json",
    ):
        assert suffix in override


@pytest.mark.skipif(os.name != "nt", reason="Windows native boundary")
def test_native_argv_forces_elevated_profile_and_no_path_lookup(tmp_path: Path, monkeypatch) -> None:
    cli = tmp_path / "codex.exe"
    cli.write_bytes(b"synthetic")
    monkeypatch.setattr(windows, "find_codex_sandbox_cli", lambda: cli)
    argv = windows.native_argv("python -m pytest -q", work_dir=str(tmp_path))
    assert argv[0] == str(cli)
    assert argv[1:5] == ["sandbox", "-P", "mewcode-p0", "-C"]
    assert 'windows.sandbox="elevated"' in argv
    assert argv[argv.index("--") + 1].lower().endswith(("powershell.exe", "pwsh.exe"))


@pytest.mark.asyncio
async def test_missing_native_backend_fails_without_host_fallback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MEWCODE_TEST_NATIVE_SANDBOX", "1")
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(
        windows, "native_argv",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            windows.NativeBoundaryUnavailable("synthetic unavailable")
        ),
    )
    with native_auto_command():
        result = await run_native_command(
            "pytest", work_dir=str(tmp_path), timeout=2, diagnostic_only=True,
        )
    assert result.is_error
    assert "AUTO_BOUNDARY_DENIED" in result.output
    assert "no host-shell fallback" in result.output
    spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_timeout_does_not_claim_unverified_descendants_stopped(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MEWCODE_TEST_NATIVE_SANDBOX", "1")
    monkeypatch.setattr(windows, "native_argv", lambda *_args, **_kwargs: ["synthetic-sandbox"])
    proc = Mock(pid=987654321, returncode=None)
    proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=proc),
    )
    cleanup = AsyncMock(return_value=False)
    monkeypatch.setattr("mewcode.tools.bash._terminate_process_tree", cleanup)
    with native_auto_command():
        result = await run_native_command(
            "Write-Output probe", work_dir=str(tmp_path), timeout=2, diagnostic_only=True,
        )
    assert result.is_error
    assert "descendant exit unconfirmed" in result.output
    assert "process tree stopped" not in result.output
    cleanup.assert_awaited_once_with(proc)


@pytest.mark.asyncio
async def test_bash_uses_native_scope_even_for_explicit_allow(tmp_path: Path, monkeypatch) -> None:
    native = AsyncMock(return_value=ToolResult("native-only"))
    monkeypatch.setattr("mewcode.tools.command_sandbox.run_native_command", native)
    with native_auto_command():
        assert native_auto_scope()
        result = await Bash().execute(Params(command="python -m pytest"))
    assert not native_auto_scope()
    assert result.output == "native-only"
    native.assert_awaited_once()


@pytest.mark.asyncio
async def test_verification_uses_native_scope(tmp_path: Path, monkeypatch) -> None:
    native = AsyncMock(return_value=NativeExecutionReceipt("exited", exit_code=0))
    monkeypatch.setattr("mewcode.tools.command_sandbox.execute_native_command", native)
    with native_auto_command():
        result = await RunVerification().execute(RunVerificationParams(command="python -m pytest"))
    assert not result.is_error
    assert '"isolation": "codex-windows-elevated"' in result.output
    native.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.name != "nt" or os.environ.get("MEWCODE_TEST_NATIVE_SANDBOX") != "1",
    reason="opt-in test requiring elevated Windows sandbox setup",
)
async def test_real_native_runner_reports_backend_without_host_fallback() -> None:
    root = Path(__file__).resolve().parents[1]
    marker = "P0_NATIVE_PROBE_" + uuid.uuid4().hex
    with native_auto_command():
        result = await execute_native_command(
            f"Write-Output {marker}", work_dir=str(root), timeout=20,
            diagnostic_only=True,
        )
    assert result.confirms_stdout_line(marker), result.render()


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.name != "nt" or os.environ.get("MEWCODE_TEST_NATIVE_SANDBOX") != "1",
    reason="opt-in test requiring elevated Windows sandbox setup",
)
async def test_real_native_runner_blocks_loopback_network() -> None:
    received: list[bytes] = []
    control = uuid.uuid4().hex.encode()
    child_nonce = uuid.uuid4().hex
    control_seen = asyncio.Event()
    handlers: set[asyncio.Task] = set()

    async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        handlers.add(task)
        try:
            payload = await asyncio.wait_for(reader.read(128), timeout=3)
            received.append(payload)
            if payload == control:
                control_seen.set()
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(task)

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        # Prove the host-side listener is reachable before testing the child.
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(control)
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        await asyncio.wait_for(control_seen.wait(), timeout=3)
        denied = "NETWORK_DENIED_" + child_nonce
        allowed = "NETWORK_ALLOWED_" + child_nonce
        command = (
            "try { $c=[Net.Sockets.TcpClient]::new(); "
            f"$t=$c.ConnectAsync('127.0.0.1',{port}); "
            "if ($t.Wait(2000) -and $c.Connected) { "
            f"$b=[Text.Encoding]::ASCII.GetBytes('{child_nonce}'); "
            "$c.GetStream().Write($b,0,$b.Length); "
            f"Write-Output {allowed} }} else {{ Write-Output {denied} }} "
            f"}} catch {{ Write-Output {denied} }} finally {{ if ($c) {{ $c.Dispose() }} }}"
        )
        root = Path(__file__).resolve().parents[1]
        with native_auto_command():
            result = await execute_native_command(
                command, work_dir=str(root), timeout=20, diagnostic_only=True,
            )
        if handlers:
            await asyncio.gather(*list(handlers))
        assert received == [control], "Sandbox reached host listener: " + repr(received)
        assert result.confirms_stdout_line(denied), result.render()
        assert allowed not in result.stdout.splitlines(), result.render()
    finally:
        server.close()
        await server.wait_closed()
        for task in list(handlers):
            task.cancel()
        if handlers:
            await asyncio.gather(*list(handlers), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.name != "nt" or os.environ.get("MEWCODE_TEST_NATIVE_SANDBOX") != "1",
    reason="opt-in test requiring elevated Windows sandbox setup",
)
async def test_real_native_runner_blocks_external_secret_name() -> None:
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="mewcode-p0-sensitive-") as folder:
        synthetic = Path(folder) / ".env"
        ordinary = Path(folder) / "ordinary.txt"
        synthetic.write_text("SYNTHETIC_ONLY=1\n", encoding="utf-8")
        ordinary.write_text("SYNTHETIC_ORDINARY=1\n", encoding="utf-8")
        escaped = str(synthetic).replace("'", "''")
        control_path = str(ordinary).replace("'", "''")
        nonce = uuid.uuid4().hex
        control_marker = "ORDINARY_READ_" + nonce
        denied = "SENSITIVE_DENIED_" + nonce
        allowed = "SENSITIVE_ALLOWED_" + nonce
        command = (
            "$ErrorActionPreference='Stop'; "
            f"$s=[IO.File]::OpenRead('{control_path}'); $s.Dispose(); "
            f"Write-Output {control_marker}; "
            f"try {{ $s=[IO.File]::OpenRead('{escaped}'); $s.Dispose(); Write-Output {allowed} }} "
            f"catch [System.UnauthorizedAccessException] {{ Write-Output {denied} }}"
        )
        # Both synthetic files start readable by the sandbox account. Otherwise
        # a private temp-directory ACL would falsely prove sensitive-name policy.
        # Only this new disposable directory is changed; never real credentials.
        try:
            grant = subprocess.run(
                ["icacls.exe", folder, "/grant", "*S-1-5-11:(OI)(CI)RX"],
                capture_output=True, timeout=10,
            )
            assert grant.returncode == 0, repr(grant.stderr)
            with native_auto_command():
                result = await execute_native_command(
                    command, work_dir=str(root), timeout=20, diagnostic_only=True,
                )
        finally:
            revoke = subprocess.run(
                ["icacls.exe", folder, "/remove:g", "*S-1-5-11"],
                capture_output=True, timeout=10,
            )
            assert revoke.returncode == 0, repr(revoke.stderr)
    assert result.confirms_stdout_line(control_marker), result.render()
    assert result.confirms_stdout_line(denied), result.render()
    assert allowed not in result.stdout.splitlines(), result.render()


@pytest.mark.parametrize("receipt", [
    NativeExecutionReceipt("spawn_failed", stderr="DENIED", detail="DENIED"),
    NativeExecutionReceipt("timed_out", stdout="DENIED", exit_code=0),
    NativeExecutionReceipt("exited", stdout="DENIED", exit_code=1),
    NativeExecutionReceipt("exited", stderr="DENIED", exit_code=0),
    NativeExecutionReceipt("exited", stdout="echo DENIED", exit_code=0),
    NativeExecutionReceipt("exited", stdout="DENIED\nDENIED", exit_code=0),
])
def test_probe_receipt_never_accepts_failure_or_echo(receipt) -> None:
    assert not receipt.confirms_stdout_line("DENIED")


def test_probe_receipt_accepts_only_successful_exact_stdout_line() -> None:
    assert NativeExecutionReceipt(
        "exited", stdout="DENIED\r\n", exit_code=0,
    ).confirms_stdout_line("DENIED")
