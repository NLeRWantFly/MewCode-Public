"""Regression for repair talk -> six new conflicts -> stale-ticket feedback."""
import copy
import json

import httpx
import pytest

from frontend import orch_drafts, server
from mewcode.orchestration.draft_sources import digest, read_sources
from mewcode.orchestration.graph_repair import SCHEMA, preview_repair
from mewcode.orchestration.repair_protocol import is_control_source
from mewcode.orchestration.workflow_draft import apply_patch, empty_document, seed_document
from mewcode.tools.inspect_stage_graph import Params
from tests.test_directed_loop_repair import five_stage_spec
from tests.test_graph_generation import API, poll
from tests.test_graph_inspection import install_candidate, host_tool, read_all
from tests.test_workflow_drafts import api_env, entry, cited, record, write_history, model_patch


def six_conflicts():
    document = seed_document(five_stage_spec().as_dict())
    for item in document["entries"]:
        if item["kind"] == "loop":
            item.update(id="atomic_loop", state="conflict")
            for key in ("repair_dispatch", "requires_repair", "repair_node_ids"):
                item["data"].pop(key, None)
        if item["kind"] == "edge" and item["data"]["from_node"] == "decide" and item["data"]["to_node"] == "repair":
            item.update(id="repair_branch", state="conflict")
            item["data"]["condition"] = "fail"
    document["entries"] += [
        entry("topology_task", body="修订并核验拓扑，不是 OCR 业务", state="conflict", evidence="proposal",
              data={"node_kind": "plan", "acceptance": ["取得拓扑校验回执"]}),
        entry("reachable_question", "question", "返修是否可达判定", state="conflict", evidence="proposal", data={"node_id": "topology_task"}),
        entry("invariants", "constraint", "保留独立复核，UNKNOWN 不算通过", state="conflict", data={"node_id": "topology_task"}),
        entry("five_choice", "decision", "采用五业务节点，不改三节点方案", state="conflict", data={"node_id": "topology_task"}),
        entry("old_question", "question", "旧修图问题记录", data={"node_id": "topology_task"}),
        entry("old_progress", "progress", "旧的拓扑修订进度", data={"node_id": "topology_task"}),
        entry("old_contract", "question", "已有合同与拓扑如何对应", data={"node_id": "topology_task"}),
        entry("old_choice", "decision", "采用先判定再返修的路线", data={"node_id": "topology_task"}),
    ]
    return document


def fixed_proposal(ticket):
    changes = {
        "atomic_loop": {"state": "active", "data": {"repair_dispatch": "after_decision", "requires_repair": True, "repair_node_ids": ["repair"]}},
        "repair_branch": {"state": "active", "data": {"condition": "loop"}},
        "topology_task": {"kind": "progress", "data": {}, "state": "active"},
        "reachable_question": {"kind": "progress", "data": {}, "state": "active"},
        "invariants": {"state": "active", "data": {"node_id": ""}},
        "five_choice": {"state": "active", "data": {"node_id": ""}},
        **{key: {"data": {"node_id": ""}} for key in ("old_question", "old_progress", "old_contract", "old_choice")},
    }
    return {"schema": SCHEMA, "ticket": ticket, "summary": "保留五业务节点，返修由判定跨轮调度；修图讨论保留为非执行记录。",
            "changes": [{"entry_id": key, "reason": "有来源的编辑元数据与业务职责分开，保留全部原标准和来源", "changes": value}
                        for key, value in changes.items()]}


