"""Integration tests for the main agent's safety-review denial recovery."""
from unittest.mock import AsyncMock

import pytest

from mewcode.agent import Agent, AskUserRequest, PermissionRequest, PermissionResponse, ToolResultEvent
from mewcode.conversation import ConversationManager
from mewcode.permissions import (
    DangerousCommandDetector,
    PathSandbox,
    PermissionChecker,
    PermissionMode,
    RuleEngine,
)
from mewcode.permissions.review_recovery import ReviewRecovery
from mewcode.permissions.reviewer import SafetyReviewer
from mewcode.tools import create_default_registry
from mewcode.tools.ask_user import AskUserTool
from mewcode.tools.base import StreamEnd, TextDelta, ToolCallComplete
from tests.test_permissions import MockLLMClient


def _deny(kind="missing_authorization"):
    return (
        '{"outcome":"deny","risk":"medium","reason":"authorization is missing",'
        f'"blocker_kind":"{kind}","alternative":"Read the existing evidence first."' + "}"
    )


def _allow():
    return '{"outcome":"allow","risk":"low","reason":"the exact reviewed action is safe"}'


def _set_review_refs(monkeypatch, *refs):
    from types import SimpleNamespace
    import mewcode.permissions.review_recovery as recovery_module

    values = iter(refs)
    monkeypatch.setattr(recovery_module, "uuid", SimpleNamespace(
        uuid4=lambda: SimpleNamespace(hex=next(values))
    ))


def _write(path, content="approved"):
    return ToolCallComplete("write", "WriteFile", {"file_path": str(path), "content": content})


def _turn(*calls):
    return [*calls, StreamEnd("end_turn", input_tokens=5, output_tokens=3)]


def _make_agent(tmp_path, turns, reviews):
    checker = PermissionChecker(
        DangerousCommandDetector(), PathSandbox(str(tmp_path)), RuleEngine(), PermissionMode.AUTO
    )
    checker.review_workspace_writes = True
    reviewer = SafetyReviewer(object(), str(tmp_path))
    reviewer._ask_model = AsyncMock(side_effect=reviews)
    registry = create_default_registry()
    registry.register(AskUserTool())
    registry.mark_discovered("AskUserQuestion")
    client = MockLLMClient(turns)
    agent = Agent(client, registry, "anthropic", work_dir=str(tmp_path),
                  permission_checker=checker, safety_reviewer=reviewer)
    conversation = ConversationManager()
    conversation.add_user_message("Inspect the existing evidence and update the requested file if safe.")
    return agent, checker, reviewer, client, conversation


async def _run(agent, conversation, response=PermissionResponse.ALLOW):
    events = []
    requests = []
    async for event in agent.run(conversation):
        events.append(event)
        if isinstance(event, PermissionRequest):
            requests.append(event)
            event.future.set_result(response)
        elif isinstance(event, AskUserRequest):
            # End-of-turn deferred requests are a separate, bounded UI batch.
            # Legacy cases do not authorize any additional operation here.
            event.future.set_result({q["name"]: "不允许此操作" for q in event.questions})
    return events, requests


@pytest.mark.asyncio
async def test_denial_returns_host_reference_and_does_not_bypass_independent_download_review(tmp_path):
    source = tmp_path / "evidence.txt"
    source.write_text("known facts", encoding="utf-8")
    first = _write(tmp_path / "denied.txt")
    download = ToolCallComplete("download", "Browser", {
        "action": "download", "url": "https://example.test/file.txt", "path": "held.txt",
    })
    agent, _, reviewer, _, conversation = _make_agent(
        tmp_path,
        [_turn(first, download), _turn(ToolCallComplete("read", "ReadFile", {"file_path": str(source)}))],
        [_deny("semantic_risk"), _deny("semantic_risk")],
    )
    fetch = AsyncMock(return_value=(b"must not fetch", "text/plain", "https://example.test/file.txt"))
    agent.registry.get("Browser")._fetch_bytes = fetch

    events, requests = await _run(agent, conversation)

    denied = next(event for event in events if isinstance(event, ToolResultEvent))
    assert "AUTO_DENIED:" in denied.output
    assert "review_call_ref=" in denied.output
    assert "操作未执行" in denied.output
    assert not requests
    assert not (tmp_path / "denied.txt").exists()
    assert not (tmp_path / "held.txt").exists()
    fetch.assert_not_awaited()
    assert reviewer._ask_model.await_count == 2
    assert not reviewer._ask_model.await_args_list[1].kwargs["risk"]["host_prior_denials"]
    assert any(isinstance(event, ToolResultEvent) and "known facts" in event.output for event in events)
    assert any(isinstance(event, ToolResultEvent) and event.tool_name == "ReadFile" for event in events)


