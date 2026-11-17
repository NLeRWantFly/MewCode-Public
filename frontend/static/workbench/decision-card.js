import {node, button} from './dom.js';
import {mountQuestion} from './question.js';
import {mountExtras} from './decision-extras.js';

export const STALE = '该事项已经处理或已过期，请以当前显示的内容为准';
export function deadlineText(item, now = Date.now()) {
  if (item.mandatory) return '需要你决定';
  const deadline = typeof item.deadline === 'number' ? item.deadline * (item.deadline < 1e12 ? 1000 : 1) : Date.parse(item.deadline);
  if (!Number.isFinite(deadline)) return '等待你的选择';
  const seconds = Math.max(0, Math.ceil((deadline - now) / 1000));
  return seconds ? `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')} 后由宿主处理` : '正在等待宿主处理超时结果';
}
export function mountDecisionCard(item, {api, store, openStep, resolved, onConflict, now = Date.now, interval = setInterval, clear = clearInterval} = {}) {
  const element = node('form', 'wb-decision-card'); element.id = 'workbench-item-' + item.id; element.tabIndex = -1;
  element.dataset.itemId = item.id;
  const title = node('h3', '', item.title), background = node('p', 'wb-card-background', item.background);
  const clock = node('p', 'wb-card-gate', deadlineText(item, now())), feedback = node('p', 'wb-error'); feedback.setAttribute('role', 'status');
  element.append(title, background, clock);
  if (item.timeout_consequence) element.append(node('p', 'wb-muted', item.timeout_consequence));
  const owner = store.generation, scope = {...store.state.scope};
  let busy = false, complete = false, dead = false, submittedFocus = false;
  const alive = () => !dead && owner === store.generation && element.isConnected;
  const questions = item.resume_required ? [] : (item.questions || []).filter(q => q.type !== 'team_intervention' && (item.type !== 'run_inputs' || !q.collect_at || q.collect_at === 'start')).map((q, index) => {
    const field = mountQuestion({...q, context: q.context === item.background ? '' : q.context}, {bundle: !!item.choice_bundle?.id, recommended: item.recommended?.[q.name], index, prefix: item.id});
    element.append(field.element); return field;
  });
  function conditions() {
    for (const field of questions) if (field.condition) {
      const value = questions.find(row => row.name === field.condition.input)?.value();
      field.show(String(Array.isArray(value) ? value.join('\n') : value ?? '').trim() === field.condition.equals);
    }
  }
  element.addEventListener('input', conditions); element.addEventListener('change', conditions); conditions();
  const extras = mountExtras(item, {api, scope}); if (!item.resume_required) element.append(extras.element);
  const submit = button(item.resume_required ? '继续任务并核对问题' : '提交选择', () => {} , 'wb-button wb-primary'); submit.type = 'submit';
  if (item.prepare_choices) {submit.textContent = '准备可选方案';submit.type = 'button';submit.addEventListener('click', async () => {
    if (busy || complete || !alive()) return;
    busy = true; disable(true); feedback.textContent = '正在准备可供选择的方案…';
    try {await api.prepare(scope,item,{action:'retry_prepare'});if(alive()){complete=true;feedback.textContent='方案已更新';store.refresh();}}
    catch(error){if(alive())feedback.textContent=error.message;}
    finally{busy=false;if(alive())disable(complete);}
  });}
  element.append(submit, feedback);
  const timer = !item.mandatory && item.deadline ? interval(() => { if (alive()) clock.textContent = deadlineText(item, now()); }, 1000) : null;
  function disable(value) { submit.disabled = value; for (const field of questions) field.disable(value); extras.disable(value); }
  element.addEventListener('submit', async event => {
    event.preventDefault(); if (busy || complete || !alive() || item.prepare_choices) return;
    submittedFocus = element.contains(document.activeElement);
    feedback.textContent = ''; busy = true; disable(true);
    try {
      const values = questions.map(q => q.read({partial: true})), actions = values.flatMap(q => q.actions);
      if (actions.length > 1 || (actions.length && values.some(q => q.value !== undefined))) throw new Error('请单独选择一个准备动作；回答问题时再提交选择。');
      let result;
      if (actions.length && item.type === 'run_inputs' && actions[0].action === 'defer') {
        feedback.textContent = '本次参数尚未提交。你可以稍后继续填写。'; return;
      }
      if (actions.length && item.type !== 'recovery_case' && ['research', 'revise', 'configure', 'retry_prepare'].includes(actions[0].action)) {
        result = await api.prepare(scope, item, {action: actions[0].action, input: actions[0].input});
      } else {
        if (actions.length && item.type !== 'recovery_case' && actions[0].action !== 'defer') throw new Error('请补充说明后重新选择当前可用的处理方式。');
        const fields = actions.length || item.resume_required ? [] : questions.map(q => q.read());
        const answers = Object.fromEntries(fields.filter(q => q.value !== undefined).map(q => [q.name, q.value]));
        const notes = Object.fromEntries(fields.filter(q => q.note).map(q => [q.name, q.note]));
        const choice_inputs = Object.assign({}, ...fields.map(q => q.choiceInputs));
        if (item.type === 'recovery_case') for (const action of actions) {
          answers[action.name] = action.token;
          if (action.input !== undefined) choice_inputs[action.token] = action.input;
        }
        const extra = await extras.read(answers); if (!alive()) return;
        result = await api.answer(scope, item, {...extra, answers, notes, choice_inputs,
          ...(actions.length && item.type !== 'recovery_case' ? {cancelled: true} : {}), ...(item.resume_required ? {action: 'resume'} : {})});
      }
      if (!alive()) return;
      if (result.inspect_node_ids?.length) { openStep?.(result.inspect_node_ids[0]); feedback.textContent = '请在步骤详情中核对操作结果。'; }
      else { complete = true; feedback.textContent = result.answered === false ? '已提交，请核对更新后的问题。' : '已提交'; resolved?.(item.id); }
      store.refresh();
    } catch (error) {
      if (!alive()) return;
      feedback.textContent = error.message || '提交未成功，请重试。';
      if (error.status === 409) { complete = true; onConflict?.(feedback.textContent); store.refresh(); }
    } finally { busy = false; if (alive()) disable(complete); }
  });
  return {element, focus() { element.scrollIntoView({block: 'center'}); element.focus(); },
    shouldRestoreFocus() { return submittedFocus && complete && (document.activeElement === document.body || element.contains(document.activeElement)); },
    destroy() { dead = true; if (timer !== null) clear(timer); element.remove(); }};
}
