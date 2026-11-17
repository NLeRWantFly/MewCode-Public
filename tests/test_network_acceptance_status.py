from __future__ import annotations

import pytest

from mewcode.sandbox.network_acceptance_status import (
    ACCEPTANCE_SCHEMA,
    UNAVAILABLE_PREFIX,
    describe_acceptance,
)


REQUIRED_CASES = (
    "http_allowed",
    "ocr_upload_allowed",
    "git_https_allowed",
    "blacklist_domain",
    "blacklist_redirect",
    "blacklist_ip",
    "direct_socket_denied",
    "network_disabled",
    "workspace_escape_denied",
    "sensitive_read_denied",
    "cancel_cleanup",
    "publication",
)


def _last(cases, **extra):
    return {
        "schema": ACCEPTANCE_SCHEMA,
        "identity": "identity-current",
        "cases": cases,
        **extra,
    }


def test_first_explicit_failure_keeps_missing_checks_untested() -> None:
    result = describe_acceptance(
        _last(
            {"http_allowed": False, **dict.fromkeys(REQUIRED_CASES[1:], None)},
            failure={
                "check": "http_allowed",
                "error_code": "SRT_HTTP_DENIED",
                "reason": "本机合成 HTTP 请求被拒绝",
            },
        ),
        identity="identity-current",
        required_cases=REQUIRED_CASES,
    )

    assert result["ready"] is False
    assert result["failed_checks"] == ["http_allowed"]
    assert result["untested_checks"] == list(REQUIRED_CASES[1:])
    assert result["stale_check"] is False
    assert result["reason"].startswith(UNAVAILABLE_PREFIX)
    assert "普通 HTTP 访问检查未通过" in result["reason"]
    assert "SRT_HTTP_DENIED" in result["reason"]
    assert "本机合成 HTTP 请求被拒绝" in result["reason"]
    assert "另有 11 项尚未执行" in result["reason"]
    assert "workspace_escape_denied" not in result["reason"]


def test_all_missing_checks_are_untested_not_failed() -> None:
    result = describe_acceptance(
        _last({}), identity="identity-current", required_cases=REQUIRED_CASES
    )

    assert result["failed_checks"] == []
    assert result["untested_checks"] == list(REQUIRED_CASES)
    assert result["ready"] is False
    assert "另有 12 项尚未执行" in result["reason"]
    assert result["reason"].startswith(UNAVAILABLE_PREFIX)


def test_legacy_all_false_without_failure_details_uses_cautious_summary() -> None:
    result = describe_acceptance(
        _last(dict.fromkeys(REQUIRED_CASES, False)),
        identity="identity-current",
        required_cases=REQUIRED_CASES,
    )

    assert result["failed_checks"] == list(REQUIRED_CASES)
    assert result["untested_checks"] == []
    assert result["reason"] == (
        UNAVAILABLE_PREFIX + " 验收未通过或未完成：普通 HTTP 访问检查。"
    )
    assert "被利用" not in result["reason"]
    assert "越权" not in result["reason"]


def test_single_false_with_all_other_checks_true_names_only_first_failure() -> None:
    cases = dict.fromkeys(REQUIRED_CASES, True)
    cases["blacklist_domain"] = False
    result = describe_acceptance(
        _last(cases), identity="identity-current", required_cases=REQUIRED_CASES
    )

    assert result["failed_checks"] == ["blacklist_domain"]
    assert result["untested_checks"] == []
    assert "域名黑名单拦截检查" in result["reason"]
    assert "普通 HTTP 访问检查" not in result["reason"]
    assert "另有" not in result["reason"]


def test_all_true_with_changed_identity_requires_reacceptance() -> None:
    result = describe_acceptance(
        _last(dict.fromkeys(REQUIRED_CASES, True)),
        identity="identity-new",
        required_cases=REQUIRED_CASES,
    )

    assert result["ready"] is False
    assert result["failed_checks"] == []
    assert result["untested_checks"] == []
    assert result["stale_check"] is True
    assert "需要重新验收" in result["reason"]
    assert result["reason"].startswith(UNAVAILABLE_PREFIX)


def test_failure_details_are_shown_only_when_the_check_matches() -> None:
    cases = dict.fromkeys(REQUIRED_CASES, True)
    cases["http_allowed"] = False
    mismatch = describe_acceptance(
        _last(
            cases,
            failure={
                "check": "publication",
                "error_code": "OTHER_CHECK_ERROR",
                "reason": "不得串到 HTTP 失败上",
            },
        ),
        identity="identity-current",
        required_cases=REQUIRED_CASES,
    )
    assert mismatch["reason"] == (
        UNAVAILABLE_PREFIX + " 验收未通过或未完成：普通 HTTP 访问检查。"
    )

    matched = describe_acceptance(
        _last(
            cases,
            failure={
                "case": "http_allowed",
                "error_code": "LOCAL_HTTP_DENIED",
                "reason": "本机 HTTP 探针未通过",
            },
        ),
        identity="identity-current",
        required_cases=REQUIRED_CASES,
    )
    assert "LOCAL_HTTP_DENIED" in matched["reason"]
    assert "本机 HTTP 探针未通过" in matched["reason"]


def test_invalid_schema_does_not_guess_any_check_passed() -> None:
    invalid = _last(dict.fromkeys(REQUIRED_CASES, True))
    invalid["schema"] = "unknown-schema"
    result = describe_acceptance(
        invalid, identity="identity-current", required_cases=REQUIRED_CASES
    )

    assert result["ready"] is False
    assert result["schema_valid"] is False
    assert result["failed_checks"] == []
    assert result["untested_checks"] == list(REQUIRED_CASES)
    assert "无法确认检查状态" in result["reason"]


def test_environment_exception_is_visible_without_claiming_checks_failed() -> None:
    result = describe_acceptance(_last(dict.fromkeys(REQUIRED_CASES, None),
        failure={'check': None, 'error_code': 'OSError', 'reason': '本机测试接收端未能创建'}),
        identity='identity-current', required_cases=REQUIRED_CASES)
    assert not result['ready']
    assert result['failed_checks'] == []
    assert result['untested_checks'] == list(REQUIRED_CASES)
    assert '本机测试接收端未能创建' in result['reason']


@pytest.mark.parametrize(
    ("case", "label"),
    [
        ("http_allowed", "普通 HTTP 访问检查"),
        ("ocr_upload_allowed", "OCR 上传检查"),
        ("git_https_allowed", "Git HTTPS 访问检查"),
        ("blacklist_domain", "域名黑名单拦截检查"),
        ("blacklist_redirect", "重定向黑名单检查"),
        ("blacklist_ip", "IP 黑名单拦截检查"),
        ("direct_socket_denied", "直接套接字限制检查"),
        ("network_disabled", "关闭联网策略检查"),
        ("workspace_escape_denied", "工作区边界检查"),
        ("sensitive_read_denied", "敏感路径读取边界检查"),
        ("cancel_cleanup", "取消后清理检查"),
        ("publication", "成果发布检查"),
    ],
)
def test_each_required_case_uses_its_chinese_label(case: str, label: str) -> None:
    cases = dict.fromkeys(REQUIRED_CASES, True)
    cases[case] = False
    result = describe_acceptance(
        _last(cases), identity="identity-current", required_cases=REQUIRED_CASES
    )

    assert label in result["reason"]
    assert case not in result["reason"]
