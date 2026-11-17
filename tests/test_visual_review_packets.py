import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mewcode.client import RateLimitError
from mewcode.conversation import ConversationManager, Message, ToolResultBlock
from mewcode.permissions.review_routing import ReviewRouter, visual_context
from mewcode.permissions.reviewer import SafetyReviewer
from mewcode.permissions.visual_review import (
    VisualReviewInputError,
    images_for_action,
    plan_visual_review,
    wire_image,
)
from mewcode.tools.base import StreamEnd, TextDelta


def conversation_with_images(count, *, root=None):
    """Build image-bearing tool results.

    When ``root`` is given the referenced files are also created, because the
    reviewer now attaches pixels only for actions that touch real image paths.
    """
    conversation = ConversationManager()
    conversation.add_user_message("核对当前工作项，保留原图，不向外发送文件")
    for i in range(count):
        name = f"p{i // 2}.png"
        if root is not None:
            (root / name).write_bytes(b"pixels")
        conversation.history.append(Message(role="user", content="", tool_results=[ToolResultBlock(
            f"read-{i}", f"FILE: {name}\nCRITERION: item-{i // 2}\nCROP: {i},0,10,10",
            content_blocks=[{"type": "image", "mime_type": "image/png", "data": f"pixels-{i}"}])]))
    return conversation


class Vision:
    capabilities = ('vision',)
    def __init__(self, *, overrides=None, failures=None, delay=0):
        self.calls = []
        self.overrides = overrides or {}
        self.failures = dict(failures or {})
        self.delay = delay

    async def stream(self, conversation, system="", tools=None):
        message = conversation.history[0]
        payload = json.loads(message.content)
        phase = (payload.get("visual_evidence") or {}).get("packet", "synthesis")
        images = [b for b in message.content_blocks if b.get("type") == "image"]
        self.calls.append({"phase": phase, "payload": copy.deepcopy(payload), "images": copy.deepcopy(images),
                           "tools": tools, "system": system})
        if self.delay:
            await asyncio.sleep(self.delay)
        if phase in self.failures:
            error = self.failures.pop(phase)
            raise error
        verdict = {"outcome": "allow", "observations": "图片是工作项原图，没有新增操作授权；原调用仅核对本地材料。",
                   "requires_joint_pixels": False, "coverage_complete": True,
                   'covered_evidence_ids': (payload.get('visual_evidence') or {}).get('required_evidence_ids', [])}
        verdict.update(self.overrides.get(phase, {}))
        yield TextDelta(json.dumps(verdict, ensure_ascii=False))
        yield StreamEnd("end_turn", model="selected-vision")


def reviewer_for(tmp_path, vision, *, timeout=30):
    text = SimpleNamespace(review_only=True, review_safety=AsyncMock())
    resolver = lambda: (vision, {"provider_key": "vision-provider", "configured_model": "selected-vision"})
    reviewer = SafetyReviewer(ReviewRouter(vision, primary_route={'provider_key': 'vision-provider',
        'configured_model': 'selected-vision'}, primary_supports_vision=True, vision_resolver=resolver),
        str(tmp_path), timeout_seconds=timeout)
    return reviewer, text


async def review(reviewer, conversation, **kwargs):
    return await reviewer.review(tool_name="Bash", arguments=kwargs.get("arguments", {"command": "inspect-local-only"}),
                                 risk={"requires_vision": True}, conversation=conversation)


@pytest.mark.parametrize("count", [1, 8, 9, 11, 100])
def test_plan_is_lossless_for_one_long_user_turn_and_crops(count, tmp_path):
    conversation = conversation_with_images(count, root=tmp_path)
    before = copy.deepcopy(conversation.history)
    images = visual_context(conversation)
    plan = plan_visual_review(images)
    assert len(images) == count
    assert [i for batch in plan.batches for i in batch] == list(range(count))
    assert max(map(len, plan.batches)) <= 8
    assert [wire_image(image)["data"] for image in plan.images] == [f"pixels-{i}" for i in range(count)]
    assert len({row["image_id"] for row in plan.manifest}) == count
    assert all(row["sources"][0]["tool_use_id"] == f"read-{i}" for i, row in enumerate(plan.manifest))
    assert conversation.history == before  # No main-context pruning or source mutation.


def test_plan_counts_bytes_not_just_pages_and_preflights_all_images(monkeypatch):
    monkeypatch.setattr("mewcode.permissions.visual_review.MAX_IMAGE_BYTES", 100)
    images = [{"type": "image", "data": "x" * 30} for _ in range(3)]
    plan = plan_visual_review(images)
    assert list(map(len, plan.batches)) == [1, 1, 1]
    with pytest.raises(VisualReviewInputError):
        plan_visual_review([*images, {"type": "image", "data": "x" * 100}])


