import {node} from './dom.js';
export const textValue = value => typeof value === 'string' ? value : value == null ? '' : JSON.stringify(value, null, 2);
export function evidence(parent, label, value) {
  if (value == null || value === '' || (Array.isArray(value) && !value.length)) return;
  const item = node('details', 'wb-evidence');
  item.append(node('summary', '', label), node('pre', 'wb-code', textValue(value))); parent.append(item);
}
export function processRows(snapshot, stepId) {
  const stage = (snapshot.node_states || snapshot.stages || {})[stepId] || {};
  const events = (snapshot.events || []).filter(row => row.node_id === stepId);
  const rows = [];
  if (stage.text) rows.push({label: '模型输出', text: stage.text});
  if (stage.thinking) rows.push({label: '模型思考', text: stage.thinking});
  const tools = Array.isArray(stage.tools) ? stage.tools : Object.values(stage.tools || {});
  for (const tool of tools) rows.push({label: tool.name || '工具调用', text: tool.output || tool.result || tool.text || '',
    args: tool.args || tool.arguments, status: tool.status});
  // Events preserve failed attempts and partial output that may not be in the latest stage.
  for (const event of events) {
    if (!/tool|delta|thinking|summary|output|completed/.test(event.type || '') && event.type !== 'expert_step') continue;
    rows.push({label: event.name || (/tool/.test(event.type) ? '工具调用' : /thinking/.test(event.type) ? '模型思考' : '模型输出'),
      text: event.text || event.output || event.result || '', args: event.args, status: event.status,
      attempt: event.node_attempt || event.attempt, time: event.created_at || event.server_time});
  }
  return {stage, rows};
}
export function renderProcess(container, snapshot, stepId, mode = 'classic') {
  container.replaceChildren();
  if (mode === 'team') {
    for (const revision of snapshot.revisions || []) {
      const item = node('section', 'wb-process-entry'); item.append(node('h4', '', `任务修订 ${revision.revision}`));
      evidence(item, '执行与交付记录', revision); container.append(item);
    }
    if (!container.children.length) container.append(node('p', 'wb-muted', '当前步骤暂无执行记录。'));
    return;
  }
  const {stage, rows} = processRows(snapshot, stepId);
  for (const row of rows) {
    const item = node('details', 'wb-process-entry');
    item.append(node('summary', '', row.label + (row.attempt ? ` · 第 ${row.attempt} 次尝试` : '')));
    if (row.text) item.append(node('pre', 'wb-code', textValue(row.text)));
    evidence(item, '输入参数', row.args); container.append(item);
  }
  if (!rows.length) container.append(node('p', 'wb-muted', '当前步骤暂无工具或模型输出记录。'));
  const cases = [...(snapshot.recovery_cases || []), ...(stage.recovery_cases || []), ...(stage.recovery_case ? [stage.recovery_case] : []),
    ...(snapshot.events || []).filter(event => event.node_id === stepId && event.case).reverse().map(event => event.case)]
    .filter(row => !row.node_id || row.node_id === stepId);
  const seen = new Set();
  for (const record of cases) {
    const key = record.case_id || JSON.stringify(record); if (seen.has(key)) continue; seen.add(key);
    const item = node('details', 'wb-recovery-history');
    item.append(node('summary', '', '恢复记录' + (record.blocker ? ' · ' + record.blocker : '')));
    if (record.symptom || record.reason) item.append(node('p', '', record.symptom || record.reason));
    for (const [index, round] of (record.rounds || []).entries()) evidence(item, `第 ${index + 1} 轮诊断与验证`, round);
    evidence(item, '完整记录', record); container.append(item);
  }
  evidence(container, '协助与复核记录', stage.assistance_receipts);
  evidence(container, '操作回执与效果核验', stage.command_receipts);
  evidence(container, '恢复调度记录', stage.scheduler_recovery);
  for (const event of snapshot.events || []) if (event.node_id === stepId && event.recovery_decision)
    evidence(container, '恢复调度决定', event.recovery_decision);
  evidence(container, '步骤检查点', stage.checkpoint);
}
