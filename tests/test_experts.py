from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from mewcode.expert_group import EXPERT_ROLE_CONTRACTS
from mewcode.experts import (
    BUILTIN_EXPERT_NAMES,
    DEFAULT_EXPERT_REGISTRY,
    EXPERT_ROLES,
    ExpertDefinition,
)


def test_builtin_definitions_cover_all_roles() -> None:
    assert BUILTIN_EXPERT_NAMES == EXPERT_ROLES
    assert EXPERT_ROLES == ("manager", "planner", "executor", "auditor")


def test_builtin_definitions_mirror_legacy_contracts() -> None:
    """During the transition the asset layer must not drift from legacy."""
    registry = DEFAULT_EXPERT_REGISTRY
    for role in EXPERT_ROLES:
        definition = registry.get(role)
        legacy = EXPERT_ROLE_CONTRACTS[role]
        assert definition is not None
        assert definition.tools == legacy.tools
        assert definition.responsibility == legacy.responsibility
        assert definition.may_write == legacy.may_write
        assert definition.may_delegate == legacy.may_delegate


def test_roles_have_task_tools_but_only_executor_can_delegate() -> None:
    registry = DEFAULT_EXPERT_REGISTRY
    for role in EXPERT_ROLES:
        definition = registry.get(role)
        assert definition is not None
        if role != "auditor":
            assert definition.may_write is True
            assert definition.may_delegate is (role == "executor")
            assert definition.tools == ("*",)
        else:
            assert definition.may_write is False
            assert definition.may_delegate is False


def test_registry_lookup_and_membership() -> None:
    registry = DEFAULT_EXPERT_REGISTRY
    assert registry.get("executor") is not None
    assert registry.get("nonexistent") is None
    assert "manager" in registry
    assert "nope" not in registry
    assert len(registry) == 4
    assert set(registry.names()) == set(EXPERT_ROLES)
    assert {definition.name for definition in registry.list()} == set(EXPERT_ROLES)


def test_definition_round_trips_through_value() -> None:
    definition = ExpertDefinition(
        name="fixer",
        tools=("ReadFile", "WriteFile"),
        responsibility="只修列出的问题。",
        may_write=True,
    )
    restored = ExpertDefinition.from_value(definition.as_dict())
    assert restored == definition


def test_from_value_rejects_invalid_inputs() -> None:
    with pytest.raises(ValueError):
        ExpertDefinition.from_value("not a dict")
    with pytest.raises(ValueError):
        ExpertDefinition.from_value({"tools": ["ReadFile"]})
    with pytest.raises(ValueError):
        ExpertDefinition.from_value({"name": "", "tools": []})


def test_definition_is_frozen() -> None:
    definition = DEFAULT_EXPERT_REGISTRY.get("manager")
    assert definition is not None
    with pytest.raises(FrozenInstanceError):
        definition.tools = ("*",)  # type: ignore[misc]
