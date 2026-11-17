"""Bounded side-process that proposes a small, evidence-backed plan change."""
from __future__ import annotations

from typing import Any

from mewcode.experts.audit_protocol import AuditCriterion
from mewcode.experts.recovery_classify import (
    BLOCKER_COMMAND,
    BLOCKER_CRITERION,
    BLOCKER_EVIDENCE,
    BLOCKER_PROVIDER,
    CHANGE_METHOD,
    CHANGE_SCOPE,
    classify_blocker,
    classify_criterion_change,
    format_stop_reason,
    normalize_blocker,
)

MAX_PROPOSALS = 1
PLANNER_EXHAUSTED = "规划器提案已用尽，保持暂停；已保留产物和未决动作。"


def proposal_budget_left(state: dict[str, Any]) -> bool:
    if str(state.get("recovery_decision") or "") in {"reject", "defer"}:
        return False
    return int(state.get("recovery_proposal_count") or 0) < MAX_PROPOSALS


def _criteria(state: dict[str, Any]) -> list[dict[str, Any]]:
    rubric = state.get("rubric") if isinstance(state.get("rubric"), dict) else {}
    items = []
    for item in rubric.get("criteria") or []:
        if isinstance(item, dict):
            items.append(AuditCriterion.model_validate(item).model_dump())
    return items


def _node_ids(node: Any) -> list[str]:
    ids = [str(getattr(node, "node_id", "") or "")]
    target = getattr(getattr(node, "failure_policy", None), "target_node_id", "") or ""
    if target:
        ids.append(str(target))
    return [item for item in ids if item]


