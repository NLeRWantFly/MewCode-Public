const assert=require('node:assert/strict');
const {pathToFileURL}=require('node:url');
const path=require('node:path');
const {setup,settle}=require('../workbench_dom.cjs');

(async()=>{
  const {mountWorkflowExport}=await import(pathToFileURL(path.resolve(__dirname,'../../frontend/static/workbench/export.js')));
  const doc=setup(),aside=doc.createElement('aside'),canvas=doc.createElement('div'),opener=doc.createElement('button');
  aside.append(canvas,opener);doc.body.append(aside);
  const state={scope:{project:'project',conversation_id:'chat'},view:{task:{mode:'workflow',id:'task',run_id:'run'},editor:{has_draft:true}}};
  const listeners=new Set(),store={state,subscribe:f=>{listeners.add(f);f(state);return()=>listeners.delete(f);}};
  const payload={source:{selection:'applied',graph_revision:1,applied_revision:1},node_count:4,edge_count:3,
    documents:{brief:{text:'完整构造说明',filename:'construction.md'},json:{text:'{"workflow":{"nodes":["complete"]}}',filename:'blueprint.json'}}};
  let calls=0,next=()=>Promise.resolve(payload),copied='';
  Object.defineProperty(global,'navigator',{configurable:true,value:{clipboard:{writeText:async text=>{copied=text;}}}});
  const component=mountWorkflowExport(aside,{api:{get:(route,query)=>{assert.equal(route,'/api/workbench/export');assert.equal(query.run_id,state.view.task.run_id);calls++;return next();}},store,opener});
  await component.open();
  assert.equal(aside.querySelector('.wb-export-preview').value,'完整构造说明');assert.equal(canvas.inert,true);
  await aside.querySelectorAll('button').find(b=>b.textContent==='复制当前内容').click();
  assert.equal(copied,'完整构造说明');
  const select=aside.querySelectorAll('select').find(e=>e.getAttribute('aria-label')==='提取格式');select.value='json';await select.dispatch('change');
  assert.equal(aside.querySelector('.wb-export-preview').value,payload.documents.json.text);assert.equal(calls,1);
  let resolve;next=()=>new Promise(done=>{resolve=done;});const pending=component.load();
  state.scope={project:'other',conversation_id:'other'};state.view.task.run_id='other';listeners.forEach(f=>f(state));
  resolve(payload);await pending;await settle();
  assert.equal(aside.querySelector('.wb-workflow-export').hidden,true);
  assert.equal(aside.querySelector('.wb-export-preview').value,'','late exports cannot cross conversation boundaries');
  assert.notEqual(canvas.inert,true);
  component.destroy();assert.equal(listeners.size,0);
  console.log('workflow_export: complete copy, format switching, read-only request and conversation fencing passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
