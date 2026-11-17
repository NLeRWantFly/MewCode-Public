import {node, button} from './dom.js';
import {hostPath} from './api.js';
import {mountNodeDialogue} from './workflow-dialogue.js';
import {renderWorkflowBlocks} from './workflow-blocks.js';

export function mountWorkflowStep(step,{api,store,task}) {
  const element=node('details','wb-step'),summary=node('summary'),name=node('strong','',step.name),status=node('span','wb-muted',step.status),body=node('div');
  summary.append(name,status);element.append(summary,body);let mounted=null,current=step;
  function load(){if(!mounted&&element.open)mounted=mountWorkflowNode(body,{api,store,view:{task},item:{
    id:step.id,kind:current.detail?.kind,config:current.detail?.config||{}}});}
  element.addEventListener('toggle',load);
  return {element,update(next){current=next;name.textContent=next.name;status.textContent=next.status;},
    open(){element.open=true;load();element.scrollIntoView({block:'nearest'});},destroy(){mounted?.destroy();element.remove();}};
}

export function artifactLink(artifact, scope, task) {
  const link=node('a','wb-workflow-file',artifact.name || artifact.path || '产物');
  const query=new URLSearchParams({...scope,run_id:task.run_id,task_id:task.id,file_id:artifact.id,download:'1'});
  link.href=hostPath('/api/workbench/artifact')+'?'+query;link.target='_blank';link.rel='noopener';
  return link;
}

