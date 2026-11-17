// Retained graph protocol helpers; the old modal renderer is retired.
  const labels = {goal:"总目标",node:"阶段",edge:"依赖",loop:"返修循环",constraint:"约束",decision:"决定",input:"运行参数",question:"待确认",artifact:"成果要求"};
  const nodeKinds = {plan:"规划",implement:"执行",reduce:"汇总",audit:"独立复核",decision:"判断",gate:"签认关口"};
  const stoppedLabels = {failed:"失败，已停止",paused:"已停止",interrupted:"已中断"};
  const actionKeys = ["update", "resume", "discuss", "repair", "save", "rebase_current_canvas", "rebuild"];
  const actionNames = {update:"更新图",resume:"继续构图",discuss:"讨论当前图",repair:"修复当前图",save:"保存阶段图",rebase_current_canvas:"基于当前画布继续整理",rebuild:"重新读取全部历史"};
  const actionFallbackReason = "当前服务器未提供完整动作状态，已暂时收紧此操作；请刷新后重试。";
  async function readGraphResponse(response) {
    let data;
    try { data = await response.json(); } catch (_) {
      throw new Error(response.ok
        ? "阶段图返回格式异常，请重试。"
        : `阶段图服务暂时不可用（HTTP ${response.status}），请重试。`);
    }
    if (!response.ok) throw new Error(typeof data?.error === "string" ? data.error
      : `阶段图操作失败（HTTP ${response.status}），请重试。`);
    if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("阶段图返回格式异常，请重试。");
    return data;
  }
  function canvasStateOf(state) {
    const raw = state?.canvas_state;
    if (raw && typeof raw === "object") {
      const count = Number(raw.node_count);
      return {exists:raw.exists === true, node_count:Number.isFinite(count) ? count : 0,
        status:raw.status === "saved" ? "saved" : "empty"};
    }
    // Legacy graph responses only expose the ability to open the saved graph.
    // A candidate is a working copy, so it must not be mistaken for the saved canvas.
    const exists = !!state?.can_open_canvas && !state?.candidate;
    return {exists, node_count:exists ? (state?.view?.nodes?.length || 0) : 0,
      status:exists ? "saved" : "empty"};
  }
  function candidateIsStale(state) {
    const candidate = state?.candidate;
    if (!candidate) return false;
    if (candidate.stale === true || candidate.is_stale === true || state?.candidate_stale === true ||
        state?.stale_candidate === true || candidate.status === "stale") return true;
    return !!(candidate.base_graph_hash && state?.base_graph_hash &&
      candidate.base_graph_hash !== state.base_graph_hash);
  }
  function computeStatus(state) {
    const check=state?.candidate?.readiness || state?.readiness;
    const diagnostics=check?.diagnostics || [];
    const blocking=check?.ready===false || !!state?.issues?.length;
    const unfinished=state?.candidate && state.candidate.status !== "ready";
    const terminal=state?.task?.status || state?.candidate?.status;
    const taskName=state?.task?.kind === "local_repair" ? "修复" : "构图";
    const savedCanvas = canvasStateOf(state).exists;
    const reviewStale = !!(state?.delivery?.review_stale || state?.candidate?.autobuild?.review_stale);
    const phase=state?.connection_lost ? "unknown" : state?.running ? "running" : stoppedLabels[terminal] ? terminal : unfinished ? "pending" : blocking ? "needs_review" : state?.can_preview ? "ready" : "pending";
    const label=state?.connection_lost ? "状态连接中断，无法确认是否仍在执行" :
      !state?.running && reviewStale ? (savedCanvas ? "当前图已保存，需校验" : "修改后尚未自审，可保存计划") :
      !state?.running && savedCanvas && stoppedLabels[terminal] ? "当前图已保存；历史整理"+stoppedLabels[terminal] :
      !state?.running && stoppedLabels[terminal] ? taskName+stoppedLabels[terminal] :
      state?.running ? (taskName === "修复" ? "AI 正在完善修订" : "正在构图（生成中）") :
      state?.process?.outcome === "waiting_adoption" ? "修订已通过校验，等待采用" :
      unfinished ? "草图已保留，可继续构图" : blocking ? "执行结构仍需由 AI 完善，待修复" :
      savedCanvas && !state?.candidate ? "阶段图已保存" : state?.can_preview ? "图已就绪，可保存" : "待继续生成";
    return {phase,label,blocking,ready:!!state?.can_preview&&!blocking&&!state?.running,codes:diagnostics.map(d=>d.code)};
  }
  function groupEdges(edges) {
    const groups=new Map();
    for(const edge of edges){
      const predicate=edge.input_when;
      const key=JSON.stringify([edge.from_node,edge.to_node,edge.condition||"always",
        predicate ? [predicate.input,predicate.equals] : null,edge.branch_group||""]);
      if(groups.has(key))groups.get(key).count++;else groups.set(key,{...edge,count:1});
    }
    return [...groups.values()];
  }
  function businessIssueRows(rows) { return (rows || []).filter(row => row.type === "business"); }
  function choicePayload(rows, selections) {
    return businessIssueRows(rows).flatMap(row => {
      const value = selections.get(row.id);
      const choices = row.prepared_choices || row.options || [];
      return choices.some(option => String(option?.id ?? option?.value ?? "") === String(value)
        || String(option?.value ?? option?.id ?? "") === String(value))
        || value === "__other__" ? [{question_id:row.id, value}] : [];
    });
  }
  function preparedChoicesForRow(row, bundle) {
    const raw = Array.isArray(row?.prepared_choices) ? row.prepared_choices
      : bundle?.questions?.find(question => question?.name === row?.name || question?.name === row?.id)?.choices;
    return Array.isArray(raw) ? raw.filter(choice => choice && (choice.id || choice.value || choice.label)).map((choice, index) => ({
      ...choice, id: String(choice.id ?? choice.value ?? choice.label ?? `choice-${index + 1}`),
      label: String(choice.label ?? choice.value ?? choice.id ?? ""), description: String(choice.description || ""),
      kind: choice.kind === "action" ? "action" : "value", action: String(choice.action || ""),
      requires_input: Boolean(choice.requires_input), input_label: String(choice.input_label || ""),
      evidence: Array.isArray(choice.evidence) ? choice.evidence.map(ref => String(ref)) : [],
      verification: choice.verification === "verified" ? "verified" : "suggested",
    })) : [];
  }
  function choiceNoteLines(rows, notes) {
    return businessIssueRows(rows).flatMap(row => {
      const note = (notes.get(row.id) || "").trim();
      return note ? [(row.title || row.id) + "：" + note] : [];
    });
  }
  function choiceNoteMessage(rows, notes) {
    const lines = choiceNoteLines(rows, notes);
    return lines.length ? "关于待决事项的补充说明：\n" + lines.join("\n") : "";
  }
  function fallbackActions(state) {
    const running = !!state?.running;
    const candidate = state?.candidate;
    const build = state?.delivery;
    const business = businessIssueRows(build?.issue_groups || []);
    const stale = candidateIsStale(state);
    const pending = Number(state?.coverage?.pending || 0) > 0;
    const newSources = Number(state?.coverage?.new_sources || 0) > 0;
    const readyCandidate = candidate?.status === "ready";
    const sourceChanged = !!(state?.sources_changed || state?.history_changed || state?.coverage?.history_changed);
    const sourceConsistent = !sourceChanged && !state?.warnings?.length;
    const retry = !!(candidate && build?.phase === "done" && build.status !== "ready");
    const saveReady = computeStatus(state).ready;
    const savedCanvas = canvasStateOf(state).exists;
    const rebuildable = !running && !!state?.coverage?.total && (!!state?.revision || !!candidate || sourceChanged);
    const blocked = reason => ({enabled:false, reason});
    const allowed = (enabled, reason) => enabled ? {enabled:true, reason:""} : blocked(reason);
    const busyReason = "当前仍在处理中，请等待完成或先停止。";
    return {
      update: allowed(!running && !stale && sourceConsistent && (!!state?.coverage?.total) &&
        (candidate ? (pending || newSources || !state.revision) : (!state.revision || pending || newSources)),
        running ? busyReason : stale ? "候选整理基于旧画布，请改为基于当前画布继续整理。" :
          "没有可更新的新增或待处理来源。"),
      resume: allowed(!running && sourceConsistent && !!candidate && (candidate.status !== "ready" ||
        (build && build.phase !== "done") || retry),
        running ? busyReason : "当前没有可继续的候选构图。"),
      // A ready, baseline-consistent candidate may still accept free-form
      // discussion. The automatic primary button only routes classified
      // business rows to the choice table below.
      discuss: allowed(!running && readyCandidate && !!build && sourceConsistent,
        running ? busyReason : !candidate ? "尚无候选图可供讨论。" : !readyCandidate ?
          "候选图尚未就绪，暂不能发送讨论。" : !business.length ? "当前没有待决定的业务事项。" :
          !sourceConsistent ? "原始来源已变化，请先刷新整理状态。" : "当前构建暂不接受讨论。"),
      repair: allowed(!running && sourceConsistent &&
        (readyCandidate && !pending || !candidate && savedCanvas && !!state?.revision),
        running ? busyReason : !sourceConsistent ? "原始来源已变化，请先刷新整理状态。" :
          candidate && !readyCandidate ? "候选图尚未就绪，暂不能修复。" :
          candidate && pending ? "仍有来源待处理，完成整理后再修复。" : "当前没有可修复的已保存画布。"),
      save: allowed(saveReady, running ? busyReason : "当前图尚未通过校验或没有可保存的预览。"),
      rebase_current_canvas: allowed(!running && sourceConsistent && stale,
        running ? busyReason : "候选整理并未检测到基于旧画布的结果。"),
      rebuild: allowed(rebuildable,
        running ? busyReason : "当前没有可重新读取的完整历史。"),
    };
  }
  function normalizedActions(state) {
    const supplied = state?.actions;
    if (!supplied || typeof supplied !== "object") return fallbackActions(state);
    const complete = actionKeys.every(key => supplied[key] && typeof supplied[key].enabled === "boolean");
    if (!complete) {
      const fallback = fallbackActions(state);
      return Object.fromEntries(actionKeys.map(key => supplied[key] && typeof supplied[key].enabled === "boolean" ?
        [key, {enabled:!!supplied[key].enabled, reason:String(supplied[key].reason || "")} ] :
        [key, {...fallback[key], reason:actionFallbackReason}]));
    }
    return Object.fromEntries(actionKeys.map(key => [key, {
      ...supplied[key], enabled:!!supplied[key].enabled, reason:String(supplied[key].reason || "")
    }]));
  }
  function actionState(state, key) {
    if (!actionKeys.includes(key)) return {enabled:false, reason:"未知的构图动作。"};
    if (state?._actions_stale) return {enabled:false, reason:"正在刷新当前动作状态，请稍候。", stale:true};
    return normalizedActions(state)[key] || {enabled:false, reason:actionFallbackReason};
  }
  function selectionState(state, selection) {
    const texts = Array.isArray(selection) ? selection : selection?.texts;
    if (!Array.isArray(texts) || !texts.some(text => String(text || "").trim()))
      return {enabled:false, reason:"没有可纳入的所选消息。"};
    if (!state) return {enabled:false, reason:"正在读取当前图状态，请稍候。"};
    if (state.running) return {enabled:false, reason:"当前仍在处理中，请等待完成或先停止。"};
    if (state.connection_lost) return {enabled:false, reason:"状态连接中断，请重新连接后再纳入所选消息。"};
    if (state._actions_stale) return {enabled:false, reason:"正在刷新当前动作状态，请稍候。"};
    if (!Number(state.coverage?.total)) return {enabled:false, reason:"尚无可核对范围的原始历史。"};
    if (state.sources_changed || state.history_changed || state.coverage?.history_changed || state.warnings?.length)
      return {enabled:false, reason:"原始来源已变化，请刷新后重新选择消息。"};
    if (candidateIsStale(state)) return {enabled:false, reason:"当前候选基线已变化，请刷新后重新选择消息。"};
    return {enabled:true, reason:""};
  }
  function continuationAction(state) {
    const update = actionState(state, "update"), resume = actionState(state, "resume");
    return update.enabled ? {key:"update", policy:update} : resume.enabled ? {key:"resume", policy:resume} :
      {key:"update", policy:update};
  }
  function actionContract(state) { return normalizedActions(state); }
  function actionOf(state) {
    if (!state) return {label:"正在读取…", disabled:true};
    if (state.running) return {label:"生成中…", disabled:true};
    const rebase = actionState(state, "rebase_current_canvas");
    if (candidateIsStale(state) || rebase.enabled)
      return {label:actionNames.rebase_current_canvas, key:"rebase_current_canvas", disabled:!rebase.enabled, reason:rebase.reason};
    if (state.basis?.needs_update) {
      const next = continuationAction(state);
      return {label:next.key === "resume" ? "继续生成" : "按取舍更新图", key:next.key, disabled:!next.policy.enabled, reason:next.policy.reason};
    }
    if (state.coverage?.new_sources) {
      const next = continuationAction(state);
      return {label:next.key === "resume" ? "继续生成" : "从新增对话更新图", key:next.key, disabled:!next.policy.enabled, reason:next.policy.reason};
    }
    if (state.candidate) {
      const done = state.candidate.status === "ready" && !state.coverage.pending;
      const policy = actionState(state, done ? "update" : "resume");
      return {label:done ? "生成完成" : "继续生成", key:done ? "update" : "resume", disabled:done || !policy.enabled, reason:done ? "当前候选图已完成整理。" : policy.reason};
    }
    const policy = actionState(state, "update");
    return {label:state.revision ? "从新增对话更新图" : "生成阶段图", key:"update", disabled:!policy.enabled, reason:policy.reason};
  }
  function autoActionOf(state) {
    if (!state) return {label:"正在读取…", disabled:true};
    if (state.running) return {label:"自动构图中…", disabled:true};
    const rebase = actionState(state, "rebase_current_canvas");
    if (candidateIsStale(state) || rebase.enabled)
      return {label:actionNames.rebase_current_canvas, key:"rebase_current_canvas", disabled:!rebase.enabled, reason:rebase.reason};
    if (state.basis?.needs_update) {
      const next = continuationAction(state);
      return {label:next.key === "resume" ? "继续自动构图" : "按取舍更新图", key:next.key, disabled:!next.policy.enabled, reason:next.policy.reason};
    }
    if (state.coverage?.new_sources) {
      const next = continuationAction(state);
      return {label:next.key === "resume" ? "继续自动构图" : "从新增对话自动更新", key:next.key, disabled:!next.policy.enabled, reason:next.policy.reason};
    }
    const build = state.delivery;
    const business = businessIssueRows(build?.issue_groups || []);
    const discuss = actionState(state, "discuss");
    if (business.length && discuss.enabled)
      return {label:"查看待确认选择", key:"discuss", disabled:false, discuss:true, reason:discuss.reason};
    if (state.candidate && build?.phase !== "done") {
      const next = continuationAction(state);
      return {label:"继续自动构图", key:next.key, disabled:!next.policy.enabled, reason:next.policy.reason};
    }
    if (state.candidate && build?.phase === "done" && !state.coverage.pending)
      if (build.status === "ready" && !computeStatus(state).blocking)
        return {label:"构建已交付", key:"save", disabled:true, reason:"当前构建已交付。"};
      else {
        const policy = actionState(state, "resume");
        return {label:"重新补全并自审", key:"resume", disabled:!policy.enabled, retry:true, reason:policy.reason};
      }
    const policy = actionState(state, "update");
    return {label:state.revision ? "从新增对话自动更新" : "自动构图", key:"update", disabled:!policy.enabled, reason:policy.reason};
  }
  const phaseLabels = {extracting:"整理有效对话",preparing:"整理有效对话",model:"整理有效对话",applying:"整理有效对话",repair:"修复当前图",autofix:"补全与自修复",review:"独立自审",gap_fill:"缺口回补",final_review:"复审",done:"交付"};
  const modelStageLabels = {preparing:"准备请求",checkpointing:"保存进度",requesting:"等待响应",queued:"本地排队",thinking:"推理中",reasoning:"推理中",streaming:"输出提案",received:"接收完成",parsing:"解析提案",validating:"校验并保存",stalled:"等待超时",time_limit:"请求总时限",tooling:"接收工具参数"};
  const reasonLabels = {rounds_exhausted:"已完成三轮结构返修，仍有问题需要查看",review_error:"独立自审未完成，请查看审查结果",coverage_gaps_remaining:"一次缺口回补后仍有遗漏",structural_not_ready:"图结构尚未就绪",model_error:"模型请求失败",parse_failed:"补全提案格式无效，未知项分类未完成",request_too_large:"图与要求超过补全输入上限",manual_edits_after_review:"图已修改；此前自审结论供参考，修改后尚未自审",blocking_goal:"总目标尚未确定，请查看待确认清单",pending_questions:"仍有事项需要查看，请核对待确认清单"};
  const reasonText = reason => reasonLabels[reason] || reason;
  phaseLabels.waiting_user = "讨论与决定"; phaseLabels.discussion = "讨论当前图";
  reasonLabels.business_decision = "已保存当前图，等待讨论业务取舍";
  reasonLabels.discussion = "讨论保留在当前构建中";
  reasonLabels.decision_adopted = "已采用决定，正在从当前图续建并重新审查";
  reasonLabels.conflict_requires_review = "剩余冲突涉及用户要求，需要核对后修订；已停止重复请求";
  reasonLabels.model_stopped = "AI 已保存当前进度并主动收束，可继续完善";
  reasonLabels.invalid_control = "AI 暂未给出明确的新方案，当前进度已保留";
  reasonLabels.repeated_control = "AI 尚未找到不同的修复方向，当前进度已保留";
  reasonLabels.history_basis_changed = "构图依据已修改，按取舍更新后重新审查";
  const adoptionText = reason => ({improved:"诊断减少",no_regression:"未引入新错误",no_improvement:"诊断未减少",new_errors:"引入新错误",nothing_applied:"提案未通过规则",not_proposed:"无结构提案",parse_failed:"提案格式无效",guard_failed:"锁定内容校验失败",model_error:"模型请求失败"}[reason] || reason);
  function listText(value) { return (value || []).join("\n"); }
  function lines(value) { return value.split(/\r?\n/).map(s => s.trim()).filter(Boolean); }

export {actionOf, autoActionOf, actionState, actionContract, canvasStateOf, candidateIsStale, selectionState, lines, computeStatus, groupEdges, businessIssueRows, choicePayload, choiceNoteLines, choiceNoteMessage, readGraphResponse};
