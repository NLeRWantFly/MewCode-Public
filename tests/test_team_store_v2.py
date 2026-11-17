"""Team persistence uses only temporary databases and host-attested actors."""
from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from mewcode.mcp.coord_protocol import CoordinationError
from mewcode.mcp.coord_store import CoordinationStore
from mewcode.teams.service import TeamService
from mewcode.teams.spec import SpecRef, TeamSpec
from mewcode.teams.store_v2 import TeamActor, TeamStateError
from test_team_spec_v2 import compile_context, team_definition


def actor(principal="owner", project="project-a", permissions=None):
    return TeamActor(principal, project, frozenset(
        permissions if permissions is not None else {"team.read", "team.plan", "team.manage"}))


def service(store, principal="owner", project="project-a"):
    return TeamService(store, actor=actor(principal, project), compilation_context=compile_context())


def setup_team(store, principal="owner", project="project-a", prefix="a"):
    api = service(store, principal, project)
    api.register_spec(team_definition(), command_id=prefix + "-spec")
    team = api.create_team(SpecRef(spec_id="software-delivery", version=1), name="delivery", command_id=prefix + "-team")
    run = api.plan_run(team["team_id"], objective="A generic outcome", budget_ref="budget-1",
                       expected_version=1, control_epoch=1, command_id=prefix + "-run")
    return api, team, run


@pytest.fixture
def store(tmp_path):
    with CoordinationStore(tmp_path / "coord.sqlite", enable_team_v2=True) as value:
        yield value


def test_default_disabled_does_not_create_v2_tables(tmp_path):
    with CoordinationStore(tmp_path / "coord.sqlite") as store:
        with pytest.raises(CoordinationError, match="TEAM_V2_DISABLED"):
            service(store)
        tables = store._conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        assert not any(row[0].startswith("team_v2_") for row in tables)
        assert "tasks" in {row[0] for row in tables}


def test_same_database_and_explicit_opt_in_are_preserved_on_reopen(tmp_path):
    path = tmp_path / "coord.sqlite"
    with CoordinationStore(path, enable_team_v2=True) as store:
        api, team, run = setup_team(store)
        original = store.teams_v2.get_run(api.actor, team["team_id"], run["team_run_id"])
        assert original["run"]["state"] == "draft"
        assert store._conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    with CoordinationStore(path) as store:
        with pytest.raises(CoordinationError, match="TEAM_V2_DISABLED"):
            _ = store.teams_v2
    with CoordinationStore(path, enable_team_v2=True) as store:
        restored = store.teams_v2.get_run(actor(), team["team_id"], run["team_run_id"])
        assert restored == original
        assert store.teams_v2.get_team(actor(), team["team_id"])["state_version"] == 2


def test_spec_registration_is_immutable_and_deduplicated(store):
    api = service(store)
    first = api.register_spec(team_definition(), command_id="create")
    assert api.register_spec(team_definition(), command_id="create") == first
    assert api.register_spec(team_definition(), command_id="same-version") == first
    assert store._conn.execute("SELECT COUNT(*) FROM team_v2_specs").fetchone()[0] == 1
    changed = team_definition()
    changed["tasks"][0]["objective"] = "Different content"
    with pytest.raises((TeamStateError, ValueError), match="SPEC_VERSION_CONFLICT"):
        api.register_spec(changed, command_id="changed")


def test_idempotency_payload_conflict_does_not_duplicate_events(store):
    api = service(store)
    api.register_spec(team_definition(), command_id="spec")
    ref = SpecRef(spec_id="software-delivery", version=1)
    first = api.create_team(ref, name="one", command_id="create")
    assert api.create_team(ref, name="one", command_id="create") == first
    with pytest.raises(TeamStateError, match="IDEMPOTENCY_CONFLICT"):
        api.create_team(ref, name="two", command_id="create")
    assert store._conn.execute("SELECT COUNT(*) FROM team_v2_instances").fetchone()[0] == 1
    assert store._conn.execute("SELECT COUNT(*) FROM team_v2_events").fetchone()[0] == 2


def test_draft_plan_replay_returns_same_run_after_state_advanced(store):
    api, team, run = setup_team(store)
    repeated = api.plan_run(team["team_id"], objective="A generic outcome", budget_ref="budget-1",
                            expected_version=1, control_epoch=1, command_id="a-run")
    assert repeated == run
    assert store._conn.execute("SELECT COUNT(*) FROM team_v2_runs").fetchone()[0] == 1


@pytest.mark.parametrize("version,epoch,code", [(1, 1, "STATE_VERSION_CONFLICT"),
                                                (2, 2, "STALE_CONTROL_EPOCH"),
                                                (True, 1, "STATE_VERSION_CONFLICT")])
def test_stale_control_and_state_rollback_without_events(store, version, epoch, code):
    api, team, _ = setup_team(store)
    before = store._conn.execute("SELECT COUNT(*) FROM team_v2_events").fetchone()[0]
    with pytest.raises(TeamStateError, match=code):
        api.plan_run(team["team_id"], objective="next", budget_ref="budget-1",
                     expected_version=version, control_epoch=epoch, command_id="stale")
    assert store._conn.execute("SELECT COUNT(*) FROM team_v2_events").fetchone()[0] == before
    assert store._conn.execute("SELECT COUNT(*) FROM team_v2_runs").fetchone()[0] == 1


