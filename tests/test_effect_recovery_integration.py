"""Host-level integration coverage for Auto denial and effect recovery."""
from __future__ import annotations

import re
from unittest.mock import AsyncMock

import pytest

from mewcode.agent import (
    Agent,
    AskUserRequest,
    PermissionRequest,
    PermissionResponse,
)
from mewcode.conversation import ConversationManager
from mewcode.permissions import (
    DangerousCommandDetector,
    PathSandbox,
    PermissionChecker,
    PermissionMode,
    RuleEngine,
)
from mewcode.permissions.checker import Decision
from mewcode.permissions.review_outcome import ReviewInconclusive
from mewcode.permissions.reviewer import SafetyReviewer
from mewcode.tools import create_default_registry
from mewcode.tools.ask_user import AskUserTool
from mewcode.tools.base import ToolCallComplete, ToolResult
from tests.test_permissions import MockLLMClient


def _deny(kind: str = "semantic_risk") -> str:
    return (
        '{"outcome":"deny","risk":"medium","reason":"the reviewed effect is unsafe",'
        f'"blocker_kind":"{kind}","alternative":"Use the existing read-only evidence."' + "}"
    )


def _allow() -> str:
    return '{"outcome":"allow","risk":"low","reason":"the exact reviewed effect is safe"}'


def _write(path, content: str = "reviewed content") -> ToolCallComplete:
    return ToolCallComplete("write-id", "WriteFile", {"file_path": str(path), "content": content})


def _make_agent(tmp_path, monkeypatch, reviews=(), *, work_dir=None):
    monkeypatch.setenv("MEWCODE_USER_DATA_DIR", ".tmp/effect-test-userdata")
    # A sibling of the workspace must be outside every implicit write root.
    # On Linux pytest's tmp_path normally lives under the allowed /tmp root.
    monkeypatch.setattr("mewcode.permissions.sandbox.tempfile.gettempdir",
                        lambda: str(tmp_path / "allowed-temp"))
    # These tests mock every command and assert it never starts. Exercise the
    # review path independently of installed Windows networking packages.
    monkeypatch.setattr("mewcode.sandbox.srt_runtime.runtime_status", lambda: {"ready": True})
    root = work_dir or tmp_path
    root.mkdir(parents=True, exist_ok=True)
    checker = PermissionChecker(
        DangerousCommandDetector(), PathSandbox(str(root)), RuleEngine(), PermissionMode.AUTO
    )
    reviewer = SafetyReviewer(object(), str(root))
    reviewer._ask_model = AsyncMock(side_effect=list(reviews))
    registry = create_default_registry()
    registry.register(AskUserTool())
    registry.mark_discovered("AskUserQuestion")
    agent = Agent(
        MockLLMClient([]), registry, "anthropic", work_dir=str(root),
        permission_checker=checker, safety_reviewer=reviewer,
    )
    agent._current_conversation = ConversationManager()
    return agent, checker, reviewer


async def _invoke(agent, call, *, answers=None, permission=PermissionResponse.ALLOW):
    """Dispatch through Agent's real tool host path and resolve any UI event."""
    events = []
    async for event in agent._execute_tool(call):
        events.append(event)
        if isinstance(event, PermissionRequest):
            event.future.set_result(permission)
        elif isinstance(event, AskUserRequest):
            values = answers(event.questions) if callable(answers) else answers
            event.future.set_result(values or {})
    results = [event[0] for event in events if isinstance(event, tuple)]
    return results, events


def _only_result(results: list[ToolResult]) -> ToolResult:
    assert len(results) == 1
    return results[0]


def _review_ref(output: str) -> str:
    match = re.search(r"review_call_ref=([a-f0-9]+)", output)
    assert match, output
    return match.group(1)


@pytest.mark.asyncio
async def test_unrelated_local_write_proceeds_after_specific_review_denial_in_same_batch(tmp_path, monkeypatch):
    agent, checker, reviewer = _make_agent(tmp_path, monkeypatch, [_deny()])
    checker.review_workspace_writes = True

    denied, _ = await _invoke(agent, _write(tmp_path / "denied.txt"))
    assert "AUTO_DENIED:" in _only_result(denied).output

    # Keep the same Agent/recovery state, as for adjacent calls in one response.
    checker.review_workspace_writes = False
    independent_path = tmp_path / "independent.txt"
    independent, _ = await _invoke(agent, _write(independent_path, "independent work"))

    assert not _only_result(independent).is_error
    assert independent_path.read_text(encoding="utf-8") == "independent work"
    assert reviewer._ask_model.await_count == 1
    assert len(agent._review_recovery.requests) == 1


