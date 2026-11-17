"""Trusted, request-scoped role extensions; graph JSON cannot redefine kinds."""
from contextvars import ContextVar
import re

from mewcode.experts.definitions import DEFAULT_EXPERT_REGISTRY
from mewcode.orchestration.errors import OrchestrationError

BUILTIN_KIND_ROLES = {"plan": "planner", "implement": "executor", "reduce": "manager",
                      "audit": "auditor", "decision": "auditor", "gate": "manager"}
expert_kind_registry = ContextVar("expert_kind_registry", default=None)


def kind_roles(registry=None):
    registry = registry if registry is not None else expert_kind_registry.get()
    registry = registry if registry is not None else DEFAULT_EXPERT_REGISTRY
    result = dict(BUILTIN_KIND_ROLES)
    for definition in registry.list():
        kind = getattr(definition, "kind", "")
        if kind and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", kind) and kind not in result:
            result[kind] = definition.name
    return result


class NodeKinds:
    def __iter__(self):
        return iter(kind_roles())

    def __contains__(self, kind):
        return kind in kind_roles()


NODE_KINDS = NodeKinds()


def parse_tools(value):
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise OrchestrationError("tools must be a list of nonempty tool names")
    return tuple(dict.fromkeys(v.strip() for v in value))
