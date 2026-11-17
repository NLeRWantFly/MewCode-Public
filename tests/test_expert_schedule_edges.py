from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from mewcode.expert_group import (
    ExpertGroupOrchestrator,
    ExpertProfile,
    ExpertRoleResult,
)
from mewcode.experts.flow import ExpertFlowSpec
from mewcode.model_registry import ModelRef


def _profile() -> ExpertProfile:
    model = ModelRef("openai", "gpt-5.6", "")
    return ExpertProfile(
        manager=model,
        planner=model,
        executor=model,
        auditor=model,
    )


def _batch_index(batches: tuple[tuple[str, ...], ...]) -> dict[str, int]:
    index: dict[str, int] = {}
    for batch_index, batch in enumerate(batches):
        for node_id in batch:
            index[node_id] = batch_index
    return index


def make_linear_flow(node_ids: list[str]) -> ExpertFlowSpec:
    """A -> B -> ... using the same ExpertFlowSpec.from_value shape as test_expert_flow."""
    if len(node_ids) < 2:
        raise ValueError("linear flow needs at least two nodes")
    roles = ["manager", "planner", "executor", "auditor"]
    nodes = []
    for offset, node_id in enumerate(node_ids):
        role = "manager" if offset == 0 or offset == len(node_ids) - 1 else roles[offset % len(roles)]
        nodes.append({"node_id": node_id, "role": role, "label": node_id})
    edges = [
        {"from": node_ids[index], "to": node_ids[index + 1]}
        for index in range(len(node_ids) - 1)
    ]
    return ExpertFlowSpec.from_value({
        "flow_id": "test.schedule.linear",
        "version": 1,
        "label": "Linear",
        "nodes": nodes,
        "edges": edges,
        "finalizer_node_id": node_ids[-1],
    })


def make_two_entry_flow(entries: list[str], finalizer: str) -> ExpertFlowSpec:
    nodes = [
        {"node_id": node_id, "role": "planner", "label": node_id}
        for node_id in entries
    ]
    nodes.append({"node_id": finalizer, "role": "manager", "label": finalizer})
    edges = [{"from": node_id, "to": finalizer} for node_id in entries]
    return ExpertFlowSpec.from_value({
        "flow_id": "test.schedule.two-entry",
        "version": 1,
        "label": "Two Entry",
        "nodes": nodes,
        "edges": edges,
        "finalizer_node_id": finalizer,
    })


def make_join_flow() -> ExpertFlowSpec:
    return ExpertFlowSpec.from_value({
        "flow_id": "test.schedule.join",
        "version": 1,
        "label": "Join",
        "nodes": [
            {"node_id": "A", "role": "planner", "label": "A"},
            {"node_id": "B", "role": "planner", "label": "B"},
            {"node_id": "J", "role": "executor", "label": "Join"},
            {"node_id": "F", "role": "manager", "label": "Final"},
        ],
        "edges": [
            {"from": "A", "to": "J"},
            {"from": "B", "to": "J"},
            {"from": "J", "to": "F"},
        ],
        "finalizer_node_id": "F",
    })


def test_topological_batches_keep_edge_order() -> None:
    flow = make_linear_flow(["A", "B", "C"])
    index = _batch_index(flow.topological_batches())
    assert index["A"] < index["B"] < index["C"]


def test_independent_entries_may_share_batch() -> None:
    """Topology may place sibling entries in one batch; runtime still serializes them."""
    flow = make_two_entry_flow(["A", "B"], finalizer="F")
    batches = flow.topological_batches()
    index = _batch_index(batches)
    assert {"A", "B"} <= set(flow.entry_node_ids())
    assert index["A"] == index["B"]
    assert index["A"] < index["F"]


def test_directed_edge_endpoints_never_share_a_batch() -> None:
    flow = make_join_flow()
    index = _batch_index(flow.topological_batches())
    for edge in flow.edges:
        assert index[edge.from_node] < index[edge.to_node]


@pytest.mark.asyncio
async def test_successor_does_not_start_before_predecessor_finishes(
    tmp_path: Path,
) -> None:
    flow = make_linear_flow(["A", "B", "F"])
    started: dict[str, float] = {}
    finished: dict[str, float] = {}

    async def node_runner(node, _model, _prompt, _policy, member=None):
        started[node.node_id] = time.monotonic()
        await asyncio.sleep(0.02)
        finished[node.node_id] = time.monotonic()
        return ExpertRoleResult(text=f"ok:{node.node_id}")

    async def fallback_runner(*_args):
        raise AssertionError("node_runner should own graph execution")

    orchestrator = ExpertGroupOrchestrator(
        runner=fallback_runner,
        node_runner=node_runner,
        archive_root=tmp_path / "linear-archives",
    )
    await orchestrator.run(
        "linear schedule",
        _profile(),
        {"providers": []},
        strategy="stable",
        flow=flow,
    )
    assert finished["A"] <= started["B"]
    assert finished["B"] <= started["F"]