@pytest.mark.asyncio
async def test_rm_denial_rechecks_same_target_across_spacing_and_writefile_without_running_shell(tmp_path, monkeypatch):
    agent, checker, reviewer = _make_agent(tmp_path, monkeypatch, [_deny(), _deny(), _deny()])
    checker.review_workspace_writes = True
    target = tmp_path / "same.txt"
    target.write_text("preserve this", encoding="utf-8")

    bash = agent.registry.get("Bash")
    bash.run = AsyncMock(side_effect=AssertionError("integration test must never execute a shell"))
    commands = [
        f'rm "{target.as_posix()}"',
        f'rm    "{target.as_posix()}"',
    ]
    references = []
    for command in commands:
        assert checker.check(bash, {"command": command}).effect == "review"
        results, _ = await _invoke(agent, ToolCallComplete("rm", "Bash", {"command": command}))
        output = _only_result(results).output
        assert "AUTO_DENIED:" in output
        references.append(_review_ref(output))

    clear, _ = await _invoke(agent, _write(target, ""))
    assert "AUTO_DENIED:" in _only_result(clear).output
    denied_items = [agent._review_recovery.requests[ref] for ref in references]
    assert denied_items[0].effect.key == denied_items[1].effect.key
    assert all(item.effect.overlaps(agent._review_recovery.signature(agent, "WriteFile", {
        "file_path": str(target), "content": "",
    })) for item in denied_items)
    assert target.read_text(encoding="utf-8") == "preserve this"
    bash.run.assert_not_awaited()
    assert reviewer._ask_model.await_count == 3


@pytest.mark.asyncio
async def test_distinct_effects_continue_and_repeated_denial_only_holds_original_effect(tmp_path, monkeypatch):
    distinct_dir = tmp_path / "distinct"
    distinct, distinct_checker, _ = _make_agent(
        distinct_dir, monkeypatch, [_deny(), _deny(), _deny()]
    )
    distinct_checker.review_workspace_writes = True
    for name in ("one.txt", "two.txt", "three.txt"):
        results, _ = await _invoke(distinct, _write(distinct_dir / name))
        assert "AUTO_DENIED:" in _only_result(results).output
    assert len(distinct._review_recovery.requests) == 3
    assert distinct._review_recovery.stalled_action(distinct, "WriteFile", _write(distinct_dir / "three.txt").arguments) is None

    repeated_dir = tmp_path / "repeated"
    repeated, repeated_checker, _ = _make_agent(
        repeated_dir, monkeypatch, [_deny(), _deny(), _deny()]
    )
    repeated_checker.review_workspace_writes = True
    same_call = _write(repeated_dir / "same.txt")
    for _ in range(3):
        results, _ = await _invoke(repeated, same_call)
        assert "AUTO_DENIED:" in _only_result(results).output
    reviews_before_hold = repeated.safety_reviewer._ask_model.await_count
    results, _ = await _invoke(repeated, same_call)
    assert "AUTO_REVIEW_DENIAL_LIMIT" in _only_result(results).output
    assert repeated.safety_reviewer._ask_model.await_count == reviews_before_hold
    repeated_checker.review_workspace_writes = False
    independent = repeated_dir / "independent.txt"
    results, _ = await _invoke(repeated, _write(independent))
    assert not _only_result(results).is_error
    assert independent.exists() and not (repeated_dir / "same.txt").exists()


