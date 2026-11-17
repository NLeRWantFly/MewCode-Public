/* Read-only approval diagnostics. Missing evidence is never displayed as zero. */
(function (root) {
  "use strict";
  const number = (value) => typeof value === "number" && Number.isFinite(value) && value >= 0
    ? String(Math.round(value * 100) / 100) : "未知";
  function format(data) {
    if (!data || data.unavailable) return "本次审批统计尚不可用";
    const review = data.model_reviews || {};
    const wait = data.waiting_for_user_ms || {};
    const quality = data.timing_quality;
    const noLatency = quality && data.count === quality.latency_invalid_count + quality.latency_unknown_count;
    const latency = noLatency ? {} : (data.latency_ms || {});
    const lines = [
      `审批记录 ${number(data.count)} · 询问 ${number(data.ask_count)} · 审查记录 ${number(review.count)} · 审查放行率 ${number(review.allow_rate_percent)}${review.allow_rate_percent == null ? "" : "%"}`,
      `明确关联的人工覆盖拒绝 ${number(data.manual_deny_override_count)} · 关联证据不足 ${number(data.manual_deny_override_unknown_count)} · 边界拒绝 ${number(data.boundary_denial_count)} · 越界申请 ${number(data.out_of_scope_request_count)}`,
      `缓存命中 ${number(data.cache_hits)} · 审批 P50/P95 ${number(latency.p50)}/${number(latency.p95)} ms · 等待用户 P50/P95 ${number(wait.p50)}/${number(wait.p95)} ms · 累计等待 ${number(wait.total)} ms`,
      "统计针对当前会话的有限审计样本；审批记录不等于工具已执行，也不代替安全验收。",
    ];
    if (data.truncation?.truncated === true) {
      lines.push("样本有裁剪或不可解析记录，请结合原始审计核验；不能当作全量结果。");
    }
    return lines.join("\n");
  }
  root.MewApprovalMetrics = { format };
  if (typeof module !== "undefined" && module.exports) module.exports = { format };
})(typeof window !== "undefined" ? window : globalThis);
