"""Graph-first API: actual stores/canvas, synthetic model, no live task writes."""
import asyncio
import copy
import json
from pathlib import Path

import httpx
import pytest

from frontend import orch_drafts, server
from mewcode.orchestration.draft_sources import digest, read_sources
from mewcode.orchestration.draft_store import DraftStore
from mewcode.orchestration import get_template, to_pipeline_graph
from mewcode.orchestration.graph_generation import _graph_actions, graph_process_state, graph_state, ready_graph_document
from mewcode.orchestration.workflow_draft import seed_document
from mewcode.pipeline.store import PipelineStore
from tests.test_generic_loops import spec_for
from tests.test_workflow_drafts import api_env, record, write_history, entry, cited, model_patch

API = "/api/orchestration/graph-generation"


def test_saved_graph_without_new_changes_does_not_ask_for_adoption_or_repeat_save():
    state = {"candidate": None, "_graph_ready": True, "_canvas_saved": True,
             "coverage": {"total": 7, "processed": 7, "pending": 0},
             "autobuild_result": {"phase": "done", "status": "ready", "autofix": {
                 "rounds": [{"structural": {"adopted": False, "applied": []}}]}}}
    result = graph_process_state(state, False)
    assert result["headline"] == "阶段图已保存"
    assert "无需重复保存" in result["last_result"]
    assert "尚未采用" not in result["last_result"]
    assert "返回对话" in result["next_step"]
    state["_graph_ready"] = False
    assert graph_process_state(state, False)["outcome"] == "needs_work"


@pytest.mark.parametrize("build", [
    {"phase": "autofix", "status": "ready"},
    {"phase": "done", "status": "needs_review"},
])
def test_ready_candidate_keeps_resume_for_unfinished_or_review_retry(build):
    base = {"nodes": [], "edges": []}
    state = {
        "candidate": {"id": "candidate", "sequence": 2, "status": "ready",
                       "base_graph_hash": digest(base), "autobuild": build},
        "running": False, "source_ids": ["s1"], "warnings": [],
        "coverage": {"total": 1, "processed": 1, "pending": 0,
                      "new_sources": 0, "history_changed": False},
        "basis": {},
    }
    actions = _graph_actions(state, base, {"entries": []}, ready=False, issues=[])
    assert actions["resume"]["enabled"]


def test_inconsistent_history_blocks_resume_update_and_rebase_actions():
    base = {"nodes": [], "edges": []}
    state = {
        "candidate": {"id": "candidate", "sequence": 2, "status": "failed",
                       "base_graph_hash": digest({"nodes": [{"node_id": "old"}], "edges": []})},
        "running": False, "source_ids": ["s1"], "warnings": [],
        "coverage": {"total": 1, "processed": 0, "pending": 1,
                      "new_sources": 0, "history_changed": True},
        "basis": {},
    }
    actions = _graph_actions(state, base, {"entries": []}, ready=False, issues=[])
    for key in ("update", "resume", "rebase_current_canvas"):
        assert not actions[key]["enabled"]
        assert "重新读取" in actions[key]["reason"]
    assert actions["rebuild"]["enabled"]


def test_waiting_user_without_business_questions_exposes_continue_as_resume():
    base = {"nodes": [], "edges": []}
    state = {
        "candidate": {"id": "candidate", "sequence": 2, "status": "ready",
                       "base_graph_hash": digest(base),
                       "autobuild": {"phase": "waiting_user", "status": "waiting_user",
                                     "discussion": {"pending": None, "proposal": None}}},
        "running": False, "source_ids": ["s1"], "warnings": [],
        "coverage": {"total": 1, "processed": 1, "pending": 0,
                      "new_sources": 0, "history_changed": False},
        "basis": {}, "revision": 0,
    }
    actions = _graph_actions(state, base, {"entries": []}, ready=False, issues=[])
    assert actions["resume"]["enabled"]
    assert actions["rebuild"]["enabled"]


