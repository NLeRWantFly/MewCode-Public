"""Planning repairs retain durable choices and runtime predicates."""
import copy
import json

import pytest

from mewcode.orchestration.draft_store import DraftStore
from mewcode.orchestration.run_inputs import predicate_matches, validate_branch_contract
from mewcode.orchestration.workflow_blocks import freeze_materials
from mewcode.orchestration.workflow_draft import apply_patch, validate_entry
from tests.test_workflow_drafts import cited, entry
from tests.test_workflow_pending import valid_materials, withdraw


@pytest.mark.parametrize("quote", ["selected_value", "fabricated_value"])
def test_durable_choice_is_citable_after_discussion_leaves_current_build(tmp_path, quote):
    blocks, _ = valid_materials(tmp_path / "project")
    store = DraftStore(blocks.project, "chat")
    saved_source = {"id": "discussion_saved", "role": "user", "text": "确认值：selected_value",
                    "purpose": "graph_discussion", "line": None}
    with store.db() as db:
        state = store._get(db)
        state["materials"]["document"]["entries"].append({
            "id": "choice", "kind": "decision", "title": "设计选择", "body": "selected_value",
            "state": "active", "data": {"category": "confirmed", "value": "selected_value"},
            "evidence": "manual", "reviewed": True,
            "sources": [{"source_id": saved_source["id"], "quote": quote}]})
        state["materials"]["pending"] = [{"entry_id": "", "code": "invalid_ignored", "path": "ignored"}]
        # The current build no longer carries a live discussion. The real host
        # stores adopted user choices in this separately scoped source ledger.
        state["candidate"] = None
        for scope, source in [(store.scope, saved_source),
                              (("other", "main"), {**saved_source, "id": "discussion_other"}),
                              (("chat", "other"), {**saved_source, "id": "discussion_other_draft"}),
                              (store.scope, {**saved_source, "id": "stale_message", "purpose": "message"})]:
            db.execute("INSERT INTO source VALUES (?,?,?,?)", (*scope, source["id"], json.dumps(source)))
        store._put(db, state)
    before = DraftStore.read_state(blocks.project, "chat")
    view = blocks.read(source_ids=[saved_source["id"]])
    assert view["sources"] == [{**saved_source, "kind": "planning_discussion"}]
    assert not {"discussion_other", "discussion_other_draft", "stale_message"} & {
        row["id"] for row in view["source_index"]}
    assert DraftStore.read_state(blocks.project, "chat") == before
    assert blocks.current_basis()["requirement_fingerprint"] == view["requirement_fingerprint"]
    result = withdraw(blocks, view)
    if quote == "selected_value":
        assert result["pending"] == []
    else:
        assert any(row["entry_id"] == "choice" and row["code"] == "invalid_source"
                   for row in result["pending"])
    after = DraftStore.read_state(blocks.project, "chat")
    assert after["materials"]["document"] == before["materials"]["document"]
    assert after["source_ids"] == before["source_ids"]
    assert saved_source["id"] not in after["materials"]["source_order"]
    assert after["candidate"] is None
    assert result["execution_authorization"] is False


def test_planning_source_read_does_not_create_missing_store(tmp_path):
    project = tmp_path / "missing"
    assert DraftStore.read_planning_sources(project, "chat") == []
    assert not project.exists()


def test_clear_branch_group_preserves_edge_predicate_and_other_fields():
    source = {"id": "s_old", "role": "user", "text": "保留选择条件，允许才执行。"}
    rows = [entry("gate"), entry("work"), entry("i_choice", kind="input", data={
        "name": "choice", "required": True, "default": None,
        "options": [{"value": "allow", "label": "允许"}, {"value": "deny", "label": "拒绝"}]}),
        entry("guard", kind="edge", data={"from_node": "gate", "to_node": "work", "condition": "pass",
            "input_when": {"input": "choice", "equals": "allow"}, "branch_group": "incorrect_single_path"})]
    document = {"entries": [cited(row, source) for row in rows], "ignored": []}
    repair = {"id": "s_repair", "role": "user", "text": "清除错误分组，保留原门禁和条件。"}
    patch = cited(entry("guard", kind="edge", data={"from_node": "gate", "to_node": "work",
        "condition": "pass", "branch_group": ""}), repair)
    repaired = apply_patch(document, {"upserts": [patch], "ignored": []}, [repair], [source])
    edge = next(row for row in repaired["entries"] if row["id"] == "guard")
    assert edge["data"] == {**rows[-1]["data"], "branch_group": ""}
    assert repaired["entries"][:-1] == document["entries"][:-1]
    assert predicate_matches(edge["data"]["input_when"], {"choice": "allow"}) is True
    assert predicate_matches(edge["data"]["input_when"], {"choice": "deny"}) is False
    with pytest.raises(ValueError):
        predicate_matches(edge["data"]["input_when"], {})
    validate_branch_contract([edge["data"]], [rows[2]["data"]], node_ids={"gate", "work"})
    with pytest.raises(ValueError, match="至少两条路径"):
        validate_branch_contract([rows[-1]["data"]], [rows[2]["data"]], node_ids={"gate", "work"})
    # Missing a field in an incremental update is still not a deletion.
    omitted = copy.deepcopy(patch)
    omitted["data"].pop("branch_group")
    kept = apply_patch(document, {"upserts": [omitted], "ignored": []}, [repair], [source])
    assert kept["entries"][-1]["data"]["branch_group"] == "incorrect_single_path"
    material_source = {"id": "s_material_repair", "role": "assistant", "text": repair["text"]}
    material_document = copy.deepcopy(repaired)
    material_document["entries"][-1]["sources"] = [{"source_id": material_source["id"], "quote": repair["text"]}]
    candidate = {"document": document, "materials_snapshot": {"sequence": 1, "document": document}}
    refreshed = freeze_materials(candidate, {"materials": {"sequence": 2, "document": material_document,
        "sources": [material_source]}}, force=True, sources=[source, repair])
    assert refreshed["document"]["entries"][-1]["data"] == edge["data"]


@pytest.mark.parametrize("value", [None, False, 42, [], "x" * 101])
def test_clearing_group_does_not_accept_invalid_types(value):
    with pytest.raises(ValueError, match="branch_group"):
        validate_entry(entry("guard", kind="edge", data={"branch_group": value}))
