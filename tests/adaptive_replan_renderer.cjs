const assert=require('node:assert/strict');
const {setup}=require('./workbench_dom.cjs');
(async()=>{
  const {renderProcess}=await import('../frontend/static/workbench/process.js');
  const doc=setup(),root=doc.createElement('section');doc.body.append(root);
  const decision={source:'primary_method_consultation',episode_id:'evidence-bound-episode',action:'diagnose_path',
    chosen:{next_step:'Check raw and normalized digest separately'},probe_max_iterations:2,probe_status:'applied',verification_status:'host_progress_only'};
  renderProcess(root,{node_states:{work:{status:'paused'}},events:[{type:'expert_step',node_id:'work',text:'检查点已经保存',recovery_decision:decision},
    {type:'expert_step',node_id:'other',recovery_decision:{episode_id:'other-node-private'}}]},'work');
  for(const value of ['检查点已经保存','恢复调度决定','evidence-bound-episode','Check raw and normalized digest separately','host_progress_only','probe_max_iterations'])assert.ok(root.textContent.includes(value),value);
  assert.doesNotMatch(root.textContent,/other-node-private|已验收/);
  assert.equal(root.querySelectorAll('button,input,textarea').length,0);
  assert.ok(root.querySelectorAll('details').every(row=>!row.open));
  renderProcess(root,{events:[{type:'expert_step',node_id:'work',text:'<img src=x onerror=alert(1)>',recovery_decision:decision}]},'work');
  assert.equal(root.querySelectorAll('img').length,0);assert.match(root.textContent,/<img/);
  console.log('adaptive replan workbench evidence: 1 case passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
