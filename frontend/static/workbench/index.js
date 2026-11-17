import {createAPI} from './api.js';
import {createStore} from './store.js?v=20261117-exploration';
import {mountLayout} from './layout.js';
import {node, button, section, icon} from './dom.js';
import {mountHeader, compactTitle} from './header.js?v=20261117-exploration';
import {mountPlan} from './plan.js';
import {mountInbox} from './inbox.js';
import {createNotices} from './chat-notices.js';
import {mountDeliverables} from './deliverables.js';
import {mountChanges} from './changes.js';
import {mountChatRevision} from './chat-revision.js';
import {mountStructure} from './structure.js?v=20261117-exploration';
import {mountDesign} from './design.js?v=20261117-exploration';
import {mountMaintenance} from './maintenance.js';
import {mountSettings} from './settings.js?v=20261026-verifier-model';
import {mountRunControls} from './run-controls.js';
import {mountRunNotice} from './run-notice.js?v=20261117-exploration';
import {mountConnections} from './connections.js?v=20261109-blueprint';
import {mountWorkflowExport} from './export.js?v=20261117-exploration';

export function mountWorkbench(aside) {
  const api = createAPI(), store = createStore({api});
  const top = node('header', 'wb-top'), title = node('strong', 'wb-brand', 'M'); title.title = 'MewCode';
  const close = button('', () => {}, 'wb-rail-button'); close.setAttribute('aria-label', '返回对话');
  close.append(icon(['M9 3H3v18h6 M14 8l5 4-5 4 M7 12h12']),node('span','','收起')); close.title='收起工作台';close.setAttribute('aria-label','收起工作台');
  const select = node('select', 'wb-task-select'); select.setAttribute('aria-label', '选择任务和历史运行');
  const message = node('p', 'wb-view-message', '这个对话还没有任务。在对话中说说你想完成什么。'); message.setAttribute('role', 'status');
  const task = node('div', 'wb-task-content'), structure = node('div', 'wb-structure-content');
  const body = node('div','wb-workspace-body'), rail = node('nav','wb-rail'), panel = node('section','wb-task-panel');
  const chatPanel=node('section','wb-chat-panel'), chatHeading=node('header','wb-panel-header');
  const chatTitle=node('strong','','当前对话'), chatContext=node('p','wb-chat-context');
  chatPanel.setAttribute('aria-label','任务对话');
  const hideChat=button('×',()=>showChat(false),'wb-close');hideChat.setAttribute('aria-label','收起任务对话');
  chatHeading.append(chatTitle,hideChat);chatPanel.append(chatHeading,chatContext);
  const runNotice=mountRunNotice(chatPanel,{openSettings:()=>{setPanel('tasks');settings.open();}});
  rail.setAttribute('aria-label','工作台工具'); panel.setAttribute('aria-label','任务面板'); panel.hidden=true;
  const panelTitle=node('strong','','任务'), panelHeader=node('header','wb-panel-header');
  const dismissPanel=button('×',()=>setPanel('structure'),'wb-close');dismissPanel.setAttribute('aria-label','关闭任务面板');
  panelHeader.append(panelTitle,dismissPanel);panel.append(panelHeader,task);
  const regions = Object.fromEntries([
    ['header', '任务概览'], ['inbox', '待你处理'], ['design', '任务要求'], ['plan', '任务计划'],
    ['deliverables', '交付物'], ['changes', '变更记录'],
  ].map(([id, label]) => [id, section(id, label)]));
  task.append(...Object.values(regions));
  const chatHost = window.MewWorkbenchHost;
  const revealChat = () => {layout.open();showChat(true);};
  const deps = {api, store, host: {...chatHost,
    designStarted: data => { revealChat(); chatHost?.designStarted?.(data); },
    leaveDesign: () => { revealChat(); return chatHost?.leaveDesign?.(); },
  }};
  const overview = node('div'), designControls = node('div'); regions.header.replaceChildren(overview, designControls);
  regions.design.replaceChildren();
  const header = mountHeader(overview, deps), plan = mountPlan(regions.plan, deps);
  const design = mountDesign(designControls, regions.design, {...deps, openGraph:()=>setPanel('structure')});
  const deliverables = mountDeliverables(regions.deliverables, deps), changes = mountChanges(regions.changes);
  let requestedItem = '';
  function openItem(id, task = store.state.view?.task) {
    setPanel('tasks'); layout.open();
    if (task && (store.state.view?.task.id !== task.id || store.state.view?.task.run_id !== task.run_id)) {
      requestedItem = id; store.selectTask(task); return true;
    }
    return inbox.open(id);
  }
  const notices = createNotices(deps.host, openItem);
  const inbox = mountInbox(regions.inbox, {...deps, notices, openStep: id => { layout.open(); setPanel('tasks'); plan.openStep(id); }});
  function railButton(label,paths,action) {
    const control=button('',action,'wb-rail-button');control.title=label;control.setAttribute('aria-label',label);
    control.append(icon(paths),node('span','',label));rail.append(control);return control;
  }
  const chatButton=railButton('对话',['M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7H3l1-5a8.5 8.5 0 1 1 17-2z'],()=>showChat(chatPanel.hidden));
  chatButton.setAttribute('aria-pressed','true');
  const nodesButton=railButton('节点',['M3 3h7v7H3z M14 14h7v7h-7z M14 3h7v7h-7z M3 14h7v7H3z'],()=>structureUI.toggleNodes());
  const tasksButton=railButton('任务',['M4 4h16v16H4z M8 8h8 M8 12h8 M8 16h5'],()=>setPanel(panel.hidden?'tasks':'structure'));
  railButton('产物',['M3 7V4h6l2 3h10v13H3z'],()=>{setPanel('tasks');panelTitle.textContent='产物';regions.deliverables.scrollIntoView({block:'start'});});
  railButton('记录',['M3 11a9 9 0 1 1 3 7 M3 4v7h7 M12 7v5l3 2'],()=>{setPanel('tasks');panelTitle.textContent='记录';regions.changes.scrollIntoView({block:'start'});});
  let connections=null;
  const connectionsButton=window.top===window?railButton('连接运行',['M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-2 2 M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l2-2'],()=>connections.browse()):null;
  connectionsButton?.setAttribute('aria-label','连接的运行');
  connectionsButton?.setAttribute('aria-keyshortcuts','Control+Shift+O');
  const railBottom=node('div','wb-rail-bottom');
  const themeButton=railButton('切换主题',['M12 3a9 9 0 1 0 9 9 7 7 0 0 1-9-9z'],()=>chatHost?.toggleTheme?.());
  const themeSwitch=node('div','wb-theme-switch');themeSwitch.setAttribute('role','group');themeSwitch.setAttribute('aria-label','界面主题');
  const themeControls=['light','dark'].map(theme=>{
    const label=theme==='light'?'浅色':'深色';
    const control=button(label,()=>{
      if(document.documentElement.getAttribute('data-theme')!==theme) chatHost?.toggleTheme?.();
      updateTheme();
    },'wb-theme-option');
    control.setAttribute('aria-label',`使用${label}主题`);themeSwitch.append(control);return control;
  });
  function updateTheme() {
    const dark=document.documentElement.getAttribute('data-theme')==='dark';
    themeControls.forEach((control,index)=>control.setAttribute('aria-pressed',String(index===(dark?1:0))));
    themeButton.title=dark?'切换到浅色主题':'切换到深色主题';
  }
  updateTheme();
  const settingsButton=railButton('设置',['M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1z'],()=>{setPanel('tasks');panelTitle.textContent='设置';settings.open();});
  railBottom.append(themeButton,settingsButton,close);rail.append(railBottom);
  let runPending=false;
  const runState=node('span','wb-run-state'), run=button('运行',async()=>{
    if(runPending)return;
    const snapshot=store.state.view?.task,generation=store.generation;
    if(!snapshot?.run_id && snapshot?.design_revision == null) {revealChat();return;}
    const requested=(snapshot?.workflow_status||snapshot?.execution_status)==='needs_input' || snapshot?.controls?.includes('resume')?'resume':'pause';
    runPending=true;updateRunControl(store.state);
    try {
      if(snapshot?.mode==='workflow' && snapshot.run_id) {
        const fresh=await api.readView({...store.state.scope},{task_id:snapshot.id,run_id:snapshot.run_id});
        if(store.generation!==generation)return;
        if(!fresh || fresh.task.id!==snapshot.id || fresh.task.run_id!==snapshot.run_id)throw Error('任务已变化，请重新选择。');
        store.accept(fresh,generation);
        header.update(store.state.view);
      }
      const value=store.state.view?.task;
      setPanel('tasks');
      if((value?.workflow_status||value?.execution_status)==='needs_input' && value.waiting_node_ids?.length) {
        plan.update(store.state.view);plan.openStep(value.waiting_node_ids[0]);return;
      }
      if(value?.controls?.includes(requested)) {
        await header.control(requested);
      } else if(!value?.run_id) {
        if(value?.design_revision != null) design.execute();
        else revealChat();
      }
    } catch(error) {
      if(store.generation===generation){message.textContent=error.message || '操作未完成，请重试。';message.hidden=false;}
    } finally {runPending=false;updateRunControl(store.state);}
  },'wb-button wb-run-button');
  function updateRunControl(state) {
    const selectedTask=state.view?.task;
    run.hidden=selectedTask?.purpose==='exploration';
    runState.textContent=selectedTask?.status || '就绪';
    runState.dataset.status=selectedTask?.status || '就绪';
    const control=selectedTask?.controls?.includes('resume')?'resume':selectedTask?.controls?.includes('pause')?'pause':null;
    const waiting=(selectedTask?.workflow_status||selectedTask?.execution_status)==='needs_input' && selectedTask.waiting_node_ids?.length;
    const pauseLabel=selectedTask?.workflow_status&&selectedTask.workflow_status!=='running'?'Ⅱ 暂停对话':'Ⅱ 暂停';
    const ordinary=!selectedTask?.run_id && selectedTask?.design_revision == null;
    run.textContent=ordinary?'继续对话':waiting?'处理等待':control==='resume'?'▶ 继续':control==='pause'?pauseLabel:'▶ 运行';
    run.disabled=runPending || !!selectedTask?.readonly || !state.scope.conversation_id ||
      (!ordinary && (state.loading || !!state.error || (!control && !waiting && !!selectedTask?.run_id)));
  }
  const compose=button('✦ AI 构图',()=>design.compose({generate:true}),'wb-button wb-compose-button');
  compose.title='根据当前对话中的目标、资料和要求生成或更新编排图';
  let exporter=null;
  const extract=button('提取结构',()=>exporter.open(),'wb-button');extract.disabled=true;
  extract.setAttribute('aria-keyshortcuts','Control+Shift+E');extract.title='提取完整结构与对话摘要 · Ctrl+Shift+E';
  top.append(title,select,themeSwitch,runState,extract,compose,run);
  body.append(rail,chatPanel,structure,panel);aside.append(top,message,body);
  function showChat(value) {
    const change = () => {
      chatPanel.hidden=!value;chatButton.setAttribute('aria-pressed',String(value));
      if (value) {setPanel('structure');document.getElementById('chat-input')?.focus({preventScroll:true});}
    };
    if (window.MewChatViewport) window.MewChatViewport.preserve(change); else change();
  }
  function setPanel(value) {
    panel.hidden=value!=='tasks';panelTitle.textContent='任务';tasksButton.setAttribute('aria-pressed',String(!panel.hidden));
    if (!panel.hidden) structureUI.toggleNodes(false);
  }
  const layout = mountLayout({aside, close, toggle: document.getElementById('workbench-toggle'),
    chatSlot:chatPanel,
    separator: document.getElementById('workbench-resize'), backdrop: document.getElementById('workbench-backdrop')});
  if(connectionsButton)connections=mountConnections(aside,{api,openWorkspace:()=>layout.open(),opener:connectionsButton});
  exporter=mountWorkflowExport(aside,{api,store,opener:extract});
  const chatRevision = mountChatRevision({...deps, openInbox: () => { setPanel('tasks'); layout.open(); regions.inbox.scrollIntoView({block: 'start'}); }});
  const structureUI = mountStructure(structure,{
    api,store,
    onMutate: async (view, operation, positions) => {
      const scope={...store.state.scope}, generation=store.generation;
      if (chatHost?.busy?.() && view.task.mode!=='workflow') throw new Error('请等当前对话完成后编辑。');
      const result=await api.post('/api/workbench/graph/edit', {...scope, task_id:view.task.id || '',
        run_id:view.task.run_id || '', basis:view.editor?.basis ?? '', operation,
        ...(positions ? {positions} : {})});
      if (generation === store.generation) {
        store.accept(result.view,generation);structureUI.update(result.view);store.refresh({tasks:true});
      }
      return result;
    },
    openStep:id=>{setPanel('tasks');plan.openStep(id);},
    onCompose:()=>{setPanel('tasks');design.compose({generate:true});},
    onEdit:async item=>{
      if (await design.compose()) chatHost?.composeMessage?.(`修改节点 ID: ${item.id}（${item.label}），保留此 ID：`);
    },
    onFocusMode:value=>{
      const change=()=>{aside.dataset.focusMode=String(value);};
      if(window.MewChatViewport) window.MewChatViewport.preserve(change);else change();
    },
    onNodesToggle:value=>nodesButton.setAttribute('aria-pressed',String(value)),
  });
  const maintenanceArea=node('div','wb-maintenance-area');task.append(maintenanceArea);
  const maintenance=mountMaintenance(maintenanceArea,{...deps,openInbox:()=>{regions.inbox.scrollIntoView({block:'start'});}});
  const settingsArea=node('div');task.append(settingsArea);const settings=mountSettings(settingsArea,deps);
  const runControlsArea=node('div');task.append(runControlsArea);const runControls=mountRunControls(runControlsArea,{...deps,openInbox:()=>regions.inbox.scrollIntoView({block:'start'})});
  let listKey = '';
  select.addEventListener('change', () => {
    const row = store.state.tasks[Number(select.value)];
    if (row) store.selectTask(row);
  });
  store.subscribe(state => {
    const list = JSON.stringify([state.tasks, state.view?.task?.id, state.view?.task?.run_id]);
    if (listKey !== list) {
      listKey = list;
      select.replaceChildren(...state.tasks.map((row, i) => {
        const option = node('option', '', `${compactTitle(row.title,28)} · ${row.status}${row.readonly&&row.purpose!=='exploration' ? ' · 历史运行' : ''}`);
        option.value = String(i);
        option.selected = row.id === state.view?.task?.id && row.run_id === state.view?.task?.run_id;
        return option;
      }));
      if (!state.tasks.length) {const option=node('option','','当前对话');select.append(option);}
      select.disabled=!state.tasks.length;
    }
    message.textContent = state.error || (state.loading ? '正在读取任务视图…' : ''); message.hidden = !message.textContent;
    task.hidden = !state.scope.conversation_id;
    overview.hidden = !state.view;
    for (const name of ['inbox','plan','deliverables','changes']) regions[name].hidden = !state.view;
    if (state.view?.task.design_stage) regions.plan.hidden = true;
    if (state.view?.task.design_stage) for (const name of ['inbox','deliverables','changes']) regions[name].hidden = !state.view[name]?.length;
    const selectedTask=state.view?.task;
    const designing=selectedTask?.design_revision != null;
    designControls.hidden=!designing;
    regions.design.hidden=!designing;
    if(designing) design.update(state.view); else design.reset();
    chatContext.textContent=selectedTask?.purpose==='exploration'?'在对话中推进任务和修改路线；图展示公开的执行过程。':selectedTask?.readonly ? '正在查看历史运行；此处仍是当前对话。'
      : '直接对话完成任务；需要分工时，节点和输入输出会随执行显示。';
    updateRunControl(state);
    compose.disabled=state.loading || !!selectedTask?.readonly || !!selectedTask?.run_id || !state.scope.conversation_id;
    compose.hidden=!designing;
    extract.hidden=!selectedTask?.run_id||selectedTask.purpose==='exploration'&&selectedTask.execution_status!=='completed';
    extract.textContent=selectedTask?.purpose==='exploration'?'提取为可复用流程':'提取结构';
    const supplement=document.getElementById('continue-task-btn');
    if(supplement)supplement.hidden=selectedTask?.purpose==='exploration';
    maintenance.update(state.view);
    maintenanceArea.hidden = !state.view?.task.run_id;
    settings.update(state.view);
    runNotice.update(state.view);
    runControls.update(state.view);
    if (!state.view) {
      chatRevision.update(null);
      inbox.update(null);
      if (!message.textContent) { message.textContent = '这个对话还没有任务。在对话中说说你想完成什么。'; message.hidden = false; }
      structureUI.update({task:{id:'',run_id:'',conversation_id:state.scope.conversation_id},editor:{planning:true,editable:false,basis:''},structure:{workflow:true,nodes:[],edges:[],groups:[],versions:[]},plan:[]});
      return;
    }
    const view = state.view;
    header.update(view); plan.update(view); inbox.update(view);
    chatRevision.update(view);
    if (requestedItem && inbox.open(requestedItem)) requestedItem = '';
    deliverables.update(view); changes.update(view);
    structureUI.update(view);
  });
  function context() {
    const value = window.MewWorkbenchHost?.context() || {};
    return {conversation_id: value.readonly ? '' : value.conversationId || '', project: value.project || ''};
  }
  const abort = new AbortController(), signal = abort.signal;
  window.addEventListener('mewcode:theme',updateTheme,{signal});
  for(const entry of document.querySelectorAll('[data-open-workbench]')) entry.addEventListener('click',()=>{setPanel('structure');layout.open();},{signal});
  window.addEventListener('mewcode:conversation-context', () => {
    store.setScope(context());
    design.refreshContext();
  }, {signal});
  window.addEventListener('mewcode:workbench-state', () => store.reconnect(context()), {signal});
  window.addEventListener('mewcode:workbench-changed', event => store.changed(event.detail), {signal});
  window.addEventListener('keydown', event => {
    if (connections && event.ctrlKey && event.shiftKey && !event.altKey && event.key.toLowerCase()==='o') {
      event.preventDefault();void connections.openCurrent();
    }
    if (event.ctrlKey && event.shiftKey && !event.altKey && event.key.toLowerCase()==='e') {
      event.preventDefault();
      if(!connections?.exportCurrent()){layout.open();void exporter.open();}
    }
  }, {signal});
  store.setScope(context());
  const requested = new URLSearchParams(location.search).get('workbench');
  if (requested) { setPanel(requested === 'structure' ? 'structure' : 'tasks'); layout.open(); }
  return {api, store, layout, regions, connections, exporter, open(tab = 'structure') { setPanel(tab); layout.open(); },
    chatContext() {
      if (!layout.isOpen()) return null;
      const state=store.state, task=!state.loading && !state.error ? state.view?.task : null;
      return {...state.scope, task_id:task?.id || '', run_id:task?.run_id || ''};
    },
    chatContinuation() {
      const state=store.state, task=!state.loading&&!state.error?state.view?.task:null;
      return task?.purpose==='exploration'&&task.execution_status!=='completed'
        ? {conversation_id:state.scope.conversation_id,task_id:task.id} : null;
    },
    openStep(id) { setPanel('tasks'); layout.open(); return plan.openStep(id); },
    openItem,
    openMaintenance(options={}) {
      setPanel('tasks');layout.open();
      if(options.conversation_id && options.conversation_id!==store.state.scope.conversation_id || options.project && options.project!==store.state.scope.project) return false;
      return maintenance.open(options.mode||'fix',options.request||'',options.fresh===true,options.node_ids||[],options.id||'');
    },
    openSettings(){setPanel('tasks');layout.open();return settings.open();},
    openCheckpoints(){setPanel('tasks');layout.open();return runControls.open();},
    openSource(type, sourceId) {
      const item = store.state.view?.inbox.find(row => row.type === type && row.source_id === sourceId);
      if (item) return openItem(item.id);
      setPanel('tasks'); layout.open(); store.refresh(); return false;
    },
    destroy() { abort.abort(); connections?.destroy(); exporter.destroy(); structureUI.destroy(); runControls.destroy(); settings.destroy(); maintenance.destroy(); design.destroy(); chatRevision.destroy(); inbox.destroy(); plan.destroy(); store.dispose(); layout.destroy(); aside.replaceChildren(); }};
}

const aside = document.getElementById('task-workbench');
if (aside && !document.body.classList.contains('mobile-runtime')) window.MewWorkbench = mountWorkbench(aside);
