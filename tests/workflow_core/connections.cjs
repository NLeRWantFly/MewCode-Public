const assert=require('node:assert/strict');
const {pathToFileURL}=require('node:url');
const path=require('node:path');
const {setup,settle}=require('../workbench_dom.cjs');

(async()=>{
  const {mountConnections}=await import(pathToFileURL(path.resolve(__dirname,'../../frontend/static/workbench/connections.js')));
  const doc=setup(),aside=doc.createElement('aside'),local=doc.createElement('div'),draft=doc.createElement('textarea'),opener=doc.createElement('button');
  draft.value='保留已有草稿';local.append(draft,opener);aside.append(local);doc.body.append(aside);
  const storage=new Map();global.localStorage={getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)};
  global.location=new URL('http://127.0.0.1:8888/');
  const row={id:'a'.repeat(32),title:'当前四书工作流',status_label:'进行中',available:true,completed:15,nodes:16,
    project:'project',url:'/workbench/connections/'+'a'.repeat(32)};
  let result={connections:[row]},reads=0,opens=0;
  const component=mountConnections(aside,{api:{get:async route=>{assert.equal(route,'/api/workbench/connections');reads++;return result;}},
    openWorkspace:()=>opens++,opener});
  await settle();await component.browse();
  assert.equal(aside.querySelector('.wb-connections').hidden,false);
  assert.equal(local.inert,true);
  const originalOpen=aside.querySelectorAll('button').find(b=>b.textContent==='打开当前工作流');
  originalOpen.focus();await component.load();
  assert.equal(document.activeElement,originalOpen,'unchanged polling preserves keyboard focus');
  await aside.querySelectorAll('button').find(b=>b.textContent==='打开当前工作流').click();
  const frame=aside.querySelector('iframe');assert.equal(frame.src,location.origin+row.url);assert.equal(frame.title,'当前四书工作流 · 实时工作流');
  result={connections:[{...row,status_label:'已完成',completed:16}]};await component.load();
  assert.equal(aside.querySelector('iframe'),frame,'status polling does not reload a running workflow');
  assert.equal(aside.querySelector('.wb-connection-state').textContent,'已完成');
  result={connections:[{...row,available:false,status_label:'连接暂不可用'}]};await component.load();
  assert.equal(aside.querySelector('iframe'),frame,'temporary disconnect retains the open page');
  assert.match(aside.querySelector('.wb-connection-notice').textContent,/连接暂不可用/);
  component.close();assert.equal(aside.querySelector('iframe'),null);assert.equal(draft.value,'保留已有草稿');
  assert.notEqual(local.inert,true);assert.equal(storage.size,0);
  component.open({...row,url:'https://example.com/workflow-live'});assert.equal(aside.querySelector('iframe'),null);
  result={connections:[row]};await component.openCurrent();
  assert.equal(aside.querySelector('iframe').src,location.origin+row.url,'shortcut opens the only available run');
  assert.ok(reads>=3&&opens>=2);component.destroy();
  console.log('workflow_connections: live identity, refresh, disconnect, draft preservation and URL boundary passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
