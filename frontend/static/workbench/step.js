import {node, button} from './dom.js';
import {evidence, textValue, renderProcess} from './process.js';
import {mountLessons} from './lessons.js';
import {mountRequirements} from './revise.js';
import {mountWorkflowStep} from './workflow-node.js?v=20261117-exploration';

export const isCurrent = step => ['进行中', '恢复中', '部分受阻', '验收中', '等你处理', '需返工', '已暂停', '已阻塞'].includes(step.status);
function lines(parent, title, rows) {
  if (!rows?.length) return;
  const block = node('div', 'wb-requirements'); block.append(node('h4', '', title));
  for (const row of rows) block.append(node('p', '', typeof row === 'string' ? row : row.body || row.description || row.name || textValue(row)));
  parent.append(block);
}
export function renderFacts(parent, step) {
  parent.replaceChildren();
  const detail = step.detail;
  if(step.status_reason) parent.append(node('p','wb-node-status-reason',step.status_reason));
  if (!detail) { parent.append(node('p', 'wb-muted', '此历史格式没有可展开的合同与验收信息。')); return; }
  if (detail.goal) { parent.append(node('h4', '', '合同目标'), node('p', '', detail.goal)); }
  if (detail.expanded_from) parent.append(node('p', 'wb-muted',
    '所属任务：' + (detail.parent_label || detail.expanded_from.contract_id) + ' · 第 ' + detail.expanded_from.contract_version + ' 版'));
  if (detail.join_children?.length) lines(parent, '汇合前须验收的子任务', detail.join_children.map(row => row.label));
  if (detail.workspace) {
    const workspace = detail.workspace;
    const status = {preparing: '准备中', active: '执行中', sealed: '已固定交付', conflict: '存在合并冲突'}[workspace.status] || '待核对';
    lines(parent, workspace.mode === 'merge' ? '合并工作区' : '独立工作区', [status, workspace.path]);
    if (workspace.conflicts?.length) lines(parent, '待解决的冲突文件', workspace.conflicts);
    evidence(parent, '查看提交与输入版本', workspace);
  }
  if (detail.budget?.length) lines(parent, '当前分支累计预算', detail.budget.map(row =>
    row.label + '：已用 ' + row.used + '，预留 ' + row.reserved +
      (row.allocation_mode === 'elastic' ? '，弹性共享，任务总额剩余 ' + row.shared_remaining : '，上限 ' + row.limit)));
  lines(parent, '适用约束', detail.constraints?.filter(row => typeof row.editable !== 'boolean'));
  lines(parent, '验收标准', detail.requirements?.filter(row => typeof row.editable !== 'boolean'));
  lines(parent, '禁止变更', detail.forbidden_changes);
  if (detail.method && !detail.method_entry) { parent.append(node('h4', '', '指定方法'), node('p', '', detail.method.description || textValue(detail.method))); }
  const checks = node('div', 'wb-checks'); checks.append(node('h4', '', '验收结果与证据'));
  for (const check of detail.checks || []) {
    const passed = check.passed ?? check.accepted;
    const row = node('div', 'wb-check');
    const label = node('span', 'wb-check-state', check.reviewed ? '集中复核通过' : check.deferred ? '待集中复核' : passed === true ? '通过' : passed === false ? (check.blocking === false ? '待修订' : '未通过') : '待核验');
    label.dataset.passed = check.reviewed ? 'true' : check.deferred ? 'undefined' : String(passed);
    row.append(label, node('span', '', check.name || check.criterion || '验收检查'));
    evidence(row, '查看证据', check.evidence || check.scores || check);
    checks.append(row);
  }
  if (!(detail.checks || []).length) checks.append(node('p', 'wb-muted', '尚无逐项验收记录。'));
  parent.append(checks);
  if (detail.artifacts?.length) {
    const outputs = node('div', 'wb-step-artifacts'); outputs.append(node('h4', '', '本次产物'));
    for (const artifact of detail.artifacts) evidence(outputs, artifact.name || artifact.path || '产物', artifact);
    parent.append(outputs);
  }
}

