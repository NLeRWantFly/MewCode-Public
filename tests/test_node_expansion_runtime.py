from __future__ import annotations

import asyncio
import copy
import shutil
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from mewcode import expert_group
from mewcode.expert_group import (
    ExpertGroupOrchestrator,
    ExpertProfile,
    ExpertRoleResult,
)
from mewcode.experts.flow import ExpertFlowSpec
from mewcode.experts.stage_context import input_revision
from mewcode.model_registry import ModelRef


@pytest.fixture
def scratch():
    root = Path(".artifacts_scratch") / "node-expansion-runtime" / uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


def _profile() -> ExpertProfile:
    return ExpertProfile(
        manager=ModelRef("openai", "manager", "low"),
        executor=ModelRef("openai", "executor", "medium"),
        auditor=ModelRef("openai", "auditor", "low"),
    )


def _flow() -> ExpertFlowSpec:
    return ExpertFlowSpec.from_value({
        "flow_id": "runtime-expansion",
        "version": 1,
        "label": "Runtime expansion",
        "nodes": [
            {
                "node_id": "implement",
                "role": "executor",
                "label": "Implement",
                "responsibility": "实现父节点合同，只处理已授权工作区。",
            },
            {"node_id": "done", "role": "manager", "label": "Done"},
        ],
        "edges": [{"from": "implement", "to": "done"}],
        "finalizer_node_id": "done",
        "meta": {"shared_constraints": ["不越出工作区"]},
    })


def _expansion() -> dict:
    proposal = {
        "parent_node_id": "implement",
        "expected_flow_version": 1,
        "reason": "先准备，再将两个互不重叠的输出分开处理。",
        "nodes": [
            {
                "node_id": "prepare",
                "label": "Prepare",
                "task": "准备共享输入摘要。",
                "input_refs": ["parent"],
                "outputs": ["prepared"],
                "acceptance": ["准备摘要完整"],
                "tools": ["ReadFile"],
                "write_paths": [],
            },
            {
                "node_id": "alpha",
                "label": "Alpha",
                "task": "交付 alpha 输出。",
                "input_refs": ["parent"],
                "outputs": ["alpha-output"],
                "acceptance": ["alpha 完成"],
                "tools": ["WriteFile"],
                "write_paths": ["alpha.txt"],
            },
            {
                "node_id": "beta",
                "label": "Beta",
                "task": "交付 beta 输出。",
                "input_refs": ["parent"],
                "outputs": ["beta-output"],
                "acceptance": ["beta 完成"],
                "tools": ["WriteFile"],
                "write_paths": ["beta.txt"],
            },
        ],
        "edges": [
            {"from_node": "prepare", "to_node": "alpha", "condition": "always"},
            {"from_node": "prepare", "to_node": "beta", "condition": "always"},
            {"from_node": "alpha", "to_node": "$join", "condition": "always"},
            {"from_node": "beta", "to_node": "$join", "condition": "always"},
        ],
        "join": {"label": "Join", "acceptance": ["三个必需交接均已完成"]},
    }
    return {
        "expansion_id": "mx-runtime0000001",
        "proposal": proposal,
        "normalized_write_paths": {},
        "input_revision": input_revision(
            sources={}, flow=_flow(), node=_flow().node_map["implement"],
            model=_profile().executor,
        ),
        "node_attempt": 1,
    }


def _completed(node_id: str, *, billable_tokens: int = 200) -> ExpertRoleResult:
    return ExpertRoleResult(
        text=f"{node_id} completed",
        status="completed",
        input_tokens=10,
        output_tokens=5,
        billable_tokens=billable_tokens,
        handoff={
            "status": "completed",
            "summary": f"{node_id} handoff",
            "artifacts": [],
            "decisions": [f"{node_id} decision"],
            "open_items": [],
            "evidence": [],
        },
    )


