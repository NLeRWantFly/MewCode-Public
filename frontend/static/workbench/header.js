import {node, button} from './dom.js';
import {mountRequirements} from './revise.js';
export const MODE_NAMES = {classic: '经典', collaborative: '协作', team: '团队'};
export function compactTitle(value, limit = 42) {
  const text = String(value || '一起完成任务').replace(/\s+/g, ' ').trim();
  return Array.from(text).length > limit ? Array.from(text).slice(0, limit).join('') + '…' : text;
}
const LABELS = {pause: '暂停', resume: '继续', cancel: '取消任务'};
export const controlBasis = task => JSON.stringify([task.id, task.run_id, task.status, task.spec_version,
  task.design_revision, task.control_epoch, task.state_version, task.team_state_version, task.controls]);

export async function controlTask({api, host, scope, task, action}) {
  if (!task.controls.includes(action)) throw new Error('任务状态已变化，请刷新后重试。');
  if(task.mode==='workflow'&&task.run_id) return api.post('/api/workbench/run-action',{...scope,run_id:task.run_id,
    task_id:task.id,operation:action==='cancel'?'stop':action,request_id:crypto.randomUUID()});
  if (task.mode === 'team') {
    if (!['pause', 'cancel'].includes(action)) throw new Error('此团队操作不可用。');
    return api.teamCommand(task.run_id, action === 'pause' ? 'suspend' : 'cancel_run', {
      command_id: crypto.randomUUID(), control_epoch: task.control_epoch,
      ...(action === 'pause' ? {expected_version: task.team_state_version} : {reason: '用户在任务工作台取消运行'}),
    });
  }
  if (action !== 'cancel' && task.design_revision != null) {
    return api.post('/api/orchestration/task-design', {...scope, action,
      expected_task_id: task.id, expected_revision: task.design_revision});
  }
  if (action === 'pause') return api.post('/api/chat/interrupt', {conversation_id: scope.conversation_id, action: 'pause'});
  return host.command({type: action === 'cancel' ? 'expert_terminate' : 'expert_resume',
    run_id: task.run_id, conversation_id: scope.conversation_id, supplemental: ''});
}

export function mountHeader(container, {api, store, host}) {
  const eyebrow = node('div', 'wb-meta'), heading = node('h1', 'wb-task-title'); heading.id = 'workbench-header-title';
  const goal = node('p', 'wb-goal'), status = node('span', 'wb-status'), activity = node('p', 'wb-activity');
  const reason = node('p', 'wb-muted'), controls = node('div', 'wb-actions'), message = node('p', 'wb-action-message');
  message.setAttribute('role', 'status');
  const details = node('details', 'wb-goal-details'), editableGoal = node('div', 'wb-goal-editor');
  details.append(node('summary', '', '查看完整目标与要求'), goal, editableGoal);
  container.replaceChildren(eyebrow, heading, status, activity, reason, controls, message, details);
  const goalUI = mountRequirements(editableGoal, {api, store});
  let current = null, owner = '', pending = false, confirm = false, buttonsReady = false;
  let submittedBasis = null;
  async function act(action) {
    if (pending || !current) return;
    if (action === 'cancel' && !confirm) { confirm = true; renderButtons(); return; }
    const snapshot = current, generation = store.generation, scope = {...store.state.scope};
    pending = true; confirm = false; submittedBasis = null; message.textContent = '正在提交…'; renderButtons();
    try {
      // Recheck the frozen view before using legacy control transports.
      const fresh = await api.readView(scope, {task_id: snapshot.task.id, run_id: snapshot.task.run_id});
      if (store.generation !== generation) return;
      if (!fresh || controlBasis(fresh.task) !== controlBasis(snapshot.task)) throw new Error('任务状态已变化，请查看最新状态后重试。');
      const response = await controlTask({api, host, scope, task: snapshot.task, action});
      if (store.generation === generation) {
        if (response?.view) store.accept(response.view, generation);
        submittedBasis = controlBasis(snapshot.task);
        message.textContent = controlBasis(current.task) === submittedBasis ? '操作已提交。' : '';
      }
    } catch (error) {
      if (store.generation === generation) message.textContent = error.message || '操作未完成，请重试。';
    } finally {
      if (store.generation === generation) { pending = false; renderButtons(); store.refresh({tasks: true}); }
    }
  }
  function renderButtons() {
    controls.replaceChildren(); buttonsReady = true;
    if (!current) return;
    if (confirm) {
      controls.append(node('p', 'wb-muted', '取消当前运行，已保存的成果与检查点会保留。'));
      const cancel = button('确认取消', () => void act('cancel'), 'wb-button wb-danger');
      controls.append(cancel, button('保留任务', () => { confirm = false; renderButtons(); }));
      cancel.focus(); return;
    }
    for (const action of current.task.controls) {
      if (!LABELS[action]) continue; // Design commands are owned by design.js.
      const label=action==='pause'&&current.task.workflow_status&&current.task.workflow_status!=='running'?'暂停节点对话':LABELS[action];
      const control = button(label, () => void act(action), 'wb-button' + (action === 'resume' ? ' wb-primary' : ''));
      control.title = {pause:'暂停当前设计或执行，保留已有进度。',resume:'从已有进度继续，沿用当前执行范围。',cancel:'停止当前运行，保留已保存成果。'}[action];
      control.disabled = pending; controls.append(control);
    }
  }
  return {
    control: act,
    update(view) {
      const nextOwner = JSON.stringify([store.generation, view.task.id, view.task.run_id]);
      const newOwner = owner !== nextOwner;
      if (newOwner) { owner = nextOwner; pending = confirm = false; submittedBasis = null; message.textContent = ''; details.open = false; }
      if (submittedBasis && submittedBasis !== controlBasis(view.task)) {
        submittedBasis = null; message.textContent = '';
      }
      const changed = JSON.stringify(current?.task.controls) !== JSON.stringify(view.task.controls)
        || current?.task.workflow_status !== view.task.workflow_status;
      current = view;
      eyebrow.textContent = view.task.purpose==='exploration' ? '探索过程' : `${MODE_NAMES[view.task.mode] || '任务'} · 规格 v${view.task.spec_version}`;
      heading.textContent = compactTitle(view.task.title); heading.title = view.task.title;
      goal.textContent = view.task.goal || view.task.title;
      const goals = (view.requirements || []).filter(row => row.kind === 'goal' && typeof row.editable === 'boolean');
      if (!goals.length && view.task.mode === 'classic' && view.task.run_id && view.task.goal) goals.push({
        id: 'fixed-goal', kind: 'goal', title: '目标', body: view.task.goal, editable: false,
        edit_reason: view.legacy ? view.legacy_message : '经典运行的目标已固定，请通过原计划修订流程更新。'});
      goalUI.update(goals, view.task);
      goal.hidden = !!goals.length;
      details.hidden = !goals.length && !goal.textContent;
      status.textContent = view.task.status; status.dataset.status = view.task.status;
      activity.textContent = view.task.current_activity;
      reason.textContent = view.legacy ? view.legacy_message : view.task.reason; reason.hidden = !reason.textContent;
      if (newOwner || changed || !buttonsReady) renderButtons();
    },
  };
}
