const assert = require('node:assert/strict');
const {pathToFileURL} = require('node:url');
const path = require('node:path');
const {setup} = require('./workbench_dom.cjs');
(async () => {
  const {mountPlan} = await import(pathToFileURL(path.resolve(__dirname, '../frontend/static/workbench/plan.js')));
  const doc = setup(), root = doc.createElement('section'); doc.body.append(root);
  const store = {generation: 1, state: {scope: {conversation_id: 'synthetic'}}};
  const plan = mountPlan(root, {store, api: {}, host: {}});
  const budget = [{label: '执行尝试', used: 3, reserved: 1, limit: 9}];
  const view = {task: {id: 'task', run_id: 'run', mode: 'collaborative', budget}, requirements: [], plan: [
    {id: 'now', title: '初步成果', status: '进行中', steps: [
      {id: 'child', name: '收集资料', status: '未开始', attempts: 1, detail: {
        goal: '按原目标搜集资料', expanded_from: {contract_id: 'parent', contract_version: 2}, parent_label: '研究报告', budget}},
      {id: 'parent', name: '研究报告', status: '未开始', attempts: 1, detail: {goal: '完整报告',
        join_children: [{id: 'child', label: '收集资料'}]}}]},
    {id: 'future', title: '后续报告', status: '待展开', deferred: true, steps: [],
      checks: [{criteria: ['覆盖所有原始需求']}]}]};
  plan.update(view);
  assert.match(root.textContent, /所属任务：研究报告 · 第 2 版/);
  assert.match(root.textContent, /当前分支累计预算执行尝试：已用 3，预留 1，上限 9/);
  assert.match(root.textContent, /汇合前须验收的子任务收集资料/);
  assert.match(root.textContent, /前序交付验收后，再生成本里程碑的具体步骤/);
  assert.match(root.textContent, /验收要求：覆盖所有原始需求/);
  assert.equal(root.querySelectorAll('.wb-step').length, 2);
  view.plan[1] = {...view.plan[1], deferred: false, status: '进行中'};
  plan.update(view);
  assert.doesNotMatch(root.textContent, /前序交付验收后/);
  plan.destroy();
  console.log('workbench_autonomous: ancestry, join, budgets, deferred milestone passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
