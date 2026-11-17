from __future__ import annotations

from copy import deepcopy
import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from mewcode.permissions import DangerousCommandDetector, PathSandbox, PermissionChecker, PermissionMode, RuleEngine
from mewcode.sandbox import lpac_runtime as lpac
from mewcode.sandbox.windows import NativeBoundaryUnavailable


def policy(workspace):
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(workspace)), RuleEngine(), PermissionMode.AUTO)
    checker.set_task_authorization(None, task_id="native-runtime-test")
    from mewcode.permissions.network_policy import NetworkPolicy
    checker.network_policy = NetworkPolicy(False)  # This suite tests the offline LPAC route.
    return checker


def runtime_paths(tmp_path):
    workspace = tmp_path / "work space 中文"
    workspace.mkdir()
    runtime = tmp_path / "host-runtime"
    for part in ("temp", "profile/AppData/Local", "profile/AppData/Roaming"):
        (runtime / part).mkdir(parents=True)
    return workspace, runtime


def test_exact_scope_omits_authority_and_preserves_declared_writes(tmp_path):
    from mewcode.experts.node_expansion import ExpansionPathSandbox
    workspace, runtime = runtime_paths(tmp_path)
    (workspace / "output").mkdir()
    (workspace / "input.txt").write_text("input")
    (workspace / ".mewcode").mkdir()
    (workspace / ".mewcode/permissions.yaml").write_text("policy")
    checker = policy(workspace)
    checker.sandbox = ExpansionPathSandbox(checker.sandbox, (str(workspace / "output"),))
    scope = lpac.NativeScope.from_checker(checker)
    request = lpac.build_runtime_request("Write-Output test", scope, runtime, timeout=4, cancel_name="host")
    assert str(workspace / "input.txt") in request["read_paths"]
    assert str(workspace / "output") in request["write_paths"]
    assert str(workspace) not in request["write_paths"]
    assert str(workspace / ".mewcode/permissions.yaml") not in request["read_paths"]
    assert not any("API_KEY" in key for key in request["environment"])
    checker.task_id = "changed"
    with pytest.raises(NativeBoundaryUnavailable, match="scope changed"):
        lpac.build_runtime_request("test", scope, runtime, timeout=4, cancel_name="host")


def test_workspace_parent_is_not_an_exact_runtime_grant(tmp_path, monkeypatch):
    workspace, runtime = runtime_paths(tmp_path)
    parent = tmp_path.resolve()
    monkeypatch.setenv("USERPROFILE", str(parent))
    request = lpac.build_runtime_request("Write-Output test", lpac.NativeScope.from_checker(policy(workspace)),
        runtime, timeout=4, cancel_name="host")
    read = {Path(path).resolve() for path in request["read_paths"]}
    write = {Path(path).resolve() for path in request["write_paths"]}
    assert parent not in read and parent not in write
    assert workspace.resolve() in read and runtime.joinpath("temp").resolve() in read
    assert request["environment"]["USERPROFILE"] == str(runtime / "profile")


