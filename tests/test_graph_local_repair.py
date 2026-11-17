"""A single scoped repair preserves facts and cannot bypass execution checks."""
import asyncio
import copy
import json

import httpx
import pytest

from frontend import orch_drafts
from mewcode.expert_group import ExpertExecutionPolicy, ExpertRoleResult
from mewcode.experts.loop_runtime import run_bounded_loop
from mewcode.orchestration.convert import from_expert_flow, to_expert_flow
from mewcode.orchestration.draft_sources import digest, read_sources
from mewcode.orchestration.draft_store import DraftStore
from mewcode.orchestration.graph_local_repair import SCHEMA, preview_repair
from mewcode.orchestration.loops import LoopContract
from mewcode.orchestration.workflow_draft import project_graph, seed_document
from tests.test_generic_loops import report, spec_for
from tests.test_graph_autobuild import make_app
from tests.test_workflow_drafts import api_env, ready

API = "/api/orchestration/graph-generation"


def prepare(store, sources):
    doc = seed_document(spec_for().as_dict())
    for e in doc['entries']:
        e['evidence'] = 'proposal'
        if e['kind'] == 'node':
            e['data'].update(outputs=['明确产物'], acceptance=['按证据核验'])
    doc['entries'] = [e for e in doc['entries'] if not (e['kind'] == 'edge' and e['data']['to_node'] == 'done')]
    ready(store, sources, doc)
    packet = store.request_local_graph_repair(None)
    proposal = {'schema':SCHEMA, 'ticket':packet['ticket'], 'summary':'补齐已存在判定到交付的依赖',
        'upserts':[{'id':'exit_edge','kind':'edge','title':'通过后交付','body':'核验通过后交付',
                    'data':{'from_node':'check','to_node':'done','condition':'pass'},'rationale':'补齐原要求中的交接，不更改业务事实'}]}
    return proposal


def test_saved_canvas_structural_entries_are_editable_only_for_explicit_repair_scope(tmp_path):
    base = spec_for().as_dict()
    store = DraftStore(tmp_path, "paper")
    with store.db() as db:
        state = store._get(db)
        state.update(revision=1, document=seed_document(base), source_ids=[], processed_ids=[], warnings=[], snapshot={})
        store._put(db, state)
    candidate = store.prepare_local_repair(base, expected_revision=1,
                                           expected_base_graph_hash=digest(base))
    packet = store.request_local_graph_repair(base)
    edge = next(entry for entry in candidate["document"]["entries"] if entry["kind"] == "edge")
    proposal = {"schema": SCHEMA, "ticket": packet["ticket"], "summary": "核对已有结构连线",
                "upserts": [{"id": edge["id"], "kind": "edge", "data": copy.deepcopy(edge["data"]),
                             "rationale": "显式修复当前已保存图的结构连线"}]}
    preview = store.preview_graph_repair(base, proposal)
    assert preview["changes"] and edge["id"] in store.get()["repair_request"]["saved_structural_ids"]

    with store.db() as db:
        state = store._get(db)
        target = next(entry for entry in state["candidate"]["document"]["entries"] if entry["id"] == edge["id"])
        target["evidence"] = "manual"
        store._put(db, state)
    with pytest.raises(ValueError, match="受保护条目"):
        store.preview_graph_repair(base, proposal)


def test_saved_canvas_repair_preserves_processed_prefix_when_adopted_and_saved(tmp_path):
    base = spec_for().as_dict()
    for node in base["nodes"]:
        node["plan_brief"] = {"steps": ["核对内容"], "outputs": ["明确产物"],
                               "acceptance": ["按证据核验"]}
    store = DraftStore(tmp_path, "paper")
    document = seed_document(base)
    for entry in document["entries"]:
        if entry["kind"] == "node":
            entry["data"].update(outputs=["明确产物"], acceptance=["按证据核验"])
    with store.db() as db:
        state = store._get(db)
        state.update(revision=1, document=document, source_ids=["old"],
                     processed_ids=["old"], selected_ids=[], warnings=[], snapshot={})
        store._put(db, state)
    candidate = store.prepare_local_repair(base, expected_revision=1,
                                           expected_base_graph_hash=digest(base))
    assert candidate["processed_ids"] == []
    assert candidate["base_processed_ids"] == ["old"]
    packet = store.request_local_graph_repair(base)
    edge = next(entry for entry in candidate["document"]["entries"] if entry["kind"] == "edge")
    proposal = {"schema": SCHEMA, "ticket": packet["ticket"], "summary": "核对已有结构连线",
                "upserts": [{"id": edge["id"], "kind": "edge", "data": copy.deepcopy(edge["data"]),
                             "rationale": "显式修复当前已保存图的结构连线"}]}
    preview = store.preview_graph_repair(base, proposal)
    adopted = store.adopt_graph_repair(base, preview["id"])
    adopted_candidate = adopted["candidate"]
    assert adopted_candidate["processed_ids"] == []
    assert adopted_candidate["base_processed_ids"] == ["old"]
    store.confirm(adopted_candidate["id"], adopted_candidate["sequence"])
    saved = store.get()
    assert saved["processed_ids"] == ["old"]


