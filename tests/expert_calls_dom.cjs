// Isolated hidden renderer: exercises real DOM code, never contacts MewCode.
const {app, BrowserWindow} = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
app.setPath('userData', process.argv[2]);
app.disableHardwareAcceleration();
const source = fs.readFileSync(path.join(root, 'frontend/static/app.js'), 'utf8');
function helper(name) {
  const start = source.search(new RegExp('  (?:async )?function ' + name + '\\('));
  if (start < 0) throw new Error('Missing helper ' + name);
  const end = source.indexOf('\n  }', start);
  if (end < 0) throw new Error('Missing helper end ' + name);
  return source.slice(start, end + 4);
}
const names = ['orchChatStem', 'orchChatKey', 'ensureOrchChatBubble', 'mirrorExpertChatEvent',
  'flushOrchChatText', 'scheduleOrchChatText', 'updateOrchProgress',
  'updateOrchChatActivity', 'renderOrchChatActivity', 'syncExpertClockTimer', 'renderExpertSnapshot',
  'pendingRecoveryCases', 'addUserMsg', 'addAssistantMarkdown',
  'setExpertTimelineFlow', 'expertStageIdentity', 'ensureExpertStageV2', 'renderExpertStageV2', 'appendExpertDeltaV2', 'appendExpertThinkingV2', 'flushExpertTextV2', 'renderExpertUsageV2',
  'ensureExpertTimelineV2', 'compactExpertStatusReason', 'expertSnapshotStatusReason', 'expertRunStatusLabel', 'renderExpertControlsV2', 'loadExpertRoleDetails',
  'tickExpertClocks', 'unionExpertWaitSeconds', 'syncExpertHumanWaits', 'renderExpertRoleWaitV2', 'expertToolWaitState',
  'renderExpertToolV2', 'updateExpertToolClock', 'formatExpertSeconds',
  'shortToolPath', 'toolArgObject', 'toolValueText', 'toolLineSummary', 'buildToolGroup', 'updateGroupTitle',
  'buildToolRow', 'toolTimingLabel', 'renderTool', 'renderToolResult'];
