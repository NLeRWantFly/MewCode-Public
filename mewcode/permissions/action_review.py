"""Host-owned action bindings shared by chat, graph and maintenance agents.

An approval is about one operation in one live execution environment. These
bindings are not execution receipts: they never claim a process was started
or that a requested sandbox was successfully enforced.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any

from mewcode.permissions.decision_envelope import canonical_digest
from mewcode.permissions.modes import PermissionMode


COMMAND_TOOLS = frozenset({"Bash", "RunVerification", "TerminalStart", "TerminalInput"})


def execution_environment(agent, tool_name: str, arguments: dict | None = None) -> dict[str, Any]:
    checker = agent.permission_checker
    sandbox = getattr(checker, "sandbox", None)
    mode = getattr(checker, "mode", agent.permission_mode)
    command = tool_name in COMMAND_TOOLS
    from mewcode.permissions.network_policy import policy_for
    network = policy_for(checker)
    backend = ("windows_network_required" if command and agent._uses_native_commands(arguments) and network.enabled else
               "windows_native_required" if command and agent._uses_native_commands(arguments) else
               "strict_container_required" if command and mode == PermissionMode.STRICT_ISOLATION else
               "host_process" if command else "structured_tool")
    writable = getattr(checker, "policy_writable_roots", getattr(sandbox, "_allowed_roots", ()))
    readonly = getattr(checker, "policy_readonly_roots", getattr(sandbox, "_read_only_roots", ()))
    scope = getattr(checker, "task_authorization", None)
    from mewcode.experts.node_expansion import ExpansionPathSandbox
    scoped_writes = (getattr(checker, "policy_write_scope_enforced", False)
                     or isinstance(sandbox, ExpansionPathSandbox))
    environment = {
        "schema": "mewcode.execution-policy.v1",
        "phase": "before_dispatch",
        "backend_policy": backend,
        "permission_mode": getattr(mode, "value", str(mode)),
        "agent_permission_mode": agent.permission_mode.value,
        "cwd": str(Path(agent.work_dir).resolve()),
        "path_policy_applies_to": "structured_file_tools",
        "external_file_write_policy": (
            "declared_scope_only" if scoped_writes else
            "auto_review_exact_file" if mode == PermissionMode.AUTO else
            "ordinary_files_allowed" if mode == PermissionMode.BYPASS else
            "explicit_authorization_required"
        ),
        "writable_roots": sorted(map(str, writable)),
        "readonly_roots": sorted(map(str, readonly)),
        "directory_grants": dict(getattr(sandbox, "dir_grants", {})),
        "file_grants": dict(getattr(sandbox, "file_grants", {})),
        "command_isolation_required": bool(getattr(checker, "requires_command_isolation", False)),
        "network_settings": network.as_dict(),
        "network_policy": ("enabled_except_blacklist" if backend == 'windows_network_required' else
                           "disabled_by_required_backend" if backend in {
            "windows_native_required", "strict_container_required"} else "no_process_network_isolation_claim"),
        "task_scope": scope.as_dict() if scope is not None else None,
        "runtime_enforcement": "host_rechecks_at_dispatch",
        "execution_confirmed": False,
    }
    if backend == 'host_process' and tool_name == 'Bash':
        if (arguments or {}).get('network') == 'proxy':
            environment['network_transport'] = {
                'route': 'host_injected_proxy',
                'targets': list((arguments or {}).get('proxy_targets') or []),
                'direct_sockets': 'not_os_isolated',
                'evidence': 'Host records authenticated CONNECT destinations and tunnel byte counts.',
                'client_setup': 'Use the injected proxy with TLS verification; never fall back to direct upload.',
            }
        from mewcode.permissions.command_sources import host_python_lookup
        lookup = host_python_lookup((arguments or {}).get('command'), agent.work_dir)
        if lookup is not None:
            environment['host_python_lookup'] = lookup
    if backend == 'windows_network_required':
        environment['network_transport'] = {
            'route': 'host_injected_proxy', 'direct_sockets': 'denied',
            'client_setup': 'Preserve HTTP_PROXY/HTTPS_PROXY; urllib default opener or ProxyHandler(); requests trust_env=True.',
            'note': 'ProxyHandler({}) disables the proxy and fails with WinError 10013; network enabled does not allow direct sockets.',
        }
    if backend in {"windows_native_required", "windows_network_required"} and tool_name in {"Bash", "RunVerification"}:
        # This is a prospective, exact-operation binding, not a receipt. Staging
        # and process launch happen only after an independent action review.
        environment["command_dispatch_plan"] = {
            "operation_digest": canonical_digest([tool_name, arguments or {}, environment["cwd"],
                getattr(checker, "task_id", "")]),
            "tool": tool_name, "task_id": getattr(checker, "task_id", ""),
            "workspace": environment["cwd"],
            "publication": "discard" if (arguments or {}).get("publication") == "discard"
                or getattr(checker, "native_publication_mode", "publish") == "discard" else "publish",
            "enforcement": 'srt_command_then_execute_staged' if network.enabled else "native_auto_command_then_execute_staged",
            "preparation_phase": "after_review_before_process_start",
            "required_checks": ["current_task_scope", "enabled_native_gate", "isolated_workspace",
                "frozen_runtime_dependencies", *(['srt_identity', 'wfp_egress', 'network_blacklist']
                    if network.enabled else ['lpac_identity', 'network_disabled']), "verified_cleanup"],
            "host_shell_fallback": False, "transaction_created": False,
        }
    return environment


def _trusted_context(agent) -> str:
    reviewer = agent.safety_reviewer
    reader = getattr(reviewer, "_conversation_context", None)
    if callable(reader):
        return reader(getattr(agent, "_current_conversation", None))
    conversation = getattr(agent, "_current_conversation", None)
    return canonical_digest([getattr(reviewer, "authorization_context", ""),
        getattr(reviewer, "authorization_version", ""),
        [m.content for m in getattr(conversation, "history", ()) if m.role == "user"
         and not m.tool_results and not getattr(m, "transient", False)]])


def _state(agent, tool_name, arguments, environment):
    checker = agent.permission_checker
    scope = getattr(checker, "task_authorization", None)
    rules = getattr(checker, "rule_engine", None)
    fingerprint = getattr(rules, "policy_fingerprint", None)
    target_reader = getattr(agent.safety_reviewer, "_readonly_action_facts", None)
    target = target_reader(tool_name, arguments) if callable(target_reader) else {}
    call_key = getattr(checker, "_call_key", None)
    tool = agent.registry.get(tool_name)
    return {
        "tool": tool_name, "arguments": arguments,
        "environment": environment, "authorization": _trusted_context(agent),
        "task_id": getattr(checker, "task_id", ""),
        "task_scope_expired": scope is not None and time.time() >= scope.expires_at,
        "rules": fingerprint() if callable(fingerprint) else None,
        "target": target,
        "structured_target_version": call_key(tool_name, arguments) if callable(call_key) else None,
        "tool_enabled": tool is not None and agent.registry.is_enabled(tool_name),
        "tool_identity": id(tool), "checker_identity": id(checker),
        "review_workspace_writes": bool(getattr(checker, "review_workspace_writes", False)),
    }


@dataclass(frozen=True)
class ActionReviewBinding:
    state_digest: str
    environment: dict[str, Any]
    # Applied only to a model allow. A queued request for human authority must
    # survive independent progress; its retry is always reviewed afresh.
    effect_digest: str = ""

    @classmethod
    def capture(cls, agent, tool_name: str, arguments: dict) -> "ActionReviewBinding":
        environment = execution_environment(agent, tool_name, arguments)
        return cls(canonical_digest(_state(agent, tool_name, arguments, environment)), copy.deepcopy(environment))

    def is_current(self, agent, tool_name: str, arguments: dict) -> bool:
        current = self.capture(agent, tool_name, arguments)
        if (self.state_digest, self.environment) != (current.state_digest, current.environment):
            return False
        if self.effect_digest:
            recovery = getattr(agent, "_review_recovery", None)
            return recovery is not None and self.effect_digest == canonical_digest(recovery.effect_context())
        return True


def stale_review_result(binding: ActionReviewBinding):
    from mewcode.permissions.reviewer import SafetyReviewResult
    return SafetyReviewResult(decision="deny", risk="unknown", denial_kind="insufficient_evidence",
        reason="AUTO_REVIEW_CONTEXT_CHANGED: 审查期间操作、授权、目标文件或执行环境已变化，旧结论已丢弃。",
        alternative="读取最新状态后重新提交所需操作；这不是缺少用户授权，不要申请扩大权限。",
        review_route={"status": "stale", "action_binding": binding.state_digest})


def stale_tool_result(binding: ActionReviewBinding):
    from mewcode.tools.base import ToolResult
    from mewcode.experts.command_receipts import execution_observation
    review = stale_review_result(binding)
    return ToolResult(review.reason + "\n" + review.alternative, is_error=True,
        host_observation={"operation_blocked": True,
                          "command_execution": execution_observation("denied", reason=review.reason)})
