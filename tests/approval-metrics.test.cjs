const { test } = require('node:test');
const assert = require('node:assert/strict');
const { format } = require('../frontend/static/approval-metrics.js');

test('missing evidence is unknown, not a perfect pass rate', () => {
  const text = format({ count: 0, model_reviews: { count: 0, allow_rate_percent: null } });
  assert.match(text, /审批记录 0/);
  assert.match(text, /审查放行率 未知/);
  assert.doesNotMatch(text, /100%|累计等待 0/);
});
test('finite evidence and bounded sample caveat', () => {
  const text = format({ count: 7, ask_count: 2, model_reviews: { count: 4, allow_rate_percent: 75 },
    manual_deny_override_count: 1, boundary_denial_count: 2, out_of_scope_request_count: 3,
    waiting_for_user_ms: { p50: 0, p95: 20, total: 20 }, truncation: { truncated: true } });
  assert.match(text, /审查放行率 75%/);
  assert.match(text, /等待用户 P50\/P95 0\/20 ms/);
  assert.match(text, /样本有裁剪/);
  assert.doesNotMatch(format({ truncation: { truncated: false, sample_limit: 10000 } }), /样本有裁剪/);
});
test('invalid numbers and unavailable responses never masquerade as evidence', () => {
  assert.match(format({ ask_count: -1, count: Infinity }), /审批记录 未知 · 询问 未知/);
  assert.equal(format({ unavailable: true }), '本次审批统计尚不可用');
  assert.match(format({ count: 2, latency_ms: { p50: 0, p95: 0 },
    timing_quality: { latency_invalid_count: 1, latency_unknown_count: 1 } }), /审批 P50\/P95 未知\/未知/);
});
