const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class EventTargetShim {
  constructor() { this.listeners = new Map(); }
  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }
  dispatchEvent(event) {
    event.target ||= this;
    event.currentTarget = this;
    for (const handler of [...(this.listeners.get(event.type) || [])]) handler.call(this, event);
    return !event.defaultPrevented;
  }
}

class ClassListShim {
  constructor() { this.values = new Set(); }
  add(...names) { names.forEach(name => this.values.add(name)); }
  remove(...names) { names.forEach(name => this.values.delete(name)); }
  contains(name) { return this.values.has(name); }
  toggle(name, force) {
    const next = force === undefined ? !this.values.has(name) : Boolean(force);
    if (next) this.values.add(name); else this.values.delete(name);
    return next;
  }
}

const VOID_TAGS = new Set(['br', 'input', 'img', 'meta', 'link', 'hr', 'textarea']);

const camel = name => name.replace(/-([a-z])/g, (_, c) => c.toUpperCase());

class ElementShim extends EventTargetShim {
  constructor(tagName, ownerDocument) {
    super();
    this.tagName = String(tagName).toUpperCase();
    this.ownerDocument = ownerDocument;
    this.classList = new ClassListShim();
    this.children = [];
    this.parentElement = null;
    this.dataset = {};
    this.attributes = new Map();
    this.textParts = [];
    this.disabled = false;
    this.value = '';
    this.title = '';
    this.open = false;
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this._html = '';
  }
  get textContent() {
    return this.textParts.join('') + this.children.map(c => c.textContent).join('');
  }
  set textContent(value) {
    this.children = [];
    this._html = '';
    this.textParts = [String(value ?? '')];
  }
  get innerHTML() { return this._html; }
  set innerHTML(html) {
    this.children = [];
    this.textParts = [];
    this._html = String(html ?? '');
    parseHtml(this._html, this.ownerDocument, this);
  }
  set className(value) {
    this.classList.values = new Set(String(value || '').split(/\s+/).filter(Boolean));
  }
  get className() { return [...this.classList.values].join(' '); }
  setAttribute(name, value) {
    const text = String(value ?? '');
    this.attributes.set(name, text);
    if (name === 'class') this.className = text;
    if (name.startsWith('data-')) this.dataset[camel(name.slice(5))] = text;
    if (name === 'open') this.open = true;
    if (name === 'title') this.title = text;
  }
  getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }
  appendChild(node) {
    if (node && node.nodeType === 3) {
      this.textParts.push(node.textContent);
      return node;
    }
    node.parentElement = this;
    this.children.push(node);
    return node;
  }
  focus() {}
  matches(selector) {
    const match = String(selector).match(/^([a-zA-Z0-9-]*)((?:\.[\w-]+)*)((?:\[[^\]]+\])*)$/);
    if (!match) return false;
    const [, tag, classPart, attrPart] = match;
    if (tag && this.tagName !== tag.toUpperCase()) return false;
    for (const cls of classPart.match(/\.[\w-]+/g) || []) {
      if (!this.classList.contains(cls.slice(1))) return false;
    }
    for (const attr of attrPart.match(/\[[^\]]+\]/g) || []) {
      const body = attr.slice(1, -1);
      const eq = body.indexOf('=');
      const name = eq < 0 ? body : body.slice(0, eq);
      if (eq < 0) {
        if (!this.attributes.has(name)) return false;
      } else {
        const expected = body.slice(eq + 1).replace(/^["']|["']$/g, '');
        if (this.getAttribute(name) !== expected) return false;
      }
    }
    return true;
  }
  querySelectorAll(selector, found = []) {
    for (const child of this.children) {
      if (child.matches(selector)) found.push(child);
      child.querySelectorAll(selector, found);
    }
    return found;
  }
}

