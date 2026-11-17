'use strict';
// Full production UI. Only test-provider timing endpoints are used below.
module.exports = async ({base, classicBase, evaluate, capture, rpc, delay, newTab}) => {
  const wait = (expression,ev=evaluate) => ev(`new Promise((resolve,reject)=>{let n=0;const timer=setInterval(async()=>{try{if(await (${expression})){clearInterval(timer);resolve(true);return;}}catch{}if(++n>600){clearInterval(timer);fetch('/__test__/workbench').then(r=>r.json()).then(s=>reject(new Error(${JSON.stringify(expression)}+'\\n'+JSON.stringify(s)+'\\n'+document.body.innerText.slice(-3500))));}},40);})`);
  const click = (text,selector='#task-workbench button',ev=evaluate) => wait(`(()=>{const b=[...document.querySelectorAll(${JSON.stringify(selector)})].find(b=>b.textContent.trim()===${JSON.stringify(text)}&&!b.disabled&&b.getClientRects().length);if(!b)return false;b.scrollIntoView({block:'center'});b.click();return true;})()`,ev);
  const boot = async(url,ev=evaluate,r=rpc) => {
    await r('Page.navigate',{url:url+'/desktop-boot?session=chat-queue-fixture-only&next=/?workbench=tasks'});
    await wait(`window.MewWorkbench?.store.state.scope.conversation_id && !window.MewWorkbenchHost.busy() && [...document.querySelectorAll('.wb-design-start button')].some(b=>b.textContent==='设计并执行'&&!b.disabled)`,ev);
  };
  const send = text => evaluate(`(()=>{const el=document.getElementById('chat-input');el.value=${JSON.stringify(text)};el.dispatchEvent(new Event('input',{bubbles:true}));document.getElementById('btn-send').click();})()`);
  const start = async(mode,url)=>{
    await boot(url);
    await evaluate(`(()=>{const s=document.querySelector('[aria-label="任务编排模式"]');s.value=${JSON.stringify(mode)};s.dispatchEvent(new Event('change',{bubbles:true}));})()`);
    if(mode==='collaborative'){
      await click('只生成编排图');
      await wait(`window.MewWorkbench.store.state.view?.task.intent==='plan' && [...document.querySelectorAll('.wb-design-start button')].some(b=>b.textContent==='设计并执行'&&!b.disabled)`);
      const original=await evaluate(`window.MewWorkbench.store.state.view.task.id`);
      await click('设计并执行');
      await wait(`document.querySelector('.wb-design-confirm')?.hidden===false`);
      if(await evaluate(`window.MewWorkbench.store.state.view.task.intent`)!=='plan')throw new Error('Execution intent changed before confirmation');
      await capture('design-execution-confirmation.png');
      await click('暂不执行');
      if(await evaluate(`window.MewWorkbench.store.state.view.task.intent`)!=='plan')throw new Error('Cancel granted execution');
      await click('设计并执行');await click('确认设计并执行');
      await wait(`window.MewWorkbench.store.state.view?.task.intent==='execute'`);
      if(await evaluate(`window.MewWorkbench.store.state.view.task.id`)!==original)throw new Error('Confirmation replaced the task');
    }else await click('设计并执行');
    await delay(500);
    await wait(`window.MewWorkbench.store.state.view?.task.design_stage`);
    await send('整理两份研究资料，核对差异并生成可追溯的交付包');
    await wait(`window.MewWorkbench.store.state.view?.inbox.length>0 && document.querySelector('.wb-decision-card') && document.querySelector('.wb-chat-notice')`);
  };
  const answer = async(value,ev=evaluate) => {
    const target=`(()=>{for(const item of window.MewWorkbench.store.state.view?.inbox||[])for(const q of item.questions||[]){const index=(q.options||q.choices||[]).findIndex(o=>(o.value??o.id)===${JSON.stringify(value)});if(index<0)continue;return [...(document.getElementById('workbench-item-'+item.id)?.querySelectorAll('input')||[])].find(i=>i.name===item.id+'-'+q.name&&i.value===String(index));}})()`;
    await wait(target,ev);
    return ev(`(()=>{const input=${target};input.click();input.closest('form').requestSubmit();})()`);
  };
  await start('collaborative',base);
  await evaluate(`(()=>{for(const id of ['human-oncall-panel','plan-revision-panel','orch-board','orch-progress-panel','orch-trace-panel','orch-canvas-overlay','expert-flow-map'])if(document.getElementById(id))throw new Error('Retired panel '+id);if(document.querySelector('#messages .wb-decision-card'))throw new Error('Answer card leaked into chat');if(!document.querySelector('.wb-chat-notice'))throw new Error('Chat notice absent');})()`);
  await evaluate(`window.MewWorkbench.openItem(window.MewWorkbench.store.state.view.inbox[0].id)`);
  await capture('t95-decision.png');

  // Two real renderer pages with the same still-unanswered request. Delay page
  // 2 background reads as well as pushes; state polling can otherwise refresh
  // it before the late submit. Release real reads after the 409, invent no view.
  const second=await newTab();
  await second.rpc('Page.navigate',{url:base+'/desktop-boot?session=chat-queue-fixture-only&next=/?workbench=tasks'});
  await wait(`window.MewWorkbench?.store.state.view?.inbox.length>0`,second.evaluate);
  await second.evaluate(`(()=>{const stop=e=>e.stopImmediatePropagation();for(const name of ['mewcode:workbench-changed','mewcode:workbench-state'])window.addEventListener(name,stop,{capture:true});let release;const delayed=new Promise(r=>release=r),api=window.MewWorkbench.api,read=api.readView,list=api.listTasks;api.readView=async(...args)=>{await delayed;return read(...args);};api.listTasks=async(...args)=>{await delayed;return list(...args);};window.__releaseReads=()=>{for(const name of ['mewcode:workbench-changed','mewcode:workbench-state'])window.removeEventListener(name,stop,{capture:true});api.readView=read;api.listTasks=list;release();};})()`);
  const version=await evaluate(`window.MewWorkbench.store.state.view.version`);
  await rpc('Page.reload');
  await wait(`window.MewWorkbench?.store.state.view?.inbox.length>0`);
  if(await evaluate(`window.MewWorkbench.store.state.view.version`)!==version)throw new Error('Reload changed host view version');
  await answer('all');
  await wait(`fetch('/__test__/workbench').then(r=>r.json()).then(s=>s.answered?.scope==='all')`);
  await second.evaluate(`window.__answerStatuses=[];const answer0=window.MewWorkbench.api.answer;window.MewWorkbench.api.answer=async(...args)=>{try{return await answer0(...args);}catch(error){window.__answerStatuses.push(error.status);window.__releaseReads();throw error;}};void 0;`);
  await answer('all',second.evaluate);
  await wait(`window.__answerStatuses.includes(409)`,second.evaluate);
  await wait(`window.MewWorkbench.store.state.view.inbox.length===0`,second.evaluate);
  await wait(`document.body.innerText.includes('该事项已经处理或已过期，请以当前显示的内容为准')`,second.evaluate);
  await second.capture('t95-stale-tab.png');await second.close();

  await wait(`!document.getElementById('btn-send').disabled && window.MewWorkbench.store.state.view.inbox.length===0`);
  await send('目标改为逐条核对全部资料，并为每项差异补充原始来源');
  await wait(`document.querySelectorAll('.wb-chat-revision-button').length>=2`);
  await evaluate(`[...document.querySelectorAll('#messages .msg.user')].at(-1).querySelector('.wb-chat-revision-button').click()`);
  await wait(`window.MewWorkbench.store.state.view.inbox.some(i=>i.type==='revision_draft')`);
  await answer('accept');
  await wait(`window.MewWorkbench.store.state.view.deliverables.some(d=>d.status==='需重新核验')`);
  const invalidated=await evaluate(`window.MewWorkbench.store.state.view.deliverables.filter(d=>d.status==='需重新核验').map(d=>({id:d.id,node_id:d.node_id,name:d.name}))`);
  await capture('t95-invalidated.png');
  await evaluate(`fetch('/__test__/review-and-resume',{method:'POST'}).then(async r=>{if(!r.ok)throw new Error(await r.text());return r.json();})`);
  await wait(`window.MewWorkbench.store.state.view.task.status==='已完成'`);
  await evaluate(`(()=>{const v=window.MewWorkbench.store.state.view;for(const old of ${JSON.stringify(invalidated)}){if(!v.deliverables.some(d=>d.node_id===old.node_id&&d.name===old.name&&d.id!==old.id&&d.status==='已验收'))throw new Error('Artifact was not reaccepted: '+old.name);if(v.deliverables.find(d=>d.id===old.id)?.status!=='需重新核验')throw new Error('Historical certificate was incorrectly reactivated');}if(v.task.spec_version<2)throw new Error('Revision not adopted');})()`);
  await evaluate(`document.documentElement.dataset.theme='light';`);
  await wait(`document.querySelector('.wb-task-select option:checked')?.textContent.includes('已完成')`);
  await evaluate(`document.getElementById('task-workbench').scrollTop=0;document.querySelector('.wb-task-content').parentElement.scrollTop=0;`);
  await capture('t95-light.png');
  await evaluate(`document.documentElement.dataset.theme='dark';document.body.dataset.theme='dark';`);
  await capture('t95-dark.png');
  await evaluate(`document.documentElement.dataset.theme='light';document.body.dataset.theme='light';window.MewWorkbench.openStep('work');`);
  await click('查看过程');
  await wait(`document.querySelector('.wb-process')?.textContent.length>20`);
  await capture('t95-process.png');
  await click('编排图');
  await evaluate(`(()=>{const summary=[...document.querySelectorAll('#task-workbench summary')].find(s=>s.textContent==='比较计划版本');if(!summary||summary.parentElement.hidden)throw new Error('Version comparison absent');summary.click();const from=document.querySelector('[aria-label="比较的原计划版本"]'),to=document.querySelector('[aria-label="比较的新计划版本"]');from.value=from.options[0].value;to.value=to.options[to.options.length-1].value;to.dispatchEvent(new Event('change',{bubbles:true}));if(from.value===to.value||!document.querySelector('.wb-version-change'))throw new Error('Actual version diff absent');})()`);
  await capture('t95-structure.png');
  await rpc('Emulation.setDeviceMetricsOverride',{width:480,height:900});
  await evaluate(`window.MewWorkbench.open();`);await delay(200);
  await evaluate(`(()=>{if(document.documentElement.scrollWidth>innerWidth+1)throw new Error('Narrow overflow');if(document.getElementById('task-workbench').dataset.drawer!=='true')throw new Error('Narrow drawer absent');})()`);
  await capture('t95-drawer.png');
  await rpc('Emulation.setDeviceMetricsOverride',{width:1440,height:1000});

  await evaluate(`(()=>{const s=document.querySelector('.wb-task-select');const idx=window.MewWorkbench.store.state.tasks.findIndex(t=>t.mode==='team');if(idx<0)throw new Error('Team missing');s.value=String(idx);s.dispatchEvent(new Event('change',{bubbles:true}));})()`);
  await wait(`window.MewWorkbench.store.state.view.task.mode==='team'`);
  await evaluate(`(()=>{const v=window.MewWorkbench.store.state.view;if(!v.plan.some(g=>g.steps.length)||!v.inbox.some(i=>i.type==='team_intervention')||!v.deliverables.length)throw new Error('Team projection incomplete');})()`);
  await capture('t95-team.png');
  for(const [old,target] of [['/orch-canvas','/?workbench=structure'],['/teams','/?workbench=tasks'],['/expert-flow','/'],['/pipeline','/']]) {
    await rpc('Page.navigate',{url:base+old});
    await wait(`location.pathname+location.search===${JSON.stringify(target)} && window.MewWorkbench`);
    if(target.includes('structure'))await wait(`document.querySelector('#task-workbench [role="tab"][aria-selected="true"]').textContent==='编排图'`);
  }
  await start('classic',classicBase);
  await evaluate(`(()=>{const v=window.MewWorkbench.store.state.view;if(v.plan.length!==1||!v.inbox.some(i=>i.type==='plan_revision'))throw new Error('Classic plan approval missing');})()`);
  await answer('approve');
  await wait(`window.MewWorkbench.store.state.view.task.status==='已完成'`);
  await evaluate(`(()=>{const steps=window.MewWorkbench.store.state.view.plan.flatMap(g=>g.steps);if(!steps.length||steps.some(s=>s.status!=='已完成'))throw new Error('Classic step state was not persisted');if(steps.some(s=>s.acceptance))throw new Error('Classic acceptance was fabricated');})()`);
  await capture('t95-classic.png');
  const provider=await evaluate(`fetch('/__test__/workbench').then(r=>r.json())`);
  if(provider.error)throw new Error(provider.error);
  return {scenarios:6,passed:6,syntheticProvider:true,realScheduler:true,realHostAnswers:true,reloadVersion:version,staleAnswerStatus:409};
};