@pytest.mark.asyncio
async def test_independent_entries_run_serially_in_declaration_order(tmp_path: Path) -> None:
    flow = make_two_entry_flow(["A", "B"], finalizer="F")
    started: dict[str, float] = {}
    finished: dict[str, float] = {}
    active = 0
    max_active = 0

    async def node_runner(node, _model, _prompt, _policy, member=None):
        nonlocal active, max_active
        started[node.node_id] = time.monotonic()
        if node.node_id in {"A", "B"}:
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            active -= 1
        finished[node.node_id] = time.monotonic()
        return ExpertRoleResult(text=f"ok:{node.node_id}")

    async def fallback_runner(*_args):
        raise AssertionError("node_runner should own graph execution")

    orchestrator = ExpertGroupOrchestrator(
        runner=fallback_runner,
        node_runner=node_runner,
        archive_root=tmp_path / "serial-archives",
    )
    await orchestrator.run(
        "serial entries",
        _profile(),
        {"providers": []},
        strategy="stable",
        flow=flow,
    )
    assert max_active == 1
    assert finished["A"] <= started["B"]


@pytest.mark.asyncio
async def test_batch_halts_when_earlier_sibling_needs_attention(tmp_path: Path) -> None:
    """HITL / blocked earlier entry must not let the next declaration sibling start."""
    flow = make_two_entry_flow(["A", "B"], finalizer="F")
    started: list[str] = []

    async def node_runner(node, _model, _prompt, _policy, member=None):
        started.append(node.node_id)
        if node.node_id == "A":
            return ExpertRoleResult(text="waiting on user", status="needs_attention")
        return ExpertRoleResult(text=f"ok:{node.node_id}")

    async def fallback_runner(*_args):
        raise AssertionError("node_runner should own graph execution")

    orchestrator = ExpertGroupOrchestrator(
        runner=fallback_runner,
        node_runner=node_runner,
        archive_root=tmp_path / "halt-archives",
    )
    await orchestrator.run(
        "halt on hitl",
        _profile(),
        {"providers": []},
        strategy="stable",
        flow=flow,
    )
    assert started == ["A"]


@pytest.mark.asyncio
async def test_join_does_not_run_when_a_predecessor_failed(tmp_path: Path) -> None:
    flow = make_join_flow()
    started: list[str] = []

    async def node_runner(node, _model, _prompt, _policy, member=None):
        started.append(node.node_id)
        if node.node_id == "B":
            return ExpertRoleResult(text="B failed", status="failed")
        return ExpertRoleResult(text=f"ok:{node.node_id}")

    async def fallback_runner(*_args):
        raise AssertionError("node_runner should own graph execution")

    orchestrator = ExpertGroupOrchestrator(
        runner=fallback_runner,
        node_runner=node_runner,
        archive_root=tmp_path / "join-archives",
    )
    await orchestrator.run(
        "join schedule",
        _profile(),
        {"providers": []},
        strategy="stable",
        flow=flow,
    )
    assert "A" in started
    assert "B" in started
    assert "J" not in started


@pytest.mark.asyncio
async def test_audit_fail_branch_can_finish_handoff_but_not_pass_acceptance(
    tmp_path: Path,
) -> None:
    flow = ExpertFlowSpec.from_value({
        "flow_id": "test.audit.fail-branch",
        "version": 1,
        "label": "Audit branch",
        "nodes": [
            {"node_id": "write", "role": "executor", "label": "撰写"},
            {"node_id": "audit", "role": "auditor", "label": "审核"},
            {"node_id": "revise", "role": "executor", "label": "修订实验"},
            {"node_id": "pack", "role": "manager", "label": "收束"},
        ],
        "edges": [
            {"from": "write", "to": "audit"},
            {"from": "audit", "to": "pack", "condition": "pass"},
            {"from": "audit", "to": "revise", "condition": "fail"},
            {"from": "revise", "to": "pack"},
        ],
        "finalizer_node_id": "pack",
    })
    started: list[str] = []

    async def node_runner(node, _model, _prompt, _policy, member=None):
        started.append(node.node_id)
        if node.node_id == "audit":
            return ExpertRoleResult(
                text="experiments missing ablation",
                status="completed",
                verdict={
                    "verdict": "NEEDS_ATTENTION",
                    "issues": [{"description": "缺消融"}],
                    "evidence": [],
                },
            )
        return ExpertRoleResult(text=f"ok:{node.node_id}", status="completed")

    async def fallback_runner(*_args):
        raise AssertionError("node_runner should own graph execution")

    orchestrator = ExpertGroupOrchestrator(
        runner=fallback_runner,
        node_runner=node_runner,
        archive_root=tmp_path / "audit-branch-archives",
    )
    result = await orchestrator.run(
        "audit fail branch",
        _profile(),
        {"providers": []},
        strategy="stable",
        flow=flow,
    )
    assert "revise" in started
    assert started.index("audit") < started.index("revise")
    assert result.status == "needs_attention"


