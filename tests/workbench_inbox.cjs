const assert = require('node:assert/strict');
const {pathToFileURL} = require('node:url');
const path = require('node:path');
const {setup, settle} = require('./workbench_dom.cjs');
const load = name => import(pathToFileURL(path.resolve(__dirname, '../frontend/static/workbench/' + name)));
const question = {name:'action',question:'如何处理',type:'choice',options:[{value:'accept',label:'确认',description:'采用当前方案',recommended:true},{value:'decline',label:'不采用'}]};
const item = (type, extra={}) => ({id:type+'.one',type,task_id:'task',run_id:'run',conversation_id:'c',title:'请确认下一步',background:'这里是背景',
  questions:[question],mandatory:true,timeout_consequence:'持续等待你的明确选择。',context:{},...extra});
const store = () => ({generation:1,state:{scope:{conversation_id:'c',project:'p'}},refreshes:0,refresh(){this.refreshes++;}});
async function choose(card, number=0) { const radio=card.element.querySelectorAll('input').filter(row=>row.type==='radio')[number];radio.checked=true;await radio.dispatch('change'); }

(async()=>{
  const {mountDecisionCard,deadlineText,STALE}=await load('decision-card.js'),{mountInbox}=await load('inbox.js');
  const {mountQuestion}=await load('question.js'),{createNotices}=await load('chat-notices.js');let count=0;
  {
    setup();const text=mountQuestion({name:'brief',question:'完整要求',type:'text'}),list=mountQuestion({name:'paths',question:'文件列表',type:'list'});
    text.element.querySelector('textarea').value='one\ntwo\nthree\nfour';list.element.querySelector('textarea').value='a.pdf\nb.pdf\nc.pdf\nd.pdf';
    assert.equal(text.read().value,'one\ntwo\nthree\nfour');assert.deepEqual(list.read().value,['a.pdf','b.pdf','c.pdf','d.pdf']);
    list.element.querySelector('textarea').value='a.pdf\nc.pdf\nd.pdf';assert.deepEqual(list.read().value,['a.pdf','c.pdf','d.pdf']);count++;
  }
  for(const type of ['permission','human_oncall','ask_user','plan_revision','file_grant','collaboration_recovery','run_inputs','recovery_case','team_intervention','team_plan','revision_draft','lesson']){
    const doc=setup(),card=mountDecisionCard(item(type),{store:store(),api:{}});doc.body.append(card.element);
    assert.match(card.element.textContent,/请确认下一步/);assert.match(card.element.textContent,/需要你决定/);
    assert.equal(card.element.querySelectorAll('input').some(input=>input.checked),false);
    assert.match(card.element.textContent,/推荐/);card.destroy();count++;
  }
  {
    const doc=setup(),s=store(),calls=[];let resolve;
    const card=mountDecisionCard(item('ask_user'),{store:s,api:{answer:async(...args)=>{calls.push(args);return new Promise(done=>resolve=done);}}});doc.body.append(card.element);
    await card.element.dispatch('submit');assert.equal(calls.length,0);assert.match(card.element.textContent,/请选择/);
    await choose(card);const pending=card.element.dispatch('submit');await settle();
    assert.equal(calls.length,1);assert.equal(card.element.querySelector('button').disabled,true);
    await card.element.dispatch('submit');assert.equal(calls.length,1);resolve({ok:true});await pending;
    assert.equal(s.refreshes,1);assert.equal(card.element.querySelector('button').disabled,true);assert.deepEqual(calls[0][2].answers,{action:'accept'});card.destroy();count++;
  }
  {
    const doc=setup(),s=store(),card=mountDecisionCard(item('permission'),{store:s,api:{answer:async()=>{throw Object.assign(new Error(STALE),{status:409});}}});doc.body.append(card.element);
    await choose(card);await card.element.dispatch('submit');assert.match(card.element.textContent,/已经处理或已过期/);assert.equal(s.refreshes,1);card.destroy();count++;
  }
  {
    const doc=setup(),s=store(),container=doc.createElement('section');doc.body.append(container);const inbox=mountInbox(container,{store:s,api:{}});
    const view={task:{id:'task',run_id:'run'},inbox:[item('ask_user',{questions:[{name:'text',question:'请写说明',type:'text'}]})]};inbox.update(view);
    const card=container.querySelector('.wb-decision-card'),input=card.querySelector('textarea');input.value='尚未提交的文字';
    inbox.update({...view,version:'changed-output'});assert.equal(container.querySelector('.wb-decision-card'),card);assert.equal(input.value,'尚未提交的文字');
    assert.equal(inbox.open(view.inbox[0].id),true);assert.equal(doc.activeElement,card);
    s.generation++;inbox.update(view);assert.notEqual(container.querySelector('.wb-decision-card'),card);inbox.destroy();count++;
  }
  {
    const doc=setup(),s=store(),container=doc.createElement('section'),calls=[];doc.body.append(container);
    const inbox=mountInbox(container,{store:s,api:{answer:async(...args)=>{calls.push(args);return {ok:true};}}});
    const row=item('human_oncall',{questions:[{name:'choice',question:'是否继续',options:[{value:'go',label:'继续'},{value:'wait',label:'暂缓'}]}]});
    const view={task:{id:'task',run_id:'run'},inbox:[row]};
    inbox.update({...view,inbox:[{...row,live:false,resume_required:true,background:'任务已暂停'}]});
    const old=container.querySelector('form');old.querySelector('button').focus();
    assert.equal(old.querySelectorAll('input').length,0);
    inbox.update({...view,inbox:[{...row,live:true}]});
    const current=container.querySelector('form');assert.notEqual(current,old);assert.equal(doc.activeElement,current);
    assert.doesNotMatch(current.textContent,/继续任务并核对问题|任务已暂停/);
    assert.equal(current.querySelectorAll('input').filter(x=>x.type==='radio').length,2);
    await current.dispatch('submit');assert.equal(calls.length,0);
    await choose({element:current});await current.dispatch('submit');
    assert.deepEqual(calls[0][2].answers,{choice:'go'});assert.equal(calls[0][2].action,undefined);
    inbox.destroy();count++;
  }
  {
    const doc=setup(),s=store(),container=doc.createElement('section');doc.body.append(container);
    const inbox=mountInbox(container,{store:s,api:{}}),row=item('ask_user',{questions:[{name:'text',type:'text',question:'说明'}]});
    const view={task:{id:'task',run_id:'run'},inbox:[row]};inbox.update(view);
    const form=container.querySelector('form'),input=form.querySelector('textarea');input.value='保留未提交的说明';input.focus();
    const reordered=JSON.parse(JSON.stringify(row),(_key,value)=>value && !Array.isArray(value) && typeof value==='object' ? Object.fromEntries(Object.entries(value).reverse()) : value);
    inbox.update({...view,version:'progress-only',inbox:[reordered]});
    assert.equal(container.querySelector('form'),form);assert.equal(input.value,'保留未提交的说明');assert.equal(doc.activeElement,input);
    inbox.destroy();count++;
  }
  {
    const doc=setup(),s=store(),container=doc.createElement('section'),calls=[];doc.body.append(container);
    const inbox=mountInbox(container,{store:s,api:{answer:async(...args)=>{calls.push(args);return {ok:true};}}});
    const first=item('permission'),row=item('ask_user');const view={task:{id:'task',run_id:'run'},inbox:[first,row]};inbox.update(view);
    const unchanged=container.querySelectorAll('form')[0],old=container.querySelectorAll('form')[1];await choose({element:old});old.querySelector('button').focus();
    const updated={...row,questions:[{...question,options:[{value:'new-choice',label:'当前选项'}]}],context:{request_id:'current-request'}};
    inbox.update({...view,inbox:[first,updated]});
    const current=container.querySelectorAll('form')[1];assert.equal(container.querySelectorAll('form')[0],unchanged);
    assert.notEqual(current,old);assert.equal(doc.activeElement,current);
    assert.equal(current.querySelectorAll('input').some(x=>x.checked),false);
    await old.dispatch('submit');await current.dispatch('submit');assert.equal(calls.length,0);
    await choose({element:current});await current.dispatch('submit');
    assert.equal(calls[0][1].context.request_id,'current-request');assert.deepEqual(calls[0][2].answers,{action:'new-choice'});
    inbox.destroy();count++;
  }
  {
    const doc=setup(),s=store(),container=doc.createElement('section');doc.body.append(container);let resolve;
    const inbox=mountInbox(container,{store:s,api:{answer:()=>new Promise(done=>resolve=done)}});
    const row=item('human_oncall'),view={task:{id:'task',run_id:'run'},inbox:[row]};
    inbox.update({...view,inbox:[{...row,resume_required:true}]});
    const pending=container.querySelector('form').dispatch('submit');await settle();inbox.update(view);
    const current=container.querySelector('form');resolve({ok:true,answered:false});await pending;
    assert.doesNotMatch(current.querySelector('.wb-error').textContent,/已提交/);assert.equal(s.refreshes,0);
    assert.equal(current.querySelector('button').disabled,false);inbox.destroy();count++;
  }
  {
    const doc=setup();const q=mountQuestion({...question,multiple:true,options:Array.from({length:6},(_,i)=>({value:i,label:'选项'+i}))});doc.body.append(q.element);
    const controls=q.element.querySelectorAll('input');controls[0].checked=true;await controls[0].dispatch('change');
    await q.element.querySelectorAll('button')[1].click();controls[5].checked=true;await controls[5].dispatch('change');
    assert.deepEqual(q.read().value,[0,5]);assert.equal(q.element.querySelectorAll('.wb-option').filter(row=>!row.hidden).length,2);count++;
  }
  {
    setup();const q=mountQuestion({name:'docs',question:'填写路径',type:'list',options:[{value:'manual',label:'自行填写'}],prepared_choices:[{id:'manual',kind:'action',action:'manual',requires_input:true}]},{bundle:true});
    const input=q.element.querySelector('input');input.checked=true;await input.dispatch('change');q.element.querySelector('textarea').value='a.txt\nb.txt';
    assert.deepEqual(q.read().value,{custom:['a.txt','b.txt']});count++;
  }
  {
    const doc=setup(),s=store(),calls=[];
    const q={name:'choice',question:'继续方式',options:[{value:'research',label:'补充材料'}],prepared_choices:[{id:'research',kind:'action',action:'research',requires_input:true}]};
    const card=mountDecisionCard(item('ask_user',{questions:[q],choice_bundle:{id:'bundle'}}),{store:s,api:{prepare:async(...args)=>{calls.push(args);return {ok:true};},answer:()=>{throw new Error('must not answer');}}});doc.body.append(card.element);
    await choose(card);card.element.querySelector('textarea').value='补充官方资料';await card.element.dispatch('submit');
    assert.deepEqual(calls[0][2],{action:'research',input:'补充官方资料'});card.destroy();count++;
  }
  {
    const doc=setup(),s=store(),calls=[];const context={id:'draft',digest:'sha256',base_version:4,changes:[{body:'新的要求'}]};
    const card=mountDecisionCard(item('revision_draft',{context}),{store:s,api:{answer:async(...args)=>{calls.push(args);return {ok:true};}}});doc.body.append(card.element);
    await choose(card);await card.element.dispatch('submit');assert.equal(calls[0][2].draft_digest,'sha256');assert.equal(calls[0][2].base_version,4);assert.match(card.element.textContent,/新的要求/);card.destroy();count++;
  }
  {
    const doc=setup(),calls=[],host={appendNotice(text,action){const button=doc.createElement('button');button.textContent=text;button.addEventListener('click',action);doc.body.append(button);return button;}};
    const task={id:'task',run_id:'run'},notices=createNotices(host,(...args)=>calls.push(args)),row=item('ask_user');
    notices.update(task,[row]);notices.update(task,[row]);assert.equal(doc.body.children.length,1);await doc.body.firstChild.click();assert.deepEqual(calls[0],[row.id,task]);
    assert.equal(doc.body.querySelectorAll('input,select,textarea').length,0);
    notices.update(task,[]);assert.equal(doc.body.firstChild.disabled,true);assert.match(doc.body.textContent,/已处理/);count++;
  }
  assert.equal(deadlineText(item('ask_user',{deadline:1}),2000),'需要你决定');
  assert.equal(deadlineText(item('ask_user',{mandatory:false,deadline:100}),90000),'0:10 后由宿主处理');count++;
  {
    const doc=setup(),s=store(),container=doc.createElement('section');doc.body.append(container);
    const inbox=mountInbox(container,{store:s,api:{answer:async()=>{throw Object.assign(new Error(STALE),{status:409});}}});
    const view={task:{id:'task',run_id:'run'},inbox:[item('ask_user')]};inbox.update(view);
    const form=container.querySelector('form');await choose({element:form});await form.dispatch('submit');
    inbox.update({...view,inbox:[]});assert.equal(container.querySelectorAll('form').length,0);
    assert.equal(container.querySelector('.wb-inbox-conflict').textContent,STALE);assert.equal(container.querySelector('.wb-inbox-conflict').hidden,false);
    s.generation++;inbox.update({...view,inbox:[]});assert.equal(container.querySelector('.wb-inbox-conflict').hidden,true);inbox.destroy();count++;
  }
  {
    const doc=setup(),s=store(),calls=[],q={name:'format',type:'choice',question:'输出格式',options:[{value:'token',label:'表格'}],prepared_choices:[{id:'token',kind:'value',value:'table'}]};
    const dependent={name:'columns',type:'text',question:'列名',required_when:{input:'format',equals:'table'}};
    const card=mountDecisionCard(item('run_inputs',{questions:[q,dependent],choice_bundle:{id:'b'}}),{store:s,api:{answer:async(...args)=>{calls.push(args);return {ok:true};}}});doc.body.append(card.element);
    assert.equal(card.element.querySelectorAll('fieldset')[1].hidden,true);await choose(card);await card.element.dispatch('change');
    assert.equal(card.element.querySelectorAll('fieldset')[1].hidden,false);await card.element.dispatch('submit');assert.equal(calls.length,0);
    card.element.querySelectorAll('fieldset')[1].querySelector('textarea').value='名称、来源';await card.element.dispatch('submit');
    assert.deepEqual(calls[0][2].answers,{format:'token',columns:{custom:'名称、来源'}});card.destroy();count++;
  }
  {
    const doc=setup(),s=store(),calls=[],q={name:'route',question:'处理方式',options:[{value:'prepare',label:'重新准备'}],prepared_choices:[{id:'prepare',kind:'action',action:'research',requires_input:true}]};
    const card=mountDecisionCard(item('recovery_case',{questions:[q],choice_bundle:{id:'b'}}),{store:s,api:{answer:async(...args)=>{calls.push(args);return {ok:true};}}});doc.body.append(card.element);
    await choose(card);card.element.querySelector('textarea').value='只读核验';await card.element.dispatch('submit');
    assert.deepEqual(calls[0][2].answers,{route:'prepare'});assert.deepEqual(calls[0][2].choice_inputs,{prepare:'只读核验'});card.destroy();count++;
  }
  {
    const doc=setup(),s=store();let resolve;
    const card=mountDecisionCard(item('permission'),{store:s,api:{answer:()=>new Promise(done=>resolve=done)}});doc.body.append(card.element);
    await choose(card);const pending=card.element.dispatch('submit');await settle();s.generation++;resolve({ok:true});await pending;
    assert.equal(s.refreshes,0);assert.doesNotMatch(card.element.querySelector('.wb-error').textContent,/已提交/);card.destroy();count++;
  }
  {
    const doc=setup(),s=store(),container=doc.createElement('section');doc.body.append(container);
    const inbox=mountInbox(container,{store:s,api:{answer:async()=>({ok:true})}}),view={task:{id:'task',run_id:'run'},inbox:[item('permission')]};inbox.update(view);
    const form=container.querySelector('form'),radio=form.querySelector('input');radio.checked=true;await radio.dispatch('change');form.querySelector('button').focus();
    const pending=form.dispatch('submit');doc.body.focus();await pending;inbox.update({...view,inbox:[]});assert.equal(doc.activeElement.id,'workbench-inbox-title');inbox.destroy();count++;
  }
  {
    const doc=setup(),s=store(),calls=[];
    const context={handling:{summary:{goal:'核对来源',progress:'检查点已保留',why_user:'缺少你持有的目录',impact:'后续索引暂不能确认'}},
      report:{diagnosis:{symptom:{summary:'目录未传递',evidence_id:'E-17'},tried:['尝试读取原目录'],hypotheses:[
        {summary:'原文件缺失',confidence:'low',evidence:['E-22']},{summary:'交接遗漏',confidence:'high',evidence:['E-23']}]}},rounds:[{result:'raw_internal_log'}],
      pending:{authority:[{scope:'限定文件'}]}};
    const q={name:'choice',question:'下一步',preserve_choice_value:true,options:[{value:'manual',action:'manual',label:'提出自己的判断',requires_input:true}]};
    const card=mountDecisionCard(item('recovery_case',{context,questions:[q]}),{store:s,api:{answer:async(...args)=>{calls.push(args);return {ok:true};}}});doc.body.append(card.element);
    for(const value of ['检查点已保留','缺少你持有的目录','后续索引暂不能确认','目录未传递','尝试读取原目录','E-17']) assert.ok(card.element.textContent.includes(value));
    assert.deepEqual(card.element.querySelectorAll('.wb-diagnosis-hypothesis').map(row=>row.querySelector('p').textContent),['交接遗漏','原文件缺失']);
    assert.equal(card.element.querySelectorAll('details').some(row=>row.open),false);
    await choose(card);await card.element.dispatch('submit');assert.equal(calls.length,0);
    card.element.querySelector('textarea').value='先核对现有清单';await card.element.dispatch('submit');
    assert.deepEqual(calls[0][2].answers,{choice:'manual'});assert.deepEqual(calls[0][2].choice_inputs,{manual:'先核对现有清单'});card.destroy();count++;
  }
  console.log(`workbench_inbox: ${count} cases passed`);
})().catch(error=>{console.error(error);process.exitCode=1;});
