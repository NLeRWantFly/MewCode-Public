import asyncio
import copy
from dataclasses import replace

import pytest

from mewcode import expert_group
from mewcode.expert_group import ExpertGroupOrchestrator, ExpertRoleResult
from mewcode.experts.flow import ExpertFlowSpec
from mewcode.experts.parallel import allocate_wave_tokens, expansion_wave
from tests.test_node_expansion_runtime import _completed, _expansion, _flow, _profile


def test_frontier_ignores_ready_writer_but_not_dependencies():
    definitions = {"read1": {"tools": ["ReadFile"]}, "read2": {"tools": ["Grep"]},
        "write": {"tools": ["WriteFile"], "write_paths": ["a"]}, "browser": {"tools": ["browser_snapshot"]}}
    assert expansion_wave(["write", "read1", "read2", "browser"], definitions) == ["read1", "read2"]
    assert expansion_wave(["write", "read1"], definitions) == ["write"]
    assert allocate_wave_tokens(9000, [1500, 0]) == [5250, 3750]
    assert sum(allocate_wave_tokens(9001, [1500, 0])) == 9001
    assert allocate_wave_tokens(1000, [1500, 0]) == [0, 0]


@pytest.mark.asyncio
async def test_micrograph_real_fanout_and_reserved_sibling_budget(tmp_path, monkeypatch):
    expansion = _expansion()
    for node in expansion["proposal"]["nodes"]:
        node.update(tools=["ReadFile"], write_paths=[])
    active, peak, scopes, finished = 0, 0, {}, []
    original = expert_group.derive_execution_policy
    monkeypatch.setattr(expert_group, "derive_execution_policy", lambda multi: replace(original(multi), token_budget=10000))
    async def run(node, model, prompt, policy, member=None):
        nonlocal active, peak
        if node.node_id == "implement":
            return ExpertRoleResult(text="plan", status="expanding", billable_tokens=1000, node_expansion=expansion)
        key = node.node_id.rsplit("::", 1)[-1]
        if key in {"alpha", "beta"}:
            assert "prepare" in finished
            scopes[key] = policy.token_budget
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(.02)
            active -= 1
        if key == "done":
            assert {"prepare", "alpha", "beta"} <= set(finished)
        finished.append(key)
        return _completed(key, billable_tokens=200)
    result = await ExpertGroupOrchestrator(runner=lambda *_: None, node_runner=run,
        archive_root=tmp_path / "archives", work_dir=tmp_path).run("test", _profile(), {"providers": []},
            strategy="stable", run_id="8" * 32, flow=_flow())
    assert peak == 2 and result.status == "completed"
    assert scopes == {"alpha": 4400, "beta": 4400}


@pytest.mark.asyncio
async def test_mixed_dag_runs_readers_concurrently_and_writer_serially(tmp_path):
    flow = ExpertFlowSpec.from_value({"flow_id": "mixed", "label": "mixed",
        "nodes": [{"node_id": name, "role": role, "label": name,
                   "tools": ["ReadFile"] if name.startswith("reader") else ["WriteFile"]} for name, role in
            [("writer", "executor"), ("reader1", "planner"), ("reader2", "manager"), ("done", "manager")]],
        "edges": [{"from": name, "to": "done"} for name in ("writer", "reader1", "reader2")],
        "finalizer_node_id": "done", "meta": {"parallel": True}})
    active, peak, writer_active = 0, 0, False
    async def run(node, model, prompt, policy, member=None):
        nonlocal active, peak, writer_active
        if node.node_id.startswith("reader"):
            assert policy.parallel_readonly and not policy.allow_delegation and not writer_active
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(.02)
            active -= 1
        if node.node_id == "writer":
            assert active == 0
            writer_active = True
            await asyncio.sleep(.01)
            writer_active = False
        return _completed(node.node_id)
    result = await ExpertGroupOrchestrator(runner=lambda *_: None, node_runner=run,
        archive_root=tmp_path / "archives", work_dir=tmp_path).run("test", _profile(), {"providers": []},
            strategy="stable", run_id="9" * 32, flow=flow)
    assert peak == 2 and result.status == "completed"


@pytest.mark.asyncio
async def test_micrograph_cancel_drains_every_sibling_and_never_joins(tmp_path):
    expansion = _expansion()
    for node in expansion["proposal"]["nodes"]:
        node.update(tools=["ReadFile"], write_paths=[])
    entered, cleanup, events = set(), set(), []
    barrier = asyncio.Event()
    async def run(node, model, prompt, policy, member=None):
        if node.node_id == "implement":
            return ExpertRoleResult(text="plan", status="expanding", node_expansion=expansion)
        key = node.node_id.rsplit("::", 1)[-1]
        if key in {"alpha", "beta"}:
            entered.add(key)
            if len(entered) == 2:
                barrier.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup.add(key)
        return _completed(key)
    task = asyncio.create_task(ExpertGroupOrchestrator(runner=lambda *_: None, node_runner=run,
        event_sink=lambda event: events.append(copy.deepcopy(event)), archive_root=tmp_path / "archives",
        work_dir=tmp_path).run("test", _profile(), {"providers": []}, strategy="stable", run_id="7" * 32, flow=_flow()))
    await asyncio.wait_for(barrier.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleanup == {"alpha", "beta"}
    assert not any(e["type"] == "expert_expansion_join_completed" for e in events)
