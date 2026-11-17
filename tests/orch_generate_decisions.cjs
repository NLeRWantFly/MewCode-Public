// Modal-only assertions retired in the T92 behavior map; all pure protocol assertions remain.
const fs=require('node:fs'),assert=require('node:assert/strict');
(async()=>{
const api=await import('../frontend/static/workbench/generation-protocol.js');
const rows = [
  {id:"choice-a",type:"business",title:"是否保留分阶段复核",options:[
    {value:"keep",label:"保留",recommended:true}, {value:"remove",label:"取消"}]},
  {id:"choice-b",type:"business",title:"节点命名方式",options:[{value:"short",label:"简短"}]},
  {id:"unclassified",type:"unclassified",title:"暂不清楚是否属于业务选择",options:[{value:"guess",label:"猜一个"}]},
  {id:"technical",type:"technical",title:"结构校验"},
  {id:"input",type:"input",title:"运行参数"},
  {id:"capability",type:"capability",title:"工具能力"},
];
assert.deepEqual(Array.from(api.businessIssueRows(rows), row => row.id), ["choice-a", "choice-b"],
  "only classified business decisions belong in the choice table");
assert.deepEqual(Array.from(api.choicePayload(rows, new Map([
  ["choice-a", "keep"], ["unclassified", "guess"], ["technical", "anything"],
])), item => ({question_id:item.question_id,value:item.value})), [{question_id:"choice-a",value:"keep"}],
  "partial explicit choices are valid; unclassified/system rows are never submitted as choices");
assert.deepEqual(Array.from(api.choicePayload(rows, new Map())), [], "an empty submission cannot approve or skip items");
assert.deepEqual(Array.from(api.choicePayload(rows, new Map([["choice-a", "unknown"]]))), [],
  "values that are not displayed options cannot be submitted");
assert.deepEqual(Array.from(api.choiceNoteLines(rows, new Map([
  ["choice-a", "保留旧要求，再讨论代价"], ["unclassified", "不要把这项当业务决定"]
])), String), ["是否保留分阶段复核：保留旧要求，再讨论代价"],
  "notes are attached only to business items and remain separate from choice payloads");
console.log('orch_generate_decisions.cjs: retained protocol checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
