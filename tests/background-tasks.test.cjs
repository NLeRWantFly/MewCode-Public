/* Dependency-free background-task panel regressions. Run: node --test tests/background-tasks.test.cjs */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../frontend/static/app.js'), 'utf8');
const panelStart = source.indexOf('  // ---------- 后台任务面板');
const panelEnd = source.indexOf('  // ---------- Human on-call', panelStart);
assert.ok(panelStart >= 0 && panelEnd > panelStart, 'background-task panel source boundary exists');
const panelSource = source.slice(panelStart, panelEnd) + `
globalThis.__bgPanel = {
  renderBgTasks, pollBgTasks, scheduleBgPoll, setBgTasksOpen,
  updateBgTaskClockLabels,
  timers: () => ({ poll: btTimer, clock: btClockTimer }),
};
`;

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[char]);
}

function decodeHtml(value) {
  return value.replace(/&(#39|quot|gt|lt|amp);/g, (_match, entity) => ({
    '#39': "'", quot: '"', gt: '>', lt: '<', amp: '&',
  })[entity]);
}

class ClassList {
  constructor(...names) { this.names = new Set(names); }
  contains(name) { return this.names.has(name); }
  add(name) { this.names.add(name); }
  remove(name) { this.names.delete(name); }
  toggle(name, force) {
    const next = force === undefined ? !this.contains(name) : !!force;
    if (next) this.add(name); else this.remove(name);
    return next;
  }
}

class Element {
  constructor(id = '') {
    this.id = id;
    this.classList = new ClassList();
    this.handlers = new Map();
    this.attrs = {};
    this.dataset = {};
    this.children = [];
    this.disabled = false;
    this._text = '';
    this._html = '';
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set innerHTML(value) {
    this._html = String(value);
    this._text = '';
    this.children = [];
    const pattern = /<span\b[^>]*data-bg-(row|group)-clock='([^']*)'[^>]*>([\s\S]*?)<\/span>/g;
    for (const match of this._html.matchAll(pattern)) {
      const child = new Element();
      child.dataset[match[1] === 'row' ? 'bgRowClock' : 'bgGroupClock'] = decodeHtml(match[2]);
      child.textContent = decodeHtml(match[3].replace(/<[^>]*>/g, ''));
      this.children.push(child);
    }
  }
  get innerHTML() {
    let clockIndex = 0;
    return this._html.replace(/(<span\b[^>]*data-bg-(?:row|group)-clock='[^']*'[^>]*>)([\s\S]*?)(<\/span>)/g,
      (_match, start, _text, end) => start + escapeHtml(this.children[clockIndex++]?.textContent || '') + end);
  }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  addEventListener(name, callback) {
    const listeners = this.handlers.get(name) || [];
    listeners.push(callback);
    this.handlers.set(name, listeners);
  }
  fire(name, event = {}) {
    for (const callback of this.handlers.get(name) || []) callback(event);
  }
  querySelectorAll(selector) {
    if (selector === '[data-bg-row-clock]') return this.children.filter(child => child.dataset.bgRowClock !== undefined);
    if (selector === '[data-bg-group-clock]') return this.children.filter(child => child.dataset.bgGroupClock !== undefined);
    return [];
  }
  focus() {}
}

function taskRow(overrides = {}) {
  return {
    id: 'agent-1', name: 'worker-1', status: 'running', tokens: 0, tools: 0,
    elapsed: 0, duration_basis: 'active_runtime', duration_quality: 'measured',
    runtime_started: true, runtime_active: false, runtime_active_slots: 0,
    task: 'do work', task_id: 'task-1', result_available: false,
    ...overrides,
  };
}

function group(rows, overrides = {}) {
  return {
    key: 'task:root-1', title: '任务 · 汇总资料', kind: 'task', status: 'running',
    agents: rows.length, tokens: 0, tools: 0, rows,
    ...overrides,
  };
}

function payload(groups, overrides = {}) {
  return {
    conversation_id: 'conversation-a', groups, running_total: 0, queued_total: 0,
    runtime_load: { queued: 0, running: 0 }, ...overrides,
  };
}

