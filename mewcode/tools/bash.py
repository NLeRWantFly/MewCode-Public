from __future__ import annotations

import asyncio
import logging
import os
import signal
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from mewcode.tools.base import Tool, ToolResult, active_work_dir

MAX_TIMEOUT = 600
PROCESS_CLEANUP_TIMEOUT = 5
log = logging.getLogger(__name__)


async def _terminate_process_tree(proc: asyncio.subprocess.Process) -> bool:
    """Attempt bounded tree cleanup; return whether the tree-kill command succeeded.

    Success is not an independent liveness proof for detached descendants.
    """
    if proc.returncode is not None:
        # A parent that exited may still have live descendants. Without a job
        # handle or process-group leader we cannot attest their termination.
        return False
    tree_stopped = False
    try:
        if os.name == "nt":
            result = await asyncio.to_thread(
                subprocess.run, ["taskkill.exe", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True, timeout=PROCESS_CLEANUP_TIMEOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            tree_stopped = result.returncode == 0
            if not tree_stopped:
                detail = (result.stderr or b"").decode(errors="replace").strip()[:240]
                log.warning(
                    "Command tree cleanup was not confirmed for PID %s: taskkill rc=%s; %s",
                    proc.pid, result.returncode, detail,
                )
        else:
            # Every command is started in its own session; never signal the
            # application's process group or unrelated terminal processes.
            os.killpg(proc.pid, signal.SIGKILL)
            tree_stopped = True
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("Command tree cleanup failed for PID %s: %s", proc.pid, exc)
    if proc.returncode is None:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=PROCESS_CLEANUP_TIMEOUT)
    except asyncio.TimeoutError:
        log.warning("Command cleanup wait expired for PID %s; descendant exit is unconfirmed", proc.pid)
        return False
    return tree_stopped


class Params(BaseModel):
    command: str = Field(description="Shell command to execute")
    timeout: int = Field(default=120, description="Timeout in seconds (max 600)")
    publication: Literal["publish", "discard"] = Field(default="publish", description=(
        "Default publish runs in the current workspace in Auto after permission checks. "
        "Use discard only when a disposable isolated copy is required; this needs the native runtime "
        "and never publishes files. The host may force discard for read-only roles."))
    network: Literal["inherit", "offline", "proxy"] = Field(default="inherit", description=(
        "Use offline with publication=discard for independent local tests in a fresh disposable "
        "native copy without network access. For an approved ordinary host command that must use a "
        "recorded host proxy, use proxy with proxy_targets. This does not claim OS network isolation, "
        "copy the project, or settle an older isolated command."))
    proxy_targets: list[str] = Field(default_factory=list, max_length=16, description=(
        "Exact HTTPS origins for network=proxy, e.g. ['https://open.bigmodel.cn']. "
        "No paths, credentials, wildcards or implicit redirects to other hosts. "
        "This narrows the proxy route and does not grant upload authorization."))

    @model_validator(mode="after")
    def offline_copy(self):
        if self.network == "offline" and self.publication != "discard":
            raise ValueError("Offline execution requires publication=discard")
        if self.network == "proxy":
            from mewcode.tools.command_proxy import proxy_origins
            proxy_origins(self.proxy_targets)
            if self.publication != "publish":
                raise ValueError("Recorded host proxy execution requires publication=publish")
        elif self.proxy_targets:
            raise ValueError("proxy_targets requires network=proxy")
        return self


class Bash(Tool):
    name = "Bash"
    description = (
        "Execute a shell command in the current project and return stdout, "
        "stderr, and its exit code. Uses PowerShell on Windows and Bash elsewhere. "
        "In the Windows network sandbox, preserve the injected HTTP_PROXY/HTTPS_PROXY: "
        "network access goes through the host proxy; direct sockets are blocked. "
        "Python urllib should use its default opener or ProxyHandler() (no arguments), "
        "never ProxyHandler({}); requests must keep trust_env=True. "
        "Do not clear proxy variables or set NO_PROXY to bypass the proxy. "
        "Custom TLS and redirect handlers can be combined with the default proxy handler."
        " For an ordinary approved host command requiring proxy evidence, select network=proxy "
        "and exact proxy_targets. The host injects an authenticated HTTPS CONNECT proxy and records "
        "destinations and byte counts in command_execution.transport; environment presence alone "
        "is not proxy evidence. Keep the default urllib proxy handler and TLS verification. "
        "Use a saved Python script when inline shell syntax cannot match the approved command prefix."
    )
    params_model = Params
    category = "command"


    async def execute(self, params: Params) -> ToolResult:
        from mewcode.experts.command_receipts import execution_observation
        timeout = max(1, min(params.timeout, MAX_TIMEOUT))
        work_dir = active_work_dir()
        from mewcode.tools.command_sandbox import (
            native_auto_scope, run_native_command, sandbox_scope, run_isolated_command,
        )
        if native_auto_scope():
            if params.network == "proxy":
                return ToolResult("Recorded host proxy mode requires an already selected ordinary host backend; "
                    "the required native backend cannot be downgraded. Use its inherited native proxy route.",
                    is_error=True, host_observation={"command_execution": execution_observation(
                        "denied", command_started=False, reason="host_proxy_backend_mismatch")})
            if params.publication == "discard":
                from mewcode.tools.command_sandbox import disposable_native_command
                with disposable_native_command(offline=params.network == "offline"):
                    return await run_native_command(params.command, work_dir=work_dir, timeout=timeout)
            return await run_native_command(params.command, work_dir=work_dir, timeout=timeout)
        if params.publication == "discard":
            return ToolResult("Disposable execution requires the native boundary; command not started.", is_error=True,
                host_observation={"command_execution": execution_observation("denied", command_started=False,
                    reason="discard is unavailable on this backend"), "error_code": "native_discard_unavailable"})
        if sandbox_scope() is not False:
            if params.network == "proxy":
                return ToolResult("Recorded host proxy mode is unavailable in the required container backend.",
                    is_error=True, host_observation={"command_execution": execution_observation(
                        "denied", command_started=False, reason="host_proxy_backend_mismatch")})
            return await run_isolated_command(params.command, work_dir=work_dir, timeout=timeout)
        if params.network == "proxy":
            from mewcode.tools.command_proxy import run_proxied_command
            return await run_proxied_command(params.command, work_dir=work_dir, timeout=timeout,
                                            targets=params.proxy_targets)
        env = _child_env(work_dir)
        shell, shell_args = _shell_command(params.command)
        proc: asyncio.subprocess.Process | None = None

        try:
            proc = await asyncio.create_subprocess_exec(
                shell,
                *shell_args,
                cwd=work_dir,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            if proc is not None:
                await _terminate_process_tree(proc)
            observation = execution_observation("timed_out", reason=f"command timed out after {timeout}s")
            observation["command_started"] = proc is not None
            return ToolResult(output=f"Error: command timed out after {timeout}s", is_error=True,
                              host_observation={"command_execution": observation})
        except asyncio.CancelledError:
            if proc is not None and proc.returncode is None:
                await _terminate_process_tree(proc)
            raise
        except Exception as e:
            return ToolResult(output=f"Error executing command: {e}", is_error=True,
                host_observation={"command_execution": execution_observation(
                    "spawn_failed" if proc is None else "outcome_unknown", reason=str(e))})

        assert proc is not None
        parts = [
            f"SHELL: {Path(shell).name}",
            f"CWD: {work_dir or os.getcwd()}",
        ]
        if stdout:
            parts.append(f"STDOUT:\n{stdout.decode(errors='replace')}")
        if stderr:
            parts.append(f"STDERR:\n{stderr.decode(errors='replace')}")
        if not stdout and not stderr:
            parts.append("(no output)")
        parts.append(f"EXIT CODE: {proc.returncode}")

        output = "\n".join(parts)
        return ToolResult(output=output, is_error=proc.returncode != 0,
            host_observation={"command_execution": execution_observation("exited",
                stdout=stdout.decode(errors="replace"), stderr=stderr.decode(errors="replace"), exit_code=proc.returncode)})


def _child_env(work_dir: str | None) -> dict[str, str]:
    # Child scripts must not inherit MewCode's provider and session secrets.
    # This is credential minimization, not an OS file/network sandbox.
    secret_markers = ("API_KEY", "ACCESS_TOKEN", "REFRESH_TOKEN", "PASSWORD",
                      "SECRET", "AUTHORIZATION", "CREDENTIAL", "PRIVATE_KEY")
    sensitive_prefixes = ("OPENAI_", "ANTHROPIC_", "CODEX_AUTH")
    proxy_names = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
    env = {key: value for key, value in os.environ.items()
           if not any(marker in key.upper() for marker in secret_markers)
           and not key.upper().startswith(sensitive_prefixes)
           and key.upper() != 'MEWCODE_DESKTOP_TOKEN'
           and key.upper() not in proxy_names}
    path_entries: list[str] = []
    if work_dir:
        venv_bin = (
            Path(work_dir) / ".venv" / ("Scripts" if os.name == "nt" else "bin")
        )
        if venv_bin.is_dir():
            path_entries.append(str(venv_bin))
    path_entries.append(str(Path(sys.executable).parent))
    if env.get("PATH"):
        path_entries.append(env["PATH"])
    env["PATH"] = os.pathsep.join(path_entries)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _shell_command(command: str) -> tuple[str, list[str]]:
    if os.name == "nt":
        shell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
        if shell is None:
            shell = os.path.join(
                os.environ.get("SystemRoot", r"C:\Windows"),
                "System32",
                "WindowsPowerShell",
                "v1.0",
                "powershell.exe",
            )
        script = (
            "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
            "$OutputEncoding = [Console]::OutputEncoding; "
            f"& {{ {command} }}; "
            "$mewcodeOk = $?; $mewcodeExit = $LASTEXITCODE; "
            "if ($null -ne $mewcodeExit) { exit $mewcodeExit }; "
            "if (-not $mewcodeOk) { exit 1 }"
        )
        return shell, ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script]

    shell = shutil.which("bash") or "/bin/sh"
    flag = "-lc" if Path(shell).name == "bash" else "-c"
    return shell, [flag, command]
