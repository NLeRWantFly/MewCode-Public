const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
const source = fs.readFileSync(require('node:path').join(__dirname, '../frontend/static/app.js'), 'utf8').replace(/\r\n/g, '\n');
const start = source.indexOf('  function toolTimingLabel(');
const context = vm.createContext({});
vm.runInContext(source.slice(start, source.indexOf('\n  }', start) + 4), context);
test('measured execution and dispatch remain separate, including zero execution', () => {
  assert.equal(context.toolTimingLabel({elapsed: 0, dispatch_seconds: 27.2}), '执行 0s · 调度 27.2s');
  assert.equal(context.toolTimingLabel({elapsed: 0.01, dispatch_seconds: 0}), '执行 0.01s · 调度 0s');
});
test('old receipts keep their original timing without inventing dispatch', () => {
  assert.equal(context.toolTimingLabel({elapsed: 27.2}), '27.2s');
  assert.equal(context.toolTimingLabel({}), '完成');
});
