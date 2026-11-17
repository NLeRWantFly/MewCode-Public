import {node,button} from './dom.js';
import {evidence} from './process.js';
const STATUS={completed:'已完成',paused:'已暂停',blocked:'受阻',failed:'失败',needs_attention:'等待处理',terminated:'已终止',continuing:'待继续',review_pending:'待验收'};

// Replaces checkpoint dialogs and board-only run controls. These buttons
// prepare a host-bound inbox card; only that card can confirm a change.
export function mountRunControls(container,{api,store,openInbox}) {
  const details=node('details','wb-run-controls'),body=node('div'),message=node('p','wb-action-message'),points=node('div'),cases=node('div'),loops=node('div'),steps=node('div');
  details.append(node('summary','','检查点与恢复设置'),body);container.append(details);message.setAttribute('role','status');
  const reload=button('读取检查点与运行设置',()=>void load()),adopt=button('核验已保存的计划修订',()=>void prepare('saved_plan'));
  const mode=node('select','wb-input');mode.setAttribute('aria-label','循环运行方式');
  for(const [value,label] of [['strict','严格累计限额'],['team','持续分段执行']]){const option=node('option','',label);option.value=value;mode.append(option);}
  const setMode=button('核对运行方式',()=>void prepare('loop_mode',{mode:mode.value}));
  const loop=node('select','wb-input');loop.setAttribute('aria-label','选择循环');
  function number(label,min,max){const wrap=node('label','wb-field',label),input=node('input','wb-input');input.type='number';input.min=min;if(max)input.max=max;input.setAttribute('aria-label',label);wrap.append(input);loops.append(wrap);return input;}
  loops.append(mode,setMode,loop);
  const ceiling=number('累计 Token 硬上限',1024,5000000),rate=number('每分钟 Token 补充速度（可选）',1),burst=number('突发 Token 容量（可选）',1024);
  const tokens=button('核对 Token 总额授权',()=>{const ceilingValue=Number(ceiling.value);if(!Number.isSafeInteger(ceilingValue)||ceilingValue<1024||ceilingValue>5000000){message.textContent='请填写 1024 至 5,000,000 之间的整数。';return;}
    void prepare('loop_tokens',{loop_id:loop.value,ceiling:ceilingValue,...(rate.value?{rate_per_minute:Number(rate.value)}:{}),...(burst.value?{burst_tokens:Number(burst.value)}:{})});});loops.append(tokens);
  body.append(reload,message,points,steps,adopt,loops,cases);
  let owner='',view=null,snapshot=null,busy=false,disposed=false;
  const current=key=>!disposed&&key===owner&&key.startsWith(store.generation+':');
  function render(){details.hidden=!view?.task.run_id||['team','workflow'].includes(view.task.mode)||view.legacy;reload.disabled=busy;
    adopt.hidden=!snapshot;adopt.disabled=busy||view?.task.readonly||snapshot?.status==='running';
    loops.hidden=!snapshot?.locked_flow?.meta?.loops?.length;
    for(const control of [mode,setMode,loop,ceiling,rate,burst,tokens,...points.querySelectorAll('button'),...steps.querySelectorAll('button')])control.disabled=busy||view?.task.readonly||snapshot?.status==='running';
    for(const control of cases.querySelectorAll('button'))control.disabled=busy||view?.task.readonly;
  }
  async function load(){if(busy||!view?.task.run_id)return;const key=owner,scope={...store.state.scope};busy=true;message.textContent='正在读取…';render();
    try{const endpoint='/api/expert-runs/'+encodeURIComponent(view.task.run_id);const [run,data]=await Promise.all([api.get(endpoint),api.get(endpoint+'/checkpoints',{conversation_id:scope.conversation_id})]);
      if(!current(key))return;if(run.conversation_id!==scope.conversation_id||data.conversation_id!==scope.conversation_id||run.updated_at!==data.updated_at)throw Error('运行记录已变化，请重新读取。');
      snapshot=run;points.replaceChildren();cases.replaceChildren();
      for(const point of data.checkpoints||[]){const row=node('details','wb-evidence');row.append(node('summary','',`第 ${point.attempt||1} 轮 · ${point.label||'检查点'} · ${STATUS[point.status]||point.status}`),node('p','wb-muted',[point.description,point.created_at].filter(Boolean).join(' · ')));
        if(data.can_restore&&!view.task.readonly)row.append(button('核对恢复至此',()=>void prepare('checkpoint',{checkpoint_id:point.checkpoint_id})));points.append(row);}
      if(!data.checkpoints?.length)points.append(node('p','wb-muted','当前还没有保存的检查点。'));
      steps.replaceChildren();
      if(!view.task.readonly && ['paused','blocked','needs_attention','completed','terminated'].includes(snapshot.status)) for(const step of snapshot.locked_flow?.nodes||[]){
        const status=(snapshot.node_states||snapshot.stages||{})[step.node_id]?.status;
        const retry=['paused','blocked','needs_attention'].includes(snapshot.status)&&['failed','stalled','budget_exhausted','needs_attention','blocked','paused'].includes(status);
        if(status==='completed'||retry){const row=node('div','wb-actions');row.append(node('span','wb-muted',step.label||step.node_id),button(retry?'核对重试此步骤':'核对从此步骤后接续',()=>void prepare(retry?'retry_node':'resume_after_node',{node_id:step.node_id})));steps.append(row);}
      }
      mode.value=snapshot.loop_control_policy?.mode||'strict';loop.replaceChildren(...(snapshot.locked_flow?.meta?.loops||[]).map(row=>{const option=node('option','',row.label||row.loop_id);option.value=row.loop_id;return option;}));
      const currentCases=Object.values(snapshot.node_states||snapshot.stages||{}).map(stage=>stage.recovery_case).filter(Boolean);
      const seen=new Set();for(const value of [...currentCases,...(snapshot.events||[]).slice().reverse().map(event=>event.case)]){
        if(!value||seen.has(value.case_id))continue;seen.add(value.case_id);
        const section=node('details','wb-evidence');section.append(node('summary','',value.report?.summary||'恢复记录'));evidence(section,'调查与处理记录',value);
        if(!view.task.readonly&&!['closed','resolved'].includes(value.status))section.append(button('核对停止本次修复',()=>void prepare('stop_repair',{case_id:value.case_id})));cases.append(section);
      }
      message.textContent='已读取。选择操作后，在“待你处理”确认。';
    }catch(error){if(current(key))message.textContent=error.message;}
    finally{if(current(key)){busy=false;render();}}
  }
  async function prepare(operation,extra={}){if(busy||!snapshot||view?.task.readonly)return;const key=owner;busy=true;message.textContent='正在核对操作依据…';render();
    try{await api.post('/api/workbench/run-action',{...store.state.scope,task_id:view.task.id,run_id:view.task.run_id,expected_updated_at:snapshot.updated_at,operation,...extra});
      if(!current(key))return;message.textContent='请在“待你处理”确认本次操作。';store.refresh();openInbox();
    }catch(error){if(current(key))message.textContent=error.message;}
    finally{if(current(key)){busy=false;render();}}
  }
  details.addEventListener('toggle',()=>{if(details.open&&!snapshot)void load();});
  return {open(){details.open=true;return load();},update(next){const key=`${store.generation}:${next?.task.id||''}:${next?.task.run_id||''}`;
    if(owner!==key){owner=key;snapshot=null;busy=false;points.replaceChildren();steps.replaceChildren();cases.replaceChildren();message.textContent='';ceiling.value=rate.value=burst.value='';details.open=false;}view=next;render();},destroy(){disposed=true;}};
}