def test_cross_project_and_unrelated_principal_cannot_read_team_or_run(store):
    api, team, run = setup_team(store)
    for untrusted in (actor("owner", "project-b"), actor("stranger")):
        with pytest.raises(TeamStateError, match="TEAM_NOT_ACCESSIBLE"):
            store.teams_v2.get_team(untrusted, team["team_id"])
        with pytest.raises(TeamStateError, match="TEAM_NOT_ACCESSIBLE"):
            store.teams_v2.get_run(untrusted, team["team_id"], run["team_run_id"])


def test_readonly_actor_cannot_write_or_fake_owner(store):
    api, team, run = setup_team(store)
    readonly = actor(permissions={"team.read"})
    with pytest.raises(TeamStateError, match="PERMISSION_REQUIRED"):
        store.teams_v2.add_member(readonly, team["team_id"], run["team_run_id"], alias="reviewer",
                                  role_id="reviewer", principal_ref="reviewer", expected_version=2,
                                  control_epoch=1, command_id="no-write")
    with pytest.raises(TeamStateError, match="TEAM_NOT_ACCESSIBLE"):
        store.teams_v2.add_member(actor("intruder"), team["team_id"], run["team_run_id"], alias="reviewer",
                                  role_id="reviewer", principal_ref="reviewer", expected_version=2,
                                  control_epoch=1, command_id="no-owner")


def test_two_connections_cas_only_one_new_plan(tmp_path):
    path = tmp_path / "coord.sqlite"
    with CoordinationStore(path, enable_team_v2=True) as first, CoordinationStore(path, enable_team_v2=True) as second:
        _, team, _ = setup_team(first)
        barrier = Barrier(2)
        def plan(store, key):
            barrier.wait(timeout=5)
            try:
                return service(store).plan_run(team["team_id"], objective=key, budget_ref="budget-1",
                                                expected_version=2, control_epoch=1, command_id=key)
            except TeamStateError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            attempts = [pool.submit(plan, first, "first"), pool.submit(plan, second, "second")]
            results = [attempt.result(timeout=10) for attempt in attempts]
        assert sum(isinstance(result, dict) for result in results) == 1
        assert "STATE_VERSION_CONFLICT" in results
        assert first._conn.execute("SELECT COUNT(*) FROM team_v2_runs").fetchone()[0] == 2


def test_version_conflict_in_other_project_does_not_leak(store):
    first = service(store, project="project-a")
    second = service(store, project="project-b")
    original = first.register_spec(team_definition(), command_id="same-id")
    changed = team_definition()
    changed["tasks"][0]["objective"] = "Another project definition"
    other = second.register_spec(changed, command_id="same-id")
    assert original["spec_hash"] != other["spec_hash"]
    assert len(store.teams_v2.spec_catalog(first.actor)) == 1
    assert len(store.teams_v2.spec_catalog(second.actor)) == 1


def test_corrupt_spec_not_silently_loaded(store):
    service(store).register_spec(team_definition(), command_id="spec")
    store._conn.execute("UPDATE team_v2_specs SET spec_hash='corrupt'")
    store._conn.commit()
    with pytest.raises(TeamStateError, match="SNAPSHOT_CORRUPT"):
        store.teams_v2.spec_catalog(actor())


def test_corrupt_compiled_run_not_silently_loaded(store):
    api, team, run = setup_team(store)
    snapshot = store.teams_v2.get_run(api.actor, team["team_id"], run["team_run_id"])
    snapshot["compiled"]["spec"]["tasks"][0]["objective"] = "changed"
    store._conn.execute("UPDATE team_v2_runs SET compiled_json=?", (json.dumps(snapshot["compiled"]),))
    store._conn.commit()
    with pytest.raises(ValueError, match="SNAPSHOT_CORRUPT"):
        store.teams_v2.get_run(api.actor, team["team_id"], run["team_run_id"])


def test_unsupported_schema_is_not_reset(tmp_path):
    path = tmp_path / "coord.sqlite"
    with CoordinationStore(path, enable_team_v2=True) as store:
        store._conn.execute("UPDATE team_v2_schema SET version=999")
        store._conn.commit()
    with pytest.raises(TeamStateError, match="SCHEMA_VERSION_UNSUPPORTED"):
        CoordinationStore(path, enable_team_v2=True)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version FROM team_v2_schema").fetchone()[0] == 999


def test_failed_event_insertion_rolls_back_entire_business_command(store):
    api = service(store)
    api.register_spec(team_definition(), command_id="spec")
    store._conn.execute("CREATE TRIGGER reject_events BEFORE INSERT ON team_v2_events BEGIN SELECT RAISE(ABORT, 'injected'); END")
    store._conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        api.create_team(SpecRef(spec_id="software-delivery", version=1), name="one", command_id="create")
    assert store._conn.execute("SELECT COUNT(*) FROM team_v2_instances").fetchone()[0] == 0
    assert store._conn.execute("SELECT COUNT(*) FROM team_v2_commands WHERE command_id='create'").fetchone()[0] == 0


def test_definition_does_not_spawn_runtime_or_create_legacy_tasks(store, monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("must not spawn"))
    api, team, run = setup_team(store)
    assert run["state"] == "draft"
    assert store._conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    snapshot = store.teams_v2.get_run(api.actor, team["team_id"], run["team_run_id"])
    assert snapshot["compiled"]["definitions"][0]["spec_id"] == "software-delivery"