const setup = `
const assert = (condition, message) => {if (!condition) throw new Error(message);};
const messagesEl = document.getElementById('messages');
let currentStatus = {expert_mode: false}, currentMsg = null, currentToolGroup = null, currentExpertTimeline = null;
const toolRows = new Map(), expertTimelines = new Map(), orchChatBubbles = new Map(), orchChatLiveKeys = new Map(), recoveryCases = new Map();
const pendingTurnToolIds = new Set(), notify = () => {};
let expertClockTimer = null;
const expertSnapshotSeq = new Map(), pipelineStates = new Map(), expertTrajectoryTypes = new Set();
const hydrateExpertTrajectory=()=>{}, renderPipeline=()=>{},
  legacyResumeAdviceFromSnapshot=()=>null, saveScopedResumeAdvice=()=>null;
const expertRoleLabels = {manager:'管理', planner:'规划', executor:'执行', auditor:'审查'};
const ORCH_BOARD_STATUS_LABELS = {completed:'完成', needs_attention:'需处理', pending:'待执行'};
const activeConversationId = () => 'c';
const esc = value => String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');
const hideEmptyState=()=>{}, scrollBottom=()=>{}, finalizeThinking=()=>{}, closeMarkdownSegment=()=>{};
let autoScroll=false;
const renderMessageAttachments=(node,attachments)=>{for(const attachment of attachments){const el=document.createElement('span');el.textContent=attachment.name;node.appendChild(el);}};
const renderMarkdown=esc, enhanceMath=()=>{}, applyExpertStep=()=>{}, fmtInt=n=>String(n || 0);
let confirmRetry=false;
const sent=[];
const askConfirm=async()=>confirmRetry, send=msg=>{if(!confirmRetry) throw new Error('A display read tried to run a task'); sent.push(msg);};
const orchBoardTabUserPinned=true, setOrchBoardTab=()=>{};
const renderOrchStageBoard=()=>{};
let requests=0;
const fetch = async () => {requests++; return {ok:true,json:async()=>({conversation_id:'c',items:[
  {type:'expert_tool_result',run_id:'run',node_id:'review',role:'auditor',attempt:15,node_attempt:2,id:'history',name:'ReadAuditArtifact',args:{page:7},output:'历史原页证据',status:'completed'},
  {type:'expert_tool_result',run_id:'run',node_id:'other-review',role:'auditor',attempt:15,node_attempt:2,id:'other-node',name:'ReadFile',output:'other node'},
  {type:'expert_tool_result',run_id:'run',node_id:'review',role:'auditor',attempt:14,node_attempt:2,id:'old-attempt',name:'ReadFile',output:'old attempt'}
]})};};
`;
const assertions = `
const streamBase={run_id:'stream',conversation_id:'c',node_id:'deep',role:'executor',attempt:1,node_attempt:1};
const streamNow=Date.now()/1000;
mirrorExpertChatEvent({...streamBase,type:'expert_node_started',status:'running',started_at:streamNow-20});
const streamBubble=orchChatBubbles.get(orchChatKey(streamBase));
assert(streamBubble.activityEl.textContent.includes('正在执行'), 'Empty live bubble before first model text');
mirrorExpertChatEvent({...streamBase,type:'expert_activity',server_time:streamNow,
  stream_activity:{phase:'processing',updated_at:streamNow-3}});
assert(streamBubble.activityEl.textContent.includes('模型正在处理') && streamBubble.activityEl.textContent.includes('最近活动 3 秒前'), 'Hidden reasoning has no truthful liveness');
assert(!streamBubble.text && streamBubble.thinkEl.classList.contains('hidden'), 'Activity exposed private reasoning');
renderOrchChatActivity(streamBubble,streamNow+7);
assert(streamBubble.activityEl.textContent.includes('最近活动 10 秒前'), 'Silent connection pretends to have fresh data');
syncExpertClockTimer();
assert(expertClockTimer !== null, 'Detached legacy timeline prevented chat clock');
mirrorExpertChatEvent({...streamBase,type:'expert_delta',text:'正文一'});
mirrorExpertChatEvent({...streamBase,type:'expert_delta',text:'正文二'});
await new Promise(resolve => setTimeout(resolve, 80));
assert(streamBubble.bodyEl.textContent==='正文一正文二', 'Live answer stopped streaming');
mirrorExpertChatEvent({...streamBase,type:'expert_stage',history:true,snapshot_aggregate:true,status:'running',started_at:streamNow-20,
  stream_activity:{phase:'processing',updated_at:streamNow-8}});
mirrorExpertChatEvent({...streamBase,type:'expert_delta',history:true,snapshot_aggregate:true,text:'正文一正文二'});
assert(streamBubble.activity.phase==='processing' && streamBubble.bodyEl.textContent==='正文一正文二','Reconnect lost activity or duplicated answer');
mirrorExpertChatEvent({...streamBase,type:'expert_tool_result',id:'stream-tool',name:'ReadFile',status:'completed',output:'ok'});
assert(streamBubble.activity.active && streamBubble.msg.dataset.status==='running','Tool result ended node streaming');
const streamSnapshot={run_id:'stream',conversation_id:'c',attempt:1,status:'running',
  locked_flow:{nodes:[{node_id:'deep',role:'executor',label:'Deep model'}],edges:[]},
  node_states:{deep:{status:'running',role:'executor',attempt:1,node_attempt:1,partial_text:'正文一正文二',started_at:streamNow-20,
    stream_activity:{phase:'processing',updated_at:streamNow-8}}},
  events:[{...streamBase,type:'expert_stage',snapshot_aggregate:true,status:'running',started_at:streamNow-20,
    stream_activity:{phase:'processing',updated_at:streamNow-8}}]};
renderExpertSnapshot(streamSnapshot);
assert(streamBubble.activity.phase==='processing' && streamBubble.activity.updatedAt===streamNow-8,
  'Full snapshot fallback reset the restored live phase');
renderExpertSnapshot({...streamSnapshot,status:'blocked'});
assert(!streamBubble.activity.active && streamBubble.activityEl.textContent==='等待处理',
  'Stopped snapshot with running child kept a live indicator');
renderExpertSnapshot(streamSnapshot);
mirrorExpertChatEvent({...streamBase,type:'expert_run_state',status:'paused'});
assert(streamBubble.activityEl.textContent==='已暂停' && streamBubble.activityEl.dataset.streaming==='false','Pause left a live spinner');
mirrorExpertChatEvent({...streamBase,type:'expert_delta',text:'尾部',history:true,snapshot_aggregate:true});
assert(!streamBubble.activity.active, 'Historical output restarted stopped activity');
for(const status of ['stalled','incomplete_audit','future_stop_state']) {
  mirrorExpertChatEvent({...streamBase,type:'expert_stage',status:'running'});
  mirrorExpertChatEvent({...streamBase,type:'expert_node_completed',status});
  mirrorExpertChatEvent({...streamBase,type:'expert_activity',stream_activity:{phase:'processing',updated_at:streamNow}});
  assert(!streamBubble.activity.active && streamBubble.activityEl.dataset.streaming==='false',
    'Finished node restarted activity for '+status);
}
syncExpertClockTimer();
assert(expertClockTimer===null,'Stopped conversation timer leaked');
streamBubble.msg.remove();
orchChatBubbles.clear(); toolRows.clear();
expertTimelines.clear();
const state = ensureExpertTimelineV2('run');
assert(!state.msg.isConnected && !messagesEl.querySelector('.expert-message'), 'Legacy execution card is still in chat');
assert(ensureExpertTimelineV2('run')===state, 'Detached run state is not reused');
assert(addUserMsg('   ')===null && messagesEl.children.length===0, 'Empty user ghost bubble');
addAssistantMarkdown('  ');
assert(messagesEl.children.length===0, 'Empty assistant ghost bubble');
const attachmentOnly=addUserMsg('',[{name:'evidence.png'}]);
assert(attachmentOnly.textContent==='evidence.png', 'Attachment-only message lost');
attachmentOnly.remove();
assert(state.cards.size===0, 'Four role placeholders were created');
setExpertTimelineFlow(state,{label:'自定义阶段图',nodes:[{node_id:'review',label:'图像一致性检查',role:'auditor'},{node_id:'edit-a',label:'修改实验',role:'executor'},{node_id:'edit-b',label:'修改图注',role:'executor'}]});
renderExpertControlsV2('blocked','run','编码异常，审查未完成',14);
assert(state.timeline.querySelector('.expert-run-status').textContent.includes('未在执行'), 'Blocked looks running');
assert(state.timeline.querySelector('.expert-run-reason').textContent.includes('编码异常'), 'Missing stop reason');
const reportReason="I will scan the PDFs first.\\n\\n## 结论\\n"+'节点完整报告 '.repeat(80);
renderExpertControlsV2('blocked','run',reportReason,14);
assert(state.statusReason.length<=220 && !state.statusReason.includes('\\n') && state.statusReason.endsWith('…'), 'Blocked banner kept the full node report');
const restoredReason=expertSnapshotStatusReason({status:'blocked',status_reason:reportReason,stage_cursor:'probe',node_states:{probe:{status:'needs_attention',handoff:{question:'请贴回只读探测命令的输出。'}}}});
assert(restoredReason==='等待用户输入：请贴回只读探测命令的输出。','Restored snapshot kept the stale full-report reason');
renderExpertControlsV2('running','run','',15);
assert(state.timeline.querySelector('.expert-run-reason').classList.contains('hidden'), 'Old stop reason survived resume');
renderExpertControlsV2('blocked','run','旧错误',14);
assert(state.status==='running' && !state.statusReason, 'Old attempt overwrote current running status');
const base={run_id:'run',conversation_id:'c',node_id:'review',role:'auditor',attempt:15,node_attempt:2,id:'live',name:'ReadAuditArtifact',args:{page:8}};
const rowKey=id=>orchChatStem(base)+'::2::'+id;
renderExpertStageV2({...base,type:'expert_stage',status:'running',model:'new-model'});
const reviewKey=expertStageIdentity(state,base).key;
assert(state.cards.get(reviewKey).querySelector('header strong').textContent==='图像一致性检查','Node title replaced by role');
assert(state.cards.get(reviewKey).querySelector('summary').textContent.includes('调用详情'),'No detail entry');
renderExpertUsageV2({...base,input_tokens:42});
assert(state.cards.get(reviewKey).querySelector('.expert-stage-meta').textContent.includes('new-model'),'Usage overwrote model metadata');
mirrorExpertChatEvent({...base,type:'expert_tool'});
assert(toolRows.has(rowKey('live')), 'Live tool dropped when expert_mode is false');
mirrorExpertChatEvent({...base,type:'expert_tool_wait',status:'waiting_permission'});
assert(toolRows.get(rowKey('live')).querySelector('.tool-elapsed').textContent==='等待授权', 'Permission wait hidden');
for (const kind of ['human_oncall', 'ask_user']) {
  const question = {...base, id: kind, name: kind === 'ask_user' ? 'AskUserQuestion' : kind,
    type:'expert_tool', status:'queued', declared_at:100};
  renderExpertToolV2(question); mirrorExpertChatEvent(question);
  const wait = {...base, id:undefined, type:'expert_role_wait', wait_id:kind, kind,
    started_at:105, active:true};
  renderExpertRoleWaitV2(wait);
  const toolRow = state.tools.get(reviewKey+'::'+kind);
  assert(toolRows.get(rowKey(kind)).querySelector('.tool-elapsed').textContent==='等待你的回答', 'Human wait remains queued');
  assert(toolRow.querySelector('.expert-tool-state').textContent==='等待你的回答', 'Detail still says queued');
  updateExpertToolClock(toolRow,200);
  assert(toolRow.querySelector('.expert-tool-times').textContent.includes('排队 5.00 秒')
    && toolRow.querySelector('.expert-tool-times').textContent.includes('等待 95.00 秒'),
    'Human wait counted as queue time: '+toolRow.querySelector('.expert-tool-times').textContent);
  // A late historical queued event must not replace the current live wait.
  renderExpertToolV2({...question,history:true}); mirrorExpertChatEvent({...question,history:true}, {force:true});
  assert(toolRows.get(rowKey(kind)).querySelector('.tool-elapsed').textContent==='等待你的回答', 'History overwrote live wait');
  renderExpertRoleWaitV2({...wait,active:false,completed_at:205});
  const reply = {...question,type:'expert_tool_result',status:'completed',completed_at:206,
    started_at:205,elapsed:1,wait_seconds:0,output:'已回答'};
  renderExpertToolV2(reply,'result'); mirrorExpertChatEvent(reply);
  assert(toolRow.querySelector('.expert-tool-state').textContent==='完成', 'Answer left tool waiting');
  assert(Number(toolRow.dataset.waitSeconds)===100, 'Answer lost measured human wait');
  toolRows.get(rowKey(kind)).remove(); toolRows.delete(rowKey(kind));
  toolRow.remove(); state.tools.delete(reviewKey+'::'+kind);
}
// Reconnect with no wait events: derive the display from the saved original
// request and its open interval; no answer or tool execution is synthesized.
const restoredQuestion = {...base,id:'reconnected',name:'human_oncall',type:'expert_tool',status:'queued',declared_at:300};
renderExpertToolV2(restoredQuestion); mirrorExpertChatEvent(restoredQuestion);
syncExpertHumanWaits(state,{run_id:'run',conversation_id:'c',attempt:15,status:'running',node_states:{review:{
  role:'auditor',attempt:15,node_attempt:2,status:'running',pending_interaction:{request_id:'reconnected'},
  wait_intervals:[{tool_id:'reconnected',kind:'human_oncall',start:305,end:null}]}}});
assert(toolRows.get(rowKey('reconnected')).querySelector('.tool-elapsed').textContent==='等待你的回答', 'Reconnect lost current human wait');
const restoredToolRow=state.tools.get(reviewKey+'::reconnected');
updateExpertToolClock(restoredToolRow,400);
assert(restoredToolRow.querySelector('.expert-tool-times').textContent.includes('排队 5.00 秒'), 'Reconnect grew fake queue time');
renderExpertRoleWaitV2({...base,type:'expert_role_wait',wait_id:'host-question',kind:'ask_user',started_at:405,active:true});
assert(!toolRows.has(rowKey('host-question')), 'Host question invented a tool call');
toolRows.get(rowKey('reconnected')).remove(); toolRows.delete(rowKey('reconnected'));
restoredToolRow.remove(); state.tools.delete(reviewKey+'::reconnected');
mirrorExpertChatEvent({...base,type:'expert_tool_start',status:'running'});
assert(toolRows.get(rowKey('live')).querySelector('.tool-elapsed').textContent==='执行中', 'Tool start not shown');
mirrorExpertChatEvent({...base,type:'expert_tool_state',status:'outcome_unknown'});
assert(toolRows.get(rowKey('live')).querySelector('.tool-elapsed').textContent==='结果未知', 'Interrupted looks active');
const result={...base,type:'expert_tool_result',output:'工具结果',status:'completed'};
mirrorExpertChatEvent(result); mirrorExpertChatEvent(result);
assert(toolRows.get(rowKey('live')).querySelector('.tool-body').textContent.split('工具结果').length===2, 'Repeated result or late result lost');
mirrorExpertChatEvent({...base,type:'expert_tool',conversation_id:'other',id:'foreign'});
assert(!toolRows.has('foreign'), 'Foreign conversation output leaked');
messagesEl.querySelector('.orch-chat-details').click();
await new Promise(resolve=>setTimeout(resolve,0));
await loadExpertRoleDetails(state,reviewKey);
assert(requests===1, 'Repeated detail request: '+state.timeline.querySelector('.expert-detail-error')?.textContent);
assert(toolRows.has(rowKey('history')), 'Historical tool only rendered into legacy timeline');
assert(messagesEl.querySelector('.orch-stage-msg .tool-group'), 'No tool group in conversation');
assert(state.cards.get(reviewKey).querySelector('.expert-tool-note'), 'No timeline details');
assert(!toolRows.has('other-node') && !toolRows.has('old-attempt'),'Node/attempt detail filtering leaked');
for(const [id,text] of [['edit-a','实验节点的输出'],['edit-b','图注节点的输出']]){
  const event={run_id:'run',node_id:id,role:'executor',attempt:15,node_attempt:1,status:'running'};
  renderExpertStageV2(event); appendExpertDeltaV2({...event,text});
  const key=expertStageIdentity(state,event).key; flushExpertTextV2(state,key);
  assert(state.cards.get(key).querySelector('.expert-stage-body').textContent===text,'Same-role node text mixed');
}
renderExpertStageV2({...base,attempt:14,status:'needs_attention',history:true});
assert(state.attemptGroups.get(14).open===false && state.attemptGroups.get(15).open===true,'History/current attempts not separated');
assert(state.cards.size===4,'Unexpected phantom role cards');
assert(!Array.from(state.cards.values()).some(c=>c.dataset.role==='planner'),'Unused planning role displayed');
renderExpertControlsV2('blocked','run','证据仍不足',15);
assert(!state.timeline.querySelector('.expert-stage.is-running'),'Blocked task still has running card');
const groupCount=messagesEl.querySelectorAll('.tg-row').length;
assert(groupCount===2, 'Tool rows duplicated');
renderExpertStageV2({...base,attempt:15,status:'skipped',reason:'branch_not_taken',history:true});
assert(state.timeline.textContent.includes('未走此分支'),'Branch skip mislabeled as completed');
renderExpertStageV2({...base,attempt:15,status:'skipped',reason:'checkpoint_completed',history:true});
assert(state.timeline.textContent.includes('已完成 · 本轮复用'),'Checkpoint reuse label missing');
renderExpertStageV2({...base,attempt:15,status:'needs_attention',history:true});
assert(state.timeline.textContent.includes('验收需处理'),'Audit result mislabeled as execution failure');
renderExpertStageV2({...base,attempt:15,status:'failed',history:true});
assert(state.timeline.textContent.includes('执行失败'),'Execution error label missing');
const stopped = {run_id:'stopped-clock',node_id:'inventory',role:'executor',attempt:1,node_attempt:1,history:true};
renderExpertStageV2({...stopped,status:'running',started_at:100,model:'original-model'});
renderExpertStageV2({...stopped,status:'paused',completed_at:111,duration_ms:11000,active_seconds:0,wait_seconds:11});
renderExpertControlsV2('terminated',stopped.run_id,'用户终止',1);
const stoppedState=expertTimelines.get(stopped.run_id);
const stoppedCard=stoppedState.cards.get(expertStageIdentity(stoppedState,stopped).key);
tickExpertClocks(); tickExpertClocks();
assert(stoppedCard.querySelector('.expert-clock').textContent==='11.00 秒','Stopped clock counts until now');
assert(stoppedCard.querySelector('.expert-stage-status').textContent==='已终止','Terminated node still says paused');
assert(stoppedCard.querySelector('.expert-stage-meta').textContent.includes('original-model'),'Completion erased model');
assert(stoppedCard.querySelector('.expert-stage-stats').textContent.includes('实际工作 0.00 秒'),'Zero active time became wall time');
renderExpertStageV2({...stopped,node_id:'unknown',status:'paused',started_at:100});
const unknown=stoppedState.cards.get(expertStageIdentity(stoppedState,{...stopped,node_id:'unknown'}).key);
assert(!unknown.querySelector('.expert-clock').dataset.start,'Incomplete stopped clock is still ticking');
assert(!messagesEl.querySelector('.expert-message'), 'Legacy card reappeared after replay');
assert(messagesEl.querySelector('.orch-stage-msg .msg-bubble .orch-stage-msg-head'), 'Node identity detached from bubble');
// A snapshot contains both a stage's full text and its aggregate delta. They
// are two views of one source, not two model responses (N01 assistance replay).
const helperEvent={run_id:'repeat-replay',conversation_id:'c',node_id:'N01__assist_994b695fda6e',
  role:'planner',attempt:1,node_attempt:1};
const authored='我将只核对宿主回执。\\ncommand_started=false，命令未启动。';
const complete=authored+'\\n\\n本节点交接声明未完成：AUTO_DENIED，原阻碍尚未解除。';
const restored={...helperEvent,history:true,snapshot_aggregate:true};
mirrorExpertChatEvent({...restored,type:'expert_stage',status:'needs_attention',text:complete});
mirrorExpertChatEvent({...restored,type:'expert_delta',text:complete});
const helperBubble=orchChatBubbles.get(orchChatKey(helperEvent));
assert(helperBubble.text===complete,'Stage snapshot and aggregate delta duplicated the full response');
for(let i=0;i<3;i++){
  mirrorExpertChatEvent({...restored,type:'expert_stage',status:'needs_attention',text:complete});
  mirrorExpertChatEvent({...restored,type:'expert_delta',text:complete});
  mirrorExpertChatEvent({...restored,type:'expert_node_completed',status:'needs_attention',text:complete});
}
assert(helperBubble.bodyEl.textContent===complete,'Repeated snapshot replay grew the response');
assert(helperBubble.msg.dataset.status==='needs_attention','Display repair marked denied work complete');
mirrorExpertChatEvent({...restored,type:'expert_thinking',text:'核对宿主回执'});
mirrorExpertChatEvent({...restored,type:'expert_thinking',text:'核对宿主回执'});
assert(helperBubble.thinkEl.textContent==='核对宿主回执','Snapshot reasoning duplicated');
mirrorExpertChatEvent({...helperEvent,node_attempt:2,type:'expert_node_started',status:'running'});
mirrorExpertChatEvent({...helperEvent,node_attempt:2,type:'expert_delta',text:'新的节点调用'});
mirrorExpertChatEvent({...restored,type:'expert_stage',status:'needs_attention',text:complete});
assert(orchChatBubbles.get(orchChatKey({...helperEvent,node_attempt:2})).text==='新的节点调用',
  'Older call snapshot overwrote the new node call');
const liveHelper={...helperEvent,node_id:'live-helper'};
mirrorExpertChatEvent({...liveHelper,type:'expert_node_started',status:'running'});
mirrorExpertChatEvent({...liveHelper,type:'expert_delta',text:authored});
mirrorExpertChatEvent({...liveHelper,type:'expert_node_completed',status:'needs_attention',text:complete});
const liveBubble=orchChatBubbles.get(orchChatKey(liveHelper));
assert(liveBubble.bodyEl.textContent===complete,'Final host diagnostic was lost behind streamed text');
renderExpertStageV2({...liveHelper,type:'expert_stage',status:'running'});
appendExpertDeltaV2({...liveHelper,type:'expert_delta',text:authored});
renderExpertStageV2({...liveHelper,type:'expert_node_completed',status:'needs_attention',text:complete});
const liveState=expertTimelines.get(liveHelper.run_id);
const liveKey=expertStageIdentity(liveState,liveHelper).key;
flushExpertTextV2(liveState,liveKey);
assert(liveState.cards.get(liveKey).querySelector('.expert-stage-body').textContent===complete,
  'Call detail text lost the final diagnostic');
for(let i=0;i<3;i++){
  renderExpertStageV2({...liveHelper,type:'expert_stage',status:'needs_attention',text:complete,snapshot_aggregate:true});
  appendExpertDeltaV2({...liveHelper,type:'expert_delta',text:complete,snapshot_aggregate:true});
  appendExpertThinkingV2({...liveHelper,type:'expert_thinking',text:'读取原回执',snapshot_aggregate:true});
}
flushExpertTextV2(liveState,liveKey);
assert(liveState.cards.get(liveKey).querySelector('.expert-stage-body').textContent===complete,
  'Repeated call detail snapshots duplicated text');
assert(liveState.cards.get(liveKey).querySelector('.expert-thinking pre').textContent==='读取原回执',
  'Repeated call detail snapshots duplicated reasoning');
// Legitimately repeated deltas stay intact. Do not deduplicate by prose.
const repeated={...helperEvent,node_id:'actual-repeated-text'};
mirrorExpertChatEvent({...repeated,type:'expert_delta',text:'检查。'});
mirrorExpertChatEvent({...repeated,type:'expert_delta',text:'检查。'});
assert(orchChatBubbles.get(orchChatKey(repeated)).text==='检查。检查。','Real model output was rewritten');
const progressNode={...helperEvent,node_id:'observable-work'};
mirrorExpertChatEvent({...progressNode,type:'expert_step',step_id:'check',label:'核对输入',status:'running',summary:'已读取两个输入文件',next_step:'运行一致性检查'});
const progressBubble=orchChatBubbles.get(orchChatKey(progressNode));
assert(progressBubble.progressEl.textContent.includes('已读取两个输入文件'),'Public work summary is missing');
assert(progressBubble.progressEl.textContent.includes('下一步：运行一致性检查'),'Next action is missing');
mirrorExpertChatEvent({...progressNode,type:'expert_tool',id:'read-1',name:'ReadFile',arguments:{path:'input.txt'}});
mirrorExpertChatEvent({...progressNode,type:'expert_tool_start',id:'read-1',name:'ReadFile'});
mirrorExpertChatEvent({...progressNode,type:'expert_tool_result',id:'read-1',name:'ReadFile',is_error:false});
assert(progressBubble.progress.size===2,'One tool produced duplicate progress rows');
assert(progressBubble.progress.get('tool:read-1').status==='completed','Tool completion not reflected');
assert(progressBubble.progress.get('tool:read-1').label.includes('input.txt'),'Tool completion lost its target');
mirrorExpertChatEvent({...progressNode,type:'expert_step',step_id:'check',label:'核对输入',status:'completed',verification:'一致性检查通过'});
assert(progressBubble.progress.size===2 && progressBubble.progressEl.textContent.includes('一致性检查通过'),'Step update was not merged');
return {legacyCardRemoved:true,liveWithoutMode:true,historyVisible:true,blockedClearly:true,toolRows:groupCount,nodeScoped:true,attemptScoped:true,publicProgress:true};
`;
app.whenReady().then(async () => {
  const win = new BrowserWindow({show:false, width:1100, height:850, webPreferences:{nodeIntegration:false,contextIsolation:true}});
  win.webContents.on('console-message', event => {if(event.level==='error') process.stderr.write('RENDERER '+event.message+'\n');});
  win.webContents.session.webRequest.onBeforeRequest((details, callback) => callback({cancel:!details.url.startsWith('data:') && details.url!=='about:blank'}));
  try {
    await win.loadURL('data:text/html;charset=utf-8,'+encodeURIComponent('<style>.hidden{display:none!important}</style><main id="messages"></main>'));
    const script = '(async()=>{'+setup+'\n'+names.map(helper).join('\n')+'\n'+assertions+'})()';
    const syntax = await win.webContents.executeJavaScript('(()=>{try{new Function('+JSON.stringify(script)+');return ""}catch(e){return String(e.stack)}})()');
    if (syntax) throw new Error(syntax);
    const result = await win.webContents.executeJavaScript(script+'.catch(error => ({error:String(error.stack || error)}))');
    if (result.error) throw new Error(result.error);
    process.stdout.write('DOM_RESULT '+JSON.stringify(result)+'\n');
    win.destroy(); app.exit(0);
  } catch (error) {
    process.stderr.write(String(error.stack || error)+'\n');
    win.destroy(); app.exit(1);
  }
});
