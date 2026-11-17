"""Duplicate-write, repair, adoption and projection regression (offline only)."""
import copy
import json
from pathlib import Path

import httpx
import pytest

from frontend import orch_drafts
from mewcode.orchestration.draft_dedupe import apply_dedupe
from mewcode.orchestration.draft_keys import duplicate_groups
from mewcode.orchestration.draft_sources import read_sources
from mewcode.orchestration.draft_store import DraftStore
from mewcode.orchestration.graph_completion import apply_completion
from mewcode.orchestration.graph_readiness import readiness
from mewcode.orchestration.graph_repair import SCHEMA
from mewcode.orchestration.workflow_draft import apply_patch, project_graph, seed_document, validate_entry
from tests.test_generic_loops import spec_for
from tests.test_graph_autobuild import make_app
from tests.test_graph_autofix import FakeModel, entry, graph, proposal, run
from tests.test_workflow_drafts import api_env, ready

API = "/api/orchestration/graph-generation"
NAMES = ["pdf_paths", "new_output_directory", "model_bindings", "content_external_authorization",
         "model_cost_policy", "enable_vector_search", "publication_choice", "hierarchy_label_interpretation", "embedding_binding"]


def broken(*, diverge=False):
    doc = seed_document(spec_for().as_dict())
    for e in doc["entries"]:
        if e["kind"] == "node":
            e["data"].update(outputs=["产物"], acceptance=["核验当前项"])
    for index, name in enumerate(NAMES):
        a = entry(f"input_{index}", "input", data={"name": name, "required": True}, evidence="saved_graph")
        b = {**copy.deepcopy(a), "id": f"copy_{index}", "evidence": "proposal"}
        if diverge and index == 0:
            b["data"]["required"] = False
        doc["entries"].extend([a, b])
    for e in list(doc["entries"]):
        if e["kind"] in {"loop", "edge"}:
            doc["entries"].append({**copy.deepcopy(e), "id": "duplicate_" + e["id"], "evidence": "proposal"})
    return doc


def store_for(tmp_path, doc):
    store = DraftStore(tmp_path, "paper")
    ready(store, [{"id": "s1", "role": "user", "text": "保留业务要求"}], doc)
    return store


def test_all_duplicate_groups_are_located_and_losslessly_retired(tmp_path):
    doc = broken()
    original = copy.deepcopy(doc)
    result = readiness(doc, None, conversation_id="paper", revision=1)
    diagnostics = [d for d in result["diagnostics"] if d["code"] == "duplicate_business_key"]
    assert len(diagnostics) == 14 and all(len(d["entry_ids"]) == 2 for d in diagnostics)
    assert doc == original and not result["ready"]
    store = store_for(tmp_path, doc)
    preview = store.preview_graph_dedupe(None)
    assert preview["adoption"]["allowed"] and len(preview["changes"]) == 14
    assert store.get()["candidate"]["document"] == original
    store.adopt_graph_repair(None, preview["id"])
    merged = store.get()["candidate"]["document"]
    assert not duplicate_groups(merged)
    project_graph(merged, None, conversation_id="paper", revision=1)
    originals = {e["id"]: e for e in original["entries"]}
    for e in merged["entries"]:
        assert e["data"] == originals[e["id"]]["data"]
        assert e["sources"] == originals[e["id"]]["sources"]
        if e["state"] == "retired":
            assert e["superseded_by"] in originals


