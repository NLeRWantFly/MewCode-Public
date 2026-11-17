"""An explicit preview can insert review without unlocking saved branch policy."""
import copy
import itertools
import json

import pytest

from mewcode.orchestration.draft_store import DraftStore
from mewcode.orchestration.graph_completion import apply_completion
from mewcode.orchestration.graph_local_repair import SCHEMA
from mewcode.orchestration.graph_readiness import readiness
from mewcode.orchestration.workflow_draft import project_graph
from mewcode.orchestration.validate import validate_spec
from tests.test_graph_autofix import document, edge, entry, node
from tests.test_workflow_drafts import ready


def publication_graph(evidence="saved_graph"):
    bypass = edge("published_to_done", "N12A", "N12B")
    bypass["data"]["condition"] = "pass"
    bypass["evidence"] = evidence
    if evidence == "reviewed":
        bypass.update(evidence="proposal", reviewed=True)
    publish = edge("publish_choice", "gate", "N12A")
    publish["data"].update(condition="pass", input_when={"input": "publication_choice", "equals": "yes"}, branch_group="publication")
    defer = edge("defer_choice", "gate", "N12B")
    defer["data"].update(condition="pass", input_when={"input": "publication_choice", "equals": "no"}, branch_group="publication")
    return document(node("plan", "plan"), node("gate", "gate"), node("N12A", "implement"), node("N12B", "reduce"),
        edge("direction", "plan", "gate"), publish, defer, bypass,
        entry("choice", "input", data={"name": "publication_choice", "required": True, "options": [
            {"value": "yes", "label": "发布"}, {"value": "no", "label": "保留候选"}]}))


def setup(tmp_path, evidence="saved_graph"):
    doc = publication_graph(evidence)
    store = DraftStore(tmp_path, "synthetic")
    ready(store, [{"id": "s1", "role": "user", "text": "授权后发布并独立核验"}], doc)
    packet = store.request_local_graph_repair(None)
    prompt = json.loads(packet["prompt"])
    assert prompt["finalizer_node_id"] == "N12B"
    assert prompt["review_insertions"] == ["published_to_done"]
    return store, review_proposal(doc, packet["ticket"])


def review_proposal(doc, ticket):
    original = next(e for e in doc["entries"] if e["id"] == "N12A")["data"]
    return {"schema": SCHEMA, "ticket": ticket, "summary": "在发布与交付之间插入独立审查，保留原签认与分支", "upserts": [
        {"id": "published_to_done", "data": {"to_node": "audit_published"}, "rationale": "原边不再绕过独立审查"},
        {"id": "audit_published", "kind": "node", "title": "独立核验发布版本", "body": "独立检查实际发布结果与审定版本的一致性",
         "data": {"node_kind": "audit", "inputs": copy.deepcopy(original["outputs"]),
                  "outputs": ["发布核验回执"], "acceptance": copy.deepcopy(original["acceptance"])}, "rationale": "核验原阶段要求"},
        {"id": "audit_to_done", "kind": "edge", "title": "核验通过后交付", "body": "核验通过后进入原固定终点",
         "data": {"from_node": "audit_published", "to_node": "N12B", "condition": "pass"}, "rationale": "保留交付终点"}]}


@pytest.mark.parametrize("evidence", ["saved_graph", "manual", "user_source", "reviewed"])
@pytest.mark.parametrize("shape", ["minimal", "kind", "prose", "wiring", "full"])
def test_locked_edge_can_only_be_changed_by_preview_and_explicit_adoption(tmp_path, evidence, shape):
    store, proposal = setup(tmp_path, evidence)
    before = store.get()["candidate"]
    original = next(e for e in before["document"]["entries"] if e["id"] == "published_to_done")
    edit = proposal["upserts"][0]
    if shape in {"kind", "full"}: edit["kind"] = original["kind"]
    if shape in {"prose", "full"}: edit.update(title=original["title"], body=original["body"])
    if shape in {"wiring", "full"}: edit["data"] = {**original["data"], **edit["data"]}
    changed, applied, rejected = apply_completion(before["document"], {"upserts": proposal["upserts"][:1]})
    assert changed == before["document"] and not applied and rejected[0]["code"] == "locked_content"
    preview = store.preview_graph_repair(None, proposal)
    assert preview["validation"]["valid"] and preview["adoption"]["allowed"]
    assert store.get()["candidate"] == before
    store.adopt_graph_repair(None, preview["id"])
    after = store.get()["candidate"]
    new = {e["id"]: e for e in after["document"]["entries"]}
    for old in before["document"]["entries"]:
        if old["id"] != "published_to_done":
            assert new[old["id"]] == old
        else:
            assert new[old["id"]]["data"] == {**old["data"], "to_node": "audit_published"}
            for key in ("title", "body", "sources", "evidence"):
                assert new[old["id"]][key] == old[key]
    spec = project_graph(after["document"], None, conversation_id="synthetic", revision=1)
    assert spec.finalizer_node_id == "N12B"
    assert spec.node_map()["audit_published"].model is None
    assert spec.node_map()["audit_published"].bind_later is True
    with pytest.raises(ValueError, match="audit_published"):
        validate_spec(spec, require_bindings=True)
    assert readiness(after["document"], None, conversation_id="synthetic", revision=1)["ready"]
    with pytest.raises(ValueError):
        store.preview_graph_repair(None, proposal)


