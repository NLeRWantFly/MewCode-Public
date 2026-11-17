"""Classify audit/repair stalls so they do not all enter the same repair branch."""
from __future__ import annotations

import re
from typing import Any

from mewcode.experts.recovery_diagnosis import KINDS as DIAGNOSIS_KINDS

BLOCKER_PROVIDER = "model_service_fault"
BLOCKER_COMMAND = "environment_fault"
BLOCKER_EVIDENCE = "insufficient_evidence"
BLOCKER_CRITERION = "business_ambiguity"

LEGACY_BLOCKER_ALIASES = {
    "provider_failure": BLOCKER_PROVIDER,
    "command_error": BLOCKER_COMMAND,
    "missing_evidence": BLOCKER_EVIDENCE,
    "criterion_mismatch": BLOCKER_CRITERION,
}

BLOCKER_LABELS = {
    BLOCKER_PROVIDER: "供应失败",
    BLOCKER_COMMAND: "命令错误",
    BLOCKER_EVIDENCE: "缺证据",
    BLOCKER_CRITERION: "验收契约不匹配",
}

CHANGE_METHOD = "check_method"
CHANGE_WORDING = "wording"
CHANGE_SCOPE = "threshold_or_scope"

COMMAND_RETRY_EXHAUSTED = "同一命令第三次仍失败"

_PROVIDER_RE = re.compile(
    r"(?i)(?:API error\s*\((?:408|409|429|500|502|503|504|529)\)|rate\s*limit|"
    r"overloaded|timeout|temporar(?:y|ily)|connection reset|connection aborted|"
    r"ECONNRESET|服务暂不可用|网络超时)"
)
_AUTH_RE = re.compile(r"(?i)(?:API error\s*\((?:401|403)\)|authentication|unauthorized|invalid api key)")
_SCENE_RE = re.compile(r"场景|scene|标签对照|类别")
_IMAGE_RE = re.compile(r"图像|图片|灰度|图5|figure|png|jpeg|视觉")
_METHOD_RE = re.compile(r"(?i)哈希|hash|文件大小|尺寸|像素|ReadAuditArtifact|核验方法")


def normalize_blocker(kind: str) -> str:
    """Translate persisted pre-diagnosis blocker labels to canonical kinds."""
    value = str(kind or "")
    value = LEGACY_BLOCKER_ALIASES.get(value, value)
    return value if value in DIAGNOSIS_KINDS else ""


def blocker_label(kind: str) -> str:
    canonical = normalize_blocker(kind)
    return BLOCKER_LABELS.get(canonical, str(kind))


def looks_like_provider_failure(text: str, *, status: str = "") -> bool:
    blob = f"{status}\n{text}"
    if _AUTH_RE.search(blob):
        return False
    if status == "failed" and _PROVIDER_RE.search(blob):
        return True
    return bool(_PROVIDER_RE.search(text or ""))


def looks_like_command_error(repaired: Any) -> bool:
    text = str(getattr(repaired, "text", "") or "")
    if COMMAND_RETRY_EXHAUSTED in text:
        return True
    records = list(getattr(repaired, "verification_records", None) or [])
    records.extend(item for item in (getattr(repaired, "activity_records", None) or []) if isinstance(item, dict))
    for item in records:
        if not isinstance(item, dict):
            continue
        output = str(item.get("output") or "")
        command = str(item.get("command") or "")
        failed = bool(item.get("is_error") or item.get("ok") is False)
        if failed and _missing_path(output or command):
            return True
        if COMMAND_RETRY_EXHAUSTED in output:
            return True
    return False


def _missing_path(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in (
        "no such file", "cannot find the path", "not found",
        "系统找不到", "无法找到", "cannot find", "file not found",
    ))


def _open_checks(state: dict[str, Any]) -> list[dict[str, Any]]:
    accepted = state.get("accepted") if isinstance(state.get("accepted"), dict) else {}
    return [
        check for check in (accepted.get("checks") or [])
        if isinstance(check, dict) and str(check.get("state") or "") != "PASS"
    ]


def _blob(state: dict[str, Any]) -> str:
    parts = [
        str((state.get("repair_progress") or {}).get("text") or ""),
        str((state.get("repair_progress") or {}).get("remaining") or ""),
    ]
    for check in _open_checks(state):
        parts.extend(str(check.get(key) or "") for key in ("acceptance_test", "evidence", "state"))
    rubric = state.get("rubric") if isinstance(state.get("rubric"), dict) else {}
    for item in rubric.get("criteria") or []:
        if isinstance(item, dict):
            parts.append(str(item.get("acceptance_test") or ""))
    return "\n".join(parts)


def classify_blocker(state: dict[str, Any], repaired: Any = None) -> str:
    """Return one canonical diagnosis kind; never a generic 'repair again'."""
    status = str(getattr(repaired, "status", "") or "")
    text = str(getattr(repaired, "text", "") or "")
    if looks_like_provider_failure(text, status=status):
        return BLOCKER_PROVIDER
    if looks_like_command_error(repaired) or COMMAND_RETRY_EXHAUSTED in str(
        (state.get("repair_progress") or {}).get("text") or ""
    ):
        return BLOCKER_COMMAND
    opens = _open_checks(state)
    unknown = [item for item in opens if str(item.get("state") or "") == "UNKNOWN"]
    blob = _blob(state) + "\n" + text
    scene = bool(_SCENE_RE.search(blob))
    image = bool(_IMAGE_RE.search(blob))
    if unknown and scene:
        return BLOCKER_EVIDENCE
    if scene and image:
        return BLOCKER_CRITERION
    if unknown:
        return BLOCKER_EVIDENCE
    # Old snapshots may contain only the legacy blocker label. Use it only
    # after current provider, command, evidence, and criterion signals fail.
    return normalize_blocker(state.get("blocker")) or BLOCKER_CRITERION


def classify_criterion_change(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> str:
    old_items = [dict(item) for item in old or []]
    new_items = [dict(item) for item in new or []]
    old_ids = [str(item.get("criterion_id") or "") for item in old_items]
    new_ids = [str(item.get("criterion_id") or "") for item in new_items]
    if old_ids != new_ids:
        return CHANGE_SCOPE
    old_sev = {str(item.get("criterion_id")): str(item.get("severity") or "") for item in old_items}
    new_sev = {str(item.get("criterion_id")): str(item.get("severity") or "") for item in new_items}
    if old_sev != new_sev:
        return CHANGE_SCOPE
    method_only = False
    wording = False
    for previous, current in zip(old_items, new_items):
        before = str(previous.get("acceptance_test") or "")
        after = str(current.get("acceptance_test") or "")
        if before == after:
            continue
        after_method = bool(_METHOD_RE.search(after))
        if after_method and ("ReadAuditArtifact" in after or "像素" in after):
            method_only = True
            continue
        wording = True
    if wording:
        return CHANGE_WORDING
    if method_only:
        return CHANGE_METHOD
    return ""


def format_stop_reason(kind: str, detail: str) -> str:
    return f"阻塞分类：{blocker_label(kind)}。{detail}"
