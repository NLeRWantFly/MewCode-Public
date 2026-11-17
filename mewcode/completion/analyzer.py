from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace

from mewcode.completion.catalog import JavaMember, catalog_has_type, simple_type_name


_IDENTIFIER = r"[A-Za-z_$][\w$]*"
_TYPE = (
    rf"(?:{_IDENTIFIER}\s*\.\s*)*{_IDENTIFIER}"
    r"(?:\s*<[^;\n={}()]+>)?(?:\s*\[\s*\])*"
)
_MEMBER_ACCESS_RE = re.compile(
    rf"(?P<receiver>{_IDENTIFIER})\.(?P<prefix>{_IDENTIFIER})?$"
)
_DECLARATION_RE = re.compile(
    rf"(?<![\w$])(?P<type>{_TYPE})\s+(?P<name>{_IDENTIFIER})\s*(?P<tail>=|;|,|\)|:)"
)
_VAR_CONSTRUCTOR_RE = re.compile(
    rf"(?<![\w$])var\s+(?P<name>{_IDENTIFIER})\s*=\s*new\s+(?P<type>{_TYPE})"
)
_ASSIGN_CONSTRUCTOR_RE_TEMPLATE = (
    rf"(?<![\w$])__RECEIVER__\s*=\s*new\s+(?P<type>{_TYPE})"
)
_CLASS_RE = re.compile(
    rf"\b(?:class|interface|record|enum)\s+(?P<name>{_IDENTIFIER})[^{{;]*\{{"
)
_METHOD_RE = re.compile(
    rf"(?m)^[ \t]*(?P<mods>(?:(?:public|protected|private|static|final|abstract|"
    rf"synchronized|native|default|strictfp)\s+)*)"
    rf"(?:<[^>{{}};]+>\s+)?(?P<return>{_TYPE}|void)\s+"
    rf"(?P<name>{_IDENTIFIER})\s*\((?P<params>[^)]*)\)\s*"
    rf"(?:throws\s+[^{{;]+\s*)?(?=[{{;])"
)
_FIELD_RE = re.compile(
    rf"(?m)^[ \t]*(?P<mods>(?:(?:public|protected|private|static|final|transient|"
    rf"volatile)\s+)*)(?P<type>{_TYPE})\s+(?P<name>{_IDENTIFIER})\s*"
    rf"(?:=[^;\n]*)?;"
)

_INVALID_DECLARATION_TYPES = {
    "break",
    "case",
    "catch",
    "continue",
    "do",
    "else",
    "finally",
    "for",
    "if",
    "new",
    "return",
    "switch",
    "throw",
    "try",
    "while",
}


@dataclass(frozen=True)
class JavaCompletionContext:
    completion_kind: str = "none"
    receiver: str = ""
    prefix: str = ""
    replacement_start: int = 0
    replacement_end: int = 0
    resolved_type: str = ""
    type_inference: str = "unresolved"
    type_confidence: float = 0.0
    declaration_evidence_hash: str = ""
    project_members: tuple[JavaMember, ...] = ()
    static_access: bool = False

    @property
    def context_key(self) -> str:
        owner = self.resolved_type or "unresolved"
        return f"{self.completion_kind}:{simple_type_name(owner)}"

    def with_resolved_type(
        self,
        type_name: str,
        inference: str,
        confidence: float,
    ) -> JavaCompletionContext:
        return replace(
            self,
            resolved_type=simple_type_name(type_name),
            type_inference=inference,
            type_confidence=confidence,
        )


