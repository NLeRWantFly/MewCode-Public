import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mewcode.expert_runs import ExpertRunStore
from mewcode.expert_group import ExpertExecutionPolicy
from mewcode.experts.flow import ExpertFlowSpec
from mewcode.experts.gate_dialogue import await_dialogue, confirmation_valid
from mewcode.experts.human_gate import gate_scope_hash
from mewcode.experts.node_ledger import ledger_for_run
from mewcode.experts.stage_run_inputs import collect_stage_run_inputs
from mewcode.experts.waits import BranchWaitPaused


def test_context_coordinator_only_reads_handoffs_and_cannot_ask_or_execute(tmp_path):
    from mewcode.expert_web import _role_registry, _restrict_context_tools
    from mewcode.tools.submit_handoff import HandoffHolder
    from tests.test_expert_children import make_registry
    parent = make_registry("ReadFile", "Bash", "WriteFile")
    session = SimpleNamespace(agent=SimpleNamespace(registry=parent), work_dir=str(tmp_path))
    policy = ExpertExecutionPolicy("stable", False, 0, node_tools=(), context_coordination=True,
                                   handoff_holder=HandoffHolder())
    registry = _role_registry(session, "manager", policy)
    assert {tool.name for tool in registry.list_tools()} == {"ReadStageResult", "SubmitHandoff", "report_step"}
    # Agent construction or discovery may add tools again. The restriction
    # is repeated before streaming and at each model turn.
    from mewcode.tools.ask_user import AskUserTool
    registry.register(AskUserTool())
    _restrict_context_tools(registry)
    assert registry.get("AskUserQuestion") is None


def current_gate(tmp_path):
    flow = ExpertFlowSpec.from_value({"flow_id": "pdf", "label": "PDF知识库", "finalizer_node_id": "pilot",
        "nodes": [{"node_id": "survey", "role": "planner", "label": "调查四个PDF"},
                  {"node_id": "approval", "role": "manager", "label": "确认费用与内容外发"},
                  {"node_id": "pilot", "role": "executor", "label": "完成代表性试点",
                   "model": {"provider_key": "configured", "model_id": "deepseek-flash", "effort": "max"}}],
        "edges": [{"from": "survey", "to": "approval"}, {"from": "approval", "to": "pilot"}],
        "meta": {"node_kinds": {"approval": "gate"}, "node_plan_briefs": {
            "approval": {"summary": "确认样本模型调用与候选文件写入的范围。", "steps": ["集中核对费用、内容与输出位置。"]},
            "pilot": {"summary": "用已保存的16页代表样本验证识别能力。", "outputs": ["试点结果及失败页清单"]}},
            "run_input_contract": [{"name": "cost", "label": "费用政策", "collect_at": "approval"},
                                   {"name": "external", "label": "外发内容", "collect_at": "approval"},
                                   {"name": "directory", "label": "成果目录", "collect_at": "approval"}]}})
    store = ExpertRunStore(tmp_path / "runs")
    run_id = "a" * 32
    store.create(run_id=run_id, conversation_id="pdf-chat", task="将四个PDF落库", attachments=[],
                 profile={}, strategy="stable", flow=flow)
    store.mutate(run_id, lambda value: value.update(node_states={
        "survey": {"status": "completed", "handoff": {"summary": "四书1073页；已完成调查，尚未全量识别。",
            "decisions": ["SQLite作为主库，保留原书来源。"], "open_items": ["图表辨识由后续试点验证。"]}},
        "approval": {"status": "running"}},
        run_inputs={"cost": "只使用现有额度，用完暂停，不超额付费。", "external": "必要文本、原页和图块。",
                    "directory": str(tmp_path / "candidate")}))
    return flow, store, run_id, SimpleNamespace()


@pytest.mark.asyncio
@pytest.mark.parametrize("generated", ["试点先验证中文、公式和图表，随后独立复核。", ""])
async def test_first_gate_question_already_has_plan_and_context_not_an_intermediate_results_question(tmp_path, generated):
    flow, store, run_id, session = current_gate(tmp_path)
    builder = AsyncMock(return_value=generated)
    questions = []
    async def emit(event):
        assert event["type"] != "ask_user", "Intermediate handoff was turned into a generic user question"
        if event["type"] == "human_oncall":
            questions.append(event)
            assert builder.await_count == 1
            assert not store.load(run_id).get("gate_confirmations")
            session._human_oncall_request.future.set_result({"choice": "pause"})
    assert not await await_dialogue(node=flow.node_map["approval"], flow=flow, session=session,
        store=store, run_id=run_id, emit=emit, proposal_builder=builder)
    question = questions[0]["questions"][0]
    for value in ("1073", "16页", "SQLite", "deepseek-flash", "max", "不超额付费", "必要文本", "成果目录"):
        assert value in question["context"]
    assert "上游中间产物" in question["context"]
    assert question["requires_explicit_selection"]
    assert "图表辨识由后续试点验证" in builder.call_args.args[0]
    assert not store.load(run_id).get("authorizations", {}).get("entries")


