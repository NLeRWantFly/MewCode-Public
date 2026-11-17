/* Dependency-free regressions for the host-only native-execution dialog. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const appSource = fs.readFileSync(path.join(__dirname, '../frontend/static/app.js'), 'utf8');
const dialogStart = appSource.indexOf('  $("native-execution-btn").addEventListener("click"');
const dialogEnd = appSource.indexOf('  $("task-auth-btn").addEventListener("click"', dialogStart);
assert.ok(dialogStart >= 0 && dialogEnd > dialogStart, 'native-execution dialog source boundary exists');
const dialogSource = appSource.slice(dialogStart, dialogEnd);

class Element {
  constructor(tagName = 'div') {
    this.tagName = tagName.toLowerCase();
    this.children = [];
    this.handlers = new Map();
    this.style = {};
    this.disabled = false;
    this.value = '';
    this._text = '';
    this.textWrites = 0;
    this.htmlWrites = 0;
  }
  set textContent(value) {
    this.textWrites += 1;
    this._text = String(value);
    this.children = [];
  }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set innerHTML(value) {
    this.htmlWrites += 1;
    this._html = String(value);
    this._text = '';
    this.children = [];
  }
  get innerHTML() { return this._html || ''; }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.children.push(child); return child; }
  addEventListener(name, callback) {
    const handlers = this.handlers.get(name) || [];
    handlers.push(callback);
    this.handlers.set(name, handlers);
  }
  fire(name, event = {}) {
    return (this.handlers.get(name) || []).map(callback => callback(event));
  }
  async click() {
    if (this.disabled) return;
    const results = (this.handlers.get('click') || []).map(callback => callback({ currentTarget: this }));
    await Promise.all(results);
  }
}

function response(data, ok = true) {
  return { ok, json: async () => data };
}

function nativeState(overrides = {}) {
  return {
    conversation_id: 'conversation-current',
    task_id: 'task-current',
    workspace: 'C:\\workspace',
    gate: { state: 'blocked', reason: '尚未满足门禁', missing_evidence: [] },
    storage: { directory: 'C:\\private-stage', revision: 'revision-17' },
    transactions: [{
      id: 'transaction-123456789', current_task: true, state: 'recovery_required', cleanup_confirmed: false,
    }],
    ...overrides,
  };
}

function fixture(fetchImpl = async () => response(nativeState())) {
  const trigger = new Element('button');
  const elements = { 'native-execution-btn': trigger };
  const document = {
    createElement: tagName => new Element(tagName),
    createTextNode: value => { const node = new Element('#text'); node.textContent = value; return node; },
  };
  const calls = [];
  const notices = [];
  let dialog;
  const context = {
    document,
    $(id) { return elements[id]; },
    fetch(url, options) { calls.push({ url, options }); return fetchImpl(url, options); },
    notify(...args) { notices.push(args); },
    openSettingsDialog(title, _trigger, build) {
      const body = new Element('section');
      const footer = new Element('footer');
      dialog = { title, body, footer };
      build(body, footer);
    },
  };
  vm.runInNewContext(dialogSource, context, { filename: 'app.js native-execution dialog' });
  const walk = (root) => [root, ...root.children.flatMap(walk)];
  return {
    calls, notices, trigger,
    get dialog() { return dialog; },
    all() { return [...walk(dialog.body), ...walk(dialog.footer)]; },
    byText(text) { return this.all().find(element => element.textContent === text); },
    async open() { await trigger.click(); await flush(); },
    setFetch(fn) { fetchImpl = fn; },
  };
}

function flush() {
  return new Promise(resolve => setImmediate(() => setImmediate(resolve)));
}

test('status and backend-provided reasons are rendered as text, with an explicit host-control button', async () => {
  const hostileReason = '<img src=x onerror=alert(1)> gate reason';
  const f = fixture(async () => response(nativeState({
    gate: { state: 'blocked', reason: hostileReason, missing_evidence: [] },
  })));
  await f.open();

  const summary = f.all().find(element => element.tagName === 'p' && element.textContent.includes('gate reason'));
  assert.match(summary.textContent, /<img src=x onerror=alert\(1\)> gate reason/);
  assert.equal(summary.htmlWrites, 0, 'backend status must never be interpreted as markup');
  assert.equal(f.dialog.title, '沙箱设置与执行恢复');
  assert.ok(f.byText('确认设置当前卷暂存目录'), 'storage changes require an explicit confirmation action');
  assert.ok(f.byText('检查并恢复'), 'recovery is a separately labelled host action');
});

test('private cleanup explains available edits without irrelevant file recovery confirmation', async () => {
  const f = fixture(async () => response(nativeState({ transactions: [{
    id: 'private-run', current_task: false, state: 'cleanup_unknown', cleanup_confirmed: false, private_only: true,
    message:'旧隔离命令缺少清理凭证，当前恢复入口无法确认收尾。', cleanup_evidence:'missing', failure_type:'RuntimeError',
    next_step:'需要修复宿主清理证据链。',
  }] })));
  await f.open();
  assert.match(f.dialog.body.textContent, /文件编辑可继续/);
  assert.match(f.dialog.body.textContent, /隔离命令暂不可用/);
  assert.match(f.dialog.body.textContent, /事务 ID：private-run/);
  assert.match(f.dialog.body.textContent, /状态：cleanup_unknown/);
  assert.match(f.dialog.body.textContent, /缺少清理凭证/);
  assert.equal(f.byText('检查并恢复').disabled, false);
  assert.equal(f.byText('查看需要恢复的文件').hidden, true);
  assert.equal(f.byText('确认仅核验或回滚以上条目').hidden, true);
});

test('Auto permits independent work while retaining an unknown isolated transaction', async () => {
  const f = fixture(async () => response(nativeState({
    execution_backend: 'host_reviewed', commands_ready: true,
    transactions: [{ id: 'private-run', state: 'cleanup_unknown', private_only: true, cleanup_evidence: 'missing' }],
  })));
  await f.open();
  assert.match(f.dialog.body.textContent, /独立的新操作可经 Auto 审查继续/);
  assert.match(f.dialog.body.textContent, /不能重放旧命令/);
  assert.match(f.dialog.body.textContent, /状态：cleanup_unknown/);
  assert.doesNotMatch(f.dialog.body.textContent, /命令暂不可用/);
});

test('storage save is bound to the loaded conversation, task and revision and carries the explicit control header', async () => {
  const f = fixture();
  await f.open();
  const directory = f.all().find(element => element.tagName === 'input');
  directory.value = '  C:\\workspace\\.host-stage  ';
  const save = f.byText('确认设置当前卷暂存目录');
  assert.ok(save);
  await save.click();

  const post = f.calls.find(call => call.options?.method === 'POST');
  assert.ok(post);
  assert.equal(post.url, '/api/chat/native-execution/storage');
  assert.equal(post.options.headers['X-Mew-Native-Control'], '1');
  const payload = JSON.parse(post.options.body);
  assert.deepEqual(payload, {
    conversation_id: 'conversation-current', task_id: 'task-current',
    directory: 'C:\\workspace\\.host-stage', revision: 'revision-17',
  });
  assert.equal(Object.hasOwn(payload, 'allowed_tools'), false);
  assert.equal(Object.hasOwn(payload, 'authorization'), false);
});

test('reconcile is only a current-task host request and does not carry execution or permission grants', async () => {
  const f = fixture(async (url, options) => {
    if (options?.method === 'POST') {
      return response({ complete: false, outcomes: [{ state: 'recovery_required', reason: 'file conflict' }] });
    }
    return response(nativeState());
  });
  await f.open();
  const recover = f.byText('检查并恢复');
  assert.ok(recover && !recover.disabled);
  await recover.click();

  const post = f.calls.find(call => call.options?.method === 'POST');
  assert.equal(post.url, '/api/chat/native-execution/reconcile');
  assert.equal(post.options.headers['X-Mew-Native-Control'], '1');
  const payload = JSON.parse(post.options.body);
  assert.equal(payload.conversation_id, 'conversation-current');
  assert.equal(payload.task_id, 'task-current');
  assert.equal(payload.revision, 'revision-17');
  assert.equal(Object.hasOwn(payload, 'command'), false);
  assert.equal(Object.hasOwn(payload, 'allowed_tools'), false);
  assert.match(f.dialog.body.children[0].textContent, /仍需处理：file conflict/);
});

test('recovery requires a displayed preview and posts only its one-shot transaction token and id', async () => {
  const transactionId = 'a'.repeat(32);
  const previewToken = 'bound-preview-token';
  const f = fixture(async (url, options) => {
    if (url.endsWith('/recovery-preview')) {
      return response({ preview: {
        transaction_id: transactionId, token: previewToken,
        original_task_id: 'old-task', current_task_id: 'task-current',
        targets: ['a.txt', 'nested/result.json'],
      } });
    }
    if (url.endsWith('/reconcile') && options?.method === 'POST') {
      return response({ complete: false, outcomes: [{ state: 'recovery_required', reason: 'cleanup unknown' }] });
    }
    return response(nativeState({ transactions: [{
      id: transactionId, current_task: false, state: 'recovery_required', cleanup_confirmed: false,
    }] }));
  });
  await f.open();

  const preview = f.byText('查看需要恢复的文件');
  const confirm = f.byText('确认仅核验或回滚以上条目');
  assert.ok(preview && confirm);
  assert.equal(confirm.disabled, true);
  await preview.click();
  assert.equal(confirm.disabled, false);
  const previewText = f.all().find(element => element.tagName === 'pre');
  assert.match(previewText.textContent, /仅核验已提交结果或回滚未完成发布；不执行命令/);
  assert.match(previewText.textContent, /a\.txt\nnested\/result\.json/);

  await confirm.click();
  const previewPost = f.calls.find(call => call.url.endsWith('/recovery-preview'));
  assert.equal(previewPost.options.headers['X-Mew-Native-Control'], '1');
  assert.deepEqual(JSON.parse(previewPost.options.body), {
    conversation_id: 'conversation-current', task_id: 'task-current', transaction_id: transactionId,
  });
  const reconcilePost = f.calls.find(call => call.url.endsWith('/reconcile') && call.options?.method === 'POST');
  assert.ok(reconcilePost);
  assert.equal(reconcilePost.options.headers['X-Mew-Native-Control'], '1');
  const payload = JSON.parse(reconcilePost.options.body);
  assert.deepEqual(payload.confirmation, { transaction_id: transactionId, token: previewToken });
  assert.equal(payload.conversation_id, 'conversation-current');
  assert.equal(payload.task_id, 'task-current');
  assert.equal(Object.hasOwn(payload, 'command'), false);
  assert.equal(Object.hasOwn(payload, 'allowed_tools'), false);
});

test('changing recovery selection or refreshing discards the earlier preview', async () => {
  const firstId = 'a'.repeat(32);
  const secondId = 'b'.repeat(32);
  const f = fixture(async (url, options) => {
    if (url.endsWith('/recovery-preview')) {
      return response({ preview: { transaction_id: JSON.parse(options.body).transaction_id,
        token: 'preview-token', targets: ['a.txt'], original_task_id: 'old', current_task_id: 'task-current' } });
    }
    return response(nativeState({ transactions: [firstId, secondId].map(id => ({
      id, current_task: false, state: 'recovery_required', cleanup_confirmed: false,
    })) }));
  });
  await f.open();
  const transaction = f.all().find(element => element.tagName === 'select');
  const preview = f.byText('查看需要恢复的文件');
  const confirm = f.byText('确认仅核验或回滚以上条目');
  const previewText = f.all().find(element => element.tagName === 'pre');

  assert.equal(transaction.value, firstId);
  await preview.click();
  assert.equal(confirm.disabled, false);
  transaction.value = secondId;
  transaction.fire('change');
  assert.equal(confirm.disabled, true);
  assert.equal(previewText.textContent, '');
  await confirm.click();
  assert.equal(f.calls.some(call => call.url.endsWith('/reconcile') && call.options?.method === 'POST'), false);

  await preview.click();
  assert.equal(confirm.disabled, false);
  await f.byText('刷新状态').click();
  assert.equal(confirm.disabled, true);
  assert.equal(previewText.textContent, '');
  assert.equal(f.calls.filter(call => call.url === '/api/chat/native-execution').length, 2);
  assert.deepEqual(f.calls.filter(call => call.url.endsWith('/recovery-preview'))
    .map(call => JSON.parse(call.options.body).transaction_id), [firstId, secondId]);
});

test('host errors are surfaced to the user and a failed save does not silently close the dialog', async () => {
  const f = fixture(async (_url, options) => {
    if (options?.method === 'POST') return response({ error: 'revision changed; reload required' }, false);
    return response(nativeState());
  });
  await f.open();
  await f.byText('确认设置当前卷暂存目录').click();

  assert.ok(f.notices.some(([message, type]) => message === 'revision changed; reload required' && type === 'error'));
  assert.match(f.dialog.body.textContent, /revision changed; reload required/);
  assert.equal(f.dialog.title, '沙箱设置与执行恢复');
  assert.match(f.dialog.body.children[0].textContent, /执行环境：尚未满足门禁/);
  assert.ok(f.byText('确认设置当前卷暂存目录'));
});

test('pending host operation disables all dialog controls so repeated user clicks do not duplicate it', async () => {
  let finishPost;
  const f = fixture((_url, options) => {
    if (options?.method === 'POST') return new Promise(resolve => { finishPost = resolve; });
    return Promise.resolve(response(nativeState()));
  });
  await f.open();
  const save = f.byText('确认设置当前卷暂存目录');
  const recover = f.byText('检查并恢复');
  const refresh = f.byText('刷新状态');
  const pending = save.click();
  await flush();

  assert.equal(save.disabled, true);
  assert.equal(recover.disabled, true);
  assert.equal(refresh.disabled, true);
  await save.click();
  assert.equal(f.calls.filter(call => call.options?.method === 'POST').length, 1);

  finishPost(response({ complete: true, outcomes: [] }));
  await pending;
  assert.equal(f.calls.filter(call => call.options?.method === 'POST').length, 1);
});