def test_does_not_inherit_credentials_or_grant_host_state(tmp_path, monkeypatch):
    workspace, runtime = runtime_paths(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic")
    monkeypatch.setenv("HTTP_PROXY", "synthetic")
    request = lpac.build_runtime_request("Get-ChildItem", lpac.NativeScope.from_checker(policy(workspace)),
        runtime, timeout=3, cancel_name="host")
    assert "OPENAI_API_KEY" not in request["environment"]
    assert "HTTP_PROXY" not in request["environment"]
    assert "GIT_CEILING_DIRECTORIES" not in request["environment"]
    assert "GIT_EXEC_PATH" not in request["environment"]
    assert str(runtime) not in request["read_paths"]
    assert str(runtime) not in request["write_paths"]


def test_bundled_git_discovery_stops_at_workspace_boundary_and_keeps_child_repos(tmp_path):
    git_executable = shutil.which("git")
    if not git_executable:
        pytest.skip("Git is required to verify repository discovery semantics")
    workspace, runtime = runtime_paths(tmp_path)
    git_dir = tmp_path / "bundled-git"
    git_dir.mkdir()
    (git_dir / "git.exe").write_bytes(b"synthetic-git")
    request = lpac.build_runtime_request(
        "git rev-parse --show-toplevel", lpac.NativeScope.from_checker(policy(workspace)),
        runtime, timeout=4, cancel_name="host", git_dir=git_dir)
    environment = request["environment"]
    assert "GIT_CEILING_DIRECTORIES" not in environment
    assert "GIT_EXEC_PATH" not in environment
    ceiling = workspace.resolve().parent.as_posix()
    command_text = request["arguments"][-1]
    assert f"$env:GIT_CEILING_DIRECTORIES=';{ceiling}';" in command_text
    assert f"$env:GIT_EXEC_PATH='{git_dir}';" in command_text
    git_environment = {**environment, "GIT_CEILING_DIRECTORIES": ";" + ceiling}

    def git_at(directory, *args):
        return subprocess.run([git_executable, "-C", str(directory), *args], env=git_environment,
                              capture_output=True, text=True, timeout=20)

    # A staging ancestor may itself be a repository. A non-repo workspace must
    # stop at its frozen boundary instead of discovering that parent.
    parent_repo = git_at(tmp_path, "init", "--quiet")
    assert parent_repo.returncode == 0, parent_repo.stderr
    outside_discovery = git_at(workspace, "rev-parse", "--show-toplevel")
    assert outside_discovery.returncode != 0

    root_repo = git_at(workspace, "init", "--quiet")
    assert root_repo.returncode == 0, root_repo.stderr
    child = workspace / "child"
    child.mkdir()
    from_child = git_at(child, "rev-parse", "--show-toplevel")
    assert from_child.returncode == 0, from_child.stderr
    assert Path(from_child.stdout.strip()).resolve() == workspace.resolve()

    nested_repo = child / "nested-repo"
    nested_repo.mkdir()
    initialized_nested = git_at(nested_repo, "init", "--quiet")
    assert initialized_nested.returncode == 0, initialized_nested.stderr
    nested_child = nested_repo / "src"
    nested_child.mkdir()
    from_nested_child = git_at(nested_child, "rev-parse", "--show-toplevel")
    assert from_nested_child.returncode == 0, from_nested_child.stderr
    assert Path(from_nested_child.stdout.strip()).resolve() == nested_repo.resolve()


def test_bundled_git_uses_private_openssl_trust_with_utf8_paths(tmp_path):
    workspace, runtime = runtime_paths(tmp_path)
    git_dir = tmp_path / "bundled-中文 git"
    git_dir.mkdir()
    for name in ("git.exe", "libcurl-openssl-4.dll", "ca-bundle.crt"):
        (git_dir / name).write_bytes(b"synthetic")
    request = lpac.build_runtime_request(
        "git ls-remote https://example.invalid/repo", lpac.NativeScope.from_checker(policy(workspace)),
        runtime, timeout=4, cancel_name="host", git_dir=git_dir)
    config = runtime / "profile" / ".gitconfig"
    text = config.read_text(encoding="utf-8")
    assert "sslBackend = openssl" in text and "sslVerify = true" in text
    assert "sslVerify = false" not in text
    assert (git_dir / "ca-bundle.crt").as_posix() in text
    assert str(git_dir / "ca-bundle.crt") in request["read_paths"]
    assert str(git_dir / "ca-bundle.crt") not in request["write_paths"]
    git_executable = shutil.which("git")
    if git_executable:
        parsed = subprocess.run([git_executable, "config", "--file", str(config), "http.sslCAInfo"],
                                capture_output=True, encoding="utf-8", timeout=20)
        assert parsed.returncode == 0, parsed.stderr
        assert parsed.stdout.strip() == (git_dir / "ca-bundle.crt").as_posix()


def test_python_alias_is_bound_to_an_exact_workspace_leaf(tmp_path):
    workspace, runtime = runtime_paths(tmp_path)
    interpreter = workspace / ".venv/Scripts/python.exe"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"synthetic")
    scope = lpac.NativeScope.from_checker(policy(workspace))
    request = lpac.build_runtime_request("python --version", scope, runtime, timeout=3, cancel_name="host",
                                        python_executable=interpreter)
    assert "Set-Alias -Name python -Value '" + str(interpreter) + "'" in request["arguments"][-1]
    assert str(interpreter) in request["read_paths"]
    outside = tmp_path / "outside.exe"
    outside.write_bytes(b"synthetic")
    with pytest.raises(NativeBoundaryUnavailable, match="frozen readable workspace interpreter"):
        lpac.build_runtime_request("python --version", scope, runtime, timeout=3, cancel_name="host",
                                  python_executable=outside)


