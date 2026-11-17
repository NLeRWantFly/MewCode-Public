from __future__ import annotations

import pytest

from mewcode.permissions.command_rules import (
    PrefixRule,
    PrefixRuleEngine,
    parse_command,
    prefix_rule_decision,
    validate_prefix_rule,
)


def test_powershell_pipeline_and_bash_statement_split_into_argv_segments() -> None:
    powershell = parse_command(
        "Get-ChildItem -LiteralPath reports | Select-Object Name",
        "powershell",
    )
    assert powershell.safe_for_prefix_rules
    assert [segment.arguments for segment in powershell.segments] == [
        ("Get-ChildItem", "-LiteralPath", "reports"),
        ("Select-Object", "Name"),
    ]
    assert powershell.segments[1].separator_before == "|"

    bash = parse_command("python a.py; Remove-Item b.txt", "bash")
    assert bash.safe_for_prefix_rules
    assert [segment.arguments for segment in bash.segments] == [
        ("python", "a.py"),
        ("Remove-Item", "b.txt"),
    ]
    assert bash.segments[1].separator_before == ";"


def test_quoted_and_escaped_separators_are_argument_text_not_command_boundaries() -> None:
    bash = parse_command("echo 'safe; rm -rf /' && echo foo\\;bar", "bash")
    assert bash.safe_for_prefix_rules
    assert [segment.arguments for segment in bash.segments] == [
        ("echo", "safe; rm -rf /"),
        ("echo", "foo;bar"),
    ]
    assert bash.segments[1].separator_before == "&&"

    powershell = parse_command('Write-Output "safe; Remove-Item x"; Get-Item \'a;''b\'', "powershell")
    assert powershell.safe_for_prefix_rules
    assert len(powershell.segments) == 2
    assert powershell.segments[0].arguments == ("Write-Output", "safe; Remove-Item x")


def test_powershell_quoting_preserves_windows_paths_and_backtick_escapes() -> None:
    parsed = parse_command(
        'Get-Content -LiteralPath "C:\\Program Files\\Mew\\a.txt"; Write-Output foo`;bar',
        "powershell",
    )
    assert parsed.safe_for_prefix_rules
    assert parsed.segments[0].arguments == (
        "Get-Content", "-LiteralPath", "C:\\Program Files\\Mew\\a.txt"
    )
    assert parsed.segments[1].arguments == ("Write-Output", "foo;bar")

    continued = parse_command("Get-Item `\r\n  reports", "powershell")
    assert continued.safe_for_prefix_rules
    assert len(continued.segments) == 1
    assert continued.segments[0].arguments == ("Get-Item", "reports")


def test_redirections_are_removed_from_argv_and_classified_by_direction() -> None:
    parsed = parse_command('rg "needle" reports > "out files/result.txt" 2>&1', "bash")
    assert parsed.safe_for_prefix_rules
    segment = parsed.segments[0]
    assert segment.arguments == ("rg", "needle", "reports")
    assert [(item.operator, item.target, item.direction) for item in segment.redirections] == [
        (">", "out files/result.txt", "write"),
        ("2>&1", None, "write"),
    ]

    source = parse_command("cat < input.txt", "bash")
    assert source.segments[0].redirections == (
        type(segment.redirections[0])("<", "input.txt", "read"),
    )


def test_opaque_interpreter_payloads_and_shell_expansion_never_allow_prefix_rules() -> None:
    examples = [
        ("python -c 'print(1)'", "bash"),
        ("bash -lc 'echo hidden'", "bash"),
        ("node -econsole.log(1)", "bash"),
        ("cmd /c echo unsafe", "bash"),
        ("cat <<'PY' | python -", "bash"),
        ("@'\nWrite-Output 'hidden'\n'@ | python -", "powershell"),
        ("Get-Content $env:APPDATA\\secret.txt", "powershell"),
        ("cat ${TARGET}", "bash"),
    ]
    for command, shell in examples:
        parsed = parse_command(command, shell)  # type: ignore[arg-type]
        assert parsed.opaque, command
        assert not parsed.safe_for_prefix_rules, command


