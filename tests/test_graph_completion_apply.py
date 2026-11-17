"""Host-side completion permissions, audit receipts, and rejection cases."""
import copy

import pytest

from mewcode.orchestration.graph_completion import (
    apply_completion, protected_view, protected_violations,
)


def entry(entry_id, kind="node", *, data=None, evidence="user_source", **more):
    return {"id": entry_id, "kind": kind, "title": entry_id, "body": "原始要求",
            "state": "active", "data": data if data is not None else {}, "sources": [],
            "evidence": evidence, **more}


def doc():
    return {"schema_version": 1, "ignored": [], "entries": [
        entry("work", data={"node_kind": "implement", "inputs": [], "outputs": [],
                            "acceptance": ["用户验收"], "steps": []}),
        entry("check", data={"node_kind": "audit", "outputs": ["复核记录"], "acceptance": ["通过"]}),
        entry("finish", data={"node_kind": "reduce", "outputs": ["交付物"], "acceptance": ["完成"]}),
        entry("extra", data={"node_kind": "implement", "outputs": ["修复"], "acceptance": ["验证"]}),
        entry("gate", data={"node_kind": "gate", "outputs": []}),
        entry("edge", "edge", data={"from_node": "check", "to_node": "work", "condition": "fail"}),
        entry("loop", "loop", data={"loop_id": "review", "entry_node_id": "work",
                                     "check_node_id": "check", "node_ids": ["work", "check"],
                                     "reviewer_node_ids": ["check"], "repair_node_ids": [],
                                     "control_mode": "strict", "exit_condition": "合格退出",
                                     "criteria": [{"criterion_id": "C", "acceptance_test": "用户标准"}],
                                     "max_rounds": 3}),
    ]}


def propose(*items, **extra):
    return {"upserts": [{"rationale": "补齐可检查的结构", **item} for item in items], **extra}


def by_id(document):
    return {e["id"]: e for e in document["entries"]}


@pytest.mark.parametrize("item,code", [
    ({"id": "work", "state": "retired"}, "delete_forbidden"),
    ({"id": "work", "body": "改写用户说明"}, "locked_content"),
    ({"id": "work", "data": {"acceptance": ["替换验收"]}}, "locked_content"),
    ({"id": "work", "data": {"node_kind": "plan"}}, "locked_content"),
    ({"id": "gate", "data": {"outputs": ["确认单"]}}, "gate_forbidden"),
    ({"id": "edge", "data": {"from_node": "extra"}}, "locked_content"),
    ({"id": "edge", "data": {"to_node": "finish"}}, "locked_content"),
    ({"id": "edge", "data": {"condition": "pass"}}, "locked_content"),
    ({"id": "loop", "data": {"node_ids": ["work"]}}, "locked_content"),
    ({"id": "loop", "data": {"max_rounds": 5}}, "locked_content"),
    ({"id": "loop", "data": {"exit_condition": "放宽"}}, "locked_content"),
    ({"id": "new_gate", "kind": "node", "title": "签认", "body": "签认",
      "data": {"node_kind": "gate"}}, "gate_forbidden"),
])
def test_forbidden_changes_are_rejected_without_mutation(item, code):
    before = doc()
    after, applied, rejected = apply_completion(before, propose(item))
    assert after == before and applied == []
    assert rejected[0]["entry_id"] == item["id"] and rejected[0]["code"] == code
    assert rejected[0]["message"]


