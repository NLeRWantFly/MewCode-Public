"""Select only frozen, capability-compatible node backup models."""
from __future__ import annotations

from typing import Any

from mewcode.model_registry import ModelRef

_EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
USER_DIFFICULT_ROUTE = "user_authorized_difficult_route"


def current_node_model(snapshot: dict[str, Any], node: dict[str, Any]) -> Any:
    """Use the executor's binding precedence, including collaborative role aliases."""
    locked, profile = snapshot.get("locked_models") or {}, snapshot.get("locked_profile") or {}
    if snapshot.get("orchestration_mode") == "collaborative":
        from mewcode.kernel.models import ALIASES
        return (locked.get(node["node_id"]) or node.get("model")
                or (snapshot.get("role_models") or {}).get("roles", {}).get(ALIASES.get(node.get("role")))
                or profile.get(node.get("role")) or profile.get("manager"))
    return (locked.get(node["node_id"]) or locked.get(node.get("role"))
            or node.get("model") or profile.get(node.get("role")))


def validate_difficulty_model(registry: Any, target: ModelRef,
                              current: ModelRef | dict[str, Any] | None = None, *,
                              allow_unavailable_current: bool = False) -> None:
    """Resolve the user's exact provider and effort, without making a request."""
    resolved = registry.resolve(target)
    if resolved.ref != target or resolved.effort != target.effort:
        raise ValueError("困难节点模型不支持所选推理档位，未自动降档或替换路由")
    required = {"chat", "tools"}
    if current is not None:
        try:
            previous = registry.resolve(ModelRef.from_value(current))
        except ValueError:
            if not allow_unavailable_current:
                raise
        else:
            required |= set(previous.capabilities)
    if not required <= set(resolved.capabilities):
        raise ValueError("困难节点模型缺少当前节点所需的工具或视觉能力")


def authorized_difficulty_route(snapshot: dict[str, Any], node: Any,
                                current: ModelRef | dict[str, Any], registry: Any
                                ) -> tuple[ModelRef | None, str]:
    """Use a host-recorded user choice; never infer a stronger cross-model route."""
    policy = snapshot.get("difficult_node_model_policy") or {}
    if (policy.get("authorized_by") != "user" or not policy.get("change_id")
            or policy.get("flow_fingerprint") != snapshot.get("flow_fingerprint")
            or node.node_id not in (policy.get("node_ids") or [])):
        return None, "no_current_user_authorized_difficult_route"
    try:
        target = ModelRef.from_value(policy.get("model"))
        if target == ModelRef.from_value(current):
            return None, "already_on_user_authorized_difficult_route"
        validate_difficulty_model(registry, target, current)
    except (ValueError, TypeError, AttributeError) as exc:
        return None, str(exc)
    return target, USER_DIFFICULT_ROUTE


def declared_backup(node: Any, current: ModelRef | dict[str, Any], registry: Any
                    ) -> tuple[ModelRef | None, str]:
    candidates = getattr(node, "backup_models", ()) or ()
    if not candidates:
        return None, "no_declared_backup"
    try:
        active = ModelRef.from_value(current)
        active_capabilities = set(registry.resolve(active).capabilities)
    except (ValueError, TypeError, AttributeError):
        # A service failure is not permission to assume the current model's
        # required modalities or select a weaker fallback.
        return None, "current_capabilities_unknown"
    for candidate in candidates:
        ref = ModelRef.from_value(candidate)
        if (ref.provider_key, ref.model_id) == (active.provider_key, active.model_id):
            continue
        try:
            capabilities = set(registry.resolve(ref).capabilities)
        except (ValueError, TypeError, AttributeError):
            continue
        if not {"chat", "tools"} <= capabilities:
            continue
        if "vision" in active_capabilities and "vision" not in capabilities:
            continue
        return ref, "declared_capability_compatible"
    return None, "no_compatible_declared_backup"


