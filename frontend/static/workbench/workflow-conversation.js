import {node,button} from './dom.js';
import {artifactLink} from './workflow-node.js?v=20261117-exploration';
import {renderWorkflowBlocks} from './workflow-blocks.js';

const STATUS={running:'进行中',suspended:'等待接续',queued:'已排队',waiting:'等待回复',completed:'已完成',
  blocked:'已阻塞',failed:'执行失败',needs_input:'等待处理',paused:'已暂停',unknown:'待核对',cancelled:'已取消'};
const KINDS={ask:'追问',revise:'补交',update:'局部更新'};
const STOPPED='模型对话已结束，节点尚未交付。';
const stamp=value=>typeof value==='number'?value*1000:Date.parse(value)||0;
const clock=value=>stamp(value)?new Date(stamp(value)).toLocaleTimeString('zh-CN',{hour12:false}):'';
const pretty=value=>typeof value==='string'?value:JSON.stringify(value,null,2);
function waitRequest(detail,attempt){
  if(attempt?.waiting_request)return attempt.waiting_request;
  let identity=attempt?.waiting_on;
  if(!identity&&attempt?.status==='suspended'){
    const last=(attempt.diagnostics||[]).filter(row=>row.name==='WaitForNodeReply').at(-1);
    try{const value=JSON.parse(last?.output||'{}');if(value.status==='waiting')identity=value.id;}catch{}
  }
  return detail?.requests?.find(row=>row.id===identity);
}
export function conversationState(detail,attempt,view){
  if(!attempt)return {status:'pending',title:'尚未开始',reason:'此节点还没有执行记录。'};
  const waiting=waitRequest(detail,attempt),status=attempt.display_status||attempt.status;
  // Older servers incorrectly label an intentionally yielded attempt as ended.
  // A running workflow and a saved wait checkpoint distinguish that case.
  const normalWait=attempt.status==='suspended'&&view.task.execution_status==='running'&&
    !view.task.runtime_fault&&(!attempt.activity_reason||attempt.activity_reason===STOPPED);
  if(normalWait||attempt.status==='suspended'&&!attempt.display_status){
    if(attempt.waiting_tasks?.length)return {status:'waiting',title:'等待工作项结果',
      reason:`已保存上下文并释放执行名额，等待 ${attempt.waiting_tasks.length} 个工作项的实际结果后自动接续。`};
    const pending=waiting&&!['completed','failed','cancelled'].includes(waiting.status);
    const target=waiting?.label||view.structure?.nodes?.find(row=>row.id===waiting?.node_id)?.label||waiting?.node_id;
    return {status:'waiting',title:pending?'正在等待节点回复':'等待接续',waiting,
      reason:pending?`正在等待「${target}」。上下文已保存，收到回复后自动继续。`:'上下文已保存，由调度器接续当前对话。'};
  }
  const reason=attempt.activity_reason||attempt.result?.reason;
  if(['blocked','failed','unknown'].includes(status))return {status,title:STATUS[status],
    reason:reason||'执行尚未交付。当前记录没有更具体的中断原因，请查看最后的工具返回。'};
  if(detail.execution_status==='paused'&&['running','suspended'].includes(status)||status==='paused')return {status:'paused',title:'已暂停',reason:reason||'执行记录和已有产物已保存，可继续当前节点。'};
  if(detail.execution_status==='terminated'&&['running','suspended'].includes(status))return {status:'paused',title:'运行已停止',reason:'本轮运行已停止，执行记录和已有产物已保存。'};
  if(status==='queued')return {status,title:'请求已排队',reason:'节点会在当前对话让出执行名额后处理这条消息。'};
  return {status,title:STATUS[status]||status,reason:reason||
    (status==='completed'?(attempt.request_id?'这次回复已完成。':'本次执行已完成，交付与历史记录已保留。'):
      status==='needs_input'?'等待所需输入或人工处理。':attempt.execution?.text||'节点正在执行。')};
}