function fixture(initialConversation = 'conversation-a') {
  const ids = [
    'bgtasks-panel', 'bgtasks-backdrop', 'bgtasks-badge', 'bgtasks-sub', 'bgtasks-body',
    'bgtasks-btn', 'bgtasks-close', 'execution-members-btn',
  ];
  const elements = Object.fromEntries(ids.map(id => [id, new Element(id)]));
  elements['bgtasks-panel'].classList.add('collapsed');
  const document = new Element('document');
  document.hidden = false;
  document.addEventListener = Element.prototype.addEventListener;
  document.fire = Element.prototype.fire;
  document.createElement = () => {
    const element = new Element();
    Object.defineProperty(element, 'innerHTML', {
      get() { return escapeHtml(element.textContent); },
      set(value) { element.textContent = decodeHtml(String(value)); },
    });
    return element;
  };
  const window = new Element('window');
  let conversationId = initialConversation;
  let now = 0;
  let fetchImpl = async () => ({ ok: true, json: async () => payload([]) });
  let nextTimerId = 0;
  const timers = new Map();
  const taskSnapshots = [];
  const context = {
    document, window, performance: { now: () => now },
    activeConversationId: () => conversationId,
    currentRuntimeLoad: { queued: 0, running: 0 },
    $(id) { return elements[id]; },
    esc: escapeHtml,
    fmtTokens: value => String(Number(value) || 0),
    fmtInt: value => String(Number(value) || 0),
    usageJson: async () => ({ ok: true }),
    renderTasks: tasks => taskSnapshots.push(tasks),
    notify() {},
    fetch: (...args) => fetchImpl(...args),
    setInterval(callback, ms) {
      const id = ++nextTimerId;
      timers.set(id, { callback, ms });
      return id;
    },
    clearInterval(id) { timers.delete(id); },
    requestAnimationFrame(callback) { callback(); },
  };
  vm.runInNewContext(panelSource, context, { filename: 'app.js background-task panel' });
  const settle = () => new Promise(resolve => setImmediate(resolve));
  return {
    panel: context.__bgPanel, elements, document, window, timers, taskSnapshots,
    setFetch(fn) { fetchImpl = fn; },
    setConversation(id) { conversationId = id; },
    advance(ms) { now += ms; },
    get now() { return now; },
    async flush() { await settle(); await settle(); },
    fireConversationContext(id) {
      window.fire('mewcode:conversation-context', { detail: { conversationId: id } });
    },
  };
}

test('task groups identify their task and list the agents beneath it with stop controls', () => {
  const f = fixture();
  f.elements['bgtasks-panel'].classList.remove('collapsed');
  const rows = [taskRow({ id: 'agent-a', task_id: 'child-a', name: 'planner' }),
    taskRow({ id: 'agent-b', task_id: 'child-b', name: 'reviewer', status: 'queued', runtime_started: false })];
  f.panel.renderBgTasks(payload([group(rows, { agents: 2 })], { queued_total: 1, running_total: 1 }));
  const html = f.elements['bgtasks-body'].innerHTML;
  assert.match(html, /任务 · 汇总资料/);
  assert.match(html, /任务下的 Agent · 2 个 Agent/);
  assert.match(html, /planner <button/);
  assert.match(html, /reviewer <small/);
  assert.match(html, /bt-stop-group/);
  assert.match(html, /bt-cancel-btn/);
  assert.doesNotMatch(html, /子 Agent · 2 个 agents/);
});

test('native lifecycle distinguishes queued, helper startup, real child and cleanup', () => {
  const f = fixture();
  f.elements['bgtasks-panel'].classList.remove('collapsed');
  for (const [native, label] of [
    [{ phase: 'queued' }, '等待原生执行名额'],
    [{ phase: 'starting', helper_pid: 42 }, '子进程待确认'],
    [{ phase: 'running', process_started: false }, '原生启动未确认'],
    [{ phase: 'running', process_started: true, child_pid: 4567 }, '原生进程已启动 · PID 4567'],
    [{ phase: 'cleaning' }, '正在取消/清理原生进程'],
    [{ phase: 'finished', cleanup_confirmed: true }, '原生结束 · 清理已确认'],
    [{ phase: 'cleanup_unknown' }, '原生清理未确认'],
    [{ phase: 'interrupted', process_started: true, child_pid: 4567 }, '历史原生状态'],
  ]) {
    f.panel.renderBgTasks(payload([group([taskRow({ task_id: 'child-a',
      parent_task_id: 'actual-parent', native_execution: native })])]));
    const html = f.elements['bgtasks-body'].innerHTML;
    assert.ok(html.includes(label), label);
    assert.match(html, /父任务 actual-parent/);
    if (native.phase !== 'running' || !native.process_started) assert.doesNotMatch(html, /进程已启动 · PID/);
  }
});

