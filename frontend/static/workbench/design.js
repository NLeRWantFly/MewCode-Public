import {node, button} from './dom.js';
import {mountDesignSources} from './design-sources.js';
import {mountGeneration} from './generation.js?v=20261109-native-planning';
const ENDPOINT = '/api/orchestration/task-design';
const executionAuthorized = design => design?.intent === 'execute' && design.intent_authority?.source === 'host_entry'
  && design.intent_authority?.intent === 'execute' && !design.intent_authority?.revoked;

export function designGuidance(design) {
  if (!design) return {step:0, title:'AI 构图', detail:'描述目标，生成节点与连接。', intent:''};
  const authorized = executionAuthorized(design);
  const intent = design.intent === 'plan' ? '仅保存编排'
    : authorized ? '允许执行 · 按已确认范围启动'
      : design.intent_authority?.revoked ? '执行未授权 · 原执行选择已失效，需要重新确认' : '执行未授权 · 需要明确选择是否执行';
  if (design.run_id) return {step:2, title:'运行与交付', detail:'在编排图中查看进度。', intent};
  if (['queued','building'].includes(design.status)) return {step:1, title:'正在构图', detail:'', intent};
  if (design.status === 'ready') return {step:1, title:'编排已保存', detail:authorized ? '等待启动。' : '继续对话可修改编排。', intent};
  if (['paused','failed','blocked'].includes(design.status)) return {step:1, title:design.status==='paused'?'构图已暂停':'构图未完成', detail:'处理当前问题后，可从已有进度继续。', intent};
  if (design.intent_authority?.revoked) return {step:0, title:'确认执行范围', detail:'选择仅保存编排，或允许执行。', intent};
  return {step:0, title:design.goal ? '继续构图' : 'AI 构图', detail:'在对话中补充输入、目标与限制。', intent};
}

