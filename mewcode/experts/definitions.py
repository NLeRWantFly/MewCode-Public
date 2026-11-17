"""Copyable expert-role definitions and a read-only registry.

This module is the asset layer for the expert team. Each expert role is a
frozen template (tools + responsibility + contract flags) that the
ExpertFactory (added in a later task) copies and overrides to create
run-scoped experts. The registry is seeded with the built-in four roles and
mirrors the legacy hardcoded EXPERT_ROLE_CONTRACTS in mewcode.expert_group
so a later swap-over stays behavior-preserving.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# The four role names that make up the expert team hierarchy.
EXPERT_ROLES: tuple[str, ...] = ("manager", "planner", "executor", "auditor")


@dataclass(frozen=True)
class ExpertDefinition:
    """One copyable expert-role template.

    tools is a tuple of tool names; "*" means the full tool set.
    may_write and may_delegate carry the contract flags that the orchestrator
    uses when building the role's registry. The flags are data, not
    enforcement: enforcement belongs to the orchestrator / tool-filter layers,
    which is why this dataclass stays a plain frozen value object.
    """

    name: str
    tools: tuple[str, ...]
    responsibility: str
    may_write: bool = False
    may_delegate: bool = False
    kind: str = ""

    @classmethod
    def from_value(cls, value: Any) -> "ExpertDefinition":
        """Build from a stored value (or pass through an existing instance)."""
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise ValueError("expert definition must be an object")
        name = str(value.get("name") or "").strip()
        if not name:
            raise ValueError("expert definition requires a name")
        raw_tools = value.get("tools") or []
        if isinstance(raw_tools, str):
            raw_tools = [raw_tools]
        if not isinstance(raw_tools, (list, tuple)):
            raise ValueError("expert definition tools must be a list")
        tools = tuple(str(tool).strip() for tool in raw_tools if str(tool).strip())
        return cls(
            name=name,
            tools=tools,
            responsibility=str(value.get("responsibility") or ""),
            may_write=bool(value.get("may_write", False)),
            may_delegate=bool(value.get("may_delegate", False)),
            kind=str(value.get("kind") or "").strip(),
        )

    def as_dict(self) -> dict[str, Any]:
        result = {
            "name": self.name,
            "tools": list(self.tools),
            "responsibility": self.responsibility,
            "may_write": self.may_write,
            "may_delegate": self.may_delegate,
        }
        if self.kind:
            result["kind"] = self.kind
        return result


# Built-in definitions mirroring the legacy EXPERT_ROLE_CONTRACTS. Content is
# kept identical on purpose: the registry is the future single source, and
# until the orchestrator swaps over, any drift between the two is a bug.
BUILTIN_EXPERT_DEFINITIONS: tuple[ExpertDefinition, ...] = (
    ExpertDefinition(
        name="manager",
        tools=("*",),
        responsibility="明确目标、约束、风险和可量化验收标准；按当前授权调查、验证和处理职责内问题，不自行委派。",
        may_write=True,
    ),
    ExpertDefinition(
        name="planner",
        tools=("*",),
        responsibility="根据已派生的成本策略制定实施方案；使用当前授权的工具取得实际证据、验证方案和修复前置条件，不自行委派。",
        may_write=True,
    ),
    ExpertDefinition(
        name="executor",
        tools=("*",),
        responsibility="负责实施与验证；工具操作遵守当前授权，仅快速模式可创建直属成员。",
        may_write=True,
        may_delegate=True,
    ),
    ExpertDefinition(
        name="auditor",
        tools=("ReadFile", "Glob", "Grep", "InspectGitState", "ReadAuditArtifact", "InspectVisualEvidence", "ReadStageResult"),
        responsibility="只读审计现有变更并复用执行证据；不运行测试、不修改、不修复、不委派。",
    ),
)

BUILTIN_EXPERT_NAMES: tuple[str, ...] = tuple(
    definition.name for definition in BUILTIN_EXPERT_DEFINITIONS
)


class ExpertRegistry:
    """Role registry seeded with built-ins and synchronized from Cordis slots."""

    def __init__(self) -> None:
        self._definitions: dict[str, ExpertDefinition] = {
            definition.name: definition
            for definition in BUILTIN_EXPERT_DEFINITIONS
        }

    def get(self, name: str) -> ExpertDefinition | None:
        return self._definitions.get(name)

    def register(self, definition: ExpertDefinition | dict[str, Any]) -> ExpertDefinition:
        parsed = ExpertDefinition.from_value(definition)
        self._definitions[parsed.name] = parsed
        return parsed

    def unregister(self, name: str) -> None:
        if name not in BUILTIN_EXPERT_NAMES:
            self._definitions.pop(name, None)

    def list(self) -> list[ExpertDefinition]:
        return list(self._definitions.values())

    def names(self) -> tuple[str, ...]:
        return tuple(self._definitions)

    def __contains__(self, name: str) -> bool:
        return name in self._definitions

    def __len__(self) -> int:
        return len(self._definitions)


DEFAULT_EXPERT_REGISTRY = ExpertRegistry()
