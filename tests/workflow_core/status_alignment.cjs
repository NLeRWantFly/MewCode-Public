const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {pathToFileURL}=require('node:url');

(async()=>{
  const {alignWorkflowView}=await import(pathToFileURL(path.resolve(__dirname,'../../frontend/static/workbench/workflow-summary.js')));
  const {old,expected}=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
  const before=structuredClone(old),shown=alignWorkflowView(old);
  assert.deepEqual(shown.task,expected.task);
  assert.deepEqual(shown.plan.map(group=>[group.id,group.status]),expected.plan.map(group=>[group.id,group.status]));
  assert.deepEqual(old,before,'read-only compatibility must preserve the host document');
  assert.equal(alignWorkflowView(shown),shown,'current host projections are authoritative');
  assert.equal(shown.task.execution_status,'running','scheduler activity remains distinct from graph progress');
  const {createAPI}=await import(pathToFileURL(path.resolve(__dirname,'../../frontend/static/workbench/api.js')));
  const api=createAPI(async()=>({ok:true,json:async()=>structuredClone(old)}));
  assert.deepEqual((await api.readView({conversation_id:'c'})).task,expected.task,'control refreshes use the same display basis');
  const {setup}=require('../workbench_dom.cjs'),doc=setup(),root=doc.createElement('div');doc.body.append(root);
  const {mountHeader}=await import(pathToFileURL(path.resolve(__dirname,'../../frontend/static/workbench/header.js')));
  const header=mountHeader(root,{api:{},store:{generation:1,state:{}},host:{}});
  const active={...expected,task:{...expected.task,status:'进行中',workflow_status:'running',controls:['pause','cancel']}};
  header.update(active);assert.ok(root.querySelectorAll('button').some(button=>button.textContent==='暂停'));
  header.update({...active,task:{...active.task,status:'已完成',workflow_status:'completed'}});
  assert.ok(root.querySelectorAll('button').some(button=>button.textContent==='暂停节点对话'),'unchanged controls still refresh their scope label');
  console.log('status alignment passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
