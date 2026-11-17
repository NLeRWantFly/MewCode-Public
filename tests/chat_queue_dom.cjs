/* Functional DOM contract for the production waiting-area module. */
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
    this.textContent = '';
    this.disabled = false;
    this.title = '';
  }
  set className(value) {
    this.classList.values = new Set(String(value || '').split(/\s+/).filter(Boolean));
  }
  get className() { return [...this.classList.values].join(' '); }
  setAttribute(name, value) { this.attributes.set(String(name), String(value)); }
  append(...nodes) { nodes.forEach(node => this.appendChild(node)); }
  appendChild(node) {
    node.parentElement = this;
    this.children.push(node);
    return node;
  }
  replaceChildren(...nodes) {
    this.children.forEach(node => { node.parentElement = null; });
    this.children = [];
    this.append(...nodes);
  }
  closest(selector) {
    let node = this;
    while (node) {
      if (selector === '[data-chat-queue-send-now]' && node.dataset.chatQueueSendNow !== undefined) return node;
      if (selector === '[data-chat-queue-remove]' && node.dataset.chatQueueRemove !== undefined) return node;
      node = node.parentElement;
    }
    return null;
  }
}

class DocumentShim {
  constructor() {
    this.readyState = 'complete';
    this.nodes = new Map();
  }
  createElement(tagName) { return new ElementShim(tagName, this); }
  getElementById(id) { return this.nodes.get(id) || null; }
  register(id, node) { this.nodes.set(id, node); }
}

class CustomEventShim {
  constructor(type, options = {}) {
    this.type = type;
    this.detail = options.detail;
    this.defaultPrevented = false;
  }
  preventDefault() { this.defaultPrevented = true; }
}

const walk = (node, predicate, found = []) => {
  if (predicate(node)) found.push(node);
  for (const child of node.children || []) walk(child, predicate, found);
  return found;
};
const settle = () => new Promise(resolve => setImmediate(resolve));

const documentShim = new DocumentShim();
const root = documentShim.createElement('div');
root.className = 'chat-queue hidden';
documentShim.register('chat-queue', root);
const windowShim = new EventTargetShim();
let activeConversation = 'conv-a';
windowShim.MewConversationContext = {getContext: () => ({conversationId: activeConversation})};
windowShim.crypto = {randomUUID: () => '00000000-0000-4000-8000-000000000001'};

const serverQueues = new Map();
const requests = [];
const response = (payload, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => payload,
});
async function fetchShim(url, options = {}) {
  requests.push({url, options});
  const body = JSON.parse(options.body || '{}');
  if (url === '/api/chat/queue') {
    const items = serverQueues.get(body.conversation_id) || [];
    items.push({
      id: body.queue_id,
      text: body.text,
      attachments: body.attachments.map(id => ({id, name: '<unsafe-name>'})),
      position: items.length + 1,
    });
    serverQueues.set(body.conversation_id, items);
    return response({conversation_id: body.conversation_id, items, accepted_id: body.queue_id});
  }
  const match = String(url).match(/^\/api\/chat\/queue\/([^/]+)\/(send-now|remove)$/);
  if (!match) return response({error: 'unexpected URL'}, 404);
  const id = decodeURIComponent(match[1]);
  let items = [...(serverQueues.get(body.conversation_id) || [])];
  const index = items.findIndex(item => item.id === id);
  if (match[2] === 'send-now' && index >= 0) items.unshift(...items.splice(index, 1));
  if (match[2] === 'remove' && index >= 0) items.splice(index, 1);
  items = items.map((item, position) => ({...item, position: position + 1}));
  serverQueues.set(body.conversation_id, items);
  return response({conversation_id: body.conversation_id, items});
}

const source = fs.readFileSync(path.join(__dirname, '../frontend/static/chat-queue.js'), 'utf8');
vm.runInNewContext(source, {
  window: windowShim,
  document: documentShim,
  CustomEvent: CustomEventShim,
  fetch: fetchShim,
  crypto: windowShim.crypto,
  console,
  setTimeout,
  clearTimeout,
  Math,
  Date,
}, {filename: 'chat-queue.js'});

