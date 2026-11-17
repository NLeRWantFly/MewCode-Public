from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from mewcode.experts.node_expansion import (
    ExpansionPathSandbox,
    JOIN_NODE_ID,
    NodeExpansionContext,
    NodeExpansionHolder,
    NodeExpansionValidationError,
)
from mewcode.permissions.sandbox import PathSandbox
from mewcode.tools.propose_node_expansion import (
    ProposeNodeExpansion,
    ProposeNodeExpansionParams,
)


def _context(tmp_path, **overrides):
    (tmp_path / "input.txt").write_text("data", encoding="utf-8")
    values = {
        "parent_node_id": "impl-1",
        "flow_version": 3,
        "input_revision": "rev-1",
        "node_attempt": 1,
        "work_dir": tmp_path,
        "allowed_tools": frozenset({"ReadFile", "WriteFile", "EditFile", "Bash"}),
        "available_input_refs": frozenset({"upstream"}),
        "sandbox": PathSandbox(str(tmp_path)),
    }
    values.update(overrides)
    return NodeExpansionContext(**values)


def _holder(tmp_path, **overrides):
    return NodeExpansionHolder(context=_context(tmp_path, **overrides))


def _node(node_id, *, tools=None, write_paths=None, input_refs=None):
    return {
        "node_id": node_id,
        "label": node_id,
        "task": f"处理 {node_id}",
        "input_refs": input_refs if input_refs is not None else ["parent"],
        "outputs": [f"{node_id}-out"],
        "acceptance": [f"{node_id} 完成"],
        "tools": tools or [],
        "write_paths": write_paths if write_paths is not None else [f"{node_id}.txt"],
    }


def _edge(from_node, to_node):
    return {"from_node": from_node, "to_node": to_node, "condition": "always"}


def _proposal(nodes, edges):
    return {
        "parent_node_id": "impl-1",
        "expected_flow_version": 3,
        "reason": "任务边界清晰，按独立写入拆分",
        "nodes": nodes,
        "edges": edges,
        "join": {"label": "汇合", "acceptance": ["全部子输出齐备"]},
    }


def _sequence_proposal():
    return _proposal(
        [
            _node("prepare", write_paths=["shared.txt"]),
            _node("write", input_refs=["upstream", "input.txt"], write_paths=["shared.txt"]),
        ],
        [_edge("prepare", "write"), _edge("write", JOIN_NODE_ID)],
    )


def _fanout_proposal():
    names = ("alpha", "beta", "gamma")
    return _proposal(
        [_node(name) for name in names],
        [_edge(name, JOIN_NODE_ID) for name in names],
    )


def _reject(holder, raw):
    with pytest.raises(NodeExpansionValidationError):
        holder.submit(raw)
    assert holder.rejection is not None
    assert holder.value is None
    return holder.rejection


def test_valid_sequence_and_fanout_have_stable_ids(tmp_path):
    holder = _holder(tmp_path)
    seq = holder.submit(_sequence_proposal())
    assert seq.expansion_id.startswith("mx-") and len(seq.expansion_id) == 19
    assert seq.normalized_write_paths["prepare"] == seq.normalized_write_paths["write"]
    assert holder.value == seq.as_dict() and holder.rejection is None
    assert holder.read() == holder.value

    fanout = holder.submit(_fanout_proposal())
    assert fanout.expansion_id.startswith("mx-")
    again = holder.submit(_fanout_proposal())
    assert again.expansion_id == fanout.expansion_id

    other = _holder(tmp_path, input_revision="rev-2")
    assert other.submit(_fanout_proposal()).expansion_id != fanout.expansion_id


def test_rejects_bad_count_cycles_unknown_refs_and_extra_fields(tmp_path):
    holder = _holder(tmp_path)
    single = _proposal([_node("solo")], [_edge("solo", JOIN_NODE_ID)])
    assert _reject(holder, single)["field"].startswith("nodes")

    many = _proposal(
        [_node(f"n{i}") for i in range(7)],
        [_edge(f"n{i}", JOIN_NODE_ID) for i in range(7)],
    )
    assert _reject(holder, many)["field"].startswith("nodes")

    cycle = _proposal(
        [_node("a"), _node("b")],
        [_edge("a", "b"), _edge("b", "a"), _edge("a", JOIN_NODE_ID)],
    )
    assert _reject(holder, cycle)["field"] == "edges"

    unknown = _proposal(
        [_node("a"), _node("b")],
        [_edge("a", "b"), _edge("b", JOIN_NODE_ID), _edge("ghost", JOIN_NODE_ID)],
    )
    assert _reject(holder, unknown)["field"] == "edges"

    self_edge = _proposal(
        [_node("a"), _node("b")],
        [_edge("a", "b"), _edge("b", JOIN_NODE_ID), _edge("a", "a")],
    )
    assert _reject(holder, self_edge)["field"] == "edges"

    unreachable = _proposal(
        [_node("a"), _node("b"), _node("c")],
        [_edge("a", JOIN_NODE_ID), _edge("b", JOIN_NODE_ID)],
    )
    rejection = _reject(holder, unreachable)
    assert rejection["field"] == "edges" and "汇合" in rejection["reason"]

    with_model = _sequence_proposal()
    with_model["nodes"][0]["model"] = "other-model"
    assert "model" in _reject(holder, with_model)["field"]

    with_budget = _sequence_proposal()
    with_budget["nodes"][1]["token_budget"] = 1000
    assert "token_budget" in _reject(holder, with_budget)["field"]

    stale = _sequence_proposal()
    stale["expected_flow_version"] = 2
    assert _reject(holder, stale)["field"] == "expected_flow_version"


