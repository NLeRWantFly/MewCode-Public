const assert = require('node:assert/strict');
const {pathToFileURL} = require('node:url');
const path = require('node:path');
const {setup} = require('./workbench_dom.cjs');
const load = name => import(pathToFileURL(path.resolve(__dirname, '../frontend/static/workbench/' + name)));

(async () => {
  const {mountRunNotice} = await load('run-notice.js');
  const {mountStep,isCurrent} = await load('step.js');
  const doc=setup(),container=doc.createElement('section');doc.body.append(container);
  let opened=0;
  const notice=mountRunNotice(container,{openSettings:()=>opened++});
  const box=container.querySelector('.wb-run-notice'),button=box.querySelector('button');
  const task={id:'t',run_id:'r',status:'已暂停',reason:'专家角色配置失效：<img src=x> 模型不可用'};
  notice.update({task});
  assert.equal(box.hidden,false);assert.match(box.textContent,/当前流程已暂停/);
  assert.match(box.textContent,/<img src=x>/);assert.equal(box.querySelectorAll('img').length,0);
  assert.equal(button.hidden,false);assert.equal(opened,0);
  await button.click();assert.equal(opened,1);
  notice.update({task:{...task,readonly:true}});assert.equal(button.hidden,true);
  notice.update({task:{...task,status:'等你处理',reason:'请提供补充材料'}});
  assert.equal(box.hidden,false);assert.equal(button.hidden,true);
  notice.update({task:{...task,status:'已阻塞',execution_status:'blocked',reason:'模型对话已结束，节点尚未交付。'}});
  assert.equal(box.hidden,false);assert.equal(button.hidden,true);
  notice.update({task:{...task,status:'已停止',reason:'',current_activity:'任务已停止'}});
  assert.match(box.textContent,/任务已停止/);
  for(const next of [null,{task:{...task,status:'进行中'}},{task:{...task,run_id:''}}]) {
    notice.update(next);assert.equal(box.hidden,true);
  }
  const first={id:'n',name:'执行',status:'已暂停',status_reason:'旧模型不可用',detail:{goal:'交付'},attempts:1};
  const item=mountStep(first,{api:{},store:{generation:1,state:{}},task});
  assert.equal(isCurrent(first),true);assert.match(item.element.textContent,/旧模型不可用/);
  item.update({...first,status_reason:'请检查新的模型配置'},task,[]);
  assert.match(item.element.textContent,/请检查新的模型配置/);
  assert.doesNotMatch(item.element.textContent,/旧模型不可用/);
  console.log('workbench run notice: 3 cases passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
