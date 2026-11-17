"""CollaborationRuntime.execute 的上下文组装与派发顺序行为测试。

沿用 tests/test_collaboration_mode.py 的真实夹具（store_for/flow/delivery）直接驱动
真实的 CollaborationRuntime.execute：invoke 是允许的模型替身，review 返回通过的验收证据。
不跑完整 run，也不 mock ledger/store/验收。本文件由主 Agent 执行。
"""
from __future__ import annotations

import asyncio
import json

from mewcode.expert_group import ExpertExecutionPolicy, ExpertRoleResult
from mewcode.kernel import collaboration as ledger
from mewcode.orchestration.collab_runtime import CollaborationRuntime
from tests.test_collaboration_mode import CATALOG, EXECUTOR, RUN_ID, delivery, flow, store_for

PROMPT = "调用者提示：请完成候选交付。"
MARKER = "\nTASK_CONTEXT_JSON\n"
BIG = "大正文-" + "x" * 3000
BUDGET = 16000
TIGHT_BUDGET = 24
CONTRACT_ID = "work"


def make_policy(**overrides):
    values = {"strategy": "stable", "allow_delegation": False, "max_direct_agents": 0,
              "max_iterations": 24, "stage_inputs": {}}
    values.update(overrides)
    return ExpertExecutionPolicy(**values)


async def passing_review(reviewer, payload, context):
    check = (payload.get("check") or {}).get("id", "check")
    return {"passed": True, "evidence": [{"check": check, "host": "observed"}], "artifacts": []}


def _runtime(tmp_path, budget=BUDGET):
    runtime = CollaborationRuntime(store=store_for(tmp_path), run_id=RUN_ID, flow=flow(),
                                   catalog=CATALOG, work_dir=tmp_path, review=passing_review)
    runtime.mutate(lambda current: current["policy"].__setitem__("context_char_budget", budget))
    return runtime


def _node(node_id=CONTRACT_ID):
    return flow().node_map[node_id]


def _split(instructions):
    """返回（投影, 紧随投影之后的恢复文本）。"""
    body = instructions.split(MARKER, 1)[1]
    projection, end = json.JSONDecoder().raw_decode(body)
    return projection, body[end:]


def _dicts(obj):
    found, stack = [], [obj]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            found.append(current)
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return found


def _work_attempt(runtime):
    attempts = [row for row in runtime.read()["attempts"].values() if row["contract_id"] == CONTRACT_ID]
    assert len(attempts) == 1
    return attempts[0]


def _assert_budget_settled(runtime):
    """真正调用模型之前：attempt 仍有效、预留已结算、根预算只记这一次 attempt。"""
    record = runtime.store.load(runtime.run_id)
    attempt = _work_attempt(runtime)
    ledger.check_attempt(runtime.read(), attempt["id"])
    budget = ledger.state(record)["lineage_budget"]
    reservation = budget["reservations"][attempt["budget_reservation_id"]]
    assert reservation["status"] == "settled"
    assert budget["root"]["used"]["attempts"] == 1
    return attempt["id"]


def test_projection_carries_current_contract_and_input_index(tmp_path):
    runtime = _runtime(tmp_path)
    version = runtime.read()["contracts"][CONTRACT_ID]["version"]
    spec = runtime.read()["spec_version"]
    stage_inputs = {"stage_in_1": {"node_id": "stage_in_1", "status": "completed",
                                   "quote": "候选已交付给下游",
                                   "handoff": {"status": "completed",
                                               "inline_outputs": [{"name": "候选", "content": BIG}]}}}
    calls = []

    async def invoke(node, model, instructions, policy, member):
        calls.append(instructions)
        assert policy.stage_inputs == stage_inputs
        assert policy.input_revision == ledger.dependencies(runtime.read(), CONTRACT_ID)
        assert len(instructions) <= BUDGET
        return delivery()

    result = asyncio.run(runtime.execute(_node(), EXECUTOR, PROMPT,
                                         make_policy(stage_inputs=stage_inputs), None, invoke))
    assert result.status == "completed"
    assert len(calls) == 1
    instructions = calls[0]
    assert instructions.startswith(PROMPT)
    assert MARKER in instructions
    projection, recovery = _split(instructions)
    assert recovery == ""
    assert "交付测试结果" in instructions
    rows = _dicts(projection)
    assert any(row.get("contract_id") == CONTRACT_ID and row.get("role") == "executor" for row in rows)
    assert any(row.get("contract_id") == CONTRACT_ID and row.get("contract_version") == version
               and row.get("spec_version") == spec for row in rows)
    # 输入只作为投影摘要/索引出现；大正文留在 policy.stage_inputs，不整段内联。
    assert "stage_in_1" in instructions
    assert BIG not in instructions


