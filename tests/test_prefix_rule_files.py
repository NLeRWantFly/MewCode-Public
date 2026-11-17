"""Integration tests for non-executable Codex-style command rule files."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mewcode.permissions import (
    DangerousCommandDetector,
    PathSandbox,
    PermissionChecker,
    PermissionMode,
    Rule,
    RuleEngine,
)
from mewcode.permissions.prefix_rule_files import load_prefix_rule_files
from mewcode.tools.bash import Bash

_HOST_SHELL = "powershell" if os.name == "nt" else "bash"


def _rule_file(root: Path, body: str) -> Path:
    directory = root / ".mewcode" / "rules"
    directory.mkdir(parents=True)
    path = directory / "default.rules"
    path.write_text(body, encoding="utf-8")
    return path


def _checker(root: Path, rules: RuleEngine, mode: PermissionMode) -> PermissionChecker:
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(root)), rules, mode)
    checker.set_task_authorization(None, task_id="prefix-rule-tests")
    return checker


def _unattested_native_boundary(monkeypatch, checker: PermissionChecker) -> None:
    checker.requires_command_isolation = True
    monkeypatch.setattr("mewcode.sandbox.windows.native_runtime_ready", lambda: False)
    monkeypatch.setattr("mewcode.sandbox.native_recovery.recover_native_runtime", lambda: None)


def test_project_prefix_rule_prompts_with_argument_boundaries(tmp_path: Path) -> None:
    _rule_file(tmp_path, f'''
prefix_rule(
    pattern=["git", "status"], decision="prompt", shell="{_HOST_SHELL}",
    match=["git status --short"], not_match=["git statusx"],
)
''')
    engine = RuleEngine(project_rules_path=tmp_path / ".mewcode" / "permissions.yaml")
    assert engine.evaluate("Bash", "git status --short") == "ask"
    assert engine.evaluate("Bash", "git statusx") is None
    assert not engine.command_rule_issues


def test_prefix_prompt_precedes_safe_command_fastpath(tmp_path: Path) -> None:
    _rule_file(tmp_path, f'''
prefix_rule(
    pattern=["git", "status"], decision="prompt", shell="{_HOST_SHELL}",
    match=["git status --short"], not_match=["git statusx"],
)
''')
    rules = RuleEngine(project_rules_path=tmp_path / ".mewcode" / "permissions.yaml")
    checker = _checker(tmp_path, rules, PermissionMode.DEFAULT)
    result = checker.check(Bash(), {"command": "git status --short"})
    assert (result.effect, result.source) == ("ask", "command_prefix_rule")


def test_prefix_rules_cannot_open_unattested_auto_boundary(tmp_path: Path, monkeypatch) -> None:
    _rule_file(tmp_path, f'''
prefix_rule(
    pattern=["python", "-m", "pytest"], decision="allow", shell="{_HOST_SHELL}",
    match=["python -m pytest -q"], not_match=["python -m unittest"],
)
''')
    rules = RuleEngine(project_rules_path=tmp_path / ".mewcode" / "permissions.yaml")
    checker = _checker(tmp_path, rules, PermissionMode.AUTO)
    _unattested_native_boundary(monkeypatch, checker)
    result = checker.check(Bash(), {"command": "python -m pytest -q"})
    assert (result.effect, result.source) == ("deny", "native_boundary_unverified")


def test_rule_files_are_never_executed_and_fail_with_source_location(tmp_path: Path) -> None:
    marker = tmp_path / "unsafe-side-effect"
    path = _rule_file(tmp_path, f'''\n__import__("pathlib").Path({str(marker)!r}).write_text("unsafe")\n''')
    result = load_prefix_rule_files(path.parent)
    assert not marker.exists()
    assert not result.engine.rules
    assert len(result.issues) == 1
    assert (result.issues[0].source, result.issues[0].line) == (str(path), 2)


def test_failed_rule_self_test_is_inactive(tmp_path: Path, caplog) -> None:
    path = _rule_file(tmp_path, '''
prefix_rule(
    pattern=["git", "push"], decision="forbidden", shell="bash",
    match=["git push origin main"], not_match=["git push origin backup"],
)
''')
    result = load_prefix_rule_files(path.parent)
    assert not result.engine.rules
    assert result.issues[0].source == str(path)
    assert "not_match self-test failed" in result.issues[0].message
    engine = RuleEngine(project_rules_path=tmp_path / ".mewcode" / "permissions.yaml")
    assert engine.evaluate("Bash", "git push origin main") == "ask"
    assert f"{path}:2" in caplog.text
    assert "git push origin backup" not in caplog.text


def test_restrictive_prefix_result_wins_over_legacy_allow(tmp_path: Path) -> None:
    _rule_file(tmp_path, f'''
prefix_rule(
    pattern=["git", "push"], decision="forbidden", shell="{_HOST_SHELL}",
    match=["git push origin main"], not_match=["git pushx origin main"],
)
''')
    legacy = tmp_path / ".mewcode" / "permissions.yaml"
    legacy.write_text('- rule: "Bash(git *)"\n  effect: allow\n', encoding="utf-8")
    engine = RuleEngine(project_rules_path=legacy)
    assert engine.matches_deny("Bash", "git push origin main")
    assert engine.evaluate("Bash", "git push origin main") == "deny"
    assert engine.evaluate_for_checker("Bash", "git push origin main") == "deny"


def test_legacy_command_wildcard_cannot_authorize_compound_or_redirection() -> None:
    allow = Rule("Bash", "git *", "allow")
    assert allow.matches("Bash", "git status")
    assert not allow.matches("Bash", "git status; git push origin main")
    assert not allow.matches("Bash", "git status | git push origin main")
    assert not allow.matches("Bash", "git status > result.txt")
    assert not allow.matches("Bash", "git status; Remove-Item private.txt")
    assert not Rule("Bash", "git status*", "allow").matches("Bash", "git statusx")


def test_legacy_command_deny_matches_later_segment_without_glob() -> None:
    deny = Rule("Bash", "git push*", "deny")
    assert deny.matches("Bash", "git status; git push origin main")
    assert not deny.matches("Bash", "git pushx origin main")


def test_unreadable_rule_directory_cannot_turn_into_command_allow(tmp_path: Path, monkeypatch) -> None:
    from mewcode.permissions import prefix_rule_files

    directory = tmp_path / ".mewcode" / "rules"
    directory.mkdir(parents=True)

    def refuse(_path):
        raise PermissionError("synthetic permission failure")

    monkeypatch.setattr(prefix_rule_files.os, "scandir", refuse)
    engine = RuleEngine(project_rules_path=tmp_path / ".mewcode" / "permissions.yaml")
    assert engine.evaluate_for_checker("Bash", "git status") == "ask"
    assert engine.command_rule_issues[0].source == str(directory)


def test_prefix_allow_cannot_override_auto_hard_deny(tmp_path: Path) -> None:
    _rule_file(tmp_path, f'''
prefix_rule(
    pattern=["rm", "-rf"], decision="allow", shell="{_HOST_SHELL}",
    match=["rm -rf /"], not_match=["rm -rf /tmp"],
)
''')
    rules = RuleEngine(project_rules_path=tmp_path / ".mewcode" / "permissions.yaml")
    checker = _checker(tmp_path, rules, PermissionMode.AUTO)
    result = checker.check(Bash(), {"command": "rm -rf /"})
    assert (result.effect, result.source) == ("deny", "invariant_guardrail")


def test_literal_rule_file_alternatives_match_each_position_and_keep_compound_deny(tmp_path: Path) -> None:
    _rule_file(tmp_path, f'''
prefix_rule(
    pattern=["git", ["push", "fetch"]], decision="forbidden", shell="{_HOST_SHELL}",
    match=["git push origin main", "git fetch origin"], not_match=["git pushx origin main"],
)
''')
    loaded = load_prefix_rule_files(tmp_path / ".mewcode" / "rules")

    assert not loaded.issues
    assert loaded.engine.evaluate("git push origin main", _HOST_SHELL).decision == "deny"
    assert loaded.engine.evaluate("git fetch origin", _HOST_SHELL).decision == "deny"
    compound = loaded.engine.evaluate(
        "echo visible; git fetch origin; python -c 'hidden'", _HOST_SHELL,
    )
    assert compound.decision == "deny"
    assert compound.reason == "forbidden prefix in opaque command"


@pytest.mark.parametrize(
    "pattern_source",
    [
        '["git", []]',
        '["git", ["push", ""]]',
        '["git", [["push", "fetch"]]]',
        '["git", 7]',
    ],
)
def test_rule_file_rejects_empty_or_nested_alternatives(tmp_path: Path, pattern_source: str) -> None:
    _rule_file(tmp_path, f'''
prefix_rule(
    pattern={pattern_source}, decision="allow", shell="{_HOST_SHELL}",
    match=["git push origin main"], not_match=["git pushx origin main"],
)
''')
    loaded = load_prefix_rule_files(tmp_path / ".mewcode" / "rules")

    assert not loaded.engine.rules
    assert len(loaded.issues) == 1
    assert loaded.issues[0].line == 2


def test_rule_file_rejects_more_than_128_alternatives(tmp_path: Path) -> None:
    alternatives = ", ".join(repr(f"verb-{index}") for index in range(129))
    _rule_file(tmp_path, f'''
prefix_rule(
    pattern=["git", [{alternatives}]], decision="allow", shell="{_HOST_SHELL}",
    match=["git verb-0 origin"], not_match=["git pushx origin main"],
)
''')
    loaded = load_prefix_rule_files(tmp_path / ".mewcode" / "rules")

    assert not loaded.engine.rules
    assert len(loaded.issues) == 1
    assert "at most 128 alternatives" in loaded.issues[0].message


def test_rule_file_still_rejects_host_executable_field(tmp_path: Path) -> None:
    _rule_file(tmp_path, f'''
prefix_rule(
    pattern=["git", ["push", "fetch"]], host_executable="git", decision="allow",
    shell="{_HOST_SHELL}", match=["git push origin main"], not_match=["git pushx"],
)
''')
    loaded = load_prefix_rule_files(tmp_path / ".mewcode" / "rules")

    assert not loaded.engine.rules
    assert len(loaded.issues) == 1
    assert "unsupported or expanded" in loaded.issues[0].message


def test_rule_file_alternative_expressions_are_never_executed(tmp_path: Path) -> None:
    marker = tmp_path / "unsafe-side-effect"
    path = _rule_file(tmp_path, f'''
prefix_rule(
    pattern=[__import__("pathlib").Path({str(marker)!r}).write_text("executed")],
    decision="allow", shell="{_HOST_SHELL}",
    match=["git push origin main"], not_match=["git pushx origin main"],
)
''')
    loaded = load_prefix_rule_files(path.parent)

    assert not marker.exists()
    assert not loaded.engine.rules
    assert len(loaded.issues) == 1
