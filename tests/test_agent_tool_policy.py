from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from mewcode.agents.parser import AgentDef
from mewcode.context import create_replacement_state
from mewcode.conversation import ConversationManager
from mewcode.permissions import PermissionMode
from mewcode.permissions.grants import GrantStore
from mewcode.permissions.policy import build_permission_checker
from mewcode.permissions.rules import Rule
from mewcode.tools import ToolRegistry
from mewcode.tools.agent_tool import AgentTool, AgentToolParams, _AgentRuntime
from mewcode.tools.base import ToolResult
from mewcode.tools.bash import Bash
from mewcode.tools.write_file import WriteFile
from mewcode.teams.models import BackendType
from mewcode.teams.registry import AgentNameRegistry


class _FakeChild:
    def __init__(self) -> None:
        self.agent_id = "unregistered-child"
        self.trace_id = ""
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.replacement_state = None

    async def run_to_completion(self, *_args) -> str:
        return "synthetic child result"


class _FakeTrace:
    def __init__(self) -> None:
        self.created = 0
        self.completed = []

    def create(self, **_kwargs):
        self.created += 1
        return SimpleNamespace(agent_id="trace-child")

    def update(self, *_args, **_kwargs) -> None:
        pass

    def complete(self, agent_id, status) -> None:
        self.completed.append((agent_id, status))


class _FakeTasks:
    def __init__(self) -> None:
        self.launched = 0

    def launch(self, **_kwargs) -> str:
        self.launched += 1
        self.last_launch = _kwargs
        return "synthetic-task"

    def get(self, task_id):
        assert task_id == "synthetic-task"
        return SimpleNamespace(owner_task_id=self.last_launch.get("owner_task_id", ""),
                               dispatch_turn_id=self.last_launch.get("dispatch_turn_id", ""))


class _FakeWorktrees:
    """Return a pre-created directory; never invoke Git or remove user files."""

    def __init__(self, path: Path, *, kept: bool = False) -> None:
        self.path = path
        self.kept = kept
        self.created: list[tuple[str, str]] = []
        self.cleaned: list[tuple[str, str]] = []

    async def create(self, name: str, ref: str):
        assert self.path.is_dir()
        self.created.append((name, ref))
        return SimpleNamespace(
            path=str(self.path), head_commit="synthetic-head", branch="synthetic-branch"
        )

    async def auto_cleanup(self, name: str, head_commit: str):
        self.cleaned.append((name, head_commit))
        return SimpleNamespace(
            kept=self.kept, path=str(self.path), branch="synthetic-branch"
        )


class _FakeTeam:
    def __init__(self) -> None:
        self.members = []
        self.saves = 0

    def get_member(self, name: str):
        return next((member for member in self.members if member.name == name), None)

    def remove_member(self, name: str) -> bool:
        member = self.get_member(name)
        if member is None:
            return False
        self.members.remove(member)
        return True

    def save(self) -> None:
        self.saves += 1


class _FakeTeamManager:
    def __init__(self, backend: BackendType = BackendType.IN_PROCESS) -> None:
        self.team = _FakeTeam()
        self.backend = backend
        self.handles = []

    def get_team(self, name: str):
        return self.team if name == "synthetic-team" else None

    def detect_backend(self) -> BackendType:
        return self.backend

    def register_member(self, name: str, member) -> None:
        assert name == "synthetic-team"
        self.team.members.append(member)

    def register_task_handle(self, agent_id: str, task_manager, task_id: str) -> None:
        self.handles.append((agent_id, task_manager, task_id))

    def remove_worktree(self, _path: str) -> None:
        pytest.fail("force worktree removal is forbidden")


def _isolate_names(monkeypatch) -> AgentNameRegistry:
    names = AgentNameRegistry()
    monkeypatch.setattr(AgentNameRegistry, "instance", classmethod(lambda _cls: names))
    return names


