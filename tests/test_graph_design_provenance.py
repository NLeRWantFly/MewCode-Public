"""Legacy design cleanup is evidence-bound, previewed, and reversible."""
import copy

import httpx
import pytest

from mewcode.orchestration.draft_store import DraftStore
from mewcode.orchestration.graph_completion import apply_completion
from mewcode.orchestration.graph_design_provenance import unsupported_designs
from mewcode.orchestration.graph_generation import delivery_view, graph_task_state
from tests.test_graph_autofix import entry, graph
from tests.test_graph_local_repair import prepare
from tests.test_workflow_drafts import api_env


def add_design(document, resolves_question_id=None):
    raw = {"id": "old_design", "kind": "decision", "title": "旧顺序说明", "body": "采用未生效的旁路审查",
           "data": {"category": "designed", "value": "执行 → 汇总 → 审查"}, "rationale": "旧版本遗留"}
    if resolves_question_id:
        raw["data"]["resolves_question_id"] = resolves_question_id
    updated, applied, rejected = apply_completion(document, {"upserts": [raw]})
    assert applied and not rejected
    row = {"proposal": {"upserts": [raw]},
           "structural": {"applied": [{"entry_id": "new_edge"}], "adopted": False, "rejected": []},
           "ledger": {"applied": [{"entry_id": "old_design", "code": "decision_added"}], "adopted": True}}
    return updated, {"phase": "done", "status": "needs_review", "autofix": {"rounds": [row]}}


def test_unchanged_added_design_and_unbroken_update_chain_are_identified():
    doc, build = add_design(graph(complete=True))
    assert [e["id"] for e in unsupported_designs(doc, build)] == ["old_design"]
    update = {"id": "old_design", "body": "修订但结构再次拒绝", "rationale": "再次尝试"}
    changed, applied, rejected = apply_completion(doc, {"upserts": [update]})
    assert applied and not rejected
    row = copy.deepcopy(build["autofix"]["rounds"][0])
    row["proposal"]["upserts"] = [update]
    row["ledger"]["applied"][0]["code"] = "own_entry_updated"
    build["autofix"]["rounds"].append(row)
    assert unsupported_designs(changed, build)[0]["body"] == update["body"]
    assert delivery_view(changed, build)["assumptions"][0]["unsupported"]
    # Losing the original add receipt cannot authorize retirement of an update.
    build["autofix"]["rounds"].pop(0)
    assert unsupported_designs(changed, build) == []


@pytest.mark.parametrize("change", [
    {"reviewed": True}, {"evidence": "user_source"}, {"sources": [{"source_id": "user", "quote": "事实"}]},
    {"body": "用户更正"}, {"state": "retired"}, {"reclassified_from": "manual"}, {"reviewed": False},
])
def test_any_provenance_or_content_change_prevents_withdrawal(change):
    doc, build = add_design(graph(complete=True))
    next(e for e in doc["entries"] if e["id"] == "old_design").update(change)
    assert unsupported_designs(doc, build) == []


@pytest.mark.parametrize("later", ["successful", "rejected", "duplicate"])
def test_later_touch_or_ambiguous_receipt_prevents_withdrawal(later):
    doc, build = add_design(graph(complete=True))
    row = copy.deepcopy(build["autofix"]["rounds"][0])
    if later == "duplicate":
        row["proposal"]["upserts"] *= 2
    elif later == "successful":
        row["structural"]["adopted"] = True
    else:
        row["ledger"]["adopted"] = False
    build["gap_fill"] = {"rounds": [row]}
    assert unsupported_designs(doc, build) == []


def test_history_containers_preserve_chronological_provenance():
    doc, build = add_design(graph(complete=True))
    for wrapped in ({"previous_attempts": [build]}, {"gap_fill_history": [build["autofix"]]}):
        assert unsupported_designs(doc, wrapped)[0]["id"] == "old_design"


def test_interleaved_phase_history_without_global_sequence_cannot_authorize_cleanup():
    doc, build = add_design(graph(complete=True))
    added = build["autofix"]["rounds"][0]
    successful = copy.deepcopy(added)
    successful["structural"]["adopted"] = True
    successful["ledger"]["applied"][0]["code"] = "own_entry_updated"
    update = copy.deepcopy(added)
    update["ledger"]["applied"][0]["code"] = "own_entry_updated"
    # Actual order may be gap-add, resumed autofix-success, then gap-update.
    build.update(autofix={"rounds": [successful]}, gap_fill_history=[{"rounds": [added]}], gap_fill={"rounds": [update]})
    assert unsupported_designs(doc, build) == []


