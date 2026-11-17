"""Strict Docker boundary and explicitly selected native-command adapter.

The reviewer decides whether the action is justified. Only the host decides
what filesystem and network resources the action can actually reach. Neither
isolated backend may fall back to a host shell when it is unavailable.
Ordinary AUTO host execution is selected by policy before review and dispatch.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING

from mewcode.tools.base import ToolResult
from mewcode.sandbox.receipt import NativeExecutionReceipt, NativeCleanupUnconfirmed

if TYPE_CHECKING:
    from mewcode.permissions.task_authorization import TaskAuthorization

_active_scope: ContextVar[TaskAuthorization | None | bool] = ContextVar(
    "mewcode_command_sandbox_scope", default=False,
)
_native_auto_scope: ContextVar[bool] = ContextVar(
    "mewcode_native_auto_command_scope", default=False,
)
_native_host_policy: ContextVar[object | None] = ContextVar("mewcode_native_host_policy", default=None)
_native_receipt_observer: ContextVar[object | None] = ContextVar("mewcode_native_receipt_observer", default=None)
_native_publication_override: ContextVar[str | None] = ContextVar("native_publication_override", default=None)
_offline_disposable: ContextVar[bool] = ContextVar("offline_disposable", default=False)


@contextmanager
def disposable_native_command(*, offline: bool = False):
    """Narrow an already-authorized command to a copy that cannot publish."""
    token = _native_publication_override.set("discard")
    offline_token = _offline_disposable.set(offline)
    try:
        yield
    finally:
        _native_publication_override.reset(token)
        _offline_disposable.reset(offline_token)


def native_receipt_observation(receipt):
    from mewcode.experts.command_receipts import execution_observation
    from mewcode.sandbox.execution_conditions import classify_execution_condition
    observation = execution_observation(receipt.status,
        stdout=receipt.stdout if receipt.execution_started is True else None,
        stderr=receipt.stderr if receipt.execution_started is True else None,
        exit_code=receipt.exit_code, reason=receipt.detail, output_truncated=receipt.output_truncated,
        command_started=receipt.execution_started)
    observation.update(error_code=receipt.error_code, cleanup_confirmed=receipt.cleanup_confirmed,
                       transaction_id=receipt.transaction_id, publication_state=receipt.publication_state,
                       successful=not receipt.is_error)
    observation['network_policy'] = receipt.network_policy
    observation["execution_condition"] = classify_execution_condition(receipt)
    result = {"command_execution": observation}
    if receipt.network_policy is not None:
        result['network_policy'] = receipt.network_policy
    if receipt.error_code in {'network_runtime_unavailable', 'network_fence_unavailable',
                              'network_disabled', 'network_blacklist', 'network_policy_changed'}:
        result['automatic_recovery'] = False
    return result


@contextmanager
def reviewed_auto_command(scope: TaskAuthorization | None):
    token = _active_scope.set(scope)
    try:
        yield
    finally:
        _active_scope.reset(token)


def sandbox_scope() -> TaskAuthorization | None | bool:
    return _active_scope.get()


@contextmanager
def native_auto_command(checker=None, *, owner=None):
    """Bind an AUTO shell call to the native OS boundary, not a text verdict."""
    token = _native_auto_scope.set(True)
    policy_token = _native_host_policy.set(checker)
    def observe(receipt):
        if owner is not None and receipt.cleanup_confirmed is False:
            owner._native_cleanup_error = receipt.detail or "Native cleanup unconfirmed"
            from mewcode.sandbox.transaction_registry import CURRENT_TRANSACTION
            identity = receipt.transaction_id or CURRENT_TRANSACTION.get()
            if identity:
                identities = set(getattr(owner, "_native_cleanup_transaction_ids", ()))
                identities.add(identity)
                owner._native_cleanup_transaction_ids = identities
            else:
                owner._native_cleanup_unbound = True
    observer_token = _native_receipt_observer.set(observe)
    try:
        if getattr(owner, "_native_cleanup_error", ""):
            from mewcode.sandbox.native_recovery import restore_cleanup_owner
            restore_cleanup_owner(owner, checker)
        if getattr(owner, "_native_cleanup_error", ""):
            raise NativeCleanupUnconfirmed(owner._native_cleanup_error)
        yield
    finally:
        _native_receipt_observer.reset(observer_token)
        _native_host_policy.reset(policy_token)
        _native_auto_scope.reset(token)
        if getattr(owner, "_native_cleanup_error", ""):
            from mewcode.sandbox.native_recovery import cleanup_blocks_project
            if cleanup_blocks_project(owner, checker):
                raise NativeCleanupUnconfirmed(owner._native_cleanup_error)


def native_auto_scope() -> bool:
    return _native_auto_scope.get()


async def run_native_command(
    command: str, *, work_dir: str | None, timeout: int, diagnostic_only: bool = False,
) -> ToolResult:
    receipt = await execute_native_command(
        command, work_dir=work_dir, timeout=timeout, diagnostic_only=diagnostic_only,
    )
    observation = native_receipt_observation(receipt)
    hint = network_socket_hint(receipt)
    if hint:
        observation['network_diagnostic'] = {
            'kind': 'check_client_proxy_configuration', 'reason': hint,
            'automatic_retry': False,
        }
    return ToolResult(receipt.render() + ('\nNETWORK_DIAGNOSTIC: ' + hint if hint else ''),
                      is_error=receipt.is_error, host_observation=observation)


def network_socket_hint(receipt: NativeExecutionReceipt) -> str:
    """Explain a socket denial without claiming which connection was attempted."""
    policy = receipt.network_policy or {}
    if (not receipt.is_error or receipt.execution_started is not True
            or policy.get('enabled') is not True
            or (policy.get('preflight') or {}).get('ready') is not True
            or 'WinError 10013' not in receipt.stdout + receipt.stderr):
        return ''
    return ('宿主联网代理预检已通过；此沙箱允许经代理联网，系统阻止直连。'
            '请先核对客户端是否禁用了代理：urllib 的 ProxyHandler({})、requests 的 trust_env=False、'
            '清空代理环境变量或设置 NO_PROXY 都可能造成此错误。'
            '保留宿主代理；urllib 使用默认 opener 或 ProxyHandler()，可同时保留 TLS 校验和禁止重定向处理器。'
            '仅凭此错误不能断定外部请求完全未发送；不自动重试上传，不关闭隔离或绕过黑名单。')


async def execute_native_command(
    command: str, *, work_dir: str | None, timeout: int, diagnostic_only: bool = False,
) -> NativeExecutionReceipt:
    """Use the gated LPAC host adapter; old elevated candidate is diagnostic-only."""
    if not native_auto_scope():
        raise RuntimeError("run_native_command used outside AUTO boundary context")
    if diagnostic_only and os.environ.get("MEWCODE_TEST_NATIVE_SANDBOX") != "1":
        return NativeExecutionReceipt(
            "denied", detail="diagnostic native probe is disabled outside explicit test mode", command_started=False,
        )
    from mewcode.permissions.network_policy import policy_for
    checker = _native_host_policy.get()
    if not diagnostic_only and not _offline_disposable.get() and policy_for(checker).enabled:
        from mewcode.sandbox.srt_runtime import execute_srt
        if not work_dir:
            return NativeExecutionReceipt('denied', command_started=False, detail='Command workspace is missing')
        return await execute_srt(command, checker=checker, work_dir=work_dir, timeout=timeout,
            publication_mode=_native_publication_override.get(), on_receipt=_native_receipt_observer.get())
    from mewcode.sandbox.windows import native_runtime_ready
    if not diagnostic_only and not native_runtime_ready():
        from mewcode.sandbox.native_recovery import recover_native_runtime
        recover_native_runtime()
    if not diagnostic_only and not native_runtime_ready():
        from mewcode.sandbox.native_gate import native_gate_status
        gate = native_gate_status()
        return NativeExecutionReceipt(
            "denied", detail=f"P0 native boundary is not enabled "
            f"({gate.state.value}: {gate.reason}). No host-shell fallback.",
            command_started=False, error_code="native_gate_" + gate.state.value,
        )
    if not work_dir:
        return NativeExecutionReceipt("denied", detail="command workspace is missing", command_started=False,
                                      error_code="native_workspace_missing")
    if not diagnostic_only:
        from mewcode.sandbox.lpac_runtime import NativeScope, execute_lpac, BACKEND
        from mewcode.sandbox.windows import NativeBoundaryUnavailable
        try:
            checker = _native_host_policy.get()
            publication_mode = _native_publication_override.get()
            if publication_mode is None and getattr(checker, "native_publication_mode", "publish") != "discard":
                from mewcode.permissions.dangerous import is_safe_command, command_requires_auto_review
                if is_safe_command(command) and not command_requires_auto_review(command, Path(work_dir))[0]:
                    publication_mode = "discard"
            scope = NativeScope.from_checker(checker, publication_mode=publication_mode,
                independent_offline=_offline_disposable.get())
        except (NativeBoundaryUnavailable, OSError, ValueError, AttributeError) as exc:
            return NativeExecutionReceipt("denied", detail=str(exc), backend=BACKEND, command_started=False,
                                          error_code="native_scope_invalid")
        return await execute_lpac(command, scope=scope, work_dir=work_dir, timeout=timeout,
                                  on_receipt=_native_receipt_observer.get())
    # The failed Codex candidate is retained for explicit historical diagnostics
    # only; a future activation cannot accidentally route production here.
    from mewcode.sandbox.windows import NativeBoundaryUnavailable, native_argv
    from mewcode.tools.bash import _child_env, _terminate_process_tree
    try:
        argv = native_argv(command, work_dir=work_dir)
    except (NativeBoundaryUnavailable, OSError, RuntimeError, ValueError) as exc:
        return NativeExecutionReceipt("denied", detail=f"{exc}; no host-shell fallback", command_started=False)
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=work_dir, env=_child_env(work_dir),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=max(1, timeout))
    except asyncio.TimeoutError:
        tree_stopped = False
        if proc is not None:
            tree_stopped = await _terminate_process_tree(proc)
        cleanup = (
            "tree-kill command succeeded; detached descendants unverified"
            if tree_stopped else "descendant exit unconfirmed"
        )
        return NativeExecutionReceipt(
            "timed_out", detail=f"native command timed out after {timeout}s; {cleanup}",
            diagnostic_only=diagnostic_only,
        )
    except asyncio.CancelledError:
        if proc is not None:
            await _terminate_process_tree(proc)
        raise
    except OSError as exc:
        return NativeExecutionReceipt(
            "spawn_failed", detail=f"native sandbox failed to start: {exc}",
            diagnostic_only=diagnostic_only,
        )
    assert proc is not None
    out = (stdout or b"").decode(errors="replace")[:40000]
    err = (stderr or b"").decode(errors="replace")[:10000]
    return NativeExecutionReceipt(
        "exited", stdout=out, stderr=err, exit_code=proc.returncode,
        diagnostic_only=diagnostic_only,
    )


def _mount_arguments(work_dir: str, scope: TaskAuthorization) -> list[str]:
    root = Path(work_dir).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("工作目录不存在")
    mounts: dict[str, tuple[Path, bool]] = {}
    for writable, paths in ((False, scope.read_directories), (True, scope.write_directories)):
        for raw in paths:
            source = Path(raw).resolve(strict=True)
            if not source.is_dir() or not source.is_relative_to(root):
                raise ValueError("隔离命令只可挂载当前工作目录内的已授权目录")
            relative = source.relative_to(root)
            target = "/workspace" + ("/" + relative.as_posix() if relative.parts else "")
            if "," in str(source) or "," in target:
                raise ValueError("隔离目录路径含容器挂载参数分隔符")
            prior = mounts.get(target)
            mounts[target] = (source, writable or bool(prior and prior[1]))
    if not mounts:
        raise ValueError("缺少任务级可读或可写目录；请在任务授权界面明确授权后再运行脚本")
    args: list[str] = []
    if "/workspace" not in mounts:
        args += ["--tmpfs", "/workspace:rw,nosuid,size=16m"]
    for target, (source, writable) in sorted(mounts.items(), key=lambda item: item[0].count("/")):
        mount = f"type=bind,source={source},target={target}"
        args += ["--mount", mount if writable else mount + ",readonly"]
    return args


async def run_isolated_command(command: str, *, work_dir: str | None, timeout: int) -> ToolResult:
    scope = sandbox_scope()
    if scope is False:
        raise RuntimeError("run_isolated_command used outside strict-isolation context")
    if scope is None or time.time() >= scope.expires_at:
        return ToolResult("AUTO_DENIED: 未授权隔离命令的任务工作目录；请先明确任务级读写范围。", is_error=True)
    if "isolated_command" not in scope.allowed_behaviors:
        return ToolResult("AUTO_DENIED: 当前任务未授权隔离命令行为。", is_error=True)
    if not work_dir:
        return ToolResult("AUTO_DENIED: 隔离命令缺少工作目录。", is_error=True)
    docker = shutil.which("docker")
    if not docker:
        return ToolResult("AUTO_DENIED: Docker 隔离运行时不可用；没有降级为宿主 Shell。", is_error=True)
    try:
        mounts = _mount_arguments(work_dir, scope)
    except (OSError, ValueError) as exc:
        return ToolResult(f"AUTO_DENIED: 隔离挂载范围无效：{exc}", is_error=True)
    image = os.environ.get("MEWCODE_SANDBOX_IMAGE", "python:3.12-slim").strip()
    if not image or any(char.isspace() for char in image):
        return ToolResult("AUTO_DENIED: 隔离镜像配置无效。", is_error=True)
    name = "mewcode-sandbox-" + uuid.uuid4().hex[:16]
    argv = [
        docker, "run", "--rm", "--pull", "never", "--name", name,
        "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--pids-limit", "64",
        "--memory", "512m", "--cpus", "1", "--user", "65534:65534",
        "--tmpfs", "/tmp:rw,nosuid,size=64m", "--workdir", "/workspace",
        "--env", "HOME=/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1",
        *mounts, "--entrypoint", "/bin/sh", image, "-lc", command,
    ]
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=max(1, timeout))
    except asyncio.TimeoutError:
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
        # Killing the CLI does not guarantee the container stopped. Remove
        # only the unique name this invocation created, never a broad set.
        await _remove_container(docker, name)
        return ToolResult(f"AUTO_DENIED: 隔离命令超过 {timeout}s，容器已清理。", is_error=True)
    except asyncio.CancelledError:
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
        await _remove_container(docker, name)
        raise
    except OSError as exc:
        return ToolResult(f"AUTO_DENIED: 隔离运行时无法启动：{exc}", is_error=True)
    assert proc is not None
    output = (stdout or b"").decode(errors="replace")
    error = (stderr or b"").decode(errors="replace")
    rendered = f"ISOLATION: docker; network=none; scope={scope.version}; image={image}\n"
    rendered += f"STDOUT:\n{output[:40000]}\nSTDERR:\n{error[:10000]}\nEXIT CODE: {proc.returncode}"
    from mewcode.experts.command_receipts import execution_observation
    return ToolResult(rendered, is_error=proc.returncode != 0,
        host_observation={"command_execution": execution_observation("exited",
            stdout=output, stderr=error, exit_code=proc.returncode)})


async def _remove_container(docker: str, name: str) -> None:
    try:
        cleanup = await asyncio.create_subprocess_exec(
            docker, "rm", "-f", name, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        await asyncio.wait_for(cleanup.wait(), timeout=5)
    except (OSError, asyncio.TimeoutError):
        pass