@pytest.mark.asyncio
async def test_routed_audit_repair_returns_to_same_audit_before_advancing(
    tmp_path: Path,
) -> None:
    flow = ExpertFlowSpec.from_value({
        "flow_id": "test.audit.finite-recheck",
        "version": 1,
        "label": "Finite audit recheck",
        "nodes": [
            {"node_id": "write", "role": "executor", "label": "撰写"},
            {
                "node_id": "audit",
                "role": "auditor",
                "label": "审核",
                "failure_policy": {
                    "action": "route",
                    "max_attempts": 2,
                    "target_node_id": "revise",
                },
            },
            {"node_id": "revise", "role": "executor", "label": "修复"},
            {"node_id": "pack", "role": "manager", "label": "收束"},
        ],
        "edges": [
            {"from": "write", "to": "audit"},
            {"from": "audit", "to": "pack", "condition": "pass"},
            {"from": "audit", "to": "revise", "condition": "fail"},
            {"from": "revise", "to": "pack"},
        ],
        "finalizer_node_id": "pack",
    })
    started: list[str] = []
    audit_runs = 0

    async def node_runner(node, _model, _prompt, _policy, member=None):
        nonlocal audit_runs
        started.append(node.node_id)
        if node.node_id == "audit":
            audit_runs += 1
            verdict = "PASS" if audit_runs == 2 else "NEEDS_ATTENTION"
            return ExpertRoleResult(
                text=f"audit:{verdict}",
                status="completed",
                verdict={"verdict": verdict, "issues": [], "evidence": []},
            )
        return ExpertRoleResult(text=f"ok:{node.node_id}", status="completed")

    orchestrator = ExpertGroupOrchestrator(
        runner=lambda *_args: None,
        node_runner=node_runner,
        archive_root=tmp_path / "finite-recheck-archives",
    )
    result = await orchestrator.run(
        "repair then recheck",
        _profile(),
        {"providers": []},
        strategy="stable",
        flow=flow,
    )

    assert started == ["write", "audit", "revise", "audit", "pack"]
    assert result.status == "completed"


@pytest.mark.asyncio
async def test_routed_recovery_stops_when_issue_count_does_not_decrease(
    tmp_path: Path,
) -> None:
    flow = ExpertFlowSpec.from_value({
        "flow_id": "test.audit.no-progress",
        "version": 1,
        "label": "No progress recovery",
        "nodes": [
            {"node_id": "write", "role": "executor", "label": "撰写"},
            {
                "node_id": "audit",
                "role": "auditor",
                "label": "审核",
                "failure_policy": {
                    "action": "route",
                    "max_attempts": 3,
                    "target_node_id": "revise",
                },
            },
            {
                "node_id": "revise",
                "role": "executor",
                "label": "修复",
                "failure_policy": {"action": "retry", "max_attempts": 3},
            },
            {"node_id": "pack", "role": "manager", "label": "收束"},
        ],
        "edges": [
            {"from": "write", "to": "audit"},
            {"from": "audit", "to": "pack", "condition": "pass"},
            {"from": "audit", "to": "revise", "condition": "fail"},
            {"from": "revise", "to": "pack"},
        ],
        "finalizer_node_id": "pack",
    })
    started: list[str] = []
    repair_policies = []
    events: list[dict] = []

    async def node_runner(node, _model, _prompt, policy, member=None):
        started.append(node.node_id)
        if node.node_id == "audit":
            return ExpertRoleResult(
                text="same unresolved issue",
                status="completed",
                verdict={
                    "verdict": "NEEDS_ATTENTION",
                    "issues": [{"file": "paper.md", "description": "still broken"}],
                    "evidence": [],
                },
            )
        if node.node_id == "revise":
            repair_policies.append(policy)
        return ExpertRoleResult(text=f"ok:{node.node_id}", status="completed")

    orchestrator = ExpertGroupOrchestrator(
        runner=lambda *_args: None,
        node_runner=node_runner,
        event_sink=lambda event: events.append(event),
        archive_root=tmp_path / "no-progress-archives",
    )
    result = await orchestrator.run(
        "bounded recovery",
        _profile(),
        {"providers": []},
        strategy="stable",
        flow=flow,
    )

    assert started == ["write", "audit", "revise", "audit"]
    assert len(repair_policies) == 1
    assert repair_policies[0].max_iterations == 40
    assert repair_policies[0].timeout_seconds == 3600.0
    assert repair_policies[0].resume_messages is False
    assert result.status != "completed"
    assert any(
        event.get("phase") == "recovery_stalled"
        and event.get("reason") == "unresolved_issues_not_decreasing"
        for event in events
    )
