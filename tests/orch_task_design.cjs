const assert=require('node:assert/strict');
const {setup,settle}=require('./workbench_dom.cjs');
const find=(root,text)=>root.querySelectorAll('button').find(el=>el.textContent===text);
const flush=async()=>{for(let i=0;i<4;i++)await settle();};
const design=(extra={})=>({task_id:'task',revision:3,status:'designing',intent:'execute',intent_authority:{source:'host_entry',intent:'execute'},orchestration_mode:'classic',specification:{entries:[]},...extra});
const view=(d)=>({version:String(d.revision),task:{id:d.task_id,run_id:d.run_id||'',mode:d.orchestration_mode,design_stage:!d.run_id,design_revision:d.revision}});
function harness(mountDesign,api,host={}){const doc=setup(),controls=doc.createElement('div'),spec=doc.createElement('section');doc.body.append(controls,spec);const store={generation:1,state:{scope:{project:'p',conversation_id:'c'}},refresh(){this.refreshes=(this.refreshes||0)+1;}};const ui=mountDesign(controls,spec,{api,store,host});return {doc,controls,spec,store,ui};}
(async()=>{
 const {mountDesign}=await import('../frontend/static/workbench/design.js');let cases=0;
 {
  let saved=null;const posts=[],chats=[];
  const h=harness(mountDesign,{get:async()=>({conversation_id:'c',task_design:saved}),post:async(_,body)=>{posts.push(body);saved=design({intent:'plan'});return {conversation_id:'c',task_design:saved};}},{designStarted:data=>chats.push(data)});
  h.ui.update(null);await flush();await h.ui.compose();await flush();
  assert.equal(posts.length,1);assert.equal(posts[0].action,'start');assert.equal(posts[0].intent,'plan');
  h.ui.compose();assert.equal(posts.length,1);assert.equal(chats.length,2);h.ui.destroy();cases++;
 }
 {
  let saved=null;const generated=[],posts=[];
  const h=harness(mountDesign,{get:async()=>({conversation_id:'c',task_design:saved}),post:async(_,body)=>{posts.push(body);saved=design({intent:'plan'});return {conversation_id:'c',task_design:saved};}},{buildGraph:scope=>generated.push(scope)});
  h.ui.update(null);await flush();await h.ui.compose({generate:true});await flush();
  assert.equal(posts.length,1);assert.equal(posts[0].intent,'plan');assert.deepEqual(generated,[{project:'p',conversation_id:'c'}]);
  h.ui.compose();assert.equal(generated.length,1);h.ui.compose({generate:true});assert.equal(generated.length,2);
  h.ui.update({...view(saved),task:{...view(saved).task,readonly:true}});await flush();h.ui.compose({generate:true});assert.equal(generated.length,2);h.ui.destroy();cases++;
 }
 {
  const saved=design({intent:'plan',specification:{entries:[{id:'old',kind:'node',title:'旧材料',body:'旧材料',sources:[]}]}});
  const h=harness(mountDesign,{get:async()=>({conversation_id:'c',task_design:saved})});
  h.ui.update({...view(saved),requirements:[{id:'current',kind:'node',title:'画布当前内容',body:'最新节点',sources:[]}]});await flush();
  assert.match(h.spec.textContent,/画布当前内容/);assert.doesNotMatch(h.spec.textContent,/旧材料/);h.ui.destroy();cases++;
 }
 for(const mode of ['classic','collaborative'])for(const intent of ['plan','execute']){
  let saved=null;const posts=[];const h=harness(mountDesign,{get:async()=>({project:'p',conversation_id:'c',task_design:saved}),post:async(_,body)=>{posts.push(body);saved=design({intent,orchestration_mode:mode});return {task_design:saved};}});
  assert.equal(find(h.controls,'设计并执行').disabled,true);h.ui.update(null);assert.equal(find(h.controls,'设计并执行').disabled,true);await flush();h.controls.querySelector('select').value=mode;
  await find(h.controls,intent==='plan'?'只生成编排图':'设计并执行').click();await flush();
  assert.deepEqual(posts,[{project:'p',conversation_id:'c',action:'start',intent,orchestration_mode:mode}]);assert.equal(h.store.refreshes,1);assert.equal(find(h.controls,'设计并执行').disabled,intent==='execute');h.ui.destroy();cases++;
 }
 {
  let resolve;const posts=[];const h=harness(mountDesign,{get:()=>new Promise(r=>resolve=r),post:async(...args)=>posts.push(args)});
  h.ui.update(null);await find(h.controls,'设计并执行').click();assert.equal(posts.length,0);
  h.store.generation++;h.store.state.scope.conversation_id='other';h.ui.update(null);const newest=resolve;
  newest({conversation_id:'other',task_design:design({goal:'new'})});await flush();assert.equal(find(h.controls,'设计并执行').disabled,true);h.ui.destroy();cases++;
 }
 {
  let saved=design({revision:6,status:'blocked'});const posts=[];
  const h=harness(mountDesign,{get:async(path)=>path.includes('progress')?{project:'p',conversation_id:'c'}:{conversation_id:'c',task_design:saved},post:async(_,body)=>{posts.push(body);saved={...saved,revision:7,intent:'plan',status:'ready'};return {task_design:saved,continuation:{message:'任务范围已修正为仅设计保存。'}};}},{busy:()=>true});
  h.ui.update(view(saved));await flush();await find(h.controls,'改为仅设计保存').click();await flush();
  assert.deepEqual(posts,[{project:'p',conversation_id:'c',action:'set_intent',intent:'plan',expected_task_id:'task',expected_revision:6}]);assert.match(h.controls.textContent,/任务范围已修正/);assert.equal(find(h.controls,'仅设计保存').hidden,true);assert.equal(find(h.controls,'设计并执行').disabled,true);h.ui.destroy();cases++;
 }
 {
  let fail=true;const h=harness(mountDesign,{get:async()=>{if(fail)throw Error('宿主原始失败原因');return {conversation_id:'c',task_design:null};},post:async()=>{throw Error('不应提交');}});
  h.ui.update(null);await flush();assert.match(h.controls.textContent,/宿主原始失败原因/);assert.equal(find(h.controls,'设计并执行').disabled,true);assert.equal(find(h.controls,'重新读取任务').hidden,false);fail=false;await find(h.controls,'重新读取任务').click();await flush();assert.equal(find(h.controls,'设计并执行').disabled,false);h.ui.destroy();cases++;
 }
 {
  let resolve;const h=harness(mountDesign,{get:()=>new Promise(r=>resolve=r),post:async()=>{throw Error('不应提交');}});h.ui.update(null);const old=resolve;h.store.generation++;h.store.state.scope.conversation_id='b';h.ui.update(null);old({conversation_id:'c',task_design:design()});await flush();assert.equal(find(h.controls,'设计并执行').disabled,true);assert.equal(find(h.controls,'设计并执行').hidden,false);resolve({conversation_id:'b',task_design:null});await flush();assert.equal(find(h.controls,'设计并执行').disabled,false);h.ui.destroy();cases++;
 }
 {
  const saved=design({run_id:'run',status:'running'});const h=harness(mountDesign,{get:async()=>({conversation_id:'c',task_design:saved}),post:async()=>{throw Error('当前编排不存在，请核对后继续。');}});
  h.ui.update(view(saved));await flush();assert.equal(find(h.controls,'仅设计保存').hidden,false);await find(h.controls,'仅设计保存').click();await flush();assert.match(h.controls.textContent,/当前编排不存在/);h.ui.destroy();cases++;
 }
 for(const extra of [{intent:'plan'}, {intent_authority:{source:'host_entry',intent:'execute',revoked:true}}, {intent_authority:{}}]){
  let saved=design(extra);const posts=[];
  const h=harness(mountDesign,{get:async()=>({conversation_id:'c',task_design:saved}),post:async(_,body)=>{posts.push(body);saved=design({revision:4});return {task_design:saved,continuation:{message:'已继续设计'}};}});
  h.ui.update(view(saved));await flush();assert.equal(find(h.controls,'设计并执行').disabled,false);
  h.ui.execute();assert.equal(posts.length,0);assert.equal(h.controls.querySelector('.wb-design-confirm').hidden,false);
  await find(h.controls,'暂不执行').click();assert.equal(posts.length,0);assert.equal(h.controls.querySelector('.wb-design-confirm').hidden,true);
  await find(h.controls,'设计并执行').click();await find(h.controls,'确认设计并执行').click();await flush();
  assert.deepEqual(posts,[{project:'p',conversation_id:'c',action:'confirm_execute',confirm_execution:true,expected_task_id:'task',expected_revision:3}]);
  assert.equal(find(h.controls,'设计并执行').disabled,true);assert.equal(h.controls.querySelector('.wb-design-confirm').hidden,true);h.ui.destroy();cases++;
 }
 {
  let saved=design({intent:'plan'});const posts=[];
  const h=harness(mountDesign,{get:async()=>({conversation_id:'c',task_design:saved}),post:async(_,body)=>posts.push(body)});
  h.ui.update(view(saved));await flush();await find(h.controls,'设计并执行').click();saved={...saved,revision:4};h.ui.update(view(saved));await flush();
  assert.equal(h.controls.querySelector('.wb-design-confirm').hidden,true);await find(h.controls,'确认设计并执行').click();assert.equal(posts.length,0);
  await find(h.controls,'设计并执行').click();h.store.generation++;h.ui.update(view(saved));await flush();assert.equal(h.controls.querySelector('.wb-design-confirm').hidden,true);h.ui.destroy();cases++;
 }
 for(const extra of [{status:'building'}, {build_request:{execution_state:'dispatching'}}, {build_request:{execution_claim_id:'claim'}}, {status:'completed'}]){
  const saved=design({intent:'plan',...extra}),posts=[];const h=harness(mountDesign,{get:async()=>({conversation_id:'c',task_design:saved}),post:async(_,body)=>posts.push(body)});
  h.ui.update(view(saved));await flush();assert.equal(find(h.controls,'设计并执行').disabled,true);await find(h.controls,'设计并执行').click();assert.equal(h.controls.querySelector('.wb-design-confirm').hidden,true);assert.equal(posts.length,0);h.ui.destroy();cases++;
 }
 {
  let saved=design({goal:'已记录的目标',intent_authority:{source:'host_entry',intent:'execute',revoked:true}});const posts=[],chats=[];
  const h=harness(mountDesign,{get:async()=>({conversation_id:'c',task_design:saved}),post:async(_,body)=>posts.push(body)},{designStarted:data=>chats.push(data)});
  h.ui.update(view(saved));await flush();
  assert.match(h.controls.querySelector('.wb-design-start').textContent,/执行未授权/);
  assert.doesNotMatch(h.controls.textContent,/允许执行 ·/);
  assert.equal(h.controls.querySelector('select').hidden,true);
  assert.equal(find(h.controls,'设计并执行').hidden,false);
  assert.match(h.spec.textContent,/目标已记录/);assert.doesNotMatch(h.spec.textContent,/下一条消息会作为任务目标/);
  await find(h.controls,'继续对话').click();await flush();assert.deepEqual(chats,[{conversation_id:'c'}]);assert.deepEqual(posts,[]);
  saved=design({revision:4,goal:'已记录的目标'});h.ui.update(view(saved));await flush();
  assert.match(h.controls.textContent,/允许执行 ·/);assert.equal(find(h.controls,'设计并执行').hidden,true);
  assert.equal(find(h.controls,'改为仅设计保存').hidden,false);h.ui.destroy();cases++;
 }
 {
  let hostBusy=true;const saved=design({status:'failed',intent_authority:{source:'host_entry',intent:'execute',revoked:true}}),posts=[];
  const h=harness(mountDesign,{get:async()=>({conversation_id:'c',task_design:saved}),post:async(_,body)=>posts.push(body)},{busy:()=>hostBusy});
  h.ui.update(view(saved));await flush();
  assert.equal(find(h.controls,'设计并执行').disabled,true);assert.equal(find(h.controls,'继续对话').disabled,true);
  hostBusy=false;h.ui.refreshContext();
  assert.equal(find(h.controls,'设计并执行').disabled,false);assert.equal(find(h.controls,'继续对话').disabled,false);
  assert.deepEqual(posts,[]);await find(h.controls,'设计并执行').click();
  assert.equal(h.controls.querySelector('.wb-design-confirm').hidden,false);assert.deepEqual(posts,[]);
  hostBusy=true;h.ui.refreshContext();await find(h.controls,'确认设计并执行').click();
  assert.equal(find(h.controls,'确认设计并执行').disabled,true);assert.deepEqual(posts,[]);h.ui.destroy();cases++;
 }
 {
  const {mountHeader}=await import('../frontend/static/workbench/header.js');const doc=setup(),root=doc.createElement('section');doc.body.append(root);
  const long='把四个阶段的资料整理成可追溯的交付包，保留全部来源和约束。'.repeat(16);
  const task={id:'long',run_id:'',title:long,goal:long,controls:[],mode:'collaborative',spec_version:2};
  const store={generation:1,state:{scope:{}}},ui=mountHeader(root,{api:{},store,host:{}});
  ui.update({task,requirements:[]});assert.ok(root.querySelector('h1').textContent.length<50);
  assert.equal(root.querySelector('.wb-goal').textContent,long);assert.equal(root.querySelector('details').open,false);
  root.querySelector('details').open=true;ui.update({task,requirements:[]});assert.equal(root.querySelector('details').open,true);
  ui.update({task:{...task,id:'other'},requirements:[]});assert.equal(root.querySelector('details').open,false);cases++;
 }
 console.log(`orch_task_design: ${cases} workbench behavior cases passed`);
})().catch(error=>{console.error(error);process.exitCode=1;});
