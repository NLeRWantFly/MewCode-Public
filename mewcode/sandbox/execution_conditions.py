"""Classify host-owned command execution facts without interpreting child text."""
from __future__ import annotations

import ast
from collections.abc import Mapping
import re
import shlex


# Only these host codes establish an environment condition. Gate, scope and
# boundary failures stay permission/boundary conditions; missing/unknown codes
# are never promoted to environment repair from stdout or stderr prose.
ERROR_CODE_CONDITIONS = {
    'network_runtime_unavailable': 'environment_unavailable',
    'network_fence_unavailable': 'environment_unavailable',
    'network_disabled': 'permission_denied',
    'network_blacklist': 'permission_denied',
    "python_runtime_missing": "environment_unavailable",
    "native_helper_start_failed": "environment_unavailable",
    "native_queue_timeout": "environment_unavailable",
    "native_transport_unknown": "effect_unknown",
    "native_receipt_unverified": "effect_unknown",
    "native_result_rejected": "execution_failed",
    "native_workspace_missing": "permission_denied",
    "native_scope_invalid": "permission_denied",
    "native_admission_failed": "permission_denied",
    "native_preparation_failed": "permission_denied",
    "native_discard_unavailable": "permission_denied",
}

_COMPLETED_PUBLICATION = frozenset({"published", "no_changes", "discarded"})
_UNCERTAIN_PUBLICATION = frozenset({"cleanup_unknown", "recovery_required", "publishing", "unknown"})


def _read(value, name, default=None):
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _observation(value):
    """Read only structured host facts. Output/stdout/stderr are never parsed."""
    outer = value
    host_observation = _read(value, "host_observation")
    if host_observation is not None:
        outer = host_observation
    command = _read(outer, "command_execution")
    if isinstance(command, Mapping):
        return command, outer
    return outer, outer


def _reason(facts, receipt, kind):
    value = _read(receipt, "detail") or _read(facts, "reason")
    if isinstance(value, str) and value.strip():
        return value.strip()[:2000]
    defaults = {
        "permission_denied": "The host boundary did not authorize this command execution.",
        "environment_unavailable": "A required host execution resource is unavailable.",
        "execution_failed": "The command did not complete successfully.",
        "effect_unknown": "The host could not confirm the command's complete effect.",
        "ready": "The host confirmed a successful command exit.",
    }
    return defaults[kind]