test('a newly running but unstarted agent does not inherit an older 59-minute row clock', () => {
  const f = fixture();
  f.elements['bgtasks-panel'].classList.remove('collapsed');
  const old = taskRow({ id: 'old-agent', status: 'completed', elapsed: 3540, runtime_started: true, task_id: 'old-task' });
  const fresh = taskRow({ id: 'new-agent', status: 'running', elapsed: 0, runtime_started: false, task_id: 'new-task' });
  f.panel.renderBgTasks(payload([group([old, fresh], { agents: 2 })], { running_total: 1 }));
  const html = f.elements['bgtasks-body'].innerHTML;
  assert.match(html, />59m00s</);
  assert.match(html, />尚未执行</);
  assert.match(html, /data-bg-group-clock='task:root-1'>尚未执行</);
  assert.doesNotMatch(html, /59m00s<\/span><\/div>/);
});

test('queued rows pause accumulated time and unstarted queued rows remain at zero', () => {
  const f = fixture();
  f.elements['bgtasks-panel'].classList.remove('collapsed');
  const running = taskRow({ elapsed: 10, runtime_active: true, runtime_active_slots: 1 });
  f.panel.renderBgTasks(payload([group([running])], { running_total: 1 }));
  const clock = [...f.timers.values()].find(timer => timer.ms === 1000);
  assert.ok(clock);
  f.advance(2000); clock.callback();
  assert.match(f.elements['bgtasks-body'].innerHTML, />12s</);

  const queued = taskRow({ status: 'queued', elapsed: 12, runtime_started: true, task_id: 'task-1' });
  f.panel.renderBgTasks(payload([group([queued])], { queued_total: 1 }));
  assert.match(f.elements['bgtasks-body'].innerHTML, />12s（已暂停）</);
  assert.equal([...f.timers.values()].some(timer => timer.ms === 1000), false);

  const unstarted = taskRow({ status: 'queued', elapsed: 0, runtime_started: false, runtime_active: false });
  f.panel.renderBgTasks(payload([group([unstarted])], { queued_total: 1 }));
  assert.match(f.elements['bgtasks-body'].innerHTML, />未开始</);
});

test('cancelled rows freeze at the backend duration and lower-bound or legacy rows stay labelled', () => {
  const f = fixture();
  f.elements['bgtasks-panel'].classList.remove('collapsed');
  const running = taskRow({ elapsed: 9, runtime_active: true, runtime_active_slots: 2 });
  f.panel.renderBgTasks(payload([group([running])], { running_total: 1 }));
  f.advance(1500);
  const cancelled = taskRow({ status: 'cancelled', elapsed: 12, runtime_active: false, runtime_active_slots: 0 });
  f.panel.renderBgTasks(payload([group([cancelled], { status: 'failed' })]));
  const clock = [...f.timers.values()].find(timer => timer.ms === 1000);
  assert.equal(clock, undefined);
  f.advance(10000);
  assert.match(f.elements['bgtasks-body'].innerHTML, />12s</);

  const lowerBound = taskRow({ status: 'completed', elapsed: 18, duration_quality: 'lower_bound' });
  const legacy = taskRow({ id: 'legacy-agent', name: 'legacy', status: 'completed', elapsed: 3540,
    duration_basis: 'legacy_wall', duration_quality: 'unknown', runtime_started: true });
  f.panel.renderBgTasks(payload([group([lowerBound, legacy], { agents: 2 })]));
  assert.match(f.elements['bgtasks-body'].innerHTML, /至少 18s（下界）/);
  assert.match(f.elements['bgtasks-body'].innerHTML, /旧记录时长未知/);
  assert.doesNotMatch(f.elements['bgtasks-body'].innerHTML, /59m00s/);
});