export function conversationEvents(detail,attempt){
  if(!attempt)return [];
  const events=[],tools=attempt.tools||{},diagnostics=attempt.diagnostics||[];
  const results=new Map(diagnostics.filter(row=>row.kind==='tool_result').map(row=>[row.tool_id,row]));
  for(const [index,row] of (attempt.progress||[]).entries())events.push({id:'p'+index,type:'message',at:row.at,text:row.text});
  const phases=attempt.phase_events||[];
  for(const [index,row] of phases.entries())if(row.phase==='reasoning')events.push({id:'phase'+index,type:'thinking',at:row.at,
    end:phases[index+1]?.at,text:'模型思考',summary:row.summary||''});
  for(const [identity,result] of results){
    const tool=tools[identity]||{};
    events.push({id:'tool'+identity,type:'tool',at:tool.started_at||tool.queued_at||result.at,
      name:result.name||tool.name,arguments:tool.arguments,output:result.output,
      status:tool.status|| (result.is_error?'failed':'completed'),end:tool.ended_at||result.at});
  }
  for(const [identity,tool] of Object.entries(tools))if(!results.has(identity))events.push({id:'tool'+identity,
    type:'tool',at:tool.started_at||tool.queued_at||attempt.started_at,name:tool.name,arguments:tool.arguments,
    status:tool.status,end:tool.ended_at});
  for(const [index,row] of diagnostics.entries())if(row.kind!=='tool_result')events.push({id:'diagnostic'+index,type:'diagnostic',
    at:row.at,name:row.name||row.kind,text:row.reason||row.output||row.text||'',status:'failed'});
  const request=detail.requests?.find(row=>row.id===attempt.request_id);
  if(request)events.push({id:'request'+request.id,type:'request',at:request.created_at||attempt.started_at,request});
  for(const row of detail.requests||[])if(row.sender_attempt_id===attempt.id)events.push({id:'sent'+row.id,type:'request',
    at:row.created_at,request:row,outgoing:true});
  if(attempt.result?.text&&attempt.result.text!==attempt.progress?.at(-1)?.text)events.push({id:'final',type:'message',
    at:attempt.ended_at||attempt.started_at,text:attempt.result.text,final:true});
  return events.sort((a,b)=>stamp(a.at)-stamp(b.at));
}

