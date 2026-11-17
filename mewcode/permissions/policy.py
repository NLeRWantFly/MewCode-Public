"""Factory for parent/child permission checkers with bounded write scopes.

The factory centralizes checker construction while keeping the existing
``PermissionChecker`` API usable by callers. Child checkers inherit the
parent's mode, rule engine, task authorization, grant store, and grant maps by
default. Their structured write tools are additionally fenced to the child's
declared writable directories before any rule or grant can allow the write.

This is an application-level path policy. It does not isolate shell commands;
that requires the native execution boundary described by the P2 work.
"""
from __future__ import annotations

import os
import tempfile
from collections.abc import MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from mewcode.permissions.checker import Decision, PermissionChecker
from mewcode.permissions.dangerous import DangerousCommandDetector
from mewcode.permissions.grants import GrantStore
from mewcode.permissions.modes import PermissionMode, mode_decide
from mewcode.permissions.rules import Rule, RuleEngine
from mewcode.permissions.sandbox import PathSandbox

PolicyProfile = Literal["standard", "auditor", "worktree"]


@dataclass(frozen=True)
class PermissionRulePaths:
    """Optional rule files. Unspecified child paths inherit the parent's."""

    user: Path | None = None
    project: Path | None = None
    local: Path | None = None


class _ConstrainedRuleEngine(RuleEngine):
    """Keep inherited rules as hard constraints and layer child rules on top.

    Parent engines are evaluated on every call, so edits to their rule files
    remain effective after a child checker has been created. Explicit deny and
    ask results from either side constrain the other; an allow only survives
    when neither side denies nor asks.
    """

    def __init__(self, parent: RuleEngine, child: RuleEngine) -> None:
        self._parent_engine = parent
        self._child_engine = child
        self._user_path = getattr(child, "_user_path", None) or getattr(parent, "_user_path", None)
        self._project_path = getattr(child, "_project_path", None) or getattr(parent, "_project_path", None)
        self._local_path = getattr(child, "_local_path", None) or getattr(parent, "_local_path", None)

    @property
    def command_rule_issues(self) -> tuple[object, ...]:
        issues: list[object] = []
        for engine in (self._parent_engine, self._child_engine):
            for issue in getattr(engine, "command_rule_issues", ()):
                if issue not in issues:
                    issues.append(issue)
        return tuple(issues)

    def policy_fingerprint(self) -> str:
        from mewcode.permissions.decision_envelope import canonical_digest
        return canonical_digest([engine.policy_fingerprint()
                                 for engine in (self._parent_engine, self._child_engine)])

    def evaluate_for_checker(self, tool_name: str, content: str) -> str | None:
        parent_effect = self._evaluate(self._parent_engine, tool_name, content)
        child_effect = self._evaluate(self._child_engine, tool_name, content)
        if parent_effect == "deny" or child_effect == "deny":
            return "deny"
        if parent_effect == "ask" or child_effect == "ask":
            return "ask"
        if parent_effect == "allow" or child_effect == "allow":
            return "allow"
        return None

    def evaluate(self, tool_name: str, content: str) -> str | None:
        return self.evaluate_for_checker(tool_name, content)

    def evaluate_command_prefix(self, tool_name: str, content: str) -> str | None:
        effects = []
        for engine in (self._parent_engine, self._child_engine):
            evaluate = getattr(engine, "evaluate_command_prefix", None)
            if callable(evaluate):
                effects.append(evaluate(tool_name, content))
        if "deny" in effects:
            return "deny"
        if "ask" in effects:
            return "ask"
        return None

    def matches_deny(self, tool_name: str, content: str) -> bool:
        for engine in (self._parent_engine, self._child_engine):
            matches_deny = getattr(engine, "matches_deny", None)
            if callable(matches_deny) and matches_deny(tool_name, content):
                return True
        return False

    def append_session_rule(self, rule: Rule) -> None:
        self._child_engine.append_session_rule(rule)

    def append_local_rule(self, rule: Rule) -> None:
        self._child_engine.append_local_rule(rule)

    @staticmethod
    def _evaluate(engine: object, tool_name: str, content: str) -> str | None:
        evaluate = getattr(engine, "evaluate_for_checker", None)
        if not callable(evaluate):
            evaluate = getattr(engine, "evaluate", None)
        if callable(evaluate):
            return evaluate(tool_name, content)
        return None


