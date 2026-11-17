"""Shared bounded contract/check schema; capability declarations are not grants."""
from __future__ import annotations

import copy
import re

CHECK_KINDS = frozenset({"command", "test", "file_exists", "schema", "review"})
CONTRACT_FIELDS = frozenset({"milestone_id", "requirement_ids", "constraint_ids", "forbidden_changes", "method", "checks", "workspace_mode"})


def blocking_check(check):
    """Only execution prerequisites stop handoff; prose review is deferred.

    Authors can explicitly require a semantic check before a consumer runs, or
    make a nonessential test advisory. Tool permissions remain host policy.
    """
    return check.get("blocking", check["kind"] != "review") is True


def handoff_check_policy(checks):
    """Expose the host's existing handoff/quality boundary without new grants."""
    return {
        'required_before_handoff': [{'check_id': row['id'], 'version': row['version']}
                                    for row in checks if blocking_check(row)],
        'may_defer_to_review': [{'check_id': row['id'], 'version': row['version']}
                                for row in checks if not blocking_check(row)],
        'ready_for_review': ('产物已能供下游审阅、只剩可延后检查时，SubmitHandoff 使用 '
                             'status=ready_for_review，unmet_checks 引用检查 ID、version 和具体缺口；'
                             '不把未通过验收写成执行 blockers。宿主会核验实际交接条件，质量仍为待复核。'),
        'evidence_boundary': ('分别记录宿主配置/派发、实际请求参数、服务返回字段与内容核验。'
                              '未记录或服务未返回的字段标为未知；不能推断模型不可用、伪造证明、'
                              '把请求档位说成服务端内部生效证明，或为补遥测重复业务调用。'),
    }


def handoff_findings_error(handoff, checks):
    """Validate typed findings; advisory evidence gaps cannot become prerequisites."""
    review_ready = (handoff.get('status') == 'ready_for_review'
                    or handoff.get('readiness') == 'ready_for_review')
    findings = handoff.get('unmet_checks') or []
    evidence_blocked = (handoff.get('status') in {'blocked', 'needs_attention'}
                        and handoff.get('blocker_kind') == 'insufficient_evidence')
    if not (review_ready or findings or (evidence_blocked and checks is not None)):
        return ''
    if checks is None:
        return '当前交接没有绑定协作检查，不能声明 ready_for_review 或引用 unmet_checks。'
    if handoff.get('readiness') == 'ready_for_review' and handoff.get('acceptance_status') != 'pending':
        return '待复核交接不能自报验收通过；验收结论由宿主检查产生。'
    if not isinstance(findings, list) or not findings:
        return '请在 unmet_checks 中引用未满足检查的 check_id、version 和具体 reason；交接状态与验收结论分别表达。'
    available = {row['id']: row for row in checks}
    seen, required = set(), []
    for row in findings:
        if not isinstance(row, dict):
            return '未满足检查必须是带 check_id、version、reason 的结构化记录。'
        key, version, reason = row.get('check_id'), row.get('version'), row.get('reason')
        check = available.get(key) if isinstance(key, str) else None
        if (check is None or type(version) is not int or version != check['version']
                or key in seen or not isinstance(reason, str) or not reason.strip()):
            return '未满足检查引用未知、过期、重复或缺少原因；请核对当前 handoff_policy。'
        seen.add(key)
        if blocking_check(check):
            required.append(key)
    if review_ready and required:
        return '这些检查是交接前置，不能延后：' + '、'.join(required)
    if review_ready and (handoff.get('blockers') or handoff.get('blocker_kind') or handoff.get('question')
                         or handoff.get('file_requests') or handoff.get('command_requests')
                         or handoff.get('contract_conflict')):
        return 'ready_for_review 不能包含执行阻塞、待授权问题或未完成转派；这些边界仍须先处理。'
    if evidence_blocked and not required:
        return ('所引用检查均可延后复核，不能作为交接阻塞。已有可审阅产物时使用 '
                'ready_for_review，保留 unmet_checks，清空 blocker_kind 和 blockers；不代表验收通过。')
    if findings and not review_ready and not evidence_blocked:
        return 'unmet_checks 需与 ready_for_review 或真实的 insufficient_evidence 交接前置阻塞对应。'
    if review_ready and not (handoff.get('artifacts') or handoff.get('inline_outputs')):
        return '待复核交接必须提供实际产物或完整 inline_outputs，不能只有状态说明。'
    return ''


def identity(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,99}", value):
        raise ValueError("合同引用必须使用稳定条目 ID")
    return value


