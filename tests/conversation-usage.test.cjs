/* Dependency-free component regression tests. Run: node --test tests/conversation-usage.test.cjs */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(id = '') {
    this.id = id; this.children = []; this.handlers = {}; this.attrs = {}; this.dataset = {};
    this.hidden = false; this.parent = null; this.value = '';
    this.style = { setProperty(name, value) { this[name] = value; } };
    this.classList = { toggle() {} };
  }
  set textContent(value) { this.value = String(value); this.children = []; }
  get textContent() { return this.value + this.children.map(n => n.textContent).join(''); }
  setAttribute(key, value) { this.attrs[key] = value; }
  addEventListener(name, callback) { (this.handlers[name] ||= []).push(callback); }
  fire(name, event = {}) { for (const callback of this.handlers[name] || []) callback(event); }
  append(...nodes) { for (const node of nodes) this.insertBefore(node, null); }
  replaceChildren(...nodes) { for (const n of this.children) n.parent = null; this.children = []; this.value = ''; this.append(...nodes); }
  insertBefore(node, before) { node.remove(); node.parent = this; this.children.splice(before ? this.children.indexOf(before) : this.children.length, 0, node); }
  remove() { if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1); this.parent = null; }
  contains(node) { return node === this || this.children.some(n => n.contains(node)); }
  getBoundingClientRect() { return { right: 1200, top: 120, bottom: 140, width: this.id.includes('tooltip') ? 270 : 200, height: this.id.includes('tooltip') ? 230 : 20 }; }
}
const tokens = total => ({ total_tokens: total, input_total: total * .8, output_tokens: total * .2, cached_input_tokens: 4, reasoning_tokens: 2 });
function sample(id = 'one', values = [75, 25]) {
  return { conversation_id: id, request_count: values.length * 2, tokens: tokens(values.reduce((a, b) => a + b, 0)),
    models: values.map((value, i) => ({ provider: 'route-' + i, model: 'model-' + i, request_count: 2, tokens: tokens(value) })), unreported_token_requests: 0 };
}
function fixture(initial = sample()) {
  const ids = ['conversation-usage', 'conversation-usage-toggle', 'conversation-usage-content', 'conversation-usage-note',
    'conversation-usage-info', 'conversation-usage-tooltip', 'conversation-usage-close', 'conversation-usage-refresh'];
  const elements = Object.fromEntries(ids.map(id => [id, new Element(id)]));
  const document = new Element(), window = new Element();
  document.getElementById = id => elements[id]; document.createElement = () => new Element();
  document.documentElement = { clientWidth: 1280, clientHeight: 720 }; document.body = new Element();
  const get = suffix => elements['conversation-usage' + suffix];
  get('').hidden = get('-tooltip').hidden = true;
  let data = initial, respond = async () => ({ ok: true, json: async () => data }), pending = [], calls = 0;
  let timerId = 0;
  const timers = new Map(), intervals = [];
  const context = { document, window, AbortController,
    setTimeout(fn, ms) { const id = ++timerId; timers.set(id, { fn, ms }); return id; },
    clearTimeout(id) { timers.delete(id); }, setInterval(fn, ms) { intervals.push({ fn, ms }); },
    fetch: (...args) => { ++calls; const promise = respond(...args); pending.push(promise); return promise; } };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../frontend/static/conversation-usage.js'), 'utf8'), context);
  const settle = () => new Promise(resolve => setImmediate(resolve));
  const flush = async () => { while (pending.length) { await Promise.allSettled(pending.splice(0)); await settle(); } };
  return { get, document, window, flush, set: next => { data = next; }, respond: fn => { respond = fn; }, calls: () => calls,
    async poll() { assert.equal(intervals.length, 1); assert.equal(intervals[0].ms, 5000); intervals[0].fn(); await settle(); },
    async expireRequest() { for (const [id, timer] of timers) if (timer.ms === 15000) { timers.delete(id); timer.fn(); } await settle(); },
    switch: (id, readonly = false) => window.fire('mewcode:conversation-context', { detail: { conversationId: id, readonly } }),
    async open() { this.switch(data.conversation_id); get('-toggle').fire('click'); await flush(); },
    async refresh() { get('-refresh').fire('click'); await flush(); } };
}
test('quiet rows contain only model names; hover exposes exact denominator and calls', async () => {
  const f = fixture(); await f.open();
  assert.equal(f.get('-content').textContent, 'model-0model-1');
  assert.equal(f.get('-tooltip').hidden, true);
  const meter = f.get('-content').children[0].children[1], track = meter.children[0];
  assert.equal(track.children[0].style.width, '75%');
  assert.equal(track.attrs['aria-valuenow'], '75');
  meter.fire('mouseenter');
  assert.equal(f.get('-tooltip').hidden, false);
  assert.match(f.get('-tooltip').textContent, /75 \/ 100 tokens/);
  assert.match(f.get('-tooltip').textContent, /本对话总量.*调用2 次/);
  f.document.fire('keydown', { key: 'Escape' }); assert.equal(f.get('-tooltip').hidden, true);
});
test('polling retains row identity and refreshes an open tooltip', async () => {
  const f = fixture(); await f.open();
  const row = f.get('-content').children[0], meter = row.children[1];
  meter.fire('focus'); f.set(sample('one', [150, 50])); await f.poll(); await f.flush();
  assert.equal(f.get('-content').children[0], row);
  assert.match(f.get('-tooltip').textContent, /150 \/ 200 tokens/);
  f.document.fire('pointerdown', { target: new Element() }); assert.equal(f.get('-tooltip').hidden, true);
});
test('zero and missing tokens are not fabricated; route collisions remain distinct', async () => {
  const data = sample('one', [0, 0]); data.models[1].model = data.models[0].model; data.unreported_token_requests = 4;
  const f = fixture(data); await f.open();
  assert.equal(f.get('-content').children.length, 2);
  for (const row of f.get('-content').children) assert.equal(row.children[1].children[0].children[0].style.width, '0%');
  f.get('-content').children[0].children[1].fire('click'); assert.match(f.get('-tooltip').textContent, /0 \/ 0 tokens/);
  f.get('-info').fire('focus'); assert.match(f.get('-tooltip').textContent, /4 次请求未报告/);
});
test('switch clears old details immediately and ignores late previous-conversation responses', async () => {
  const f = fixture(); await f.open();
  let finish;
  f.respond(() => new Promise(resolve => { finish = resolve; })); f.get('-refresh').fire('click');
  const previous = finish;
  f.respond(async () => ({ ok: true, json: async () => sample('two', []) })); f.switch('two');
  previous({ ok: true, json: async () => sample('one') }); await f.flush();
  assert.match(f.get('-content').textContent, /还没有模型足迹/);
  assert.doesNotMatch(f.get('-content').textContent, /model-/);
  f.switch('two', true); assert.match(f.get('-content').textContent, /请选择/);
  assert.equal(f.get('-tooltip').hidden, true);
});
test('errors clear misleading stale rows; hidden panels do not request data', async () => {
  const f = fixture(); await f.open(); f.respond(async () => ({ ok: false })); await f.refresh();
  assert.match(f.get('-content').textContent, /暂时读不到/);
  f.get('-info').fire('click'); assert.match(f.get('-tooltip').textContent, /暂时离线/);
  f.get('-close').fire('click'); const before = f.calls(); await f.refresh();
  assert.equal(f.calls(), before); assert.equal(f.get('-tooltip').hidden, true);
});
test('model names are plain text and zero division or malformed values never make NaN bars', async () => {
  const data = sample('one', [0]); data.models[0].model = '<img src=x onerror=alert(1)>'; data.models[0].tokens.total_tokens = 'invalid';
  const f = fixture(data); await f.open();
  assert.equal(f.get('-content').children[0].children[0].textContent, data.models[0].model);
  assert.equal(f.get('-content').children[0].children[1].children[0].children[0].style.width, '0%');
});