def _legend_and_scene_split(criteria: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    rewritten: list[dict[str, Any]] = []
    changed = False
    used = {str(item.get("criterion_id") or "") for item in criteria}
    for item in criteria:
        text = str(item.get("acceptance_test") or "")
        if "场景" in text and any(token in text for token in ("图像", "图", "画面")):
            legend_id = str(item.get("criterion_id") or "FIG-LEGEND")
            scene_id = "FIG-SCENE"
            suffix = 2
            while scene_id in used:
                scene_id = f"FIG-SCENE-{suffix}"
                suffix += 1
            rewritten.append({
                "criterion_id": legend_id,
                "severity": item.get("severity") or "major",
                "acceptance_test": (
                    "GT/预测图例、框色与方法列对应关系正确，不把 GT 当作预测结果。"
                ),
            })
            rewritten.append({
                "criterion_id": scene_id,
                "severity": "blocking",
                "acceptance_test": (
                    "场景标签可追溯到用户提供的对照或文内明确映射；"
                    "仅凭灰度图不得推断场景类别。"
                ),
            })
            used.add(scene_id)
            changed = True
            continue
        rewritten.append(item)
    return rewritten if changed else None


def _method_pixel_revision(criteria: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    rewritten: list[dict[str, Any]] = []
    changed = False
    for item in criteria:
        text = str(item.get("acceptance_test") or "")
        if any(token in text for token in ("哈希", "hash", "文件大小")) and "ReadAuditArtifact" not in text:
            rewritten.append({
                **item,
                "acceptance_test": (
                    "按现有要求核验：必须通过 ReadAuditArtifact 读取图像像素，"
                    "不得用文件哈希或尺寸文本代替视觉证据。"
                ),
            })
            changed = True
            continue
        rewritten.append(item)
    return rewritten if changed else None


def build_proposal(
    state: dict[str, Any],
    *,
    node: Any,
    blocker: str,
) -> dict[str, Any] | None:
    blocker = normalize_blocker(blocker)
    if not blocker:
        return None
    criteria = _criteria(state)
    affected = _node_ids(node)
    if blocker == BLOCKER_EVIDENCE:
        return {
            "proposal_id": f"rev-{affected[0] if affected else 'audit'}-evidence",
            "blocker": blocker,
            "change_kind": "",
            "summary": "现有图像无法单独确定场景或标签，需要你提供对照或批准改范围。",
            "why": "灰度/单图不足以建立场景类别；继续修图不会产生可验收证据。",
            "affected_nodes": affected,
            "affected_criteria": [
                str(item.get("criterion_id") or "") for item in criteria
                if "场景" in str(item.get("acceptance_test") or "")
            ],
            "revalidate": ["场景相关验收项在获得对照前保持受阻"],
            "execution_scope": "补充对照后按原契约继续；常规读取、编辑、核验已包含在后续执行中。",
            "rubric_patch": None,
            "question": "请提供场景/标签对照，或批准把无法核验的场景主张从本节点拆出。",
        }
    method = _method_pixel_revision(criteria)
    if method is not None:
        return {
            "proposal_id": f"rev-{affected[0] if affected else 'audit'}-method",
            "blocker": blocker,
            "change_kind": CHANGE_METHOD,
            "summary": "同一要求改为读取图像像素核验，不改变通过线。",
            "why": "当前验收把文件哈希或尺寸当成了视觉证据。",
            "affected_nodes": affected,
            "affected_criteria": [str(item.get("criterion_id") or "") for item in criteria],
            "revalidate": ["按新核验方法重评受影响产物；旧通过不继承"],
            "execution_scope": "只重评该审计/修复对；常规读改核验已覆盖。",
            "rubric_patch": method,
            "question": "",
        }
    split = _legend_and_scene_split(criteria)
    if split is not None:
        return {
            "proposal_id": f"rev-{affected[0] if affected else 'audit'}-split",
            "blocker": blocker,
            "change_kind": CHANGE_SCOPE,
            "summary": (
                "把图示审计拆成两项：GT/预测图例正确性，以及场景标签可追溯性。"
                "修复节点先改正图例并同步文档；场景分类在你提供对照或批准改范围前保持受阻。"
            ),
            "why": "场景类别无法从现有图像单独建立；把两项绑在同一通过线上会迫使模型继续空转修图。",
            "affected_nodes": affected,
            "affected_criteria": [str(item.get("criterion_id") or "") for item in criteria],
            "revalidate": ["新验收版本下重评该审计/修复对", "旧清单 PASS 不得带到新清单"],
            "execution_scope": "只重做被点名的审计/修复对；前置已完成阶段保留；常规读改核验已覆盖。",
            "rubric_patch": split,
            "question": "",
        }
    if blocker == BLOCKER_CRITERION and criteria:
        return {
            "proposal_id": f"rev-{affected[0] if affected else 'audit'}-wording",
            "blocker": blocker,
            "change_kind": "wording",
            "summary": "当前验收条文与可获得证据不对齐，需要澄清措辞后再重评。",
            "why": "继续按原条文修复没有新的可核验进展。",
            "affected_nodes": affected,
            "affected_criteria": [str(item.get("criterion_id") or "") for item in criteria],
            "revalidate": ["澄清后按新验收版本重评受影响产物"],
            "execution_scope": "只重评该审计/修复对。",
            "rubric_patch": criteria,
            "question": "请批准澄清验收措辞，或补充仍缺的证据。",
        }
    return None


def resolve_stall(
    state: dict[str, Any],
    *,
    node: Any,
    repaired: Any = None,
) -> dict[str, Any]:
    blocker = classify_blocker(state, repaired)
    state["blocker"] = blocker
    if blocker == BLOCKER_PROVIDER:
        retries = int(state.get("provider_retries") or 0)
        if retries < 1:
            return {"action": "retry_provider", "blocker": blocker, "reason": ""}
        return {
            "action": "stop",
            "blocker": blocker,
            "reason": format_stop_reason(blocker, "临时供应失败已在剩余预算内重试过，保持暂停。"),
        }
    if blocker == BLOCKER_COMMAND:
        return {
            "action": "stop",
            "blocker": blocker,
            "reason": format_stop_reason(blocker, "错误命令已自动修正一次仍失败，保留现场。"),
        }
    if not proposal_budget_left(state):
        return {
            "action": "stop",
            "blocker": blocker,
            "reason": format_stop_reason(blocker, PLANNER_EXHAUSTED),
        }
    proposal = build_proposal(state, node=node, blocker=blocker)
    state["recovery_proposal_count"] = int(state.get("recovery_proposal_count") or 0) + 1
    if proposal is None:
        return {
            "action": "stop",
            "blocker": blocker,
            "reason": format_stop_reason(blocker, "未找到有证据支撑的替代方案，保持暂停。"),
        }
    if proposal.get("rubric_patch"):
        proposal["change_kind"] = classify_criterion_change(
            _criteria(state), proposal["rubric_patch"]
        ) or proposal.get("change_kind")
    # Repeated prose is a new host decision, not the old approval card.
    import uuid
    proposal['request_id'] = uuid.uuid4().hex
    if proposal.get("change_kind") == CHANGE_METHOD:
        state["pending_recovery_revision"] = proposal
        return {
            "action": "reassess",
            "blocker": blocker,
            "proposal": proposal,
            "reason": format_stop_reason(
                blocker,
                "核验方法已自动改为新验收版本；旧通过不继承，继续时按新版本重评。",
            ),
        }
    state["pending_recovery_revision"] = proposal
    return {
        "action": "await_revision",
        "blocker": blocker,
        "proposal": proposal,
        "reason": format_stop_reason(
            blocker,
            f"{proposal['summary']} 请在修订卡中选择：批准、补充信息、拒绝或稍后。",
        ),
    }