def test_ready_legacy_candidate_without_autobuild_cannot_start_discussion():
    base = {"nodes": [], "edges": []}
    state = {
        "candidate": {"id": "candidate", "sequence": 1, "status": "ready",
                       "base_graph_hash": digest(base)},
        "running": False, "source_ids": ["s1"], "warnings": [],
        "coverage": {"total": 1, "processed": 1, "pending": 0,
                      "new_sources": 0, "history_changed": False},
        "basis": {}, "revision": 0,
    }
    actions = _graph_actions(state, base, {"entries": []}, ready=False, issues=[])
    assert not actions["discuss"]["enabled"]


def test_invalidated_empty_repair_preview_never_reports_checked_or_save_ready():
    base = {"nodes": [{"node_id": "work"}], "edges": []}
    state = {
        "candidate": {"id": "candidate", "sequence": 2, "status": "ready",
                       "base_graph_hash": digest(base)},
        "running": False, "source_ids": [], "warnings": [],
        "coverage": {"total": 0, "processed": 0, "pending": 0, "history_changed": False},
        "local_repair_run": {"id": "run", "candidate_id": "candidate", "sequence": 2,
                              "status": "completed"},
        # public_state deliberately exposes this key as None after its CAS
        # check fails; an old repair_preview must not be reused.
        "local_repair_preview": None,
        "repair_preview": {"changes": [], "validation": {"valid": True}},
        "_graph_ready": False,
    }
    process = graph_process_state(state, False)
    assert process["outcome"] == "needs_work"
    assert "校验通过，无需修改" not in process["headline"]


def test_saved_review_stale_parameter_edit_remains_plan_saveable_but_not_executable():
    base = spec_for().as_dict()
    for node in base["nodes"]:
        node["plan_brief"] = {"steps": ["核对内容"], "outputs": ["明确产物"],
                               "acceptance": ["按证据核验"]}
    original_hash = digest(base)
    edited_base = copy.deepcopy(base)
    edited_base["label"] = "已保存图（参数已改）"
    document = seed_document(base)
    for entry in document["entries"]:
        if entry["kind"] == "node":
            entry["data"].update(outputs=["明确产物"], acceptance=["按证据核验"])
    state = {
        "project": str(Path.cwd()), "conversation_id": "paper", "draft_id": "main",
        "revision": 1, "document": document, "source_ids": [],
        "processed_ids": [], "selected_ids": [], "warnings": [], "history_basis": {},
        "coverage": {"total": 0, "processed": 0, "pending": 0, "history_changed": False},
        "candidate": None, "running": False, "progress": None, "basis": {},
        "autobuild_result": {"phase": "done", "status": "ready", "reason": "",
                              "review": {"gaps": []}, "review_stale": False,
                              "graph_hash": original_hash},
    }
    result = graph_state(state, edited_base)
    assert result["can_preview"] and not result["execution_ready"], {
        "issues": result["issues"], "readiness": result["readiness"],
        "delivery": result["delivery"], "process": result["process"],
    }
    assert result["delivery"]["review_stale"]
    assert result["process"]["outcome"] == "needs_work"
    assert "修改后待校验" not in result["process"]["headline"]


def test_manual_candidate_save_still_rejects_design_lint_errors(tmp_path):
    base = {
        "spec_id": "manual-lint", "label": "手工图", "goal": "完成全量读取",
        "nodes": [{"node_id": "plan", "kind": "plan", "label": "计划", "goal": "读取",
                   "plan_brief": {"steps": ["逐页读取全部文件"], "outputs": ["清单"], "acceptance": ["清单可核对"]}}],
        "edges": [], "finalizer_node_id": "plan", "meta": {},
    }
    document = seed_document(base)
    state = {"revision": 0, "document": {"schema_version": 1, "entries": [], "ignored": []},
             "source_ids": [], "processed_ids": [], "warnings": [], "history_basis": {},
             "candidate": {"id": "candidate", "sequence": 0, "base_revision": 0,
                           "base_graph_hash": digest(base), "document": document, "status": "ready",
                           "processed_ids": [], "source_ids": [], "mode": "selected"}}
    with pytest.raises(ValueError, match="逐项全量处理"):
        ready_graph_document(state, state["candidate"], base, conversation_id="paper")