def classify_execution_condition(receipt_or_observation) -> dict:
    """Classify one receipt/host observation into a safe generic condition.

    ``stdout``, ``stderr`` and rendered tool output are deliberately ignored.
    ``safe_to_retry`` is true only for a typed environment condition with a
    reliable ``command_started is False`` fact and no unresolved cleanup or
    publication state.
    """
    facts, outer = _observation(receipt_or_observation)
    status = _read(facts, "status")
    if not isinstance(status, str):
        status = ""
    status = status.strip().lower()
    completed_status = status == "completed"

    if hasattr(receipt_or_observation, "execution_started"):
        started = _read(receipt_or_observation, "execution_started")
    else:
        started = _read(facts, "command_started")
        if started is None and status in {"exited", "completed"}:
            started = True
    started = started if type(started) is bool else None

    code = _read(facts, "error_code") or _read(outer, "error_code") or _read(receipt_or_observation, "error_code") or ""
    code = code.strip() if isinstance(code, str) else ""
    cleanup = _read(facts, "cleanup_confirmed")
    if type(cleanup) is not bool:
        cleanup = None
    exit_code = _read(facts, "exit_code")
    transaction_id = _read(facts, "transaction_id") or ""
    publication = _read(facts, "publication_state") or ""
    publication = publication.strip().lower() if isinstance(publication, str) else ""

    explicit = ERROR_CODE_CONDITIONS.get(code)
    if code.startswith("native_gate_"):
        explicit = "permission_denied"

    kind = None
    if cleanup is False or publication in _UNCERTAIN_PUBLICATION:
        kind = "effect_unknown"
    elif publication and publication not in _COMPLETED_PUBLICATION | {"rejected", "not_applicable"}:
        kind = "effect_unknown"
    elif completed_status and started is False:
        kind = "effect_unknown"
    elif transaction_id and publication not in _COMPLETED_PUBLICATION | {"rejected"}:
        kind = "effect_unknown"
    elif status in {"exited", "completed"}:
        if type(exit_code) is not int:
            kind = "effect_unknown"
        elif publication == "rejected":
            kind = "execution_failed" if started is True else "permission_denied"
        else:
            kind = "ready" if exit_code == 0 else "execution_failed"
    elif started is True:
        if code == "native_result_rejected" and cleanup is not False:
            kind = "execution_failed"
        else:
            kind = "effect_unknown"
    elif explicit == "effect_unknown":
        kind = "effect_unknown"
    elif explicit == "environment_unavailable":
        kind = "environment_unavailable"
    elif status == "spawn_failed" and started is False:
        kind = "environment_unavailable"
    elif status == "spawn_failed" and started is None:
        kind = "effect_unknown"
    elif status == "timed_out" and started is False:
        kind = "environment_unavailable"
    elif status == "cancelled" and started is False:
        kind = "execution_failed"
    elif status in {"timed_out", "cancelled", "outcome_unknown"}:
        kind = "effect_unknown"
    elif explicit == "execution_failed":
        kind = "execution_failed" if started is True else "permission_denied"
    else:
        # Unknown denied codes are boundary conditions, never automatic repair.
        kind = "permission_denied"

    rejected_retry = (kind == "environment_unavailable" and bool(transaction_id)
                      and publication == "rejected" and started is False and cleanup is True)
    safe_publication = (publication != "rejected"
                        and (not transaction_id or publication in _COMPLETED_PUBLICATION))
    safe_to_retry = (kind == "environment_unavailable" and started is False and cleanup is not False
                     and publication not in _UNCERTAIN_PUBLICATION
                     and (safe_publication or rejected_retry)
                     and code not in {'network_runtime_unavailable', 'network_fence_unavailable'}
                     and _read(outer, 'automatic_recovery') is not False)
    result = {"kind": kind, "safe_to_retry": safe_to_retry,
              "command_started": started, "reason": _reason(facts, receipt_or_observation, kind)}
    if code:
        result["code"] = code
    return result


_READONLY_COMMANDS = frozenset({
    "cat", "dir", "get-childitem", "get-content", "get-location", "ls", "pwd", "type",
    "uname", "whoami", "which", "where", "where.exe",
})
_WRITE_COMMANDS = frozenset({
    "add-content", "append", "chmod", "chown", "copy", "cp", "del", "erase", "install",
    "mkdir", "move", "mv", "new-item", "out-file", "remove-item", "rename-item", "rm",
    "rmdir", "set-content", "set-itemproperty", "tee", "touch",
})
_GIT_READONLY = frozenset({"diff", "log", "show", "status", "rev-parse", "ls-files", "diff-tree"})
_GIT_WRITE = frozenset({
    "apply", "branch", "checkout", "cherry-pick", "clean", "commit", "config", "fetch",
    "merge", "pull", "push", "rebase", "reset", "revert", "stash", "switch", "tag",
    "worktree",
})
_PACKAGE_INSTALLERS = frozenset({"pip", "pip3", "uv", "uv.exe", "npm", "npx", "pnpm",
                                  "yarn", "cargo", "conda", "brew", "apt", "apt-get"})
_SAFE_PROBE_IMPORTS = frozenset({
    "collections", "datetime", "importlib.metadata", "itertools", "json", "math", "os",
    "pathlib", "platform", "re", "sqlite3", "statistics", "string", "sys", "sysconfig",
    "time", "typing",
})
_WRITE_PYTHON_METHODS = frozenset({
    "chmod", "mkdir", "open", "replace", "rename", "rmdir", "touch", "unlink",
    "write_bytes", "write_text",
})