@pytest.mark.asyncio
async def test_six_conflicts_trial_feedback_correct_adopt_save(api_env):
    _, _, path, owner = api_env
    store = install_candidate(owner, path, six_conflicts())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        packet = (await client.post(API + "/repair", json={**owner, "action": "request"})).json()
        tool = host_tool(owner)
        data, pages = await read_all(tool, ticket=packet["ticket"])
        assert any(p["code"] == "projection_invalid" and "cycle" in p["message"] for p in data["validation.diagnostics"])
        assert any(p["rule"] == "after_decision" for p in data["runtime.semantics"])
        original, file_before = store.get(), store.path.read_bytes()
        proposal = fixed_proposal(packet["ticket"])
        broken = copy.deepcopy(proposal)
        broken["changes"][1]["changes"]["data"]["condition"] = "fail"
        async def trial(p, version=pages[0]["snapshot_id"]):
            response = await tool.run(Params(action="validate", snapshot_id=version, proposal=p), work_dir=owner["project"])
            assert not response.is_error, response.output
            return json.loads(response.output)
        failed = await trial(broken)
        assert not failed["valid"] and any("普通" in p["message"] for p in failed["diagnostics"])
        passed = await trial(proposal)
        assert passed["valid"] and passed["read_only"] and not passed["adopted"]
        assert store.get() == original and store.path.read_bytes() == file_before
        invalid_preview = (await client.post(API + "/repair", json={**owner, "action": "preview", "proposal": broken})).json()
        rejected = await client.post(API + "/repair", json={**owner, "action": "adopt", "preview_id": invalid_preview["id"]})
        assert rejected.status_code == 409 and store.get()["candidate"] == original["candidate"]
        preview = (await client.post(API + "/repair", json={**owner, "action": "preview", "proposal": proposal})).json()
        assert preview["validation"]["valid"]
        adoption = await client.post(API + "/repair", json={**owner, "action": "adopt", "preview_id": preview["id"]})
        assert adoption.status_code == 200, adoption.text
        before = {e["id"]: e for e in original["candidate"]["document"]["entries"]}
        after = {e["id"]: e for e in store.get()["candidate"]["document"]["entries"]}
        assert all(after[key]["sources"] == value["sources"] for key, value in before.items())
        assert after["topology_task"]["reclassified_from"] == before["topology_task"]
        for node in ("first", "second", "audit", "repair", "decide"):
            assert after[node] == before[node]
        for key in ("exit_condition", "criteria", "control_mode", "token_budget"):
            assert after["atomic_loop"]["data"].get(key) == before["atomic_loop"]["data"].get(key)
        graph = (await client.post(API + "/graph", json=owner)).json()
        save = await client.post(API + "/graph", json={**owner, "action": "apply", "preview_id": graph["id"]})
        assert save.status_code == 200, save.text
        spec = server._conversation_plan_store("paper").get(owner["project"]).spec
        assert {n.node_id for n in spec.nodes} == {"first", "second", "audit", "repair", "decide", "done"}
        stale = await tool.execute(Params(action="validate", snapshot_id=pages[0]["snapshot_id"], proposal=proposal))
        assert stale.is_error


@pytest.mark.asyncio
@pytest.mark.parametrize("protected", ["reviewed", "user_source", "saved", "business_role", "prior_user_source"])
async def test_reclassification_cannot_remove_business_contract(api_env, protected):
    _, _, path, owner = api_env
    store = install_candidate(owner, path, six_conflicts())
    state = store.get()
    target = next(e for e in state["candidate"]["document"]["entries"] if e["id"] == "topology_task")
    if protected == "reviewed": target["reviewed"] = True
    elif protected == "user_source": target["evidence"] = "user_source"
    elif protected == "saved": target["evidence"] = "saved_graph"
    elif protected == "prior_user_source": state["document"]["entries"].append({**copy.deepcopy(target), "evidence": "user_source", "reviewed": True})
    else: target["data"]["node_kind"] = "implement"
    from mewcode.orchestration.graph_repair import issue_request
    req, _ = issue_request(state, None)
    state["repair_request"] = req
    with pytest.raises(ValueError, match="不能移除业务"):
        preview_repair(state, None, fixed_proposal(req["id"]))


