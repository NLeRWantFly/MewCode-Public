from __future__ import annotations

import pytest

from mewcode.teams.manager import TeamError, TeamManager
from mewcode.teams.models import TeammateInfo
from mewcode.teams.registry import AgentNameRegistry


def _member(name="alice", agent_id="a1", active=True):
    return TeammateInfo(
        name=name, agent_id=agent_id, agent_type="worker",
        model="inherit", worktree_path="", backend_type="in-process",
        is_active=active,
    )


@pytest.fixture
def manager(fake_home):
    return TeamManager()


def test_create_team_registers_lead_alias(manager):
    team = manager.create_team("demo", lead_agent_id="lead-uuid")
    assert AgentNameRegistry.instance().resolve("lead") == "lead-uuid"
    assert team.name == "demo"


def test_get_team_lazy_load_registers_lead_alias(fake_home, manager):
    manager.create_team("demo", lead_agent_id="lead-uuid")
    AgentNameRegistry.reset()
    fresh = TeamManager()
    team = fresh.get_team("demo")
    assert team is not None
    assert AgentNameRegistry.instance().resolve("lead") == "lead-uuid"


def test_idle_notice_reaches_lead_mailbox(manager):
    """set_member_idle 写的 idle 信必须能被 drain_lead_mailbox 消费到。"""
    manager.create_team("demo", lead_agent_id="lead-uuid")
    manager.register_member("demo", _member())
    manager.set_member_idle("demo", "alice")
    notes = manager.drain_lead_mailbox()
    assert len(notes) == 1
    assert "alice" in notes[0]
    assert "<team-notification" in notes[0]


def test_delete_team_refuses_active_members(manager):
    manager.create_team("demo", lead_agent_id="lead-uuid")
    manager.register_member("demo", _member(active=True))
    with pytest.raises(TeamError):
        manager.delete_team("demo")


def test_delete_team_cleans_up(fake_home, manager):
    manager.create_team("demo", lead_agent_id="lead-uuid")
    manager.register_member("demo", _member(active=False))
    manager.delete_team("demo")
    assert manager.get_team("demo") is None
    assert not (fake_home / ".mewcode" / "teams" / "demo").exists()
    assert AgentNameRegistry.instance().resolve("lead") is None
    assert AgentNameRegistry.instance().resolve("alice") is None
