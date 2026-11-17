"""Host guards remain authoritative; Auto review never creates durable grants."""

import pytest

from mewcode.agent import Agent, PermissionRequest, PermissionResponse
from mewcode.conversation import ConversationManager
from mewcode.permissions import DangerousCommandDetector, PathSandbox, PermissionChecker, PermissionMode, RuleEngine, Rule
from mewcode.permissions.task_authorization import TaskAuthorization
from mewcode.skills.loader import SkillLoader
from mewcode.tools import create_default_registry
from mewcode.tools.bash import Bash
from mewcode.tools.base import ToolCallComplete, StreamEnd, TextDelta, ToolResult
from tests.test_permissions import MockLLMClient


def checker(root):
    policy = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(root)), RuleEngine(), PermissionMode.AUTO)
    policy.set_task_authorization(TaskAuthorization.from_user_scope({
        "read_directories": [str(root)], "allowed_behaviors": ["isolated_command"],
    }, task_id="test-task"), task_id="test-task")
    return policy


def test_exact_grant_never_expands_or_overrides_guards(tmp_path, monkeypatch):
    monkeypatch.setattr("mewcode.sandbox.windows.native_runtime_ready", lambda: True)
    monkeypatch.setattr("mewcode.sandbox.srt_runtime.runtime_status", lambda: {"ready": True})
    policy = checker(tmp_path)
    args = {"command": "git push origin main"}
    assert policy.check(Bash(), args).effect == "review"
    assert policy.grant_call("Bash", args) is False
    assert policy.check(Bash(), args).effect == "review"
    assert policy.check(Bash(), {"command": "git push origin another"}).effect == "review"
    policy.rule_engine.append_session_rule(Rule("Bash", "git push*", "deny"))
    assert policy.check(Bash(), args).effect == "deny"
    policy.grant_call("Bash", {"command": "rm -rf /"})
    assert policy.check(Bash(), {"command": "rm -rf /"}).effect == "deny"
    policy.mode = PermissionMode.PLAN
    assert policy.check(Bash(), args).effect == "deny"


@pytest.mark.asyncio
async def test_auto_reviews_each_action_without_permission_prompt_or_prefix_grant(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from mewcode.permissions.reviewer import SafetyReviewer
    monkeypatch.setattr("mewcode.sandbox.windows.native_runtime_ready", lambda: True)
    monkeypatch.setattr("mewcode.sandbox.srt_runtime.runtime_status", lambda: {"ready": True})
    args = {"command": 'python -c "print(42)"'}
    client = MockLLMClient([
        [ToolCallComplete("first", "Bash", args), StreamEnd("end_turn")],
        [ToolCallComplete("second", "Bash", args), StreamEnd("end_turn")],
        [TextDelta("done"), StreamEnd("end_turn")],
    ])
    policy = checker(tmp_path)
    registry = create_default_registry()
    execute = AsyncMock(return_value=ToolResult(output="42"))
    monkeypatch.setattr(registry.get("Bash"), "execute", execute)
    reviewer = SafetyReviewer(object(), str(tmp_path))
    reviewer._ask_model = AsyncMock(return_value='{"outcome":"allow","rationale":"Authorized local calculation within task scope."}')
    agent = Agent(client, registry, "anthropic", work_dir=str(tmp_path), permission_checker=policy, safety_reviewer=reviewer)
    assert agent.safety_reviewer is reviewer
    conversation = ConversationManager()
    conversation.add_user_message("run the calculation")
    asks = []
    async for event in agent.run(conversation):
        if isinstance(event, PermissionRequest):
            assert event.arguments == args and event.source == "auto_escalation"
            asks.append(event)
            event.future.set_result(PermissionResponse.ALLOW_ALWAYS)
    assert not asks
    assert execute.await_count == 2 and reviewer._ask_model.await_count == 2
    assert policy.rule_engine.evaluate("Bash", args["command"]) is None
    assert policy.check(Bash(), args).effect == "review"
    assert policy.check(Bash(), {"command": 'python -c "print(43)"'}).effect == "review"






def test_default_agent_preserves_unresolved_review_when_original_call_is_missing(tmp_path):
    agent = Agent(MockLLMClient([]), create_default_registry(), "anthropic", work_dir=str(tmp_path))
    conversation = ConversationManager(safety_review_state={"attempts": 2, "pending_call_ref": "old"})
    agent._restore_safety_review_state(conversation)
    assert agent.safety_reviewer is not None
    assert conversation.safety_review_state == {"attempts": 2, "pending_call_ref": "old"}
    assert not conversation.retired_safety_reviews
    assert agent._safety_review_unavailable.startswith("AUTO_REVIEW_INCONCLUSIVE")
    assert agent._safety_review_pending_call is None








def test_standard_project_skill_directory_is_discovered(tmp_path):
    path = tmp_path / ".agents" / "skills" / "typed"
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text("---\nname: typed\ndescription: Typed judgment\n---\nInstructions", encoding="utf-8")
    assert SkillLoader(str(tmp_path)).load_all()["typed"].source_path == path / "SKILL.md"