def _tool(
    workspace: Path,
    *,
    definition: AgentDef | None = None,
    parent_checker=None,
    parent_mode: PermissionMode | None = PermissionMode.AUTO,
    enable_fork: bool = False,
    worktree_manager=None,
    team_manager=None,
    teammate_permission_mode: str = "",
):
    conversation = ConversationManager()
    conversation.add_user_message("synthetic request")
    parent = SimpleNamespace(
        work_dir=str(workspace),
        permission_checker=parent_checker,
        permission_mode=parent_mode,
        registry=ToolRegistry(),
        max_iterations=3,
        agent_id="parent",
        trace_id="",
        session_id="synthetic-session",
        replacement_state=create_replacement_state(),
        _current_conversation=conversation,
    )
    loader = SimpleNamespace(
        get=lambda _name: definition,
        list_agents=lambda: [(definition.agent_type, "synthetic")] if definition else [],
    )
    tasks = _FakeTasks()
    trace = _FakeTrace()
    tool = AgentTool(
        loader, tasks, trace, parent, enable_fork=enable_fork,
        worktree_manager=worktree_manager, team_manager=team_manager,
        teammate_permission_mode=teammate_permission_mode,
    )
    created = []
    tool._new_conversation = ConversationManager
    tool._select_runtime = lambda _params, _definition: _AgentRuntime(
        client=object(), protocol="synthetic", context_window=4096, auxiliary_client=object()
    )

    def create_child(_runtime, _registry, **kwargs):
        created.append(kwargs)
        return _FakeChild()

    tool._create_child_agent = create_child
    return tool, created, tasks, trace


@pytest.mark.asyncio
@pytest.mark.parametrize("isolation", [None, "worktree"])
async def test_foreground_cancellation_closes_child_and_records_terminal_status(tmp_path, isolation):
    definition = AgentDef(agent_type="synthetic", when_to_use="test")
    worktrees = _FakeWorktrees(tmp_path)
    tool, _, _, trace = _tool(tmp_path, definition=definition,
        parent_checker=build_permission_checker(tmp_path), worktree_manager=worktrees)
    started, closed = asyncio.Event(), asyncio.Event()

    class Scope:
        async def aclose(self):
            closed.set()

    class Child(_FakeChild):
        _cordis_runtime = Scope()

        async def run_to_completion(self, *_args):
            started.set()
            await asyncio.Event().wait()

    tool._create_child_agent = lambda *_args, **_kwargs: Child()
    task = asyncio.create_task(tool.execute(AgentToolParams(
        prompt="synthetic", description="test", subagent_type="synthetic", isolation=isolation)))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    assert trace.completed == [("trace-child", "cancelled")]
    assert tool.runs["trace-child"]["status"] == "cancelled"
    assert worktrees.cleaned == []


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True])
async def test_default_child_inherits_auto_native_boundary_before_launch(
    tmp_path: Path, monkeypatch, background: bool
) -> None:
    monkeypatch.setattr("mewcode.sandbox.windows.native_runtime_ready", lambda: False)
    parent = build_permission_checker(tmp_path, mode=PermissionMode.AUTO)
    definition = AgentDef(agent_type="synthetic", when_to_use="test", permission_mode="default")
    tool, created, tasks, trace = _tool(tmp_path, definition=definition, parent_checker=parent)
    tool._parent_agent._chat_turn_id = "synthetic-turn"
    tool._parent_agent._chat_task_id = "synthetic-parent-task"

    result = await tool.execute(AgentToolParams(
        prompt="synthetic", description="test", subagent_type="synthetic",
        run_in_background=background,
    ))

    assert result.is_error is False
    assert len(created) == 1 and trace.created == 1
    assert tasks.launched == int(background)
    if background:
        assert tasks.last_launch["dispatch_turn_id"] == "synthetic-turn"
        assert tasks.last_launch["owner_task_id"] == "synthetic-parent-task"
        assert "Dispatch turn ID: synthetic-turn" in result.output
        assert "Parent task ID: synthetic-parent-task" in result.output
    checker = created[0]["permission_checker"]
    assert checker.mode is PermissionMode.AUTO
    assert checker.rule_engine is parent.rule_engine
    command = checker.check(Bash(), {"command": "git status"})
    assert command.effect == "deny"
    assert command.source == "native_boundary_unverified"