def test_locked_allowed_changes_have_receipts_audits_and_frozen_views():
    before = doc()
    proposal = propose(
        {"id": "work", "data": {"outputs": ["报告"], "inputs": ["用户主题"]}},
        {"id": "edge", "data": {"condition": "loop"}},
        {"id": "loop", "data": {"node_ids": ["work", "check", "extra"],
                                  "repair_node_ids": ["extra"]}},
    )
    after, applied, rejected = apply_completion(before, proposal)
    assert rejected == []
    assert [a["code"] for a in applied] == ["list_filled", "condition_to_loop", "loop_members_extended"]
    old, new = by_id(before), by_id(after)
    for receipt in applied:
        assert receipt["op"] == "modify" and receipt["before"] == old[receipt["entry_id"]]
        assert receipt["after"] == new[receipt["entry_id"]]
        audit = new[receipt["audit_entry_id"]]
        assert audit["title"].startswith("AI 补全：") and audit["data"]["category"] == "designed"
        assert audit["evidence"] == "ai_completed" and audit["rationale"]
    assert len({new[receipt["audit_entry_id"]]["body"] for receipt in applied}) == 3
    for entry_id, original in old.items():
        assert protected_view(original) == protected_view(new[entry_id])
        assert protected_violations(original, new[entry_id]) == []
    repeated, repeat_applied, _ = apply_completion(after, proposal)
    assert repeat_applied == []
    assert len([e for e in repeated["entries"] if e["id"].startswith("audit_")]) == 3


def test_protected_relation_guard_detects_bypassed_whitelist():
    before = by_id(doc())
    changed = copy.deepcopy(before["work"])
    changed["data"]["acceptance"] = ["新验收"]
    assert protected_violations(before["work"], changed)[0]["field"] == "data.acceptance"
    changed = copy.deepcopy(before["edge"])
    changed["data"]["condition"] = "pass"
    assert protected_violations(before["edge"], changed)
    changed = copy.deepcopy(before["loop"])
    changed["data"]["node_ids"] = ["work"]
    assert protected_violations(before["loop"], changed)
    changed = copy.deepcopy(before["loop"])
    changed["data"].pop("control_mode")
    changed["data"]["timeout_seconds"] = None
    assert any(v["field"] == "data.timeout_seconds" for v in protected_violations(before["loop"], changed))


def test_locked_loop_cannot_add_absent_policy_key_even_with_null_value():
    before = doc()
    by_id(before)["loop"]["data"].pop("control_mode")
    after, applied, rejected = apply_completion(before, propose(
        {"id": "loop", "data": {"control_mode": None}}))
    assert after == before and applied == [] and rejected[0]["code"] == "locked_content"


def test_additions_are_ai_completed_and_never_create_conflicts():
    before = doc()
    new_loop = {"id": "fresh_loop", "kind": "loop", "title": "返修", "body": "复核失败返修",
                "data": {"loop_id": "fresh", "entry_node_id": "work", "check_node_id": "check",
                         "node_ids": ["work", "check"], "reviewer_node_ids": ["check"],
                         "control_mode": "team", "exit_condition": "全部通过",
                         "criteria": [{"criterion_id": "C1", "acceptance_test": "复核通过"}]}}
    after, applied, rejected = apply_completion(before, propose(
        {"id": "new_edge", "kind": "edge", "title": "依赖", "body": "先工作再复核",
         "data": {"from_node": "work", "to_node": "check", "condition": "always"}},
        {"id": "new_node", "kind": "node", "title": "连接", "body": "整理连接", "data": {"node_kind": "plan"}},
        new_loop,
        {"id": "assume", "kind": "decision", "title": "默认顺序", "body": "先写后查",
         "data": {"category": "designed"}},
        {"id": "topic", "kind": "input", "title": "主题", "body": "运行时填写",
         "data": {"name": "主题", "required": True}},
        {"id": "ask", "kind": "question", "title": "待确认", "body": "是否删除文件",
         "data": {"blocking": True}},
    ))
    assert rejected == []
    assert [a["code"] for a in applied] == ["edge_added", "node_added", "loop_added",
                                            "decision_added", "input_added", "question_added"]
    assert all(by_id(after)[a["entry_id"]]["evidence"] == "ai_completed" for a in applied)
    assert all(a["audit_entry_id"] is None for a in applied)
    assert not [e for e in after["entries"] if e["state"] == "conflict"]
    assert by_id(after)["ask"]["data"]["blocking"] is True
    assert before == doc()


