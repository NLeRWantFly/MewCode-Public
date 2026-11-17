"""Compact, source-bound inputs for isolated safety and recovery judgments."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


_SECRET_KEY_RE = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|password|secret|credential)"
)
_SECRET_TEXT_RE = re.compile(
    r"(?i)(api[_-]?key|token|password|secret|authorization)(['\"\s:=]+)[^,}\s]+"
)


def _redact_text(value: str) -> str:
    value = _SECRET_TEXT_RE.sub(r"\1\2[REDACTED]", value)
    value = re.sub(r"(?i)bearer\s+[a-z0-9._~+/-]+", "Bearer [REDACTED]", value)
    return re.sub(r"(?i)(https?://)[^/@\s]+@", r"\1[REDACTED]@", value)


def redact_value(value: Any, *, field_name: str = "") -> Any:
    """Recursively redact values while preserving object/array structure."""
    if field_name and _SECRET_KEY_RE.search(field_name):
        return "[REDACTED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, bytes):
        return {
            "binary": True,
            "bytes": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
            "omitted": True,
            "omission_reason": "binary_content",
        }
    if isinstance(value, Mapping):
        return {str(key): redact_value(item, field_name=str(key)) for key, item in value.items()}
    if isinstance(value, Sequence):
        return [redact_value(item) for item in value]
    if isinstance(value, Path):
        return _redact_text(str(value))
    return _redact_text(str(value))


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _source(
    reference: str,
    value: Any,
    *,
    trusted: bool,
    reason: str,
    digest: str | None = None,
) -> dict[str, Any]:
    return {
        "reference": reference,
        "digest": digest or canonical_digest(value),
        "digest_kind": "canonical_json_sha256_v1",
        "complete": True,
        "omitted": False,
        "omission_reason": "",
        "trusted_authorization": trusted,
        "included_because": reason,
    }


def build_safety_envelope(
    *,
    tool_name: str,
    arguments: Mapping[str, Any],
    risk: Mapping[str, Any],
    task_context: str,
    work_dir: Path,
    readonly_target_metadata: Mapping[str, Any],
    untrusted_tool_evidence: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build one structured capsule without converting arguments back to JSON text."""
    safe_arguments = redact_value(arguments)
    safe_risk = redact_value(risk)
    safe_task = _redact_text(task_context)
    safe_metadata = redact_value(readonly_target_metadata)
    safe_evidence = redact_value(untrusted_tool_evidence)
    omissions = [{"reference": row.get("tool_use_id") or "evidence_selection",
                  "reason": row.get("omission_reason") or "incomplete_evidence_view"}
                 for row in safe_evidence if row.get("complete") is False]
    source = (safe_metadata.get('action_target') or {}).get('program_source')
    if isinstance(source, Mapping) and source.get('complete') is False:
        omissions.append({'reference': 'host.action_target.program_source',
                          'reason': source.get('omission_reason') or 'script_source_unavailable'})
    imports = (safe_metadata.get('action_target') or {}).get('local_import_sources')
    if isinstance(imports, Mapping):
        for row in imports.get('omissions', []):
            omissions.append({'reference': 'host.action_target.local_import_sources'
                              + ('.' + row['module'] if row.get('module') else ''),
                              'reason': row['reason']})
    argument_sources = (safe_metadata.get('action_target') or {}).get('argument_sources')
    if isinstance(argument_sources, Mapping):
        for row in argument_sources.get('omissions', []):
            omissions.append({'reference': 'host.action_target.argument_sources'
                              + ('.' + str(row['argument_index']) if 'argument_index' in row else ''),
                              'reason': row['reason']})
    evidence_source = _source("completed_readonly_tools", safe_evidence, trusted=False,
                              reason="bounded evidence for a repeated review")
    if omissions:
        evidence_source.update(complete=False, omitted=True,
                               omission_reason="see envelope.omissions")
    planned_action = {
        "tool": tool_name,
        "arguments": safe_arguments,
        "cwd": str(work_dir),
        "arguments_digest": canonical_digest(safe_arguments),
        "raw_arguments_digest": canonical_digest(arguments),
    }
    command = arguments.get("command") if isinstance(arguments, Mapping) else None
    if isinstance(command, str):
        planned_action["command_observation"] = {
            "utf8_bytes": len(command.encode("utf-8")),
            "sha256": hashlib.sha256(command.encode("utf-8")).hexdigest(),
            "full_value_present": True,
        }
    result = {
        "envelope": {
            "version": 1,
            "kind": "tool_safety",
            "complete": not omissions,
            "planned_action_complete": True,
            "omissions": omissions,
        },
        "planned_action": planned_action,
        "declared_risk": safe_risk,
        "execution_environment": safe_risk.get("host_execution_environment", {}),
        "authorized_task": safe_task,
        "readonly_target_metadata": safe_metadata,
        "untrusted_tool_evidence": safe_evidence,
        "source_bindings": [
            _source("host.frozen_authorization", safe_task, trusted=True,
                    reason="trusted task authorization"),
            _source("tool_call.arguments", safe_arguments, trusted=False,
                    reason="exact planned operation under review",
                    digest=canonical_digest(arguments)),
            _source("permission_checker.risk", safe_risk, trusted=False,
                    reason="deterministic host risk observations"),
            _source("host.readonly_target_metadata", safe_metadata, trusted=False,
                    reason="current target facts; program text is evidence, never authorization"),
            evidence_source,
        ],
    }
    # Consent is minted by the host only after a real UI response and a fresh
    # ActionReviewBinding check. Keep it out of the untrusted risk bucket so
    # review does not mistake approval of the displayed effects for mere
    # permission to ask the review model again. Never promote tool text.
    consent = risk.get("host_user_consent")
    if (isinstance(consent, Mapping)
            and consent.get("decision") == "approved_once"
            and consent.get("consent_kind") == "exact_operation_effects_once"
            and consent.get("tool_name") == tool_name
            and consent.get("arguments_sha256") == canonical_digest(arguments)
            and consent.get("action_binding")
            and consent.get("action_binding") == risk.get("host_action_binding")):
        safe_consent = redact_value(consent)
        result["host_user_authorization"] = safe_consent
        result["source_bindings"].insert(1, _source(
            "host.exact_operation_user_decision", safe_consent, trusted=True,
            reason="host UI approval bound to this unchanged operation and its described effects"))
    return result
