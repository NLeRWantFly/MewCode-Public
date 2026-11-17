// Pointer behavior in an in-memory DOM; visual hit targets are also checked in Chromium.
const assert=require('node:assert/strict');
const {pathToFileURL}=require('node:url');
const path=require('node:path');
const {setup,Element}=require('../workbench_dom.cjs');
const all=(root,predicate)=>root.children.flatMap(child=>[...(predicate(child)?[child]:[]),...all(child,predicate)]);
const has=(el,name)=>el.className.split(' ').includes(name);
const dataName=name=>name.replace(/-([a-z])/g,(_,c)=>c.toUpperCase());
const setAttribute=Element.prototype.setAttribute;
Element.prototype.setAttribute=function(key,value){setAttribute.call(this,key,value);if(key==='class')this.className=String(value);};
function matches(el,selector){return selector.split(',').some(part=>{
  part=part.trim();if(part.startsWith('.'))return has(el,part.slice(1));
  if(part.startsWith('[data-'))return el.dataset[dataName(part.slice(6,-1))]!==undefined;
  return el.tagName.toLowerCase()===part;
});}
Element.prototype.closest=function(selector){for(let el=this;el;el=el.parentNode)if(matches(el,selector))return el;return null;};
Element.prototype.getBoundingClientRect=function(){return {left:0,top:0,width:this.clientWidth||1000,height:this.clientHeight||800};};
Element.prototype.setPointerCapture=function(id){this.captured=id;};
Element.prototype.hasPointerCapture=function(id){return this.captured===id;};
Element.prototype.releasePointerCapture=function(){this.captured=null;};
let now=0,frames=[];
global.performance={now:()=>now};
global.requestAnimationFrame=fn=>(frames.push(fn),frames.length);
global.cancelAnimationFrame=()=>{};
const frame=()=>{const batch=frames;frames=[];batch.forEach(fn=>fn());};
const evt=(target,x=0,y=0)=>({target,clientX:x,clientY:y,button:0,pointerId:1,stopPropagation(){}});
const clone=value=>structuredClone(value);