def test_ambiguous_syntax_fails_closed() -> None:
    assert not parse_command("echo 'unfinished", "bash").parseable
    assert not parse_command("echo x & echo y", "powershell").parseable
    assert not parse_command("echo x |", "bash").parseable
    assert not parse_command("echo x >", "bash").parseable
    assert not parse_command("echo x", "cmd").parseable  # type: ignore[arg-type]


def _rule(pattern: object, decision: str, **kwargs: object) -> PrefixRule:
    return PrefixRule(
        pattern=pattern,  # type: ignore[arg-type]
        decision=decision,  # type: ignore[arg-type]
        match=kwargs.get("match", ("git push origin main",)),  # type: ignore[arg-type]
        not_match=kwargs.get("not_match", ("git pushx origin main",)),  # type: ignore[arg-type]
        shell=kwargs.get("shell", "bash"),  # type: ignore[arg-type]
        source=".mewcode/rules/default.rules",
        line=4,
    )


def test_prefix_rules_use_argument_boundaries_and_most_restrictive_decision() -> None:
    allow = _rule(("git", "push"), "allow")
    ask = _rule(("git", "push"), "ask")
    deny = _rule(("git", "push"), "deny")
    engine = PrefixRuleEngine([allow, ask, deny])

    assert all(result.active for result in engine.registrations)
    assert engine.evaluate("git push origin main").decision == "deny"
    assert engine.evaluate("git pushx origin main").decision is None
    assert engine.evaluate("git status").decision is None


def test_compound_allow_requires_a_rule_for_every_segment() -> None:
    engine = PrefixRuleEngine([_rule(("git", "push"), "allow")])

    assert engine.evaluate("git push origin main; Remove-Item important.txt").decision == "ask"
    assert engine.evaluate("git push origin main; git push origin backup").decision == "allow"


def test_prefix_allow_does_not_authorize_shell_redirection_targets() -> None:
    engine = PrefixRuleEngine([_rule(("git", "push"), "allow")])

    assert engine.evaluate("git push origin main > out.txt").decision == "ask"
    assert engine.evaluate("git push origin main < input.txt").decision == "ask"


def test_self_test_failure_disables_rule_and_reports_source_location() -> None:
    bad = _rule(
        ("git", "push"),
        "allow",
        match=("git push origin main",),
        not_match=("git push backup",),
    )
    engine = PrefixRuleEngine([bad])

    registration = engine.registrations[0]
    assert not registration.active
    assert registration.source == ".mewcode/rules/default.rules"
    assert registration.line == 4
    assert any("not_match self-test failed" in error for error in registration.errors)
    assert engine.evaluate("git push origin main").decision is None


def test_unmatched_self_test_set_is_inactive_and_opaque_command_returns_ask() -> None:
    no_tests = PrefixRule(("git", "push"), "allow")
    engine = PrefixRuleEngine([no_tests])

    assert not engine.registrations[0].active
    assert engine.evaluate("python -c 'print(1)'").decision == "ask"


def test_visible_forbidden_prefix_stays_denied_when_later_syntax_is_opaque() -> None:
    engine = PrefixRuleEngine([_rule(("git", "push"), "deny")])
    assert engine.evaluate("git push origin main; python -c 'print(1)'").decision == "deny"
    assert engine.evaluate("git push origin main $(unknown)").decision == "deny"


def test_decision_aliases_map_to_the_three_internal_levels() -> None:
    assert prefix_rule_decision("prompt") == "ask"
    assert prefix_rule_decision("forbidden") == "deny"
    assert prefix_rule_decision("allow") == "allow"


