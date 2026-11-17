const assert = require('node:assert/strict');
const {setup,settle} = require('./workbench_dom.cjs');
(async()=>{
  const {mountGeneration}=await import('../frontend/static/workbench/generation.js');
  const document=setup(), container=document.createElement('div'); document.body.append(container);
  const scope={project:'p',conversation_id:'c'}, timers=[], requests=[];
  const api={get:async(path)=>{requests.push(path);return {project:'p',conversation_id:'c',running:false,
    candidate:{log:[]},process:{headline:'旧 model_stopped',last_result:'旧网络授权错误',next_step:'重复构图'}};},
    post:async()=>{throw Error('Read-only diagnosis must not submit');}};
  const ui=mountGeneration(container,{api,store:{generation:1,state:{scope}},delay:fn=>{timers.push(fn);return timers.length;},cancelDelay:()=>{}});
  const health={fingerprint:'v1',identity:{material_sequence:7,candidate_sequence:88,candidate_id:'candidate'},
    summary:'当前素材有 2 项结构阻塞',previous_failure:{status:'historical'},runtime:{status:'current'},
    current_validation:{diagnostics:[{message:'resolution 收集节点不存在'},{message:'batch_size 收集节点不存在'}]}};
  ui.update({task_id:'task',health,build_request:{state:'failed'}});
  await settle();await settle();
  assert.equal(container.hidden,false);
  const current=container.querySelector('.wb-generation-health');
  assert.match(container.querySelector('details').textContent,/素材版本 7 · 候选版本 88/);
  assert.match(container.querySelector('details').textContent,/原失败属于旧版本/);
  assert.match(current.textContent,/resolution/);assert.match(current.textContent,/batch_size/);
  assert.doesNotMatch(container.textContent,/旧网络授权错误|重复构图/);
  assert.match(container.textContent,/历史失败已保留/);
  ui.update({task_id:'task',health:{...health,fingerprint:'v2',summary:'当前结构校验通过',current_validation:{diagnostics:[]}},build_request:{state:'failed'}});
  timers.shift()();await settle();await settle();
  assert.match(current.textContent,/当前结构校验通过/);assert.doesNotMatch(current.textContent,/收集节点不存在/);
  assert.ok(requests.every(path=>path.endsWith('/progress')));
  ui.destroy();console.log('orch_design_health: current version, stale failure and no automatic retry passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
