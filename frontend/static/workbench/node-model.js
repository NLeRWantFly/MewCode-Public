import {node,button} from './dom.js';
import {modelPicker} from './model-picker.js';

// A node binding is an explicit user gesture, never an inferred model upgrade.
export function mountNodeModel(container,{api,store,view,item,onDraftSave}) {
  const card=node('section','wb-node-model'),summary=node('p','wb-muted'),fields=node('div'),message=node('p','wb-action-message');
  const loadButton=button(view.task.readonly?'查看模型':'修改模型',load);
  const saveButton=button('保存节点模型',save,'wb-button wb-primary');saveButton.hidden=true;
  const entry=(view.requirements||[]).find(row=>row.kind==='node'&&row.id===item.id);
  let binding=entry?.data?.model||item.model||null,state=null,picker=null,busy=false,disposed=false;
  const generation=store.generation,scope={...store.state.scope},runId=view.task.run_id;
  const active=()=>!disposed&&card.isConnected&&generation===store.generation;
  const describe=()=>summary.textContent=binding?`${binding.provider_key} · ${binding.model_id}${binding.effort?' · '+binding.effort:''}`:runId?'读取后显示当前运行模型':'尚未指定节点模型';
  const render=()=>{loadButton.disabled=busy;saveButton.disabled=busy;picker?.disable(busy);};
  message.setAttribute('role','status');describe();card.append(node('strong','','节点模型'),summary,loadButton,fields,saveButton,message);container.append(card);
  async function load(){
    if(busy||!active())return;busy=true;message.textContent='正在读取模型…';render();
    try{
      const [catalog,current]=await Promise.all([
        api.get('/api/models/catalog',{fast:true,include_intelligence:false}),
        runId?api.get('/api/expert-runs/'+encodeURIComponent(runId)+'/node-model',{...scope,node_id:item.id}):null,
      ]);
      if(!active())return;
      if(current&&(current.run_id!==runId||current.node_id!==item.id||current.conversation_id!==scope.conversation_id))throw Error('任务或节点已切换，请重新读取。');
      state=current;if(current)binding=current.model;describe();fields.replaceChildren();picker=null;
      const editable=!view.task.readonly&&(runId?current.editable:view.editor?.editable);
      saveButton.hidden=!editable;
      if(!editable){message.textContent=current?.reason||'当前编排只读。';return;}
      const models=(catalog.models||[]).filter(row=>row.available!==false&&!row.review_only&&row.tools!==false&&row.protocol!=='typesafe'&&(!row.capabilities?.length||row.capabilities.includes('tools')));
      picker=modelPicker(fields,'节点',models,binding);
      message.textContent=runId?'保存后用于该节点后续执行；流程保持暂停。':'保存到当前节点，重新构图时保留此模型。';
      loadButton.textContent='重新读取模型';
    }catch(error){if(active())message.textContent=error.message;}
    finally{if(active()){busy=false;render();}}
  }
  async function save(){
    if(busy||!picker||!active())return;busy=true;message.textContent='正在保存…';render();
    try{
      const model=picker.get();
      if(runId){
        const result=await api.post('/api/expert-runs/'+encodeURIComponent(runId)+'/difficult-node-model',{
          ...scope,model,node_ids:[item.id],apply_node_ids:[item.id],record_backup:false,
          expected_updated_at:state.updated_at,command_id:crypto.randomUUID(),
        });
        if(!active())return;state={...state,updated_at:result.snapshot.updated_at};store.refresh();
      }else await onDraftSave({action:'upsert',kind:'node',id:item.id,fields:{data:{model}}});
      if(!active())return;binding=model;describe();message.textContent='节点模型已保存。';
    }catch(error){if(active())message.textContent=error.message;}
    finally{if(active()){busy=false;render();}}
  }
  return {destroy(){disposed=true;}};
}