(async()=>{
  const {mountWorkflowCanvas}=await import(pathToFileURL(path.resolve(__dirname,'../../frontend/static/workbench/workflow-canvas.js')));
  const {summarizeLoops}=await import(pathToFileURL(path.resolve(__dirname,'../../frontend/static/workbench/workflow-summary.js')));
  let cases=0;
  {
    const view={structure:{nodes:[
      {id:'loop',kind:'loop',status:'进行中',iteration:[],execution:{phase:'starting'}},
      {id:'loop/a',scope:'loop',kind:'work',label:'A',status:'进行中',iteration:[1],
        progress:[1,3,5,7].map(at=>({at,text:String(at)})),artifacts:[{id:'shared'}],
        execution:{phase:'reasoning',text:'模型正在思考',active:true,at:9}},
      {id:'loop/b',scope:'loop',kind:'work',label:'B',status:'进行中',iteration:[1],
        progress:[2,4,6].map(at=>({at,text:String(at)})),artifacts:[{id:'shared'},{id:'unique'}],
        execution:{phase:'waiting_node',text:'等待节点回复',active:false,at:10}},
      {id:'loop2/work',scope:'loop2',kind:'work',progress:[{at:99,text:'unrelated'}]}]}};
    const original=clone(view),shown=summarizeLoops(view),loop=shown.structure.nodes[0];
    assert.deepEqual(loop.progress.map(row=>row.at),[3,4,5,6,7],'parent shows only the latest five child updates');
    assert.deepEqual(loop.artifacts.map(row=>row.id),['shared','unique']);
    assert.match(loop.activity,/第 2 轮/);assert.match(loop.execution.text,/A · 模型正在思考/);
    assert.match(loop.execution.detail,/B · 等待节点回复/);
    assert.deepEqual(view,original,'display aggregation cannot mutate execution state');
    assert.deepEqual(summarizeLoops(shown),shown,'queued view refreshes do not duplicate summaries');
    view.structure.nodes[0].execution={phase:'paused',text:'已暂停',active:false};
    assert.equal(summarizeLoops(view).structure.nodes[0].execution.phase,'paused');cases++;
  }
  {
    const view={structure:{nodes:[
      {id:'outer',kind:'loop',status:'进行中',iteration:[]},
      {id:'outer/inner',scope:'outer',kind:'loop',status:'进行中',iteration:[2]},
      {id:'outer/inner/work',scope:'outer/inner',kind:'work',label:'处理材料',status:'进行中',iteration:[2,4],
        progress:[{at:10,text:'已有结果'}],execution:{phase:'waiting_node',text:'等待节点回复',active:false}}]}};
    const [outer,inner]=summarizeLoops(view).structure.nodes;
    assert.equal(outer.progress.length,1);assert.equal(inner.progress.length,1);
    assert.match(outer.activity,/第 3 轮/);assert.match(inner.activity,/第 5 轮/);
    assert.equal(outer.execution.phase,'waiting_node');assert.equal(outer.execution.active,false);
    view.structure.nodes[2]={id:'outer/inner/work',scope:'outer/inner',kind:'work',label:'处理材料',status:'未开始',iteration:[]};
    const reset=summarizeLoops(view).structure.nodes[0];
    assert.ok(!reset.progress?.length,'a reset round must not keep an old child update');
    assert.equal(reset.execution.phase,'waiting_children');
    view.structure.nodes[0].status='已完成';view.structure.nodes[0].execution={phase:'completed',text:'已交付',active:false};
    assert.equal(summarizeLoops(view).structure.nodes[0].execution.phase,'completed');cases++;
  }
  function fixture(){
    const doc=setup(),root=doc.createElement('div');doc.body.append(root);
    const nodes=['a','b','c','check'].map(id=>({id,label:id,scope:'',status:'未开始',kind:id==='check'?'judge':'work',config:{},progress:[],artifacts:[]}));
    let state={task:{id:'task',run_id:'run'},structure:{nodes,edges:[{id:'ab',local_id:'ab',from:'a',to:'b',condition:'always',scope:''}]},
      plan:[{steps:nodes.map(n=>({id:n.id,detail:{inputs:[],outputs:[]}}))}],
      editor:{revision:1,basis:'v1',positions:{a:{x:100,y:100},b:{x:600,y:100},c:{x:600,y:550},check:{x:100,y:550}},has_draft:false}};
    const calls=[],store={generation:1,state:{scope:{}},accept(){},refresh(){}};
    const api={get:async()=>({attempts:[]}),post:async()=>({})};
    const canvas=mountWorkflowCanvas(root,{api,store,onMutate:async(view,op)=>{
      calls.push({view:clone(view),op:clone(op)});state=clone(state);
      for(const item of op.action==='batch'?op.operations:[op]){
        if(item.action==='positions')state.editor.positions={...state.editor.positions,...item.positions};
        if(item.kind==='edge'){
          const edge={id:item.id||'new'+calls.length,local_id:item.id||'new'+calls.length,scope:'',
            from:item.fields.source,to:item.fields.target,condition:item.fields.port};
          state.structure.edges=state.structure.edges.filter(e=>e.id!==edge.id);state.structure.edges.push(edge);
        }
        if(item.kind==='node')state.structure.nodes.push({id:item.id,scope:'',...item.fields});
      }
      state.editor.revision++;state.editor.basis='v'+state.editor.revision;return {view:clone(state)};
    }});
    canvas.update(clone(state));
    const find=name=>all(root,el=>has(el,name))[0];
    const card=id=>all(root,el=>has(el,'wb-workflow-card')&&el.dataset.nodeId===id)[0];
    const port=(id,name)=>all(card(id),el=>el.dataset.port===name)[0];
    const viewport=find('wb-workflow-viewport');viewport.clientWidth=1000;viewport.clientHeight=800;
    let under=null;doc.elementFromPoint=()=>under;
    const camera=()=>find('wb-workflow-stage').style.transform.match(/translate\(([-\d.]+)px,([-\d.]+)px\) scale\(([-\d.]+)\)/).slice(1).map(Number);
    const portPoint=(id,name)=>{
      const el=card(id),p=port(id,name),world={x:parseFloat(el.style.left)+parseFloat(el.style.width),y:parseFloat(el.style.top)+parseFloat(p.style.top)};
      const [x,y,scale]=camera();return {...world,clientX:x+world.x*scale,clientY:y+world.y*scale};
    };
    return {root,canvas,calls,find,card,port,viewport,under:el=>under=el,state:()=>state,
      camera,portPoint,
      click:async text=>{now+=500;await all(root,el=>el.tagName==='BUTTON'&&el.textContent===text)[0].dispatch('click',evt(root));}};
  }
  {
    const f=fixture(),card=f.card('a'),body=all(card,el=>has(el,'wb-workflow-task-preview'))[0];
    const wire=f.find('wb-graph-edge'),before=wire.getAttribute('d');
    await f.viewport.dispatch('pointerdown',evt(body,200,200));
    const zoom=f.camera()[2];
    await f.viewport.dispatch('pointermove',evt(body,200+100*zoom,200+50*zoom));frame();
    assert.equal(card.style.left,'200px');assert.equal(card.style.top,'150px');
    assert.notEqual(wire.getAttribute('d'),before,'wires follow during drag');
    f.canvas.update({...clone(f.state()),task:{id:'task',run_id:'run',updated_at:123}});
    assert.equal(f.card('a'),card,'polling must not replace captured card');
    await f.viewport.dispatch('pointerup',evt(body,200+100*zoom,200+50*zoom));
    assert.deepEqual(f.calls[0].op.positions,{a:{x:200,y:150}});
    assert.equal(f.find('wb-node-inspector').hidden,true,'drag must not open details');
    assert.equal(f.card('a').style.left,'200px','saved coordinates survive refresh');cases++;
  }
  {
    const f=fixture(),card=f.card('a');
    await f.viewport.dispatch('pointerdown',evt(card,100,100));
    await f.viewport.dispatch('pointermove',evt(card,200,200));frame();
    await f.viewport.dispatch('pointercancel',evt(card));
    assert.equal(card.style.left,'100px');assert.equal(f.calls.length,0);cases++;
  }
  {
    const f=fixture(),port=f.port('check','fail'),start=f.portPoint('check','fail');
    await f.viewport.dispatch('pointerdown',evt(port,start.clientX,start.clientY));
    f.under(f.card('c'));
    await f.viewport.dispatch('pointermove',evt(port,650,600));
    assert.equal(f.card('c').dataset.dropTarget,'true');
    assert.ok(f.find('wb-wire-preview').getAttribute('d').startsWith(`M ${start.x} ${start.y} `),'fail preview starts at its own port');
    assert.match(f.find('wb-wire-tip').textContent,/不通过.*连接到：c/,'readable target follows pointer');
    const oldCard=f.card('check');f.canvas.update(clone(f.state()));assert.equal(f.card('check'),oldCard);
    await f.viewport.dispatch('pointerup',evt(port,650,600));
    const operations=f.calls[0].op.operations;
    assert.equal(operations[0].action,'positions','connection preserves layout');
    assert.deepEqual(operations[1].fields,{source:'check',target:'c',port:'fail'});
    const line=all(f.root,el=>has(el,'wb-graph-edge')&&el.dataset.from==='check')[0];
    assert.ok(line.getAttribute('d').startsWith(`M ${start.x} ${start.y} `),'saved fail wire matches its port');cases++;
  }
  {
    const f=fixture();now+=500;
    const start=f.portPoint('a','always');
    await f.viewport.dispatch('pointerdown',evt(f.port('a','always'),start.clientX,start.clientY));
    await f.viewport.dispatch('pointerup',evt(f.port('a','always'),start.clientX,start.clientY));
    assert.ok(f.find('wb-wire-preview').getAttribute('d').endsWith(`${start.x+100} ${start.y}`),'click starts a visible provisional wire');
    assert.equal(f.card('b').dataset.connectable,'false','duplicate is not offered');
    assert.equal(f.card('c').dataset.connectable,'true');
    now+=500;
    await f.card('c').dispatch('click',evt(f.card('c')));
    assert.equal(f.calls[0].op.operations[1].fields.target,'c');
    now+=500;await f.port('b','always').dispatch('click',evt(f.port('b','always')));
    assert.equal(f.card('a').dataset.connectable,'false','cycle is not offered');
    await f.card('a').dispatch('click',evt(f.card('a')));
    assert.equal(f.calls.length,1);assert.match(f.find('wb-graph-message').textContent,/依赖环/);
    await f.root.dispatch('keydown',{target:f.viewport,key:'Escape'});
    assert.equal(f.viewport.dataset.connecting,'false');cases++;
  }
  {
    const f=fixture();await f.find('wb-graph-edge-hit').dispatch('click',evt(f.find('wb-graph-edge-hit')));
    const handle=f.find('wb-wire-reconnect');assert.equal(handle.hidden,false);
    await f.viewport.dispatch('pointerdown',evt(handle,550,200));
    f.under(f.card('c'));await f.viewport.dispatch('pointermove',evt(handle,600,600));
    await f.viewport.dispatch('pointerup',evt(handle,600,600));
    const op=f.calls[0].op.operations[1];assert.equal(op.id,'ab');assert.equal(op.fields.target,'c');
    assert.equal(f.state().structure.edges.length,1,'rewire changes the existing edge');cases++;
    assert.equal(all(f.root,el=>el.getAttribute('aria-label')==='下游节点')[0].value,'c','inspector follows the rewired target');
  }
  {
    const f=fixture(),initial=f.find('wb-workflow-stage').style.transform;await f.click('适应');
    assert.notEqual(f.find('wb-workflow-stage').style.transform,initial);
    const stage=f.find('wb-workflow-stage');
    const before=stage.style.transform.match(/translate\(([-\d.]+)px,([-\d.]+)px\) scale\(([-\d.]+)\)/).slice(1).map(Number);
    await f.viewport.dispatch('wheel',{...evt(f.viewport,300,200),deltaY:-100});
    const after=stage.style.transform.match(/translate\(([-\d.]+)px,([-\d.]+)px\) scale\(([-\d.]+)\)/).slice(1).map(Number);
    assert.ok(Math.abs((300-before[0])/before[2]-(300-after[0])/after[2])<1e-8);
    assert.ok(Math.abs((200-before[1])/before[2]-(200-after[1])/after[2])<1e-8);cases++;
  }
  {
    const f=fixture();assert.ok(f.camera()[2]>=1,'default is readable without zooming');
    const view=clone(f.state());view.editor.positions={};f.canvas.update(view);
    assert.equal(f.card('c').style.top,f.card('check').style.top,'independent nodes share a compact row');
    assert.ok(parseFloat(f.card('b').style.left)-parseFloat(f.card('a').style.left)-parseFloat(f.card('a').style.width)<=80);
    f.viewport.clientWidth=500;f.viewport.clientHeight=300;
    await f.click('适应');assert.ok(f.camera()[2]>=.9,'fit does not make text tiny');
    await f.click('总览');assert.ok(f.camera()[2]<.9,'overview remains available');
    const start=f.portPoint('check','fail');now+=500;
    await f.port('check','fail').dispatch('click',evt(f.port('check','fail')));
    const after=f.portPoint('check','fail');assert.ok(f.camera()[2]>=.9);
    assert.ok(Math.abs(after.clientX-start.clientX)<1e-8&&Math.abs(after.clientY-start.clientY)<1e-8,'zoom keeps the chosen outlet under the pointer');
    await f.root.dispatch('keydown',{target:f.viewport,key:'Escape'});cases++;
  }
  {
    const f=fixture(),card=f.card('a'),wire=f.find('wb-graph-edge'),view=clone(f.state());
    const item=view.structure.nodes.find(n=>n.id==='a');
    item.execution={phase:'tool_running',text:'正在调用工具 · ReadFile',active:true,at:Date.now()/1000-12};
    f.canvas.update(view);
    assert.equal(f.card('a'),card,'activity changes patch the footer without replacing cards');
    assert.equal(f.find('wb-graph-edge'),wire,'activity must not redraw connections');
    assert.equal(f.find('wb-workflow-execution-text').textContent,item.execution.text);
    assert.match(f.find('wb-workflow-execution-time').textContent,/12秒/);
    await f.viewport.dispatch('pointerdown',evt(card,100,100));
    await f.viewport.dispatch('pointermove',evt(card,200,200));
    item.execution={phase:'output',text:'正在回复',active:true,at:Date.now()/1000};
    f.canvas.update(clone(view));
    assert.equal(f.card('a'),card,'drag remains captured during live status updates');
    assert.equal(f.find('wb-workflow-execution-text').textContent,'正在回复');
    await f.viewport.dispatch('pointercancel',evt(card));
    item.execution={phase:'completed',text:'已交付',active:false};f.canvas.update(clone(view));
    assert.equal(f.find('wb-workflow-execution').dataset.active,'false');
    assert.equal(f.find('wb-workflow-execution-time').textContent,'');
    f.canvas.destroy();cases++;
  }
  {
    const f=fixture(),view=clone(f.state());
    Object.assign(view.structure.nodes[0],{kind:'loop',label:'样章循环',status:'进行中',iteration:[],
      execution:{phase:'starting',text:'节点正在执行',active:true}});
    const child={id:'a/make',scope:'a',kind:'work',label:'制作样章',status:'进行中',iteration:[0],
      config:{},progress:[{at:10,text:'已生成候选包'}],artifacts:[{id:'package',name:'样章.zip'}],
      execution:{phase:'tool_running',text:'正在调用工具 · ReportArtifact',active:true,at:Date.now()/1000-5}};
    view.structure.nodes.push(child,{id:'a/review',scope:'a',kind:'work',label:'检查样章',status:'未开始',config:{}});
    f.canvas.update(view);
    assert.match(f.card('a').textContent,/制作样章：已生成候选包/);
    assert.match(f.card('a').textContent,/样章.zip/);
    assert.match(f.card('a').textContent,/第 1 轮 · 子节点 0\/2 已完成/);
    const card=f.card('a');child.execution={phase:'waiting_model',text:'等待模型响应',active:true};
    f.canvas.update(clone(view));
    assert.equal(f.card('a'),card,'child phase changes patch the parent without replacing cards');
    assert.match(card.textContent,/制作样章 · 等待模型响应/);
    now+=500;await card.dispatch('dblclick',evt(card));
    assert.ok(f.card('a/make'),'double click opens the loop body');
    assert.match(f.find('wb-canvas-label').textContent,/样章循环/);
    assert.equal(f.find('wb-conversation-modal'),undefined,'a container has no model conversation');
    await f.click('← 上一层');assert.ok(f.card('a'));f.canvas.destroy();cases++;
  }
  {
    const f=fixture(),out=f.port('a','always'),start=f.portPoint('a','always');
    await f.viewport.dispatch('pointerdown',evt(out,start.clientX,start.clientY));
    await f.viewport.dispatch('pointermove',evt(f.viewport,995,400));
    const before=f.camera();frame();assert.ok(f.camera()[0]<before[0],'drag near edge pans toward offscreen targets');
    await f.viewport.dispatch('pointercancel',evt(f.viewport));
    const stopped=f.camera();frame();assert.deepEqual(f.camera(),stopped,'cancellation stops auto pan');
    assert.equal(f.calls.length,0);cases++;
  }
  console.log(`workflow_canvas_interactions: ${cases} cases passed`);
})().catch(error=>{console.error(error);process.exitCode=1;});
