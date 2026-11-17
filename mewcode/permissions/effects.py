"""Deterministic host-side summaries of tool effects.

This module never executes a command.  Its shell parser recognizes only a few
single-command forms whose complete target can be established without shell
expansion; everything else receives an opaque, conservative signature.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit


EffectKind = Literal[
    "read", "write", "delete", "net_egress", "exec_unbounded", "authority_change"
]
Blast = Literal["file", "directory", "repository", "session", "external", "unknown"]

_READ_TOOLS = frozenset({"ReadFile", "ReportArtifact"})
_CONTROL_READ_TOOLS = frozenset({"ResolveAutoReview", "AskUserQuestion"})
_WRITE_TOOLS = frozenset({"WriteFile", "EditFile"})
_SHELL_TOOLS = frozenset({"Bash", "RunVerification"})
_AUTHORITY_FILE_NAMES = frozenset({
    "permissions.grants.json", "permissions.yaml", "permissions.local.yaml",
})
_ALL_TARGETS = ("host:*", "path:*")
_EXPANSION_CHARS = frozenset("$*?[]{}%!")
_SHELL_OPERATORS = frozenset(";&|<>")
_POWERSHELL_EXPRESSION_CHARS = frozenset(",()@")


@dataclass(frozen=True)
class EffectSignature:
    """A stable, tool-independent summary of an action's likely effects."""

    targets: tuple[str, ...]
    kind: EffectKind
    blast: Blast
    reversible: Literal["unknown"] = "unknown"
    uncertain: bool = False

    def __post_init__(self) -> None:
        # A canonical tuple makes equality and the stable key independent of
        # the order in which a tool's local and remote targets were discovered.
        object.__setattr__(self, "targets", tuple(sorted(set(self.targets))))

    def as_dict(self) -> dict[str, Any]:
        return {
            "targets": list(self.targets),
            "kind": self.kind,
            "blast": self.blast,
            "reversible": self.reversible,
            "uncertain": self.uncertain,
        }

    @property
    def key(self) -> str:
        """A stable digest of the normalized effect, with no raw secrets."""
        payload = json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def overlaps(self, other: "EffectSignature") -> bool:
        """Whether the signatures may describe overlapping effects.

        Reads remain usable as evidence after a denied write.  Unknown command
        effects are intentionally broad, while known write/delete effects use
        path ancestry so a parent-directory operation cannot evade a file
        denial by switching tools.
        """
        if not isinstance(other, EffectSignature):
            return False
        if self.kind == "read" or other.kind == "read":
            if self.kind != other.kind:
                return False
            return any(_same_typed_target(left, right, "path")
                       or _same_typed_target(left, right, "host")
                       or _same_typed_target(left, right, "session")
                       for left in self.targets for right in other.targets)
        if self.kind == "exec_unbounded" or other.kind == "exec_unbounded":
            return True
        if self.uncertain or other.uncertain:
            return True
        # A permission-policy mutation can change the authority for any later
        # side effect, so it is deliberately not treated as an unrelated file.
        if self.kind == "authority_change" or other.kind == "authority_change":
            return True

        for left in self.targets:
            for right in other.targets:
                if _target_class(left) != _target_class(right):
                    continue
                target_type = _target_class(left)
                if target_type == "path":
                    if (self.kind in {"write", "delete"}
                            and other.kind in {"write", "delete"}
                            and _path_targets_overlap(left[5:], right[5:])):
                        return True
                elif target_type == "host":
                    if ("net_egress" in {self.kind, other.kind}
                            and _host_targets_overlap(left[5:], right[5:])):
                        return True
                elif target_type == "session" and left == right:
                    return True
        return False


@dataclass(frozen=True)
class _Token:
    value: str
    quoted: bool = False
    unquoted_expression: bool = False