(async () => {
  const api = windowShim.MewChatQueue;
  assert.ok(api, 'waiting-area API is exposed');
  assert.equal(root.classList.contains('hidden'), true);

  api.syncAll({
    'conv-a': [{id: 'queued-a', text: '<img src=x onerror=alert(1)>', attachments: [], position: 1}],
    'conv-b': [{id: 'queued-b', text: 'other conversation', attachments: [], position: 1}],
  });
  assert.equal(root.classList.contains('hidden'), false);
  assert.equal(walk(root, node => node.classList?.contains('chat-queue-text'))[0].textContent,
    '<img src=x onerror=alert(1)>', 'message copy is rendered as text');
  assert.equal(walk(root, node => node.dataset?.chatQueueSendNow === 'queued-a').length, 1,
    'only the active conversation is rendered');
  assert.equal(walk(root, node => node.dataset?.chatQueueSendNow === 'queued-b').length, 0);

  activeConversation = 'conv-b';
  windowShim.dispatchEvent(new CustomEventShim('mewcode:conversation-context'));
  assert.equal(api.getState().items[0].id, 'queued-b');
  assert.equal(walk(root, node => node.classList?.contains('chat-queue-text'))[0].textContent,
    'other conversation');

  activeConversation = 'conv-a';
  windowShim.dispatchEvent(new CustomEventShim('mewcode:conversation-context'));
  serverQueues.set('conv-a', api.getState().items);
  const accepted = await api.enqueue({
    conversationId: 'conv-a',
    text: 'new follow-up',
    attachments: [{id: 'attachment-1', name: 'local.txt'}],
  });
  assert.equal(accepted.queueId, 'chat-00000000-0000-4000-8000-000000000001');
  const enqueueRequest = requests.at(-1);
  assert.equal(enqueueRequest.options.headers['X-Mew-Chat-Queue'], '1');
  assert.equal(enqueueRequest.options.credentials, 'same-origin');
  assert.deepEqual(JSON.parse(enqueueRequest.options.body).attachments, ['attachment-1']);
  assert.deepEqual(api.getState().items.map(item => item.text), [
    '<img src=x onerror=alert(1)>', 'new follow-up',
  ]);

  const secondSendNow = walk(root, node => node.dataset?.chatQueueSendNow === accepted.queueId)[0];
  root.dispatchEvent({
    type: 'click', target: secondSendNow, defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; },
  });
  await settle();
  await settle();
  assert.equal(requests.at(-1).url,
    '/api/chat/queue/chat-00000000-0000-4000-8000-000000000001/send-now');
  assert.deepEqual(api.getState().items.map(item => item.id), [accepted.queueId, 'queued-a']);

  const removeFirst = walk(root, node => node.dataset?.chatQueueRemove === accepted.queueId)[0];
  root.dispatchEvent({
    type: 'click', target: removeFirst, defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; },
  });
  await settle();
  await settle();
  assert.equal(requests.at(-1).url,
    '/api/chat/queue/chat-00000000-0000-4000-8000-000000000001/remove');
  assert.deepEqual(api.getState().items.map(item => item.id), ['queued-a']);

  api.sync({
    conversation_id: 'conv-a', revision: 10,
    items: [{id: 'new-state', text: 'newer event', attachments: [], position: 1}],
  });
  assert.equal(api.sync({
    conversation_id: 'conv-a', revision: 9,
    items: [{id: 'stale-state', text: 'stale response', attachments: [], position: 1}],
  }), false, 'an older mutation response is ignored');
  assert.deepEqual(api.getState().items.map(item => item.id), ['new-state']);
  assert.equal(api.getState().revision, 10);

  api.sync({conversation_id: 'conv-a', items: [], revision: 11});
  assert.equal(root.classList.contains('hidden'), true);
  console.log('chat queue DOM contract passed: scoping, safe rendering, enqueue, send-now, remove, stale-response rejection, and empty state');
})().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
