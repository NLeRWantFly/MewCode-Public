"""Frozen, discrete audit contracts and deterministic acceptance decisions.

Models supply evidence and per-criterion observations. Only this module computes
the score and decides whether a candidate can replace the accepted artifact.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AuditCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    criterion_id: str = Field(min_length=1)
    acceptance_test: str = Field(min_length=1)
    severity: Literal["blocking", "major", "minor"] = "major"


class AuditCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    criterion_id: str = Field(min_length=1)
    state: Literal["PASS", "PARTIAL", "FAIL", "UNKNOWN"]
    evidence: str = Field(min_length=1, description="可核验观察；UNKNOWN 写明缺少的证据")
    location: str = Field(min_length=1, description="相对文件路径及段落/图表/行号，或验证记录位置")


class AuditAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rubric_hash: str
    artifact_hash: str
    baseline_hash: str = ""
    checks: list[AuditCheck] = Field(description=(
        "逐项观察对象数组；每项必须填写 criterion_id、state（PASS/PARTIAL/FAIL/UNKNOWN）、evidence、location。"
        "不能用 true/false 代替对象；缺证据时填 UNKNOWN 并说明缺口。"))
    baseline_checks: list[AuditCheck] = Field(default_factory=list,
        description="基线逐项观察，格式与 checks 相同；无基线时为空数组。")
    approach: str = Field(default="", description="本次验证的修复方法；复审时说明")

    @field_validator('checks', 'baseline_checks', mode='before')
    @classmethod
    def require_check_objects(cls, value):
        if isinstance(value, (list, tuple)) and any(not isinstance(row, (dict, AuditCheck)) for row in value):
            raise ValueError('逐项检查必须是对象数组，不能填写 true/false 或字符串。'
                '每项必填 criterion_id（冻结清单编号）、state（PASS/PARTIAL/FAIL/UNKNOWN）、'
                'evidence（已核验的观察）、location（证据位置）。'
                '请保留原 rubric_hash、artifact_hash 和实际证据；缺证据时用 UNKNOWN，不要补造通过结论。')
        return value


def content_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _normalized_criteria(items: Any) -> list[dict[str, Any]]:
    return [AuditCriterion.model_validate(item).model_dump() for item in items or []]


def default_criteria(node: Any) -> list[dict[str, Any]]:
    return _normalized_criteria([
        {"criterion_id": "REQ-01", "severity": "blocking",
         "acceptance_test": node.responsibility.split("决策指南（执行期必须遵守）", 1)[0].strip() or node.label},
        {"criterion_id": "EVD-01", "severity": "major",
         "acceptance_test": "本节点每项通过结论都有可定位的原始证据；缺证据记 UNKNOWN。"},
        {"criterion_id": "REG-01", "severity": "blocking",
         "acceptance_test": "本阶段已正确的内容、数据和用户锁定约束保持正确，没有新引入的错误。"},
    ])


def configured_criteria(node: Any, flow: Any) -> list[dict[str, Any]]:
    configured_rubrics = (flow.meta or {}).get("audit_rubrics", {})
    if not isinstance(configured_rubrics, dict):
        raise ValueError("audit_rubrics 必须是以审计节点编号为键的对象")
    configured = configured_rubrics.get(node.node_id)
    criteria = default_criteria(node) if configured is None else _normalized_criteria(configured)
    ids = [item["criterion_id"] for item in criteria]
    if not ids or len(ids) != len(set(ids)) or len(ids) > 64:
        raise ValueError("验收项必须非空、编号唯一且不超过 64 项")
    return criteria


def rubric_identity(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": int(value.get("version") or 1),
        "node_id": value.get("node_id"),
        "scope": value.get("scope"),
        "criteria": _normalized_criteria(value.get("criteria")),
    }


def freeze_rubric(node: Any, flow: Any, saved: dict | None = None) -> dict:
    criteria = configured_criteria(node, flow)
    value = {"version": 1, "node_id": node.node_id, "scope": node.responsibility,
             "criteria": criteria}
    value["rubric_hash"] = content_hash(rubric_identity(value))
    if saved:
        saved_criteria = _normalized_criteria(saved.get("criteria"))
        if saved_criteria == criteria and str(saved.get("node_id") or "") == str(node.node_id):
            return saved
        raise ValueError("验收标准与已冻结版本不同；请明确创建新一轮审查，不能续用旧分数")
    return value


def bump_rubric(
    saved: dict[str, Any],
    criteria: list[dict[str, Any]],
    *,
    change_kind: str,
    approved: bool,
) -> dict[str, Any]:
    from mewcode.experts.recovery_classify import CHANGE_METHOD, CHANGE_SCOPE, CHANGE_WORDING

    kind = str(change_kind or "")
    if kind in {CHANGE_WORDING, CHANGE_SCOPE} and not approved:
        raise ValueError("措辞或阈值/范围变更必须经批准后才能激活新验收版本")
    if kind not in {CHANGE_METHOD, CHANGE_WORDING, CHANGE_SCOPE}:
        raise ValueError("验收修订必须声明核验方法、措辞或阈值/范围")
    next_criteria = _normalized_criteria(criteria)
    ids = [item["criterion_id"] for item in next_criteria]
    if not ids or len(ids) != len(set(ids)) or len(ids) > 64:
        raise ValueError("验收项必须非空、编号唯一且不超过 64 项")
    value = {
        "version": int(saved.get("version") or 1) + 1,
        "node_id": saved.get("node_id"),
        "scope": saved.get("scope") or "",
        "criteria": next_criteria,
        "change_kind": kind,
        "supersedes": saved.get("rubric_hash"),
    }
    value["rubric_hash"] = content_hash(rubric_identity(value))
    return value


def set_flow_audit_rubric(flow: Any, node_id: str, criteria: list[dict[str, Any]]) -> None:
    meta = flow.meta if isinstance(getattr(flow, "meta", None), dict) else None
    if meta is None:
        raise ValueError("编排方案缺少可写入的验收清单")
    rubrics = dict(meta.get("audit_rubrics") or {})
    rubrics[str(node_id)] = _normalized_criteria(criteria)
    meta["audit_rubrics"] = rubrics


def drop_incompatible_assessment(state: dict[str, Any]) -> None:
    """Old PASS/scores must not be copied onto a new criterion version."""
    previous = state.get("rubric") if isinstance(state.get("rubric"), dict) else {}
    history = list(state.get("rubric_history") or [])
    if previous.get("rubric_hash"):
        history.append({
            "rubric_hash": previous.get("rubric_hash"),
            "version": previous.get("version"),
            "change_kind": previous.get("change_kind"),
        })
    state["rubric_history"] = history[-8:]
    for key in ("accepted", "baseline_result", "score", "last_decision"):
        state.pop(key, None)


SCORES = {"PASS": 0, "PARTIAL": 1, "FAIL": 2, "UNKNOWN": 2}


def vector(rubric: dict, checks: list[dict]) -> dict[str, int]:
    parsed = [AuditCheck.model_validate(check) for check in checks]
    ids = [check.criterion_id for check in parsed]
    expected = {item["criterion_id"] for item in rubric["criteria"]}
    if len(ids) != len(set(ids)) or set(ids) != expected:
        raise ValueError("复审必须且只能覆盖冻结清单中的每一项，编号不得重复")
    if any(not check.evidence.strip() or not check.location.strip() for check in parsed):
        raise ValueError("每个验收项都必须提供非空证据和位置")
    return {check.criterion_id: SCORES[check.state] for check in parsed}


def validate_assessment(
    rubric: dict, verdict: dict | None, artifact_hash: str, baseline_hash: str = "",
) -> dict:
    report = AuditAssessment.model_validate((verdict or {}).get("assessment") or {})
    if report.rubric_hash != rubric["rubric_hash"]:
        raise ValueError("复审引用的 rubric_hash 与冻结标准不一致")
    if report.artifact_hash != artifact_hash:
        raise ValueError("复审证据不是当前候选版本")
    value = report.model_dump()
    scores = vector(rubric, value["checks"])
    if baseline_hash:
        if report.baseline_hash != baseline_hash:
            raise ValueError("缺少配对复审或 baseline_hash 不匹配")
        vector(rubric, value["baseline_checks"])
    all_pass = all(score == 0 for score in scores.values())
    if all_pass != ((verdict or {}).get("verdict") == "PASS"):
        raise ValueError("PASS 结论与逐项验收状态矛盾")
    issues = [item for item in (verdict or {}).get("issues") or []
              if item.get("origin") != "out_of_scope"]
    if all_pass and issues:
        raise ValueError("仍有未解决问题，不能提交 PASS")
    if not all_pass:
        ids = [str(item.get("issue_id") or "").strip() for item in issues]
        criteria = {item["criterion_id"]: item for item in rubric["criteria"]}
        expected = {key for key, score in scores.items() if score > 0}
        covered = {item.get("criterion_id") for item in issues}
        if not ids or "" in ids or len(ids) != len(set(ids)) or covered != expected:
            raise ValueError("每个未通过项必须关联唯一 issue_id 和对应 criterion_id")
        for issue in issues:
            criterion = criteria[issue["criterion_id"]]
            if issue.get("acceptance_test") != criterion["acceptance_test"]:
                raise ValueError("问题关闭条件必须沿用冻结验收项的 acceptance_test")
    return value


@dataclass(frozen=True)
class AuditDecision:
    outcome: str
    reason: str
    before: int
    after: int
    improved: tuple[str, ...] = ()
    regressed: tuple[str, ...] = ()


def decide_candidate(rubric: dict, accepted: dict, report: dict) -> AuditDecision:
    expected = str(rubric.get("rubric_hash") or "")
    old_hash = str(accepted.get("rubric_hash") or "")
    new_hash = str(report.get("rubric_hash") or "")
    if (
        (expected and old_hash and old_hash != expected)
        or (expected and new_hash and new_hash != expected)
        or (old_hash and new_hash and old_hash != new_hash)
    ):
        return AuditDecision("invalid", "不同验收版本的分数不能比较是否改善", 0, 0)
    old = vector(rubric, accepted["checks"])
    paired = vector(rubric, report["baseline_checks"])
    new = vector(rubric, report["checks"])
    before, after = sum(old.values()), sum(new.values())
    if report["baseline_hash"] != accepted["artifact_hash"]:
        return AuditDecision("invalid", "复审的旧稿版本不匹配", before, after)
    old_states = {check["criterion_id"]: check["state"] for check in accepted["checks"]}
    paired_states = {check["criterion_id"]: check["state"] for check in report["baseline_checks"]}
    if paired != old or paired_states != old_states:
        return AuditDecision("invalid", "旧稿配对评分漂移，需确认基线后再继续", before, after)
    if any(check["state"] == "UNKNOWN" for check in report["checks"]):
        return AuditDecision("unknown", "存在缺失证据，不能判定完成", before, after)
    regressed = tuple(key for key in old if new[key] > old[key])
    improved = tuple(key for key in old if new[key] < old[key])
    if regressed:
        return AuditDecision("regression", "有验收项变差，候选稿不合入", before, after,
                             improved, regressed)
    if not improved:
        return AuditDecision("plateau", "没有验收项改善", before, after)
    return AuditDecision("complete" if after == 0 else "improved",
                         "全部验收项通过" if after == 0 else "验收项改善且没有退化",
                         before, after, improved)
