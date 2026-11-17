const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../frontend/static/app.js'), 'utf8');
const start = source.indexOf('  function updateUsage(u) {');
const end = source.indexOf('  // ---------- 输入区弹层', start);
assert.ok(start >= 0 && end > start, 'could not locate updateUsage');

function applyUsage(status, usage) {
  const context = {
    currentStatus: status,
    cacheUsage: () => ({rate: null}),
    updateContextBadge: () => {},
    updateStatusLine: () => {},
  };
  vm.runInNewContext(`${source.slice(start, end)}\nthis.updateUsage = updateUsage;`, context);
  context.updateUsage(usage);
  return context.currentStatus;
}

test('late usage event cannot replace the detected auto-window denominator', () => {
  const status = applyUsage({window: 1_050_000}, {
    ctx: 766_537,
    window: 105_000,
    total_in: 766_537,
    total_out: 0,
    cache_usage_available: false,
  });

  assert.equal(status.window, 1_050_000);
  assert.equal(status.ctx, 766_537);
});

test('late usage event cannot replace a status-derived manual window', () => {
  const status = applyUsage({window: 1_000_000, context_tier: 1_000_000}, {
    ctx: 140_000,
    window: 105_000,
    total_in: 140_000,
    total_out: 0,
    cache_usage_available: false,
  });

  assert.equal(status.window, 1_000_000);
  assert.equal(status.context_tier, 1_000_000);
});
