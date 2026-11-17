import {node, button, icon} from './dom.js';
import {mountWorkflowCanvas} from './workflow-canvas.js?v=20261117-exploration';
import {layout, routeGraph} from './geometry.js?v=20261102-loop-boundaries';
import {evidence} from './process.js';
import {mountNodeModel} from './node-model.js?v=20261026-model-edit';
import {outlets, nodeForm, edgeForm, conditions, kinds} from './graph-editor.js?v=20261019-node-id';
import {loopRegions, loopBoxes, renderLoopRegion, renderWorkBoard, progressText, businessLabel} from './work-progress.js?v=20261102-progress';

const SEMANTIC = ['label', 'goal', 'milestone_id', 'method', 'checks', 'requirement_ids', 'constraint_ids', 'requirements', 'requirements_hash'];
const FIELD_NAMES = {label:'名称',goal:'目标',milestone_id:'里程碑',method:'指定方法',checks:'验收检查',
  requirement_ids:'要求引用',constraint_ids:'约束引用',requirements:'要求内容',requirements_hash:'要求版本',connections:'连接'};
const canonical = value => JSON.stringify(sort(value));
function sort(value) {
  if (Array.isArray(value)) return value.map(sort);
  return value && typeof value === 'object' ? Object.fromEntries(Object.keys(value).sort().map(key => [key, sort(value[key])])) : value;
}
function facts(plan, item) {
  return {...Object.fromEntries(SEMANTIC.map(key => [key, item[key] ?? null])),
    connections: (plan.edges || []).filter(edge => edge.from === item.id || edge.to === item.id).map(canonical).sort()};
}
export function compareVersions(before, after) {
  const old = new Map(before.nodes.map(item => [item.id, item])), next = new Map(after.nodes.map(item => [item.id, item])), result = [];
  for (const [id, item] of next) {
    if (!old.has(id)) result.push({id, label:item.label, kind:'新增', after:item});
    else {
      const left = facts(before, old.get(id)), right = facts(after, item);
      const fields = Object.keys(left).filter(key => canonical(left[key]) !== canonical(right[key])
        && !(key === 'requirements_hash' && left.requirements && right.requirements));
      if (fields.length) result.push({id, label:item.label, kind:'修改', fields:[...new Set(fields.map(key => FIELD_NAMES[key]))], field_keys:fields, before:left, after:right});
    }
  }
  for (const [id, item] of old) if (!next.has(id)) result.push({id, label:item.label, kind:'删除', before:item});
  return result;
}

