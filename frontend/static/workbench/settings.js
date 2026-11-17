import {node,button} from './dom.js';
import {modelPicker} from './model-picker.js';
const samePath=(a,b)=>String(a||'').replace(/\\/g,'/').replace(/\/$/,'').toLowerCase()===String(b||'').replace(/\\/g,'/').replace(/\/$/,'').toLowerCase();

// Retains project roles and versioned paused-run model changes from the old
// expert configuration dialog. Merely reading settings never starts a task.
export function mountSettings(container,{api,store}) {
  const details=node('details','wb-settings'),body=node('div'),fields=node('div'),message=node('p','wb-action-message');
  details.append(node('summary','','模型与运行设置'),body);container.append(details);message.setAttribute('role','status');
  const hint=node('p','wb-muted','项目配置用于以后的运行。暂停任务可单独应用核查模型、辅助执行器或困难步骤模型；已完成结果与单独指定的节点验收模型保留。');
  const reload=button('读取当前配置',()=>void load()),save=button('保存项目配置',()=>void persist());
  const apply=button('应用辅助执行器到暂停任务',()=>void persist('capability-executor'));
  const verifier=button('应用核查模型到暂停任务',()=>void persist('verifier-model'));
  const difficult=button('为选中步骤设置困难模型',()=>void persist('difficult-node-model'));
  const nodes=node('div','wb-model-nodes'),difficulty=node('div'),actions=node('div','wb-actions');actions.append(save,verifier,apply,difficult);
  body.append(hint,reload,fields,difficulty,nodes,actions,message);
  let task=null,owner='',state=null,snapshot=null,pickers={},nodeChecks=[],difficultPicker=null,busy=false,disposed=false;
  const current=key=>!disposed&&key===owner&&key.startsWith(store.generation+':');
  function render(){details.hidden=task?.mode==='team'||!!task?.readonly;reload.disabled=busy;save.disabled=busy||!state;
    const paused=!!snapshot&&['paused','blocked','needs_attention'].includes(snapshot.status);
    verifier.hidden=!paused||task?.mode!=='collaborative';verifier.disabled=busy;
    apply.hidden=difficult.hidden=nodes.hidden=difficulty.hidden=!paused;apply.disabled=difficult.disabled=busy;
    for(const picker of Object.values(pickers))picker.disable(busy);difficultPicker?.disable(busy);
    for(const check of nodeChecks)check.disabled=busy;
  }
  async function load(){
    if(busy||!store.state.scope.conversation_id)return;const key=owner,scope={...store.state.scope};busy=true;message.textContent='正在读取…';render();
    try{const [profile,catalog,run]=await Promise.all([api.get('/api/expert-profile'),api.get('/api/models/catalog'),task?.run_id?api.get('/api/expert-runs/'+encodeURIComponent(task.run_id)):null]);
      if(!current(key))return;
      if(!samePath(profile.project,scope.project)||(run&&run.conversation_id!==scope.conversation_id))throw Error('项目或对话已切换，请重新读取配置。');
      state=profile;snapshot=run;fields.replaceChildren();difficulty.replaceChildren();nodes.replaceChildren();pickers={};nodeChecks=[];
      const existing=state.profile||state.recommended_profile||{},roles=existing.role_models||{},resolved=state.role_models?.roles||{};
      const models=(catalog.models||[]).filter(model=>model.available!==false);
      for(const [name,label,initial,optional] of [['manager','管理',existing.manager,false],['planner','规划',roles.planner||existing.planner,true],
        ['executor','执行',roles.executor||existing.executor,false],['auditor','核查',roles.verifier||resolved.verifier||existing.auditor,false],
        ['reviewer','安全审查',roles.reviewer||resolved.reviewer||existing.auditor,false],['arbiter','裁决',roles.arbiter||resolved.arbiter||existing.manager,false],
        ['capability_executor','辅助执行器',existing.capability_executor,true]]){
        const eligible=name==='capability_executor'?models.filter(model=>model.tools!==false&&!model.review_only&&model.protocol!=='typesafe'&&(!model.capabilities?.length||model.capabilities.includes('tools'))):models.filter(model=>name==='reviewer'||!model.review_only);
        pickers[name]=modelPicker(fields,label,eligible,initial,{optional});
      }
      difficultPicker=modelPicker(difficulty,'困难步骤',models.filter(row=>!row.review_only),null);
      for(const row of snapshot?.locked_flow?.nodes||[]){const label=node('label','wb-check-field'),check=node('input');check.type='checkbox';check.value=row.node_id;nodeChecks.push(check);label.append(check,node('span','',row.label||row.node_id));nodes.append(label);}
      message.textContent='配置已读取。';
    }catch(error){if(current(key))message.textContent=error.message;}
    finally{if(current(key)){busy=false;render();}}
  }
  async function persist(operation='profile'){
    if(busy||!state)return;const key=owner,scope={...store.state.scope};busy=true;message.textContent='正在保存…';render();
    try{let result;
      if(operation==='profile'){
        const values=Object.fromEntries(Object.entries(pickers).map(([name,picker])=>[name,picker.get()]));
        result=await api.put('/api/expert-profile',{...(state.profile||state.recommended_profile||{}),...scope,activate:false,
          manager:values.manager,planner:values.planner,executor:values.executor,auditor:values.auditor,capability_executor:values.capability_executor,
          role_models:{planner:values.planner||values.manager,executor:values.executor,verifier:values.auditor,reviewer:values.reviewer,arbiter:values.arbiter}});
      }else{
        if(!snapshot)throw Error('请先读取当前暂停任务。');
        const selected=nodeChecks.filter(check=>check.checked).map(check=>check.value);
        if(operation==='difficult-node-model'&&!selected.length)throw Error('请选择需要设置的步骤。');
        result=await api.post('/api/expert-runs/'+encodeURIComponent(snapshot.run_id)+'/'+operation,{...scope,expected_updated_at:snapshot.updated_at,command_id:crypto.randomUUID(),
          model:operation==='capability-executor'?pickers.capability_executor.get():operation==='verifier-model'?pickers.auditor.get():difficultPicker.get(),...(operation==='difficult-node-model'?{node_ids:selected,apply_node_ids:selected}:{})});
      }
      if(!current(key))return;if(result.snapshot)snapshot=result.snapshot;if(result.profile)state.profile=result.profile;
      message.textContent=operation==='profile'?'项目配置已保存。':'运行配置已保存，检查点保留，可继续任务。';store.refresh();
    }catch(error){if(current(key))message.textContent=error.message;}
    finally{if(current(key)){busy=false;render();}}
  }
  details.addEventListener('toggle',()=>{if(details.open&&!state)void load();});
  return {open(){details.open=true;return load();},update(view){task=view?.task||null;const key=`${store.generation}:${task?.id||''}:${task?.run_id||''}`;
    if(owner!==key){owner=key;state=snapshot=null;pickers={};nodeChecks=[];difficultPicker=null;busy=false;fields.replaceChildren();nodes.replaceChildren();difficulty.replaceChildren();message.textContent='';details.open=false;}render();},destroy(){disposed=true;}};
}