def test_preview_adopt_provenance_and_cas(tmp_path):
    store = DraftStore(tmp_path, 'paper')
    proposal = prepare(store, [{'id':'s1','role':'user','text':'有据核验'}])
    before = store.get()['candidate']
    preview = store.preview_graph_repair(None, proposal)
    assert preview['validation']['valid'] and preview['local']
    assert preview['changes'][0]['after']['evidence'] == 'ai_completed'
    assert store.get()['candidate'] == before
    store.adopt_graph_repair(None, preview['id'])
    after = store.get()['candidate']
    assert after['processed_ids'] == before['processed_ids']
    original = {e['id']:e for e in before['document']['entries']}
    assert all(e == original[e['id']] for e in after['document']['entries'] if e['kind']=='node')
    assert next(e for e in after['document']['entries'] if e['id']=='exit_edge')['evidence']=='ai_completed'
    with pytest.raises(ValueError):
        store.preview_graph_repair(None, proposal)


@pytest.mark.parametrize('field,value', [('criteria',[{'criterion_id':'REQ-01','acceptance_test':'Always accept'}]),
                                      ('control_mode','team'),('token_budget',5_000_000),('required_node_ids',[])])
def test_existing_loop_policy_is_frozen(tmp_path, field, value):
    store = DraftStore(tmp_path, 'paper')
    proposal = prepare(store, [{'id':'s1','role':'user','text':'有据核验'}])
    loop = next(e for e in store.get()['candidate']['document']['entries'] if e['kind']=='loop')
    proposal['upserts'].append({'id':loop['id'],'data':{field:value},'rationale':'试图修改验收或预算'})
    with pytest.raises(ValueError,match='既有循环'):
        store.preview_graph_repair(None, proposal)


@pytest.mark.parametrize('field,value', [('body','删除验收'),('data',{'acceptance':[]}),('state','retired')])
def test_business_node_changes_rejected(tmp_path, field, value):
    store = DraftStore(tmp_path, 'paper')
    proposal = prepare(store, [{'id':'s1','role':'user','text':'有据核验'}])
    proposal['upserts'].append({'id':'work',field:value,'rationale':'不能更改要求'})
    with pytest.raises(ValueError):
        store.preview_graph_repair(None, proposal)