// Only routing excludes back edges; the graph still displays every connection.
function layoutEdges(nodes, edges) {
  const outgoing = new Map(nodes.map(item => [item.id, []])), selected = [];
  function reaches(from, to, seen = new Set()) {
    if (from === to) return true;
    if (seen.has(from)) return false; seen.add(from);
    return (outgoing.get(from) || []).some(next => reaches(next, to, seen));
  }
  for (const edge of edges) if (edge.condition!=='loop' && outgoing.has(edge.from) && outgoing.has(edge.to) && !reaches(edge.to, edge.from)) {
    outgoing.get(edge.from).push(edge.to); selected.push([edge.from, edge.to]);
  }
  return selected;
}
export function graphLayout(structure) {
  const nodes = structure.nodes || [], groups = [...(structure.groups || [])], positions = {}, boxes = [];
  for (const item of nodes) if (!groups.some(group => group.id === item.milestone_id)) groups.push({id:item.milestone_id,label:'任务计划'});
  const sizes=nodes.map(item=>({id:item.id,w:270,h:194+Math.max(0,outlets(item,structure.edges).length-1)*24}));
  const calculated=layout(sizes,layoutEdges(nodes,structure.edges || []),150,90);
  for (const item of sizes) positions[item.id]={...calculated[item.id],w:item.w,h:item.h};
  let width=320,height=220;
  for (const group of groups) {
    const members = nodes.filter(item => item.milestone_id === group.id); if (!members.length) continue;
    const bounds=members.map(item=>positions[item.id]);
    const x=Math.min(...bounds.map(p=>p.x))-20,y=Math.min(...bounds.map(p=>p.y))-40;
    const right=Math.max(...bounds.map(p=>p.x+p.w))+20,bottom=Math.max(...bounds.map(p=>p.y+p.h))+20;
    boxes.push({...group,x,y,w:right-x,h:bottom-y});width=Math.max(width,right+40);height=Math.max(height,bottom+40);
  }
  return {positions,groups:boxes,width,height};
}
function svgNode(tag, attributes = {}) {
  const element = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [name,value] of Object.entries(attributes)) element.setAttribute(name,value);
  return element;
}
function renderGraph(container, structure, selectNode, locations=new Map(), editor={}) {
  container.replaceChildren();
  if (!structure.nodes?.length) {
    const empty = node('div','wb-graph-empty');
    empty.append(node('strong','','从对话开始任务'),node('p','wb-muted','直接描述要完成的事情。执行中的分工、依赖和输出会逐步显示在这里。'));
    container.append(empty); return null;
  }
  const computed = graphLayout(structure), stage = node('div','wb-graph-stage');
  for (const [id,position] of locations) if (computed.positions[id]) Object.assign(computed.positions[id],position);
  for (const group of computed.groups) {
    const boxes=structure.nodes.filter(item=>item.milestone_id===group.id).map(item=>computed.positions[item.id]);
    if (!boxes.length) continue;
    group.x=Math.min(...boxes.map(box=>box.x))-20;group.y=Math.min(...boxes.map(box=>box.y))-40;
    group.w=Math.max(...boxes.map(box=>box.x+box.w))+20-group.x;group.h=Math.max(...boxes.map(box=>box.y+box.h))+20-group.y;
  }
  const boxes=Object.values(computed.positions), regions=loopBoxes(loopRegions(structure),computed.positions);
  computed.x=Math.min(0,...boxes.map(box=>box.x-40));computed.y=Math.min(0,...boxes.map(box=>box.y-60));
  computed.width=Math.max(...boxes.map(box=>box.x+box.w))+40-computed.x;
  computed.height=Math.max(...boxes.map(box=>box.y+box.h))+40-computed.y;
  stage.style.width = computed.width + 'px'; stage.style.height = computed.height + 'px';
  for (const group of computed.groups) {
    if (computed.groups.length===1 && group.label==='任务计划') continue;
    const element = node('div','wb-graph-group'); element.append(node('strong','',group.label));
    Object.assign(element.style,{left:group.x+'px',top:group.y+'px',width:group.w+'px',height:group.h+'px'});stage.append(element);
  }
  for(const region of regions) stage.append(renderLoopRegion(region,editor.focusLoop));
  const svg = svgNode('svg',{width:computed.width,height:computed.height,'aria-hidden':'true'});
  svg.style.overflow='visible';
  const displayEdges=[...(structure.edges || [])];
  for(const region of regions) if(!displayEdges.some(edge=>edge.from===region.check_node_id && edge.condition==='loop')) {
    displayEdges.push({from:region.check_node_id,to:region.entry_node_id,condition:'loop',visual_only:true});
  }
  const portMap=new Map(structure.nodes.map(item=>[item.id,outlets(item,displayEdges)]));
  const edges=displayEdges.filter(edge=>computed.positions[edge.from] && computed.positions[edge.to]);
  const connections=edges.map(edge=>{
    const a=computed.positions[edge.from],b=computed.positions[edge.to];
    const ports=portMap.get(edge.from),portIndex=Math.max(0,ports.findIndex(port=>edge.id && port.edgeId===edge.id || port.condition===(edge.condition || 'always') && !port.edgeId));
    return {key:edge.id || `${edge.from}:${edge.to}:${edge.condition}`,start:{x:a.x+a.w,y:a.y+49+portIndex*24},
      end:{x:b.x,y:b.y+49},feedback:edge.condition==='loop'};
  });
  const wires=routeGraph(connections,boxes,{feedbackBounds:[...boxes,...regions]});
  for (const [index,edge] of edges.entries()) {
    const b=computed.positions[edge.to],wire=wires[index];
    const path = svgNode('path',{d:wire.d,'class':'wb-graph-edge'});
    path.dataset.condition = edge.condition || 'always';path.dataset.from=edge.from;path.dataset.to=edge.to;
    path.dataset.feedback=String(wire.feedback);svg.append(path);
    const tip=svgNode('path',{d:`M ${b.x-8} ${b.y+45} L ${b.x} ${b.y+49} L ${b.x-8} ${b.y+53}`,'class':'wb-graph-edge wb-graph-arrow'});
    tip.dataset.condition=path.dataset.condition;tip.dataset.from=edge.from;tip.dataset.to=edge.to;svg.append(tip);
    if (wire.feedback) {
      const label=svgNode('text',{x:(connections[index].start.x+connections[index].end.x)/2,y:wire.channelY-9,
        'class':'wb-graph-return-label','text-anchor':'middle'});
      label.textContent=edge.condition==='loop' ? '↶ 循环继续 · 按结果进入下一批或修正' : '↶ 回连';svg.append(label);
    }
    if(editor.editable && edge.id && !edge.visual_only) {
      const hit=svgNode('path',{d:wire.d,'class':'wb-graph-edge-hit'});
      hit.dataset.edgeId=edge.id;hit.addEventListener('click',event=>{event.stopPropagation();editor.selectEdge(edge);});svg.append(hit);
    }
  }
  const allPoints=wires.flatMap(wire=>wire.points);
  computed.x=Math.min(0,...boxes.map(box=>box.x-40),...regions.map(box=>box.x-20),...allPoints.map(p=>p.x-40));
  computed.y=Math.min(0,...boxes.map(box=>box.y-60),...regions.map(box=>box.y-20),...allPoints.map(p=>p.y-40));
  computed.width=Math.max(...boxes.map(box=>box.x+box.w),...regions.map(box=>box.x+box.w),...allPoints.map(p=>p.x))+40-computed.x;
  computed.height=Math.max(...boxes.map(box=>box.y+box.h),...regions.map(box=>box.y+box.h),...allPoints.map(p=>p.y))+40-computed.y;
  svg.setAttribute('width',computed.width);svg.setAttribute('height',computed.height);
  stage.append(svg);
  for (const item of structure.nodes) {
    const box = computed.positions[item.id], element = node('article','wb-graph-node');
    element.tabIndex=0;element.setAttribute('role','group');
    element.addEventListener('click',event=>{if(!event.target.closest?.('.wb-node-port')) selectNode(item.id);});
    element.addEventListener('keydown',event=>{if(event.target===element && ['Enter',' '].includes(event.key)) {event.preventDefault();selectNode(item.id);}});
    element.dataset.nodeId = item.id; element.dataset.status = item.status || '待执行';
    element.setAttribute('aria-label',`${editor.runtime ? businessLabel(item.label) : item.label+' · ID: '+item.id} · ${item.status || '待执行'}`);
    element.setAttribute('aria-pressed','false');
    const current=regions.some(region=>region.active_node_ids?.includes(item.id));
    element.dataset.current=String(current);
    element.dataset.problem=String(regions.some(region=>region.issues?.some(issue=>issue.node_id===item.id && issue.state!=='resolved')));
    const report=item.work_report;
    const title=node('strong','wb-node-title');title.append(node('span','wb-node-chevron',current?'●':'⌄'),
      node('span','',editor.runtime && report?.label ? report.label : businessLabel(item.label)));
    const ports=node('div','wb-node-io'), outputLabels=node('div','wb-node-output-labels');
    const outputs=portMap.get(item.id);
    outputLabels.append(...outputs.map(port=>node('span','',port.label)));
    ports.append(node('span','','输入'),outputLabels);
    const goal=node('span','wb-node-goal-preview',report?.summary || report?.work_object ||
      (editor.runtime ? current ? '正在处理，等待这一步汇报进展。' :
        item.status==='已完成' ? '这一步已执行完，暂无具体情况汇报。' : '具体处理情况将在执行后汇报。' : item.goal || '等待任务输入'));
    const status=node('span','wb-node-widget');status.append(node('span','','状态'),node('span','wb-node-status',item.status || '待执行'));
    const position=node('span','wb-node-work-position',report?.progress ? progressText(report.progress) : current ? '正在这里' : '');
    element.append(title,ports);
    if(!editor.runtime) {
      const identity=node('code','wb-node-id wb-node-id-card',`ID: ${item.id}`);identity.title=item.id;element.append(identity);
    }
    element.append(position,goal,status);
    for (const [direction,port,index] of [['in',{},0],...outputs.map((port,index)=>['out',port,index])]) {
      const handle = button('',event=>editor.portClick?.(item,direction,port,event),'wb-node-port wb-node-port-'+direction);
      handle.dataset.nodeId=item.id;handle.dataset.direction=direction;handle.dataset.condition=port.condition || '';
      handle.style.top=(43+index*24)+'px';handle.disabled=!editor.editable;
      handle.setAttribute('aria-label',`${editor.runtime ? businessLabel(item.label) : item.label} · ${direction==='in'?'输入':port.label+'出口'}`);
      handle.title=direction==='in'?'连接到此节点':'拖动到下游输入点，也可先点击出口再点击输入';
      element.append(handle);
    }
    Object.assign(element.style,{left:box.x+'px',top:box.y+'px',width:box.w+'px',height:box.h+'px'});stage.append(element);
  }
  stage.style.width=computed.width+'px';stage.style.height=computed.height+'px';container.append(stage);
  return {...computed,stage,regions};
}

