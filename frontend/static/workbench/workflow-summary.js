const STATUS={pending:'未开始',running:'进行中',completed:'已完成',skipped:'已跳过',paused:'已暂停',
  blocked:'已阻塞',needs_input:'等你处理',unknown:'待核对',failed:'执行失败',terminated:'已停止'};
const STATE=Object.fromEntries(Object.entries(STATUS).map(([key,label])=>[label,key]));
Object.assign(STATE,{'部分受阻':'running','恢复中':'running'});
function aggregate(statuses){
  for(const status of ['running','unknown','failed','blocked','needs_input','paused','terminated'])if(statuses.includes(status))return status;
  return statuses.length&&statuses.every(status=>['completed','skipped'].includes(status))?'completed':'pending';
}

// Compatibility for already-running hosts that predate the scoped display fields.
// Current hosts own this projection; their workflow_status is never reinterpreted.
export function alignWorkflowView(view){
  if(view?.task?.purpose==='exploration')return view;
  if(view?.task?.mode!=='workflow'||!view.task.run_id||view.task.workflow_status||view.task.design_stage||!view.plan?.length)return view;
  const task={...view.task},stopped=['paused','terminated','failed','unknown','blocked'].includes(task.execution_status);
  const plan=view.plan.map(group=>({...group,steps:group.steps.map(step=>{
    if(!stopped||step.status!=='进行中'&&!step.execution_suspended)return {...step};
    return {...step,status:STATUS[task.execution_status],execution_suspended:true,
      status_reason:task.reason||'流程已停止推进，已有进度保留。'};
  })}));
  const steps=new Map(plan.flatMap(group=>group.steps.map(step=>[step.id,step])));
  if(!stopped)for(const [path,step]of steps){
    if(step.detail?.kind!=='loop'||step.status!=='进行中')continue;
    const children=[...steps.values()].filter(child=>child.id.startsWith(path+'/'));
    if(children.some(child=>['已阻塞','执行失败','待核对'].includes(child.status)))step.status='部分受阻';
    else if(children.some(child=>child.detail?.automatic_continuation))step.status='恢复中';
  }
  const counts={running:0,waiting:0,queued:0};
  for(const step of steps.values()){
    const detail=step.detail||{},activity=detail.node_dialogue;
    if(activity)for(const name of Object.keys(counts))counts[name]+=activity[name]||0;
    else if(detail.kind!=='loop'&&detail.execution_active&&step.status!=='进行中')counts[detail.execution_suspended?'waiting':'running']++;
  }
  if(stopped)counts.running=counts.waiting=0;
  const root=[...steps.values()].filter(step=>!step.id.includes('/'));
  let progress=aggregate(root.map(step=>STATE[step.status]||'pending'));
  if(stopped||task.execution_status==='needs_input')progress=task.execution_status;
  else if(task.execution_status==='running'&&progress==='pending')progress='running';
  task.workflow_status=progress;task.status=STATUS[progress]||task.status;task.node_dialogue=counts;
  task.blocked_branches=[...steps.values()].filter(step=>step.detail?.kind!=='loop'&&['已阻塞','执行失败','待核对'].includes(step.status)).length;
  task.waiting_branches=[...steps.values()].filter(step=>step.status==='等你处理').length;
  task.paused_branches=root.filter(step=>step.status==='已暂停').length;
  if(progress==='needs_input'){
    task.waiting_node_ids=[...steps.values()].filter(step=>step.status==='等你处理').map(step=>step.id);task.waiting_for='user';
  }
  const replies=task.execution_status==='running'?counts.running+counts.waiting+counts.queued:0;
  let activity;
  if(progress==='running'){
    if(task.automatic_recovery_count)task.status='恢复中';
    else if(task.blocked_branches)task.status='部分受阻';
    task.controls=['pause','cancel'];const active=root.filter(step=>step.status==='进行中').map(step=>step.name);
    activity=active.length?'正在进行：'+active.slice(0,3).join('、'):'正在按依赖执行';
    for(const [name,count]of [['分支已暂停',task.paused_branches],['分支待处理',task.waiting_branches],['节点已阻塞',task.blocked_branches||0]])if(count)activity+=`；${count} 个${name}`;
    if(task.automatic_recovery_count)activity+=`；${task.automatic_recovery_count} 条对话等待自动续接`;
  }else{
    task.controls=['completed','needs_input','terminated'].includes(progress)?[]:['resume'];
    if(replies)task.controls.push('pause');if(progress!=='completed'||replies)task.controls.push('cancel');
    const reason=root.find(step=>STATE[step.status]===progress&&step.status_reason)?.status_reason||task.reason||'';
    if(reason)task.reason=reason;
    activity={completed:'主流程已完成',needs_input:'主流程等待你的处理',paused:'主流程已暂停',blocked:'主流程已阻塞',
      failed:'主流程执行失败',unknown:'主流程等待核对结果',terminated:'主流程已停止'}[progress]||STATUS[progress]||'';
    if(reason&&progress!=='completed')activity+='：'+reason;
  }
  if(replies)activity+='；节点对话：'+Object.entries({running:'进行中',waiting:'等待回复',queued:'排队'})
    .filter(([name])=>counts[name]).map(([name,label])=>`${counts[name]} 条${label}`).join('，');
  task.current_activity=activity;
  for(const group of plan)group.status=steps.get(group.id)?.status||(group.id==='workflow'?task.status:STATUS[aggregate(group.steps.map(step=>STATE[step.status]||'pending'))]);
  const nodes=(view.structure?.nodes||[]).map(node=>{
    const step=steps.get(node.id);return step?{...node,status:step.status,status_reason:step.status_reason||''}:node;
  });
  return {...view,task,plan,structure:{...view.structure,nodes}};
}

