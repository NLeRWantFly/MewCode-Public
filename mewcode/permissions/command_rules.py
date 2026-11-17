"""Conservative shell parsing and parameter-prefix permission rules.

This module deliberately does not execute commands or decide filesystem
boundaries. Callers must run host hard-deny and path checks before applying a
rule result. Parse uncertainty and opaque code are represented as ``ask`` so a
prefix rule can never turn an ambiguous command into an allow.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

ShellKind = Literal["bash", "powershell"]
Decision = Literal["allow", "ask", "deny"]
PatternAlternative = str | tuple[str, ...] | list[str]
PrefixPatternInput = tuple[PatternAlternative, ...] | list[PatternAlternative]
NormalizedPrefixPattern = tuple[tuple[str, ...], ...]

_DECISION_RANK: dict[str, int] = {"allow": 0, "ask": 1, "deny": 2}
_REDIRECTIONS = (
    "2>&1", "1>&2", "&>>", "&>", "2>>", "1>>", "2>", "1>",
    ">>", "<<<", "<<", "<>", ">", "<",
)
_OPAQUE_INTERPRETER_FLAGS = frozenset({
    "-c", "--command", "-command", "-encodedcommand", "-enc",
    "--eval", "-e", "--execute", "-exec", "-",
})
_OPAQUE_INTERPRETERS = frozenset({
    "python", "python2", "python3", "py", "node", "nodejs", "deno",
    "ruby", "perl", "php", "bash", "sh", "zsh", "pwsh", "powershell",
    "cmd", "iex", "invoke-expression", "eval",
})
_MAX_PREFIX_PATTERN_LENGTH = 128
_MAX_PREFIX_ALTERNATIVES = 128


def _normalize_prefix_pattern(pattern: object) -> tuple[NormalizedPrefixPattern | None, tuple[str, ...]]:
    """Normalize each literal position to an OR tuple and reject bad shapes."""
    if not isinstance(pattern, (list, tuple)) or not pattern:
        return None, ("prefix pattern must be a non-empty list or tuple",)
    if len(pattern) > _MAX_PREFIX_PATTERN_LENGTH:
        return None, (f"prefix pattern must contain at most {_MAX_PREFIX_PATTERN_LENGTH} positions",)

    positions: list[tuple[str, ...]] = []
    errors: list[str] = []
    for index, value in enumerate(pattern):
        if isinstance(value, str):
            alternatives = (value,)
        elif isinstance(value, (list, tuple)):
            if not value:
                errors.append(f"prefix pattern position {index + 1} must not have empty alternatives")
                continue
            if len(value) > _MAX_PREFIX_ALTERNATIVES:
                errors.append(
                    f"prefix pattern position {index + 1} must have at most "
                    f"{_MAX_PREFIX_ALTERNATIVES} alternatives"
                )
                continue
            if any(not isinstance(item, str) or not item for item in value):
                errors.append(
                    f"prefix pattern position {index + 1} alternatives must be non-empty strings"
                )
                continue
            alternatives = tuple(value)
        else:
            errors.append(
                f"prefix pattern position {index + 1} must be a non-empty string or a list/tuple of strings"
            )
            continue

        if any(not item for item in alternatives):
            errors.append(f"prefix pattern position {index + 1} must not be empty")
            continue
        positions.append(alternatives)

    if errors:
        return None, tuple(errors)
    return tuple(positions), ()


@dataclass(frozen=True)
class Redirection:
    """A parsed shell redirection; target is ``None`` for fd duplication."""

    operator: str
    target: str | None
    direction: Literal["read", "write"]


@dataclass(frozen=True)
class CommandSegment:
    """One argv sequence separated by a shell control or pipeline operator."""

    arguments: tuple[str, ...]
    redirections: tuple[Redirection, ...] = ()
    separator_before: str | None = None


@dataclass(frozen=True)
class ParsedCommand:
    shell: str
    segments: tuple[CommandSegment, ...]
    opaque: bool = False
    opaque_reasons: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def parseable(self) -> bool:
        return bool(self.segments) and not self.errors

    @property
    def safe_for_prefix_rules(self) -> bool:
        return self.parseable and not self.opaque


def _quoted(command: str, start: int, quote: str, shell: str) -> tuple[str, int, bool, bool]:
    """Read one quoted word fragment and return (value, next, closed, dynamic)."""
    value: list[str] = []
    index = start + 1
    dynamic = False
    while index < len(command):
        char = command[index]
        if char == quote:
            if shell == "powershell" and quote == "'" and index + 1 < len(command) and command[index + 1] == "'":
                value.append("'")
                index += 2
                continue
            return "".join(value), index + 1, True, dynamic
        if shell == "powershell" and quote == '"' and char == "`":
            if index + 1 >= len(command):
                return "".join(value), index + 1, False, dynamic
            value.append(command[index + 1])
            index += 2
            continue
        if shell == "bash" and quote == '"' and char == "\\":
            if index + 1 >= len(command):
                return "".join(value), index + 1, False, dynamic
            following = command[index + 1]
            if following in {'$', '`', '"', "\\", "\n"}:
                if following != "\n":
                    value.append(following)
                index += 2
                continue
            value.append("\\")
            index += 1
            continue
        if quote == '"' and char == "$":
            dynamic = True
        if shell == "bash" and quote == '"' and char == "`":
            dynamic = True
        value.append(char)
        index += 1
    return "".join(value), index, False, dynamic


def _here_string(command: str, start: int) -> tuple[int, bool]:
    """Consume a PowerShell here-string beginning at ``@'`` or ``@\"``."""
    quote = command[start + 1]
    opening_end = start + 2
    if opening_end >= len(command) or command[opening_end] not in "\r\n":
        return start, False
    body_start = opening_end + 1
    if command[opening_end:opening_end + 2] == "\r\n":
        body_start += 1
    terminator = re.compile(r"(?m)^" + re.escape(quote + "@") + r"(?=\r?$)")
    match = terminator.search(command, body_start)
    if not match:
        return len(command), False
    end = match.end()
    if end < len(command) and command[end] == "\r":
        end += 1
    if end < len(command) and command[end] == "\n":
        end += 1
    return end, True