def effect_signature(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    work_dir: str | os.PathLike[str] | None,
    tool: Any = None,
) -> EffectSignature:
    """Extract a deterministic effect signature without running the tool."""
    args = arguments if isinstance(arguments, dict) else {}
    root = _resolved_path(work_dir, work_dir=None) if work_dir else None

    if tool_name in _READ_TOOLS | _WRITE_TOOLS:
        raw_path = args.get("file_path")
        kind: EffectKind = "read" if tool_name in _READ_TOOLS else "write"
        return _path_signature(raw_path, kind, root,
                               authority=(kind == "write" and _is_authority_path(raw_path, root)))

    if tool_name in _CONTROL_READ_TOOLS:
        # These host control tools inspect review state or collect a user
        # response.  Their orchestration effect must not be conflated with a
        # prior file/command denial.
        return EffectSignature((), "read", "unknown")

    design = workflow_design_contract(tool_name, args, work_dir=work_dir, tool=tool)
    if design is not None:
        return EffectSignature(**design["effect"])

    host_control = _host_control_signature(tool_name, args, tool)
    if host_control is not None:
        return host_control

    if tool_name in {"Browser", "browser", "WebFetch", "web_fetch"}:
        return _browser_signature(args, root, tool)

    if tool_name in _SHELL_TOOLS:
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return _unbounded_signature(str(command or ""), root)
        parsed = _split_simple_command(command)
        if parsed is None:
            return _unbounded_signature(command, root)
        result = _simple_command_signature(parsed, command, root)
        return result if result is not None else _unbounded_signature(command, root)

    # Unknown tools stay unknown even if their category sounds read-only or
    # their argument names resemble paths.  Only explicit handlers above may
    # claim a bounded target.
    return _unbounded_signature(_json_hash_material(tool_name, args), root)


def workflow_design_contract(tool_name, arguments, *, work_dir, tool):
    """Describe only a validated, live, conversation-bound built-in operation.

    Queueing a design is a storage transition. Its later model requests,
    validation, saving and optional business dispatch have separate host gates;
    this classification neither approves them nor claims they are free/offline.
    """
    if tool_name not in {"ReadWorkflowBlocks", "AdvanceTaskDesign", "SubmitWorkflowBlocks"}:
        return None
    from mewcode.orchestration.workflow_blocks import WorkflowBlocks
    from mewcode.tools.task_design import AdvanceTaskDesign
    from mewcode.tools.workflow_blocks import ReadWorkflowBlocks, SubmitWorkflowBlocks
    expected = {"ReadWorkflowBlocks": ReadWorkflowBlocks, "AdvanceTaskDesign": AdvanceTaskDesign,
                "SubmitWorkflowBlocks": SubmitWorkflowBlocks}[tool_name]
    if type(tool) is not expected or type(getattr(tool, "blocks", None)) is not WorkflowBlocks:
        return None
    root = _resolved_path(work_dir, None)
    blocks = tool.blocks
    cid = blocks.conversation_id
    if root is None or not isinstance(cid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", cid):
        return None
    try:
        if _resolved_path(blocks.project, None) != root:
            return None
        storage = (root / ".mewcode" / "workflow-drafts.sqlite3").resolve()
        source = Path(blocks.source_path).resolve()
        if (not storage.is_relative_to(root) or not source.is_relative_to(root)
                or source.name != cid + ".jsonl"):
            return None
        blocks._check()
        params = tool.params_model.model_validate(arguments)
    except (OSError, RuntimeError, ValueError, TypeError):
        return None
    action = params.action if tool_name == "AdvanceTaskDesign" else (
        "read" if tool_name == "ReadWorkflowBlocks" else "submit")
    kind = "read" if action in {"read", "diagnose"} else "write"
    auxiliary = bool(tool_name == "SubmitWorkflowBlocks" and params.use_jev and tool.judgment_runtime)
    targets = ["path:" + _path_text(storage), "session:" + _path_text(root) + "/" + cid]
    if tool_name == "ReadWorkflowBlocks":
        targets.append("path:" + _path_text(source))
    if auxiliary:
        targets.append("host:*")
    signature = EffectSignature(tuple(targets), kind, "external" if auxiliary else "session")
    return {"schema": "mewcode.workflow-design-effect.v1", "tool": tool_name, "action": action,
            "workspace": str(root), "conversation_id": cid, "effect": signature.as_dict(),
            "immediate_effect": ("Read this conversation's design/materials and bound source history."
                if kind == "read" else "Write only this conversation's workflow specification or build queue in host storage."),
            "auxiliary_model_request": auxiliary,
            "later_phases": ("After this turn the host may request graph-generation and independent-check models, "
                "save the validated graph, and use the existing execution entry only under the host-selected intent. "
                "Those phases retain their own authority, network, cost and dispatch checks."
                if action == "build" else "No business dispatch or permission grant is performed by this operation."),
            "authority": "Effect facts only; neither model proposals nor continuation text expand user authority."}


def legacy_workflow_fallback(tool_name, arguments, *, work_dir):
    """The exact pre-contract fallback, for verified historical calls only."""
    root = _resolved_path(work_dir, None) if work_dir else None
    return _unbounded_signature(_json_hash_material(tool_name, arguments), root)


def _host_control_signature(name, arguments, tool):
    """Recognize concrete built-ins, never a plugin's claimed read category.

    Handoff/assistance tools record proposals in their host-bound holder. The
    scheduler separately validates any requested command, patch or clearance;
    recording one does not execute it. Auxiliary judgments do send text to a
    model service, but cannot execute the proposed choices.
    """
    if name in {"SubmitHandoff", "SubmitAssistanceReceipt", "RequestAssistance", "ReadStageResult"}:
        from mewcode.tools.read_stage_result import ReadStageResult
        from mewcode.tools.request_assistance import RequestAssistance, SubmitAssistanceReceipt
        from mewcode.tools.submit_handoff import SubmitHandoff
        classes = {"SubmitHandoff": SubmitHandoff, "SubmitAssistanceReceipt": SubmitAssistanceReceipt,
                   "RequestAssistance": RequestAssistance, "ReadStageResult": ReadStageResult}
        if type(tool) is classes[name]:
            return EffectSignature((), "read", "unknown")
    elif name == "InspectToolHistory":
        from mewcode.tools.inspect_tool_history import InspectToolHistory
        if type(tool) is InspectToolHistory:
            if arguments.get("action", "report") == "report":
                return EffectSignature((), "read", "unknown")
            if arguments.get("action") == "sample":
                return EffectSignature(("host:*",), "net_egress", "external")
    elif name == "DecideNextStep":
        from mewcode.tools.decide_next_step import DecideNextStep
        if type(tool) is DecideNextStep:
            return EffectSignature(("host:*",), "net_egress", "external")
    return None


def _json_hash_material(tool_name: str, arguments: dict[str, Any]) -> str:
    try:
        return json.dumps([tool_name, arguments], ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), default=str)
    except Exception:
        return str(tool_name)