@pytest.mark.parametrize("malformed", [{"id": []}, {"id": {}}, 42, None])
def test_rejected_malformed_model_history_does_not_break_graph_reads(malformed):
    doc, build = add_design(graph(complete=True))
    build["autofix"]["rounds"].append({"proposal": {"upserts": [malformed]}, "structural": {
        "adopted": False, "rejected": [{"entry_id": [], "code": "not_whitelisted"}]}})
    assert unsupported_designs(doc, build)[0]["id"] == "old_design"
    assert delivery_view(doc, build)["assumptions"]


@pytest.mark.parametrize("old_phase", ["done", "review"])
def test_preview_adoption_retires_only_proven_design_and_updates_task(tmp_path, old_phase):
    store = DraftStore(tmp_path, "paper")
    proposal = prepare(store, [{"id": "s1", "role": "user", "text": "保持用户要求"}])
    c = store.get()["candidate"]
    doc, build = add_design(c["document"])
    build.update(phase=old_phase, reason="request_too_large")
    c = store.checkpoint(c, doc, c["processed_ids"], "ready", autobuild=build)
    packet = store.request_local_graph_repair(None)
    proposal["ticket"] = packet["ticket"]
    before = store.get()["candidate"]
    preview = store.preview_graph_repair(None, proposal)
    assert preview["validation"]["valid"] and preview["adoption"]["allowed"]
    assert store.get()["candidate"] == before
    retired = next(e for e in preview["changes"] if e["entry_id"] == "old_design")
    assert retired["after"] == {**retired["before"], "state": "retired"}
    after = store.adopt_graph_repair(None, preview["id"])
    assert graph_task_state(after, False)["adopted"]
    assert after["candidate"]["autobuild"]["phase"] == "done"
    assert delivery_view(after["candidate"]["document"], after["candidate"]["autobuild"])["assumptions"] == []
    with pytest.raises(ValueError):
        store.adopt_graph_repair(None, preview["id"])
    c = after["candidate"]
    store.edit(c["id"], c["sequence"], "old_design", {"state": "active"})
    assert not graph_task_state(store.get(), False).get("adopted")


def test_saved_build_without_legacy_phase_does_not_look_interrupted():
    assert graph_task_state({"autobuild_result": {"status": "ready"}}, False)["status"] == "ready"


def test_withdrawing_design_reopens_its_technical_question():
    doc = graph(complete=True)
    doc["entries"].append(entry("technical", "question", data={"issue_type": "technical", "blocking": False}))
    doc, build = add_design(doc, "technical")
    assert not delivery_view(doc, build)["pending"]
    next(e for e in doc["entries"] if e["id"] == "old_design")["state"] = "retired"
    view = delivery_view(doc, build)
    assert [item["id"] for item in view["pending"]] == ["technical"]
    assert [item["id"] for item in view["issue_groups"]] == ["technical"]


@pytest.mark.asyncio
async def test_ready_graph_cleanup_preview_does_not_need_another_model_call(api_env, monkeypatch):
    from frontend import orch_drafts
    from mewcode.orchestration.draft_sources import read_sources
    from tests.test_graph_autobuild import make_app
    _, _, path, owner = api_env
    store = DraftStore(owner["project"], "paper")
    proposal = prepare(store, read_sources(path)[0])
    fixed = store.preview_graph_repair(None, proposal)
    store.adopt_graph_repair(None, fixed["id"])
    c = store.get()["candidate"]
    doc, build = add_design(c["document"])
    before = store.checkpoint(c, doc, c["processed_ids"], "ready", autobuild=build)
    async def forbidden(*args, **kwargs):
        raise AssertionError("Proven history cleanup must not wait for a model")
    monkeypatch.setattr(orch_drafts, "collect_model_text", forbidden)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app()), base_url="http://test") as client:
        response = await client.post("/api/orchestration/graph-generation/repair", json={**owner, "action": "local"})
        assert response.status_code == 200, response.text
        preview = response.json()
        assert [row["entry_id"] for row in preview["changes"]] == ["old_design"]
        assert preview["adoption"]["allowed"]
        assert store.get()["candidate"] == before
