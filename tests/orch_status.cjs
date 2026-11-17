const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../frontend/static/app.js'), 'utf8');
function helper(name) {
  const start = source.indexOf('  function ' + name + '(');
  assert(start >= 0, name);
  const end = source.indexOf('\n  function ', start + 1);
  assert(end > start, name);
  return source.slice(start, end);
}
const context = {assert};
vm.createContext(context);
vm.runInContext(['compactExpertStatusReason','expertSnapshotStatusReason'].map(helper).join('\n') + `
const reportReason='progress\\n\\n## report\\n'+'full report '.repeat(80);
assert.equal(expertSnapshotStatusReason({status:'blocked',status_reason:reportReason,stage_cursor:'probe',node_states:{probe:{status:'needs_attention',handoff:{question:'请贴回探测输出。'}}}}),'等待用户输入：请贴回探测输出。');
const compact=compactExpertStatusReason(reportReason);
assert.equal(compact.length,220);
assert.equal(compact.includes('\\n'),false);
assert.equal(compact.endsWith('…'),true);
consoleResult = 'status assertions passed';
`, context);
process.stdout.write(context.consoleResult + '\n');