@pytest.mark.asyncio
async def test_parent_rules_grants_task_store_and_frozen_writes_survive_foreground_spawn(
    tmp_path: Path,
) -> None:
    declared = tmp_path / "declared"
    declared.mkdir()
    store = GrantStore(tmp_path)
    parent = build_permission_checker(
        tmp_path, mode=PermissionMode.AUTO, writable_scope=[declared], grant_store=store
    )
    parent.rule_engine.append_session_rule(Rule("WriteFile", "*blocked.txt", "deny"))
    parent.rule_engine.append_session_rule(Rule("WriteFile", "*ask.txt", "ask"))
    parent.task_authorization = SimpleNamespace(version="v1", covers=lambda *_a, **_k: False)
    parent.task_id = "task-parent"
    parent.plan_file_path = str(tmp_path / "plan.md")
    parent.sandbox.file_grants[str(declared / "granted.txt")] = True
    parent.sandbox.dir_grants[str(tmp_path)] = True
    definition = AgentDef(agent_type="synthetic", when_to_use="test", permission_mode="default")
    tool, created, _tasks, _trace = _tool(tmp_path, definition=definition, parent_checker=parent)

    result = await tool.execute(AgentToolParams(
        prompt="synthetic", description="test", subagent_type="synthetic"
    ))

    assert result.is_error is False
    checker = created[0]["permission_checker"]
    assert checker.grant_store is store
    assert checker.task_authorization is parent.task_authorization
    assert checker.task_id == parent.task_id
    assert checker.plan_file_path == parent.plan_file_path
    assert checker.sandbox.file_grants is parent.sandbox.file_grants
    assert checker.sandbox.dir_grants is parent.sandbox.dir_grants
    assert checker.policy_writable_roots == parent.policy_writable_roots
    write = WriteFile()
    for name, expected in (("blocked.txt", "deny"), ("ask.txt", "ask")):
        decision = checker.check(write, {
            "file_path": str(declared / name), "content": "synthetic"
        })
        assert decision.effect == expected
    outside = checker.check(write, {
        "file_path": str(tmp_path / "outside.txt"), "content": "synthetic"
    })
    assert outside.effect == "deny" and outside.source == "child_write_scope"


@pytest.mark.asyncio
async def test_broader_child_mode_is_rejected_without_creating_client_or_task(
    tmp_path: Path,
) -> None:
    parent = build_permission_checker(tmp_path, mode=PermissionMode.AUTO)
    definition = AgentDef(agent_type="synthetic", when_to_use="test", permission_mode="dontAsk")
    tool, created, tasks, trace = _tool(tmp_path, definition=definition, parent_checker=parent)
    tool._select_runtime = lambda *_args: pytest.fail("client selected before policy validation")
    tool._new_conversation = lambda: pytest.fail("conversation created before policy validation")

    result = await tool.execute(AgentToolParams(
        prompt="synthetic", description="test", subagent_type="synthetic",
        run_in_background=True,
    ))

    assert result.is_error is True
    assert "permission policy rejected" in result.output
    assert "cannot be broader" in result.output
    assert created == [] and tasks.launched == 0 and trace.created == 0
    assert tool.runs == {}


@pytest.mark.asyncio
async def test_fork_inherits_plan_mode_instead_of_old_dontask_default(tmp_path: Path) -> None:
    parent = build_permission_checker(tmp_path, mode=PermissionMode.PLAN)
    tool, created, tasks, trace = _tool(
        tmp_path, parent_checker=parent, parent_mode=PermissionMode.PLAN, enable_fork=True
    )

    result = await tool.execute(AgentToolParams(prompt="synthetic", description="test"))

    assert result.is_error is False
    assert len(created) == 1 and tasks.launched == 1 and trace.created == 1
    checker = created[0]["permission_checker"]
    assert checker.mode is PermissionMode.PLAN
    assert checker.check(WriteFile(), {
        "file_path": str(tmp_path / "blocked.txt"), "content": "synthetic"
    }).effect == "deny"


def test_missing_parent_checker_uses_parent_mode_and_real_rule_paths(tmp_path: Path) -> None:
    policy_dir = tmp_path / ".mewcode"
    policy_dir.mkdir()
    project_rules = policy_dir / "permissions.yaml"
    project_rules.write_text('- rule: "WriteFile(*)"\n  effect: deny\n', encoding="utf-8")
    definition = AgentDef(agent_type="synthetic", when_to_use="test", permission_mode="default")
    tool, _created, _tasks, _trace = _tool(tmp_path, definition=definition, parent_checker=None)

    checker = tool._build_regular_child_checker(definition)

    assert checker.mode is PermissionMode.AUTO
    assert checker.policy_writable_roots == (tmp_path.resolve(),)
    assert checker.rule_engine._user_path == Path.home() / ".mewcode" / "permissions.yaml"
    assert checker.rule_engine._project_path == project_rules
    assert checker.rule_engine._local_path == policy_dir / "permissions.local.yaml"
    assert checker.grant_store.path == policy_dir / "permissions.grants.json"
    assert checker.check(WriteFile(), {
        "file_path": str(tmp_path / "blocked.txt"), "content": "synthetic"
    }).effect == "deny"