@pytest.mark.asyncio
async def test_prior_text_draft_does_not_make_unsaved_meta_work_unfixable(api_env):
    _, _, path, owner = api_env
    store = install_candidate(owner, path, six_conflicts())
    state = store.get()
    state["document"] = copy.deepcopy(state["candidate"]["document"])
    for item in state["document"]["entries"]: item["reviewed"] = True
    from mewcode.orchestration.graph_repair import issue_request
    req, _ = issue_request(state, None)
    state["repair_request"] = req
    proposal = fixed_proposal(req["id"])
    preview = preview_repair(state, None, proposal)
    assert preview["validation"]["valid"]
    assert next(e for e in preview["document"]["entries"] if e["id"] == "topology_task")["reclassified_from"]["sources"]


@pytest.mark.asyncio
async def test_unrelated_old_cycle_allows_explicit_partial_adoption_not_save(api_env):
    _, _, path, owner = api_env
    document = six_conflicts()
    document["entries"] += [entry("sample", data={"node_kind": "implement"}), entry("human", data={"node_kind": "gate"}),
        entry("sample_human", "edge", data={"from_node": "sample", "to_node": "human", "condition": "always"}),
        entry("human_sample", "edge", data={"from_node": "human", "to_node": "sample", "condition": "fail"})]
    store = install_candidate(owner, path, document)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        packet = (await client.post(API + "/repair", json={**owner, "action": "request"})).json()
        proposal = fixed_proposal(packet["ticket"])
        before = store.get()["candidate"]
        preview = (await client.post(API + "/repair", json={**owner, "action": "preview", "proposal": proposal})).json()
        assert not preview["validation"]["valid"]
        assert preview["adoption"]["allowed"] and preview["adoption"]["partial"]
        assert all(not (set(p["entry_ids"]) & {e["entry_id"] for e in proposal["changes"]}) for p in preview["validation"]["diagnostics"])
        assert store.get()["candidate"] == before
        adopted = await client.post(API + "/repair", json={**owner, "action": "adopt", "preview_id": preview["id"]})
        assert adopted.status_code == 200, adopted.text
        after = store.get()["candidate"]
        assert after["log"][-1]["partial"] and after["log"][-1]["remaining_diagnostics"]
        assert not any(e["state"] == "conflict" for e in after["document"]["entries"])
        assert next(e for e in after["document"]["entries"] if e["id"] == "human")["data"]["node_kind"] == "gate"
        assert (await client.post(API + "/graph", json=owner)).status_code == 409


@pytest.mark.asyncio
async def test_related_annotations_cannot_change_requirements(api_env):
    _, _, path, owner = api_env
    store = install_candidate(owner, path, six_conflicts())
    packet = store.request_graph_repair(None)
    proposal = fixed_proposal(packet["ticket"])
    next(c for c in proposal["changes"] if c["entry_id"] == "old_choice")["changes"]["body"] = "改成三个节点"
    with pytest.raises(ValueError, match="不能改变业务要求"):
        preview_repair(store.get(), None, proposal)


@pytest.mark.asyncio
async def test_trial_stale_scope_and_snapshot_never_change_store(api_env):
    current, _, path, owner = api_env
    store = install_candidate(owner, path, six_conflicts())
    packet = store.request_graph_repair(None)
    tool = host_tool(owner)
    _, pages = await read_all(tool, ticket=packet["ticket"])
    before = store.path.read_bytes()
    proposal = fixed_proposal(packet["ticket"])
    assert (await tool.execute(Params(action="validate", snapshot_id="0" * 64, proposal=proposal))).is_error
    current["cid"] = "different"
    assert (await tool.execute(Params(action="validate", snapshot_id=pages[0]["snapshot_id"], proposal=proposal))).is_error
    assert store.path.read_bytes() == before