class _PolicyPathSandbox(PathSandbox):
    """PathSandbox whose write roots are set by the permission policy."""

    def __init__(
        self,
        project_root: Path,
        writable_roots: tuple[Path, ...],
        *,
        file_grants: MutableMapping[str, bool],
        dir_grants: MutableMapping[str, bool],
        readonly_roots: tuple[Path, ...] = (),
    ) -> None:
        super().__init__(str(project_root), file_grants=file_grants, dir_grants=dir_grants)
        self._policy_project_root = project_root
        self._allowed_roots = list(writable_roots)
        self._policy_readonly_roots = readonly_roots

    @property
    def project_root(self) -> Path:
        # The first writable root may be the temp directory for auditor agents.
        return self._policy_project_root

    def allows_policy_write(self, path: str) -> bool:
        """Check the declared directory scope without consulting grants.

        Grants authorize only targets within this already-bounded scope. This
        prevents a broad inherited directory grant or a later grant-map update
        from widening a child's declared write roots.
        """
        try:
            candidate = Path(path).expanduser()
            if not candidate.is_absolute():
                candidate = self.project_root / candidate
            resolved = candidate.resolve(strict=False)
            if any(_is_within(resolved, root) for root in self._policy_readonly_roots):
                return False
            return any(_is_within(resolved, root) for root in self._allowed_roots)
        except Exception:
            return False


class _ScopedPermissionChecker(PermissionChecker):
    """Checker enforcing child write scope before rules and grants are read."""

    def check(self, tool, arguments):  # type: ignore[no-untyped-def]
        if tool.category == "write" and self.policy_write_scope_enforced:
            from mewcode.permissions.rules import extract_content

            target = extract_content(tool.name, arguments)
            if not target or not self.sandbox.allows_policy_write(target):
                return Decision(
                    "deny",
                    "Child permission policy blocked a write outside its declared scope",
                    source="child_write_scope",
                    subcondition="write_target_outside_child_scope" if target else "write_target_unverifiable",
                    clearance="request_narrower_parent_approved_writable_scope",
                )
        return super().check(tool, arguments)