@pytest.mark.asyncio
async def test_workspace_keeps_unclaimed_canvas_readonly_and_outside_chat_base(api_env, model):
    _, _, _, owner = api_env
    pipeline = PipelineStore(owner["project"])
    pipeline.create_pipeline(to_pipeline_graph(get_template("linear", goal="旧版绘图"), graph_id="g_legacy"))
    path = Path(owner["project"]) / ".mewcode/pipelines/g_legacy.json"
    before = path.read_bytes()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        state = (await client.get(API, params=owner)).json()
        assert state["can_open_canvas"] and state["canvas_claim_required"]
        assert not state["view"]["nodes"] and state["candidate"] is None
        assert not model and path.read_bytes() == before
        pipeline.create_pipeline(to_pipeline_graph(get_template("linear", goal="另一张旧图"), graph_id="g_other"))
        ambiguous = (await client.get(API, params=owner)).json()
        assert not ambiguous["can_open_canvas"] and not ambiguous["canvas_claim_required"]
        assert not model and path.read_bytes() == before


async def poll(client, owner):
    for _ in range(300):
        r = await client.get(API, params=owner)
        assert r.status_code == 200, r.text
        if not r.json()["running"]:
            return r.json()
        await asyncio.sleep(.01)
    pytest.fail("generation did not settle")


@pytest.fixture
def model(monkeypatch):
    calls = []
    async def complete(client, **kw):
        assert client == ("chosen", "high"), "do not change route/effort"
        assert "直接生成" in kw["system"]
        inputs = json.loads(kw["user_text"])
        calls.append(inputs)
        return json.dumps(model_patch(inputs), ensure_ascii=False)
    monkeypatch.setattr(orch_drafts, "collect_model_text", complete)
    return calls


async def generate(client, owner):
    response = await client.post(API + "/update", json=owner)
    assert response.status_code == 202, response.text
    state = await poll(client, owner)
    assert state["can_preview"], state
    preview = await client.post(API + "/graph", json={**owner, "action": "preview"})
    assert preview.status_code == 200, preview.text
    return state, preview.json()


@pytest.mark.asyncio
async def test_graph_extraction_accepts_large_accumulated_document(api_env, monkeypatch):
    _, _, path, owner = api_env
    calls = []
    async def model(client, **kwargs):
        payload = json.loads(kwargs["user_text"])
        calls.append(kwargs["user_text"])
        assert kwargs["max_generated_chars"] is None
        result = model_patch(payload)
        if len(calls) == 1:
            result["upserts"].extend(cited({**entry(f"rule{i}", "constraint", body="完整既有要求" * 900), "title": f"约束{i}"}, payload["sources"][0])
                                      for i in range(36))
        else:
            assert len(kwargs["user_text"]) > 160000
            assert len([e for e in payload["document"]["entries"] if e["id"].startswith("rule")]) == 36
        return json.dumps(result, ensure_ascii=False)
    monkeypatch.setattr(orch_drafts, "collect_model_text", model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        await generate(client, owner)
        write_history(path, [record("user", "补充要求：保持全部现有约束")], append=True)
        assert (await client.post(API + "/update", json=owner)).status_code == 202
        state = await poll(client, owner)
        assert state["candidate"]["status"] == "ready" and state["can_preview"]
        assert len(calls) == 2


@pytest.mark.asyncio
async def test_direct_generate_edit_save_reopen_without_text_confirmation(api_env, model):
    current, plans, path, owner = api_env
    store = DraftStore(owner["project"], "paper")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        state, preview = await generate(client, owner)
        assert not state["can_open_canvas"], "an unsaved candidate is not a formal canvas"
        assert len(model) == 1 and store.get()["revision"] == 0
        assert "document" not in state and "document" not in state["candidate"]
        assert plans.get(owner["project"]) is None
        assert [n["node_id"] for n in state["view"]["nodes"]] == ["analyze", "audit"]
        assert (await client.post(API + "/action", json={**owner, "action": "confirm"})).status_code == 409
        c = state["candidate"]
        edit = {**owner, "action": "edit", "candidate_id": c["id"], "sequence": c["sequence"], "entry_id": "analyze",
                "changes": {"body": "用户在图上修改的职责", "data": {"inputs": ["原稿.txt"], "acceptance": ["不得丢段落"], "steps": ["逐段比对"]}}}
        changed = await client.post(API + "/action", json=edit)
        assert changed.status_code == 200, changed.text
        assert (await client.post(API + "/action", json=edit)).status_code == 409, "late editor must conflict"
        assert (await client.post(API + "/graph", json={**owner,"action":"apply","preview_id":preview["id"]})).status_code == 409
        preview = (await client.post(API + "/graph", json=owner)).json()
        assert preview["spec"]["nodes"][0]["plan_brief"]["acceptance"] == ["不得丢段落"]
        r = await client.post(API + "/graph", json={**owner, "action":"apply", "preview_id":preview["id"], "spec":{"forged":True}})
        assert r.status_code == 200, r.text
        assert r.json()["spec"]["nodes"][0]["goal"] == "用户在图上修改的职责"
        assert len(model) == 1, "editing/projecting/saving must not invoke another model"
        assert (await client.get(API, params=owner)).json()["can_open_canvas"]
        saved = store.get()
        assert saved["candidate"] is None and saved["revision"] == 1
        assert not next(e for e in saved["document"]["entries"] if e["id"] == "audit").get("reviewed"), "graph save is not fabricated per-clause sign-off"
        with store.db() as db:
            version = json.loads(db.execute("SELECT data FROM version").fetchone()[0])
        assert version["accepted_via"] == "graph_save"
        assert (await client.post(API + "/graph", json={**owner,"action":"apply","preview_id":preview["id"]})).json()["already_applied"]
        reopened = await client.post("/api/orchestration/open-canvas", json={"conversation_id":"paper"})
        assert reopened.json()["spec"]["nodes"][0]["goal"] == "用户在图上修改的职责"
        assert server._conversation_plan_store("paper").get(owner["project"]).status == "drafting"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["append", "replace", "corrupt"])