@pytest.mark.asyncio
async def test_sequence_fanout_children_are_bounded_and_host_join_completes(
    scratch, monkeypatch,
):
    events: list[dict] = []
    calls: list[tuple[str, ModelRef, str, object]] = []
    original = expert_group.derive_execution_policy
    monkeypatch.setattr(
        expert_group,
        "derive_execution_policy",
        lambda multi_agent: replace(
            original(multi_agent), token_budget=5000, timeout_seconds=300,
        ),
    )

    async def node_runner(node, model, prompt, policy, member=None):
        calls.append((node.node_id, model, prompt, policy))
        if node.node_id == "implement":
            return ExpertRoleResult(
                text="proposal submitted",
                status="expanding",
                billable_tokens=1000,
                node_expansion=_expansion(),
            )
        if "::mx-runtime0000001::" in node.node_id:
            return _completed(node.node_id)
        return _completed(node.node_id, billable_tokens=0)

    result = await ExpertGroupOrchestrator(
        runner=lambda *_: None,
        node_runner=node_runner,
        event_sink=lambda event: events.append(copy.deepcopy(event)),
        archive_root=scratch / "archives",
        work_dir=scratch,
    ).run(
        "GLOBAL TASK MUST NOT REACH CHILD PROMPTS",
        _profile(),
        {"providers": []},
        strategy="stable",
        run_id="a" * 32,
        flow=_flow(),
    )

    assert result.status == "completed"
    child_calls = [item for item in calls if "::mx-runtime0000001::" in item[0]]
    assert [item[0].rsplit("::", 1)[-1] for item in child_calls] == [
        "prepare", "alpha", "beta",
    ]
    assert all(item[1] == _profile().executor for item in child_calls)
    assert all("GLOBAL TASK MUST NOT REACH CHILD PROMPTS" not in item[2]
               for item in child_calls)
    assert all(item[3].allow_delegation is False for item in child_calls)
    assert all(item[3].authorization_node_id == "implement" for item in child_calls)
    assert all(item[3].expansion_holder is None for item in child_calls)
    assert all("ProposeNodeExpansion" in item[3].deny_tools for item in child_calls)
    assert [item[3].token_budget for item in child_calls] == [4000, 3800, 3600]
    alpha_prompt = next(item[2] for item in child_calls if item[0].endswith("::alpha"))
    beta_prompt = next(item[2] for item in child_calls if item[0].endswith("::beta"))
    assert "prepare handoff" in alpha_prompt and "prepare handoff" in beta_prompt
    assert "beta handoff" not in alpha_prompt and "alpha handoff" not in beta_prompt

    join = next(event for event in events
                if event["type"] == "expert_expansion_join_completed")
    assert join["join_handoff"]["status"] == "completed"
    assert len(join["join_handoff"]["children"]) == 3
    assert sum(event["type"] == "expert_expansion_join_completed" for event in events) == 1
    parent_completed = [event for event in events
                        if event["type"] == "expert_node_completed"
                        and event["node_id"] == "implement"
                        and event["status"] == "completed"]
    assert len(parent_completed) == 1


@pytest.mark.asyncio
async def test_resume_reuses_completed_child_and_keeps_expansion_id(scratch):
    expansion = _expansion()
    first_events: list[dict] = []
    call_counts = {"prepare": 0, "alpha": 0, "beta": 0}

    async def interrupted_runner(node, model, prompt, policy, member=None):
        if node.node_id == "implement":
            return ExpertRoleResult(
                text="proposal submitted", status="expanding",
                node_expansion=copy.deepcopy(expansion),
            )
        local_id = node.node_id.rsplit("::", 1)[-1]
        if local_id in call_counts:
            call_counts[local_id] += 1
            if local_id == "alpha":
                raise asyncio.CancelledError("simulated process stop")
            return _completed(node.node_id)
        return _completed(node.node_id, billable_tokens=0)

    first = ExpertGroupOrchestrator(
        runner=lambda *_: None,
        node_runner=interrupted_runner,
        event_sink=lambda event: first_events.append(copy.deepcopy(event)),
        archive_root=scratch / "first",
        work_dir=scratch,
    )
    with pytest.raises(asyncio.CancelledError):
        await first.run(
            "private global task", _profile(), {"providers": []},
            strategy="stable", run_id="b" * 32, flow=_flow(),
        )

    prepare_id = "implement::mx-runtime0000001::prepare"
    prepare_event = next(
        event for event in first_events
        if event.get("type") == "expert_node_completed"
        and event.get("node_id") == prepare_id
    )
    checkpoint = {
        "locked_flow": _flow().as_dict(),
        "node_states": {
            "implement": {
                "status": "expanding",
                "text": "proposal submitted",
                "input_revision": expansion["input_revision"],
            },
            prepare_id: {
                "status": "completed",
                "text": prepare_event["text"],
                "handoff": prepare_event["handoff"],
                "input_revision": prepare_event["input_revision"],
                "input_tokens": prepare_event["input_tokens"],
                "output_tokens": prepare_event["output_tokens"],
                "billable_tokens": prepare_event["billable_tokens"],
            },
        },
        "node_attempts": {"implement": 1, prepare_id: 1},
        "node_expansions": {
            "implement": {
                **copy.deepcopy(expansion),
                "parent_node_id": "implement",
                "status": "running",
            }
        },
    }
    resume_events: list[dict] = []

    async def resumed_runner(node, model, prompt, policy, member=None):
        assert node.node_id != "implement"
        local_id = node.node_id.rsplit("::", 1)[-1]
        if local_id in call_counts:
            call_counts[local_id] += 1
        return _completed(node.node_id, billable_tokens=0)

    result = await ExpertGroupOrchestrator(
        runner=lambda *_: None,
        node_runner=resumed_runner,
        event_sink=lambda event: resume_events.append(copy.deepcopy(event)),
        archive_root=scratch / "resume",
        work_dir=scratch,
    ).run(
        "private global task", _profile(), {"providers": []},
        strategy="stable", run_id="b" * 32, attempt=2,
        checkpoint=checkpoint, flow=_flow(),
    )

    assert result.status == "completed"
    assert call_counts == {"prepare": 1, "alpha": 2, "beta": 1}
    restored = next(event for event in resume_events
                    if event.get("type") == "expert_expansion_child_completed"
                    and event.get("local_id") == "prepare")
    assert restored["restored"] is True
    activated = next(event for event in resume_events
                     if event.get("type") == "expert_node_expansion_activated")
    assert activated["expansion_id"] == expansion["expansion_id"]
    assert sum(event.get("type") == "expert_expansion_join_completed"
               for event in resume_events) == 1