export function mountWorkflowNode(container,{api,store,view,item}) {
  if(view.task.purpose==='exploration'){
    const records=node('div','wb-workflow-conversation');container.append(records);
    let disposed=false;
    api.get('/api/workbench/node',{...store.state.scope,run_id:view.task.run_id,task_id:view.task.id,node_id:item.id}).then(value=>{
      if(disposed)return;
      const presentation=node('div');records.append(presentation);
      renderWorkflowBlocks(presentation,value.presentation_blocks||[],{scope:store.state.scope,task:view.task});
      for(const row of value.observations||[]){
        const entry=node('article','wb-workflow-message');
        entry.append(node('strong','',row.actor==='chat'?'主对话':row.actor));
        if(row.iteration?.length)entry.append(node('span','wb-muted',' · 循环 '+row.iteration.join(' / ')));
        entry.append(node('p','',row.text||row.result?.text||row.reason||row.status));records.append(entry);
      }
      if(!value.observations?.length)records.append(node('p','wb-muted','尚无公开执行记录。'));
    }).catch(error=>{if(!disposed)records.textContent=error.message;});
    return {destroy(){disposed=true;records.remove();}};
  }
  const scope={...store.state.scope}, owner=store.generation, task=view.task;
  let disposed=false, snapshot=null, snapshotKey='', activeAttempt='', pending=false,loading=false;
  const active=()=>!disposed&&owner===store.generation;
  const controls=node('div','wb-workflow-node-controls'), notice=node('p','wb-muted');
  const history=node('select','wb-input');history.setAttribute('aria-label','执行记录');
  const conversation=node('div','wb-workflow-conversation'), artifacts=node('div','wb-workflow-artifacts');
  const presentation=node('div');
  const diagnostics=node('details','wb-workflow-diagnostics');diagnostics.append(node('summary','','工具诊断记录'));
  const diagnosticBody=node('pre');diagnostics.append(diagnosticBody);
  const inputForm=node('details');inputForm.append(node('summary','','绑定输入／现有文件'));
  const inputName=node('input','wb-input'), inputValue=node('textarea','wb-input');
  inputName.placeholder='输入名称';inputName.setAttribute('aria-label','输入名称');
  inputValue.placeholder='文件路径、文字或 JSON';inputValue.setAttribute('aria-label','输入值');
  const inputType=node('select','wb-input');
  for(const [value,label] of [['file','现有文件'],['text','文字'],['json','JSON 数据']]) {
    const option=node('option','',label);option.value=value;inputType.append(option);
  }
  inputType.setAttribute('aria-label','输入类型');
  inputForm.append(inputName,inputType,inputValue,button('保存输入',async()=>{
    if(!inputName.value.trim()) {notice.textContent='请填写输入名称';return;}
    try {
      const value=inputType.value==='file'?{path:inputValue.value.trim()}:inputType.value==='json'?JSON.parse(inputValue.value):inputValue.value;
      await act('bind_inputs',{inputs:{[inputName.value.trim()]:value}});
    } catch(error) {notice.textContent=error.message;}
  }));
  const labels={pause:'暂停节点',resume:'继续节点',rerun:'仅重跑此节点',continue_from:'从此处继续',skip:'跳过'};
  for(const [operation,label] of Object.entries(labels)) controls.append(button(label,()=>act(operation)));
  const adopt=button('采用这次交付',()=>act('adopt',{attempt_id:activeAttempt}));
  const reload=button('刷新记录',()=>load());
  if(item.kind==='judge'&&item.config?.manual) {
    controls.append(button('通过',()=>act('judge_answer',{value:true})),button('不通过',()=>act('judge_answer',{value:false})));
  }
  const dialogueHost=node('div');
  container.append(controls,node('p','wb-muted','重跑保留历史记录，会再次执行所选范围内的操作；不会回滚文件或数据库。'),
    inputForm,notice,history,adopt,reload,dialogueHost,presentation,conversation,artifacts,diagnostics);
  const dialogue=mountNodeDialogue(dialogueHost,{api,store,scope,task,item,active,refresh:load});
  async function act(operation,extra={}) {
    if(pending||!active())return;
    pending=true;notice.textContent='正在保存…';
    try {
      const result=await api.post('/api/workbench/run-action',{...scope,run_id:task.run_id,task_id:task.id,
        request_id:crypto.randomUUID(),operation,node_id:item.id,expected_revision:task.plan_version,...extra});
      if(active()) {store.accept(result.view,owner);store.refresh();notice.textContent='已保存';await load();}
    } catch(error) {if(active())notice.textContent=error.message;}
    finally {pending=false;}
  }
  function render() {
    if(snapshot)dialogue.update(snapshot,activeAttempt);
    const attempt=snapshot?.attempts.find(row=>row.id===activeAttempt);
    const blocks=activeAttempt===snapshot?.current_attempt_id?snapshot?.presentation_blocks||[]:
      Object.values(attempt?.presentation_blocks||{});
    renderWorkflowBlocks(presentation,blocks,{scope,task});
    conversation.replaceChildren();artifacts.replaceChildren();
    if(!attempt) {conversation.append(node('p','wb-muted','此节点尚无公开进展。'));adopt.hidden=true;return;}
    adopt.hidden=attempt.status!=='completed'||!!attempt.request_id;
    for(const progress of attempt.progress || []) {
      const message=node('article','wb-workflow-message');message.append(node('p','',progress.text));conversation.append(message);
    }
    const text=attempt.result?.text;
    if(text && text!==attempt.progress?.at(-1)?.text) {
      const message=node('article','wb-workflow-message wb-workflow-delivery');
      message.append(node('strong','','本次交付'),node('div','',text));conversation.append(message);
    }
    if(attempt.result?.reason) conversation.append(node('p','wb-node-status-reason',attempt.result.reason));
    for(const artifact of [...(attempt.artifacts || []),...(attempt.report?[attempt.report]:[])]) {
      const row=node('div','wb-workflow-artifact');row.append(artifactLink(artifact,scope,task));
      row.append(button('预览',async()=>{
        try {
          const result=await api.get('/api/workbench/artifact',{...scope,run_id:task.run_id,task_id:task.id,file_id:artifact.id});
          if(!active())return;
          let preview=row.querySelector('pre');if(!preview){preview=node('pre','wb-workflow-file-preview');row.append(preview);}
          preview.textContent=result.preview+(result.truncated?'\n…预览已截断，下载可查看完整文件。':'');
        }catch(error){if(active())notice.textContent=error.message;}
      }));artifacts.append(row);
    }
    diagnosticBody.textContent=(attempt.diagnostics || []).map(row=>[row.name||row.kind,row.output||row.reason||''].join('\n')).join('\n\n');
    diagnostics.hidden=!attempt.diagnostics?.length;
  }
  async function load() {
    if(loading||!active())return;
    loading=true;
    try {
      const result=await api.get('/api/workbench/node',{...scope,run_id:task.run_id,task_id:task.id,node_id:item.id});
      if(!active())return;
      const nextKey=JSON.stringify(result);
      if(nextKey===snapshotKey)return;
      snapshotKey=nextKey;snapshot=result;
      if(!result.attempts.some(row=>row.id===activeAttempt)) activeAttempt=result.current_attempt_id||result.attempts.at(-1)?.id;
      history.replaceChildren(...result.attempts.map(row=>{
        const request=result.requests?.find(value=>value.id===row.request_id);
        const kind=request?({ask:'追问',revise:'补交',update:'局部更新'}[request.kind]||'对话'):'执行';
        const status={running:'进行中',blocked:'已阻塞',suspended:'等待接续',completed:'已完成',failed:'未完成',paused:'已暂停',needs_input:'等待输入',unknown:'待核对'}[row.display_status||row.status]||row.status;
        const option=node('option','',`第 ${row.number} 次${kind} · 图 v${row.graph_revision}${row.iteration?.length?' · 循环 '+row.iteration.map(v=>v+1).join('/') : ''} · ${status}`);
        option.value=row.id;return option;
      }));history.value=activeAttempt||'';render();
    }catch(error){if(active())notice.textContent=error.message;}
    finally{loading=false;}
  }
  history.addEventListener('change',()=>{activeAttempt=history.value;render();});
  void load();
  const timer=setInterval(()=>{if(snapshot?.capabilities?.node_messages)void load();},2000);
  timer.unref?.();
  return {destroy(){disposed=true;clearInterval(timer);dialogue.destroy();},refresh:load};
}
