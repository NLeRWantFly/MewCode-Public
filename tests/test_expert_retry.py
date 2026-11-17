import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mewcode.expert_runs import ExpertRunStore, parse_resume_message, is_explicit_new_run_message
from mewcode.expert_web import run_expert_chat
from tests.test_audit_protocol import flow_spec


@pytest.mark.parametrize("text", [
    "从失败的阶段开始",
    "根据之前的对话，我们已经执行到了12.4阶段，从这个阶段开始，恢复之前的记录，而不是从新开始",
    "从12.4阶段开始", "重试失败节点", "请恢复之前的任务", "继续",
])
def test_recovery_intent_does_not_start_a_new_run(text):
    assert parse_resume_message(text)[0]


@pytest.mark.parametrize("text", [
    "我怎么继续呢", "继续按钮在哪里", "不要恢复任务", "是否可以重试失败节点？",
    "解释如何恢复之前的记录", "新建任务：另做一个报告",
])
def test_questions_and_new_task_are_not_execution_consent(text):
    assert not parse_resume_message(text)[0]


def test_new_run_requires_explicit_leading_command():
    assert is_explicit_new_run_message("新建任务：测试另一个项目")
    assert is_explicit_new_run_message("请重新开始任务")
    assert not is_explicit_new_run_message("根据之前记录恢复，而不是从新开始")
    assert not is_explicit_new_run_message("不要新建任务：继续之前的")


def blocked_run(tmp_path):
    store = ExpertRunStore(tmp_path / "runs")
    flow = flow_spec().as_dict()
    flow["nodes"].insert(0, {"node_id": "prepare", "role": "executor", "label": "已完成修订"})
    flow["edges"].insert(0, {"from": "prepare", "to": "audit"})
    run_id = "a" * 32
    store.create(run_id=run_id, conversation_id="c", task="原始长任务", attachments=[], profile={},
                 strategy="stable", flow=flow)
    store.mutate(run_id, lambda s: s.update(
        status="blocked", resume_after_node="prepare", stage_cursor="repair",
        node_states={"prepare": {"status": "completed", "passed": True, "text": "已交付",
                                 "messages": [{"role": "assistant", "content": "durable"}]},
                     "audit": {"status": "needs_attention", "passed": False},
                     "repair": {"status": "failed", "passed": False, "text": "API error (502)"}},
        audit_protocols={"audit": {"rubric": {"rubric_hash": "fixed"}, "round": 3, "no_progress": 2},
                         "other": {"round": 2}},
    ))
    return store, run_id, flow


def test_retry_preserves_progress_and_only_renews_its_audit_budget(tmp_path):
    store, run_id, flow = blocked_run(tmp_path)
    before = store.load(run_id)
    flow["nodes"][2]["model"] = {"provider_key": "new", "model_id": "working-model"}
    resumed = store.resume(run_id, strategy="stable", retry_node_id="repair", flow=flow)
    assert resumed["run_id"] == run_id
    assert resumed["node_states"] == before["node_states"]
    assert "resume_after_node" not in resumed
    assert resumed["retry_node_id"] == "repair"
    assert resumed["audit_protocols"]["audit"]["round"] == 0
    assert resumed["audit_protocols"]["audit"]["rubric"] == {"rubric_hash": "fixed"}
    assert resumed["audit_protocols"]["other"] == {"round": 2}
    assert resumed["audit_restarts"][0]["previous"] == before["audit_protocols"]["audit"]


@pytest.mark.parametrize("retry,anchor", [("prepare", ""), ("missing", ""), ("repair", "prepare")])
def test_invalid_retry_is_atomic(tmp_path, retry, anchor):
    store, run_id, _ = blocked_run(tmp_path)
    before = store.load(run_id)
    with pytest.raises(ValueError):
        store.resume(run_id, strategy="stable", retry_node_id=retry, resume_after_node=anchor)
    assert store.load(run_id) == before


def test_new_graph_ids_cannot_discard_completed_checkpoints(tmp_path):
    store, run_id, flow = blocked_run(tmp_path)
    before = store.load(run_id)
    changed = copy.deepcopy(flow)
    changed["nodes"][0]["node_id"] = "renamed_prepare"
    changed["edges"][0]["from"] = "renamed_prepare"
    with pytest.raises(ValueError, match="保护进度"):
        store.resume(run_id, strategy="stable", flow=changed)
    assert store.load(run_id) == before


def test_terminated_run_requires_explicit_checkpoint_to_restart(tmp_path):
    store, run_id, _ = blocked_run(tmp_path)
    store.mutate(run_id, lambda s: s.update(status="terminated",
        review_queues={"audit": {"records": {"page": {"state": "complete"}}}}))
    before = store.load(run_id)
    with pytest.raises(ValueError, match="not resumable"):
        store.resume(run_id, strategy="stable")
    assert store.load(run_id) == before
    resumed = store.resume(run_id, strategy="stable", resume_after_node="prepare")
    assert resumed["status"] == "running"
    assert resumed["run_id"] == run_id
    assert resumed["node_states"]["prepare"] == before["node_states"]["prepare"]
    assert "repair" not in resumed["node_states"]
    assert not resumed["review_queues"]
    assert resumed["review_queue_archives"][0]["queue"] == before["review_queues"]["audit"]


def test_running_run_cannot_restart_from_checkpoint(tmp_path):
    store, run_id, _ = blocked_run(tmp_path)
    store.mutate(run_id, lambda s: s.update(status="running"))
    before = store.load(run_id)
    with pytest.raises(ValueError, match="not resumable"):
        store.resume(run_id, strategy="stable", resume_after_node="prepare")
    assert store.load(run_id) == before


@pytest.mark.asyncio
async def test_followup_cannot_silently_create_another_expert_run(tmp_path):
    store, run_id, _ = blocked_run(tmp_path)
    before = store.load(run_id)
    ws = SimpleNamespace(send_json=AsyncMock())
    session = SimpleNamespace(conversation_id="c", status_payload=lambda: {}, tasks_payload=lambda: [])
    # Missing profile/provider/session machinery is intentional: the guard must
    # return before model setup, transcript writes, or graph execution.
    await run_expert_chat(ws, "根据之前记录处理这个问题", session=session,
                          registry_service=None, profile_store=None, run_store=store)
    events = [call.args[0] for call in ws.send_json.call_args_list]
    assert events[-1]["type"] == "done"
    assert events[-1]["error_code"] == "unconfirmed_flow_start"
    assert any("未启动编排" in event.get("text", "") for event in events)
    assert store.load(run_id) == before
    assert len(store.list_for_conversation("c")) == 1
