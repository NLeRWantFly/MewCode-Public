from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

from mewcode.conversation import Message, ToolResultBlock, ToolUseBlock
from mewcode.permissions import (
    DangerousCommandDetector, PathSandbox, PermissionChecker, PermissionMode, RuleEngine,
)
from mewcode.pipeline.assets import AssetStore
from mewcode.pipeline.executor import PipelineExecutor
from mewcode.pipeline.models import AssetRef, GraphEdge, GraphNode, NodeType, PipelineGraph
from mewcode.pipeline.variables import VariableContext
from mewcode.serialization import (
    build_anthropic_messages,
    build_chat_completion_messages,
    build_openai_input,
)


PNG = b"\x89PNG\r\n\x1a\nminimal"


def image_block() -> dict:
    return {
        "type": "image",
        "mime_type": "image/png",
        "data": base64.b64encode(PNG).decode("ascii"),
    }


def test_image_content_maps_to_all_protocols() -> None:
    message = Message(role="user", content="inspect", content_blocks=[image_block()])
    anthropic = build_anthropic_messages([message])[0]["content"]
    responses = build_openai_input([message])[0]["content"]
    chat = build_chat_completion_messages([message])[0]["content"]
    assert [block["type"] for block in anthropic] == ["text", "image"]
    assert [block["type"] for block in responses] == ["input_text", "input_image"]
    assert [block["type"] for block in chat] == ["text", "image_url"]


def test_tool_screenshot_is_not_downgraded_to_placeholder() -> None:
    message = Message(
        role="user", content="",
        tool_results=[ToolResultBlock("call-1", "done", content_blocks=[image_block()])],
    )
    messages = [Message(role="assistant", content="", tool_uses=[ToolUseBlock("call-1", "ReadAuditArtifact", {})]), message]
    anthropic = build_anthropic_messages(messages)[1]["content"][0]
    responses = build_openai_input(messages)
    chat = build_chat_completion_messages(messages)
    assert anthropic["content"][1]["type"] == "image"
    assert responses[2]["content"][1]["type"] == "input_image"
    assert chat[2]["content"][1]["type"] == "image_url"


def test_asset_store_is_content_addressed(tmp_path) -> None:
    store = AssetStore(tmp_path)
    first = store.put(PNG, "image/png", "one.png")
    second = store.put(PNG, "image/png", "two.png")
    assert first.asset == second.asset
    assert len(list(store.root.glob("*"))) == 1
    assert base64.b64decode(store.content_block(first)["data"]) == PNG


def test_variable_asset_reference_uses_side_channel() -> None:
    ref = {"asset": "sha256:" + "a" * 64, "mime": "image/png", "name": "shot.png"}
    ctx = VariableContext()
    ctx.set_output("upstream", {"screenshot": ref})
    template = "Review {{upstream.screenshot}} carefully"
    assert ctx.interpolate(template) == "Review  carefully"
    assert ctx.extract_asset_refs(template) == [ref]


def test_executor_forwards_images_and_tool_whitelist() -> None:
    received: list[dict] = []

    async def factory(**kwargs):
        received.append(kwargs)
        return "ok", {"input_tokens": 1, "output_tokens": 1}

    async def capabilities():
        return {
            "models": [{"id": "vision-model", "efforts": ["low"], "vision": True}],
            "current_model": "vision-model",
        }

    ref = AssetRef(asset="sha256:" + "b" * 64, mime="image/png", name="diagram.png")
    graph = PipelineGraph(
        graph_id="g_mm", name="multimodal",
        nodes=[
            GraphNode(id="s", type=NodeType.START),
            GraphNode(
                id="t", type=NodeType.TASK, goal="inspect", auto_split=False,
                attachments=[ref], tools=["browser.*"],
            ),
            GraphNode(id="e", type=NodeType.END),
        ],
        edges=[
            GraphEdge(id="a", from_node="s", to_node="t"),
            GraphEdge(id="b", from_node="t", to_node="e"),
        ],
    )
    executor = PipelineExecutor(
        agent_factory=factory,
        capabilities_provider=capabilities,
        asset_resolver=lambda value: {**image_block(), "asset": value["asset"]},
    )
    record = asyncio.run(executor.execute(graph))
    assert record.status.value == "completed"
    image_call = next(call for call in received if call.get("attachments"))
    assert image_call["attachments"][0]["type"] == "image"
    assert image_call["tools"] == ["browser.*"]


def test_computer_use_defaults_to_confirmation(tmp_path) -> None:
    checker = PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox(str(tmp_path)),
        rule_engine=RuleEngine(),
        mode=PermissionMode.DEFAULT,
    )
    tool = SimpleNamespace(
        name="ComputerControl", category="other", interaction_kind="computer_use"
    )
    assert checker.check(tool, {}).effect == "ask"
