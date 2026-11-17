"""Capability declaration failures stay visible and repairable in graph flows."""
import copy

import httpx
import pytest

from frontend import server
from mewcode.kernel.capabilities import declaration
from mewcode.orchestration.draft_store import DraftStore
from mewcode.orchestration.workflow_blocks import WorkflowBlocks
from mewcode.orchestration.workflow_draft import (
    DraftPatchError, entry_contract, project_graph, seed_document, validate_entry,
)
from tests.test_workflow_drafts import api_env, record, write_history


API = "/api/orchestration/graph-generation"


def _capability_node(blockers, resolvers=None):
    return {
        "id": "inspect_ocr_capability",
        "kind": "node",
        "title": "核查 OCR 能力",
        "body": "调查现有适配与凭据引用，将核查结果写入记录。",
        "data": {
            "node_kind": "implement",
            "inputs": ["扫描件"],
            "outputs": ["适配核查记录"],
            "acceptance": ["记录已有适配与凭据引用"],
            "steps": ["调查现有适配与凭据引用"],
            "capabilities": {"possible_blockers": blockers, "resolvers": resolvers or {}},
        },
        "sources": [],
    }


def test_capability_declaration_reports_unknown_blocker_fields_as_value_errors():
    with pytest.raises(ValueError, match=r"possible_blockers contains unknown blocker"):
        declaration({"possible_blockers": ["OCR与embedding无专用注册工具；需调查已有适配与凭据引用"]})

    with pytest.raises(ValueError, match=r"resolvers contains unknown blocker"):
        declaration({"resolvers": {"调查已有OCR适配": "human"}})


def test_capability_declaration_keeps_canonical_and_legacy_blocker_compatibility():
    result = declaration({
        "possible_blockers": ["capability_missing", "permission_denied"],
        "resolvers": {"permission_denied": "human"},
    })

    assert result["possible_blockers"] == ["capability_missing", "permission"]
    assert result["resolvers"] == {"permission": "human"}


def test_workflow_entry_reports_invalid_resolver_key_with_contract_hint():
    raw = _capability_node([])
    raw["data"]["capabilities"]["resolvers"] = {"调查已有 OCR 适配": "human"}

    with pytest.raises(DraftPatchError) as caught:
        validate_entry(raw)

    diagnostic = caught.value.diagnostic
    assert diagnostic["entry_id"] == raw["id"]
    assert diagnostic["path"] == "data.capabilities.resolvers"
    assert diagnostic["code"] == "invalid_capability_resolver"
    assert "node.body" in diagnostic["hint"]
    capabilities = entry_contract()["field_types"]["node"]["capabilities"]
    blockers = capabilities["possible_blockers"]["values"]
    assert "permission" in blockers and "capability_missing" in blockers
    assert capabilities["resolvers"]["keys"] == blockers


def test_submit_invalid_capability_is_pending_until_corrected_and_then_projects(tmp_path):
    project = tmp_path / "project"
    source_path = project / ".mewcode" / "sessions" / "chat.jsonl"
    write_history(source_path, [record("user", "用户要求：识别扫描件并调查 OCR 与 embedding 的现有适配。")])
    blocks = WorkflowBlocks(project, "chat", source_path=source_path, base_spec=lambda: None)
    snapshot = blocks.read()
    user_source = blocks.read(source_ids=[snapshot["source_index"][0]["id"]])["sources"][0]
    bad = _capability_node(["OCR与embedding无专用注册工具；需调查已有适配与凭据引用"])
    bad["sources"] = [{"source_id": user_source["id"], "quote": user_source["text"]}]

    receipt = blocks.submit(expected_sequence=snapshot["sequence"], source_hash=snapshot["source_hash"],
                            base_graph_hash=snapshot["base_graph_hash"], upserts=[bad], ignored=[],
                            turn_id="invalid-capability")
    issue, = [item for item in receipt["pending"] if item.get("entry_id") == bad["id"]]
    assert issue["code"] == "invalid_capability_blocker"
    assert issue["path"] == "data.capabilities.possible_blockers"
    assert "entry_contract" in issue["hint"]

    state = DraftStore(project, "chat").get()["materials"]
    assert any(item["id"] == bad["id"] for item in state["pending_upserts"])
    assert not any(item["id"] == bad["id"] for item in state["document"]["entries"])

    next_snapshot = blocks.read()
    corrected = copy.deepcopy(bad)
    corrected["data"]["capabilities"] = {
        "possible_blockers": ["capability_missing"],
        "resolvers": {"capability_missing": "human"},
    }
    receipt = blocks.submit(expected_sequence=next_snapshot["sequence"], source_hash=next_snapshot["source_hash"],
                            base_graph_hash=next_snapshot["base_graph_hash"], upserts=[corrected], ignored=[],
                            turn_id="corrected-capability")
    assert "inspect_ocr_capability" in receipt["saved_entry_ids"]
    assert not any(item.get("entry_id") == corrected["id"] for item in receipt["pending"])

    settled = DraftStore(project, "chat").get()["materials"]
    assert not any(item["id"] == corrected["id"] for item in settled["pending_upserts"])
    projected = project_graph(settled["document"], None, conversation_id="chat", revision=1).as_dict()
    node, = [item for item in projected["nodes"] if item["node_id"] == corrected["id"]]
    assert node["capabilities"]["possible_blockers"] == ["capability_missing"]


@pytest.mark.asyncio
async def test_graph_generation_get_keeps_legacy_invalid_node_visible_and_unsaveable(api_env):
    _, _, _, owner = api_env
    spec = {
        "spec_id": "legacy-capability",
        "label": "旧图",
        "goal": "检查 OCR 适配",
        "nodes": [{"node_id": "inspect", "label": "检查 OCR 适配", "kind": "implement",
                   "goal": "调查已有适配与凭据引用。",
                   "plan_brief": {"steps": ["调查已有适配"], "outputs": ["适配记录"],
                                  "acceptance": ["记录可核对"]}}],
        "edges": [],
        "finalizer_node_id": "inspect",
        "meta": {},
    }
    document = seed_document(spec)
    node = next(item for item in document["entries"] if item["id"] == "inspect")
    node["data"]["capabilities"] = {
        "possible_blockers": ["OCR与embedding无专用注册工具；需调查已有适配与凭据引用"]
    }
    store = DraftStore(owner["project"], "paper")
    with store.db() as db:
        state = store._get(db)
        state["document"] = document
        state["revision"] = 1
        store._put(db, state)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        response = await client.get(API, params=owner)

    assert response.status_code == 200, response.text
    result = response.json()
    assert any(item["node_id"] == "inspect" for item in result["view"]["nodes"])
    assert not result["can_preview"]
    assert not result["actions"]["save"]["enabled"]
    assert result["readiness"]["ready"] is False
    assert any("possible_blockers" in issue and "OCR与embedding" in issue for issue in result["issues"])
