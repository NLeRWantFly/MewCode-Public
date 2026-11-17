"""行为测试：run() 的提示词组装、质量重试与上游来源版本冻结。

只替身 node_runner 与两个模型边界（协作验收、协调者），其余走真实
ExpertGroupOrchestrator / ExpertRunStore / ledger 验收链路。
"""
from __future__ import annotations

import copy
import json

import pytest

from mewcode.expert_group import ExpertGroupOrchestrator, ExpertRoleResult, REPORT_STYLE_CONTRACT
from mewcode.expert_runs import ExpertRunStore
from mewcode.experts.authorization_ledger import authorization_block
from mewcode.experts.flow import ExpertFlowSpec
from mewcode.kernel import collaboration as ledger
from mewcode.tools.read_stage_result import ReadStageResult, ReadStageResultParams
from tests.test_collaboration_mode import (CATALOG, EXECUTOR, PROFILE, REVIEWER, RUN_ID, delivery,
    flow as fixture_flow, store_for, require_reviews)

CONTRACT_HEADER = "【协作工作合同】"
BIG_BODY = "上游正文段落" * 5000


def retry_flow(attempts=6):
    """与 fixtures 同构的协作流程，只把失败重试上限提高到足以观察质量重试。"""
    meta = {"orchestration_mode": "collaborative", "node_plan_briefs": {
        "work": {"known_inputs": ["目标"], "outputs": ["候选"], "acceptance": ["候选符合原始目标"]},
        "deliver": {"known_inputs": ["候选"], "outputs": ["结果"], "acceptance": ["全量结果符合原始目标"]}}}
    meta["collaboration_spec"] = {"format": ledger.FORMAT, "task_id": "task-one", "goal": "交付测试结果",
        "document": {"entries": [{"id": "goal", "kind": "goal", "title": "目标", "body": "交付测试结果",
                                   "state": "active", "data": {}, "sources": [{"source_id": "user_1", "quote": "交付测试结果"}]}]}}
    return ExpertFlowSpec.from_value({"flow_id": "sample", "version": 1, "label": "样例", "meta": meta,
        "nodes": [{"node_id": key, "role": "executor", "label": key, "responsibility": "交付测试结果",
                   "failure_policy": {"action": "stop", "max_attempts": attempts}} for key in ("work", "deliver")],
        "edges": [{"from_node": "work", "to_node": "deliver"}], "finalizer": "deliver"})


def retry_store(tmp_path, flow_value):
    store = ExpertRunStore(tmp_path / "runs", storage='sqlite')
    store.create(run_id=RUN_ID, conversation_id="conversation", task="交付测试结果", attachments=[],
                 profile=PROFILE.as_dict(), strategy="stable", flow=flow_value)
    store.mutate(RUN_ID, lambda record: record.update(role_models={"roles": {"verifier": REVIEWER.as_dict(),
        "reviewer": REVIEWER.as_dict(), "executor": EXECUTOR.as_dict(), "planner": EXECUTOR.as_dict()}}), durable=True)
    return store


def failing_work_reviewer(failures=2):
    """按 attempt 计数失败：前 failures 次 work 交接被拒，之后通过。"""
    seen = []
    async def reviewer(model, payload, context):
        if context["node_id"] != "work":
            return {"passed": True, "evidence": ["非 work 阶段验收通过"]}
        attempt = context["attempt_id"]
        if attempt not in seen:
            seen.append(attempt)
        index = seen.index(attempt)
        if index < failures:
            mark = "MARK-ONE" if index == 0 else "MARK-TWO"
            return {"passed": False, "evidence": [mark + " 未覆盖目标"], "reason": mark + " 缺少目标证据"}
        return {"passed": True, "evidence": ["work 覆盖目标"]}
    return reviewer


def orchestrate(store, tmp_path, runner, reviewer, coordinate=None):
    return ExpertGroupOrchestrator(runner=None, node_runner=runner, run_store=store,
        work_dir=tmp_path, archive_root=tmp_path / "archive", collaboration_review=reviewer,
        collaboration_coordinate=coordinate, event_sink=lambda event: store.append_event(RUN_ID, event))


async def run_flow(handle, flow_value, store):
    return await handle.run("交付测试结果", PROFILE, CATALOG, strategy="stable", run_id=RUN_ID,
                            flow=flow_value, checkpoint=store.load(RUN_ID))


def base_of(prompt):
    assert CONTRACT_HEADER in prompt, "execute 必须先拼固定合同说明"
    return prompt.split(CONTRACT_HEADER)[0].removesuffix("\n\n")


@pytest.mark.asyncio
async def test_unchanged_required_failure_stops_without_repeated_planning_or_user_question(tmp_path):
    flow_value = retry_flow()
    store = retry_store(tmp_path, flow_value)
    require_reviews(store, 'work')
    dispatched, diagnoses = [], []

    async def runner(node, model, prompt, policy, member=None):
        dispatched.append(node.node_id)
        return delivery("同一个未修复的交付")

    async def reviewer(model, payload, context):
        return {"passed": False, "evidence": ["交付仍缺少指定结果"], "reason": "同一缺项"}

    async def coordinate(model, payload, system):
        diagnoses.append(payload)
        return {"action": "retry", "reason": "补齐缺项", "instructions": "核对指定结果"}

    result = await run_flow(orchestrate(store, tmp_path, runner, reviewer, coordinate), flow_value, store)
    assert result.status != "completed"
    assert dispatched == ["work", "work"]
    assert diagnoses == []
    value = store.load(RUN_ID)["collaboration"]
    assert not value.get('recovery_gates')
    assert len(value["attempts"]) == 2
    assert "work" not in value["certificates"]