export function openWorkflowConversation({root,api,store,view,item,onOpenNode,onClose}){
  const owner=store.generation,scope={...store.state.scope},task={...view.task};
  let currentView=view,snapshot=null,selected='',selectedByUser=false,disposed=false,loading=false,sending=false,
    snapshotKey='',pendingMessage=null,controller=null,expanded=new Set();
  const dialog=node('dialog','wb-conversation-modal');dialog.setAttribute('aria-label',item.label+' · 节点对话');
  const header=node('header','wb-conversation-header'),heading=node('div'),title=node('h2','',item.label),badges=node('div','wb-conversation-badges');
  heading.append(title,node('p','wb-muted','节点对话'));const fullscreen=button('⛶',()=>{dialog.classList.toggle('is-fullscreen');},'wb-button wb-conversation-icon');
  fullscreen.setAttribute('aria-label','切换对话框全屏');
  const close=button('×',destroy,'wb-button wb-conversation-icon');close.setAttribute('aria-label','关闭节点对话');
  header.append(heading,badges,fullscreen,close);
  const body=node('div','wb-conversation-body'),sidebar=node('aside','wb-conversation-sidebar'),history=node('nav','wb-conversation-history');
  history.setAttribute('aria-label','执行记录');const files=node('div','wb-conversation-files');
  sidebar.append(node('h3','','执行记录'),history,files);
  const main=node('section','wb-conversation-main'),attemptTitle=node('h3'),banner=node('div','wb-conversation-banner');
  banner.setAttribute('role','status');const log=node('div','wb-conversation-log');log.setAttribute('aria-label','节点对话内容');log.tabIndex=0;
  const notice=node('p','wb-conversation-notice');notice.setAttribute('role','status');
  const composer=node('form','wb-conversation-composer'),input=node('textarea','wb-input');
  input.placeholder='给这个节点补充说明或提问…';input.setAttribute('aria-label','给此节点的消息');input.rows=2;
  const actions=node('div','wb-conversation-compose-actions'),kind=node('select','wb-input');kind.setAttribute('aria-label','消息类型');
  for(const [value,label]of [['ask','追问'],['revise','要求补交']]){const option=node('option','',label);option.value=value;kind.append(option);}
  const versions=node('select','wb-input');versions.setAttribute('aria-label','消息依据的交付版本');
  const send=button('发送',()=>sendMessage(),'wb-button wb-primary');
  const latest=button('↓ 回到最新',()=>{log.scrollTop=log.scrollHeight;},'wb-button wb-conversation-latest');
  const reasonToggle=button('状态与原因',()=>{reasons.classList.toggle('is-visible');},'wb-button wb-conversation-reason-toggle');
  actions.append(kind,versions,node('span','wb-muted','消息发送到当前节点'),send);composer.append(input,actions);
  composer.addEventListener('submit',event=>{event.preventDefault();void sendMessage();});
  input.addEventListener('keydown',event=>{if((event.ctrlKey||event.metaKey)&&event.key==='Enter'){event.preventDefault();void sendMessage();}});
  const navigation=node('div','wb-conversation-log-navigation');navigation.append(latest,reasonToggle);
  main.append(attemptTitle,banner,log,navigation,notice,composer);
  const reasons=node('aside','wb-conversation-reasons');body.append(sidebar,main,reasons);dialog.append(header,body);
  (root.closest('#task-workbench')||root).append(dialog);
  dialog.addEventListener('cancel',event=>{event.preventDefault();destroy();});
  dialog.addEventListener('close',destroy);
  if(dialog.showModal)dialog.showModal();else dialog.setAttribute('open','');
  close.focus();
  function active(){return !disposed&&owner===store.generation;}
  function selectedAttempt(){
    if(selected.startsWith('request:')){const request=snapshot?.requests?.find(r=>r.id===selected.slice(8));
      return request?{id:selected,request_id:request.id,status:'queued',progress:[],started_at:request.created_at}:null;}
    return snapshot?.attempts?.find(row=>row.id===selected);
  }
  function attemptName(attempt){
    const request=snapshot?.requests?.find(r=>r.id===attempt.request_id);
    return request?`${KINDS[request.kind]||'对话'} · ${clock(request.created_at||attempt.started_at)}`:
      attempt.number===1?'首次执行':`第 ${attempt.number||1} 次执行`;
  }
  function chooseDefault(){
    if(selectedByUser&&(snapshot.attempts.some(row=>row.id===selected)||snapshot.requests?.some(row=>'request:'+row.id===selected)))return;
    const busy=snapshot.attempts.filter(row=>['running','suspended','blocked','needs_input'].includes(row.status))
      .sort((a,b)=>stamp(b.started_at)-stamp(a.started_at));
    const queued=snapshot.requests?.filter(row=>row.node_id===item.id&&row.status==='queued').at(-1);
    selected=busy[0]?.id||(queued?'request:'+queued.id:'')||snapshot.current_attempt_id||snapshot.attempts.at(-1)?.id||'';
  }
  function disclosure(identity,label,content,{open=false,className=''}={}){
    const group=node('details','wb-conversation-disclosure '+className);group.dataset.eventId=identity;
    group.open=expanded.has(identity)||open;group.append(node('summary','',label),content);return group;
  }
  function pre(value){const el=node('pre','wb-conversation-code');el.textContent=pretty(value)||'';return el;}
  function toolCard(event){
    const content=node('div','wb-conversation-tool-content');
    content.append(node('h4','','调用参数'),event.arguments===undefined?node('p','wb-muted','此历史记录未保存调用参数。'):pre(event.arguments));
    content.append(node('h4','','返回结果'),event.output===undefined?node('p','wb-muted',event.status==='running'?'工具正在执行…':'尚未记录返回结果。'):pre(event.output));
    const status={completed:'成功',failed:'返回错误',unknown:'结果待核对',queued:'准备调用',running:'执行中'}[event.status]||event.status;
    const duration=stamp(event.end)>stamp(event.at)?` · ${((stamp(event.end)-stamp(event.at))/1000).toFixed(1)} 秒`:'';
    const group=disclosure(event.id,`${event.name||'工具调用'} · ${status}${duration}`,content,{className:'wb-conversation-tool'});
    group.dataset.status=event.status;return group;
  }
  function renderLog(attempt,changedSelection=false){
    const follow=changedSelection||log.scrollHeight-log.scrollTop-log.clientHeight<70,position=log.scrollTop;
    expanded=changedSelection?new Set():new Set([...log.querySelectorAll('details')].filter(el=>el.open).map(el=>el.dataset.eventId));
    log.replaceChildren();
    const blocks=attempt?.id===snapshot.current_attempt_id?snapshot.presentation_blocks||[]:
      Object.values(attempt?.presentation_blocks||{});
    if(blocks.length){const visual=node('div');renderWorkflowBlocks(visual,blocks,{scope,task});log.append(visual);}
    for(const event of conversationEvents(snapshot,attempt)){
      const article=node('article','wb-conversation-event');article.dataset.eventId=event.id;
      if(event.type==='tool')article.append(toolCard(event));
      else if(event.type==='thinking'){
        const duration=event.end?` · ${Math.max(0,(stamp(event.end)-stamp(event.at))/1000).toFixed(1)} 秒`:'';
        article.append(disclosure(event.id,'思考'+duration,node('p','wb-muted',event.summary||'此段记录了思考状态；公开进展见下方节点回复。')));
      }else if(event.type==='request'){
        const request=event.request,source=request.sender_kind==='user'?'你':nodeLabel(request.sender_node_id)||'节点消息';
        article.append(node('div','wb-conversation-author',`${event.outgoing?'发往 '+nodeLabel(request.node_id):source} · ${KINDS[request.kind]||'消息'} · ${clock(event.at)}`));
        article.append(node('p','wb-conversation-request',request.message||''));
        if(event.outgoing){
          article.append(node('span','wb-muted',STATUS[request.status]||request.status));
          if(request.reply?.text)article.append(node('p','wb-conversation-reply',request.reply.text));
          if(request.node_id!==item.id)article.append(button('查看该节点对话',()=>onOpenNode?.(request.node_id)));
        }
      }else{
        article.append(node('div','wb-conversation-author',`${event.type==='diagnostic'?'执行记录':event.final?'本次交付':item.label} · ${clock(event.at)}`));
        article.append(node('p',event.type==='diagnostic'?'wb-conversation-error':'',event.text||''));
      }
      log.append(article);
    }
    if(!log.children.length)log.append(node('p','wb-muted',attempt?'尚无公开进展，记录到达后会显示在这里。':'此节点还未开始执行。'));
    const execution=attempt?.execution;
    if(execution?.active&&conversationState(snapshot,attempt,currentView).status==='running'){
      const live=node('p','wb-conversation-live',execution.text||'节点正在执行');live.setAttribute('role','status');log.append(live);
    }
    log.scrollTop=follow?log.scrollHeight:position;
  }
  function nodeLabel(identity){return currentView.structure?.nodes?.find(n=>n.id===identity)?.label||identity;}
  function reasonRow(label,value){const row=node('div','wb-conversation-reason-row');row.append(node('dt','',label),node('dd','',value||'—'));return row;}
  function renderReason(attempt,state){
    reasons.replaceChildren(node('h3','',state.status==='running'?'当前执行':'状态与原因'));
    const hide=button('关闭原因详情',()=>reasons.classList.remove('is-visible'),'wb-button wb-conversation-reason-toggle');reasons.append(hide);
    const rows=node('dl');rows.append(reasonRow('当前状态',state.title));
    if(state.waiting){rows.append(reasonRow('等待节点',state.waiting.label||nodeLabel(state.waiting.node_id)),
      reasonRow('接续条件','收到这条请求的回复'));
      const message=node('p','',state.waiting.message||'');rows.append(disclosure('waiting-input','所需输入',message));}
    else rows.append(reasonRow('原因',state.reason));
    rows.append(reasonRow('执行开始',clock(attempt?.started_at)),reasonRow('最后活动',clock(attempt?.last_activity_at||attempt?.ended_at||attempt?.progress?.at(-1)?.at)));
    reasons.append(rows);
    if(state.waiting?.node_id&&state.waiting.node_id!==item.id)reasons.append(button('查看等待节点的对话',()=>onOpenNode?.(state.waiting.node_id),'wb-button wb-primary'));
    if(['blocked','failed','paused','unknown'].includes(state.status)&&!task.readonly){
      const resume=button('继续此节点',async()=>{
        resume.disabled=true;notice.textContent='正在继续…';
        try{const result=await api.post('/api/workbench/run-action',{...scope,run_id:task.run_id,task_id:task.id,
          operation:'resume',node_id:item.id,request_id:crypto.randomUUID()});
          if(active()){store.accept(result.view,owner);store.refresh();await load();}}
        catch(error){if(active())notice.textContent=error.message;}finally{if(active())resume.disabled=false;}
      });reasons.append(resume);
    }
    const error=(attempt?.diagnostics||[]).filter(row=>row.is_error||row.kind==='artifact_error').at(-1);
    if(error){const content=node('div');content.append(node('p','wb-muted','保留此工具的原始返回；单次工具错误不等于整个节点停止。'),pre(error.output||error.reason));
      reasons.append(disclosure('last-tool-error',`最近的工具错误 · ${error.name||error.kind}`,content,{className:'wb-conversation-last-error'}));}
  }
  function render(changedSelection=false){
    if(!snapshot)return;
    const attempt=selectedAttempt(),state=conversationState(snapshot,attempt,currentView);
    attemptTitle.textContent=attempt?attemptName(attempt):'执行记录';
    banner.dataset.status=state.status;banner.replaceChildren(node('strong','',state.title),node('span','',state.reason));
    badges.replaceChildren();const delivery=snapshot.deliveries?.find(row=>row.id===snapshot.published_delivery_id);
    if(delivery)badges.append(node('span','wb-conversation-badge wb-conversation-delivered',`v${delivery.number} 已交付`));
    if(attempt)badges.append(node('span','wb-conversation-badge',attempt.request_id&&state.status==='running'?'正在处理'+(KINDS[snapshot.requests?.find(r=>r.id===attempt.request_id)?.kind]||'追问'):state.title));
    history.replaceChildren();
    const choices=[...snapshot.attempts].reverse();
    for(const request of snapshot.requests||[])if(request.node_id===item.id&&request.status==='queued'&&!request.attempt_id)choices.unshift({id:'request:'+request.id,request_id:request.id,status:'queued',started_at:request.created_at});
    for(const value of choices){const control=button('',()=>{selected=value.id;selectedByUser=true;expanded=new Set();render(true);},'wb-conversation-history-item');
      control.dataset.attemptId=value.id;control.setAttribute('aria-current',String(value.id===selected));
      const info=conversationState(snapshot,value,currentView);control.append(node('strong','',attemptName(value)),node('span','',info.title));history.append(control);}
    files.replaceChildren(node('h3','',delivery?`交付文件 · v${delivery.number}`:'当前产出'));
    const artifacts=delivery?.result?.artifacts||attempt?.artifacts||[];
    for(const artifact of artifacts)files.append(artifactLink(artifact,scope,task));
    if(!artifacts.length)files.append(node('p','wb-muted','暂无产出文件'));
    const desired=versions.value||snapshot.published_delivery_id||'';
    versions.replaceChildren();const context=node('option','','当前节点上下文');context.value='';versions.append(context);
    for(const value of snapshot.deliveries||[]){const option=node('option','',`基于交付 v${value.number}`);option.value=value.id;versions.append(option);}
    versions.value=snapshot.deliveries?.some(row=>row.id===desired)?desired:'';
    composer.hidden=task.readonly||!snapshot.capabilities?.node_messages;
    renderLog(attempt,changedSelection);renderReason(attempt,state);
  }
  async function load(){
    if(!active()){destroy();return;}if(loading)return;loading=true;controller=new AbortController();
    try{const next=await api.get('/api/workbench/node',{...scope,run_id:task.run_id,task_id:task.id,node_id:item.id},controller.signal);
      if(!active())return;
      const key=JSON.stringify(next);if(snapshotKey===key)return;
      const before=selected;snapshot=next;snapshot.attempts??=[];snapshotKey=key;chooseDefault();render(before!==selected);
    }catch(error){if(active()&&error.name!=='AbortError'){notice.textContent=error.message;}}
    finally{loading=false;}
  }
  async function sendMessage(){
    if(sending||!active()||task.readonly)return;
    const text=input.value.trim();if(!text){input.focus();return;}
    const delivery=snapshot.deliveries?.find(row=>row.id===versions.value),attempt=selectedAttempt();
    const request=snapshot.requests?.find(row=>row.id===attempt?.request_id);
    const body={...scope,run_id:task.run_id,task_id:task.id,node_id:item.id,kind:kind.value,message:text,
      target_attempt_id:delivery?.attempt_id||request?.target_attempt_id||(!attempt?.id.startsWith('request:')&&attempt?.id)||snapshot.current_attempt_id,
      base_delivery_id:delivery?.id||'',parent_request_id:request?.id||'',references:[]};
    const signature=JSON.stringify(body);if(pendingMessage?.signature!==signature)pendingMessage={signature,id:crypto.randomUUID()};
    sending=true;send.disabled=true;notice.textContent='正在发送…';
    try{const result=await api.post('/api/workbench/node-message',{...body,request_id:pendingMessage.id});
      if(!active())return;input.value='';pendingMessage=null;selectedByUser=false;notice.textContent='消息已送达。';
      store.accept(result.view,owner);store.refresh();await load();
    }catch(error){if(active())notice.textContent=error.message;}
    finally{sending=false;if(active())send.disabled=false;}
  }
  function updateView(next){
    currentView=next;if(next.task.run_id!==task.run_id||next.task.id!==task.id||!active()){destroy();return;}
    const execution=next.structure?.nodes?.find(n=>n.id===item.id)?.execution;
    if(execution&&snapshot){const attempt=snapshot.attempts.find(row=>row.id===(execution.attempt_id||snapshot.current_attempt_id));
      if(attempt&&JSON.stringify(attempt.execution)!==JSON.stringify(execution)){attempt.execution=execution;render();}}
  }
  const timer=setInterval(()=>void load(),2000);timer.unref?.();
  function destroy(){if(disposed)return;disposed=true;clearInterval(timer);controller?.abort();if(dialog.open&&dialog.close)dialog.close();dialog.remove();onClose?.();}
  void load();return {destroy,refresh:load,updateView,element:dialog};
}
