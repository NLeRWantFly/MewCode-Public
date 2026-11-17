"""Product-facing orchestration graph (plan / implement / reduce / audit / gate)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from mewcode.orchestration.plan_text import compact_plan_text, unique_plan_items

from mewcode.orchestration.kinds import NODE_KINDS, parse_tools
NodeKind = Literal["plan", "implement", "reduce", "audit", "gate", "decision"]


from mewcode.orchestration.errors import OrchestrationError  # re-exported


def _items(value: Any, field: str) -> list[Any]:
    """Read a list-of-items field from untrusted JSON.

    ``"nodes": 3`` used to raise "TypeError: 'int' object is not iterable",
    which escapes endpoints that only catch orchestration errors.
    """
    if value is None or value == "":
        return []
    if isinstance(value, (str, bytes, int, float, bool)):
        raise OrchestrationError(f"{field} must be a list of objects")
    try:
        return list(value)
    except TypeError:
        raise OrchestrationError(f"{field} must be a list of objects") from None


def _int_field(value: Any, default: int, field: str) -> int:
    """Parse an integer field from untrusted spec JSON.

    A bad value has to surface as OrchestrationError (which endpoints map to a
    readable 400) instead of a bare ValueError escaping to a 500.
    """
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        # OverflowError happens for floats like 1e999, which JSON parses to inf.
        raise OrchestrationError(f"{field} must be an integer, got {value!r}") from None


def parse_review_channels(value: Any) -> tuple[str, ...] | None:
    if value is None:
        return None
    channels = _items(value, 'review_channels')
    if any(channel not in ('text', 'pixel', 'command') for channel in channels):
        raise OrchestrationError('review_channels must contain only text, pixel, command')
    return tuple(channel for channel in ('text', 'pixel', 'command') if channel == 'text' or channel in channels)


@dataclass(frozen=True)
class ModelBinding:
    provider_key: str = ""
    model_id: str = ""
    effort: str = ""

    @classmethod
    def from_value(cls, value: Any) -> "ModelBinding | None":
        if value is None:
            return None
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise OrchestrationError("model binding must be an object")
        provider_key = str(value.get("provider_key") or "").strip()
        model_id = str(value.get("model_id") or value.get("model") or "").strip()
        effort = str(value.get("effort") or "").strip()
        if effort == "auto":
            effort = ""
        if not provider_key and not model_id:
            return None
        return cls(provider_key=provider_key, model_id=model_id, effort=effort)

    def as_dict(self) -> dict[str, str]:
        return {
            "provider_key": self.provider_key,
            "model_id": self.model_id,
            "effort": self.effort,
        }

    @property
    def is_complete(self) -> bool:
        return bool(self.provider_key and self.model_id)


@dataclass(frozen=True)
class PlanBrief:
    """Executable plan sketch for a stage — not a vague slogan."""

    summary: str = ""
    known_inputs: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    steps: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    acceptance: tuple[str, ...] = ()

    @classmethod
    def from_value(cls, value: Any) -> "PlanBrief | None":
        if value is None:
            return None
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            return None
        known = value.get("known_inputs") or value.get("known") or []
        questions = value.get("open_questions") or value.get("questions") or []
        if not isinstance(known, list):
            known = []
        if not isinstance(questions, list):
            questions = []
        summary = compact_plan_text(value.get("summary") or value.get("goal"))
        known_t = unique_plan_items(known)
        questions_t = unique_plan_items(questions)
        steps = unique_plan_items(value.get("steps"))
        outputs = unique_plan_items(value.get("outputs"))
        acceptance = unique_plan_items(value.get("acceptance"))
        if not summary and not known_t and not questions_t and not steps and not outputs and not acceptance:
            return None
        return cls(summary=summary, known_inputs=known_t, open_questions=questions_t,
                   steps=steps, outputs=outputs, acceptance=acceptance)

    def as_dict(self) -> dict[str, Any]:
        result = {
            "summary": self.summary,
            "known_inputs": list(self.known_inputs),
            "open_questions": list(self.open_questions),
        }
        for key in ("steps", "outputs", "acceptance"):
            if getattr(self, key):
                result[key] = list(getattr(self, key))
        return result

    @property
    def needs_clarify(self) -> bool:
        return bool(self.open_questions)


@dataclass(frozen=True)
class GoalContract:
    """Versioned objective: what to deliver, what not to do, how to verify."""

    objective: str = ""
    acceptance: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    open_assumptions: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    run_inputs: tuple[dict[str, Any], ...] = ()

    @classmethod
    def from_value(cls, value: Any) -> "GoalContract | None":
        if value is None or isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            return None
        objective = compact_plan_text(value.get("objective") or value.get("goal"))
        acceptance = unique_plan_items(value.get("acceptance"))
        constraints = unique_plan_items(value.get("constraints"))
        open_assumptions = unique_plan_items(value.get("open_assumptions"))
        assumptions = unique_plan_items(value.get("assumptions"))
        raw_inputs = value.get("run_inputs")
        if raw_inputs is None:
            raw_inputs = ()
        if not isinstance(raw_inputs, (list, tuple)) or any(not isinstance(item, dict) for item in raw_inputs):
            raise ValueError("goal_contract.run_inputs 必须为运行参数对象数组")
        run_inputs = tuple(dict(item) for item in raw_inputs)
        if not objective and not acceptance and not constraints and not open_assumptions and not assumptions and not run_inputs:
            return None
        return cls(
            objective=objective,
            acceptance=acceptance,
            constraints=constraints,
            open_assumptions=open_assumptions,
            assumptions=assumptions,
            run_inputs=run_inputs,
        )

    def as_dict(self) -> dict[str, Any]:
        result = {
            "objective": self.objective,
            "acceptance": list(self.acceptance),
            "constraints": list(self.constraints),
            "open_assumptions": list(self.open_assumptions),
        }
        if self.assumptions:
            result["assumptions"] = list(self.assumptions)
        if self.run_inputs:
            result["run_inputs"] = [dict(item) for item in self.run_inputs]
        return result


@dataclass(frozen=True)
class SpecAction:
    """One recorded change to the spec — the traceability unit."""

    action_id: str
    revision: int
    kind: str  # insert_node | remove_node | reroute | bind_model | revise_contract | recover | ...
    payload: dict[str, Any] = field(default_factory=dict)
    actor: str = "user"  # user | planner | leader_model | system
    reason: str = ""
    created_at: str = ""

    @classmethod
    def from_value(cls, value: Any) -> "SpecAction | None":
        if value is None or isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            return None
        action_id = str(value.get("action_id") or value.get("id") or "").strip()
        kind = str(value.get("kind") or "").strip()
        if not action_id or not kind:
            return None
        payload = value.get("payload") if isinstance(value.get("payload"), dict) else {}
        return cls(
            action_id=action_id,
            revision=_int_field(value.get("revision"), 0, "SpecAction.revision"),
            kind=kind,
            payload=dict(payload),
            actor=str(value.get("actor") or "user").strip() or "user",
            reason=str(value.get("reason") or "").strip(),
            created_at=str(value.get("created_at") or "").strip(),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "revision": self.revision,
            "kind": self.kind,
            "payload": dict(self.payload),
            "actor": self.actor,
            "reason": self.reason,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class LoopPolicy:
    """Unbounded-iteration loop guard: stall detection replaces iteration caps."""

    max_iterations: int = 0  # 0 = unlimited; stall detection is the only brake
    stall_threshold: int = 3  # consecutive rounds without information gain
    on_stall: str = "escalate_model"  # escalate_model | fallback | park
    fallback_node_id: str = ""

    @classmethod
    def from_value(cls, value: Any) -> "LoopPolicy | None":
        if value is None or isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            return None
        on_stall = str(value.get("on_stall") or "escalate_model").strip()
        if on_stall not in {"escalate_model", "fallback", "park"}:
            on_stall = "escalate_model"
        return cls(
            max_iterations=max(0, _int_field(value.get("max_iterations"), 0, "loop_policy.max_iterations")),
            stall_threshold=max(1, _int_field(value.get("stall_threshold"), 3, "loop_policy.stall_threshold")),
            on_stall=on_stall,
            fallback_node_id=str(value.get("fallback_node_id") or "").strip(),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_iterations": self.max_iterations,
            "stall_threshold": self.stall_threshold,
            "on_stall": self.on_stall,
            "fallback_node_id": self.fallback_node_id,
        }


@dataclass(frozen=True)
class OrchestrationNode:
    node_id: str
    kind: NodeKind
    label: str
    goal: str = ""
    prefers: str = ""  # frontend | backend | copy | other | ""
    model: ModelBinding | None = None
    bind_later: bool = False
    audit_target: str = ""  # implement node_id for audit failure route
    max_audit_retries: int = 3
    position: dict[str, float] | None = None
    plan_brief: PlanBrief | None = None
    tools: tuple[str, ...] | None = None
    deny_tools: tuple[str, ...] = ()
    fallback_node_ids: tuple[str, ...] = ()
    on_blocked: str = ""  # "" | retry | fallback | propose_change | escalate_model
    escalation_model: ModelBinding | None = None
    max_auto_recovery: int = 1
    loop_policy: LoopPolicy | None = None
    review_channels: tuple[str, ...] | None = None
    capabilities: dict[str, Any] | None = None

    @classmethod
    def from_value(cls, value: Any) -> "OrchestrationNode":
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise OrchestrationError("node must be an object")
        node_id = str(value.get("node_id") or value.get("id") or "").strip()
        kind = str(value.get("kind") or value.get("type") or "").strip()
        if not node_id:
            raise OrchestrationError("node_id is required")
        if kind not in NODE_KINDS:
            raise OrchestrationError(f"unknown node kind: {kind}")
        retries = _int_field(value.get("max_audit_retries"), 3, "max_audit_retries")
        if retries < 1 or retries > 10:
            raise OrchestrationError("max_audit_retries must be 1..10")
        auto_recovery = _int_field(value.get("max_auto_recovery"), 1, "max_auto_recovery")
        if auto_recovery < 0 or auto_recovery > 10:
            raise OrchestrationError("max_auto_recovery must be 0..10")
        on_blocked = str(value.get("on_blocked") or "").strip()
        if on_blocked and on_blocked not in {"retry", "fallback", "propose_change", "escalate_model"}:
            raise OrchestrationError(f"unknown on_blocked: {on_blocked}")
        fallback_ids = tuple(
            cleaned
            for cleaned in (
                item.strip() if isinstance(item, str) else ""
                for item in _items(value.get("fallback_node_ids"), "fallback_node_ids")
            )
            if cleaned
        )
        pos = value.get("position")
        position = None
        if isinstance(pos, dict):
            try:
                position = {"x": float(pos.get("x") or 0), "y": float(pos.get("y") or 0)}
            except (TypeError, ValueError):
                position = None
        brief = PlanBrief.from_value(value.get("plan_brief"))
        if brief is None and (
            value.get("known_inputs") is not None or value.get("open_questions") is not None
        ):
            brief = PlanBrief.from_value({
                "summary": value.get("goal") or "",
                "known_inputs": value.get("known_inputs") or [],
                "open_questions": value.get("open_questions") or [],
            })
        from mewcode.kernel.capabilities import declaration
        return cls(
            node_id=node_id,
            kind=kind,  # type: ignore[arg-type]
            label=str(value.get("label") or node_id).strip(),
            goal=compact_plan_text(value.get("goal") or value.get("responsibility")),
            prefers=str(value.get("prefers") or "").strip(),
            model=ModelBinding.from_value(value.get("model")),
            bind_later=bool(value.get("bind_later")),
            audit_target=str(value.get("audit_target") or "").strip(),
            max_audit_retries=retries,
            position=position,
            plan_brief=brief,
            tools=parse_tools(value.get("tools")),
            deny_tools=parse_tools(value.get("deny_tools")) or (),
            fallback_node_ids=fallback_ids,
            on_blocked=on_blocked,
            escalation_model=ModelBinding.from_value(value.get("escalation_model")),
            max_auto_recovery=auto_recovery,
            loop_policy=LoopPolicy.from_value(value.get("loop_policy")),
            review_channels=parse_review_channels(value.get('review_channels')),
            capabilities=declaration(value.get('capabilities')),
        )

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "node_id": self.node_id,
            "kind": self.kind,
            "label": self.label,
            "goal": self.goal,
            "prefers": self.prefers,
            "bind_later": self.bind_later,
            "audit_target": self.audit_target,
            "max_audit_retries": self.max_audit_retries,
        }
        if self.model is not None:
            result["model"] = self.model.as_dict()
        if self.position is not None:
            result["position"] = dict(self.position)
        if self.plan_brief is not None:
            result["plan_brief"] = self.plan_brief.as_dict()
        if self.tools is not None:
            result["tools"] = list(self.tools)
        if self.deny_tools:
            result["deny_tools"] = list(self.deny_tools)
        if self.fallback_node_ids:
            result["fallback_node_ids"] = list(self.fallback_node_ids)
        if self.on_blocked:
            result["on_blocked"] = self.on_blocked
        if self.escalation_model is not None:
            result["escalation_model"] = self.escalation_model.as_dict()
        if self.max_auto_recovery != 1:
            result["max_auto_recovery"] = self.max_auto_recovery
        if self.loop_policy is not None:
            result["loop_policy"] = self.loop_policy.as_dict()
        if self.review_channels is not None:
            result['review_channels'] = list(self.review_channels)
        if self.capabilities is not None:
            result['capabilities'] = self.capabilities
        return result


@dataclass(frozen=True)
class OrchestrationEdge:
    from_node: str
    to_node: str
    condition: str = "always"
    input_when: dict[str, str] | None = None
    branch_group: str = ""

    @classmethod
    def from_value(cls, value: Any) -> "OrchestrationEdge":
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise OrchestrationError("edge must be an object")
        from_node = str(value.get("from_node") or value.get("from") or "").strip()
        to_node = str(value.get("to_node") or value.get("to") or "").strip()
        if not from_node or not to_node:
            raise OrchestrationError("edge from_node and to_node are required")
        return cls(
            from_node=from_node,
            to_node=to_node,
            condition=str(value.get("condition") or "always").strip(),
            input_when=value.get("input_when"),
            branch_group=str(value.get("branch_group") or "").strip(),
        )

    def as_dict(self) -> dict[str, Any]:
        result = {
            "from_node": self.from_node,
            "to_node": self.to_node,
            "condition": self.condition,
        }
        if self.input_when is not None:
            result["input_when"] = dict(self.input_when)
        if self.branch_group:
            result["branch_group"] = self.branch_group
        return result


@dataclass(frozen=True)
class OrchestrationSpec:
    spec_id: str
    label: str
    version: int = 1
    nodes: tuple[OrchestrationNode, ...] = ()
    edges: tuple[OrchestrationEdge, ...] = ()
    finalizer_node_id: str = ""
    template_id: str = ""
    goal: str = ""
    require_confirm_gate: bool = True
    meta: dict[str, Any] = field(default_factory=dict)
    goal_contract: GoalContract | None = None
    actions: tuple[SpecAction, ...] = ()

    @classmethod
    def from_value(cls, value: Any) -> "OrchestrationSpec":
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise OrchestrationError("orchestration spec must be an object")
        nodes = tuple(
            OrchestrationNode.from_value(item) for item in _items(value.get("nodes"), "nodes")
        )
        edges = tuple(
            OrchestrationEdge.from_value(item) for item in _items(value.get("edges"), "edges")
        )
        if not nodes:
            raise OrchestrationError("orchestration needs at least one node")
        finalizer = str(
            value.get("finalizer_node_id") or value.get("finalizer") or ""
        ).strip()
        if not finalizer:
            reduce_nodes = [n for n in nodes if n.kind == "reduce"]
            finalizer = reduce_nodes[-1].node_id if reduce_nodes else nodes[-1].node_id
        meta = value.get("meta") if isinstance(value.get("meta"), dict) else {}
        meta = dict(meta)
        if isinstance(value.get("loops"), list):
            meta["loops"] = value["loops"]
        actions = tuple(
            action
            for action in (SpecAction.from_value(item) for item in _items(value.get("actions"), "actions"))
            if action is not None
        )
        return cls(
            spec_id=str(value.get("spec_id") or value.get("id") or "orchestration").strip(),
            label=str(value.get("label") or "编排图").strip(),
            version=_int_field(value.get("version"), 1, "version"),
            nodes=nodes,
            edges=edges,
            finalizer_node_id=finalizer,
            template_id=str(value.get("template_id") or "").strip(),
            goal=str(value.get("goal") or "").strip(),
            require_confirm_gate=bool(value.get("require_confirm_gate", True)),
            meta=dict(meta),
            goal_contract=GoalContract.from_value(value.get("goal_contract")),
            actions=actions,
        )

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "spec_id": self.spec_id,
            "label": self.label,
            "version": self.version,
            "nodes": [node.as_dict() for node in self.nodes],
            "edges": [edge.as_dict() for edge in self.edges],
            "finalizer_node_id": self.finalizer_node_id,
            "template_id": self.template_id,
            "goal": self.goal,
            "require_confirm_gate": self.require_confirm_gate,
            "meta": dict(self.meta),
        }
        if self.goal_contract is not None:
            result["goal_contract"] = self.goal_contract.as_dict()
        if self.actions:
            result["actions"] = [action.as_dict() for action in self.actions]
        return result

    def node_map(self) -> dict[str, OrchestrationNode]:
        return {node.node_id: node for node in self.nodes}
