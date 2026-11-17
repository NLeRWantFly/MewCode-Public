const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'frontend/static/app.js'), 'utf8');
const start = source.indexOf('  function addHandoffReviewCard(');
const end = source.indexOf('  function addErrorMsg(', start);
assert.ok(start > 0 && end > start);

class Element {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.textContent = '';
    this.className = '';
  }
  appendChild(child) { this.children.push(child); return child; }
  append(...children) { children.forEach(child => this.appendChild(child)); }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  get childElementCount() { return this.children.length; }
  get value() {
    return this._value === undefined && this.tag === 'select'
      ? (this.children[0]?.value || '') : (this._value || '');
  }
  set value(value) { this._value = value; }
}

const messagesEl = new Element('main');
const sent = [];
const errors = [];
const context = {
  document: { createElement: tag => new Element(tag) },
  messagesEl,
  hideEmptyState() {}, scrollBottom() {},
  send: value => sent.push(value),
  addErrorMsg: text => errors.push(text),
};
vm.runInNewContext(source.slice(start, end) +
  '\nglobalThis.renderCard=addHandoffReviewCard;' +
  '\nglobalThis.renderExcerpt=addHandoffReviewExcerpt;' +
  '\nglobalThis.renderWorklist=addHandoffReviewWorklist;', context);

function descendants(node) {
  return [node, ...node.children.flatMap(descendants)];
}
function one(node, className) {
  const matches = descendants(node).filter(child => child.className.split(' ').includes(className));
  assert.equal(matches.length, 1, `expected one ${className}`);
  return matches[0];
}

context.renderCard('run-a', {
  source_id: 'source-a', raw_bytes: 2653896, total: 1284, classified: 1281,
  core_total: 799, supporting_total: 485, reviewed: 799,
  retained: 0, held: 799, omission_reviewed: 0, deterministic_hold_records: 706,
  estimated_tokens_upper: 214641, budget_shortage: 'budget_unknown',
  first_unreviewed_ref: 'record-a', first_hold_ref: 'record-b',
  routes: [{provider_key:'desktop:example_provider_7', model_id:'jev-latest',
    label:'desktop:example_provider_7 · jev-latest'}],
}, 'conversation-a');
const card = messagesEl.children[0];
assert.equal(card.className, 'msg handoff-review-card');
assert.equal(card.attributes['aria-label'], '历史暂停接力审阅归档');
assert.match(one(card, 'handoff-review-note').textContent, /不再作为恢复前置条件/);
assert.equal(one(card, 'handoff-review-actions').childElementCount, 3);
assert.ok(!descendants(card).some(item => ['input', 'select'].includes(item.tag)));
one(card, 'handoff-review-actions').children[0].listeners.click();
assert.equal(sent.length, 1);
assert.equal(sent[0].type, 'handoff_review_expand');
assert.equal(sent[0].source_id, 'source-a');
assert.equal(sent[0].conversation_id, 'conversation-a');

context.renderCard('run-a', {
  source_id:'source-a', total:1284, classified:1281,
  estimated_tokens_upper:249641,
  budget_recommendation: {
    suggested_total_tokens:250000, spent_tokens:0,
    unknown_reserved_tokens:0, estimated_remaining_tokens_upper:249641,
  },
  blocker_diagnosis: {action:'request_explicit_total_cap'},
  routes:[{provider_key:'desktop:example_provider_7',model_id:'jev-latest'}],
}, 'conversation-a');
const suggestedCard = messagesEl.children[1];
assert.ok(!descendants(suggestedCard).some(item => ['input', 'select'].includes(item.tag)));
assert.equal(sent.length, 1, 'historical budgets cannot authorize calls');
one(suggestedCard, 'handoff-review-actions').children[0].listeners.click();
assert.equal(sent.at(-1).type, 'handoff_review_worklist');
assert.equal(sent.at(-1).source_id, 'source-a');