@pytest.mark.parametrize("order", list(itertools.permutations(range(3))))
def test_node_and_edges_are_valid_in_any_proposal_order(tmp_path, order):
    store, proposal = setup(tmp_path)
    proposal["upserts"] = [proposal["upserts"][i] for i in order]
    assert store.preview_graph_repair(None, proposal)["validation"]["valid"]


@pytest.mark.parametrize("fault", ["origin", "condition", "predicate", "branch", "scope", "kind", "model",
    "coordinate", "empty_body", "empty_outputs", "empty_acceptance", "dropped_input", "no_exit", "fail_exit",
    "bypass", "after_final", "duplicate", "forged_sources", "edge_title", "edge_body", "new_null",
    "sources_same", "evidence_same", "state_same", "reviewed_same"])
def test_insertion_rejects_policy_changes_and_incomplete_or_forged_review(tmp_path, fault):
    store, proposal = setup(tmp_path)
    edits = proposal["upserts"]
    if fault == "origin": edits[0]["data"]["from_node"] = "plan"
    elif fault == "condition": edits[0]["data"]["condition"] = "always"
    elif fault == "predicate": edits[0]["data"]["input_when"] = None
    elif fault == "branch": edits[0]["data"]["branch_group"] = "changed"
    elif fault == "scope": edits.append({"id": "publish_choice", "data": {"to_node": "N12B"}, "rationale": "跳过发布"})
    elif fault == "kind": edits[1]["data"]["node_kind"] = "implement"
    elif fault == "model": edits[1]["data"]["model"] = {"provider_key": "unknown", "model_id": "unknown"}
    elif fault == "coordinate": edits[1]["data"]["execution_mode"] = "coordinate"
    elif fault == "empty_body": edits[1]["body"] = ""
    elif fault == "empty_outputs": edits[1]["data"]["outputs"] = []
    elif fault == "empty_acceptance": edits[1]["data"]["acceptance"] = []
    elif fault == "dropped_input": edits[1]["data"]["inputs"] = []
    elif fault == "no_exit": edits.pop()
    elif fault == "fail_exit": edits[2]["data"]["condition"] = "fail"
    elif fault == "bypass": edits[0] = {"id": "side_edge", "kind": "edge", "title": "旁路", "body": "并行审查",
        "data": {"from_node": "N12A", "to_node": "audit_published", "condition": "pass"}, "rationale": "留下原旁路"}
    elif fault == "after_final": edits[0]["data"]["to_node"] = "N12B"
    elif fault == "duplicate": edits.append(copy.deepcopy(edits[1]))
    elif fault == "forged_sources": edits[1]["sources"] = [{"source_id": "s1", "quote": "捏造"}]
    elif fault == "edge_title": edits[0]["title"] = "改名"
    elif fault == "edge_body": edits[0]["body"] = "改职责"
    elif fault == "new_null": edits[0]["data"]["input_when"] = None
    elif fault == "sources_same": edits[0]["sources"] = []
    elif fault == "evidence_same": edits[0]["evidence"] = "saved_graph"
    elif fault == "state_same": edits[0]["state"] = "active"
    elif fault == "reviewed_same": edits[0]["reviewed"] = False
    before = store.get()
    with pytest.raises(ValueError):
        store.preview_graph_repair(None, proposal)
    assert store.get() == before


def test_protected_edge_error_names_entry_and_actual_field(tmp_path):
    store, proposal = setup(tmp_path)
    proposal["upserts"][0]["data"]["condition"] = "always"
    with pytest.raises(ValueError, match=r"published_to_done.*data.condition.*只允许改 to_node"):
        store.preview_graph_repair(None, proposal)


def test_no_change_explanation_is_visible_but_cannot_be_adopted(tmp_path):
    store, proposal = setup(tmp_path)
    before = store.get()["candidate"]
    proposal.update(summary="需要用户明确一个业务取舍，当前没有结构修复提案", upserts=[])
    preview = store.preview_graph_repair(None, proposal)
    assert preview["changes"] == [] and not preview["adoption"]["allowed"]
    assert preview["summary"] == proposal["summary"]
    assert store.get()["candidate"] == before
    with pytest.raises(ValueError, match="没有可采用"):
        store.adopt_graph_repair(None, preview["id"])
    store.request_local_graph_repair(None)
    with pytest.raises(ValueError, match="重签发"):
        store.preview_graph_repair(None, proposal)


@pytest.mark.parametrize("include_kind", [False, True])
def test_unlocked_edge_still_reuses_existing_audit(tmp_path, include_kind):
    bypass = edge("bypass", "work", "finish")
    bypass["evidence"] = "proposal"
    doc = document(node("plan", "plan"), node("work", "implement"), node("audit", "audit"), node("finish", "reduce"),
                   edge("p_w", "plan", "work"), edge("p_a", "plan", "audit"), edge("a_f", "audit", "finish"), bypass)
    store = DraftStore(tmp_path, "synthetic")
    ready(store, [{"id": "s1", "role": "user", "text": "审查后交付"}], doc)
    packet = store.request_local_graph_repair(None)
    edit = {"id": "bypass", "data": {"to_node": "audit"}, "rationale": "复用已有审查"}
    if include_kind:
        edit["kind"] = "edge"
    preview = store.preview_graph_repair(None, {"schema": SCHEMA, "ticket": packet["ticket"],
        "summary": "复用已有审查", "upserts": [edit]})
    assert preview["validation"]["valid"]
    assert len(preview["changes"]) == 1
