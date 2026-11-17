"""Narrow, host-verified reconciliation for simple single-file overwrites."""
from __future__ import annotations

from pathlib import Path
import re
import shlex

from mewcode.file_fingerprints import content_digest, normalized_text_digest
from mewcode.sandbox.execution_conditions import classify_command_effect


_UNSUPPORTED_LITERAL = re.compile(r"[\\$%`;&|<>*?{}\[\]#!^\x00-\x1f]")
_UNSUPPORTED_PATH_EXPANSION = re.compile(r"[\$%`;&|<>*?{}\[\]#!^\x00-\x1f]")


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _request_value(receipt):
    receipt = _mapping(receipt)
    request = receipt.get("request")
    return _mapping(request)


def _parse_echo_overwrite(command: str):
    """Parse only ``echo literal words > one-path``; shell expansion is rejected."""
    if not isinstance(command, str) or not command.strip():
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except (ValueError, TypeError):
        return None
    redirects = [index for index, token in enumerate(tokens) if token == ">"]
    if len(redirects) != 1:
        return None
    redirect = redirects[0]
    if redirect < 2 or redirect != len(tokens) - 2 or tokens[0] != "echo":
        return None
    if tokens[redirect + 1] in {"", ".", ".."}:
        return None
    words = tokens[1:redirect]
    if not words or words[0].startswith("-"):
        return None
    if any(_UNSUPPORTED_LITERAL.search(value) for value in (*words, tokens[redirect + 1])):
        return None
    return " ".join(words) + "\n", tokens[redirect + 1]


def _absolute_target(value: str, work_dir: str):
    if not isinstance(value, str) or not value.strip() or re.search(r"[\x00-\x1f]", value):
        return None
    try:
        root = Path(work_dir).expanduser().resolve(strict=False)
        candidate = Path(value)
        target = (candidate if candidate.is_absolute() else root / candidate).resolve(strict=False)
        if target == root or not target.is_relative_to(root):
            return None
        return target
    except (OSError, RuntimeError, ValueError, TypeError):
        return None


def _absolute_observed_path(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        path = Path(value)
        if not path.is_absolute():
            return None
        return path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError, TypeError):
        return None


def _observation_parts(observation):
    """Accept an original host tool row, never rendered text or a free-form claim."""
    row = _mapping(observation)
    name = row.get("name") or row.get("tool_name")
    if name != "ReadFile" or type(row.get("is_error")) is not bool:
        return None
    args = _mapping(row.get("args"))
    host = _mapping(row.get("host_observation"))
    state = _mapping(host.get("file_state"))
    path = _absolute_observed_path(state.get("absolute_path"))
    if path is None or type(state.get("exists")) is not bool or type(state.get("is_file")) is not bool:
        return None
    text_hash = state.get("text_sha256")
    if state["exists"] and state["is_file"]:
        if row["is_error"] or not isinstance(text_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", text_hash):
            return None
    elif state["exists"] is False:
        if not row["is_error"] or state["is_file"] or text_hash is not None:
            return None
    else:
        return None
    requested_path = args.get("file_path")
    if not isinstance(requested_path, str) or not requested_path.strip():
        return None
    return path, state, requested_path


def verify_effect_observations(original_receipt, observations, work_dir) -> dict | None:
    """Compute an effect only from a matching host ReadFile observation.

    The supported command is a literal single-file overwrite such as
    ``echo changed > result.txt``. Compound commands, append/delete operations,
    shell expansion, unparsed commands, and observations of other paths return
    ``None`` so they remain unresolved.
    """
    receipt = _mapping(original_receipt)
    request = _request_value(receipt)
    node_id = receipt.get("node_id")
    command = request.get("command")
    if not isinstance(node_id, str) or not isinstance(command, str):
        return None
    if classify_command_effect(request) != "write":
        return None
    verification_command = request.get("verification_command")
    if verification_command and verification_command.strip() != command.strip():
        return None
    parsed = _parse_echo_overwrite(command)
    if parsed is None:
        return None
    expected_text, command_target = parsed
    if "\\" in command_target or _UNSUPPORTED_PATH_EXPANSION.search(command_target):
        return None
    target = _absolute_target(command_target, work_dir)
    if target is None:
        return None

    fingerprint = receipt.get("effect_fingerprint")
    if (not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)):
        return None
    from mewcode.experts.command_receipts import command_effect_fingerprint
    if fingerprint != command_effect_fingerprint(node_id, request):
        return None

    declared = request.get("declared_write_paths", request.get("write_paths"))
    if declared:
        paths = declared if isinstance(declared, (list, tuple, set)) else [declared]
        declared_targets = [_absolute_target(path, work_dir) for path in paths if isinstance(path, str)]
        if len(declared_targets) != 1 or declared_targets[0] != target:
            return None

    expected_hash = content_digest(expected_text)
    matches = []
    for observation in observations if isinstance(observations, (list, tuple)) else []:
        parts = _observation_parts(observation)
        if parts is None:
            continue
        observed_path, state, requested_path = parts
        read_target = _absolute_target(requested_path, work_dir)
        if read_target != target or observed_path != target:
            continue
        matches.append(state)
    if not matches:
        return None
    if len({(item["exists"], item["is_file"], item.get("text_sha256")) for item in matches}) != 1:
        return None
    state = matches[-1]
    # A helper may have written after its ReadFile observation. Recheck at the
    # host proof boundary; stale absence or content cannot authorize a retry.
    try:
        target.stat()
        current = (True, target.is_file(), normalized_text_digest(target) if target.is_file() else None)
    except FileNotFoundError:
        current = (False, False, None)
    except (OSError, UnicodeError):
        return None
    if current != (state['exists'], state['is_file'], state.get('text_sha256')):
        return None
    if state["exists"] is False:
        effect = "not_applied"
        observed_hash = None
    elif state["exists"] and state["is_file"]:
        observed_hash = state["text_sha256"]
        effect = "applied" if observed_hash == expected_hash else "partial"
    else:
        return None
    proof = {
        "host_verified_effect": effect,
        "target": str(target),
        "effect_fingerprint": fingerprint,
        "expected_sha256": expected_hash,
        "result_sha256": observed_hash,
        "observed_exists": state["exists"],
    }
    return {"effect": effect, "proof": proof}