async def test_apply_rechecks_source_snapshot(api_env, model, change):
    _, plans, path, owner = api_env
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        _, preview = await generate(client, owner)
        if change == "corrupt":
            with path.open("a", encoding="utf-8") as f:
                f.write("not-json\n")
        else:
            write_history(path, [record("user", "新增或变化的要求")], append=change == "append")
        result = await client.post(API + "/graph", json={**owner,"action":"apply","preview_id":preview["id"]})
        assert result.status_code == 409, result.text
        assert plans.get(owner["project"]) is None
        assert DraftStore(owner["project"], "paper").get()["revision"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "pending", "chat_busy"])
async def test_graph_save_keeps_active_runs_untouched(api_env, model, monkeypatch, status):
    current, plans, path, owner = api_env
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        _, preview = await generate(client, owner)
        if status == "chat_busy":
            current["busy"] = True
        else:
            monkeypatch.setattr(server.EXPERT_RUNS, "list_for_conversation", lambda _: [{"status":status}])
        result = await client.post(API + "/graph", json={**owner,"action":"apply","preview_id":preview["id"]})
        assert result.status_code == 409, result.text
        assert plans.get(owner["project"]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["paused", "waiting", "blocked"])
@pytest.mark.parametrize("newer_completed", [False, True])
async def test_save_new_plan_preserves_historical_run_snapshots(api_env, model, monkeypatch, status, newer_completed):
    _, plans, _, owner = api_env
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        _, preview = await generate(client, owner)
        runs = [{"run_id": "old", "status": status, "updated_at": "2026-10-04T15:12:09Z",
                 "locked_flow": copy.deepcopy(preview["spec"]), "node_statuses": {"analyze": "completed"},
                 "budget": {"spent": 42}}]
        if newer_completed:
            runs.insert(0, {"run_id": "new", "status": "completed"})
        before = json.dumps(runs, ensure_ascii=False)
        monkeypatch.setattr(server.EXPERT_RUNS, "list_for_conversation", lambda _: runs)
        def forbidden(*args, **kwargs):
            raise AssertionError("Saving must not update or restart an existing run")
        for method in ("mutate", "mutate_stopped", "resume", "transition"):
            monkeypatch.setattr(server.EXPERT_RUNS, method, forbidden)
        result = await client.post(API + "/graph", json={**owner, "action": "apply", "preview_id": preview["id"]})
        assert result.status_code == 200, result.text
        assert result.json()["preserved_runs"] == [{"run_id": "old", "status": status}]
        assert json.dumps(runs, ensure_ascii=False) == before
        assert server._conversation_plan_store("paper").get(owner["project"]) is not None
        assert len(model) == 1
        assert DraftStore(owner["project"], "paper").get()["candidate"] is None


