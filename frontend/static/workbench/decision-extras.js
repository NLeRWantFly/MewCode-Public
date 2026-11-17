import {node} from './dom.js';
import {textValue,evidence} from './process.js';

export function mountExtras(item, deps) {
  const element = node('div', 'wb-decision-extras'), fields = {}, context = item.context || {};
  function text(key, label) {
    const wrap = node('label', 'wb-field', label), input = node('textarea', 'wb-input'); input.rows = 2;
    wrap.append(input); fields[key] = input; element.append(wrap); return input;
  }
  function select(key, label, options) {
    const wrap = node('label', 'wb-field', label), input = node('select', 'wb-input');
    for (const [value, title] of [['', '请选择'], ...options]) { const option = node('option', '', title); option.value = value; input.append(option); }
    wrap.append(input); fields[key] = input; element.append(wrap); return input;
  }
  function check(key, label) {
    const wrap = node('label', 'wb-check-field'), input = node('input'); input.type = 'checkbox';
    wrap.append(input, node('span', '', label)); fields[key] = input; element.append(wrap);
  }
  if (item.type === 'plan_revision') {
    const proposal = context.proposal || {};
    for (const [label, value] of [['修订原因', proposal.why], ['执行范围', proposal.execution_scope], ['需要重验', proposal.revalidate]])
      if (value?.length) element.append(node('p', 'wb-muted', label + '：' + textValue(value)));
    text('recovery_info', '补充信息'); check('grant_extra_budget', '明确允许本次修订增加执行预算'); check('sync_canvas', '将本次修订同步到保存的编排图');
  }
  if (item.type === 'recovery_case') {
    const report = context.report || {}, diagnosis = report.diagnosis || {}, summary = context.handling?.summary || {};
    for (const [label, value] of [['当前目标', summary.goal], ['已保留的进展', summary.progress],
      ['需要你决定的原因', summary.why_user], ['影响', summary.impact || diagnosis.impact], ['建议', summary.recommendation],
      ['发现的问题', diagnosis.symptom?.summary], ['已尝试', diagnosis.tried]])
      if (value?.length) element.append(node('p', 'wb-muted', label + '：' + (Array.isArray(value) ? value.join('；') : value)));
    const confidence = {high: 0, medium: 1, low: 2};
    for (const hypothesis of [...(diagnosis.hypotheses || [])].sort((a,b) => (confidence[a.confidence] ?? 3) - (confidence[b.confidence] ?? 3))) {
      const row = node('section', 'wb-diagnosis-hypothesis'); row.append(node('p', '', hypothesis.summary));
      evidence(row, '依据与待核实信息', {证据: hypothesis.evidence, 待核实: hypothesis.open_questions}); element.append(row);
    }
    evidence(element, '问题证据', diagnosis.symptom);
    evidence(element, '授权范围', context.pending?.authority);
    evidence(element, '尝试经过和技术详情', {报告: report, 诊断记录: context.rounds, 执行边界: context.boundary});
    text('note', '处理说明（可选）');
    select('remember_scope', '这次选择的适用范围', [['run', '本次运行'], ['once', '仅这一次']]);
    fields.remember_scope.value = 'run';
  }
  if (item.type === 'collaboration_recovery' && context.kind !== 'execution_evidence_available') {
    if (context.budget) {
      const budget = context.budget, names = {attempts:'执行尝试', revisions:'计划修订', expansions:'任务拆分'};
      element.append(node('p', 'wb-muted', (budget.scope === 'root' ? '整个任务' : '当前任务分支') + '的' +
        (names[budget.kind] || budget.kind) + '剩余额度不足以执行本次安排。历史用量会保留；弹性额度共享任务总额，追加次数则增加所列范围。'));
      const wrap = node('label', 'wb-field', '追加次数（仅选择追加指定额度时填写）'), input = node('input', 'wb-input');
      input.type = 'number'; input.min = '1'; input.step = '1'; input.value = '1';
      wrap.append(input); fields.budget_amount = input; element.append(wrap);
    }
    const coordinator = context.kind === 'coordinator_unavailable';
    const model = select('model', coordinator ? '换绑协调服务时使用' : '换模型重试时使用', []);
    const verifier = context.verifier_switch_available ? select('verifier_model', '更换当前步骤的验收模型时使用', []) : null;
    const status = node('p', 'wb-muted', '正在读取可用模型…'); element.append(status);
    if (deps.api.get) void deps.api.get('/api/orchestration/task-design', deps.scope).then(value => {
      for (const ref of value.task_design?.collaboration?.[coordinator ? 'coordinator_models' : 'recovery_models'] || []) {
        const option = node('option', '', ref.provider_key + ' / ' + ref.model_id); option.value = JSON.stringify(ref); model.append(option);
      }
      for (const ref of verifier ? value.task_design?.collaboration?.verification_models || [] : []) {
        const option = node('option', '', ref.provider_key + ' / ' + ref.model_id + (ref.effort ? ' · ' + ref.effort : ''));
        option.value = JSON.stringify(ref); verifier.append(option);
      }
      status.textContent = model.children.length > 1 ? '' : '当前没有可用于重试的模型。';
    }).catch(error => { status.textContent = error.message; });
  }
  if (item.type === 'team_intervention' && context.source?.state === 'needs_clarification') {
    select('intent', '这条消息的用途', [['information', '补充资料'], ['question', '询问进度'], ['change_task', '调整原消息指定的步骤目标'],
      ['change_goal', '调整根目标'], ['pause', '暂停所选范围'], ['cancel', '取消当前运行']]);
    element.append(node('p', 'wb-muted', context.source.request?.message || ''));
  }
  if (item.type === 'file_grant') {
    element.append(node('p', 'wb-muted', context.access === 'read' ? '授权范围：读取此文件' : '授权范围：读写此文件'));
  }
  if (item.type === 'permission' && context.directory_scope) {
    element.append(node('p', 'wb-muted', `目录授权仅覆盖 ${context.directory_scope} 及其子目录，有效期 ${Math.round(context.directory_ttl_seconds / 86400)} 天。`));
  }
  if (item.type === 'revision_draft') {
    for (const change of context.changes || []) element.append(node('p', 'wb-revision-preview', textValue(change.body || change.value || change)));
    element.append(node('p', 'wb-muted', '确认后将记录修改，并重新核验受影响的工作。'));
  }
  if (item.type === 'maintenance_proposal') {
    for(const change of context.proposal?.changes||[]) evidence(element,change.title||'修订内容',{修改前:change.before,修改后:change.after});
    for(const action of context.proposal?.run_actions||[]) {
      evidence(element,action.label||action.reason||'本次运行操作',{操作:action.action,修改前:action.before,修改后:action.after});
      if(action.requires_confirmation) check('confirm:'+action.id,'明确应用这项运行修改');
    }
    evidence(element,'执行环境检修',context.proposal?.runtime_actions);
  }
  if (item.type === 'run_action') evidence(element,'操作依据与影响',context.details);
  async function read(answers) {
    const data = Object.fromEntries(Object.entries(fields).map(([key, field]) => [key, field.type === 'checkbox' ? field.checked : field.value.trim()]));
    if (item.type === 'plan_revision' && answers.action === 'supply_info' && !data.recovery_info) throw new Error('请填写补充信息');
    if (item.type === 'collaboration_recovery' && ['switch_model', 'switch_coordinator'].includes(answers.action) && !data.model) throw new Error('请选择已配置的模型');
    if (item.type === 'collaboration_recovery' && answers.action === 'switch_verifier' && !data.verifier_model) throw new Error('请选择已配置的验收模型');
    if (item.type === 'collaboration_recovery' && answers.action === 'add_budget') {
      const amount = Number(data.budget_amount);
      if (!Number.isSafeInteger(amount) || amount < 1) throw new Error('追加次数必须为正整数');
      data.budget_amount = amount;
    } else delete data.budget_amount;
    if (item.type === 'collaboration_recovery' && answers.action === 'switch_verifier') data.model = JSON.parse(data.verifier_model);
    else if (data.model && (item.type !== 'collaboration_recovery' || ['switch_model', 'switch_coordinator'].includes(answers.action)))
      data.model = JSON.parse(data.model);
    else delete data.model;
    delete data.verifier_model;
    if (item.type === 'revision_draft') Object.assign(data, {draft_id: context.id, draft_digest: context.digest, base_version: context.base_version});
    if (item.type === 'maintenance_proposal') {
      data.confirmed_run_action_ids=Object.keys(fields).filter(key=>key.startsWith('confirm:')&&data[key]).map(key=>key.slice(8));
      for(const key of Object.keys(data)) if(key.startsWith('confirm:'))delete data[key];
    }
    if (data.intent) {
      const request = {...context.source.request, intent: data.intent, confirmed: false};
      if (['change_goal', 'change_task'].includes(data.intent)) {
        const view = await deps.api.get('/api/team-v2/runs/' + encodeURIComponent(item.run_id));
        if (data.intent === 'change_goal') request.mission = {...view.orchestration.mission.contract, objective: request.message};
        else {
          const task = view.tasks.find(row => row.task_id === request.task_id);
          if (!task) throw new Error('原消息未指定步骤，请先撤回请求并明确目标步骤。');
          request.contract = {...task.contract, objective: request.message};
        }
      }
      data.request = request; delete data.intent;
    } else if (fields.intent) throw new Error('请选择这条消息的用途');
    return data;
  }
  return {element, read, disable(value) { for (const field of Object.values(fields)) field.disabled = value; }};
}
