import {node, button} from './dom.js';

export function progressText(progress) {
  if (!progress) return '等待数量进度汇报';
  const unit=progress.unit || '项', total=progress.total;
  if (Number.isInteger(progress.current)) return `第 ${progress.current} ${unit}${total ? ` / 共 ${total} ${unit}` : ' · 总量待确认'}`;
  return `已处理 ${progress.completed ?? 0}${unit}${total ? ` / 共 ${total}${unit}` : ' · 总量待确认'}`;
}

export function progressMeter(progress) {
  if (!progress?.total) return null;
  const completed=Number.isInteger(progress.completed), value=completed ? progress.completed : progress.current;
  return Number.isInteger(value) ? {value,max:progress.total,percent:Math.max(0,Math.min(100,value/progress.total*100)),
    label:completed ? '已处理数量' : '当前处理位置'} : null;
}

export function businessLabel(label='') {
  return label.replace(/\s*[（(](?:gpt|grok|deepseek|claude|gemini|模型)[^）)]*[）)]/gi,'').trim();
}

export function loopRegions(structure) {
  const ids=new Set((structure.nodes || []).map(row=>row.id));
  const explicit=(structure.loops || []).filter(row=>row.node_ids?.some(id=>ids.has(id)));
  const regions=explicit.map(row=>({...row,node_ids:row.node_ids.filter(id=>ids.has(id))}));
  const edges=structure.edges || [], forward=edges.filter(edge=>edge.condition!=='loop');
  function reachable(start, reverse=false) {
    const found=new Set(), pending=[start];
    while(pending.length) {
      const id=pending.pop();if(found.has(id)) continue;found.add(id);
      for(const edge of forward) if((reverse?edge.to:edge.from)===id) pending.push(reverse?edge.from:edge.to);
    }
    return found;
  }
  for(const edge of edges.filter(row=>row.condition==='loop' && ids.has(row.from) && ids.has(row.to))) {
    if(regions.some(row=>row.check_node_id===edge.from && row.entry_node_id===edge.to)) continue;
    const after=reachable(edge.to), before=reachable(edge.from,true);
    const members=[...ids].filter(id=>after.has(id)&&before.has(id));
    if(!members.includes(edge.from) || !members.includes(edge.to)) continue;
    regions.push({id:`return:${edge.from}:${edge.to}`,label:'循环处理',node_ids:members,
      entry_node_id:edge.to,check_node_id:edge.from,active_node_ids:[],issues:[]});
  }
  return regions;
}

export function loopBoxes(regions, positions) {
  return regions.flatMap((region,index)=>{
    const boxes=region.node_ids.map(id=>positions[id]).filter(Boolean);if(!boxes.length) return [];
    const x=Math.min(...boxes.map(p=>p.x))-30,y=Math.min(...boxes.map(p=>p.y))-94-index*6;
    return [{...region,x,y,w:Math.max(...boxes.map(p=>p.x+p.w))+30-x,
      h:Math.max(...boxes.map(p=>p.y+p.h))+62-y}];
  });
}

export function renderLoopRegion(region, focus) {
  const area=node('section','wb-loop-region');area.dataset.loopId=region.id;
  area.setAttribute('aria-label',`${region.progress?.scope || region.label} · 循环区域`);
  Object.assign(area.style,{left:region.x+'px',top:region.y+'px',width:region.w+'px',height:region.h+'px'});
  const header=node('div','wb-loop-heading'), badge=node('span','wb-loop-badge','↻ 循环');
  const title=node('strong','',region.progress?.scope || region.label);
  const position=node('span','wb-loop-position',progressText(region.progress));
  header.append(badge,title,position);
  if(region.round) header.append(node('span','wb-loop-round',`第 ${region.round} 轮`));
  if(focus) header.append(button('查看循环',()=>focus(region.id),'wb-loop-focus'));
  area.append(header,node('span','wb-loop-direction','按当前批次推进 · 需要修正时回到对应步骤'));
  return area;
}

