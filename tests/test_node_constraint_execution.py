"""Actual scheduler/role input boundaries, with a deterministic no-network model."""
import asyncio
import base64
import copy
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image

from frontend import server
from mewcode import expert_web
from mewcode.agent import StreamText, ToolUseEvent, ToolResultEvent
from mewcode.conversation import ConversationManager
from mewcode.orchestration import OrchestrationSpec, to_expert_flow, to_pipeline_graph
from mewcode.orchestration.activity import ActivityStore
from mewcode.orchestration.constraints import attachment_store, attachment_owner
from mewcode.permissions.sandbox import PathSandbox
from mewcode.pipeline.store import PipelineStore
from tests.test_expert_flow import _profile
from tests.test_orchestration_revision_sync import stores, spec_for, put_plan


@pytest.mark.asyncio
async def test_scheduler_receives_text_image_and_locks_each_attempt(stores, monkeypatch):
    buffer=io.BytesIO()
    Image.new("RGB",(3,2),"blue").save(buffer,format="PNG")
    png=buffer.getvalue()
    ref=attachment_store(stores.project).save(attachment_owner("g_editable"),"required.png",png,"image/png")
    def spec(version):
        raw=spec_for().as_dict()
        raw["version"]=version
        raw["meta"]["annotations"]=[{"id":"requirement","mode":"constraint","target_id":"experiment",
            "text":f"采用第 {version} 版要求", "attachments":[{"attachment_id":ref.id,"kind":"image","name":ref.name}]}]
        return OrchestrationSpec.from_value(raw)
    put_plan(stores,spec(1))
    calls=[]
    events=[]
    class FakeAgent:
        client=SimpleNamespace(expose_reasoning=False)
        total_input_tokens=total_output_tokens=1
        permission_checker=SimpleNamespace(sandbox=PathSandbox(str(stores.project)))
        def __init__(self,model): self.model=model
        def set_review_authorization_context(self,_context): pass
        async def run(self,conversation):
            calls.append((self.model.model_id,copy.deepcopy(conversation.history)))
            if self.model.model_id=="old-model":
                if sum(m=="old-model" for m,_ in calls)==1:
                    raise asyncio.CancelledError()
                target=stores.project / "result.md"
                yield ToolUseEvent(tool_id="write-1",tool_name="WriteFile",arguments={"file_path":str(target),"content":"verified output"})
                target.write_text("verified output",encoding="utf-8")
                yield ToolResultEvent(tool_id="write-1",tool_name="WriteFile",output="written",is_error=False,elapsed=0.01)
            yield StreamText(text="本节点处理结束")
    monkeypatch.setattr(expert_web,"_build_role_agent",lambda _s,_r,_role,model,_p,_h:
        (FakeAgent(model),None,SimpleNamespace(conversation=ConversationManager(),aclose=AsyncMock())))
    session=SimpleNamespace(work_dir=str(stores.project),conversation_id="constraint-run",multi_agent=False,
        session=SimpleNamespace(meta=SimpleNamespace(message_count=0),append=lambda m:None,append_expert_run=lambda *a,**kw:None),
        conversation=ConversationManager(),context=SimpleNamespace(get=lambda k,default=None:default,slots=SimpleNamespace(resolve=lambda k:{})),
        task_manager=SimpleNamespace(list_tasks=lambda:[],mark_notified=lambda ids:None),expert_run_id="",expert_control_action="pause",
        expert_child_event_callback=None,status_payload=lambda:{},tasks_payload=lambda:[])
    registry=SimpleNamespace(catalog=AsyncMock(return_value={"providers":[]}),
        resolve=lambda model:SimpleNamespace(
            ref=SimpleNamespace(effort=model.effort),
            config=SimpleNamespace(protocol="anthropic",model="claude-sonnet-4"),
        ))
    kwargs=dict(session=session,registry_service=registry,profile_store=SimpleNamespace(get=lambda p:_profile()),flow_store=stores.flows,plan_store=stores.plans,run_store=stores.runs)
    ws=SimpleNamespace(send_json=AsyncMock(side_effect=lambda e:events.append(e)))
    await expert_web.run_expert_chat(ws,"核对图文约束",confirmed_new_flow=to_expert_flow(spec(1)),**kwargs)
    run_id=next(e["run_id"] for e in events if e["type"]=="done")
    assert stores.runs.load(run_id)["status"]=="paused"
    put_plan(stores,spec(2))
    await expert_web.run_expert_chat(ws,"继续",resume_run_id=run_id,**kwargs)
    assert [m for m,_ in calls]==["prepare-model","old-model","old-model"]
    assert "采用第 1 版要求" in str(calls[1][1])
    assert "采用第 2 版要求" in str(calls[2][1])
    for _,history in calls[1:]:
        images=[b for m in history for b in m.content_blocks if b.get("type")=="image"]
        assert images and base64.b64decode(images[-1]["data"])==png
    store=ActivityStore(stores.project)
    store.bind("constraint-run","g_editable",run_id,nodes=[{"id":"experiment"},{"id":"prepare"}])
    rows=store.page("constraint-run","g_editable",run_id,node_id="experiment",limit=100)["items"]
    applied=[r for r in rows if r.get("constraints")]
    assert len(applied)==2 and len({e["constraints"][0]["version"] for e in applied})==2
    assert {e["attempt"] for e in applied}=={1,2}
    assert {e["graph_version"] for e in applied}=={1,2}
    assert any(r.get("file",{}).get("name")=="result.md" for r in rows)
    assert not any(e.get("name")=="report_step" for e in events)