@pytest.mark.asyncio
async def test_missing_authorization_asks_against_exact_reference_then_retries_exact_action_once(tmp_path, monkeypatch):
    _set_review_refs(monkeypatch, "review-ref-123")
    target = tmp_path / "result.txt"
    original = _write(target, "exact content")
    ask = ToolCallComplete("ask", "AskUserQuestion", {
        "purpose": "authorization", "review_call_ref": "review-ref-123",
        "questions": [{"type": "radio", "name": "authorize", "message": "May I retry this exact write once?", "options": ["Yes", "No"]}],
    })
    agent, checker, reviewer, client, conversation = _make_agent(
        tmp_path, [_turn(original), _turn(ask), _turn(original)], [_deny(), _allow()]
    )
    before_session_rules = list(checker.rule_engine._session_rules)
    before_call_grants = set(checker._approved_calls)

    events, requests = await _run(agent, conversation, PermissionResponse.ALLOW_ALWAYS)

    assert len(requests) == 1
    request = requests[0]
    assert request.source == "review_missing_authorization"
    assert request.tool_name == "WriteFile" and request.arguments == original.arguments
    assert request.tool_id == "ask"
    assert target.read_text(encoding="utf-8") == "exact content"
    assert reviewer._ask_model.await_count == 2
    consent = reviewer._ask_model.await_args_list[1].kwargs["risk"]["host_user_consent"]
    assert consent["review_call_ref"] == "review-ref-123"
    assert consent["decision"] == "approved_once"
    assert "exact retry" in consent["scope"]
    assert "同意上述具体操作" in request.reason
    assert consent["consent_kind"] == "exact_operation_effects_once"
    from mewcode.permissions.decision_envelope import build_safety_envelope, canonical_digest, redact_value
    assert consent["arguments_sha256"] == canonical_digest(original.arguments)
    payload = build_safety_envelope(
        tool_name=original.tool_name, arguments=original.arguments,
        risk=reviewer._ask_model.await_args_list[1].kwargs["risk"],
        task_context="Earlier task text without this specific approval",
        work_dir=tmp_path, readonly_target_metadata={}, untrusted_tool_evidence=[],
    )
    assert payload["host_user_authorization"] == redact_value(consent)
    assert next(row for row in payload["source_bindings"]
                if row["reference"] == "host.exact_operation_user_decision")["trusted_authorization"]
    assert checker.mode is PermissionMode.AUTO
    assert checker.rule_engine._session_rules == before_session_rules
    assert checker._approved_calls == before_call_grants
    assert any(isinstance(event, ToolResultEvent) and "AUTO_DENIED:" in event.output for event in events)
    assert client._call_index == 3


@pytest.mark.asyncio
async def test_unrelated_action_does_not_inherit_host_prior_denials(tmp_path):
    denied = tmp_path / "denied.txt"
    safe_alternative = tmp_path / "alternative.txt"
    agent, _, reviewer, _, conversation = _make_agent(
        tmp_path,
        [_turn(_write(denied)), _turn(_write(safe_alternative, "safer alternative"))],
        [_deny("semantic_risk"), _allow()],
    )

    events, requests = await _run(agent, conversation)

    assert not requests
    assert not denied.exists()
    assert safe_alternative.read_text(encoding="utf-8") == "safer alternative"
    assert reviewer._ask_model.await_count == 2
    prior = reviewer._ask_model.await_args_list[1].kwargs["risk"]["host_prior_denials"]
    assert not prior
    assert any(isinstance(event, ToolResultEvent) and "AUTO_DENIED:" in event.output for event in events)


