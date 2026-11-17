"""Declarative control plan derived from a confirmed OrchestrationSpec.

This is not a user-script surface. Stages, retries, degrade/fallback, and
human checkpoints are data; execution still goes through ``to_expert_flow``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from mewcode.orchestration.convert import to_expert_flow
from mewcode.orchestration.spec import OrchestrationError, OrchestrationSpec

StageMode = Literal["sequential", "parallel"]
RetryAction = Literal["retry", "route", "fallback", "stop"]


@dataclass(frozen=True)
class ControlStage:
    stage_id: str
    order: int
    mode: StageMode
    node_ids: tuple[str, ...]
    label: str = ""

    @classmethod
    def from_value(cls, value: Any) -> "ControlStage":
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise OrchestrationError("control stage must be an object")
        node_ids = tuple(
            str(item).strip()
            for item in (value.get("node_ids") or value.get("nodes") or [])
            if str(item).strip()
        )
        if not node_ids:
            raise OrchestrationError("control stage needs node_ids")
        mode = str(value.get("mode") or "").strip()
        if mode not in {"sequential", "parallel"}:
            mode = "parallel" if len(node_ids) > 1 else "sequential"
        return cls(
            stage_id=str(value.get("stage_id") or value.get("id") or "").strip()
            or f"stage-{value.get('order') or 0}",
            order=int(value.get("order") or 0),
            mode=mode,  # type: ignore[arg-type]
            node_ids=node_ids,
            label=str(value.get("label") or "").strip(),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "order": self.order,
            "mode": self.mode,
            "node_ids": list(self.node_ids),
            "label": self.label,
        }


@dataclass(frozen=True)
class RetryRule:
    node_id: str
    max_attempts: int
    action: RetryAction = "retry"

    @classmethod
    def from_value(cls, value: Any) -> "RetryRule":
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise OrchestrationError("retry rule must be an object")
        node_id = str(value.get("node_id") or "").strip()
        if not node_id:
            raise OrchestrationError("retry rule needs node_id")
        action = str(value.get("action") or "retry").strip()
        if action not in {"retry", "route", "fallback", "stop"}:
            raise OrchestrationError(f"unknown retry action: {action}")
        attempts = int(value.get("max_attempts") or 1)
        if attempts < 1 or attempts > 10:
            raise OrchestrationError("retry max_attempts must be 1..10")
        return cls(node_id=node_id, max_attempts=attempts, action=action)  # type: ignore[arg-type]

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "max_attempts": self.max_attempts,
            "action": self.action,
        }


@dataclass(frozen=True)
class DegradeRule:
    node_id: str
    action: Literal["route", "fallback"]
    target_node_id: str
    max_attempts: int = 1

    @classmethod
    def from_value(cls, value: Any) -> "DegradeRule":
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise OrchestrationError("degrade rule must be an object")
        node_id = str(value.get("node_id") or "").strip()
        target = str(value.get("target_node_id") or value.get("fallback_node_id") or "").strip()
        action = str(value.get("action") or "route").strip()
        if action not in {"route", "fallback"}:
            raise OrchestrationError(f"unknown degrade action: {action}")
        if not node_id or not target:
            raise OrchestrationError("degrade rule needs node_id and target_node_id")
        attempts = int(value.get("max_attempts") or 1)
        if attempts < 1 or attempts > 10:
            raise OrchestrationError("degrade max_attempts must be 1..10")
        return cls(
            node_id=node_id,
            action=action,  # type: ignore[arg-type]
            target_node_id=target,
            max_attempts=attempts,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "action": self.action,
            "target_node_id": self.target_node_id,
            "max_attempts": self.max_attempts,
        }


@dataclass(frozen=True)
class HumanCheckpoint:
    node_id: str
    kind: str = "gate"
    label: str = ""
    required: bool = True

    @classmethod
    def from_value(cls, value: Any) -> "HumanCheckpoint":
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise OrchestrationError("checkpoint must be an object")
        node_id = str(value.get("node_id") or "").strip()
        if not node_id:
            raise OrchestrationError("checkpoint needs node_id")
        return cls(
            node_id=node_id,
            kind=str(value.get("kind") or "gate").strip() or "gate",
            label=str(value.get("label") or "").strip(),
            required=bool(value.get("required", True)),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": self.kind,
            "label": self.label,
            "required": self.required,
        }


@dataclass(frozen=True)
class ControlPlan:
    """Order/parallel stages plus retry, degrade, and human checkpoints."""

    stages: tuple[ControlStage, ...] = ()
    retries: tuple[RetryRule, ...] = ()
    degrade: tuple[DegradeRule, ...] = ()
    checkpoints: tuple[HumanCheckpoint, ...] = ()
    spec_id: str = ""
    version: int = 1
    draft: bool = True
    loops: tuple[dict[str, Any], ...] = ()

    @classmethod
    def from_value(cls, value: Any) -> "ControlPlan":
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise OrchestrationError("control plan must be an object")
        return cls(
            stages=tuple(
                ControlStage.from_value(item) for item in (value.get("stages") or [])
            ),
            retries=tuple(
                RetryRule.from_value(item) for item in (value.get("retries") or [])
            ),
            degrade=tuple(
                DegradeRule.from_value(item) for item in (value.get("degrade") or [])
            ),
            checkpoints=tuple(
                HumanCheckpoint.from_value(item)
                for item in (value.get("checkpoints") or [])
            ),
            spec_id=str(value.get("spec_id") or "").strip(),
            version=int(value.get("version") or 1),
            draft=bool(value.get("draft", True)),
            loops=tuple(value.get("loops") or ()),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "stages": [stage.as_dict() for stage in self.stages],
            "retries": [rule.as_dict() for rule in self.retries],
            "degrade": [rule.as_dict() for rule in self.degrade],
            "checkpoints": [item.as_dict() for item in self.checkpoints],
            "spec_id": self.spec_id,
            "version": self.version,
            "draft": self.draft,
            "loops": list(self.loops),
        }

    def summary(self) -> str:
        order = " ⇉ ".join(
            "+".join(stage.node_ids) if stage.mode == "parallel" else ",".join(stage.node_ids)
            for stage in self.stages
        )
        extras: list[str] = []
        if self.retries:
            extras.append(f"重试 {len(self.retries)}")
        if self.degrade:
            extras.append(f"降级 {len(self.degrade)}")
        if self.checkpoints:
            extras.append(f"检查点 {len(self.checkpoints)}")
        if self.loops:
            extras.append(f"协作循环 {len(self.loops)}")
        suffix = f"（{'，'.join(extras)}）" if extras else ""
        return (order or "（空计划）") + suffix


def _stage_label(spec: OrchestrationSpec, node_ids: tuple[str, ...], mode: str) -> str:
    nodes = spec.node_map()
    labels = [nodes[nid].label for nid in node_ids if nid in nodes]
    if mode == "parallel" and len(labels) > 1:
        return "并行：" + " / ".join(labels)
    return labels[0] if labels else ",".join(node_ids)


def build_control_plan(
    spec: OrchestrationSpec,
    *,
    draft: bool = True,
) -> ControlPlan:
    """Derive a declarative control plan from Spec topology and node policies."""
    flow = to_expert_flow(spec)
    batches = flow.topological_batches()
    stages: list[ControlStage] = []
    for index, batch in enumerate(batches):
        mode: StageMode = "parallel" if len(batch) > 1 else "sequential"
        stages.append(
            ControlStage(
                stage_id=f"stage-{index + 1}",
                order=index + 1,
                mode=mode,
                node_ids=batch,
                label=_stage_label(spec, batch, mode),
            )
        )

    scheduled_ids = {node.node_id for node in flow.nodes}
    loop_ids = {node_id for loop in flow.meta.get("loops") or [] for node_id in loop["node_ids"]}
    retries: list[RetryRule] = []
    degrade: list[DegradeRule] = []
    checkpoints: list[HumanCheckpoint] = []
    for node in spec.nodes:
        if spec.meta.get('workflow_core'):
            continue  # Core owns dependencies and explicit controls, without legacy retries.
        # Constraint-pipeline children are not scheduled; skip their control rules.
        if node.node_id not in scheduled_ids:
            continue
        if node.node_id in loop_ids:
            continue  # The shared loop watchdog owns retries and escalation.
        if node.kind == "gate":
            checkpoints.append(
                HumanCheckpoint(
                    node_id=node.node_id,
                    kind="gate",
                    label=node.label or "确认编排",
                    required=True,
                )
            )
        if node.kind == "implement":
            retries.append(
                RetryRule(node_id=node.node_id, max_attempts=3, action="retry")
            )
        if node.kind == "audit" and node.audit_target:
            target = node.audit_target
            flow_node = flow.node_map.get(node.node_id)
            if (
                flow_node is not None
                and flow_node.failure_policy.action == "route"
                and flow_node.failure_policy.target_node_id
            ):
                target = flow_node.failure_policy.target_node_id
            if target not in scheduled_ids:
                continue
            degrade.append(
                DegradeRule(
                    node_id=node.node_id,
                    action="route",
                    target_node_id=target,
                    max_attempts=node.max_audit_retries,
                )
            )
            retries.append(
                RetryRule(
                    node_id=node.node_id,
                    max_attempts=node.max_audit_retries,
                    action="route",
                )
            )

    return ControlPlan(
        stages=tuple(stages),
        retries=tuple(retries),
        degrade=tuple(degrade),
        checkpoints=tuple(checkpoints),
        spec_id=spec.spec_id,
        version=spec.version,
        draft=draft,
        loops=tuple(flow.meta.get("loops") or ()),
    )