function parseHtml(html, doc, host) {
  const stack = [host];
  const re = /<!--[\s\S]*?-->|<\/?[a-zA-Z][^>]*>|[^<]+/g;
  let token;
  while ((token = re.exec(html))) {
    const text = token[0];
    if (text.startsWith('<!--')) continue;
    if (text.startsWith('</')) {
      if (stack.length > 1) stack.pop();
      continue;
    }
    if (text.startsWith('<')) {
      const tagMatch = text.match(/^<([a-zA-Z0-9-]+)/);
      if (!tagMatch) continue;
      const el = doc.createElement(tagMatch[1]);
      const attrRe = /([a-zA-Z_:][-\w:.]*)(?:\s*=\s*("([^"]*)"|'([^']*)'|[^\s>]+))?/g;
      let attr;
      const attrText = text.slice(tagMatch[0].length, text.endsWith('/>') ? -2 : -1);
      while ((attr = attrRe.exec(attrText))) {
        const value = attr[3] !== undefined ? attr[3]
          : attr[4] !== undefined ? attr[4]
          : attr[2] !== undefined ? attr[2] : '';
        el.setAttribute(attr[1], value);
      }
      stack[stack.length - 1].appendChild(el);
      if (!VOID_TAGS.has(el.tagName.toLowerCase()) && !text.endsWith('/>')) stack.push(el);
      continue;
    }
    if (text.trim()) stack[stack.length - 1].appendChild(doc.createTextNode(text));
  }
}

class DocumentShim {
  constructor() {
    this.readyState = 'complete';
    this.nodes = new Map();
    this.body = new ElementShim('body', this);
    this.visibilityState = 'visible';
  }
  createElement(tagName) { return new ElementShim(tagName, this); }
  createTextNode(text) { return { nodeType: 3, textContent: String(text) }; }
  getElementById(id) { return this.nodes.get(id) || null; }
  register(id, tag = 'div') {
    const node = this.createElement(tag);
    this.nodes.set(id, node);
    return node;
  }
}

const settle = async (rounds = 6) => {
  for (let index = 0; index < rounds; index += 1) {
    await new Promise(resolve => setImmediate(resolve));
  }
};

const documentShim = new DocumentShim();
const windowShim = new EventTargetShim();
windowShim.document = documentShim;
windowShim.matchMedia = () => ({ matches: false, addEventListener() {} });
windowShim.visualViewport = null;
const intervalCallbacks = [];
windowShim.setInterval = (fn) => { intervalCallbacks.push(fn); return intervalCallbacks.length; };
windowShim.clearInterval = () => {};
windowShim.__TAURI_INTERNALS__ = { invoke: async () => ({}) };

[
  'mobile-pairing', 'mobile-pairing-state', 'mobile-pairing-copy', 'mobile-pairing-button',
  'mobile-unpair', 'mobile-node-list', 'mobile-peer-list', 'mobile-manual-host',
  'mobile-connection-text', 'mobile-connection-dot', 'mobile-history', 'mobile-history-count',
  'mobile-detail-sheet', 'mobile-detail-title', 'mobile-detail-meta', 'mobile-detail-back',
  'mobile-sync-chip', 'mobile-new-chat', 'mobile-orch-board', 'mobile-orch-title',
  'mobile-orch-status', 'mobile-orch-steps', 'mobile-interrupt', 'mobile-resume',
  'mobile-prompt-sheet', 'mobile-prompt-badge', 'mobile-prompt-question', 'mobile-prompt-context',
  'mobile-prompt-options', 'mobile-prompt-allow', 'mobile-prompt-always', 'mobile-prompt-deny',
  'mobile-prompt-submit', 'mobile-prompt-cancel', 'mobile-send-status', 'mobile-refresh',
  'mobile-retry',
].forEach(id => documentShim.register(id));
documentShim.register('mobile-host-input', 'input');
documentShim.register('mobile-host-connect', 'button');
documentShim.register('mobile-detail-body', 'div');
documentShim.register('mobile-composer', 'form');
documentShim.register('mobile-composer-input', 'textarea');
documentShim.register('mobile-composer-send', 'button');
documentShim.register('mobile-composer-interrupt', 'button');
documentShim.register('mobile-prompt-other', 'textarea');

const historyEl = documentShim.getElementById('mobile-history');
const detailSheet = documentShim.getElementById('mobile-detail-sheet');
const detailBody = documentShim.getElementById('mobile-detail-body');
const detailTitle = documentShim.getElementById('mobile-detail-title');
const composer = documentShim.getElementById('mobile-composer');
const composerInput = documentShim.getElementById('mobile-composer-input');
const composerSend = documentShim.getElementById('mobile-composer-send');
const composerStatus = documentShim.getElementById('mobile-send-status');
const detailBack = documentShim.getElementById('mobile-detail-back');

