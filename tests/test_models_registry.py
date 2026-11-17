from __future__ import annotations

from mewcode.teams.models import AgentTeam, TeammateInfo, unique_team_name
from mewcode.teams.registry import AgentNameRegistry


def test_team_save_load_roundtrip(tmp_path):
    team = AgentTeam(
        name="demo",
        lead_agent_id="lead123",
        config_path=str(tmp_path / "config.json"),
        description="d",
    )
    team.add_member(TeammateInfo(
        name="alice", agent_id="a1", agent_type="worker",
        model="inherit", worktree_path="/tmp/wt", backend_type="in-process",
        is_active=True,
    ))
    team.save()
    loaded = AgentTeam.load(team.config_path)
    assert loaded.lead_agent_id == "lead123"
    assert loaded.members[0].name == "alice"
    assert loaded.members[0].is_active is True


def test_unique_team_name_increments(fake_home):
    base = fake_home / ".mewcode" / "teams"
    (base / "demo").mkdir(parents=True)
    (base / "demo-2").mkdir()
    assert unique_team_name("demo") == "demo-3"
    assert unique_team_name("fresh") == "fresh"


def test_team_name_is_sanitized(fake_home):
    assert unique_team_name("My Team!!") == "my-team"


def test_registry_resolve_name_and_id():
    reg = AgentNameRegistry.instance()
    reg.register("alice", "a1")
    assert reg.resolve("alice") == "a1"
    assert reg.resolve("a1") == "a1"
    assert reg.resolve("nobody") is None
    reg.unregister("alice")
    assert reg.resolve("alice") is None


def test_get_member_by_name_or_id():
    team = AgentTeam(name="t", lead_agent_id="l")
    m = TeammateInfo(
        name="bob", agent_id="b1", agent_type="worker",
        model="inherit", worktree_path="", backend_type="in-process",
    )
    team.add_member(m)
    assert team.get_member("bob") is m
    assert team.get_member("b1") is m
    assert team.get_member("x") is None