export function mountDesign(controls, specification, {api, store, host, openGraph}) {
  const entry = node('div', 'wb-design-start'), introduction = node('p', 'wb-muted');
  const nextTitle = node('h2','wb-section-title');
  const mode = node('select'); mode.setAttribute('aria-label', '任务编排模式');
  for (const [value,label] of [['classic','工作流']]) {const option = node('option','',label);option.value=value;mode.append(option);} mode.value = 'classic';
  const actions = node('div','wb-actions'), message = node('p','wb-action-message'), intent = node('p','wb-muted'); message.setAttribute('role','status');
  const plan = button('只生成编排图', () => void (design ? restrict() : start('plan'))), execute = button('设计并执行', () => void (design ? requestExecution() : start('execute')), 'wb-button wb-primary');
  const narrow = button('仅设计保存', () => void restrict());
  const leave = button('返回普通对话', () => {if(!busy && host?.leaveDesign?.()) message.textContent='已返回普通对话。';});
  const navigation = node('div','wb-actions');
  const chat = button('继续对话', () => {
    if (busy || loading || !loaded || host?.busy?.() || !design) return;
    host?.designStarted?.({conversation_id:store.state.scope.conversation_id});
    message.textContent='在图旁的对话中补充要求。';
  }, 'wb-button wb-primary');
  const graph = button('查看编排图', () => openGraph?.()); navigation.append(chat, graph);
  plan.title='新任务：进入设计，生成并保存编排后停下。已有任务：改为仅设计保存。';
  execute.title='允许主模型继续设计，并在编排校验通过后按已确认范围执行。';
  leave.title='把后续消息切回普通对话；不会暂停或取消已有任务。';
  actions.append(plan,execute,narrow,leave);
  const retry = button('重新读取任务', () => void refresh());
  const confirmation = node('section','wb-design-confirm'), explanation = node('p','', '继续当前设计，并按已确认范围执行。');
  const confirmActions = node('div','wb-actions'), confirm = button('确认设计并执行', () => void confirmExecution(), 'wb-button wb-primary');
  confirmation.setAttribute('aria-label','确认任务执行');
  confirmActions.append(confirm,button('暂不执行', () => {pendingConfirmation=null;render();}));
  confirmation.append(explanation,confirmActions);
  entry.append(nextTitle,introduction,mode,intent,navigation,actions,confirmation,message,retry);
  const progress = node('div','wb-generation'); controls.append(entry,progress);
  const heading = node('h2','wb-section-title','任务要求'), rows = node('div'), empty = node('p','wb-muted');
  heading.id = 'workbench-design-title';
  specification.append(heading,empty,rows);
  const sources = mountDesignSources(rows,{api,store}), generation = mountGeneration(progress,{api,store});
  let owner = '', signature = '', task = null, design = null, specifications = null, busy = false, loading = true, loaded = false, reading = 0, disposed = false, pendingConfirmation = null;
  const current = key => !disposed && owner === key && owner.startsWith(store.generation + ':');
  const canConfirm = () => design && !design.run_id && !design.build_request?.execution_claim_id && !design.build_request?.execution_state
    && ['designing','paused','failed','blocked','needs_input','ready'].includes(design.status)
    && (design.intent === 'plan' || design.intent_authority?.revoked || design.intent_authority?.source !== 'host_entry' || design.intent_authority?.intent !== 'execute');
  function render() {
    const writable = !!store.state.scope.conversation_id && !task?.readonly && !host?.context?.().readonly;
    controls.hidden = !writable || (!!task?.run_id && task.design_revision == null);
    entry.hidden = !!task?.run_id && !design;
    mode.hidden = true;
    specification.hidden = !task?.design_stage;
    mode.disabled = !!design || busy || loading;
    if (design) mode.value = design.orchestration_mode;
    const blocked = busy || loading || !loaded || !writable;
    const guidance = designGuidance(design);
    nextTitle.textContent = guidance.title; introduction.textContent = guidance.detail;
    introduction.hidden = !guidance.detail;
    chat.hidden = !design || !!task?.run_id || ['queued','building','completed'].includes(design.status);
    chat.disabled = blocked || !!host?.busy?.();
    chat.className = 'wb-button' + (design?.intent_authority?.revoked ? '' : ' wb-primary');
    plan.className = 'wb-button' + (design?.intent_authority?.revoked ? ' wb-primary' : '');
    execute.className = 'wb-button' + (design?.intent_authority?.revoked ? '' : ' wb-primary');
    graph.hidden = !openGraph; graph.disabled = !task;
    navigation.hidden = chat.hidden && graph.hidden;
    plan.disabled = blocked || (design ? design.intent !== 'execute' || design.status === 'completed' : !!host?.busy?.());
    execute.disabled = blocked || (design && !canConfirm()) || !!host?.busy?.();
    if (pendingConfirmation && (pendingConfirmation.expected_task_id !== design?.task_id || pendingConfirmation.expected_revision !== design?.revision)) pendingConfirmation=null;
    confirmation.hidden = !pendingConfirmation;
    confirm.disabled = blocked || !canConfirm() || !!host?.busy?.();
    plan.textContent = design ? '改为仅设计保存' : '只生成编排图';
    plan.hidden = !!task?.run_id || design?.intent === 'plan' || design?.status === 'completed';
    execute.hidden = !!task?.run_id || executionAuthorized(design) || design?.status === 'completed';
    narrow.hidden = !task?.run_id || !design || design.intent !== 'execute' || design.status === 'completed'; narrow.disabled = blocked;
    leave.hidden = !design; leave.disabled = blocked;
    retry.hidden = loading || loaded; retry.disabled = loading || busy;
    intent.textContent = guidance.intent;
    intent.hidden = !guidance.intent;
    intent.dataset.warning = String(!!design && design.intent === 'execute' && !executionAuthorized(design));
    const entries=specifications ?? design?.specification?.entries ?? [];
    empty.hidden = !!entries.length;
    empty.textContent = design?.goal || task?.goal ? '目标已记录，可继续对话补充。' : '对话中的要求将显示在这里。';
    sources.update(entries, task || {});
    generation.update(task?.design_stage ? design : null);
  }
  async function refresh() {
    const key = owner, read = ++reading;
    loading = true; render();
    try {
      const data = await api.get(ENDPOINT,{...store.state.scope});
      if (!current(key) || read !== reading) return;
      if (data.conversation_id !== store.state.scope.conversation_id) throw new Error('项目或对话已切换，请回到任务所属对话。');
      design = !task || data.task_design?.task_id === task.id ? data.task_design : null;
      loaded = true;
    } catch(error) {if(current(key) && read === reading) {loaded=false;message.textContent = error.message;}}
    finally {if(current(key) && read === reading) {loading=false;render();}}
  }
  async function submit(body, generate = false) {
    const key = owner, scope = {...store.state.scope};
    busy = true; message.textContent = '正在提交…'; render();
    try {
      const data = await api.post(ENDPOINT,{...scope,...body});
      if (!current(key)) return;
      design = data.task_design; message.textContent = data.continuation?.message || design?.reason || '任务已创建，请在对话中填写或补充目标。';
      store.refresh({tasks:true}); host?.designStarted?.(data);
      if (generate) host?.buildGraph?.(scope);
      await refresh();
      return true;
    } catch(error) {if(current(key)) {message.textContent = error.message;if(error.status===409)await refresh();}}
    finally {if(current(key)) {busy=false;render();}}
  }
  async function start(intent, generate = false) {if(busy||loading||!loaded||design||host?.busy?.())return false;return submit({action:'start',intent,orchestration_mode:mode.value},generate);}
  async function restrict() {if(busy||loading||!loaded||!design)return;await submit({action:'set_intent',intent:'plan',expected_task_id:design.task_id,expected_revision:design.revision});}
  function requestExecution() {
    if(busy||loading||!loaded||host?.busy?.()||!canConfirm())return;
    pendingConfirmation={expected_task_id:design.task_id,expected_revision:design.revision};render();
  }
  async function confirmExecution() {
    if(busy||loading||!loaded||host?.busy?.()||!canConfirm()||!pendingConfirmation)return;
    const binding=pendingConfirmation;pendingConfirmation=null;
    await submit({action:'confirm_execute',confirm_execution:true,...binding});
  }
  render();
  return {update(view) {
    const nextOwner = `${store.generation}:${view?.task.id || ''}:${view?.task.run_id || ''}`;
    const nextSignature = JSON.stringify([view?.version,store.state.scope]);
    task = view?.task || null;
    specifications = task?.design_stage && Array.isArray(view.requirements) ? view.requirements : null;
    if(owner !== nextOwner) {owner=nextOwner;signature='';design=null;busy=false;loaded=false;loading=true;pendingConfirmation=null;message.textContent='';mode.value='classic';reading++;generation.update(null);}
    render();
    if(signature !== nextSignature) {signature=nextSignature;if(store.state.scope.conversation_id && (!task || task.design_revision != null)) void refresh();else loading=false;}
  }, compose({generate = false} = {}) {
    if (busy || loading || !loaded || host?.busy?.() || task?.readonly || task?.run_id || host?.context?.().readonly) return false;
    if (design) {
      host?.designStarted?.({conversation_id:store.state.scope.conversation_id});
      if (generate) host?.buildGraph?.({...store.state.scope});
      return true;
    }
    return start('plan',generate);
  }, reset() {
    // Invalidate late design reads when returning to ordinary conversation.
    owner='';signature='';task=null;design=null;loaded=false;loading=false;
    pendingConfirmation=null;reading++;generation.update(null);
  }, execute() {
    if (task?.readonly || host?.context?.().readonly) return;
    return design ? requestExecution() : start('execute');
  }, refresh, refreshContext: render, destroy() {disposed=true;reading++;generation.destroy();}};
}