test('stop pending remains running until a later terminal response', async () => {
  const f = fixture();
  f.elements['bgtasks-panel'].classList.remove('collapsed');
  const pendingRow = taskRow({ elapsed: 10, runtime_active: true, runtime_active_slots: 2,
    cancellation_pending: true });
  const terminalRow = taskRow({ status: 'cancelled', elapsed: 14, runtime_active: false,
    runtime_active_slots: 0, cancellation_pending: false });
  const snapshots = [
    payload([group([pendingRow])], { running_total: 1 }),
    payload([group([terminalRow], { status: 'failed' })]),
  ];
  f.setFetch(async () => ({ ok: true, json: async () => snapshots.shift() }));
  await f.panel.pollBgTasks();
  let html = f.elements['bgtasks-body'].innerHTML;
  assert.match(html, /正在停止…/);
  assert.doesNotMatch(html, /已取消/);
  assert.match(html, /bt-cancel-btn[^>]*disabled/);
  assert.doesNotMatch(html, /bt-stop-group/);
  const clock = [...f.timers.values()].find(timer => timer.ms === 1000);
  assert.ok(clock, 'cleanup time continues only while the server reports active slots');
  f.advance(2000); clock.callback();
  assert.match(f.elements['bgtasks-body'].innerHTML, />14s</);

  await f.panel.pollBgTasks();
  html = f.elements['bgtasks-body'].innerHTML;
  assert.match(html, /worker-1 · 已取消/);
  assert.match(html, />14s</);
  assert.equal(f.elements['bgtasks-sub'].textContent, '全部结束');
  assert.doesNotMatch(html, /bt-cancel-btn/);
  assert.equal([...f.timers.values()].some(timer => timer.ms === 1000), false);
  f.advance(5000);
  assert.match(f.elements['bgtasks-body'].innerHTML, />14s</);
});

test('historical active-runtime zero with missing evidence is not shown as unstarted', () => {
  const f = fixture();
  f.elements['bgtasks-panel'].classList.remove('collapsed');
  const unknown = taskRow({ status: 'completed', elapsed: 0, runtime_started: false,
    duration_basis: 'active_runtime', duration_quality: 'unknown' });
  f.panel.renderBgTasks(payload([group([unknown], { status: 'completed' })]));
  assert.match(f.elements['bgtasks-body'].innerHTML, /执行时长未知/);
  assert.doesNotMatch(f.elements['bgtasks-body'].innerHTML, />未开始</);
});

test('one-second interpolation uses explicit active slots and stops when the panel is hidden or closed', async () => {
  const f = fixture();
  const running = taskRow({ elapsed: 20, runtime_active: true, runtime_active_slots: 2 });
  f.setFetch(async () => ({ ok: true, json: async () => payload([group([running])], { running_total: 1 }) }));
  f.panel.setBgTasksOpen(true, { restoreFocus: false });
  await f.flush();
  assert.equal([...f.timers.values()].some(timer => timer.ms === 5000), true);
  const clock = [...f.timers.values()].find(timer => timer.ms === 1000);
  assert.ok(clock);
  f.advance(1500); clock.callback();
  assert.match(f.elements['bgtasks-body'].innerHTML, />23s</);
  f.advance(10000); clock.callback();
  assert.match(f.elements['bgtasks-body'].innerHTML, />30s/);
  assert.equal([...f.timers.values()].some(timer => timer.ms === 1000), false, 'local clock expires after one poll window');

  f.document.hidden = true;
  f.document.fire('visibilitychange');
  assert.equal(f.panel.timers().poll, null);
  assert.equal(f.panel.timers().clock, null);
  assert.equal(f.timers.size, 0);

  f.document.hidden = false;
  f.document.fire('visibilitychange');
  await f.flush();
  assert.equal([...f.timers.values()].some(timer => timer.ms === 5000), true);
  assert.equal([...f.timers.values()].some(timer => timer.ms === 1000), true);
  f.panel.setBgTasksOpen(false, { restoreFocus: false });
  assert.equal(f.timers.size, 0);
  assert.equal(f.panel.timers().clock, null);
});

test('queued_total zero is authoritative even when the global runtime load reports queued work', () => {
  const f = fixture();
  f.elements['bgtasks-panel'].classList.remove('collapsed');
  f.panel.renderBgTasks(payload([], { queued_total: 0, running_total: 0, runtime_load: { queued: 7, running: 0 } }));
  assert.equal(f.elements['bgtasks-sub'].textContent, '');
  assert.equal(f.elements['bgtasks-badge'].classList.contains('hidden'), true);
});