def test_attempt_is_committed_and_settled_before_provider_call(tmp_path):
    runtime = _runtime(tmp_path)
    seen = []

    async def invoke(node, model, instructions, policy, member):
        seen.append(_assert_budget_settled(runtime))
        return delivery()

    result = asyncio.run(runtime.execute(_node(), EXECUTOR, PROMPT, make_policy(), None, invoke))
    assert result.status == "completed"
    assert len(seen) == 1


def test_oversized_budget_blocks_without_provider_call(tmp_path):
    runtime = _runtime(tmp_path, budget=TIGHT_BUDGET)
    calls = []

    async def invoke(node, model, instructions, policy, member):
        calls.append(instructions)
        return delivery()

    result = asyncio.run(runtime.execute(_node(), EXECUTOR, PROMPT, make_policy(), None, invoke))
    assert calls == []
    assert result.status == "blocked"
    assert result.error_kind == "collaboration_checkpoint"
    assert "预算" in result.text
    assert runtime.read()["certificates"] == {}
    assert all(row["status"] != "accepted" for row in runtime.read()["attempts"].values())


def test_missing_or_invalid_budget_blocks_without_provider_call(tmp_path):
    for index, bad in enumerate((None, 0, True, "16000")):
        runtime = _runtime(tmp_path / f"budget-{index}", budget=bad)
        calls = []

        async def invoke(node, model, instructions, policy, member):
            calls.append(instructions)
            return delivery()

        result = asyncio.run(runtime.execute(_node(), EXECUTOR, PROMPT, make_policy(), None, invoke))
        assert calls == [], f"非法预算 {bad!r} 不应调用模型"
        assert result.status == "blocked"
        assert result.error_kind == "collaboration_checkpoint"


def test_provider_retry_keeps_single_attempt_and_latest_recovery(tmp_path):
    runtime = _runtime(tmp_path)
    calls, used, attempt_ids = [], [], []

    async def invoke(node, model, instructions, policy, member):
        calls.append(instructions)
        attempt_ids.append(_assert_budget_settled(runtime))
        used.append(runtime.read()["lineage_budget"]["root"]["used"]["attempts"])
        if len(calls) <= 2:
            return ExpertRoleResult(text="瞬时故障", status="failed", error_kind="provider_error")
        return delivery()

    decisions = [{"action": "continue_checkpoint", "chosen": {"next_step": "恢复甲", "reason": "网络抖动"}},
                 {"action": "diagnose_path", "chosen": {"next_step": "恢复乙", "reason": "诊断后重试"}}]

    async def fake_recover(node, result, policy, *, identity):
        return decisions.pop(0)

    runtime.recover_fault = fake_recover
    result = asyncio.run(runtime.execute(_node(), EXECUTOR, PROMPT, make_policy(), None, invoke))
    assert result.status == "completed"
    assert len(calls) == 3
    assert all(call.startswith(PROMPT) and MARKER in call for call in calls)
    _, tail0 = _split(calls[0])
    _, tail1 = _split(calls[1])
    _, tail2 = _split(calls[2])
    assert tail0 == ""
    # 每轮用原提示 + 最新恢复 + 新投影重建：旧恢复既不缺席也不累积。
    assert tail1 and tail2
    assert tail1 != tail2
    assert tail1 not in tail2
    # 三次 provider 调用复用同一个 attempt，预算 counts 不累积。
    assert len(set(attempt_ids)) == 1
    assert used == [1, 1, 1]