def _command_field(value, name, default=None):
    return _read(value, name, default)


def _as_paths(value):
    if value is None or value == "" or value == () or value == []:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(item for item in value if isinstance(item, str) and item.strip())
    return ("<declared-write-path>",)


def _shell_redirects_to_write(command):
    """Find shell output redirection outside quotes, including PowerShell syntax."""
    quote = None
    escaped = False
    for char in command:
        if escaped:
            escaped = False
            continue
        if char == "`" and quote == '"':
            escaped = True
            continue
        if char == "\\" and quote == '"':
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == ">":
            return True
    return False


def _shell_tokens(command):
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return None


def _basename(token):
    return re.split(r"[\\/]", str(token).strip('"\''))[-1].lower()


def _python_command_code(tokens):
    """Return ``(is_python, code)`` for a simple Python ``-c`` invocation."""
    if not tokens:
        return False, None
    start = 1 if tokens[0] == "&" else 0
    if start >= len(tokens):
        return False, None
    executable = _basename(tokens[start])
    if not (executable.startswith("python") or executable in {"py", "py.exe"}):
        return False, None
    for index in range(start + 1, len(tokens)):
        if tokens[index] == "-c":
            if index + 1 >= len(tokens):
                return True, None
            return True, tokens[index + 1]
    return False, None


def _python_expr_is_inert(node, names):
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, ast.Attribute):
        return _python_expr_is_inert(node.value, names)
    if isinstance(node, ast.Subscript):
        return (_python_expr_is_inert(node.value, names)
                and _python_expr_is_inert(node.slice, names))
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_python_expr_is_inert(item, names) for item in node.elts)
    if isinstance(node, ast.Dict):
        return all(key is None or _python_expr_is_inert(key, names) for key in node.keys) and all(
            _python_expr_is_inert(value, names) for value in node.values)
    if isinstance(node, ast.JoinedStr):
        return all(isinstance(item, ast.Constant) or
                   (isinstance(item, ast.FormattedValue) and _python_expr_is_inert(item.value, names))
                   for item in node.values)
    return False


def _python_probe_effect(code):
    """Classify only import/print probes with an explicit safe-module allowlist."""
    if not isinstance(code, str):
        return "unknown"
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, TypeError):
        return "unknown"

    imported_names = set()
    for statement in tree.body:
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                if alias.name not in _SAFE_PROBE_IMPORTS:
                    return "unknown"
                imported_names.add(alias.asname or alias.name.split(".", 1)[0])
        elif isinstance(statement, ast.ImportFrom):
            if statement.level or statement.module not in _SAFE_PROBE_IMPORTS:
                return "unknown"
            if any(alias.name == "*" for alias in statement.names):
                return "unknown"
            imported_names.update(alias.asname or alias.name for alias in statement.names)
        elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
            call = statement.value
            if not isinstance(call.func, ast.Name) or call.func.id != "print" or call.keywords:
                return "unknown"
            if not all(_python_expr_is_inert(arg, imported_names) for arg in call.args):
                return "unknown"
        else:
            # Assignments, assertions, function calls and control flow can hide
            # imports with side effects or filesystem/process operations.
            return "unknown"
    return "readonly" if tree.body else "unknown"


def _python_obvious_write(code):
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, TypeError):
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
        if name in {"write_text", "write_bytes", "unlink", "rename", "replace", "mkdir", "rmdir", "touch", "chmod"}:
            return True
        if name == "open":
            mode = node.args[1].value if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) else ""
            mode = next((kw.value.value for kw in node.keywords
                         if kw.arg == "mode" and isinstance(kw.value, ast.Constant)), mode)
            if isinstance(mode, str) and any(flag in mode for flag in "wax+"):
                return True
    return False


