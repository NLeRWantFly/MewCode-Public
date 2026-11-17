"""External model invocation contracts and the Grok Build CLI adapter.

The coordination broker owns task state, not model inference.  An external
runner may use this module after it has claimed a task and validated the task
constraints.  The adapter never uses a shell and never reads API credentials
from task payloads.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from .coord_capabilities import _grok_environment
from .coord_protocol import (
    CoordinationError,
    EXTERNAL_EXECUTOR_MODEL_ID,
    WriteMode,
    now_iso,
    validate_task_limits,
)


class ExternalModelError(CoordinationError):
    """An invocation could not be prepared or completed."""


class ModelCallStatus(StrEnum):
    COMPLETED = "completed"
    AUTH_REQUIRED = "auth_required"
    FAILED = "failed"
    TIMEOUT = "timeout"
    REJECTED = "rejected"


@dataclass(frozen=True)
class ModelInvocationRequest:
    """A bounded, serializable request for one external model turn."""

    task_id: str
    agent_id: str
    model_id: str
    prompt: str
    cwd: Path
    reasoning_effort: str = "low"
    max_turns: int = 1
    no_subagents: bool = True
    permission_mode: str = "plan"
    allowed_tools: tuple[str, ...] = ()
    allowed_skills: tuple[str, ...] = ()
    allowed_paths: tuple[str, ...] = ()
    forbidden_paths: tuple[str, ...] = ()
    write_mode: str = WriteMode.READ_ONLY.value
    timeout_seconds: int = 120
    metadata: dict[str, Any] = field(default_factory=dict)
    data_only: bool = False

    def validate(self, *, expected_model_id: str) -> None:
        if not self.task_id.strip() or not self.agent_id.strip():
            raise ExternalModelError("task_id and agent_id are required")
        if self.model_id != expected_model_id:
            raise ExternalModelError(f"external adapter only accepts model {expected_model_id}")
        if not self.prompt.strip():
            raise ExternalModelError("prompt is required")
        try:
            validate_task_limits(self.max_turns)
        except CoordinationError as exc:
            raise ExternalModelError(str(exc)) from exc
        if self.max_turns > 1 and self.no_subagents is False:
            raise ExternalModelError("unbounded subagent execution is not allowed")
        if self.permission_mode not in {"plan", "acceptEdits", "default"}:
            raise ExternalModelError("unsupported Grok permission mode")
        if self.write_mode not in {item.value for item in WriteMode}:
            raise ExternalModelError("invalid write_mode")
        if self.write_mode == WriteMode.READ_ONLY.value and self.permission_mode != "plan":
            raise ExternalModelError("read_only invocation must use permission_mode=plan")
        if self.timeout_seconds < 1 or self.timeout_seconds > 120:
            raise ExternalModelError("timeout_seconds must be between 1 and 120")
        if not self.cwd.is_absolute():
            raise ExternalModelError("cwd must be an absolute path")
        if set(self.allowed_paths) & set(self.forbidden_paths):
            raise ExternalModelError("allowed_paths and forbidden_paths overlap")
        if self.data_only and (self.allowed_tools or self.allowed_skills or self.allowed_paths
                               or self.max_turns != 1 or not self.no_subagents or self.write_mode != 'read_only'):
            raise ExternalModelError("data-only turns cannot grant tools, skills, paths or extra turns")
        for name in (*self.allowed_tools, *self.allowed_skills, *self.allowed_paths, *self.forbidden_paths):
            if not isinstance(name, str) or not name.strip():
                raise ExternalModelError("constraint entries must be non-empty strings")

    @classmethod
    def from_task(
        cls,
        task: dict[str, Any],
        *,
        prompt: str,
        cwd: str | Path,
        reasoning_effort: str = "low",
        permission_mode: str | None = None,
        timeout_seconds: int = 120,
    ) -> "ModelInvocationRequest":
        """Create a request from the Broker's normalized task representation."""
        mode = str(task.get("write_mode", WriteMode.READ_ONLY.value))
        max_turns = int(task.get("max_turns", 1))
        if "run_terminal_cmd" in {name.strip().lower() for name in task.get("allowed_tools", [])}:
            max_turns = max(max_turns, 2)
        return cls(
            task_id=str(task.get("task_id", "")),
            agent_id=str(task.get("target_agent_id") or task.get("assignee_id") or ""),
            model_id=EXTERNAL_EXECUTOR_MODEL_ID,
            prompt=prompt,
            cwd=Path(cwd).resolve(),
            reasoning_effort=reasoning_effort,
            max_turns=max_turns,
            no_subagents=True,
            permission_mode=permission_mode or ("plan" if mode == WriteMode.READ_ONLY.value else "acceptEdits"),
            allowed_tools=tuple(task.get("allowed_tools", [])),
            allowed_skills=tuple(task.get("allowed_skills", [])),
            allowed_paths=tuple(task.get("allowed_paths", [])),
            forbidden_paths=tuple(task.get("forbidden_paths", [])),
            write_mode=mode,
            timeout_seconds=timeout_seconds,
        )