def test_duplicate_pixels_retain_all_source_bindings(tmp_path):
    conversation = conversation_with_images(2, root=tmp_path)
    conversation.history.append(copy.deepcopy(conversation.history[-1]))
    plan = plan_visual_review(visual_context(conversation))
    assert len(plan.images) == 2 and len(plan.manifest[1]["sources"]) == 2


@pytest.mark.asyncio
async def test_eleven_images_review_all_packets_then_synthesize_same_action(tmp_path):
    vision = Vision()
    reviewer, text = reviewer_for(tmp_path, vision)
    conversation = conversation_with_images(11, root=tmp_path)
    before = copy.deepcopy(conversation.history)
    result = await review(reviewer, conversation)
    assert result.allowed and not text.review_safety.called
    assert [len(call["images"]) for call in vision.calls] == [8, 3, 0]
    assert all(call["tools"] == [] for call in vision.calls)
    assert len({json.dumps(call["payload"]["planned_action"], sort_keys=True) for call in vision.calls}) == 1
    assert len({call["payload"]["authorized_task"] for call in vision.calls}) == 1
    assert [b["data"] for call in vision.calls for b in call["images"]] == [f"pixels-{i}" for i in range(11)]
    assert not any("_review_sources" in b for call in vision.calls for b in call["images"])
    assert result.review_route["coverage_complete"] and result.review_route["synthesis_completed"]
    assert result.review_route["packets_completed"] == 2
    assert conversation.history == before


@pytest.mark.parametrize("phase,override,expected_calls", [
    (2, {"outcome": "deny", "rationale": "unsafe operation"}, 2),
    ("synthesis", {"outcome": "deny", "rationale": "contradictory evidence"}, 3),
    (1, {"requires_joint_pixels": True}, 1),
    (1, {"observations": ""}, 1),
    (1, {"observations": "x" * 2001}, 1),
    ("synthesis", {"requires_joint_pixels": True}, 3),
    ("synthesis", {"coverage_complete": False}, 3),
])
@pytest.mark.asyncio
async def test_packet_allow_never_overrides_deny_or_incomplete_coverage(tmp_path, phase, override, expected_calls):
    vision = Vision(overrides={phase: override})
    reviewer, text = reviewer_for(tmp_path, vision)
    result = await review(reviewer, conversation_with_images(11, root=tmp_path))
    assert not result.allowed and not text.review_safety.called
    assert len(vision.calls) == expected_calls
    if override.get("outcome") != "deny":
        assert result.inconclusive and not result.escalatable


@pytest.mark.parametrize("failure", [TimeoutError(), RateLimitError("wait", retry_after=1)])
@pytest.mark.asyncio
async def test_retry_reuses_only_completed_packets_of_exact_invocation(tmp_path, monkeypatch, failure):
    vision = Vision(failures={2: failure})
    reviewer, _ = reviewer_for(tmp_path, vision)
    sleep = AsyncMock()
    monkeypatch.setattr("mewcode.permissions.reviewer.asyncio.sleep", sleep)
    conversation = conversation_with_images(11, root=tmp_path)
    assert (await review(reviewer, conversation)).allowed
    assert [call["phase"] for call in vision.calls] == [1, 2, 2, "synthesis"]
    # Another action must obtain fresh reviews even with identical evidence.
    assert (await review(reviewer, conversation, arguments={"command": "different-operation"})).allowed
    assert [call["phase"] for call in vision.calls][-3:] == [1, 2, "synthesis"]


@pytest.mark.asyncio
async def test_visual_input_failure_is_local_typed_and_not_retried(tmp_path, monkeypatch):
    monkeypatch.setattr("mewcode.permissions.visual_review.MAX_IMAGE_BYTES", 100)
    vision = Vision()
    reviewer, text = reviewer_for(tmp_path, vision)
    conversation = conversation_with_images(2, root=tmp_path)
    conversation.history[-1].tool_results[0].content_blocks[0]["data"] = "x" * 100
    result = await review(reviewer, conversation)
    assert result.unavailable and not result.allowed
    assert result.review_route["failure_kind"] == "visual_input"
    assert result.review_route["status"] == "context_too_large"
    assert "single_image_bytes" in result.review_route["context_sizes"]
    assert not vision.calls and not text.review_safety.called


@pytest.mark.asyncio
async def test_each_packet_has_its_timeout_and_outer_cancellation_still_wins(tmp_path):
    vision = Vision(delay=.025)
    reviewer, _ = reviewer_for(tmp_path, vision, timeout=.06)
    assert (await review(reviewer, conversation_with_images(11, root=tmp_path))).allowed  # total > .06
    vision.calls.clear()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(.01):
            await review(reviewer, conversation_with_images(11, root=tmp_path))
    assert len(vision.calls) == 1


