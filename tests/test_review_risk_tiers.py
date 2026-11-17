from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mewcode.conversation import ConversationManager
from mewcode.permissions.review_routing import ReviewRouter
from mewcode.permissions.review_outcome import ReviewInconclusive
from mewcode.permissions.reviewer import SafetyReviewer


@pytest.mark.asyncio
async def test_unrelated_business_image_does_not_make_action_safety_visual(tmp_path):
    text = SimpleNamespace(review_only=True, review_safety=AsyncMock(
        return_value={"outcome": "allow", "risk_level": "low", "rationale": "local action"}))
    visual_calls = []
    router = ReviewRouter(text, vision_resolver=lambda: visual_calls.append(True))
    conversation = ConversationManager()
    conversation.add_user_message("Inspect image as business evidence")
    conversation.history[-1].content_blocks = [{"type": "image", "data": "pixels"}]
    result = await SafetyReviewer(router, str(tmp_path)).review(
        tool_name="Bash", arguments={"command": "echo local"}, risk={}, conversation=conversation)
    assert result.allowed and result.review_route["risk_tier"] == 1
    assert text.review_safety.await_count == 1
    assert visual_calls == []


@pytest.mark.asyncio
async def test_declared_backup_only_runs_after_low_confidence_and_keeps_receipt(tmp_path):
    primary = SimpleNamespace(review_only=True, review_safety=AsyncMock(
        side_effect=ReviewInconclusive("low confidence", route={"model": "primary"},
                                       unresolved=["risk"])))
    backup = SimpleNamespace(review_only=True, review_safety=AsyncMock(
        return_value={"outcome": "allow", "risk_level": "low", "rationale": "bounded action"}))
    router = ReviewRouter(primary, primary_route={"model": "primary"},
                          fallback_resolver=lambda: (backup, {"model": "declared-backup"}))
    result = await SafetyReviewer(router, str(tmp_path)).review(
        tool_name="Bash", arguments={"command": "echo local"}, risk={}, conversation=None)
    assert result.allowed
    assert result.review_route["model"] == "declared-backup"
    assert result.review_route["fallback_for"] == "low_confidence"
    assert result.review_route["primary_unresolved"] == ["risk"]
    assert primary.review_safety.await_count == backup.review_safety.await_count == 1


@pytest.mark.asyncio
async def test_no_declared_backup_keeps_low_confidence_failed_closed(tmp_path):
    primary = SimpleNamespace(review_only=True, review_safety=AsyncMock(
        side_effect=ReviewInconclusive("low confidence", route={}, unresolved=["scope"])))
    result = await SafetyReviewer(ReviewRouter(primary), str(tmp_path)).review(
        tool_name="Bash", arguments={"command": "echo local"}, risk={}, conversation=None)
    assert not result.allowed and result.inconclusive


@pytest.mark.asyncio
async def test_backup_never_overrides_an_explicit_policy_denial(tmp_path):
    primary = SimpleNamespace(review_only=True, review_safety=AsyncMock(return_value={
        "outcome": "deny", "risk_level": "high", "rationale": "violates policy",
        "blocker_kind": "policy_violation",
    }))
    backup = SimpleNamespace(review_only=True, review_safety=AsyncMock(return_value={
        "outcome": "allow", "risk_level": "low", "rationale": "would allow",
    }))
    router = ReviewRouter(primary, fallback_resolver=lambda: (backup, {"model": "backup"}))
    result = await SafetyReviewer(router, str(tmp_path)).review(
        tool_name="Bash", arguments={"command": "echo local"}, risk={}, conversation=None)
    assert not result.allowed and result.denial_kind == "policy_violation"
    assert backup.review_safety.await_count == 0
