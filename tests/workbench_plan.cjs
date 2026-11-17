const assert = require('node:assert/strict');
const {pathToFileURL} = require('node:url');
const path = require('node:path');
const {setup, settle} = require('./workbench_dom.cjs');
const load = name => import(pathToFileURL(path.resolve(__dirname, '../frontend/static/workbench/' + name)));
const step = (id, status, detail = {goal: '交付资料', constraints: [{body: '保留原始文件'}], requirements: ['逐项核对'],
  checks: [{name: '来源完整', passed: true, evidence: {text: '<img src=x onerror=alert(1)>'}}], artifacts: []}) =>
  ({id, name: '步骤 ' + id, status, attempts: 1, detail, detail_ref: detail ? {run_id: 'run', node_id: id} : null, acceptance: null});
const view = (mode, groups) => ({version: 'v1', task: {id: 'task', run_id: 'run', mode, title: '整理资料', goal: '输出差异',
  status: '进行中', spec_version: 2, design_revision: 3, controls: ['pause','cancel'], current_activity: '核对中'}, plan: groups});
const group = (id, status, steps) => ({id, title: '里程碑 ' + id, status, steps});

(async () => {
  const {mountPlan} = await load('plan.js'), {mountHeader, controlTask} = await load('header.js');
  const {renderProcess, processRows} = await load('process.js'), {mountLessons} = await load('lessons.js');
  let cases = 0;
  {
    const doc = setup(), container = doc.createElement('section'); doc.body.append(container);
    const calls = [], store = {generation: 1, state: {scope: {conversation_id: 'c', project: 'p'}}};
    const api = {get: async (url, query) => { calls.push({url,query}); return {run_id:'run',flow_id:'flow',node_states:{a:{tools:{tool:{name:'read_file',output:'内容'}}}},events:[]}; }};
    const plan = mountPlan(container, {api,store,host:{showStep:()=>true}});
    const classic = view('classic', [group('default','进行中',[step('a','进行中'),step('b','未开始')])]);
    plan.update(classic);
    assert.equal(container.querySelectorAll('.wb-milestone').length,1);
    const active = container.querySelectorAll('.wb-step')[0]; assert.equal(active.open,true);
    assert.equal(container.querySelectorAll('.wb-step')[1].open,false);
    assert.match(active.textContent,/来源完整/); assert.match(active.textContent,/<img src=x onerror=alert\(1\)>/);
    assert.equal(active.querySelectorAll('img').length,0); assert.equal(calls.length,0);
    await active.querySelectorAll('button').find(x=>x.textContent==='查看过程').click(); await settle();
    assert.equal(calls.length,1); assert.deepEqual(calls[0].query,{node_id:'a'}); assert.match(active.textContent,/read_file/);
    active.open = false; plan.update({...classic, version:'v2'});
    assert.equal(container.querySelectorAll('.wb-step')[0],active); assert.equal(active.open,false);
    plan.openStep('a'); assert.equal(active.open,true); assert.equal(active.scrolled,true);
    store.generation++; plan.update(view('classic',[group('default','未开始',[step('b','未开始')])]));
    assert.equal(active.isConnected,false); plan.destroy(); cases++;
  }
  {
    const doc=setup(), container=doc.createElement('section');doc.body.append(container);
    const store={generation:1,state:{scope:{conversation_id:'c'}}};
    const plan=mountPlan(container,{store,api:{},host:{}});
    plan.update(view('collaborative',[group('one','已完成',[step('a','已完成')]),group('two','进行中',[step('b','验收中')])]));
    const groups=container.querySelectorAll('.wb-milestone');assert.equal(groups[0].open,false);assert.equal(groups[1].open,true);
    const legacy = view('classic',[group('default','已完成',[step('a','已完成',null)])]);legacy.legacy=true;
    store.generation++;plan.update(legacy);
    assert.match(container.textContent,/此历史格式/);assert.equal(container.querySelector('.wb-actions').hidden,true);
    plan.destroy();cases++;
  }
  {
    const doc=setup(), container=doc.createElement('section');doc.body.append(container);
    const store={generation:1,state:{scope:{conversation_id:'c'}}};
    const plan=mountPlan(container,{store,api:{get:async()=>({revisions:[{revision:2,attempts:[{status:'completed'}]}]})},host:{}});
    const team=view('team',[group('responsibility','进行中',[step('a','进行中')])]);plan.update(team);
    const buttons=container.querySelectorAll('button'); assert.equal(buttons.find(x=>x.textContent==='在执行过程中查看').hidden,true);
    await buttons.find(x=>x.textContent==='查看过程').click();await settle();assert.match(container.textContent,/任务修订 2/);
    plan.destroy();cases++;
  }
  {
    const doc=setup(), container=doc.createElement('section');doc.body.append(container);
    const record={node_states:{a:{command_receipts:[{status:'verified'}],assistance_receipts:[{notes:'修复环境'}],
      recovery_case:{case_id:'c',rounds:[{verification:{effect:'resolved'}}]}}},events:[{node_id:'a',type:'expert_tool_result',name:'test',output:'通过'},
      {node_id:'foreign',type:'expert_delta',text:'不可串入'}]};
    assert.equal(processRows(record,'a').rows.length,1);renderProcess(container,record,'a');
    assert.match(container.textContent,/恢复记录/);assert.match(container.textContent,/操作回执与效果核验/);
    assert.match(container.textContent,/修复环境/);assert.doesNotMatch(container.textContent,/不可串入/);cases++;
  }
  {
    const doc=setup(), container=doc.createElement('section');doc.body.append(container);
    const current=view('collaborative',[]), calls=[];
    const store={generation:1,state:{scope:{conversation_id:'c',project:'p'}},refresh:()=>calls.push('refresh')};
    let changed=false; const api={readView:async()=>changed?{...current,task:{...current.task,spec_version:9}}:{...current,version:'new-output'},
      post:async(url,body)=>calls.push({url,body})};
    const header=mountHeader(container,{api,store,host:{}});header.update(current);
    await container.querySelectorAll('button').find(x=>x.textContent==='暂停').click();await settle();
    assert.equal(calls[0].body.expected_revision,3);assert.equal(calls[0].body.expected_task_id,'task');
    assert.match(container.textContent,/操作已提交。/);
    assert.doesNotMatch(container.textContent,/正在读取最新状态/);
    const paused={...current,version:'paused',task:{...current.task,status:'已暂停',design_revision:4,controls:['resume']}};
    header.update(paused);
    assert.doesNotMatch(container.textContent,/操作已提交/);
    assert.ok(container.querySelectorAll('button').find(x=>x.textContent==='继续'));
    header.update(current);
    changed=true;const before=calls.filter(x=>x.url).length;
    await container.querySelectorAll('button').find(x=>x.textContent==='暂停').click();await settle();
    assert.equal(calls.filter(x=>x.url).length,before);assert.match(container.textContent,/任务状态已变化/);
    header.update({...current,legacy:true,legacy_message:'旧格式，只读',task:{...current.task,controls:[]}});
    assert.match(container.textContent,/旧格式，只读/);assert.equal(container.querySelectorAll('button').length,0);cases++;
  }
  {
    const doc=setup(), container=doc.createElement('section');doc.body.append(container);
    let current=view('workflow',[]);const calls=[];
    current.task={...current.task,run_id:'',status:'已暂停',design_stage:true,controls:['resume']};
    const scope={conversation_id:'c',project:'p'};
    const store={generation:1,state:{scope},refresh(){}};
    const api={readView:async()=>current,post:async(url,body)=>{calls.push({url,body});return{};}};
    const header=mountHeader(container,{api,store,host:{command(){throw new Error('Design must resume its own conversation');}}});
    header.update(current);
    await container.querySelectorAll('button').find(x=>x.textContent==='继续').click();await settle();
    assert.deepEqual(calls,[{url:'/api/orchestration/task-design',body:{...scope,action:'resume',expected_task_id:'task',expected_revision:3}}]);
    current={...current,task:{...current.task,status:'设计中',controls:['pause']}};header.update(current);
    await container.querySelectorAll('button').find(x=>x.textContent==='暂停').click();await settle();
    assert.equal(calls[1].url,'/api/orchestration/task-design');assert.equal(calls[1].body.action,'pause');
    cases++;
  }
  {
    const calls=[], api={post:async(url,body)=>calls.push({url,body})};
    const task={...view('workflow',[]).task,status:'已暂停',controls:['resume','pause','cancel']};
    for(const action of task.controls)await controlTask({api,host:{},scope:{conversation_id:'c'},task,action});
    assert.ok(calls.every(call=>call.url==='/api/workbench/run-action'&&call.body.run_id==='run'&&call.body.request_id));
    assert.deepEqual(calls.map(call=>call.body.operation),['resume','pause','stop']);cases++;
  }
  {
    const calls=[], api={teamCommand:async(...args)=>calls.push(args),post:async(...args)=>calls.push(args)},host={command:async value=>calls.push(value)};
    await controlTask({api,host,scope:{conversation_id:'c'},task:{...view('classic',[]).task,design_revision:null},action:'cancel'});
    assert.equal(calls[0].type,'expert_terminate');assert.equal(calls[0].run_id,'run');
    await controlTask({api,host,scope:{conversation_id:'c'},task:{...view('team',[]).task,control_epoch:2,team_state_version:7},action:'pause'});
    assert.equal(calls[1][1],'suspend');assert.equal(calls[1][2].expected_version,7);assert.equal(calls[1][2].control_epoch,2);cases++;
  }
  {
    const fs = require('node:fs'), vm = require('node:vm');
    const source = fs.readFileSync(path.resolve(__dirname, '../frontend/static/app.js'), 'utf8');
    const start = source.indexOf('  function pipelineItemFromEvent('), end = source.indexOf('\n  }',start) + 4;
    const apply = vm.runInNewContext(source.slice(start,end) + '\npipelineItemFromEvent');
    const state = {items:new Map(),order:[]};
    apply(state,{item_id:'step',text:'first'});apply(state,{item_id:'step',text:' delta'});
    assert.equal(state.items.get('step').text,'first delta');
    for (let n=0;n<3;n++) apply(state,{item_id:'step',text:'host snapshot',replace_text:true});
    assert.equal(state.items.get('step').text,'host snapshot');assert.equal(state.order.length,1);cases++;
  }
  {
    const doc=setup(),container=doc.createElement('section');doc.body.append(container);const calls=[];
    const data={lessons:[{lesson_id:'active',flow_id:'f',scope:'a',text:'经验一',status:'active'},
      {lesson_id:'pending',flow_id:'f',scope:'a',text:'必须在待办确认',status:'pending'}]};
    mountLessons(container,{api:{get:async()=>data,patch:async(url,body)=>calls.push(body),post:async(url,body)=>calls.push(body)},
      flowId:'f',stepId:'a',active:()=>true});await settle();
    assert.doesNotMatch(container.textContent,/必须在待办确认/);assert.match(container.textContent,/经验一/);
    await container.querySelectorAll('button').find(x=>x.textContent==='停用').click();await settle();assert.equal(calls[0].status,'disabled');
    container.querySelector('textarea').value='用户新经验';await container.querySelector('form').dispatch('submit');await settle();
    assert.equal(calls[1].text,'用户新经验');
    await container.querySelectorAll('button').find(x=>x.textContent==='删除').click();assert.equal(calls.length,2);
    await container.querySelectorAll('button').find(x=>x.textContent==='确认删除').click();await settle();assert.equal(calls[2].delete,true);cases++;
  }
  console.log(`workbench_plan: ${cases} cases passed (classic/collaborative/team/legacy, details, recovery, controls, lessons)`);
})().catch(error=>{console.error(error);process.exitCode=1;});