def build_permission_checker(
    project_root: str | os.PathLike[str],
    *,
    mode: PermissionMode | str | None = None,
    parent_policy: PermissionChecker | None = None,
    rule_paths: PermissionRulePaths | None = None,
    shared_file_grants: MutableMapping[str, bool] | None = None,
    shared_directory_grants: MutableMapping[str, bool] | None = None,
    writable_scope: Sequence[str | os.PathLike[str]] | None = None,
    readonly_scope: Sequence[str | os.PathLike[str]] = (),
    profile: PolicyProfile = "standard",
    grant_store: GrantStore | None = None,
) -> PermissionChecker:
    """Build one checker and constrain it to the requested writable scope.

    ``standard`` roots may write the project directory at the root level, but
    child policies default to only ``project_root`` (plus any explicit scope).
    ``auditor`` policies default to the system temp directory. ``worktree``
    policies are limited to exactly their project/worktree root.

    ``readonly_scope`` excludes host-owned directories even if they sit inside
    a writable root. Children inherit exclusions; grants cannot override them.

    ``parent_policy`` can be any checker made here or a legacy checker backed
    by ``PathSandbox``. For a legacy checker the sandbox's writable roots and
    writable directory grants become the maximum child scope.
    """
    root = _existing_directory(project_root, label="project root")
    if profile not in {"standard", "auditor", "worktree"}:
        raise ValueError(f"unsupported permission profile: {profile}")

    inherited_mode = getattr(parent_policy, "mode", None)
    parent_mode = _parse_mode(inherited_mode) if inherited_mode is not None else None
    selected_mode = _parse_mode(mode) if mode is not None else parent_mode
    if selected_mode is None:
        selected_mode = PermissionMode.AUTO
    if parent_mode is not None and not _mode_is_narrower(selected_mode, parent_mode):
        raise ValueError("child permission mode cannot be broader than its parent")

    parent_roots = _parent_writable_roots(parent_policy)
    readonly_roots = tuple(dict.fromkeys((
        *getattr(parent_policy, "policy_readonly_roots", ()),
        *(_existing_directory(path, label="read-only directory") for path in readonly_scope),
    )))
    auditor_roots = (_existing_directory(tempfile.gettempdir(), label="temp directory"),)
    if profile == "auditor":
        requested_roots = _canonical_scope(writable_scope) if writable_scope is not None else auditor_roots
        if requested_roots != auditor_roots:
            raise ValueError("auditor profile is writable only in the system temp directory")
    elif writable_scope is not None:
        requested_roots = _canonical_scope(writable_scope)
    elif parent_policy is not None:
        requested_roots = (root,)
    elif profile == "worktree":
        requested_roots = (root,)
    else:
        requested_roots = tuple(dict.fromkeys((root, _existing_directory(tempfile.gettempdir(), label="temp directory"))))

    if profile == "worktree" and requested_roots != (root,):
        raise ValueError("worktree profile must be writable only at its project/worktree root")
    if parent_policy is not None:
        for requested in requested_roots:
            if not any(_is_within(requested, allowed) for allowed in parent_roots):
                raise ValueError(f"child writable scope exceeds parent policy: {requested}")

    inherited_file_grants = getattr(getattr(parent_policy, "sandbox", None), "file_grants", {})
    inherited_dir_grants = getattr(getattr(parent_policy, "sandbox", None), "dir_grants", {})
    file_grants = _shared_grant_map(shared_file_grants, inherited_file_grants)
    dir_grants = _shared_grant_map(shared_directory_grants, inherited_dir_grants)

    engine = _build_rule_engine(parent_policy, rule_paths)
    sandbox = _PolicyPathSandbox(
        root,
        requested_roots,
        file_grants=file_grants,
        dir_grants=dir_grants,
        readonly_roots=readonly_roots,
    )
    selected_store = grant_store or getattr(parent_policy, "grant_store", None)
    checker = _ScopedPermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=sandbox,
        rule_engine=engine,
        mode=selected_mode,
        grant_store=selected_store,
    )

    if parent_policy is not None:
        checker.plan_file_path = parent_policy.plan_file_path
        checker.network_policy_store = getattr(parent_policy, 'network_policy_store', checker.network_policy_store)
        if hasattr(parent_policy, 'network_policy'):
            checker.network_policy = parent_policy.network_policy
        checker.task_authorization = parent_policy.task_authorization
        checker.task_id = parent_policy.task_id

    # Metadata is intentionally attached to the checker so existing callers
    # can pass it directly as ``parent_policy`` without another wrapper type.
    checker.policy_profile = profile
    checker.policy_writable_roots = requested_roots
    checker.policy_readonly_roots = readonly_roots
    # A normal top-level session follows its selected permission mode for
    # external files. Only explicit scopes and delegated contracts are fixed
    # write boundaries; treating every factory caller as a child prevented
    # Auto's reviewer and full access from handling ordinary app settings.
    checker.policy_write_scope_enforced = (
        parent_policy is not None or profile != "standard"
        or writable_scope is not None or bool(readonly_roots)
    )
    # Whole-project executor children share Auto's reviewed host execution.
    # Auditors, excluded paths and narrower write sets still require isolation;
    # a child's existence alone is not a reason to require another runtime.
    checker.requires_command_isolation = (
        getattr(parent_policy, "requires_command_isolation", False)
        or profile == "auditor" or bool(readonly_roots)
        or not any(_is_within(root, allowed) for allowed in requested_roots)
    )
    return checker


