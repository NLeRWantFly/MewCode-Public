import copy

from mewcode.experts.auto_card import auto_card


def paused_stage():
    return {
        "node_id": "pilot", "label": "准备工具并完成试点",
        "handoff": {"blocker_kind": "capability_missing", "status": "needs_attention",
            "blockers": ["本节点交接声明未完成：blocked"],
            "command_requests": [{"command": "python -c \"import fitz\"",
                "purpose": "确认 PDF 工具依赖，随后跑通识别到查询链路"}]},
        "tools": {"probe": {"name": "Bash", "status": "failed", "is_error": True,
            "args": {"command": "python -c \"import fitz\""},
            "output": "Permission denied: 子节点命令需要可用的隔离执行器；宿主命令无法保证声明的写入范围。"}},
        "available_tools": ["Bash", "WriteFile", "SubmitHandoff"],
    }


def paused_decision():
    return {"action": "hold_until_clearance", "trigger": "capability_missing",
        "error": "存在未决阻塞：capability_missing (current_stop; 清除条件: capability_binding_receipt)",
        "clearance_conditions": [{"kind": "capability_missing", "ref": "current_stop",
            "clearance": "capability_binding_receipt"}]}


def test_capability_pause_shows_node_actual_denial_and_affected_delivery_without_changing_authority():
    stage, decision = paused_stage(), paused_decision()
    original = copy.deepcopy((stage, decision))
    view = auto_card(decision, stage)
    assert "准备工具并完成试点" in view["reason"]
    assert "隔离执行器" in view["reason"]
    assert "确认 PDF 工具依赖" in view["reason"]
    assert "capability_binding_receipt" not in view["reason"]
    assert "隔离" in view["next_step"] and "回执" in view["next_step"]
    assert (stage, decision) == original


def test_missing_details_are_explicit_instead_of_inventing_a_missing_tool():
    view = auto_card(paused_decision(), {"node_id": "n", "label": "处理节点", "handoff": {}})
    assert "未说明具体" in view["reason"]
    assert "补充" in view["next_step"]
    assert "Bash" not in view["reason"]


def test_double_wrapped_capability_blocker_retains_missing_detail_without_mutating_input():
    prefix = "本节点交接声明未完成："
    stage = {"node_id": "n", "label": "工具节点", "handoff": {"blockers": [
        prefix + prefix + "缺少 BrowserSearch，无法检索指定来源。"]}}
    original = copy.deepcopy(stage)

    view = auto_card(paused_decision(), stage)

    assert "缺少 BrowserSearch，无法检索指定来源。" in view["reason"]
    assert view["reason"].count(prefix) == 0
    assert stage == original


def test_pure_status_placeholders_do_not_appear_as_capability_details():
    prefix = "本节点交接声明未完成："
    stage = {"node_id": "n", "label": "工具节点", "handoff": {"blockers": [
        prefix + "blocked", prefix + "needs_attention", "needs_attention"]}}

    view = auto_card(paused_decision(), stage)

    assert "blocked" not in view["reason"]
    assert "needs_attention" not in view["reason"]
    assert "未说明具体缺少" in view["reason"]


def test_resolved_old_tool_failure_is_not_presented_as_current_capability_gap():
    stage = paused_stage()
    stage["tools"]["success"] = {"name": "Bash", "status": "completed", "is_error": False,
        "args": {"command": "python -c \"import fitz\""}, "output": "fitz available"}
    stage["handoff"]["blockers"] = ["当前缺少已确认的视觉模型绑定"]
    view = auto_card(paused_decision(), stage)
    assert "视觉模型绑定" in view["reason"]
    assert "子节点命令" not in view["reason"]


def test_model_upgrade_card_names_exact_target_and_preserves_progress():
    decision = {"action": "switch_declared_route", "source": "user_authorized_difficulty_route",
        "model_switch": {"to": {"provider_key": "chosen", "model_id": "gpt-6.1-sol", "effort": "xhigh"}}}
    view = auto_card(decision, {"label": "困难执行节点"},
                     health={"work_delta": {"write_receipts": 2, "inline_outputs": 4}})
    assert "gpt-6.1-sol" in view["reason"] and "极高" in view["reason"]
    assert [row["value"] for row in view["progress"]] == [2, 4]


def test_old_current_pause_is_reprojected_without_mutating_history_or_clearing_it(tmp_path):
    from mewcode.expert_runs import ExpertRunStore
    stage = paused_stage()
    stage.update(status="needs_attention", scheduler_recovery={"decisions": [
        dict(paused_decision(), decision_id="current", presentation={"reason": "old generic code"})]})
    old_event = {"type": "expert_step", "node_id": "pilot", "recovery_decision": {
        "action": "hold_until_clearance", "decision_id": "old", "presentation": {"reason": "old historical reason"}}}
    current_event = {"type": "expert_step", "node_id": "pilot", "recovery_decision":
        copy.deepcopy(stage["scheduler_recovery"]["decisions"][-1])}
    snapshot = {"run_id": "a" * 32, "conversation_id": "owner", "status": "paused",
        "node_states": {"pilot": stage}, "events": [old_event, current_event],
        "locked_models": {"pilot": {"provider_key": "chosen", "model_id": "gpt-6.1-sol", "effort": "xhigh"}},
        "difficult_node_model_policy": {"model": {"provider_key": "chosen", "model_id": "gpt-6.1-sol", "effort": "xhigh"}}}
    original = copy.deepcopy(snapshot)
    projected = ExpertRunStore._public_value(snapshot)
    card = projected["node_states"]["pilot"]["scheduler_recovery"]["decisions"][-1]["presentation"]
    assert "隔离执行器" in card["reason"]
    assert "gpt-6.1-sol" in card["note"] and "极高" in card["note"]
    assert "阻塞仍需处理" in card["note"]
    decision_events = [e["recovery_decision"] for e in projected["events"] if e.get("recovery_decision")]
    assert decision_events[0]["presentation"]["reason"] == "old historical reason"
    assert "隔离执行器" in decision_events[1]["presentation"]["reason"]
    assert snapshot == original and projected["status"] == "paused"
