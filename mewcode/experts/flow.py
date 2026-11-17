"""Validated, serializable expert-team flow specifications."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
from typing import Any, Literal

from mewcode.model_registry import ModelRef
from mewcode.runtime_paths import user_data_root


FailureAction = Literal["retry", "route", "fallback", "stop"]
ExpertNodeKind = Literal["role", "pipeline"]


class ExpertFlowError(ValueError):
    """Raised when an expert flow cannot be executed deterministically."""


def normalize_edge_condition(raw: str) -> str:
    """Canonicalize canvas/runtime edge labels to always / pass / fail."""
    text = str(raw or "").strip()
    folded = text.casefold()
    if text in {"正确", "通过", "是"} or folded in {"pass", "passed", "success", "yes", "true"}:
        return "pass"
    if text in {"错误", "未通过", "否"} or folded in {"fail", "failed", "failure", "needs_attention", "no", "false"}:
        return "fail"
    if folded in {"", "always"}:
        return "always"
    return folded or "always"


def edge_condition_matches(
    condition: str,
    *,
    passed: bool,
    status: str = "",
) -> bool:
    """Whether a handoff edge should fire for a completed predecessor."""
    kind = normalize_edge_condition(condition)
    if kind == "always":
        return True
    if kind == "pass":
        return passed
    if kind == "fail":
        return not passed
    if kind.startswith("status:"):
        return status == kind.split(":", 1)[1].strip()
    return False


@dataclass(frozen=True)
class ExpertNodePosition:
    """Persisted canvas coordinates for a one-to-one expert-flow projection."""

    x: float
    y: float

    @classmethod
    def from_value(cls, value: Any) -> "ExpertNodePosition | None":
        if value is None:
            return None
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise ExpertFlowError("node position must be an object")
        try:
            x = float(value.get("x"))
            y = float(value.get("y"))
        except (TypeError, ValueError) as exc:
            raise ExpertFlowError("node position needs numeric x and y") from exc
        if not math.isfinite(x) or not math.isfinite(y):
            raise ExpertFlowError("node position must be finite")
        return cls(x=x, y=y)

    def as_dict(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y}


@dataclass(frozen=True)
class ExpertFailurePolicy:
    action: FailureAction = "stop"
    max_attempts: int = 1
    target_node_id: str = ""
    fallback_node_ids: tuple[str, ...] = ()

    @classmethod
    def from_value(cls, value: Any) -> "ExpertFailurePolicy":
        if isinstance(value, cls):
            return value
        if value is None:
            return cls()
        if not isinstance(value, dict):
            raise ExpertFlowError("failure_policy must be an object")
        action = str(value.get("action") or "stop")
        if action not in {"retry", "route", "fallback", "stop"}:
            raise ExpertFlowError(f"unknown failure action: {action}")
        max_attempts = int(value.get("max_attempts") or 1)
        if max_attempts < 1 or max_attempts > 10:
            raise ExpertFlowError("failure max_attempts must be between 1 and 10")
        fallback_value = value.get("fallback_node_ids") or []
        if not isinstance(fallback_value, (list, tuple)):
            raise ExpertFlowError("failure fallback_node_ids must be an array")
        fallback_node_ids = tuple(
            item.strip() for item in fallback_value
            if isinstance(item, str) and item.strip()
        )
        if len(fallback_node_ids) != len(fallback_value):
            raise ExpertFlowError("failure fallback_node_ids must contain non-empty strings")
        return cls(
            action=action,  # type: ignore[arg-type]
            max_attempts=max_attempts,
            target_node_id=str(value.get("target_node_id") or "").strip(),
            fallback_node_ids=fallback_node_ids,
        )

    def as_dict(self) -> dict[str, Any]:
        result = {
            "action": self.action,
            "max_attempts": self.max_attempts,
            "target_node_id": self.target_node_id,
        }
        if self.fallback_node_ids:
            result["fallback_node_ids"] = list(self.fallback_node_ids)
        return result


@dataclass(frozen=True)
class ExpertMemberSpec:
    member_id: str
    name: str
    task: str
    model: ModelRef | None = None

    @classmethod
    def from_value(cls, value: Any) -> "ExpertMemberSpec":
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise ExpertFlowError("member must be an object")
        member_id = str(value.get("member_id") or value.get("id") or "").strip()
        if not member_id:
            raise ExpertFlowError("member_id is required")
        model_value = value.get("model")
        return cls(
            member_id=member_id,
            name=str(value.get("name") or member_id).strip(),
            task=str(value.get("task") or "").strip(),
            model=ModelRef.from_value(model_value) if model_value else None,
        )

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "member_id": self.member_id,
            "name": self.name,
            "task": self.task,
        }
        if self.model is not None:
            result["model"] = self.model.as_dict()
        return result


@dataclass(frozen=True)
class ExpertPipelineSpec:
    """Reference to a reusable project PipelineGraph."""

    graph_id: str
    entry_node_id: str = ""
    output_key: str = ""
    budget_total: int | None = None

    @classmethod
    def from_value(cls, value: Any) -> "ExpertPipelineSpec":
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise ExpertFlowError("pipeline must be an object")
        graph_id = str(value.get("graph_id") or value.get("pipeline_id") or "").strip()
        if not graph_id:
            raise ExpertFlowError("pipeline graph_id is required")
        if Path(graph_id).name != graph_id or graph_id in {".", ".."}:
            raise ExpertFlowError("pipeline graph_id must be a local graph identifier")
        raw_budget = value.get("budget_total")
        budget_total = None if raw_budget is None or raw_budget == "" else int(raw_budget)
        if budget_total is not None and budget_total < 1:
            raise ExpertFlowError("pipeline budget_total must be positive")
        return cls(
            graph_id=graph_id,
            entry_node_id=str(value.get("entry_node_id") or "").strip(),
            output_key=str(value.get("output_key") or "").strip(),
            budget_total=budget_total,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "graph_id": self.graph_id,
            "entry_node_id": self.entry_node_id,
            "output_key": self.output_key,
            "budget_total": self.budget_total,
        }


@dataclass(frozen=True)
class ExpertNodeSpec:
    node_id: str
    role: str
    label: str
    kind: ExpertNodeKind = "role"
    responsibility: str = ""
    model: ModelRef | None = None
    members: tuple[ExpertMemberSpec, ...] = ()
    reducer: str = "collect"
    failure_policy: ExpertFailurePolicy = field(default_factory=ExpertFailurePolicy)
    pipeline: ExpertPipelineSpec | None = None
    position: ExpertNodePosition | None = None
    tools: tuple[str, ...] | None = None
    deny_tools: tuple[str, ...] = ()
    backup_models: tuple[ModelRef, ...] = ()
    capabilities: dict[str, Any] | None = None

    @classmethod
    def from_value(cls, value: Any) -> "ExpertNodeSpec":
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise ExpertFlowError("node must be an object")
        node_id = str(value.get("node_id") or value.get("id") or "").strip()
        kind = str(value.get("kind") or ("pipeline" if value.get("pipeline") else "role"))
        if kind not in {"role", "pipeline"}:
            raise ExpertFlowError(f"unknown expert node kind: {kind}")
        role = str(value.get("role") or ("pipeline" if kind == "pipeline" else "")).strip()
        if not node_id:
            raise ExpertFlowError("node_id is required")
        if kind == "role" and not role:
            raise ExpertFlowError("role is required for role nodes")
        members = tuple(
            ExpertMemberSpec.from_value(item) for item in value.get("members") or []
        )
        if len(members) > 8:
            raise ExpertFlowError(f"node {node_id} has more than 8 members")
        member_ids = [member.member_id for member in members]
        if len(member_ids) != len(set(member_ids)):
            raise ExpertFlowError(f"node {node_id} has duplicate member ids")
        pipeline_value = value.get("pipeline")
        pipeline = (
            ExpertPipelineSpec.from_value(pipeline_value)
            if pipeline_value is not None else None
        )
        if kind == "pipeline" and pipeline is None:
            raise ExpertFlowError(f"pipeline node {node_id} needs a pipeline reference")
        if kind == "pipeline" and members:
            raise ExpertFlowError(f"pipeline node {node_id} cannot define members")
        if kind == "role" and pipeline is not None:
            raise ExpertFlowError(f"role node {node_id} cannot define a pipeline")
        model_value = value.get("model")
        backups = value.get("backup_models") or []
        if not isinstance(backups, (list, tuple)) or len(backups) > 3:
            raise ExpertFlowError("backup_models must be a list of at most three declared models")
        from mewcode.orchestration.kinds import parse_tools
        from mewcode.kernel.capabilities import declaration
        return cls(
            node_id=node_id,
            role=role,
            label=str(value.get("label") or node_id).strip(),
            kind=kind,  # type: ignore[arg-type]
            responsibility=str(value.get("responsibility") or "").strip(),
            model=ModelRef.from_value(model_value) if model_value else None,
            members=members,
            reducer=str(value.get("reducer") or "collect").strip(),
            failure_policy=ExpertFailurePolicy.from_value(value.get("failure_policy")),
            pipeline=pipeline,
            position=ExpertNodePosition.from_value(value.get("position")),
            tools=parse_tools(value.get("tools")),
            deny_tools=parse_tools(value.get("deny_tools")) or (),
            backup_models=tuple(ModelRef.from_value(item) for item in backups),
            capabilities=declaration(value.get('capabilities')),
        )

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "node_id": self.node_id,
            "role": self.role,
            "label": self.label,
            "kind": self.kind,
            "responsibility": self.responsibility,
            "members": [member.as_dict() for member in self.members],
            "reducer": self.reducer,
            "failure_policy": self.failure_policy.as_dict(),
        }
        if self.model is not None:
            result["model"] = self.model.as_dict()
        if self.pipeline is not None:
            result["pipeline"] = self.pipeline.as_dict()
        if self.position is not None:
            result["position"] = self.position.as_dict()
        if self.tools is not None:
            result["tools"] = list(self.tools)
        if self.deny_tools:
            result["deny_tools"] = list(self.deny_tools)
        if self.backup_models:
            result["backup_models"] = [model.as_dict() for model in self.backup_models]
        if self.capabilities is not None:
            result['capabilities'] = self.capabilities
        return result


@dataclass(frozen=True)
class ExpertEdgeSpec:
    from_node: str
    to_node: str
    condition: str = "always"
    router: str = "default"
    input_when: dict[str, str] | None = None
    branch_group: str = ""

    @classmethod
    def from_value(cls, value: Any) -> "ExpertEdgeSpec":
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise ExpertFlowError("edge must be an object")
        from_node = str(value.get("from_node") or value.get("from") or "").strip()
        to_node = str(value.get("to_node") or value.get("to") or "").strip()
        if not from_node or not to_node:
            raise ExpertFlowError("edge from_node and to_node are required")
        return cls(
            from_node=from_node,
            to_node=to_node,
            condition=str(value.get("condition") or "always").strip(),
            router=str(value.get("router") or "default").strip(),
            input_when=value.get("input_when"),
            branch_group=str(value.get("branch_group") or "").strip(),
        )

    def as_dict(self) -> dict[str, Any]:
        result = {
            "from_node": self.from_node,
            "to_node": self.to_node,
            "condition": self.condition,
            "router": self.router,
        }
        if self.input_when is not None:
            result["input_when"] = dict(self.input_when)
        if self.branch_group:
            result["branch_group"] = self.branch_group
        return result


@dataclass(frozen=True)
class ExpertFlowSpec:
    flow_id: str
    version: int
    label: str
    nodes: tuple[ExpertNodeSpec, ...]
    edges: tuple[ExpertEdgeSpec, ...]
    finalizer_node_id: str
    finalizer_plugin: str = "model"
    # Nesting / constraint metadata from orchestration:
    # - canvas_groups: refine parent→children (board + convert input)
    # - constraint_pipelines / schedule_skipped_node_ids: children folded into
    #   parent responsibility and omitted from the schedulable node list
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_value(cls, value: Any) -> "ExpertFlowSpec":
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise ExpertFlowError("expert flow must be an object")
        raw_nodes = tuple(
            ExpertNodeSpec.from_value(item) for item in value.get("nodes") or []
        )
        legacy_finalizer = str(value.get("finalizer") or "").strip()
        raw_meta = value.get("meta")
        meta = dict(raw_meta) if isinstance(raw_meta, dict) else {}
        flow = cls(
            flow_id=str(value.get("flow_id") or "").strip(),
            version=int(value.get("version") or 1),
            label=str(value.get("label") or value.get("flow_id") or "").strip(),
            nodes=raw_nodes,
            edges=tuple(ExpertEdgeSpec.from_value(item) for item in value.get("edges") or []),
            finalizer_node_id=str(
                value.get("finalizer_node_id") or legacy_finalizer
            ).strip(),
            finalizer_plugin=str(value.get("finalizer_plugin") or "model").strip(),
            meta=meta,
        )
        flow.validate()
        return flow

    @property
    def node_map(self) -> dict[str, ExpertNodeSpec]:
        return {node.node_id: node for node in self.nodes}

    def validate(
        self,
        *,
        roles: set[str] | None = None,
        routers: set[str] | None = None,
        reducers: set[str] | None = None,
        finalizers: set[str] | None = None,
        pipelines: set[str] | None = None,
    ) -> None:
        if not self.flow_id:
            raise ExpertFlowError("flow_id is required")
        if self.version < 1:
            raise ExpertFlowError("flow version must be positive")
        if not self.nodes:
            raise ExpertFlowError("expert flow needs at least one node")
        node_ids = [node.node_id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ExpertFlowError("expert flow has duplicate node ids")
        node_map = self.node_map
        if self.finalizer_node_id not in node_map:
            raise ExpertFlowError("finalizer node is missing")
        if not self.finalizer_plugin:
            raise ExpertFlowError("finalizer plugin is required")
        if finalizers is not None and self.finalizer_plugin not in finalizers:
            raise ExpertFlowError(
                f"expert flow uses unknown finalizer {self.finalizer_plugin}"
            )
        for node in self.nodes:
            if node.kind == "role" and roles is not None and node.role not in roles:
                raise ExpertFlowError(f"node {node.node_id} uses unknown role {node.role}")
            if (
                node.kind == "role"
                and reducers is not None
                and node.reducer not in reducers
            ):
                raise ExpertFlowError(f"node {node.node_id} uses unknown reducer {node.reducer}")
            if node.kind == "pipeline":
                if node.pipeline is None:
                    raise ExpertFlowError(
                        f"pipeline node {node.node_id} needs a pipeline reference"
                    )
                if pipelines is not None and node.pipeline.graph_id not in pipelines:
                    raise ExpertFlowError(
                        f"node {node.node_id} uses unknown pipeline "
                        f"{node.pipeline.graph_id}"
                    )
            policy = node.failure_policy
            if policy.action == "route":
                if not policy.target_node_id or policy.target_node_id not in node_map:
                    raise ExpertFlowError(
                        f"node {node.node_id} has an unknown failure target"
                    )
            elif policy.action == "fallback":
                targets = policy.fallback_node_ids or (
                    (policy.target_node_id,) if policy.target_node_id else ()
                )
                if not targets:
                    raise ExpertFlowError(
                        f"node {node.node_id} needs at least one fallback target"
                    )
                if any(target not in node_map for target in targets):
                    raise ExpertFlowError(
                        f"node {node.node_id} has an unknown fallback target"
                    )
                if any(target == node.node_id for target in targets):
                    raise ExpertFlowError(
                        f"node {node.node_id} cannot fall back to itself"
                    )
        seen_edges: set[tuple[str, str, str]] = set()
        from mewcode.orchestration.run_inputs import validate_branch_contract
        try:
            validate_branch_contract(self.edges, self.meta.get("run_input_contract", []), node_ids=set(node_map))
        except ValueError as exc:
            raise ExpertFlowError(str(exc)) from exc
        for edge in self.edges:
            if edge.from_node not in node_map or edge.to_node not in node_map:
                raise ExpertFlowError("edge references an unknown node")
            if edge.from_node == edge.to_node:
                raise ExpertFlowError("self-referencing edges are not allowed")
            key = (edge.from_node, edge.to_node, edge.condition)
            if key in seen_edges:
                raise ExpertFlowError("expert flow has duplicate edges")
            seen_edges.add(key)
            if routers is not None and edge.router not in routers:
                raise ExpertFlowError(f"edge uses unknown router {edge.router}")
        self.topological_batches()
        if self.meta.get("loops"):
            from mewcode.orchestration.loops import compile_loops
            try:
                _, loops = compile_loops(self.nodes, self.edges, self.meta["loops"])
                for loop in loops:
                    if any(node_map[node_id].members or node_map[node_id].kind != "role" for node_id in loop.node_ids):
                        raise ValueError("有界循环体使用独立角色节点；成员或嵌套 pipeline 请先展开为节点以统一计费")
            except (ValueError, TypeError, AttributeError) as exc:
                raise ExpertFlowError(str(exc)) from exc
        reachable = set(self.entry_node_ids())
        changed = True
        while changed:
            changed = False
            for edge in self.edges:
                if edge.from_node in reachable and edge.to_node not in reachable:
                    reachable.add(edge.to_node)
                    changed = True
        if self.finalizer_node_id not in reachable:
            raise ExpertFlowError("finalizer is not reachable")

    def entry_node_ids(self) -> tuple[str, ...]:
        targets = {edge.to_node for edge in self.edges}
        return tuple(node.node_id for node in self.nodes if node.node_id not in targets)

    def topological_batches(self) -> tuple[tuple[str, ...], ...]:
        incoming = {node.node_id: 0 for node in self.nodes}
        outgoing: dict[str, list[str]] = {node.node_id: [] for node in self.nodes}
        for edge in self.edges:
            if edge.from_node not in incoming or edge.to_node not in incoming:
                raise ExpertFlowError("edge references an unknown node")
            incoming[edge.to_node] += 1
            outgoing[edge.from_node].append(edge.to_node)
        ready = [node.node_id for node in self.nodes if incoming[node.node_id] == 0]
        batches: list[tuple[str, ...]] = []
        visited = 0
        while ready:
            batch = tuple(ready)
            batches.append(batch)
            ready = []
            for node_id in batch:
                visited += 1
                for target in outgoing[node_id]:
                    incoming[target] -= 1
                    if incoming[target] == 0:
                        ready.append(target)
        if visited != len(self.nodes):
            raise ExpertFlowError("normal expert flow edges must form a DAG")
        return tuple(batches)

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "flow_id": self.flow_id,
            "version": self.version,
            "label": self.label,
            "nodes": [node.as_dict() for node in self.nodes],
            "edges": [edge.as_dict() for edge in self.edges],
            "finalizer_node_id": self.finalizer_node_id,
            "finalizer_plugin": self.finalizer_plugin,
        }
        if self.meta:
            payload["meta"] = dict(self.meta)
        return payload


class ExpertFlowRegistry:
    def __init__(self, flows: tuple[ExpertFlowSpec, ...] = ()) -> None:
        self._flows = {flow.flow_id: flow for flow in flows}

    def register(self, flow: ExpertFlowSpec | dict[str, Any]) -> ExpertFlowSpec:
        parsed = ExpertFlowSpec.from_value(flow)
        self._flows[parsed.flow_id] = parsed
        return parsed

    def unregister(self, flow_id: str) -> None:
        self._flows.pop(flow_id, None)

    def get(self, flow_id: str) -> ExpertFlowSpec | None:
        return self._flows.get(flow_id)

    def list(self) -> list[ExpertFlowSpec]:
        return list(self._flows.values())


class ExpertFlowStore:
    """Project-keyed user flow selections stored outside the repository."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path or user_data_root() / "expert-flows.json")

    @staticmethod
    def _project_key(project_path: str | os.PathLike[str]) -> str:
        resolved = Path(project_path).expanduser().resolve()
        return os.path.normcase(str(resolved)).replace("\\", "/")

    def _load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def get(self, project_path: str | os.PathLike[str]) -> ExpertFlowSpec | None:
        value = self._load().get(self._project_key(project_path))
        if not isinstance(value, dict):
            return None
        try:
            return ExpertFlowSpec.from_value(value)
        except ExpertFlowError:
            return None

    def put(
        self,
        project_path: str | os.PathLike[str],
        flow: ExpertFlowSpec | dict[str, Any],
    ) -> ExpertFlowSpec:
        parsed = ExpertFlowSpec.from_value(flow)
        data = self._load()
        data[self._project_key(project_path)] = parsed.as_dict()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.path)
        return parsed

    def delete(self, project_path: str | os.PathLike[str]) -> None:
        data = self._load()
        if data.pop(self._project_key(project_path), None) is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.path)


