import {node} from './dom.js';
import {formatDuration} from './draft-protocol.js';

function activityText(data, now) {
  if (!data.running) return '';
  const progress = data.progress || {}, model = progress.model || {};
  if (!model.stage && !progress.model_label && !progress.request_started_at && !model.waiting) return '';
  const stages = {preparing: '准备模型请求', queued: '等待请求排队', requesting: '等待模型响应',
    thinking: '模型处理中', reasoning: '模型处理中', processing: '模型处理中', streaming: '正在接收结果',
    received: '结果已接收', parsing: '正在解析结果', validating: '正在校验结果', checkpointing: '正在保存进度'};
  const parts = [stages[model.stage] || '后台处理中'];
  if (progress.model_label) parts.push(progress.model_label);
  const started = Date.parse(progress.request_started_at || '');
  if (Number.isFinite(started)) parts.push('本次请求已用 ' + formatDuration(Math.max(0, now - started)));
  if (Number.isFinite(model.output_chars) && model.output_chars > 0) parts.push(`已接收 ${model.output_chars} 字符`);
  const lastEvent = Date.parse(model.last_event_at || '');
  if (['preparing', 'queued', 'requesting', 'thinking', 'reasoning', 'processing', 'streaming'].includes(model.stage)) {
    if (Number.isFinite(lastEvent)) parts.push('距上次模型消息 ' + formatDuration(Math.max(0, now - lastEvent)));
    else if (['queued', 'requesting'].includes(model.stage)) parts.push('尚未收到模型消息');
  }
  if (model.waiting) parts.push('仍在等待当前响应');
  return parts.join(' · ');
}

// Read-only progress. No automatic start, retry, save, timeout answer or model call.
export function mountGeneration(container, {api, store, delay = setTimeout, cancelDelay = clearTimeout, now = Date.now}) {
  const headline = node('p', 'wb-generation-headline'), rail = node('ol', 'wb-generation-rail');
  const activity = node('p', 'wb-muted wb-generation-activity'); activity.hidden = true;
  const result = node('p', 'wb-muted'), next = node('p', 'wb-muted'), warning = node('p', 'wb-action-message');
  const healthBox = node('section', 'wb-generation-health'), healthTitle = node('p', 'wb-generation-headline');
  const healthBasis = node('p', 'wb-muted'), healthProblems = node('ul');
  healthBox.append(healthTitle, healthProblems); healthBox.hidden = true;
  const details = node('details'), evidence = node('pre', 'wb-evidence');
  details.append(node('summary', '', '生成记录'), healthBasis, rail, result, next, evidence);
  container.append(healthBox, headline, activity, details, warning); container.hidden = true;
  warning.setAttribute('role', 'status');
  headline.setAttribute('role', 'status');
  let owner = '', signature = '', timer = null, request = null, current = null, disposed = false;
  function stop() { if (timer !== null) cancelDelay(timer); timer = null; request?.abort(); request = null; }
  async function read() {
    const selected = owner, scope = {...store.state.scope}, abort = request = new AbortController();
    try {
      const data = await api.get('/api/orchestration/graph-autobuild/progress', scope, abort.signal);
      if (disposed || owner !== selected || abort.signal.aborted) return;
      if (data.conversation_id !== scope.conversation_id || String(data.project).replace(/\\/g,'/').toLowerCase() !== String(scope.project).replace(/\\/g,'/').toLowerCase()) throw new Error('生成进度不属于当前对话。');
      const process = data.process || {}, progress = data.progress || {};
      const health = current?.health;
      const showHealth = Boolean(health && (health.identity?.material_sequence || health.identity?.candidate_id));
      const visible = Boolean(data.running || data.candidate || current?.build_request || showHealth);
      container.hidden = !visible;
      const live = activityText(data, now());
      activity.textContent = live; activity.hidden = !live;
      const key = JSON.stringify([process, data.running, data.candidate?.log, live, current?.reason, health?.fingerprint]);
      warning.textContent = '';
      if (key === signature) return;
      signature = key;
      healthBox.hidden = !showHealth;
      healthTitle.textContent = showHealth ? (health.current_validation?.diagnostics?.length ? '当前问题' : health.summary) : '';
      healthBasis.textContent = showHealth ? `素材版本 ${health.identity.material_sequence ?? '未知'} · 候选版本 ${health.identity.candidate_sequence ?? '尚未生成'}`
        + (health.previous_failure?.status === 'historical' ? ' · 原失败属于旧版本' : '')
        + (health.runtime?.status === 'restart_required' ? ' · 运行代码待更新' : '') : '';
      healthProblems.replaceChildren(...(showHealth ? health.current_validation?.diagnostics || [] : []).map(problem =>
        node('li', '', (problem.severity === 'warning' ? '提示：' : '需修正：') + problem.message)));
      if (showHealth && health.current_validation?.omitted_count) healthProblems.append(node('li', '', `另有 ${health.current_validation.omitted_count} 项诊断未展开；修正后继续校验。`));
      const outdated = !data.running && showHealth && health.previous_failure?.status === 'historical';
      headline.textContent = outdated ? '此前生成记录（旧版本）' : process.headline || current?.reason || '正在整理任务规格';
      headline.hidden = showHealth && !data.running;
      rail.replaceChildren(...(process.stages || []).map((label, index) => {
        const step = node('li', index === process.stage_index ? 'is-current' : '', label);
        if (index === process.stage_index) step.setAttribute('aria-current', 'step'); return step;
      }));
      result.textContent = outdated ? '历史失败已保留，当前阻塞以上方诊断为准。' : process.last_result || '';
      next.textContent = showHealth && !data.running ? '' : process.next_step ? '下一步：' + process.next_step : '';
      const rows = (data.candidate?.log || []).map(row => `${row.status === 'ok' ? '已完成' : '历史尝试'} · ${row.duration_ms ? formatDuration(row.duration_ms) : ''} ${row.error || ''}`);
      if (progress.model_label) rows.unshift('使用模型：' + progress.model_label);
      if (data.running && progress.model?.waiting) rows.unshift('仍在等待当前模型响应，尚未重新提交请求。');
      if (process.soft_boundary && data.running) rows.unshift('已到软边界；完整结果保存后再评估继续或收束。');
      evidence.textContent = rows.join('\n') || '尚无生成过程记录'; warning.textContent = '';
    } catch (error) {
      if (!disposed && owner === selected && !abort.signal.aborted) { container.hidden = false; activity.textContent = ''; activity.hidden = true; warning.textContent = '生成进度暂时无法读取，任务状态以概览为准。' + (error.message || ''); }
    } finally {
      if (request === abort) request = null;
      if (!disposed && owner === selected && !abort.signal.aborted) timer = delay(() => {timer = null; void read();}, 2000);
    }
  }
  return {update(design) {
    current = design;
    if(design?.workflow_format==='mewcode.workflow.v2') {
      stop();owner='';container.hidden=!design.build_request||!!design.run_id;
      healthBox.hidden=true;activity.hidden=true;details.hidden=true;warning.textContent='';
      headline.hidden=false;headline.textContent=design.reason||'节点与连接已保存。';return;
    }
    details.hidden=false;
    const key = design && !design.run_id ? JSON.stringify([store.generation, design.task_id]) : '';
    if (key === owner) return;
    stop(); owner = key; signature = ''; container.hidden = true; warning.textContent = ''; headline.textContent = ''; rail.replaceChildren();
    activity.textContent = ''; activity.hidden = true;
    if (key) void read();
  }, destroy() { disposed = true; stop(); }};
}