test('slow requests survive repeated polls/events and coalesce one trailing refresh', async () => {
  const f = fixture(); await f.open();
  let finish, signal;
  f.respond((_url, options) => {
    assert.equal(options.cache, 'no-store'); signal = options.signal;
    return new Promise(resolve => { finish = resolve; });
  });
  f.get('-refresh').fire('click');
  for (let i = 0; i < 3; i++) await f.poll();
  f.window.fire('mewcode:usage-updated', { detail: { conversationId: 'one' } });
  assert.equal(f.calls(), 2); assert.equal(signal.aborted, false);
  const completeFirst = finish;
  completeFirst({ ok: true, json: async () => sample('one', [150, 50]) });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.calls(), 3);
  const meter = f.get('-content').children[0].children[1]; meter.fire('focus');
  assert.match(f.get('-tooltip').textContent, /150 \/ 200 tokens/);
  finish({ ok: true, json: async () => sample('one', [300, 100]) }); await f.flush();
  assert.match(f.get('-tooltip').textContent, /300 \/ 400 tokens/);
});

test('usage events refresh only the active conversation and pause while hidden', async () => {
  const f = fixture(); await f.open();
  f.set(sample('one', [300, 100]));
  f.window.fire('mewcode:usage-updated', { detail: { conversationId: 'other' } });
  f.window.fire('mewcode:conversation-updated', { detail: { conversationId: 'other' } });
  assert.equal(f.calls(), 1);
  f.window.fire('mewcode:usage-updated', { detail: { conversationId: 'one' } }); await f.flush();
  assert.equal(f.calls(), 2);
  f.get('-content').children[0].children[1].fire('focus');
  assert.match(f.get('-tooltip').textContent, /300 \/ 400 tokens/);
  f.document.hidden = true; f.document.fire('visibilitychange'); await f.poll();
  f.window.fire('mewcode:usage-updated', { detail: { conversationId: 'one' } });
  assert.equal(f.calls(), 2);
  f.document.hidden = false; f.document.fire('visibilitychange'); await f.flush();
  assert.equal(f.calls(), 3);
});