@pytest.mark.asyncio
async def test_scoped_history_api_rejects_cross_conversation_and_preserves_corrections(stores, monkeypatch):
    graph=to_pipeline_graph(spec_for(),graph_id="g_editable")
    PipelineStore(str(stores.project)).create_pipeline(graph)
    monkeypatch.setattr(server,"_active_conversation_id",lambda:"api-conv")
    monkeypatch.setattr(server.SESSION,"runtime_for",lambda conv:None)
    ledger=ActivityStore(stores.project)
    ledger.bind("api-conv","g_editable","chat:api",nodes=[{"id":"experiment","label":"修改实验"}])
    event_id=ledger.record("api-conv","chat:api","tool",{"kind":"tool","text":"修改实验报告", "at":"2026-09-11"})
    scope=dict(project=str(stores.project),conversation_id="api-conv",graph_id="g_editable",run_id="chat:api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url="http://test") as client:
        initial=await client.get("/api/orchestration/node-history",params=scope)
        assert initial.status_code==200 and len(initial.json()["items"])==1
        assert initial.json()["items"][0]["node_id"]==""
        bad=await client.get("/api/orchestration/node-history",params={**scope,"conversation_id":"other"})
        assert bad.status_code==409
        assert (await client.get("/api/orchestration/node-history",params={**scope,"project":""})).status_code==409
        assigned=await client.post("/api/orchestration/history-attribution",json={**scope,"event_id":event_id,"node_id":"experiment"})
        assert assigned.status_code==200
        updated=await client.get("/api/orchestration/node-history",params={**scope,"node_id":"experiment","after":initial.json()["cursor"]})
        assert updated.json()["items"][0]["attribution"]=="manual"
        await client.post("/api/orchestration/history-attribution",json={**scope,"event_id":event_id,"node_id":None})
        removed=await client.get("/api/orchestration/node-history",params={**scope,"node_id":"experiment","after":updated.json()["cursor"]})
        assert removed.json()["removed_ids"]==[event_id]


@pytest.mark.asyncio
async def test_constraint_upload_full_content_scope_and_partial_rollback(stores, monkeypatch):
    PipelineStore(str(stores.project)).create_pipeline(to_pipeline_graph(spec_for(), graph_id="g_editable"))
    monkeypatch.setattr(server, "_active_conversation_id", lambda:"upload-conv")
    scope = dict(project=str(stores.project), conversation_id="upload-conv", graph_id="g_editable")
    buffer = io.BytesIO()
    Image.new("RGB", (3,2), "blue").save(buffer, format="PNG")
    png = buffer.getvalue()
    assets = attachment_store(stores.project)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        uploaded = await client.post("/api/orchestration/constraint-attachments", params=scope, files=[("files", ("full.png", png, "image/png"))])
        assert uploaded.status_code == 200, uploaded.text
        identity = uploaded.json()["attachments"][0]["id"]
        preview = await client.get("/api/orchestration/constraint-attachments/"+identity, params=scope)
        assert preview.status_code == 200 and preview.content == png
        assert (await client.get("/api/orchestration/constraint-attachments/"+identity, params={**scope,"conversation_id":"other"})).status_code == 404
        before = set(assets.root.rglob("*"))
        partial = await client.post("/api/orchestration/constraint-attachments", params=scope,
            files=[("files", ("good.txt", b"complete requirement", "text/plain")), ("files", ("bad.png", b"not an image", "image/png"))])
        assert partial.status_code == 400
        assert set(assets.root.rglob("*")) == before


@pytest.mark.asyncio
async def test_targeted_supplement_route_scope_and_receipt(stores, monkeypatch):
    PipelineStore(str(stores.project)).create_pipeline(to_pipeline_graph(spec_for(), graph_id="g_editable"))
    run_id = "e" * 32
    stores.runs.create(run_id=run_id, conversation_id="supplement-conv", task="task", attachments=[],
        profile=_profile().as_dict(), strategy="stable", flow=to_expert_flow(spec_for()))
    monkeypatch.setattr(server, "_active_conversation_id", lambda:"supplement-conv")
    runtime = SimpleNamespace(expert_run_id=run_id)
    monkeypatch.setattr(server.SESSION, "_active_queue_runtime", lambda conv:runtime)
    async def accept(_runtime, identity, text):
        from mewcode.orchestration.interventions import enqueue_intervention
        stores.runs.mutate(run_id, lambda s:(s.update(status="running"), enqueue_intervention(s,identity,text,kind="takeover")))
        return {"new":True, "message":"accepted"}
    monkeypatch.setattr(server, "_accept_expert_intervention", accept)
    body=dict(project=str(stores.project),conversation_id="supplement-conv",graph_id="g_editable",
        run_id=run_id,node_id="experiment",queue_id="request-1",text="补充交付格式")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url="http://test") as client:
        assert (await client.post("/api/orchestration/node-supplement",json=body)).status_code==403
        headers={"X-Mew-Chat-Queue":"1"}
        assert (await client.post("/api/orchestration/node-supplement",json={**body,"run_id":"other"},headers=headers)).status_code==409
        assert (await client.post("/api/orchestration/node-supplement",json=body,headers=headers)).status_code==200
    snapshot=stores.runs.load(run_id)
    assert snapshot["intervention_receipts"][0]["target_node_id"]=="experiment"
    assert "[experiment]" in snapshot["interventions"][0]["text"]
