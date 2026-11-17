const assert=require('node:assert/strict'),path=require('node:path');
const {pathToFileURL}=require('node:url');const {setup}=require('./workbench_dom.cjs');
const load=name=>import(pathToFileURL(path.resolve(__dirname,'../frontend/static/workbench/'+name)));
(async()=>{
  const {progressText,progressMeter,loopRegions,loopBoxes,renderWorkBoard}=await load('work-progress.js');
  const {mountStructure}=await load('structure.js');let cases=0;
  const progress={scope:'电路基础',unit:'页',current:5,total:110,completed:4};
  const issue={id:'formula-2',scope:'电路基础',location:'第5页第二个公式',message:'公式缺少分母，暂时不能确认计算结果。',state:'open',
    next_step:'对照原书补齐分母，再请复核确认。',node_id:'review',reporter:'原图复核（grok-4.7/xhigh）',seq:3};
  const report={node_id:'read',node_label:'逐页阅读',seq:4,summary:'这本书读到第5页，公式正在修正。',progress};
  const nodes=['read','review','check','human'].map((id,index)=>({id,label:['逐页阅读','原图复核（grok-4.7/xhigh）','检查本批结果','人工确认'][index],
    status:id==='read'?'进行中':'需返工',milestone_id:'m',work_report:id==='read'?report:null}));
  const edges=[{from:'read',to:'review'},{from:'review',to:'check'},{from:'check',to:'human',condition:'pass'}];
  const region={id:'loop',label:'逐页处理',node_ids:['read','review','check'],entry_node_id:'read',check_node_id:'check',
    round:3,active_node_ids:['read'],report,progress,issues:[issue]};
  const view={task:{id:'book',run_id:'run',status:'进行中'},plan:[{id:'m',steps:[]}],structure:{nodes,edges,groups:[],loops:[region],versions:[]},work_board:{report}};
  {
    assert.equal(progressText(progress),'第 5 页 / 共 110 页');
    assert.equal(progressText({...progress,total:null}),'第 5 页 · 总量待确认');
    assert.equal(progressMeter(progress).value,4,'position must not become a completed count');
    assert.equal(progressMeter({...progress,completed:undefined}).label,'当前处理位置');
    assert.equal(progressMeter({...progress,total:null}),null);cases++;
  }
  {
    const inferred=loopRegions({nodes,edges:[...edges,{from:'check',to:'read',condition:'loop'}]});
    assert.deepEqual(inferred[0].node_ids,['read','review','check']);assert.ok(!inferred[0].node_ids.includes('human'));
    assert.equal(loopRegions(view.structure).length,1);
    assert.equal(loopRegions({...view.structure,edges:[...edges,{from:'check',to:'read',condition:'loop'}]}).length,1);
    const boxes=loopBoxes(inferred,{read:{x:0,y:0,w:100,h:100},review:{x:200,y:0,w:100,h:100},check:{x:400,y:0,w:100,h:100}});
    assert.ok(boxes[0].x<0 && boxes[0].y<0 && boxes[0].w>500);cases++;
  }
  {
    const doc=setup(),root=doc.createElement('section');doc.body.append(root);let focus='';
    renderWorkBoard(root,view,{focusNode:id=>focus=id});
    assert.match(root.textContent,/电路基础/);assert.match(root.textContent,/第 5 页 \/ 共 110 页/);
    assert.match(root.textContent,/第5页第二个公式/);assert.match(root.textContent,/公式缺少分母/);
    assert.match(root.textContent,/原图复核反馈/);assert.doesNotMatch(root.textContent,/grok-4.7|environment_fault|n_check|哈希/);
    const bar=root.querySelector('.wb-work-meter');assert.equal(bar.getAttribute('aria-valuenow'),'4');
    assert.equal(bar.getAttribute('aria-valuemax'),'110');
    await root.querySelectorAll('button').find(row=>row.textContent==='定位当前').click();assert.equal(focus,'read');
    renderWorkBoard(root,{...view,structure:{...view.structure,loops:[{...region,issues:[{...issue,state:'fixing'}]}]}});
    assert.match(root.textContent,/正在修正/);
    renderWorkBoard(root,{...view,structure:{...view.structure,loops:[{...region,issues:[{...issue,state:'resolved'}]}]}});
    assert.match(root.textContent,/已修正 · 待复核/);assert.doesNotMatch(root.textContent,/验收通过/);cases++;
  }
  {
    const doc=setup(),root=doc.createElement('section');doc.body.append(root);
    renderWorkBoard(root,{...view,structure:{...view.structure,loops:[{...region,progress:{scope:'第二本书',unit:'页',current:2,total:30}}]}});
    assert.doesNotMatch(root.textContent,/公式缺少分母/,'a previous book must not supply the current problem');
    renderWorkBoard(root,{task:{id:'other'},structure:{nodes:[],edges:[]}});assert.equal(root.hidden,true);assert.equal(root.textContent,'');cases++;
  }
  {
    const doc=setup(),root=doc.createElement('section');doc.body.append(root);const ui=mountStructure(root,{});
    ui.update(view);const graph=root.querySelector('.wb-graph');graph.clientWidth=1200;graph.clientHeight=500;
    assert.equal(root.querySelectorAll('.wb-loop-region').length,1);
    assert.ok(root.querySelectorAll('path').some(row=>row.dataset.condition==='loop'),'host loop must render a return even without a saved back edge');
    assert.ok(root.querySelectorAll('.wb-graph-node').some(row=>row.dataset.current==='true'));
    assert.ok(root.querySelectorAll('.wb-graph-node').every(row=>!row.querySelector('.wb-node-id')),'runtime cards keep IDs in the inspector');
    const camera=()=>root.querySelector('.wb-graph-stage').style.transform;
    await root.querySelectorAll('button').find(row=>row.textContent==='定位当前').click();
    assert.ok(Number(camera().match(/scale\((.+)\)/)[1])>=.7);
    ui.update({...view,structure:{...view.structure,loops:[{...region,progress:{...progress,current:6,completed:5}}]}});
    assert.match(root.querySelector('.wb-work-board').textContent,/第 6 页 \/ 共 110 页/);
    const remembered=camera();ui.update(view);assert.equal(camera(),remembered,'incoming reports preserve the user camera');
    ui.update({task:{id:'next',run_id:'new'},plan:[],structure:{nodes:[],edges:[],groups:[],versions:[]}});
    assert.doesNotMatch(root.textContent,/公式缺少分母|电路基础/);cases++;
  }
  console.log(`workbench_progress: ${cases} cases passed`);
})().catch(error=>{console.error(error);process.exitCode=1;});