def _interpreter_is_opaque(arguments: list[str]) -> str:
    if not arguments:
        return ""
    executable = arguments[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    if executable.endswith(".exe"):
        executable = executable[:-4]
    if executable not in _OPAQUE_INTERPRETERS:
        return ""
    if executable in {"iex", "invoke-expression", "eval"}:
        return "dynamic-eval command"
    for argument in arguments[1:]:
        normalized = argument.lower()
        if normalized in _OPAQUE_INTERPRETER_FLAGS:
            return "interpreter inline or stdin payload"
        if executable == "cmd" and normalized in {"/c", "/k"}:
            return "interpreter inline or stdin payload"
        if normalized.startswith(("--command=", "--eval=", "--execute=", "-command:", "-encodedcommand:")):
            return "interpreter inline or stdin payload"
        if executable in {"bash", "sh", "zsh"} and normalized.startswith("-") and "c" in normalized[1:]:
            return "interpreter inline or stdin payload"
        if executable in {"python", "python2", "python3", "py"} and normalized.startswith("-c"):
            return "interpreter inline or stdin payload"
        if executable in {"node", "nodejs", "deno"} and normalized.startswith("-e"):
            return "interpreter inline or stdin payload"
    if executable in {"python", "python2", "python3", "py", "node", "nodejs", "deno", "ruby", "perl", "php"} and "-" in arguments[1:]:
        return "interpreter inline or stdin payload"
    return ""


def parse_command(command: str, shell: ShellKind = "bash") -> ParsedCommand:
    """Parse bash or PowerShell syntax into argv segments, conservatively.

    Supported separators are ``|``, ``;``, ``&&``, ``||`` and newlines.
    Redirect targets are separated from argv and marked read or write. Shell
    expansion, dynamic quoting, here-documents, unsupported operators,
    unclosed quotes and interpreter inline payloads make the result unsuitable
    for allow decisions.
    """
    if shell not in ("bash", "powershell"):
        return ParsedCommand(str(shell), (), errors=("unsupported shell dialect",))
    if not isinstance(command, str) or not command.strip():
        return ParsedCommand(shell, (), errors=("empty command",))

    segments: list[CommandSegment] = []
    errors: list[str] = []
    opaque_reasons: list[str] = []
    arguments: list[str] = []
    redirections: list[Redirection] = []
    word: list[str] = []
    word_started = False
    pending_redirection: tuple[str, Literal["read", "write"]] | None = None
    separator_before: str | None = None
    index = 0

    def add_opaque(reason: str) -> None:
        if reason not in opaque_reasons:
            opaque_reasons.append(reason)

    def finish_word() -> None:
        nonlocal word_started, word, pending_redirection
        if not word_started:
            return
        value = "".join(word)
        if pending_redirection is not None:
            operator, direction = pending_redirection
            redirections.append(Redirection(operator, value, direction))
            pending_redirection = None
        else:
            arguments.append(value)
        word = []
        word_started = False

    def finish_segment(operator: str | None) -> None:
        nonlocal arguments, redirections, separator_before, pending_redirection
        finish_word()
        if pending_redirection is not None:
            errors.append(f"redirection {pending_redirection[0]} has no target")
            pending_redirection = None
        if arguments or redirections:
            inline_reason = _interpreter_is_opaque(arguments)
            if inline_reason:
                add_opaque(inline_reason)
            segments.append(CommandSegment(tuple(arguments), tuple(redirections), separator_before))
            arguments = []
            redirections = []
            separator_before = operator
        elif operator not in (None, "newline", ";"):
            errors.append(f"empty command before {operator}")
        elif operator == ";" and separator_before is not None:
            errors.append("empty command between separators")
        elif operator is None and separator_before in ("|", "&&", "||"):
            errors.append(f"trailing command separator {separator_before}")
        if operator in ("|", "&&", "||"):
            separator_before = operator
        elif operator in (";", "newline") and separator_before is None:
            separator_before = operator

    while index < len(command):
        char = command[index]

        if shell == "powershell" and char == "@" and index + 2 < len(command) and command[index + 1] in "'\"":
            end, closed = _here_string(command, index)
            if end != index:
                finish_word()
                if pending_redirection is not None:
                    errors.append("here-string cannot be a redirection target")
                    pending_redirection = None
                word_started = True
                word.append("<here-string>")
                add_opaque("PowerShell here-string")
                if not closed:
                    errors.append("unclosed PowerShell here-string")
                index = end
                continue

        if char in "'\"":
            value, next_index, closed, dynamic = _quoted(command, index, char, shell)
            word_started = True
            word.extend(value)
            if dynamic:
                add_opaque("interpolated quoted string")
            if not closed:
                errors.append("unclosed quote")
                index = next_index
                break
            index = next_index
            continue

        if shell == "bash" and char == "\\":
            if index + 1 >= len(command):
                errors.append("trailing escape")
                break
            following = command[index + 1]
            if following == "\r" and index + 2 < len(command) and command[index + 2] == "\n":
                index += 3
                continue
            if following != "\n":
                word.append(following)
                word_started = True
            index += 2
            continue

        if shell == "powershell" and char == "`":
            if index + 1 >= len(command):
                errors.append("trailing PowerShell escape")
                break
            following = command[index + 1]
            if following == "\r" and index + 2 < len(command) and command[index + 2] == "\n":
                index += 3
                continue
            if following not in "\r\n":
                word.append(following)
                word_started = True
            index += 2
            continue

        if char in " \t\r":
            finish_word()
            index += 1
            continue

        if char == "\n":
            finish_segment("newline")
            index += 1
            continue

        if shell == "bash" and char == "#" and (index == 0 or command[index - 1].isspace()):
            finish_word()
            while index < len(command) and command[index] != "\n":
                index += 1
            continue
        if shell == "powershell" and char == "#" and (index == 0 or command[index - 1].isspace()):
            finish_word()
            while index < len(command) and command[index] != "\n":
                index += 1
            continue
        if shell == "powershell" and command.startswith("<#", index):
            finish_word()
            end = command.find("#>", index + 2)
            if end < 0:
                errors.append("unclosed PowerShell block comment")
                break
            index = end + 2
            continue

        if char == "$":
            add_opaque("shell variable or command expansion")
        elif shell == "bash" and char == "`":
            add_opaque("command substitution")
        elif char in "(){}":
            add_opaque("shell grouping or script block")
        elif char in "*?[]~":
            add_opaque("pathname or wildcard expansion")
        elif char == "!":
            add_opaque("history or shell negation syntax")
        elif shell == "powershell" and char == "%":
            add_opaque("environment or pipeline alias ambiguity")

        if char == "<" and command.startswith("<<<", index):
            finish_word()
            if pending_redirection is not None:
                errors.append("nested redirection")
            pending_redirection = ("<<<", "read")
            add_opaque("here-string or here-document")
            index += 3
            continue

        matched_redirection = next((op for op in _REDIRECTIONS if command.startswith(op, index)), None)
        if matched_redirection is not None:
            finish_word()
            if matched_redirection in {"2>&1", "1>&2"}:
                redirections.append(Redirection(matched_redirection, None, "write"))
            elif matched_redirection in {"<<", "<<<"}:
                add_opaque("here-string or here-document")
                if pending_redirection is not None:
                    errors.append("nested redirection")
                pending_redirection = (matched_redirection, "read")
            else:
                direction: Literal["read", "write"] = "read" if matched_redirection.startswith("<") else "write"
                if pending_redirection is not None:
                    errors.append("nested redirection")
                pending_redirection = (matched_redirection, direction)
            index += len(matched_redirection)
            continue

        separator = next((op for op in ("&&", "||", "|", ";") if command.startswith(op, index)), None)
        if char == "&" and separator is None:
            finish_word()
            errors.append("unsupported shell operator '&'")
            index += 1
            continue
        if separator is not None:
            finish_segment(separator)
            index += len(separator)
            continue

        word.append(char)
        word_started = True
        index += 1

    finish_segment(None)
    if shell == "bash" and segments:
        first = segments[0].arguments
        if first and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", first[0]):
            add_opaque("environment assignment prefix")
    if not segments and not errors:
        errors.append("command produced no arguments")
    return ParsedCommand(
        shell=shell,
        segments=tuple(segments),
        opaque=bool(opaque_reasons),
        opaque_reasons=tuple(opaque_reasons),
        errors=tuple(errors),
    )


@dataclass(frozen=True)
class PrefixRule:
    """A self-tested argv prefix, with optional OR choices at each position."""

    pattern: PrefixPatternInput
    decision: Decision
    match: tuple[str, ...] = ()
    not_match: tuple[str, ...] = ()
    shell: ShellKind = "bash"
    source: str = ""
    line: int | None = None

    def __post_init__(self) -> None:
        normalized, errors = _normalize_prefix_pattern(self.pattern)
        if normalized is not None and not errors:
            object.__setattr__(self, "pattern", normalized)
        for name in ("match", "not_match"):
            examples = getattr(self, name)
            if isinstance(examples, (list, tuple)):
                object.__setattr__(self, name, tuple(examples))


@dataclass(frozen=True)
class RuleRegistration:
    active: bool
    errors: tuple[str, ...] = ()
    source: str = ""
    line: int | None = None


@dataclass(frozen=True)
class RuleEvaluation:
    decision: Decision | None
    matched_rules: tuple[PrefixRule, ...] = ()
    reason: str = ""
    parsed: ParsedCommand | None = None


def _rule_matches_segment(rule: PrefixRule, segment: CommandSegment, shell: str) -> bool:
    pattern, errors = _normalize_prefix_pattern(rule.pattern)
    if pattern is None or errors or len(segment.arguments) < len(pattern):
        return False
    for index, (argument, alternatives) in enumerate(zip(segment.arguments, pattern)):
        # PowerShell command lookup is case-insensitive, but native argv is
        # not: e.g. git branch -d and -D have different effects. Allow rules
        # must preserve literal argument case. Restrictions stay conservative
        # so changing cmdlet parameter/path case cannot evade an existing deny.
        if shell == "powershell" and (index == 0 or rule.decision != "allow"):
            folded = argument.casefold()
            if not any(folded == alternative.casefold() for alternative in alternatives):
                return False
        elif argument not in alternatives:
            return False
    return True


def _example_matches(rule: PrefixRule, command: str) -> bool:
    parsed = parse_command(command, rule.shell)
    return parsed.safe_for_prefix_rules and any(
        _rule_matches_segment(rule, segment, rule.shell) for segment in parsed.segments
    )


def validate_prefix_rule(rule: PrefixRule) -> tuple[str, ...]:
    """Return self-test/configuration errors; an invalid rule must stay inactive."""
    errors: list[str] = []
    normalized_pattern, pattern_errors = _normalize_prefix_pattern(rule.pattern)
    errors.extend(pattern_errors)
    if not isinstance(rule.decision, str) or rule.decision not in _DECISION_RANK:
        errors.append("decision must be allow, ask, or deny")
    if rule.shell not in ("bash", "powershell"):
        errors.append("unsupported shell dialect")
    for name in ("match", "not_match"):
        examples = getattr(rule, name)
        if (not isinstance(examples, (list, tuple)) or not examples or
                any(not isinstance(example, str) or not example.strip() for example in examples)):
            errors.append(f"{name} must be a non-empty list or tuple of non-empty strings")
    if normalized_pattern is not None and not errors:
        for example in rule.match:
            if not _example_matches(rule, example):
                errors.append(f"match self-test failed: {example}")
        for example in rule.not_match:
            if _example_matches(rule, example):
                errors.append(f"not_match self-test failed: {example}")
    return tuple(errors)


class PrefixRuleEngine:
    """Evaluate active argv-prefix rules with deny > ask > allow precedence.

    Every rule needs positive and negative self-tests. An unparseable or opaque
    command returns ``ask``. In a compound command, every segment must match an
    explicit rule before an allow result can be returned.
    """

    def __init__(self, rules: tuple[PrefixRule, ...] | list[PrefixRule] = ()) -> None:
        self._rules: list[PrefixRule] = []
        self.registrations: list[RuleRegistration] = []
        for rule in rules:
            self.register(rule)

    @property
    def rules(self) -> tuple[PrefixRule, ...]:
        return tuple(self._rules)

    def register(self, rule: PrefixRule) -> RuleRegistration:
        errors = validate_prefix_rule(rule)
        registration = RuleRegistration(
            active=not errors,
            errors=errors,
            source=rule.source,
            line=rule.line,
        )
        self.registrations.append(registration)
        if not errors:
            self._rules.append(rule)
        return registration

    def evaluate(self, command: str, shell: ShellKind = "bash") -> RuleEvaluation:
        parsed = parse_command(command, shell)
        if not parsed.safe_for_prefix_rules:
            # A visible forbidden prefix stays forbidden even when later
            # syntax is opaque.  Ambiguity may prevent an allow, but must not
            # downgrade an explicit host deny into an approvable prompt.
            forbidden = tuple(
                rule for segment in parsed.segments for rule in self._rules
                if rule.shell == shell and rule.decision == "deny"
                and _rule_matches_segment(rule, segment, shell)
            )
            if forbidden:
                return RuleEvaluation(
                    "deny", matched_rules=forbidden,
                    reason="forbidden prefix in opaque command", parsed=parsed,
                )
            detail = "; ".join(parsed.errors or parsed.opaque_reasons)
            return RuleEvaluation("ask", reason=detail or "ambiguous command", parsed=parsed)

        matched: list[PrefixRule] = []
        per_segment: list[list[PrefixRule]] = []
        for segment in parsed.segments:
            hits = [
                rule for rule in self._rules
                if rule.shell == shell and _rule_matches_segment(rule, segment, shell)
            ]
            per_segment.append(hits)
            matched.extend(hits)
        if not matched:
            return RuleEvaluation(None, reason="no prefix rule matched", parsed=parsed)

        decisions = [rule.decision for rule in matched]
        if len(parsed.segments) > 1 and any(not hits for hits in per_segment):
            decisions.append("ask")
        # A prefix rule covers argv, not a shell-selected input/output file.
        # Redirects need a separate path-bound authorization before allow.
        if any(segment.redirections for segment in parsed.segments):
            decisions.append("ask")
        decision = max(decisions, key=lambda item: _DECISION_RANK[item])
        return RuleEvaluation(
            decision=decision,
            matched_rules=tuple(matched),
            reason="most restrictive matching prefix decision",
            parsed=parsed,
        )


def prefix_rule_decision(value: str) -> Decision:
    """Normalize user-facing decision spellings used by ``*.rules`` files."""
    normalized = value.strip().lower()
    aliases = {"prompt": "ask", "forbidden": "deny"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in _DECISION_RANK:
        raise ValueError(f"unsupported prefix rule decision: {value}")
    return normalized  # type: ignore[return-value]