def _resolved_path(raw: Any, work_dir: Path | str | os.PathLike[str] | None) -> Path | None:
    if not isinstance(raw, (str, os.PathLike)):
        return None
    value = os.fspath(raw).strip()
    if not value or "\x00" in value:
        return None
    try:
        path = Path(value).expanduser()
        if not path.is_absolute():
            base = Path(work_dir) if work_dir is not None else Path.cwd()
            path = base / path
        return path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError, TypeError):
        try:
            return Path(os.path.abspath(os.fspath(raw)))
        except (OSError, RuntimeError, ValueError, TypeError):
            return None


def _path_text(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _path_signature(path_value: Any, kind: EffectKind, root: Path | None, *,
                    authority: bool = False) -> EffectSignature:
    path = _resolved_path(path_value, root)
    if path is None:
        fallback_kind: EffectKind = "authority_change" if authority else kind
        return EffectSignature(("path:*",), fallback_kind, "unknown", uncertain=True)
    target = "path:" + _path_text(path)
    if authority and kind == "write":
        return EffectSignature((target,), "authority_change", _path_blast(path, root, path_value), uncertain=False)
    return EffectSignature((target,), kind, _path_blast(path, root, path_value), uncertain=False)


def _path_blast(path: Path, root: Path | None, raw_path: Any) -> Blast:
    try:
        if path.is_dir() or _has_directory_syntax(raw_path):
            target_blast: Blast = "directory"
        else:
            target_blast = "file"
    except OSError:
        return "unknown"
    if root is None:
        return target_blast
    try:
        path.relative_to(root)
    except ValueError:
        return "external"
    except OSError:
        return "unknown"
    return target_blast


def _has_directory_syntax(raw: Any) -> bool:
    if not isinstance(raw, str):
        return False
    return raw.endswith(("/", "\\")) or raw in {".", ".."}


def _is_authority_path(raw_path: Any, root: Path | None) -> bool:
    path = _resolved_path(raw_path, root)
    if path is None:
        return False
    parts = [part.casefold() for part in path.parts]
    for index, part in enumerate(parts):
        if part != ".mewcode" or index + 1 >= len(parts):
            continue
        child = parts[index + 1]
        if child in _AUTHORITY_FILE_NAMES:
            return True
        if (child == "rules" and index + 2 == len(parts) - 1
                and parts[-1].endswith(".rules")):
            return True
    return False


def _browser_signature(arguments: dict[str, Any], root: Path | None, tool: Any) -> EffectSignature:
    action = str(arguments.get("action") or "open").casefold()
    if action == "download":
        raw_path = arguments.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raw_path = _download_basename(arguments.get("url"))
        url_target = _url_target(arguments.get("url"))
        signature = _path_signature(raw_path, "write", root,
                                    authority=_is_authority_path(raw_path, root))
        targets = [*signature.targets, url_target or "host:*"]
        return EffectSignature(tuple(targets), signature.kind,
                               "external" if url_target else signature.blast,
                               uncertain=signature.uncertain or url_target is None)

    if action == "open" or arguments.get("url"):
        host = _url_target(arguments.get("url"))
        return EffectSignature((host or "host:*",), "net_egress", "external", uncertain=host is None)

    # A snapshot/links call without a URL reads the Browser's already-held page;
    # it does not issue another request. The host is only provenance.
    browser = getattr(tool, "_browser", tool)
    current_url = getattr(browser, "_current_url", "")
    host = _url_target(current_url)
    if action in {"snapshot", "links"}:
        return EffectSignature((host,) if host else (), "read", "unknown", uncertain=host is None)
    return _unbounded_signature(_json_hash_material("Browser", arguments), root)


def _download_basename(url: Any) -> str | None:
    if not isinstance(url, str):
        return None
    try:
        name = urlsplit(url).path.rsplit("/", 1)[-1]
    except ValueError:
        return None
    return name or "download.bin"


def _url_target(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
        if parsed.scheme.casefold() not in {"http", "https", "ssh", "git", "git+ssh"} or not hostname:
            return None
        if any(char in hostname for char in "$*?[]{}%!`/\\"):
            return None
        host = hostname.rstrip(".").casefold()
        # Canonicalize internationalized hostnames without retaining URL
        # credentials, paths, fragments, or query strings.
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return None
        if ":" in host:
            host = f"[{host}]"
        if port is not None:
            host += f":{port}"
        return "host:" + host
    except (ValueError, UnicodeError):
        return None


def _split_simple_command(command: str) -> list[_Token] | None:
    """Tokenize a single shell command; refuse operators and substitutions."""
    tokens: list[_Token] = []
    current: list[str] = []
    quote: str | None = None
    quoted = False
    unquoted_expression = False
    index = 0
    while index < len(command):
        char = command[index]
        if char in "\r\n\x00":
            return None
        if quote is not None:
            if char == quote:
                quote = None
            elif char == "\\" and os.name != "nt" and quote == '"':
                if index + 1 >= len(command):
                    return None
                following = command[index + 1]
                if following in {'"', "\\", "$", "`", "\n"}:
                    current.append(following)
                    index += 1
                else:
                    current.append(char)
            else:
                current.append(char)
        else:
            if char in _SHELL_OPERATORS or char == "`":
                return None
            if char in {"'", '"'}:
                quote = char
                quoted = True
            elif char.isspace():
                if current or quoted:
                    tokens.append(_Token("".join(current), quoted, unquoted_expression))
                    current, quoted, unquoted_expression = [], False, False
            elif char == "\\" and os.name != "nt":
                if index + 1 >= len(command):
                    return None
                current.append(command[index + 1])
                index += 1
            else:
                if os.name == "nt" and char in _POWERSHELL_EXPRESSION_CHARS:
                    # PowerShell command arguments can still evaluate arrays,
                    # splats, and nested expressions unless these characters
                    # are inside a quoted literal.
                    unquoted_expression = True
                current.append(char)
        index += 1
    if quote is not None:
        return None
    if current or quoted:
        tokens.append(_Token("".join(current), quoted, unquoted_expression))
    return tokens or None


def _simple_command_signature(tokens: list[_Token], raw_command: str,
                              root: Path | None) -> EffectSignature | None:
    command = tokens[0].value.casefold()
    args = tokens[1:]
    if command in {"git", "git.exe"}:
        return _git_push_signature(args, root) if args and args[0].value.casefold() == "push" else None
    if command in {"rm", "unlink", "del"}:
        if command == "rm" and len(args) == 2 and args[0].value == "--":
            args = args[1:]
        if len(args) != 1 or _unsafe_path_token(args[0]):
            return None
        return _path_signature(args[0].value, "delete", root)
    if command == "remove-item":
        if len(args) == 2 and args[0].value.casefold() in {"-path", "-literalpath"}:
            args = args[1:]
        if len(args) != 1 or _unsafe_path_token(args[0]):
            return None
        return _path_signature(args[0].value, "delete", root)
    if command == "truncate":
        if (len(args) != 3 or args[0].value != "-s" or args[1].value != "0"
                or _unsafe_path_token(args[2])):
            return None
        return _path_signature(args[2].value, "write", root)
    if command in {"get-content", "cat"}:
        if command == "get-content" and len(args) == 2 and args[0].value.casefold() in {"-path", "-literalpath"}:
            args = args[1:]
        if len(args) != 1 or _unsafe_path_token(args[0]):
            return None
        return _path_signature(args[0].value, "read", root)
    return None


def _unsafe_path_token(token: _Token) -> bool:
    value = token.value
    if not value or value == "-" or token.unquoted_expression:
        return True
    if any(char in value for char in _EXPANSION_CHARS):
        return True
    # PowerShell variable expansion is active inside double quotes too.
    if "$" in value or "`" in value:
        return True
    return value.startswith("-")


def _git_push_signature(args: list[_Token], root: Path | None) -> EffectSignature | None:
    if len(args) > 3 or any(not item.value or item.value.startswith("-") for item in args):
        return None
    remote = args[1] if len(args) >= 2 else None
    if remote is not None:
        value = remote.value
        if any(char in value for char in "$%!`"):
            return None
        if any(char in value for char in "*?[]{}") and not remote.quoted:
            return None
        url_target = _url_target(value)
        if "://" in value and url_target is None:
            return None
        if url_target is None:
            # SCP-style SSH remotes disclose only their host, never user/path.
            match = re.fullmatch(r"(?:[^@/:]+@)?([^/:]+):[^\s]+", value)
            if match:
                host = match.group(1).rstrip(".").casefold()
                url_target = "host:" + host
            elif not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
                return None
    else:
        url_target = None
    if len(args) == 3 and not re.fullmatch(r"\+?[A-Za-z0-9_./:-]+", args[2].value):
        return None
    repo_target = "path:" + _path_text(root) if root is not None else "path:*"
    targets = [repo_target, url_target or "host:*"]
    return EffectSignature(tuple(targets), "net_egress", "repository",
                           uncertain=root is None)


def _unbounded_signature(command: str, root: Path | None) -> EffectSignature:
    canonical = _normalize_unbounded_command(command)
    command_digest = hashlib.sha256(canonical.encode("utf-8", errors="surrogatepass")).hexdigest()
    targets = [*_ALL_TARGETS, "path:command-sha256/" + command_digest]
    if root is not None:
        targets.append("path:" + _path_text(root))
    return EffectSignature(tuple(targets), "exec_unbounded", "unknown", uncertain=True)


def _normalize_unbounded_command(command: str) -> str:
    """Collapse only unquoted horizontal whitespace, preserving shell meaning."""
    if "\n" in command or "\r" in command:
        return command
    out: list[str] = []
    quote: str | None = None
    escaped = False
    pending_space = False
    for char in command:
        if escaped:
            if pending_space and out:
                out.append(" ")
            pending_space = False
            out.append(char)
            escaped = False
            continue
        if char == "\\":
            if pending_space and out:
                out.append(" ")
            pending_space = False
            out.append(char)
            escaped = True
            continue
        if quote is not None:
            out.append(char)
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            if pending_space and out:
                out.append(" ")
            pending_space = False
            quote = char
            out.append(char)
        elif char in " \t":
            pending_space = True
        else:
            if pending_space and out:
                out.append(" ")
            pending_space = False
            out.append(char)
    if quote is not None:
        return command
    return "".join(out).strip()


def _target_class(target: str) -> str:
    return target.partition(":")[0] if ":" in target else "unknown"


def _same_typed_target(left: str, right: str, expected: str) -> bool:
    if _target_class(left) != expected or _target_class(right) != expected:
        return False
    if expected == "path":
        return _path_targets_overlap(left[5:], right[5:])
    if expected == "session":
        return left == right
    return _host_targets_overlap(left[5:], right[5:])


def _path_targets_overlap(left: str, right: str) -> bool:
    if left == "*" or right == "*":
        return True
    try:
        common = os.path.commonpath((left, right))
        common = os.path.normcase(os.path.normpath(common))
        return common in {os.path.normcase(os.path.normpath(left)),
                          os.path.normcase(os.path.normpath(right))}
    except (OSError, ValueError):
        return left == right


def _host_targets_overlap(left: str, right: str) -> bool:
    return left == "*" or right == "*" or left.casefold() == right.casefold()
