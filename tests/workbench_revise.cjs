const assert = require('node:assert/strict');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {setup, settle} = require('./workbench_dom.cjs');
const load = name => import(pathToFileURL(path.resolve(__dirname, '../frontend/static/workbench/' + name)));
const task = {id:'task',run_id:'run',mode:'collaborative',spec_version:2,status:'进行中'};
const entry = {id:'hard',kind:'constraint',title:'保留来源',body:'保留原文',editable:true,origin:'user',strength:'hard'};
function harness() {
  const doc=setup(), root=doc.createElement('section');doc.body.append(root);
  const store={generation:1,state:{scope:{project:'p',conversation_id:'c'}},refresh(){this.refreshed=(this.refreshed||0)+1;}};
  return {doc,root,store};
}
const find = (root,text) => root.querySelectorAll('button').find(row=>row.textContent===text);
(async()=>{
  const {mountRequirements,STALE_REVISION}=await load('revise.js');
  const {sourceForMessage}=await load('chat-revision.js');
  const {mountDeliverables,readDelivery}=await load('deliverables.js');
  const {mountChanges}=await load('changes.js');
  let cases=0;
  {
    const {root,store}=harness(),calls=[];
    const ui=mountRequirements(root,{store,api:{revision:async(scope,value)=>calls.push(value)}});
    const rows=[{...entry,id:'node',field:'method',title:'指定方法',body:'离线处理'},
      {...entry,id:'node',field:'acceptance',title:'验收标准',body:'保留顺序缺口\n后续解析前验证'}];
    ui.update(rows,task);assert.equal(root.querySelectorAll('form').length,2);
    const buttons=root.querySelectorAll('button').filter(row=>row.textContent==='修改');
    await buttons[1].click();const input=root.querySelectorAll('textarea')[1];input.value='修改后的验收';
    await root.querySelectorAll('form')[1].dispatch('submit');
    assert.equal(calls[0].entry_id,'node');assert.equal(calls[0].field,'acceptance');
    assert.equal(calls[0].body,'修改后的验收');assert.match(root.textContent,/离线处理/);cases++;
  }
  {
    const {root,store}=harness();let resolve;const calls=[];
    const ui=mountRequirements(root,{store,api:{revision:(scope,value)=>{calls.push({scope,value});return new Promise(r=>resolve=r);}}});
    ui.update([entry],task);await find(root,'修改').click();const input=root.querySelector('textarea');input.value='用户明确修改硬约束';
    ui.update([{...entry,revision:{status:'等待生效'}}],task);assert.equal(root.querySelector('textarea'),input);assert.equal(input.value,'用户明确修改硬约束');
    const promise=root.querySelector('form').dispatch('submit');await settle();
    assert.equal(find(root,'提交修改').disabled,true);assert.equal(calls.length,1);await root.querySelector('form').dispatch('submit');assert.equal(calls.length,1);
    assert.equal(calls[0].value.expected_version,2);assert.equal(calls[0].value.expected_run_id,'run');assert.equal(calls[0].value.entry_id,'hard');
    assert.equal(calls[0].value.body,'用户明确修改硬约束');assert.ok(calls[0].value.request_id);assert.deepEqual(calls[0].scope,store.state.scope);
    resolve({});await promise;assert.match(root.textContent,/已记录/);assert.equal(store.refreshed,1);cases++;
  }
  {
    const {root,store}=harness();let calls=0;
    const ui=mountRequirements(root,{store,api:{revision:async()=>calls++}});ui.update([entry],task);
    await find(root,'修改').click();const input=root.querySelector('textarea');input.value='我的草稿';
    ui.update([entry],{...task,spec_version:3});await root.querySelector('form').dispatch('submit');
    assert.equal(calls,0);assert.match(root.textContent,new RegExp(STALE_REVISION));assert.equal(input.value,'我的草稿');cases++;
  }
  {
    const {root,store}=harness();const ui=mountRequirements(root,{store,api:{revision:async()=>{throw Object.assign(new Error(STALE_REVISION),{status:409});}}});
    ui.update([entry],task);await find(root,'修改').click();root.querySelector('textarea').value='新内容';await root.querySelector('form').dispatch('submit');
    assert.match(root.textContent,new RegExp(STALE_REVISION));assert.equal(root.querySelector('textarea').value,'新内容');assert.equal(store.refreshed,1);cases++;
  }
  {
    const {root,store}=harness();const ui=mountRequirements(root,{store,api:{}});
    for(const status of ['已记录','等待生效','已生效']) {ui.update([{...entry,revision:{status}}],task);assert.match(root.textContent,new RegExp(status));}
    ui.update([{...entry,origin:'host',editable:false,edit_reason:'这是系统策略，不能在这里修改'}],task);
    assert.equal(find(root,'修改').hidden,true);assert.match(root.textContent,/这是系统策略/);await find(root,'修改').click();assert.equal(root.querySelector('form').hidden,true);
    ui.update([entry],{...task,readonly:true});assert.equal(find(root,'修改').hidden,true);assert.match(root.textContent,/历史运行只读/);cases++;
  }
  {
    const {root,store}=harness();let resolve;const ui=mountRequirements(root,{store,api:{revision:()=>new Promise(r=>resolve=r)}});
    ui.update([entry],task);await find(root,'修改').click();const pending=root.querySelector('form').dispatch('submit');store.generation++;
    ui.update([{...entry,body:'新会话'}],{...task,id:'other'});resolve({});await pending;assert.equal(store.refreshed,undefined);assert.ok(!root.textContent.includes('已记录'));cases++;
  }
  {
    const messages=[{text:'相同内容'},{text:'相同内容'},{text:'不同内容'}],sources=[{id:'u1',role:'user',text:'相同内容'},{id:'a1',role:'assistant',text:'相同内容'},{id:'u2',role:'user',text:'相同内容'},{id:'u3',role:'user',text:'不同内容'}];
    assert.equal(sourceForMessage(messages,sources,messages[1]).id,'u2');
    assert.throws(()=>sourceForMessage(messages.slice(1),sources,messages[1]),/来源不完整/);cases++;
  }
  {
    const {root,store}=harness();const calls=[];let resolve;
    const api={get:(path,query)=>{calls.push({path,query});return new Promise(r=>resolve=r);}};
    const ui=mountDeliverables(root,{store,api});const item={id:'a',kind:'file',file_id:'collab:hash',name:'交付.txt',status:'需重新核验',reason:'相关要求已变化'};
    ui.update({task,deliverables:[item]});assert.match(root.textContent,/需重新核验/);
    const pending=find(root,'预览文件').click();assert.deepEqual(calls[0],{path:'/api/expert-runs/run/files/collab%3Ahash',query:{conversation_id:'c'}});
    resolve({file:{preview:'<script>不会执行</script>',preview_type:'text'}});await pending;assert.equal(root.querySelectorAll('script').length,0);assert.match(root.textContent,/<script>/);
    ui.update({task,deliverables:[{...item,status:'已验收',reason:''}]});assert.match(root.textContent,/已验收/);assert.match(root.textContent,/<script>/);cases++;
  }
  {
    const {root,store}=harness();let resolve;const ui=mountDeliverables(root,{store,api:{get:()=>new Promise(r=>resolve=r)}});
    ui.update({task,deliverables:[{id:'a',kind:'file',file_id:'receipt',name:'文件',status:'已验收'}]});const pending=find(root,'预览文件').click();
    store.generation++;ui.update({task:{...task,id:'other'},deliverables:[]});resolve({file:{preview:'旧任务内容'}});await pending;assert.ok(!root.textContent.includes('旧任务内容'));cases++;
  }
  {
    assert.equal((await readDelivery({}, {},task,{kind:'inline',inline_content:'内联成果'})).preview,'内联成果');
    let path;const file=await readDelivery({get:async value=>{path=value;return {content_b64:btoa('team result'),media_type:'text/plain'};}},{},task,{kind:'team_artifact',artifact_version:'version'});
    assert.equal(path,'/api/team-v2/artifacts/version');assert.equal(file.preview,'team result');
    await assert.rejects(()=>readDelivery({}, {},task,{kind:'file'}),/没有可读取/);cases++;
  }
  {
    const {root}=harness(),ui=mountChanges(root);
    const view={task,changes:[{id:'one',label:'要求修订',reason:'补充证据',actor:'user',evidence:{text:'<script>证据</script>'}}]};ui.update(view);
    const details=root.querySelector('details');details.open=true;ui.update(view);assert.equal(root.querySelector('details'),details);assert.equal(details.open,true);
    assert.match(root.textContent,/补充证据/);assert.match(root.textContent,/你/);assert.equal(root.querySelectorAll('script').length,0);cases++;
  }
  console.log(`workbench_revise: ${cases} cases passed`);
})().catch(error=>{console.error(error);process.exitCode=1;});
