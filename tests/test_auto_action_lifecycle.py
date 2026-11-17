import asyncio
from unittest.mock import AsyncMock

import pytest

from mewcode.agent import Agent, ToolExecutionStartEvent
from mewcode.conversation import ConversationManager
from mewcode.permissions import DangerousCommandDetector, PathSandbox, PermissionChecker, PermissionMode, RuleEngine
from mewcode.permissions.action_review import execution_environment
from mewcode.permissions.reviewer import SafetyReviewer
from mewcode.permissions.rules import Rule
from mewcode.tools import create_default_registry
from mewcode.tools.base import ToolCallComplete


def setup_agent(tmp_path):
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(tmp_path)), RuleEngine(), PermissionMode.AUTO)
    checker.review_workspace_writes = True
    reviewer = SafetyReviewer(object(), str(tmp_path))
    reviewer._ask_model = AsyncMock(return_value='{"outcome":"allow","rationale":"Requested local action within confirmed scope."}')
    agent = Agent(object(), create_default_registry(), "anthropic", work_dir=str(tmp_path),
                  permission_checker=checker, safety_reviewer=reviewer)
    agent._current_conversation = ConversationManager()
    agent._current_conversation.add_user_message("Write the requested result in this workspace.")
    call = ToolCallComplete("write-1", "WriteFile", {"file_path": str(tmp_path / "result.txt"), "content": "approved"})
    return agent, reviewer, call


async def execute(agent, call, interactive):
    if interactive:
        events = [event async for event in agent._execute_tool_impl(call)]
        return [event[0] for event in events if isinstance(event, tuple)][-1]
    return await agent._execute_tool_noninteractive_impl(call)