@pytest.mark.asyncio
async def test_conflict_visible_on_graph_and_resolved_in_node(api_env, monkeypatch):
    _, _, _, owner = api_env
    async def conflict(client, **kw):
        value = model_patch(json.loads(kw["user_text"]))
        value["upserts"][0]["state"] = "conflict"
        return json.dumps(value)
    monkeypatch.setattr(orch_drafts, "collect_model_text", conflict)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        await client.post(API + "/update", json=owner)
        state = await poll(client, owner)
        assert not state["can_preview"] and state["view"]["nodes"][0]["state"] == "conflict"
        assert any("冲突" in e for e in state["issues"])
        assert (await client.post(API + "/graph", json=owner)).status_code == 409
        c = state["candidate"]
        r = await client.post(API + "/action", json={**owner, "action":"edit", "candidate_id":c["id"], "sequence":c["sequence"],
            "entry_id":"analyze", "changes":{"state":"active","body":"明确新的职责"}})
        assert r.status_code == 200 and r.json()["can_preview"], r.text
        assert (await client.post(API + "/graph", json=owner)).status_code == 200


@pytest.mark.asyncio
async def test_stale_canvas_and_cross_conversation_rejected(api_env, model):
    current, _, _, owner = api_env
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        _, preview = await generate(client, owner)
        manual = copy.deepcopy(preview["spec"])
        manual["label"] = "另一编辑器保存的流程"
        r = await client.post("/api/orchestration/open-canvas", json={**owner,"spec":manual})
        assert r.status_code == 200, r.text
        assert (await client.post(API + "/graph", json={**owner,"action":"apply","preview_id":preview["id"]})).status_code == 409
        assert (await client.post(API + "/graph", json=owner)).status_code == 409
        assert (await client.post(API + "/update", json=owner)).status_code == 409
        state = (await client.get(API, params=owner)).json()
        assert not state["can_preview"] and any("画布已被修改" in issue for issue in state["issues"])
        current["cid"] = "elsewhere"
        for url in [API, API + "/progress", API + "/source/fake"]:
            assert (await client.get(url, params=owner)).status_code == 409
        for url in [API + "/graph", API + "/action", API + "/update"]:
            assert (await client.post(url, json=owner)).status_code == 409


@pytest.mark.asyncio
async def test_selected_reply_does_not_start_full_history_scan(api_env, monkeypatch):
    _, _, path, owner = api_env
    write_history(path,[record("user","早期要求" * 20000),record("assistant","建议增加独立复核节点")])
    calls = []
    async def selected(client, **kw):
        data = json.loads(kw["user_text"]); calls.append(data)
        return json.dumps(model_patch(data))
    monkeypatch.setattr(orch_drafts,"collect_model_text",selected)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url="http://test") as client:
        await client.post(API + "/update",json={**owner,"selection":{"texts":["建议增加独立复核节点"]}})
        state = await poll(client,owner)
        assert len(calls) == 1 and len(calls[0]["sources"]) == 1
        assert state["coverage"]["mode"] == "selected" and state["coverage"]["total"] == 1
        assert all(e["evidence"] == "proposal" for e in state["elements"])
        preview = (await client.post(API + "/graph",json=owner)).json()
        assert (await client.post(API + "/graph",json={**owner,"action":"apply","preview_id":preview["id"]})).status_code == 200
        repeated = await client.post(API + "/update",json={**owner,"selection":{"texts":["建议增加独立复核节点"]}})
        assert repeated.json()["already_included"] and len(calls) == 1


