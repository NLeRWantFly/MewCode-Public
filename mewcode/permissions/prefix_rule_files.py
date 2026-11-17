"""Read a deliberately small, non-executable subset of Codex ``*.rules``.

Only literal ``prefix_rule(...)`` calls are accepted.  In particular, a rule
file is never executed as Python or Starlark.  Invalid files/rules remain
inactive and expose their source location for a user-facing diagnostic.
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass
from pathlib import Path

from mewcode.permissions.command_rules import (
    PrefixRule,
    PrefixRuleEngine,
    prefix_rule_decision,
)

_ALLOWED_FIELDS = frozenset({"pattern", "decision", "match", "not_match", "shell", "justification"})
_MAX_RULE_FILE_BYTES = 128 * 1024
_MAX_PREFIX_PATTERN_LENGTH = 128
_MAX_PREFIX_ALTERNATIVES = 128


@dataclass(frozen=True)
class RuleFileIssue:
    source: str
    line: int
    message: str


@dataclass(frozen=True)
class LoadedPrefixRules:
    engine: PrefixRuleEngine
    issues: tuple[RuleFileIssue, ...]


def _strings(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ValueError(f"{field} must be a non-empty list of strings")
    return tuple(value)


def _pattern(value: object) -> tuple[str | tuple[str, ...], ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("pattern must be a non-empty list or tuple")
    if len(value) > _MAX_PREFIX_PATTERN_LENGTH:
        raise ValueError(f"pattern must contain at most {_MAX_PREFIX_PATTERN_LENGTH} positions")

    positions: list[str | tuple[str, ...]] = []
    for index, item in enumerate(value):
        if isinstance(item, str):
            if not item:
                raise ValueError(f"pattern position {index + 1} must not be empty")
            positions.append(item)
            continue
        if not isinstance(item, (list, tuple)) or not item:
            raise ValueError(f"pattern position {index + 1} must be a non-empty string or list/tuple of strings")
        if len(item) > _MAX_PREFIX_ALTERNATIVES:
            raise ValueError(
                f"pattern position {index + 1} must have at most {_MAX_PREFIX_ALTERNATIVES} alternatives"
            )
        if any(not isinstance(alternative, str) or not alternative for alternative in item):
            raise ValueError(f"pattern position {index + 1} alternatives must be non-empty strings")
        positions.append(tuple(item))
    return tuple(positions)


def _load_file(path: Path, engine: PrefixRuleEngine, issues: list[RuleFileIssue]) -> None:
    try:
        if path.stat().st_size > _MAX_RULE_FILE_BYTES:
            raise ValueError("rule file exceeds 128 KiB")
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path), mode="exec")
    except (OSError, UnicodeError, SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        issues.append(RuleFileIssue(str(path), getattr(exc, "lineno", 1) or 1, str(exc)))
        return

    for statement in tree.body:
        line = statement.lineno
        call = statement.value if isinstance(statement, ast.Expr) else None
        if (not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name)
                or call.func.id != "prefix_rule" or call.args):
            issues.append(RuleFileIssue(str(path), line, "only literal prefix_rule(...) calls are supported"))
            continue
        try:
            if any(keyword.arg is None or keyword.arg not in _ALLOWED_FIELDS for keyword in call.keywords):
                raise ValueError("unsupported or expanded prefix_rule field")
            fields = {keyword.arg: ast.literal_eval(keyword.value) for keyword in call.keywords}
            if len(fields) != len(call.keywords):
                raise ValueError("duplicate prefix_rule field")
            if "justification" in fields and not isinstance(fields["justification"], str):
                raise ValueError("justification must be a string")
            decision = fields.get("decision", "allow")
            if not isinstance(decision, str):
                raise ValueError("decision must be a string")
            shell = fields.get("shell", "powershell" if os.name == "nt" else "bash")
            if shell not in {"powershell", "bash"}:
                raise ValueError("shell must be powershell or bash")
            rule = PrefixRule(
                pattern=_pattern(fields.get("pattern")),
                decision=prefix_rule_decision(decision),
                match=_strings(fields.get("match"), "match"),
                not_match=_strings(fields.get("not_match"), "not_match"),
                shell=shell,
                source=str(path),
                line=line,
            )
            registration = engine.register(rule)
            for error in registration.errors:
                issues.append(RuleFileIssue(str(path), line, error))
        except (ValueError, TypeError, SyntaxError, RecursionError, MemoryError) as exc:
            issues.append(RuleFileIssue(str(path), line, str(exc)))


def load_prefix_rule_files(*directories: Path | None) -> LoadedPrefixRules:
    """Load explicitly configured rule directories, preserving file provenance."""
    engine = PrefixRuleEngine()
    issues: list[RuleFileIssue] = []
    for directory in directories:
        if directory is None:
            continue
        try:
            with os.scandir(directory) as entries:
                files = sorted(
                    (Path(entry.path) for entry in entries if entry.name.endswith(".rules")),
                    key=lambda path: path.name,
                )
        except FileNotFoundError:
            continue
        except OSError as exc:
            issues.append(RuleFileIssue(str(directory), 1, str(exc)))
            continue
        for path in files:
            if path.is_symlink():
                issues.append(RuleFileIssue(str(path), 1, "symlinked rule files are not loaded"))
                continue
            _load_file(path, engine, issues)
    return LoadedPrefixRules(engine, tuple(issues))
