import {node,button} from './dom.js';

export function mountWorkflowExport(aside,{api,store,opener}) {
  const panel=node('section','wb-workflow-export');panel.hidden=true;panel.setAttribute('aria-label','提取工作流结构');
  const heading=node('header','wb-export-heading'),title=node('strong','','提取工作流结构');
  const closeButton=button('返回画布',()=>close());
  heading.append(title,closeButton);
  const help=node('p','wb-export-help','保留顺序、分支、循环、并发依赖和对话摘要。提取只保存结构；明确要求复用并绑定新材料后才执行。');
  const controls=node('div','wb-export-controls');
  const source=node('select','wb-input');source.setAttribute('aria-label','提取的结构版本');
  const applied=node('option','','已应用结构'),draft=node('option','','未应用草稿');applied.value='applied';draft.value='draft';source.append(applied,draft);source.value='applied';
  const format=node('select','wb-input');format.setAttribute('aria-label','提取格式');
  for(const [value,label]of [['brief','构造说明（含完整结构与摘要）'],['json','完整 JSON'],['pseudocode','结构伪代码'],['recipe','已固化的可执行流程']]){
    const option=node('option','',label);option.value=value;format.append(option);
  }
  format.value='brief';
  const refresh=button('重新提取',()=>load()),copy=button('复制当前内容',()=>copyText()),download=button('下载当前格式',()=>downloadText());
  controls.append(source,format,refresh,copy,download);
  const status=node('p','wb-export-status');status.setAttribute('role','status');
  const preview=node('textarea','wb-export-preview');preview.readOnly=true;preview.spellcheck=false;preview.setAttribute('aria-label','工作流结构内容');
  panel.append(heading,help,controls,status,preview);aside.append(panel);
  let payload=null,owner='',serial=0,controller=null,background=[],disposed=false;
  const key=()=>JSON.stringify([store.state.scope,store.state.view?.task?.id,store.state.view?.task?.run_id]);
  const documentValue=()=>payload?.documents[format.value];
  function enabled(value){copy.disabled=download.disabled=!value;}
  function render(){
    const value=documentValue();preview.value=value?.text||'';enabled(!!value);
    if(payload)status.textContent=`${payload.source.selection==='draft'?'未应用草稿':'已应用结构'} v${payload.source.graph_revision} · ${payload.node_count} 个节点 · ${payload.edge_count} 条连线 · 对话摘要依据已应用图 v${payload.source.applied_revision}`;
  }
  function close(){
    serial++;controller?.abort();controller=null;panel.hidden=true;payload=null;preview.value='';enabled(false);
    for(const [element,inert]of background)element.inert=inert;background=[];
    opener.focus();
  }
  async function load(){
    const task=store.state.view?.task;if(!task?.run_id||task.mode!=='workflow')return;
    controller?.abort();controller=new AbortController();const ticket=++serial,identity=key();owner=identity;
    payload=null;preview.value='';enabled(false);status.textContent='正在提取结构…';
    try{
      const value=await api.get('/api/workbench/export',{...store.state.scope,run_id:task.run_id,task_id:task.id,selection:source.value},controller.signal);
      if(disposed||panel.hidden||ticket!==serial||identity!==key())return;
      payload=value;render();
      if(task.purpose==='exploration'){
        const recipe=await api.post('/api/workbench/recipes',{...store.state.scope,run_id:task.run_id,task_id:task.id});
        if(disposed||panel.hidden||ticket!==serial||identity!==key())return;
        payload.documents.recipe={text:JSON.stringify(recipe,null,2),content_type:'application/json',filename:'workflow-recipe.json'};
        render();status.textContent+=' · 已保存可复用流程，未启动执行';
      }
      if(task.solidified_recipe){
        const recipe=await api.get('/api/workbench/recipes',{...store.state.scope,recipe_id:task.solidified_recipe.id,
          version:task.solidified_recipe.version},controller.signal);
        if(disposed||panel.hidden||ticket!==serial||identity!==key())return;
        payload.documents.recipe={text:JSON.stringify(recipe,null,2),content_type:'application/json',filename:'workflow-recipe.json'};render();
      }
    }catch(error){if(!disposed&&!panel.hidden&&ticket===serial&&identity===key()&&error.name!=='AbortError')status.textContent=error.message;}
  }
  async function open(){
    if(opener.disabled||disposed)return;
    if(panel.hidden){
      for(const element of aside.children)if(element!==panel){background.push([element,element.inert]);element.inert=true;}
      panel.hidden=false;source.value='applied';format.value='brief';closeButton.focus();
    }
    await load();
  }
  async function copyText(){
    const value=documentValue();if(!value||owner!==key())return;
    const ticket=serial;
    try{
      await navigator.clipboard.writeText(value.text);
      if(!disposed&&!panel.hidden&&ticket===serial)status.textContent='已复制，可粘贴到下一个对话作为构造标准。';
    }catch{
      if(!disposed&&!panel.hidden&&ticket===serial){preview.focus();preview.select();status.textContent='内容已选中，请按 Ctrl+C 复制。';}
    }
  }
  function downloadText(){
    const value=documentValue();if(!value||owner!==key())return;
    const url=URL.createObjectURL(new Blob([value.text],{type:value.content_type+';charset=utf-8'}));
    const link=node('a');link.href=url;link.download=value.filename;panel.append(link);link.click();link.remove();
    setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  source.addEventListener('change',()=>load());format.addEventListener('change',render);
  panel.addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();event.stopPropagation();close();}});
  const unsubscribe=store.subscribe(state=>{
    const task=state.view?.task;
    opener.disabled=task?.mode!=='workflow'||!task?.run_id||state.loading||!!state.error||task.purpose==='exploration'&&task.execution_status!=='completed';
    draft.disabled=!state.view?.editor?.has_draft;
    if(!panel.hidden&&owner&&owner!==key())close();
  });
  enabled(false);
  return {open,close,load,destroy(){disposed=true;unsubscribe();close();panel.remove();}};
}