@pytest.mark.asyncio
async def test_question_about_plan_is_coordinated_into_new_proposal_and_not_approved(tmp_path):
    flow, store, run_id, session = current_gate(tmp_path)
    builder = AsyncMock(return_value="先验证样本，再独立复核。")
    seen = []
    async def emit(event):
        if event["type"] == "human_oncall":
            seen.append(event)
            assert not store.load(run_id).get("gate_confirmations")
            session._human_oncall_request.future.set_result({"choice": "你目前的计划是什么，先告诉我" if len(seen) == 1 else "continue"})
    assert await await_dialogue(node=flow.node_map["approval"], flow=flow, session=session,
        store=store, run_id=run_id, emit=emit, proposal_builder=builder)
    assert builder.await_count == 2 and "你目前的计划是什么" in builder.call_args.args[0]
    record = store.load(run_id)
    assert confirmation_valid(flow.node_map["approval"], flow, record)
    assert record["gate_confirmations"]["approval"]["request_id"] == seen[1]["request_id"]


@pytest.mark.asyncio
async def test_changed_next_model_invalidates_approval_for_plan_user_saw(tmp_path):
    flow, store, run_id, session = current_gate(tmp_path)
    async def emit(event):
        if event["type"] == "human_oncall":
            store.mutate(run_id, lambda value: value.update(locked_models={"pilot": {
                "provider_key": "other", "model_id": "other-model", "effort": "high"}}))
            session._human_oncall_request.future.set_result({"choice": "continue"})
    assert not await await_dialogue(node=flow.node_map["approval"], flow=flow, session=session,
        store=store, run_id=run_id, emit=emit)
    assert not store.load(run_id).get("gate_confirmations")


@pytest.mark.asyncio
async def test_legacy_empty_gate_question_is_replaced_without_answer_or_grant(tmp_path):
    flow, store, run_id, session = current_gate(tmp_path)
    pending = {"kind": "ask_user", "tool": "AskUserQuestion", "request_id": "old-question",
        "scope_hash": gate_scope_hash(flow.node_map["approval"], flow.meta) + ":clarify",
        "questions": [{"name": "requirements", "message": "有什么要求？", "type": "text"}]}
    def seed(value):
        value["node_states"]["approval"]["pending_interaction"] = copy.deepcopy(pending)
        ledger = ledger_for_run(value)
        ledger.record("approval", "question", {"request_id": "old-question"})
        ledger.record("approval", "blocker", {"kind": "human_question", "ref": "old-question"})
        ledger.record("approval", "blocker", {"kind": "effect_unknown", "ref": "unrelated-command"})
    store.mutate(run_id, seed)
    async def emit(event):
        if event["type"] == "human_oncall":
            assert event["request_id"] != "old-question"
            session._human_oncall_request.future.set_result({"choice": "pause"})
    assert not await await_dialogue(node=flow.node_map["approval"], flow=flow, session=session,
        store=store, run_id=run_id, emit=emit)
    record = store.load(run_id)
    retired = record["node_states"]["approval"]["interaction_history"][0]
    assert retired["pending"] == pending and not retired["answered"]
    assert not record.get("gate_confirmations") and not record.get("authorizations", {}).get("entries")
    assert any(f.payload.get("ref") == "unrelated-command" for f in ledger_for_run(record).view("approval").active_blockers)


@pytest.mark.asyncio
async def test_reconnect_preserves_same_concrete_plan_and_request(tmp_path):
    flow, store, run_id, session = current_gate(tmp_path)
    ready = asyncio.Event()
    first = []
    async def initial_emit(event):
        if event["type"] == "human_oncall":
            first.append(event)
            ready.set()
    task = asyncio.create_task(await_dialogue(node=flow.node_map["approval"], flow=flow, session=session,
        store=store, run_id=run_id, emit=initial_emit))
    await asyncio.wait_for(ready.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    builder = AsyncMock(side_effect=AssertionError("Already saved plan must not be regenerated"))
    async def resumed_emit(event):
        if event["type"] == "human_oncall":
            assert event["resumed"] and event["request_id"] == first[0]["request_id"]
            assert event["questions"] == first[0]["questions"]
            session._human_oncall_request.future.set_result({"choice": "continue"})
    assert await await_dialogue(node=flow.node_map["approval"], flow=flow, session=session,
        store=store, run_id=run_id, emit=resumed_emit, proposal_builder=builder)
    assert not builder.await_count


@pytest.mark.asyncio
async def test_stage_parameters_have_background_and_do_not_approve_gate(tmp_path):
    flow, store, run_id, session = current_gate(tmp_path)
    store.mutate(run_id, lambda value: value["run_inputs"].pop("cost"))
    asked = []
    async def emit(event):
        if event["type"] == "ask_user":
            asked.append(event)
            session._ask_user_request.future.set_result({"cost": "仅用现有额度"})
    values = await collect_stage_run_inputs(node=flow.node_map["approval"], flow=flow, session=session,
        store=store, run_id=run_id, emit=emit)
    assert values["cost"] == "仅用现有额度"
    assert "1073" in asked[0]["questions"][0]["context"]
    assert not store.load(run_id).get("gate_confirmations")


@pytest.mark.asyncio
async def test_cancelled_confirmation_keeps_node_incomplete(tmp_path):
    flow, store, run_id, session = current_gate(tmp_path)
    async def emit(event):
        if event["type"] == "human_oncall":
            session._human_oncall_request.future.set_result(None)
    with pytest.raises(BranchWaitPaused):
        await await_dialogue(node=flow.node_map["approval"], flow=flow, session=session,
            store=store, run_id=run_id, emit=emit)
    record = store.load(run_id)
    assert record["node_states"]["approval"]["status"] == "paused"
    assert record["node_states"]["approval"]["pending_interaction"]
    assert not record.get("gate_confirmations")
