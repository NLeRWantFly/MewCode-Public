"""Ordinary app settings must work without an interactive authorization card."""
import pytest

from mewcode.agent import Agent, PermissionRequest
from mewcode.permissions import PermissionMode, Rule
from mewcode.permissions.network_policy import NetworkPolicy
from mewcode.permissions.policy import build_permission_checker
from mewcode.permissions.reviewer import SafetyReviewResult
from mewcode.experts.node_expansion import ExpansionPathSandbox
from mewcode.tools import create_default_registry
from mewcode.tools.base import ToolCallComplete
from mewcode.tools.bash import Bash
from mewcode.tools.write_file import WriteFile
from tests.test_permissions import MockLLMClient


@pytest.fixture
def settings_case(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "app-settings"
    outside.mkdir()
    monkeypatch.setattr("mewcode.permissions.sandbox.tempfile.gettempdir", lambda: str(root))
    return root, outside / "expert-profiles.json"


@pytest.mark.parametrize("mode,effect", [
    (PermissionMode.DEFAULT, "ask"), (PermissionMode.AUTO, "review"),
    (PermissionMode.BYPASS, "allow"), (PermissionMode.PLAN, "deny"),
])
def test_external_settings_follow_selected_mode(settings_case, mode, effect):
    root, target = settings_case
    checker = build_permission_checker(root, mode=mode)
    decision = checker.check(WriteFile(), {"file_path": str(target), "content": "{}"})
    assert decision.effect == effect
    assert not checker.sandbox.file_grants and not checker.sandbox.dir_grants


@pytest.mark.asyncio
@pytest.mark.parametrize("interactive", [True, False])
@pytest.mark.parametrize("mode,approved", [
    (PermissionMode.AUTO, True), (PermissionMode.AUTO, False), (PermissionMode.BYPASS, True),
])
async def test_actual_external_write_without_permission_prompt(settings_case, interactive, mode, approved):
    root, target = settings_case
    checker = build_permission_checker(root, mode=mode)
    agent = Agent(MockLLMClient([]), create_default_registry(), "anthropic",
                  work_dir=str(root), permission_checker=checker)
    calls = []

    async def review(**kwargs):
        calls.append(kwargs)
        return SafetyReviewResult("allow" if approved else "deny", "low", "fixture decision")

    agent._review_tool = review
    call = ToolCallComplete("settings-write", "WriteFile", {"file_path": str(target), "content": "{}"})
    if interactive:
        events = [event async for event in agent._execute_tool(call)]
        assert not any(isinstance(event, PermissionRequest) for event in events)
        result = [event[0] for event in events if isinstance(event, tuple)][-1]
    else:
        result = await agent._execute_tool_noninteractive(call)
    assert result.is_error is not approved
    assert target.exists() is approved
    if approved:
        assert target.read_text(encoding="utf-8") == "{}"
    assert len(calls) == (1 if mode == PermissionMode.AUTO else 0)
    assert not checker.sandbox.file_grants and not checker.sandbox.dir_grants


@pytest.mark.parametrize("mode", [PermissionMode.AUTO, PermissionMode.BYPASS])
@pytest.mark.parametrize("scope", ["child", "worktree", "explicit", "expansion"])
def test_declared_scopes_remain_enforced(settings_case, mode, scope):
    root, target = settings_case
    parent = build_permission_checker(root, mode=mode)
    if scope == "child":
        checker = build_permission_checker(root, parent_policy=parent)
    elif scope == "worktree":
        checker = build_permission_checker(root, mode=mode, profile="worktree")
    elif scope == "explicit":
        checker = build_permission_checker(root, mode=mode, writable_scope=[root])
    else:
        checker = parent
        checker.sandbox = ExpansionPathSandbox(parent.sandbox, [str(root / "result.txt")])
    assert checker.check(WriteFile(), {"file_path": str(target), "content": "{}"}).effect == "deny"


@pytest.mark.parametrize("mode", [PermissionMode.AUTO, PermissionMode.BYPASS])
def test_external_protections_and_explicit_rules(settings_case, monkeypatch, mode):
    root, target = settings_case
    monkeypatch.setenv("USERPROFILE", str(target.parent))
    checker = build_permission_checker(root, mode=mode)
    for path in ("../app-settings/expert-profiles.json", str(target.parent / ".ssh" / "id_rsa"),
                 str(target.parent / "permissions.grants.json")):
        assert checker.check(WriteFile(), {"file_path": path, "content": "{}"}).effect == "deny"
    checker.rule_engine.append_session_rule(Rule("WriteFile", "*", "deny"))
    assert checker.check(WriteFile(), {"file_path": str(target), "content": "{}"}).effect == "deny"
    assert checker.check(Bash(), {"command": "format C:"}).source == "invariant_guardrail"


def test_full_access_command_backend_respects_explicit_boundaries(settings_case):
    root, _ = settings_case
    checker = build_permission_checker(root, mode=PermissionMode.BYPASS)
    checker.network_policy = NetworkPolicy(True)
    assert not checker.uses_native_commands({"command": "python -m pytest"})
    assert checker.uses_native_commands({"network": "offline"})
    assert checker.uses_native_commands({"publication": "discard"})
    checker.network_policy = NetworkPolicy(False)
    assert checker.uses_native_commands({})
    checker.network_policy = NetworkPolicy(True)
    checker.sandbox = ExpansionPathSandbox(checker.sandbox, [str(root / "result.txt")])
    assert checker.uses_native_commands({})


def test_review_envelope_describes_external_scope_accurately(settings_case):
    from mewcode.permissions.action_review import execution_environment
    root, _ = settings_case
    checker = build_permission_checker(root, mode=PermissionMode.AUTO)
    agent = Agent(MockLLMClient([]), create_default_registry(), "anthropic",
                  work_dir=str(root), permission_checker=checker)
    assert execution_environment(agent, "WriteFile")["external_file_write_policy"] == "auto_review_exact_file"
    agent.permission_checker = build_permission_checker(root, parent_policy=checker)
    assert execution_environment(agent, "WriteFile")["external_file_write_policy"] == "declared_scope_only"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [PermissionMode.AUTO, PermissionMode.BYPASS])
async def test_edit_existing_external_settings(settings_case, mode):
    root, target = settings_case
    target.write_text('{"role":"before"}', encoding="utf-8")
    checker = build_permission_checker(root, mode=mode)
    agent = Agent(MockLLMClient([]), create_default_registry(), "anthropic",
                  work_dir=str(root), permission_checker=checker)

    async def review(**kwargs):
        return SafetyReviewResult("allow", "low", "user requested role setting")

    agent._review_tool = review
    read = await agent._execute_tool_noninteractive(ToolCallComplete("read-settings", "ReadFile", {"file_path": str(target)}))
    assert not read.is_error
    edited = await agent._execute_tool_noninteractive(ToolCallComplete("edit-settings", "EditFile", {
        "file_path": str(target), "old_string": "before", "new_string": "after",
    }))
    assert not edited.is_error
    assert target.read_text(encoding="utf-8") == '{"role":"after"}'
