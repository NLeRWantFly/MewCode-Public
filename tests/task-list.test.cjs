const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const app = fs.readFileSync(path.join(__dirname, '../frontend/static/app.js'), 'utf8');
const start = app.indexOf('  function taskListDuration(');
const end = app.indexOf('  function restoreTasksCache(', start);
assert.ok(start >= 0 && end > start);
function harness() {
  const list = {innerHTML:'', children:[], appendChild(row) { this.children.push(row); }};
  const count = {textContent:''};
  const ctx = {document:{createElement:() => ({})}, sessionStorage:{setItem() {}},
    currentStatus:{conversation_id:'fixture'}, $:id=>id==='task-list'?list:count,
    fmtDur:n=>n+'s', esc:s=>String(s).replaceAll('<','&lt;').replaceAll('>','&gt;')};
  vm.createContext(ctx);
  vm.runInContext(app.slice(start,end)+';globalThis.api={taskListDuration,renderTasks};',ctx);
  return {ctx,list,count};
}
test('sidebar uses measured time, labels unknown/queued/lower-bound without wall-time fallback',()=>{
  const {ctx}=harness(); const label=ctx.api.taskListDuration;
  assert.equal(label({elapsed:3540}), '旧记录时长未知');
  assert.equal(label({elapsed:3540,duration_basis:'legacy_wall'}), '旧记录时长未知');
  const measured={duration_basis:'active_runtime',runtime_started:false,elapsed:0,status:'queued'};
  assert.equal(label(measured),'未开始');
  assert.equal(label({...measured,runtime_started:true,elapsed:12.5}),'累计执行 12.5s');
  assert.match(label({...measured,elapsed:8,runtime_started:true,duration_quality:'lower_bound'}),/^至少 8s/);
  assert.equal(label({...measured,elapsed:null}),'执行时长未知');
  assert.equal(label({...measured,status:'completed',duration_quality:'unknown'}),'执行时长未知');
});
test('sidebar counts queue separately, names parent task and preserves cleanup/cancel outcomes',()=>{
  const {ctx,list,count}=harness();
  ctx.api.renderTasks([
    {id:'a',name:'first',task:'work',owner_task_title:'<parent A>',status:'running',cancellation_pending:true},
    {id:'b',name:'second',task:'work',owner_task_title:'parent B',status:'queued'},
    {id:'c',name:'third',task:'work',status:'failed',cleanup_failed:true},
    {id:'d',name:'last',task:'work',status:'cancelled'},
  ]);
  assert.equal(count.textContent,'1 运行中 · 1 排队中');
  assert.match(list.children[0].innerHTML,/所属任务：&lt;parent A&gt;/);
  assert.match(list.children[0].innerHTML,/正在停止/);
  assert.match(list.children[2].innerHTML,/清理未确认/);
  assert.match(list.children[3].innerHTML,/已取消/);
  for(const row of list.children) assert.doesNotMatch(row.innerHTML,/undefineds|nulls|3540s/);
});
