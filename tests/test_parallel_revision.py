import copy

import pytest

from mewcode.expert_group import ExpertRoleResult
from mewcode.kernel import collaboration as ledger
from mewcode.kernel.acceptance import Acceptance
from mewcode.orchestration.collab_compile import compile_plan
from mewcode.orchestration.convert import to_expert_flow
from mewcode.orchestration.loops import LoopContract
from mewcode.orchestration.parallel_revision import revise_parallel_flow, saved_branch_delivery
from tests.test_collaboration_mode import flow as basic, delivery, passing_report, RUN_ID
from tests.test_parallel_fusion import spec


def fixture():
    from dataclasses import asdict
    from mewcode.experts.flow import ExpertFlowSpec
    new = to_expert_flow(spec()).as_dict()
    new['meta'].update(orchestration_mode='collaborative',
        collaboration_spec=copy.deepcopy(basic().meta['collaboration_spec']))
    new['meta']['node_plan_briefs'] = {n['node_id']: {'known_inputs':['原始输入'], 'outputs':['结果'],
        'acceptance':['内容及证据完整']} for n in new['nodes']}
    old = copy.deepcopy(new)
    old['meta']['loops'][0].pop('parallel_groups')
    old['edges'] = [{'from_node':a, 'to_node':b} for a,b in
        [('start','a'),('a','b'),('b','c'),('c','fuse'),('fuse','audit'),('audit','done')]]
    old,new = ExpertFlowSpec.from_value(old),ExpertFlowSpec.from_value(new)
    row = {'run_id':RUN_ID,'status':'paused','active_node_ids':[], 'locked_flow':old.as_dict(),
        'locked_models':{},'authorization_manifest':{'approved':False},'attempt':7,'loop_states':{}}
    value = ledger.initialize(row,compile_plan(old))
    results={}
    for key in ('start','a','b','c'):
        attempt=ledger.dispatch(value,key)
        attempt['delivery']=asdict(delivery(key))
        value['attempts'][attempt['id']]['delivery']=attempt['delivery']
        ledger.complete(value,attempt['id'],passing_report(value,key))
        results[key]=attempt['delivery']
    loop=LoopContract.from_value(old.meta['loops'][0])
    row['loop_states']['pages']={'round':1,'round_open':True,'tokens':321,'elapsed':19,'status':'running',
        'round_results':results,'contract_hash':loop.contract_hash,'contract_snapshot':loop.as_dict(),
        'completed_items':[],'item_index':0,'item_round':1,'reports':{},'history':[], 'scores':None,
        'score':0,'stalled':0,'evidence_history':[], 'active_node':'','active_at':None}
    return row,new


def revise(row,new):
    return revise_parallel_flow(row,new,source_ref='user:parallel',request='三路并发，融合全部有效结果，审计关键疑点后定向返修。')


def test_revision_keeps_original_receipts_budget_and_rechecks_changed_inputs():
    row,new=fixture(); before=copy.deepcopy(row)
    revised=revise(row,new); value=revised['collaboration']
    assert row==before
    assert revised['authorization_manifest']==row['authorization_manifest']
    assert revised['locked_models']==row['locked_models'] and revised['attempt']==7
    assert set(revised['loop_states']['pages']['round_results'])=={'start','a'}
    assert revised['loop_states']['pages']['tokens']==321 and revised['loop_states']['pages']['elapsed']==19
    assert ledger.valid_certificate(value,'a') and not ledger.valid_certificate(value,'b')
    assert set(value['parallel_delivery_rechecks'])=={'b','c'}
    assert value['attempts']==row['collaboration']['attempts']
    ledger.initialize(revised,compile_plan(new))
    changed=copy.deepcopy(revised);changed['locked_flow']['nodes'][0]['responsibility']='unreviewed'
    with pytest.raises(ValueError,match='另一版'):
        ledger.initialize(changed,compile_plan(new))


def test_revision_preserves_published_nodes_and_proves_resume_lineage():
    from mewcode.orchestration.revisions import _published_execution_flow
    row,new=fixture()
    runtime=copy.deepcopy(row['locked_flow'])
    extra=copy.deepcopy(runtime['nodes'][0]);extra['node_id']='investigation'
    runtime['nodes'].append(extra)
    runtime['edges'].append({'from_node':'investigation','to_node':'start','condition':'always','router':'default'})
    value=row['collaboration']; hashed=ledger.digest(runtime)
    value.update(runtime_flow=runtime, runtime_flow_hash=hashed,
        flow_versions=[{'plan_version':1,'flow':copy.deepcopy(runtime),'flow_hash':hashed,'publication_id':'pub'}],
        graph_publications={'pub':{'status':'published','new_plan_version':1,'fragment':{'flow':copy.deepcopy(runtime)}}})
    row['flow_state']={'loop_state_key':'pages','loop_state':copy.deepcopy(row['loop_states']['pages'])}
    revised=revise(row,new)
    assert 'investigation' in _published_execution_flow(revised).node_map
    assert revised['collaboration']['graph_publications']==value['graph_publications']
    assert revised['flow_state']['loop_state']==revised['loop_states']['pages']
    revised['collaboration']['graph_publications']['pub']['status']='prepared'
    with pytest.raises(ValueError):_published_execution_flow(revised)