def _existing_directory(value: str | os.PathLike[str], *, label: str) -> Path:
    try:
        result = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"{label} must be an existing directory") from exc
    if not result.is_dir():
        raise ValueError(f"{label} must be an existing directory")
    if result == Path(result.anchor):
        raise ValueError(f"{label} cannot be a filesystem root")
    return result


def _canonical_scope(values: Sequence[str | os.PathLike[str]]) -> tuple[Path, ...]:
    if isinstance(values, (str, bytes)) or not values:
        raise ValueError("writable_scope must be a non-empty sequence of directories")
    roots = tuple(dict.fromkeys(_existing_directory(value, label="writable scope") for value in values))
    # A nested root adds no authority and can hide an accidental duplicate.
    return tuple(root for root in roots if not any(root != other and _is_within(root, other) for other in roots))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _parse_mode(value: PermissionMode | str) -> PermissionMode:
    if isinstance(value, PermissionMode):
        return value
    try:
        return PermissionMode(str(value))
    except ValueError as exc:
        raise ValueError(f"unknown permission mode: {value}") from exc


def _mode_is_narrower(child: PermissionMode, parent: PermissionMode) -> bool:
    if parent is PermissionMode.AUTO:
        return child in {PermissionMode.AUTO, PermissionMode.PLAN}
    if parent is PermissionMode.STRICT_ISOLATION:
        return child in {PermissionMode.STRICT_ISOLATION, PermissionMode.PLAN}

    rank = {"deny": 0, "ask": 1, "review": 1, "allow": 2}
    for category in ("read", "write", "command", "harness"):
        if rank[mode_decide(child, category)] > rank[mode_decide(parent, category)]:
            return False
    return True


def _parent_writable_roots(parent: PermissionChecker | None) -> tuple[Path, ...]:
    if parent is None:
        return ()
    explicit = getattr(parent, "policy_writable_roots", None)
    if explicit is not None:
        return tuple(Path(path).resolve() for path in explicit)
    sandbox = parent.sandbox
    roots = list(getattr(sandbox, "_allowed_roots", (sandbox.project_root,)))
    for raw, writable in getattr(sandbox, "dir_grants", {}).items():
        if writable:
            try:
                roots.append(Path(raw).resolve(strict=False))
            except (OSError, RuntimeError, ValueError):
                continue
    normalized = []
    for item in roots:
        try:
            path = Path(item).resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            continue
        if path not in normalized:
            normalized.append(path)
    return tuple(normalized)


def _build_rule_engine(
    parent: PermissionChecker | None,
    paths: PermissionRulePaths | None,
) -> RuleEngine:
    if parent is None:
        return RuleEngine(
            user_rules_path=paths.user if paths else None,
            project_rules_path=paths.project if paths else None,
            local_rules_path=paths.local if paths else None,
        )
    parent_engine = parent.rule_engine
    if paths is None:
        return parent_engine
    engine = RuleEngine(
        user_rules_path=paths.user or getattr(parent_engine, "_user_path", None),
        project_rules_path=paths.project or getattr(parent_engine, "_project_path", None),
        local_rules_path=paths.local or getattr(parent_engine, "_local_path", None),
    )
    return _ConstrainedRuleEngine(parent_engine, engine)


def _shared_grant_map(
    explicit: MutableMapping[str, bool] | None,
    inherited: MutableMapping[str, bool] | dict[str, bool],
) -> MutableMapping[str, bool]:
    if explicit is None:
        return inherited
    if explicit is inherited:
        return explicit
    for key, value in inherited.items():
        explicit.setdefault(key, value)
    return explicit


__all__ = ["PermissionRulePaths", "PolicyProfile", "build_permission_checker"]
