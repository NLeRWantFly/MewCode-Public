"""API integration checks for workflow materials and graph discussion."""
import asyncio
import copy
import json

import httpx
import pytest

from frontend import orch_drafts, server
from mewcode.orchestration.draft_store import DraftStore
from mewcode.orchestration.workflow_blocks import WorkflowBlocks, freeze_materials
from mewcode.orchestration.workflow_draft import entry_contract
from tests.test_workflow_drafts import api_env, model_patch, record, write_history
from tests.test_graph_generation import API as GRAPH_API, poll as poll_graph


AUTOBUILD_API = "/api/orchestration/graph-autobuild"


def block_node(entry_id, title, body, source):
    return {"id": entry_id, "kind": "node", "title": title, "body": body,
            "state": "active", "data": {"node_kind": "implement", "inputs": ["原始材料"],
                "outputs": ["处理结果"], "acceptance": ["保留原始要求"], "steps": ["逐项处理"]},
            "sources": [{"source_id": source["id"], "quote": source["text"]}]}


@pytest.mark.asyncio
async def test_extraction_receives_host_entry_contract_and_frozen_materials_refresh_only_on_include_new(api_env, monkeypatch):
    _, _, path, owner = api_env
    write_history(path, [record("user", "请整理原始资料并保留逐项核对。")])
    blocks = WorkflowBlocks(owner["project"], owner["conversation_id"], source_path=path)
    first_read = blocks.read()
    first_source = next(row for row in first_read["source_index"] if row["role"] == "user")
    first_source = blocks.read(source_ids=[first_source["id"]])["sources"][0]
    first_receipt = blocks.submit(expected_sequence=first_read["sequence"], source_hash=first_read["source_hash"],
        base_graph_hash=first_read["base_graph_hash"],
        upserts=[block_node("workflow_index", "整理资料索引", "先建立资料索引。", first_source)], ignored=[], turn_id="initial-material")

    calls, entered, release = [], asyncio.Event(), asyncio.Event()
    async def model(client, **kwargs):
        payload = json.loads(kwargs["user_text"])
        calls.append(payload)
        assert payload["entry_contract"] == entry_contract()
        if len(calls) == 1:
            assert payload["workflow_materials"]["sequence"] == first_receipt["sequence"]
            assert "workflow_index" in {row["id"] for row in payload["document"]["entries"]}
            entered.set()
            await release.wait()
        else:
            assert payload["workflow_materials"]["sequence"] == second_receipt["sequence"]
            assert "workflow_review" in {row["id"] for row in payload["document"]["entries"]}
        return json.dumps(model_patch(payload), ensure_ascii=False)

    monkeypatch.setattr(orch_drafts, "collect_model_text", model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        started = await client.post(GRAPH_API + "/update", json=owner)
        assert started.status_code == 202, started.text
        store = DraftStore(owner["project"], owner["conversation_id"])
        try:
            await asyncio.wait_for(entered.wait(), 3)
            frozen = store.get()["candidate"]
            assert frozen["materials_snapshot"]["sequence"] == first_receipt["sequence"]

            latest = blocks.read()
            user_ref = next(row for row in latest["source_index"] if row["role"] == "user")
            user_ref = blocks.read(source_ids=[user_ref["id"]])["sources"][0]
            second_receipt = blocks.submit(expected_sequence=latest["sequence"], source_hash=latest["source_hash"],
                base_graph_hash=latest["base_graph_hash"],
                upserts=[block_node("workflow_review", "复核资料索引", "再逐项复核索引。", user_ref)],
                ignored=[{"source_id": first_source["id"], "reason": "本轮未重复提交已有素材"}], turn_id="later-material")
            assert second_receipt["sequence"] == first_receipt["sequence"] + 1
            in_flight = store.get()["candidate"]
            assert in_flight["materials_snapshot"]["sequence"] == first_receipt["sequence"]
            assert "workflow_review" not in {row["id"] for row in in_flight["document"]["entries"]}
            assert "workflow_review" not in json.dumps(calls[0], ensure_ascii=False)
        finally:
            release.set()

        state = await poll_graph(client, owner)
        assert state["can_preview"] and len(calls) == 1
        refreshed = await client.post(GRAPH_API + "/update", json={**owner, "include_new": True})
        assert refreshed.status_code == 202, refreshed.text
        state = await poll_graph(client, owner)
        assert state["can_preview"] and len(calls) == 2
        candidate = store.get()["candidate"]
        assert candidate["materials_snapshot"]["sequence"] == second_receipt["sequence"]
        assert "workflow_review" in {row["id"] for row in candidate["document"]["entries"]}


@pytest.mark.asyncio
async def test_include_new_refreshes_material_with_retained_original_citation(api_env, monkeypatch):
    _, _, path, owner = api_env
    write_history(path, [record("user", "整理原始资料，建立索引并逐项核对。")])
    blocks = WorkflowBlocks(owner["project"], owner["conversation_id"], source_path=path)
    snapshot = blocks.read()
    source_id = next(row["id"] for row in snapshot["source_index"] if row["role"] == "user")
    source = blocks.read(source_ids=[source_id])["sources"][0]

    def save(body):
        view = blocks.read()
        return blocks.submit(expected_sequence=view["sequence"], source_hash=view["source_hash"],
            base_graph_hash=view["base_graph_hash"], ignored=[],
            upserts=[block_node("workflow_index", "整理资料索引", body, source)], turn_id=body)

    save("建立资料索引。")

    async def model(client, **kwargs):
        return json.dumps(model_patch(json.loads(kwargs["user_text"])), ensure_ascii=False)

    monkeypatch.setattr(orch_drafts, "collect_model_text", model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        assert (await client.post(GRAPH_API + "/update", json=owner)).status_code == 202
        await poll_graph(client, owner)
        store = DraftStore(owner["project"], owner["conversation_id"])
        original_ref = {"source_id": source_id, "quote": source["text"]}
        # Imported/extracted entries can retain an original citation alongside
        # the payload that a main-conversation material submission appends.
        with store.db() as db:
            state = store._get(db)
            entry = next(e for e in state["materials"]["document"]["entries"] if e["id"] == "workflow_index")
            entry["sources"].insert(0, original_ref)
            store._put(db, state)
        changed = save("建立资料索引，并逐项核对文件名。")
        assert not changed["pending"]
        response = await client.post(GRAPH_API + "/update", json={**owner, "include_new": True})
        assert response.status_code == 202, response.text
        await poll_graph(client, owner)
        candidate = store.get()["candidate"]
        refreshed = next(e for e in candidate["document"]["entries"] if e["id"] == "workflow_index")
        assert refreshed["body"] == "建立资料索引，并逐项核对文件名。"
        assert original_ref in refreshed["sources"]
        assert candidate["history_basis"]["entry_field_evidence"]["workflow_index"]["body"] == "proposal"
        assert candidate["materials_snapshot"]["sequence"] == changed["sequence"]


@pytest.mark.asyncio
async def test_workflow_materials_partial_schema_failure_and_retry_keep_independent_entry(api_env):
    _, _, path, owner = api_env
    write_history(path, [record("user", "请准备本地 OCR 硬件评估节点。"),
                         record("assistant", "建议先建立评估节点，再比较硬件方案。")])
    blocks = WorkflowBlocks(owner["project"], owner["conversation_id"], source_path=path)
    snapshot = blocks.read()
    sources = {row["id"]: row for row in blocks.read(source_ids=[row["id"] for row in snapshot["source_index"]])["sources"]}
    user = next(row for row in sources.values() if row["role"] == "user")
    assistant = next(row for row in sources.values() if row["role"] == "assistant")
    node = block_node("hardware_assessment", "评估本地 OCR 硬件", "梳理本地 OCR 的硬件要求。", user)
    bad = {"id": "hardware_choice", "kind": "decision", "title": "本地 OCR 硬件方案",
        "body": "把硬件方案留作设计建议。", "state": "active",
        "data": {"category": "designed", "value": "待评估", "options": ["A", "B"],
                 "node_id": "hardware_assessment"},
        "sources": [{"source_id": assistant["id"], "quote": assistant["text"]}]}

    receipt = blocks.submit(expected_sequence=snapshot["sequence"], source_hash=snapshot["source_hash"],
        base_graph_hash=snapshot["base_graph_hash"], upserts=[node, bad], ignored=[], turn_id="partial-material")
    assert receipt["saved_entry_ids"] == ["hardware_assessment"]
    assert any(row["entry_id"] == "hardware_choice" and row["code"] == "unsupported_data_field"
               for row in receipt["pending"])
    materials = DraftStore(owner["project"], owner["conversation_id"]).get()["materials"]
    assert [row["id"] for row in materials["document"]["entries"]] == ["hardware_assessment"]
    assert materials["requirement_sources"]["hardware_assessment"] == [{"source_id": user["id"], "quote": user["text"]}]
    assert any(row["id"] == "hardware_choice" for row in materials["pending_upserts"])
    assert materials["pending"][0]["code"] == "unsupported_data_field"

    retry_view = blocks.read()
    corrected = copy.deepcopy(bad)
    corrected["data"] = {"category": "designed", "value": "待评估", "node_id": "hardware_assessment"}
    retry = blocks.submit(expected_sequence=retry_view["sequence"], source_hash=retry_view["source_hash"],
        base_graph_hash=retry_view["base_graph_hash"], upserts=[corrected], ignored=[], turn_id="partial-material-retry")
    assert retry["saved_entry_ids"] == ["hardware_choice"]
    assert not retry["pending"]
    saved = DraftStore(owner["project"], owner["conversation_id"]).get()["materials"]
    assert {row["id"] for row in saved["document"]["entries"]} == {"hardware_assessment", "hardware_choice"}
    assert saved["requirement_sources"]["hardware_choice"] == [{"source_id": assistant["id"], "quote": assistant["text"]}]
    assert not saved["pending_upserts"]


@pytest.mark.asyncio
async def test_discussion_material_patch_uses_real_user_turn_and_host_receipt_without_adoption(api_env, monkeypatch):
    from tests.test_graph_discussion import prepare

    _, plans, _, owner = api_env
    prompts = []
    async def model(client, **kwargs):
        payload = json.loads(kwargs["user_text"])
        prompts.append(payload)
        assert payload["workflow_materials"]["contract"] == entry_contract()
        assert {row["id"] for row in payload["workflow_materials"]["document"]["entries"]} >= {"plan", "audit", "finish"}
        turn = next(row for row in reversed(payload["messages"]) if row["role"] == "user")
        return json.dumps({"reply": "我已把节点保存为待评估素材。", "proposal": None,
            "materials": {"upserts": [block_node("ocr_setup", "准备 OCR 服务", "整理服务接入参数。",
                {"id": turn["id"], "text": turn["text"]})], "ignored": []}}, ensure_ascii=False)

    monkeypatch.setattr(orch_drafts, "collect_model_text", model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        assert (await client.get(AUTOBUILD_API, params=owner)).status_code == 200
        store = DraftStore(owner["project"], owner["conversation_id"])
        candidate = prepare(store)
        candidate = store.freeze_materials(candidate, freeze_materials(candidate, store.get()))
        original_document = copy.deepcopy(candidate["document"])
        before_snapshot = candidate["materials_snapshot"].get("sequence", 0)
        response = await client.post(AUTOBUILD_API + "/discussion", json={**owner, "action": "send",
            "candidate_id": candidate["id"], "sequence": candidate["sequence"],
            "message": "先评估 OCR 接入参数，不代表开始执行。"})
        assert response.status_code == 200, response.text
        state = store.get()
        after = state["candidate"]
        discussion = after["autobuild"]["discussion"]
        assert len(prompts) == 1
        actual_user = next(row for row in discussion["messages"] if row["role"] == "user")
        assert state["materials"]["requirement_sources"]["ocr_setup"] == [
            {"source_id": actual_user["id"], "quote": actual_user["text"]}]
        assert discussion["material_receipt"]["saved_entry_ids"] == ["ocr_setup"]
        assert "material_receipt" not in prompts[0]
        assert after["document"] == original_document
        assert after["materials_snapshot"].get("sequence", 0) == before_snapshot
        assert after["autobuild"]["phase"] == "waiting_user"
        assert plans.get(owner["project"]) is None

        # The model may not forge the host-owned receipt field.
        async def forged(client, **kwargs):
            return json.dumps({"reply": "伪造回执", "proposal": None,
                               "material_receipt": {"saved_entry_ids": ["forged"]}})
        monkeypatch.setattr(orch_drafts, "collect_model_text", forged)
        candidate = store.get()["candidate"]
        rejected = await client.post(AUTOBUILD_API + "/discussion", json={**owner, "action": "send",
            "candidate_id": candidate["id"], "sequence": candidate["sequence"], "message": "再确认一次保存状态。"})
        assert rejected.status_code == 409
        assert store.get()["candidate"]["autobuild"]["discussion"].get("material_receipt") == discussion["material_receipt"]
        assert "ocr_setup" in {row["id"] for row in store.get()["materials"]["document"]["entries"]}


@pytest.mark.asyncio
async def test_discussion_repairs_pending_materials_in_same_turn_and_keeps_candidate_frozen(api_env, monkeypatch):
    from tests.test_graph_discussion import prepare

    _, plans, _, owner = api_env
    prompts = []
    discussion_text = "先整理 OCR 服务参数，并把硬件型号留作待评估素材。"

    async def model(client, **kwargs):
        payload = json.loads(kwargs["user_text"])
        prompts.append(payload)
        assert payload["workflow_materials"]["contract"] == entry_contract()
        user_turn = next(row for row in reversed(payload["messages"]) if row["role"] == "user")
        assert user_turn["text"] == discussion_text
        node = block_node("ocr_setup", "准备 OCR 服务", "整理 OCR 接入参数。", user_turn)
        decision = {"id": "decision_local_ocr_hardware", "kind": "decision",
            "title": "本地 OCR 硬件方案", "body": "先把硬件型号保留为待评估设计。",
            "state": "active", "data": {"category": "designed", "value": "待评估",
                "node_id": "ocr_setup", "hardware": "型号 A"},
            "sources": [{"source_id": user_turn["id"], "quote": user_turn["text"]}]}
        if len(prompts) == 1:
            return json.dumps({"reply": "我先保存参数节点和待评估方案。", "proposal": None,
                "materials": {"upserts": [node, decision], "ignored": []}}, ensure_ascii=False)

        assert payload["binding"] == prompts[0]["binding"]
        repair = payload["repair"]
        assert repair["pending_upserts"] == [decision]
        diagnostic = next(row for row in repair["diagnostics"]
                          if row["entry_id"] == "decision_local_ocr_hardware")
        assert diagnostic["code"] == "unsupported_data_field"
        assert diagnostic["path"] == "data"
        assert diagnostic["unsupported_fields"] == ["hardware"]
        assert "ocr_setup" in {row["id"] for row in payload["workflow_materials"]["document"]["entries"]}
        decision["data"] = {"category": "designed", "value": "待评估", "node_id": "ocr_setup"}
        return json.dumps({"reply": "已依据字段诊断修正待评估素材。", "proposal": None,
            "materials": {"upserts": [decision], "ignored": []}}, ensure_ascii=False)

    monkeypatch.setattr(orch_drafts, "collect_model_text", model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        assert (await client.get(AUTOBUILD_API, params=owner)).status_code == 200
        store = DraftStore(owner["project"], owner["conversation_id"])
        candidate = prepare(store)
        candidate = store.freeze_materials(candidate, freeze_materials(candidate, store.get()))
        original_document = copy.deepcopy(candidate["document"])
        response = await client.post(AUTOBUILD_API + "/discussion", json={**owner, "action": "send",
            "candidate_id": candidate["id"], "sequence": candidate["sequence"], "message": discussion_text})
        assert response.status_code == 200, response.text

        saved = store.get()
        after = saved["candidate"]
        discussion = after["autobuild"]["discussion"]
        assert len(prompts) == 2
        assert prompts[0]["binding"] == prompts[1]["binding"]
        assert discussion["material_receipt"]["saved_entry_ids"] == [
            "ocr_setup", "decision_local_ocr_hardware"]
        assert discussion["material_receipt"]["pending"] == []
        assert saved["materials"]["pending"] == []
        assert saved["materials"]["pending_upserts"] == []
        assert {row["id"] for row in saved["materials"]["document"]["entries"]} >= {
            "ocr_setup", "decision_local_ocr_hardware"}
        user_turn = next(row for row in discussion["messages"] if row["role"] == "user")
        for entry_id in ("ocr_setup", "decision_local_ocr_hardware"):
            assert saved["materials"]["requirement_sources"][entry_id] == [
                {"source_id": user_turn["id"], "quote": user_turn["text"]}]
        assert after["document"] == original_document
        assert after["autobuild"]["phase"] == "waiting_user"
        assert after["autobuild"]["model_calls"] == 4
        assert after["autobuild"]["call_attempts"] == 4


@pytest.mark.asyncio
async def test_discussion_withdraws_anonymous_pending_and_preserves_candidate(api_env, monkeypatch):
    from tests.test_graph_discussion import prepare

    _, plans, _, owner = api_env
    prompts = []

    async def model(client, **kwargs):
        payload = json.loads(kwargs['user_text'])
        prompts.append(payload)
        assert payload['workflow_materials']['pending_contract']['withdraw_pending']
        return json.dumps({'reply': '已撤回错误的未保存提案。', 'proposal': None,
            'materials': {'upserts': [], 'ignored': [], 'withdraw_pending': [
                {'diagnostic_id': row['diagnostic_id'], 'reason': '撤回历史错误提交'}
                for row in payload['workflow_materials']['pending']]}}, ensure_ascii=False)

    monkeypatch.setattr(orch_drafts, 'collect_model_text', model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        assert (await client.get(AUTOBUILD_API, params=owner)).status_code == 200
        store = DraftStore(owner['project'], owner['conversation_id'])
        candidate = prepare(store)
        with store.db() as db:
            state = store._get(db)
            for row in state['candidate']['document']['entries']:
                row.update(evidence='manual', sources=[])
            state['materials'] = {'sequence': 1, 'pending': [
                {'entry_id': '', 'path': 'ignored[0]', 'code': 'invalid_ignored'}], 'pending_upserts': []}
            store._put(db, state)
        original = copy.deepcopy(store.get()['candidate']['document'])
        response = await client.post(AUTOBUILD_API + '/discussion', json={**owner, 'action': 'send',
            'candidate_id': candidate['id'], 'sequence': candidate['sequence'], 'message': '撤回历史错误提交。'})
        assert response.status_code == 200, response.text
        saved = store.get()
        receipt = saved['candidate']['autobuild']['discussion']['material_receipt']
        assert len(prompts) == 1
        assert receipt['pending'] == [] and len(receipt['withdrawn_pending_ids']) == 1
        assert saved['materials']['pending_history'][-1]['action'] == 'withdraw_pending'
        assert saved['candidate']['document'] == original
        assert plans.get(owner['project']) is None
        assert plans.get(owner["project"]) is None
        assert server.EXPERT_RUNS.list_for_conversation(owner["conversation_id"]) == []