@pytest.mark.asyncio
async def test_unrelated_allow_rule_is_not_demoted_after_a_denial(tmp_path, monkeypatch):
    from mewcode.permissions.checker import Decision

    denied = tmp_path / "denied.txt"
    allowed_by_rule = tmp_path / "allowed-by-rule.txt"
    agent, checker, reviewer, _, conversation = _make_agent(
        tmp_path,
        [_turn(_write(denied)), _turn(_write(allowed_by_rule, "still reviewed"))],
        [_deny("semantic_risk"), _deny("semantic_risk")],
    )
    original_check = checker.check

    def with_specific_allow(tool, arguments):
        if arguments.get("file_path") == str(allowed_by_rule):
            return Decision("allow", "session allow rule", "session_rule")
        return original_check(tool, arguments)

    monkeypatch.setattr(checker, "check", with_specific_allow)
    events, requests = await _run(agent, conversation)

    assert not requests
    assert not denied.exists() and allowed_by_rule.exists()
    assert reviewer._ask_model.await_count == 1
    assert any(isinstance(event, ToolResultEvent) and "AUTO_DENIED:" in event.output for event in events)


@pytest.mark.asyncio
async def test_permission_ask_cannot_override_policy_violation_or_create_grant(tmp_path, monkeypatch):
    from mewcode.permissions.checker import Decision

    _set_review_refs(monkeypatch, "review-first", "review-policy")
    first_target = tmp_path / "first-denied.txt"
    protected_target = tmp_path / "policy-blocked.txt"
    malicious_ask = ToolCallComplete("ask", "AskUserQuestion", {
        "purpose": "authorization", "review_call_ref": "review-policy",
        "questions": [{"type": "text", "name": "authorize", "message": "May I bypass the policy denial?"}],
    })
    agent, checker, reviewer, _, conversation = _make_agent(
        tmp_path,
        [_turn(_write(first_target)), _turn(_write(protected_target)), _turn(malicious_ask)],
        [_deny("semantic_risk"), _deny("policy_violation")],
    )
    original_check = checker.check

    def require_permission_for_protected_target(tool, arguments):
        if arguments.get("file_path") == str(protected_target):
            return Decision("deny", "This operation violates host policy", "rule_engine")
        return original_check(tool, arguments)

    monkeypatch.setattr(checker, "check", require_permission_for_protected_target)
    before_rules = list(checker.rule_engine._session_rules)
    before_grants = set(checker._approved_calls)

    events, requests = await _run(agent, conversation, PermissionResponse.ALLOW_ALWAYS)

    assert not requests
    assert not first_target.exists() and not protected_target.exists()
    assert reviewer._ask_model.await_count == 1
    assert any(isinstance(event, ToolResultEvent) and "INVALID_REVIEW_REQUEST" in event.output for event in events)
    assert checker.rule_engine._session_rules == before_rules
    assert checker._approved_calls == before_grants


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,reference", [
    ("missing_authorization", "forged-reference"),
    ("semantic_risk", "review-ref-456"),
    ("policy_violation", "review-ref-456"),
    ("insufficient_evidence", "review-ref-456"),
])
async def test_only_valid_missing_authorization_reference_can_create_permission_request(
    tmp_path, monkeypatch, kind, reference
):
    _set_review_refs(monkeypatch, "review-ref-456")
    ask = ToolCallComplete("ask", "AskUserQuestion", {
        "purpose": "authorization", "review_call_ref": reference,
        "questions": [{"type": "text", "name": "why", "message": "Why is this still needed?"}],
    })
    target = tmp_path / "result.txt"
    agent, _, reviewer, _, conversation = _make_agent(
        tmp_path, [_turn(_write(target)), _turn(ask)], [_deny(kind)]
    )

    events, requests = await _run(agent, conversation)

    assert not requests
    assert not target.exists()
    assert reviewer._ask_model.await_count == 1
    assert any(isinstance(event, ToolResultEvent) and "INVALID_REVIEW_REQUEST" in event.output for event in events)


