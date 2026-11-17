"""Repair proposals reach fresh review without reopening host permissions."""
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from mewcode import expert_web
from mewcode.expert_group import ExpertExecutionPolicy
from mewcode.model_registry import ModelRef
from mewcode.permissions.modes import PermissionMode
from mewcode.permissions.policy import build_permission_checker
from mewcode.permissions.reviewer import SafetyReviewResult
from mewcode.permissions.rules import Rule
from mewcode.tools.base import ToolCallComplete
from mewcode.tools.write_file import WriteFile
from tests.test_auto_effect_checkpoint import remember
from tests.test_effect_recovery_integration import _make_agent, _invoke, _write, _deny, _allow
from tests.test_expert_permission_policy import session_for


async def invoke(agent, call, mode):
    if mode == "completion":
        return await agent._execute_tool_noninteractive(call)
    results, _ = await _invoke(agent, call)
    assert len(results) == 1
    return results[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["stream", "completion"])
@pytest.mark.parametrize("approved", [False, True])
async def test_revised_same_target_gets_review_after_unchanged_attempts_stall(tmp_path, monkeypatch, mode, approved):
    agent, checker, reviewer = _make_agent(tmp_path, monkeypatch)
    reviewer._ask_model = AsyncMock(return_value=_deny())
    checker.review_workspace_writes = True
    target = tmp_path / "repair.py"
    original = _write(target, "old proposal")
    for _ in range(3):
        assert "AUTO_DENIED:" in (await invoke(agent, original, mode)).output
    reviews_before_hold = reviewer._ask_model.await_count
    assert "AUTO_REVIEW_DENIAL_LIMIT" in (await invoke(agent, original, mode)).output
    assert reviewer._ask_model.await_count == reviews_before_hold

    checker.review_workspace_writes = False
    reviewer._ask_model.return_value = _allow() if approved else _deny()
    corrected = _write(target, "corrected proposal")
    result = await invoke(agent, corrected, mode)
    assert reviewer._ask_model.await_count == reviews_before_hold + 1
    prior = reviewer._ask_model.call_args.kwargs["risk"]["host_prior_denials"]
    assert prior and all(row["arguments"] == original.arguments for row in prior)
    assert result.is_error is (not approved)
    assert target.exists() is approved
    if approved:
        assert target.read_text(encoding="utf-8") == "corrected proposal"


@pytest.mark.asyncio
async def test_stalled_checkpoint_keeps_history_but_accepts_corrected_proposal(tmp_path, monkeypatch):
    agent, checker, _ = _make_agent(tmp_path, monkeypatch, [_deny(), _deny(), _deny()])
    checker.review_workspace_writes = True
    original = _write(tmp_path / "repair.py", "old proposal")
    for _ in range(3):
        result = await invoke(agent, original, "stream")
    remember(agent, original, result)
    resumed, _, reviewer = _make_agent(tmp_path, monkeypatch, [_allow()])
    resumed._current_conversation = copy.deepcopy(agent._current_conversation)
    resumed._restore_safety_review_state(resumed._current_conversation)
    assert "AUTO_REVIEW_DENIAL_LIMIT" in (await invoke(resumed, original, "stream")).output
    assert not (await invoke(resumed, _write(tmp_path / "repair.py", "fixed"), "stream")).is_error
    reviewer._ask_model.assert_awaited_once()
    assert (tmp_path / "repair.py").read_text(encoding="utf-8") == "fixed"
    assert resumed._review_recovery.total_denials == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["stream", "completion"])
async def test_inconclusive_proposal_can_be_repaired_before_retiring_original(tmp_path, monkeypatch, mode):
    agent, _, reviewer = _make_agent(tmp_path, monkeypatch, [_allow()])
    target = tmp_path / "repair.py"
    target.write_text("old", encoding="utf-8")
    assert not (await invoke(agent, ToolCallComplete("read", "ReadFile", {"file_path": str(target)}), mode)).is_error
    original = _write(target, "unnecessary replacement")
    agent._inconclusive_review_message(SafetyReviewResult(decision="deny", risk="unknown",
        inconclusive=True, reason="replacement too broad", alternative="make a targeted edit"), original)
    pending = agent._review_recovery.pending_reviews()[0]
    corrected = ToolCallComplete("repair", "EditFile", {
        "file_path": str(target), "old_string": "old", "new_string": "fixed"})
    assert not (await invoke(agent, corrected, mode)).is_error
    reviewer._ask_model.assert_awaited_once()
    assert target.read_text(encoding="utf-8") == "fixed"
    assert not pending.abandoned and pending in agent._review_recovery.pending_reviews()
    # The executor may retire its obsolete proposal after the repair, without
    # asking the user or claiming that the old replacement executed.
    retired = ToolCallComplete("retire", "ResolveAutoReview", {"action": "abandon",
        "review_call_refs": [pending.reference], "reason": "Targeted edit completed the repair."})
    assert not (await invoke(agent, retired, mode)).is_error
    assert not agent._review_recovery.pending_reviews()
    assert pending.abandoned and all(row["tool_name"] != "WriteFile" for row in agent._review_recovery.ledger)


def test_recovery_role_uses_normal_workspace_policy(tmp_path, monkeypatch):
    parent = build_permission_checker(tmp_path, mode=PermissionMode.AUTO)
    session = session_for(parent, tmp_path)
    session.agent.client = SimpleNamespace(get_usage_recorder=lambda: None)
    created = []
    original = expert_web._expert_permission_checker

    def capture(*args, **kwargs):
        checker = original(*args, **kwargs)
        created.append(checker)
        return checker

    monkeypatch.setattr(expert_web, "_expert_permission_checker", capture)
    service = SimpleNamespace(resolve=Mock(side_effect=RuntimeError("stop before model creation")))
    with pytest.raises(RuntimeError, match="stop before model creation"):
        expert_web._build_role_agent(session, service, "executor", ModelRef("test", "test"),
            ExpertExecutionPolicy("stable", False, 0, execution_recovery=True))
    checker, = created
    args = {"file_path": str(tmp_path / "runtime.json"), "content": "{}"}
    assert checker.check(WriteFile(), args).effect == "allow"
    parent.rule_engine.append_session_rule(Rule("WriteFile", "*", "deny"))
    assert checker.check(WriteFile(), args).effect == "deny"