def text_list(value, *, ids=False):
    if (not isinstance(value, list) or len(value) > 100
            or any(not isinstance(item, str) or not item.strip() or len(item) > 2000 for item in value)):
        raise ValueError("合同文字列表必须包含不超过 100 项非空文字")
    if len(set(value)) != len(value):
        raise ValueError("合同列表不能重复引用")
    if ids:
        for item in value:
            identity(item)
    return copy.deepcopy(value)


def checks(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 100:
        raise ValueError("验收检查必须包含 1–100 项")
    result, used = [], set()
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("验收检查必须为对象")
        kind = raw.get("kind")
        if kind not in CHECK_KINDS:
            raise ValueError("验收检查类型无效")
        key = identity(raw.get("id"))
        if key in used:
            raise ValueError("验收检查 ID 重复")
        used.add(key)
        allowed = {"id", "kind", "version", "criteria", "blocking"} | (
            {"command"} if kind in {"command", "test"} else {"path", "schema"} if kind == "schema"
            else {"path"} if kind == "file_exists" else set())
        if set(raw) - allowed:
            raise ValueError("验收检查含不支持字段")
        row = copy.deepcopy(raw)
        if "blocking" in row and type(row["blocking"]) is not bool:
            raise ValueError("检查的 blocking 必须为布尔值")
        row.setdefault("version", 1)
        if type(row["version"]) is not int or row["version"] < 1:
            raise ValueError("验收检查版本无效")
        for field in ({"command"} if kind in {"command", "test"} else {"path"} if kind in {"file_exists", "schema"} else set()):
            if not isinstance(row.get(field), str) or not row[field].strip() or len(row[field]) > 8000:
                raise ValueError("验收检查缺少明确命令或路径")
        if kind == "schema":
            if not isinstance(row.get("schema"), dict):
                raise ValueError("schema 检查缺少 JSON Schema")
            # External schema resolution would introduce an undeclared network
            # operation. Local definitions and references remain available.
            def local(item):
                if isinstance(item, dict):
                    for keyword in ("$ref", "$dynamicRef", "$recursiveRef"):
                        if keyword in item and (not isinstance(item[keyword], str) or not item[keyword].startswith("#")):
                            raise ValueError("验收 schema 仅支持本地引用")
                    for nested in item.values():
                        local(nested)
                elif isinstance(item, list):
                    for nested in item:
                        local(nested)
            local(row["schema"])
            from jsonschema import validators
            from jsonschema.exceptions import SchemaError
            try:
                validators.validator_for(row["schema"]).check_schema(row["schema"])
            except SchemaError as exc:
                raise ValueError("验收 JSON Schema 无效：" + exc.message) from exc
        if kind == "review" and not row.get("criteria"):
            raise ValueError("独立复核必须列出验收标准")
        if "criteria" in row:
            text_list(row["criteria"])
        result.append(row)
    return result


def design_contract(value):
    if not isinstance(value, dict) or set(value) - CONTRACT_FIELDS:
        raise ValueError("协作合同包含未知字段")
    result = copy.deepcopy(value)
    if "workspace_mode" in result and (not isinstance(result["workspace_mode"], str)
                                      or result["workspace_mode"] not in {"worktree", "merge"}):
        raise ValueError("协作工作区模式只能是 worktree 或 merge")
    if "milestone_id" in result:
        identity(result["milestone_id"])
    for key in ("requirement_ids", "constraint_ids", "forbidden_changes"):
        if key in result:
            text_list(result[key], ids=key != "forbidden_changes")
    if "method" in result:
        method = result["method"]
        if (not isinstance(method, dict) or set(method) != {"id", "description"}
                or not isinstance(method["description"], str) or not 1 <= len(method["description"]) <= 4000):
            raise ValueError("指定方法须包含稳定 id 和完整 description")
        identity(method["id"])
    if "checks" in result:
        result["checks"] = checks(result["checks"])
    return result


def changed_fields(before, after):
    result = {key for key in ("title", "body", "state") if before.get(key) != after.get(key)}
    result |= {"data." + key for key in set(before.get("data", {})) | set(after.get("data", {}))
               if before.get("data", {}).get(key) != after.get("data", {}).get(key)}
    return result


def revised_acceptance_goal(contract, updated_checks):
    """Refresh the generated acceptance tail without rewriting authored steps."""
    old = '；'.join(item for check in contract['checks'] for item in check.get('criteria', []))
    new = '；'.join(item for check in updated_checks for item in check.get('criteria', []))
    goal = contract['goal']
    suffix = '\n验收：' + old
    if old and goal.endswith(suffix):
        goal = goal[:-len(suffix)]
    return goal + ('\n验收：' + new if new else '')