@pytest.mark.asyncio
async def test_failed_child_blocks_join_parent_and_base_graph_downstream(scratch):
    events: list[dict] = []
    calls: list[str] = []

    async def node_runner(node, model, prompt, policy, member=None):
        calls.append(node.node_id)
        if node.node_id == "implement":
            return ExpertRoleResult(
                text="proposal submitted", status="expanding",
                node_expansion=_expansion(),
            )
        if node.node_id.endswith("::alpha"):
            return ExpertRoleResult(
                text="alpha failed", status="failed", error_kind="tool_failed",
                handoff={"status": "needs_attention", "summary": "alpha failed",
                         "artifacts": [], "decisions": [], "open_items": ["retry"],
                         "evidence": []},
            )
        return _completed(node.node_id)

    result = await ExpertGroupOrchestrator(
        runner=lambda *_: None,
        node_runner=node_runner,
        event_sink=lambda event: events.append(copy.deepcopy(event)),
        archive_root=scratch / "failed",
        work_dir=scratch,
    ).run(
        "global task", _profile(), {"providers": []},
        strategy="stable", run_id="c" * 32, flow=_flow(),
    )

    assert result.status == "blocked"
    assert "done" not in calls
    assert not any(node_id.endswith("::beta") for node_id in calls)
    assert any(event["type"] == "expert_expansion_join_blocked" for event in events)
    assert not any(event["type"] == "expert_expansion_join_completed" for event in events)
    parent = [event for event in events
              if event.get("type") == "expert_node_completed"
              and event.get("node_id") == "implement"][-1]
    assert parent["status"] == "blocked"


@pytest.mark.asyncio
async def test_resume_after_durable_join_does_not_join_or_charge_children_again(scratch):
    expansion = _expansion()
    join_handoff = {
        "status": "completed",
        "summary": "durable deterministic join",
        "artifacts": [],
        "decisions": [],
        "open_items": [],
        "evidence": [],
        "children": [],
        "join_acceptance": ["三个必需交接均已完成"],
        "expansion_id": expansion["expansion_id"],
    }
    checkpoint = {
        "locked_flow": _flow().as_dict(),
        "node_states": {"implement": {"status": "expanding", "text": "proposal submitted"}},
        "node_attempts": {"implement": 1},
        "node_expansions": {
            "implement": {
                **copy.deepcopy(expansion),
                "parent_node_id": "implement",
                "status": "joining",
                "join": {"status": "completed"},
                "join_handoff": copy.deepcopy(join_handoff),
            }
        },
    }
    calls: list[str] = []
    events: list[dict] = []

    async def node_runner(node, model, prompt, policy, member=None):
        calls.append(node.node_id)
        assert node.node_id == "done"
        return _completed(node.node_id, billable_tokens=0)

    result = await ExpertGroupOrchestrator(
        runner=lambda *_: None,
        node_runner=node_runner,
        event_sink=lambda event: events.append(copy.deepcopy(event)),
        archive_root=scratch / "joined-resume",
        work_dir=scratch,
    ).run(
        "private global task", _profile(), {"providers": []},
        strategy="stable", run_id="d" * 32, attempt=2,
        checkpoint=checkpoint, flow=_flow(),
    )

    assert result.status == "completed" and calls == ["done"]
    assert not any(event.get("type") == "expert_expansion_join_completed"
                   for event in events)
    restored = next(event for event in events
                    if event.get("type") == "expert_node_expansion_completed")
    assert restored["restored"] is True


@pytest.mark.asyncio
async def test_stale_parent_input_invalidates_saved_proposal_before_activation(scratch):
    expansion = _expansion()
    expansion["input_revision"] = "stale-parent-revision"
    checkpoint = {
        "locked_flow": _flow().as_dict(),
        "node_states": {"implement": {"status": "expanding", "text": "old proposal"}},
        "node_attempts": {"implement": 1},
        "node_expansions": {
            "implement": {**copy.deepcopy(expansion), "status": "accepted"}
        },
    }
    calls: list[str] = []
    events: list[dict] = []

    async def node_runner(node, model, prompt, policy, member=None):
        calls.append(node.node_id)
        return _completed(node.node_id, billable_tokens=0)

    result = await ExpertGroupOrchestrator(
        runner=lambda *_: None,
        node_runner=node_runner,
        event_sink=lambda event: events.append(copy.deepcopy(event)),
        archive_root=scratch / "stale",
        work_dir=scratch,
    ).run(
        "private global task", _profile(), {"providers": []},
        strategy="stable", run_id="e" * 32, attempt=2,
        checkpoint=checkpoint, flow=_flow(),
    )

    assert result.status == "completed"
    assert calls == ["implement", "done"]
    invalidated = next(event for event in events
                       if event.get("type") == "expert_node_expansion_invalidated")
    assert invalidated["reason"] == "input_revision_changed"
    assert not any(event.get("type") == "expert_node_expansion_activated"
                   for event in events)