@pytest.mark.parametrize("change,code", [
    ({"control_mode": "strict"}, "loop_policy_forbidden"),
    ({"max_rounds": 3}, "budget_forbidden"),
    ({"loop_id": "review"}, "duplicate_id"),
    ({"loop_id": " " * 80 + "x"}, "not_whitelisted"),
])
def test_new_loop_policy_rejections(change, code):
    data = {"loop_id": "fresh", "entry_node_id": "work", "check_node_id": "check",
            "node_ids": ["work", "check"], "reviewer_node_ids": ["check"],
            "control_mode": "team", "exit_condition": "全部通过",
            "criteria": [{"criterion_id": "C", "acceptance_test": "复核通过"}], **change}
    after, applied, rejected = apply_completion(doc(), propose(
        {"id": "new_loop", "kind": "loop", "title": "循环", "data": data}))
    assert applied == [] and after == doc() and rejected[0]["code"] == code


def test_missing_rationale_bad_refs_and_delete_list_are_rejected():
    after, applied, rejected = apply_completion(doc(), {"upserts": [
        {"id": "bad", "kind": "edge", "title": "坏边", "data": {"from_node": "absent", "to_node": "work"},
         "rationale": "尝试连边"},
        {"id": "no_reason", "kind": "question", "title": "待问", "data": {"blocking": False}},
    ], "deletes": ["work"]})
    assert after == doc() and applied == []
    assert {e["code"] for e in rejected} == {"bad_reference", "missing_rationale", "delete_forbidden"}


def test_malformed_new_refs_and_loop_criteria_are_rejected_per_item():
    loop_data = {"loop_id": "fresh", "entry_node_id": "work", "check_node_id": "check",
                 "node_ids": ["work", "check"], "reviewer_node_ids": ["check"],
                 "control_mode": "team", "exit_condition": "通过",
                 "criteria": [{"criterion_id": 12, "acceptance_test": True}]}
    after, applied, rejected = apply_completion(doc(), propose(
        {"id": "bad_edge", "kind": "edge", "title": "坏边",
         "data": {"from_node": ["work"], "to_node": "check"}},
        {"id": "bad_loop_ref", "kind": "loop", "title": "坏循环",
         "data": {**loop_data, "entry_node_id": ["work"]}},
        {"id": "bad_criteria", "kind": "loop", "title": "坏验收", "data": loop_data},
    ))
    assert after == doc() and applied == []
    assert [entry["code"] for entry in rejected] == ["bad_reference", "bad_reference", "loop_policy_forbidden"]


def test_nonlocked_node_edge_and_ai_ledger_updates_follow_matrix():
    before = doc()
    before["entries"].extend([
        entry("proposal_node", data={"node_kind": "implement", "outputs": [], "acceptance": ["旧标准"]},
              evidence="proposal"),
        entry("ai_node", data={"node_kind": "implement", "outputs": ["旧产物"],
                               "acceptance": ["旧标准"]}, evidence="ai_completed", rationale="初始设计"),
        entry("proposal_edge", "edge", data={"from_node": "work", "to_node": "check",
                                               "condition": "always"}, evidence="proposal"),
        entry("ai_decision", "decision", data={"category": "designed"},
              evidence="ai_completed", rationale="初始设计"),
        entry("ai_question", "question", data={"blocking": False},
              evidence="ai_completed", rationale="初始设计"),
        entry("ai_input", "input", data={"name": "主题", "required": True},
              evidence="ai_completed", rationale="初始设计"),
    ])
    after, applied, rejected = apply_completion(before, propose(
        {"id": "proposal_node", "data": {"outputs": ["新产物"]}},
        {"id": "ai_node", "title": "新的连接节点", "body": "新的职责",
         "data": {"node_kind": "plan", "parent_id": "work", "outputs": ["新产物"]}},
        {"id": "proposal_edge", "data": {"to_node": "finish"}},
        {"id": "ai_decision", "title": "新的假设", "data": {"value": "8"}},
        {"id": "ai_question", "body": "新的待确认说明", "data": {"blocking": True}},
        {"id": "ai_input", "title": "运行主题", "data": {"description": "用户提供"}},
    ))
    assert rejected == [] and len(applied) == 6
    assert [receipt["code"] for receipt in applied] == ["list_filled", "own_entry_updated",
                                                         "own_entry_updated", "own_entry_updated",
                                                         "own_entry_updated", "own_entry_updated"]
    assert all(receipt["audit_entry_id"] is None for receipt in applied)
    assert by_id(after)["ai_node"]["data"]["parent_id"] == "work"