test('timed-out fetch releases the refresh slot and the next poll recovers', async () => {
  const f = fixture(); await f.open();
  f.respond((_url, { signal }) => new Promise((_resolve, reject) => {
    signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })));
  }));
  f.get('-refresh').fire('click'); await f.expireRequest(); await f.flush();
  assert.match(f.get('-content').textContent, /暂时读不到/);
  f.respond(async () => ({ ok: true, json: async () => sample() })); await f.poll(); await f.flush();
  assert.equal(f.get('-content').textContent, 'model-0model-1');
});

test('closing cancels in-flight work and late responses cannot replace reopened data', async () => {
  const f = fixture(); await f.open(); let finish, signal;
  f.respond((_url, options) => { signal = options.signal; return new Promise(resolve => { finish = resolve; }); });
  f.get('-refresh').fire('click'); await f.poll(); f.get('-close').fire('click');
  assert.equal(signal.aborted, true);
  f.respond(async () => ({ ok: true, json: async () => sample('one', [300, 100]) }));
  f.get('-toggle').fire('click'); finish({ ok: true, json: async () => sample() }); await f.flush();
  assert.equal(f.calls(), 3);
  f.get('-content').children[0].children[1].fire('focus');
  assert.match(f.get('-tooltip').textContent, /300 \/ 400 tokens/);
});

test('incomplete ledger reads are clearly marked rather than shown as complete', async () => {
  const data = sample(); data.partial = true;
  const f = fixture(data); await f.open();
  assert.equal(f.get('-info').dataset.warning, 'true'); f.get('-info').fire('focus');
  assert.match(f.get('-tooltip').textContent, /部分账本暂时不可读/);
});

test('overlay styles never target the chat layout', () => {
  const css = fs.readFileSync(path.join(__dirname, '../frontend/static/conversation-usage.css'), 'utf8');
  assert.match(css, /#conversation-usage\s*\{\s*position:\s*fixed/);
  assert.doesNotMatch(css, /#messages|#trajectory-view|#input-area|\.input-controls|conversation-usage-open/);
});

test('real chat and graph usage handlers notify the usage panel without a conversation mutation', async () => {
  const f = fixture(); await f.open();
  let conversationMutations = 0;
  f.window.CustomEvent = class { constructor(type, { detail }) { this.type = type; this.detail = detail; } };
  f.window.dispatchEvent = event => f.window.fire(event.type, event);
  f.window.addEventListener('mewcode:conversation-updated', () => ++conversationMutations);
  const source = fs.readFileSync(path.join(__dirname, '../frontend/static/app.js'), 'utf8');
  const emitter = source.slice(source.indexOf('  function emitConversationUpdated('), source.indexOf('  window.MewConversationContext ='));
  const cases = ['usage', 'expert_usage', 'expert_child_event'].map(type => {
    const start = source.indexOf('        case "' + type + '":');
    assert.ok(start >= 0);
    return source.slice(start, source.indexOf('          break;', start) + '          break;'.length);
  }).join('\n');
  const context = { window: f.window, document: f.document, scopedId: 'one', activeConversationId: () => 'other',
    updateUsage() {}, renderExpertUsageV2() {}, renderExpertChildEventV2() {} };
  vm.createContext(context); vm.runInContext(emitter, context);
  for (const type of ['usage', 'expert_usage', 'expert_child_event']) {
    context.msg = { type, child_event_type: 'usage' };
    vm.runInContext('switch (msg.type) {\n' + cases + '\n}', context); await f.flush();
  }
  assert.equal(f.calls(), 4); assert.equal(conversationMutations, 0);
  context.msg = { type: 'expert_child_event', child_event_type: 'delta' };
  vm.runInContext('switch (msg.type) {\n' + cases + '\n}', context);
  assert.equal(f.calls(), 4);
});