def test_hard_link_cannot_enter_request(tmp_path):
    workspace, runtime = runtime_paths(tmp_path)
    target = tmp_path / "outside"
    target.write_text("synthetic")
    (workspace / "linked").hardlink_to(target)
    with pytest.raises(NativeBoundaryUnavailable, match="hard-linked"):
        lpac.build_runtime_request("test", lpac.NativeScope.from_checker(policy(workspace)),
            runtime, timeout=3, cancel_name="host")


def envelope():
    return {"schema": "mewcode-lpac-receipt-v1", "request_id": "id", "backend": lpac.BACKEND,
        "receipt": {"job_cleanup_ok": True, "acl_cleanup_ok": True, "profile_cleanup_ok": True,
            "appcontainer_verified": True, "lpac_verified": True, "lpac_verification_method": "access_check",
            "lpac_access_mask": 2, "capabilities": lpac.CAPABILITIES, "error": "", "exit_code": 0,
            "stdout": "true output", "stderr": "", "timed_out": False, "cancelled": False}}


def test_valid_receipt_and_nonzero_child_exit():
    data = envelope()
    result = lpac.decode_receipt(json.dumps(data).encode(), "id", 0)
    assert not result.is_error and result.cleanup_confirmed is True
    data["receipt"]["exit_code"] = 8
    result = lpac.decode_receipt(json.dumps(data).encode(), "id", 8)
    assert result.status == "exited" and result.is_error and result.exit_code == 8


@pytest.mark.parametrize("key,value", [
    ("job_cleanup_ok", False), ("profile_cleanup_ok", None), ("acl_cleanup_ok", "true"),
    ("appcontainer_verified", False), ("lpac_access_mask", 3), ("lpac_verification_method", "unknown"),
    ("capabilities", ["internetClient"]), ("stdout", {}), ("error", "failure"), ("exit_code", False),
])
def test_untrusted_or_incomplete_receipts_never_pass(key, value):
    data = envelope()
    data["receipt"][key] = value
    assert lpac.decode_receipt(json.dumps(data).encode(), "id", 0).is_error


@pytest.mark.parametrize("value", [b"{}", b"null", b"[]", b"not json"])
def test_bad_envelope_fails(value):
    assert lpac.decode_receipt(value, "id", 0).is_error


def test_child_cannot_forge_outer_receipt_or_reuse_identity():
    data = envelope()
    data["receipt"]["stdout"] = json.dumps(envelope())
    assert lpac.decode_receipt(json.dumps(data).encode(), "different-id", 0).is_error


def test_timeout_cleanup_and_output_truncation_are_explicit():
    data = envelope()
    data["receipt"]["timed_out"] = True
    result = lpac.decode_receipt(json.dumps(data).encode(), "id", 1)
    assert result.status == "timed_out" and result.cleanup_confirmed
    data = envelope()
    data["receipt"]["stdout"] = "marker\n" + "x" * 50000
    result = lpac.decode_receipt(json.dumps(data).encode(), "id", 0)
    assert result.output_truncated and not result.confirms_stdout_line("marker")