DEFAULT_EXPERT_FLOW = ExpertFlowSpec.from_value({
    "flow_id": "builtin.default",
    "version": 1,
    "label": "标准专家团",
    "nodes": [
        {"node_id": "manager.intake", "role": "manager", "label": "目标管理", "reducer": "collect"},
        {"node_id": "planner", "role": "planner", "label": "方案规划", "reducer": "collect"},
        {"node_id": "executor", "role": "executor", "label": "执行", "reducer": "collect", "failure_policy": {"action": "retry", "max_attempts": 3}},
        {"node_id": "auditor", "role": "auditor", "label": "审计", "reducer": "collect", "failure_policy": {"action": "route", "max_attempts": 3, "target_node_id": "executor"}},
        {"node_id": "manager.finalize", "role": "manager", "label": "最终汇总", "reducer": "collect"},
    ],
    "edges": [
        {"from": "manager.intake", "to": "planner"},
        {"from": "planner", "to": "executor"},
        {"from": "executor", "to": "auditor"},
        {"from": "auditor", "to": "manager.finalize"},
    ],
    "finalizer": "manager.finalize",
})


DEFAULT_EXPERT_FLOW_REGISTRY = ExpertFlowRegistry((DEFAULT_EXPERT_FLOW,))


__all__ = [
    "DEFAULT_EXPERT_FLOW",
    "DEFAULT_EXPERT_FLOW_REGISTRY",
    "ExpertEdgeSpec",
    "ExpertFailurePolicy",
    "ExpertFlowError",
    "ExpertFlowRegistry",
    "ExpertFlowSpec",
    "ExpertFlowStore",
    "ExpertMemberSpec",
    "ExpertNodeSpec",
    "ExpertNodePosition",
    "ExpertPipelineSpec",
]
