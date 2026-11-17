"""Built-in orchestration templates (old topologies demoted to templates)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mewcode.orchestration.spec import OrchestrationSpec
from mewcode.orchestration.nested_template import nested_delivery
from mewcode.orchestration.oxr_books_template import oxr_books


def _linear_delivery(goal: str = "") -> dict[str, Any]:
    return {
        "spec_id": "template.linear",
        "label": "线性交付",
        "template_id": "linear",
        "goal": goal,
        "require_confirm_gate": True,
        "nodes": [
            {
                "node_id": "confirm",
                "kind": "gate",
                "label": "确认编排",
                "goal": "用户确认节点绑定后再执行",
                "bind_later": True,
            },
            {
                "node_id": "plan",
                "kind": "plan",
                "label": "方案规划",
                "goal": goal or "调研并写出可执行方案",
                "bind_later": True,
                "position": {"x": 80, "y": 40},
            },
            {
                "node_id": "implement",
                "kind": "implement",
                "label": "实现",
                "goal": "按方案修改代码并验证",
                "bind_later": True,
                "position": {"x": 80, "y": 140},
            },
            {
                "node_id": "audit",
                "kind": "audit",
                "label": "验收",
                "goal": "对照目标验收实现",
                "audit_target": "implement",
                "max_audit_retries": 3,
                "bind_later": True,
                "position": {"x": 80, "y": 240},
            },
            {
                "node_id": "summarize",
                "kind": "reduce",
                "label": "总结",
                "goal": "汇总变更与验证结果",
                "bind_later": True,
                "position": {"x": 80, "y": 340},
            },
        ],
        "edges": [
            {"from": "confirm", "to": "plan"},
            {"from": "plan", "to": "implement"},
            {"from": "implement", "to": "audit"},
            {"from": "audit", "to": "summarize"},
        ],
        "finalizer_node_id": "summarize",
    }


def _fe_be_parallel(goal: str = "") -> dict[str, Any]:
    return {
        "spec_id": "template.fe_be_parallel",
        "label": "前端 + 后端并行",
        "template_id": "fe_be_parallel",
        "goal": goal,
        "require_confirm_gate": True,
        "nodes": [
            {
                "node_id": "confirm",
                "kind": "gate",
                "label": "确认编排",
                "goal": "确认各节点模型绑定",
                "bind_later": True,
            },
            {
                "node_id": "plan",
                "kind": "plan",
                "label": "方案规划",
                "goal": goal or "拆分前端与后端工作并给出接口约定",
                "bind_later": True,
                "position": {"x": 200, "y": 40},
            },
            {
                "node_id": "fe_impl",
                "kind": "implement",
                "label": "前端实现",
                "goal": "实现前端 / UI 相关变更",
                "prefers": "frontend",
                "bind_later": True,
                "position": {"x": 80, "y": 160},
            },
            {
                "node_id": "be_impl",
                "kind": "implement",
                "label": "后端实现",
                "goal": "实现后端 / API / 数据相关变更",
                "prefers": "backend",
                "bind_later": True,
                "position": {"x": 320, "y": 160},
            },
            {
                "node_id": "join",
                "kind": "reduce",
                "label": "汇合摘要",
                "goal": "汇总前后端变更，列出冲突与待验证点",
                "bind_later": True,
                "position": {"x": 200, "y": 280},
            },
            {
                "node_id": "audit",
                "kind": "audit",
                "label": "整体验收",
                "goal": "验收端到端是否达成目标",
                "audit_target": "be_impl",
                "max_audit_retries": 2,
                "bind_later": True,
                "position": {"x": 200, "y": 380},
            },
            {
                "node_id": "summarize",
                "kind": "reduce",
                "label": "最终总结",
                "goal": "给用户可读的完成报告",
                "bind_later": True,
                "position": {"x": 200, "y": 480},
            },
        ],
        "edges": [
            {"from": "confirm", "to": "plan"},
            {"from": "plan", "to": "fe_impl"},
            {"from": "plan", "to": "be_impl"},
            {"from": "fe_impl", "to": "join"},
            {"from": "be_impl", "to": "join"},
            {"from": "join", "to": "audit"},
            {"from": "audit", "to": "summarize"},
        ],
        "finalizer_node_id": "summarize",
    }


def _classic_four_role(goal: str = "") -> dict[str, Any]:
    """Legacy expert-team topology as a selectable template."""
    return {
        "spec_id": "template.classic_four_role",
        "label": "经典四角色（兼容）",
        "template_id": "classic_four_role",
        "goal": goal,
        "require_confirm_gate": True,
        "nodes": [
            {
                "node_id": "confirm",
                "kind": "gate",
                "label": "确认编排",
                "bind_later": True,
            },
            {
                "node_id": "manager_intake",
                "kind": "reduce",
                "label": "目标澄清",
                "goal": "澄清目标与约束",
                "bind_later": True,
            },
            {
                "node_id": "plan",
                "kind": "plan",
                "label": "方案规划",
                "goal": goal or "制定实施方案",
                "bind_later": True,
            },
            {
                "node_id": "implement",
                "kind": "implement",
                "label": "执行",
                "goal": "实现方案",
                "bind_later": True,
            },
            {
                "node_id": "audit",
                "kind": "audit",
                "label": "审计",
                "goal": "审计实现",
                "audit_target": "implement",
                "bind_later": True,
            },
            {
                "node_id": "summarize",
                "kind": "reduce",
                "label": "最终汇总",
                "goal": "汇总结果",
                "bind_later": True,
            },
        ],
        "edges": [
            {"from": "confirm", "to": "manager_intake"},
            {"from": "manager_intake", "to": "plan"},
            {"from": "plan", "to": "implement"},
            {"from": "implement", "to": "audit"},
            {"from": "audit", "to": "summarize"},
        ],
        "finalizer_node_id": "summarize",
    }


def _research_write(goal: str = "") -> dict[str, Any]:
    """调研 → 检索/学习 → 评判 → 撰写（可并行子稿）→ 审查."""
    return {
        "spec_id": "template.research_write",
        "label": "调研撰写",
        "template_id": "research_write",
        "goal": goal,
        "require_confirm_gate": True,
        "nodes": [
            {
                "node_id": "confirm",
                "kind": "gate",
                "label": "确认编排",
                "goal": "确认调研与撰写阶段后再执行",
                "bind_later": True,
            },
            {
                "node_id": "survey",
                "kind": "plan",
                "label": "调研",
                "goal": goal or "明确问题、范围与评价标准",
                "bind_later": True,
                "position": {"x": 200, "y": 40},
            },
            {
                "node_id": "retrieve",
                "kind": "implement",
                "label": "检索 / 学习",
                "goal": "检索文献或资料并消化要点",
                "prefers": "other",
                "bind_later": True,
                "position": {"x": 200, "y": 150},
            },
            {
                "node_id": "judge",
                "kind": "reduce",
                "label": "评判",
                "goal": "筛选可靠来源，列出可用论点与缺口",
                "bind_later": True,
                "position": {"x": 200, "y": 260},
            },
            {
                "node_id": "write_body",
                "kind": "implement",
                "label": "撰写正文",
                "goal": "按评判结果撰写主体文稿",
                "prefers": "copy",
                "bind_later": True,
                "position": {"x": 80, "y": 370},
            },
            {
                "node_id": "write_companion",
                "kind": "implement",
                "label": "撰写摘要 / 并列稿",
                "goal": "并行撰写摘要、提纲或配套小节",
                "prefers": "copy",
                "bind_later": True,
                "position": {"x": 320, "y": 370},
            },
            {
                "node_id": "review",
                "kind": "audit",
                "label": "审查",
                "goal": "对照目标与来源审查文稿",
                "audit_target": "write_body",
                "max_audit_retries": 2,
                "bind_later": True,
                "position": {"x": 200, "y": 480},
            },
            {
                "node_id": "summarize",
                "kind": "reduce",
                "label": "汇总",
                "goal": "给出可交付文稿与修订说明",
                "bind_later": True,
                "position": {"x": 200, "y": 590},
            },
        ],
        "edges": [
            {"from": "confirm", "to": "survey"},
            {"from": "survey", "to": "retrieve"},
            {"from": "retrieve", "to": "judge"},
            {"from": "judge", "to": "write_body"},
            {"from": "judge", "to": "write_companion"},
            {"from": "write_body", "to": "review"},
            {"from": "write_companion", "to": "review"},
            {"from": "review", "to": "summarize"},
        ],
        "finalizer_node_id": "summarize",
    }


_TEMPLATES: dict[str, Callable[[str], dict[str, Any]]] = {
    "linear": _linear_delivery,
    "fe_be_parallel": _fe_be_parallel,
    "classic_four_role": _classic_four_role,
    "research_write": _research_write,
    "nested_delivery": nested_delivery,
    "oxr_books": oxr_books,
}


def list_templates() -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for template_id, factory in _TEMPLATES.items():
        spec = OrchestrationSpec.from_value(factory(""))
        out.append({
            "template_id": template_id,
            "label": spec.label,
            "spec_id": spec.spec_id,
            "node_count": str(len(spec.nodes)),
        })
    return out


def get_template(template_id: str, goal: str = "") -> OrchestrationSpec:
    factory = _TEMPLATES.get(template_id)
    if factory is None:
        raise KeyError(f"unknown orchestration template: {template_id}")
    return OrchestrationSpec.from_value(factory(goal))
