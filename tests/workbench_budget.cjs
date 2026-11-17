const assert = require('node:assert/strict');
const {pathToFileURL} = require('node:url');
const path = require('node:path');
const {setup} = require('./workbench_dom.cjs');
(async()=>{
  setup();
  const {mountExtras} = await import(pathToFileURL(path.resolve(__dirname,'../frontend/static/workbench/decision-extras.js')));
  const item = {type:'collaboration_recovery', context:{budget:{scope:'lineage',kind:'attempts'}}};
  const extras = mountExtras(item,{api:{},scope:{}});
  const input = extras.element.querySelectorAll('input').find(row=>row.type==='number');
  assert.ok(input);
  assert.match(extras.element.textContent,/当前任务分支.*执行尝试/);
  input.value='3';
  assert.deepEqual(await extras.read({action:'add_budget'}),{budget_amount:3});
  assert.deepEqual(await extras.read({action:'revise'}),{});
  input.value='';
  assert.deepEqual(await extras.read({action:'enable_elastic_budget'}),{});
  for(const bad of ['', '0', '-1', '1.5', 'NaN']){
    input.value=bad;
    await assert.rejects(()=>extras.read({action:'add_budget'}),/正整数/);
  }
  const recovery = mountExtras({type:'collaboration_recovery', context:{kind:'coordinator_unavailable'}},{api:{},scope:{}});
  const selector = recovery.element.querySelector('select');
  assert.match(recovery.element.textContent,/换绑协调服务/);
  await assert.rejects(()=>recovery.read({action:'switch_coordinator'}),/请选择已配置/);
  selector.value = JSON.stringify({provider_key:'fixture',model_id:'coordinator',effort:''});
  assert.equal((await recovery.read({action:'switch_coordinator'})).model.model_id,'coordinator');
  assert.deepEqual(await recovery.read({action:'resume_coordinator'}),{});
  const verifier = mountExtras({type:'collaboration_recovery',context:{verifier_switch_available:true}},{api:{},scope:{}});
  const picks = verifier.element.querySelectorAll('select');
  assert.match(verifier.element.textContent,/更换当前步骤的验收模型/);
  await assert.rejects(()=>verifier.read({action:'switch_verifier'}),/请选择已配置的验收模型/);
  picks[0].value = JSON.stringify({provider_key:'old-executor',model_id:'execute',effort:''});
  picks[1].value = JSON.stringify({provider_key:'other-verifier',model_id:'verify',effort:'high'});
  assert.deepEqual((await verifier.read({action:'switch_verifier'})).model,{provider_key:'other-verifier',model_id:'verify',effort:'high'});
  assert.equal((await verifier.read({action:'switch_model'})).model.model_id,'execute');
  assert.deepEqual(await verifier.read({action:'retry'}),{});
  const recheck = mountExtras({type:'collaboration_recovery',context:{kind:'execution_evidence_available'}},{api:{},scope:{}});
  assert.equal(recheck.element.querySelectorAll('select').length,0);
  assert.deepEqual(await recheck.read({action:'recheck_execution_evidence'}),{});
  const {renderFacts} = await import(pathToFileURL(path.resolve(__dirname,'../frontend/static/workbench/step.js')));
  const facts = document.createElement('div');
  renderFacts(facts,{detail:{budget:[{kind:'attempts',label:'执行尝试',used:11,reserved:1,
    limit:null,nominal_limit:14,allocation_mode:'elastic',shared_remaining:587}]}});
  assert.match(facts.textContent,/已用 11，预留 1，弹性共享，任务总额剩余 587/);
  assert.doesNotMatch(facts.textContent,/上限 null|上限 14/);
  console.log('workbench_budget: exact amount and invalid inputs passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