test('elapsed is never extrapolated without both explicit slot activity and a slot count', () => {
  const f = fixture();
  f.elements['bgtasks-panel'].classList.remove('collapsed');
  for (const contract of [
    { runtime_active: undefined, runtime_active_slots: 2 },
    { runtime_active: true, runtime_active_slots: undefined },
  ]) {
    const row = taskRow({ elapsed: 5, ...contract });
    f.panel.renderBgTasks(payload([group([row])]));
    f.advance(3000);
    f.panel.updateBgTaskClockLabels();
    assert.equal([...f.timers.values()].some(timer => timer.ms === 1000), false);
    assert.match(f.elements['bgtasks-body'].innerHTML, /5s（累计）/);
  }
});

test('older polls and responses from a switched conversation cannot replace current rows', async () => {
  const f = fixture();
  f.elements['bgtasks-panel'].classList.remove('collapsed');
  const pending = [];
  f.setFetch(() => new Promise(resolve => pending.push(resolve)));
  const older = f.panel.pollBgTasks();
  const newer = f.panel.pollBgTasks();
  pending[1]({ ok: true, json: async () => payload([group([taskRow({ name: 'newer-result' })])]) });
  await newer;
  pending[0]({ ok: true, json: async () => payload([group([taskRow({ name: 'older-result' })])]) });
  await older;
  assert.match(f.elements['bgtasks-body'].innerHTML, /newer-result/);
  assert.doesNotMatch(f.elements['bgtasks-body'].innerHTML, /older-result/);

  const stale = f.panel.pollBgTasks();
  f.setConversation('conversation-b');
  f.fireConversationContext('conversation-b');
  assert.doesNotMatch(f.elements['bgtasks-body'].innerHTML, /newer-result/);
  pending[2]({ ok: true, json: async () => payload([group([taskRow({ name: 'stale-conversation' })])]) });
  await stale;
  assert.doesNotMatch(f.elements['bgtasks-body'].innerHTML, /stale-conversation/);
});

test('authoritative background polls refresh the sidebar but stale conversation polls never do', async () => {
  const f = fixture();
  f.elements['bgtasks-panel'].classList.remove('collapsed');
  const tasks = [{ id: 'child-a', status: 'cancelled', elapsed: 12 }];
  f.setFetch(async () => ({ ok: true, json: async () => payload([], { tasks }) }));
  await f.panel.pollBgTasks();
  assert.equal(f.taskSnapshots.length, 1);
  assert.equal(f.taskSnapshots[0], tasks);

  let release;
  f.setFetch(() => new Promise(resolve => { release = resolve; }));
  const stale = f.panel.pollBgTasks();
  f.setConversation('conversation-b');
  release({ ok: true, json: async () => payload([], { tasks: [{ id: 'wrong-conversation' }] }) });
  await stale;
  assert.equal(f.taskSnapshots.length, 1);

  f.setFetch(async () => ({ ok: true, json: async () => payload([], {
    conversation_id: 'conversation-b', tasks: [],
  }) }));
  await f.panel.pollBgTasks();
  assert.equal(f.taskSnapshots.length, 2);
  assert.equal(f.taskSnapshots[1].length, 0);
});

test('HTTP failure retains valid rows, freezes their clock, and recovers on a valid poll', async () => {
  const f = fixture();
  f.elements['bgtasks-panel'].classList.remove('collapsed');
  const active = taskRow({ name: 'last-valid-row', elapsed: 10, runtime_active: true, runtime_active_slots: 1 });
  const recovered = taskRow({ name: 'last-valid-row', elapsed: 15, runtime_active: true, runtime_active_slots: 1 });
  const responses = [
    { ok: true, json: async () => payload([group([active])], { running_total: 1 }) },
    { ok: false, json: async () => ({ groups: [] }) },
    { ok: true, json: async () => payload([group([recovered])], { running_total: 1 }) },
  ];
  f.setFetch(async () => responses.shift());
  await f.panel.pollBgTasks();
  const clock = [...f.timers.values()].find(timer => timer.ms === 1000);
  assert.ok(clock);
  f.advance(1500); clock.callback();
  assert.match(f.elements['bgtasks-body'].innerHTML, />12s</);

  await f.panel.pollBgTasks();
  let html = f.elements['bgtasks-body'].innerHTML;
  assert.match(html, /last-valid-row/);
  assert.match(html, /12s（累计）/);
  assert.equal(f.elements['bgtasks-sub'].textContent, '更新失败 · 状态待刷新');
  assert.equal([...f.timers.values()].some(timer => timer.ms === 1000), false);
  f.advance(5000);
  assert.match(f.elements['bgtasks-body'].innerHTML, /12s（累计）/);

  await f.panel.pollBgTasks();
  html = f.elements['bgtasks-body'].innerHTML;
  assert.match(html, /last-valid-row/);
  assert.doesNotMatch(f.elements['bgtasks-sub'].textContent, /更新失败/);
  assert.equal(f.elements['bgtasks-sub'].textContent, '1 个运行中');
  assert.equal([...f.timers.values()].some(timer => timer.ms === 1000), true);
});