const DIR_A = 'C:\\work\\main';
const DIR_B = 'D:\\copy\\main';
const DIR_E = 'C:\\work\\empty';

const items = [];
for (let index = 0; index < 170; index += 1) {
  items.push({
    source: 'mewcode',
    external_id: `a-${index}`,
    scope: 'project',
    title: `对话 ${index}`,
    project_id: 'pA',
    project_path: DIR_A,
    parent_folder: DIR_A,
    updated_at: `2026-09-${String(15 - (index % 14)).padStart(2, '0')}`,
    message_count: index + 1,
  });
}
items.push({
  source: 'mewcode', external_id: 'b-1', scope: 'project', title: '副本对话',
  project_id: 'pB', project_path: DIR_B, parent_folder: DIR_B,
  updated_at: '2026-09-12', message_count: 2,
});
items.push({
  source: 'mewcode', external_id: 'u-1', scope: 'user', title: '普通聊天',
  project_id: '', project_path: '', parent_folder: '普通对话',
  updated_at: '2026-09-11', message_count: 1,
});

const historyPayload = {
  node_id: 'desktop-1',
  active_conversation_id: '',
  running_conversation_ids: [],
  projects: [
    { id: 'pA', dir: DIR_A, label: 'main', active: true },
    { id: 'pB', dir: DIR_B, label: 'main', active: false },
    { id: 'pE', dir: DIR_E, label: 'empty', active: false },
  ],
  items,
};

const requests = [];
let pairedEndpoint = 'desktop.test';
const defaultDetail = async (externalId) => ({
  source: 'mewcode',
  external_id: externalId,
  scope: 'project',
  project_id: 'pA',
  project_path: DIR_A,
  title: `对话 ${externalId}`,
  active: true,
  running: false,
  messages: [{ role: 'user', text: '已存在的消息', kind: 'message' }],
});
const idleLive = async () => ({ running: false, events: [], pending: [], steps: [] });
let sendResponder = async () => ({ operation_id: 'op-0', conversation_id: 'a-0', status: 'running' });
let detailResponder = defaultDetail;
let liveResponder = idleLive;
let createResponder = async () => ({ id: 'new-1', scope: 'project' });
const bridge = {
  mobileBackendUrl: async () => 'http://desktop.test',
  mobilePairingState: async () => ({ paired: true, endpoint: pairedEndpoint, peers: [] }),
  mobileDiscoverNodes: async () => ({ available: true, connected: true, nodes: [] }),
  mobileRemoteRequest: (requestPath, method = 'GET', payload) => {
    requests.push({ path: requestPath, method, payload });
    if (requestPath.startsWith('/api/remote/history?')) return Promise.resolve(historyPayload);
    if (requestPath.startsWith('/api/remote/history/item')) {
      const externalId = decodeURIComponent((requestPath.match(/external_id=([^&]+)/) || [])[1] || '');
      return detailResponder(externalId);
    }
    if (requestPath.startsWith('/api/remote/live')) return liveResponder(requestPath);
    if (requestPath.startsWith('/api/remote/operations/')) {
      return Promise.resolve({ operation_id: 'op-1', status: 'running', events: [] });
    }
    if (requestPath.startsWith('/api/remote/conversations/message')) return sendResponder(payload);
    if (requestPath === '/api/remote/conversations' && method === 'POST') return createResponder();
    return Promise.resolve({ ok: true });
  },
};

const source = fs.readFileSync(path.join(__dirname, '../frontend/static/mobile.js'), 'utf8');
vm.runInNewContext(source, {
  window: windowShim,
  document: documentShim,
  console,
  setTimeout,
  clearTimeout,
  setImmediate,
  Math,
  Date,
  JSON,
  Promise,
  encodeURIComponent,
}, { filename: 'mobile.js' });

const countPosts = () => requests.filter(r => r.path.startsWith('/api/remote/conversations/message')).length;
const click = (el) => el.dispatchEvent({ type: 'click', preventDefault() { this.defaultPrevented = true; } });
const rowFor = (id) => historyEl.querySelectorAll('.mobile-history-row')
  .find(row => row.dataset.id === id);

