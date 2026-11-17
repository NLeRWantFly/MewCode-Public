import {node,button} from './dom.js';
import {evidence} from './process.js';

// Replaces the maintenance modal and its chat-composer takeover. Decisions
// are rendered exclusively by inbox.js; this foldout retains the investigation.
export function mountMaintenance(container,{api,store,openInbox}) {
  const details=node('details','wb-maintenance'), content=node('div');
  details.append(node('summary','','维护与修复'),content); container.append(details);
  const choose=node('select','wb-input'); choose.setAttribute('aria-label','维护记录');
  const mode=node('select','wb-input'); mode.setAttribute('aria-label','维护目的');
  for(const [value,label] of [['fix','调查并修复问题'],['update','讨论编排更新']]){const option=node('option','',label);option.value=value;mode.append(option);}
  const target=node('select','wb-input');target.setAttribute('aria-label','优先检查的步骤');
  const history=node('div'), status=node('p','wb-muted'), message=node('p','wb-action-message'); message.setAttribute('role','status');
  const input=node('textarea','wb-input'); input.rows=3;input.maxLength=16000;input.setAttribute('aria-label','维护要求');input.placeholder='描述需要调查或更新的内容';
  const send=button('发送维护要求',()=>void submit()), stop=button('停止本次调查',()=>void operate('stop'));
  const fresh=button('按当前计划新开讨论',()=>void open(mode.value,'',true)), repair=button('检修执行环境',()=>void operate('repair-runtime'));
  const pending=button('去待你处理作答',()=>openInbox());
  const actions=node('div','wb-actions');actions.append(send,stop,fresh,repair);
  content.append(choose,mode,target,status,history,pending,input,actions,message);
  let owner='',view=null,state=null,selected='',busy=false,disposed=false,listKey='',historyKey='';
  const current=key=>!disposed&&key===owner&&key.startsWith(store.generation+':');
  function render(){
    details.hidden=!view||view.task.mode==='team'||view.legacy;
    const rows=view?.maintenance||[], key=JSON.stringify(rows.map(row=>[row.id,row.mode,row.revision]));
    if(key!==listKey){listKey=key;choose.replaceChildren(...rows.map(row=>{const option=node('option','',(row.mode==='fix'?'问题调查':'编排更新')+' · '+(row.updated_at||'').replace('T',' ').slice(0,16));option.value=row.id;return option;}));}
    choose.hidden=!rows.length;choose.value=selected;
    const disabled=busy||view?.task.readonly;
    input.disabled=mode.disabled=target.disabled=fresh.disabled=!!disabled;
    send.disabled=!!disabled||state?.status==='running'||state?.status==='applied'||state?.stale;
    stop.hidden=state?.status!=='running';stop.disabled=!!disabled;
    repair.hidden=!state||state.mode!=='fix';repair.disabled=!!disabled||state?.status==='running'||(view?.task.workflow_status||view?.task.execution_status)==='running'||view?.task.status==='进行中';
    pending.hidden=!(view?.inbox||[]).some(row=>row.type.startsWith('maintenance_')&&row.context.id===state?.id);
    status.textContent=state?.interrupted?'上次调查已中断。原记录已保留，可重新发起。':state?.error||state?.run_notice||state?.agent?.detail||'';
    const next=JSON.stringify([state?.id,state?.messages,state?.agent?.dialogue,state?.agent?.events,state?.proposal]);
    if(next!==historyKey){historyKey=next;history.replaceChildren();
      for(const row of state?.messages||[]){const entry=node('details','wb-evidence');entry.append(node('summary','',row.role==='user'?'你的要求或决定':'维护回复'),node('pre','wb-code',row.text||''));history.append(entry);}
      evidence(history,'调查中的工具与输出',state?.agent?.dialogue||state?.agent?.events);
      evidence(history,'当前修订摘要',state?.proposal?.summary);
    }
  }
  async function operate(action,extra={}){
    if(busy||!view||view.task.readonly)return null;
    const key=owner;busy=true;message.textContent='正在提交…';render();
    try{const data=await api.post('/api/workbench/maintenance',{...store.state.scope,task_id:view.task.id,run_id:view.task.run_id,
      action,...(action!=='open'?{id:state?.id,revision:state?.revision}:{}),...extra});
      if(!current(key))return null;state=data;selected=data.id;message.textContent=data.error||'已提交';render();store.refresh();return data;
    }catch(error){if(current(key))message.textContent=error.message;return null;}
    finally{if(current(key)){busy=false;render();}}
  }
  async function open(kind='fix',request='',newRecord=false,nodeIds=[],id=''){
    details.open=true;mode.value=kind;if(request)input.value=request;
    return operate('open',{mode:kind,request,node_ids:nodeIds.length?nodeIds:target.value?[target.value]:[],fresh:newRecord,...(id?{id}:{})});
  }
  async function submit(){
    const text=input.value.trim();if(!text||busy)return;const key=owner;
    if(!state&&!await open(mode.value))return;
    if(!current(key))return;
    if(await operate('send',{message:text}))input.value='';
  }
  choose.addEventListener('change',()=>{selected=choose.value;state=view?.maintenance.find(row=>row.id===selected);render();});
  return {open,update(next){
    const key=`${store.generation}:${next?.task.id||''}:${next?.task.run_id||''}`;
    if(key!==owner){owner=key;state=null;selected='';busy=false;input.value='';message.textContent='';listKey=historyKey='';details.open=false;}
    view=next;
    const saved=next?.maintenance?.find(row=>row.id===selected)||(!selected?next?.maintenance?.[0]:null);
    if(saved&&(!state||saved.revision>=state.revision)){state=saved;selected=saved.id;mode.value=saved.mode;}
    const old=target.value;target.replaceChildren(...[['','整个任务'],...(next?.plan||[]).flatMap(group=>group.steps.map(step=>[step.id,step.name||step.label||step.id]))].map(([value,label])=>{const option=node('option','',label);option.value=value;return option;}));target.value=old;
    render();
  },destroy(){disposed=true;}};
}
