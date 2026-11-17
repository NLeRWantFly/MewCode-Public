const assert=require('node:assert/strict');
const {setup,settle}=require('./workbench_dom.cjs');
const flush=async()=>{for(let i=0;i<3;i++)await settle();};
function harness(){const doc=setup(),root=doc.createElement('section');doc.body.append(root);return {doc,root,store:{generation:1,state:{scope:{project:'p',conversation_id:'c'}}}};}
(async()=>{
 const {mountGeneration}=await import('../frontend/static/workbench/generation.js');
 const {mountDesignSources}=await import('../frontend/static/workbench/design-sources.js');
 const {mountDecisionCard}=await import('../frontend/static/workbench/decision-card.js');let cases=0;
 {
  const h=harness();let poll;const calls=[];let fail=false;
  const api={get:async(path)=>{calls.push(path);if(fail)throw Error('断开');return {project:'p',conversation_id:'c',running:true,candidate:{log:[{status:'failed',error:'历史超时'}]},process:{headline:'独立审查中',stages:['整理','审查','交付'],stage_index:1,last_result:'补全已保存',next_step:'核对覆盖'},progress:{model:{waiting:true}}};}};
  const ui=mountGeneration(h.root,{api,store:h.store,delay:fn=>(poll=fn,1),cancelDelay:()=>{poll=null;}});ui.update({task_id:'task',build_request:{id:'build'}});await flush();assert.equal(calls.length,1);assert.match(h.root.textContent,/独立审查中/);assert.match(h.root.textContent,/历史超时/);assert.match(h.root.textContent,/尚未重新提交/);h.root.querySelector('details').open=true;
  fail=true;poll();await flush();assert.match(h.root.textContent,/生成进度暂时无法读取/);assert.match(h.root.textContent,/独立审查中/);assert.equal(h.root.querySelector('details').open,true);ui.update({task_id:'task',run_id:'run'});assert.equal(h.root.hidden,true);assert.equal(poll,null);ui.destroy();cases++;
 }
 {
  const h=harness();let resolve;const ui=mountGeneration(h.root,{api:{get:()=>new Promise(r=>resolve=r)},store:h.store,delay:()=>1,cancelDelay:()=>{}});ui.update({task_id:'first'});const first=resolve;h.store.generation++;ui.update({task_id:'second'});first({project:'p',conversation_id:'c',running:true,process:{headline:'旧任务已完成'}});await flush();assert.ok(!h.root.textContent.includes('旧任务'));ui.destroy();cases++;
 }
 {
  const h=harness();let resolve;const calls=[];const ui=mountDesignSources(h.root,{api:{get:(...args)=>(calls.push(args),new Promise(r=>resolve=r))},store:h.store});
  const entries=[{id:'req',kind:'constraint',title:'保留原文',body:'<img src=x>',evidence:'user_source',sources:[{source_id:'source',quote:'用户原话'}]}];ui.update(entries,{id:'task'});assert.match(h.root.textContent,/关联用户原话/);assert.equal(h.root.querySelectorAll('img').length,0);h.root.querySelector('details').open=true;ui.update(entries,{id:'task'});assert.equal(h.root.querySelector('details').open,true);
  const read=h.root.querySelector('button').click();resolve({text:'完整原话'});await read;assert.match(h.root.textContent,/完整原话/);assert.equal(calls[0][0],'/api/orchestration/draft/source/source');
  h.store.generation++;ui.update([],{id:'next'});assert.equal(h.root.textContent,'');cases++;
 }
 {
  const h=harness();let resolve;h.store.refresh=()=>{};let prepares=0,answers=0;
  const card=mountDecisionCard({id:'pending',title:'设计取舍',type:'design_choice',mandatory:true,prepare_choices:true,questions:[],context:{}},{store:h.store,api:{prepare:()=>{prepares++;return new Promise(r=>resolve=r);},answer:()=>answers++}});h.root.append(card.element);
  await card.element.dispatch('submit');assert.equal(answers,0);const click=card.element.querySelector('button').click();await settle();assert.equal(prepares,1);assert.equal(card.element.querySelector('button').disabled,true);resolve({});await click;assert.equal(answers,0);assert.match(card.element.textContent,/方案已更新/);card.destroy();cases++;
 }
 console.log(`workbench_design: ${cases} source/progress/choice cases passed`);
})().catch(error=>{console.error(error);process.exitCode=1;});