class JavaLightweightAnalyzer:
    """Fault-tolerant Java context analysis for keystroke-time completion.

    This is intentionally not a compiler. It masks comments and literals,
    recognizes a small set of structural patterns, and fails closed to an
    unresolved context when the source is incomplete.
    """

    def analyze(self, source: str, cursor_offset: int) -> JavaCompletionContext:
        masked = mask_java_non_code(source)
        before_cursor = masked[:cursor_offset]
        access = _MEMBER_ACCESS_RE.search(before_cursor)
        if access is None:
            return JavaCompletionContext(
                replacement_start=cursor_offset,
                replacement_end=cursor_offset,
                project_members=self._extract_project_members(masked),
            )

        receiver = access.group("receiver")
        prefix = access.group("prefix") or ""
        context = JavaCompletionContext(
            completion_kind="member",
            receiver=receiver,
            prefix=prefix,
            replacement_start=cursor_offset - len(prefix),
            replacement_end=cursor_offset,
            project_members=self._extract_project_members(masked),
        )

        project_types = {member.declaring_type for member in context.project_members}
        if receiver in project_types or catalog_has_type(receiver):
            return replace(
                context,
                resolved_type=receiver,
                type_inference="type_name",
                type_confidence=0.97,
                static_access=True,
            )

        declaration = self._last_declaration(before_cursor, receiver)
        if declaration is not None:
            type_name, evidence = declaration
            return replace(
                context,
                resolved_type=simple_type_name(type_name),
                type_inference="declaration",
                type_confidence=1.0,
                declaration_evidence_hash=self._evidence_hash(evidence),
            )

        constructor = self._constructor_assignment(before_cursor, receiver)
        if constructor is not None:
            type_name, evidence = constructor
            return replace(
                context,
                resolved_type=simple_type_name(type_name),
                type_inference="constructor_assignment",
                type_confidence=0.9,
                declaration_evidence_hash=self._evidence_hash(evidence),
            )

        return context

    @staticmethod
    def _last_declaration(source: str, receiver: str) -> tuple[str, str] | None:
        matches: list[re.Match[str]] = []
        for match in _DECLARATION_RE.finditer(source):
            if match.group("name") != receiver:
                continue
            type_name = simple_type_name(match.group("type"))
            if type_name in _INVALID_DECLARATION_TYPES:
                continue
            matches.append(match)

        var_matches = [
            match
            for match in _VAR_CONSTRUCTOR_RE.finditer(source)
            if match.group("name") == receiver
        ]
        if var_matches and (
            not matches or var_matches[-1].start() >= matches[-1].start()
        ):
            match = var_matches[-1]
            return match.group("type"), match.group(0)

        if not matches:
            return None
        match = matches[-1]
        if simple_type_name(match.group("type")) == "var":
            return None
        return match.group("type"), match.group(0)

    @staticmethod
    def _constructor_assignment(
        source: str,
        receiver: str,
    ) -> tuple[str, str] | None:
        pattern = re.compile(
            _ASSIGN_CONSTRUCTOR_RE_TEMPLATE.replace(
                "__RECEIVER__",
                re.escape(receiver),
            )
        )
        matches = list(pattern.finditer(source))
        if not matches:
            return None
        match = matches[-1]
        return match.group("type"), match.group(0)

    @staticmethod
    def _evidence_hash(evidence: str) -> str:
        normalized = " ".join(evidence.split())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def _extract_project_members(self, masked_source: str) -> tuple[JavaMember, ...]:
        members: list[JavaMember] = []
        for class_match in _CLASS_RE.finditer(masked_source):
            owner = class_match.group("name")
            opening_brace = class_match.end() - 1
            closing_brace = _matching_brace(masked_source, opening_brace)
            if closing_brace is None:
                closing_brace = len(masked_source)
            body_start = opening_brace + 1
            body = masked_source[body_start:closing_brace]
            depths = _brace_depths(body)

            for match in _METHOD_RE.finditer(body):
                if depths[match.start()] != 1:
                    continue
                name = match.group("name")
                parameters = _parameter_types(match.group("params"))
                members.append(
                    JavaMember(
                        declaring_type=owner,
                        name=name,
                        kind="method",
                        return_type=" ".join(match.group("return").split()),
                        parameters=parameters,
                        priority=0.82,
                        is_static="static" in match.group("mods").split(),
                        source="project_symbol",
                    )
                )

            for match in _FIELD_RE.finditer(body):
                if depths[match.start()] != 1:
                    continue
                members.append(
                    JavaMember(
                        declaring_type=owner,
                        name=match.group("name"),
                        kind="field",
                        return_type=" ".join(match.group("type").split()),
                        priority=0.7,
                        is_static="static" in match.group("mods").split(),
                        source="project_symbol",
                    )
                )
        return tuple(members)


def mask_java_non_code(source: str) -> str:
    """Replace comments and literals with spaces while preserving offsets."""

    chars = list(source)
    masked = list(source)
    index = 0
    state = "code"
    while index < len(chars):
        char = chars[index]
        next_char = chars[index + 1] if index + 1 < len(chars) else ""

        if state == "code":
            if char == "/" and next_char == "/":
                masked[index] = masked[index + 1] = " "
                state = "line_comment"
                index += 2
                continue
            if char == "/" and next_char == "*":
                masked[index] = masked[index + 1] = " "
                state = "block_comment"
                index += 2
                continue
            if source.startswith('"""', index):
                masked[index:index + 3] = [" ", " ", " "]
                state = "text_block"
                index += 3
                continue
            if char == '"':
                masked[index] = " "
                state = "string"
                index += 1
                continue
            if char == "'":
                masked[index] = " "
                state = "char"
                index += 1
                continue
            index += 1
            continue

        if state == "line_comment":
            if char == "\n":
                state = "code"
            else:
                masked[index] = " "
            index += 1
            continue

        if state == "block_comment":
            if char == "*" and next_char == "/":
                masked[index] = masked[index + 1] = " "
                state = "code"
                index += 2
            else:
                if char not in "\r\n":
                    masked[index] = " "
                index += 1
            continue

        if state == "text_block":
            if source.startswith('"""', index):
                masked[index:index + 3] = [" ", " ", " "]
                state = "code"
                index += 3
            else:
                if char not in "\r\n":
                    masked[index] = " "
                index += 1
            continue

        if state in {"string", "char"}:
            delimiter = '"' if state == "string" else "'"
            if char == "\\":
                masked[index] = " "
                if index + 1 < len(chars):
                    if chars[index + 1] not in "\r\n":
                        masked[index + 1] = " "
                    index += 2
                else:
                    index += 1
            elif char == delimiter:
                masked[index] = " "
                state = "code"
                index += 1
            else:
                if char not in "\r\n":
                    masked[index] = " "
                index += 1

    return "".join(masked)


def _matching_brace(source: str, opening_brace: int) -> int | None:
    depth = 0
    for index in range(opening_brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def _brace_depths(body: str) -> list[int]:
    depths: list[int] = [1] * (len(body) + 1)
    depth = 1
    for index, char in enumerate(body):
        depths[index] = depth
        if char == "{":
            depth += 1
        elif char == "}":
            depth = max(1, depth - 1)
    depths[len(body)] = depth
    return depths


def _parameter_types(parameters: str) -> tuple[str, ...]:
    if not parameters.strip():
        return ()
    result: list[str] = []
    for parameter in parameters.split(","):
        cleaned = re.sub(r"@\w+(?:\([^)]*\))?\s*", "", parameter).strip()
        cleaned = re.sub(r"\bfinal\s+", "", cleaned)
        parts = cleaned.rsplit(maxsplit=1)
        parameter_type = parts[0] if len(parts) == 2 else cleaned
        if parameter_type:
            result.append(" ".join(parameter_type.split()))
    return tuple(result)