const sourceText = JSON.stringify({type:'assistant',content:[
  {type:'text',text:'Untrusted source <script>alert(1)</script>'},
  {type:'tool_use',name:'Bash',id:'call-a',input:{command:'Get-ChildItem'}},
]});
context.renderExcerpt({run_id:'run-a',view:{record_id:'record-a',view_offset:0,
  view_total_chars:sourceText.length,visible_text:sourceText}}, 'conversation-a');
const excerpt = messagesEl.children[2];
assert.equal(excerpt.className, 'msg handoff-review-excerpt');
assert.equal(one(excerpt, 'handoff-review-readable').childElementCount, 2);
const raw = one(excerpt, 'handoff-review-raw');
assert.equal(raw.open, undefined, 'readable JSON keeps exact raw source collapsed but available');
assert.equal(raw.children[1].textContent, sourceText);
assert.equal(one(excerpt, 'handoff-review-readable').children[0].children[1].textContent,
  'Untrusted source <script>alert(1)</script>');

context.renderExcerpt({run_id:'run-a',view:{record_id:'record-b',view_offset:0,
  source_id:'source-old', next_view_offset:12,
  view_total_chars:1000,visible_text:'{"content":['}}, 'conversation-a');
assert.equal(one(messagesEl.children[3], 'handoff-review-raw').open, true,
  'partial source must stay visible as raw text');
messagesEl.children[3].children.at(-1).listeners.click();
assert.equal(sent.at(-1).source_id, 'source-old');
assert.equal(sent.at(-1).view_offset, 12);

context.renderCard('run-a', {
  source_id:'source-a', total:1284, classified:1284, core_total:799,
  supporting_total:485, reviewed:799, retained:0, held:799,
  authorized_tokens:2000000, deterministic_hold_records:0,
  semantic_hold_reasons:{semantic_uncertain:71},
  primary_route:{provider_key:'desktop:example_provider_7', model_id:'jev-latest'},
  routes:[
    {provider_key:'desktop:example_provider_7', model_id:'jev-latest'},
    {provider_key:'desktop:example_provider_7', model_id:'jev-preview'},
  ],
}, 'conversation-a');
const escalatedCard = messagesEl.children[4];
assert.ok(!descendants(escalatedCard).some(item => ['input', 'select'].includes(item.tag)));
assert.match(one(escalatedCard, 'handoff-review-status').textContent, /只读归档/);

context.renderWorklist({run_id:'run-a',page:{offset:0,total:26,next_offset:25,source_id:'source-old',
  items:[{record_id:'record-hard',record_type:'tool_result',reason:'unknown',
    tools:['Bash'],candidate_host_receipts:1,
    required_evidence:'inspect_command_effects_and_external_state'}]}}, 'conversation-a');
const worklist = messagesEl.children[5];
assert.match(worklist.children[1].textContent, /未核验副作用/);
assert.equal(one(worklist, 'handoff-review-source-part').children[2].textContent, '展开来源');
one(worklist, 'handoff-review-source-part').children[2].listeners.click();
assert.equal(sent.at(-1).reference_id, 'record-hard');
assert.equal(sent.at(-1).source_id, 'source-old');
assert.ok(!descendants(worklist).some(item => /核验本页/.test(item.textContent)));
worklist.children.at(-1).listeners.click();
assert.equal(sent.at(-1).type, 'handoff_review_worklist');
assert.equal(sent.at(-1).offset, 25);
assert.equal(sent.at(-1).source_id, 'source-old');
assert.ok(sent.every(item => ['handoff_review_expand', 'handoff_review_worklist'].includes(item.type)));

const css = fs.readFileSync(path.join(root, 'frontend/static/chat-messages.css'), 'utf8');
assert.match(css, /\.msg\.handoff-review-card/);
assert.match(css, /\.msg\.handoff-review-excerpt/);
console.log('retired handoff archive, read-only paging, readable excerpt, and exact raw fallback passed');