@pytest.mark.asyncio
async def test_cumulative_effects_record_completed_write_but_not_denied_script_review(tmp_path, monkeypatch):
    agent, checker, reviewer = _make_agent(tmp_path, monkeypatch, [_allow(), _deny()])
    checker.review_workspace_writes = True
    script = tmp_path / "verify.py"

    write_results, _ = await _invoke(agent, _write(script, "raise SystemExit(0)\n"))
    assert not _only_result(write_results).is_error
    assert script.read_text(encoding="utf-8") == "raise SystemExit(0)\n"

    bash = agent.registry.get("Bash")
    bash.run = AsyncMock(side_effect=AssertionError("script execution is forbidden in this test"))
    command = f'python "{script.name}"'
    original_check = checker.check

    def review_script(tool, arguments):
        if tool.name == "Bash" and arguments.get("command") == command:
            return Decision("review", "script review requested", "test_script_review",
                            risk=checker._risk(tool, command, arguments))
        return original_check(tool, arguments)

    checker.check = review_script
    script_results, _ = await _invoke(agent, ToolCallComplete("script", "Bash", {"command": command}))
    assert "AUTO_DENIED:" in _only_result(script_results).output

    risk = reviewer._ask_model.await_args_list[1].kwargs["risk"]
    rows = risk["host_cumulative_effects"]["rows"]
    assert len(rows) == 1
    assert rows[0]["tool_name"] == "WriteFile"
    assert rows[0]["status"] == "tool_reported_completed"
    assert len(agent._review_recovery.ledger) == 1
    assert all(row["tool_name"] != "Bash" for row in agent._review_recovery.ledger)
    bash.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_host_missing_authority_denial_does_not_prompt_or_substitute_reviewer(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    agent, _, reviewer = _make_agent(tmp_path, monkeypatch, [], work_dir=workspace)

    results, events = await _invoke(agent, _write(outside))
    result = _only_result(results)

    assert result.is_error and "AUTO_DENIED:" in result.output
    assert "blocker_kind=missing_authorization" in result.output
    assert _review_ref(result.output) in agent._review_recovery.requests
    assert not any(isinstance(event, (PermissionRequest, AskUserRequest)) for event in events)
    assert reviewer._ask_model.await_count == 0
    assert not outside.exists()


@pytest.mark.asyncio
async def test_denied_curl_host_forces_browser_and_webfetch_review_without_network(tmp_path, monkeypatch):
    agent, checker, reviewer = _make_agent(tmp_path, monkeypatch, [_deny(), _deny(), _deny()])
    url = "https://example.test/reports/status?token=must-not-be-retained"
    bash = agent.registry.get("Bash")
    browser = agent.registry.get("Browser")
    web_fetch = agent.registry.get("WebFetch")
    bash.run = AsyncMock(side_effect=AssertionError("curl must never execute"))
    browser.run = AsyncMock(side_effect=AssertionError("Browser must never access the network"))
    web_fetch.run = AsyncMock(side_effect=AssertionError("WebFetch must never access the network"))

    original_check = checker.check

    def read_fast_path(tool, arguments):
        if tool.name in {"Browser", "WebFetch"}:
            # Model a read-only host fast path whose risk profile says
            # side_effect=none; the prior denied network effect still wins.
            return Decision("allow", "test read-only fast path", "auto_fast_path",
                            risk=checker._risk(tool, "", arguments))
        return original_check(tool, arguments)

    checker.check = read_fast_path
    denied_curl, _ = await _invoke(agent, ToolCallComplete(
        "curl", "Bash", {"command": f"curl {url}"}
    ))
    assert "AUTO_DENIED:" in _only_result(denied_curl).output

    browser_result, _ = await _invoke(agent, ToolCallComplete(
        "browser", "Browser", {"action": "open", "url": url}
    ))
    fetch_result, _ = await _invoke(agent, ToolCallComplete(
        "fetch", "WebFetch", {"url": url}
    ))

    assert "AUTO_DENIED:" in _only_result(browser_result).output
    assert "AUTO_DENIED:" in _only_result(fetch_result).output
    assert reviewer._ask_model.await_count == 3
    for review_call in reviewer._ask_model.await_args_list[1:]:
        risk = review_call.kwargs["risk"]
        assert risk["side_effect"] == "none"
        assert risk["host_effect_signature"]["kind"] == "net_egress"
        assert any(item["tool_name"] == "Bash" for item in risk["host_prior_denials"])
        assert "must-not-be-retained" not in repr(risk["host_effect_signature"])
    assert all(target.startswith("host:") for target in
               reviewer._ask_model.await_args_list[2].kwargs["risk"]["host_effect_signature"]["targets"])
    bash.run.assert_not_awaited()
    browser.run.assert_not_awaited()
    web_fetch.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_batch_authorization_asks_once_and_consumes_only_the_approved_exact_retry(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    first_path = tmp_path / "first-outside.txt"
    second_path = tmp_path / "second-outside.txt"
    agent, _, reviewer = _make_agent(tmp_path, monkeypatch, [_allow()], work_dir=workspace)

    first_denied, _ = await _invoke(agent, _write(first_path, "first"))
    second_denied, _ = await _invoke(agent, _write(second_path, "second"))
    first_ref = _review_ref(_only_result(first_denied).output)
    second_ref = _review_ref(_only_result(second_denied).output)
    assert reviewer._ask_model.await_count == 0

    ask = ToolCallComplete("batch-ask", "AskUserQuestion", {
        "purpose": "authorization",
        "review_call_refs": [first_ref, second_ref],
        "questions": [{"type": "text", "name": "why", "message": "Explain why either operation is still needed."}],
    })
    ask_results, ask_events = await _invoke(
        agent,
        ask,
        answers=lambda questions: {
            question["name"]: ("授权此操作一次并复审" if question["name"] == first_ref else "不允许此操作")
            for question in questions
        },
    )
    prompts = [event for event in ask_events if isinstance(event, AskUserRequest)]
    assert len(prompts) == 1
    assert {question["name"] for question in prompts[0].questions} == {first_ref, second_ref}
    assert "AUTO_AUTHORIZATION_BATCH_RESULT" in _only_result(ask_results).output
    assert agent._review_recovery.requests[first_ref].user_response == "approved_once"
    assert agent._review_recovery.requests[second_ref].user_response == "rejected"

    permitted, _ = await _invoke(agent, _write(first_path, "first"))
    assert not _only_result(permitted).is_error
    assert first_path.read_text(encoding="utf-8") == "first"
    assert reviewer._ask_model.await_count == 1
    consent = reviewer._ask_model.await_args.kwargs["risk"]["host_user_consent"]
    assert consent["review_call_ref"] == first_ref
    assert consent["decision"] == "approved_once"
    assert agent._review_recovery.requests[first_ref].user_response == "consumed"

    repeated, _ = await _invoke(agent, _write(first_path, "first"))
    assert _only_result(repeated).is_error
    assert "AUTO_DENIED:" in _only_result(repeated).output
    assert reviewer._ask_model.await_count == 1
    assert "host_user_consent" in reviewer._ask_model.await_args_list[0].kwargs["risk"]
    assert first_path.read_text(encoding="utf-8") == "first"


@pytest.mark.asyncio
async def test_inconclusive_write_reviews_related_edit_and_abandonment_keeps_history(
    tmp_path, monkeypatch
):
    agent, checker, reviewer = _make_agent(tmp_path, monkeypatch, [], work_dir=tmp_path)
    from types import SimpleNamespace

    async def inconclusive(*_args, **_kwargs):
        raise ReviewInconclusive(
            "the target scope is still unclear", route={"status": "needs_evidence"}, unresolved=["scope"]
        )

    reviewer.client = SimpleNamespace(review_safety=AsyncMock(side_effect=inconclusive))
    reviewer._ask_model = SafetyReviewer._ask_model.__get__(reviewer, SafetyReviewer)
    checker.review_workspace_writes = True
    target = tmp_path / "uncertain-scope.txt"
    first, _ = await _invoke(agent, _write(target, "unresolved write"))
    first_result = _only_result(first)
    assert first_result.is_error and "AUTO_REVIEW_INCONCLUSIVE:" in first_result.output
    reference = _review_ref(first_result.output)

    checker.review_workspace_writes = False
    edit = ToolCallComplete("edit", "EditFile", {
        "file_path": str(target), "old_string": "unresolved", "new_string": "changed",
    })
    held, _ = await _invoke(agent, edit)
    assert _only_result(held).is_error
    assert "reviewed_call=EditFile" in _only_result(held).output
    assert "AUTO_REVIEW_INCONCLUSIVE:" in _only_result(held).output
    assert reviewer.client.review_safety.await_count == 2

    independent_path = tmp_path / "independent.txt"
    independent, _ = await _invoke(agent, _write(independent_path, "independent"))
    assert not _only_result(independent).is_error
    assert independent_path.read_text(encoding="utf-8") == "independent"
    assert reviewer.client.review_safety.await_count == 2

    resolve = ToolCallComplete("abandon", "ResolveAutoReview", {
        "action": "abandon", "review_call_refs": [reference],
        "reason": "Leave the uncertain file unchanged and report the unresolved scope.",
    })
    resolved, _ = await _invoke(agent, resolve)
    assert "AUTO_OPERATION_ABANDONED:" in _only_result(resolved).output
    assert reference in agent._review_recovery.requests
    assert agent._review_recovery.requests[reference].abandoned
    assert agent._current_conversation.retired_safety_reviews[-1]["status"] == "abandoned_not_executed_or_approved"
    assert not target.exists()

    replacement, _ = await _invoke(agent, _write(target, "a materially different attempt"))
    assert _only_result(replacement).is_error
    assert "AUTO_REVIEW_INCONCLUSIVE:" in _only_result(replacement).output
    assert reviewer.client.review_safety.await_count == 3
    assert not target.exists()
    assert len(agent._review_recovery.requests) == 3
    assert agent._review_recovery.requests[reference].abandoned
    assert any(not item.abandoned for item in agent._review_recovery.pending_reviews())


@pytest.mark.asyncio
async def test_noninteractive_resolve_can_abandon_host_authority_request_without_a_popup(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    agent, _, reviewer = _make_agent(tmp_path, monkeypatch, [], work_dir=workspace)

    denied = await agent._execute_tool_noninteractive_impl(_write(outside))
    assert denied.is_error and "AUTO_DENIED:" in denied.output
    assert "blocker_kind=missing_authorization" in denied.output
    reference = _review_ref(denied.output)
    assert reviewer._ask_model.await_count == 0
    assert not outside.exists()

    resolve = ToolCallComplete("resolve-noninteractive", "ResolveAutoReview", {
        "action": "abandon", "review_call_refs": [reference],
        "reason": "The external path is outside this node's authority; return it to the user.",
    })
    resolved = await agent._execute_tool_noninteractive_impl(resolve)
    assert not resolved.is_error
    assert "AUTO_OPERATION_ABANDONED:" in resolved.output
    assert agent._review_recovery.requests[reference].abandoned
    assert reference in agent._review_recovery.requests
    assert reviewer._ask_model.await_count == 0
    assert not outside.exists()