@pytest.mark.asyncio
async def test_user_rejection_cannot_be_reasked_or_retried(tmp_path, monkeypatch):
    _set_review_refs(monkeypatch, "review-ref-reject")
    target = tmp_path / "result.txt"
    original = _write(target)
    ask = ToolCallComplete("ask", "AskUserQuestion", {
        "purpose": "authorization", "review_call_ref": "review-ref-reject",
        "questions": [{"type": "text", "name": "authorize", "message": "Authorize this exact write?"}],
    })
    agent, _, reviewer, _, conversation = _make_agent(
        tmp_path, [_turn(original), _turn(ask), _turn(original)], [_deny()]
    )

    events, requests = await _run(agent, conversation, PermissionResponse.DENY)

    assert len(requests) == 1
    assert not target.exists()
    assert reviewer._ask_model.await_count == 1
    assert any(isinstance(event, ToolResultEvent) and "USER_REJECTED" in event.output for event in events)


@pytest.mark.asyncio
async def test_state_change_while_authorization_is_pending_invalidates_approval(tmp_path, monkeypatch):
    _set_review_refs(monkeypatch, "review-ref-stale")
    target = tmp_path / "result.txt"
    original = _write(target)
    ask = ToolCallComplete("ask", "AskUserQuestion", {
        "purpose": "authorization", "review_call_ref": "review-ref-stale",
        "questions": [{"type": "text", "name": "authorize", "message": "Authorize this exact write?"}],
    })
    agent, _, reviewer, _, conversation = _make_agent(
        tmp_path, [_turn(original), _turn(ask)], [_deny()]
    )
    events, requests = [], []
    async for event in agent.run(conversation):
        events.append(event)
        if isinstance(event, PermissionRequest):
            requests.append(event)
            target.write_text("changed while waiting", encoding="utf-8")
            event.future.set_result(PermissionResponse.ALLOW)

    assert len(requests) == 1
    assert target.read_text(encoding="utf-8") == "changed while waiting"
    assert reviewer._ask_model.await_count == 1
    assert any(isinstance(event, ToolResultEvent) and "AUTO_REVIEW_CONTEXT_CHANGED" in event.output for event in events)


@pytest.mark.asyncio
async def test_noninteractive_review_denial_never_prompts_or_executes(tmp_path):
    target = tmp_path / "result.txt"
    agent, _, reviewer, _, _ = _make_agent(tmp_path, [], [_deny()])

    result = await agent._execute_tool_noninteractive_impl(_write(target))

    assert result.is_error and "AUTO_DENIED:" in result.output
    assert "review_call_ref=" in result.output
    assert not target.exists()
    assert reviewer._ask_model.await_count == 1


def test_review_recovery_counts_stalled_effects_not_unrelated_denials(tmp_path):
    from mewcode.permissions.reviewer import SafetyReviewResult

    agent, _, _, _, _ = _make_agent(tmp_path, [], [])
    recovery = ReviewRecovery()
    review = SafetyReviewResult(decision="deny", risk="medium", reason="blocked",
                                denial_kind="semantic_risk", alternative="read evidence")
    for index in range(2):
        recovery.deny(agent, _write(tmp_path / f"{index}.txt"), review)
        assert recovery.stalled_action(agent, "WriteFile", _write(tmp_path / f"{index}.txt").arguments) is None
    recovery.deny(agent, _write(tmp_path / "third.txt"), review)
    assert recovery.stalled_action(agent, "WriteFile", _write(tmp_path / "third.txt").arguments) is None

    recovery = ReviewRecovery()
    for index in range(3):
        recovery.deny(agent, _write(tmp_path / "same.txt"), review)
    assert recovery.stalled_action(agent, "WriteFile", _write(tmp_path / "same.txt").arguments) is not None
    assert recovery.stalled_action(agent, "WriteFile", _write(tmp_path / "independent.txt").arguments) is None