def _git_subcommand(tokens, start):
    index = start + 1
    while index < len(tokens):
        token = tokens[index]
        if token in {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token.lower(), tokens[index + 1:]
    return "", []


def _explicit_write_command(tokens):
    if not tokens:
        return False
    start = 1 if tokens[0] == "&" else 0
    if start >= len(tokens):
        return False
    executable = _basename(tokens[start])
    words = [str(token).lower() for token in tokens[start + 1:]]
    if executable in _WRITE_COMMANDS:
        return True
    if executable in _PACKAGE_INSTALLERS:
        if executable in {"pip", "pip3", "uv", "uv.exe"}:
            return "install" in words or "add" in words
        return "install" in words or "add" in words or (executable in {"npm", "npx"} and "ci" in words)
    if executable in {"python", "python3", "python.exe", "python3.exe", "py", "py.exe"}:
        return ("-m" in words and any(word in words for word in {"pip", "ensurepip"})
                and any(word in words for word in {"install", "upgrade", "uninstall"}))
    if executable in {"git", "git.exe"}:
        action, _ = _git_subcommand(tokens, start)
        return action in _GIT_WRITE
    return False


def _trusted_readonly_policy(command, policy):
    commands = (_command_field(policy, "readonly_commands") or
                _command_field(policy, "read_only_commands") or ())
    if isinstance(commands, str):
        commands = (commands,)
    return any(isinstance(item, str) and item.strip() == command for item in commands)


def classify_command_effect(request, *, policy=None):
    """Classify the requested command's persistent effect using host facts.

    The request's natural-language claims are deliberately ignored.  A trusted
    discard publication boundary with no declared write paths is read-only;
    otherwise only a narrow command allowlist and inert standard-library
    Python import/print probes are classified as read-only.  Unrecognized
    commands remain unknown and must be handled like writes.
    """
    if isinstance(request, str):
        command = request.strip()
    else:
        command = _command_field(request, "command", "")
        command = command.strip() if isinstance(command, str) else ""

    write_paths = _as_paths(_command_field(request, "declared_write_paths"))
    if not write_paths:
        write_paths = _as_paths(_command_field(request, "write_paths"))
    if not write_paths:
        write_paths = _as_paths(_command_field(policy, "declared_write_paths"))
    if not write_paths:
        write_paths = _as_paths(_command_field(policy, "write_paths"))
    publication = (_command_field(policy, "publication_mode") or
                   _command_field(policy, "publication") or "")
    if isinstance(publication, str) and publication.strip().lower() == "discard" and not write_paths:
        return "readonly"

    tool_name = (_command_field(request, "tool_name") or _command_field(request, "tool") or "")
    if isinstance(tool_name, str) and tool_name.strip().lower() in {
            "writefile", "editfile", "write_file", "edit_file", "writeartifact", "movefile", "copyfile"}:
        return "write"
    if not command:
        return "unknown"
    if _shell_redirects_to_write(command):
        return "write"
    tokens = _shell_tokens(command)
    if tokens is None:
        return "unknown"
    if _explicit_write_command(tokens):
        return "write"

    is_python, code = _python_command_code(tokens)
    if is_python:
        if _python_obvious_write(code):
            return "write"
        return _python_probe_effect(code)

    start = 1 if tokens and tokens[0] == "&" else 0
    if start >= len(tokens):
        return "unknown"
    executable = _basename(tokens[start])
    lowered = [str(token).lower() for token in tokens]
    if executable in {"python", "python3", "python.exe", "python3.exe", "py", "py.exe"}:
        return "readonly" if len(tokens) > start + 1 and tokens[start + 1].lower() in {"--version", "-v"} else "unknown"
    if executable in {"git", "git.exe"}:
        action, args = _git_subcommand(tokens, start)
        if action in _GIT_READONLY and not any(
                arg in {"--ext-diff", "--textconv", "--output", "--output-directory"} for arg in args):
            return "readonly"
        return "unknown"
    if executable in _READONLY_COMMANDS and not any(token in {";", "|", "&&", "||"} for token in lowered):
        return "readonly"
    if _trusted_readonly_policy(command, policy):
        return "readonly"
    return "unknown"