(async () => {
  windowShim.initMobileRuntime(bridge);
  await settle();

  const rows = historyEl.querySelectorAll('.mobile-history-row');
  assert.equal(rows.length, 172, 'every supplied history row must render (no 160 cap)');
  const groupKeys = historyEl.querySelectorAll('details[data-group-key]').map(el => el.dataset.groupKey);
  assert.ok(groupKeys.includes('project:pA'), 'project A group');
  assert.ok(groupKeys.includes('project:pB'), 'same-basename project B is a separate group');
  assert.ok(groupKeys.includes('project:pE'), 'empty project group is seeded');
  assert.ok(groupKeys.includes('user:普通对话'), 'user conversations stay in 普通对话');
  assert.ok(historyEl.innerHTML.includes('此项目还没有对话'), 'empty project shows placeholder');

  const groupA = historyEl.querySelectorAll('details[data-group-key]').find(el => el.dataset.groupKey === 'project:pA');
  const sourceGroup = historyEl.querySelectorAll('details[data-group-key]').find(el => el.dataset.groupKey === 'source:mewcode');
  groupA.open = false;
  sourceGroup.open = false;
  await intervalCallbacks[0]();
  await settle();
  const afterPoll = historyEl.querySelectorAll('details[data-group-key]');
  assert.equal(afterPoll.find(el => el.dataset.groupKey === 'project:pA').open, false, 'collapsed group stays collapsed across polls');
  assert.equal(afterPoll.find(el => el.dataset.groupKey === 'source:mewcode').open, false, 'collapsed source group stays collapsed across polls');
  assert.equal(historyEl.querySelectorAll('.mobile-history-row').length, 172);
  afterPoll.forEach(el => { el.open = true; });

  click(rowFor('a-0'));
  await settle();
  assert.equal(detailSheet.classList.contains('hidden'), false);
  assert.ok(detailBody.innerHTML.includes('已存在的消息'), 'detail renders history messages');
  const detailRequest = requests.find(r => r.path.startsWith('/api/remote/history/item'));
  const liveRequest = requests.find(r => r.path.startsWith('/api/remote/live'));
  assert.ok(detailRequest.path.includes('project_id=pA'), 'detail query addresses the project');
  assert.ok(liveRequest.path.includes('project_id=pA'), 'live query addresses the project');

  let rejectOldLive;
  liveResponder = requestPath => requestPath.includes('conversation_id=a-1&')
    ? new Promise((resolve, reject) => { rejectOldLive = reject; }) : idleLive();
  click(rowFor('a-1'));
  await settle(2);
  assert.equal(typeof rejectOldLive, 'function');
  click(rowFor('b-1'));
  await settle();
  const bodyBeforeReject = detailBody.innerHTML;
  const titleBeforeReject = detailTitle.textContent;
  rejectOldLive(new Error('old peer dropped'));
  await settle();
  assert.equal(detailBody.innerHTML, bodyBeforeReject);
  assert.equal(detailTitle.textContent, titleBeforeReject);
  assert.equal(documentShim.getElementById('mobile-composer-interrupt').classList.contains('hidden'), true);
  let resolveOldLive;
  liveResponder = requestPath => requestPath.includes('conversation_id=a-2&')
    ? new Promise(resolve => { resolveOldLive = resolve; }) : idleLive();
  click(rowFor('a-2'));
  await settle(2);
  assert.equal(typeof resolveOldLive, 'function');
  click(rowFor('a-3'));
  await settle();
  const bodyBeforeResolve = detailBody.innerHTML;
  resolveOldLive({ conversation_id: 'a-2', running: true, events: [{ type: 'expert_node_delta', node_id: 'old', text: 'STALE_TEXT' }], pending: [{ type: 'permission', description: 'STALE_PROMPT' }], steps: [] });
  await settle();
  assert.equal(detailBody.innerHTML, bodyBeforeResolve);
  assert.equal(documentShim.getElementById('mobile-composer-interrupt').classList.contains('hidden'), true);
  assert.equal(documentShim.getElementById('mobile-prompt-sheet').classList.contains('hidden'), true);
  liveResponder = idleLive;

  let resolveOldDetail;
  detailResponder = externalId => externalId === 'a-4'
    ? new Promise(resolve => { resolveOldDetail = resolve; }) : defaultDetail(externalId);
  click(rowFor('a-4'));
  await settle(2);
  assert.equal(typeof resolveOldDetail, 'function');
  click(rowFor('b-1'));
  await settle();
  const bodyBeforeDetail = detailBody.innerHTML;
  const titleBeforeDetail = detailTitle.textContent;
  resolveOldDetail({
    source: 'mewcode', external_id: 'a-4', scope: 'project', project_id: 'pA',
    project_path: DIR_A, title: 'STALE_DETAIL', active: true, running: true,
    messages: [{ role: 'assistant', text: 'STALE_DETAIL_BODY', kind: 'message' }],
  });
  await settle();
  assert.equal(detailBody.innerHTML, bodyBeforeDetail);
  assert.equal(detailTitle.textContent, titleBeforeDetail);
  detailResponder = defaultDetail;
  click(rowFor('a-0'));
  await settle();

  composerInput.value = '甲对话草稿';
  click(rowFor('b-1'));
  await settle();
  assert.equal(composerInput.value, '', 'switching panes restores the target draft, not A text');
  composerInput.value = '乙对话草稿';
  click(rowFor('a-0'));
  await settle();
  assert.equal(composerInput.value, '甲对话草稿', 'switching back restores A draft');

  let resolveSend;
  let rejectSend;
  sendResponder = () => new Promise((resolve, reject) => { resolveSend = resolve; rejectSend = reject; });
  composerInput.value = '第一条消息';
  click(composerSend);
  await settle(2);
  assert.equal(countPosts(), 1);
  assert.equal(composerSend.disabled, true, 'send button is busy while the POST is in flight');
  assert.equal(composerStatus.textContent, '正在发送到电脑…');
  click(composerSend);
  await settle(2);
  assert.equal(countPosts(), 1, 'duplicate tap must not issue a second POST');
  const sendCall = requests.at(-1);
  assert.equal(sendCall.payload.text, '第一条消息');
  assert.equal(sendCall.payload.project_id, 'pA');

  rejectSend(new Error('连接已断开'));
  await settle();
  assert.equal(composerSend.disabled, false, 'sending releases after the POST settles');
  assert.equal(composerStatus.dataset.state, 'error');
  assert.ok(composerStatus.textContent.includes('发送失败'), 'inline error is visible');
  assert.ok(composerStatus.textContent.includes('连接已断开'));
  assert.equal(composerInput.value, '第一条消息', 'failed send keeps the typed text');
  assert.equal(composerStatus.classList.contains('hidden'), false);

  await intervalCallbacks[0]();
  await settle();
  assert.ok(composerStatus.textContent.includes('发送失败'), 'poll does not clear the error');
  assert.equal(composerInput.value, '第一条消息');

  sendResponder = async () => ({});
  click(composerSend);
  await settle();
  assert.ok(composerStatus.textContent.includes('发送失败'), 'ack-less response reports failure');
  assert.ok(composerStatus.textContent.includes('未确认'));
  assert.equal(composerInput.value, '第一条消息', 'unacknowledged send keeps the text');

  sendResponder = async () => ({ operation_id: 'op-1', conversation_id: 'a-0', status: 'running' });
  click(composerSend);
  await settle();
  assert.equal(composerInput.value, '', 'acked send clears the untouched input');
  assert.equal(composerStatus.textContent, '已发送，等待电脑回复');
  assert.equal(composerStatus.dataset.state, 'connected');
  assert.ok(requests.some(r => r.path.startsWith('/api/remote/operations/op-1')), 'operation is polled');

  sendResponder = () => new Promise((resolve) => { resolveSend = resolve; });
  composerInput.value = '第二条';
  click(composerSend);
  await settle(2);
  composerInput.value = '第三条（发送中补写）';
  resolveSend({ operation_id: 'op-2', conversation_id: 'a-0', status: 'running' });
  await settle();
  assert.equal(composerInput.value, '第三条（发送中补写）', 'newer typing survives the ack');

  sendResponder = async () => ({ queued: true, conversation_id: 'a-0', status: 'queued' });
  click(composerSend);
  await settle();
  assert.equal(composerStatus.textContent, '已排队，当前回复结束后继续');
  assert.equal(composerInput.value, '');

  sendResponder = () => new Promise((resolve) => { resolveSend = resolve; });
  composerInput.value = '切换前发送';
  click(composerSend);
  await settle(2);
  click(rowFor('b-1'));
  await settle(2);
  assert.equal(detailTitle.textContent, '对话 b-1');
  assert.equal(composerInput.value, '乙对话草稿', 'B pane restores its own draft');
  assert.equal(composerSend.disabled, true, 'in-flight send keeps the composer locked across panes');
  resolveSend({ operation_id: 'op-9', conversation_id: 'a-0', status: 'running' });
  await settle();
  assert.equal(composerSend.disabled, false, 'send lock releases once the in-flight POST settles');
  assert.notEqual(composerStatus.textContent, '已发送，等待电脑回复',
    'late ack for a closed pane must not claim success in the new pane');
  click(rowFor('a-0'));
  await settle();
  assert.equal(composerInput.value, '', 'acked send cleared A saved draft');
  click(rowFor('b-1'));
  await settle();
  assert.equal(composerInput.value, '乙对话草稿', 'B draft untouched by A ack');

  sendResponder = () => new Promise((resolve) => { resolveSend = resolve; });
  composerInput.value = '关闭前发送';
  click(composerSend);
  await settle(2);
  click(detailBack);
  resolveSend({ operation_id: 'op-10', conversation_id: 'b-1', status: 'running' });
  await settle();
  assert.equal(detailSheet.classList.contains('hidden'), true);
  assert.equal(composerStatus.classList.contains('hidden'), true, 'closed pane keeps no stale status');
  assert.equal(composerSend.disabled, true, 'closed pane keeps composer disabled without a conversation');

  click(rowFor('a-1'));
  await settle();
  composerInput.value = '拼音输入中';
  const postsBefore = countPosts();
  composerInput.dispatchEvent({ type: 'keydown', key: 'Enter', keyCode: 229, isComposing: true, preventDefault() { this.defaultPrevented = true; } });
  await settle(2);
  assert.equal(countPosts(), postsBefore, 'IME Enter does not send');
  composerInput.dispatchEvent({ type: 'keydown', key: 'Enter', keyCode: 13, isComposing: false, preventDefault() { this.defaultPrevented = true; } });
  await settle(2);
  assert.equal(countPosts(), postsBefore + 1, 'plain Enter sends');
  resolveSend({ operation_id: 'op-ime', conversation_id: 'a-1', status: 'running' });
  await settle();

  sendResponder = () => new Promise((resolve) => { resolveSend = resolve; });
  click(rowFor('a-0'));
  await settle();
  composerInput.value = '同一文本';
  click(composerSend);
  await settle(2);
  pairedEndpoint = 'desktop-2.test';
  click(documentShim.getElementById('mobile-refresh'));
  await settle();
  assert.equal(detailSheet.classList.contains('hidden'), true, 'peer switch closes the old pane');
  click(rowFor('a-0'));
  await settle();
  composerInput.value = '同一文本';
  resolveSend({ operation_id: 'op-peer', conversation_id: 'a-0', status: 'running' });
  await settle();
  assert.equal(composerInput.value, '同一文本', 'late ack from the old peer must not clear the new peer draft');
  assert.notEqual(composerStatus.textContent, '已发送，等待电脑回复');
  click(rowFor('b-1'));
  await settle();
  click(rowFor('a-0'));
  await settle();
  assert.equal(composerInput.value, '同一文本', 'new-peer draft survives the stale ack');
  pairedEndpoint = 'desktop.test';

  let resolveCreate;
  createResponder = () => new Promise(resolve => { resolveCreate = resolve; });
  click(documentShim.getElementById('mobile-new-chat'));
  await settle(2);
  assert.equal(typeof resolveCreate, 'function');
  pairedEndpoint = 'desktop-3.test';
  click(documentShim.getElementById('mobile-refresh'));
  await settle();
  resolveCreate({ id: 'late-1', scope: 'project' });
  await settle();
  assert.equal(detailSheet.classList.contains('hidden'), true, 'late create response does not open a pane on the new peer');
  pairedEndpoint = 'desktop.test';
  createResponder = async () => ({ id: 'new-1', scope: 'project' });

  console.log('mobile collab DOM contract passed: full history groups, guarded send ack, retained input, IME guard, pane-switch isolation');
})().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
