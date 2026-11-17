import {node, button, place} from './dom.js';
import {evidence} from './process.js';
export const STALE_REVISION = '要求已被更新，请在最新版本上重新修改';
const LABELS = {goal: '目标', constraint: '约束', acceptance: '验收标准', decision: '决定', assumption: '假设', method: '指定方法'};

export function mountRevision(container, {api, store}) {
  const title = node('strong', 'wb-requirement-title'), text = node('p', 'wb-requirement-body');
  const state = node('p', 'wb-muted'), proposed = node('p', 'wb-revision-proposed'), reason = node('p', 'wb-muted'), sources = node('div');
  const form = node('form', 'wb-revision-form'), input = node('textarea'), feedback = node('p', 'wb-action-message');
  input.rows = 4; input.maxLength = 16000; input.required = true;
  const edit = button('修改', begin), cancel = button('取消编辑', close), save = node('button', 'wb-button wb-primary', '提交修改');
  save.type = 'submit'; form.append(input, node('p', 'wb-muted', '提交后先记录修订，核对影响后在安全点生效。'), save, cancel);
  form.hidden = true; feedback.setAttribute('role', 'status');
  container.append(title, text, state, proposed, reason, sources, edit, form, feedback);
  let entry, task, scope, generation, basis, editing = false, pending = false, owner = '', sourceKey = '';
  const active = () => generation === store.generation && container.isConnected;
  const editable = () => entry.editable && !task.readonly && task.revision_available !== false;
  function begin() {
    if (!editable() || pending) return;
    basis = task.spec_version; input.value = entry.revision?.pending_body ?? entry.body;
    editing = true; form.hidden = false; edit.hidden = true; feedback.textContent = ''; input.focus();
  }
  function close() { if (pending) return; editing = false; form.hidden = true; edit.hidden = !editable(); edit.focus(); }
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (pending || !active() || !editable()) return;
    if (!input.value.trim()) { feedback.textContent = '请填写完整的新要求'; input.focus(); return; }
    if (basis !== task.spec_version) { feedback.textContent = STALE_REVISION; return; }
    const submittedOwner = owner;
    pending = true; save.disabled = cancel.disabled = true; feedback.textContent = '正在提交…';
    try {
      await api.revision(scope, {expected_task_id: task.id, expected_run_id: task.run_id, expected_version: basis, entry_id: entry.id,
        field: entry.field || 'body', body: input.value, request_id: crypto.randomUUID()});
      if (!active() || owner !== submittedOwner) return;
      editing = false; form.hidden = true; edit.hidden = false; feedback.textContent = '已记录'; edit.focus();
    } catch (error) {
      if (active() && owner === submittedOwner) feedback.textContent = error.message;
    } finally {
      if (active() && owner === submittedOwner) { pending = false; save.disabled = cancel.disabled = false; store.refresh(); }
    }
  });
  return {update(next, nextTask) {
    const nextOwner = JSON.stringify([store.generation, nextTask.id, nextTask.run_id, next.id, next.field || 'body']);
    if (owner !== nextOwner) {
      owner = nextOwner; generation = store.generation; scope = {...store.state.scope};
      editing = pending = false; form.hidden = true; feedback.textContent = ''; save.disabled = cancel.disabled = false;
    }
    entry = next; task = nextTask;
    title.textContent = next.title || LABELS[next.kind] || '要求'; text.textContent = next.body;
    input.setAttribute('aria-label', '修改' + title.textContent);
    state.textContent = next.revision?.status || ''; state.hidden = !state.textContent;
    proposed.hidden = !next.revision?.pending_body || next.revision.status === '已生效' || next.revision.pending_body === next.body;
    proposed.textContent = proposed.hidden ? '' : '待生效：' + next.revision.pending_body;
    reason.textContent = task.readonly ? '历史运行只读' : !next.editable ? next.edit_reason || '当前条目不能在这里修改'
      : task.revision_available === false ? '此运行未绑定当前任务，不能在这里修订' : '';
    reason.hidden = !reason.textContent; edit.hidden = editing || !editable();
    if (editing && basis !== task.spec_version) feedback.textContent = STALE_REVISION;
    const key = JSON.stringify(next.sources || []);
    if (sourceKey !== key) { sourceKey = key; sources.replaceChildren(); if (next.sources?.length) evidence(sources, '查看来源', next.sources); }
  }};
}

export function mountRequirements(container, deps) {
  const rows = new Map(); let owner = '';
  return {update(entries, task) {
    const nextOwner = JSON.stringify([deps.store.generation, task.id, task.run_id]);
    if (owner !== nextOwner) { owner = nextOwner; rows.clear(); container.replaceChildren(); }
    const keep = new Set();
    entries.forEach((entry, index) => {
      const key = JSON.stringify([entry.id, entry.field || 'body']);
      keep.add(key); let row = rows.get(key);
      if (!row) { const element = node('div', 'wb-requirement'); row = {element, ui: mountRevision(element, deps)}; rows.set(key, row); }
      row.ui.update(entry, task); place(container, row.element, index);
    });
    for (const [id, row] of rows) if (!keep.has(id)) { row.element.remove(); rows.delete(id); }
    container.hidden = !entries.length;
  }};
}
