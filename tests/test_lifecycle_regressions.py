from __future__ import annotations

import asyncio

from mewcode.agents.task_manager import BackgroundTask, TaskManager
from mewcode.teams.mailbox import create_message
from mewcode.teams.manager import TeamManager
from mewcode.teams.models import TeammateInfo
from mewcode.teams.registry import AgentNameRegistry


class StubAgent:
    """最小可用的 teammate 替身：run_to_completion 立即返回并记录 prompt。"""

    def __init__(self, agent_id: str, team_name: str, team_manager) -> None:
        self.agent_id = agent_id
        self.team_name = team_name
        self._team_manager = team_manager
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self._current_conversation = None
        self._shutdown_requested_by = None
        self.work_dir = ""
        self.prompts: list[str] = []

    async def run_to_completion(self, task, conversation=None, event_callback=None):
        self.prompts.append(task)
        return f"done: {task[:30]}"


def _member(name="alice", agent_id="a1", active=True):
    return TeammateInfo(
        name=name, agent_id=agent_id, agent_type="worker",
        model="inherit", worktree_path="", backend_type="in-process",
        is_active=active,
    )


def test_status_stays_running_during_idle_and_cancel_works(fake_home):
    """回归：status 提前置 completed 会让 TaskStop/delete_team 全部失效。"""
    async def scenario():
        manager = TeamManager()
        manager.create_team("demo", lead_agent_id="lead-uuid")
        manager.register_member("demo", _member())
        agent = StubAgent("a1", "demo", manager)
        tm = TaskManager()
        tm.teammate_idle_timeout = 10.0
        task_id = tm.launch(agent, "initial")
        manager.register_task_handle("a1", tm, task_id)

        await asyncio.sleep(1.6)  # 初始任务已完成，进入常驻空闲循环
        bg = tm.get(task_id)
        assert bg.status == "running"  # 空闲阶段必须仍是 running
        assert tm.cancel(task_id) is True  # TaskStop 路径可用

        await asyncio.sleep(0.3)
        assert bg.status == "cancelled"
        # 被取消的 teammate 立刻标记 idle（-p 模式无 app 轮询兜底）
        member = manager.get_team("demo").get_member("alice")
        assert member.is_active is False

    asyncio.run(scenario())


def test_shutdown_request_does_not_drop_batch(fake_home):
    """回归：同批次含 shutdown_request 时，普通消息不得被静默丢弃。"""
    async def scenario():
        manager = TeamManager()
        manager.create_team("demo", lead_agent_id="lead-uuid")
        mailbox = manager.get_mailbox("demo")
        agent = StubAgent("a1", "demo", manager)
        tm = TaskManager()
        tm.teammate_idle_timeout = 30.0
        bg = BackgroundTask(id="t1", name="alice", agent=agent, task="x")

        mailbox.write("a1", create_message("lead-uuid", "a1", "extra work", "s"))
        mailbox.write("a1", create_message(
            "lead", "a1", "please stop", "s", message_type="shutdown_request",
        ))

        await tm._idle_loop(bg, mailbox, "lead-uuid")

        assert any("extra work" in p for p in agent.prompts)
        inbox = mailbox.read("lead-uuid")
        assert any(m.message_type == "shutdown_response" for m in inbox)

    asyncio.run(scenario())


def test_shutdown_flag_from_active_run_is_honored(fake_home):
    """回归：运行中被 _consume_mailbox 抢先消费的 shutdown 也要握手退出。"""
    async def scenario():
        manager = TeamManager()
        manager.create_team("demo", lead_agent_id="lead-uuid")
        mailbox = manager.get_mailbox("demo")
        agent = StubAgent("a1", "demo", manager)
        agent._shutdown_requested_by = "lead"
        tm = TaskManager()
        bg = BackgroundTask(id="t1", name="alice", agent=agent, task="x")

        await tm._idle_loop(bg, mailbox, "lead-uuid")

        inbox = mailbox.read("lead-uuid")
        assert any(m.message_type == "shutdown_response" for m in inbox)
        assert agent._shutdown_requested_by is None

    asyncio.run(scenario())


def test_prune_stale_member_reuses_name(fake_home):
    """回归：同名已退出成员必须清位，重生才能复用原名。"""
    from mewcode.tools.agent_tool import _prune_stale_member

    manager = TeamManager()
    team = manager.create_team("demo", lead_agent_id="lead-uuid")
    manager.register_member("demo", _member(active=False))
    assert _prune_stale_member(team, "alice") is True
    assert team.get_member("alice") is None
    assert AgentNameRegistry.instance().resolve("alice") is None

    manager.register_member("demo", _member(name="bob", agent_id="b1", active=True))
    assert _prune_stale_member(team, "bob") is False  # 活跃成员不清
    assert team.get_member("bob") is not None


def test_lead_alias_survives_deleting_one_of_two_teams(fake_home):
    """回归：多团队共享 lead 时，删一个团队不得注销全局 lead 别名。"""
    manager = TeamManager()
    manager.create_team("t1", lead_agent_id="lead-uuid")
    manager.create_team("t2", lead_agent_id="lead-uuid")
    manager.delete_team("t1")
    assert AgentNameRegistry.instance().resolve("lead") == "lead-uuid"
    manager.delete_team("t2")
    assert AgentNameRegistry.instance().resolve("lead") is None


def test_teammate_permission_mode_merge_override():
    """回归：显式配置必须能双向覆盖（空值哨兵，而非默认值哨兵）。"""
    from mewcode.config import AppConfig, _merge_config

    base = AppConfig(providers=[], teammate_permission_mode="dontAsk")
    override = AppConfig(providers=[], teammate_permission_mode="acceptEdits")
    assert _merge_config(base, override).teammate_permission_mode == "acceptEdits"

    base2 = AppConfig(providers=[], teammate_permission_mode="acceptEdits")
    override2 = AppConfig(providers=[])  # 未显式配置
    assert _merge_config(base2, override2).teammate_permission_mode == "acceptEdits"