def declared_reasoning_upgrade(node: Any, current: ModelRef | dict[str, Any], registry: Any
                               ) -> tuple[ModelRef | None, str]:
    """Select a frozen same-model *higher effort*, never infer cross-model IQ.

    A low-confidence judgment is a recovery signal, not proof that a different
    model is stronger. We require an explicit node backup and a verified effort
    level on the same provider/model, preserving tools and modality.
    """
    try:
        active = ModelRef.from_value(current)
        resolved = registry.resolve(active)
        levels = registry._effort_levels(resolved.protocol, active.model_id, resolved.provider)
        current_effort = active.effort or resolved.effort
        if current_effort not in levels or current_effort not in _EFFORT_ORDER:
            return None, "current_effort_unknown"
        required = set(resolved.capabilities) | {"chat", "tools"}
    except (ValueError, TypeError, AttributeError):
        return None, "current_effort_unknown"
    for candidate in getattr(node, "backup_models", ()) or ():
        try:
            ref = ModelRef.from_value(candidate)
            if ((ref.provider_key, ref.model_id) != (active.provider_key, active.model_id)
                    or ref.effort not in levels or ref.effort not in _EFFORT_ORDER
                    or _EFFORT_ORDER.index(ref.effort) <= _EFFORT_ORDER.index(current_effort)):
                continue
            upgraded = registry.resolve(ref)
            if (upgraded.effort != ref.effort
                    or not required <= set(upgraded.capabilities)):
                continue
        except (ValueError, TypeError, AttributeError):
            continue
        return ref, "declared_higher_effort_same_model"
    return None, "no_declared_higher_effort"


def bind_declared_backup(snapshot: dict[str, Any], *, node_id: str, role: str,
                         expected_current: ModelRef | dict[str, Any],
                         backup: ModelRef, input_revision: str,
                         flow_fingerprint: str, receipt_id: str,
                         reason: str = "declared_model_service_recovery") -> dict[str, Any]:
    """Atomically mutate an in-memory checkpoint, or raise before any change."""
    from mewcode.experts.recovery_state import blockers
    if reason not in {"declared_model_service_recovery", "declared_reasoning_upgrade", USER_DIFFICULT_ROUTE}:
        raise ValueError("unknown model switch reason")
    stage = (snapshot.get("node_states") or {}).get(node_id) or {}
    if (not receipt_id or not input_revision or stage.get("input_revision") != input_revision
            or snapshot.get("flow_fingerprint") != flow_fingerprint or blockers(stage)):
        raise ValueError("node input or host boundary changed before model switch")
    node = next((row for row in (snapshot.get("locked_flow") or {}).get("nodes") or []
                 if isinstance(row, dict) and row.get("node_id") == node_id), None)
    if not node:
        raise ValueError("node is not in the frozen flow")
    if reason == USER_DIFFICULT_ROUTE:
        policy = snapshot.get("difficult_node_model_policy") or {}
        if (policy.get("authorized_by") != "user" or not policy.get("change_id")
                or policy.get("flow_fingerprint") != flow_fingerprint
                or node_id not in (policy.get("node_ids") or [])
                or ModelRef.from_value(policy.get("model")) != backup):
            raise ValueError("difficulty model is not the current user-authorized route")
    elif backup.as_dict() not in (node.get("backup_models") or []):
        raise ValueError("backup model is not in the frozen node contract")
    bindings = snapshot.get("locked_models") or {}
    current = bindings.get(node_id) or bindings.get(role)
    if current is None or ModelRef.from_value(current) != ModelRef.from_value(expected_current):
        raise ValueError("active model binding changed before switch")
    if any(row.get("receipt_id") == receipt_id for row in snapshot.get("model_route_recoveries") or []):
        raise ValueError("model switch receipt already exists")
    receipt = {"schema_version": 1, "receipt_id": receipt_id, "node_id": node_id,
               "input_revision": input_revision, "flow_fingerprint": flow_fingerprint,
               "from": ModelRef.from_value(current).as_dict(), "to": backup.as_dict(),
               "reason": reason, "changes_contract": False}
    snapshot.setdefault("locked_models", {})[node_id] = backup.as_dict()
    snapshot.setdefault("model_route_recoveries", []).append(receipt)
    return receipt