@pytest.mark.parametrize('fault',['running','active','model','capability','limit','gate','source','other_goal'])
def test_migration_rejects_changes_outside_requested_parallel_scope(fault):
    row,new=fixture(); raw=new.as_dict()
    if fault=='running':row['status']='running'
    if fault=='active':row['active_node_ids']=['b']
    if fault=='model':raw['nodes'][1]['model']={'provider_key':'other','model_id':'changed','effort':'max'}
    if fault=='capability':raw['nodes'][1]['capabilities']['write_paths']=['new_path']
    if fault=='limit':raw['meta']['loops'][0]['token_budget']=999999
    if fault=='gate':raw['edges']=[e for e in raw['edges'] if e['to_node']!='done']+[{'from_node':'fuse','to_node':'done'}]
    if fault=='source':raw['meta']['collaboration_spec']['goal']='different'
    if fault=='other_goal':raw['nodes'][1]['responsibility']='changed independent recognition'
    with pytest.raises(ValueError):revise(row,raw)


def test_saved_delivery_is_not_reused_when_its_content_changes(tmp_path):
    row,new=fixture(); revised=revise(row,new); value=revised['collaboration']
    assert saved_branch_delivery(value,'b',Acceptance(tmp_path))['text']=='b'
    saved=value['parallel_delivery_rechecks']['b']['source_attempt_id']
    value['attempts'][saved]['delivery']['text']='tampered'
    assert saved_branch_delivery(value,'b',Acceptance(tmp_path)) is None


def test_explicit_scopes_narrow_existing_shared_write_roots(tmp_path):
    from mewcode.expert_group import ExpertExecutionPolicy
    from mewcode.orchestration.parallel import parse_groups, branch_policy
    from mewcode.orchestration.validate import validate_spec
    raw=spec()
    for node in raw.nodes:
        if node.node_id in {'a','b','c'}:node.capabilities['write_paths']=['results']
    group=raw.meta['loops'][0]['parallel_groups'][0]
    group['write_scopes']={key:['results/'+key] for key in ('a','b','c')}
    validate_spec(raw)
    flow=to_expert_flow(raw)
    policy=branch_policy(ExpertExecutionPolicy('stable',False,0,work_dir=str(tmp_path)),flow.node_map['a'],parse_groups([group])[0])
    assert policy.expansion_write_paths==(str((tmp_path/'results/a').resolve()),)
    group['write_scopes']['a']=['outside']
    with pytest.raises(ValueError,match='扩大'):validate_spec(raw)


@pytest.mark.asyncio
async def test_migrated_host_rechecks_saved_branches_without_model_replay(tmp_path):
    from dataclasses import replace
    from tests.test_collaboration_integration import orchestrator, run
    from tests.test_iterative_collaboration import retry_store
    from tests.test_generic_loops import report
    row,new=fixture()
    new.meta['loops'][0]['parallel_groups'][0]['branch_timeout_seconds']=180
    candidate=revise(row,new)
    store=retry_store(tmp_path,new)
    store.mutate(RUN_ID,lambda current:current.update(candidate),durable=True)
    invoked=[]
    async def runner(node,model,prompt,policy,*args):
        invoked.append(node.node_id)
        assert node.node_id not in {'start','a','b','c'}
        result=delivery(node.node_id)
        return replace(result,loop_decision=report(policy.loop_context)) if node.node_id=='audit' else result
    async def review(*args):return {'passed':True,'evidence':['重新核对交付内容']}
    result=await run(orchestrator(store,tmp_path,runner,review),store,new)
    assert result.status=='completed',result.final_summary
    assert invoked==['fuse','audit','done']
    value=store.load(RUN_ID)['collaboration']
    assert all(ledger.valid_certificate(value,key) for key in ('b','c','fuse','done'))
    assert {a['contract_id'] for a in value['attempts'].values() if a.get('rechecked_from')}=={'b','c'}