def test_reported_duplicate_counts_fixture_is_fully_repairable(tmp_path):
    # Synthetic, desensitized topology reproduces the reported counts. No live
    # conversation, file paths or database contents are committed to this test.
    doc = json.loads((Path(__file__).parent / "fixtures/broken_draft_duplicates.json").read_text(encoding="utf-8"))
    assert len([e for e in doc["entries"] if e["kind"] == "edge"]) == 79
    groups = duplicate_groups(doc)
    assert len(groups) == 50
    check = readiness(doc, None, conversation_id="paper", revision=1)
    assert len([d for d in check["diagnostics"] if d["code"] == "duplicate_business_key"]) == 50
    store = store_for(tmp_path, doc)
    preview = store.preview_graph_dedupe(None)
    assert len(preview["changes"]) == 50 and preview["adoption"]["allowed"]
    store.adopt_graph_repair(None, preview["id"])
    merged = store.get()["candidate"]["document"]
    assert not duplicate_groups(merged)
    spec = project_graph(merged, None, conversation_id="paper", revision=1)
    assert len(spec.edges) == 41 and len(spec.meta["loops"]) == 3


def test_reseed_preserves_retirement_audit_and_does_not_duplicate(tmp_path):
    doc, _, _ = apply_dedupe(broken())
    spec = project_graph(doc, None, conversation_id="paper", revision=1).as_dict()
    reseeded = seed_document(spec, previous=doc)
    retired = {e["id"]: e for e in doc["entries"] if e["state"] == "retired"}
    assert {e["id"]: e for e in reseeded["entries"] if e["state"] == "retired"} == retired
    store = store_for(tmp_path, doc)
    store.rebase(store.get()["candidate"], spec)
    assert not duplicate_groups(store.get()["candidate"]["document"])


def test_disagreement_needs_choice_and_stale_preview_is_rejected(tmp_path):
    store = store_for(tmp_path, broken(diverge=True))
    preview = store.preview_graph_dedupe(None)
    assert not preview["adoption"]["allowed"] and len(preview["unresolved"]) == 1
    with pytest.raises(ValueError, match="选择"):
        store.adopt_graph_repair(None, preview["id"])
    group = preview["unresolved"][0]
    selected = store.preview_graph_dedupe(None, {group["id"]: "copy_0"})
    with pytest.raises(ValueError, match="失效"):
        store.adopt_graph_repair(None, preview["id"])
    store.adopt_graph_repair(None, selected["id"])
    assert next(e for e in store.get()["candidate"]["document"]["entries"] if e["id"] == "copy_0")["data"]["required"] is False


def test_dedupe_keeps_source_union_and_partial_adoption(tmp_path):
    doc = broken()
    next(e for e in doc["entries"] if e["id"] == "work")["state"] = "conflict"
    inputs = [e for e in doc["entries"] if e["id"] in {"input_0", "copy_0"}]
    for i, e in enumerate(inputs):
        e["sources"] = [{"source_id": "s1", "quote": f"来源{i}"}]
    store = store_for(tmp_path, doc)
    preview = store.preview_graph_dedupe(None)
    assert preview["adoption"]["allowed"] and preview["adoption"]["partial"]
    store.adopt_graph_repair(None, preview["id"])
    survivor = next(e for e in store.get()["candidate"]["document"]["entries"] if e["id"] == "input_0")
    assert survivor["sources"] == inputs[0]["sources"] + inputs[1]["sources"]


def test_chat_reissued_ticket_can_retire_inputs_and_migrate_references(tmp_path):
    doc = broken()
    doc["entries"].append(entry("question", "question", data={"input_id": "copy_0"}))
    store = store_for(tmp_path, doc)
    first = store.request_graph_repair(None)
    second = store.request_graph_repair(None)
    assert set(["input_0", "copy_0"]).issubset(second["entry_ids"])
    change = {"entry_id": "copy_0", "reason": "保留相同用户定义", "changes": {"state": "retired", "superseded_by": "input_0"}}
    p = {"schema": SCHEMA, "ticket": first["ticket"], "summary": "合并重复参数", "changes": [change]}
    with pytest.raises(ValueError, match="重签发"):
        store.preview_graph_repair(None, p)
    p["ticket"] = second["ticket"]
    preview = store.preview_graph_repair(None, p)
    assert preview["adoption"]["allowed"] and preview["adoption"]["partial"]
    store.adopt_graph_repair(None, preview["id"])
    assert next(e for e in store.get()["candidate"]["document"]["entries"] if e["id"] == "question")["data"]["input_id"] == "input_0"