def test_missing_checker_without_mode_defaults_to_auto_and_rejects_dontask(tmp_path: Path) -> None:
    definition = AgentDef(agent_type="synthetic", when_to_use="test", permission_mode="dontAsk")
    tool, _created, _tasks, _trace = _tool(
        tmp_path, definition=definition, parent_checker=None, parent_mode=None
    )

    with pytest.raises(ValueError, match="cannot be broader"):
        tool._build_regular_child_checker(definition)


def _bounded_parent(workspace: Path):
    store = GrantStore(workspace)
    parent = build_permission_checker(
        workspace, mode=PermissionMode.AUTO, writable_scope=[workspace], grant_store=store
    )
    parent.rule_engine.append_session_rule(Rule("WriteFile", "*blocked.txt", "deny"))
    parent.rule_engine.append_session_rule(Rule("WriteFile", "*ask.txt", "ask"))
    parent.task_authorization = SimpleNamespace(version="v1", covers=lambda *_a, **_k: False)
    parent.task_id = "parent-task"
    parent.plan_file_path = str(workspace / "plan.md")
    parent.sandbox.file_grants[str(workspace / "grant.txt")] = True
    parent.sandbox.dir_grants[str(workspace)] = True
    return parent


def _assert_worktree_policy(checker, parent, worktree: Path, workspace: Path) -> None:
    assert checker.mode is PermissionMode.AUTO
    assert checker.policy_profile == "worktree"
    assert checker.policy_writable_roots == (worktree.resolve(),)
    assert checker.rule_engine is parent.rule_engine
    assert checker.grant_store is parent.grant_store
    assert checker.task_authorization is parent.task_authorization
    assert checker.task_id == parent.task_id
    assert checker.plan_file_path == parent.plan_file_path
    assert checker.sandbox.file_grants is parent.sandbox.file_grants
    assert checker.sandbox.dir_grants is parent.sandbox.dir_grants
    write = WriteFile()
    for name, effect in (("blocked.txt", "deny"), ("ask.txt", "ask")):
        assert checker.check(write, {
            "file_path": str(worktree / name), "content": "synthetic"
        }).effect == effect
    outside = checker.check(write, {
        "file_path": str(workspace / "outside.txt"), "content": "synthetic"
    })
    assert outside.effect == "deny" and outside.source == "child_write_scope"


