const assert=require('node:assert/strict');
const {setup,settle}=require('./workbench_dom.cjs');
const flush=async()=>{for(let i=0;i<4;i++)await settle();};
const find=(root,text)=>root.querySelectorAll('button').find(row=>row.textContent===text);
function harness(){const doc=setup(),root=doc.createElement('section');doc.body.append(root);return {root,store:{generation:1,state:{scope:{project:'p',conversation_id:'c'}},refresh(){this.refreshed=true;}}};}
const task={id:'task',run_id:'run',mode:'classic',status:'已暂停'};
(async()=>{
 const {mountMaintenance}=await import('../frontend/static/workbench/maintenance.js');
 const {mountSettings}=await import('../frontend/static/workbench/settings.js');
 const {mountRunControls}=await import('../frontend/static/workbench/run-controls.js');
 const {mountExtras}=await import('../frontend/static/workbench/decision-extras.js');let cases=0;
 {
  const h=harness(),calls=[];let release;
  const ui=mountMaintenance(h.root,{store:h.store,api:{post:(path,body)=>(calls.push({path,body}),new Promise(r=>release=r))},openInbox(){}});
  ui.update({task,maintenance:[],plan:[],inbox:[]});h.root.querySelector('textarea').value='保留我的维护草稿';
  const opened=ui.open('fix');assert.equal(calls.length,1);await find(h.root,'发送维护要求').click();assert.equal(calls.length,1);
  release({id:'m',revision:0,status:'ready',mode:'fix',messages:[]});await opened;assert.equal(h.root.querySelector('textarea').value,'保留我的维护草稿');
  ui.update({task,maintenance:[{id:'m',revision:1,status:'ready',mode:'fix',messages:[{role:'assistant',text:'<img src=x>调查完成'}]}],plan:[],inbox:[{type:'maintenance_proposal',context:{id:'m'}}]});
  assert.equal(h.root.querySelector('textarea').value,'保留我的维护草稿');assert.equal(h.root.querySelectorAll('img').length,0);assert.equal(find(h.root,'去待你处理作答').hidden,false);
  assert.equal(h.root.querySelectorAll('form').length,0);assert.equal(h.root.querySelectorAll('input').length,0);ui.destroy();cases++;
 }
 {
  const h=harness(),extras=mountExtras({type:'maintenance_proposal',context:{proposal:{changes:[{title:'标签',before:'a',after:'b'}],run_actions:[{id:'action',action:'set_node_tools',before:[],after:['ReadFile'],requires_confirmation:true}]}}},{});
  h.root.append(extras.element);assert.deepEqual((await extras.read({})).confirmed_run_action_ids,[]);h.root.querySelector('input').checked=true;
  assert.deepEqual((await extras.read({})).confirmed_run_action_ids,['action']);extras.disable(true);assert.equal(h.root.querySelector('input').disabled,true);cases++;
 }
 {
  const h=harness(),calls=[],ref={provider_key:'configured',model_id:'chosen',effort:'high'};
  const snapshot={run_id:'run',conversation_id:'c',updated_at:'frozen',status:'paused',locked_flow:{nodes:[{node_id:'work',label:'核对'}]}};
  const api={get:async path=>path==='/api/models/catalog'?{models:[{...ref,efforts:['high','max']}]}:path==='/api/expert-profile'?{project:'p',configured:true,profile:{manager:ref,executor:ref,auditor:ref,planner_routes:{code:['unchanged']}}}:snapshot,
   put:async(path,body)=>{calls.push({path,body});return{profile:body};},post:async(path,body)=>{calls.push({path,body});return{snapshot:{...snapshot,updated_at:'after'}};}};
  const ui=mountSettings(h.root,{api,store:h.store});ui.update({task});await ui.open();await find(h.root,'保存项目配置').click();await flush();
  assert.equal(calls[0].body.activate,false);assert.deepEqual(calls[0].body.planner_routes,{code:['unchanged']});assert.equal(calls[0].body.role_models.reviewer.model_id,'chosen');
  await find(h.root,'应用辅助执行器到暂停任务').click();await flush();assert.equal(calls[1].body.expected_updated_at,'frozen');assert.equal(calls[1].body.model,null);assert.equal(calls[1].body.conversation_id,'c');
  ui.update({task:{...task,mode:'collaborative'}});
  assert.equal(find(h.root,'应用核查模型到暂停任务').hidden,false);
  await find(h.root,'应用核查模型到暂停任务').click();await flush();
  assert.equal(calls[2].path,'/api/expert-runs/run/verifier-model');assert.deepEqual(calls[2].body.model,ref);
  assert.equal(calls[2].body.expected_updated_at,'after');assert.equal(calls[2].body.conversation_id,'c');
  ui.destroy();cases++;
 }
 {
  const h=harness(),calls=[];let jumped=0;
  const snapshot={run_id:'run',conversation_id:'c',updated_at:'v1',status:'paused',node_states:{done:{status:'completed'},repair:{status:'failed'}},locked_flow:{nodes:[{node_id:'done',label:'已完成步骤'},{node_id:'repair',label:'失败步骤'}],meta:{loops:[{loop_id:'loop'}]}}};
  const api={get:async path=>path.endsWith('/checkpoints')?{conversation_id:'c',updated_at:'v1',can_restore:true,checkpoints:[{checkpoint_id:'point',label:'已保存',attempt:1,status:'paused'}]}:snapshot,post:async(path,body)=>(calls.push({path,body}),{})};
  const ui=mountRunControls(h.root,{api,store:h.store,openInbox:()=>jumped++});ui.update({task});await ui.open();await find(h.root,'核对恢复至此').click();await flush();
  assert.equal(calls[0].path,'/api/workbench/run-action');assert.equal(calls[0].body.operation,'checkpoint');assert.equal(calls[0].body.expected_updated_at,'v1');assert.equal(jumped,1);
  const total=h.root.querySelectorAll('input')[0];total.value='200000';await find(h.root,'核对 Token 总额授权').click();await flush();assert.equal(calls[1].body.ceiling,200000);
  await find(h.root,'核对重试此步骤').click();await flush();assert.equal(calls[2].body.operation,'retry_node');assert.equal(calls[2].body.node_id,'repair');
  await find(h.root,'核对从此步骤后接续').click();await flush();assert.equal(calls[3].body.operation,'resume_after_node');assert.equal(calls[3].body.node_id,'done');
  assert.ok(calls.every(row=>!row.path.includes('/restore')));ui.destroy();cases++;
 }
 {
  const h=harness(),releases=[];const ui=mountRunControls(h.root,{store:h.store,api:{get:()=>new Promise(r=>releases.push(r))},openInbox(){}});ui.update({task});const promise=ui.open();h.store.generation++;ui.update({task:{...task,id:'next'}});releases.forEach(release=>release({}));await promise;assert.ok(!h.root.textContent.includes('操作已'));ui.destroy();cases++;
 }
 {
  const h=harness(),calls=[],snapshot={conversation_id:'c',updated_at:'v2',status:'running',node_states:{work:{recovery_case:{case_id:'case',status:'open',report:{summary:'正在调查'}}}},events:[],locked_flow:{}};
  const ui=mountRunControls(h.root,{store:h.store,openInbox(){},api:{get:async path=>path.endsWith('/checkpoints')?{conversation_id:'c',updated_at:'v2',checkpoints:[]}:snapshot,post:async(path,body)=>(calls.push(body),{})}});
  ui.update({task:{...task,status:'进行中'}});await ui.open();const stop=find(h.root,'核对停止本次修复');assert.ok(!stop.disabled);await stop.click();await flush();
  assert.equal(calls[0].operation,'stop_repair');assert.equal(calls[0].case_id,'case');ui.destroy();cases++;
 }
 console.log(`workbench_controls: ${cases} maintenance/model/checkpoint cases passed`);
})().catch(error=>{console.error(error);process.exitCode=1;});
