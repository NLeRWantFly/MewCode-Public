from __future__ import annotations

import os
import re
import hashlib
import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

from mewcode.permissions.dangerous import (
    DangerousCommandDetector,
    command_authorization_reason,
    command_review_prior,
    is_auto_local_command,
    is_workspace_local_command,
    is_safe_command,
    command_requires_auto_review,
    command_sensitive_read_reason,
)
from mewcode.permissions.modes import DecisionEffect, PermissionMode, mode_decide
from mewcode.permissions.grants import GrantKind, GrantStore, GrantStoreError
from mewcode.permissions.rules import RuleEngine, extract_content
from mewcode.permissions.sandbox import PATH_ESCAPE_REASON, PathSandbox
from mewcode.permissions.sensitive_paths import classify_sensitive_read_path
from mewcode.permissions.task_authorization import TaskAuthorization
from mewcode.tools.base import Tool

_PLAN_MODE_ALLOWED_TOOLS = frozenset({
    "Agent",
    "ToolSearch",
    "AskUserQuestion",
    "human_oncall",
    "ExitPlanMode",
})

_NETWORK_PATTERN = re.compile(
    r"(?i)\b(curl|wget|invoke-webrequest|git\s+push|npm\s+publish|"
    r"deploy|kubectl|terraform|pulumi|ssh|scp)\b"
)

_HOST_PERMISSION_FILES = frozenset({
    "permissions.grants.json", "permissions.yaml", "permissions.local.yaml",
})


@dataclass
class Decision:
    effect: DecisionEffect
    reason: str
    source: str = "mode"
    rule_id: str = ""
    risk: dict[str, Any] = field(default_factory=dict)
    cache_hit: bool = False
    subcondition: str = ""
    clearance: str = ""


# Modes that may ask the user about a path outside the workspace.  Bypass and
# DontAsk are deliberately excluded: asking there would contradict the mode.
_SANDBOX_ASK_MODES = frozenset({
    PermissionMode.DEFAULT,
    PermissionMode.ACCEPT_EDITS,
    PermissionMode.AUTO,
    PermissionMode.STRICT_ISOLATION,
    PermissionMode.CUSTOM,
})