@pytest.mark.asyncio
async def test_long_history_stop_resume_and_legacy_progress_reuse(api_env, monkeypatch):
    _, _, path, owner = api_env
    write_history(path, [record("user", "最早保留双稿" + "甲" * 80000), record("user", "后续阶段" + "乙" * 80000)])
    before = path.read_bytes()
    blocked, entered = asyncio.Event(), asyncio.Event()
    calls = []
    async def model(client, **kw):
        data = json.loads(kw["user_text"]); batch = data["sources"]
        calls.append([s["id"] for s in batch])
        if len(calls) == 2:
            entered.set(); await blocked.wait()
        return json.dumps({"upserts":[cited(entry("n_"+batch[0]["id"],body=batch[0]["text"][:8]),batch[0])],
            "ignored":[{"source_id":s["id"],"reason":"同一要求的后续文字"} for s in batch[1:]]})
    monkeypatch.setattr(orch_drafts,"collect_model_text",model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url="http://test") as client:
        # Existing background draft extraction and its successful chunks survive the UI change.
        await client.post("/api/orchestration/draft/update",json=owner)
        await asyncio.wait_for(entered.wait(),3)
        stopped = await client.post(API + "/action",json={**owner,"action":"stop"})
        assert stopped.json()["coverage"]["processed"] > 0 and not stopped.json()["can_preview"]
        assert (await client.post(API + "/graph",json=owner)).status_code == 409
        await client.post(API + "/update",json=owner)
        state = await poll(client,owner)
        assert state["coverage"]["pending"] == 0 and state["can_preview"]
        assert all(set(calls[0]).isdisjoint(c) for c in calls[1:])
        assert any("最早保留双稿" in e["body"] for e in state["elements"])
        assert path.read_bytes() == before
        store = DraftStore(owner["project"],"paper")
        assert store.get()["revision"] == 0, "no implicit text confirmation"
        assert store.get()["candidate"]["processed_ids"] == [s["id"] for s in read_sources(path)[0]]


@pytest.mark.asyncio
async def test_new_generation_uses_latest_saved_graph_not_old_index(api_env, model, monkeypatch):
    _, _, path, owner = api_env
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url="http://test") as client:
        _, preview = await generate(client,owner)
        applied = (await client.post(API + "/graph",json={**owner,"action":"apply","preview_id":preview["id"]})).json()
        spec = applied["spec"]
        node = spec["nodes"][0]
        node.update(label="人工新名称",goal="人工新职责",position={"x":123,"y":456},model={"provider_key":"bound","model_id":"chosen","effort":"high"})
        node["plan_brief"]["acceptance"] = ["人工验收条件"]
        spec["meta"]["annotations"] = [{"id":"draft_local", "workflow_draft_entry":"local", "mode":"constraint",
            "target_id":"analyze", "text":"节点原有约束不能丢", "attachments":[{"attachment_id":"original","name":"原稿"}]}]
        spec["meta"]["workflow_draft"]["parents"] = {"audit":"analyze"}
        updated = await client.post("/api/orchestration/open-canvas",json={**owner,"spec":spec,"expected_version":spec["meta"]["pipeline_graph_version"]})
        assert updated.status_code == 200,updated.text
        reopened = (await client.get(API,params=owner)).json()
        assert reopened["can_open_canvas"]
        assert reopened["view"]["nodes"][0]["label"] == "人工新名称", "reopening must show latest canvas, not old index"
        no_call_preview = (await client.post(API + "/graph",json=owner)).json()
        assert no_call_preview["spec"]["nodes"][0]["goal"] == "人工新职责"
        write_history(path,[record("user","新增一条运行动态，无新要求")],append=True)
        async def ignore(client,**kw):
            inputs=json.loads(kw["user_text"])
            assert next(e for e in inputs["document"]["entries"] if e["id"]=="analyze")["body"]=="人工新职责"
            return json.dumps({"upserts":[],"ignored":[{"source_id":s["id"],"reason":"动态"} for s in inputs["sources"]]})
        monkeypatch.setattr(orch_drafts,"collect_model_text",ignore)
        _, next_preview = await generate(client,owner)
        final = next_preview["spec"]["nodes"][0]
        assert final["label"] == "人工新名称" and final["goal"] == "人工新职责"
        assert final["plan_brief"]["acceptance"] == ["人工验收条件"]
        assert final["model"] == node["model"] and final["position"] == node["position"]
        assert next_preview["spec"]["meta"]["workflow_draft"]["parents"] == {"audit":"analyze"}
        assert next_preview["spec"]["meta"]["node_constraints"]["analyze"][0]["text"] == "节点原有约束不能丢"
        assert next_preview["spec"]["meta"]["node_constraints"]["analyze"][0]["attachments"] == [{"attachment_id":"original","name":"原稿"}]
