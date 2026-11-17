const assert=require('node:assert/strict'),path=require('node:path');
const {pathToFileURL}=require('node:url');const {setup}=require('./workbench_dom.cjs');
const load=name=>import(pathToFileURL(path.resolve(__dirname,'../frontend/static/workbench/'+name)));
const base={version:1,nodes:[{id:'a',label:'整理来源',goal:'核对',milestone_id:'m1'},{id:'b',label:'输出清单',goal:'汇总',milestone_id:'m2'}],edges:[{from:'a',to:'b',condition:'always'}]};
(async()=>{
  const {compareVersions,graphLayout,mountStructure}=await load('structure.js');let cases=0;
  {
    assert.deepEqual(compareVersions(base,{...base,nodes:base.nodes.map(row=>({...row,status:'已完成'}))}),[]);
    const after={version:2,nodes:[{...base.nodes[0],goal:'补充证据'},{id:'c',label:'最终交付',milestone_id:'m2'}],edges:[{from:'a',to:'c',condition:'always'}]};
    const changes=compareVersions(base,after);assert.deepEqual(changes.map(row=>[row.id,row.kind]),[['a','修改'],['c','新增'],['b','删除']]);
    assert.deepEqual(changes[0].fields,['目标','连接']);assert.ok(compareVersions(after,base).some(row=>row.id==='c'&&row.kind==='删除'));cases++;
  }
  {
    const structure={nodes:[...base.nodes,{id:'parallel',label:'并行',milestone_id:'m1'}],groups:[{id:'m1',label:'研究'},{id:'m2',label:'交付'}],edges:[...base.edges,{from:'b',to:'a'}]};
    const original=JSON.stringify(structure),computed=graphLayout(structure);assert.deepEqual(computed,graphLayout(structure));assert.equal(JSON.stringify(structure),original);
    const boxes=Object.values(computed.positions);for(let i=0;i<boxes.length;i++)for(let j=i+1;j<boxes.length;j++){
      const a=boxes[i],b=boxes[j];assert.ok(a.x+a.w<=b.x||b.x+b.w<=a.x||a.y+a.h<=b.y||b.y+b.h<=a.y);
    }
    for(const item of structure.nodes){const box=computed.positions[item.id],group=computed.groups.find(row=>row.id===item.milestone_id);assert.ok(box.x>=group.x&&box.y>=group.y&&box.x+box.w<=group.x+group.w&&box.y+box.h<=group.y+group.h);}
    assert.equal(computed.groups.length,2);cases++;
  }
  {
    const nodes=['a','b','c'].map(id=>({id,label:id}));
    const edges=[{from:'c',to:'a',condition:'loop'},{from:'a',to:'b'},{from:'b',to:'c'}];
    const p=graphLayout({nodes,edges}).positions;
    assert.ok(p.a.x<p.b.x && p.b.x<p.c.x,'explicit returns must not reverse the main flow');cases++;
  }
  {
    const doc=setup(),root=doc.createElement('section');doc.body.append(root);const opened=[];
    const ui=mountStructure(root,{openStep:id=>opened.push(id)});
    ui.update({task:{id:'none'},structure:{nodes:[],edges:[],groups:[],versions:[]}});assert.match(root.textContent,/添加节点/);
    const view={task:{id:'task',run_id:'run'},plan:[{steps:[{id:'b',detail:{goal:'汇总资料',artifacts:[{name:'交付清单'}]}}]}],structure:{...base,nodes:base.nodes.map(row=>({...row,status:'进行中'})),groups:[{id:'m1',label:'研究'},{id:'m2',label:'交付'}],versions:[base]}};
    ui.update(view);assert.equal(root.querySelectorAll('.wb-graph-node').length,2);assert.equal(root.querySelectorAll('textarea').length,0);
    assert.equal(root.querySelector('.wb-version-compare').hidden,true);assert.ok(root.querySelectorAll('button').every(row=>!row.hasAttribute('contenteditable')&&!row.draggable));
    const selected=root.querySelectorAll('.wb-graph-node')[1];await selected.click();assert.deepEqual(opened,[]);
    const inspector=root.querySelector('.wb-node-inspector');assert.equal(inspector.hidden,false);assert.match(inspector.textContent,/汇总资料/);assert.match(inspector.textContent,/交付清单/);
    assert.equal(selected.getAttribute('aria-pressed'),'true');
    await inspector.querySelectorAll('button').find(row=>row.textContent==='查看步骤与产物').click();assert.deepEqual(opened,['b']);assert.equal(inspector.hidden,true);await selected.click();selected.focus();
    ui.update({...view,structure:{...view.structure,nodes:view.structure.nodes.map(row=>({...row,status:'已完成'}))}});
    assert.equal(doc.activeElement.dataset.nodeId,'b');assert.equal(root.querySelectorAll('.wb-graph-group').length,2);
    assert.equal(root.querySelectorAll('.wb-node-port').length,4);
    const detailsButton=inspector.querySelectorAll('button').find(row=>row.textContent==='查看步骤与产物');ui.update({...view,structure:{...view.structure,nodes:view.structure.nodes.map(row=>({...row,status:'已完成'}))}});assert.equal(inspector.contains(detailsButton),true);
    ui.update({task:{id:'other'},structure:{nodes:[],edges:[],groups:[],versions:[]}});assert.equal(inspector.hidden,true);cases++;
  }
  {
    const doc=setup(),root=doc.createElement('section');doc.body.append(root);let composed=0;
    const ui=mountStructure(root,{onCompose:()=>composed++});
    ui.update({task:{id:'task'},structure:{...base,groups:[],versions:[]}});
    const find=label=>root.querySelectorAll('button').find(row=>row.textContent===label || row.getAttribute('aria-label')===label),graph=root.querySelector('.wb-graph');
    const camera=()=>root.querySelector('.wb-graph-stage').style.transform;
    graph.clientWidth=400;graph.clientHeight=300;
    await find('+').click();assert.match(camera(),/scale\(1.1\)/);
    await find('适应').click();const scaled=Number(camera().match(/scale\((.+)\)/)[1]);assert.ok(scaled<1);
    const beforePan=camera();graph.setPointerCapture=id=>{graph.captured=id;};
    await graph.dispatch('pointerdown',{button:0,pointerId:7,clientX:100,clientY:100,target:{closest:()=>null}});
    await graph.dispatch('pointermove',{clientX:75,clientY:80});assert.equal(graph.captured,7);assert.notEqual(camera(),beforePan);
    const afterPan=camera();await graph.dispatch('pointercancel');await graph.dispatch('pointermove',{clientX:0,clientY:0});assert.equal(camera(),afterPan);assert.equal(graph.dataset.panning,'false');
    graph.getBoundingClientRect=()=>({left:0,top:0});await graph.dispatch('wheel',{deltaY:-100,clientX:200,clientY:150});assert.notEqual(camera(),afterPan);
    const dragged=root.querySelector('.wb-graph-node'),oldLeft=dragged.style.left,oldWire=graph.querySelector('path').getAttribute('d');
    await graph.dispatch('pointerdown',{button:0,pointerId:8,clientX:100,clientY:100,target:{closest:()=>dragged}});
    await graph.dispatch('pointermove',{clientX:130,clientY:130});await graph.dispatch('pointerup');
    assert.notEqual(root.querySelector('.wb-graph-node').style.left,oldLeft);assert.notEqual(graph.querySelector('path').getAttribute('d'),oldWire);
    assert.equal(doc.activeElement.dataset.nodeId,'a');const movedLeft=root.querySelector('.wb-graph-node').style.left;
    ui.update({task:{id:'task'},structure:{...base,groups:[],versions:[]}});assert.equal(root.querySelector('.wb-graph-node').style.left,movedLeft);
    await root.querySelector('.wb-graph-node').click();
    const draftCamera=camera();ui.update({task:{id:'task',run_id:'first-run'},structure:{...base,groups:[],versions:[]}});
    assert.equal(root.querySelector('.wb-graph-node').style.left,movedLeft);assert.equal(camera(),draftCamera);
    assert.equal(root.querySelector('.wb-node-inspector').hidden,false);
    await find('专注模式').click();assert.equal(root.dataset.expanded,'true');assert.equal(doc.activeElement,graph);
    await root.dispatch('keydown',{key:'Escape',stopPropagation(){}});assert.equal(root.dataset.expanded,'false');assert.equal(doc.activeElement,find('专注模式'));
    ui.toggleNodes();const library=root.querySelector('.wb-node-library');assert.equal(library.hidden,false);assert.equal(doc.activeElement,root.querySelector('input'));
    const search=root.querySelector('input');search.value='输出';await search.dispatch('input');assert.equal(root.querySelectorAll('.wb-node-list-item').length,1);
    await root.querySelector('.wb-node-list-item').click();assert.match(root.querySelector('.wb-node-inspector').textContent,/输出清单/);
    assert.equal(library.hidden,true);assert.ok(Number(root.querySelector('.wb-graph-tools').children[1].textContent.replace('%',''))>=60);
    ui.update({task:{id:'new'},structure:{nodes:[],edges:[],groups:[],versions:[]}});await find('✦ AI 构图').click();assert.equal(composed,1);
    ui.update({task:{id:'history',readonly:true},structure:{nodes:[],edges:[],groups:[],versions:[]}});assert.equal(find('✦ AI 构图').hidden,true);ui.destroy();cases++;
  }
  {
    const doc=setup(),root=doc.createElement('section');doc.body.append(root);const ui=mountStructure(root,{openStep(){}});
    const second={...base,version:2,nodes:base.nodes.map(row=>({...row,goal:'新目标'}))};
    const view={task:{id:'task',run_id:'run'},structure:{...base,groups:[],versions:[base,second]}};ui.update(view);
    const [from,to]=root.querySelectorAll('select');assert.equal(from.value,'1');assert.equal(to.value,'2');assert.equal(root.querySelector('.wb-version-compare').hidden,false);
    assert.match(root.textContent,/2 个步骤发生变化/);assert.match(root.textContent,/修改 · 整理来源/);
    to.value='1';await to.dispatch('change');assert.match(root.textContent,/步骤与连接相同/);
    ui.update({...view,structure:{...view.structure,versions:[base,second,{...second,version:3}]}});assert.equal(to.value,'1');cases++;
  }
  {
    const doc=setup(),root=doc.createElement('section');doc.body.append(root);const edited=[];
    const ui=mountStructure(root,{onEdit:item=>edited.push(item.id),openStep(){throw Error('draft has no run history');}});
    const view={task:{id:'draft',design_stage:true},structure:{...base,groups:[],versions:[],unresolved_edges:1},plan:[{steps:[{id:'a',detail:{goal:'核对资料',inputs:['两份资料'],outputs:['对照表'],requirements:['逐条保留出处'],sources:[{quote:'请保留原文'}]}}]}]};
    ui.update(view);await root.querySelector('.wb-graph-node').click();
    const inspector=root.querySelector('.wb-node-inspector');assert.match(inspector.textContent,/两份资料/);assert.match(inspector.textContent,/对照表/);assert.match(inspector.textContent,/逐条保留出处/);assert.match(inspector.textContent,/请保留原文/);
    assert.equal(root.querySelector('.wb-graph-node').querySelector('.wb-node-id').textContent,'ID: a');
    assert.equal(inspector.querySelector('.wb-node-id').textContent,'ID: a');
    assert.match(root.textContent,/编排草稿/);assert.match(root.textContent,/1 条连接待补全/);
    const edit=inspector.querySelectorAll('button').find(button=>button.textContent==='在对话中修改');await edit.click();assert.deepEqual(edited,['a']);
    // Same names and later renames must still select the exact persisted ID.
    const renamed={...view,structure:{...view.structure,nodes:view.structure.nodes.map(item=>({...item,label:'同名节点'}))}};
    ui.update(renamed);ui.toggleNodes();const search=root.querySelector('input');search.value='b';await search.dispatch('input');
    assert.equal(root.querySelectorAll('.wb-node-list-item').length,1);
    assert.equal(root.querySelector('.wb-node-list-item').querySelector('.wb-node-id').textContent,'ID: b');
    await root.querySelector('.wb-node-list-item').click();
    await inspector.querySelectorAll('button').find(button=>button.textContent==='在对话中修改').click();assert.deepEqual(edited,['a','b']);
    assert.ok(!inspector.querySelectorAll('button').some(button=>button.textContent==='查看步骤与产物'));ui.destroy();cases++;
  }
  {
    const doc=setup(),root=doc.createElement('section');doc.body.append(root);const edits=[];
    const {outlets}=await load('graph-editor.js');
    assert.deepEqual(outlets({id:'a',kind:'decision'}).map(p=>p.condition),['pass','fail']);
    assert.equal(outlets({id:'a',kind:'implement'},[{id:'x',from:'a',to:'b',condition:'always'},{id:'y',from:'a',to:'c',condition:'always'}]).length,2);
    const view={task:{id:'draft',conversation_id:'chat',design_stage:true},editor:{editable:true,basis:'b1',positions:{}},structure:{...base,nodes:[{...base.nodes[0],kind:'decision'},base.nodes[1]],edges:[],groups:[],versions:[]}};
    const ui=mountStructure(root,{onMutate:async(v,op,positions)=>{edits.push({v,op,positions});}});ui.update(view);
    const find=label=>root.querySelectorAll('button').find(b=>b.textContent===label || b.getAttribute('aria-label')===label);
    await find('整理来源 · 是出口').click();await find('输出清单 · 输入').click();
    assert.deepEqual(edits[0].op,{action:'upsert',kind:'edge',fields:{data:{from_node:'a',to_node:'b',condition:'pass'}}});
    assert.equal(edits[0].v.editor.basis,'b1');assert.equal(typeof edits[0].positions.a.x,'number');
    await find('+ 添加节点').click();
    const form=root.querySelector('form');form.querySelector('input').value='人工汇总';form.querySelector('select').value='reduce';
    ui.update({...view,editor:{...view.editor,basis:'newer-concurrent-basis'}});
    await form.dispatch('submit');assert.equal(edits[1].op.fields.title,'人工汇总');assert.equal(edits[1].op.fields.data.node_kind,'reduce');
    assert.equal(edits[1].v.editor.basis,'b1','forms must submit against the revision they were opened on');
    ui.update({...view,task:{...view.task,readonly:true}});assert.equal(find('+ 添加节点').hidden,true);
    assert.ok(root.querySelectorAll('.wb-node-port').every(p=>p.disabled));ui.destroy();cases++;
  }
  console.log(`workbench_structure: ${cases} cases passed`);
})().catch(error=>{console.error(error);process.exitCode=1;});