@dataclass(frozen=True)
class ModelInvocationResult:
    provider_id: str
    model_id: str
    task_id: str
    status: ModelCallStatus
    exit_code: int | None
    stdout: str
    stderr: str
    command: tuple[str, ...]
    started_at: str
    completed_at: str
    parsed_output: Any = None

    def as_evidence(self) -> dict[str, Any]:
        """Return bounded evidence suitable for ``coord_submit_result``."""
        return {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "status": self.status.value,
            "exit_code": self.exit_code,
            "command": list(self.command),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "parsed_output": self.parsed_output,
        }


class ExternalModelInvoker(Protocol):
    provider_id: str
    model_id: str

    def invoke(self, request: ModelInvocationRequest) -> ModelInvocationResult:
        """Execute one explicitly bounded model call."""

    async def invoke_async(self, request: ModelInvocationRequest) -> ModelInvocationResult:
        """Run the adapter without blocking an async coordinator."""


Runner = Callable[..., subprocess.CompletedProcess[str]]


# Broker tasks use friendly names such as ``Read`` and ``Grep`` while the
# headless Grok CLI accepts its internal IDs.  Keep this list deliberately
# closed: logical coordination/MCP names must remain in the task contract, not
# accidentally become an invalid ``--tools`` value.
_GROK_BUILTIN_TOOL_IDS = {
    "read": "read_file",
    "read_file": "read_file",
    "readfile": "read_file",
    "grep": "grep",
    "list": "list_dir",
    "list_dir": "list_dir",
    "listdir": "list_dir",
    "web_search": "web_search",
    "websearch": "web_search",
    "web_fetch": "web_fetch",
    "webfetch": "web_fetch",
    "search_replace": "search_replace",
    "searchreplace": "search_replace",
    "run_terminal_cmd": "run_terminal_cmd",
    "bash": "run_terminal_cmd",
}