def test_prefix_pattern_alternatives_normalize_and_match_per_argument() -> None:
    rule = _rule(("git", ["push", "fetch"]), "allow")
    engine = PrefixRuleEngine([rule])

    assert rule.pattern == (("git",), ("push", "fetch"))
    assert engine.registrations[0].active
    assert engine.evaluate("git push origin main").decision == "allow"
    assert engine.evaluate("git fetch origin").decision == "allow"
    assert engine.evaluate("git status origin").decision is None


def test_pattern_alternatives_keep_shell_case_rules() -> None:
    powershell = _rule(
        ("Get-ChildItem", ["-Path", "-LiteralPath", "-path", "-LITERALPATH"]),
        "allow",
        shell="powershell",
        match=("get-childitem -path reports",),
        not_match=("Get-ChildItem -Name",),
    )
    powershell_engine = PrefixRuleEngine([powershell])
    assert powershell_engine.registrations[0].active
    assert powershell_engine.evaluate("GET-CHILDITEM -LITERALPATH reports", "powershell").decision == "allow"
    assert powershell_engine.evaluate("Get-ChildItem -name reports", "powershell").decision is None

    bash = _rule(
        ("git", ["Push", "Fetch"]),
        "allow",
        shell="bash",
        match=("git Push origin main",),
        not_match=("git push origin main",),
    )
    bash_engine = PrefixRuleEngine([bash])
    assert bash_engine.registrations[0].active
    assert bash_engine.evaluate("git Push origin main", "bash").decision == "allow"
    assert bash_engine.evaluate("git push origin main", "bash").decision is None


@pytest.mark.parametrize(
    "pattern",
    [
        (),
        ("git", ()),
        ("git", ("push", "")),
        ("git", (("push", "fetch"),)),
        ("git", None),
        ("git", 4),
        ("git", tuple(f"verb-{index}" for index in range(129))),
        tuple(f"arg-{index}" for index in range(129)),
    ],
)
def test_invalid_direct_patterns_report_errors_and_stay_inactive(pattern: object) -> None:
    rule = _rule(pattern, "allow")

    errors = validate_prefix_rule(rule)
    engine = PrefixRuleEngine([rule])

    assert errors
    assert not engine.registrations[0].active
    assert engine.registrations[0].errors
    assert engine.evaluate("git push origin main").decision is None


def test_alternative_deny_survives_compound_and_opaque_syntax() -> None:
    deny = _rule(("git", ["push", "fetch"]), "deny")
    engine = PrefixRuleEngine([deny])

    result = engine.evaluate("echo visible; git fetch origin; python -c 'hidden'", "bash")
    assert result.decision == "deny"
    assert result.reason == "forbidden prefix in opaque command"


def test_powershell_allow_cannot_fold_native_argument_case() -> None:
    rule = _rule(
        ("git", "branch", "-d"), "allow", shell="powershell",
        match=("git branch -d reviewed",), not_match=("git status",),
    )
    engine = PrefixRuleEngine([rule])
    assert engine.registrations[0].active
    assert engine.evaluate("git branch -d reviewed", "powershell").decision == "allow"
    assert engine.evaluate("git branch -D reviewed", "powershell").decision != "allow"


def test_powershell_deny_stays_conservative_for_case_variants() -> None:
    engine = PrefixRuleEngine([_rule(
        ("Remove-Item", "-LiteralPath", "protected"), "deny", shell="powershell",
        match=("Remove-Item -LiteralPath protected",), not_match=("Get-Item protected",),
    )])
    assert engine.evaluate("remove-item -literalpath PROTECTED", "powershell").decision == "deny"


@pytest.mark.parametrize("field", ["match", "not_match"])
@pytest.mark.parametrize("value", [None, 42, "git push origin main", {"a": "b"}, (None,), (42,), ("",)])
def test_malformed_direct_self_tests_disable_rule_without_crashing(field, value) -> None:
    engine = PrefixRuleEngine([_rule(("git", "push"), "allow", **{field: value})])
    assert not engine.registrations[0].active
    assert engine.evaluate("git push origin main").decision != "allow"
