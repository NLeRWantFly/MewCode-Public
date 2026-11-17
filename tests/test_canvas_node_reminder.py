import re

from mewcode.orchestration.revisions import canvas_nodes_reminder


def test_full_listing_has_header_plus_one_line_per_node():
    nodes = [{"id": f"n{i}", "label": f"节点{i}"} for i in range(21)]
    lines = canvas_nodes_reminder(nodes, {}).split("\n")
    assert len(lines) == 22
    assert lines[0].startswith("画布节点清单")
    assert all(re.match(r"^\S+ \| .+ \| 未开始$", line) for line in lines[1:])


def test_last_reported_node_is_marked():
    nodes = [{"id": "a", "label": "甲"}, {"id": "b", "label": "乙"}]
    lines = canvas_nodes_reminder(nodes, {}, last_reported_node="b").split("\n")
    assert lines[1] == "a | 甲 | 未开始"
    assert lines[2] == "b | 乙 | 未开始 ← 上次"


def test_truncation_keeps_hot_and_reported_nodes():
    nodes = [{"id": f"n{i}", "label": f"节点{i}"} for i in range(60)]
    latest = {"n55": "running", "n56": "failed", "n57": "running", "n59": "running"}
    lines = canvas_nodes_reminder(nodes, latest, last_reported_node="n58").split("\n")
    assert len(lines) <= 40
    assert lines[-1].startswith("…另有")
    listed = "\n".join(lines)
    for node_id in ("n55", "n56", "n57", "n58", "n59"):
        assert node_id in listed


def test_status_mapping():
    nodes = [{"id": "a", "label": "甲"}, {"id": "b", "label": "乙"}]
    lines = canvas_nodes_reminder(nodes, {"a": "running", "b": "completed"}).split("\n")
    assert lines[1] == "a | 甲 | 执行中"
    assert lines[2] == "b | 乙 | 已记录"


def test_queued_reminder_flushes_after_tool_results():
    """A canvas created mid-turn must reach the model at the next safe boundary
    without splitting an unanswered tool call from its result."""
    from mewcode.conversation import ConversationManager, ToolResultBlock, ToolUseBlock

    conv = ConversationManager()
    conv.add_user_message("干活")
    conv.add_assistant_message("", tool_uses=[ToolUseBlock(tool_use_id="t1", tool_name="Bash", arguments={})])
    conv.queue_marker_reminder("[MEWCODE_CANVAS_NODES]", "画布节点清单…")
    # Still pending: an unanswered tool call sits at the tail.
    assert not any("MEWCODE_CANVAS_NODES" in str(m.content) for m in conv.history)
    conv.add_tool_results_message([ToolResultBlock(tool_use_id="t1", content="done")])
    tail = conv.history[-1]
    assert "MEWCODE_CANVAS_NODES" in str(tail.content)
    assert tail.transient and tail.context_kind == "[MEWCODE_CANVAS_NODES]"


def test_queued_reminder_flushes_on_next_user_message():
    from mewcode.conversation import ConversationManager

    conv = ConversationManager()
    conv.add_user_message("第一轮")
    conv.queue_marker_reminder("[MEWCODE_CANVAS_NODES]", "画布节点清单…")
    conv.add_user_message("下一轮")
    kinds = [m.content for m in conv.history if m.role == "user"]
    assert "下一轮" in str(kinds[-2]) or "下一轮" in str(kinds[-1])
    assert any("MEWCODE_CANVAS_NODES" in str(m.content) for m in conv.history)


def test_queue_marker_reminder_replaces_same_marker():
    from mewcode.conversation import ConversationManager

    conv = ConversationManager()
    conv.queue_marker_reminder("[M]", "旧")
    conv.queue_marker_reminder("[M]", "新")
    conv.add_user_message("x")
    texts = [str(m.content) for m in conv.history]
    assert sum("[M]" in t for t in texts) == 1
    assert any("新" in t for t in texts)