export function mountStep(step, {api, store, task, host}) {
  if (task.mode === 'workflow') return mountWorkflowStep(step,{api,store,task});
  const element = node('details', 'wb-step'), summary = node('summary', 'wb-step-summary');
  element.dataset.stepId = step.id;
  const dot = node('span', 'wb-step-dot'), name = node('span', 'wb-step-name'), status = node('span', 'wb-step-status');
  dot.setAttribute('aria-hidden', 'true'); summary.append(dot, name, status);
  const body = node('div', 'wb-step-body'), meta = node('p', 'wb-muted'), facts = node('div', 'wb-step-facts');
  const actions = node('div', 'wb-actions'), process = node('div', 'wb-process'), lessons = node('div', 'wb-lessons');
  process.hidden = lessons.hidden = true;
  const showProcess = button('查看过程', () => void loadProcess());
  const trajectory = button('在执行过程中查看', () => void locate());
  const showLessons = button('管理经验', () => void loadLessons());
  actions.append(showProcess, trajectory, showLessons); body.append(meta, facts, actions, process, lessons); element.append(summary, body);
  const requirements = node('div', 'wb-step-requirements'); body.insertBefore(requirements, actions);
  const requirementsUI = mountRequirements(requirements, {api, store});
  let current = step, contractKey = '', previousStatus = '', snapshot = null, pending = null, lessonsUI = null;
  const owner = store.generation, scope = {...store.state.scope}, run = task.run_id;
  const active = () => owner === store.generation && element.isConnected;
  async function read() {
    if (pending) return pending;
    const path = task.mode === 'team' ? '/api/team-v2/tasks/' + encodeURIComponent(step.id) + '/history'
      : '/api/expert-runs/' + encodeURIComponent(run);
    const work = api.get(path, task.mode === 'team' ? {} : {node_id: step.id}); pending = work;
    try { const value = await work; if (active()) snapshot = value.snapshot || value; return snapshot; }
    finally { if (pending === work) pending = null; }
  }
  async function loadProcess() {
    if (!active()) return;
    process.hidden = false; showProcess.disabled = true; process.replaceChildren(node('p', 'wb-muted', '正在读取过程…'));
    try {
      const value = await read(); if (!active()) return;
      renderProcess(process, value, step.id, task.mode); showProcess.textContent = '刷新过程';
    } catch (error) { if (active()) process.replaceChildren(node('p', 'wb-error', error.message || '过程暂时无法读取。')); }
    finally { if (active()) showProcess.disabled = false; }
  }
  async function locate() {
    trajectory.disabled = true;
    try {
      const value = snapshot || await read(); if (!active()) return;
      if (host.showStep({conversation_id: scope.conversation_id, run_id: run, node_id: step.id, snapshot: value}) === false)
        throw new Error('当前对话中没有这个步骤的执行过程。');
    } catch (error) { if (active()) { process.hidden = false; process.replaceChildren(node('p', 'wb-error', error.message)); } }
    finally { if (active()) trajectory.disabled = false; }
  }
  async function loadLessons() {
    lessons.hidden = false; showLessons.disabled = true;
    try {
      const value = snapshot || await read(); if (!active()) return;
      const flowId = value.flow_id || value.locked_flow?.flow_id;
      if (!flowId) { lessons.replaceChildren(node('p', 'wb-muted', '这个运行没有可关联的流程经验。')); return; }
      lessonsUI?.destroy(); lessonsUI = mountLessons(lessons, {api, flowId, stepId: step.id, active,
        onChanged: () => store.refresh()});
    } catch (error) { if (active()) lessons.replaceChildren(node('p', 'wb-error', error.message)); }
    finally { if (active()) showLessons.disabled = false; }
  }
  const result = {
    element,
    update(next, nextTask = task, entries = []) {
      current = next; name.textContent = next.name; status.textContent = next.status; element.dataset.status = next.status;
      if (previousStatus !== next.status && isCurrent(next)) element.open = true;
      previousStatus = next.status;
      const acceptance = next.acceptance;
      meta.textContent = `${next.attempts} 次尝试` + (acceptance ? ` · ${acceptance.status || acceptance.summary || ''}` +
        (typeof acceptance.total === 'number' ? `（${acceptance.passed}/${acceptance.total}）` : '') : '');
      const method = entries.find(row => row.kind === 'method' && row.id === next.id);
      const detail = next.detail && {...next.detail, method_entry: method};
      const key = JSON.stringify([detail,next.status_reason]);
      if (key !== contractKey) { contractKey = key; renderFacts(facts, {...next, detail}); }
      requirementsUI.update([...(next.detail?.constraints || []), ...(next.detail?.requirements || []), ...(method ? [method] : [])]
        .filter(row => row.kind !== 'goal' && typeof row.editable === 'boolean'), nextTask);
      actions.hidden = !next.detail_ref;
      trajectory.hidden = task.mode === 'team'; showLessons.hidden = task.mode === 'team';
    },
    open() { element.open = true; summary.focus(); element.scrollIntoView({block: 'nearest'}); },
    destroy() { lessonsUI?.destroy(); element.remove(); },
  };
  result.update(step); return result;
}
