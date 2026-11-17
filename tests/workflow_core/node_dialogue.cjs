const assert=require('node:assert/strict');
const {pathToFileURL}=require('node:url');
const path=require('node:path');
const {setup,settle}=require('../workbench_dom.cjs');

(async()=>{
  const {mountWorkflowNode}=await import(pathToFileURL(path.resolve(__dirname,'../../frontend/static/workbench/workflow-node.js')));
  const doc=setup(),root=doc.createElement('section');doc.body.append(root);
  let snapshot={capabilities:{node_messages:true},node_id:'a',current_attempt_id:'initial',
    attempts:[{id:'initial',number:1,status:'completed',session_id:'session-a',progress:[],result:{text:'old'},artifacts:[]}],
    requests:[],published_delivery_id:'v1',deliveries:[{id:'v1',number:1,attempt_id:'initial',session_id:'session-a',result:{text:'old',artifacts:[]}}]};
  let loseResponse=false;
  const calls=[],store={generation:1,state:{scope:{conversation_id:'chat',project:'test'}},accept(){},refresh(){}};
  const api={get:async()=>structuredClone(snapshot),post:async(url,body)=>{
    calls.push({url,body});
    if(url.endsWith('/cancel'))snapshot.requests.find(row=>row.id===body.message_id).status='cancelled';
    else if(!snapshot.requests.some(row=>row.id===body.request_id))snapshot.requests.push({...body,id:body.request_id,sender_kind:'user',sender_node_id:'',status:'queued',reply:{}});
    if(loseResponse){loseResponse=false;throw new Error('Network response lost');}
    return {view:{},request:snapshot.requests.at(-1)};
  }};
  const mounted=mountWorkflowNode(root,{api,store,view:{task:{id:'task',run_id:'run',plan_version:1}},item:{id:'a'}});
  await settle();
  const input=root.querySelectorAll('textarea').find(row=>row.getAttribute('aria-label')==='给此节点的消息');
  const refs=root.querySelectorAll('input').find(row=>row.getAttribute('aria-label')==='关联文件或页码');
  const click=async text=>root.querySelectorAll('button').find(row=>row.textContent===text).click();
  input.value='第 53 页缺字，请补交';refs.value='page:53, page.txt';
  await click('要求补交');
  assert.equal(calls.length,1);assert.equal(calls[0].url,'/api/workbench/node-message');
  assert.equal(calls[0].body.kind,'revise');assert.equal(calls[0].body.base_delivery_id,'v1');
  assert.equal(calls[0].body.target_attempt_id,'initial');assert.deepEqual(calls[0].body.references,['page:53','page.txt']);
  assert.equal(input.value,'');assert.match(root.textContent,/已排队/);
  const version=root.querySelectorAll('details').find(row=>row.getAttribute('data-delivery-id')==='v1');
  version.open=true;await mounted.refresh();assert.equal(version.open,true);
  input.value='仍在输入的第二个问题';await mounted.refresh();assert.equal(input.value,'仍在输入的第二个问题');
  snapshot.requests[0].status='completed';snapshot.requests[0].reply={text:'已补齐'};await mounted.refresh();
  assert.equal(root.querySelectorAll('details').find(row=>row.getAttribute('data-delivery-id')==='v1').open,true);
  assert.match(root.textContent,/已补齐/);await click('继续追问');
  await click('发送追问');assert.equal(calls[1].body.parent_request_id,calls[0].body.request_id);
  assert.equal(calls[1].body.kind,'ask');
  await click('取消这次请求');assert.equal(calls[2].url,'/api/workbench/node-message/cancel');
  assert.ok(!calls.some(row=>row.body.operation==='rerun'||row.body.operation==='pause'));
  input.value='重试同一消息';loseResponse=true;
  await click('发送追问');assert.equal(input.value,'重试同一消息');
  await click('发送追问');assert.equal(calls[3].body.request_id,calls[4].body.request_id);
  assert.equal(snapshot.requests.length,3);
  mounted.destroy();
  console.log('Node dialogue: send, revision, context binding, reply, draft preservation, cancellation passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