@pytest.mark.asyncio
async def test_reclassified_original_survives_later_manual_and_model_edits(api_env):
    _, _, path, owner = api_env
    store = install_candidate(owner, path, six_conflicts())
    packet = store.request_graph_repair(None)
    preview = store.preview_graph_repair(None, fixed_proposal(packet["ticket"]))
    store.adopt_graph_repair(None, preview["id"])
    candidate = store.get()["candidate"]
    old = next(e for e in candidate["document"]["entries"] if e["id"] == "topology_task")
    store.edit(candidate["id"], candidate["sequence"], old["id"], {"body": "已作为非执行记录保留"})
    document = store.get()["candidate"]["document"]
    edited = next(e for e in document["entries"] if e["id"] == old["id"])
    assert edited["reclassified_from"] == old["reclassified_from"]
    source = {"id": "later-note", "role": "user", "text": "补充原记录的说明"}
    incoming = {k: v for k, v in edited.items() if k not in {"reviewed", "reclassified_from"}}
    result = apply_patch(document, {"upserts": [cited({**incoming, "body": "补充说明"}, source)], "ignored": []}, [source])
    assert next(e for e in result["entries"] if e["id"] == old["id"])["reclassified_from"] == old["reclassified_from"]


def reply(p, prefix="修订如下"):
    return prefix + "\n```mewcode-graph-repair\n" + json.dumps(p, ensure_ascii=False) + "\n```"


def test_protocol_source_ids_and_full_text_survive_index_upgrade(tmp_path):
    text = reply(fixed_proposal("a" * 32), prefix="完整说明" * 4000)
    path = tmp_path / "public.jsonl"
    write_history(path, [record("assistant", text), record("user", "开发一款流程图产品，需要修订节点接口")])
    sources, warnings, _ = read_sources(path)
    assert not warnings
    controls = sources[:-1]
    assert len(controls) > 1 and all(is_control_source(s) for s in controls)
    assert "".join(s["text"] for s in controls) == text
    assert not is_control_source(sources[-1])
    for s in sources:
        old = {k: v for k, v in s.items() if k not in {"id", "purpose"}}
        assert s["id"] == "s_" + digest(old)[:24]
    with pytest.raises(ValueError, match="修订单"):
        apply_patch(empty_document(), {"upserts": [cited(entry(), controls[0])], "ignored": []}, controls)
    from mewcode.orchestration.draft_store import DraftStore
    store = DraftStore(tmp_path, "history")
    legacy = [{k: v for k, v in s.items() if k != "purpose"} for s in sources]
    store.refresh(legacy, [], {})
    store.refresh(sources, [], {})
    assert all(is_control_source(s) for s in store.sources()[:-1]), "upgrade all old split fragments, not only the fence fragment"


@pytest.mark.asyncio
async def test_protocol_reply_cannot_be_reincluded_and_full_scan_skips_without_model(api_env, monkeypatch):
    _, _, path, owner = api_env
    calls = []
    async def generate(client, **kw):
        data = json.loads(kw["user_text"]); calls.append(data)
        return json.dumps(model_patch(data))
    monkeypatch.setattr(orch_drafts, "collect_model_text", generate)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        await client.post(API + "/update", json=owner)
        await poll(client, owner)
        from mewcode.orchestration.draft_store import DraftStore
        store = DraftStore(owner["project"], owner["conversation_id"])
        original = store.get()["candidate"]
        text = reply(fixed_proposal("a" * 32), prefix="修订说明" * 2200)
        write_history(path, [record("assistant", text)], append=True)
        response = await client.post(API + "/update", json={**owner, "selection": {"texts": [text]}})
        assert response.status_code == 409 and "预览 AI 修订" in response.text
        assert store.get()["candidate"] == original and len(calls) == 1
        assert (await client.post(API + "/update", json=owner)).status_code == 202
        state = await poll(client, owner)
        assert state["candidate"]["status"] == "ready", state
        assert store.get()["candidate"]["document"]["entries"] == original["document"]["entries"]
        assert len(calls) == 1
        assert store.get()["candidate"]["document"]["ignored"]
