"""Task authorization and approval telemetry must never widen a user grant."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from mewcode.permissions import DangerousCommandDetector, PathSandbox, PermissionChecker, PermissionMode, Rule, RuleEngine
from mewcode.permissions.audit import AuditLogger
from mewcode.permissions.task_authorization import TaskAuthorization
from mewcode.tools.bash import Bash
from mewcode.tools.browser import Browser, WebFetch, scoped_network_host
from mewcode.tools.read_file import ReadFile
from mewcode.tools.write_file import WriteFile
from mewcode.agent import Agent, ToolResultEvent
from mewcode.conversation import ConversationManager
from mewcode.permissions.reviewer import SafetyReviewResult
from mewcode.tools import create_default_registry
from mewcode.tools.base import StreamEnd, TextDelta, ToolCallComplete
from tests.test_permissions import MockLLMClient


def _checker(root, mode=PermissionMode.DEFAULT):
    return PermissionChecker(DangerousCommandDetector(), PathSandbox(str(root)), RuleEngine(), mode)


def test_exact_grant_does_not_match_argument_change_or_plan_mode(tmp_path):
    checker = _checker(tmp_path)
    assert checker.grant_call("Bash", {"command": "python script.py"}) is False
    assert checker.check(Bash(), {"command": "python script.py"}).effect == "ask"
    args = {"file_path": str(tmp_path / "new.txt"), "content": "x"}
    assert checker.grant_call("WriteFile", args) is True
    assert checker.check(WriteFile(), args).source == "user_grant"
    assert checker.check(WriteFile(), {**args, "content": "changed"}).effect == "ask"
    checker.mode = PermissionMode.PLAN
    assert checker.check(WriteFile(), args).effect == "deny"


def test_deny_rule_overrides_exact_grant_and_task_scope(tmp_path):
    checker = _checker(tmp_path)
    target = tmp_path / "work.txt"
    checker.rule_engine.append_session_rule(Rule("WriteFile", str(target), "deny"))
    checker.grant_call("WriteFile", {"file_path": str(target), "content": "x"})
    scope = TaskAuthorization.from_user_scope({
        "write_directories": [str(tmp_path)], "allowed_tools": ["WriteFile"],
    }, task_id="task-1")
    checker.set_task_authorization(scope, task_id="task-1")
    assert checker.check(WriteFile(), {"file_path": str(target), "content": "x"}).effect == "deny"


def test_exact_file_grant_invalidates_when_target_content_changes(tmp_path):
    checker = _checker(tmp_path)
    target = tmp_path / "draft.txt"
    target.write_text("old", encoding="utf-8")
    args = {"file_path": str(target), "content": "new"}
    checker.grant_call("WriteFile", args)
    assert checker.check(WriteFile(), args).source == "user_grant"
    target.write_text("changed outside grant", encoding="utf-8")
    assert checker.check(WriteFile(), args).source != "user_grant"


def test_task_scope_is_versioned_and_target_bound(tmp_path):
    checker = _checker(tmp_path)
    outside = tmp_path.parent / "unapproved" / "file.txt"
    scope = TaskAuthorization.from_user_scope({
        "read_directories": [str(tmp_path)], "write_directories": [str(tmp_path)],
        "allowed_tools": ["ReadFile", "WriteFile"],
    }, task_id="task-1")
    checker.set_task_authorization(scope, task_id="task-1")
    assert checker.check(WriteFile(), {"file_path": str(tmp_path / "new.txt"), "content": "x"}).source == "task_authorization"
    assert checker.check(ReadFile(), {"file_path": str(outside)}).source != "task_authorization"
    checker.set_task_authorization(scope, task_id="task-2")
    assert checker.check(WriteFile(), {"file_path": str(tmp_path / "new.txt"), "content": "x"}).source != "task_authorization"
    assert TaskAuthorization.from_record(scope.as_dict()).version == scope.version
    altered = scope.as_dict()
    altered["write_directories"] = []
    with pytest.raises(ValueError):
        TaskAuthorization.from_record(altered)


def test_directory_scope_alone_does_not_authorize_shell_behavior(tmp_path):
    checker = _checker(tmp_path, PermissionMode.STRICT_ISOLATION)
    scope = TaskAuthorization.from_user_scope({"read_directories": [str(tmp_path)]}, task_id="task")
    checker.set_task_authorization(scope, task_id="task")
    assert checker.check(Bash(), {"command": 'python -c "print(1)"'}).effect == "deny"


def test_network_scope_is_only_for_exact_webfetch_host(tmp_path):
    checker = _checker(tmp_path)
    checker.mode = PermissionMode.AUTO
    assert checker.check(WebFetch(), {"url": "https://example.com/article"}).effect == "review"
    scope = TaskAuthorization.from_user_scope({
        "network_hosts": ["example.com"], "allowed_tools": ["WebFetch", "ReadFile"],
    }, task_id="task-1")
    checker.set_task_authorization(scope, task_id="task-1")
    assert checker.check(WebFetch(), {"url": "https://example.com/article"}).source == "task_authorization"
    assert checker.check(WebFetch(), {"url": "https://other.example.com/article"}).source != "task_authorization"
    assert checker.check(WebFetch(), {"url": "https://example.com:8443/article"}).source != "task_authorization"
    assert checker.check(ReadFile(), {"file_path": str(tmp_path.parent / "secret.txt"),
                                       "url": "https://example.com"}).source != "task_authorization"
    with pytest.raises(ValueError):
        TaskAuthorization.from_user_scope({"allowed_tools": ["Bash"]}, task_id="task-1")


@pytest.mark.asyncio
async def test_scoped_fetch_rejects_redirect_without_proxy(monkeypatch):
    options = {}

    class Response:
        is_redirect = True

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return Response()

    def client_factory(**kwargs):
        options.update(kwargs)
        return Client()

    monkeypatch.setattr("mewcode.tools.browser.httpx.AsyncClient", client_factory)
    with scoped_network_host("example.com"):
        with pytest.raises(ValueError, match="redirect"):
            await Browser()._fetch_url("https://example.com/a")
        with pytest.raises(ValueError, match="outside"):
            await Browser()._fetch_url("https://other.example.com/a")
    assert options["follow_redirects"] is False and options["trust_env"] is False


def test_audit_summary_keeps_human_wait_and_cache_hit(tmp_path):
    logger = AuditLogger(str(tmp_path), session_id="s")
    logger.log_decision(tool_name="ReadFile", params_summary="x", decision="allow",
                        source_layer="task_authorization", latency_ms=5)
    logger.log_decision(tool_name="Bash", params_summary="x", decision="user_allow",
                        source_layer="human_decision", wait_ms=100,
                        authorization_scope="exact_operation_once", user_choice="allow")
    logger.log_decision(tool_name="Bash", params_summary="x", decision="allow",
                        source_layer="auto_safety_reviewer", latency_ms=20, cache_hit=True)
    summary = logger.summary()
    assert summary["count"] == 3
    assert summary["by_source"]["human_decision"] == 1
    assert summary["cache_hits"] == 1
    assert summary["human_wait_ms"]["total"] == 100


@pytest.mark.asyncio
async def test_scope_change_during_review_prevents_execution(tmp_path):
    checker = _checker(tmp_path, PermissionMode.STRICT_ISOLATION)
    original = TaskAuthorization.from_user_scope({"read_directories": [str(tmp_path)],
                                                  "allowed_behaviors": ["isolated_command"]}, task_id="task")
    checker.set_task_authorization(original, task_id="task")

    class Reviewer:
        async def review(self, **_kwargs):
            replacement = TaskAuthorization.from_user_scope({"read_directories": [str(tmp_path)],
                                                              "allowed_behaviors": ["isolated_command"]}, task_id="task")
            checker.set_task_authorization(replacement, task_id="task")
            return SafetyReviewResult(decision="allow", risk="low", reason="looks safe")

    registry = create_default_registry()
    registry.get("Bash").run = AsyncMock()
    client = MockLLMClient([
        [ToolCallComplete("one", "Bash", {"command": 'python -c "print(1)"'}), StreamEnd("end_turn")],
        [TextDelta("done"), StreamEnd("end_turn")],
    ])
    agent = Agent(client, registry, "anthropic", work_dir=str(tmp_path),
                  permission_checker=checker, safety_reviewer=Reviewer())
    conversation = ConversationManager()
    conversation.add_user_message("Check one script")
    events = [event async for event in agent.run(conversation)]
    results = [event for event in events if isinstance(event, ToolResultEvent)]
    assert results and results[0].is_error and "AUTO_REVIEW_CONTEXT_CHANGED" in results[0].output
    registry.get("Bash").run.assert_not_awaited()
