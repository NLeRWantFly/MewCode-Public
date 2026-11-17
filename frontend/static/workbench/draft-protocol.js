// Pure source/progress helpers retained for protocol compatibility.
  const names = {goal:"总目标",node:"流程节点",edge:"执行依赖",loop:"返修循环",constraint:"约束",decision:"裁决",question:"待确认",artifact:"成果要求",progress:"历史进度"};
  const evidenceNames = {user_source:"关联用户原话 · 归纳待核对",reported:"历史汇报 · 非验收",proposal:"助手建议 · 非作者确认",saved_graph:"已有流程定义"};
  function coverageText(data) {
    const c = data.coverage || {};
    return (c.mode === "selected" ? "本次选中 · " : "") + "已整理 " + (c.processed || 0) + " / " + (c.total || 0) + " 个来源片段 · 待整理 " + (c.pending || 0) +
      (c.history_changed ? " · 历史已变化，需重新扫描" : "");
  }
  function formatDuration(ms) {
    const total = Math.max(0, Math.round((ms || 0) / 1000));
    const h = Math.floor(total / 3600), m = Math.floor(total % 3600 / 60), s = total % 60;
    const mm = String(m).padStart(2, "0"), ss = String(s).padStart(2, "0");
    return h ? h + ":" + mm + ":" + ss : m + ":" + ss;
  }
  function etaText(log, remainingChunks) {
    const done = (log || []).filter(e => e.status === "ok" && typeof e.duration_ms === "number");
    if (!done.length || !(remainingChunks > 0)) return "";
    const avg = done.reduce((sum, e) => sum + e.duration_ms, 0) / done.length;
    const ms = avg * remainingChunks;
    if (ms < 60000) return "预计还需不到 1 分钟";
    return "预计还需约 " + Math.max(1, Math.round(ms / 60000)) + " 分钟";
  }
  function withLogicalChunks(log) {
    let chunk = 1;
    return (log || []).map(entry => {
      const item = {...entry, chunk};
      if (entry.status === "ok") chunk += 1;
      return item;
    });
  }
  function failedAttemptText(entry, running) {
    return "✗ 块 " + entry.chunk + (running ? " · 历史失败（当前正在继续）：" : " · 失败：") + (entry.error || "");
  }
  // Single source of truth for the step indicator and the one prominent action.
  function stepOf(state, preview) {
    const primary = (label, action, enabled, reason) => ({label, action, enabled, reason});
    if (!state) return {step: 1, primary: primary("开始整理", "none", false, "正在读取原始历史…")};
    const c = state.coverage || {}, candidate = state.candidate || null;
    const pending = c.pending || 0, processed = c.processed || 0, total = c.total || 0;
    const revision = state.revision || 0, est = c.chunks_estimate || 0;
    const warnings = state.warnings || [];
    const doc = candidate ? candidate.document : state.document;
    const conflicts = (doc.entries || []).filter(e => e.state === "conflict").length;
    if (state.running) {
      const p = state.progress;
      return {step: 2, primary: primary("整理中…", "none", false,
        (p ? "第 " + p.chunk + " 块进行中" : "整理进行中") + "，可随时「停止整理」；关闭面板或切换对话不会中断")};
    }
    if (candidate && pending > 0) return {step: 2, primary: primary(processed > 0 ? "继续整理" : "开始整理", "resume", true,
      "还有 " + pending + " 个片段（约 " + est + " 块）未整理" + (candidate.error ? "；上次：" + candidate.error : ""))};
    if (!candidate && revision === 0) return {step: total > 0 ? 2 : 1, primary: primary("开始整理", "start",
      total > 0 && !c.history_changed, total > 0 ? "从完整原始历史开始，约 " + est + " 块，每块调用一次模型"
        : (warnings[0] || "尚无可整理的原始对话"))};
    if (candidate && pending === 0) {
      if (conflicts > 0) return {step: 3, primary: primary("确认底稿", "none", false,
        "有 " + conflicts + " 项冲突待处理：点开条目选择「保留旧条款」或保存新内容")};
      if (warnings.length) return {step: 3, primary: primary("确认底稿", "none", false, warnings[0])};
      if (candidate.status !== "ready") return {step: 3, primary: primary("继续整理", "resume", true,
        candidate.error || "上次未完成")};
      return {step: 3, primary: primary("确认底稿", "confirm", true,
        "确认的是流程要求，不是任务完成；确认后才能生成流程图")};
    }
    if (preview && !preview.applied_hash) return {step: 4, primary: primary("保存到画布", "apply", true,
      "保存为当前对话的新流程版本；不会启动执行")};
    if (preview && preview.applied_hash) return {step: 4, primary: primary("生成流程差异", "graph", true,
      "已保存到画布。有新对话后可「整理新增记录」再生成")};
    return {step: 4, primary: primary("生成流程差异", "graph", true,
      pending > 0 ? "有 " + pending + " 个新片段未整理，可先「整理新增记录」，或直接按 v" + revision + " 生成"
        : "按已确认的底稿 v" + revision + " 确定性生成，不调用模型")};
  }
  function sameScope(a, b) {
    return !!a && !!b && a.conversation_id === b.conversation_id &&
      String(a.project).replace(/\\/g, "/").toLowerCase() === String(b.project).replace(/\\/g, "/").toLowerCase();
  }
  function layout(spec) {
    const nodes = spec.nodes || [], rank = {}, degrees = {}, outgoing = {};
    nodes.forEach(n => { rank[n.node_id] = 0; degrees[n.node_id] = 0; outgoing[n.node_id] = []; });
    (spec.edges || []).filter(e => e.condition !== "loop").forEach(e => {
      if (outgoing[e.from_node] && Object.hasOwn(degrees, e.to_node)) { outgoing[e.from_node].push(e.to_node); degrees[e.to_node]++; }
    });
    const queue = nodes.filter(n => !degrees[n.node_id]).map(n => n.node_id);
    for (let i = 0; i < queue.length; i++) {
      outgoing[queue[i]].forEach(id => {
        rank[id] = Math.max(rank[id], rank[queue[i]] + 1);
        if (--degrees[id] === 0) queue.push(id);
      });
    }
    const columns = {}, result = {};
    nodes.forEach(n => {
      const row = rank[n.node_id], col = columns[row] || 0;
      result[n.node_id] = {x: 36 + col * 250, y: 32 + row * 110}; columns[row] = col + 1;
    });
    return {positions: result, width: Math.max(600, ...Object.values(columns).map(n => n * 250 + 72)),
      height: Math.max(180, ...Object.values(rank).map(n => (n + 1) * 110 + 40))};
  }

export {coverageText, sameScope, layout, formatDuration, etaText, stepOf, withLogicalChunks, failedAttemptText};