@pytest.mark.asyncio
async def test_builtin_worktree_inherits_auto_and_all_parent_constraints(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    worktree = workspace / "new-worktree"
    worktree.mkdir(parents=True)
    parent = _bounded_parent(workspace)
    worktrees = _FakeWorktrees(worktree)
    tool, created, tasks, trace = _tool(
        workspace, parent_checker=parent, worktree_manager=worktrees
    )

    result = await tool._execute_with_worktree(AgentToolParams(
        prompt="synthetic", description="test"
    ))

    assert result.is_error is False
    assert len(created) == 1 and trace.created == 1 and tasks.launched == 0
    assert len(worktrees.created) == len(worktrees.cleaned) == 1
    _assert_worktree_policy(created[0]["permission_checker"], parent, worktree, workspace)


@pytest.mark.asyncio
async def test_builtin_teammate_inherits_auto_and_all_parent_constraints(
    tmp_path: Path, monkeypatch
) -> None:
    names = _isolate_names(monkeypatch)
    workspace = tmp_path / "workspace"
    worktree = workspace / "teammate-worktree"
    worktree.mkdir(parents=True)
    parent = _bounded_parent(workspace)
    worktrees = _FakeWorktrees(worktree)
    teams = _FakeTeamManager()
    tool, created, tasks, trace = _tool(
        workspace, parent_checker=parent, worktree_manager=worktrees,
        team_manager=teams,
    )

    result = await tool.execute(AgentToolParams(
        prompt="synthetic", description="test", team_name="synthetic-team"
    ))

    assert result.is_error is False
    assert len(created) == tasks.launched == trace.created == 1
    assert len(teams.team.members) == len(teams.handles) == 1
    assert names.resolve("worker") == "trace-child"
    assert worktrees.cleaned == []  # Started teammates own their worktree lifetime.
    _assert_worktree_policy(created[0]["permission_checker"], parent, worktree, workspace)


@pytest.mark.asyncio
@pytest.mark.parametrize("as_teammate", [False, True])
async def test_worktree_outside_frozen_parent_scope_rejects_without_launch_and_preserves_changes(
    tmp_path: Path, monkeypatch, as_teammate: bool
) -> None:
    names = _isolate_names(monkeypatch)
    workspace = tmp_path / "workspace"
    worktree = tmp_path / "custom-outside-worktree"
    workspace.mkdir()
    worktree.mkdir()
    worktrees = _FakeWorktrees(worktree, kept=True)
    teams = _FakeTeamManager() if as_teammate else None
    tool, created, tasks, trace = _tool(
        workspace, parent_checker=_bounded_parent(workspace),
        worktree_manager=worktrees, team_manager=teams,
    )
    tool._select_runtime = lambda *_args: pytest.fail("client selected before policy validation")

    params = AgentToolParams(
        prompt="synthetic", description="test",
        team_name="synthetic-team" if as_teammate else None,
    )
    result = await (tool.execute(params) if as_teammate else tool._execute_with_worktree(params))

    assert result.is_error is True
    assert "child writable scope exceeds parent policy" in result.output
    assert str(worktree) in result.output
    assert "Authorize the exact worktree directory" in result.output
    assert "Worktree preserved" in result.output
    assert len(worktrees.created) == len(worktrees.cleaned) == 1
    assert worktree.is_dir()  # Mock cleanup never force-removes a dirty worktree.
    assert created == [] and tasks.launched == trace.created == 0
    assert tool.runs == {} and names.list_all() == {}
    if teams is not None:
        assert teams.team.members == []


@pytest.mark.asyncio
@pytest.mark.parametrize("as_teammate", [False, True])
async def test_explicit_broader_worktree_or_teammate_mode_rejected_before_launch(
    tmp_path: Path, monkeypatch, as_teammate: bool
) -> None:
    names = _isolate_names(monkeypatch)
    workspace = tmp_path / "workspace"
    worktree = workspace / "new-worktree"
    worktree.mkdir(parents=True)
    worktrees = _FakeWorktrees(worktree, kept=True)
    teams = _FakeTeamManager() if as_teammate else None
    definition = AgentDef(
        agent_type="synthetic", when_to_use="test", isolation="worktree",
        permission_mode="default" if as_teammate else "dontAsk",
    )
    tool, created, tasks, trace = _tool(
        workspace, definition=definition, parent_checker=_bounded_parent(workspace),
        worktree_manager=worktrees, team_manager=teams,
        teammate_permission_mode="dontAsk" if as_teammate else "",
    )
    tool._select_runtime = lambda *_args: pytest.fail("client selected before policy validation")

    result = await tool.execute(AgentToolParams(
        prompt="synthetic", description="test", subagent_type="synthetic",
        team_name="synthetic-team" if as_teammate else None,
    ))

    assert result.is_error is True
    assert "cannot be broader" in result.output
    assert "Worktree preserved" in result.output
    assert len(worktrees.created) == len(worktrees.cleaned) == 1
    assert created == [] and tasks.launched == trace.created == 0
    assert tool.runs == {} and names.list_all() == {}
    if teams is not None:
        assert teams.team.members == []


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [BackendType.TMUX, BackendType.ITERM2])
@pytest.mark.parametrize("mode", [
    PermissionMode.AUTO, PermissionMode.STRICT_ISOLATION, PermissionMode.PLAN,
])
async def test_restricted_parent_rejects_cross_process_pane_before_worktree_creation(
    tmp_path: Path, monkeypatch, backend: BackendType, mode: PermissionMode
) -> None:
    names = _isolate_names(monkeypatch)
    worktree = tmp_path / "new-worktree"
    worktree.mkdir()
    worktrees = _FakeWorktrees(worktree)
    teams = _FakeTeamManager(backend)
    parent = build_permission_checker(tmp_path, mode=mode, writable_scope=[tmp_path])
    tool, created, tasks, trace = _tool(
        tmp_path, parent_checker=parent, worktree_manager=worktrees, team_manager=teams
    )
    tool._select_runtime = lambda *_args: pytest.fail("client selected before policy validation")

    result = await tool.execute(AgentToolParams(
        prompt="synthetic", description="test", team_name="synthetic-team"
    ))

    assert result.is_error is True
    assert backend.value in result.output and mode.value in result.output
    assert "cannot carry" in result.output
    assert worktrees.created == worktrees.cleaned == []
    assert created == [] and tasks.launched == trace.created == 0
    assert teams.team.members == [] and names.list_all() == {} and tool.runs == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("requested_mode", [
    PermissionMode.AUTO, PermissionMode.STRICT_ISOLATION, PermissionMode.PLAN,
])
async def test_pane_rejects_explicit_restricted_child_mode_from_unrestricted_parent(
    tmp_path: Path, monkeypatch, requested_mode: PermissionMode
) -> None:
    names = _isolate_names(monkeypatch)
    worktree = tmp_path / "new-worktree"
    worktree.mkdir()
    worktrees = _FakeWorktrees(worktree)
    teams = _FakeTeamManager(BackendType.TMUX)
    parent = build_permission_checker(
        tmp_path, mode=PermissionMode.DONT_ASK, writable_scope=[tmp_path]
    )
    tool, created, tasks, trace = _tool(
        tmp_path, parent_checker=parent, worktree_manager=worktrees,
        team_manager=teams, teammate_permission_mode=requested_mode.value,
    )
    tool._select_runtime = lambda *_args: pytest.fail("client selected before policy validation")

    result = await tool.execute(AgentToolParams(
        prompt="synthetic", description="test", team_name="synthetic-team"
    ))

    assert result.is_error is True
    assert "cannot carry" in result.output and requested_mode.value in result.output
    assert worktrees.created == worktrees.cleaned == []
    assert created == [] and tasks.launched == trace.created == 0
    assert teams.team.members == [] and names.list_all() == {} and tool.runs == {}