class GrokCLIInvoker:
    """Grok Build adapter using its documented headless single-turn CLI."""

    provider_id = "grok-build"
    model_id = EXTERNAL_EXECUTOR_MODEL_ID

    def __init__(self, executable: str = "grok", *, project_root: str | Path | None = None, grok_home: str | Path | None = None, runner: Runner | None = None) -> None:
        self.executable = executable
        self.project_root = Path(project_root).resolve() if project_root else None
        self.grok_home = Path(grok_home).expanduser().resolve() if grok_home else None
        self._runner = runner or subprocess.run

    def _environment(self) -> dict[str, str]:
        """Use the shared explicit Grok home, then the project-scoped default."""
        return _grok_environment(self.project_root, self.grok_home)

    def build_command(self, request: ModelInvocationRequest, *, _isolated_cwd: Path | None = None) -> list[str]:
        request.validate(expected_model_id=self.model_id)
        if self.project_root is not None and not (request.data_only and _isolated_cwd == request.cwd):
            try:
                request.cwd.relative_to(self.project_root)
            except ValueError as exc:
                raise ExternalModelError("request cwd must stay inside the fixed project root") from exc
            self._validate_paths(request)
        rules = self._constraint_rules(request)
        max_turns = self._effective_max_turns(request)
        command = [
            self.executable,
            "--cwd",
            str(request.cwd),
            "--model",
            self.model_id,
            "--reasoning-effort",
            request.reasoning_effort,
            "--max-turns",
            str(max_turns),
            "--permission-mode",
            request.permission_mode,
            "--rules",
            rules,
            "--output-format",
            "json",
            "--single",
            request.prompt,
        ]
        if request.no_subagents:
            command.insert(command.index("--permission-mode"), "--no-subagents")
        # ``--tools`` is Grok's built-in tool allowlist.  Coordination task
        # permissions are enforced by the broker and MCP namespace; an empty
        # value would disable the built-ins and can also make Grok reject the
        # invocation. Only pass this flag when the task explicitly names real
        # Grok built-in tool ids.
        builtin_tools = tuple(
            dict.fromkeys(
                _GROK_BUILTIN_TOOL_IDS[name.strip().lower()]
                for name in request.allowed_tools
                if name.strip().lower() in _GROK_BUILTIN_TOOL_IDS
            )
        )
        if builtin_tools:
            command.insert(command.index("--output-format"), "--tools")
            command.insert(command.index("--output-format"), ",".join(builtin_tools))
            if "run_terminal_cmd" in builtin_tools:
                command.insert(command.index("--output-format"), "--allow")
                command.insert(command.index("--output-format"), "Bash")
                command.insert(command.index("--output-format"), "--always-approve")
        if request.data_only:
            # Defense in depth: no tools, deny even MCP tool calls, no backend
            # search or cross-session memory. Unsupported CLI versions fail.
            command[1:1] = ["--tools=", "--deny", "*", "--no-memory", "--disable-web-search"]
        return command

    def data_only_preflight(self, cwd: Path) -> None:
        """Never trust an empty task allowlist to disable CLI extensions.

        Inspect uses the same cwd/home as inference. Unknown inspect formats
        are rejected instead of guessing whether hooks or MCP are enabled.
        This does not change any installed user configuration.
        """
        options = dict(cwd=str(cwd), check=False, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=20, shell=False, env=self._environment())
        try:
            help_result = self._runner([self.executable, "--help"], **options)
            required = ("--tools", "--deny", "--no-memory", "--disable-web-search", "--no-subagents")
            if help_result.returncode or any(flag not in help_result.stdout for flag in required):
                raise ExternalModelError("DATA_ONLY_UNAVAILABLE: CLI safety flags are unavailable")
            inspected = self._runner([self.executable, "--cwd", str(cwd), "inspect", "--json"], **options)
            manifest = json.loads(inspected.stdout)
            if inspected.returncode or not isinstance(manifest, dict):
                raise ValueError("invalid inspect result")
            for key in ("rules", "skills", "hooks", "plugins", "mcp_servers"):
                if key not in manifest or manifest[key] not in ([], {}):
                    raise ExternalModelError("DATA_ONLY_UNAVAILABLE: isolated inspect must explicitly show no rules, skills, hooks, plugins or MCP servers")
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise ExternalModelError("DATA_ONLY_UNAVAILABLE: unable to verify isolated CLI configuration") from exc

    @staticmethod
    def _effective_max_turns(request: ModelInvocationRequest) -> int:
        """Give terminal-enabled tasks enough turns without changing the global cap."""
        terminal_enabled = any(name.strip().lower() == "run_terminal_cmd" for name in request.allowed_tools)
        max_turns = max(request.max_turns, 2) if terminal_enabled else request.max_turns
        return validate_task_limits(max_turns)

    def _validate_paths(self, request: ModelInvocationRequest) -> None:
        assert self.project_root is not None
        for raw in (*request.allowed_paths, *request.forbidden_paths):
            candidate = Path(raw)
            resolved = (candidate if candidate.is_absolute() else request.cwd / candidate).resolve()
            try:
                resolved.relative_to(self.project_root)
            except ValueError as exc:
                raise ExternalModelError(f"task path escapes the fixed project root: {raw}") from exc

    @staticmethod
    def _constraint_rules(request: ModelInvocationRequest) -> str:
        def join(values: Sequence[str]) -> str:
            return ", ".join(values) if values else "(none)"

        return (
            "Coordination task contract. Follow these limits exactly. "
            f"task_id={request.task_id}; write_mode={request.write_mode}; "
            f"allowed_tools=[{join(request.allowed_tools)}]; "
            f"allowed_skills=[{join(request.allowed_skills)}]; "
            f"allowed_paths=[{join(request.allowed_paths)}]; "
            f"forbidden_paths=[{join(request.forbidden_paths)}]. "
            "Do not use undeclared tools or skills, do not read or write forbidden paths, "
            "and do not spawn subagents. Return concise evidence for the caller."
        )

    def invoke(self, request: ModelInvocationRequest) -> ModelInvocationResult:
        if request.data_only:
            # Never start a data-only CLI inside the user's repository: the
            # harness may discover instructions or index files before tools
            # run. Only a newly created empty context is supplied to Grok.
            # Credentials remain in its configured home, not in the prompt.
            request.validate(expected_model_id=self.model_id)
            with tempfile.TemporaryDirectory(prefix='mewcode-team-context-') as temporary:
                isolated = Path(temporary).resolve()
                if any((parent / '.git').exists() for parent in isolated.parents):
                    raise ExternalModelError('DATA_ONLY_UNAVAILABLE: temporary context must be outside all repositories')
                self.data_only_preflight(isolated)
                return self._invoke_prepared(replace(request, cwd=isolated), _isolated_cwd=isolated)
        return self._invoke_prepared(request)

    def _invoke_prepared(self, request: ModelInvocationRequest, *, _isolated_cwd: Path | None = None) -> ModelInvocationResult:
        command = tuple(self.build_command(request, _isolated_cwd=_isolated_cwd))
        started = now_iso()
        try:
            completed = self._runner(
                list(command),
                cwd=str(request.cwd),
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=request.timeout_seconds,
                shell=False,
                env=self._environment(),
            )
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            status = self._status(completed.returncode, stdout, stderr)
            stdout, stderr = self._sanitize_auth_evidence(status, stdout, stderr)
            parsed = self._parse_output(stdout)
            return ModelInvocationResult(self.provider_id, self.model_id, request.task_id, status, completed.returncode, stdout, stderr, command, started, now_iso(), parsed)
        except subprocess.TimeoutExpired as exc:
            stdout = self._text(exc.stdout)
            stderr = self._text(exc.stderr)
            return ModelInvocationResult(self.provider_id, self.model_id, request.task_id, ModelCallStatus.TIMEOUT, None, stdout, stderr, command, started, now_iso())
        except OSError as exc:
            return ModelInvocationResult(self.provider_id, self.model_id, request.task_id, ModelCallStatus.FAILED, None, "", str(exc), command, started, now_iso())

    def probe_auth(self, *, timeout_seconds: int = 20) -> ModelInvocationResult:
        """Check whether the local Grok CLI can use its configured credentials.

        ``grok inspect`` only reports local configuration, so it is not an
        authentication check. ``grok models`` reaches the authenticated model
        catalog without sending a task prompt.
        """
        command = (self.executable, "models")
        started = now_iso()
        cwd = str(self.project_root) if self.project_root is not None else None
        try:
            completed = self._runner(
                list(command),
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                shell=False,
                env=self._environment(),
            )
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            status = self._status(completed.returncode, stdout, stderr)
            stdout, stderr = self._sanitize_auth_evidence(status, stdout, stderr)
            return ModelInvocationResult(
                self.provider_id,
                self.model_id,
                "preflight",
                status,
                completed.returncode,
                stdout,
                stderr,
                command,
                started,
                now_iso(),
            )
        except subprocess.TimeoutExpired as exc:
            return ModelInvocationResult(
                self.provider_id,
                self.model_id,
                "preflight",
                ModelCallStatus.TIMEOUT,
                None,
                self._text(exc.stdout),
                self._text(exc.stderr),
                command,
                started,
                now_iso(),
            )
        except OSError as exc:
            return ModelInvocationResult(
                self.provider_id,
                self.model_id,
                "preflight",
                ModelCallStatus.FAILED,
                None,
                "",
                str(exc),
                command,
                started,
                now_iso(),
            )

    async def invoke_async(self, request: ModelInvocationRequest) -> ModelInvocationResult:
        return await asyncio.to_thread(self.invoke, request)

    def _status(self, exit_code: int, stdout: str, stderr: str) -> ModelCallStatus:
        combined = f"{stdout}\n{stderr}".lower()
        if (
            "not signed in" in combined
            or "not authenticated" in combined
            or "no auth credentials" in combined
            or "xai_api_key" in combined
            or ("auth.json" in combined and ("access is denied" in combined or "permission denied" in combined))
        ):
            return ModelCallStatus.AUTH_REQUIRED
        return ModelCallStatus.COMPLETED if exit_code == 0 else ModelCallStatus.FAILED

    @staticmethod
    def _sanitize_auth_evidence(status: ModelCallStatus, stdout: str, stderr: str) -> tuple[str, str]:
        if status is ModelCallStatus.AUTH_REQUIRED and "auth.json" in f"{stdout}\n{stderr}".lower():
            return "", "Grok auth.json is inaccessible; run scripts/login_coord_grok.ps1 to authenticate the configured Grok home."
        return stdout, stderr

    @staticmethod
    def _parse_output(stdout: str) -> Any:
        try:
            return json.loads(stdout)
        except (TypeError, json.JSONDecodeError):
            return None

    @staticmethod
    def _text(value: str | bytes | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value
