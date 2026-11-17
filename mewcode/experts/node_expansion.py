"""Runtime micrograph expansion proposals: data contract and pure validator.

An active implement/executor node may ask the host to expand itself into a
bounded, single-level micrograph (sequence / fan-out / one join). This module
only validates proposals and stamps stable identities; activation, scheduling
and persistence live in the host runtime.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

JOIN_NODE_ID = "$join"
FORBIDDEN_EXPANSION_TOOLS = frozenset({
    "Agent", "TeamCreate", "TeamDelete", "TaskRetry", "ProposeNodeExpansion",
})
EXPANSION_REJECTION_TOOL = "NodeExpansionRejected"


class ExpansionNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    label: str = Field(min_length=1, max_length=160)
    task: str = Field(min_length=1, max_length=4000)
    input_refs: list[str] = Field(min_length=1, max_length=16)
    outputs: list[str] = Field(min_length=1, max_length=16)
    acceptance: list[str] = Field(min_length=1, max_length=16)
    tools: list[str] = Field(default_factory=list, max_length=32)
    write_paths: list[str] = Field(default_factory=list, max_length=32)
    # Omission retains the legacy full-parent contract. Explicit scopes must
    # collectively cover the parent and are rechecked at publication.
    requirement_ids: list[str] | None = Field(default=None, min_length=1)


class ExpansionEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_node: str = Field(min_length=1, max_length=64)
    to_node: str = Field(min_length=1, max_length=64)
    condition: Literal["always"] = "always"


class ExpansionJoin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=160)
    acceptance: list[str] = Field(min_length=1, max_length=16)


class NodeExpansionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_node_id: str = Field(min_length=1, max_length=200)
    expected_flow_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2000)
    nodes: list[ExpansionNode] = Field(min_length=2, max_length=6)
    edges: list[ExpansionEdge] = Field(min_length=2, max_length=32)
    join: ExpansionJoin


class NodeExpansionValidationError(ValueError):
    """Field-scoped rejection reason persisted for the user-facing pause."""

    def __init__(self, field: str, reason: str):
        super().__init__(f"{field}: {reason}")
        self.field = field
        self.reason = reason

    def as_dict(self) -> dict[str, str]:
        return {"field": self.field, "reason": self.reason}


@dataclass
class NodeExpansionContext:
    parent_node_id: str
    flow_version: int
    input_revision: str
    node_attempt: int
    work_dir: Path
    allowed_tools: frozenset[str]
    available_input_refs: frozenset[str]
    sandbox: Any = None


class ExpansionPathSandbox:
    """Read the parent sandbox, but bound writes to declared child scopes."""

    def __init__(self, parent: Any, write_paths: tuple[str, ...]) -> None:
        self.parent = parent
        self._write_scope_invalid = False
        normalized_paths: list[str] = []
        try:
            project_root = Path(parent.project_root)
        except Exception:
            project_root = None
            self._write_scope_invalid = True
        for path in write_paths:
            try:
                candidate = Path(path).expanduser()
                if not candidate.is_absolute():
                    if project_root is None:
                        raise ValueError("project root is unavailable")
                    candidate = project_root / candidate
                normalized_paths.append(
                    os.path.normcase(str(candidate.resolve(strict=False)))
                )
            except Exception:
                # A malformed or unresolvable declaration can never broaden
                # the frozen write contract.
                self._write_scope_invalid = True
        self.write_paths = tuple(normalized_paths)

    @property
    def project_root(self) -> Path:
        return self.parent.project_root

    def __getattr__(self, name: str) -> Any:
        return getattr(self.parent, name)

    def check_declared_write_scope(self, path: str) -> tuple[bool, str]:
        """Check only this and ancestor expansion declarations, not parent paths.

        PermissionChecker uses this as a hard contract gate before grants or
        mode fast paths.  The ordinary ``check`` method still applies the
        parent's full path policy independently.
        """
        if self._write_scope_invalid:
            return False, "无法解析微图子节点声明的写入范围；该写入范围按拒绝处理。"
        try:
            candidate = Path(path).expanduser()
            if not candidate.is_absolute():
                candidate = self.project_root / candidate
            normalized = os.path.normcase(str(candidate.resolve(strict=False)))
            if not any(
                normalized == allowed
                or Path(normalized).is_relative_to(Path(allowed))
                for allowed in self.write_paths
            ):
                return False, (
                    f"路径 {path} 不在微图子节点声明的写入范围内；"
                    "运行时展开不得扩大父节点或提案的写权限。"
                )

            # A nested expansion may only narrow its parent contract.  Do not
            # call parent.check here: that remains the separate path-policy
            # responsibility of check().
            parent_scope_check = getattr(
                self.parent, "check_declared_write_scope", None
            )
            if callable(parent_scope_check):
                parent_ok, parent_reason = parent_scope_check(path)
                if not parent_ok:
                    return False, parent_reason or (
                        f"路径 {path} 超出父微图节点声明的写入范围。"
                    )
            return True, ""
        except Exception:
            return False, "无法解析待写入路径；该写入按拒绝处理。"

    def check(self, path: str, *, read_only: bool = False) -> tuple[bool, str]:
        ok, reason = self.parent.check(path, read_only=read_only)
        if not ok or read_only:
            return ok, reason
        return self.check_declared_write_scope(path)

    def grant_approved_path(self, path: str, *, read_only: bool = True) -> str:
        # User grants may satisfy the parent sandbox, but cannot mutate the
        # frozen child write contract.  Read grants remain parent-owned.
        if not read_only:
            return ""
        return self.parent.grant_approved_path(path, read_only=True)


@dataclass(frozen=True)
class ValidatedNodeExpansion:
    proposal: NodeExpansionProposal
    expansion_id: str
    normalized_write_paths: dict[str, tuple[str, ...]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "expansion_id": self.expansion_id,
            "proposal": self.proposal.model_dump(mode="json"),
            "normalized_write_paths": {
                node_id: list(paths)
                for node_id, paths in self.normalized_write_paths.items()
            },
        }


def _fail(field: str, reason: str) -> None:
    raise NodeExpansionValidationError(field, reason)


def _require_text(value: str, field: str) -> str:
    text = str(value).strip()
    if not text:
        _fail(field, "不能为空白")
    return text


def _paths_overlap(first: str, second: str) -> bool:
    if first == second:
        return True
    left, right = Path(first), Path(second)
    return right.is_relative_to(left) or left.is_relative_to(right)


@dataclass
class NodeExpansionHolder:
    """Host-side slot that records the accepted proposal or the rejection."""

    context: NodeExpansionContext
    value: dict[str, Any] | None = None
    rejection: dict[str, str] | None = None
    budget_capacity: Callable[[], dict[str, Any]] | None = None

    def set_allowed_tools(self, names: set[str] | frozenset[str]) -> None:
        self.context.allowed_tools = frozenset(names)

    def submit(self, raw: dict[str, Any]) -> ValidatedNodeExpansion:
        self.value = None
        self.rejection = None
        try:
            validated = self._validate(raw)
        except NodeExpansionValidationError as exc:
            self.rejection = exc.as_dict()
            raise
        self.value = validated.as_dict()
        return validated

    def read(self) -> dict[str, Any] | None:
        return self.value

    def _validate(self, raw: dict[str, Any]) -> ValidatedNodeExpansion:
        ctx = self.context
        try:
            proposal = NodeExpansionProposal.model_validate(raw)
        except ValidationError as exc:
            errors = exc.errors()
            first = errors[0] if errors else {}
            loc = ".".join(str(part) for part in first.get("loc", ())) or "proposal"
            raise NodeExpansionValidationError(
                loc, str(first.get("msg") or "提案格式无效")
            ) from exc
        parent_id = _require_text(proposal.parent_node_id, "parent_node_id")
        _require_text(proposal.reason, "reason")
        _require_text(proposal.join.label, "join.label")
        for item_index, item in enumerate(proposal.join.acceptance):
            _require_text(item, f"join.acceptance.{item_index}")
        if parent_id != ctx.parent_node_id:
            _fail("parent_node_id", "提案父节点与当前活动节点不一致")
        if proposal.expected_flow_version != ctx.flow_version:
            _fail("expected_flow_version", "提案针对的基础图版本已过期，请按当前图重新提交")

        node_ids = [node.node_id for node in proposal.nodes]
        if len(node_ids) != len(set(node_ids)):
            _fail("nodes", "工作节点 node_id 必须唯一")
        id_set = set(node_ids)

        seen_edges: set[tuple[str, str]] = set()
        successors: dict[str, list[str]] = {nid: [] for nid in node_ids}
        predecessors: dict[str, set[str]] = {nid: set() for nid in node_ids}
        join_predecessors: list[str] = []
        for edge in proposal.edges:
            if edge.from_node == JOIN_NODE_ID:
                _fail("edges", f"{JOIN_NODE_ID} 是保留汇合点，不能作为边的起点")
            for endpoint in (edge.from_node, edge.to_node):
                if endpoint != JOIN_NODE_ID and endpoint not in id_set:
                    _fail("edges", f"边引用了未声明的节点 {endpoint}")
            if edge.from_node == edge.to_node:
                _fail("edges", f"节点 {edge.from_node} 不允许自环边")
            key = (edge.from_node, edge.to_node)
            if key in seen_edges:
                _fail("edges", f"重复边 {edge.from_node} -> {edge.to_node}")
            seen_edges.add(key)
            if edge.to_node == JOIN_NODE_ID:
                join_predecessors.append(edge.from_node)
            else:
                successors[edge.from_node].append(edge.to_node)
                predecessors[edge.to_node].add(edge.from_node)

        indegree = {nid: len(predecessors[nid]) for nid in node_ids}
        queue = [nid for nid in node_ids if indegree[nid] == 0]
        ordered: list[str] = []
        while queue:
            nid = queue.pop()
            ordered.append(nid)
            for nxt in successors[nid]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    queue.append(nxt)
        if len(ordered) != len(node_ids):
            _fail("edges", "微图必须是无环拓扑；不允许循环或递归结构")

        reaches_join: set[str] = set()
        stack = list(join_predecessors)
        while stack:
            nid = stack.pop()
            if nid in reaches_join:
                continue
            reaches_join.add(nid)
            stack.extend(predecessors[nid])
        for nid in node_ids:
            if nid not in reaches_join:
                _fail("edges", f"节点 {nid} 无法沿有向边到达汇合点 {JOIN_NODE_ID}")

        reaches: dict[str, set[str]] = {nid: set() for nid in node_ids}
        for nid in node_ids:
            stack = list(successors[nid])
            while stack:
                cur = stack.pop()
                if cur in reaches[nid]:
                    continue
                reaches[nid].add(cur)
                stack.extend(successors[cur])

        named_refs = set(ctx.available_input_refs) | {"parent"}
        normalized: dict[str, tuple[str, ...]] = {}
        for index, node in enumerate(proposal.nodes):
            _require_text(node.label, f"nodes.{index}.label")
            _require_text(node.task, f"nodes.{index}.task")
            for item_index, item in enumerate(node.outputs):
                _require_text(item, f"nodes.{index}.outputs.{item_index}")
            for item_index, item in enumerate(node.acceptance):
                _require_text(item, f"nodes.{index}.acceptance.{item_index}")
            for item_index, ref in enumerate(node.input_refs):
                text = _require_text(ref, f"nodes.{index}.input_refs.{item_index}")
                if text in named_refs:
                    continue
                if ctx.sandbox is None:
                    _fail(
                        f"nodes.{index}.input_refs.{item_index}",
                        f"输入引用 {text} 未登记且无沙箱可按工作区路径校验",
                    )
                ok, message = ctx.sandbox.check(text, read_only=True)
                if not ok:
                    _fail(
                        f"nodes.{index}.input_refs.{item_index}",
                        f"输入引用 {text} 不可读：{message}",
                    )
            if node.tools:
                stripped_tools = [
                    _require_text(tool, f"nodes.{index}.tools.{item_index}")
                    for item_index, tool in enumerate(node.tools)
                ]
                forbidden = set(stripped_tools) & FORBIDDEN_EXPANSION_TOOLS
                if forbidden:
                    _fail(
                        f"nodes.{index}.tools",
                        f"子节点不得使用 {sorted(forbidden)[0]}；微图不允许嵌套展开或团队工具",
                    )
                extra = set(stripped_tools) - set(ctx.allowed_tools)
                if extra:
                    _fail(
                        f"nodes.{index}.tools",
                        f"工具 {sorted(extra)[0]} 超出父节点授权范围，子节点只能缩减不能新增",
                    )
            paths: list[str] = []
            for item_index, raw_path in enumerate(node.write_paths):
                text = _require_text(raw_path, f"nodes.{index}.write_paths.{item_index}")
                if ctx.sandbox is None:
                    _fail(
                        f"nodes.{index}.write_paths.{item_index}",
                        "缺少路径沙箱，无法证明写入范围受控",
                    )
                ok, message = ctx.sandbox.check(text, read_only=False)
                if not ok:
                    _fail(f"nodes.{index}.write_paths.{item_index}", message)
                candidate = Path(text).expanduser()
                if not candidate.is_absolute():
                    candidate = ctx.work_dir / candidate
                paths.append(os.path.normcase(str(candidate.resolve(strict=False))))
            normalized[node.node_id] = tuple(paths)

        for left_index, left in enumerate(proposal.nodes):
            for right in proposal.nodes[left_index + 1:]:
                if (right.node_id in reaches[left.node_id]
                        or left.node_id in reaches[right.node_id]):
                    continue
                for left_path in normalized[left.node_id]:
                    for right_path in normalized[right.node_id]:
                        if _paths_overlap(left_path, right_path):
                            _fail(
                                "nodes.write_paths",
                                f"并列节点 {left.node_id} 与 {right.node_id} 写入范围重叠："
                                f"{left_path} 与 {right_path}",
                            )

        payload = {
            "proposal": proposal.model_dump(mode="json"),
            "input_revision": ctx.input_revision,
            "node_attempt": ctx.node_attempt,
        }
        digest = hashlib.sha256(json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()[:16]
        return ValidatedNodeExpansion(
            proposal=proposal,
            expansion_id=f"mx-{digest}",
            normalized_write_paths=normalized,
        )