def test_nonlocked_proposal_and_reviewed_entries_still_respect_limits():
    before = doc()
    before["entries"].extend([
        entry("p_node", data={"node_kind": "implement", "outputs": ["已有"]}, evidence="proposal"),
        entry("reviewed_node", data={"node_kind": "implement", "outputs": []},
              evidence="proposal", reviewed=True),
        entry("p_loop", "loop", data={"loop_id": "p", "entry_node_id": "work",
                                        "check_node_id": "check", "node_ids": ["work", "check"],
                                        "reviewer_node_ids": ["check"], "control_mode": "strict",
                                        "exit_condition": "原标准"}, evidence="proposal"),
        entry("ai_loop", "loop", data={"loop_id": "ai", "entry_node_id": "work",
                                         "check_node_id": "check", "node_ids": ["work", "check"],
                                         "reviewer_node_ids": ["check"], "control_mode": "team",
                                         "exit_condition": "原标准"}, evidence="ai_completed", rationale="初始设计"),
    ])
    for upsert, expected in [
        ({"id": "p_node", "data": {"outputs": ["替换"]}}, "not_whitelisted"),
        ({"id": "reviewed_node", "data": {"node_kind": "plan"}}, "locked_content"),
        ({"id": "p_loop", "data": {"exit_condition": "放宽"}}, "loop_policy_forbidden"),
        ({"id": "p_loop", "data": {"criteria": None}}, "loop_policy_forbidden"),
        ({"id": "ai_loop", "data": {"control_mode": "strict"}}, "loop_policy_forbidden"),
        ({"id": "ai_loop", "data": {"max_rounds": 2}}, "budget_forbidden"),
    ]:
        after, applied, rejected = apply_completion(before, propose(upsert))
        assert after == before and applied == [] and rejected[0]["code"] == expected
    after, applied, rejected = apply_completion(before, propose(
        {"id": "reviewed_node", "data": {"outputs": ["补全"]}},
        {"id": "ai_loop", "data": {"exit_condition": "新标准",
                                    "criteria": [{"criterion_id": "C", "acceptance_test": "通过"}]}}))
    assert rejected == [] and [a["code"] for a in applied] == ["list_filled", "own_entry_updated"]
    assert applied[0]["audit_entry_id"] in by_id(after)
    assert protected_violations(by_id(before)["reviewed_node"], by_id(after)["reviewed_node"]) == []


@pytest.mark.parametrize("kind,data", [
    ("goal", {}), ("constraint", {}), ("artifact", {}), ("progress", {}),
    ("decision", {"category": "designed"}), ("question", {"blocking": False}),
    ("input", {"name": "主题", "required": True}),
])
def test_nonlocked_ledger_entries_outside_ai_ownership_cannot_change(kind, data):
    before = doc()
    before["entries"].append(entry("ledger", kind, data=data, evidence="proposal"))
    after, applied, rejected = apply_completion(before, propose({"id": "ledger", "body": "改写"}))
    assert after == before and applied == [] and rejected[0]["code"] == "not_whitelisted"


@pytest.mark.parametrize("change", [
    {"sources": [{"source_id": "fake", "quote": "伪来源"}]},
    {"evidence": "proposal"}, {"reviewed": True}, {"kind": "question"},
    {"state": "conflict"},
])
def test_existing_identity_source_and_state_cannot_change(change):
    before = doc()
    after, applied, rejected = apply_completion(before, propose({"id": "work", **change}))
    assert after == before and applied == [] and rejected[0]["code"] == "not_whitelisted"