@pytest.mark.asyncio
async def test_closed_gate_does_not_spawn_or_inspect_scope(monkeypatch):
    spawn = AsyncMock()
    monkeypatch.setattr(lpac.asyncio, "create_subprocess_exec", spawn)
    result = await lpac.execute_lpac("test", scope=None, work_dir="missing", timeout=1)
    assert result.status == "denied"
    spawn.assert_not_called()


@pytest.mark.asyncio
async def test_production_route_never_selects_failed_codex_candidate(tmp_path, monkeypatch):
    from mewcode.sandbox import windows
    from mewcode.tools.command_sandbox import native_auto_command, execute_native_command
    from mewcode.sandbox.receipt import NativeExecutionReceipt
    monkeypatch.setattr(windows, "native_runtime_ready", lambda: True)
    monkeypatch.setattr(windows, "native_argv", lambda *_a, **_k: pytest.fail("production used old candidate"))
    execute = AsyncMock(return_value=NativeExecutionReceipt("exited", exit_code=0, backend=lpac.BACKEND))
    monkeypatch.setattr(lpac, "execute_lpac", execute)
    with native_auto_command(policy(tmp_path)):
        result = await execute_native_command("test", work_dir=str(tmp_path), timeout=1)
    assert result.backend == lpac.BACKEND
    assert execute.call_args.kwargs["scope"].workspace == tmp_path.resolve()


@pytest.mark.asyncio
async def test_missing_host_scope_never_launches_even_with_gate_mocked(tmp_path, monkeypatch):
    from mewcode.sandbox import windows
    from mewcode.tools.command_sandbox import native_auto_command, execute_native_command
    monkeypatch.setattr(windows, "native_runtime_ready", lambda: True)
    execute = AsyncMock()
    monkeypatch.setattr(lpac, "execute_lpac", execute)
    with native_auto_command():
        result = await execute_native_command("test", work_dir=str(tmp_path), timeout=1)
    assert result.is_error
    execute.assert_not_called()


import asyncio


class _FakeHelperProcess:
    def __init__(self, stdout=b"", *, transport_error=None):
        self.stdout = stdout
        self.transport_error = transport_error
        self.returncode = None
        self.communicate_started = asyncio.Event()
        self.release_communicate = asyncio.Event()
        self.communicate_cancelled = asyncio.Event()
        self.wait_called = asyncio.Event()
        self.killed = False
        self.input_bytes = None

    async def communicate(self, input_bytes):
        self.input_bytes = input_bytes
        self.communicate_started.set()
        if self.transport_error is not None:
            await asyncio.sleep(0)
            raise self.transport_error
        try:
            await self.release_communicate.wait()
        except asyncio.CancelledError:
            self.communicate_cancelled.set()
            raise
        self.returncode = 0
        return self.stdout, b""

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        if self.returncode is None:
            self.returncode = -9
        self.wait_called.set()
        return self.returncode


class _FakeCancelEvent:
    def __init__(self):
        self.signalled = asyncio.Event()
        self.signal_count = 0

    def signal(self):
        self.signal_count += 1
        self.signalled.set()


def _fake_helper_request(*, timeout_ms=1000):
    return {"request_id": "id", "timeout_ms": timeout_ms}


def _install_fake_helper(monkeypatch, process):
    async def create_process(*_args, **_kwargs):
        return process
    monkeypatch.setattr(lpac.asyncio, "create_subprocess_exec", create_process)


@pytest.mark.asyncio
async def test_cancel_signals_first_and_waits_for_clean_host_receipt(monkeypatch):
    process = _FakeHelperProcess(json.dumps(envelope()).encode())
    _install_fake_helper(monkeypatch, process)
    cancel = _FakeCancelEvent()
    receipts = []
    task = asyncio.create_task(lpac.invoke_helper(Path("fake-helper.exe"), _fake_helper_request(),
                                                    cancel, on_receipt=receipts.append))
    await process.communicate_started.wait()
    task.cancel()
    await cancel.signalled.wait()
    assert cancel.signal_count == 1
    assert not task.done(), "cancellation must wait for the helper cleanup receipt"
    process.release_communicate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(receipts) == 1 and receipts[0].cleanup_confirmed is True
    assert process.returncode == 0 and not process.killed and not process.wait_called.is_set()