async def read_all(tool, node_id):
    offset, chunks = 0, []
    for _ in range(64):
        result = await tool.execute(ReadStageResultParams(node_id=node_id, offset=offset, limit=24000))
        assert not result.is_error, result.output
        lines = result.output.split("\n", 5)
        total, next_offset = int(lines[2].split(": ", 1)[1]), int(lines[4].split(": ", 1)[1])
        chunks.append(lines[5])
        offset = next_offset
        if offset >= total:
            return "".join(chunks)
    raise AssertionError("来源分页未在有限轮次内结束")


@pytest.mark.asyncio
async def test_required_retry_carries_current_failure_without_extra_coordinator_instruction(tmp_path):
    flow_value = retry_flow()
    store = retry_store(tmp_path, flow_value)
    require_reviews(store, 'work')
    work_prompts, deliver_prompts = [], []
    calls = []

    async def coordinate(model, payload, system):
        calls.append(copy.deepcopy(payload))
        instruction = ("指导一", "指导二")[len(calls) - 1]
        return {"action": "retry", "instructions": instruction, "reason": "依据本次失败检查修正"}

    async def runner(node, model, prompt, policy, member=None):
        if node.node_id == "work":
            mark = ("MARK-ONE", "MARK-TWO", "MARK-THREE")[min(len(work_prompts), 2)]
            work_prompts.append(prompt)
            return ExpertRoleResult(text=mark + " 初稿", handoff={"status": "completed",
                "inline_outputs": [{"name": "候选", "content": mark + " 初稿"}]})
        deliver_prompts.append(prompt)
        return delivery("最终结果")

    result = await run_flow(orchestrate(store, tmp_path, runner, failing_work_reviewer(failures=2), coordinate),
                            flow_value, store)
    assert result.status == "completed", result.final_summary
    assert len(work_prompts) == 3 and len(deliver_prompts) == 1

    bases = [base_of(prompt) for prompt in work_prompts]
    authorization = authorization_block(store.load(RUN_ID), "work")
    for base in bases:
        assert REPORT_STYLE_CONTRACT in base
        assert authorization in base
    assert bases[1] == bases[2] and '具体问题见 prior_failed_checks' in bases[1]
    assert 'MARK-TWO' in work_prompts[2] and '指导一' not in bases[2]

    value = store.load(RUN_ID)["collaboration"]
    attempts = [row for row in value["attempts"].values() if row["contract_id"] == "work"]
    failed = [row for row in attempts if row.get("status") in {"rejected", "superseded_returned"}]
    assert len(attempts) == 3
    assert len(failed) == 2
    assert all(row.get("report") or row.get("delivery") for row in failed)
    assert calls == []


@pytest.mark.asyncio
async def test_downstream_keeps_full_source_in_stage_inputs_not_prompt(tmp_path):
    store = store_for(tmp_path)
    captured = {}

    async def runner(node, model, prompt, policy, member=None):
        if node.node_id == "work":
            return ExpertRoleResult(text=BIG_BODY, handoff={"status": "completed",
                "inline_outputs": [{"name": "候选", "content": BIG_BODY}]})
        captured["prompt"], captured["policy"] = prompt, policy
        return delivery("最终结果")

    async def reviewer(model, payload, context):
        return {"passed": True, "evidence": ["覆盖目标"]}

    result = await run_flow(orchestrate(store, tmp_path, runner, reviewer), fixture_flow(), store)
    assert result.status == "completed", result.final_summary
    prompt, sources = captured["prompt"], captured["policy"].stage_inputs
    assert BIG_BODY not in prompt
    assert sources["work"]["status"] == "completed"
    assert BIG_BODY in sources["work"]["text"]
    revision = sources["work"]["source_revision"]
    assert revision in prompt
    raw = {key: {name: item for name, item in value.items() if name != "source_revision"}
           for key, value in sources.items()}
    assert revision == ReadStageResult(copy.deepcopy(raw)).revisions["work"]

    tool = ReadStageResult(sources)
    index = await tool.execute(ReadStageResultParams(node_id=""))
    assert revision in index.output
    body = await read_all(tool, "work")
    assert body == tool.sources["work"]
    assert BIG_BODY in body


@pytest.mark.asyncio
async def test_retry_without_coordinator_keeps_original_scope(tmp_path):
    flow_value = retry_flow()
    store = retry_store(tmp_path, flow_value)
    require_reviews(store, 'work')
    prompts = []

    async def runner(node, model, prompt, policy, member=None):
        if node.node_id == "work":
            mark = "MARK-ONE" if not prompts else "MARK-TWO"
            prompts.append(prompt)
            return ExpertRoleResult(text=mark + " 初稿", handoff={"status": "completed",
                "inline_outputs": [{"name": "候选", "content": mark + " 初稿"}]})
        return delivery("最终结果")

    result = await run_flow(orchestrate(store, tmp_path, runner, failing_work_reviewer(failures=1)), flow_value, store)
    assert result.status == "completed", result.final_summary
    assert len(prompts) == 2
    bases = [base_of(prompt) for prompt in prompts]
    assert REPORT_STYLE_CONTRACT in bases[1] and '交接条件' in bases[1]
    assert authorization_block(store.load(RUN_ID), 'work') in bases[1]
    assert "MARK-ONE" not in bases[1]
    assert authorization_block(store.load(RUN_ID), "work") in bases[0]
