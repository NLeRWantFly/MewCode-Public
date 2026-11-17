import {node,button} from './dom.js';
import {layout,routeGraph} from './geometry.js';
import {mountWorkflowNode,artifactLink} from './workflow-node.js?v=20261117-exploration';
import {openWorkflowConversation} from './workflow-conversation.js?v=20261117-exploration';
import {summarizeLoops} from './workflow-summary.js?v=20261117-exploration';
import {renderWorkflowBlocks} from './workflow-blocks.js';

const NAMES={work:'执行',judge:'判断',branch:'分支',loop:'循环'};
const WIDTH=340,PORT_Y=94,PORT_GAP=36,DEFAULT_ZOOM=1.05,EDIT_ZOOM=.9;
const PORT_NAMES={always:'完成',pass:'通过',fail:'不通过'};
const uuid=prefix=>prefix+crypto.randomUUID().replaceAll('-','').slice(0,12);
function svg(tag,attributes={}) {const item=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [k,v]of Object.entries(attributes))item.setAttribute(k,v);return item;}

export function mountWorkflowCanvas(container,{api,store,onMutate,onNodesToggle,onFocusMode}) {
  const toolbar=node('div','wb-graph-toolbar'),palette=node('div','wb-workflow-palette');
  const body=node('div','wb-graph-body'),viewport=node('div','wb-graph wb-workflow-viewport');
  const inspector=node('section','wb-node-inspector'),message=node('p','wb-graph-message');
  const hint=node('div','wb-canvas-hint'),hintText=node('span');
  const wireTip=node('div','wb-wire-tip'),wireSource=node('span'),wireTarget=node('strong');
  wireTip.append(wireSource,wireTarget);wireTip.hidden=true;wireTip.setAttribute('aria-hidden','true');
  const cancelWire=button('取消连线',()=>cancelConnection());cancelWire.hidden=true;
  hint.append(hintText,cancelWire);hint.setAttribute('role','status');hint.setAttribute('aria-live','polite');
  message.setAttribute('role','status');
  inspector.hidden=true;viewport.tabIndex=0;viewport.setAttribute('aria-label','自由编排画布');
  let current=null,scope='',selected='',connection=null,details=null,busy=false,zoom=DEFAULT_ZOOM,offset={x:24,y:24};
  let positions={},stage=null,wires=null,drag=null,detailKey='',editMode=false,disposed=false;
  let queuedView=null,renderKey='',selectedEdge='',suppressClickUntil=0,wireFrame=null;
  let cards=new Map(),activityRows=new Map(),wireElements=[],preview=null,reconnect=null;
  const activityClock=setInterval(()=>updateActivities(queuedView||current),1000);activityClock?.unref?.();
  let wirePointer=null,panFrame=null;
  let conversation=null;
  function enterLoop(id){cancelConnection();scope=id;selected='';detailKey='';render();}
  function openConversation(id){
    const item=current?.structure.nodes.find(n=>n.id===id);if(!item)return;
    if(item.kind==='loop'){conversation?.destroy();conversation=null;enterLoop(id);return;}
    if(current.task.purpose==='exploration'){select(id);return;}
    if(!current.task.run_id){select(id);message.textContent='节点尚未执行，启动后可双击查看对话。';return;}
    conversation?.destroy();
    conversation=openWorkflowConversation({root:container,api,store,view:current,item,
      onOpenNode:openConversation,onClose:()=>{conversation=null;cards.get(id)?.focus();}});
  }
  const back=button('← 上一层',()=>{cancelConnection();scope=scope.split('/').slice(0,-1).join('/');selected='';render();});
  const breadcrumb=node('span','wb-canvas-label','工作流');
  const undo=button('撤销',()=>mutate({action:'undo'})),redo=button('重做',()=>mutate({action:'redo'}));
  const apply=button('应用并继续',()=>applyGraph(),'wb-button wb-primary');
  const arrange=button('自动布局',()=>mutate({action:'positions',scope,positions:{}}));
  const fit=button('适应',()=>fitGraph());
  fit.title='保持文字可读，至少 90%；拖动空白查看其余节点';
  const overview=button('总览',()=>fitGraph(true));overview.title='缩小以查看整张流程图';
  const minus=button('−',()=>setZoom(zoom/1.2)),plus=button('+',()=>setZoom(zoom*1.2));
  minus.setAttribute('aria-label','缩小画布');plus.setAttribute('aria-label','放大画布');
  const zoomLabel=button('105%',()=>setZoom(1),'wb-button wb-canvas-zoom');zoomLabel.title='恢复到 100%';
  toolbar.append(back,breadcrumb,undo,redo,arrange,minus,zoomLabel,plus,fit,overview,apply);
  for(const [kind,label] of Object.entries(NAMES)) {
    const control=button('+ '+label,()=>editor(null,kind));control.draggable=true;control.dataset.component=kind;
    control.addEventListener('dragstart',event=>event.dataTransfer.setData('application/x-mew-component',kind));palette.append(control);
  }
  body.append(viewport,wireTip,hint,inspector);container.replaceChildren(toolbar,palette,body,message);
  const currentNodes=()=>current?.structure.nodes.filter(item=>(item.scope||'')===scope)||[];
  const editable=()=>current?.editor?.editable!==false;
  const currentEdges=()=>current?.structure.edges.filter(edge=>(edge.scope||'')===scope)||[];
  const local=id=>id.split('/').at(-1);
  function height(item){return 284+Math.max(1,Math.min(5,item.progress?.length||0))*18+(ports(item).length-1)*PORT_GAP+
    new Set((item.presentation_blocks||[]).map(block=>block.slot)).size*148;}
  function updateActivities(view){
    if(!view||disposed)return;
    for(const item of view.structure.nodes){
      const parts=activityRows.get(item.id);if(!parts)continue;
      const value=item.execution||{phase:'idle',text:item.status==='已完成'?'已交付':item.status||'尚未开始'};
      const stale=!!store.state.error;
      parts.row.dataset.phase=value.phase;parts.row.dataset.active=String(!!value.active&&!stale);
      const text=(stale?'状态暂未更新 · ':'')+value.text;
      if(parts.label.textContent!==text)parts.label.textContent=text;
      const seconds=Math.max(0,Math.floor(Date.now()/1000-(value.at||Date.now()/1000)));
      parts.elapsed.textContent=value.active&&value.at&&!stale?(seconds<60?`${seconds}秒`:`${Math.floor(seconds/60)}分${seconds%60}秒`):'';
      parts.row.title=[value.text,value.detail,value.at?'状态开始于 '+new Date(value.at*1000).toLocaleTimeString():'',
        stale?'连接暂不可用，显示最后一次状态':''].filter(Boolean).join('\n');
    }
  }
  function compactPositions(nodes,edges){
    const linked=new Set(edges.flatMap(edge=>[edge.from,edge.to]));
    const connected=nodes.filter(n=>linked.has(n.id)),loose=nodes.filter(n=>!linked.has(n.id));
    const result=layout(connected.map(n=>({id:n.id,w:WIDTH,h:height(n)})),edges.map(e=>[e.from,e.to]),72,36);
    for(const point of Object.values(result)){point.x-=80;point.y-=60;}
    let rowY=connected.length?Math.max(...connected.map(n=>result[n.id].y+height(n)))+40:40;
    for(let index=0;index<loose.length;index+=3){
      const row=loose.slice(index,index+3);
      row.forEach((item,column)=>{result[item.id]={x:40+column*(WIDTH+72),y:rowY};});
      rowY+=Math.max(...row.map(height))+36;
    }
    return result;
  }
  function camera(){
    if(stage){stage.style.transform=`translate(${offset.x}px,${offset.y}px) scale(${zoom})`;
      stage.style.setProperty('--wb-port-scale',Math.max(1,1/zoom));
      stage.style.setProperty('--wb-port-dot-scale',Math.min(1,PORT_GAP*zoom/20));}
    zoomLabel.textContent=Math.round(zoom*100)+'%';
  }
  function canvasPoint(event){const rect=viewport.getBoundingClientRect();return {x:(event.clientX-rect.left-offset.x)/zoom,y:(event.clientY-rect.top-offset.y)/zoom};}
  function setZoom(value,anchor={x:viewport.clientWidth/2,y:viewport.clientHeight/2}){
    const next=Math.max(.15,Math.min(2.5,value));
    offset={x:anchor.x-(anchor.x-offset.x)*next/zoom,y:anchor.y-(anchor.y-offset.y)*next/zoom};zoom=next;camera();
  }
  function fitGraph(all=false){
    const boxes=currentNodes().map(item=>({...positions[item.id],h:height(item)}));if(!boxes.length)return;
    const left=Math.min(...boxes.map(p=>p.x)),top=Math.min(...boxes.map(p=>p.y));
    const width=Math.max(...boxes.map(p=>p.x+WIDTH))-left,boundsHeight=Math.max(...boxes.map(p=>p.y+p.h))-top;
    zoom=Math.max(all ? .15 : EDIT_ZOOM,Math.min(DEFAULT_ZOOM,((viewport.clientWidth||900)-80)/width,((viewport.clientHeight||650)-100)/boundsHeight));
    offset={x:Math.max(40,((viewport.clientWidth||900)-width*zoom)/2)-left*zoom,y:32-top*zoom};camera();
  }
  async function mutate(operation) {
    if(busy||!current||!editable())return;
    cancelConnection(false);
    // Keep the user's arrangement when adding or reconnecting an edge.
    if(!['positions','undo','redo'].includes(operation.action)){
      const fixed=Object.fromEntries(Object.entries(positions).map(([id,p])=>[local(id),{x:p.x,y:p.y}]));
      operation={action:'batch',operations:[{action:'positions',scope,positions:fixed},
        ...(operation.action==='batch'?operation.operations:[{scope,...operation}])]};
    }
    const owner=current.task.run_id||current.task.id;let failure='';
    busy=true;message.textContent='正在保存…';
    try {
      const result=await onMutate(current,{scope,...operation},null);
      if(disposed||owner!==(current.task.run_id||current.task.id))return;
      if(result?.view)queuedView=result.view;
      message.textContent=current.editor.planning?'工作流已保存。':'草稿已保存，点击“应用并继续”更新执行。';
      return result;
    }catch(error){failure=error.message;}
    finally{busy=false;flushView();if(failure&&!disposed){render();message.textContent=failure;}}
  }
  async function applyGraph(){
    if(busy||!current||!editable())return;busy=true;
    try {
      const result=await api.post('/api/workbench/graph/apply',{...store.state.scope,task_id:current.task.id,
        run_id:current.task.run_id,request_id:crypto.randomUUID(),expected_revision:current.editor.revision});
      if(disposed)return;store.accept(result.view);queuedView=result.view;
      message.textContent=result.switching?'正在让受影响的节点暂停并切换到新图；其他分支继续。':'已应用，正在继续。';
    }catch(error){message.textContent=error.message;}finally{busy=false;flushView();}
  }
  function ports(item){
    const connected=currentEdges().filter(e=>e.from===item.id).map(e=>e.condition);
    const configured=item.kind==='judge'?['pass','fail']:item.kind==='branch'?
      [...(item.config?.cases||[]).map(c=>c.port),...(item.config?.default?[item.config.default]:[])]:['always'];
    const result=[...new Set([...configured,...connected])];return result.length?result:['always'];
  }
  function anchor(id,port){
    const point=positions[id],item=currentNodes().find(n=>n.id===id);
    return {x:point.x+(port===undefined?0:WIDTH),y:point.y+PORT_Y+(port===undefined?0:Math.max(0,ports(item).indexOf(port))*PORT_GAP)};
  }
  function updateHint(){
    cancelWire.hidden=!connection;
    hintText.textContent=connection ? `${currentNodes().find(n=>n.id===connection.from)?.label||''} · ${PORT_NAMES[connection.port]||connection.port} → ${connection.target?currentNodes().find(n=>n.id===connection.target)?.label:'拖到高亮节点，或点击目标节点'}`
      :!editable()?'点击节点查看记录 · 双击循环查看循环体 · 拖动空白移动 · 滚轮缩放':'双击节点查看对话／进入循环 · 拖动卡片移动 · 拖动出口连线 · 滚轮缩放';
    viewport.dataset.connecting=String(!!connection);
    wireTip.hidden=!connection;
    if(connection){
      wireSource.textContent=`${currentNodes().find(n=>n.id===connection.from)?.label||''} · ${PORT_NAMES[connection.port]||connection.port}`;
      wireTarget.textContent=connection.target?`连接到：${currentNodes().find(n=>n.id===connection.target)?.label}`:'拖到目标卡片，松开连接';
      const point=connection.target?anchor(connection.target):connection.point;
      const x=point.x*zoom+offset.x,y=point.y*zoom+offset.y;
      wireTip.style.left=Math.max(12,Math.min(x+22,(viewport.clientWidth||900)-312))+'px';
      wireTip.style.top=Math.max(12,Math.min(y+24,(viewport.clientHeight||650)-140))+'px';
      wireTip.dataset.ready=String(!!connection.target);
    }
  }
  function drawWires(){
    wireFrame=null;
    const edges=wireElements.map(row=>row.edge),boxes=currentNodes().map(n=>({...positions[n.id],w:WIDTH,h:height(n)}));
    const routes=routeGraph(edges.map(edge=>({key:edge.id,start:anchor(edge.from,edge.condition),end:anchor(edge.to)})),boxes);
    wireElements.forEach((row,index)=>{
      const wire=routes[index],end=anchor(row.edge.to);
      row.line.setAttribute('d',wire.d);row.hit.setAttribute('d',wire.d);
      row.arrow.setAttribute('d',`M ${end.x-24} ${end.y-5} L ${end.x-16} ${end.y} L ${end.x-24} ${end.y+5}`);
      row.label.setAttribute('x',wire.labelX);row.label.setAttribute('y',wire.labelY);
      row.group.dataset.selected=String(row.edge.id===selectedEdge);
      row.group.dataset.reconnecting=String(row.edge.id===connection?.edge?.id);
    });
    if(reconnect){const edge=edges.find(e=>e.id===selectedEdge);reconnect.hidden=!edge||!!connection;
      if(edge){const end=anchor(edge.to);reconnect.style.left=(end.x-42)+'px';reconnect.style.top=end.y+'px';}}
    drawPreview();
  }
  function scheduleWires(){if(wireFrame===null)wireFrame=requestAnimationFrame(drawWires);}
  function drawPreview(){
    if(!preview)return;
    preview.style.display=connection?'':'none';if(!connection)return;
    const from=anchor(connection.from,connection.port),to=connection.target?anchor(connection.target):connection.point;
    const pull=Math.max(70,Math.abs(to.x-from.x)*.45);
    preview.setAttribute('d',`M ${from.x} ${from.y} C ${from.x+pull} ${from.y}, ${to.x-pull} ${to.y}, ${to.x} ${to.y}`);
    preview.dataset.snapped=String(!!connection.target);
  }
  function render(){
    if(!current)return;back.hidden=!scope;breadcrumb.textContent=scope?'循环体 · '+(current.structure.nodes.find(item=>item.id===scope)?.label||scope):'自由编排';
    undo.disabled=!current.editor.can_undo;redo.disabled=!current.editor.can_redo;
    for(const control of [undo,redo,arrange,palette])control.hidden=!editable();
    apply.hidden=!!current.editor.planning||!editable();
    if(!editable())breadcrumb.textContent=scope?'循环体 · '+(current.structure.nodes.find(item=>item.id===scope)?.label||scope):'探索过程 · 修改请在对话中提出';
    apply.disabled=!current.editor.has_draft||current.editor.switching;
    apply.textContent=current.editor.switching?'正在应用…':'应用并继续';
    const impact=current.editor.impact;
    if(impact&&!busy)message.textContent=`待应用：${impact.affected.length} 个节点受影响，${impact.retained.length} 个节点保留。重跑不会回滚已有文件或数据库。`;
    else if(!busy&&!current.editor.switching)message.textContent='';
    const nodes=currentNodes(),edges=currentEdges();
    positions=compactPositions(nodes,edges);
    for(const item of nodes){const saved=current.editor.positions?.[item.id];if(saved)positions[item.id]={...positions[item.id],...saved};}
    viewport.replaceChildren();stage=node('div','wb-graph-stage wb-workflow-stage');stage.style.transformOrigin='0 0';viewport.append(stage);camera();
    wires=svg('svg',{width:8000,height:8000,'class':'wb-workflow-wires'});stage.append(wires);
    cards=new Map();activityRows=new Map();wireElements=[];
    for(const edge of edges){
      const a=positions[edge.from],b=positions[edge.to];if(!a||!b)continue;
      const group=svg('g',{'class':'wb-workflow-wire'});
      if(edge.selection)group.dataset.route=edge.selection;
      const line=svg('path',{'class':'wb-graph-edge'}),hit=svg('path',{'class':'wb-graph-edge-hit'});
      const arrow=svg('path',{'class':'wb-graph-edge wb-graph-arrow'});
      line.dataset.from=edge.from;line.dataset.to=edge.to;hit.dataset.edgeId=edge.id;
      line.dataset.condition=edge.condition;arrow.dataset.condition=edge.condition;
      hit.addEventListener('click',event=>{event.stopPropagation();if(editable()&&!connection)edgeEditor(edge);});
      const label=svg('text',{'class':'wb-workflow-edge-label','text-anchor':'middle'});
      label.textContent=edge.condition==='always'?'':PORT_NAMES[edge.condition]??edge.condition;
      group.append(line,arrow,hit,label);wires.append(group);wireElements.push({edge,group,line,hit,arrow,label});
    }
    preview=svg('path',{'class':'wb-graph-edge wb-wire-preview'});wires.append(preview);
    if(!nodes.length)stage.append(node('p','wb-graph-empty','直接在对话中开始任务；执行中的分工、依赖和输出会逐步显示在这里。'));
    for(const item of nodes){
      const card=node('article','wb-graph-node wb-workflow-card');card.dataset.nodeId=item.id;card.dataset.status=item.status;card.tabIndex=0;
      card.title=item.kind==='loop'?'双击进入循环体；按 Enter 也可进入':current.task.purpose==='exploration'?'查看公开执行记录':current.task.run_id?'双击查看节点对话；按 Enter 也可打开':'启动执行后可查看节点对话';
      const point=positions[item.id];Object.assign(card.style,{left:point.x+'px',top:point.y+'px',width:WIDTH+'px',height:height(item)+'px'});
      const heading=node('header','wb-workflow-card-title'),grip=node('span','wb-workflow-grip','⠿');
      grip.setAttribute('aria-hidden','true');grip.hidden=!editable();heading.title=editable()?'拖动卡片移动位置':'查看执行记录';
      heading.append(grip,node('strong','',item.label),node('span','wb-node-kind',NAMES[item.kind]));
      heading.dataset.dragNode=item.id;
      const rail=node('div','wb-workflow-port-rail');rail.style.height=(ports(item).length*PORT_GAP)+'px';
      const activity=item.status==='已阻塞'&&/provider stalled:.*phase=first_event/.test(item.activity||item.status_reason||'')
        ?'等待模型响应超时':item.activity;
      const status=node('span','wb-node-status',item.status+(item.delivery_version?` · v${item.delivery_version}`:'')+(activity?' · '+activity:''));
      status.title=[item.status,item.status_reason||item.activity].filter(Boolean).join(' · ');
      card.append(heading,status,rail,node('p','wb-workflow-task-preview',item.goal));
      const progress=node('ol','wb-workflow-progress');
      for(const entry of (item.progress||[]).slice(-5))progress.append(node('li','',entry.text));
      if(!progress.children.length)progress.append(node('li','wb-muted','尚无公开进展'));
      card.append(progress);
      if(item.presentation_blocks?.length){
        const blocks=node('div');renderWorkflowBlocks(blocks,item.presentation_blocks,{scope:store.state.scope,task:current.task,compact:true});
        blocks.dataset.compact='true';card.append(blocks);
      }
      const footer=node('footer','wb-workflow-card-files'),artifact=item.artifacts?.[0];
      if(artifact){footer.append(artifactLink(artifact,store.state.scope,current.task));if(item.artifacts.length>1)footer.append(node('span','',` +${item.artifacts.length-1}`));}
      else footer.textContent='暂无产出文件';card.append(footer);
      const execution=node('div','wb-workflow-execution'),dot=node('span','wb-workflow-execution-dot');
      const label=node('span','wb-workflow-execution-text'),elapsed=node('span','wb-workflow-execution-time');
      dot.setAttribute('aria-hidden','true');elapsed.setAttribute('aria-hidden','true');
      label.setAttribute('role','status');label.setAttribute('aria-live','polite');label.setAttribute('aria-atomic','true');
      execution.append(dot,label,elapsed);card.append(execution);activityRows.set(item.id,{row:execution,label,elapsed});
      const input=button('',event=>{event.stopPropagation();if(!clickSuppressed())connect(item.id);},'wb-node-port wb-node-port-in');
      input.dataset.inputNode=item.id;input.setAttribute('aria-label',item.label+' 输入');input.title='将连线拖到这里，或直接放到卡片上';input.style.top=PORT_Y+'px';
      input.disabled=!editable();
      const inputLabel=node('span','wb-workflow-port-label wb-workflow-port-label-in','输入');inputLabel.style.top=PORT_Y+'px';card.append(input,inputLabel);
      for(const [index,port]of ports(item).entries()){
        const outlet=button('',event=>{event.stopPropagation();if(!clickSuppressed())startConnection(item.id,port);},'wb-node-port wb-node-port-out');
        outlet.dataset.outputNode=item.id;outlet.dataset.port=port;outlet.dataset.condition=port;
        outlet.disabled=!editable();
        outlet.style.top=(PORT_Y+index*PORT_GAP)+'px';outlet.setAttribute('aria-label',item.label+' '+port+' 出口');
        outlet.title=(PORT_NAMES[port]||port)+'：拖到下游节点，或点击后选择目标';
        const portLabel=node('span','wb-workflow-port-label wb-workflow-port-label-out',PORT_NAMES[port]||port);
        portLabel.style.top=outlet.style.top;portLabel.dataset.condition=port;card.append(outlet,portLabel);
      }
      card.addEventListener('click',event=>{if(event.target.closest('button,a')||clickSuppressed())return;if(connection)connect(item.id);else select(item.id);});
      card.addEventListener('dblclick',event=>{if(event.target.closest('button,a')||clickSuppressed()||connection)return;event.stopPropagation();openConversation(item.id);});
      card.addEventListener('keydown',event=>{if(event.target===card&&event.key==='Enter'){event.preventDefault();if(connection)connect(item.id);else openConversation(item.id);}});
      stage.append(card);cards.set(item.id,card);
      progress.scrollTop=progress.scrollHeight;
    }
    reconnect=button('↪',event=>{event.stopPropagation();const edge=currentEdges().find(e=>e.id===selectedEdge);
      if(edge&&!clickSuppressed())startConnection(edge.from,edge.condition,edge);},'wb-wire-reconnect');
    reconnect.dataset.reconnectEdge='true';reconnect.title='拖动此手柄改接终点';reconnect.setAttribute('aria-label','拖动改接连线终点');stage.append(reconnect);
    updateSelection();drawWires();updateHint();updateActivities(current);
    renderInspector();
    renderKey=viewKey(current);
  }
  function clickSuppressed(){return performance.now()<suppressClickUntil;}
  function updateSelection(){for(const [id,card]of cards)card.dataset.selected=String(id===selected);}
  function connectionProblem(target){
    if(!connection)return '';
    if(connection.from===target)return '不能连接自身；重复执行请使用循环组件。';
    const edges=currentEdges().filter(e=>e.id!==connection.edge?.id);
    if(edges.some(e=>e.from===connection.from&&e.to===target&&e.condition===connection.port))return '这条连线已存在。';
    const queue=[target],seen=new Set();
    while(queue.length){const id=queue.pop();if(id===connection.from)return '这会形成依赖环；请使用循环组件。';
      if(seen.has(id))continue;seen.add(id);queue.push(...edges.filter(e=>e.from===id).map(e=>e.to));}
    return '';
  }
  function startConnection(from,port,edge=null){
    if(busy)return;
    const start=anchor(from,port);
    if(zoom<EDIT_ZOOM)setZoom(1,{x:start.x*zoom+offset.x,y:start.y*zoom+offset.y});
    connection={from,port,edge,target:'',point:{x:start.x+100,y:start.y}};
    for(const [id,card]of cards){card.dataset.connectable=String(!connectionProblem(id));card.dataset.connectionSource=String(id===from);}
    for(const outlet of cards.get(from).querySelectorAll('.wb-node-port-out'))outlet.dataset.connecting=String(outlet.dataset.port===port);
    drawWires();updateHint();
  }
  function cancelConnection(flush=true){
    stopWirePan();
    connection=null;
    for(const card of cards.values()){delete card.dataset.connectable;delete card.dataset.connectionSource;delete card.dataset.dropTarget;}
    for(const card of cards.values())for(const outlet of card.querySelectorAll('.wb-node-port-out'))delete outlet.dataset.connecting;
    drawPreview();updateHint();if(reconnect)reconnect.hidden=!selectedEdge;
    for(const row of wireElements)row.group.dataset.reconnecting='false';
    if(flush)flushView();
  }
  function aimConnection(event){
    if(!connection)return;
    connection.point=canvasPoint(event);connection.target='';
    const under=document.elementFromPoint(event.clientX,event.clientY)?.closest('.wb-workflow-card');
    let target=under&&viewport.contains(under)?under.dataset.nodeId:'';
    if(!target){let distance=32/zoom;
      for(const id of cards.keys()){const p=anchor(id),delta=Math.hypot(p.x-connection.point.x,p.y-connection.point.y);
        if(delta<distance&&!connectionProblem(id)){distance=delta;target=id;}}
    }
    if(target&&!connectionProblem(target))connection.target=target;
    for(const [id,card]of cards)card.dataset.dropTarget=String(id===connection.target);
    drawPreview();updateHint();
  }
  function stopWirePan(){if(panFrame!==null)cancelAnimationFrame(panFrame);panFrame=null;wirePointer=null;}
  function panWire(){
    panFrame=null;if(!connection||drag?.kind!=='wire'||!wirePointer)return;
    const rect=viewport.getBoundingClientRect(),x=wirePointer.clientX-rect.left,y=wirePointer.clientY-rect.top;
    const width=viewport.clientWidth,height=viewport.clientHeight;
    if(x<0||y<0||x>width||y>height)return;
    const speed=(p,size)=>p<48?(48-p)/4:p>size-48?-(p-size+48)/4:0;
    const dx=speed(x,width),dy=speed(y,height);if(!dx&&!dy)return;
    offset.x+=dx;offset.y+=dy;camera();aimConnection(wirePointer);
    panFrame=requestAnimationFrame(panWire);
  }
  async function connect(target){
    if(!connection)return;const problem=connectionProblem(target);if(problem){message.textContent=problem;return;}
    const value=connection;cancelConnection(false);
    const saved=await mutate({action:'upsert',kind:'edge',...(value.edge?{id:value.edge.local_id}:{}),
      fields:{source:local(value.from),target:local(target),port:value.port}});
    if(saved&&value.edge){const edge=currentEdges().find(e=>e.id===value.edge.id);if(edge)edgeEditor(edge);}
  }
  function select(id){selected=id;selectedEdge='';editMode=false;detailKey='';updateSelection();drawWires();renderInspector();}
  function renderInspector(){
    if(editMode)return;
    const item=current.structure.nodes.find(n=>n.id===selected);inspector.hidden=!item;
    if(!item){details?.destroy();details=null;inspector.replaceChildren();return;}
    const key=JSON.stringify([item.id,item.status,item.progress,item.artifacts,current.editor.revision]);
    if(key===detailKey)return;detailKey=key;details?.destroy();details=null;
    const heading=node('header','wb-inspector-heading');heading.append(node('h3','',item.label),button('×',()=>{selected='';updateSelection();renderInspector();}));
    inspector.replaceChildren(heading,node('p','',item.goal));
    const actions=node('div','wb-actions');
    if(item.kind==='loop')actions.append(button('进入循环体',()=>enterLoop(item.id),'wb-button wb-primary'));
    else if(current.task.run_id&&current.task.purpose!=='exploration')actions.append(button('打开节点对话',()=>openConversation(item.id),'wb-button wb-primary'));
    if(editable())actions.append(button('编辑',()=>editor(item)),button('复制',()=>mutate({action:'upsert',kind:'node',id:uuid('n_'),
      fields:nodeFields(item,item.label+' 副本')})),button('删除',()=>mutate({action:'delete',kind:'node',id:local(item.id)})));
    if(editable())actions.append(button('前插判断',()=>insertAround(item,'before')),button('后插判断',()=>insertAround(item,'after')));
    inspector.append(actions);
    if(current.task.run_id)details=mountWorkflowNode(inspector,{api,store,view:current,item});
  }
  function nodeFields(item,label=item.label){
    const detail=current.plan.flatMap(g=>g.steps).find(s=>s.id===item.id)?.detail||{};
    return {label,task:item.goal||'',kind:item.kind,config:structuredClone(item.config||{}),
      inputs:detail.inputs||[],outputs:detail.outputs||[],...(detail.model?{model:detail.model}:{})};
  }
  async function insertAround(item,where){
    const id=uuid('check_'),edges=currentEdges().filter(e=>where==='before'?e.to===item.id:e.from===item.id);
    const operations=[{action:'upsert',kind:'node',id,fields:{kind:'judge',label:'判断 '+item.label,task:'',config:{}}}];
    for(const edge of edges)operations.push({action:'upsert',kind:'edge',id:edge.local_id,fields:{
      source:where==='after'?id:local(edge.from),target:where==='before'?id:local(edge.to),port:edge.condition}});
    operations.push({action:'upsert',kind:'edge',fields:{source:where==='before'?id:local(item.id),target:where==='before'?local(item.id):id,port:'always'}});
    await mutate({action:'batch',operations:operations.map(op=>({scope,...op}))});
  }
  function field(parent,label,type='input',value=''){
    const wrapper=node('label','wb-field',label),input=node(type,'wb-input');input.value=value;input.setAttribute('aria-label',label);wrapper.append(input);parent.append(wrapper);return input;
  }
  function selectField(parent,label,options,value){
    const input=field(parent,label,'select');for(const [key,text]of Object.entries(options)){const opt=node('option','',text);opt.value=key;input.append(opt);}input.value=value;return input;
  }
  function editor(item,kind='work',placement=null){
    if(!editable())return;
    cancelConnection();
    editMode=true;details?.destroy();details=null;inspector.hidden=false;inspector.replaceChildren();
    const form=node('form','wb-graph-edit-form');inspector.append(form);kind=item?.kind||kind;
    form.append(node('h3','',item?'编辑节点':'添加'+NAMES[kind]));
    const label=field(form,'名称','input',item?.label||NAMES[kind]),task=field(form,'任务／判断条件','textarea',item?.goal||'');task.rows=5;
    const config=structuredClone(item?.config||{}),fields={};
    if(kind==='work'||kind==='judge'){
      fields.prompt=field(form,'补充 Prompt','textarea',config.prompt||'');fields.prompt.rows=3;
      fields.constraints=field(form,'约束（每行一项）','textarea',(config.constraints||[]).join('\n'));
      fields.serviceTier=selectField(form,'执行速度',{'':'沿用当前设置',fast:'Fast',default:'标准',priority:'Priority'},config.service_tier||'');
    }
    if(kind==='judge'){
      fields.method=selectField(form,'判断方式',{model:'模型判断',expression:'条件表达式',manual:'人工判断'},config.manual?'manual':config.expression?'expression':'model');
      fields.path=field(form,'条件字段','input',config.expression?.path||'inputs.result.value');
      fields.value=field(form,'等于（JSON 值）','input',JSON.stringify(config.expression?.equals??true));
    }
    if(kind==='branch'){
      fields.source=field(form,'判断结果字段','input',config.source||'inputs.check.value');
      fields.cases=field(form,'出口 = 值（每行一项）','textarea',(config.cases||[]).map(c=>c.port+' = '+JSON.stringify(c.equals)).join('\n'));
      fields.fallback=field(form,'默认出口（可选）','input',config.default||'');
    }
    if(kind==='loop'){
      fields.mode=selectField(form,'循环方式',{until:'满足条件后退出',foreach:'逐项处理材料',count:'指定次数'},config.mode||'until');
      fields.until=field(form,'退出条件字段','input',config.until?.path||'results.check.value');
      fields.items=field(form,'材料列表（每行一项）','textarea',(Array.isArray(config.items)?config.items:[]).map(item=>typeof item==='string'?item:JSON.stringify(item)).join('\n'));
      fields.count=field(form,'指定次数','input',config.count??1);fields.count.type='number';fields.count.min='0';
      form.append(node('p','wb-muted','按退出判断、材料列表或指定次数结束；耗时和 Token 仅作统计，不会自动暂停循环。'));
    }
    const detail=item?nodeFields(item):{};
    const inputs=field(form,'输入名称（每行一个，必需）','textarea',(detail.inputs||[]).filter(v=>typeof v==='object'&&v.required!==false).map(v=>v.name).join('\n'));
    const outputs=field(form,'期望产出（每行一项）','textarea',(detail.outputs||[]).join('\n'));
    const save=button('保存',()=>{},'wb-button wb-primary');save.type='submit';
    form.append(save,button('取消',()=>{editMode=false;detailKey='';renderInspector();}));
    form.addEventListener('submit',async event=>{
      event.preventDefault();
      try{
        if(fields.prompt){
          config.prompt=fields.prompt.value;
          config.constraints=fields.constraints.value.split('\n').map(s=>s.trim()).filter(Boolean);
          if(fields.serviceTier.value)config.service_tier=fields.serviceTier.value;
          else delete config.service_tier;
        }
        if(kind==='judge'){
          delete config.expression;delete config.manual;
          if(fields.method.value==='expression')config.expression={path:fields.path.value.trim(),equals:JSON.parse(fields.value.value)};
          if(fields.method.value==='manual')config.manual=true;
          config.criteria=task.value.split('\n').filter(Boolean);
        }
        if(kind==='branch'){
          config.source=fields.source.value.trim();config.default=fields.fallback.value.trim()||null;
          config.cases=fields.cases.value.split('\n').filter(s=>s.trim()).map(line=>{const i=line.indexOf('=');if(i<1)throw new Error('出口格式：名称 = 值');return {port:line.slice(0,i).trim(),equals:JSON.parse(line.slice(i+1).trim())};});
        }
        if(kind==='loop'){
          config.mode=fields.mode.value;config.until={path:fields.until.value.trim(),equals:true};config.count=Number(fields.count.value);
          config.items=fields.items.value.split('\n').filter(s=>s.trim()).map(s=>{try{return JSON.parse(s);}catch{return s;}});
          config.graph??={nodes:[],edges:[]};
        }
        const id=item?local(item.id):uuid('n_');
        const operation={action:'upsert',kind:'node',id,fields:{
          label:label.value.trim(),task:task.value,kind,config,
          inputs:[...(detail.inputs||[]).filter(v=>typeof v==='string'||v.required===false),...inputs.value.split('\n').filter(Boolean).map(name=>({name:name.trim(),required:true}))],
          outputs:outputs.value.split('\n').filter(Boolean)}};
        const saved=await mutate(placement?{action:'batch',operations:[{scope,...operation},
          {action:'positions',scope,positions:{[id]:placement}}]}:operation);
        if(saved){editMode=false;detailKey='';renderInspector();}
      }catch(error){message.textContent=error.message;}
    });
  }
  function edgeEditor(edge){
    cancelConnection();selected='';selectedEdge=edge.id;updateSelection();drawWires();
    editMode=true;details?.destroy();details=null;inspector.hidden=false;inspector.replaceChildren(node('h3','','编辑连线'));
    inspector.append(node('p','wb-muted','拖动连线末端的 ↪ 手柄，可直接改接到另一节点。'),
      button('在画布上改接终点',()=>startConnection(edge.from,edge.condition,edge)));
    const form=node('form','wb-graph-edit-form');inspector.append(form);
    const choices=Object.fromEntries(currentNodes().map(item=>[local(item.id),item.label]));
    const from=selectField(form,'上游节点',choices,local(edge.from)),to=selectField(form,'下游节点',choices,local(edge.to));
    const port=field(form,'出口','input',edge.condition);
    const save=button('保存连线',()=>{});save.type='submit';form.append(save);
    form.append(button('删除连线',async()=>{await mutate({action:'delete',kind:'edge',id:edge.local_id});editMode=false;renderInspector();}),
      button('在线上插入判断',async()=>{await mutate({action:'insert',kind:'node',id:uuid('check_'),edge_id:edge.local_id,
        fields:{kind:'judge',label:'判断',task:'',config:{}}});editMode=false;renderInspector();}));
    form.addEventListener('submit',async event=>{event.preventDefault();await mutate({action:'upsert',kind:'edge',id:edge.local_id,
      fields:{source:from.value,target:to.value,port:port.value.trim()||'always'}});editMode=false;renderInspector();});
  }
  viewport.addEventListener('dragover',event=>event.preventDefault());
  viewport.addEventListener('drop',event=>{event.preventDefault();const kind=event.dataTransfer.getData('application/x-mew-component');if(NAMES[kind])editor(null,kind,canvasPoint(event));});
  viewport.addEventListener('pointerdown',event=>{
    if(event.button!==0||busy)return;
    const output=event.target.closest('[data-output-node]'),handle=event.target.closest('[data-reconnect-edge]');
    if((output||handle)&&editable()){
      event.preventDefault();
      const edge=handle?currentEdges().find(e=>e.id===selectedEdge):null;if(handle&&!edge)return;
      let from=output?.dataset.outputNode||edge.from,port=output?.dataset.port||edge.condition;
      // Enlarged hit targets may overlap at overview zoom; pick the nearest visible outlet.
      if(output){const point=canvasPoint(event),item=currentNodes().find(n=>n.id===from);
        port=ports(item).reduce((best,next)=>Math.abs(anchor(from,next).y-point.y)<Math.abs(anchor(from,best).y-point.y)?next:best,port);}
      startConnection(from,port,edge);
      drag={kind:'wire',x:event.clientX,y:event.clientY,pointerId:event.pointerId,moved:false};
      viewport.setPointerCapture?.(event.pointerId);return;
    }
    if(connection)return;
    if(event.target.closest('button,a,input,textarea,select,.wb-graph-edge-hit'))return;
    const card=event.target.closest('.wb-workflow-card'),id=card?.dataset.nodeId;
    if(id&&!editable())return;
    drag={kind:id?'node':'pan',x:event.clientX,y:event.clientY,id,offset:{...offset},point:id?{...positions[id]}:null,
      pointerId:event.pointerId,moved:false};
    // Capture after the drag threshold so an ordinary click still reaches its card.
  });
  viewport.addEventListener('pointermove',event=>{
    if(connection)aimConnection(event);
    if(!drag||event.pointerId!==drag.pointerId)return;
    const dx=event.clientX-drag.x,dy=event.clientY-drag.y;
    if(!drag.moved&&Math.hypot(dx,dy)>4){drag.moved=true;viewport.setPointerCapture?.(event.pointerId);
      viewport.dataset.panning=String(drag.kind==='pan');if(drag.id){selectedEdge='';cards.get(drag.id).dataset.dragging='true';}}
    if(drag.kind==='wire'&&drag.moved){wirePointer={clientX:event.clientX,clientY:event.clientY};if(panFrame===null)panFrame=requestAnimationFrame(panWire);}
    if(!drag.moved||drag.kind==='wire')return;event.preventDefault();
    if(drag.id){const p={x:drag.point.x+dx/zoom,y:drag.point.y+dy/zoom};positions[drag.id]=p;
      const element=cards.get(drag.id);element.style.left=p.x+'px';element.style.top=p.y+'px';scheduleWires();
    }else{offset={x:drag.offset.x+dx,y:drag.offset.y+dy};camera();}
  });
  viewport.addEventListener('pointerup',async event=>{
    if(!drag||event.pointerId!==drag.pointerId)return;
    const before=drag;drag=null;viewport.dataset.panning='false';
    stopWirePan();
    if(viewport.hasPointerCapture?.(event.pointerId))viewport.releasePointerCapture(event.pointerId);
    if(before.id)delete cards.get(before.id)?.dataset.dragging;
    if(before.moved||before.kind==='wire')suppressClickUntil=performance.now()+300;
    if(before.kind==='wire'){
      if(before.moved)aimConnection(event);
      if(before.moved&&connection?.target)await connect(connection.target);
      else if(before.moved){const problem=connectionProblem(document.elementFromPoint(event.clientX,event.clientY)?.closest('.wb-workflow-card')?.dataset.nodeId);
        cancelConnection();message.textContent=problem||'未连接：把出口拖到高亮节点上，或点击出口后选择目标。';}
    }else if(before.id&&before.moved){drawWires();await mutate({action:'positions',positions:{[local(before.id)]:positions[before.id]}});}
    flushView();
  });
  function cancelGesture(){
    const before=drag;drag=null;
    if(before?.id){positions[before.id]=before.point;const card=cards.get(before.id);if(card){card.style.left=before.point.x+'px';card.style.top=before.point.y+'px';delete card.dataset.dragging;}}
    if(before?.kind==='pan')offset=before.offset;
    if(before&&viewport.hasPointerCapture?.(before.pointerId))viewport.releasePointerCapture(before.pointerId);
    viewport.dataset.panning='false';suppressClickUntil=performance.now()+300;cancelConnection();camera();drawWires();
  }
  viewport.addEventListener('pointercancel',cancelGesture);
  viewport.addEventListener('lostpointercapture',()=>{if(drag)cancelGesture();});
  viewport.addEventListener('click',event=>{
    if(connection&&!event.target.closest('.wb-workflow-card,button')&&!clickSuppressed())cancelConnection();
  });
  viewport.addEventListener('wheel',event=>{
    const progress=event.target.closest('.wb-workflow-progress');
    if(progress&&progress.scrollHeight>progress.clientHeight&&!event.ctrlKey&&!event.metaKey)return;
    event.preventDefault();if(drag||connection)return;const rect=viewport.getBoundingClientRect();
    setZoom(zoom*Math.exp(-event.deltaY*.001),{x:event.clientX-rect.left,y:event.clientY-rect.top});
  },{passive:false});
  container.addEventListener('keydown',event=>{
    if(event.target.closest('.wb-conversation-modal'))return;
    if(event.target.closest('input,textarea,select'))return;
    if((event.ctrlKey||event.metaKey)&&event.key==='z'){event.preventDefault();mutate({action:event.shiftKey?'redo':'undo'});}
    if(event.key==='Escape'){cancelGesture();editMode=false;selected='';selectedEdge='';detailKey='';updateSelection();drawWires();renderInspector();}
  });
  function viewKey(view){return JSON.stringify([scope,{...view.structure,nodes:view.structure.nodes.map(({execution,...item})=>item)},view.editor]);}
  function flushView(){if(queuedView&&!drag&&!connection&&!busy&&!disposed){const view=queuedView;queuedView=null;update(view);}}
  function update(view){
    if(disposed)return;
    view=summarizeLoops(view);
    conversation?.updateView(view);
    const changedOwner=current&&(view.task.id!==current.task.id||view.task.run_id!==current.task.run_id);
    if(changedOwner){queuedView=null;cancelGesture();scope='';selected='';selectedEdge='';editMode=false;detailKey='';renderKey='';zoom=DEFAULT_ZOOM;offset={x:24,y:24};}
    if(drag||connection||busy){queuedView=view;updateActivities(view);return;}
    current=view;if(renderKey!==viewKey(view))render();else{updateActivities(view);renderInspector();}
  }
  return {update,toggleNodes(value=palette.hidden){palette.hidden=!value;onNodesToggle?.(value);},destroy(){
    disposed=true;conversation?.destroy();clearInterval(activityClock);stopWirePan();if(wireFrame!==null)cancelAnimationFrame(wireFrame);details?.destroy();onFocusMode?.(false);}};
}
