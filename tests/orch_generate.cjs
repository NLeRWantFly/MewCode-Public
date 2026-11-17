// Modal-only assertions retired in the T92 behavior map; all pure protocol assertions remain.
const fs=require('node:fs'),assert=require('node:assert/strict');
(async()=>{
const api=await import('../frontend/static/workbench/generation-protocol.js');
assert.equal(api.actionOf(null).disabled,true);
assert.equal(api.actionOf({running:true}).label,"生成中…");
assert.equal(api.actionOf({candidate:{status:"paused"},coverage:{pending:5}}).label,"继续生成");
assert.equal(api.actionOf({candidate:{status:"ready"},coverage:{pending:0}}).disabled,true);
assert.equal(api.actionOf({revision:0,coverage:{total:10,pending:10}}).disabled,false);
assert.equal(api.actionOf({revision:1,coverage:{total:10,pending:0}}).disabled,true);
assert.equal(api.autoActionOf({running:true}).label,"自动构图中…");
assert.equal(api.autoActionOf({candidate:{status:"paused"},delivery:{phase:"review"},coverage:{total:5,pending:0}}).label,"继续自动构图");
const retry = api.autoActionOf({candidate:{status:"ready"},delivery:{phase:"done",status:"needs_review"},coverage:{total:5,pending:0}});
assert.equal(retry.label,"重新补全并自审");
assert.equal(retry.disabled,false);
assert.equal(retry.retry,true);
assert.equal(api.autoActionOf({candidate:{status:"ready"},delivery:{phase:"review"},coverage:{pending:0}}).label,"继续自动构图");
assert.equal(api.autoActionOf({candidate:{status:"ready"},delivery:{phase:"done",status:"ready"},coverage:{total:5,pending:0}}).disabled,true);
assert.equal(api.autoActionOf({candidate:{status:"ready"},coverage:{total:5,pending:0}}).disabled,false);
assert.deepEqual(Array.from(api.lines("第一项\r\n\n 第二项 ")),["第一项","第二项"]);
const fullActions = {
  update:{enabled:false,reason:"没有新增来源"}, resume:{enabled:false,reason:"候选已完成"},
  discuss:{enabled:true,reason:"可继续讨论"}, repair:{enabled:false,reason:"没有结构问题"},
  save:{enabled:false,reason:"没有预览"}, rebase_current_canvas:{enabled:false,reason:"基线一致"},
  rebuild:{enabled:true,reason:"可以显式重读"},
};
assert.equal(api.actionOf({candidate:{status:"ready",stale:true},coverage:{new_sources:2},actions:{...fullActions,rebase_current_canvas:{enabled:true,reason:"可 rebase"}}}).label,"基于当前画布继续整理");
const failedWithSystemIssue=api.autoActionOf({candidate:{status:"failed"},delivery:{phase:"waiting_user",issue_groups:[{type:"unclassified"}]},coverage:{total:5,pending:0}});
assert.equal(failedWithSystemIssue.label,"继续自动构图");
assert.equal(failedWithSystemIssue.discuss,undefined);
assert.equal(api.autoActionOf({candidate:{status:"ready"},delivery:{phase:"waiting_user",issue_groups:[{type:"business"}]},coverage:{total:5,pending:0},actions:fullActions}).label,"查看待确认选择");
assert.equal(api.actionState({candidate:{status:"ready"},delivery:{phase:"done"},coverage:{total:1,pending:0},actions:fullActions},"discuss").enabled,true);
const changedSources={candidate:{status:"ready"},revision:4,coverage:{total:10,pending:0,history_changed:true}};
assert.equal(api.actionState(changedSources,"update").enabled,false,"changed history cannot use ordinary update in fallback mode");
assert.equal(api.actionState(changedSources,"resume").enabled,false,"changed history cannot resume an old candidate in fallback mode");
assert.equal(api.actionState(changedSources,"rebuild").enabled,true,"explicit full reread remains available in fallback mode");
assert.equal(api.actionOf(changedSources).disabled,true,"changed history disables the ordinary primary action");
assert.equal(api.actionState({candidate:{status:"ready"},revision:4,coverage:{total:10,pending:0},actions:fullActions},"rebuild").enabled,true);
assert.equal(api.selectionState({revision:4,coverage:{total:10,pending:0},actions:fullActions},{texts:["只纳入这一条"]}).enabled,true,
  "selected-message import does not depend on completed full-history update action");
assert.equal(api.selectionState(null,{texts:["只纳入这一条"]}).enabled,false,"selection waits for the initial state read");
assert.equal(api.selectionState({revision:4,coverage:{total:10,pending:0,history_changed:true},actions:fullActions},{texts:["过期消息"]}).enabled,false,
  "selection still refuses a changed source snapshot");
const canvasState=api.canvasStateOf({canvas_state:{exists:true,node_count:25,status:"saved"}});
assert.equal(canvasState.exists,true);
assert.equal(canvasState.node_count,25);
assert.equal(canvasState.status,"saved");
const grouped=api.groupEdges([{from_node:"a",to_node:"b"},{from_node:"a",to_node:"b",condition:"always"},{from_node:"a",to_node:"b",condition:"pass"}]);
assert.equal(grouped.length,2);
assert.equal(grouped[0].count,2);
const fixture=JSON.parse(fs.readFileSync("tests/fixtures/broken_draft_duplicates.json","utf8"));
const fixtureEdges=api.groupEdges(fixture.entries.filter(e=>e.kind==="edge"&&e.state!=="retired").map(e=>e.data));
assert.equal(fixtureEdges.length,41);
assert.equal(fixtureEdges.reduce((n,e)=>n+e.count,0),79);
const invalid={candidate:{status:"ready",readiness:{ready:false,diagnostics:[{code:"duplicate_business_key"}]}},issues:[],running:false,can_preview:false};
assert.equal(api.computeStatus(invalid).ready,false);
assert.ok(api.computeStatus(invalid).label.includes("待修复"));
assert.ok(api.computeStatus({...invalid,running:true}).label.includes("生成中"));
assert.equal(api.computeStatus({can_preview:true,candidate:null,canvas_state:{exists:true,node_count:3,status:"saved"}}).label,"阶段图已保存");
(async () => {
  await assert.rejects(api.readGraphResponse({ok:false,status:500,json:async()=>{throw new SyntaxError("Unexpected token I");}}),
    /阶段图服务暂时不可用（HTTP 500）/);
  await assert.rejects(api.readGraphResponse({ok:false,status:409,json:async()=>({error:"候选图已变化，请刷新"})}), /候选图已变化/);
  await assert.rejects(api.readGraphResponse({ok:true,status:200,json:async()=>null}), /返回格式异常/);
  await assert.rejects(api.readGraphResponse({ok:true,status:200,json:async()=>{throw new SyntaxError();}}), /返回格式异常/);
  const graph = {conversation_id:"paper",view:{nodes:[{id:"prepare"}]}};
  assert.equal(await api.readGraphResponse({ok:true,status:200,json:async()=>graph}), graph);
  console.log("graph HTTP errors retain useful diagnostics without JSON parser errors");
})().catch(error => { console.error(error); process.exitCode = 1; });
console.log('orch_generate.cjs: retained protocol checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
