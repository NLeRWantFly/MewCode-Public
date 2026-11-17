"""Propose an orchestration graph from a natural-language goal."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any

from mewcode.orchestration.control_plan import build_control_plan
from mewcode.orchestration.convert import apply_profile_bindings, merge_node_models
from mewcode.orchestration.plan_session import PlanSession, build_plan_session
from mewcode.orchestration.spec import OrchestrationError, OrchestrationSpec
from mewcode.orchestration.templates import get_template
from mewcode.orchestration.validate import validate_spec

_FE_RE = re.compile(
    r"(前端|ui|ux|css|html|react|vue|svelte|界面|样式|组件)",
    re.I,
)
_BE_RE = re.compile(
    r"(后端|api|服务端|数据库|sql|server|路由|接口|python|fastapi)",
    re.I,
)
_RESEARCH_RE = re.compile(r"(论文|文献|撰写)")


def pick_template_id(goal: str, template_id: str = "") -> str:
    if template_id:
        return template_id
    text = goal or ""
    if _RESEARCH_RE.search(text):
        return "research_write"
    if _FE_RE.search(text) and _BE_RE.search(text):
        return "fe_be_parallel"
    return "linear"


def try_parse_planner_json(text: str) -> dict[str, Any] | None:
    """Extract a JSON object from planner model output if present."""
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, count=1, flags=re.I)
        text = re.sub(r"\s*```\s*$", "", text)
        text = text.strip()
    try:
        value = json.loads(text)
        if isinstance(value, dict) and value.get("nodes"):
            return value
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\"nodes\"[\s\S]*\}", text)
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) and value.get("nodes") else None


def _coerce_spec(value: Any) -> OrchestrationSpec | None:
    if value is None:
        return None
    if isinstance(value, OrchestrationSpec):
        return value
    if isinstance(value, PlanSession) and value.spec is not None:
        return value.spec
    if not isinstance(value, dict):
        return None
    raw = value
    if "nodes" not in raw and isinstance(raw.get("spec"), dict):
        raw = raw["spec"]
    try:
        return OrchestrationSpec.from_value(raw)
    except (OrchestrationError, TypeError, ValueError):
        return None


def _stamp_goal(spec: OrchestrationSpec, goal: str, revision: str = "") -> OrchestrationSpec:
    if not goal and not revision:
        return spec
    nodes = []
    for node in spec.nodes:
        if node.kind == "plan":
            text = goal or node.goal
            if revision:
                text = f"{text}\n修订：{revision}".strip() if text else revision
            nodes.append(replace(node, goal=text))
        else:
            nodes.append(node)
    return replace(spec, nodes=tuple(nodes), goal=goal or spec.goal)


def _apply_bindings(
    spec: OrchestrationSpec,
    bindings: dict[str, Any] | None,
    previous: OrchestrationSpec | None,
) -> OrchestrationSpec:
    spec = merge_node_models(spec, previous)
    bindings = bindings or {}
    return apply_profile_bindings(
        spec,
        manager=bindings.get("manager"),
        planner=bindings.get("planner"),
        executor=bindings.get("executor"),
        auditor=bindings.get("auditor"),
        frontend=bindings.get("frontend"),
        backend=bindings.get("backend"),
    )


def _spec_from_template(goal: str, template_id: str) -> OrchestrationSpec:
    chosen = pick_template_id(goal, template_id)
    try:
        spec = get_template(chosen, goal=goal)
    except KeyError as exc:
        raise OrchestrationError(str(exc)) from exc
    if goal:
        spec = _stamp_goal(spec, goal)
    return spec


def propose_orchestration(
    goal: str,
    *,
    template_id: str = "",
    bindings: dict[str, Any] | None = None,
    raw_spec: dict[str, Any] | None = None,
    previous_spec: dict[str, Any] | OrchestrationSpec | PlanSession | None = None,
    revision: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Build a draft OrchestrationSpec.

    If ``raw_spec`` is provided (e.g. planner model JSON), validate and normalize it.
    If ``previous_spec`` + ``revision`` are provided, refresh the stage graph from
    heuristics or planner JSON and merge prior node bindings.
    Otherwise select a template from the goal heuristics.
    """
    goal = (goal or "").strip()
    revision = (revision or "").strip()
    previous = _coerce_spec(previous_spec)
    previous_session = previous_spec if isinstance(previous_spec, PlanSession) else None
    if previous is not None and not goal:
        goal = previous.goal
    if previous_session is not None and not session_id:
        session_id = previous_session.session_id
    if previous is not None and not session_id:
        session_id = str((previous.meta or {}).get("session_id") or "")
    if previous_session is None and previous is not None:
        previous_session = PlanSession.from_value({
            "session_id": session_id or str(previous.meta.get("session_id") or ""),
            "goal": previous.goal,
            "spec": previous.as_dict(),
            "turn_summaries": previous.meta.get("turn_summaries") or [],
            "bindings": previous.meta.get("bindings") or {},
            "status": previous.meta.get("plan_status") or "drafting",
        })
        if not session_id:
            session_id = previous_session.session_id

    planner_from_revision = try_parse_planner_json(revision) if revision else None
    if planner_from_revision is not None and raw_spec is None:
        raw_spec = planner_from_revision

    revision_applied = bool(revision) or (
        previous is not None and raw_spec is not None
    )

    if raw_spec is not None:
        try:
            spec = OrchestrationSpec.from_value(
                {**raw_spec, "goal": goal or raw_spec.get("goal") or ""}
            )
        except (OrchestrationError, TypeError, ValueError) as exc:
            raise OrchestrationError(f"planner spec invalid: {exc}") from exc
        if previous is not None:
            spec = replace(spec, version=max(spec.version, previous.version) + 1)
    elif previous is not None and revision:
        hint = f"{goal} {revision}".strip()
        chosen = pick_template_id(hint, template_id)
        previous_template = previous.template_id or pick_template_id(previous.goal)
        if chosen != previous_template or template_id:
            spec = _spec_from_template(goal, chosen if not template_id else template_id)
        else:
            spec = previous
            spec = _stamp_goal(spec, goal, revision)
        spec = replace(spec, version=previous.version + 1, goal=goal or spec.goal)
    elif previous is not None and not template_id:
        spec = previous
        if goal and goal != spec.goal:
            spec = _stamp_goal(spec, goal)
            spec = replace(spec, goal=goal)
    else:
        spec = _spec_from_template(goal, template_id)

    spec = _apply_bindings(spec, bindings, previous)
    meta = dict(spec.meta)
    if session_id:
        meta["session_id"] = session_id
    spec = replace(spec, meta=meta)

    warnings = validate_spec(spec, require_bindings=False)
    control_plan = build_control_plan(spec, draft=True)
    if revision_applied:
        note = f"已应用修订：{revision}" if revision else "已应用规划 JSON"
        message = note
    else:
        note = "初稿"
        message = "请为各节点绑定模型并确认后再运行专家团"

    plan_session = build_plan_session(
        spec,
        session_id=session_id or str(meta.get("session_id") or ""),
        goal=goal or spec.goal,
        bindings=bindings,
        previous=previous_session,
        revision=revision,
        note=note,
        draft_plan=True,
    )
    meta = dict(spec.meta)
    meta["session_id"] = plan_session.session_id
    meta["turn_summaries"] = [item.as_dict() for item in plan_session.turn_summaries]
    meta["plan_status"] = plan_session.status
    if bindings:
        meta["bindings"] = dict(bindings)
    spec = replace(spec, meta=meta)
    plan_session = replace(plan_session, spec=spec)

    return {
        "spec": spec.as_dict(),
        "warnings": warnings,
        "template_id": spec.template_id or pick_template_id(goal, template_id),
        "confirmed": False,
        "message": message,
        "revision_applied": revision_applied,
        "control_plan": control_plan.as_dict(),
        "plan_session": plan_session.as_dict(),
    }