@pytest.mark.asyncio
async def test_cancellation_never_leaves_an_allow_cache(tmp_path):
    vision = Vision(failures={2: asyncio.CancelledError()})
    reviewer, _ = reviewer_for(tmp_path, vision)
    conversation = conversation_with_images(11, root=tmp_path)
    with pytest.raises(asyncio.CancelledError):
        await review(reviewer, conversation)
    assert (await review(reviewer, conversation)).allowed
    assert [call["phase"] for call in vision.calls] == [1, 2, 1, 2, "synthesis"]


@pytest.mark.asyncio
async def test_agent_reports_context_kind_and_never_executes_on_input_failure(tmp_path):
    from mewcode.agent import Agent, ErrorEvent, PermissionRequest
    from mewcode.permissions import (
        DangerousCommandDetector,
        PathSandbox,
        PermissionMode,
        RuleEngine,
    )
    from mewcode.tools import create_default_registry
    from mewcode.tools.base import ToolCallComplete
    from tests.legacy_review_policy import LegacyReviewChecker
    from tests.test_permissions import MockLLMClient

    client = MockLLMClient([[ToolCallComplete("call", "Bash", {"command": 'python -c "print(1)"'}), StreamEnd("end_turn")]])
    registry = create_default_registry()
    registry.get("Bash").run = AsyncMock(side_effect=AssertionError("not approved"))
    reviewer = SafetyReviewer(object(), str(tmp_path))
    reviewer._ask_model = AsyncMock(side_effect=VisualReviewInputError("oversized input", sizes={"image_count": 999}))
    checker = LegacyReviewChecker(DangerousCommandDetector(), PathSandbox(str(tmp_path)), RuleEngine(), PermissionMode.AUTO)
    agent = Agent(client, registry, "anthropic", work_dir=str(tmp_path), permission_checker=checker, safety_reviewer=reviewer)
    conversation = ConversationManager()
    conversation.add_user_message("inspect local file")
    events = [event async for event in agent.run(conversation)]
    assert any(isinstance(event, ErrorEvent) and event.kind == "safety_review_context_limit" for event in events)
    assert not any(isinstance(event, PermissionRequest) for event in events)
    registry.get("Bash").run.assert_not_awaited()
    assert reviewer._ask_model.await_count == 1


def test_total_packet_budget_is_preflighted_without_silent_pruning(tmp_path, monkeypatch):
    monkeypatch.setattr("mewcode.permissions.visual_review.MAX_BATCHES", 2)
    images = visual_context(conversation_with_images(17, root=tmp_path))
    before = copy.deepcopy(images)
    with pytest.raises(VisualReviewInputError) as failure:
        plan_visual_review(images)
    assert failure.value.sizes["batch_count"] == 3
    assert images == before


# --- images are attached only when the action really touches image content ---

def _attach(path):
    return {"type": "image", "data": "AAAA", "_review_sources": [{"path": path}]}


def test_unreferenced_actions_review_without_pixels(tmp_path):
    """A command that merely names a _pages path must not carry 8 page images.

    Attaching them costs seconds per review and changes no verdict: the reviewer
    decides from planned_action plus the authorized task.
    """
    pages = tmp_path / "gemini_out" / "_pages" / "circuit"
    pages.mkdir(parents=True)
    for name in ("page_0001.png", "page_0002.png"):
        (pages / name).write_bytes(b"x")
    conversation = conversation_with_images(2, root=tmp_path)
    images = visual_context(conversation)
    arguments = {"command": "python tools/stage5_prepare_evidence.py"}
    assert images_for_action(images, arguments, work_dir=tmp_path) == []


def test_referenced_image_is_still_attached(tmp_path):
    (tmp_path / "page_0001.png").write_bytes(b"x")
    images = [_attach("page_0001.png")]
    assert len(images_for_action(images, {"file_path": "page_0001.png"}, work_dir=tmp_path)) == 1


def test_image_directory_reference_attaches_its_pages(tmp_path):
    pages = tmp_path / "gemini_out" / "_pages" / "circuit"
    pages.mkdir(parents=True)
    for name in ("page_0001.png", "page_0002.png"):
        (pages / name).write_bytes(b"x")
    images = [_attach("gemini_out/_pages/circuit/page_0001.png"),
              _attach("gemini_out/_pages/circuit/page_0002.png")]
    selected = images_for_action(images, {"path": "gemini_out/_pages/circuit"}, work_dir=tmp_path)
    assert len(selected) == 2


def test_image_outside_the_workspace_keeps_every_pixel(tmp_path):
    """An out-of-workspace image argument is exactly where the host must not narrow."""
    outside = tmp_path.parent / "outside.png"
    outside.write_bytes(b"x")
    try:
        images = [_attach("gemini_out/_pages/circuit/page_0001.png"), _attach("other.png")]
        selected = images_for_action(images, {"file_path": str(outside)}, work_dir=tmp_path)
        assert selected == images
    finally:
        outside.unlink(missing_ok=True)