def test_coordinator_roundtrips_and_has_no_write_or_delegation_tools():
    raw = spec_for().as_dict()
    raw['meta']['workflow_draft']={'parents':{'work':'prepare'},'coordination_nodes':['prepare']}
    raw['nodes'][0]['tools']=['Bash','Agent','WriteFile']
    from mewcode.orchestration.spec import OrchestrationSpec
    flow = to_expert_flow(OrchestrationSpec.from_value(raw))
    node = flow.node_map['prepare']
    assert node.role == 'planner' and set(node.tools)=={'ReadFile','Glob','Grep'}
    assert '[COORDINATE]' in node.responsibility
    restored=from_expert_flow(flow)
    assert restored.meta['workflow_draft']==raw['meta']['workflow_draft']
    assert restored.node_map()['prepare'].goal==raw['nodes'][0]['goal']
    seeded=seed_document(restored.as_dict())
    assert next(e for e in seeded['entries'] if e['id']=='prepare')['data']['execution_mode']=='coordinate'


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['pass','failed_audit','skipped_required'])
async def test_exit_cannot_hide_required_failed_or_skipped_work(fault):
    raw=spec_for().as_dict()
    raw['nodes'].insert(2,{'node_id':'inner_audit','kind':'audit','label':'内审','goal':'核验'})
    raw['edges']=[e for e in raw['edges'] if (e.get('from'),e.get('to'))!=('work','check')]
    raw['edges'] += [{'from':'work','to':'inner_audit'}, {'from':'inner_audit','to':'check','condition':'pass'},
                     {'from':'inner_audit','to':'check','condition':'fail'}]
    raw['meta']['loops'][0]['node_ids'].append('inner_audit')
    raw['meta']['loops'][0]['required_node_ids']=['work','inner_audit']
    if fault=='skipped_required':
        raw['nodes'].insert(2,{'node_id':'write','kind':'implement','label':'写入','goal':'写入'})
        raw['edges'] += [{'from':'inner_audit','to':'write','condition':'pass'}, {'from':'write','to':'check','condition':'pass'}]
        raw['meta']['loops'][0]['node_ids'].append('write')
        raw['meta']['loops'][0]['required_node_ids']=['work','write']
    from mewcode.orchestration.spec import OrchestrationSpec
    flow=to_expert_flow(OrchestrationSpec.from_value(raw))
    contract=LoopContract.from_value(flow.meta['loops'][0])
    calls=[]
    async def runner(node, incoming, policy, prompt):
        calls.append(node.node_id)
        if node.node_id=='check': return ExpertRoleResult(text='证据',loop_decision=report(policy.loop_context))
        if node.node_id=='inner_audit': return ExpertRoleResult(text='核验',verdict={'verdict':'PASS' if fault=='pass' else 'NEEDS_ATTENTION'})
        return ExpertRoleResult(text='产物')
    async def emit(_): pass
    _, result=await run_bounded_loop(contract=contract,flow=flow,saved={},run_node=runner,emit=emit,
        policy_for=lambda _:ExpertExecutionPolicy('stable',False,0),incoming=[])
    assert (result.status=='completed') == (fault=='pass')
    if fault!='pass': assert '必需阶段' in result.text


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome',['success','invalid','stop','scope_switch'])
async def test_repair_protocol_failures_are_assessed_without_implicit_adoption(api_env, monkeypatch, outcome):
    current, plans, path, owner=api_env
    sources,_,_=read_sources(path)
    store=DraftStore(owner['project'],'paper')
    prepare(store,sources)
    before=copy.deepcopy(store.get()['candidate'])
    entered=asyncio.Event(); release=asyncio.Event(); calls=[]
    async def model(client, **kwargs):
        calls.append(client)
        assert client==('chosen','high')
        assert kwargs['total_timeout'] is None
        entered.set()
        await release.wait()
        data=json.loads(kwargs['user_text'])
        proposal={'schema':SCHEMA,'ticket':data['ticket'],'summary':'补齐交接','upserts':'invalid list'}
        if outcome!='invalid':
            proposal['upserts']=[{'id':'exit_edge','kind':'edge','title':'核验后交付','body':'核验后交付',
                'data':{'from_node':'check','to_node':'done','condition':'pass'},'rationale':'原图末端缺边'}]
        return json.dumps(proposal)
    monkeypatch.setattr(orch_drafts,'collect_model_text',model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app()),base_url='http://test') as client:
        task=asyncio.create_task(client.post(API+'/repair',json={**owner,'action':'local'}))
        await asyncio.wait_for(entered.wait(),2)
        if outcome=='stop':
            assert (await client.post(API+'/action',json={**owner,'action':'stop'})).status_code==200
        if outcome=='scope_switch': current['cid']='another'
        release.set()
        response=await task
        assert response.status_code==(200 if outcome=='success' else 409),response.text
    assert len(calls)==(2 if outcome=='invalid' else 1) and store.get()['candidate']==before
    assert plans.get(owner['project']) is None
    assert bool(store.get().get('repair_preview'))==(outcome=='success')


@pytest.mark.asyncio
async def test_stop_during_source_refresh_never_starts_model(api_env, monkeypatch):
    _, _, path, owner=api_env
    sources,_,_=read_sources(path)
    store=DraftStore(owner['project'],'paper')
    prepare(store,sources)
    before=copy.deepcopy(store.get()['candidate'])
    entered=asyncio.Event(); release=asyncio.Event(); calls=[]
    original=orch_drafts.asyncio.to_thread
    async def delayed(fn,*args,**kwargs):
        if fn is orch_drafts.read_sources:
            entered.set()
            await release.wait()
        return await original(fn,*args,**kwargs)
    async def model(*args,**kwargs): calls.append(1); return '{}'
    monkeypatch.setattr(orch_drafts.asyncio,'to_thread',delayed)
    monkeypatch.setattr(orch_drafts,'collect_model_text',model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app()),base_url='http://test') as client:
        task=asyncio.create_task(client.post(API+'/repair',json={**owner,'action':'local'}))
        await asyncio.wait_for(entered.wait(),2)
        response=await client.post(API+'/action',json={**owner,'action':'stop'})
        assert response.status_code==200
        release.set()
        assert (await task).status_code==409
    assert not calls and store.get()['candidate']==before
    assert store.get().get('repair_preview') is None