class PermissionChecker:
    """Deterministic permission policy used before any optional safety review."""

    def __init__(
        self,
        detector: DangerousCommandDetector,
        sandbox: PathSandbox,
        rule_engine: RuleEngine,
        mode: PermissionMode = PermissionMode.DEFAULT,
        grant_store: GrantStore | None = None,
    ) -> None:
        self.detector = detector
        self.sandbox = sandbox
        self.rule_engine = rule_engine
        self.mode = mode
        self.plan_file_path: str = ""
        self._approved_calls: set[str] = set()
        self.task_authorization: TaskAuthorization | None = None
        self.task_id: str = ""
        self.grant_store = grant_store
        self.review_workspace_writes = False
        from mewcode.permissions.network_policy import NetworkPolicyStore
        self.network_policy_store = NetworkPolicyStore()

    def set_task_authorization(self, authorization: TaskAuthorization | None, *, task_id: str) -> None:
        self.task_authorization = authorization
        self.task_id = task_id

    def uses_native_commands(self, arguments: dict | None = None) -> bool:
        """Choose the backend before review; never fall back after a failed run.

        Auto uses the reviewed host runner; full access uses the host runner.
        Explicit network limits,
        disposable execution and genuinely restricted child scopes still need
        an OS boundary.
        """
        if self.mode in {PermissionMode.AUTO, PermissionMode.BYPASS}:
            from mewcode.permissions.network_policy import policy_for
            network = policy_for(self)
            return (self.requires_native_commands()
                    or (arguments or {}).get("network") == "offline"
                    or not network.enabled or bool(network.denied_domains)
                    or getattr(self, "native_publication_mode", "publish") == "discard"
                    or (arguments or {}).get("publication") == "discard")
        return os.name == "nt" and self.mode != PermissionMode.PLAN

    def requires_native_commands(self) -> bool:
        from mewcode.experts.node_expansion import ExpansionPathSandbox
        return isinstance(self.sandbox, ExpansionPathSandbox) or getattr(self, "requires_command_isolation", False)

    def _call_key(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Bind a session grant to task, arguments and current file state."""
        target_state: dict[str, Any] = {}
        if tool_name in {"ReadFile", "WriteFile", "EditFile", "ReportArtifact"}:
            raw = extract_content(tool_name, arguments)
            if raw:
                try:
                    path = Path(raw).expanduser()
                    if not path.is_absolute():
                        path = self.sandbox.project_root / path
                    path = path.resolve(strict=False)
                    target_state["path"] = str(path)
                    if path.is_file():
                        stat = path.stat()
                        target_state.update(size=stat.st_size, mtime_ns=stat.st_mtime_ns)
                        if stat.st_size <= 4 * 1024 * 1024:
                            target_state["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                    else:
                        target_state["exists"] = path.exists()
                except (OSError, RuntimeError, ValueError):
                    target_state["unknown"] = True
        authorization = self.task_authorization
        payload = [tool_name, arguments, self.task_id,
                   authorization.version if authorization else "", target_state]
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                         default=str).encode()).hexdigest()

    def grant_call(self, tool_name, arguments) -> bool:
        """Remember a structured exact call; shell effects cannot be cached."""
        if tool_name in {"Bash", "RunVerification", "TerminalStart", "TerminalInput"}:
            return False
        self._approved_calls.add(self._call_key(tool_name, arguments))
        return True

    def check(self, tool: Tool, arguments: dict[str, Any]) -> Decision:
        content = extract_content(tool.name, arguments)
        # Glob/Grep use `pattern` for rule matching, but `path` is their
        # filesystem target. Never mistake a search expression for a path.
        path_content = (
            str(arguments.get("path") or self.sandbox.project_root)
            if tool.name in {"Glob", "Grep"} else content
        )
        risk = self._risk(tool, content, arguments)
        authorization_reason = ""
        review_prior = ""
        if tool.category == "command" or tool.name == "TerminalInput":
            authorization_reason = command_authorization_reason(content)
            review_prior = command_review_prior(content)

        if tool.category in {"write", "command"} or tool.name == "TerminalInput":
            from mewcode.sandbox.transaction_registry import workspace_write_block, workspace_command_block
            command = tool.category == "command" or tool.name == "TerminalInput"
            native_command = command and self.uses_native_commands(arguments)
            blocker = workspace_command_block if native_command else workspace_write_block
            offline_copy = (native_command and tool.name in {"Bash", "RunVerification"}
                and arguments.get("network") == "offline"
                and (arguments.get("publication") == "discard" or tool.name == "RunVerification"
                     or getattr(self, "native_publication_mode", "publish") == "discard"))
            block_args = {"independent_command": content} if offline_copy else {}
            blocked = blocker(self.sandbox.project_root, **block_args)
            if blocked and self.mode == PermissionMode.AUTO:
                from mewcode.sandbox.native_recovery import recover_unpublished_transactions
                recover_unpublished_transactions(self)
                blocked = blocker(self.sandbox.project_root, **block_args)
            if blocked:
                return Decision("deny", blocked, "native_transaction_recovery", risk=risk,
                                subcondition="unsettled_command" if command else "unsettled_publication",
                                clearance="reconcile_native_transaction")

            if command and not native_command:
                from mewcode.sandbox.transaction_diagnostics import pending_command_context
                try:
                    pending = pending_command_context(self.sandbox.project_root, content)
                except (OSError, ValueError, RuntimeError):
                    return Decision("deny", "无法读取旧执行状态，需先恢复宿主事务诊断。",
                        "native_transaction_recovery", risk=risk, subcondition="unknown_effect",
                        clearance="reconcile_native_transaction")
                if pending.get("replays_unknown"):
                    return Decision("deny", "原隔离命令结果仍未知，不能换执行器重放同一命令。",
                        "unknown_command_replay", risk=risk, subcondition="unknown_effect",
                        clearance="verify_original_effect_or_choose_independent_work")
                if pending.get("items"):
                    risk = {**risk, "pending_isolated_operations": pending["items"]}
                    review_prior = "; ".join(filter(None, (review_prior,
                        "旧隔离副本的执行结果未知；只审查独立的新操作，不将旧事务当作成功或重放旧命令。")))

        # An expansion node's declared write set is an immutable application-
        # policy boundary for structured file writes.  It must run before
        # session/persistent grants, approved revisions, rules, or mode
        # fast-paths.  This is not an OS isolation boundary for shell commands.
        if tool.category == "write" and tool.name in {"WriteFile", "EditFile", "PreparePythonEnvironment"}:
            from mewcode.experts.node_expansion import ExpansionPathSandbox

            if isinstance(self.sandbox, ExpansionPathSandbox):
                try:
                    in_scope, scope_reason = self.sandbox.check_declared_write_scope(
                        path_content
                    )
                except Exception:
                    in_scope, scope_reason = False, (
                        "无法验证微图子节点声明的写入范围；该写入按拒绝处理。"
                    )
                if not in_scope:
                    return Decision(
                        "deny",
                        scope_reason or "写入不在微图子节点声明范围内。",
                        source="node_write_scope",
                        risk=risk,
                        subcondition="outside_declared_write_set",
                        clearance="revise_node_contract_with_authorization",
                    )

        # Project-local policy files are host authority, not model artifacts.
        # A normal WriteFile/EditFile must not manufacture its own grant or
        # wildcard rule, regardless of mode, task authorization, or reviewer.
        if tool.category == "write" and path_content:
            candidate = Path(path_content).expanduser()
            if not candidate.is_absolute():
                candidate = self.sandbox.project_root / candidate
            try:
                candidate = candidate.resolve(strict=False)
                policy_dir = (self.sandbox.project_root / ".mewcode").resolve(strict=False)
                protected = (candidate.parent == policy_dir and candidate.name.lower() in _HOST_PERMISSION_FILES)
                protected = protected or (
                    candidate.parent == policy_dir / "rules" and candidate.suffix.lower() == ".rules"
                )
                if self.grant_store is not None:
                    protected = protected or candidate == self.grant_store.path.resolve(strict=False)
            except (OSError, RuntimeError, ValueError):
                protected = True
            if protected:
                return Decision(
                    "deny", "Host permission policy files can only be changed by the user-facing authority flow",
                    source="permission_authority_guardrail", risk=risk,
                    subcondition="host_permission_file_write",
                    clearance="use_explicit_user_permission_flow",
                )
        if tool.category == "command" and re.search(
            r"(?i)(?:permissions\.grants\.json|permissions\.local\.yaml|(?:^|[/\\])permissions\.yaml|\.mewcode[/\\]rules[/\\][^\s'\"]+\.rules)",
            content or "",
        ):
            from mewcode.permissions.effects import effect_signature
            effect = effect_signature(tool.name, arguments, work_dir=str(self.sandbox.project_root), tool=tool)
            if (effect.kind in {"write", "delete", "authority_change"}
                    or (effect.kind != "read" and self.mode != PermissionMode.AUTO)):
                return Decision(
                    "deny", "Command mutates a host permission authority file",
                    source="permission_authority_guardrail", risk=risk,
                    subcondition="host_permission_file_command",
                    clearance="use_explicit_user_permission_flow",
                )
            if effect.kind != "read":
                review_prior = "; ".join(filter(None, (review_prior,
                    "Command references permission authority; inspect whether it reads or mutates. Writes cannot be authorized by the executor.")))

        # Invariant guardrail: no mode, including Bypass, can override this.
        if tool.category == "command" or tool.name == "TerminalInput":
            hit, reason = self.detector.detect(content)
            if hit:
                return Decision(
                    "deny",
                    f"Blocked by invariant safety guardrail: {reason}",
                    source="invariant_guardrail",
                    risk=risk,
                    subcondition="invariant_command_hazard",
                    clearance="replace_with_safe_alternative",
                )

        if tool.category == "read" and path_content:
            sensitive = classify_sensitive_read_path(path_content, workspace_root=self.sandbox.project_root)
            if sensitive.sensitive:
                return Decision(
                    "deny", f"Sensitive read blocked: {sensitive.reason}; use a sanitized copy or host export",
                    source="sensitive_read_mask", risk=risk,
                    subcondition="sensitive_read_target", clearance="use_sanitized_copy_or_host_export",
                )
        if tool.category == "command":
            sensitive_reason = command_sensitive_read_reason(content or "", self.sandbox.project_root)
            if sensitive_reason:
                return Decision(
                    "deny", f"Sensitive command read blocked: {sensitive_reason}; use a sanitized copy or host export",
                    source="sensitive_read_mask", risk=risk,
                    subcondition="sensitive_command_read_target", clearance="use_sanitized_copy_or_host_export",
                )

        from mewcode.permissions.network_policy import NetworkDenied, policy_for
        network_policy = policy_for(self)
        if risk.get('network') and not (tool.name in {'Browser', 'browser'}
                and arguments.get('action') in {'snapshot', 'links'} and not arguments.get('url')):
            try:
                if not network_policy.enabled:
                    network_policy.check_host('network.invalid')
                if isinstance(arguments.get('url'), str) and arguments['url']:
                    network_policy.check_url(arguments['url'])
            except NetworkDenied as exc:
                return Decision('deny', str(exc), 'network_policy', risk=risk,
                    subcondition='network_disabled' if not network_policy.enabled else 'network_blacklist',
                    clearance='change_sandbox_network_settings')

        explicit_rule_effect = self.rule_engine.evaluate_for_checker(tool.name, content)
        if explicit_rule_effect == "deny":
            return Decision("deny", "Explicit deny rule", "rule_engine", risk=risk,
                            subcondition="explicit_deny_rule",
                            clearance="change_rule_with_user_authorization")

        # Conversation-owned graph metadata is equally bounded outside Plan.
        # Local drafting does not require a second permission card, while an
        # explicit ask rule or optional paid/network advice keeps normal review.
        if tool.name in {"ReadWorkflowBlocks", "AdvanceTaskDesign", "SubmitWorkflowBlocks"}:
            from mewcode.permissions.effects import workflow_design_contract
            contract = workflow_design_contract(tool.name, arguments,
                work_dir=self.sandbox.project_root, tool=tool)
            if contract is None:
                return Decision("deny", "Workflow design tool, arguments or live conversation scope are invalid",
                    "host_tool_scope", risk=risk)
            risk = {**risk, "host_workflow_design_contract": contract}
            if explicit_rule_effect != "ask" and not risk.get("network") and not risk.get("cost"):
                return Decision("allow", "Conversation-bound workflow design state only",
                    "plan_mode" if self.mode == PermissionMode.PLAN else "workflow_design", risk=risk)

        # Planning is read-only even when execution has an existing grant,
        # wildcard allow rule or approved repair revision.
        if self.mode == PermissionMode.PLAN:
            if tool.name == "RuntimeDoctor" and arguments.get("action") == "recover":
                return Decision("deny", "Plan mode cannot repair host runtime state", "plan_mode", risk=risk)
            if tool.name in _PLAN_MODE_ALLOWED_TOOLS:
                return Decision("allow", "Plan mode: allowed tool", "plan_mode", risk=risk)
            if tool.name in {"WriteFile", "EditFile"} and self._is_plan_file(content):
                return Decision("allow", "Plan mode: current plan file", "plan_mode", risk=risk)
            if tool.category != "read":
                return Decision("deny", "Plan模式只允许读取和写入当前计划文件。", "plan_mode", risk=risk)

        if (os.name == 'nt' or self.mode in {PermissionMode.AUTO, PermissionMode.STRICT_ISOLATION}) and tool.name in {"TerminalStart", "TerminalInput"}:
            if tool.name == "TerminalInput" and arguments.get("stop") is True:
                return Decision("allow", "Stop owned terminal process", "terminal_stop", risk=risk)
            return Decision("deny", "交互终端尚未接入沙箱网络与文件边界，请使用 Bash 执行命令。", "terminal_boundary", risk=risk,
                subcondition="interactive_terminal_unbounded", clearance="use_structured_tool_or_wait_for_native_boundary")

        if self.mode in {PermissionMode.AUTO, PermissionMode.STRICT_ISOLATION} and (tool.category == "command" or tool.name == "TerminalInput"):
            if tool.name in {"TerminalStart", "TerminalInput"}:
                if tool.name == "TerminalInput" and arguments.get("stop") is True:
                    return Decision("allow", "Stop owned terminal process", "terminal_stop", risk=risk)
                return Decision("deny", "AUTO_DENIED: interactive terminal is unavailable before the native execution boundary; use Bash or structured tools, or explicitly change mode", "terminal_boundary", risk=risk, subcondition="interactive_terminal_unbounded", clearance="use_structured_tool_or_wait_for_native_boundary")

            if (self.mode == PermissionMode.AUTO and self.uses_native_commands(arguments)
                    and tool.name in {"Bash", "RunVerification"}):
                if network_policy.enabled and arguments.get("network") != "offline":
                    from mewcode.sandbox.srt_runtime import runtime_status
                    status = runtime_status()
                    if not status['ready']:
                        return Decision('deny', status['reason'], 'network_runtime', risk=risk,
                            subcondition='network_runtime_unavailable', clearance='configure_network_runtime_once')
                from mewcode.sandbox.windows import native_runtime_ready
                offline_native = not network_policy.enabled or arguments.get("network") == "offline"
                if offline_native and not native_runtime_ready():
                    from mewcode.sandbox.native_recovery import recover_native_runtime
                    recover_native_runtime()
                if offline_native and self.requires_native_commands() and not native_runtime_ready():
                    from mewcode.sandbox.native_gate import native_gate_status
                    status = native_gate_status()
                    return Decision(
                        "deny",
                        f"AUTO_BOUNDARY_DENIED ({status.state.value}): 子节点命令需要可用的隔离执行器；"
                        "宿主命令无法保证声明的写入范围。",
                        "native_boundary_unverified", risk=risk,
                        subcondition="native_boundary_not_attested",
                        clearance="use_structured_tools_or_attest_native_backend",
                    )

            if self.mode == PermissionMode.STRICT_ISOLATION and tool.name in {"Bash", "RunVerification"}:
                scope = self.task_authorization
                if scope is None:
                    return Decision("deny", "STRICT_ISOLATION_DENIED: missing task authorization; authorize a concrete task scope", "isolation_guardrail", risk=risk, subcondition="missing_authorization", clearance="authorize_task_scope")
                if self.task_id != scope.task_id:
                    return Decision("deny", "STRICT_ISOLATION_DENIED: task ID differs from authorized scope; refresh the task authorization", "isolation_guardrail", risk=risk, subcondition="task_id_mismatch", clearance="refresh_task_authorization")
                if not scope.read_directories and not scope.write_directories:
                    return Decision("deny", "STRICT_ISOLATION_DENIED: no authorized directory; grant a concrete work directory", "isolation_guardrail", risk=risk, subcondition="missing_directory", clearance="grant_work_directory")
                if "isolated_command" not in scope.allowed_behaviors:
                    return Decision("deny", "STRICT_ISOLATION_DENIED: isolated_command behavior is not authorized; request that exact behavior", "isolation_guardrail", risk=risk, subcondition="missing_isolated_command", clearance="authorize_isolated_command")

        if tool.name == "ExportHostState":
            from mewcode.tools.export_host_state import ExportHostState
            if self.mode == PermissionMode.PLAN:
                return Decision("deny", "Plan mode cannot export workspace files", "plan_mode", risk=risk)
            if isinstance(tool, ExportHostState) and tool.root == self.sandbox.project_root:
                return Decision("allow", "Host-bound deterministic state export", "host_state_export", risk=risk)
            return Decision("deny", "Host-state exporter is not bound to this workspace", "host_tool_scope", risk=risk,
                            subcondition="host_tool_scope_mismatch", clearance="rebuild_tool_for_current_workspace")

        if explicit_rule_effect == "ask":
            return Decision("ask", "Command prefix rule requires confirmation", "command_prefix_rule", risk=risk,
                            subcondition="command_prefix_prompt", clearance="approve_exact_operation_or_change_rule")

        if self.mode == PermissionMode.AUTO and (authorization_reason or review_prior):
            return Decision("review", authorization_reason or review_prior, "command_risk_prior",
                risk={**risk, "risk_prior": "high", "review_prior": review_prior,
                      "specific_authority_required": authorization_reason},
                subcondition="specific_authorization" if authorization_reason else "heuristic_review",
                clearance="review_exact_effect_and_existing_authority")
        if authorization_reason and self.mode not in {PermissionMode.BYPASS, PermissionMode.DONT_ASK}:
            return Decision("ask", authorization_reason, "command_authorization", risk=risk,
                            subcondition="specific_authorization", clearance="authorize_exact_effect")

        if tool.name == "RuntimeDoctor":
            from mewcode.tools.runtime_doctor import RuntimeDoctor
            if isinstance(tool, RuntimeDoctor):
                return Decision("allow", "Bounded host-authorized runtime service", "host_runtime_recovery", risk=risk)

        # A user grant is scoped to an operation, not a mode override. Plan
        # mode remains read-only even when a call was approved in another mode.
        if (self.mode != PermissionMode.PLAN and self._approved_calls
                and self._call_key(tool.name, arguments) in self._approved_calls):
            return Decision("allow", "Exact operation approved for this session", "user_grant", risk=risk)

        authorization = self.task_authorization
        if (self.mode != PermissionMode.PLAN and authorization is not None
                and not (self.review_workspace_writes and tool.category == "write")
                and authorization.covers(tool, arguments, task_id=self.task_id)):
            return Decision(
                "allow", f"Task scope {authorization.version} covers this exact target",
                "task_authorization", rule_id=authorization.version, risk=risk,
            )

        # Read-only commands are deterministic fast-paths in every mode.
        if (tool.category == "command" and is_safe_command(content or "")
                and not command_requires_auto_review(content or "", self.sandbox.project_root)[0]):
            if self.mode == PermissionMode.AUTO:
                if not self.uses_native_commands(arguments):
                    return Decision("review", "Auto模式：审核宿主命令后执行。", "auto_escalation", risk=risk)
                return Decision("allow", "Native backend selected for read-only command", "auto_native_readonly", risk=risk)
            return Decision("allow", "Safe read-only command", "safe_readonly", risk=risk)

        if tool.category in ("read", "write") and path_content:
            ok, reason = self.sandbox.check(path_content, read_only=tool.category == "read")
            if not ok:
                if reason == PATH_ESCAPE_REASON:
                    return Decision("deny", reason, "path_escape_guardrail", risk=risk,
                                    subcondition="implicit_path_escape",
                                    clearance="use_explicit_ordinary_external_path")
                if tool.category == "write" and tool.name in {"WriteFile", "EditFile", "ReportArtifact"}:
                    try:
                        store = self.grant_store
                        if store is None and self.sandbox.project_root.is_dir():
                            store = GrantStore(self.sandbox.project_root)
                            self.grant_store = store
                        grant = store.matching_grant(GrantKind.WRITE_DIRECTORY, content) if store else None
                    except GrantStoreError as exc:
                        return Decision("deny", f"Persistent grant store invalid: {exc}", "grant_store_guardrail", risk=risk, subcondition="grant_store_invalid", clearance="repair_grant_store_with_user")
                    if grant is not None:
                        return Decision("allow", "Explicit persistent writable-directory grant", "persistent_directory_grant", rule_id=grant.grant_id, risk=risk)
                if (self.mode in {PermissionMode.AUTO, PermissionMode.BYPASS}
                        and tool.name in {"WriteFile", "EditFile"}):
                    external_ok, external_reason = self.sandbox.check_external_write(path_content)
                    if not external_ok:
                        return Decision("deny", external_reason, "external_write_guardrail", risk=risk,
                                        subcondition="protected_external_write",
                                        clearance="use_host_settings_or_explicit_scoped_authorization")
                    if self.mode == PermissionMode.BYPASS:
                        return Decision("allow", "完全放行：普通外部文件写入", "full_access_external_write", risk=risk)
                    return Decision("review", "Auto模式：自动审核外部文件写入，无需额外文件授权卡。",
                                    "auto_external_write", risk={**risk, "side_effect": "external"},
                                    subcondition="external_file_write_review",
                                    clearance="review_exact_target_and_existing_user_request")
                if self.mode not in _SANDBOX_ASK_MODES:
                    # Plan mode is read-only by construction, and Bypass/DontAsk
                    # mean "do not interrupt me": neither may turn a path-scope
                    # failure into an interactive card.
                    return Decision(
                        "deny",
                        f"Path sandbox blocked operation: {reason}",
                        "sandbox",
                        risk=risk,
                        subcondition="path_outside_write_scope" if tool.category == "write" else "read_path_guardrail",
                        clearance="request_exact_scope_or_use_workspace_path",
                    )
                # Outside the workspace: ask the user, naming the exact path and
                # the access being requested. Approving records a grant for this
                # run, so the same folder does not ask again for every file.
                access = "只读" if tool.category == "read" else "读写"
                return Decision(
                    "ask",
                    f"{reason}\n申请{access}访问：{content}。同意后本轮对该路径生效"
                    "（目录授权含子目录，不含同级目录），拒绝则本次操作不执行。",
                    "sandbox_path",
                    risk=risk,
                    subcondition="path_outside_write_scope" if tool.category == "write" else "read_path_guardrail",
                    clearance="authorize_exact_path_or_directory",
                )

        # This explicit host override is separate from entering recovery.
        # Ordinary repair agents do not enable it.
        if (self.review_workspace_writes and self.mode == PermissionMode.AUTO
                and tool.category == "write"):
            return Decision("review", "宿主策略要求核对这项具体写入。",
                "execution_recovery_review", risk=risk,
                subcondition="execution_condition_repair", clearance="review_exact_repair")

        from mewcode.experts.recovery_scope import current_approved_revision
        revision = current_approved_revision()
        if revision and tool.category in {"read", "write"}:
            return Decision("allow", "Approved revision execution", "approved_revision", risk=risk)
        if revision and tool.category == "command" and is_safe_command(content or ""):
            return Decision("allow", "Approved revision verification", "approved_revision", risk=risk)

        explicit = explicit_rule_effect
        if explicit == "allow":
            return Decision("allow", "Explicit permission rule", "rule_engine", risk=risk)
        if explicit == "deny":
            return Decision("deny", "Explicit permission rule", "rule_engine", risk=risk)

        # Known local work is automatic; ambiguous operations use the reviewer.
        if self.mode in {PermissionMode.AUTO, PermissionMode.STRICT_ISOLATION}:
            from mewcode.mcp.playwright_browser import is_trusted_auto_browser_tool

            if is_trusted_auto_browser_tool(tool.name):
                return Decision(
                    "allow",
                    "Auto mode: trusted browser tool",
                    "auto_browser_trust",
                    risk=risk,
                )
            if (tool.category == "read" and not risk.get("network")
                    and risk.get("side_effect") == "none"):
                return Decision("allow", "Auto mode: read-only tool", "auto_fast_path", risk=risk)
            if (tool.name in {'WebFetch', 'web_fetch', 'Browser', 'browser'}
                    and risk.get('side_effect') == 'none'):
                return Decision('allow', '联网已开启，目标未命中黑名单', 'network_policy',
                                rule_id=network_policy.revision, risk=risk)
            if (tool.name in {"WriteFile", "EditFile"} and content
                    and risk.get("side_effect") == "workspace"):
                return Decision("allow", "Auto mode: workspace edit", "auto_fast_path", risk=risk)
            if tool.category == "command":
                if self.mode == PermissionMode.AUTO:
                    local_validation = is_auto_local_command(content or "", self.sandbox.project_root)
                    workspace_script = is_workspace_local_command(content or "", self.sandbox.project_root)
                    if local_validation or workspace_script:
                        from mewcode.sandbox.windows import native_fastpath_ready
                        if self.uses_native_commands(arguments) and native_fastpath_ready():
                            return Decision(
                                "allow", "Attested native boundary covers local command",
                                "auto_native_validation" if local_validation else "auto_native_workspace_script",
                                risk=risk,
                            )
                        return Decision(
                            "review",
                            "Auto模式：审核工作区脚本或验证命令后执行。",
                            "auto_escalation", risk=risk,
                            subcondition="workspace_command_review",
                            clearance="review_exact_command",
                        )
                needs_review, why = command_requires_auto_review(
                    content or "", self.sandbox.project_root
                )
                return Decision(
                    "review",
                    f"Auto 模式审核不具备 OS 隔离的命令：{why or '潜在脚本副作用不可静态界定'}",
                    "auto_escalation",
                    risk=risk,
                )
            return Decision(
                "review",
                "Auto 模式自动审核外部或未知副作用是否符合用户任务。",
                "auto_escalation",
                risk=risk,
            )

        if getattr(tool, "interaction_kind", "") == "computer_use":
            if self.mode not in {PermissionMode.BYPASS, PermissionMode.DONT_ASK}:
                return Decision(
                    "ask",
                    "Computer use requires explicit confirmation",
                    "interaction_policy",
                    risk=risk,
                )

        effect = mode_decide(self.mode, str(tool.category))
        if effect == "allow":
            return Decision(effect, f"Permission mode {self.mode.value} allowed", "mode", risk=risk)
        if effect == "deny":
            return Decision(effect, f"Permission mode {self.mode.value} denied", "mode", risk=risk)
        if effect == "review":
            # Defensive fallback for any future non-Auto review matrix entry.
            return Decision("deny", "Safety reviewer is available only in Auto mode", "mode", risk=risk)
        return Decision("ask", "User confirmation required", "hitl", risk=risk)

    @staticmethod
    def _risk(tool: Tool, content: str, arguments: dict[str, Any]) -> dict[str, Any]:
        risk_factory = getattr(tool, "permission_risk", None)
        if callable(risk_factory):
            profile = risk_factory(arguments).as_dict()
        else:
            interaction = str(getattr(tool, "interaction_kind", ""))
            category = str(getattr(tool, "category", "unknown"))
            profile = {
                "side_effect": (
                    "none" if category == "read" else
                    "workspace" if category == "write" else
                    "external" if interaction in {"computer_use", "browser"} else
                    "unknown"
                ),
                "network": interaction in {"computer_use", "browser"},
                "cost": False,
                "reversible": category in {"read", "write"},
                "sensitive_data": interaction in {"computer_use", "browser"},
                "target_type": interaction or category,
            }
        if tool.category == "command":
            if is_safe_command(content or ""):
                profile.update(side_effect="none", reversible=True)
            elif is_auto_local_command(content or ""):
                profile.update(side_effect="workspace", reversible=True)
            if _NETWORK_PATTERN.search(content or ""):
                profile.update(network=True, target_type="external")
        return profile

    def _is_plan_file(self, target_path: str) -> bool:
        if not self.plan_file_path or not target_path:
            return False
        try:
            target = Path(target_path).expanduser()
            plan = Path(self.plan_file_path).expanduser()
            if not target.is_absolute():
                target = self.sandbox.project_root / target
            if not plan.is_absolute():
                plan = self.sandbox.project_root / plan
            return os.path.normcase(str(target.resolve(strict=False))) == os.path.normcase(str(plan.resolve(strict=False)))
        except (OSError, RuntimeError, ValueError):
            return False