// Loop containers schedule their children; their own attempt has no model feed.
// Derive display data without changing any persisted execution or delivery state.
export function summarizeLoops(view) {
  const source=view.structure?.nodes||[];
  if(!source.some(item=>item.kind==='loop'))return view;
  const nodes=source.map(item=>{
    if(item.kind!=='loop')return item;
    const descendants=source.filter(child=>child.id.startsWith(item.id+'/'));
    const children=descendants.filter(child=>child.scope===item.id);
    const leaves=descendants.filter(child=>child.kind!=='loop');
    const progress=leaves.flatMap(child=>(child.progress||[]).map(entry=>({...entry,
      node_id:child.id,text:`${child.label}：${entry.text}`})))
      .sort((a,b)=>(a.at||0)-(b.at||0)).slice(-5);
    const artifacts=[],seen=new Set();
    for(const artifact of [...(item.artifacts||[]),...leaves.flatMap(child=>child.artifacts||[])]){
      const key=artifact.id||artifact.path||artifact.stored_path;
      if(key&&seen.has(key))continue;
      if(key)seen.add(key);artifacts.push(artifact);
    }
    const depth=(item.iteration||[]).length;
    const rounds=leaves.map(child=>child.iteration?.[depth]).filter(Number.isInteger);
    const round=rounds.length?`第 ${Math.max(...rounds)+1} 轮 · `:'';
    const completed=children.filter(child=>child.status==='已完成').length;
    const skipped=children.filter(child=>child.status==='已跳过').length;
    const activity=children.length?`${round}子节点 ${completed}/${children.length} 已完成`+(skipped?` · ${skipped} 已跳过`:''):item.activity;
    let execution=item.execution;
    const stopped=['paused','terminated','blocked','failed','needs_input','unknown'].includes(execution?.phase);
    if(['进行中','部分受阻','恢复中'].includes(item.status)&&!stopped){
      const active=leaves.filter(child=>child.status==='进行中'||child.execution?.active)
        .sort((a,b)=>Number(!!b.execution?.active)-Number(!!a.execution?.active)
          ||(b.execution?.at||0)-(a.execution?.at||0));
      const child=active[0]||leaves.find(child=>['恢复中','已阻塞','执行失败','等你处理','待核对','已暂停'].includes(child.status));
      if(child){
        const phase=child.execution||{phase:'starting',text:child.status,active:false};
        execution={...phase,node_id:child.id,text:`${child.label} · ${phase.text}`+(active.length>1?` · 共 ${active.length} 个节点进行中`:''),
          detail:[phase.detail,...active.slice(1).map(row=>`${row.label} · ${row.execution?.text||row.status}`)].filter(Boolean).join('\n')};
      }else execution={phase:'waiting_children',text:'等待子节点执行',active:false};
    }
    return {...item,progress:progress.length?progress:item.progress,artifacts,activity,execution};
  });
  return {...view,structure:{...view.structure,nodes}};
}
