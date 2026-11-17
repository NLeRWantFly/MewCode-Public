// Modal-only assertions retired in the T92 behavior map; all pure protocol assertions remain.
const fs=require('node:fs'),assert=require('node:assert/strict');
(async()=>{
const api=await import('../frontend/static/workbench/draft-protocol.js');
assert.equal(api.sameScope({project:"C:\\P",conversation_id:"a"},{project:"c:/p",conversation_id:"a"}),true);
assert.equal(api.sameScope({project:"C:\\P",conversation_id:"a"},{project:"C:\\P",conversation_id:"b"}),false);
assert.match(api.coverageText({coverage:{total:120,processed:20,pending:100,history_changed:true}}),/待整理 100.*历史已变化/);
const plan={nodes:[{node_id:"a"},{node_id:"b"},{node_id:"c"},{node_id:"d"}],
  edges:[{from_node:"a",to_node:"b",condition:"always"},{from_node:"a",to_node:"c",condition:"always"},
    {from_node:"b",to_node:"d",condition:"pass"},{from_node:"c",to_node:"d",condition:"always"},
    {from_node:"d",to_node:"a",condition:"loop"}]};
const before=JSON.stringify(plan), positions=api.layout(plan).positions;
assert.equal(before,JSON.stringify(plan));
assert.equal(positions.b.y,positions.c.y);
assert.notEqual(positions.b.x,positions.c.x);
assert.ok(positions.d.y>positions.b.y);
assert.equal(api.formatDuration(83000),"1:23");
assert.equal(api.formatDuration(3723000),"1:02:03");
assert.match(api.etaText([{status:"ok",duration_ms:60000}],3),/约 3 分钟/);
assert.equal(api.etaText([],3),"");
assert.deepEqual(api.withLogicalChunks([
  {chunk:7,status:"retry"},{chunk:8,status:"failed"},{chunk:9,status:"ok"},{chunk:10,status:"retry"}
]).map(x=>x.chunk),[1,1,1,2]);
assert.equal(api.failedAttemptText({chunk:1,error:"超时"},true),"✗ 块 1 · 历史失败（当前正在继续）：超时");
assert.equal(api.failedAttemptText({chunk:1,error:"超时"},false),"✗ 块 1 · 失败：超时");
const S=(o)=>Object.assign({coverage:{total:0,processed:0,pending:0},revision:0,warnings:[],running:false,
  candidate:null,changes:[],versions:[],document:{entries:[]}},o);
assert.equal(api.stepOf(null).primary.label,"开始整理");
assert.equal(api.stepOf(null).primary.enabled,false);
assert.ok(api.stepOf(null).primary.reason.length>0);
assert.equal(api.stepOf(S()).primary.label,"开始整理");
assert.equal(api.stepOf(S({coverage:{total:5,pending:5,chunks_estimate:2}})).primary.action,"start");
const working=S({coverage:{total:5,processed:2,pending:3,chunks_estimate:2},
  candidate:{status:"working",log:[],document:{entries:[]}}});
assert.equal(api.stepOf(working).primary.label,"继续整理");
assert.equal(api.stepOf(working).primary.action,"resume");
assert.match(api.stepOf(working).primary.reason,/未整理/);
const ready=S({revision:0,candidate:{status:"ready",document:{entries:[]}}});
assert.equal(api.stepOf(ready).primary.label,"确认底稿");
assert.equal(api.stepOf(ready).primary.action,"confirm");
const selectedReady = S({candidate:{mode:"selected",status:"ready",document:{entries:[]}},
  coverage:{mode:"selected",total:1,processed:1,pending:0,history_total:200}});
assert.equal(api.stepOf(selectedReady).primary.action,"confirm");
assert.match(api.coverageText(selectedReady),/本次选中/);
const conflicted=S({candidate:{status:"ready",document:{entries:[{kind:"node",state:"conflict"}]}}});
assert.equal(api.stepOf(conflicted).primary.label,"确认底稿");
assert.equal(api.stepOf(conflicted).primary.enabled,false);
assert.ok(api.stepOf(conflicted).primary.reason.length>0);
assert.equal(api.stepOf(S({revision:1})).primary.label,"生成流程差异");
assert.equal(api.stepOf(S({revision:1})).primary.action,"graph");
assert.equal(api.stepOf(S({revision:1}),{id:"p",revision:1}).primary.label,"保存到画布");
assert.equal(api.stepOf(S({revision:1}),{id:"p",revision:1}).primary.action,"apply");
assert.equal(api.stepOf(S({revision:1}),{id:"p",revision:1,applied_hash:"x"}).primary.label,"生成流程差异");
assert.equal(api.stepOf(S({running:true,progress:{chunk:2}})).primary.label,"整理中…");
assert.equal(api.stepOf(S({running:true,progress:{chunk:2}})).primary.enabled,false);
assert.ok(api.stepOf(S({running:true,progress:{chunk:2}})).primary.reason.length>0);
console.log('orch_drafts.cjs: retained protocol checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