export function renderWorkBoard(container, view, actions={}) {
  const regions=loopRegions(view.structure || {}), active=regions.find(row=>row.active_node_ids?.length);
  const region=active || [...regions].sort((a,b)=>(b.report?.seq || 0)-(a.report?.seq || 0))[0];
  const board=region || view.work_board || {}, report=board.report;
  const progress=board.progress || report?.progress;
  container.hidden=!view.task.run_id && !regions.length;
  container.replaceChildren();if(container.hidden) return;
  container.setAttribute('aria-label','任务进度与问题');
  const overview=node('div','wb-work-overview'), top=node('div','wb-work-eyebrow');
  top.append(node('span','','当前任务'),node('span','wb-work-state',view.task.status || '进行中'));
  const scope=progress?.scope || report?.work_object || '当前任务进度';
  overview.append(top,node('strong','wb-work-title',scope));
  const counts=node('div','wb-work-counts');counts.append(node('strong','',progressText(progress)));
  if(progress?.current && Number.isInteger(progress.completed)) counts.append(node('span','wb-work-completed',`已处理 ${progress.completed}${progress.unit || '项'}`));
  overview.append(counts);
  const meter=progressMeter(progress);
  if(meter) {
    const track=node('div','wb-work-meter');track.setAttribute('role','progressbar');
    track.setAttribute('aria-label',meter.label);track.setAttribute('aria-valuemin','0');
    track.setAttribute('aria-valuemax',meter.max);track.setAttribute('aria-valuenow',meter.value);
    track.setAttribute('aria-valuetext',progressText(progress));
    const fill=node('span');fill.style.width=meter.percent+'%';track.append(fill);overview.append(track);
  }
  overview.append(node('p','wb-work-summary',report?.summary || report?.label || (view.task.design_stage ? '此区域会按条件循环，运行后在这里汇报进度。' : '正在整理当前处理位置，取得进展后会在这里汇报。')));
  const navigation=node('div','wb-work-navigation');
  const current=region?.active_node_ids?.[0] || report?.node_id;
  if(current) {
    const currentNode=(view.structure.nodes || []).find(row=>row.id===current);
    const title=currentNode?.work_report?.label || currentNode?.label || report?.node_label;
    navigation.append(node('span','',`当前步骤 · ${businessLabel(title)}`),button('定位当前',()=>actions.focusNode?.(current),'wb-work-locate'));
  }
  if(region) navigation.append(button(`↻ ${region.round ? `第 ${region.round} 轮 · ` : ''}查看循环范围`,()=>actions.focusLoop?.(region.id),'wb-work-locate'));
  overview.append(navigation);container.append(overview);

  const scopeIssues=(board.issues || []).filter(row=>!progress?.scope || row.scope===progress.scope);
  const pending=scopeIssues.filter(row=>row.state!=='resolved'), issue=pending[0] || scopeIssues[0];
  const problem=node('div','wb-work-problem');problem.dataset.state=issue?.state || (report?.blocker?'open':'none');
  if(issue) {
    const title=node('div','wb-work-problem-heading');
    title.append(node('span','',({open:'需要处理',fixing:'正在修正',resolved:'已修正 · 待复核'})[issue.state]),
      node('span','wb-work-feedback',`${businessLabel(issue.reporter || '')}反馈`));
    problem.append(title,node('strong','wb-work-location',issue.location),node('p','wb-work-problem-text',issue.message));
    if(issue.next_step) problem.append(node('p','wb-work-next',`接下来：${issue.next_step}`));
    if(issue.node_id) problem.append(button('查看反馈位置',()=>actions.focusNode?.(issue.origin_node_id || issue.node_id),'wb-work-locate'));
    if(pending.length>1) {
      const more=node('details','wb-work-more');more.append(node('summary','',`另有 ${pending.length-1} 个待处理问题`));
      for(const row of pending.slice(1,4)) more.append(node('p','',`${row.location}：${row.message}`));
      problem.append(more);
    }
  } else if(report?.blocker) {
    problem.append(node('strong','wb-work-problem-heading','当前受阻'),node('p','wb-work-problem-text',report.blocker));
    if(report.next_step) problem.append(node('p','wb-work-next',`接下来：${report.next_step}`));
  } else {
    problem.append(node('strong','wb-work-problem-heading',report?.findings?'最新情况':'接下来'),
      node('p','wb-work-problem-text',report?.findings || report?.next_step || '尚无具体问题汇报。'));
    if(report?.findings && report.next_step) problem.append(node('p','wb-work-next',`接下来：${report.next_step}`));
  }
  container.append(problem);
}
