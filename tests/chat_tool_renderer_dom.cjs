// Execute the real tool-row and live-result renderers from app.js in a small DOM VM.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'frontend/static/app.js'), 'utf8');

function sourceBetween(startMarker, endMarker, label) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start);
  if (start < 0 || end < 0) throw new Error(`${label} source missing`);
  return source.slice(start, end);
}

const escSource = sourceBetween(
  '  function esc(s) {', '\n  function notify(', 'esc',
);
const argSource = sourceBetween(
  '  function shortToolPath(value) {', '\n  function stopTurnClock(', 'tool argument helpers',
);
const renderSource = sourceBetween(
  '  function buildToolGroup(tools) {', '\n  function settlePendingToolRows(', 'tool renderer',
);

function escapeText(value) {
  return String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function decodeText(value) {
  return String(value).replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
}

class FakeElement {
  constructor(tagName) {
    this.tagName = tagName;
    this.className = '';
    this.dataset = {};
    this.children = [];
    this._classes = new Set();
    this._text = '';
    this._html = '';
    this.classList = {
      contains: (name) => this._classes.has(name),
      add: (name) => this._classes.add(name),
      remove: (name) => this._classes.delete(name),
      toggle: (name, force) => {
        const enabled = force === undefined ? !this._classes.has(name) : !!force;
        if (enabled) this._classes.add(name);
        else this._classes.delete(name);
        return enabled;
      },
    };
  }

  set textContent(value) { this._text = value == null ? '' : String(value); }
  get textContent() { return this._text; }

  set innerHTML(value) {
    this._html = String(value);
    if (this.tagName === 'details' && this.className.includes('tg-row')) {
      const match = this._html.match(/<div class='tool-body'>([\s\S]*?)<\/div>/);
      if (match) this.querySelector('.tool-body').textContent = decodeText(match[1]);
    }
  }

  get innerHTML() {
    return this.tagName === 'div' ? escapeText(this._text) : this._html;
  }

  appendChild(child) {
    this.children.push(child);
    return child;
  }

  querySelector(selector) {
    if (this.tagName === 'details' && this.className.includes('tool-group')) {
      if (selector === '.tg-body') return this.tgBody ||= new FakeElement('div');
      if (selector === '.tg-title') return this.tgTitle ||= new FakeElement('span');
      if (selector === '.tg-status') return this.tgStatus ||= new FakeElement('span');
    }
    if (this.tagName === 'details' && this.className.includes('tg-row')) {
      if (selector === '.tool-elapsed') return this.elapsed ||= new FakeElement('span');
      if (selector === '.tool-body') return this.toolBody ||= new FakeElement('div');
      if (selector === '.tool-name') return this.toolName ||= new FakeElement('span');
    }
    return null;
  }

  querySelectorAll(selector) {
    if (selector !== '.tg-row') return [];
    return (this.tgBody?.children || []).filter((row) => row.className.includes('tg-row'));
  }
}

const bubble = new FakeElement('div');
const pendingTurnToolIds = new Set();
const toolRows = new Map();
const context = {
  document: { createElement: (tag) => new FakeElement(tag) },
  currentMsg: { querySelector: (selector) => selector === '.msg-bubble' ? bubble : null },
  currentToolGroup: null,
  pendingTurnToolIds,
  toolRows,
  beginAssistant() { throw new Error('unexpected assistant initialization'); },
  finalizeThinking() {},
  closeMarkdownSegment() {},
  scrollBottom() {},
};

const runner = `${escSource}\n${argSource}\n${renderSource}\n(
  () => {
    const row = (args, name = 'Echo') => buildToolRow({ name, args, done: false });
    const snapshots = {
      emptyObject: row({}).querySelector('.tool-body').textContent,
      emptyArray: row([]).querySelector('.tool-body').textContent,
      jsonString: row('{"raw": true}').querySelector('.tool-body').textContent,
      nullValue: row(null).querySelector('.tool-body').textContent,
      undefinedValue: row(undefined).querySelector('.tool-body').textContent,
      falseValue: row(false).querySelector('.tool-body').textContent,
      zeroValue: row(0).querySelector('.tool-body').textContent,
      arrayValue: row([1, {ok: true}]).querySelector('.tool-body').textContent,
      escapedObject: row({html: '<img src=x onerror=alert(1)>'}).innerHTML,
      legacyObjectResult: buildToolRow({
        name: 'Echo', args: {}, result: {count: 2}, done: true,
      }).querySelector('.tool-body').textContent,
      namedSkill: row({name: 'computer-use'}, 'LoadSkill').dataset.summary,
      emptySkill: row({}, 'LoadSkill').dataset.summary,
    };
    renderTool({id: 'live-1', name: 'LoadSkill', args: {name: 'computer-use'}});
    const liveRow = toolRows.get('live-1');
    const beforeResult = liveRow.querySelector('.tool-body').textContent;
    renderToolResult({id: 'live-1', output: {loaded: true, html: '<script>bad()</script>'}});
    snapshots.liveSummary = liveRow.dataset.summary;
    snapshots.liveBeforeResult = beforeResult;
    snapshots.liveObjectResult = liveRow.querySelector('.tool-body').textContent;
    snapshots.liveObjectResultHTML = liveRow.querySelector('.tool-body').innerHTML;
    snapshots.liveDone = liveRow.dataset.done === '1';
    snapshots.liveGroupStatus = currentToolGroup.querySelector('.tg-status').textContent;
    renderTool({id: 'live-false', name: 'BooleanTool', args: null});
    renderToolResult({id: 'live-false', output: false});
    snapshots.liveFalseResult = toolRows.get('live-false').querySelector('.tool-body').textContent;
    renderTool({id: 'live-zero', name: 'NumericTool', args: null});
    renderToolResult({id: 'live-zero', output: 0});
    snapshots.liveZeroResult = toolRows.get('live-zero').querySelector('.tool-body').textContent;
    snapshots.pendingCount = pendingTurnToolIds.size;
    return snapshots;
  }
)()`;

const result = vm.runInNewContext(runner, context);
process.stdout.write(JSON.stringify(result));
