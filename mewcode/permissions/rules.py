from __future__ import annotations

import os
import logging
import re
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Literal

import yaml

Effect = Literal["allow", "deny"]
CommandEffect = Literal["allow", "ask", "deny"]

_RULE_RE = re.compile(r"^(\w+)\((.+)\)$")
_COMMAND_RULE_TOOLS = frozenset({"Bash", "RunVerification", "TerminalStart", "TerminalInput"})
_LOGGER = logging.getLogger(__name__)


def _policy_file_fingerprint(path: Path) -> dict[str, Any]:
    import hashlib
    try:
        stat = path.stat()
        if stat.st_size > 128 * 1024:
            return {"size": stat.st_size, "modified": stat.st_mtime_ns, "oversized": True}
        return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "resolved": str(path.resolve())}
    except FileNotFoundError:
        return {"absent": True}
    except OSError as exc:
        return {"error": type(exc).__name__}

_CONTENT_FIELDS: dict[str, str] = {
    "Bash": "command",
    "TerminalStart": "command",
    "TerminalInput": "text",
    "RunVerification": "command",
    "ReadFile": "file_path",
    "WriteFile": "file_path",
    "EditFile": "file_path",
    "ReportArtifact": "file_path",
    "Glob": "pattern",
    "Grep": "pattern",
}


@dataclass(frozen=True)
class Rule:
    tool_name: str
    pattern: str
    effect: Effect


    def matches(self, tool_name: str, content: str) -> bool:
        if self.tool_name != tool_name:
            return False
        if tool_name in _COMMAND_RULE_TOOLS:
            return _legacy_command_rule_matches(self.pattern, content, self.effect)
        return fnmatch(content, self.pattern)


def _legacy_command_rule_matches(pattern: str, content: str, effect: Effect) -> bool:
    """Conservatively interpret legacy command rules as argv prefixes.

    The only supported wildcard is a single trailing ``*``.  A deny applies
    if *any* parsed segment has that prefix; an allow requires *every*
    segment to have it and forbids opaque syntax and redirection.  Unsupported
    wildcard forms are inactive until the user migrates them to ``*.rules``.
    """
    from mewcode.permissions.command_rules import parse_command

    if pattern == "*":
        return effect == "deny" and bool(content.strip())
    if "*" not in pattern:
        # An exact user-authored command grant retains its old meaning.  AUTO
        # still enforces the native runtime gate before this can authorize it.
        return pattern == content
    if pattern.count("*") != 1 or not pattern.endswith("*") or any(
        char in pattern for char in "?["
    ):
        return False
    shell = "powershell" if os.name == "nt" else "bash"
    prefix = parse_command(pattern[:-1].rstrip(), shell)
    command = parse_command(content, shell)
    if (not prefix.safe_for_prefix_rules or len(prefix.segments) != 1
            or not command.parseable or not prefix.segments[0].arguments):
        return False
    if effect == "allow" and (len(command.segments) != 1
                              or not command.safe_for_prefix_rules or any(
                                  segment.redirections for segment in command.segments
                              )):
        return False
    expected = prefix.segments[0].arguments

    def matches_segment(arguments: tuple[str, ...]) -> bool:
        if len(arguments) < len(expected):
            return False
        actual = arguments[:len(expected)]
        if shell == "powershell":
            return tuple(value.casefold() for value in actual) == tuple(
                value.casefold() for value in expected
            )
        return actual == expected

    hits = [matches_segment(segment.arguments) for segment in command.segments]
    return all(hits) if effect == "allow" else any(hits)


def parse_rule(raw: str, effect: Effect) -> Rule:
    m = _RULE_RE.match(raw.strip())
    if not m:
        raise ValueError(f"无效的规则语法: {raw}")
    return Rule(tool_name=m.group(1), pattern=m.group(2), effect=effect)


def extract_content(tool_name: str, arguments: dict[str, Any]) -> str:
    if tool_name == "PreparePythonEnvironment":
        return ".venv"  # Fixed target, including calls which omit all optional arguments.
    field = _CONTENT_FIELDS.get(tool_name)
    if field is None:
        return ""
    return str(arguments.get(field, ""))


def _load_rules_file(path: Path) -> list[Rule]:
    if not path.is_file():
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return []
    if not isinstance(raw, list):
        return []
    rules: list[Rule] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        rule_str = entry.get("rule", "")
        effect = entry.get("effect", "")
        if effect not in ("allow", "deny"):
            continue
        try:
            rules.append(parse_rule(rule_str, effect))
        except ValueError:
            continue
    return rules


