const assert=require('node:assert/strict'),path=require('node:path');
const {pathToFileURL}=require('node:url');const {setup}=require('./workbench_dom.cjs');
const target={provider_key:'chosen',model_id:'exact-model',effort:'high'};
const old={provider_key:'missing-provider',model_id:'old-model',effort:'max'};
const catalog={models:[{...target,available:true,tools:true,efforts:['high','max']}]};
(async()=>{
  const {mountNodeModel}=await import(pathToFileURL(path.resolve(__dirname,'../frontend/static/workbench/node-model.js')));
  let cases=0;
  function fixture({run=true,readonly=false,editable=true,get}={}){
    const doc=setup(),root=doc.createElement('section');doc.body.append(root);
    const calls=[],saved=[],store={generation:1,state:{scope:{project:'project',conversation_id:'chat'}},refresh(){calls.push(['refresh']);}};
    const state={run_id:'run',node_id:'work',conversation_id:'chat',model:old,updated_at:'v1',editable,reason:editable?'':'请先暂停流程'};
    const api={get:async(url,query)=>{calls.push(['get',url,query]);return get?get(url,query):url.includes('catalog')?catalog:state;},
      post:async(url,body)=>{calls.push(['post',url,body]);return {snapshot:{updated_at:'v2'}};}};
    const view={task:{id:'task',conversation_id:'chat',run_id:run?'run':'',readonly},editor:{editable:true,basis:'draft-v1'},
      requirements:[{id:'work',kind:'node',data:{model:old}}]};
    const ui=mountNodeModel(root,{api,store,view,item:{id:'work'},onDraftSave:async operation=>saved.push(operation)});
    const load=()=>root.querySelectorAll('button')[0].click(),save=()=>root.querySelectorAll('button')[1].click();
    const choose=async()=>{const [model,effort]=root.querySelectorAll('select');model.value=JSON.stringify([target.provider_key,target.model_id]);await model.dispatch('change');effort.value=target.effort;await effort.dispatch('change');};
    return {root,ui,store,api,calls,saved,load,save,choose};
  }
  {
    const f=fixture();assert.equal(f.calls.length,0);await f.load();assert.match(f.root.textContent,/missing-provider · old-model · max/);
    assert.ok(f.root.querySelectorAll('option').some(row=>row.textContent.includes('当前不可用')));
    await f.choose();await f.save();const sent=f.calls.find(row=>row[0]==='post')[2];
    assert.deepEqual(sent.model,target);assert.deepEqual(sent.node_ids,['work']);assert.deepEqual(sent.apply_node_ids,['work']);
    assert.equal(sent.record_backup,false);assert.equal(sent.expected_updated_at,'v1');assert.equal(sent.conversation_id,'chat');
    assert.equal(f.calls.filter(row=>row[0]==='post').length,1);assert.ok(!f.calls.some(row=>String(row[1]).includes('expert-profile')));
    assert.match(f.root.textContent,/chosen · exact-model · high/);cases++;
  }
  {
    const f=fixture({run:false});await f.load();await f.choose();await f.save();
    assert.deepEqual(f.saved,[{action:'upsert',kind:'node',id:'work',fields:{data:{model:target}}}]);
    assert.ok(!f.calls.some(row=>row[0]==='post'||String(row[1]).includes('expert-runs')));cases++;
  }
  for(const options of [{readonly:true},{editable:false}]){
    const f=fixture(options);await f.load();assert.equal(f.root.querySelectorAll('select').length,0);
    assert.equal(f.root.querySelectorAll('button')[1].hidden,true);await f.save();assert.ok(!f.calls.some(row=>row[0]==='post'));cases++;
  }
  {
    const f=fixture();await f.load();await f.choose();f.api.post=async()=>{throw Error('检查点已变化，请重新读取');};await f.save();
    assert.match(f.root.textContent,/检查点已变化/);assert.match(f.root.textContent,/missing-provider · old-model/);cases++;
  }
  {
    let finish;const promise=new Promise(resolve=>finish=resolve);
    const f=fixture({get:async url=>url.includes('catalog')?catalog:promise});const loading=f.load();f.ui.destroy();
    finish({run_id:'run',node_id:'work',conversation_id:'chat',model:target,editable:true});await loading;
    assert.equal(f.root.querySelectorAll('select').length,0);cases++;
  }
  {
    const f=fixture();await f.load();await f.choose();f.store.generation++;await f.save();assert.ok(!f.calls.some(row=>row[0]==='post'));cases++;
  }
  console.log(`workbench_node_model: ${cases} cases passed`);
})().catch(error=>{console.error(error);process.exitCode=1;});
