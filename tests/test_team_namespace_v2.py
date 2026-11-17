from __future__ import annotations

import pytest

from mewcode.teams.store_v2 import TeamStateError
from mewcode.teams.registry import AgentNameRegistry
from test_team_store_v2 import actor, setup_team, store


def add_reviewer(store, api, team, run, principal="reviewer", alias="reviewer", version=2, command="member"):
    return store.teams_v2.add_member(api.actor, team["team_id"], run["team_run_id"], alias=alias,
                                     role_id="reviewer", principal_ref=principal,
                                     expected_version=version, control_epoch=1, command_id=command)


def test_same_aliases_are_isolated_from_each_other_and_legacy_registry(store):
    a, team_a, run_a = setup_team(store, "lead-a", prefix="a")
    b, team_b, run_b = setup_team(store, "lead-b", prefix="b")
    add_reviewer(store, a, team_a, run_a, "reviewer-a")
    add_reviewer(store, b, team_b, run_b, "reviewer-b")
    AgentNameRegistry.instance().register("lead", "wrong-global-lead")
    for api, team, run, owner, reviewer in (
        (a, team_a, run_a, "lead-a", "reviewer-a"),
        (b, team_b, run_b, "lead-b", "reviewer-b"),
    ):
        assert store.teams_v2.resolve_member(api.actor, team["team_id"], run["team_run_id"], "lead") == owner
        assert store.teams_v2.resolve_member(api.actor, team["team_id"], run["team_run_id"], "REVIEWER") == reviewer
    assert AgentNameRegistry.instance().resolve("lead") == "wrong-global-lead"


def test_member_is_limited_to_its_own_run(store):
    api, team, first = setup_team(store)
    add_reviewer(store, api, team, first)
    second = api.plan_run(team["team_id"], objective="second", budget_ref="b",
                          expected_version=3, control_epoch=1, command_id="second-run")
    reviewer = actor("reviewer", permissions={"team.read"})
    view = store.teams_v2.get_run(reviewer, team["team_id"], first["team_run_id"])
    assert "compiled" not in view
    assert "objective" not in view["run"]
    assert view["tasks"][0]["reviewer_role"] == "reviewer"
    assert store.teams_v2.resolve_member(reviewer, team["team_id"], first["team_run_id"], "lead") == "owner"
    with pytest.raises(TeamStateError, match="RUN_NOT_ACCESSIBLE"):
        store.teams_v2.resolve_member(reviewer, team["team_id"], second["team_run_id"], "lead")


def test_alias_conflict_case_insensitive_and_lead_reserved(store):
    api, team, run = setup_team(store)
    add_reviewer(store, api, team, run)
    with pytest.raises(TeamStateError, match="ALIAS_CONFLICT"):
        add_reviewer(store, api, team, run, principal="other", alias="REVIEWER", version=3, command="dup")
    with pytest.raises(TeamStateError, match="RESERVED_ALIAS"):
        add_reviewer(store, api, team, run, alias="LEAD", version=3, command="lead")


def test_member_cannot_self_accept_across_roles(store):
    api, team, run = setup_team(store)
    store.teams_v2.add_member(api.actor, team["team_id"], run["team_run_id"], alias="producer",
                              role_id="producer", principal_ref="same-person", expected_version=2,
                              control_epoch=1, command_id="producer")
    with pytest.raises(TeamStateError, match="SELF_ACCEPTANCE"):
        add_reviewer(store, api, team, run, "same-person", version=3)


def test_member_add_is_idempotent_without_rewriting_ownership(store):
    api, team, run = setup_team(store)
    first = add_reviewer(store, api, team, run)
    assert add_reviewer(store, api, team, run) == first
    assert store.teams_v2.get_team(api.actor, team["team_id"])["state_version"] == 3


def test_unknown_member_does_not_fall_back_to_global_registry(store):
    api, team, run = setup_team(store)
    AgentNameRegistry.instance().register("other", "another-team")
    with pytest.raises(TeamStateError, match="MEMBER_NOT_FOUND"):
        store.teams_v2.resolve_member(api.actor, team["team_id"], run["team_run_id"], "other")