test('malformed payload is not treated as an empty completed result and a valid empty result clears the error', async () => {
  const f = fixture();
  f.elements['bgtasks-panel'].classList.remove('collapsed');
  f.setFetch(async () => ({ ok: true, json: async () => payload([group([taskRow({ name: 'keep-this-row' })])], { running_total: 1 }) }));
  await f.panel.pollBgTasks();
  assert.match(f.elements['bgtasks-body'].innerHTML, /keep-this-row/);

  f.setFetch(async () => ({ ok: true, json: async () => ({
    conversation_id: 'conversation-a', groups: [{ key: 'bad-group', rows: null }], running_total: 0, queued_total: 0,
  }) }));
  await f.panel.pollBgTasks();
  assert.match(f.elements['bgtasks-body'].innerHTML, /keep-this-row/);
  assert.equal(f.elements['bgtasks-sub'].textContent, '更新失败 · 状态待刷新');
  assert.doesNotMatch(f.elements['bgtasks-body'].innerHTML, /全部完成|暂无子 Agent/);

  f.setFetch(async () => ({ ok: true, json: async () => payload([], { running_total: 0, queued_total: 0 }) }));
  await f.panel.pollBgTasks();
  assert.match(f.elements['bgtasks-body'].innerHTML, /暂无子 Agent 任务/);
  assert.doesNotMatch(f.elements['bgtasks-sub'].textContent, /更新失败/);
  assert.notEqual(f.elements['bgtasks-sub'].textContent, '全部完成');
});

test('cleanup failure is not presented as completion and only ticks if a slot remains active', () => {
  const f = fixture();
  f.elements['bgtasks-panel'].classList.remove('collapsed');
  const cleanupUnconfirmed = taskRow({ status: 'cancelled', elapsed: 10, cleanup_failed: true,
    runtime_active: false, runtime_active_slots: 0 });
  f.panel.renderBgTasks(payload([group([cleanupUnconfirmed], { status: 'failed' })]));
  let html = f.elements['bgtasks-body'].innerHTML;
  assert.match(html, /清理未确认/);
  assert.doesNotMatch(html, /worker-1 · 已取消/);
  assert.match(html, /data-bg-group-clock='task:root-1'>清理未确认/);
  assert.equal([...f.timers.values()].some(timer => timer.ms === 1000), false);
  f.advance(3000); f.panel.updateBgTaskClockLabels();
  assert.match(f.elements['bgtasks-body'].innerHTML, />10s</);

  const stillActive = taskRow({ status: 'cancelled', elapsed: 10, cleanup_failed: true,
    runtime_active: true, runtime_active_slots: 1 });
  f.panel.renderBgTasks(payload([group([stillActive], { status: 'failed' })]));
  const clock = [...f.timers.values()].find(timer => timer.ms === 1000);
  assert.ok(clock);
  f.advance(1500); clock.callback();
  html = f.elements['bgtasks-body'].innerHTML;
  assert.match(html, />12s（清理未确认）</);
});

test('task names and agent supplied text are escaped in grouped rendering', () => {
  const f = fixture();
  f.elements['bgtasks-panel'].classList.remove('collapsed');
  const dangerous = '<img src=x onerror=alert(1)>';
  const row = taskRow({ name: dangerous, task: dangerous, id: 'agent-escape', task_id: 'child-escape' });
  f.panel.renderBgTasks(payload([group([row], { title: dangerous, key: 'task:<unsafe>' })]));
  const html = f.elements['bgtasks-body'].innerHTML;
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.doesNotMatch(html, /<img src=x/);
  assert.doesNotMatch(html, /data-bg-group-clock='task:<unsafe>'/);
});
