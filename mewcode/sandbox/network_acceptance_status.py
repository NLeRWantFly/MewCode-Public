"""Pure, cautious descriptions of the latest network acceptance result."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


ACCEPTANCE_SCHEMA = "mewcode-srt-acceptance-v1"
UNAVAILABLE_PREFIX = "NETWORK_RUNTIME_UNAVAILABLE:"

_CHECK_LABELS = {
    "http_allowed": "普通 HTTP 访问检查",
    "ocr_upload_allowed": "OCR 上传检查",
    "git_https_allowed": "Git HTTPS 访问检查",
    "blacklist_domain": "域名黑名单拦截检查",
    "blacklist_redirect": "重定向黑名单检查",
    "blacklist_ip": "IP 黑名单拦截检查",
    "direct_socket_denied": "直接套接字限制检查",
    "network_disabled": "关闭联网策略检查",
    "workspace_escape_denied": "工作区边界检查",
    "sensitive_read_denied": "敏感路径读取边界检查",
    "cancel_cleanup": "取消后清理检查",
    "publication": "成果发布检查",
}


def _label(check: str) -> str:
    return _CHECK_LABELS.get(check, "其他联网验收检查")


def _unavailable(message: str) -> str:
    return f"{UNAVAILABLE_PREFIX} {message}"


def _failure_details(last: Mapping[str, Any], check: str) -> tuple[str, str] | None:
    failure = last.get("failure")
    if not isinstance(failure, Mapping):
        return None
    matched_check = failure.get("check")
    if matched_check is None:
        matched_check = failure.get("case")
    if matched_check != check:
        return None
    error_code = failure.get("error_code")
    detail = failure.get("reason")
    code_text = error_code.strip()[:120] if isinstance(error_code, str) else ""
    detail_text = detail.strip().replace("\r", " ").replace("\n", " ")[:320] if isinstance(detail, str) else ""
    if not code_text and not detail_text:
        return None
    return code_text, detail_text


def _unknown_count_suffix(count: int) -> str:
    return f"另有 {count} 项尚未执行。" if count else ""


def describe_acceptance(
    last: Any,
    *,
    identity: str,
    required_cases: Sequence[str],
) -> dict[str, Any]:
    """Describe a saved acceptance record without treating unknowns as passes.

    Only literal ``True`` passes a check and only literal ``False`` is reported
    in ``failed_checks``. Missing, null, and other values remain untested.
    Human-facing text uses Chinese check labels and makes no claim that a failed
    boundary check was exploited.
    """
    required = tuple(dict.fromkeys(
        case for case in required_cases if isinstance(case, str) and case
    ))
    schema_valid = isinstance(last, Mapping) and last.get("schema") == ACCEPTANCE_SCHEMA
    if not schema_valid:
        untested = list(required)
        reason = _unavailable(
            "验收记录格式无效，无法确认检查状态。" + _unknown_count_suffix(len(untested))
        )
        return {
            "ready": False,
            "schema_valid": False,
            "failed_checks": [],
            "untested_checks": untested,
            "stale_check": False,
            "reason": reason,
        }

    cases = last.get("cases")
    if not isinstance(cases, Mapping):
        cases = {}
    failed = [case for case in required if cases.get(case) is False]
    untested = [
        case for case in required
        if cases.get(case) is not True and cases.get(case) is not False
    ]
    stale = last.get("identity") != identity
    ready = not stale and not failed and not untested

    if ready:
        reason = ""
    elif failed:
        first = failed[0]
        details = _failure_details(last, first)
        if details is None:
            message = f"验收未通过或未完成：{_label(first)}。"
        else:
            error_code, detail = details
            message = f"{_label(first)}未通过"
            if error_code:
                message += f"（{error_code}）"
            if detail:
                message += f"：{detail}"
            message += "。"
        message += _unknown_count_suffix(len(untested))
        if stale:
            message += "验收记录对应的运行器身份已变化，需要重新验收。"
        reason = _unavailable(message)
    else:
        message = "尚未完成本机验收。" + _unknown_count_suffix(len(untested))
        failure = last.get('failure')
        if isinstance(failure, Mapping) and failure.get('check') is None and isinstance(failure.get('reason'), str):
            detail = failure['reason'].strip().replace('\r', ' ').replace('\n', ' ')[:320]
            if detail:
                message = "本轮验收未完成：" + detail + "。" + _unknown_count_suffix(len(untested))
        if stale:
            message += "验收记录对应的运行器身份已变化，需要重新验收。"
        reason = _unavailable(message)

    return {
        "ready": ready,
        "schema_valid": True,
        "failed_checks": failed,
        "untested_checks": untested,
        "stale_check": stale,
        "reason": reason,
    }
