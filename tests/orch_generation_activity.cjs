const assert = require('node:assert/strict');
const {setup, settle} = require('./workbench_dom.cjs');
(async () => {
  const {mountGeneration} = await import('../frontend/static/workbench/generation.js');
  const document = setup(), container = document.createElement('div'); document.body.append(container);
  const scope = {project:'p',conversation_id:'c'}, timers = [], calls = [];
  let now = Date.parse('2026-10-17T03:00:00Z'), fail = false;
  const data = {project:'p',conversation_id:'c',running:true,candidate:{log:[]},
    process:{headline:'AI 构图',stages:['处理历史','补全结构'],stage_index:1},
    progress:{request_started_at:'2026-10-17T02:58:00Z',model_label:'review-model',
      model:{stage:'requesting',output_chars:0}}};
  const api = {get:async path => {calls.push(path); if (fail) throw Error('offline'); return data;},
    post:async () => {throw Error('Progress must remain read-only');}};
  const ui = mountGeneration(container,{api,store:{generation:1,state:{scope}},now:() => now,
    delay:fn => {timers.push(fn);return timers.length;},cancelDelay:() => {}});
  const poll = async () => {timers.shift()();await settle();await settle();};
  ui.update({task_id:'task'});await settle();await settle();
  const activity = container.querySelector('.wb-generation-activity');
  assert.equal(activity.hidden,false);
  assert.match(activity.textContent,/等待模型响应.*review-model.*2:00.*尚未收到模型消息/);
  now += 2000;await poll();assert.match(activity.textContent,/2:02/);
  data.progress.model = {stage:'streaming',output_chars:120,last_event_at:'2026-10-17T03:00:01Z',
    output_preview:'PRIVATE RESPONSE',thinking_summary:'PRIVATE THINKING'};
  await poll();assert.match(activity.textContent,/正在接收结果.*120 字符.*0:01/);
  data.progress.model.output_chars = 180;data.progress.model.waiting = true;
  await poll();assert.match(activity.textContent,/180 字符.*仍在等待当前响应/);
  assert.doesNotMatch(container.textContent,/PRIVATE/);
  data.progress.model.stage = 'validating';data.progress.model.waiting = false;
  await poll();assert.match(activity.textContent,/正在校验结果/);
  assert.doesNotMatch(activity.textContent,/距上次模型消息/);
  fail = true;await poll();assert.equal(activity.hidden,true);assert.equal(activity.textContent,'');
  fail = false;await poll();assert.equal(activity.hidden,false);
  data.running = false;data.process.headline = '审查请求准备失败';
  await poll();assert.equal(activity.hidden,true);assert.equal(activity.textContent,'');
  assert.match(container.textContent,/审查请求准备失败/);
  assert.ok(calls.every(path => path.endsWith('/progress')));
  ui.update(null);assert.equal(container.hidden,true);ui.destroy();
  console.log('orch_generation_activity: elapsed, stream, wait, finish and read-only polling passed');
})().catch(error => {console.error(error);process.exitCode=1;});