@pytest.mark.asyncio
async def test_cancel_with_incomplete_cleanup_never_confirms_handoff(monkeypatch):
    data = envelope()
    data["receipt"]["cancelled"] = True
    data["receipt"]["profile_cleanup_ok"] = False
    process = _FakeHelperProcess(json.dumps(data).encode())
    _install_fake_helper(monkeypatch, process)
    cancel = _FakeCancelEvent()
    receipts = []
    task = asyncio.create_task(lpac.invoke_helper(Path("fake-helper.exe"), _fake_helper_request(),
                                                    cancel, on_receipt=receipts.append))
    await process.communicate_started.wait()
    task.cancel()
    await cancel.signalled.wait()
    process.release_communicate.set()
    with pytest.raises(RuntimeError, match="cleanup unconfirmed"):
        await task
    assert len(receipts) == 1
    assert receipts[0].cleanup_confirmed is False
    assert receipts[0].status == "spawn_failed"


@pytest.mark.parametrize("interrupt", ["second_cancel", "helper_deadline"])
@pytest.mark.asyncio
async def test_unconfirmed_second_cancel_or_helper_deadline_kills_only_owned_helper(
        interrupt, monkeypatch):
    process = _FakeHelperProcess()
    _install_fake_helper(monkeypatch, process)
    cancel = _FakeCancelEvent()
    original_wait_for = asyncio.wait_for
    second_wait_started = asyncio.Event()
    calls = 0

    async def observed_wait_for(awaitable, timeout):
        nonlocal calls
        calls += 1
        if calls == 2:
            second_wait_started.set()
        if interrupt == "helper_deadline":
            # Deterministically expire both host waits without waiting 25 s.
            if calls == 1:
                await asyncio.sleep(0)  # Let the owned communicate task start first.
            awaitable.cancel()
            raise asyncio.TimeoutError
        return await original_wait_for(awaitable, timeout)

    monkeypatch.setattr(lpac.asyncio, "wait_for", observed_wait_for)
    task = asyncio.create_task(lpac.invoke_helper(Path("fake-helper.exe"), _fake_helper_request(), cancel))
    await process.communicate_started.wait()
    if interrupt == "second_cancel":
        task.cancel()
        await cancel.signalled.wait()
        await second_wait_started.wait()
        task.cancel()
    with pytest.raises(RuntimeError, match="cleanup unconfirmed"):
        await task
    assert cancel.signal_count == 1
    assert process.killed and process.wait_called.is_set()
    assert process.communicate_cancelled.is_set()


@pytest.mark.asyncio
async def test_transport_exception_after_spawn_reaps_owned_helper(monkeypatch):
    process = _FakeHelperProcess(transport_error=OSError("synthetic pipe failure"))
    _install_fake_helper(monkeypatch, process)
    cancel = _FakeCancelEvent()
    with pytest.raises(OSError, match="synthetic pipe failure"):
        await lpac.invoke_helper(Path("fake-helper.exe"), _fake_helper_request(), cancel)
    assert process.communicate_started.is_set()
    assert process.killed and process.wait_called.is_set()


@pytest.mark.asyncio
async def test_invoke_helper_rejects_bad_json_and_wrong_request_identity(monkeypatch):
    for raw in (b"not json", json.dumps({**envelope(), "request_id": "wrong"}).encode()):
        process = _FakeHelperProcess(raw)
        process.release_communicate.set()
        _install_fake_helper(monkeypatch, process)
        cancel = _FakeCancelEvent()
        result = await lpac.invoke_helper(Path("fake-helper.exe"), _fake_helper_request(), cancel)
        assert result.is_error and result.cleanup_confirmed is not True
        assert not process.killed
