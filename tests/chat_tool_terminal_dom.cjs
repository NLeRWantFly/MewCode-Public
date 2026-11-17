// Execute the actual app.js terminal-state function against a minimal DOM.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'frontend/static/app.js'), 'utf8');
const start = source.indexOf('  function settlePendingToolRows(msg = {}) {');
const end = source.indexOf('\n  function buildTurnChangesCard', start);
if (start < 0 || end < 0) throw new Error('settlePendingToolRows source missing');
const functionSource = source.slice(start, end);

function classList(initial = []) {
  const values = new Set(initial);
  return {
    contains: (name) => values.has(name),
    toggle: (name, force) => force ? values.add(name) : values.delete(name),
  };
}

const span = { textContent: '执行中', classList: classList() };
const body = { textContent: '{\n  "action": "list"\n}' };
const status = { textContent: '…' };
const group = {
  classList: classList(['is-running']),
  querySelector: (selector) => selector === '.tg-status' ? status : null,
};
const row = {
  dataset: { done: '0' },
  classList: classList(),
  querySelector: (selector) => selector === '.tool-elapsed' ? span : body,
  closest: () => group,
};
const pendingTurnToolIds = new Set(['tool-1']);
const context = {
  pendingTurnToolIds,
  toolRows: new Map([['tool-1', row]]),
  updateGroupTitle: (target) => {
    target.classList.toggle('is-running', false);
    status.textContent = '完成';
  },
};
vm.runInNewContext(
  functionSource + '\nsettlePendingToolRows({ interrupted: true });',
  context,
);

process.stdout.write(JSON.stringify({
  done: row.dataset.done === '1',
  label: span.textContent,
  groupRunning: group.classList.contains('is-running'),
  groupStatus: status.textContent,
  hasExplanation: body.textContent.includes('随本轮任务中断'),
  pendingCount: pendingTurnToolIds.size,
}));