def test_rejects_tool_or_scope_expansion(tmp_path):
    holder = _holder(tmp_path)
    for bad_tool in ("Agent", "TeamCreate", "ProposeNodeExpansion"):
        raw = _sequence_proposal()
        raw["nodes"][0]["tools"] = [bad_tool]
        assert _reject(holder, raw)["field"].endswith("tools")

    raw = _sequence_proposal()
    raw["nodes"][0]["tools"] = ["WebSearch"]
    assert _reject(holder, raw)["field"].endswith("tools")

    raw = _sequence_proposal()
    raw["nodes"][0]["write_paths"] = ["   "]
    assert _reject(holder, raw)["field"] == "nodes.0.write_paths.0"

    raw = _sequence_proposal()
    raw["nodes"][0]["tools"] = ["   "]
    assert _reject(holder, raw)["field"] == "nodes.0.tools.0"

    raw = _sequence_proposal()
    raw["nodes"][0]["outputs"] = ["   "]
    assert _reject(holder, raw)["field"] == "nodes.0.outputs.0"

    # PathSandbox always allows the tempdir, so "../outside.txt" from tmp_path
    # would stay in-bounds; the drive anchor is guaranteed outside every root.
    outside = str(Path(tmp_path.anchor) / "mewcode_outside_scope.txt")
    raw = _sequence_proposal()
    raw["nodes"][0]["write_paths"] = [outside]
    assert _reject(holder, raw)["field"] == "nodes.0.write_paths.0"

    raw = _sequence_proposal()
    raw["nodes"][0]["input_refs"] = [outside]
    # Explicit ordinary external reads are part of the current parent policy;
    # declaring that input does not add writes or broaden the parent's policy.
    assert holder.submit(raw).proposal.nodes[0].input_refs == [outside]

    class ReadRestrictedParent:
        def check(self, path, *, read_only=False):
            if read_only and path == outside:
                return False, "parent input restriction"
            return holder.context.sandbox.check(path, read_only=read_only)

    bounded = _holder(tmp_path, sandbox=ReadRestrictedParent())
    assert _reject(bounded, raw)["field"] == "nodes.0.input_refs.0"

    holder.set_allowed_tools({"ReadFile", "WriteFile", "EditFile", "Bash", "WebSearch"})
    ok = holder.submit(_sequence_proposal())
    assert ok.expansion_id.startswith("mx-")


def test_parallel_write_overlap_rejected_but_ordered_overlap_allowed(tmp_path):
    holder = _holder(tmp_path)
    parallel = _proposal(
        [
            _node("a", write_paths=["out"]),
            _node("b", write_paths=["out/a.txt"]),
        ],
        [_edge("a", JOIN_NODE_ID), _edge("b", JOIN_NODE_ID)],
    )
    rejection = _reject(holder, parallel)
    assert "重叠" in rejection["reason"] or "overlap" in rejection["reason"]

    same_target = _proposal(
        [
            _node("a", write_paths=["out/result.txt"]),
            _node("b", write_paths=["out/result.txt"]),
        ],
        [_edge("a", JOIN_NODE_ID), _edge("b", JOIN_NODE_ID)],
    )
    assert _reject(holder, same_target)["field"] == "nodes.write_paths"

    if os.name == "nt":
        case_variant = _proposal(
            [
                _node("a", write_paths=["out/result.txt"]),
                _node("b", write_paths=["OUT/RESULT.TXT"]),
            ],
            [_edge("a", JOIN_NODE_ID), _edge("b", JOIN_NODE_ID)],
        )
        assert _reject(holder, case_variant)["field"] == "nodes.write_paths"

    ordered = _proposal(
        [
            _node("a", write_paths=["out"]),
            _node("b", write_paths=["out/a.txt"]),
        ],
        [_edge("a", "b"), _edge("b", JOIN_NODE_ID)],
    )
    validated = holder.submit(ordered)
    assert validated.expansion_id.startswith("mx-")


def test_runtime_child_sandbox_restricts_writes_but_keeps_parent_reads(tmp_path):
    allowed = tmp_path / "out"
    sandbox = ExpansionPathSandbox(
        PathSandbox(str(tmp_path)),
        (str(allowed.resolve()),),
    )
    assert sandbox.check("input.txt", read_only=True)[0]
    assert sandbox.check("out/result.txt", read_only=False)[0]
    ok, reason = sandbox.check("other.txt", read_only=False)
    assert not ok and "声明的写入范围" in reason


@pytest.mark.asyncio
async def test_tool_records_rejection_for_runtime_pause(tmp_path):
    holder = _holder(tmp_path)
    tool = ProposeNodeExpansion(holder)

    bad = copy.deepcopy(_sequence_proposal())
    bad["parent_node_id"] = "other-node"
    result = await tool.execute(ProposeNodeExpansionParams(proposal=bad))
    assert result.is_error
    payload = json.loads(result.output)
    assert payload["status"] == "rejected"
    assert holder.rejection is not None
    assert payload["field"] == holder.rejection["field"]
    assert payload["reason"] == holder.rejection["reason"]
    assert payload["field"] == "parent_node_id"
    assert "暂停" in payload["instruction"]

    result = await tool.execute(
        ProposeNodeExpansionParams(proposal=_sequence_proposal())
    )
    assert not result.is_error
    payload = json.loads(result.output)
    assert payload["status"] == "accepted"
    assert holder.value is not None
    assert payload["expansion_id"] == holder.value["expansion_id"]
    assert holder.rejection is None
    assert "等待宿主执行微图" in payload["instruction"]