@pytest.mark.parametrize("kind,data", [("input", {"name": "pdf_paths"}), ("loop", {"loop_id": "L1"}),
                                       ("edge", {"from_node": "plan", "to_node": "audit"})])
def test_extraction_rejects_duplicate_business_key_atomically(kind, data):
    doc = graph(complete=True)
    if kind == "edge":
        old = next(e for e in doc["entries"] if e["id"] == "first")
    else:
        old = entry("original", kind, data=data)
        doc["entries"].append(old)
    before = copy.deepcopy(doc)
    source = {"id": "s", "role": "user", "text": "保留定义"}
    raw = {**copy.deepcopy(old), "id": "copy", "sources": [{"source_id": "s", "quote": "保留定义"}]}
    with pytest.raises(ValueError, match="duplicate_business_key"):
        apply_patch(doc, {"upserts": [raw], "ignored": []}, [source])
    assert doc == before


def test_completion_cannot_introduce_duplicate_parameter():
    doc = graph(complete=True)
    doc["entries"].append(entry("original", "input", data={"name": "pdf_paths"}))
    raw = entry("copy", "input", data={"name": "pdf_paths"})
    raw.update(rationale="从来源补全参数")
    result, applied, rejected = apply_completion(doc, {"upserts": [raw]})
    assert not applied and result == doc
    assert rejected[0]["code"] in {"duplicate_business_key", "duplicate_id"}


def test_foreach_draft_projects_without_losing_items():
    doc = seed_document(spec_for().as_dict())
    loop = next(e for e in doc["entries"] if e["kind"] == "loop")
    loop["data"].update(mode="foreach", items=[{"item_id": "batch1", "goal": "核验第1–10页"}])
    validate_entry(loop)
    spec = project_graph(doc, None, conversation_id="paper", revision=1)
    assert spec.meta["loops"][0]["mode"] == "foreach"
    assert spec.meta["loops"][0]["items"] == loop["data"]["items"]
    loop["data"]["items"].append(copy.deepcopy(loop["data"]["items"][0]))
    with pytest.raises(ValueError, match="foreach"):
        validate_entry(loop)


@pytest.mark.asyncio
async def test_autofix_dedupes_before_model_and_preserves_locked_content():
    doc = graph(complete=True)
    a = entry("original", "input", data={"name": "pdf_paths"}, evidence="saved_graph")
    b = {**copy.deepcopy(a), "id": "copy"}
    doc["entries"].extend([a, b])
    model = FakeModel(proposal())
    result = await run(doc, model)
    assert result["host_dedupe"] and not duplicate_groups(result["document"])
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_one_click_dedupe_api_never_calls_model_and_rechecks_scope(api_env, monkeypatch):
    current, plans, path, owner = api_env
    store = DraftStore(owner["project"], "paper")
    sources, _, _ = read_sources(path)
    ready(store, sources, broken())
    async def forbidden(*args, **kwargs):
        pytest.fail("duplicate repair must not invoke a provider")
    monkeypatch.setattr(orch_drafts, "collect_model_text", forbidden)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app()), base_url="http://test") as client:
        response = await client.post(API + "/repair", json={**owner, "action": "local"})
        assert response.status_code == 200, response.text
        preview = response.json()
        assert preview["dedupe"] and preview["adoption"]["allowed"]
        current["cid"] = "different"
        refused = await client.post(API + "/repair", json={**owner, "action": "adopt", "preview_id": preview["id"]})
        assert refused.status_code == 409
        current["cid"] = "paper"
        adopted = await client.post(API + "/repair", json={**owner, "action": "adopt", "preview_id": preview["id"]})
        assert adopted.status_code == 200, adopted.text
        assert not plans.get(owner["project"])