@pytest.mark.asyncio
@pytest.mark.parametrize("interactive", [True, False])
async def test_current_review_executes_exact_write(tmp_path, interactive):
    agent, reviewer, call = setup_agent(tmp_path)
    result = await execute(agent, call, interactive)
    assert not result.is_error
    assert (tmp_path / "result.txt").read_text() == "approved"
    risk = reviewer._ask_model.await_args.kwargs["risk"]
    assert risk["host_execution_environment"]["backend_policy"] == "structured_tool"
    assert risk["host_execution_environment"]["execution_confirmed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("interactive", [True, False])
@pytest.mark.parametrize("change", ["arguments", "mode", "rules", "roots", "grant", "authorization", "target", "tool"])
async def test_changed_action_or_authority_discards_pending_allow(tmp_path, interactive, change):
    agent, reviewer, call = setup_agent(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def review(**kwargs):
        entered.set()
        await release.wait()
        assert kwargs["arguments"]["content"] == "approved"
        return '{"outcome":"allow","rationale":"Requested local action within confirmed scope."}'

    reviewer._ask_model = AsyncMock(side_effect=review)
    task = asyncio.create_task(execute(agent, call, interactive))
    await asyncio.wait_for(entered.wait(), 2)
    checker = agent.permission_checker
    if change == "arguments":
        call.arguments["content"] = "unreviewed"
    elif change == "mode":
        checker.mode = PermissionMode.PLAN
    elif change == "rules":
        checker.rule_engine.append_session_rule(Rule("WriteFile", "*", "deny"))
    elif change == "roots":
        checker.sandbox._allowed_roots.append(tmp_path / "new-root")
    elif change == "grant":
        checker.sandbox.dir_grants[str(tmp_path / "grant")] = True
    elif change == "authorization":
        agent._current_conversation.add_user_message("Stop; do not write this result.")
    elif change == "target":
        (tmp_path / "result.txt").write_text("user change")
    elif change == "tool":
        agent.registry.disable("WriteFile")
    release.set()
    result = await task
    assert result.is_error and "AUTO_REVIEW_CONTEXT_CHANGED" in result.output
    assert not (tmp_path / "result.txt").exists() or (tmp_path / "result.txt").read_text() == "user change"


@pytest.mark.asyncio
async def test_dispatch_event_yield_cannot_reuse_changed_approval(tmp_path):
    agent, reviewer, call = setup_agent(tmp_path)
    events = agent._execute_tool_impl(call)
    event = await anext(events)
    assert isinstance(event, ToolExecutionStartEvent)
    call.arguments["content"] = "mutated after approval"
    result, _, _ = await anext(events)
    assert result.is_error and "AUTO_REVIEW_CONTEXT_CHANGED" in result.output
    assert result.host_observation["command_execution"]["command_started"] is False
    assert not (tmp_path / "result.txt").exists()
    await events.aclose()


@pytest.mark.asyncio
async def test_cancelled_review_never_writes(tmp_path):
    agent, reviewer, call = setup_agent(tmp_path)
    entered = asyncio.Event()
    async def review(**kwargs):
        entered.set()
        await asyncio.Event().wait()
    reviewer._ask_model = AsyncMock(side_effect=review)
    task = asyncio.create_task(execute(agent, call, False))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not (tmp_path / "result.txt").exists()
    assert not reviewer._allows


def test_environment_reports_required_backend_not_execution(tmp_path, monkeypatch):
    agent, _, _ = setup_agent(tmp_path)
    monkeypatch.setattr(agent, "_uses_native_commands", lambda arguments=None: False)
    env = execution_environment(agent, "Bash")
    assert env["backend_policy"] == "host_process"
    assert env["network_policy"] == "no_process_network_isolation_claim"
    monkeypatch.setattr(agent, "_uses_native_commands", lambda arguments=None: True)
    env = execution_environment(agent, "Bash")
    assert env["backend_policy"] == "windows_network_required" and not env["execution_confirmed"]
    assert env['network_policy'] == 'enabled_except_blacklist'
    from mewcode.permissions.network_policy import NetworkPolicy
    agent.permission_checker.network_policy = NetworkPolicy(False)
    assert execution_environment(agent, 'Bash')['backend_policy'] == 'windows_native_required'


def test_native_review_plan_binds_exact_future_operation_not_an_execution_receipt(tmp_path, monkeypatch):
    from mewcode.permissions.action_review import ActionReviewBinding
    agent, _, _ = setup_agent(tmp_path)
    monkeypatch.setattr(agent, '_uses_native_commands', lambda arguments=None: True)
    args = {'command': 'python inspect.py', 'timeout': 120, 'publication': 'discard'}
    bound = ActionReviewBinding.capture(agent, 'Bash', args)
    plan = bound.environment['command_dispatch_plan']
    assert plan['publication'] == 'discard' and not plan['host_shell_fallback']
    assert not plan['transaction_created'] and not bound.environment['execution_confirmed']
    assert plan['preparation_phase'] == 'after_review_before_process_start'
    assert bound.is_current(agent, 'Bash', args)
    assert not bound.is_current(agent, 'Bash', {**args, 'publication': 'publish'})
    assert not bound.is_current(agent, 'Bash', {**args, 'command': 'other operation'})


@pytest.mark.asyncio
@pytest.mark.parametrize("interactive", [True, False])
async def test_native_recovery_during_check_matches_review_and_dispatch(tmp_path, monkeypatch, interactive):
    from mewcode.tools.command_sandbox import _native_auto_scope
    from mewcode.tools.base import ToolResult
    from mewcode.permissions.checker import Decision
    agent, reviewer, _ = setup_agent(tmp_path)
    ready = False
    monkeypatch.setattr(agent, "_uses_native_commands", lambda arguments=None: ready)
    def check(tool, arguments):
        nonlocal ready
        ready = True  # Runtime recovery inside permission checking.
        return Decision("review", "Review repaired runtime", "auto_escalation", risk={})
    monkeypatch.setattr(agent.permission_checker, "check", check)
    async def run(params, **kwargs):
        assert _native_auto_scope.get() is True
        assert reviewer._ask_model.await_args.kwargs["risk"]["host_execution_environment"]["backend_policy"] == "windows_network_required"
        return ToolResult("Verified guard; no process launched")
    agent.registry.get("Bash").run = AsyncMock(side_effect=run)
    result = await execute(agent, ToolCallComplete("bash-1", "Bash", {"command": "python inspect.py"}), interactive)
    assert not result.is_error
    agent.registry.get("Bash").run.assert_awaited_once()


@pytest.mark.parametrize("outside,large", [(True, False), (False, True)])
def test_target_version_covers_external_grants_and_large_files(tmp_path, outside, large):
    from mewcode.permissions.action_review import ActionReviewBinding
    import os
    project = tmp_path / "project"
    project.mkdir()
    agent, _, call = setup_agent(project)
    path = (tmp_path if outside else project) / "existing.txt"
    original = "a" * (5 * 1024 * 1024 if large else 100)
    path.write_text(original)
    if outside:
        agent.permission_checker.sandbox.file_grants[str(path)] = True
    call.arguments["file_path"] = str(path)
    binding = ActionReviewBinding.capture(agent, call.tool_name, call.arguments)
    stat = path.stat()
    path.write_text("b" * len(original))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert not binding.is_current(agent, call.tool_name, call.arguments)


@pytest.mark.asyncio
async def test_inconclusive_cache_expires_when_execution_policy_changes(tmp_path):
    from mewcode.permissions.review_outcome import ReviewInconclusive
    agent, reviewer, _ = setup_agent(tmp_path)
    reviewer._ask_model = AsyncMock(side_effect=[
        ReviewInconclusive("scope uncertain", route={}, unresolved=["scope"]),
        '{"outcome":"allow","rationale":"Requested local action within confirmed scope."}',
    ])
    request = dict(tool_name="Bash", arguments={"command": "python inspect.py"}, risk={},
                   conversation=agent._current_conversation)
    first = await agent._review_tool(**request)
    assert first.inconclusive
    assert (await agent._review_tool(**request)).cache_hit
    agent.permission_checker.sandbox.dir_grants[str(tmp_path / "new-grant")] = False
    current = await agent._review_tool(**request)
    assert current.allowed and not current.cache_hit
    assert reviewer._ask_model.await_count == 2
