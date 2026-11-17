import {node, button} from './dom.js';
import {artifactLink} from './workflow-node.js?v=20261117-exploration';

const statuses={queued:'已排队',running:'正在处理',blocked:'已阻塞',waiting:'等待节点回复',paused:'已暂停',completed:'已回复',failed:'本次请求未完成',cancelled:'已取消'};

export function mountNodeDialogue(container,{api,store,scope,task,item,active,refresh}) {
  let snapshot=null,sending=false,parent='',base='',attempt='',pendingMessage=null;
  const root=node('section','wb-node-dialogue'),title=node('h4','','与此节点对话');
  const help=node('p','wb-muted','保留本节点上下文。补交生成新版本，不暂停其他节点。');
  const versions=node('select','wb-input');versions.setAttribute('aria-label','追问依据的交付版本');
  const input=node('textarea','wb-input wb-node-message-input');input.setAttribute('aria-label','给此节点的消息');
  input.placeholder='直接提问，或说明需要修正、补充的内容…';input.rows=4;
  const references=node('input','wb-input');references.setAttribute('aria-label','关联文件或页码');
  references.placeholder='关联文件、页码或记录（可选，用逗号分隔）';
  const notice=node('p','wb-muted');notice.setAttribute('role','status');
  const actions=node('div','wb-actions');
  const ask=button('发送追问',()=>send('ask')),revise=button('要求补交',()=>send('revise'));
  actions.append(ask,revise);
  const messages=node('div','wb-node-dialogue-messages'),deliveries=node('div','wb-node-delivery-versions');
  root.append(title,help,versions,input,references,actions,notice,messages,deliveries);root.hidden=true;container.append(root);

  async function send(kind) {
    if(sending||!active())return;
    const text=input.value.trim();if(!text){notice.textContent='请填写消息内容';return;}
    const chosen=snapshot?.deliveries?.find(value=>value.id===versions.value);
    const body={...scope,run_id:task.run_id,task_id:task.id,node_id:item.id,kind,message:text,
      target_attempt_id:chosen?.attempt_id||attempt,base_delivery_id:chosen?.id||'',
      references:references.value.split(/[,，\n]/).map(value=>value.trim()).filter(Boolean),parent_request_id:parent};
    const signature=JSON.stringify(body);
    if(pendingMessage?.signature!==signature)pendingMessage={signature,id:crypto.randomUUID()};
    sending=true;ask.disabled=revise.disabled=true;notice.textContent='正在发送…';
    try {
      const result=await api.post('/api/workbench/node-message',{...body,request_id:pendingMessage.id});
      if(!active())return;
      input.value='';parent='';pendingMessage=null;notice.textContent='消息已送达，其他节点继续执行。';
      store.accept(result.view,store.generation);store.refresh();await refresh();
    }catch(error){if(active())notice.textContent=error.message;}
    finally{sending=false;if(active())ask.disabled=revise.disabled=false;}
  }

  function update(next,selectedAttempt) {
    snapshot=next;attempt=selectedAttempt||next.current_attempt_id||'';
    root.hidden=!next.capabilities?.node_messages;
    if(root.hidden)return;
    const selected=next.attempts.find(row=>row.id===attempt);
    const available=(next.deliveries||[]).filter(value=>!selected?.session_id||value.session_id===selected.session_id);
    const desired=versions.value||base||next.published_delivery_id;
    versions.replaceChildren();
    const current=node('option','','当前节点上下文');current.value='';versions.append(current);
    for(const value of available){const option=node('option','',`交付 v${value.number}${value.id===next.published_delivery_id?' · 当前版本':''}`);option.value=value.id;versions.append(option);}
    versions.value=available.some(value=>value.id===desired)?desired:'';
    messages.replaceChildren();
    for(const request of next.requests||[]) {
      const card=node('article','wb-node-dialogue-message');
      const incoming=request.node_id===item.id;
      const source=request.sender_kind==='user'?'你':request.sender_kind==='system'?'上游更新':request.sender_node_id;
      card.append(node('strong','',`${source} → ${request.node_id} · ${request.kind==='ask'?'追问':request.kind==='update'?'局部更新':'补交'}`),
        node('span','wb-node-request-status',statuses[request.display_status||request.status]||request.status),node('p','',request.message));
      if(request.activity_reason)card.append(node('p','wb-muted',request.activity_reason));
      if(request.references?.length)card.append(node('p','wb-muted','关联：'+request.references.join('、')));
      const reply=request.reply||{};
      if(reply.text)card.append(node('div','wb-node-dialogue-reply',reply.text));
      if(reply.reason)card.append(node('p','wb-muted',reply.reason));
      if(request.delivery_id){const version=next.deliveries?.find(value=>value.id===request.delivery_id);card.append(node('p','wb-muted',version?`已补交 v${version.number}`:'新交付已登记'));}
      const progress=next.attempts.find(row=>row.id===request.attempt_id)?.progress||[];
      if(!['completed','failed','cancelled'].includes(request.status))for(const value of progress.slice(-5))card.append(node('p','wb-muted',value.text));
      if(incoming)card.append(button('继续追问',()=>{parent=request.id;base=request.delivery_id||request.base_delivery_id;versions.value=base||'';input.focus();notice.textContent='继续这条消息；节点会保留此前上下文。';}));
      if(!['completed','failed','cancelled'].includes(request.status))card.append(button('取消这次请求',async()=>{
        try{const result=await api.post('/api/workbench/node-message/cancel',{...scope,run_id:task.run_id,task_id:task.id,message_id:request.id});
          if(active()){store.accept(result.view,store.generation);await refresh();}}
        catch(error){if(active())notice.textContent=error.message;}
      }));
      messages.append(card);
    }
    const expanded=new Set([...deliveries.querySelectorAll('details')].filter(value=>value.open).map(value=>value.getAttribute('data-delivery-id')));
    deliveries.replaceChildren();
    if(available.length)deliveries.append(node('h4','','交付版本'));
    for(const delivery of available.slice().reverse()) {
      const group=node('details','wb-node-delivery-version');
      group.setAttribute('data-delivery-id',delivery.id);group.open=expanded.has(delivery.id);
      group.append(node('summary','',`v${delivery.number}${delivery.id===next.published_delivery_id?' · 当前版本':''}`));
      if(delivery.changed_files?.length)group.append(node('p','wb-muted','变更文件：'+delivery.changed_files.join('、')));
      if(delivery.unresolved_requests?.length)group.append(node('p','wb-muted',`交付时有 ${delivery.unresolved_requests.length} 个请求尚未解决，回复保留在节点对话中。`));
      for(const artifact of delivery.result?.artifacts||[])group.append(artifactLink(artifact,scope,task));
      if(delivery.result?.text)group.append(node('p','',delivery.result.text));
      deliveries.append(group);
    }
  }
  return {update,destroy(){root.remove();}};
}