@pytest.mark.asyncio
async def test_unlaunched_teammate_registration_failure_preserves_dirty_worktree(
    tmp_path: Path, monkeypatch
) -> None:
    names = _isolate_names(monkeypatch)
    worktree = tmp_path / "new-worktree"
    worktree.mkdir()
    worktrees = _FakeWorktrees(worktree, kept=True)
    teams = _FakeTeamManager()

    def fail_register(_name, _member) -> None:
        raise RuntimeError("synthetic registration failure")

    teams.register_member = fail_register
    parent = build_permission_checker(
        tmp_path, mode=PermissionMode.AUTO, writable_scope=[tmp_path]
    )
    tool, created, tasks, trace = _tool(
        tmp_path, parent_checker=parent, worktree_manager=worktrees, team_manager=teams
    )

    result = await tool.execute(AgentToolParams(
        prompt="synthetic", description="test", team_name="synthetic-team"
    ))

    assert result.is_error is True and "Worktree preserved" in result.output
    assert len(created) == 1 and tasks.launched == 0
    assert len(worktrees.cleaned) == 1 and worktree.is_dir()
    assert trace.created == 1 and trace.completed == [("trace-child", "failed")]
    assert teams.team.members == [] and names.list_all() == {} and tool.runs == {}


@pytest.mark.asyncio
async def test_legacy_unrestricted_pane_still_spawns_without_claiming_checker_transfer(
    tmp_path: Path, monkeypatch
) -> None:
    names = _isolate_names(monkeypatch)
    worktree = tmp_path / "new-worktree"
    worktree.mkdir()
    worktrees = _FakeWorktrees(worktree)
    teams = _FakeTeamManager(BackendType.TMUX)
    parent = build_permission_checker(
        tmp_path, mode=PermissionMode.DONT_ASK, writable_scope=[tmp_path]
    )
    tool, created, tasks, trace = _tool(
        tmp_path, parent_checker=parent, worktree_manager=worktrees, team_manager=teams
    )
    tool._spawn_pane_teammate = lambda *_args: ToolResult(output="pane started")

    result = await tool.execute(AgentToolParams(
        prompt="synthetic", description="test", team_name="synthetic-team"
    ))

    assert result.is_error is False and result.output == "pane started"
    assert worktrees.created and worktrees.cleaned == []
    assert created == [] and tasks.launched == 0 and trace.created == 1
    assert len(teams.team.members) == 1 and names.resolve("worker") == "trace-child"