class RuleEngine:


    def __init__(
        self,
        user_rules_path: Path | None = None,
        project_rules_path: Path | None = None,
        local_rules_path: Path | None = None,
    ) -> None:
        self._user_path = user_rules_path
        self._project_path = project_rules_path
        self._local_path = local_rules_path
        # In-memory grants for the current agent/session (HITL allow, etc.).
        self._session_rules: list[Rule] = []
        self.command_rule_issues = ()
        self._reported_command_rule_issues: set[tuple[str, int, str]] = set()
        self._load_command_prefix_rules()

    def _load_tiers(self) -> list[list[Rule]]:
        tiers: list[list[Rule]] = []
        for p in (self._user_path, self._project_path, self._local_path):
            tiers.append(_load_rules_file(p) if p else [])
        return tiers

    def policy_fingerprint(self) -> str:
        """Bind pending reviews to rule contents, including newly added files."""
        import hashlib
        import json

        files, directories = {}, set()
        for path in (self._user_path, self._project_path, self._local_path):
            if path is not None:
                files[str(path)] = _policy_file_fingerprint(path)
                directories.add(path.parent / "rules")
        for directory in sorted(directories, key=str):
            try:
                for path in sorted(directory.glob("*.rules")):
                    files[str(path)] = _policy_file_fingerprint(path)
            except OSError as exc:
                files[str(directory)] = type(exc).__name__
        payload = [files, [(r.tool_name, r.pattern, r.effect) for r in self._session_rules]]
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def _load_command_prefix_rules(self):
        """Load user/project ``*.rules`` without executing their contents.

        The directories are siblings of the configured legacy YAML files, so
        an explicit rule-path configuration also selects its prefix rules.
        Absence of a configured path means no implicit home-directory lookup.
        """
        from mewcode.permissions.prefix_rule_files import load_prefix_rule_files

        directories = []
        for path in (self._user_path, self._project_path, self._local_path):
            if path is not None and path.parent / "rules" not in directories:
                directories.append(path.parent / "rules")
        result = load_prefix_rule_files(*directories)
        self.command_rule_issues = result.issues
        for issue in result.issues:
            key = (issue.source, issue.line, issue.message)
            if key not in self._reported_command_rule_issues:
                # Self-test details can contain user-authored command text.
                # Log the rule position and category, not the example itself.
                _LOGGER.warning(
                    "Inactive command rule at %s:%d: %s",
                    issue.source, issue.line, issue.message.partition(":")[0],
                )
                self._reported_command_rule_issues.add(key)
        return result.engine

    def evaluate_command_prefix(self, tool_name: str, content: str) -> CommandEffect | None:
        if tool_name not in {"Bash", "RunVerification", "TerminalStart"}:
            return None
        engine = self._load_command_prefix_rules()
        if not engine.rules and not self.command_rule_issues:
            return None
        shell = "powershell" if os.name == "nt" else "bash"
        decision = engine.evaluate(content, shell).decision if engine.rules else None
        # A malformed or unreadable policy file may have contained a deny.
        # Never treat its absence as permission to take the fast path.
        if self.command_rule_issues and decision != "deny":
            return "ask"
        return decision


    def evaluate(self, tool_name: str, content: str) -> CommandEffect | None:
        prefix = self.evaluate_command_prefix(tool_name, content)
        if prefix in {"deny", "ask"}:
            return prefix
        for rule in reversed(self._session_rules):
            if rule.matches(tool_name, content):
                return rule.effect
        for rules in self._load_tiers():
            for rule in reversed(rules):
                if rule.matches(tool_name, content):
                    return rule.effect
        return prefix

    def evaluate_for_checker(self, tool_name: str, content: str) -> CommandEffect | None:
        """Evaluate one call with hard-deny precedence and one rule-file read."""
        prefix = self.evaluate_command_prefix(tool_name, content)
        tiers = self._load_tiers()
        all_rules = [self._session_rules, *tiers]
        if prefix == "deny" or any(
            rule.effect == "deny" and rule.matches(tool_name, content)
            for rules in all_rules for rule in rules
        ):
            return "deny"
        if prefix == "ask":
            return "ask"
        for rules in all_rules:
            for rule in reversed(rules):
                if rule.matches(tool_name, content):
                    return rule.effect
        return prefix

    def matches_deny(self, tool_name: str, content: str) -> bool:
        """A matching deny is a host boundary, never shadowed by an allow tier."""
        if self.evaluate_command_prefix(tool_name, content) == "deny":
            return True
        return any(
            rule.effect == "deny" and rule.matches(tool_name, content)
            for rules in [self._session_rules, *self._load_tiers()]
            for rule in rules
        )

    def append_session_rule(self, rule: Rule) -> None:
        """Remember an allow/deny for this process/session only."""
        self._session_rules.append(rule)

    def append_local_rule(self, rule: Rule) -> None:
        # Always apply in-session so HITL grants work without a YAML path
        # (expert agents often construct RuleEngine() with no local file).
        self.append_session_rule(rule)
        if self._local_path is None:
            return
        self._local_path.parent.mkdir(parents=True, exist_ok=True)
        existing = _load_rules_file(self._local_path)
        existing.append(rule)
        entries = [{"rule": f"{r.tool_name}({r.pattern})", "effect": r.effect} for r in existing]
        self._local_path.write_text(yaml.dump(entries, allow_unicode=True), encoding="utf-8")