export function mountStructure(container, options) {
  let workflow=false, mounted=mountLegacyStructure(container,options);
  return {update(view) {
    const next=view.task.mode==='workflow';
    if(next!==workflow){mounted.destroy();workflow=next;mounted=next?mountWorkflowCanvas(container,options):mountLegacyStructure(container,options);}
    mounted.update(view);
  },toggleNodes(value){mounted.toggleNodes(value);},destroy(){mounted.destroy();}};
}

function mountLegacyStructure(container, {api, store, openStep, onCompose, onEdit, onFocusMode, onNodesToggle, onMutate}) {
  const toolbar = node('div','wb-graph-toolbar'), heading = node('span','wb-canvas-label','编排图'), count = node('span','wb-graph-count');
  const workBoard=node('section','wb-work-board');workBoard.hidden=true;
  const graph = node('div','wb-graph'), compare = node('details','wb-version-compare'), controls = node('div','wb-version-selects'), result = node('div','wb-version-results');
  const inspector = node('section','wb-node-inspector'), body = node('div','wb-graph-body'); inspector.hidden = true;
  const library=node('section','wb-node-library'), libraryHeader=node('header','wb-panel-header'), search=node('input','wb-node-search'), nodeList=node('div','wb-node-list');
  library.hidden=true;library.setAttribute('aria-label','节点列表');search.placeholder='搜索节点名称或 ID';search.setAttribute('aria-label','搜索节点');
  const closeLibrary=button('×',()=>toggleNodes(false),'wb-close');closeLibrary.setAttribute('aria-label','关闭节点列表');
  libraryHeader.append(node('strong','','节点'),closeLibrary);library.append(libraryHeader,search,nodeList);
  inspector.setAttribute('aria-label','节点详情');
  graph.tabIndex = 0; graph.setAttribute('aria-label','编排画布，可滚动或拖动画布空白处');
  let current = null, owner = '', graphKey = '', versionsKey = '', selected = '', inspectorKey = '', drawing = null, zoom = 1, expanded = false, pan = null;
  let modelCard=null;
  let offset={x:0,y:0}, fitted=false, renderedStructure=null;
  let busy=false, connection=null, wiring=null, editing=false, positionsKey='';
  const message=node('p','wb-graph-message');message.hidden=true;message.setAttribute('role','status');
  const locations=new Map();
  const editable=()=>!!onMutate && !!current?.editor?.editable && !current?.task.readonly && !current?.task.run_id;
  const add=button('+ 添加节点',()=>editNode());
  const addEdge=button('连接节点',()=>editEdge());
  const arrange=button('自动整理',async()=>{
    locations.clear();redraw();fitGraph();
    if(editable()) await mutate({action:'positions',positions:{}},false);
  });
  const minus = button('−',()=>setZoom(zoom-.1)), plus = button('+',()=>setZoom(zoom+.1));
  minus.setAttribute('aria-label','缩小画布'); plus.setAttribute('aria-label','放大画布');
  const scale = button('100%',()=>setZoom(1)); scale.title = '恢复原始大小';
  const fit = button('适应',()=>fitGraph()); fit.setAttribute('aria-label','适应画布');
  const expand = button('',()=>setExpanded(!expanded)); expand.setAttribute('aria-label','专注模式');expand.title='专注模式';expand.setAttribute('aria-pressed','false');
  expand.append(icon(['M8 3H3v5 M16 3h5v5 M3 16v5h5 M21 16v5h-5']));
  const compose = button('✦ AI 构图',()=>{setExpanded(false);onCompose?.();},'wb-button wb-primary'); compose.hidden = !onCompose;
  const viewportControls = node('div','wb-graph-tools'); viewportControls.append(minus,scale,plus,fit,expand);
  toolbar.append(heading,add,addEdge,arrange); body.append(graph,inspector);
  const from = node('select'), to = node('select'); from.setAttribute('aria-label','比较的原计划版本');to.setAttribute('aria-label','比较的新计划版本');
  controls.append(from,node('span','','→'),to); compare.append(node('summary','','比较计划版本'),controls,result);
  container.replaceChildren(library,toolbar,workBoard,body,count,viewportControls,compare,message);
  function tell(text,error=false) {message.textContent=text;message.hidden=!text;message.dataset.error=String(error);}
  function redraw() {
    drawing=renderGraph(graph,renderedStructure,selectNode,locations,{editable:editable() && !busy,selectEdge:editEdge,portClick,
      runtime:!!current?.task.run_id,focusLoop});
    paintSelection();paintCamera();
  }
  async function mutate(operation,close=true,expected=current) {
    if(!editable() || busy) return false;
    const before=expected, identity=owner;
    const positions=operation.action==='positions' ? null : Object.fromEntries(Object.entries(drawing?.positions || {}).map(([id,p])=>[id,{x:p.x,y:p.y}]));
    busy=true;add.disabled=addEdge.disabled=arrange.disabled=true;tell('正在保存…');
    try {
      const result=await onMutate(before,operation,positions);
      if(current.task.conversation_id!==before.task.conversation_id || owner!==identity && before.task.id) return false;
      if(operation.action==='upsert' && operation.kind==='node' && !operation.id && result?.changed_ids?.[0]) {
        selected=result.changed_ids[0];
        const box=drawing?.positions[selected];
        if(box) {setZoom(Math.max(.65,zoom));offset={x:Math.max(150,(graph.clientWidth-300)/2)-(box.x+box.w/2)*zoom,y:graph.clientHeight/2-(box.y+box.h/2)*zoom};paintCamera();}
      }
      if(close) {editing=false;inspectorKey='';renderInspector();}
      tell('已保存');return true;
    } catch(error) {
      if(owner===identity) {
        tell(error.message || '保存失败，请重试。',true);
        if(operation.action==='positions') {locations.clear();for(const [id,point] of Object.entries(current.editor?.positions || {})) locations.set(id,point);}
      }
      return false;
    }
    finally {busy=false;add.disabled=false;addEdge.disabled=(current?.structure?.nodes?.length || 0)<2;arrange.disabled=false;if(renderedStructure) redraw();}
  }
  function cancelEdit() {editing=false;inspectorKey='';renderInspector();}
  function editNode(item=null) {
    if(!editable() || busy) return;
    editing=true;inspector.hidden=false;
    const detail=(current.plan || []).flatMap(group=>group.steps || []).find(step=>step.id===item?.id)?.detail;
    const basis=current;
    inspector.replaceChildren(nodeForm(item,detail,op=>mutate(op,true,basis),cancelEdit));
    inspector.querySelector('input')?.focus();
  }
  function editEdge(edge=null) {
    if(!editable() || busy) return;
    editing=true;inspector.hidden=false;
    const basis=current;
    inspector.replaceChildren(edgeForm(edge,current.structure.nodes,op=>mutate(op,true,basis),cancelEdit));
    if(edge?.id) inspector.append(button('删除连线',()=>mutate({action:'delete',id:edge.id},true,basis),'wb-button wb-delete'));
  }
  function portClick(item,direction,port,event) {
    event?.stopPropagation?.();
    if(!editable() || busy) return;
    if(direction==='out') {
      connection={id:item.id,condition:port.condition,view:current};tell(`从「${item.label} · ${port.label}」连接：点击下游输入点，Esc 取消。`);
    } else if(connection) connect(item.id);
  }
  async function connect(to) {
    if(!connection) return;
    const from=connection;connection=null;
    if(from.id===to) {tell('请选择另一个节点的输入。',true);return;}
    await mutate({action:'upsert',kind:'edge',fields:{data:{from_node:from.id,to_node:to,condition:from.condition}}},true,from.view);
  }
  function paintCamera() {
    if (drawing) drawing.stage.style.transform=`translate(${offset.x}px, ${offset.y}px) scale(${zoom})`;
    graph.style.backgroundPosition=`${offset.x}px ${offset.y}px`;
    graph.style.backgroundSize=`${24*zoom}px ${24*zoom}px`;
  }
  function setZoom(value,anchor={x:(graph.clientWidth || 0)/2,y:(graph.clientHeight || 0)/2}) {
    const previous = zoom; zoom = Math.max(.1,Math.min(2,Math.round(value*100)/100));
    scale.textContent = Math.round(zoom*100)+'%'; minus.disabled = zoom<=.1; plus.disabled = zoom>=2;
    if(previous!==zoom) offset={x:anchor.x-(anchor.x-offset.x)*zoom/previous,y:anchor.y-(anchor.y-offset.y)*zoom/previous};
    paintCamera();
  }
  function fitGraph() {
    if (!drawing || !graph.clientWidth || !graph.clientHeight) return;
    setZoom(Math.min(1,(graph.clientWidth-80)/drawing.width,(graph.clientHeight-100)/drawing.height));
    offset={x:(graph.clientWidth-drawing.width*zoom)/2-drawing.x*zoom,y:(graph.clientHeight-drawing.height*zoom)/2-drawing.y*zoom};paintCamera();fitted=true;
  }
  function focusNode(id) {
    const box=drawing?.positions[id];if(!box) return;
    setZoom(Math.max(.85,Math.min(1,zoom)));
    offset={x:Math.max(210,Math.min(420,(graph.clientWidth || 800)*.3))-(box.x+box.w/2)*zoom,
      y:Math.min(320,(graph.clientHeight || 480)*.42)-(box.y+box.h/2)*zoom};paintCamera();
    for(const element of graph.querySelectorAll('.wb-graph-node')) element.dataset.located=String(element.dataset.nodeId===id);
  }
  function focusLoop(id) {
    const box=drawing?.regions.find(row=>row.id===id);if(!box) return;
    setZoom(Math.min(1,((graph.clientWidth || 800)-80)/box.w,((graph.clientHeight || 480)-80)/box.h));
    offset={x:((graph.clientWidth || 800)-box.w*zoom)/2-box.x*zoom,
      y:((graph.clientHeight || 480)-box.h*zoom)/2-box.y*zoom};paintCamera();
  }
  function setExpanded(value) {
    expanded = value; container.dataset.expanded = String(value);onFocusMode?.(value);
    expand.setAttribute('aria-pressed',String(value));expand.setAttribute('aria-label',value?'退出专注模式':'专注模式');
    if (value) graph.focus();
  }
  container.addEventListener('keydown',event=>{
    if(event.key==='Escape' && (connection || editing)) {event.preventDefault();event.stopPropagation();connection=null;wiring=null;tell('');cancelEdit();}
    else if (event.key==='Escape' && expanded) {event.preventDefault();event.stopPropagation();setExpanded(false);expand.focus();}
    else if (event.key==='Escape' && selected) {event.preventDefault();event.stopPropagation();selected='';renderInspector();graph.focus();}
  });
  graph.addEventListener('pointerdown',event=>{
    if (event.button!==0 && event.button!==1) return;
    const port=event.target.closest('.wb-node-port');
    if(port?.dataset.direction) {
      if(port.dataset.direction==='out' && editable() && !busy) {
        connection={id:port.dataset.nodeId,condition:port.dataset.condition,view:current};
        wiring={x:event.clientX,y:event.clientY,moved:false,port};
        graph.setPointerCapture(event.pointerId);event.preventDefault();
      }
      return;
    }
    if(event.target.closest('.wb-graph-edge-hit')?.dataset.edgeId) return;
    const control=event.target.closest('button');
    if (event.button===0 && control && !control.dataset.nodeId) return;
    const id=event.button===0?event.target.closest('.wb-graph-node')?.dataset.nodeId:null;
    if (!id) graph.focus({preventScroll:true});
    pan={x:event.clientX,y:event.clientY,left:offset.x,top:offset.y,id,position:id?{...drawing.positions[id]}:null,moved:false,view:current};
    event.preventDefault();graph.setPointerCapture(event.pointerId); graph.dataset.panning='true';
  });
  graph.addEventListener('pointermove',event=>{
    if(wiring && drawing) {
      if(Math.hypot(event.clientX-wiring.x,event.clientY-wiring.y)>3) wiring.moved=true;
      const rect=graph.getBoundingClientRect(),port=wiring.port.getBoundingClientRect();
      let preview=graph.querySelector('.wb-wire-preview');
      if(!preview) {preview=svgNode('path',{'class':'wb-graph-edge wb-wire-preview'});drawing.stage.querySelector('svg').append(preview);}
      const x=(port.left+port.width/2-rect.left-offset.x)/zoom,y=(port.top+port.height/2-rect.top-offset.y)/zoom;
      const endX=(event.clientX-rect.left-offset.x)/zoom,endY=(event.clientY-rect.top-offset.y)/zoom;
      preview.setAttribute('d',`M ${x} ${y} C ${x+70} ${y} ${endX-70} ${endY} ${endX} ${endY}`);return;
    }
    if (!pan) return;
    const dx=event.clientX-pan.x,dy=event.clientY-pan.y;
    if (Math.hypot(dx,dy)>3) pan.moved=true;
    if (pan.id && pan.moved) {
      locations.set(pan.id,{x:pan.position.x+dx/zoom,y:pan.position.y+dy/zoom});
      redraw();renderInspector();
    } else if (!pan.id) {offset={x:pan.left+dx,y:pan.top+dy};paintCamera();}
  });
  graph.addEventListener('pointerup',async event=>{
    if(wiring) {
      const moved=wiring.moved;wiring=null;graph.querySelector('.wb-wire-preview')?.remove();
      if(moved) {
        const target=document.elementFromPoint(event.clientX,event.clientY)?.closest('.wb-node-port-in');
        if(target) await connect(target.dataset.nodeId);
        else {connection=null;tell('拖到下游节点左侧的输入点即可连接。');}
      } else tell('点击下游节点的输入点完成连接，Esc 取消。');
      return;
    }
    const moved=pan?.id && pan.moved,basis=pan?.view;
    if (pan?.id) {
      if (!pan.moved) selectNode(pan.id);
      Array.from(graph.querySelectorAll('.wb-graph-node')).find(element=>element.dataset.nodeId===pan.id)?.focus();
    }
    pan=null;graph.dataset.panning='false';
    if(moved && editable()) await mutate({action:'positions',positions:Object.fromEntries(locations)},false,basis);
  });
  for (const name of ['pointercancel','lostpointercapture']) graph.addEventListener(name,()=>{pan=null;wiring=null;graph.querySelector('.wb-wire-preview')?.remove();graph.dataset.panning='false';});
  graph.addEventListener('wheel',event=>{
    event.preventDefault();const rect=graph.getBoundingClientRect();
    setZoom(zoom*Math.exp(-event.deltaY*.0015),{x:event.clientX-rect.left,y:event.clientY-rect.top});
  },{passive:false});
  const resize=typeof ResizeObserver==='function'?new ResizeObserver(()=>{if (!fitted) fitGraph();}):null;resize?.observe(graph);
  function toggleNodes(value=library.hidden) {library.hidden=!value;onNodesToggle?.(value);if(value) search.focus();}
  function renderNodeList() {
    const query=search.value.trim().toLowerCase();nodeList.replaceChildren();
    for (const item of current?.structure?.nodes || []) {
      if (query && !`${item.label} ${item.id}`.toLowerCase().includes(query)) continue;
      const entry=button('',()=>{
        toggleNodes(false);
        selectNode(item.id);const box=drawing?.positions[item.id];
        if (box) {
          // Large real workflows may fit below 20%; search should reveal a
          // readable node in the part of the canvas not covered by details.
          const covered=inspector.hidden ? 0 : (inspector.offsetWidth || 0);
          const width=Math.max(200,graph.clientWidth-covered);
          setZoom(Math.max(zoom,Math.min(.8,(width-32)/box.w)));
          offset={x:width/2-(box.x+box.w/2)*zoom,y:graph.clientHeight/2-(box.y+box.h/2)*zoom};paintCamera();
        }
      },'wb-node-list-item');
      entry.append(node('strong','',item.label),node('code','wb-node-id',`ID: ${item.id}`),node('span','',item.status || '待执行'));nodeList.append(entry);
    }
    if (!nodeList.children.length) nodeList.append(node('p','wb-muted',query?'没有匹配的节点':'暂无节点'));
  }
  search.addEventListener('input',renderNodeList);
  function selectNode(id) { selected=id;editing=false;inspectorKey=''; renderInspector(); }
  function paintSelection() {
    graph.dataset.selected=String(!!selected);
    for(const path of graph.querySelectorAll('.wb-graph-edge')) path.dataset.highlight=String(path.dataset.from===selected || path.dataset.to===selected);
    for(const element of graph.querySelectorAll('.wb-graph-node')) element.setAttribute('aria-pressed',String(element.dataset.nodeId===selected));
  }
  function renderInspector() {
    if(editing) return;
    const item = current?.structure?.nodes?.find(row=>row.id===selected);
    if(!item) selected='';paintSelection();
    const step = (current.plan || []).flatMap(group=>group.steps || []).find(row=>row.id===selected);
    const nextKey = canonical([owner,item,step,current.structure,current.task.status,current.task.readonly,current.editor]);
    if (inspectorKey===nextKey) return; inspectorKey=nextKey;
    modelCard?.destroy();modelCard=null;
    inspector.hidden = !item; inspector.replaceChildren(); if (!item) return;
    const title = node('div','wb-inspector-heading');
    const close = button('×',()=>{selected='';renderInspector();},'wb-close'); close.setAttribute('aria-label','关闭节点详情');
    title.append(node('h3','',item.label),close);
    inspector.append(title,node('code','wb-node-id',`ID: ${item.id}`),node('span','wb-status',item.status || '待执行'));
    if(step?.status_reason || item.status_reason) inspector.append(node('p','wb-node-status-reason',step?.status_reason || item.status_reason));
    const detail = step?.detail || item;
    if(api && store && current.task.mode!=='team') {
      const captured=current;
      modelCard=mountNodeModel(inspector,{api,store,view:captured,item,
        onDraftSave:operation=>onMutate(captured,operation,null)});
    }
    if(item.kind) inspector.append(node('span','wb-node-kind',kinds[item.kind] || item.kind));
    if (detail.goal) inspector.append(node('p','wb-node-goal',detail.goal));
    for (const [key,label] of [['inputs','输入'],['outputs','预期产出'],['requirements','验收要求'],['constraints','约束']]) {
      if (!detail[key]?.length) continue;
      const list=node('ul','wb-node-connections');
      for (const value of detail[key]) list.append(node('li','',typeof value==='string'?value:value.body || value.name || value.description || value.title || ''));
      inspector.append(node('strong','',label),list);
    }
    if (detail.sources?.length) {
      const sources=node('details','wb-node-sources');sources.append(node('summary','','对话依据'));
      for (const source of detail.sources) if (source.quote) sources.append(node('blockquote','',source.quote));
      inspector.append(sources);
    }
    if (detail.artifacts?.length) {
      const outputs = node('ul','wb-node-connections');
      for (const artifact of detail.artifacts) outputs.append(node('li','',artifact.name || artifact.path || '产物'));
      inspector.append(node('strong','','产物'),outputs);
    }
    const connections = (current.structure.edges || []).filter(edge=>edge.from===selected || edge.to===selected);
    if (connections.length) {
      const links = node('ul','wb-node-connections');
      for (const edge of connections) {
        const incoming = edge.to===selected, other = current.structure.nodes.find(row=>row.id===(incoming?edge.from:edge.to));
        const otherId=incoming?edge.from:edge.to;
        const label=`${incoming?'←':'→'} ${other?.label || otherId} [${otherId}]${edge.condition && edge.condition!=='always'?' · '+(conditions[edge.condition] || edge.condition):''}`;
        const row=node('li');row.append(editable() && edge.id ? button(label,()=>editEdge(edge),'wb-connection-link') : node('span','',label));links.append(row);
      }
      inspector.append(links);
    }
    if(editable()) {
      inspector.append(button('编辑节点',()=>editNode(item),'wb-button wb-primary'));
      const remove=button('删除节点',()=>{
        const confirm=node('div','wb-delete-confirm');
        confirm.append(node('p','',`删除「${item.label}」及其 ${connections.length} 条连线？`),
          button('确定删除',()=>mutate({action:'delete',id:item.id}),'wb-button wb-delete'),button('取消',()=>confirm.remove()));
        remove.replaceWith ? remove.replaceWith(confirm) : inspector.append(confirm);
      },'wb-button wb-delete');inspector.append(remove);
    }
    if (onEdit && current.task.design_stage && !current.task.readonly) inspector.append(button('在对话中修改',()=>onEdit(item),'wb-button wb-primary'));
    if (openStep && step && !current.task.design_stage) inspector.append(button('查看步骤与产物',()=>{
      const id=selected;selected='';renderInspector();setExpanded(false);openStep(id);
    }));
  }
  function compareSelected() {
    const versions = current?.structure.versions || [], before = versions.find(row => String(row.version)===from.value), after = versions.find(row => String(row.version)===to.value);
    result.replaceChildren(); if (!before || !after) return;
    const changes = compareVersions(before,after);
    result.append(node('p','wb-muted',changes.length ? `${changes.length} 个步骤发生变化` : '两个版本的步骤与连接相同'));
    for (const change of changes) {
      const row = node('article','wb-version-change');row.dataset.change=change.kind;
      row.append(node('strong','',change.kind+' · '+change.label));
      if (change.fields?.length) row.append(node('p','wb-muted',change.fields.join('、')));
      const humanFacts = facts => Object.fromEntries(Object.entries(facts || {}).filter(([key]) => key !== 'requirements_hash'
        && (!change.field_keys || change.field_keys.includes(key))).map(([key,value]) => [FIELD_NAMES[key] || key,value]));
      evidence(row,'查看变化内容',{'原版本':humanFacts(change.before),'新版本':humanFacts(change.after)});result.append(row);
    }
  }
  from.addEventListener('change',compareSelected);to.addEventListener('change',compareSelected);
  return {toggleNodes, destroy(){resize?.disconnect();modelCard?.destroy();}, update(view) {
    const startingRun=current?.task.id===view.task.id && !current.task.run_id && !!view.task.run_id && !view.task.readonly;
    current = view; const nextOwner = JSON.stringify([view.task.conversation_id,view.task.id,view.task.run_id]);
    if (owner !== nextOwner) {
      owner=nextOwner;graphKey=versionsKey=positionsKey='';compare.open=false;from.value=to.value='';editing=false;connection=null;wiring=null;tell('');
      if (!startingRun) {selected='';zoom=1;offset={x:0,y:0};fitted=false;locations.clear();setExpanded(false);search.value='';}
    }
    const original=view.structure || {nodes:[],edges:[],groups:[],versions:[]};
    if(view.editor && positionsKey!==canonical(view.editor.positions || {})) {
      positionsKey=canonical(view.editor.positions || {});locations.clear();
      for(const [id,point] of Object.entries(view.editor.positions || {})) locations.set(id,point);
    }
    const steps=new Map((view.plan || []).flatMap(group=>group.steps || []).map(step=>[step.id,step]));
    const structure={...original,nodes:original.nodes.map(item=>({...item,goal:steps.get(item.id)?.detail?.goal || item.goal}))};
    heading.textContent=structure.phase==='saved'?'已保存编排':view.task.design_stage?'编排草稿':view.task.run_id?'运行编排':'编排图';
    renderedStructure=structure;
    renderWorkBoard(workBoard,{...view,structure},{focusNode,focusLoop});
    add.hidden=addEdge.hidden=!editable();arrange.hidden=!structure.nodes.length;
    addEdge.disabled=busy || structure.nodes.length<2;
    count.textContent = structure.nodes.length ? `${structure.nodes.length} 节点 · ${(structure.edges || []).length} 连接` : '';
    if (structure.unresolved_edges) count.textContent+=` · ${structure.unresolved_edges} 条连接待补全`;
    compose.hidden = !onCompose || !!view.task.readonly || !!view.task.run_id || view.task.design_revision == null;
    const key=canonical([structure.nodes,structure.edges,structure.groups,structure.loops,view.editor?.positions,editable()]);
    if (graphKey!==key) {
      const focused=document.activeElement?.dataset?.nodeId;graphKey=key;redraw();setZoom(zoom);
      if (!drawing) graph.querySelector('.wb-graph-empty')?.append(compose);
      if (!fitted) {
        fitGraph();
        const active=structure.loops?.flatMap(row=>row.active_node_ids || [])[0];
        if(active) focusNode(active);
      }
      renderNodeList();
      if (focused) Array.from(graph.querySelectorAll('.wb-graph-node')).find(row=>row.dataset.nodeId===focused)?.focus();
    }
    renderInspector();
    const versions=structure.versions || [], nextVersions=canonical(versions);
    compare.hidden=versions.length<2;
    if (versionsKey!==nextVersions) {
      versionsKey=nextVersions;const oldFrom=from.value,oldTo=to.value;
      for (const select of [from,to]) select.replaceChildren(...versions.map(version=>{const option=node('option','',`计划 v${version.version}`);option.value=String(version.version);return option;}));
      from.value=versions.some(row=>String(row.version)===oldFrom)?oldFrom:String(versions.at(-2)?.version || versions[0]?.version || '');
      to.value=versions.some(row=>String(row.version)===oldTo)?oldTo:String(versions.at(-1)?.version || '');
      compareSelected();
    }
  }};
}
