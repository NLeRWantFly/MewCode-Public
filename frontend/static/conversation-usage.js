/* Per-conversation receipts; no token estimation from rendered messages. */
(() => {
  "use strict";
  const panel = document.getElementById("conversation-usage");
  const toggle = document.getElementById("conversation-usage-toggle");
  if (!panel || !toggle) return;
  const content = document.getElementById("conversation-usage-content");
  const note = document.getElementById("conversation-usage-note");
  const info = document.getElementById("conversation-usage-info");
  const tooltip = document.getElementById("conversation-usage-tooltip");
  const rows = new Map(), details = new WeakMap();
  const colors = ["#aa92d4", "#75b9a7", "#dfa77f", "#8daedb", "#d991ae", "#a7b978"];
  let conversationId = "", generation = 0, inFlight = null, refreshQueued = false, activeTip = null, hideTimer = null;
  const count = value => Number.isFinite(Number(value)) ? Math.max(0, Number(value)) : 0;
  const number = value => count(value).toLocaleString("zh-CN");
  function text(tag, value, cls) {
    const node = document.createElement(tag);
    node.textContent = value;
    if (cls) node.className = cls;
    return node;
  }
  function visible(open) {
    panel.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    if (open) refresh();
    else { cancelRefresh(); hideDetails(); }
  }
  function cancelRefresh() {
    ++generation;
    refreshQueued = false;
    if (inFlight) {
      clearTimeout(inFlight.timeout);
      inFlight.controller.abort();
      inFlight = null;
    }
  }
  function hideDetails() {
    clearTimeout(hideTimer);
    tooltip.hidden = true;
    activeTip = null;
  }
  function positionDetails() {
    if (!activeTip || tooltip.hidden) return;
    const anchor = activeTip.getBoundingClientRect(), box = tooltip.getBoundingClientRect();
    const width = document.documentElement.clientWidth, height = document.documentElement.clientHeight;
    tooltip.style.left = Math.max(8, Math.min(anchor.right - box.width, width - box.width - 8)) + "px";
    const below = anchor.bottom + 8;
    tooltip.style.top = Math.max(8, Math.min(below + box.height <= height - 8 ? below : anchor.top - box.height - 8, height - box.height - 8)) + "px";
  }
  function showDetails(anchor) {
    const data = details.get(anchor);
    if (!data || panel.hidden) return;
    clearTimeout(hideTimer);
    activeTip = anchor;
    tooltip.replaceChildren(text("strong", data.title), text("div", data.value, "conversation-usage-tip-value"), text("p", data.caption));
    if (data.metrics) {
      const metrics = text("dl", "");
      for (const [label, value] of data.metrics) metrics.append(text("dt", label), text("dd", value));
      tooltip.append(metrics);
    }
    tooltip.hidden = false;
    positionDetails();
  }
  function attachDetails(anchor) {
    anchor.setAttribute("aria-describedby", tooltip.id);
    anchor.addEventListener("mouseenter", () => showDetails(anchor));
    anchor.addEventListener("mouseleave", () => {
      if (document.activeElement !== anchor) hideTimer = setTimeout(hideDetails, 120);
    });
    anchor.addEventListener("focus", () => showDetails(anchor));
    anchor.addEventListener("blur", hideDetails);
    // Tap opens the same details on touch screens; outside tap / Escape closes.
    anchor.addEventListener("click", () => showDetails(anchor));
  }
  function clearRows(message) {
    hideDetails(); rows.clear(); details.delete(info);
    content.replaceChildren(text("p", message, "conversation-usage-empty"));
  }
  function render(data) {
    const total = count(data.tokens.total_tokens), present = new Set();
    if (!data.models.length) clearRows("还没有模型足迹，聊几句再来看看。");
    else if (!rows.size) content.replaceChildren();
    data.models.forEach((model, index) => {
      const key = JSON.stringify([model.provider, model.model]);
      present.add(key);
      let row = rows.get(key);
      if (!row) {
        const item = text("section", "", "conversation-usage-model");
        const label = text("div", model.model, "conversation-usage-label");
        const meter = text("button", "", "conversation-usage-meter");
        meter.type = "button";
        const track = text("span", "", "conversation-usage-track");
        const fill = text("span", "", "conversation-usage-fill");
        track.setAttribute("role", "progressbar");
        track.setAttribute("aria-valuemin", "0");
        track.setAttribute("aria-label", model.model + " 的对话 token 占比");
        track.append(fill); meter.append(track); item.append(label, meter);
        let hash = 0;
        for (const char of key) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
        item.style.setProperty("--usage-color", colors[hash % colors.length]);
        attachDetails(meter);
        row = { item, meter, track, fill }; rows.set(key, row);
      }
      const used = count(model.tokens.total_tokens), ratio = total ? Math.min(100, used / total * 100) : 0;
      const value = number(used) + " / " + number(total) + " tokens";
      row.fill.style.width = ratio + "%";
      row.track.setAttribute("aria-valuemax", String(total || 1));
      row.track.setAttribute("aria-valuenow", String(Math.min(used, total)));
      row.track.setAttribute("aria-valuetext", value + (total ? "，占比 " + ratio.toFixed(1) + "%" : "，暂无已报告的 token"));
      row.meter.setAttribute("aria-label", model.model + "：查看用量明细");
      details.set(row.meter, {
        title: model.model, value,
        caption: "该模型用量 / 本对话总量" + (total ? " · " + ratio.toFixed(1) + "%" : " · 暂无已报告的 token"),
        metrics: [["提供方", model.provider], ["调用", number(model.request_count) + " 次"],
          ["输入 / 输出", number(model.tokens.input_total) + " / " + number(model.tokens.output_tokens)],
          ["缓存（输入内）", number(model.tokens.cached_input_tokens)], ["推理（输出内）", number(model.tokens.reasoning_tokens)]]
      });
      // Retain the focused/hovered node across polling; do not rebuild every 5s.
      if (content.children[index] !== row.item) content.insertBefore(row.item, content.children[index] || null);
    });
    for (const [key, row] of rows) {
      if (!present.has(key)) { if (activeTip === row.meter) hideDetails(); row.item.remove(); rows.delete(key); }
    }
    note.textContent = "进度条表示本对话累计 token 占比，不是上下文容量或额度。包含主模型、子 Agent、审查与 Jev 的已归属请求；缓存与推理不重复计数。" +
      (data.unreported_token_requests ? " " + number(data.unreported_token_requests) + " 次请求未报告非零 token，用量可能不完整。" : "") +
      " 每次请求收到用量回执后更新；未收到回执的失败/中断请求及无法归属的旧记录不补算。" +
      (data.partial ? " 部分账本暂时不可读，当前用量不完整，将自动重试。" : "");
    details.set(info, {title: "这次对话的小账本", value: number(data.request_count) + " 次调用 · " + number(total) + " tokens", caption: note.textContent});
    info.dataset.warning = String(!!data.unreported_token_requests || !!data.partial);
    if (activeTip) showDetails(activeTip);
  }
  async function refresh() {
    if (panel.hidden || document.hidden || !conversationId) return;
    // Coalesce polling and stream events. Never abort a slow request to start
    // an identical one: a response slower than the poll interval must still land.
    if (inFlight) { refreshQueued = true; return; }
    const id = conversationId, version = ++generation;
    const request = { controller: new AbortController(), timeout: null, timedOut: false };
    inFlight = request;
    request.timeout = setTimeout(() => {
      request.timedOut = true;
      request.controller.abort();
    }, 15000);
    try {
      const response = await fetch("/api/chat/usage?conversation_id=" + encodeURIComponent(id), {
        signal: request.controller.signal, cache: "no-store"
      });
      if (!response.ok) throw new Error("统计暂时不可用");
      const data = await response.json();
      if (version !== generation || id !== conversationId || data.conversation_id !== id) return;
      render(data);
    } catch (error) {
      if ((error.name !== "AbortError" || request.timedOut) && version === generation) {
        clearRows("暂时读不到用量，点 ↻ 重试一下。");
        note.textContent = "统计暂时不可用，请刷新重试。";
        details.set(info, {title: "暂时离线", value: "统计未更新", caption: note.textContent});
        info.dataset.warning = "true";
      }
    } finally {
      clearTimeout(request.timeout);
      if (inFlight === request) {
        inFlight = null;
        if (refreshQueued) { refreshQueued = false; refresh(); }
      }
    }
  }
  attachDetails(info);
  tooltip.addEventListener("mouseenter", () => clearTimeout(hideTimer));
  tooltip.addEventListener("mouseleave", hideDetails);
  document.addEventListener("pointerdown", event => {
    if (activeTip && !activeTip.contains(event.target) && !tooltip.contains(event.target)) hideDetails();
  });
  document.addEventListener("keydown", event => { if (event.key === "Escape") hideDetails(); });
  window.addEventListener("resize", hideDetails);
  panel.addEventListener("scroll", hideDetails);
  toggle.addEventListener("click", () => visible(panel.hidden));
  document.getElementById("conversation-usage-close").addEventListener("click", () => visible(false));
  document.getElementById("conversation-usage-refresh").addEventListener("click", refresh);
  window.addEventListener("mewcode:conversation-context", event => {
    const id = event.detail.readonly ? "" : String(event.detail.conversationId || "");
    if (id !== conversationId) {
      conversationId = id; cancelRefresh();
      clearRows(id ? "正在整理模型足迹…" : "请选择一个对话。");
      info.dataset.warning = "false";
      note.textContent = "";
    }
    refresh();
  });
  function refreshConversation(event) {
    if (String(event.detail?.conversationId || "") === conversationId) refresh();
  }
  window.addEventListener("mewcode:conversation-updated", refreshConversation);
  window.addEventListener("mewcode:usage-updated", refreshConversation);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) cancelRefresh();
    else refresh();
  });
  setInterval(refresh, 5000);
})();