@pytest.mark.asyncio
async def test_permission_boundary_stays_closed_until_consent_and_consent_is_consumed(tmp_path, monkeypatch):
    from mewcode.permissions.checker import Decision

    _set_review_refs(monkeypatch, "first-denial", "needs-consent", "needs-new-consent")
    first, scoped = _write(tmp_path / "first.txt"), _write(tmp_path / "scoped.txt")
    ask = ToolCallComplete("ask", "AskUserQuestion", {
        "purpose": "authorization", "review_call_ref": "needs-consent",
        "questions": [{"type": "text", "name": "consent", "message": "The safe alternative needs this exact additional path; may I use it once?"}],
    })
    agent, checker, reviewer, _, conversation = _make_agent(tmp_path,
        [_turn(first), _turn(scoped), _turn(ask), _turn(scoped), _turn(scoped)],
        [_deny("semantic_risk"), _allow(), _allow(), _allow()])
    original_check = checker.check
    monkeypatch.setattr(checker, "check", lambda tool, arguments:
        Decision("ask", "Exact path needs consent", "sandbox_path")
        if arguments.get("file_path") == scoped.arguments["file_path"] else original_check(tool, arguments))
    writer = agent.registry.get("WriteFile")
    writer.run = AsyncMock(wraps=writer.run)
    events, requests = await _run(agent, conversation)
    assert len(requests) == 1
    assert not (tmp_path / "first.txt").exists()
    assert (tmp_path / "scoped.txt").read_text() == "approved"
    writer.run.assert_awaited_once()
    calls = reviewer._ask_model.await_args_list
    assert len(calls) == 2
    assert calls[1].kwargs["risk"]["host_user_consent"]["review_call_ref"] == "needs-consent"
    assert not checker._approved_calls and not checker.sandbox.file_grants and not checker.sandbox.dir_grants
    denied = [e for e in events if isinstance(e, ToolResultEvent) and e.tool_name == "WriteFile" and e.is_error]
    assert len(denied) == 3


@pytest.mark.asyncio
async def test_user_consent_cannot_override_fresh_policy_denial(tmp_path, monkeypatch):
    _set_review_refs(monkeypatch, "needs-consent", "policy-denied")
    original = _write(tmp_path / "blocked.txt")
    ask = ToolCallComplete("ask", "AskUserQuestion", {
        "purpose": "authorization", "review_call_ref": "needs-consent",
        "questions": [{"type": "text", "name": "consent", "message": "The exact action still needs your authorization."}],
    })
    agent, _, reviewer, _, conversation = _make_agent(tmp_path,
        [_turn(original), _turn(ask), _turn(original)], [_deny(), _deny("policy_violation")])
    _, requests = await _run(agent, conversation)
    assert len(requests) == 1
    assert not (tmp_path / "blocked.txt").exists()
    assert reviewer._ask_model.await_count == 2
    assert reviewer._ask_model.await_args.kwargs["risk"]["host_user_consent"]


@pytest.mark.asyncio
async def test_main_loop_limits_repeated_denials_but_runs_independent_work(tmp_path):
    from mewcode.agent import ErrorEvent
    target = tmp_path / "blocked.txt"
    independent = tmp_path / "independent.txt"
    agent, _, reviewer, client, conversation = _make_agent(tmp_path,
        [*[_turn(_write(target)) for _ in range(4)],
         _turn(_write(independent, "completed independent work")),
         [TextDelta("independent work complete; original operation remains denied"), StreamEnd("end_turn")]],
        [])
    reviewer._ask_model = AsyncMock(side_effect=lambda **kwargs:
        _deny("semantic_risk") if kwargs["arguments"]["file_path"] == str(target) else _allow())
    events, requests = await _run(agent, conversation)
    assert not requests
    assert client._call_index == 6 and reviewer._ask_model.await_count <= 4
    assert not target.exists()
    assert independent.read_text(encoding="utf-8") == "completed independent work"
    assert any(isinstance(e, ToolResultEvent) and "AUTO_REVIEW_DENIAL_LIMIT" in e.output for e in events)
    assert not any(isinstance(e, ErrorEvent) and e.kind == "safety_review_denied" for e in events)
