/* MewCode Web 客户端逻辑 */
(() => {
  "use strict";

  const isTauriMobile = window.mewcodeDesktop?.runtime === "tauri-mobile"
    || ((window.__TAURI_INTERNALS__ || window.__TAURI__) && /Android/i.test(navigator.userAgent));
  if (isTauriMobile) {
    const startMobile = () => {
      if (typeof window.startMewcodeMobileRuntime === "function") {
        return !!window.startMewcodeMobileRuntime();
      }
      return false;
    };
    if (!startMobile()) {
      document.body.classList.add("mobile-runtime");
      let tries = 0;
      const timer = setInterval(() => {
        tries += 1;
        if (startMobile() || tries > 80) clearInterval(timer);
      }, 50);
    }
    return;
  }

  function initMobileRuntime() {
    if (typeof window.startMewcodeMobileRuntime === "function") {
      window.startMewcodeMobileRuntime();
    }
  }
  // Keep a few mobile collab markers in the desktop bundle so tests can see
  // that phone/computer handoff is wired without duplicating the Android UI.
  const HISTORY_SOURCE_ORDER = ["mewcode"];
  const MOBILE_COLLAB_MARKERS = {
    speech_only: "speech_only=true",
    composer: "mobile-composer",
    focus: "remote_conversation_focus",
    connectManualHost: true,
  };
  void HISTORY_SOURCE_ORDER;
  void MOBILE_COLLAB_MARKERS;
  const $ = (id) => document.getElementById(id);
  const messagesEl = $("messages");
  const inputEl = $("chat-input");
  const sendBtn = $("btn-send");
  const stopBtn = $("btn-stop");
  const attachBtn = $("attach-btn");
  const ctxBadge = $("ctx-badge");
  const runtimeCapsule = $("runtime-capsule");
  const runtimeCapsuleLabel = $("runtime-capsule-label");
  const sessionBadge = $("session-badge");
  const cacheBadge = $("cache-badge");
  const permBar = $("permission-bar");
  const pairingBar = $("pairing-bar");
  const humanOnCallPanel = $("chat-human-oncall");
  const drawer = $("drawer");
  const popover = $("popover");
  const attachmentTray = $("attachment-tray");
  const attachmentInput = $("attachment-input");
  const settingsOverlay = $("settings-overlay");
  const settingsDialogBody = $("settings-dialog-body");
  const settingsDialogFooter = $("settings-dialog-footer");
  const trajectoryViewEl = $("trajectory-view");
  const pipelineItemsEl = $("pipeline-items");
  const trajectoryOverviewTrackEl = $("trajectory-overview-track");
  const trajectoryDetailsEl = $("trajectory-details");
  const trajectoryDetailContentEl = $("trajectory-detail-content");
  const trajectorySearchEl = $("trajectory-search");

  const cordis = window.MewCodeCordis;
  cordis.define("ui.layout", (ctx) => {
    ctx.declare("ui.conversation.views", "keyed");
    ctx.declare("ui.menus", "list");
    ctx.declare("ui.settings", "list");
  }, { purpose: "Conversation workspace layout slots" });
  cordis.define("ui.conversation", (ctx) => {
    ctx.register("ui.conversation.views", { id: "chat" }, { key: "chat" });
  }, { requires: ["ui.layout"], purpose: "Conversation projection" });
  cordis.define("ui.trajectory", (ctx) => {
    ctx.register("ui.conversation.views", { id: "trajectory" }, { key: "trajectory" });
  }, { requires: ["ui.layout"], purpose: "Pipeline trajectory projection" });
  void Promise.all([
    cordis.load("ui.conversation"),
    cordis.load("ui.trajectory"),
  ])
    .then(loadExternalUiPlugins)
    .catch((error) => console.error("UI plugins failed to load", error));

  async function loadExternalUiPlugins() {
    const response = await fetch("/api/plugins/ui", { cache: "no-store" });
    if (!response.ok) throw new Error(`UI plugin manifest failed: ${response.status}`);
    const manifest = await response.json();
    for (const item of manifest.plugins || []) {
      const safeName = String(item.plugin || "").replace(/[^a-zA-Z0-9._-]/g, "-");
      if (!safeName) continue;
      const pluginName = `external.assets.${safeName}`;
      cordis.define(pluginName, async (ctx) => {
        for (const href of item.styles || []) {
          const link = document.createElement("link");
          link.rel = "stylesheet";
          link.href = href;
          link.dataset.mewcodePlugin = safeName;
          document.head.appendChild(link);
          ctx.effect(() => link.remove());
        }
        for (const src of item.scripts || []) {
          await new Promise((resolve, reject) => {
            const script = document.createElement("script");
            script.src = src;
            script.async = false;
            if (item.module) script.type = "module";
            script.dataset.mewcodePlugin = safeName;
            script.onload = resolve;
            script.onerror = () => reject(new Error(`UI plugin failed: ${src}`));
            document.head.appendChild(script);
            ctx.effect(() => script.remove());
          });
        }
        for (const menu of item.menus || []) ctx.register("ui.menus", menu);
        for (const setting of item.settings || []) ctx.register("ui.settings", setting);
        await ctx.emit("ui/plugin_loaded", item);
      }, { requires: ["ui.layout"], purpose: `External UI assets: ${safeName}` });
      await cordis.load(pluginName);
    }
  }
  const pipelineStates = new Map();
  let pipelineClockTimer = null;
  let conversationView = "chat";
  let trajectorySearchQuery = "";
  let trajectoryUseDuration = true;
  let trajectorySelectedItemId = "";
  let trajectoryDetailTab = "summary";
  const trajectoryCollapsedTurns = new Set();
  /** 编排商量模式：主聊天用当前模型对话，尚未 activate expert_mode */
  let orchestrationPlanning = false;
  /** 本轮已在主聊天发出开跑前澄清，再次确认开跑时跳过重复提问 */

  let ws = null;
  let reconnectTimer = null;
  let reconnectAttempt = 0;
  let reconnectNeedsReplay = false;
  let reconnectReplayPending = false;
  let streaming = false;
  const runningConversations = new Set();
  const pendingConversationStarts = new Map();
  const stoppingConversations = new Set();
  const conversationStopTimers = new Map();
  const pendingConversationPrompts = new Map();
  const conversationEventBuffers = new Map();
  const expertLastSeq = new Map();
  const expertSnapshotSeq = new Map();
  // Resume guidance is scoped to the exact conversation/run pair.  A recovery
  // card from another run must never make the current run look actionable.
  const expertResumeAdvices = new Map();
  const backendTurnScopes = new Map();
  const backendErrorNotices = new Map();
  let backendErrorScopeCounter = 0;
  const expertTimelines = new Map();
  let conversationLoadController = null;
  let conversationNavigationToken = 0;
  let conversationNavigationPending = false;
  let currentPermissionConversationId = "";
  let currentPermissionRequestId = "";
  let currentMsg = null;
  let currentText = "";
  let currentTextSegment = "";
  let currentMarkdownSegment = null;
  let currentToolGroup = null;   // 当前工具聚合组（details.tool-group）
  let currentThinkingEl = null;
  let currentThinkingStarted = 0;
  let currentThinkingText = "";
  let turnStartedAt = 0;
  let turnClockTimer = null;
  const toolRows = new Map();    // tool_id -> 行元素
  const pendingTurnToolIds = new Set(); // 当前普通对话回合中尚无结果的工具

  // 全局状态
  let currentStatus = {};
  let currentRuntimeLoad = { limit: 4, running: 0, queued: 0, queue: [] };
  let currentEfforts = { levels: [], current: "auto" };
  let currentSlots = [];
  let permissionModes = [];
  let modelList = [];
  let modelCatalog = null;
  let modelCatalogAt = 0;
  let modelCatalogRequest = null;
  let catalogRefreshTimer = null;
  let runtimeSettingsView = null;
  let currentExpertTimeline = null;
  let usageTerminalRefreshTimer = null;
  let searchTimer = null;
  let readonlyView = false;      // 跨项目只读预览 / 外部对话只读
  let currentExternalOpen = null;
  let conversationContextSignature = "";
  let currentHumanOnCall = null;
  let currentAskUser = null;
  // A reconnect reloads the active conversation and clears live DOM state.
  // Keep only unsubmitted HumanOnCall drafts, keyed by the exact prompt,
  // conversation, and run so a new request can never inherit an old decision.
  const humanOnCallDrafts = new Map();
  const askUserDrafts = new Map();
  // Prepared candidates are replayed only for the same conversation, run,
  // request and bundle version. This mirrors the server's stale-bundle gate.
  const interactionChoiceBundleCache = new Map();
  let pendingAttachments = [];
  let attachmentUploadBusy = false;
  let chatQueueSubmissionPending = false;
  let settingsReturnFocus = null;
  let settingsDialogCleanup = null;

  let sidebarData = { machines: [], work_dir: "" };
  let workspaceOpenState = {};
  // The backend registry is the sole command catalog. Parsing remains generic
  // so an unknown slash command still reaches the backend for a useful error.
  let slashCommands = [];
  let slashMatches = [];
  let slashSelectedIndex = 0;
  let projectContextReturnFocus = null;
  const PROJECT_OPEN_STATE_KEY = "mewcode-project-open-state-v1";
  const UI_STATE_KEY = "mewcode-ui-state-v1";
  let projectOpenState = {};
  let rememberedFamilyKey = "";
  hydrateUiStateFromStorage();

  if (window.mewcodeDesktop) document.body.classList.add("is-desktop");

  function activeConversationId() {
    return String(currentStatus.conversation_id || "");
  }

  function conversationChatContext() {
    const conversationId = activeConversationId();
    const unsupported = document.body?.classList?.contains("mobile-runtime");
    const readonly = !!readonlyView || !!unsupported;
    return {
      conversationId,
      available: !!conversationId && !conversationNavigationPending && !readonly,
      busy: !!conversationId && !!streaming,
      readonly,
    };
  }

  function emitConversationContext(force = false) {
    const detail = conversationChatContext();
    const signature = [
      detail.conversationId,
      detail.available ? "1" : "0",
      detail.busy ? "1" : "0",
      detail.readonly ? "1" : "0",
    ].join("\u0000");
    if (!force && signature === conversationContextSignature) return;
    conversationContextSignature = signature;
    if (typeof window.dispatchEvent !== "function") return;
    let event;
    if (typeof window.CustomEvent === "function") {
      event = new window.CustomEvent("mewcode:conversation-context", { detail });
    } else if (document.createEvent) {
      event = document.createEvent("CustomEvent");
      event.initCustomEvent("mewcode:conversation-context", false, false, detail);
    }
    if (event) window.dispatchEvent(event);
  }

  function emitConversationUpdated(conversationId, type = "mewcode:conversation-updated") {
    const id = String(conversationId || "");
    if (!id || typeof window.dispatchEvent !== "function") return;
    let event;
    const detail = { conversationId: id };
    if (typeof window.CustomEvent === "function") {
      event = new window.CustomEvent(type, { detail });
    } else if (document.createEvent) {
      event = document.createEvent("CustomEvent");
      event.initCustomEvent(type, false, false, detail);
    }
    if (event) window.dispatchEvent(event);
  }

  window.MewConversationContext = {
    getContext: () => ({ ...conversationChatContext() }),
  };
  window.MewWorkbenchHost = {
    toggleTheme: () => applyTheme(document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark'),
    context: () => ({ ...conversationChatContext(), project: currentStatus.work_dir || "" }),
    command: workbenchCommand,
    busy: () => streaming || readonlyView || conversationNavigationPending,
    composeMessage: text => {
      if (readonlyView || conversationNavigationPending) return false;
      inputEl.value = inputEl.value ? inputEl.value + '\n' + text : text;
      inputEl.dispatchEvent(new Event('input', {bubbles:true}));
      inputEl.focus();
      return true;
    },
    buildGraph: ({conversation_id}) => {
      if (conversation_id !== activeConversationId() || streaming || readonlyView || conversationNavigationPending) return false;
      if (!messagesEl.querySelector('.msg.user') && !inputEl.value.trim() && !pendingAttachments.length) {
        inputEl.focus();return false;
      }
      const instruction='结合本对话已有的目标、资料和要求，生成或更新编排图并保存；保留仍有效的节点和连接。';
      inputEl.value=inputEl.value.trim() ? inputEl.value.trim()+'\n\n'+instruction : instruction;
      inputEl.dispatchEvent(new Event('input', {bubbles:true}));
      void sendChat(true);
      return true;
    },
    designStarted: data => {
      if (data.conversation_id !== activeConversationId()) return;
      orchestrationPlanning = true;
      send({type: 'set_orchestration_planning', on: true});
      inputEl.focus();
    },
    leaveDesign: () => {
      if (readonlyView || conversationNavigationPending) return false;
      if (currentStatus.expert_mode) send({type:'set_expert_mode',on:false});
      send({type:'set_orchestration_planning',on:false});
      orchestrationPlanning = false;
      addSystemMsg('已返回普通对话，任务记录和检查点仍可在工作台查看。');
      inputEl.focus();
      return true;
    },
    showStep: options => window.MewCurrentExecution.open(options),
    userMessages: () => Array.from(messagesEl.querySelectorAll('.msg.user')).filter(row => typeof row.mewRawUserText === 'string')
      .map(element => ({element, text: element.mewRawUserText})),
    appendNotice: (text, open) => {
      hideEmptyState();
      const row = document.createElement('div'); row.className = 'msg system wb-chat-notice';
      const link = document.createElement('button'); link.type = 'button'; link.textContent = text;
      link.addEventListener('click', open); row.appendChild(link); messagesEl.appendChild(row); scrollBottom(); return link;
    },
  };
  function workbenchPrompt(msg) {
    if (document.body.classList.contains('mobile-runtime') || !(msg.run_id || msg.task_id || msg.task_design?.task_id)) return false;
    window.MewWorkbench?.store.refresh({tasks: true});
    return true;
  }
  function workbenchCommand(command) {
    const owner = conversationChatContext();
    if (owner.readonly || command.conversation_id !== owner.conversationId
        || !['expert_resume', 'expert_terminate'].includes(command.type)) {
      return Promise.reject(new Error('请回到任务所属对话后操作。'));
    }
    return new Promise((resolve, reject) => {
      const abort = new AbortController();
      const finish = (error, result) => { clearTimeout(timer); abort.abort(); error ? reject(error) : resolve(result); };
      const timer = setTimeout(() => finish(new Error('请求已发送，尚未确认结果。请查看刷新后的任务状态。')), 15000);
      window.addEventListener('mewcode:workbench-host-event', event => {
        const msg = event.detail;
        if (msg.conversation_id && msg.conversation_id !== command.conversation_id) return;
        if (msg.run_id && msg.run_id !== command.run_id) return;
        if (msg.type === 'error') finish(new Error(backendErrorText(msg)));
        else if (msg.run_id === command.run_id) finish(null, msg);
      }, {signal: abort.signal});
      if (!send(command)) finish(new Error('连接暂时不可用，请稍后重试。'));
    });
  }
  emitConversationContext(true);

  function beginConversationNavigation() {
    const token = ++conversationNavigationToken;
    conversationNavigationPending = true;
    syncActiveStreaming();
    return token;
  }

  function endConversationNavigation(token) {
    if (token !== conversationNavigationToken) return;
    conversationNavigationPending = false;
    syncActiveStreaming();
  }

  function renderContinuousStatus() {
    const chip = $("topbar-mode-goal");
    if (!chip) return;
    const task = currentStatus.continuation || {};
    const labels = {running: "持续执行", reviewing: "结束审核", intervening: "处理插话", paused: "持续任务已暂停", waiting_user: "等待补充", blocked: "持续任务受阻", complete: "持续目标已完成", terminated: "持续任务已终止"};
    chip.classList.toggle("hidden", !task.id && !currentStatus.goal_mode);
    chip.textContent = labels[task.status] || "目标";
    chip.title = [task.reason, task.objective || currentStatus.goal].filter(Boolean).join("\n");
  }

  function syncActiveStreaming() {
    const conversationId = activeConversationId();
    const startPending = pendingConversationStarts.has(conversationId);
    const stopping = stoppingConversations.has(conversationId);
    streaming = runningConversations.has(conversationId) || startPending || stopping;
    const connected = !!ws && ws.readyState === WebSocket.OPEN;
    const queuesInsteadOfSending = streaming && !stopping;
    const durableChat = currentStatus.execution_kind === "chat" && !!currentStatus.continuation?.id;
    sendBtn.disabled = conversationNavigationPending || stopping || !connected ||
      readonlyView || chatQueueSubmissionPending;
    sendBtn.classList.toggle("is-queueing", queuesInsteadOfSending);
    sendBtn.title = queuesInsteadOfSending
      ? (currentStatus.execution_kind === "orchestration" && !pendingAttachments.length ? "临时接管：主模型先处理这条任务，再返回编排" : (currentStatus.continuation?.status && ["running", "reviewing", "intervening"].includes(currentStatus.continuation.status) && !pendingAttachments.length ? "插入消息：先处理，再审核是否继续原任务" : "加入等待区"))
      : "发送";
    sendBtn.setAttribute("aria-label", sendBtn.title);
    attachBtn.disabled = chatQueueSubmissionPending;
    // Stopping uses an HTTP control endpoint and remains available even when
    // the stream WebSocket is reconnecting.
    stopBtn.disabled = !streaming;
    stopBtn.classList.toggle("hidden", !streaming);
    stopBtn.classList.toggle("is-stopping", stopping);
    stopBtn.textContent = stopping
      ? "停止中…"
      : (currentStatus.execution_kind === "orchestration" || durableChat ? "暂停" : "打断");
    stopBtn.title = stopping
      ? "正在确认服务端已停止"
      : (currentStatus.execution_kind === "orchestration" || durableChat ? "暂停任务并保存检查点" : "打断当前回复");
    stopBtn.setAttribute("aria-label", stopBtn.title);
    window.MewChatQueue?.render?.();
    emitConversationContext();
  }

  function clearPendingConversationStart(conversationId) {
    const id = String(conversationId || "");
    const pending = pendingConversationStarts.get(id);
    if (pending && pending.timer) window.clearTimeout(pending.timer);
    pendingConversationStarts.delete(id);
  }

  function clearConversationStopTimer(conversationId) {
    const id = String(conversationId || "");
    const timer = conversationStopTimers.get(id);
    if (timer) window.clearTimeout(timer);
    conversationStopTimers.delete(id);
  }

  function releaseConversationActivity(conversationId) {
    const id = String(conversationId || "");
    clearPendingConversationStart(id);
    clearConversationStopTimer(id);
    runningConversations.delete(id);
    stoppingConversations.delete(id);
    syncActiveStreaming();
  }

  function failUnacknowledgedConversationStart(conversationId, detail) {
    const id = String(conversationId || "");
    if (!pendingConversationStarts.has(id)) return;
    releaseConversationActivity(id);
    if (id !== activeConversationId()) return;
    finishStream({ interrupted: false, elapsed: 0 });
    addErrorMsg(detail || "消息未被服务端确认，已解除输入锁，请重新发送。");
  }

  async function reconcilePendingConversationStart(conversationId) {
    const id = String(conversationId || "");
    const pending = pendingConversationStarts.get(id);
    if (!pending) return;
    pending.attempts = Number(pending.attempts || 0) + 1;
    try {
      const response = await fetch(
        "/api/chat/status?conversation_id=" + encodeURIComponent(id),
        { cache: "no-store" }
      );
      if (!response.ok) throw new Error("status " + response.status);
      const status = await response.json();
      if (!pendingConversationStarts.has(id)) return;
      if (status.running) {
        clearPendingConversationStart(id);
        runningConversations.add(id);
        syncActiveStreaming();
        return;
      }
      failUnacknowledgedConversationStart(
        id,
        "消息没有进入服务端，已解除输入锁。请重新发送本条消息。"
      );
    } catch {
      if (!pendingConversationStarts.has(id)) return;
      if (pending.attempts >= 3) {
        failUnacknowledgedConversationStart(
          id,
          "服务端连续未确认本轮请求，已自动解除输入锁。请检查连接后重新发送。"
        );
        return;
      }
      pending.timer = window.setTimeout(
        () => void reconcilePendingConversationStart(id),
        3000
      );
    }
  }

  function markConversationStartPending(conversationId) {
    const id = String(conversationId || "");
    clearPendingConversationStart(id);
    const pending = { attempts: 0, timer: 0 };
    pending.timer = window.setTimeout(
      () => void reconcilePendingConversationStart(id),
      8000
    );
    pendingConversationStarts.set(id, pending);
    syncActiveStreaming();
  }

  function externalSourceLabel(source) {
    return source === "claude-code" ? "Claude Code" : source === "codex" ? "Codex" : "MewCode";
  }

  function setReadonlyView(on, meta = null) {
    readonlyView = !!on;
    currentExternalOpen = on ? meta : null;
    if (inputEl) inputEl.disabled = !!on;
    if (sendBtn) sendBtn.disabled = !!on;
    const continueBtn = $("continue-in-mewcode");
    if (continueBtn) continueBtn.classList.toggle("hidden", !on);
    emitConversationContext();
  }

  function resetLiveMessageState() {
    clearLiveMarkdownState(true);
    if (currentMsg) {
      const bubble = currentMsg.querySelector(".msg-bubble");
      if (bubble && !bubble.children.length) currentMsg.remove();
    }
    currentMsg = null;
    currentText = "";
    currentTextSegment = "";
    currentMarkdownSegment = null;
    currentToolGroup = null;
    for (const state of expertTimelines.values()) {
      for (const timer of state.textRenderTimers.values()) clearTimeout(timer);
    }
    expertTimelines.clear();
    currentExpertTimeline = null;
    toolRows.clear();
    pendingTurnToolIds.clear();
    rememberHumanOnCallDraft();
    currentHumanOnCall = null;
    currentAskUser = null;
    humanOnCallPanel.classList.add("hidden");
    permBar.classList.add("hidden");
  }

  function pipelineStateFor(conversationId) {
    const id = String(conversationId || "");
    let state = pipelineStates.get(id);
    if (!state) {
      state = { items: new Map(), order: [], lastSeq: 0, expertFlows: new Map() };
      pipelineStates.set(id, state);
    }
    return state;
  }

  function expertRunScopeKey(conversationId, runId) {
    const conversation = String(conversationId || "");
    const run = String(runId || "");
    return conversation && run ? conversation + "::" + run : "";
  }

  function scopedResumeAdvice(conversationId, runId) {
    const key = expertRunScopeKey(conversationId, runId);
    return key ? expertResumeAdvices.get(key) || null : null;
  }

  function saveScopedResumeAdvice(advice, conversationId, runId) {
    const conversation = String(conversationId || advice?.conversation_id || "");
    const run = String(runId || advice?.run_id || "");
    const key = expertRunScopeKey(conversation, run);
    if (!key) return null;
    const timeline = expertTimelines.get(run);
    const flow = pipelineStates.get(conversation)?.expertFlows?.get(run);
    if (!advice || typeof advice !== "object") {
      expertResumeAdvices.delete(key);
      if (timeline) timeline.resumeAdvice = null;
      if (flow) flow.resumeAdvice = null;
      return null;
    }
    if (advice.run_id && String(advice.run_id) !== run) return null;
    if (advice.conversation_id && String(advice.conversation_id) !== conversation) return null;
    const value = {...advice, run_id: run, conversation_id: conversation};
    expertResumeAdvices.set(key, value);
    if (timeline) timeline.resumeAdvice = value;
    if (flow) flow.resumeAdvice = value;
    return value;
  }

  function legacyResumeAdviceFromSnapshot(snapshot, conversationId) {
    if (!snapshot || typeof snapshot !== "object") return null;
    const runId = String(snapshot.run_id || "");
    const cid = String(conversationId || snapshot.conversation_id || "");
    if (!runId || !cid) return null;
    if (!["paused", "blocked", "needs_attention"].includes(String(snapshot.status || ""))) return null;
    const states = snapshot.node_states || snapshot.stages || {};
    const nodes = new Map((snapshot.locked_flow?.nodes || []).map((node) => [String(node.node_id || ""), node]));
    const candidates = [];
    const openCases = [];
    for (const [nodeId, nodeState] of Object.entries(states)) {
      const recoveryCase = nodeState?.recovery_case || nodeState?.pending_recovery_case;
      if (!recoveryCase || !["reported", "awaiting_user"].includes(String(recoveryCase.status || ""))) continue;
      const pending = recoveryCase?.pending;
      const hasPending = pending && (typeof pending !== "object" || Object.keys(pending).length > 0);
      const node = nodes.get(String(nodeId)) || {};
      const label = String(node.label || nodeState.label || nodeId);
      if (!recoveryCase.closed_by_user || hasPending) {
        openCases.push({nodeId: String(nodeId), label, recoveryCase});
        continue;
      }
      const bindingValid = String(recoveryCase.node_id || "") === String(nodeId)
        && recoveryCase.epoch != null && nodeState.epoch != null
        && String(recoveryCase.epoch) === String(nodeState.epoch)
        && Boolean(recoveryCase.input_revision) && Boolean(nodeState.input_revision)
        && String(recoveryCase.input_revision) === String(nodeState.input_revision)
        && ["continuing", "failed", "stalled", "needs_attention", "blocked", "budget_exhausted", "paused"].includes(String(nodeState.status || ""));
      const stale = !bindingValid;
      candidates.push({nodeId: String(nodeId), label, recoveryCase, nodeState, stale});
    }
    const summaries = [...candidates, ...openCases].map(({nodeId, label, recoveryCase, stale}) => ({
      case_id: String(recoveryCase.case_id || ""), node_id: nodeId, node_label: label,
      status: String(recoveryCase.status || ""), closed_by_user: !!recoveryCase.closed_by_user,
      reason: !recoveryCase.closed_by_user ? "当前恢复案件仍待处理。"
        : stale ? "案件代际或输入版本已变化，请核对当前节点状态后再决定。"
        : "已停止，当前节点尚未完成。",
    }));
    const invalidClosedCase = openCases.some(({recoveryCase}) => !!recoveryCase.closed_by_user);
    if (invalidClosedCase || (openCases.length && candidates.length)) {
      return {
        run_id: runId, conversation_id: cid, action: "review_cases",
        reason: invalidClosedCase ? "恢复案件状态不完整，请刷新并核对后再选择。"
          : "当前有未决恢复案件，请逐项查看后选择。",
        node_id: "", node_label: "", case_id: "",
        cases: summaries,
      };
    }
    // Preserve the old service's real waiting card when there is no stopped
    // case that can be safely described from the snapshot alone.
    if (openCases.length) return null;
    if (!candidates.length) return null;
    if (candidates.length !== 1 || candidates[0].stale) {
      return {
        run_id: runId, conversation_id: cid, action: "review_cases",
        reason: candidates.length > 1
          ? "当前有多个已停止恢复案件，请逐项查看后选择。"
          : "案件来源代际或输入版本已变化，请核对当前节点状态后再决定。",
        node_id: candidates.length === 1 ? candidates[0].nodeId : "",
        node_label: candidates.length === 1 ? candidates[0].label : "",
        case_id: candidates.length === 1 ? String(candidates[0].recoveryCase.case_id || "") : "",
        cases: summaries,
      };
    }
    const candidate = candidates[0];
    return {
      run_id: runId, conversation_id: cid, action: "retry_node",
      reason: "上次自动修复已停止；「" + candidate.label + "」尚未完成。",
      node_id: candidate.nodeId, node_label: candidate.label,
      case_id: String(candidate.recoveryCase.case_id || ""), cases: summaries,
    };
  }

  function invalidateScopedResumeAdvice(event) {
    if (!event?.run_id) return;
    const conversation = String(event.conversation_id || activeConversationId() || "");
    const run = String(event.run_id || "");
    const key = expertRunScopeKey(conversation, run);
    if (!key) return;
    expertResumeAdvices.delete(key);
    const timeline = expertTimelines.get(run);
    const flow = pipelineStates.get(conversation)?.expertFlows?.get(run);
    if (timeline) timeline.resumeAdvice = null;
    if (flow) flow.resumeAdvice = null;
  }

  function resetPipeline(conversationId, events = []) {
    const id = String(conversationId || "");
    const state = { items: new Map(), order: [], lastSeq: 0, expertFlows: new Map() };
    pipelineStates.set(id, state);
    for (const event of events || []) applyPipelineEvent(event, false);
    if (id === activeConversationId()) renderPipeline();
  }

  function pipelineItemFromEvent(state, event) {
    const itemId = String(event.item_id || event.id || ("item-" + event.seq));
    let item = state.items.get(itemId);
    if (!item) {
      item = {
        item_id: itemId,
        turn_id: String(event.turn_id || ""),
        seq: Number(event.seq || 0),
        parent_id: event.parent_id || null,
        role: String(event.role || "system"),
        kind: String(event.kind || "event"),
        name: String(event.name || ""),
        phase: String(event.phase || "start"),
        status: String(event.status || "running"),
        text: "",
        meta: {},
        started_at: event.started_at || event.timestamp || null,
        completed_at: event.completed_at || null,
        duration_ms: event.duration_ms == null ? null : Number(event.duration_ms),
      };
      state.items.set(itemId, item);
      state.order.push(itemId);
    }
    if (event.turn_id) item.turn_id = String(event.turn_id);
    if (event.seq != null) item.seq = Number(event.seq || 0);
    if (event.parent_id !== undefined) item.parent_id = event.parent_id;
    if (event.name) item.name = String(event.name);
    if (event.role) item.role = String(event.role);
    if (event.kind) item.kind = String(event.kind);
    if (event.phase) item.phase = String(event.phase);
    if (event.started_at && !item.started_at) item.started_at = event.started_at;
    if (event.replace_text) item.text = String(event.text || '');
    else if (event.text) item.text += String(event.text);
    if (event.meta && typeof event.meta === "object") item.meta = { ...item.meta, ...event.meta };
    if (event.status) item.status = String(event.status);
    if (event.completed_at) item.completed_at = event.completed_at;
    if (event.duration_ms != null) item.duration_ms = Number(event.duration_ms);
    return item;
  }

  function applyPipelineEvent(event, repaint = true) {
    if (!event || event.type !== "pipeline_item") return;
    const conversationId = String(event.conversation_id || activeConversationId());
    if (!conversationId) return;
    const state = pipelineStateFor(conversationId);
    const seq = event.synthetic ? 0 : Number(event.seq || 0);
    if (seq && seq <= state.lastSeq) return;
    if (seq) state.lastSeq = seq;
    pipelineItemFromEvent(state, event);
    if (repaint && conversationId === activeConversationId()) renderPipeline();
    syncPipelineClock();
  }

  const expertTrajectoryTypes = new Set([
    "expert_flow_defined", "expert_node_started", "expert_node_completed",
    "expert_node_delta", "expert_node_thinking", "expert_node_tool",
    "expert_member_started", "expert_member_completed", "expert_member_delta",
    "expert_member_thinking", "expert_member_tool", "expert_join_started",
    "expert_join_completed", "expert_handoff", "expert_flow_state",
    "expert_pipeline_started", "expert_pipeline_completed",
    "expert_pipeline_step_started", "expert_pipeline_step_completed",
    "expert_pipeline_step_output", "expert_pipeline_step_thinking",
    "expert_pipeline_step_tool", "expert_pipeline_step_usage",
    "expert_pipeline_warning", "expert_run_state", "expert_run_done",
  ]);

  function expertTrajectoryTime(value) {
    if (typeof value === "number" && Number.isFinite(value)) {
      return new Date(value * 1000).toISOString();
    }
    return value || null;
  }

  function applyExpertTrajectoryEvent(event, repaint = true) {
    if (!event || !expertTrajectoryTypes.has(event.type)) return;
    const conversationId = String(event.conversation_id || activeConversationId());
    if (!conversationId) return;
    const state = pipelineStateFor(conversationId);
    const runId = String(event.run_id || "expert");
    const previousFlow = state.expertFlows.get(runId);
    const seq = Number(event.seq || 0);
    if (!event.history && seq && seq <= Number(previousFlow?.lastSeq || 0)) return;
    // History populates the trajectory below, but never changes checkpoint lights.
    if (!event.history) {
      if (event.type === "expert_flow_defined" && event.flow) {
        state.expertFlows.set(runId, {
          runId, status: "pending", phase: "defined",
          statuses: new Map(), members: new Map(), handoffs: new Set(),
          ...previousFlow, flow: event.flow,
          lastSeq: Math.max(seq, Number(previousFlow?.lastSeq || 0)),
          updatedAt: event.server_time || Date.now() / 1000,
        });
        if (conversationId === activeConversationId()) {
          setExpertTimelineFlow(ensureExpertTimelineV2(runId), event.flow);
        }
      }
      let flowState = state.expertFlows.get(runId);
      if (!flowState) {
        flowState = {
          runId, flow: null, status: "pending", phase: "",
          statuses: new Map(), members: new Map(), handoffs: new Set(),
          lastSeq: 0, updatedAt: 0,
        };
        state.expertFlows.set(runId, flowState);
      }
      flowState.members ||= new Map();
      flowState.auditProtocols ||= new Map();
      flowState.loopStates ||= new Map();
      flowState.lastSeq = Math.max(Number(flowState.lastSeq || 0), Number(event.seq || 0));
      const rawEventTime = event.server_time || flowState.updatedAt || Date.now() / 1000;
      const parsedEventTime = typeof rawEventTime === "number"
        ? rawEventTime : Date.parse(String(rawEventTime)) / 1000;
      flowState.updatedAt = Number.isFinite(parsedEventTime)
        ? parsedEventTime : Date.now() / 1000;
      const nodeId = String(event.node_id || "");
      const memberId = String(event.member_id || "");
      if (event.stage_cursor !== undefined) flowState.stageCursor = String(event.stage_cursor || "");
      else if (nodeId && event.type === "expert_node_started"
        && flowState.flow?.nodes?.some(node => node.node_id === nodeId)) flowState.stageCursor = nodeId;
      if (nodeId && ["expert_node_started", "expert_node_completed"].includes(event.type)) {
        const previous = flowState.statuses.get(nodeId) || {};
        flowState.statuses.set(nodeId, {
          ...previous,
          status: event.status || (event.type.endsWith("started") ? "running" : "completed"),
          started_at: expertTrajectoryTime(event.started_at) || previous.started_at || null,
          completed_at: expertTrajectoryTime(event.completed_at) || previous.completed_at || null,
          duration_ms: event.duration_ms == null ? previous.duration_ms : Number(event.duration_ms),
          passed: event.passed === undefined ? previous.passed : !!event.passed,
          artifact_count: Array.isArray(event.handoff?.artifacts) ? event.handoff.artifacts.length : previous.artifact_count || 0,
          findings_discharged: !!event.findings_discharged,
          reason: event.reason || "",
          status_reason: event.type.endsWith("started") ? "" : (event.status_reason ||
            (["failed", "stalled", "budget_exhausted"].includes(event.status) ? String(event.transcript || event.text || "").slice(0, 1500) : "")),
        });

      }
      if (Array.isArray(event.discharged_audit_node_ids)) {
        const resolved = new Set(event.discharged_audit_node_ids);
        for (const [id, info] of flowState.statuses) {
          info.findings_discharged = resolved.has(id);
        }
      }
      if (event.resume_requires_retry !== undefined) flowState.resumeRequiresRetry = !!event.resume_requires_retry;
      if (event.phase === "resuming") flowState.resumeRequiresRetry = false;
      if (nodeId && memberId && ["expert_member_started", "expert_member_completed"].includes(event.type)) {
        const memberKey = nodeId + ":" + memberId;
        const previous = flowState.members.get(memberKey) || {};
        flowState.members.set(memberKey, {
          ...previous,
          node_id: nodeId,
          member_id: memberId,
          member_name: event.member_name || previous.member_name || memberId,
          status: event.status || (event.type.endsWith("started") ? "running" : "completed"),
          started_at: expertTrajectoryTime(event.started_at) || previous.started_at || null,
          completed_at: expertTrajectoryTime(event.completed_at) || previous.completed_at || null,
          duration_ms: event.duration_ms == null ? previous.duration_ms : Number(event.duration_ms),
        });
      }
      if (event.type === "expert_handoff") {
        flowState.handoffs.add(String(event.from_node) + "->" + String(event.to_node));
      }
      if (event.type === "expert_flow_state") {
        if (event.loop_state && event.loop_state_key) {
          flowState.loopStates.set(event.loop_state_key, {...event.loop_state, loop_id: event.loop_id,
            contract: event.loop_contract || {}});
        }
        if (event.audit_protocol && nodeId) {
          flowState.auditProtocols.set(nodeId, event.audit_protocol);
        }
        flowState.phase = String(event.phase || flowState.phase || "");
        flowState.status = String(event.status || (
          event.phase === "done" ? "completed" : "running"
        ));
        flowState.started_at = expertTrajectoryTime(event.started_at) || flowState.started_at || null;
        flowState.completed_at = expertTrajectoryTime(event.completed_at) || flowState.completed_at || null;
      } else if (event.type === "expert_node_started") {
        flowState.status = "running";
        flowState.started_at ||= expertTrajectoryTime(event.started_at);
      } else if (event.type === "expert_run_state" || event.type === "expert_run_done") {
        flowState.status = String(event.status || flowState.status || "pending");
      }
    }

    const nodeId = String(event.node_id || "");
    const memberId = String(event.member_id || "");
    const base = {
      type: "pipeline_item", synthetic: true, conversation_id: conversationId,
      turn_id: "expert:" + runId, seq: Number(event.seq || 0),
      replace_text: event.snapshot_aggregate === true,
    };
    let item = null;
    if (event.type.startsWith("expert_node_")) {
      const phase = event.type.replace("expert_node_", "");
      const toolId = String(event.id || event.tool_id || "");
      const isTool = phase === "tool";
      item = {
        ...base,
        item_id: isTool
          ? "expert-tool:" + runId + ":" + nodeId + ":" + toolId
          : "expert-node:" + runId + ":" + nodeId,
        parent_id: isTool ? "expert-node:" + runId + ":" + nodeId : null,
        role: isTool ? "tool" : "expert",
        kind: isTool ? "tool" : (phase === "thinking" ? "thinking" : "node"),
        name: isTool ? (event.name || "工具") : (event.label || nodeId || event.role),
        phase: event.phase || phase,
        status: event.status || (
          phase === "completed" || (isTool && phase === "result")
            ? "completed" : "running"
        ),
        text: event.text || event.output || "",
        started_at: expertTrajectoryTime(event.started_at),
        completed_at: expertTrajectoryTime(event.completed_at),
        duration_ms: event.duration_ms,
        meta: {
          node_id: nodeId, role: event.role, arguments: event.args,
          passed: event.passed, recovery_round: event.recovery_round,
        },
      };
    } else if (event.type.startsWith("expert_member_")) {
      const phase = event.type.replace("expert_member_", "");
      const toolId = String(event.id || event.tool_id || "");
      const isTool = phase === "tool";
      item = {
        ...base,
        item_id: isTool
          ? "expert-member-tool:" + runId + ":" + nodeId + ":" + memberId + ":" + toolId
          : "expert-member:" + runId + ":" + nodeId + ":" + memberId,
        parent_id: "expert-node:" + runId + ":" + nodeId,
        role: isTool ? "tool" : "member",
        kind: isTool ? "tool" : (phase === "thinking" ? "thinking" : "member"),
        name: isTool ? (event.name || "工具") : (event.member_name || memberId),
        phase: event.phase || phase,
        status: event.status || (
          phase === "completed" || (isTool && phase === "result")
            ? "completed" : "running"
        ),
        text: event.text || event.output || "",
        started_at: expertTrajectoryTime(event.started_at),
        completed_at: expertTrajectoryTime(event.completed_at),
        duration_ms: event.duration_ms,
        meta: { node_id: nodeId, member_id: memberId, arguments: event.args },
      };
    } else if (event.type.startsWith("expert_pipeline_")) {
      const pipelineNodeId = String(event.pipeline_node_id || "");
      const pipelineItemId = "expert-pipeline:" + runId + ":" + nodeId;
      if (["expert_pipeline_started", "expert_pipeline_completed"].includes(event.type)) {
        item = {
          ...base,
          item_id: pipelineItemId,
          parent_id: "expert-node:" + runId + ":" + nodeId,
          role: "pipeline", kind: "pipeline",
          name: event.label || event.pipeline_id || "Pipeline",
          status: event.status || (event.type.endsWith("completed") ? "completed" : "running"),
          started_at: expertTrajectoryTime(event.started_at),
          completed_at: expertTrajectoryTime(event.completed_at),
          duration_ms: event.duration_ms,
          text: event.error || "",
          meta: { node_id: nodeId, pipeline_id: event.pipeline_id },
        };
      } else if (event.type === "expert_pipeline_step_tool") {
        const toolId = String(event.tool_id || event.id || event.seq || "tool");
        item = {
          ...base,
          item_id: "expert-pipeline-tool:" + runId + ":" + nodeId + ":" + pipelineNodeId + ":" + toolId,
          parent_id: "expert-pipeline-step:" + runId + ":" + nodeId + ":" + pipelineNodeId,
          role: "tool", kind: "tool", name: event.name || "工具",
          phase: event.phase || "start", status: event.status || "running",
          text: event.text || "", duration_ms: event.duration_ms,
          meta: {
            node_id: nodeId, pipeline_node_id: pipelineNodeId,
            pipeline_id: event.pipeline_id, arguments: event.args,
          },
        };
      } else if (event.type === "expert_pipeline_warning") {
        item = {
          ...base,
          item_id: "expert-pipeline-warning:" + runId + ":" + nodeId + ":" + event.seq,
          parent_id: pipelineItemId,
          role: "pipeline", kind: "warning", name: "Pipeline 警告",
          status: event.status || "completed", text: event.text || "",
          meta: { node_id: nodeId, pipeline_id: event.pipeline_id },
        };
      } else {
        const phase = event.type.replace("expert_pipeline_step_", "");
        item = {
          ...base,
          item_id: "expert-pipeline-step:" + runId + ":" + nodeId + ":" + pipelineNodeId,
          parent_id: pipelineItemId,
          role: "pipeline", kind: phase === "thinking" ? "thinking" : "pipeline_step",
          name: event.label || pipelineNodeId || "Pipeline step",
          phase, status: event.status || (phase === "completed" ? "completed" : "running"),
          text: event.text || event.error || "",
          started_at: expertTrajectoryTime(event.started_at),
          completed_at: expertTrajectoryTime(event.completed_at),
          duration_ms: event.duration_ms,
          meta: {
            node_id: nodeId, pipeline_node_id: pipelineNodeId,
            pipeline_id: event.pipeline_id, step_type: event.step_type,
          },
        };
      }
    } else if (event.type === "expert_handoff") {
      item = {
        ...base,
        item_id: "expert-handoff:" + runId + ":" + event.seq,
        role: "handoff", kind: "handoff",
        name: String(event.from_node) + " → " + String(event.to_node),
        status: "completed", text: event.summary || "",
        started_at: expertTrajectoryTime(event.created_at || event.server_time),
        completed_at: expertTrajectoryTime(event.created_at || event.server_time),
        duration_ms: 0,
        meta: {
          from_node: event.from_node, to_node: event.to_node,
          condition: event.condition,
        },
      };
    } else if (event.type.startsWith("expert_join_")) {
      item = {
        ...base,
        item_id: "expert-join:" + runId + ":" + nodeId,
        parent_id: "expert-node:" + runId + ":" + nodeId,
        role: "expert", kind: "join",
        name: event.type.endsWith("started") ? "汇合成员" : "成员汇合完成",
        status: event.type.endsWith("completed") ? "completed" : "running",
        text: event.reducer
          ? "Reducer: " + event.reducer
          : "并行成员 " + Number(event.member_count || 0),
      };
    } else if (event.type === "expert_flow_state" && event.phase && event.phase !== "done") {
      item = {
        ...base,
        item_id: "expert-flow-state:" + runId + ":" + event.seq,
        role: "handoff", kind: event.phase,
        name: event.phase.startsWith("audit_") ? "量化验收" : (event.phase === "recovery" ? "恢复路由" : "流程状态"),
        status: event.status || "completed",
        text: event.reason || [event.node_id, event.target_node_id].filter(Boolean).join(" → "),
      };
    }
    if (item) pipelineItemFromEvent(state, item);
    if (repaint) {
      if (conversationId === activeConversationId()) renderPipeline();

      syncPipelineClock();
    }
  }

  function hydrateExpertTrajectory(snapshot) {
    if (!snapshot || !snapshot.run_id || !snapshot.locked_flow) return;
    const snapshotTime = Date.parse(snapshot.updated_at || snapshot.created_at || "");
    const base = {
      conversation_id: snapshot.conversation_id || activeConversationId(),
      run_id: snapshot.run_id,
      server_time: Number.isFinite(snapshotTime) ? snapshotTime / 1000 : 0,
    };
    const state = pipelineStateFor(base.conversation_id);
    const previous = state.expertFlows.get(snapshot.run_id);
    if (snapshot.last_seq != null && Number(snapshot.last_seq) < Number(previous?.lastSeq || 0)) return;
    // Only an authoritative snapshot may clear invalidated checkpoints. Build
    // the entire replacement before repainting or broadcasting its progress.
    state.expertFlows.delete(snapshot.run_id);
    applyExpertTrajectoryEvent({
      ...base, type: "expert_flow_defined", flow: snapshot.locked_flow,
    }, false);
    for (const node of snapshot.locked_flow.nodes || []) {
      const nodeState = (snapshot.node_states || snapshot.stages || {})[node.node_id] || {};
      if (!Object.keys(nodeState).length) continue;
      applyExpertTrajectoryEvent({
        ...base, ...nodeState,
        type: nodeState.status === "running" ? "expert_node_started" : "expert_node_completed",
        node_id: node.node_id, role: node.role, label: node.label,
        text: nodeState.text || nodeState.partial_text || "",
      }, false);
    }
    for (const member of Object.values(snapshot.members || {})) {
      applyExpertTrajectoryEvent({
        ...base, ...member,
        type: member.status === "running" ? "expert_member_started" : "expert_member_completed",
      }, false);
    }
    for (const handoff of snapshot.handoffs || []) {
      applyExpertTrajectoryEvent({ ...base, ...handoff, type: "expert_handoff" }, false);
    }
    const hydrated = pipelineStates.get(base.conversation_id)?.expertFlows?.get(snapshot.run_id);
    if (hydrated) {
      hydrated.activatedAt = Math.max(Number(snapshot.activated_at) || 0, ...[snapshot.created_at,
        ...(snapshot.attempts || []).map(attempt => attempt.started_at)]
        .map(value => Date.parse(String(value || "")) / 1000).filter(Number.isFinite));
      hydrated.auditProtocols = new Map(Object.entries(snapshot.audit_protocols || {}));
      hydrated.resumeRequiresRetry = !!snapshot.resume_requires_retry;
      hydrated.loopStates = new Map(Object.entries(snapshot.loop_states || {}).map(([key, value]) => [key,
        {...value, loop_id: key.split("/").pop(), contract: (snapshot.locked_flow?.meta?.loops || [])
          .find((loop) => loop.loop_id === key.split("/").pop()) || {}}]));
    }
    if (snapshot.flow_state && snapshot.flow_state.phase) {
      applyExpertTrajectoryEvent({
        ...base,
        ...snapshot.flow_state,
        type: "expert_flow_state",
        status: snapshot.status || snapshot.flow_state.status,
        completed_at: snapshot.completed_at || null,
      }, false);
    } else {
      applyExpertTrajectoryEvent({
        ...base,
        type: "expert_run_state",
        status: snapshot.status || "pending",
      }, false);
    }
    if (hydrated) {
      hydrated.lastSeq = Number(snapshot.last_seq || 0);
      hydrated.stageCursor = String(snapshot.stage_cursor || "");
    }

    syncPipelineClock();
  }

  function rehydrateExpertRuns(conversationId, expertRuns) {
    const id = String(conversationId || "");
    if (!id) return;
    const sorted = [...(expertRuns || [])].sort((left, right) => (
      String(left.updated_at || left.created_at || "").localeCompare(
        String(right.updated_at || right.created_at || "")
      )
    ));
    for (const snapshot of sorted) {
      if (!snapshot || !snapshot.run_id) continue;
      // hydrateExpertTrajectory uses activeConversationId for base.conversation_id
      // when snapshot.conversation_id is missing; ensure snapshot carries it.
      if (!snapshot.conversation_id) snapshot.conversation_id = id;
      hydrateExpertTrajectory(snapshot);
    }
    if (id === activeConversationId()) {
      renderPipeline();
    }
  }

  function pipelineDuration(item) {
    if (item.duration_ms != null && Number.isFinite(item.duration_ms)) return Math.max(0, item.duration_ms);
    if (!item.started_at) return 0;
    const start = Date.parse(item.started_at);
    return Number.isFinite(start) ? Math.max(0, Date.now() - start) : 0;
  }

  function formatPipelineDuration(milliseconds) {
    const seconds = Math.max(0, milliseconds) / 1000;
    if (seconds < 1) return Math.round(milliseconds) + "ms";
    if (seconds < 60) return seconds.toFixed(1) + "s";
    const minutes = Math.floor(seconds / 60);
    return minutes + "m " + Math.floor(seconds % 60) + "s";
  }

  function pipelineJson(value) {
    if (value == null || value === "") return "";
    try { return JSON.stringify(value, null, 2); } catch (_) { return String(value); }
  }

  function pipelineRoleLabel(item) {
    if (item.role === "user") return "USER";
    if (item.role === "tool") return "TOOL";
    if (item.role === "expert") return "EXPERT";
    if (item.role === "member") return "MEMBER";
    if (item.role === "handoff") return "HANDOFF";
    if (item.role === "assistant" && item.kind === "thinking") return "THINK";
    return String(item.role || "event").toUpperCase();
  }

  function pipelinePreview(item) {
    const args = item.meta && item.meta.arguments ? pipelineJson(item.meta.arguments) : "";
    if (item.role === "tool") {
      const call = args ? args.replace(/\s+/g, " ") : "";
      const result = item.text ? item.text.replace(/\s+/g, " ") : "";
      return [call, result ? "→ " + result : ""].filter(Boolean).join(" ");
    }
    return String(item.text || "").replace(/\s+/g, " ");
  }

  function pipelineSpan(item) {
    const start = Date.parse(item.started_at || "");
    if (!Number.isFinite(start)) return null;
    let end = Date.parse(item.completed_at || "");
    if (!Number.isFinite(end)) {
      const duration = item.duration_ms != null ? Number(item.duration_ms) : NaN;
      end = Number.isFinite(duration) ? start + Math.max(0, duration) : Date.now();
    }
    return { start, end: Math.max(start, end) };
  }

  function pipelineTurns(state) {
    const turns = [];
    const byId = new Map();
    for (const itemId of state?.order || []) {
      const item = state.items.get(itemId);
      if (!item) continue;
      const id = String(item.turn_id || "turn-" + (item.seq || turns.length + 1));
      let turn = byId.get(id);
      if (!turn) {
        turn = { id, items: [] };
        byId.set(id, turn);
        turns.push(turn);
      }
      turn.items.push(item);
    }
    return turns;
  }

  function trajectoryMatches(item) {
    const query = trajectorySearchQuery.trim().toLowerCase();
    if (!query) return true;
    const haystack = [
      pipelineRoleLabel(item), item.kind, item.name, item.status, item.text,
      pipelineJson(item.meta && item.meta.arguments),
    ].join(" ").toLowerCase();
    return haystack.includes(query);
  }

  function appendTrajectoryCell(row, className, text) {
    const cell = document.createElement("span");
    cell.className = className;
    cell.setAttribute("role", "cell");
    cell.textContent = text;
    row.appendChild(cell);
    return cell;
  }

  function createTrajectoryRow(item, itemIndex) {
    const row = document.createElement("div");
    row.className = "trajectory-row";
    row.dataset.itemId = item.item_id;
    row.dataset.role = item.role;
    row.dataset.kind = item.kind;
    row.dataset.status = item.status;
    row.setAttribute("role", "row");
    row.setAttribute("tabindex", "0");
    row.setAttribute("aria-selected", item.item_id === trajectorySelectedItemId ? "true" : "false");
    const role = pipelineRoleLabel(item);
    const preview = pipelinePreview(item) || (item.status === "running" ? "等待输出…" : "无内容");
    appendTrajectoryCell(row, "trajectory-row-index", String(itemIndex + 1).padStart(2, "0"));
    appendTrajectoryCell(row, "trajectory-row-role", role);
    const content = appendTrajectoryCell(row, "trajectory-row-content", preview);
    content.title = preview;
    appendTrajectoryCell(row, "trajectory-row-name", item.name || item.kind);
    appendTrajectoryCell(row, "trajectory-row-status", ({running: "运行中", completed: "完成", failed: "失败", cancelled: "已取消", interrupted: "已打断", queued: "排队中", waiting: "等待中", blocked: "受阻"})[item.status] || item.status);
    const time = appendTrajectoryCell(row, "trajectory-row-time", formatPipelineDuration(pipelineDuration(item)));
    time.classList.toggle("is-running", item.status === "running");
    const select = () => {
      trajectorySelectedItemId = item.item_id;
      trajectoryDetailTab = "summary";
      renderPipeline();
    };
    row.addEventListener("click", select);
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        select();
      }
    });
    return row;
  }

  function trajectoryTurnDuration(turn) {
    const spans = turn.items.map(pipelineSpan).filter(Boolean);
    if (!spans.length) return turn.items.reduce((total, item) => total + pipelineDuration(item), 0);
    return Math.max(...spans.map((span) => span.end)) - Math.min(...spans.map((span) => span.start));
  }

  function createDetailSection(label, value, code = false) {
    const section = document.createElement("section");
    section.className = "trajectory-detail-section";
    const heading = document.createElement("h3");
    heading.textContent = label;
    const body = document.createElement(code ? "pre" : "p");
    body.className = code ? "trajectory-detail-code" : "trajectory-detail-text";
    body.textContent = value || "无记录";
    section.append(heading, body);
    return section;
  }

  function renderTrajectoryDetails(state, step) {
    if (step) {
      const exact = 'expert-node:' + step.run_id + ':' + step.node_id;
      const found = state?.items.get(exact) || [...(state?.items.values() || [])].find(item =>
        item.turn_id === 'expert:' + step.run_id && item.meta?.node_id === step.node_id);
      if (found) trajectorySelectedItemId = found.item_id;
    }
    const item = state?.items.get(trajectorySelectedItemId);
    if (!item) {
      trajectoryDetailsEl.classList.add("hidden");
      trajectoryDetailsEl.setAttribute("aria-hidden", "true");
      return;
    }
    trajectoryDetailsEl.classList.remove("hidden");
    trajectoryDetailsEl.setAttribute("aria-hidden", "false");
    $("trajectory-detail-kicker").textContent = pipelineRoleLabel(item) + " · " + item.kind;
    $("trajectory-detail-title").textContent = item.name || item.kind || "事件详情";
    document.querySelectorAll("#trajectory-detail-tabs [data-detail-tab]").forEach((tab) => {
      tab.setAttribute("aria-selected", tab.dataset.detailTab === trajectoryDetailTab ? "true" : "false");
    });
    trajectoryDetailContentEl.replaceChildren();
    const args = item.meta && item.meta.arguments ? pipelineJson(item.meta.arguments) : "";
    const execution = item.meta && item.meta.execution_duration_ms != null
      ? formatPipelineDuration(Number(item.meta.execution_duration_ms)) : "未单独记录";
    if (trajectoryDetailTab === "input") {
      trajectoryDetailContentEl.appendChild(createDetailSection(
        item.role === "tool" ? "Arguments" : "Input",
        item.role === "tool" ? args : item.role === "user" ? item.text : "该步骤没有独立输入文本",
        item.role === "tool",
      ));
    } else if (trajectoryDetailTab === "output") {
      trajectoryDetailContentEl.appendChild(createDetailSection(
        item.kind === "thinking" ? "Thinking" : "Output",
        item.text || (item.role === "tool" ? "工具尚未返回结果" : "该步骤没有输出文本"),
        item.role === "tool" || item.kind === "thinking",
      ));
    } else if (trajectoryDetailTab === "timing") {
      const timing = document.createElement("dl");
      timing.className = "trajectory-detail-timing";
      [["Status", item.status], ["Duration", formatPipelineDuration(pipelineDuration(item))],
        ["Execution", execution], ["Started", item.started_at || "未记录"],
        ["Completed", item.completed_at || (item.status === "running" ? "进行中" : "未记录")]].forEach(([label, value]) => {
        const dt = document.createElement("dt"); dt.textContent = label;
        const dd = document.createElement("dd"); dd.textContent = value;
        if (label === "Duration") dd.dataset.durationFor = item.item_id;
        timing.append(dt, dd);
      });
      trajectoryDetailContentEl.appendChild(timing);
    } else {
      trajectoryDetailContentEl.append(
        createDetailSection("Step", pipelineRoleLabel(item) + " / " + (item.name || item.kind)),
        createDetailSection("Status", item.status === "running" ? "运行中" : item.status),
        createDetailSection("Preview", pipelinePreview(item) || "无内容"),
      );
    }
  }

  function renderTrajectoryOverview(state, turns) {
    if (!trajectoryOverviewTrackEl) return;
    trajectoryOverviewTrackEl.replaceChildren();
    const items = (state?.order || []).map((id) => state.items.get(id)).filter(Boolean);
    if (!items.length) {
      const empty = document.createElement("span");
      empty.className = "trajectory-overview-empty";
      empty.textContent = "完成一次运行后，这里会显示真实步骤的时间跨度";
      trajectoryOverviewTrackEl.appendChild(empty);
      return;
    }
    const spans = items.map(pipelineSpan);
    const valid = spans.filter(Boolean);
    const start = valid.length ? Math.min(...valid.map((span) => span.start)) : 0;
    const end = valid.length ? Math.max(...valid.map((span) => span.end)) : 1;
    const total = Math.max(1, end - start);
    const count = items.length;
    const turnStarts = new Set();
    turns.forEach((turn) => { if (turn.items[0]) turnStarts.add(turn.items[0].item_id); });
    items.forEach((item, index) => {
      const span = spans[index];
      let left = index / count * 100;
      let width = Math.max(0.55, 100 / count - 0.8);
      if (trajectoryUseDuration && span) {
        left = (span.start - start) / total * 100;
        width = Math.max(0.55, (span.end - span.start) / total * 100);
      }
      if (turnStarts.has(item.item_id)) {
        const boundary = document.createElement("span");
        boundary.className = "trajectory-overview-boundary";
        boundary.style.left = left + "%";
        boundary.title = "新的 turn";
        trajectoryOverviewTrackEl.appendChild(boundary);
      }
      const bar = document.createElement("button");
      bar.type = "button";
      bar.className = "trajectory-overview-span";
      bar.dataset.role = item.role;
      bar.dataset.kind = item.kind;
      bar.dataset.status = item.status;
      bar.dataset.selected = item.item_id === trajectorySelectedItemId ? "true" : "false";
      bar.style.left = left + "%";
      bar.style.width = width + "%";
      bar.style.top = (item.role === "user" ? 7 : item.role === "tool" ? 35 : 21) + "px";
      bar.title = pipelineRoleLabel(item) + " · " + formatPipelineDuration(pipelineDuration(item));
      bar.setAttribute("aria-label", bar.title);
      bar.addEventListener("click", () => {
        trajectorySelectedItemId = item.item_id;
        trajectoryDetailTab = "summary";
        renderPipeline();
        const row = pipelineItemsEl.querySelector("[data-item-id='" + CSS.escape(item.item_id) + "']");
        row?.scrollIntoView({ block: "center" });
      });
      trajectoryOverviewTrackEl.appendChild(bar);
    });
  }

   // "live" | "progress"

   // {label, status, key}

  const orchChatBubbles = new Map();
  const orchChatLiveKeys = new Map();

  function orchChatStem(event) {
    const conversationId = String(event.conversation_id || activeConversationId());
    const runId = String(event.run_id || "");
    const nodeId = String(event.node_id || "").trim();
    const role = String(event.role || "").trim();
    const id = nodeId || role || "stage";
    return conversationId + "::" + runId + "::" + Number(event.attempt || 0) + "::" + id + "::" + String(event.member_id || "");
  }

  function orchChatKey(event) {
    if (event.type === 'recovery_case' && event.case?.case_id) {
      // A case survives node retries and must never share the node output body.
      return [event.conversation_id || activeConversationId(), event.run_id,
        'case', event.case.case_id].join('::');
    }
    const stem = orchChatStem(event);
    if (event.recovery_decision) {
      if (event.recovery_decision.boundary_type === "continuation"
          || event.recovery_decision.source === "host_progress_checkpoint"
          || event.recovery_decision.presentation?.kind === "continuation") return stem + "::continuation";
      return stem + "::recovery::" + String(event.recovery_decision.decision_id || event.seq || "decision");
    }
    const type = String(event.type || "");
    const nodeAttempt = Number(event.node_attempt || 0);
    const starting = type === "expert_node_started" ||
      type === "expert_member_started" ||
      type === "expert_pipeline_started";
    if (starting) {
      const gen = nodeAttempt > 0
        ? nodeAttempt
        : (Number(orchChatLiveKeys.get(stem + ":gen") || 0) + 1);
      orchChatLiveKeys.set(stem + ":gen", gen);
      const key = stem + "::" + gen;
      orchChatLiveKeys.set(stem, key);
      return key;
    }
    // An explicit call identity also applies to history replay. A snapshot of
    // an older call must not overwrite the latest live call's bubble.
    if (nodeAttempt > 0) return stem + "::" + nodeAttempt;
    return orchChatLiveKeys.get(stem) || (stem + "::1");
  }

  function ensureOrchChatBubble(event) {
    const key = orchChatKey(event);
    let entry = orchChatBubbles.get(key);
    if (entry && entry.msg.isConnected) return entry;
    hideEmptyState();
    const msg = document.createElement("div");
    msg.className = "msg assistant orch-stage-msg";
    const head = document.createElement("div");
    head.className = "orch-stage-msg-head";
    const label = event.label || event.name || event.node_id || event.role || "阶段";
    const model = event.model || "";
    head.innerHTML = "<strong></strong><span class='orch-stage-msg-model'></span>";
    head.querySelector("strong").textContent = String(label);
    head.querySelector(".orch-stage-msg-model").textContent = model ? String(model) : "";
    const bubble = document.createElement("div");
    bubble.className = "msg-bubble";
    const thinkEl = document.createElement("div");
    thinkEl.className = "thinking hidden";
    const bodyEl = document.createElement("div");
    bodyEl.className = "md";
    const activityEl = document.createElement("div");
    activityEl.className = "orch-stream-activity hidden";
    activityEl.setAttribute("role", "status");
    const progressEl = document.createElement('ol');
    progressEl.className = 'orch-progress-trail hidden';
    progressEl.setAttribute('aria-label', '执行进展');
    bubble.append(head, activityEl, progressEl, thinkEl, bodyEl);
    msg.appendChild(bubble);
    if (event.run_id && event.node_id) {
      const details = document.createElement("button");
      details.type = "button";
      details.className = "orch-chat-details";
      details.textContent = "调用详情";
      details.addEventListener("click", async () => {
        const state = expertTimelines.get(String(event.run_id));
        if (!state) return;
        details.disabled = true;
        details.textContent = "加载中…";
        const key = expertStageIdentity(state, event).key;
        if (!state.cards.has(key)) ensureExpertStageV2(state, event);
        await loadExpertRoleDetails(state, key);
        details.disabled = false;
        details.textContent = state.detailsLoaded.has(key) ? "调用详情" : "加载失败 · 重试";
        const group = bubble.querySelector(".tool-group");
        if (group) group.open = true;
      });
      head.appendChild(details);
    }
    messagesEl.appendChild(msg);
    entry = { msg, bodyEl, thinkEl, head, bubble, activityEl, text: "", thinking: "", label: String(label),
      runId: String(event.run_id || ""), attempt: Number(event.attempt || 0), activity: {},
      progressEl, progress: new Map() };
    orchChatBubbles.set(key, entry);
    scrollBottom();
    return entry;
  }

  function flushOrchChatText(entry) {
    clearTimeout(entry.textTimer);
    entry.textTimer = null;
    if (!entry.msg.isConnected) return;
    entry.bodyEl.innerHTML = renderMarkdown(entry.text);
    enhanceMath(entry.bodyEl);
    scrollBottom();
  }

  function scheduleOrchChatText(entry, immediate = false) {
    if (immediate) flushOrchChatText(entry);
    else if (!entry.textTimer) entry.textTimer = setTimeout(() => flushOrchChatText(entry), 50);
  }

  function renderOrchChatActivity(entry, now = Date.now() / 1000) {
    const state = entry.activity;
    const labels = {waiting: "等待模型响应", processing: "模型正在处理", generating: "正在生成正文",
      tool_pending: "准备调用工具", tool_running: "正在执行工具", waiting_permission: "等待授权", waiting_user: "等待你的回答",
      running: "正在执行", completed: "已完成", failed: "执行失败", paused: "已暂停",
      blocked: "等待处理", needs_attention: "需要处理", cancelled: "已取消", terminated: "已终止",
      escalated: "等待协助", budget_exhausted: "预算已用尽", interrupted: "已中断", skipped: "已跳过",
      stalled: "进展停滞", incomplete_audit: "审查未完成"};
    const parts = [labels[state.phase] || (state.active ? "正在执行" : "已停止")];
    if (state.active && state.startedAt > 0) parts.push("已运行 " + Math.max(0, Math.floor(now - state.startedAt)) + " 秒");
    if (state.active && state.updatedAt > 0) parts.push("最近活动 " + Math.max(0, Math.floor(now - state.updatedAt)) + " 秒前");
    entry.activityEl.textContent = parts.join(" · ");
    entry.activityEl.classList.toggle("hidden", !state.phase || (!state.active && state.phase === "completed" && !!entry.text));
    entry.activityEl.dataset.streaming = state.active ? "true" : "false";
  }

  function updateOrchChatActivity(entry, event) {
    const type = String(event.type || "");
    const stage = type === "expert_stage" || type.endsWith("_started") || type.endsWith("_completed");
    const tool = type.includes("_tool");
    const terminal = new Set(["completed", "failed", "paused", "blocked", "needs_attention", "cancelled",
      "terminated", "escalated", "budget_exhausted", "interrupted", "skipped", "stalled", "incomplete_audit"]);
    const state = entry.activity;
    if (stage && !tool && (terminal.has(event.status)
        || (type.endsWith("_completed") && event.status !== "running"))) {
      state.active = false;
      state.phase = event.status || "completed";
      entry.msg.dataset.status = state.phase;
    } else if (stage && !tool && (event.status === "running" || type.endsWith("_started"))) {
      state.active = true;
      state.phase = event.stream_activity?.phase || "running";
      state.startedAt = Number(event.started_at || state.startedAt || event.server_time || 0);
      state.updatedAt = Number(event.stream_activity?.updated_at || 0);
      entry.msg.dataset.status = "running";
    } else if (type === 'expert_tool_wait' && event.status === 'waiting_user' && state.active !== false) {
      state.phase = 'waiting_user';
    } else if (!event.history && !event.snapshot_aggregate && state.active !== false) {
      state.active = true;
      state.phase = event.stream_activity?.phase || (type.includes("thinking") ? "processing"
        : type.includes("delta") || type.endsWith("_output") ? "generating"
        : type === "expert_tool_wait" ? (event.status === "waiting_user" ? "waiting_user" : "waiting_permission")
        : type === "expert_tool_start" ? "tool_running"
        : type === "expert_tool_result" ? "waiting"
        : tool ? "tool_pending" : state.phase || "running");
      state.updatedAt = Number(event.stream_activity?.updated_at || event.server_time || Date.now() / 1000);
    }
    renderOrchChatActivity(entry);
  }

  function updateOrchProgress(entry, event) {
    const step = event.type === 'expert_step';
    const name = String(event.name || event.tool || '');
    if (!step && name === 'report_step') return; // Its public report arrives as expert_step.
    const identity = step ? 'step:' + String(event.step_id || event.label || event.seq)
      : 'tool:' + String(event.id || event.tool_id || '');
    if (identity === 'tool:') return;
    const previous = entry.progress.get(identity) || {};
    const args = event.arguments || event.args;
    const names = {ReadFile:'读取', ReadStageResult:'核对上游交付', ReadRunArchive:'核对历史回执',
      ReadAuditArtifact:'读取原始证据', ReadRecoveryContext:'读取恢复上下文',
      VerifyRecoveryCheckpoint:'核对检查点', AgentStatus:'核对执行状态', InspectGitState:'检查文件变更',
      Glob:'查找文件', Grep:'检索', WriteFile:'写入', EditFile:'修改', Bash:'执行命令',
      InspectVisualEvidence:'查看图像证据', SubmitHandoff:'提交本阶段结果',
      RequestAssistance:'请求协助', SubmitVerdict:'提交检查结论',
      AskUserQuestion:'等待你的选择', human_oncall:'等待你处理'};
    const label = step ? String(event.label || '进度更新') : previous.label ||
      toolLineSummary(name, args).replace(name, names[name] || name);
    const details = step ? [event.summary, event.work_object, event.purpose, event.findings, event.changes,
      event.verification, event.blocker, event.assistance_receipt?.notes, event.next_step && '下一步：' + event.next_step]
      .filter(Boolean).filter((text, i, all) => all.indexOf(text) === i).join('\n') || previous.details || '' : previous.details || '';
    const status = step ? event.status : event.is_error || event.status === 'failed' ? 'failed'
      : event.type === 'expert_tool_result' || event.status === 'completed' ? 'completed'
      : event.type === 'expert_tool_start' ? 'running' : event.status || 'pending';
    entry.progress.set(identity, {label, details, status: status || previous.status || 'running'});
    while (entry.progress.size > 6) entry.progress.delete(entry.progress.keys().next().value);
    entry.progressEl.replaceChildren(...[...entry.progress.values()].map(item => {
      const row = document.createElement('li'); row.dataset.status = item.status;
      const title = document.createElement('span'); title.textContent = item.label;
      const state = document.createElement('small');
      state.textContent = ({completed:'完成', failed:'失败', running:'进行中', pending:'准备中',
        waiting_user:'等你回答', waiting_permission:'等待授权', outcome_unknown:'结果待核对'})[item.status] || item.status;
      row.append(title, state);
      if (item.details) {const detail = document.createElement('p'); detail.textContent = item.details; row.append(detail);}
      return row;
    }));
    entry.progressEl.classList.remove('hidden');
  }

  const recoveryCases = new Map();
  function pendingRecoveryCases(runId) {
    return [...recoveryCases.values()].filter(event => event.run_id === runId
      && (!event.conversation_id || event.conversation_id === activeConversationId())
      && (['reported', 'awaiting_user'].includes(event.case.status) || event.case.handling?.owner === 'system')
      && !['resolved', 'superseded'].includes(event.case.status)
      && !event.archived && event.actionable !== false && !event.case.closed_by_user
      && ((event.case.pending && Object.keys(event.case.pending).length) || event.case.handling?.owner === 'system'));
  }

  function applyRecoveryCase(event) {
    if (!event.case?.case_id) return;
    if (event.conversation_id && event.conversation_id !== activeConversationId()) return;
    // A newly received case supersedes any earlier read-only suggestion for
    // this exact run.  Snapshot replay marks its synthetic events as history,
    // then restores the authoritative advice after all cases are projected.
    if (!event.history && typeof invalidateScopedResumeAdvice === "function") {
      invalidateScopedResumeAdvice(event);
    }
    const timeline = expertTimelines.get(event.run_id);
    if (timeline && event.case.handling && !event.archived) timeline.handling = event.case.handling;
    if (event.archived || event.actionable === false) recoveryCases.delete(orchChatKey(event));
    else recoveryCases.set(orchChatKey(event), event);
    if (!event.history) workbenchPrompt(event);

  }


  function mirrorExpertChatEvent(event, options = {}) {
    // Mode toggles and reconnect state may lag behind an already running task.
    // The event's conversation owns its output, not the current composer mode.
    if (!event) return;
    if (event.conversation_id && String(event.conversation_id) !== activeConversationId()) return;
    // Aliases from expert_web.emit carry legacy_type; primary event already mirrored.
    if (event.legacy_type) return;
    event = expertToolWaitState(event);
    const type = String(event.type || "");
    if (event.restored || event.reason === 'checkpoint_completed') return;
    if (type === 'expert_step') {
      if (!event.node_id) return;
      const entry = ensureOrchChatBubble(event);
      updateOrchProgress(entry, event);
      scrollBottom();
      return;
    }
    if (["expert_run_state", "expert_run_done", "expert_cancelled"].includes(type)) {
      const status = event.status || (type === "expert_cancelled" ? "cancelled" : "");
      if (status && !["running", "queued", "pending", "pausing"].includes(status)) {
        for (const entry of orchChatBubbles.values()) {
          if (entry.runId === String(event.run_id || "") && entry.activity.active
              && (!event.attempt || entry.attempt <= Number(event.attempt))) {
            updateOrchChatActivity(entry, {...event, type: "expert_node_completed", status});
          }
        }
      }
      return;
    }
    const stageLike = new Set([
      "expert_stage", "expert_node_started", "expert_node_completed",
      "expert_member_started", "expert_member_completed",
      "expert_pipeline_step_started", "expert_pipeline_step_completed",
    ]);
    const deltaLike = new Set([
      "expert_delta", "expert_node_delta", "expert_member_delta",
      "expert_pipeline_step_output",
    ]);
    const thinkLike = new Set([
      "expert_thinking", "expert_node_thinking", "expert_member_thinking",
      "expert_pipeline_step_thinking",
    ]);
    const toolLike = new Set([
      "expert_tool", "expert_tool_start", "expert_tool_result",
      "expert_tool_wait", "expert_tool_state",
      "expert_node_tool", "expert_member_tool", "expert_pipeline_step_tool",
    ]);

    if (stageLike.has(type) || deltaLike.has(type) || thinkLike.has(type) || toolLike.has(type) || type === "expert_activity") {
      const entry = ensureOrchChatBubble(event);
      updateOrchChatActivity(entry, event);
      if ((event.label || event.name) && !toolLike.has(type)) {
        entry.head.querySelector("strong").textContent = String(event.label || event.name);
      }
      if (event.model) {
        entry.head.querySelector(".orch-stage-msg-model").textContent = String(event.model);
      }
      if (stageLike.has(type) && event.status && (type.endsWith("_completed") || event.status === "completed" || event.status === "failed" || event.status === "needs_attention" || event.status === "paused")) {
        entry.msg.dataset.status = String(event.status);
      }
      // Stage/completion text is the full checkpoint, including final host
      // diagnostics. Replace the streamed view even when it already has text.
      if (
        typeof event.text === "string"
        && (type.endsWith("_completed") || type === "expert_stage")
      ) {
        entry.text = event.text;
        scheduleOrchChatText(entry, true);
        renderOrchChatActivity(entry);
        scrollBottom();
      }
    }
    if (deltaLike.has(type) && event.text) {
      const entry = ensureOrchChatBubble(event);
      entry.text = (event.snapshot_aggregate ? "" : entry.text) + String(event.text);
      scheduleOrchChatText(entry, event.history || event.snapshot_aggregate);
      scrollBottom();
    }
    if (thinkLike.has(type) && event.text) {
      const entry = ensureOrchChatBubble(event);
      entry.thinking = (event.snapshot_aggregate ? "" : entry.thinking) + String(event.text);
      entry.thinkEl.classList.remove("hidden");
      entry.thinkEl.textContent = entry.thinking;
      scrollBottom();
    }
    if (toolLike.has(type)) {
      const entry = ensureOrchChatBubble(event);
      updateOrchProgress(entry, event);
      const prevMsg = currentMsg;
      const prevGroup = currentToolGroup;
      currentMsg = entry.msg;
      currentToolGroup = entry.msg.querySelector(".msg-bubble > .tool-group:last-of-type") || null;
      try {
        const payload = {
          name: event.name || event.tool || "tool",
          tool_id: event.tool_id || event.id,
          arguments: event.arguments || event.args || (event.command ? { command: event.command } : {}),
          ...event,
        };
        payload.id = payload.id || payload.tool_id;
        if (payload.id) payload.id = orchChatStem(event) + "::" + Number(event.node_attempt || 0) + "::" + payload.id;
        payload.args = payload.args || payload.arguments || {};
        const isResult = type === "expert_tool_result" || type.endsWith("_result") || event.phase === "result"
          || ["completed", "failed"].includes(event.status);
        if (isResult) {
          if (payload.id && !toolRows.has(payload.id)) renderTool(payload, false);
          const row = toolRows.get(payload.id);
          // Lazy detail loads may repeat an already received live result.
          if (row?.dataset.expertResult !== "1") renderToolResult({
            ...payload,
            output: event.output || event.result || event.text || "",
            is_error: !!event.is_error || event.status === "failed",
            elapsed: event.elapsed,
          });
          if (row) { row.dataset.expertResult = "1"; delete row.dataset.expertState; }
        } else {
          if (!payload.id || !toolRows.has(payload.id)) renderTool(payload, false);
          const row = toolRows.get(payload.id);
          const stateLabel = ({waiting_permission: "等待授权", waiting_user: "等待你的回答", outcome_unknown: "结果未知",
            interrupted: "已中断", interrupted_before_execution: "执行前已中断", running: "执行中", queued: "排队"})[event.status]
            || (type === "expert_tool_wait" ? "等待授权" : type === "expert_tool_start" ? "执行中" : "排队");
          if (row && row.dataset.done !== "1") {
            row.querySelector(".tool-elapsed").textContent = stateLabel;
            row.dataset.expertState = stateLabel;
            if (["outcome_unknown", "interrupted", "interrupted_before_execution"].includes(event.status)) row.dataset.done = "1";
            const group = row.closest(".tool-group");
            if (group) {
              updateGroupTitle(group);
              group.querySelector(".tg-status").textContent = stateLabel;
            }
          }
        }
      } finally {
        currentMsg = prevMsg;
        currentToolGroup = prevGroup;
      }
      scrollBottom();
    }

  }

  function updateTrajectoryTabCount() {
    const countEl = $("trajectory-tab-count");
    const state = pipelineStates.get(activeConversationId());
    const count = state?.order.length || 0;
    countEl.textContent = String(count);
    countEl.classList.toggle("hidden", count === 0);
  }

  function renderPipeline() {
    updateTrajectoryTabCount();
    if (!pipelineItemsEl || conversationView !== "trajectory") return;
    const state = pipelineStates.get(activeConversationId());
    const previousHeight = pipelineItemsEl.scrollHeight;
    const previousTop = pipelineItemsEl.scrollTop;
    const focusedItem = pipelineItemsEl.contains(document.activeElement) ? document.activeElement?.dataset?.itemId : '';
    const followTail = !previousHeight || previousHeight - previousTop - pipelineItemsEl.clientHeight < 96;
    const turns = pipelineTurns(state);
    const counts = {};
    for (const id of state?.order || []) {
      const status = state.items.get(id)?.status || "unknown";
      counts[status] = (counts[status] || 0) + 1;
    }
    const summary = [["running", "运行中"], ["completed", "完成"], ["failed", "失败"], ["cancelled", "已取消"], ["interrupted", "已打断"], ["queued", "排队中"], ["blocked", "受阻"], ["waiting", "等待中"]]
      .filter(([status]) => counts[status]).map(([status, label]) => counts[status] + " " + label).join(" · ");
    const matchedTurns = turns.filter((turn) => turn.items.some(trajectoryMatches));
    $("pipeline-sub").textContent = state?.order.length
      ? state.order.length + " 个步骤" + (summary ? " · " + summary : " · 已记录")
      : "尚无执行事件";
    pipelineItemsEl.replaceChildren();
    if (!matchedTurns.length) {
      const empty = document.createElement("div");
      empty.className = "trajectory-empty";
      empty.textContent = trajectorySearchQuery ? "没有匹配的轨迹记录" : "当前对话还没有执行事件。执行开始后会实时显示步骤；写下计划不会自动启动团队。";
      pipelineItemsEl.appendChild(empty);
      renderTrajectoryOverview(state, turns);

      renderTrajectoryDetails(state);
      return;
    }
    let visibleIndex = 0;
    matchedTurns.forEach((turn, turnIndex) => {
      const wrapper = document.createElement("section");
      wrapper.className = "trajectory-turn";
      wrapper.dataset.turnId = turn.id;
      const collapsed = trajectoryCollapsedTurns.has(turn.id);
      const header = document.createElement("div");
      header.className = "trajectory-turn-header";
      header.setAttribute("role", "row");
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "trajectory-turn-toggle";
      toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
      toggle.innerHTML = "<svg class='ui-icon' aria-hidden='true'><use href='/static/icons.svg#chevron-down'></use></svg>";
      const title = document.createElement("strong");
      title.textContent = "Turn " + (turnIndex + 1);
      const meta = document.createElement("span");
      meta.textContent = turn.items.length + " steps";
      const duration = document.createElement("span");
      duration.className = "trajectory-turn-duration";
      duration.textContent = formatPipelineDuration(trajectoryTurnDuration(turn));
      header.append(toggle, title, meta, duration);
      const toggleTurn = () => {
        if (trajectoryCollapsedTurns.has(turn.id)) trajectoryCollapsedTurns.delete(turn.id);
        else trajectoryCollapsedTurns.add(turn.id);
        renderPipeline();
      };
      header.addEventListener("click", (event) => {
        if (event.target !== toggle && !toggle.contains(event.target)) toggleTurn();
      });
      toggle.addEventListener("click", toggleTurn);
      wrapper.appendChild(header);
      if (!collapsed) {
        for (const item of turn.items) {
          if (!trajectoryMatches(item)) continue;
          wrapper.appendChild(createTrajectoryRow(item, visibleIndex));
          visibleIndex += 1;
        }
      }
      pipelineItemsEl.appendChild(wrapper);
    });
    renderTrajectoryOverview(state, turns);

    renderTrajectoryDetails(state);
    if (followTail) pipelineItemsEl.scrollTop = pipelineItemsEl.scrollHeight;
    else pipelineItemsEl.scrollTop = previousTop;
    if (focusedItem) [...pipelineItemsEl.querySelectorAll('[data-item-id]')].find(row => row.dataset.itemId === focusedItem)?.focus({preventScroll: true});
  }

  function tickPipelineClocks() {
    if (!pipelineItemsEl || conversationView !== "trajectory") return;
    const state = pipelineStates.get(activeConversationId());
    if (!state) return;
    for (const row of pipelineItemsEl.querySelectorAll('.trajectory-row[data-status="running"]')) {
      const item = state.items.get(row.dataset.itemId);
      const time = row.querySelector(".trajectory-row-time");
      if (item?.status === "running" && time) time.textContent = formatPipelineDuration(pipelineDuration(item));
    }
    const turns = new Map(pipelineTurns(state).map(turn => [turn.id, turn]));
    for (const wrapper of pipelineItemsEl.querySelectorAll(".trajectory-turn")) {
      const turn = turns.get(wrapper.dataset.turnId);
      if (!turn?.items.some(item => item.status === "running")) continue;
      const time = wrapper.querySelector(".trajectory-turn-duration");
      if (time) time.textContent = formatPipelineDuration(trajectoryTurnDuration(turn));
    }
    if (trajectoryDetailTab === "timing") {
      const item = state.items.get(trajectorySelectedItemId);
      const time = trajectoryDetailContentEl.querySelector("[data-duration-for]");
      if (item?.status === "running" && time?.dataset.durationFor === item.item_id) {
        time.textContent = formatPipelineDuration(pipelineDuration(item));
      }
    }
  }

  function syncPipelineClock() {
    const state = pipelineStates.get(activeConversationId());
    const hasRunning = !!state && state.order.some((id) => state.items.get(id)?.status === "running");
    if (!hasRunning) {
      if (pipelineClockTimer) clearInterval(pipelineClockTimer);
      pipelineClockTimer = null;
      return;
    }
    if (!pipelineClockTimer) {
      // Clocks must preserve rows, focus and scroll; full rendering belongs to data changes.
      pipelineClockTimer = setInterval(tickPipelineClocks, 1000);
    }
  }

  function setConversationView(view, persist = true) {
    const change = () => updateConversationView(view, persist);
    if (window.MewChatViewport) window.MewChatViewport.preserve(change); else change();
  }

  function updateConversationView(view, persist = true) {
    conversationView = view === "trajectory" ? "trajectory" : "chat";
    if (persist) {
      try { localStorage.setItem("mewcode-conversation-view-v1", conversationView); } catch (_) {}
    }
    const trajectory = conversationView === "trajectory";
    messagesEl.classList.toggle("hidden", trajectory);
    trajectoryViewEl.classList.toggle("hidden", !trajectory);
    messagesEl.setAttribute("aria-hidden", trajectory ? "true" : "false");
    trajectoryViewEl.setAttribute("aria-hidden", trajectory ? "false" : "true");
    $("view-chat-tab").classList.toggle("is-active", !trajectory);
    $("view-trajectory-tab").classList.toggle("is-active", trajectory);
    $("view-chat-tab").setAttribute("aria-selected", trajectory ? "false" : "true");
    $("view-trajectory-tab").setAttribute("aria-selected", trajectory ? "true" : "false");
    document.body.dataset.conversationView = conversationView;
    cordis.emit("conversation.view.changed", { view: conversationView });
    if (trajectory) renderPipeline();
  }

  try { conversationView = localStorage.getItem("mewcode-conversation-view-v1") || "chat"; } catch (_) {}
  window.MewCurrentExecution = {
    open(step) {
      if (step?.conversation_id && step.conversation_id !== activeConversationId()) return false;
      if (step?.snapshot) {
        hydrateExpertTrajectory(step.snapshot);
        for (const event of step.snapshot.events || []) {
          if (!event.snapshot_aggregate || event.node_id !== step.node_id) continue;
          const type = event.type === 'expert_delta' ? 'expert_node_delta' :
            ['expert_tool_result', 'expert_tool_state', 'expert_tool_wait'].includes(event.type) ? 'expert_node_tool' : '';
          if (type) applyExpertTrajectoryEvent({...event, type,
            conversation_id: step.conversation_id, run_id: step.run_id,
            label: step.snapshot.locked_flow?.nodes?.find(node => node.node_id === step.node_id)?.label,
            status: event.status || step.snapshot.node_states?.[step.node_id]?.status,
          }, false);
        }
      }
      trajectorySearchQuery = "";
      if (trajectorySearchEl) trajectorySearchEl.value = "";
      trajectoryCollapsedTurns.clear();
      if (step) renderTrajectoryDetails(pipelineStates.get(activeConversationId()), step);
      setConversationView("trajectory");
      const row = step && [...pipelineItemsEl.querySelectorAll('[data-item-id]')].find(el => el.dataset.itemId === trajectorySelectedItemId);
      if (row) { row.scrollIntoView({block: 'center'}); row.focus(); }
      else $("view-trajectory-tab")?.focus();
      return !step || !!row;
    },
  };
  $("view-chat-tab")?.addEventListener("click", () => setConversationView("chat"));
  $("view-trajectory-tab")?.addEventListener("click", () => setConversationView("trajectory"));
  $("view-chat-tab")?.addEventListener("keydown", (event) => {
    if (event.key === "ArrowRight") { event.preventDefault(); $("view-trajectory-tab").focus(); setConversationView("trajectory"); }
  });
  $("view-trajectory-tab")?.addEventListener("keydown", (event) => {
    if (event.key === "ArrowLeft") { event.preventDefault(); $("view-chat-tab").focus(); setConversationView("chat"); }
  });
  $("trajectory-duration-toggle")?.addEventListener("click", () => {
    trajectoryUseDuration = !trajectoryUseDuration;
    $("trajectory-duration-toggle").classList.toggle("is-active", trajectoryUseDuration);
    $("trajectory-duration-toggle").setAttribute("aria-pressed", trajectoryUseDuration ? "true" : "false");
    renderPipeline();
  });
  $("trajectory-collapse-turns")?.addEventListener("click", () => {
    const turns = pipelineTurns(pipelineStates.get(activeConversationId()));
    const allCollapsed = turns.length > 0 && turns.every((turn) => trajectoryCollapsedTurns.has(turn.id));
    trajectoryCollapsedTurns.clear();
    if (!allCollapsed) turns.forEach((turn) => trajectoryCollapsedTurns.add(turn.id));
    $("trajectory-collapse-turns").setAttribute("aria-pressed", allCollapsed ? "false" : "true");
    renderPipeline();
  });
  trajectorySearchEl?.addEventListener("input", () => {
    trajectorySearchQuery = trajectorySearchEl.value;
    renderPipeline();
  });
  $("trajectory-details-close")?.addEventListener("click", () => {
    trajectorySelectedItemId = "";
    renderPipeline();
  });
  $("trajectory-detail-tabs")?.addEventListener("click", (event) => {
    const tab = event.target.closest("[data-detail-tab]");
    if (!tab) return;
    trajectoryDetailTab = tab.dataset.detailTab || "summary";
    renderPipeline();
  });
  setConversationView(conversationView, false);

  function hideSlashMenu() {
    slashMatches = [];
    slashSelectedIndex = 0;
    $("slash-menu").classList.add("hidden");
    $("slash-menu").replaceChildren();
  }

  function selectSlashCommand(command) {
    inputEl.value = "/" + command.name + " ";
    inputEl.dispatchEvent(new Event("input"));
    hideSlashMenu();
    inputEl.focus();
  }

  function renderSlashMenu() {
    const raw = inputEl.value;
    const match = raw.match(/^\/([^\s]*)$/);
    if (!match) {
      hideSlashMenu();
      return;
    }
    const query = match[1].toLowerCase();
    slashMatches = slashCommands.filter((command) =>
      command.name.startsWith(query) ||
      (command.aliases || []).some((alias) => alias.startsWith(query))
    );
    if (!slashMatches.length) {
      hideSlashMenu();
      return;
    }
    slashSelectedIndex = Math.min(slashSelectedIndex, slashMatches.length - 1);
    const menu = $("slash-menu");
    menu.replaceChildren();
    slashMatches.forEach((command, index) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "slash-command" + (index === slashSelectedIndex ? " is-selected" : "");
      button.setAttribute("role", "option");
      button.setAttribute("aria-selected", index === slashSelectedIndex ? "true" : "false");
      button.innerHTML = "<strong>" + esc(command.usage) + "</strong><span>" +
        esc(command.description) + "</span>";
      button.addEventListener("mousedown", (event) => event.preventDefault());
      button.addEventListener("click", () => selectSlashCommand(command));
      menu.appendChild(button);
    });
    menu.classList.remove("hidden");
  }

  function parseSlashCommand(text) {
    const match = String(text || "").trim().match(/^\/([^\s]+)(?:\s+([\s\S]*))?$/);
    return match ? { name: match[1], args: match[2] || "" } : null;
  }

  function activateConversation(id, running = false) {
    window.MewChatViewport?.select(id);
    currentStatus.conversation_id = String(id || "");
    currentStatus.conversation_title = "新对话";
    if (running) runningConversations.add(currentStatus.conversation_id);
    trajectorySelectedItemId = "";
    trajectoryCollapsedTurns.clear();
    orchChatBubbles.clear();
    orchChatLiveKeys.clear();
    recoveryCases.clear();
    resetLiveMessageState();
    syncActiveStreaming();
    emitConversationContext();
    renderPipeline();

  }

  const bufferedStreamTypes = new Set([
    "turn_started", "delta", "thinking", "tool", "tool_result", "pipeline_item", "usage", "note", "error",
    "expert_stage", "expert_delta", "expert_thinking", "expert_activity", "expert_step", "expert_tool",
    "expert_tool_start", "expert_tool_wait", "expert_tool_result", "expert_tool_state",
    "expert_role_wait", "expert_usage", "expert_child", "expert_child_event",
    "expert_run_state", "expert_run_done", "expert_flow_defined", "recovery_case",
    "expert_node_started", "expert_node_completed", "expert_node_delta",
    "expert_node_thinking", "expert_node_tool", "expert_member_started",
    "expert_member_completed", "expert_member_delta", "expert_member_thinking",
    "expert_member_tool", "expert_join_started", "expert_join_completed",
    "expert_handoff", "expert_flow_state",
    "expert_takeover_state",
  ]);

  function rememberConversationEvent(message) {
    const id = String(message.conversation_id || "");
    if (!id || !bufferedStreamTypes.has(message.type)) return;
    if (message.run_id && message.seq != null) {
      const key = id + ":" + message.run_id;
      const previous = Number(expertLastSeq.get(key) || 0);
      if (Number(message.seq) <= previous) return;
      expertLastSeq.set(key, Number(message.seq));
    }
    const events = conversationEventBuffers.get(id) || [];
    events.push(message);
    if (events.length > 2000) events.splice(0, events.length - 2000);
    conversationEventBuffers.set(id, events);
  }

  function replayConversationEvents(conversationId) {
    const events = conversationEventBuffers.get(String(conversationId || "")) || [];

    for (const message of events) {
      if (message.run_id && message.seq != null) {
        const snapshotSeq = Number(expertSnapshotSeq.get(
          String(conversationId || "") + ":" + message.run_id
        ) || 0);
        if (Number(message.seq) <= snapshotSeq) continue;
      }
      if (expertTrajectoryTypes.has(message.type)) applyExpertTrajectoryEvent(message);
      if (message.type === "turn_started") {
        rememberBackendTurnScope(message, conversationId);
        if (message.text || (message.attachments || []).length) {
          addUserMsg(message.text || "", message.attachments || []);
        }
        if (!message.expert_mode && !currentMsg) beginAssistant();
      } else if (message.type === "delta") appendDelta(message.text);
      else if (message.type === "thinking") appendThinking(message.text);
      else if (message.type === "tool") renderTool(message);
      else if (message.type === "tool_result") renderToolResult(message);
      else if (message.type === "pipeline_item") applyPipelineEvent(message);
      else if (message.type === "usage") updateUsage(message);
      else if (message.type === "note") {
        if (!isToolDenialNote(message.text)) addSystemMsg(message.text);
      }
      else if (message.type === "error") {
        if (message.error_code === "recovery_resume_blocked") {
          renderBlockedResumeError(message, conversationId);
        } else {
          displayBackendError(message, conversationId);
        }
      }
      else if (["expert_stage", "expert_node_started", "expert_node_completed"].includes(message.type)) renderExpertStageV2(message);
      else if (message.type === "recovery_case") applyRecoveryCase(message);
      else if (message.type === "expert_delta") appendExpertDeltaV2(message);
      else if (message.type === "expert_thinking") appendExpertThinkingV2(message);
      else if (message.type === "expert_tool") renderExpertToolV2(message);
      else if (message.type === "expert_tool_start") renderExpertToolV2(message, "start");
      else if (message.type === "expert_tool_wait") renderExpertToolV2(message, "wait");
      else if (message.type === "expert_tool_result") renderExpertToolV2(message, "result");
      else if (message.type === "expert_tool_state") renderExpertToolV2(message, "state");
      else if (message.type === "expert_role_wait") renderExpertRoleWaitV2(message);
      else if (message.type === "expert_usage") renderExpertUsageV2(message);
      else if (message.type === "expert_child") renderExpertChildV2(message);
      else if (message.type === "expert_child_event") renderExpertChildEventV2(message);
      else if (message.type === "expert_takeover_state" && message.text) addSystemMsg(message.text);
      else if (message.type === "expert_run_state" || message.type === "expert_run_done") {
        renderExpertControlsV2(message.status, message.run_id, message.reason || message.status_reason || "", message.attempt);
      }
      if (
        String(message.type || "").startsWith("expert_")
        && message.type !== "expert_flow_defined"
      ) {
        mirrorExpertChatEvent(message);
      }
    }
  }

  // ---------- 工具函数 ----------

  function esc(s) {
    const d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }

  function notify(message, type = "info", options) {
    if (window.MewFeedback && typeof window.MewFeedback.toast === "function") {
      return window.MewFeedback.toast(message, type, options);
    }
    if (type === "error") addErrorMsg(message);
    else addSystemMsg(message);
    return null;
  }

  function askConfirm(options) {
    if (window.MewFeedback && typeof window.MewFeedback.confirm === "function") {
      return window.MewFeedback.confirm(options);
    }
    return Promise.resolve(window.confirm((options && options.message) || "请确认操作"));
  }
  function renderMarkdown(text) {
    if (window.MewMarkdown && typeof window.MewMarkdown.renderMarkdown === "function") {
      return window.MewMarkdown.renderMarkdown(text);
    }
    if (window.marked) {
      marked.setOptions({ breaks: true, gfm: true });
      const html = marked.parse(text);
      if (window.DOMPurify) return DOMPurify.sanitize(html);
    }
    return "<pre style='white-space:pre-wrap'>" + esc(text) + "</pre>";
  }

  function enhanceMath(root) {
    if (window.MewMarkdown && typeof window.MewMarkdown.enhanceMath === "function") {
      window.MewMarkdown.enhanceMath(root);
    }
  }

  function scrollBottom(force = false) {
    window.MewChatViewport?.scrollToEnd(force);
  }

  function fmtInt(n) {
    return (n || 0).toLocaleString("en-US");
  }

  function formatFileSize(bytes) {
    const value = Math.max(Number(bytes) || 0, 0);
    if (value < 1024) return value + " B";
    if (value < 1024 * 1024) return (value / 1024).toFixed(value < 10240 ? 1 : 0) + " KiB";
    return (value / 1024 / 1024).toFixed(1) + " MiB";
  }

  function fmtTime(iso) {
    if (!iso) return "";
    try {
      const d = new Date(iso);
      if (Number.isNaN(d.getTime())) return "";
      const now = new Date();
      return d.toDateString() === now.toDateString()
        ? d.toTimeString().slice(0, 5)
        : d.getMonth() + 1 + "/" + d.getDate();
    } catch {
      return "";
    }
  }

  function uiStateBucketKey() {
    return String(
      (sidebarData && sidebarData.work_dir) ||
      currentStatus.work_dir ||
      "__default__"
    );
  }

  function readUiStateStore() {
    try {
      const saved = JSON.parse(localStorage.getItem(UI_STATE_KEY) || "{}");
      return saved && typeof saved === "object" && !Array.isArray(saved) ? saved : {};
    } catch {
      return {};
    }
  }

  function writeUiStateStore(store) {
    try {
      localStorage.setItem(UI_STATE_KEY, JSON.stringify(store || {}));
    } catch {
      // 隐私模式或存储空间受限时，当前页面内仍能保留状态。
    }
  }

  function persistUiState() {
    const store = readUiStateStore();
    store[uiStateBucketKey()] = {
      workspaceOpen: { ...workspaceOpenState },
      projectOpen: { ...projectOpenState },
      familyKey: rememberedFamilyKey || "",
    };
    writeUiStateStore(store);
    try {
      // Keep legacy key in sync for older builds / partial migrations.
      localStorage.setItem(PROJECT_OPEN_STATE_KEY, JSON.stringify(projectOpenState));
    } catch {
      /* ignore */
    }
  }

  function hydrateUiStateFromStorage() {
    const bucket = readUiStateStore()[uiStateBucketKey()] || {};
    workspaceOpenState =
      bucket.workspaceOpen && typeof bucket.workspaceOpen === "object"
        ? { ...bucket.workspaceOpen }
        : {};
    if (bucket.projectOpen && typeof bucket.projectOpen === "object") {
      projectOpenState = { ...bucket.projectOpen };
    } else {
      try {
        const legacy = JSON.parse(localStorage.getItem(PROJECT_OPEN_STATE_KEY) || "{}");
        projectOpenState =
          legacy && typeof legacy === "object" && !Array.isArray(legacy) ? legacy : {};
      } catch {
        projectOpenState = {};
      }
    }
    rememberedFamilyKey = String(bucket.familyKey || "");
  }

  function rememberProjectOpen(dir, open) {
    projectOpenState[String(dir || "")] = !!open;
    persistUiState();
  }

  function rememberWorkspaceOpen(key, open) {
    workspaceOpenState[String(key || "")] = !!open;
    persistUiState();
  }

  function rememberRuntimeFamily(familyKey) {
    rememberedFamilyKey = String(familyKey || "");
    persistUiState();
  }

  function groupOpenDefault(hasStored, storedOpen, onActivePath) {
    if (hasStored) return !!storedOpen;
    return !!onActivePath;
  }

  function hideEmptyState() {
    const empty = $("chat-empty-state");
    if (empty) empty.remove();
  }

  function renderEmptyState() {
    if (messagesEl.querySelector(".msg, .session-summary, .expert-timeline")) {
      hideEmptyState();
      return;
    }
    hideEmptyState();
    const activeProject = (sidebarData.projects || []).find((project) => project.active);
    const projectName = currentStatus.project_name || (activeProject && activeProject.label) || "当前项目";
    const recentProjects = (sidebarData.projects || [])
      .filter((project) => !project.active)
      .slice(0, 2);
    const section = document.createElement("section");
    section.id = "chat-empty-state";
    section.className = "chat-empty-state";
    section.setAttribute("aria-label", "快速开始");
    section.innerHTML =
      "<div class='empty-state-mark' aria-hidden='true'>M</div>" +
      "<p class='empty-state-eyebrow'>" + esc(projectName) + "</p>" +
      "<h1>今天想完成什么？</h1>" +
      "<p class='empty-state-copy'>描述目标、贴出代码，或直接告诉我从哪里开始。</p>";
    const prompts = document.createElement("div");
    prompts.className = "empty-state-prompts";
    const suggestions = [
      ["file-code", "了解这个项目", "先看清结构、关键模块和可改进之处"],
      ["wrench", "修复一个问题", "定位根因，并给出可以验证的修复"],
      ["sparkles", "实现一个功能", "从设计到实现，保持与现有架构一致"],
      ["expert", "规划复杂任务", "拆解目标、执行变更，并复核结果"],
    ];
    for (const [icon, label, prompt] of suggestions) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "empty-prompt";
      button.innerHTML = "<svg class='ui-icon' aria-hidden='true'><use href='/static/icons.svg#" +
        icon + "'></use></svg><span><strong>" + esc(label) + "</strong><small>" + esc(prompt) + "</small></span>";
      button.addEventListener("click", () => {
        inputEl.value = prompt;
        inputEl.dispatchEvent(new Event("input"));
        inputEl.focus();
      });
      prompts.appendChild(button);
    }
    section.appendChild(prompts);
    if (recentProjects.length) {
      const recent = document.createElement("div");
      recent.className = "empty-state-recent";
      const label = document.createElement("span");
      label.textContent = "最近项目";
      recent.appendChild(label);
      for (const project of recentProjects) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = project.label;
        button.title = project.dir;
        button.addEventListener("click", async () => {
          const response = await fetch("/api/projects/activate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ dir: project.dir }),
          });
          const result = await response.json();
          if (!response.ok || result.error) {
            notify(result.error || "切换项目失败", "error");
            return;
          }
          messagesEl.innerHTML = "";
          await Promise.all([loadSidebar(), refreshState()]);
          renderEmptyState();
          inputEl.focus();
        });
        recent.appendChild(button);
      }
      section.appendChild(recent);
    }
    messagesEl.appendChild(section);
  }

  function cacheUsage(status) {
    const hasSlotMetrics = status.cache_cumulative_usage_available != null;
    const cacheRead = Math.max(Number(
      hasSlotMetrics ? status.cache_cumulative_read : status.cache_read
    ) || 0, 0);
    const cacheCreation = Math.max(Number(
      hasSlotMetrics ? status.cache_cumulative_creation : status.cache_creation
    ) || 0, 0);
    const uncachedInput = Math.max(Number(
      hasSlotMetrics ? status.cache_cumulative_miss : status.total_in
    ) || 0, 0);
    const calculatedPrompt = uncachedInput + cacheRead + cacheCreation;
    const promptTokens = Math.max(
      hasSlotMetrics ? calculatedPrompt : Number(status.prompt_tokens) || calculatedPrompt,
      0
    );
    const epoch = Math.max(Number(status.cache_epoch) || 0, 0);
    // A cache slot reports availability explicitly.  Treat a reported false as
    // authoritative so counters from the previously selected provider cannot
    // leak into the new provider's badge after a model switch.
    const available = hasSlotMetrics
      ? status.cache_cumulative_usage_available === true ||
        status.cache_cumulative_usage_available === 1
      : status.cache_usage_available === true ||
        status.cache_usage_available === 1 || cacheRead > 0 || cacheCreation > 0;
    const reportedRate = available && status.cache_cumulative_hit_rate != null
      ? Number(status.cache_cumulative_hit_rate)
      : status.cache_hit_rate != null
        ? Number(status.cache_hit_rate)
        : NaN;
    const rate = available && Number.isFinite(reportedRate)
      ? Math.min(Math.max(reportedRate, 0), 1)
      : (promptTokens > 0 ? Math.min(cacheRead / promptTokens, 1) : 0);
    const requestRead = Math.max(Number(status.cache_request_read) || 0, 0);
    const requestMiss = Math.max(Number(status.cache_request_miss) || 0, 0);
    const requestTotal = requestRead + requestMiss;
    const requestAvailable = status.cache_request_usage_available === true ||
      status.cache_request_usage_available === 1 || requestRead > 0 || requestMiss > 0;
    const reportedRequestRate = requestAvailable && status.cache_request_hit_rate != null
      ? Number(status.cache_request_hit_rate)
      : NaN;
    const requestRate = Number.isFinite(reportedRequestRate)
      ? Math.min(Math.max(reportedRequestRate, 0), 1)
      : (requestTotal > 0 ? requestRead / requestTotal : 0);
    return {
      cacheRead, cacheCreation, promptTokens, rate, available, epoch,
      requestRead, requestMiss, requestRate, requestAvailable,
      cumulativeRead: Math.max(
        epoch > 0 && status.cache_cumulative_read != null
          ? Number(status.cache_cumulative_read) || 0
          : cacheRead,
        0
      ),
      cumulativeMiss: Math.max(
        epoch > 0 && status.cache_cumulative_miss != null
          ? Number(status.cache_cumulative_miss) || 0
          : uncachedInput,
        0
      ),
      miss: Math.max(
        status.cache_miss != null ? Number(status.cache_miss) || 0 : uncachedInput,
        0
      ),
      requestCostUsd: Math.max(Number(status.cache_request_cost_usd) || 0, 0),
      costUsd: Math.max(Number(status.cache_cost_usd) || 0, 0),
      reason: String(status.cache_invalidation_reason || ""),
      lcpTokens: Math.max(Number(status.cache_lcp_tokens) || 0, 0),
    };
  }

  function formatCacheRate(rate) {
    const percent = Math.min(Math.max(Number(rate) || 0, 0), 1) * 100;
    if (percent === 0) return "0%";
    if (percent < 0.1) return "<0.1%";
    if (percent < 10) return percent.toFixed(1).replace(/\.0$/, "") + "%";
    return Math.round(percent) + "%";
  }

  function updateContextBadge(status) {
    const parts = [];
    let pct = null;
    if (status.window > 0 && status.ctx > 0) {
      pct = Math.min(Math.round((status.ctx / status.window) * 100), 100);
      parts.push("ctx " + pct + "% (" + fmtInt(status.ctx) + "/" + fmtInt(status.window) + ")");
    } else if (status.window > 0) {
      parts.push("ctx --/" + fmtInt(status.window));
    } else {
      parts.push("ctx --");
    }
    const detectedWindow = Math.max(Number(status.detected_window) || 0, 0);
    const manualCap = Number(status.context_tier) > 0 &&
      Number(status.window) > 0 && detectedWindow > Number(status.window);
    if (manualCap) {
      parts.push("手动限额 · 模型上限 " + fmtInt(detectedWindow));
    }
    const cache = cacheUsage(status);
    if (cache.available) {
      parts.push(cache.epoch > 0
        ? "cache e" + cache.epoch + " now " +
          (cache.requestAvailable ? formatCacheRate(cache.requestRate) : "--") +
          " / total " + formatCacheRate(cache.rate)
        : "cache " + formatCacheRate(cache.rate));
      ctxBadge.title = cache.epoch > 0 && cache.requestAvailable
        ? "本次 hit " + fmtInt(cache.requestRead) + " / miss " + fmtInt(cache.requestMiss) +
          " · 累计 hit " + fmtInt(cache.cumulativeRead) + " / miss " + fmtInt(cache.cumulativeMiss) +
          " · 本次 $" + cache.requestCostUsd.toFixed(6) +
          " / 累计 $" + cache.costUsd.toFixed(6) +
          (cache.reason ? " · " + cache.reason : "") +
          (cache.lcpTokens > 0 ? " · LCP " + fmtInt(cache.lcpTokens) + " tokens" : "")
        : "缓存读取 " + fmtInt(cache.cacheRead) + " / prompt " +
          fmtInt(cache.promptTokens) + " tokens" +
          (cache.cacheCreation > 0
            ? " · 缓存写入 " + fmtInt(cache.cacheCreation)
            : "");
    } else {
      ctxBadge.title = "当前 provider 尚未返回缓存用量";
    }
    if (!cache.available) {
      parts.push(cache.epoch > 0
        ? "cache e" + cache.epoch + " 未上报"
        : "cache --");
    }
    ctxBadge.textContent = parts.join(" · ");

    const ring = $("composer-ctx-ring");
    const ringPct = $("composer-ctx-pct");
    if (ring && ringPct) {
      const shown = pct == null ? 0 : pct;
      ring.style.setProperty("--ctx-pct", String(shown));
      ringPct.textContent = pct == null ? "--" : (pct + "%");
      const detail = status.window > 0
        ? "上下文 " + (pct == null ? "--" : pct + "%") +
          " · " + fmtInt(status.ctx || 0) + " / " + fmtInt(status.window)
        : "上下文占用尚未上报";
      ring.title = detail + (manualCap
        ? "\n当前使用手动限额，模型完整窗口 " + fmtInt(detectedWindow)
        : "") + (ctxBadge.title ? "\n" + ctxBadge.title : "");
      ring.classList.toggle("is-hot", pct != null && pct >= 85);
    }
  }

  // ---------- 主题 ----------

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    $("theme-icon-use").setAttribute(
      "href",
      theme === "dark" ? "/static/icons.svg#sun" : "/static/icons.svg#moon"
    );
    $("theme-toggle").setAttribute(
      "aria-label",
      theme === "dark" ? "切换到浅色主题" : "切换到深色主题"
    );
    localStorage.setItem("mewcode-theme", theme);
    window.dispatchEvent(new CustomEvent('mewcode:theme', {detail: theme}));
    if (window.mewcodeDesktop && typeof window.mewcodeDesktop.setWindowTheme === "function") {
      window.mewcodeDesktop.setWindowTheme(theme).catch(() => {});
    }
  }
  $("theme-toggle").addEventListener("click", () => {
    const cur = document.documentElement.getAttribute("data-theme");
    applyTheme(cur === "dark" ? "light" : "dark");
  });
  const desktopTheme = new URLSearchParams(location.search).get("desktop_theme");
  applyTheme(
    localStorage.getItem("mewcode-theme") ||
    (desktopTheme === "light" ? "light" : desktopTheme === "dark" ? "dark" :
      (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"))
  );
  $("completion-link").addEventListener("click", () => {
    window.location.href = "/completion";
  });

  // ---------- 抽屉 ----------

  const drawerBackdrop = $("drawer-backdrop");
  const drawerMobileQuery = matchMedia("(max-width: 860px)");
  let drawerOpen = false;

  function isMobileDrawer() {
    return drawerMobileQuery.matches;
  }

  function setDrawer(open) {
    drawerOpen = !!open;
    drawer.classList.toggle("collapsed", !drawerOpen);
    drawer.classList.toggle("drawer-open", drawerOpen);
    drawer.setAttribute("aria-hidden", drawerOpen ? "false" : "true");
    $("drawer-toggle").setAttribute("aria-expanded", drawerOpen ? "true" : "false");
    $("drawer-toggle-2").setAttribute("aria-expanded", drawerOpen ? "true" : "false");
    if (drawerBackdrop) {
      drawerBackdrop.classList.toggle("hidden", !drawerOpen || !isMobileDrawer());
      drawerBackdrop.setAttribute("aria-hidden", drawerOpen && isMobileDrawer() ? "false" : "true");
    }
    localStorage.setItem("mewcode-drawer", open ? "1" : "0");
  }
  $("drawer-toggle").addEventListener("click", () => setDrawer(false));
  $("drawer-toggle-2").addEventListener("click", () =>
    setDrawer(!drawerOpen)
  );
  drawerBackdrop?.addEventListener("click", () => setDrawer(false));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && isMobileDrawer() && drawerOpen) {
      setDrawer(false);
      $("drawer-toggle-2").focus();
    }
  });
  drawerMobileQuery.addEventListener?.("change", () => {
    if (drawerOpen) setDrawer(true);
    else setDrawer(false);
  });
  setDrawer(localStorage.getItem("mewcode-drawer") !== "0");

  // ---------- 弹出菜单 ----------

  function positionPopover(anchor, placement = "down") {
    if (!anchor || !document.contains(anchor) || popover.classList.contains("hidden")) return;
    const rect = anchor.getBoundingClientRect();
    const pw = popover.offsetWidth;
    const ph = popover.offsetHeight;
    const margin = 8;
    const availableBelow = window.innerHeight - rect.bottom - margin;
    const availableAbove = rect.top - margin;
    const placeUp = placement === "up" || (
      placement !== "down" && availableBelow < ph && availableAbove > availableBelow
    );
    const heightLimit = popover.classList.contains("runtime-model-popover") ? 560 : 440;
    popover.style.maxHeight = Math.max(
      140, Math.min(heightLimit, placeUp ? availableAbove : availableBelow)
    ) + "px";
    popover.style.left =
      Math.max(8, Math.min(rect.left, window.innerWidth - pw - 12)) + "px";
    if (placeUp) {
      popover.style.top =
        Math.max(8, rect.top - Math.min(popover.offsetHeight, availableAbove) - 8) + "px";
    } else {
      popover.style.top = Math.min(
        rect.bottom + 6,
        window.innerHeight - Math.min(popover.offsetHeight, availableBelow) - 8
      ) + "px";
    }
  }

  function showPopover(anchor, build, placement = "down") {
    if (runtimeSettingsView && runtimeSettingsView.root === popover) {
      runtimeSettingsView = null;
      if (catalogRefreshTimer) {
        clearInterval(catalogRefreshTimer);
        catalogRefreshTimer = null;
      }
    }
    projectContextReturnFocus = null;
    popover.className = "";
    popover.removeAttribute("role");
    popover.removeAttribute("aria-label");
    popover.innerHTML = "";
    build(popover);
    popover.classList.remove("hidden");
    positionPopover(anchor, placement);
  }

  function hidePopover(restoreProjectFocus = true) {
    const returnFocus = restoreProjectFocus && popover.classList.contains("project-context-menu")
      ? projectContextReturnFocus
      : null;
    popover.classList.add("hidden");
    projectContextReturnFocus = null;
    runtimeSettingsView = null;
    if (catalogRefreshTimer) {
      clearInterval(catalogRefreshTimer);
      catalogRefreshTimer = null;
    }
    if (returnFocus && document.contains(returnFocus)) returnFocus.focus();
  }
  function showContextPopover(x, y, label, returnFocus, build) {
    if (runtimeSettingsView && runtimeSettingsView.root === popover) {
      runtimeSettingsView = null;
      if (catalogRefreshTimer) {
        clearInterval(catalogRefreshTimer);
        catalogRefreshTimer = null;
      }
    }
    projectContextReturnFocus = returnFocus || null;
    popover.className = "project-context-menu";
    popover.innerHTML = "";
    popover.setAttribute("role", "menu");
    popover.setAttribute("aria-label", label);
    build(popover);
    popover.classList.remove("hidden");
    const margin = 8;
    popover.style.maxHeight = Math.max(140, window.innerHeight - margin * 2) + "px";
    popover.style.left = Math.max(
      margin, Math.min(Number(x) || margin, window.innerWidth - popover.offsetWidth - margin)
    ) + "px";
    popover.style.top = Math.max(
      margin, Math.min(Number(y) || margin, window.innerHeight - popover.offsetHeight - margin)
    ) + "px";
    requestAnimationFrame(() => {
      const first = popover.querySelector("[role='menuitem']");
      if (first) first.focus();
    });
  }
  document.addEventListener("click", (e) => {
    if (!popover.contains(e.target) && !e.target.closest(".mf-click") &&
        !e.target.closest("#model-effort-btn") && !e.target.closest("#perm-mode-btn")) {
      hidePopover(false);
    }
    const link = e.target.closest && e.target.closest("a[href]");
    // Embedded app navigation may already be handled by its own launcher.
    // Relative app routes resolve to HTTP too, but must retain desktop cookies.
    if (!e.defaultPrevented && link && window.mewcodeDesktop && /^https?:\/\//i.test(link.href) &&
        new URL(link.href).origin !== window.location.origin) {
      e.preventDefault();
      window.mewcodeDesktop.openExternal(link.href);
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !popover.classList.contains("hidden")) {
      event.preventDefault();
      hidePopover();
    }
  });
  popover.addEventListener("keydown", (event) => {
    if (!popover.classList.contains("project-context-menu")) return;
    const items = Array.from(popover.querySelectorAll("[role='menuitem']"));
    if (!items.length) return;
    const current = Math.max(items.indexOf(document.activeElement), 0);
    let next = -1;
    if (event.key === "ArrowDown") next = (current + 1) % items.length;
    else if (event.key === "ArrowUp") next = (current - 1 + items.length) % items.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = items.length - 1;
    if (next >= 0) {
      event.preventDefault();
      items[next].focus();
    }
  });

  function popItem(label, current, onClick, desc) {
    const div = document.createElement("div");
    div.className = "pop-item" + (current ? " current" : "");
    div.innerHTML = esc(label) + (desc ? "<div class='pop-desc'>" + esc(desc) + "</div>" : "");
    div.addEventListener("click", () => { hidePopover(); onClick(); });
    return div;
  }

  function popTitle(text) {
    const t = document.createElement("div");
    t.className = "pop-title";
    t.textContent = text;
    return t;
  }

  function projectMenuItem(label, onClick, tone = "") {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "pop-item project-menu-item" + (tone ? " " + tone : "");
    button.setAttribute("role", "menuitem");
    button.textContent = label;
    button.addEventListener("click", () => {
      hidePopover();
      onClick();
    });
    return button;
  }

  function closeSettingsDialog() {
    if (settingsDialogCleanup) settingsDialogCleanup();
    settingsDialogCleanup = null;
    settingsOverlay.classList.add("hidden");
    settingsOverlay.setAttribute("aria-hidden", "true");
    settingsDialogBody.innerHTML = "";
    settingsDialogFooter.innerHTML = "";
    if (settingsReturnFocus && document.contains(settingsReturnFocus)) {
      settingsReturnFocus.focus();
    }
    settingsReturnFocus = null;
  }

  function openSettingsDialog(title, trigger, build) {
    hidePopover();
    if (settingsDialogCleanup) settingsDialogCleanup();
    settingsDialogCleanup = null;
    settingsReturnFocus = trigger || document.activeElement;
    $("settings-dialog-title").textContent = title;
    settingsDialogBody.innerHTML = "";
    settingsDialogFooter.innerHTML = "";
    build(settingsDialogBody, settingsDialogFooter);
    settingsOverlay.classList.remove("hidden");
    settingsOverlay.setAttribute("aria-hidden", "false");
    requestAnimationFrame(() => {
      const target = settingsDialogBody.querySelector("input, button, select, textarea");
      (target || $("settings-dialog-close")).focus();
    });
  }

  let currentPairingRequestId = "";

  function pairingDeviceLabel(item) {
    return item.label || item.hostname || item.device_id || item.node_id || "远程设备";
  }

  function showPairingRequest(item) {
    if (!item || !item.request_id || !pairingBar) return;
    currentPairingRequestId = item.request_id;
    $("pairing-desc").textContent = pairingDeviceLabel(item) + " 请求连接这台电脑";
    pairingBar.classList.remove("hidden");
  }

  async function answerPairing(decision) {
    const requestId = currentPairingRequestId;
    if (!requestId) return;
    currentPairingRequestId = "";
    if (pairingBar) pairingBar.classList.add("hidden");
    const res = await fetch("/api/pairing/decide", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request_id: requestId, decision }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      notify(data.error || "无法处理配对请求", "error");
      return;
    }
    if (decision === "deny") notify("已拒绝配对请求", "warning");
    else notify(decision === "always" ? "已始终允许该设备" : "已允许本次连接", "success");
  }

  async function openPhoneSettings(anchor) {
    openSettingsDialog("手机设备", anchor, (body, footer) => {
      const intro = document.createElement("div");
      intro.className = "settings-intro";
      intro.innerHTML = "<strong>批准手机接入这台电脑</strong><p>手机请填写下面的 MagicDNS 域名（*.ts.net），不要填 100.x 地址。点选这台电脑后，这里会出现配对请求。允许本次只在这次会话有效；始终允许会记住该设备。</p>";
      body.appendChild(intro);
      const endpointHint = document.createElement("p");
      endpointHint.className = "text-faint";
      endpointHint.id = "pairing-magicdns-hint";
      body.appendChild(endpointHint);
      const status = document.createElement("div");
      status.className = "settings-warning hidden";
      status.id = "pairing-status-error";
      body.appendChild(status);
      const requestBox = document.createElement("div");
      requestBox.id = "pairing-request-list";
      requestBox.className = "pairing-device-list";
      body.appendChild(requestBox);
      const deviceBox = document.createElement("div");
      deviceBox.id = "pairing-device-list";
      deviceBox.className = "pairing-device-list";
      body.appendChild(deviceBox);

      async function refreshStatus() {
        const res = await fetch("/api/pairing/status", { cache: "no-store" });
        const data = await res.json().catch(() => ({}));
        renderRequests(data.requests || []);
        renderDevices(data.devices || []);
        const endpointHost = String(data.endpoint || "").replace(/^https:\/\//i, "").replace(/\/$/, "");
        if (endpointHost && data.https_published) {
          endpointHint.textContent = "手机填写：" + endpointHost;
        } else if (endpointHost) {
          endpointHint.textContent = "MagicDNS：" + endpointHost + "（Tailscale HTTPS 尚未发布，请保持电脑端 MewCode 运行）";
        } else {
          endpointHint.textContent = "";
        }
        if (!data.available) {
          status.textContent = data.error || "Tailscale is not connected";
          status.classList.remove("hidden");
        } else if (data.https_published === false && data.https_error) {
          status.textContent = data.https_error;
          status.classList.remove("hidden");
        } else {
          status.classList.add("hidden");
        }
        if ((data.requests || [])[0]) showPairingRequest(data.requests[0]);
      }

      function renderRequests(requests) {
        if (!requests.length) {
          requestBox.innerHTML = "<div class='conv-empty'>没有待批准的配对请求</div>";
          return;
        }
        requestBox.innerHTML = requests.map((item) =>
          "<div class='pairing-request-row'><div>" + esc(pairingDeviceLabel(item)) +
          "<div class='text-faint'>" + esc(item.node_id || item.device_id || "") + "</div></div>" +
          "<div class='pairing-request-actions'>" +
          "<button type='button' class='btn allow pairing-decide' data-id='" + esc(item.request_id) + "' data-decision='once'>允许本次</button>" +
          "<button type='button' class='btn always pairing-decide' data-id='" + esc(item.request_id) + "' data-decision='always'>始终允许</button>" +
          "<button type='button' class='btn deny pairing-decide' data-id='" + esc(item.request_id) + "' data-decision='deny'>拒绝</button>" +
          "</div></div>"
        ).join("");
        requestBox.querySelectorAll(".pairing-decide").forEach((button) => {
          button.addEventListener("click", async () => {
            currentPairingRequestId = button.dataset.id;
            await answerPairing(button.dataset.decision);
            refreshStatus();
          });
        });
      }

      function renderDevices(devices) {
        if (!devices.length) {
          deviceBox.innerHTML = "<div class='conv-empty'>尚未配对设备</div>";
          return;
        }
        deviceBox.innerHTML = devices.map((item) =>
          "<div class='pairing-device-row'><span>" + esc(pairingDeviceLabel(item)) +
          " · " + (item.allow === "once" ? "本次" : "始终允许") + "</span>" +
          "<button type='button' class='btn always pairing-revoke' data-id='" + esc(item.device_id) + "'>解除</button></div>"
        ).join("");
        deviceBox.querySelectorAll(".pairing-revoke").forEach((button) => {
          button.addEventListener("click", async () => {
            const res = await fetch("/api/pairing/revoke", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ device_id: button.dataset.id }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) notify(data.error || "解除失败", "error");
            refreshStatus();
          });
        });
      }

      const refresh = document.createElement("button");
      refresh.type = "button";
      refresh.className = "btn always";
      refresh.textContent = "刷新";
      refresh.addEventListener("click", refreshStatus);
      footer.append(refresh);
      refreshStatus();
    });
  }

  $("workspace-settings").addEventListener("click", (event) => openPhoneSettings(event.currentTarget));
  $("continue-in-mewcode").addEventListener("click", async () => {
    if (!currentExternalOpen) return;
    const item = currentExternalOpen;
    const res = await fetch("/api/workspace/continue", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source: item.source,
        external_id: item.external_id || item.id,
        node_id: item.node_id || "local",
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      notify(data.error || "无法用 MewCode 继续", "error");
      return;
    }
    setReadonlyView(false);
    await openConversation(data.scope === "normal" ? "normal" : "project", data.id);
  });

  $("settings-dialog-close").addEventListener("click", closeSettingsDialog);
  settingsOverlay.addEventListener("mousedown", (event) => {
    if (event.target === settingsOverlay) closeSettingsDialog();
  });
  document.addEventListener("keydown", (event) => {
    if (settingsOverlay.classList.contains("hidden")) return;
    if (event.key === "Escape") {
      event.preventDefault();
      closeSettingsDialog();
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = [...settingsOverlay.querySelectorAll(
      "button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary, [tabindex]:not([tabindex='-1'])"
    )].filter((node) => node.offsetParent !== null);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });

  async function loadModelCatalog(force = false) {
    const catalogTtl = (modelCatalog && modelCatalog.cc_switch && modelCatalog.cc_switch.available) ? 8000 : 60000;
    if (modelCatalog && !force && Date.now() - modelCatalogAt < catalogTtl) return modelCatalog;
    if (modelCatalogRequest) return modelCatalogRequest;
    const url = force
      ? "/api/models/catalog?refresh=true&_=" + Date.now()
      : "/api/models/catalog";
    modelCatalogRequest = (async () => {
      const res = await fetch(url, force ? {
        cache: "no-store",
        headers: { "Cache-Control": "no-cache" },
      } : undefined);
      if (!res.ok) throw new Error("模型目录加载失败: HTTP " + res.status);
      modelCatalog = await res.json();
      modelCatalogAt = Date.now();
      updateModelIntelligenceLabels();
      updateShellStatus();
      return modelCatalog;
    })();
    try {
      return await modelCatalogRequest;
    } finally {
      modelCatalogRequest = null;
    }
  }

  function iqText(summary) {
    if (!summary) return "智力 --";
    if (summary.kind === "exact") return "智力 " + Number(summary.iq).toFixed(1);
    const lo = Number(summary.min_iq).toFixed(1);
    const hi = Number(summary.max_iq).toFixed(1);
    return lo === hi ? "智力 " + lo + "（自动档）" : "智力 " + lo + "–" + hi + "（自动档）";
  }

  function currentCatalogModel() {
    const models = modelCatalog && modelCatalog.models || [];
    return models.find((model) =>
      model.provider_key === currentStatus.provider_key &&
      model.model_id === currentStatus.model
    ) || models.find((model) => model.model_id === currentStatus.model) || null;
  }

  function currentIqSummary() {
    const current = currentCatalogModel();
    if (!current) return null;
    const effort = currentEfforts.current || "auto";
    const byEffort = current.intelligence_by_effort || current.iq_by_effort || {};
    return effort === "auto" ? (current.iq || null) : (byEffort[effort] || null);
  }

  function formatRadarTime(value) {
    if (!value) return "--";
    const parsed = typeof value === "number"
      ? new Date(value * 1000)
      : new Date(value);
    return Number.isNaN(parsed.getTime())
      ? String(value)
      : parsed.toLocaleString("zh-CN", { hour12: false });
  }

  function updateModelIntelligenceLabels() {
    const summary = currentIqSummary();
    const model = String(currentStatus.model || "--");
    const shortModel = model.length > 18 ? model.slice(0, 16) + "…" : model;
    const effort = currentEfforts.current || "auto";
    const iq = summary && summary.kind === "exact"
      ? Math.round(Number(summary.iq))
      : (summary && summary.max_iq != null ? Math.round(Number(summary.max_iq)) : "");
    $("model-effort-label").textContent = iq
      ? shortModel + " · " + effort + " · " + iq
      : shortModel + " · " + effort;
    if (currentStatus.fast_mode) $("model-effort-label").textContent += " · Fast";
    const full = model + " · " + effort + (currentStatus.fast_mode ? " · Fast" : "") + (summary ? " · " + iqText(summary) : "");
    const btn = $("model-effort-btn");
    if (btn) btn.title = "模型与推理强度 · " + full;
  }

  function runtimeChoice(label, meta, options = {}) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "runtime-choice" +
      (options.current ? " current" : "") +
      (options.viewing ? " viewing" : "") +
      (options.unavailable ? " unavailable" : "") +
      (options.pending ? " pending" : "");
    button.disabled = !!options.disabled;
    const copy = document.createElement("span");
    copy.className = "runtime-choice-copy";
    const name = document.createElement("strong");
    name.textContent = label;
    copy.appendChild(name);
    if (meta) {
      const description = document.createElement("small");
      description.textContent = meta;
      copy.appendChild(description);
    }
    button.appendChild(copy);
    if (options.badges && options.badges.length) {
      const badges = document.createElement("span");
      badges.className = "runtime-choice-badges";
      for (const value of options.badges) {
        const badge = document.createElement("span");
        badge.textContent = value;
        badges.appendChild(badge);
      }
      button.appendChild(badges);
    }
    if (options.onClick) button.addEventListener("click", options.onClick);
    return button;
  }

  function modelContextBadge(tokens) {
    const value = Number(tokens || 0);
    if (!value) return "";
    if (value >= 1000000) {
      const millions = value / 1000000;
      return (Number.isInteger(millions) ? millions.toFixed(0) : millions.toFixed(2)) + "M ctx";
    }
    return Math.round(value / 1000) + "k ctx";
  }

  function runtimeProviderItems(catalog) {
    const slotByKey = new Map(currentSlots.map((slot) => [slot.key, slot]));
    const providers = (catalog.providers || []).map((provider) => ({
      ...provider,
      slot: slotByKey.get(provider.key) || null,
    }));
    const known = new Set(providers.map((provider) => provider.key));
    for (const slot of currentSlots) {
      if (known.has(slot.key)) continue;
      const displayName = customProviderFamilyName({
        key: slot.key,
        name: "",
        label: slot.label,
        slot,
      });
      providers.push({
        key: slot.key,
        name: displayName,
        label: displayName,
        brand: "custom",
        protocol: "",
        available: !slot.error,
        error: slot.error || "",
        slot,
      });
    }
    return providers;
  }

  /** Map raw provider/slot keys onto the user-facing runtime families. */
  function runtimeProviderFamilyId(provider) {
    const slot = provider && provider.slot ? provider.slot : {};
    const brand = String((provider && provider.brand) || "").toLowerCase();
    const protocol = String((provider && provider.protocol) || "").toLowerCase();
    const source = String(
      (provider && provider.source) || slot.source || ""
    ).toLowerCase();
    const parts = [
      provider && provider.key,
      provider && provider.name,
      provider && provider.label,
      brand,
      source,
      slot.key,
      slot.label,
      slot.origin,
    ].map((value) => String(value || "").toLowerCase());
    const blob = parts.join(" ");
    if (brand === "magpie" || /(?:^|[:\s-])magpie(?:$|[\s\[])/.test(blob)) return "magpie";
    // A provider the user explicitly saved under the OpenAI-Compatible/custom
    // preset is a multi-brand relay.  Its label embeds a default model such as
    // "[kimi-k3]" or "[glm-5.3]", so the brand keyword fallback below must NOT
    // apply to it — otherwise every relay gets bucketed into whichever vendor
    // its default model happens to name.  Only infer a brand family from the
    // blob when the provider was NOT explicitly saved as custom.
    const notExplicitCustom = brand !== "custom";
    if (brand === "xai" || (notExplicitCustom && /grok|\bxai\b|x\.ai/.test(blob))) return "grok";
    // Anthropic protocol / Claude naming both map to the Claude Code family.
    if (brand === "anthropic" || (notExplicitCustom && /claude|anthropic/.test(blob))) return "claude-code";
    // Brand-specific desktop providers must win before the OpenAI/Codex heuristic.
    // Important: do NOT put `protocol` into the openai regex blob — `openai-compat`
    // contains a `\bopenai\b` match and used to dump DeepSeek into Codex.
    if (brand === "deepseek" || (notExplicitCustom && /deepseek/.test(blob))) return "deepseek";
    if (brand === "openrouter" || (notExplicitCustom && /openrouter/.test(blob))) return "openrouter";
    if (brand === "kimi" || (notExplicitCustom && /moonshot|\bkimi\b/.test(blob))) return "kimi";
    if (brand === "opencode-go" || (notExplicitCustom && /opencode/.test(blob))) return "opencode-go";
    if (source === "codex" || (notExplicitCustom && /codex/.test(blob))) return "codex";
    // Exact OpenAI protocol (ChatGPT / Codex relay), never openai-compat.
    if (protocol === "openai") return "codex";
    // Desktop openai-compat / custom brands are not a single "其他" bucket —
    // runtimeProviderFamilies expands them into one row per provider.
    if (protocol === "openai-compat" || brand === "custom") return "custom";
    return "custom";
  }

  function customProviderFamilyKey(provider) {
    const key = String((provider && provider.key) || "").trim();
    return key ? ("provider:" + key) : "custom";
  }

  function customProviderFamilyName(provider) {
    const slot = (provider && provider.slot) || {};
    let named = String(
      (provider && provider.name) ||
      (provider && provider.label) ||
      slot.label ||
      ""
    ).trim();
    // Slot labels look like "antigravity  [gemini-…]  (桌面客户端)".
    const bracket = named.match(/^(.+?)\s+\[/);
    if (bracket) named = bracket[1].trim();
    named = named.replace(/\s*\((桌面客户端|Desktop|Electron[^)]*)\)\s*$/i, "").trim();
    if (named) return named;
    const key = String((provider && provider.key) || slot.key || "").trim();
    if (!key) return "其他";
    return key.replace(/^(desktop|cc-switch):/i, "") || key;
  }

  function enrichClaudeFamilyModels(family, providerModels, catalog, preferredKey) {
    if (!family || family.key !== "claude-code") return providerModels || [];
    if ((providerModels || []).length) return providerModels;
    const providerKey = preferredKey ||
      (family.providerKeys && family.providerKeys[0]) || "";
    if (!providerKey) return providerModels || [];
    const caps = (catalog && catalog.capability_catalog && catalog.capability_catalog.models) || [];
    const skip = new Set(["claude", "claude-3"]);
    const seeded = [];
    for (const item of caps) {
      const brand = String(item.provider_brand || item.brand || "").toLowerCase();
      const modelId = String(item.model_id || "").trim();
      if (!modelId || skip.has(modelId)) continue;
      if (brand !== "anthropic" && !/^claude/i.test(modelId)) continue;
      seeded.push({
        provider_key: providerKey,
        model_id: modelId,
        available: true,
        protocol: "anthropic",
        source_label: item.source_label || "官方能力目录",
        vision: !!item.vision,
        efforts: item.efforts || [],
        default_effort: item.default_effort || "",
        context_window: item.context_window || 0,
        iq: null,
      });
    }
    return seeded;
  }

  function runtimeProviderFamilies(catalog) {
    const knownOrder = [
      "codex",
      "magpie",
      "claude-code",
      "grok",
      "deepseek",
      "openrouter",
      "kimi",
      "opencode-go",
    ];
    const grouped = {
      codex: {
        key: "codex",
        name: "Codex",
        members: [],
        providerKeys: [],
      },
      magpie: {
        key: "magpie",
        name: "Magpie",
        members: [],
        providerKeys: [],
      },
      "claude-code": {
        key: "claude-code",
        name: "Claude Code",
        members: [],
        providerKeys: [],
      },
      grok: {
        key: "grok",
        name: "Grok",
        members: [],
        providerKeys: [],
      },
      deepseek: {
        key: "deepseek",
        name: "DeepSeek",
        members: [],
        providerKeys: [],
      },
      openrouter: {
        key: "openrouter",
        name: "OpenRouter",
        members: [],
        providerKeys: [],
      },
      kimi: {
        key: "kimi",
        name: "Kimi",
        members: [],
        providerKeys: [],
      },
      "opencode-go": {
        key: "opencode-go",
        name: "OpenCode Go",
        members: [],
        providerKeys: [],
      },
    };
    const customOrder = [];
    for (const provider of runtimeProviderItems(catalog)) {
      let familyId = runtimeProviderFamilyId(provider);
      if (!grouped[familyId] || familyId === "custom") {
        familyId = customProviderFamilyKey(provider);
        if (!grouped[familyId]) {
          grouped[familyId] = {
            key: familyId,
            name: customProviderFamilyName(provider),
            members: [],
            providerKeys: [],
          };
          customOrder.push(familyId);
        }
      }
      const family = grouped[familyId];
      family.members.push(provider);
      if (provider.key) family.providerKeys.push(provider.key);
    }
    // Hide empty brand families so the picker stays short; keep the core three
    // visible even when offline so users can still open configure flows.
    const alwaysShow = new Set(["codex", "claude-code", "grok"]);
    const orderedKeys = [
      ...knownOrder,
      ...customOrder.filter((key) => !knownOrder.includes(key)),
    ];
    return orderedKeys.filter((key) => {
      const family = grouped[key];
      return family && (alwaysShow.has(family.key) || family.members.length > 0);
    }).map((key) => {
      const family = grouped[key];
      const members = family.members;
      const availableMembers = members.filter((item) => {
        const slot = item.slot || {};
        return item.available !== false && !item.error && !slot.error;
      });
      const currentMember = members.find((item) => item.key === currentStatus.provider_key);
      const error = members.map((item) => item.error || (item.slot && item.slot.error) || "")
        .find(Boolean) || "";
      return {
        ...family,
        providerKeys: [...new Set(family.providerKeys)],
        available: availableMembers.length > 0,
        error: availableMembers.length ? "" : (error || (members.length ? "" : "未发现可用配置")),
        default_model: (currentMember && currentMember.default_model) ||
          (availableMembers[0] && availableMembers[0].default_model) || "",
        slot: (currentMember && currentMember.slot) ||
          (availableMembers[0] && availableMembers[0].slot) ||
          (members[0] && members[0].slot) || null,
      };
    });
  }

  function familyProviderKeySet(family) {
    return new Set((family && family.providerKeys) || []);
  }

  function familyModelDedupeScore(model, preferredKeys) {
    const key = String(model.provider_key || "");
    const source = String(model.source || "").toLowerCase();
    let score = 0;
    if (model.available !== false) score += 40;
    if (key && preferredKeys.current && key === preferredKeys.current) score += 100;
    if (key && preferredKeys.state && key === preferredKeys.state) score += 80;
    if (source === "cc-switch" || key.startsWith("cc-switch:")) score += 60;
    if (source === "claude-code" || source === "codex") score += 30;
    if (source === "desktop") score += 10;
    return score;
  }

  /** One row per model_id inside a family (cc-switch + native slots often overlap). */
  function dedupeFamilyModels(models, preferredKeys = {}) {
    const best = new Map();
    for (const model of models || []) {
      const modelId = String(model.model_id || "").trim();
      if (!modelId) continue;
      const key = modelId.toLowerCase();
      const prev = best.get(key);
      if (
        !prev ||
        familyModelDedupeScore(model, preferredKeys) >
          familyModelDedupeScore(prev, preferredKeys)
      ) {
        best.set(key, model);
      }
    }
    return [...best.values()];
  }

  function selectedRuntimeModel(state, catalog) {
    const families = runtimeProviderFamilies(catalog);
    const family = families.find((item) => item.key === state.familyKey) ||
      families.find((item) => item.providerKeys.includes(state.providerKey));
    const keySet = familyProviderKeySet(family);
    let providerModels = (catalog.models || []).filter((model) => {
      if (model.review_only) return false;
      if (keySet.size) return keySet.has(model.provider_key);
      return model.provider_key === state.providerKey;
    });
    providerModels = enrichClaudeFamilyModels(
      family,
      providerModels,
      catalog,
      state.providerKey || (family && family.providerKeys[0]) || ""
    );
    const preferredKeys = {
      current: currentStatus.provider_key || "",
      state: state.providerKey || "",
    };
    providerModels = dedupeFamilyModels(providerModels, preferredKeys);
    const currentInFamily = keySet.has(currentStatus.provider_key) ||
      (!keySet.size && state.providerKey === currentStatus.provider_key);
    if (
      currentInFamily &&
      currentStatus.model &&
      !providerModels.some((model) => model.model_id === currentStatus.model)
    ) {
      providerModels = [{
        provider_key: currentStatus.provider_key,
        model_id: currentStatus.model,
        available: true,
        protocol: currentStatus.protocol || "",
        source_label: "当前会话",
      }, ...providerModels];
    }
    let selected = providerModels.find((model) => model.model_id === state.modelId);
    if (!selected && currentInFamily) {
      selected = providerModels.find((model) => model.model_id === currentStatus.model);
    }
    if (
      !selected &&
      state.pending &&
      state.pending.type === "model" &&
      (keySet.has(state.pending.providerKey) || state.pending.providerKey === state.providerKey)
    ) {
      selected = providerModels.find((model) => model.model_id === state.pending.modelId);
    }
    return { selected, providerModels, family };
  }

  function clearRuntimePendingTimer(state) {
    if (state && state.pendingTimer) {
      clearTimeout(state.pendingTimer);
      state.pendingTimer = null;
    }
  }

  function beginRuntimeModelSwitch(state, model) {
    if (
      model.provider_key === currentStatus.provider_key &&
      model.model_id === currentStatus.model &&
      !state.pending
    ) {
      state.providerKey = model.provider_key;
      state.modelId = model.model_id;
      state.feedback = "";
      state.feedbackError = false;
      state.softUpdate = true;
      renderRuntimeSettingsPanel(state);
      return;
    }
    state.providerKey = model.provider_key;
    state.modelId = model.model_id;
    state.pending = {
      type: "model",
      providerKey: model.provider_key,
      modelId: model.model_id,
    };
    state.pendingFailed = false;
    state.feedback = "正在切换到 " + model.model_id + "…";
    state.feedbackError = false;
    clearRuntimePendingTimer(state);
    state.pendingTimer = setTimeout(() => {
      if (
        runtimeSettingsView === state &&
        state.pending &&
        state.pending.type === "model" &&
        state.pending.modelId === model.model_id
      ) {
        state.pending = null;
        state.pendingFailed = true;
        state.feedback = "切换超时，请再试一次";
        state.feedbackError = true;
        state.softUpdate = true;
        renderRuntimeSettingsPanel(state);
      }
    }, 15000);
    state.softUpdate = true;
    renderRuntimeSettingsPanel(state);
    send({
      type: "select_model_ref",
      provider_key: model.provider_key,
      model_id: model.model_id,
      effort: "",
    });
  }

  function fillRuntimeModelList(modelList, state, providerModels, selected, pendingModel) {
    const query = (state.query || "").trim().toLowerCase();
    const visibleModels = providerModels.filter((model) =>
      !query || model.model_id.toLowerCase().includes(query)
    );
    modelList.replaceChildren();
    for (const model of visibleModels) {
      const actual = model.provider_key === currentStatus.provider_key &&
        model.model_id === currentStatus.model;
      const viewed = selected && model.model_id === selected.model_id;
      const badges = [
        ...(model.iq ? [iqText(model.iq)] : []),
        ...(model.context_window ? [modelContextBadge(model.context_window)] : []),
        ...(model.vision ? ["Vision"] : []),
      ];
      modelList.appendChild(runtimeChoice(
        model.model_id,
        [model.protocol, model.source_label || model.provider_brand, model.context_policy,
          model.unavailable_reason,
          ...(model.capabilities || []).filter((value) => value !== "chat" && value !== "tools")]
          .filter(Boolean).join(" · "),
        {
          current: actual,
          viewing: viewed,
          unavailable: model.available === false,
          disabled: model.available === false,
          pending: pendingModel && viewed,
          badges,
          onClick: () => beginRuntimeModelSwitch(state, model),
        }
      ));
    }
    if (!visibleModels.length) {
      const empty = document.createElement("p");
      empty.className = "runtime-empty";
      empty.textContent = query ? "没有匹配的模型" : "该 Provider 未返回可用模型（能力目录不会伪造可用性）";
      modelList.appendChild(empty);
    }
  }

  function softRefreshRuntimeSettings(state, ctx) {
    const root = state.root;
    const grid = root.querySelector("[data-runtime-grid]");
    if (!grid) return false;
    const providerList = grid.querySelector(".provider-column .runtime-choice-list");
    if (providerList) {
      const buttons = [...providerList.querySelectorAll(".runtime-choice")];
      ctx.families.forEach((family, index) => {
        const button = buttons[index];
        if (!button) return;
        const isCurrent = family.providerKeys.includes(currentStatus.provider_key);
        button.classList.toggle("current", isCurrent);
        button.classList.toggle("viewing", family.key === state.familyKey);
        button.classList.toggle("unavailable", !family.available && !family.members.length);
      });
    }
    const modelList = grid.querySelector(".model-column .runtime-choice-list");
    if (modelList) {
      fillRuntimeModelList(
        modelList, state, ctx.providerModels, ctx.selected, ctx.pendingModel
      );
    }
    const effortList = grid.querySelector(".effort-column .runtime-choice-list");
    if (effortList) {
      effortList.replaceChildren();
      if (!ctx.selected) {
        const empty = document.createElement("p");
        empty.className = "runtime-empty";
        empty.textContent = "请先选择模型";
        effortList.appendChild(empty);
      } else {
        const efforts = ["", ...new Set(ctx.selected.efforts || [])];
        const byEffort = ctx.selected.intelligence_by_effort || ctx.selected.iq_by_effort || {};
        for (const effort of efforts) {
          const summary = effort ? byEffort[effort] : ctx.selected.iq;
          const label = effort || "auto（默认）";
          const effortMeta = summary ? iqText(summary) : (
            effort && effort === ctx.selected.default_effort
              ? "官方能力目录默认档"
              : (effort ? "官方能力目录支持" : "跟随 Provider 默认")
          );
          effortList.appendChild(runtimeChoice(
            label,
            effortMeta,
            {
              current: ctx.selectedIsCurrent && effort === ctx.currentEffort,
              viewing: effort === ctx.viewedEffort,
              disabled: !ctx.selectedIsCurrent || !!ctx.pendingModel || !!ctx.pendingEffort,
              pending: ctx.pendingEffort && effort === ctx.viewedEffort,
              onClick: () => {
                state.pending = { type: "effort", effort };
                state.pendingFailed = false;
                state.feedback = "正在应用 Effort " + (effort || "auto") + "…";
                state.softUpdate = true;
                renderRuntimeSettingsPanel(state);
                send({ type: "set_effort", level: effort });
              },
            }
          ));
        }
      }
    }
    let feedback = root.querySelector(".runtime-feedback");
    if (state.feedback) {
      if (!feedback) {
        const footer = root.querySelector(".runtime-iq-footer");
        feedback = document.createElement("p");
        feedback.className = "runtime-feedback";
        if (footer) footer.appendChild(feedback);
      }
      if (feedback) {
        feedback.className = "runtime-feedback" + (state.feedbackError ? " error" : "");
        feedback.textContent = state.feedback;
      }
    } else if (feedback) {
      feedback.remove();
    }
    if (state.focusSearch) {
      const search = grid.querySelector(".runtime-model-search");
      if (search) {
        search.focus();
        const end = search.value.length;
        search.setSelectionRange(end, end);
      }
      state.focusSearch = false;
    }
    for (const element of root.querySelectorAll('.runtime-fast-mode')) element.fastModeControl?.update();
    return true;
  }

  function restoreReviewSelection(select, modelId = "", providerKey = "") {
    let selected = [...select.options].find(option => option.value === modelId &&
      (option.dataset.providerKey || "") === providerKey);
    if (!selected && modelId) {
      selected = document.createElement("option");
      selected.value = modelId;
      selected.dataset.providerKey = providerKey;
      selected.textContent = modelId + " · 当前不可用（请重新选择）";
      selected.disabled = true;
      select.appendChild(selected);
    }
    for (const option of select.options) option.selected = option === selected;
  }

  function renderRuntimeSettingsPanel(state) {
    if (runtimeSettingsView !== state || !state.root) return;
    const root = state.root;
    const catalog = modelCatalog || { providers: [], models: [], intelligence: {} };
    const families = runtimeProviderFamilies(catalog);
    if (!state.familyKey) {
      const currentFamily = families.find((item) =>
        item.providerKeys.includes(currentStatus.provider_key)
      );
      const remembered = rememberedFamilyKey &&
        families.some((item) => item.key === rememberedFamilyKey)
        ? rememberedFamilyKey
        : "";
      state.familyKey = remembered ||
        (currentFamily && currentFamily.key) ||
        (families.find((item) => item.available) || families[0] || {}).key || "codex";
    }
    if (!families.some((item) => item.key === state.familyKey)) {
      state.familyKey = (families[0] && families[0].key) || "codex";
    }
    if (state.familyKey && state.familyKey !== rememberedFamilyKey) {
      rememberRuntimeFamily(state.familyKey);
    }
    const activeFamily = families.find((item) => item.key === state.familyKey);
    if (activeFamily) {
      if (activeFamily.providerKeys.includes(currentStatus.provider_key)) {
        state.providerKey = currentStatus.provider_key;
      } else if (!activeFamily.providerKeys.includes(state.providerKey)) {
        state.providerKey = activeFamily.providerKeys[0] || "";
      }
    }
    const { selected, providerModels } = selectedRuntimeModel(state, catalog);
    const selectedIsCurrent = !!selected &&
      selected.provider_key === currentStatus.provider_key &&
      selected.model_id === currentStatus.model;
    const pendingModel = state.pending && state.pending.type === "model";
    const pendingEffort = state.pending && state.pending.type === "effort";
    const currentEffort = currentEfforts.current === "auto" ? "" : (currentEfforts.current || "");
    const viewedEffort = pendingEffort
      ? state.pending.effort
      : (selectedIsCurrent ? currentEffort : "");

    if (state.softUpdate) {
      state.softUpdate = false;
      if (softRefreshRuntimeSettings(state, {
        families, selected, providerModels, selectedIsCurrent,
        pendingModel, pendingEffort, currentEffort, viewedEffort, catalog,
      })) {
        return;
      }
    }

    root.innerHTML = "";
    const header = document.createElement("header");
    header.className = "runtime-settings-head";
    const heading = document.createElement("div");
    heading.innerHTML = "<strong>模型与推理</strong><span>点左侧 Provider 家族浏览模型，点模型才切换</span>";
    const close = document.createElement("button");
    close.type = "button";
    close.className = "runtime-settings-close";
    close.setAttribute("aria-label", "关闭模型设置");
    close.textContent = "×";
    close.addEventListener("click", hidePopover);
    header.append(heading, close);
    root.appendChild(header);

    const grid = document.createElement("div");
    grid.className = "runtime-settings-grid";
    grid.dataset.runtimeGrid = "1";

    const providerColumn = document.createElement("section");
    providerColumn.className = "runtime-settings-column provider-column";
    providerColumn.innerHTML = "<header><strong>1. Provider</strong><small>选择 Codex、Claude Code、Grok、DeepSeek 等</small></header>";
    const providerList = document.createElement("div");
    providerList.className = "runtime-choice-list";
    for (const family of families) {
      const isCurrent = family.providerKeys.includes(currentStatus.provider_key);
      const emptyFamily = !family.members.length && !family.providerKeys.length;
      const badges = [
        ...(isCurrent ? ["当前"] : []),
        ...(!family.available ? ["需检查配置"] : []),
      ];
      const meta = isCurrent && currentStatus.model
        ? currentStatus.model
        : (family.default_model || (!family.available ? (family.error || "点击浏览 / 检查配置") : "点击浏览模型"));
      providerList.appendChild(runtimeChoice(
        family.name,
        meta,
        {
          current: isCurrent,
          viewing: family.key === state.familyKey,
          unavailable: emptyFamily,
          badges,
          onClick: () => {
            state.familyKey = family.key;
            rememberRuntimeFamily(family.key);
            state.providerKey = family.providerKeys.includes(currentStatus.provider_key)
              ? currentStatus.provider_key
              : (family.providerKeys[0] || "");
            state.query = "";
            const keySet = familyProviderKeySet(family);
            const choices = dedupeFamilyModels(
              (catalog.models || []).filter(
                (model) => keySet.has(model.provider_key) && model.available !== false
              ),
              {
                current: currentStatus.provider_key || "",
                state: state.providerKey || "",
              }
            );
            if (isCurrent) {
              state.modelId = currentStatus.model;
            } else if (!choices.some((model) => model.model_id === state.modelId)) {
              const fallback = choices.find(
                (model) => model.model_id === family.default_model
              ) || choices[0];
              state.modelId = fallback ? fallback.model_id : "";
              if (fallback) state.providerKey = fallback.provider_key;
            }
            if (!choices.length) {
              state.feedback = family.available
                ? family.name + " 没有可切换的模型，请刷新目录或检查模型配置"
                : (family.error || family.name + " 暂无可用模型，仍可浏览目录");
              state.feedbackError = family.available;
            } else {
              state.feedback = "正在浏览 " + family.name + "，点击模型完成切换";
              state.feedbackError = false;
            }
            state.softUpdate = true;
            renderRuntimeSettingsPanel(state);
          },
        }
      ));
    }
    if (!families.length) {
      providerList.innerHTML = "<p class='runtime-empty'>" +
        (state.refreshing ? "正在加载 Provider 与 IQ 数据…" : "未发现 Provider") +
        "</p>";
    }
    providerColumn.appendChild(providerList);
    const viewedFamily = families.find((family) => family.key === state.familyKey);
    if (
      window.mewcodeDesktop &&
      viewedFamily &&
      viewedFamily.key === "grok" &&
      !viewedFamily.available
    ) {
      const configureGrok = document.createElement("button");
      configureGrok.type = "button";
      configureGrok.className = "runtime-provider-connect";
      configureGrok.textContent = "配置 desktop:example_provider_2 Provider";
      configureGrok.addEventListener("click", () => {
        const anchor = state.anchor;
        hidePopover();
        void openProviderManager(anchor, { initialBrand: "xai" });
      });
      providerColumn.appendChild(configureGrok);
    }
    grid.appendChild(providerColumn);

    const modelColumn = document.createElement("section");
    modelColumn.className = "runtime-settings-column model-column";
    const modelHeader = document.createElement("header");
    modelHeader.innerHTML = "<strong>2. 模型</strong><small>选择后立即切换并回到 auto</small>";
    const search = document.createElement("input");
    search.type = "search";
    search.className = "runtime-model-search";
    search.placeholder = "搜索当前 Provider 的模型";
    search.value = state.query || "";
    search.addEventListener("input", () => {
      state.query = search.value;
      state.focusSearch = true;
      clearTimeout(state.searchTimer);
      state.searchTimer = setTimeout(() => {
        state.softUpdate = true;
        renderRuntimeSettingsPanel(state);
      }, 120);
    });
    const modelList = document.createElement("div");
    modelList.className = "runtime-choice-list";
    fillRuntimeModelList(modelList, state, providerModels, selected, pendingModel);
    modelColumn.append(modelHeader, search, modelList);
    grid.appendChild(modelColumn);

    const effortColumn = document.createElement("section");
    effortColumn.className = "runtime-settings-column effort-column";
    effortColumn.innerHTML = "<header><strong>3. Effort</strong><small>模型生效后可即时调整</small></header>";
    const effortList = document.createElement("div");
    effortList.className = "runtime-choice-list";
    if (!selected) {
      effortList.innerHTML = "<p class='runtime-empty'>请先选择模型</p>";
    } else {
      const efforts = ["", ...new Set(selected.efforts || [])];
      const byEffort = selected.intelligence_by_effort || selected.iq_by_effort || {};
      for (const effort of efforts) {
        const summary = effort ? byEffort[effort] : selected.iq;
        const label = effort || "auto（默认）";
        const effortMeta = summary ? iqText(summary) : (
          effort && effort === selected.default_effort
            ? "官方能力目录默认档"
            : (effort ? "官方能力目录支持" : "跟随 Provider 默认")
        );
        effortList.appendChild(runtimeChoice(
          label,
          effortMeta,
          {
            current: selectedIsCurrent && effort === currentEffort,
            viewing: effort === viewedEffort,
            disabled: !selectedIsCurrent || !!pendingModel || !!pendingEffort,
            pending: pendingEffort && effort === viewedEffort,
            onClick: () => {
              state.pending = { type: "effort", effort };
              state.pendingFailed = false;
              state.feedback = "正在应用 Effort " + (effort || "auto") + "…";
              state.softUpdate = true;
              renderRuntimeSettingsPanel(state);
              send({ type: "set_effort", level: effort });
            },
          }
        ));
      }
    }
    effortColumn.appendChild(effortList);
    grid.appendChild(effortColumn);
    root.appendChild(grid);

    // Meta strip after the 3-column grid is mounted, so a banner/select
    // failure can never leave Provider / Model / Effort truncated.
    try {
      const metaStrip = document.createElement("div");
      metaStrip.className = "runtime-settings-meta";
      if (window.MewFastMode) {
        const speedBox = document.createElement('div');
        window.MewFastMode.mount(speedBox, {getStatus:()=>currentStatus, onChange:status=>{
          currentStatus={...currentStatus,...status};
          updateModelIntelligenceLabels();
          renderRuntimeSettingsPanel(state);
        }});
        metaStrip.appendChild(speedBox);
      }
      const cc = (modelCatalog && modelCatalog.cc_switch) || {};
      if (cc.available && (cc.current || []).length) {
        const banner = document.createElement("div");
        banner.className = "runtime-cc-switch runtime-cc-switch-compact";
        const n = (cc.current || []).length;
        banner.innerHTML = "<strong>cc-switch</strong><small>已连接 " + n +
          " 路 API，切换 Codex / Claude Code 时静默跟随</small>";
        metaStrip.appendChild(banner);
      }
      const review = (modelCatalog && modelCatalog.review_model) || {};
      const reviewBox = document.createElement("div");
      reviewBox.className = "runtime-review-model";
      reviewBox.innerHTML = "<strong>审核</strong><small>审核模型</small>";
      const reviewSelect = document.createElement("select");
      reviewSelect.setAttribute("aria-label", "审核模型");
      reviewSelect.innerHTML = '<option value="" disabled>未指定（跟随辅助模型）</option>';
      for (const model of (modelCatalog && modelCatalog.models) || []) {
        if (model.available === false) continue;
        const opt = document.createElement("option");
        opt.value = model.model_id;
        opt.dataset.providerKey = model.provider_key || "";
        opt.dataset.supportsVision = model.vision && !model.review_only ? "true" : "false";
        const providerLabel = model.provider_name || model.provider_key || "";
        opt.textContent = model.model_id + (providerLabel ? " · " + providerLabel : "");
        if (
          review.model_id &&
          model.model_id === review.model_id &&
          (!review.provider_key || model.provider_key === review.provider_key)
        ) opt.selected = true;
        reviewSelect.appendChild(opt);
      }
      restoreReviewSelection(reviewSelect, review.model_id || "", review.provider_key || "");
      reviewSelect.addEventListener("change", async () => {
        const opt = reviewSelect.selectedOptions[0];
        const modelId = opt ? opt.value : "";
        const providerKey = opt ? (opt.dataset.providerKey || "") : "";
        reviewSelect.disabled = visionSelect.disabled = true;
        try {
          const res = await fetch("/api/settings/review-model", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ model_id: modelId, provider_key: providerKey }),
          });
          const data = await res.json();
          if (!res.ok) throw new Error(data.error || "保存失败");
          if (modelCatalog) modelCatalog.review_model = data;
          syncVisionVisibility();
          notify("审核设置已更新，下一次审核生效", "success");
        } catch (err) {
          const saved = (modelCatalog && modelCatalog.review_model) || {};
          restoreReviewSelection(reviewSelect, saved.model_id || "", saved.provider_key || "");
          syncVisionVisibility();
          notify(err.message || String(err), "error");
        } finally {
          reviewSelect.disabled = visionSelect.disabled = false;
        }
      });
      reviewBox.appendChild(reviewSelect);
      const visionLabel = document.createElement("small");
      visionLabel.textContent = "审核模型不支持图片时，用它查看图像并把观察交给审核模型判定；它不单独给出审核结论。";
      reviewBox.appendChild(visionLabel);
      const visionSelect = document.createElement("select");
      visionSelect.setAttribute("aria-label", "看图能力");
      visionSelect.innerHTML = '<option value="">未指定（主审核模型不支持图片时暂停）</option>';
      for (const model of (modelCatalog && modelCatalog.models) || []) {
        if (model.available === false || !model.vision || model.review_only) continue;
        const opt = document.createElement("option");
        opt.value = model.model_id;
        opt.dataset.providerKey = model.provider_key || "";
        opt.textContent = model.model_id + " · " + (model.provider_name || model.provider_key);
        opt.selected = review.vision_model_id === model.model_id && review.vision_provider_key === model.provider_key;
        visionSelect.appendChild(opt);
      }
      restoreReviewSelection(visionSelect, review.vision_model_id || "", review.vision_provider_key || "");
      visionSelect.addEventListener("change", async () => {
        const opt = visionSelect.selectedOptions[0];
        const selected = reviewSelect.selectedOptions[0];
        reviewSelect.disabled = visionSelect.disabled = true;
        try {
          const res = await fetch("/api/settings/review-model", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ model_id: selected?.value || "", provider_key: selected?.dataset.providerKey || "",
              vision_model_id: opt?.value || "", vision_provider_key: opt?.dataset.providerKey || "" }),
          });
          const data = await res.json();
          if (!res.ok) throw new Error(data.error || "保存失败");
          if (modelCatalog) modelCatalog.review_model = data;
          notify("审核设置已更新，下一次审核生效", "success");
        } catch (err) {
          const saved = (modelCatalog && modelCatalog.review_model) || {};
          restoreReviewSelection(visionSelect, saved.vision_model_id || "", saved.vision_provider_key || "");
          notify(err.message || String(err), "error");
        } finally {
          reviewSelect.disabled = visionSelect.disabled = false;
        }
      });
      reviewBox.appendChild(visionSelect);
      function syncVisionVisibility() {
        const direct = reviewSelect.selectedOptions[0]?.dataset.supportsVision === "true";
        visionLabel.hidden = visionSelect.hidden = direct;
      }
      syncVisionVisibility();
      metaStrip.appendChild(reviewBox);
      root.insertBefore(metaStrip, grid);
    } catch (metaErr) {
      console.warn("runtime settings meta strip failed", metaErr);
    }

    const radar = catalog.intelligence || {};
    const capabilityCatalog = catalog.capability_catalog || {};
    const trackedModels = (capabilityCatalog.models || []).length;
    const byEffort = selected && (selected.intelligence_by_effort || selected.iq_by_effort || {});
    const summary = selected
      ? (viewedEffort ? byEffort[viewedEffort] : selected.iq)
      : null;
    const radarStatus = radar.status === "live" ? "实时数据" :
      radar.status === "published" ? "官网发布快照" :
      radar.status === "stale" ? "本地缓存数据" : "数据不可用";
    const footer = document.createElement("footer");
    footer.className = "runtime-iq-footer" + (radar.status === "stale" ? " stale" : "");
    const iqCopy = document.createElement("div");
    iqCopy.className = "runtime-iq-copy";
    iqCopy.innerHTML = "<strong>" + esc(summary ? iqText(summary) : "当前模型暂无 IQ") + "</strong>" +
      "<span>" + esc(summary ? ("样本 " + fmtInt(summary.runs_total || 0)) : radarStatus) + "</span>" +
      "<small>Radar 数据时间 " + esc(formatRadarTime(radar.source_updated_at)) +
      " · 最后检查 " + esc(formatRadarTime(radar.checked_at || radar.fetched_at)) +
      (trackedModels ? " · 官方能力目录 " + trackedModels + " 项" : "") + "</small>";
    const footerActions = document.createElement("div");
    footerActions.className = "runtime-iq-actions";
    const source = document.createElement("a");
    source.href = "https://codexradar.com/";
    source.target = "_blank";
    source.rel = "noopener noreferrer";
    source.textContent = "CodeRadar · 第三方数据";
    const refresh = document.createElement("button");
    refresh.type = "button";
    refresh.className = "runtime-refresh" + (state.refreshing ? " refreshing" : "");
    refresh.disabled = !!state.refreshing;
    refresh.textContent = state.refreshing ? "正在刷新…" : "刷新 IQ 与目录";
    refresh.addEventListener("click", () => refreshRuntimeSettings(state, true));
    footerActions.append(source, refresh);
    if (selected && selected.source_url) {
      const capabilitySource = document.createElement("a");
      capabilitySource.href = selected.source_url;
      capabilitySource.target = "_blank";
      capabilitySource.rel = "noopener noreferrer";
      capabilitySource.textContent = "模型官方能力";
      footerActions.prepend(capabilitySource);
    }
    footer.append(iqCopy, footerActions);
    if (state.feedback) {
      const feedback = document.createElement("p");
      feedback.className = "runtime-feedback" + (state.feedbackError ? " error" : "");
      feedback.textContent = state.feedback;
      footer.appendChild(feedback);
    }
    root.appendChild(footer);

    const advanced = document.createElement("details");
    advanced.className = "runtime-advanced";
    advanced.open = !!state.advancedOpen;
    advanced.innerHTML = "<summary>高级设置</summary>";
    advanced.addEventListener("toggle", () => { state.advancedOpen = advanced.open; });
    const advancedBody = document.createElement("div");
    advancedBody.className = "runtime-advanced-body";
    const context = document.createElement("div");
    context.className = "runtime-context-options";
    const detected = currentStatus.detected_window || 0;
    const contextLabel = document.createElement("span");
    contextLabel.textContent = "上下文使用上限" + (detected ? "（模型窗口 " + fmtInt(detected) + "）" : "");
    context.appendChild(contextLabel);
    const policyLabel = document.createElement("span");
    policyLabel.className = "runtime-policy-note";
    const activePolicy = currentStatus.context_policy || (selected && selected.context_policy) || "legacy";
    policyLabel.textContent = "路由 " + activePolicy +
      (currentStatus.context_stable_prefix ? " · 稳定前缀" : "") +
      (currentStatus.effort_changes_cache_affinity ? " · Effort 切换新建缓存世代" : "");
    context.appendChild(policyLabel);
    for (const tier of [
      { value: 0, label: "自动 · 模型完整窗口" },
      { value: 200000, label: "200k" },
      { value: 204800, label: "204.8k" },
      { value: 256000, label: "256k" },
      { value: 272000, label: "272k" },
      { value: 400000, label: "400k" },
      { value: 500000, label: "500k" },
      { value: 1000000, label: "1M" },
      { value: 1050000, label: "1.05M" },
    ]) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = Number(currentStatus.context_tier ?? 0) === tier.value ? "current" : "";
      button.textContent = tier.label;
      if (tier.value === 0) button.title = "使用模型完整窗口；较长输入可能增加延迟和费用";
      button.addEventListener("click", () => send({ type: "set_context_tier", window: tier.value }));
      context.appendChild(button);
    }
    const advancedActions = document.createElement("div");
    advancedActions.className = "runtime-advanced-actions";
    if (window.mewcodeDesktop) {
      const manage = document.createElement("button");
      manage.type = "button";
      manage.textContent = "管理桌面 Provider…";
      manage.addEventListener("click", () => {
        const anchor = state.anchor;
        hidePopover();
        void openProviderManager(anchor);
      });
      advancedActions.appendChild(manage);
    }
    const reset = document.createElement("button");
    reset.type = "button";
    reset.textContent = "重置 Effort 并重载 Provider";
    reset.addEventListener("click", () => {
      send({ type: "set_effort", level: "" });
      send({ type: "reload_providers" });
      state.feedback = "已请求重置 Effort 并重载 Provider";
      state.feedbackError = false;
      renderRuntimeSettingsPanel(state);
    });
    advancedActions.appendChild(reset);
    advancedBody.append(context, advancedActions);
    advanced.appendChild(advancedBody);
    root.appendChild(advanced);

    requestAnimationFrame(() => {
      positionPopover(state.anchor, state.placement);
      if (state.focusSearch) {
        const input = root.querySelector(".runtime-model-search");
        if (input) {
          input.focus();
          input.setSelectionRange(input.value.length, input.value.length);
        }
        state.focusSearch = false;
      }
    });
  }

  async function refreshRuntimeSettings(state, force = false, silent = false) {
    if (state.refreshing || runtimeSettingsView !== state) return;
    const previousSourceTime = modelCatalog && modelCatalog.intelligence &&
      modelCatalog.intelligence.source_updated_at;
    state.refreshing = true;
    state.feedbackError = false;
    if (!silent) state.feedback = "正在检查 CodeRadar 与模型目录…";
    renderRuntimeSettingsPanel(state);
    try {
      await loadModelCatalog(force);
      const radar = modelCatalog && modelCatalog.intelligence || {};
      if (!silent) {
        if (radar.status === "stale" || radar.status === "unavailable") {
          state.feedback = "刷新失败，已保留最近一次可用的 IQ 数据";
          state.feedbackError = true;
        } else if (previousSourceTime && previousSourceTime === radar.source_updated_at) {
          state.feedback = "已刷新，Radar 暂无新数据";
        } else {
          state.feedback = "IQ 已更新至 " + formatRadarTime(radar.source_updated_at);
        }
      }
    } catch (error) {
      state.feedback = "刷新失败：" + (error.message || String(error));
      state.feedbackError = true;
      if (!silent) notify(state.feedback, "error");
    } finally {
      state.refreshing = false;
      if (runtimeSettingsView === state) renderRuntimeSettingsPanel(state);
    }
  }

  function reconcileRuntimeSettingsState() {
    const state = runtimeSettingsView;
    if (!state || popover.classList.contains("hidden")) return;
    state.focusSearch = document.activeElement &&
      document.activeElement.classList.contains("runtime-model-search");
    if (state.pending) {
      if (state.pending.type === "model") {
        const succeeded = currentStatus.provider_key === state.pending.providerKey &&
          currentStatus.model === state.pending.modelId;
        if (!succeeded) {
          renderRuntimeSettingsPanel(state);
          return;
        }
        clearRuntimePendingTimer(state);
        state.providerKey = currentStatus.provider_key;
        const matchedFamily = runtimeProviderFamilies(modelCatalog || {}).find((item) =>
          item.providerKeys.includes(currentStatus.provider_key)
        );
        if (matchedFamily) state.familyKey = matchedFamily.key;
        state.modelId = currentStatus.model;
        state.feedback = "已切换到 " + currentStatus.model + "，Effort 使用 auto";
        state.feedbackError = false;
      } else {
        const actual = currentEfforts.current === "auto" ? "" : (currentEfforts.current || "");
        if (actual !== state.pending.effort) {
          renderRuntimeSettingsPanel(state);
          return;
        }
        state.feedback = "Effort 已设置为 " + (actual || "auto");
        state.feedbackError = false;
      }
      state.pending = null;
      state.pendingFailed = false;
    }
    renderRuntimeSettingsPanel(state);
  }

  // Prepared interaction choices are deliberately scoped to the exact
  // conversation, run, request and source spec. A refreshed bundle therefore
  // cannot silently reuse a choice or a draft from an older run/version.
  const orchestrationChoiceBundleCache = new Map();

  async function testProviderConnection(profile) {
    // Use the authenticated same-origin backend directly. Older native shells
    // have a shorter IPC timeout and hide the actual upstream failure behind
    // "error sending request". Saved secrets stay in the backend registry.
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 75000);
    try {
      const response = await fetch("/api/client/providers/test", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ profile }), signal: controller.signal,
      });
      const raw = await response.text();
      let result;
      try { result = JSON.parse(raw); }
      catch { throw new Error(`Provider 测试未返回有效结果（HTTP ${response.status}），请检查后端连接`); }
      if (!response.ok) {
        const detail = result.error || `HTTP ${response.status}`;
        if (response.status === 401 || response.status === 403) {
          throw new Error(`Provider 测试未通过桌面会话校验（${detail}）。请从桌面应用重新打开后再试。`);
        }
        throw new Error(detail);
      }
      return result;
    } catch (error) {
      if (error && error.name === "AbortError") {
        throw new Error("Provider 测试超过 75 秒；请检查后端及上游连接，不代表 API Key 已被判定无效");
      }
      throw error;
    } finally { clearTimeout(timer); }
  }

  const GROK_DESKTOP_PROVIDER_PRESET = Object.freeze({
    label: "desktop:example_provider_2",
    protocol: "openai",
    base_url: "https://api.x.ai/v1",
    default_model: "grok-4.6",
  });

  function mergeDesktopProviderPresets(rawPresets) {
    const presets = rawPresets && typeof rawPresets === "object"
      ? { ...rawPresets }
      : {};
    if (!presets.xai) presets.xai = { ...GROK_DESKTOP_PROVIDER_PRESET };
    if (!presets.typesafe) presets.typesafe = {
      label: "desktop:example_provider_7（评审）", protocol: "typesafe",
      base_url: "https://api.typesafe.ai/v1", default_model: "jev-latest",
    };
    return presets;
  }

  async function openProviderManager(anchor, options = {}) {
    const desktop = window.mewcodeDesktop;
    if (!desktop) {
      notify("当前环境不支持桌面 Provider 管理", "warning");
      return;
    }
    try {
      const [profiles, rawPresets] = await Promise.all([
        desktop.listProviders(), desktop.getProviderPresets(),
      ]);
      const presets = mergeDesktopProviderPresets(rawPresets);
      const requestedBrand = String(options.initialBrand || "").trim();
      openSettingsDialog("Provider 管理", anchor, (body, footer) => {
        const intro = document.createElement("div");
        intro.className = "settings-intro";
        intro.innerHTML = "<strong>跨品牌模型注册中心</strong><p>密钥由 Electron safeStorage 加密；Python 仅在当前进程内存中使用。官方 OpenRouter 测试连接只拉取账号模型目录；DeepSeek 等 OpenAI Compatible 会在测试时自动探测 /v1/models 并填入模型列表。保存前请填写默认模型。</p>";
        body.appendChild(intro);
        const list = document.createElement("div");
        list.className = "provider-profile-list";
        body.appendChild(list);
        let editProviderProfile = () => {};
        let testProviderProfile = async () => {};
        for (const profile of profiles || []) {
          const row = document.createElement("div");
          row.className = "provider-profile-row";
          row.dataset.providerKey = profile.provider_key;
          row.innerHTML = "<span><strong>" + esc(profile.name) + "</strong><small>" +
            esc(profile.default_model) + " · " + esc(profile.base_url || "") + "</small></span>";
          row.addEventListener("click", (event) => {
            if (event.target.closest("button")) return;
            editProviderProfile(profile);
          });
          const actions = document.createElement("div");
          actions.className = "provider-profile-actions";
          const edit = document.createElement("button");
          edit.type = "button"; edit.textContent = "编辑";
          edit.addEventListener("click", () => editProviderProfile(profile));
          const testSaved = document.createElement("button");
          testSaved.type = "button"; testSaved.textContent = "测试";
          testSaved.addEventListener("click", () => testProviderProfile(profile, testSaved));
          const remove = document.createElement("button");
          remove.type = "button"; remove.className = "provider-row-remove"; remove.textContent = "删除";
          remove.addEventListener("click", async () => {
            await desktop.removeProvider(profile.provider_key);
            modelCatalog = null;
            send({ type: "reload_providers" });
            closeSettingsDialog();
            openProviderManager(anchor);
          });
          actions.append(edit, testSaved, remove);
          row.appendChild(actions); list.appendChild(row);
        }
        const form = document.createElement("div");
        form.className = "provider-form settings-form-grid";
        body.appendChild(form);
        const editorTitle = document.createElement("strong");
        editorTitle.className = "provider-editor-title";
        form.appendChild(editorTitle);
        const makeField = (label, input) => {
          const wrap = document.createElement("label");
          wrap.innerHTML = "<span>" + esc(label) + "</span>";
          wrap.appendChild(input); form.appendChild(wrap);
          return wrap;
        };
        const brand = document.createElement("select");
        for (const [id, preset] of Object.entries(presets || {})) {
          const option = document.createElement("option"); option.value = id; option.textContent = preset.label; brand.appendChild(option);
        }
        if (requestedBrand && [...brand.options].some((option) => option.value === requestedBrand)) {
          brand.value = requestedBrand;
        }
        const contextChannel = document.createElement("select");
        for (const [value, label] of [
          ["auto", "自动（推荐）"],
          ["deepseek", "DeepSeek 定制"],
          ["generic", "通用上下文"],
        ]) {
          const option = document.createElement("option");
          option.value = value; option.textContent = label; contextChannel.appendChild(option);
        }
        const fields = {
          name: Object.assign(document.createElement("input"), { placeholder: "显示名称" }),
          base_url: Object.assign(document.createElement("input"), { placeholder: "https://…/v1" }),
          default_model: Object.assign(document.createElement("input"), { placeholder: "默认模型 ID" }),
          models: Object.assign(document.createElement("input"), { placeholder: "其他模型，逗号分隔" }),
          api_key: Object.assign(document.createElement("input"), { placeholder: "API Key", type: "password" }),
        };
        makeField("品牌预设", brand); makeField("显示名称", fields.name);
        makeField("API Endpoint", fields.base_url);
        const defaultModelWrap = makeField("默认模型", fields.default_model);
        const modelsWrap = makeField("手工模型列表", fields.models);
        makeField("API Key", fields.api_key);
        makeField("上下文通道", contextChannel);
        const openrouterWrap = document.createElement("label");
        openrouterWrap.innerHTML = "<span>默认模型</span>";
        const openrouterPicker = document.createElement("div");
        openrouterPicker.className = "provider-model-picker";
        const openrouterFilter = Object.assign(document.createElement("input"), {
          placeholder: "搜索模型",
        });
        const openrouterSelect = document.createElement("select");
        openrouterPicker.append(openrouterFilter, openrouterSelect);
        openrouterWrap.appendChild(openrouterPicker);
        form.appendChild(openrouterWrap);
        const testFeedback = document.createElement("p");
        testFeedback.className = "provider-test-feedback hidden";
        testFeedback.setAttribute("role", "status");
        testFeedback.setAttribute("aria-live", "polite");
        form.appendChild(testFeedback);
        const showTestFeedback = (message, state) => {
          testFeedback.textContent = message;
          testFeedback.className = "provider-test-feedback " + state;
        };
        let editingProviderKey = "";
        let editingHasSecret = false;
        let openrouterModels = [];
        let openrouterCatalogOk = false;
        let saveButton = null;
        const isOfficialOpenRouter = () => {
          if (brand.value !== "openrouter") return false;
          try {
            const host = new URL(fields.base_url.value.trim()).hostname.toLowerCase();
            return host === "openrouter.ai" || host === "www.openrouter.ai";
          } catch (_error) {
            return false;
          }
        };
        const fillOpenRouterSelect = (selected) => {
          const query = openrouterFilter.value.trim().toLowerCase();
          openrouterSelect.innerHTML = "";
          const placeholder = document.createElement("option");
          placeholder.value = "";
          placeholder.textContent = openrouterModels.length
            ? "请选择默认模型"
            : "请先测试连接以拉取账号模型列表";
          openrouterSelect.appendChild(placeholder);
          for (const id of openrouterModels) {
            if (query && !id.toLowerCase().includes(query)) continue;
            const option = document.createElement("option");
            option.value = id;
            option.textContent = id;
            openrouterSelect.appendChild(option);
          }
          if (selected && openrouterModels.includes(selected)) {
            if (![...openrouterSelect.options].some((option) => option.value === selected)) {
              const kept = document.createElement("option");
              kept.value = selected;
              kept.textContent = selected;
              openrouterSelect.appendChild(kept);
            }
            openrouterSelect.value = selected;
          } else {
            openrouterSelect.value = "";
          }
        };
        const syncOpenRouterForm = () => {
          const official = isOfficialOpenRouter();
          defaultModelWrap.classList.toggle("hidden", official);
          modelsWrap.classList.toggle("hidden", official);
          openrouterWrap.classList.toggle("hidden", !official);
          if (official) fillOpenRouterSelect(openrouterSelect.value || fields.default_model.value.trim());
          if (saveButton) saveButton.disabled = official && (!openrouterCatalogOk || !openrouterSelect.value);
        };
        const applyPreset = () => {
          const preset = presets[brand.value] || {};
          fields.name.value = preset.label || fields.name.value;
          fields.base_url.value = preset.base_url || fields.base_url.value;
          fields.default_model.value = preset.default_model || fields.default_model.value;
          if (!editingProviderKey) {
            openrouterModels = [];
            openrouterCatalogOk = false;
            openrouterSelect.value = "";
          }
          syncOpenRouterForm();
        };
        brand.addEventListener("change", applyPreset);
        fields.base_url.addEventListener("input", syncOpenRouterForm);
        openrouterFilter.addEventListener("input", () => fillOpenRouterSelect(openrouterSelect.value));
        openrouterSelect.addEventListener("change", syncOpenRouterForm);
        fields.api_key.addEventListener("input", () => {
          if (!isOfficialOpenRouter()) return;
          if (fields.api_key.value.trim()) {
            openrouterCatalogOk = false;
          } else if (editingHasSecret && openrouterModels.length) {
            openrouterCatalogOk = true;
          }
          syncOpenRouterForm();
        });
        applyPreset();
        const payload = () => {
          const official = isOfficialOpenRouter();
          const data = {
            provider_key: editingProviderKey || undefined,
            brand: brand.value, name: fields.name.value.trim(),
            protocol: (presets[brand.value] || {}).protocol,
            provider_type: (presets[brand.value] || {}).provider_type || "standard",
            base_url: fields.base_url.value.trim(),
            default_model: official ? openrouterSelect.value.trim() : fields.default_model.value.trim(),
            models: official
              ? openrouterModels.slice()
              : fields.models.value.split(",").map((value) => value.trim()).filter(Boolean),
            context_channel: contextChannel.value,
          };
          // Omit empty key so desktop restore keeps the previously saved secret.
          const secret = fields.api_key.value.trim();
          if (secret) data.api_key = secret;
          return data;
        };
        editProviderProfile = (profile) => {
          editingProviderKey = profile ? profile.provider_key : "";
          editingHasSecret = Boolean(profile && profile.has_secret);
          if (profile) {
            if ([...brand.options].some((option) => option.value === profile.brand)) {
              brand.value = profile.brand;
            }
            fields.name.value = profile.name || "";
            fields.base_url.value = profile.base_url || "";
            fields.default_model.value = profile.default_model || "";
            fields.models.value = (profile.models || []).join(", ");
            fields.api_key.value = "";
            fields.api_key.placeholder = profile.has_secret
              ? "留空则继续使用已保存密钥"
              : "API Key";
            contextChannel.value = profile.context_channel || "auto";
            editorTitle.textContent = "编辑 " + (profile.name || "Provider");
            openrouterModels = (profile.models || []).slice();
            openrouterCatalogOk = openrouterModels.length > 0;
            fillOpenRouterSelect(profile.default_model || "");
          } else {
            fields.name.value = "";
            fields.base_url.value = "";
            fields.default_model.value = "";
            fields.models.value = "";
            fields.api_key.value = "";
            fields.api_key.placeholder = "API Key";
            contextChannel.value = "auto";
            editorTitle.textContent = "新增 Provider";
            openrouterModels = [];
            openrouterCatalogOk = false;
            applyPreset();
            fillOpenRouterSelect("");
          }
          testFeedback.className = "provider-test-feedback hidden";
          syncOpenRouterForm();
          for (const row of list.querySelectorAll(".provider-profile-row")) {
            row.classList.toggle("editing", row.dataset.providerKey === editingProviderKey);
          }
        };
        testProviderProfile = async (profile, button) => {
          if (profile) editProviderProfile(profile);
          button.disabled = true;
          const official = isOfficialOpenRouter();
          showTestFeedback(
            official ? "正在拉取 OpenRouter 账号模型目录…" : "正在测试鉴权、流式响应和工具调用…",
            "pending",
          );
          try {
            const result = await testProviderConnection(payload());
            const latency = Number.isFinite(result.latency_ms) ? `（${result.latency_ms} ms）` : "";
            const detected = (result.models || []).map((value) => String(value).trim()).filter(Boolean);
            if (official && result.ok) {
              const previous = openrouterSelect.value || fields.default_model.value.trim();
              openrouterModels = detected;
              openrouterCatalogOk = openrouterModels.length > 0;
              fillOpenRouterSelect(previous);
              syncOpenRouterForm();
            } else if (!official && detected.length) {
              // Auto-detect OpenAI-compatible catalogs (DeepSeek, etc.).
              fields.models.value = detected.join(", ");
              if (!fields.default_model.value.trim()) {
                fields.default_model.value = detected[0];
              } else if (!detected.includes(fields.default_model.value.trim())) {
                // Keep the user's typed default even if the catalog omits it.
              }
            }
            const catalogHint = (!official && detected.length)
              ? ` 已自动填入 ${detected.length} 个模型。`
              : "";
            showTestFeedback(
              result.ok
                ? `${result.note || (official ? "已验证账号模型目录。" : "连接测试通过")}${catalogHint}${latency}`
                : `${result.error || "测试失败"}${catalogHint}`,
              result.ok ? "success" : "error",
            );
          } catch (error) {
            showTestFeedback(error.message || String(error), "error");
          } finally {
            button.disabled = false;
          }
        };
        const initialProfile = requestedBrand
          ? (profiles || []).find((profile) => profile.brand === requestedBrand) || null
          : (profiles || [])[0] || null;
        if (requestedBrand && !initialProfile) brand.value = requestedBrand;
        editProviderProfile(initialProfile);
        const cancel = document.createElement("button");
        cancel.type = "button"; cancel.className = "btn always"; cancel.textContent = "关闭";
        cancel.addEventListener("click", closeSettingsDialog);
        const add = document.createElement("button");
        add.type = "button"; add.className = "btn always"; add.textContent = "新增 Provider";
        add.addEventListener("click", () => editProviderProfile(null));
        const test = document.createElement("button");
        test.type = "button"; test.className = "btn always"; test.textContent = "测试连接";
        test.addEventListener("click", () => testProviderProfile(null, test));
        const save = document.createElement("button");
        save.type = "button"; save.className = "btn allow"; save.textContent = "加密保存";
        saveButton = save;
        syncOpenRouterForm();
        save.addEventListener("click", async () => {
          if (isOfficialOpenRouter() && (!openrouterCatalogOk || !openrouterSelect.value)) {
            showTestFeedback("请先测试连接并选择默认模型", "error");
            return;
          }
          save.disabled = true;
          try {
            const result = await desktop.saveProvider(payload());
            modelCatalog = null; send({ type: "reload_providers" }); closeSettingsDialog();
            notify(result.persisted ? "Provider 已加密保存" : "安全存储不可用，Provider 仅在本次会话中使用", result.persisted ? "success" : "warning");
          } catch (error) { showTestFeedback(error.message || String(error), "error"); }
          finally { save.disabled = false; }
        });
        footer.append(add, cancel, test, save);
      });
    } catch (error) {
      const message = error && error.message ? String(error.message) : String(error || "打开失败");
      if (/not allowed|plugin not found|permission/i.test(message)) {
        notify("桌面 Provider 桥接未授权，请重启刚编译的客户端后再试", "error");
      } else {
        notify(message, "error");
      }
    }
  }

  // ---------- 侧栏：项目 / 普通对话 / 最近 ----------

  function renderSidebarSkeleton() {
    const wrap = $("sidebar-groups");
    if (!wrap || wrap.children.length) return;
    wrap.innerHTML = "<div class='sidebar-skeleton' aria-label='正在加载会话列表'>" +
      "<span class='skeleton-line short'></span><span class='skeleton-line'></span>" +
      "<span class='skeleton-line'></span><span class='skeleton-line short'></span></div>";
  }

  let sidebarUnreadKeys = new Set();

  function conversationStateKey(c) {
    const identity = String(c.external_id || c.id || "").trim();
    return [
      String(c.source || "mewcode").trim() || "mewcode",
      String(c.node_id || "local").trim() || "local",
      identity,
    ].join("|");
  }

  function conversationIsUnread(c) {
    if (c && c.active) return false;
    return sidebarUnreadKeys.has(conversationStateKey(c));
  }

  async function loadSidebar() {
    renderSidebarSkeleton();
    try {
      const res = await fetch("/api/workspace-tree", { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const previousWorkDir = uiStateBucketKey();
      sidebarData = await res.json();
      if (uiStateBucketKey() !== previousWorkDir) {
        hydrateUiStateFromStorage();
      }
      sidebarUnreadKeys = new Set(
        Array.isArray(sidebarData.unread) ? sidebarData.unread.map(String) : []
      );
      const q = $("conv-search").value.trim().toLowerCase();
      renderWorkspaceTree(sidebarData.machines || [], q);
      renderEmptyState();
    } catch (error) {
      const wrap = $("sidebar-groups");
      if (wrap && wrap.querySelector(".sidebar-skeleton")) {
        wrap.innerHTML = "<div class='conv-empty'>会话列表加载失败</div>";
      }
      notify("会话列表加载失败，请稍后重试", "error");
    }
  }

  function activeWorkDir() {
    return String((sidebarData && sidebarData.work_dir) || currentStatus.work_dir || "");
  }

  function projectOnActivePath(project) {
    const work = activeWorkDir();
    if (!project) return false;
    if (project.active) return true;
    if (work && project.dir && String(project.dir) === work) return true;
    return false;
  }

  function sourceOnActivePath(machine, source) {
    return (source.projects || []).some((project) => projectOnActivePath(project));
  }

  function machineOnActivePath(machine) {
    if (machine && machine.kind === "local") return true;
    return (machine.sources || []).some((source) => sourceOnActivePath(machine, source));
  }

  function matchQ(c, q) {
    return !q || (c.title + " " + (c.summary || "")).toLowerCase().includes(q);
  }

  function treeMatches(item, q) {
    if (!q) return true;
    return String(item.label || item.title || item.id || "").toLowerCase().includes(q);
  }

  function convItem(c, opts) {
    const writable = opts.writable !== false && c.writable !== false && c.source === "mewcode" && (c.node_id || "local") === "local";
    const li = document.createElement("li");
    const done = !c.running && !c.active;
    const unread = conversationIsUnread(c);
    li.className = "conv-item" + (c.active ? " active" : "") +
      (c.running ? " running" : "") + (done ? " done" : "") + (writable ? "" : " is-readonly") +
      (unread ? " is-unread" : "");
    if (unread) li.title = (c.title || "") + "（未读）";
    li.innerHTML =
      "<div class='conv-title'>" + esc(c.title) + "</div>" +
      "<div class='conv-meta'><span class='conv-time'>" + esc(fmtTime(c.last_active)) + "</span>" +
      "<span class='conv-count'>" + (c.message_count || 0) + " 条</span>" +
      (writable && opts.scope !== "dir" ? "<button class='conv-del' title='删除' aria-label='删除对话'><svg class='ui-icon' aria-hidden='true'><use href='/static/icons.svg#close'></use></svg></button>" : "") +
      "</div>";
    li.addEventListener("click", (e) => {
      if (e.target.closest(".conv-del") || e.target.closest(".conv-context-menu")) return;
      if (isMobileDrawer()) setDrawer(false);
      openWorkspaceConversation(c, opts);
    });
    li.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      e.stopPropagation();
      showConversationContextMenu(e.clientX, e.clientY, c, opts, writable);
    });
    const del = li.querySelector(".conv-del");
    if (del) {
      del.addEventListener("click", async (e) => {
        e.stopPropagation();
        const confirmed = await askConfirm({
          title: "删除对话？",
          message: "删除后无法恢复这段会话。",
          confirmLabel: "删除",
          tone: "danger",
        });
        if (!confirmed) return;
        const url = "/api/conversations/" + opts.scope + "/" + c.id +
          (opts.dir ? "?dir=" + encodeURIComponent(opts.dir) : "");
        const res = await fetch(url, { method: "DELETE" });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || data.error) {
          addErrorMsg(data.error || "删除失败（状态码 " + res.status + "）");
          return;
        }
        // 删的是当前活动对话：清空消息区（服务端不再自动新建顶替，
        // 下次发送消息时才惰性创建新对话）
        if (c.active || data.active_deleted) {
          messagesEl.innerHTML = "";
          setReadonlyView(false);
          addSystemMsg("已删除当前对话，发送消息将开始新对话");
        }
        loadSidebar();
        refreshState();
      });
    }
    return li;
  }

  async function removeProjectFromSidebar(project) {
    const confirmed = await askConfirm({
      title: "移除项目？",
      message: "“" + project.label + "”将仅从项目列表中移除，不会删除本地目录、文件或历史会话。",
      confirmLabel: "移除",
      tone: "danger",
    });
    if (!confirmed) return;
    try {
      const response = await fetch("/api/projects/remove", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ dir: project.dir }),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok || result.error) {
        notify(result.error || "移除项目失败", "error");
        return;
      }
      delete projectOpenState[String(project.dir || "")];
      persistUiState();
      notify("已从列表移除项目 " + project.label, "success");
      await loadSidebar();
    } catch (error) {
      notify(error.message || "移除项目失败", "error");
    }
  }

  function projectConversationRefs(project) {
    return (project.conversations || []).map((c) => ({
      source: c.source || "mewcode",
      node_id: c.node_id || "local",
      external_id: c.external_id || c.id || "",
      conversation_id: c.id || "",
    }));
  }

  async function bulkSidebarConversationState(action, items) {
    const res = await fetch("/api/workspace/conversation-state", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: action, items: items }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.error) throw new Error(data.error || ("操作失败 " + res.status));
    return data;
  }

  function showProjectContextMenu(project, x, y, returnFocus) {
    const desktop = window.mewcodeDesktop;
    const canOpenDirectory = !!(desktop && typeof desktop.openDirectory === "function");
    const refs = projectConversationRefs(project);
    showContextPopover(x, y, project.label + " 项目操作", returnFocus, (menu) => {
      if (canOpenDirectory) {
        menu.appendChild(projectMenuItem("在资源管理器中打开", async () => {
          try {
            await desktop.openDirectory(project.dir);
          } catch (error) {
            notify(error.message || "无法打开项目目录", "error");
          }
        }));
      }
      menu.appendChild(projectMenuItem("全部标为已读", async () => {
        if (!refs.length) {
          notify("该项目没有可标记的对话", "warning");
          return;
        }
        const data = await bulkSidebarConversationState("mark_all_read", refs);
        notify("已将 " + (data.count || refs.length) + " 条对话标为已读", "success");
        await loadSidebar();
      }));
      menu.appendChild(projectMenuItem("全部归档", async () => {
        if (!refs.length) {
          notify("该项目没有可归档的对话", "warning");
          return;
        }
        const confirmed = await askConfirm({
          title: "全部归档？",
          message: "将归档「" + project.label + "」下的 " + refs.length + " 条对话（可从归档恢复的语义与单条归档相同）。",
          confirmLabel: "全部归档",
          tone: "danger",
        });
        if (!confirmed) return;
        const data = await bulkSidebarConversationState("archive_all", refs);
        notify("已归档 " + (data.count || refs.length) + " 条对话", "success");
        await loadSidebar();
      }));
      if (!project.active) {
        menu.appendChild(projectMenuItem(
          "从侧栏移除",
          () => removeProjectFromSidebar(project),
          "danger"
        ));
      }
    });
    return true;
  }

  function projectGroup(p, q) {
    const det = document.createElement("details");
    det.className = "side-group";
    const convs = p.conversations.filter((c) => matchQ(c, q));
    const dirKey = String(p.dir || "");
    const stored = projectOpenState[dirKey];
    det.open = q
      ? convs.length > 0
      : groupOpenDefault(stored !== undefined, stored, projectOnActivePath(p));
    det.addEventListener("toggle", () => {
      // 搜索时临时展开匹配项目，不覆盖用户平时的折叠偏好。
      if (!q) rememberProjectOpen(p.dir, det.open);
    });
    const head = document.createElement("summary");
    head.className = "side-group-title";
    head.innerHTML =
      "<span class='project-label' title='" + esc(p.dir) + "'>" + esc(p.label) + "</span>" +
      (p.active ? "<span class='project-active-dot' title='当前项目'></span>" : "") +
      "<button class='group-add' title='在此项目新建对话' aria-label='在 " +
      esc(p.label) + " 中新建对话'>＋</button>";
    head.addEventListener("contextmenu", (event) => {
      if (!showProjectContextMenu(p, event.clientX, event.clientY, head)) return;
      event.preventDefault();
      event.stopPropagation();
    });
    head.addEventListener("keydown", (event) => {
      if (event.key !== "ContextMenu" && !(event.shiftKey && event.key === "F10")) return;
      const rect = head.getBoundingClientRect();
      if (!showProjectContextMenu(p, rect.left + 20, rect.bottom + 2, head)) return;
      event.preventDefault();
      event.stopPropagation();
    });
    head.querySelector(".group-add").addEventListener("click", async (e) => {
      e.preventDefault();
      e.stopPropagation();
      const res = await fetch("/api/conversations/new", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scope: "project", dir: p.dir }),
      });
      const data = await res.json();
      if (data.error) {
        addErrorMsg(data.error);
        return;
      }
      activateConversation(data.id, false);
      setReadonlyView(false);
      messagesEl.innerHTML = "";
      loadSidebar();
      refreshState();
      inputEl.focus();
    });
    det.appendChild(head);
    const ul = document.createElement("ul");
    ul.className = "conv-list";
    if (!convs.length) {
      ul.innerHTML = "<li class='conv-empty'>暂无对话</li>";
    }
    for (const c of convs) {
      ul.appendChild(convItem(c, { scope: p.active ? "project" : "dir", dir: p.dir }));
    }
    det.appendChild(ul);
    return det;
  }

  function workspaceItemMatches(machine, source, project, conv, q) {
    if (!q) return true;
    return treeMatches(machine, q) || treeMatches(source, q) || treeMatches(project, q) || matchQ(conv, q);
  }

  function collapseAllWorkspaceGroups() {
    const wrap = $("sidebar-groups");
    if (!wrap) return;
    wrap.querySelectorAll("details.ws-machine, details.ws-source, details.side-group").forEach((node) => {
      node.open = false;
    });
    Object.keys(workspaceOpenState).forEach((key) => {
      workspaceOpenState[key] = false;
    });
    Object.keys(projectOpenState).forEach((key) => {
      projectOpenState[key] = false;
    });
    persistUiState();
    notify("已收起全部侧栏分组", "success");
  }

  function revealAddedProjectInSidebar(dir) {
    const search = $("conv-search");
    if (search) search.value = "";
    workspaceOpenState["machine:local"] = true;
    workspaceOpenState["source:local:mewcode"] = true;
    projectOpenState[String(dir || "")] = true;
    persistUiState();
  }

  async function addProjectFromToolbar(trigger) {
    // Use the same in-app existing-folder picker in browser, Electron and
    // Tauri.  The old Tauri-only PowerShell dialog could return no path and
    // silently stop before /api/projects/add, leaving no project registered.
    showProjectPicker(trigger);
  }

  function renderWorkspaceTree(machines, q) {
    const wrap = $("sidebar-groups");
    wrap.innerHTML = "";
    const toolbar = document.createElement("div");
    toolbar.className = "ws-toolbar";
    toolbar.innerHTML =
      "<span class='ws-toolbar-label'>项目</span>" +
      "<div class='ws-toolbar-actions'>" +
      "<button class='ws-tool-btn' id='collapse-all-projects' type='button' title='全部收起' aria-label='全部收起侧栏分组'>" +
      "<svg class='ui-icon' aria-hidden='true'><use href='/static/icons.svg#collapse-all'></use></svg></button>" +
      "<button class='ws-tool-btn' id='add-project' type='button' title='添加已有项目' aria-label='添加已有项目文件夹'>" +
      "<svg class='ui-icon' aria-hidden='true'><use href='/static/icons.svg#folder-plus'></use></svg></button>" +
      "</div>";
    wrap.appendChild(toolbar);
    toolbar.querySelector("#collapse-all-projects").addEventListener("click", (e) => {
      e.stopPropagation();
      collapseAllWorkspaceGroups();
    });
    toolbar.querySelector("#add-project").addEventListener("click", async (e) => {
      e.stopPropagation();
      try {
        await addProjectFromToolbar(e.currentTarget);
      } catch (error) {
        notify(error.message || "添加项目失败", "error");
      }
    });
    for (const machine of machines) {
      wrap.appendChild(machineGroup(machine, q));
    }
  }

  function machineGroup(machine, q) {
    const det = document.createElement("details");
    det.className = "ws-machine" + (machine.kind === "remote" ? " is-remote" : " is-local");
    const key = "machine:" + machine.id;
    const hasMatch = !q || (machine.sources || []).some((source) =>
      (source.projects || []).some((project) =>
        (project.conversations || []).some((conv) => workspaceItemMatches(machine, source, project, conv, q))
      ) || treeMatches(machine, q)
    );
    const stored = workspaceOpenState[key];
    det.open = q
      ? hasMatch
      : groupOpenDefault(stored !== undefined, stored, machineOnActivePath(machine));
    det.addEventListener("toggle", () => {
      if (!q) rememberWorkspaceOpen(key, det.open);
    });
    const head = document.createElement("summary");
    head.className = "ws-machine-title";
    head.innerHTML = "<span>" + esc(machine.label || "未命名机器") + "</span>" +
      (machine.error ? "<small class='ws-machine-error'>" + esc(machine.error) + "</small>" : "");
    det.appendChild(head);
    for (const source of machine.sources || []) {
      det.appendChild(sourceGroup(machine, source, q));
    }
    return det;
  }

  function sourceGroup(machine, source, q) {
    const det = document.createElement("details");
    det.className = "ws-source";
    const key = "source:" + machine.id + ":" + source.id;
    const hasMatch = !q || (source.projects || []).some((project) =>
      (project.conversations || []).some((conv) => workspaceItemMatches(machine, source, project, conv, q))
    ) || treeMatches(source, q);
    const stored = workspaceOpenState[key];
    det.open = q
      ? hasMatch
      : groupOpenDefault(stored !== undefined, stored, sourceOnActivePath(machine, source));
    det.addEventListener("toggle", () => {
      if (!q) rememberWorkspaceOpen(key, det.open);
    });
    const head = document.createElement("summary");
    head.className = "ws-source-title";
    head.textContent = source.label || source.id;
    det.appendChild(head);
    for (const project of source.projects || []) {
      det.appendChild(workspaceProjectGroup(machine, source, project, q));
    }
    if (!(source.projects || []).length) {
      const empty = document.createElement("div");
      empty.className = "conv-empty";
      empty.textContent = "暂无对话";
      det.appendChild(empty);
    }
    return det;
  }

  function workspaceProjectGroup(machine, source, project, q) {
    const localMew = machine.kind === "local" && source.id === "mewcode";
    if (localMew && project.dir) {
      return projectGroup({
        dir: project.dir,
        label: project.label,
        active: project.dir === sidebarData.work_dir,
        conversations: (project.conversations || []).map((item) => ({
          ...item,
          source: "mewcode",
          writable: true,
          node_id: "local",
        })),
      }, q);
    }
    const det = document.createElement("details");
    const kind = project.kind || (project.dir ? "project" : "chats");
    det.className = "side-group" + (kind === "chats" ? " is-chats" : " is-project");
    const convs = (project.conversations || []).filter((c) => workspaceItemMatches(machine, source, project, c, q));
    const key = "project:" + machine.id + ":" + source.id + ":" + project.id;
    const stored = workspaceOpenState[key];
    det.open = q
      ? convs.length > 0
      : groupOpenDefault(stored !== undefined, stored, projectOnActivePath(project));
    det.addEventListener("toggle", () => {
      if (!q) rememberWorkspaceOpen(key, det.open);
    });
    const head = document.createElement("summary");
    head.className = "side-group-title";
    const iconId = kind === "chats" ? "chat" : "folder";
    head.innerHTML = "<svg class='ui-icon project-kind-icon' aria-hidden='true'><use href='/static/icons.svg#" + iconId + "'></use></svg>" +
      "<span class='project-label'>" + esc(project.label || "普通对话") + "</span>" +
      (localMew && !project.dir ? "<button class='group-add' title='新建普通对话' type='button'>＋</button>" : "");
    det.appendChild(head);
    const add = head.querySelector(".group-add");
    if (add) {
      add.addEventListener("click", async (e) => {
        e.preventDefault();
        e.stopPropagation();
        const res = await fetch("/api/conversations/new", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ scope: "normal" }),
        });
        const data = await res.json();
        if (data.error) {
          addErrorMsg(data.error);
          return;
        }
        activateConversation(data.id, false);
        setReadonlyView(false);
        messagesEl.innerHTML = "";
        loadSidebar();
        refreshState();
        inputEl.focus();
      });
    }
    const ul = document.createElement("ul");
    ul.className = "conv-list";
    if (!convs.length) ul.innerHTML = "<li class='conv-empty'>暂无对话</li>";
    for (const c of convs) {
      ul.appendChild(convItem(c, {
        scope: c.scope === "user" ? "normal" : (c.scope || "project"),
        dir: c.dir || project.dir,
        writable: c.writable,
      }));
    }
    det.appendChild(ul);
    return det;
  }

﻿  let conversationContextMenuEl = null;
  function hideConversationContextMenu() {
    if (conversationContextMenuEl) {
      conversationContextMenuEl.remove();
      conversationContextMenuEl = null;
    }
  }
  document.addEventListener("click", hideConversationContextMenu);
  document.addEventListener("scroll", hideConversationContextMenu, true);

  async function patchSidebarConversationState(action, c) {
    const res = await fetch("/api/workspace/conversation-state", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action: action,
        source: c.source || "mewcode",
        node_id: c.node_id || "local",
        external_id: c.external_id || c.id || "",
        conversation_id: c.id || "",
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.error) throw new Error(data.error || ("操作失败 " + res.status));
    return data;
  }

  function showConversationContextMenu(x, y, c, opts, writable) {
    hideConversationContextMenu();
    const menu = document.createElement("div");
    menu.className = "conv-context-menu";
    menu.style.left = Math.min(x, window.innerWidth - 180) + "px";
    menu.style.top = Math.min(y, window.innerHeight - 120) + "px";
    const isExternal = (c.source || "mewcode") !== "mewcode" || (c.node_id || "local") !== "local" || c.writable === false;
    const mk = (label, cls, handler) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = label;
      if (cls) btn.className = cls;
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        hideConversationContextMenu();
        try { await handler(); } catch (err) { notify(err.message || String(err), "error"); }
      });
      menu.appendChild(btn);
    };
    if (writable && !isExternal) {
      mk("归档", "", async () => {
        await patchSidebarConversationState("archive", c);
        notify("已归档", "success");
        loadSidebar();
      });
      mk("删除", "danger", async () => {
        const confirmed = await askConfirm({
          title: "删除对话？",
          message: "删除后无法恢复这段会话。",
          confirmLabel: "删除",
          tone: "danger",
        });
        if (!confirmed) return;
        const url = "/api/conversations/" + opts.scope + "/" + c.id +
          (opts.dir ? "?dir=" + encodeURIComponent(opts.dir) : "");
        const res = await fetch(url, { method: "DELETE" });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || data.error) throw new Error(data.error || "删除失败");
        if (c.active || data.active_deleted) {
          messagesEl.innerHTML = "";
          setReadonlyView(false);
          addSystemMsg("已删除当前对话，发送消息将开始新对话");
        }
        loadSidebar();
        refreshState();
      });
    } else {
      mk("从侧栏隐藏", "danger", async () => {
        await patchSidebarConversationState("hide", c);
        notify("已从侧栏隐藏（未删除源文件）", "success");
        loadSidebar();
      });
      mk("归档", "", async () => {
        await patchSidebarConversationState("archive", c);
        notify("已归档", "success");
        loadSidebar();
      });
    }
    document.body.appendChild(menu);
    conversationContextMenuEl = menu;
  }

  function attachAssistantCopyButton(wrap, text) {
    const body = String(text || "").trim();
    if (!body || !wrap) return;
    if (wrap.closest(".tool-group, .tg-row, .expert-tool-note")) return;
    const replies = [...wrap.querySelectorAll(".msg-bubble > .md")].filter((el) =>
      String(el.innerText || el.textContent || "").trim()
    );
    const lastMd = replies[replies.length - 1];
    if (!lastMd) return;
    let actions = lastMd.nextElementSibling && lastMd.nextElementSibling.classList.contains("msg-actions")
      ? lastMd.nextElementSibling
      : wrap.querySelector(".msg-actions");
    if (!actions) {
      actions = document.createElement("div");
      actions.className = "msg-actions";
    }
    lastMd.insertAdjacentElement("afterend", actions);
    if (actions.querySelector(".msg-copy-btn")) return;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "msg-copy-btn";
    btn.title = "复制回复";
    btn.innerHTML = "<svg class='ui-icon' aria-hidden='true'><use href='/static/icons.svg#copy'></use></svg><span>复制</span>";
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      try {
        await navigator.clipboard.writeText(body);
        notify("已复制助手回复", "success");
      } catch (err) {
        notify("复制失败", "error");
      }
    });
    actions.prepend(btn);
  }

  async function openWorkspaceConversation(c, opts) {
    // Read-state bookkeeping must never sit in front of navigation.  In
    // particular, a busy backend used to make the mark-read request hang and
    // the actual conversation switch was never attempted.
    sidebarUnreadKeys.delete(conversationStateKey(c));
    void patchSidebarConversationState("mark_read", c).catch(() => {
      // 已读标记失败不阻断打开对话。
    });
    const localWritable = (c.node_id || "local") === "local" && c.source === "mewcode" && c.writable !== false;
    if (localWritable) {
      setReadonlyView(false);
      if (opts.scope === "dir") return openForeignConversation(opts.dir, c.id);
      return openConversation(c.scope === "user" ? "normal" : (opts.scope || "project"), c.id);
    }
    await openExternalConversation(c);
  }

  async function openExternalConversation(item) {
    const navigationToken = beginConversationNavigation();
    window.MewChatViewport?.select('external:' + [item.node_id, item.source, item.external_id || item.id].join(':'));
    setReadonlyView(true, item);
    currentStatus.conversation_title = (externalSourceLabel(item.source) + " / " + (item.title || "未命名对话"));
    $("topbar-conversation").textContent = currentStatus.conversation_title;
    messagesEl.innerHTML = "";
    addSystemMsg("正在加载只读对话…");
    try {
      const nodeId = item.node_id || "local";
      const source = item.source || "codex";
      const externalId = item.external_id || item.id;
      const url = nodeId === "local"
        ? "/api/external-agents/" + encodeURIComponent(source) + "/" + encodeURIComponent(externalId)
        : "/api/tailscale/nodes/" + encodeURIComponent(nodeId) + "/history/item?source=" +
          encodeURIComponent(source) + "&external_id=" + encodeURIComponent(externalId);
      const res = await fetch(url, { cache: "no-store" });
      const data = await res.json();
      if (navigationToken !== conversationNavigationToken) return;
      if (!res.ok) throw new Error(data.error || "加载失败");
      messagesEl.innerHTML = "";
      const messages = data.messages || data.entries || [];
      if (!messages.length) renderEmptyState();
      for (const message of messages) {
        if (message.kind && message.kind !== "message") continue;
        const role = message.role || "";
        const text = message.text || message.content || "";
        if (role === "user") addUserMsg(text, message.attachments || []);
        else if (role === "assistant") {
          renderHistoryAssistant({ text, tools: message.tools || [] });
        }
      }
      scrollBottom(true);
    } catch (error) {
      if (navigationToken === conversationNavigationToken) {
        messagesEl.innerHTML = "";
        addErrorMsg(error.message || String(error));
      }
    } finally {
      endConversationNavigation(navigationToken);
    }
  }

  // 添加项目：路径输入 + 目录浏览
  let browsePath = "";
  function showProjectPicker(anchor) {
    showPopover(anchor, async (pop) => {
      browsePath = "";
      pop.appendChild(popTitle("选择已有项目文件夹"));
      const row = document.createElement("div");
      row.className = "browse-row";
      row.innerHTML =
        "<input id='browse-input' type='text' placeholder='输入文件夹路径…'>" +
        "<button id='browse-go'>进入</button>";
      pop.appendChild(row);
      const list = document.createElement("div");
      list.id = "browse-list";
      list.className = "browse-list";
      pop.appendChild(list);
      const foot = document.createElement("div");
      foot.className = "browse-foot";
      foot.innerHTML =
        "<span id='browse-cur'></span>" +
        "<button id='browse-pick' class='btn allow' disabled>添加此文件夹</button>";
      pop.appendChild(foot);

      const input = row.querySelector("#browse-input");
      const cur = foot.querySelector("#browse-cur");
      const pick = foot.querySelector("#browse-pick");
      async function go(path) {
        pick.disabled = true;
        const res = await fetch("/api/browse?path=" + encodeURIComponent(path || ""));
        const data = await res.json();
        if (data.error) {
          browsePath = "";
          list.innerHTML = "<div class='conv-empty'>" + esc(data.error) + "</div>";
          return;
        }
        browsePath = data.path;
        pick.disabled = false;
        cur.textContent = data.path;
        input.value = data.path;
        list.innerHTML = "";
        if (data.parent) {
          const up = document.createElement("div");
          up.className = "pop-item";
          up.textContent = "..（上级）";
          up.addEventListener("click", () => go(data.parent));
          list.appendChild(up);
        }
        for (const d of data.dirs || []) {
          const item = document.createElement("div");
          item.className = "pop-item";
          item.textContent = "▣ " + d;
          item.addEventListener("click", () =>
            go(data.path.replace(/[\\/]+$/, "") + "\\" + d));
          list.appendChild(item);
        }
        if (!(data.dirs || []).length) {
          const empty = document.createElement("div");
          empty.className = "conv-empty";
          empty.textContent = "（无子目录）";
          list.appendChild(empty);
        }
      }
      row.querySelector("#browse-go").addEventListener("click", () => go(input.value));
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") go(input.value);
      });
      pick.addEventListener("click", async () => {
        if (!browsePath) return;
        pick.disabled = true;
        const res = await fetch("/api/projects/add", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ dir: browsePath }),
        });
        const data = await res.json();
        if (data.error) {
          pick.disabled = false;
          addErrorMsg(data.error);
          return;
        }
        hidePopover();
        addSystemMsg("已添加项目: " + data.label);
        revealAddedProjectInSidebar(data.dir);
        await loadSidebar();
      });
      // 初始路径：当前工作目录的父级
      const start = sidebarData.work_dir
        ? sidebarData.work_dir.replace(/[\\/][^\\/]+$/, "")
        : "";
      go(start || "");
    }, "down");
  }

  $("conv-search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(loadSidebar, 250);
  });

  $("new-chat").addEventListener("click", async () => {
    // 顶部“新建对话”默认在当前激活项目内创建
    const navigationToken = beginConversationNavigation();
    try {
      const res = await fetch("/api/conversations/new", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scope: "project" }),
      });
      const data = await res.json();
      if (navigationToken !== conversationNavigationToken) return;
      if (data.error) {
        addSystemMsg(data.error);
        return;
      }
      activateConversation(data.id, false);
      setReadonlyView(false);
      messagesEl.innerHTML = "";
      resetPipeline(data.id, []);
      renderEmptyState();
      loadSidebar();
      refreshState(navigationToken);
      inputEl.focus();
    } catch (error) {
      if (navigationToken === conversationNavigationToken) {
        addSystemMsg("新建对话失败: " + (error.message || String(error)));
      }
    } finally {
      endConversationNavigation(navigationToken);
    }
  });

  async function openConversation(scope, id) {
    const apiScope = scope === "normal" ? "normal" : "project";
    if (conversationLoadController) conversationLoadController.abort();
    const controller = new AbortController();
    const navigationToken = beginConversationNavigation();
    conversationLoadController = controller;
    try {
      const res = await fetch("/api/conversations/" + apiScope + "/" + id, {
        signal: controller.signal,
      });
      const data = await res.json();
      if (
        controller.signal.aborted ||
        conversationLoadController !== controller ||
        navigationToken !== conversationNavigationToken
      ) return;
      if (data.error) {
        addSystemMsg("加载失败: " + data.error);
        return;
      }
      window.MewChatViewport.navigate(data.id, () => {
        activateConversation(data.id, !!data.running);
        setReadonlyView(false);
        renderEntries(data.entries || [], data.expert_runs || []);
        resetPipeline(data.id, data.pipeline_events || []);
        rehydrateExpertRuns(data.id, data.expert_runs || []);
        replayConversationEvents(data.id);
      });
      replayPendingConversationPrompt();
      loadSidebar();
      refreshState(navigationToken);
    } catch (error) {
      if (
        error.name !== "AbortError" &&
        navigationToken === conversationNavigationToken
      ) addSystemMsg("加载失败: " + (error.message || String(error)));
    } finally {
      if (conversationLoadController === controller) {
        conversationLoadController = null;
        endConversationNavigation(navigationToken);
      }
    }
  }

  async function openForeignConversation(dir, id) {
    // 非激活项目的会话：先激活该项目再加载（可继续对话）
    const navigationToken = beginConversationNavigation();
    try {
      const res = await fetch("/api/projects/activate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ dir }),
      });
      const data = await res.json();
      if (navigationToken !== conversationNavigationToken) return;
      if (data.error) {
        addErrorMsg(data.error);
        return;
      }
      setReadonlyView(false);
      await openConversation("project", id);
    } catch (error) {
      if (navigationToken === conversationNavigationToken) {
        addErrorMsg("切换项目失败: " + (error.message || String(error)));
      }
    } finally {
      // openConversation owns the newer token while it loads.  This call is
      // intentionally token-guarded so an older project switch cannot unlock
      // a newer navigation.
      endConversationNavigation(navigationToken);
    }
  }

  function renderEntries(entries, expertRuns = []) {
    messagesEl.innerHTML = "";
    for (const state of expertTimelines.values()) {
      for (const timer of state.textRenderTimers.values()) clearTimeout(timer);
    }
    expertTimelines.clear();
    currentExpertTimeline = null;
    orchChatBubbles.clear();
    orchChatLiveKeys.clear();
    recoveryCases.clear();
    const snapshotsByIndex = new Map();
    // display_index used to track raw session message_count (incl. tools),
    // which drifts above the user/assistant entry list. Clamp so historical
    // expert runs still render after restart instead of vanishing.
    const sortedRuns = [...(expertRuns || [])].sort((left, right) => (
      String(left.updated_at || left.created_at || "").localeCompare(
        String(right.updated_at || right.created_at || "")
      )
    ));
    for (const snapshot of sortedRuns) {
      let index = Number(snapshot.display_index);
      if (!Number.isFinite(index) || index < 0 || index > entries.length) {
        index = entries.length;
      }
      const group = snapshotsByIndex.get(index) || [];
      group.push(snapshot);
      snapshotsByIndex.set(index, group);
    }
    for (let index = 0; index <= entries.length; index += 1) {
      for (const snapshot of (snapshotsByIndex.get(index) || [])) {
        renderExpertSnapshot(snapshot);
      }
      if (index === entries.length) break;
      const entry = entries[index];
      if (entry.role === "user") {
        addUserMsg(entry.text, entry.attachments || []);
      } else {
        renderHistoryAssistant(entry);
      }
    }
    if (!entries.length && !expertRuns.length) renderEmptyState();
    renderPipeline();
    scrollBottom(true);
  }

  function renderHistoryAssistant(entry) {
    const hasText = !!(entry.text && entry.text.trim());
    const tools = entry.tools || [];
    const thinking = String(entry.thinking || "").trim();
    const changes = entry.changes || [];
    if (!hasText && !tools.length && !thinking && !changes.length) return;
    const wrap = document.createElement("div");
    wrap.className = "msg assistant";
    const bubble = document.createElement("div");
    bubble.className = "msg-bubble";
    if (thinking) {
      bubble.appendChild(buildThinkingBlock(thinking, { done: true }));
    }
    if (hasText) {
      const md = document.createElement("div");
      md.className = "md";
      md.innerHTML = renderMarkdown(entry.text);
      bubble.appendChild(md);
      enhanceMath(md);
    }
    if (tools.length) {
      bubble.appendChild(buildToolGroup(tools.map((t) => ({
        name: t.name, args: t.args, result: t.result,
        is_error: t.is_error, elapsed: null, done: true,
      }))));
    }
    if (changes.length) {
      bubble.appendChild(buildTurnChangesCard(changes));
    }
    wrap.appendChild(bubble);
    if (hasText) attachAssistantCopyButton(wrap, entry.text);
    messagesEl.appendChild(wrap);
  }

  // ---------- 消息渲染 ----------

  function renderMessageAttachments(container, attachments) {
    if (!attachments || !attachments.length) return;
    const gallery = document.createElement("div");
    gallery.className = "message-attachments";
    for (const attachment of attachments) {
      const card = document.createElement(attachment.preview_url ? "a" : "div");
      card.className = "message-attachment" + (attachment.kind === "image" ? " is-image" : "");
      if (attachment.preview_url) {
        card.href = attachment.preview_url;
        card.download = attachment.name || "attachment";
      }
      if (attachment.kind === "image" && attachment.preview_url) {
        const image = document.createElement("img");
        image.src = attachment.preview_url;
        image.alt = attachment.name || "图片附件";
        image.loading = "lazy";
        card.appendChild(image);
      } else {
        const icon = document.createElement("span");
        icon.className = "attachment-kind-icon";
        icon.textContent = ({ pdf: "PDF", docx: "DOC", spreadsheet: "XLS", text: "TXT" })[attachment.kind] || "FILE";
        card.appendChild(icon);
      }
      const meta = document.createElement("span");
      meta.innerHTML = "<strong>" + esc(attachment.name || "附件") + "</strong><small>" +
        esc(formatFileSize(attachment.size || 0) + (attachment.truncated ? " · 已截断" : "")) + "</small>";
      card.appendChild(meta);
      gallery.appendChild(card);
    }
    container.appendChild(gallery);
  }

  // ---------- 对话折叠 ----------
  // 每条 .msg 都可折叠成一行摘要（长回复 / 编排执行记录占屏时很有用）。
  // 用 MutationObserver 统一装饰，覆盖现有和将来新增的所有消息类型。

  const MSG_FOLD_ROLE_LABELS = [
    ["expert-message", "编排执行记录"],
    ["orch-stage-msg", "阶段消息"],
    ["user", "你"],
    ["assistant", "AI"],
    ["system", "系统"],
    ["error", "错误"],
  ];

  function messageFoldRoleLabel(msgEl) {
    for (const [cls, label] of MSG_FOLD_ROLE_LABELS) {
      if (msgEl.classList.contains(cls)) return label;
    }
    return "消息";
  }

  function messageFoldExcerpt(msgEl) {
    const text = String(msgEl.textContent || "").replace(/\s+/g, " ").trim();
    if (!text) return "（空）";
    return text.length > 80 ? text.slice(0, 80) + "…" : text;
  }

  function decorateFoldableMessage(msgEl) {
    if (!(msgEl instanceof HTMLElement)) return;
    if (!msgEl.classList.contains("msg")) return;
    if (msgEl.classList.contains("msg-fold-ready")) return;
    msgEl.classList.add("msg-fold-ready");

    const label = messageFoldRoleLabel(msgEl);
    const excerpt = document.createElement("span");
    excerpt.className = "msg-fold-excerpt";

    const summary = document.createElement("button");
    summary.type = "button";
    summary.className = "msg-fold-summary";
    summary.hidden = true;
    summary.title = "点击展开";
    const tag = document.createElement("span");
    tag.className = "msg-fold-tag";
    tag.textContent = label;
    summary.append(tag, excerpt);

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "msg-fold-btn";

    const setCollapsed = (collapsed) => {
      msgEl.classList.toggle("collapsed", collapsed);
      if (collapsed) excerpt.textContent = messageFoldExcerpt(msgEl);
      btn.textContent = collapsed ? "▸" : "▾";
      btn.title = collapsed ? "展开这条消息" : "折叠这条消息";
      btn.setAttribute("aria-label", btn.title);
      summary.hidden = !collapsed;
      if (collapsed) scrollBottom();
    };

    btn.addEventListener("click", () => {
      setCollapsed(!msgEl.classList.contains("collapsed"));
    });
    summary.addEventListener("click", () => setCollapsed(false));

    msgEl.append(summary, btn);
  }

  const msgFoldObserver = new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      for (const node of mutation.addedNodes) {
        if (node instanceof HTMLElement) {
          if (node.classList.contains("msg")) decorateFoldableMessage(node);
          node.querySelectorAll?.(":scope > .msg").forEach(decorateFoldableMessage);
        }
      }
    }
  });
  msgFoldObserver.observe(messagesEl, { childList: true });
  messagesEl.querySelectorAll(":scope > .msg").forEach(decorateFoldableMessage);

  function addUserMsg(text, attachments = [], destination = messagesEl, forceScroll = true) {
    if (!String(text || "").trim() && !attachments?.length) return null;
    hideEmptyState();
    const div = document.createElement("div");
    div.className = "msg user";
    div.mewRawUserText = String(text || '');
    queueMicrotask(() => window.dispatchEvent(new CustomEvent('mewcode:user-message')));
    const bubble = document.createElement("div");
    bubble.className = "msg-bubble";
    if (text) {
      const value = document.createElement("div");
      value.className = "user-message-text md";
      value.innerHTML = renderMarkdown(text);
      bubble.appendChild(value);
      enhanceMath(value);
    }
    renderMessageAttachments(bubble, attachments);
    div.appendChild(bubble);
    destination.appendChild(div);
    // 发送新消息：无条件回到底部并恢复跟随
    scrollBottom(forceScroll);
    return div;
  }

  function isToolDenialNote(text) {
    const value = String(text || "").trim();
    return value.startsWith("AUTO_DENIED:") || value.startsWith("Permission denied:");
  }

  function addSystemMsg(text) {
    hideEmptyState();
    const div = document.createElement("div");
    div.className = "msg system";
    div.textContent = text;
    messagesEl.appendChild(div);
    scrollBottom();
  }

  function addHandoffReviewCard(runId, progress, conversationId) {
    if (!progress || !progress.source_id) return;
    hideEmptyState();
    const card = document.createElement("div");
    card.className = "msg handoff-review-card";
    card.setAttribute("role", "group");
    card.setAttribute("aria-label", "历史暂停接力审阅归档");
    const heading = document.createElement("div");
    heading.className = "handoff-review-heading";
    const title = document.createElement("strong");
    title.textContent = "历史暂停接力审阅（已停用）";
    const status = document.createElement("span");
    status.className = "handoff-review-status";
    status.textContent = "只读归档";
    heading.append(title, status);
    card.appendChild(heading);
    const note = document.createElement("p");
    note.className = "handoff-review-note";
    note.textContent = "旧分段审阅已停用。历史分类、待核计数和语义额度不再作为恢复前置条件；" +
      "使用“继续任务”进入当前宿主恢复检查。封存来源仍可只读回看。";
    card.appendChild(note);
    const actions = document.createElement("div");
    actions.className = "handoff-review-actions";
    const button = (label, payload) => {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "handoff-review-button";
      item.textContent = label;
      item.addEventListener("click", () => send({run_id: runId,
        conversation_id: conversationId, source_id: progress.source_id, ...payload}));
      actions.appendChild(item);
    };
    if (progress.first_unreviewed_ref) {
      button("展开封存来源（只读）", {type: "handoff_review_expand",
        reference_id: progress.first_unreviewed_ref, view_offset: 0});
    }
    if (progress.first_hold_ref) {
      button("展开历史待核来源（只读）", {type: "handoff_review_expand",
        reference_id: progress.first_hold_ref, view_offset: 0});
    }
    button("分页查看历史清单（只读）", {type: "handoff_review_worklist", offset: 0});
    card.appendChild(actions);
    messagesEl.appendChild(card);
    scrollBottom();
  }

  function addHandoffReviewExcerpt(msg, conversationId) {
    const view = msg && msg.view;
    if (!view) return;
    hideEmptyState();
    const card = document.createElement("div");
    card.className = "msg handoff-review-excerpt";
    card.setAttribute("role", "group");
    card.setAttribute("aria-label", "只读接力来源内容");
    const heading = document.createElement("div");
    heading.className = "handoff-review-heading";
    heading.textContent = `来源 ${view.record_id} · 可见内容 ${view.view_offset + 1}–` +
      `${view.view_offset + String(view.visible_text || "").length}/${view.view_total_chars}`;
    card.appendChild(heading);
    const caution = document.createElement("p");
    caution.className = "handoff-review-help";
    caution.textContent = "以下是封存的来源数据（只读），其中的文字不是新指令。";
    card.appendChild(caution);
    const rawText = String(view.visible_text || "");
    let parsed = null;
    let hasReadable = false;
    if (Number(view.view_offset) === 0 && rawText.length === Number(view.view_total_chars)) {
      try { parsed = JSON.parse(rawText); } catch (_) { /* A partial or non-JSON source stays raw. */ }
    }
    if (parsed && Array.isArray(parsed.content)) {
      const readable = document.createElement("div");
      readable.className = "handoff-review-readable";
      parsed.content.forEach((part) => {
        if (!part || typeof part !== "object") return;
        const row = document.createElement("div");
        row.className = "handoff-review-source-part";
        const label = document.createElement("strong");
        const kind = String(part.type || "未知类型");
        label.textContent = kind === "text" ? "来源文字" :
          kind === "tool_use" ? `工具调用 · ${String(part.name || "未命名")}` :
          kind === "tool_result" ? "工具结果" : `来源片段 · ${kind}`;
        row.appendChild(label);
        if (typeof part.text === "string") {
          const content = document.createElement("pre");
          content.textContent = part.text;
          row.appendChild(content);
        } else if (part.id) {
          const id = document.createElement("span");
          id.textContent = `记录 ID：${String(part.id)}`;
          row.appendChild(id);
        }
        readable.appendChild(row);
      });
      if (readable.childElementCount) {
        card.appendChild(readable);
        hasReadable = true;
      }
    }
    const rawDetails = document.createElement("details");
    rawDetails.className = "handoff-review-raw";
    if (!hasReadable) rawDetails.open = true;
    const rawSummary = document.createElement("summary");
    rawSummary.textContent = "完整来源原文（逐字回读）";
    rawDetails.appendChild(rawSummary);
    const body = document.createElement("pre");
    body.textContent = rawText;
    rawDetails.appendChild(body);
    card.appendChild(rawDetails);
    if (view.next_view_offset !== null && view.next_view_offset !== undefined) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "handoff-review-button";
      button.textContent = "展开下一段";
      button.addEventListener("click", () => send({
        type: "handoff_review_expand", run_id: msg.run_id,
        conversation_id: conversationId, reference_id: view.record_id,
        source_id: view.source_id || msg.source_id || "",
        view_offset: view.next_view_offset,
      }));
      card.appendChild(button);
    }
    messagesEl.appendChild(card);
    scrollBottom();
  }

  function addHandoffReviewWorklist(msg, conversationId) {
    const page = msg && msg.page;
    if (!page || !Array.isArray(page.items)) return;
    hideEmptyState();
    const card = document.createElement("div");
    card.className = "msg handoff-review-card";
    card.setAttribute("role", "group");
    card.setAttribute("aria-label", "历史暂停接力对账清单（只读）");
    const heading = document.createElement("div");
    heading.className = "handoff-review-heading";
    heading.textContent = `历史对账清单 ${Number(page.offset) + 1}–` +
      `${Number(page.offset) + page.items.length}/${Number(page.total)}` +
      ` · 宿主已核实 ${Number(page.verified_count || 0)} · 未决 ${Number(page.remaining_count || 0)}`;
    card.appendChild(heading);
    const caution = document.createElement("p");
    caution.className = "handoff-review-warning";
    caution.textContent = "这是旧分段审阅的只读记录。未核验副作用保持原有未知状态，历史计数不再阻塞当前恢复；恢复检查和业务验收由现行宿主机制处理。";
    card.appendChild(caution);
    const reconciliationReasons = {
      call_provenance_unverified: "调用来源未核实",
      call_result_pair_unverified: "调用与结果无法唯一配对",
      conflicting_duplicate_results: "同一调用的结果记录冲突",
      error_effect_unverified: "报错调用的副作用未知",
      tool_effect_scope_unverified: "工具副作用范围未知",
      host_file_receipt_unverified: "宿主文件回执未通过核验",
      current_write_intent_unverified: "当前文件与原始写入意图不一致",
      source_unparseable: "来源无法解析",
    };
    page.items.forEach(item => {
      const row = document.createElement("div");
      row.className = "handoff-review-source-part";
      const title = document.createElement("strong");
      title.textContent = `${String(item.record_type)} · ${String(item.reason)} · ` +
        `${(item.tools || []).join(", ")}`;
      row.appendChild(title);
      const note = document.createElement("span");
      note.textContent = `回执候选 ${Number(item.candidate_host_receipts || 0)} · ` +
        `所需核验 ${String(item.required_evidence || "unknown")} · ` +
        `${item.status === "verified" ? "宿主副作用已核实" : "未决"}` +
        (item.last_attempt_reason && item.status !== "verified" ?
          `（${reconciliationReasons[item.last_attempt_reason] || String(item.last_attempt_reason)}）` : "");
      row.appendChild(note);
      const expand = document.createElement("button");
      expand.type = "button";
      expand.className = "handoff-review-button";
      expand.textContent = "展开来源";
      expand.addEventListener("click", () => send({
        type: "handoff_review_expand", run_id: msg.run_id,
        conversation_id: conversationId,
        source_id: page.source_id || msg.source_id || "",
        reference_id: item.record_id, view_offset: 0,
      }));
      row.appendChild(expand);
      card.appendChild(row);
    });
    if (page.next_offset !== null && page.next_offset !== undefined) {
      const next = document.createElement("button");
      next.type = "button";
      next.className = "handoff-review-button";
      next.textContent = "下一页";
      next.addEventListener("click", () => send({
        type: "handoff_review_worklist", run_id: msg.run_id,
        conversation_id: conversationId, offset: page.next_offset,
        source_id: page.source_id || msg.source_id || "",
      }));
      card.appendChild(next);
    }
    messagesEl.appendChild(card);
    scrollBottom();
  }

  function addErrorMsg(text) {
    hideEmptyState();
    const div = document.createElement("div");
    div.className = "msg error";
    div.innerHTML = "<div class='msg-bubble'>" + esc(text) + "</div>";
    messagesEl.appendChild(div);
    scrollBottom();
  }

  function rememberBackendTurnScope(message, conversationId) {
    const id = String(conversationId || message?.conversation_id || activeConversationId() || "");
    if (!id) return "";
    // A run can emit more than one real turn.  Keep an explicit server turn
    // identity when one is present; otherwise allocate a fresh client-side
    // sequence for every turn_started.  run_id is only a dimension of the
    // error key, never the turn identity.
    const identity = message?.turn_id || message?.turn_key || message?.request_id;
    const scope = String(identity || ("turn-" + (++backendErrorScopeCounter)));
    backendTurnScopes.set(id, {sequence: scope, run_id: String(message?.run_id || message?.resume_run_id || "")});
    for (const key of backendErrorNotices.keys()) {
      if (key.startsWith(id + "::")) backendErrorNotices.delete(key);
    }
    return scope;
  }

  function backendErrorScope(message, conversationId) {
    const id = String(conversationId || message?.conversation_id || activeConversationId() || "");
    if (!id) return "";
    const explicit = message?.turn_id || message?.turn_key || message?.request_id;
    let turn = explicit ? String(explicit) : "";
    const current = backendTurnScopes.get(id);
    if (!turn && current) turn = String(current.sequence || "");
    if (!turn) {
      turn = "turn-" + (++backendErrorScopeCounter);
      backendTurnScopes.set(id, {sequence: turn, run_id: ""});
    }
    const run = String(message?.run_id || message?.resume_run_id || current?.run_id || "");
    return id + "::" + turn + "::" + run;
  }

  function backendErrorText(message) {
    return String(message?.text || message?.error || message?.message || "后台任务失败");
  }

  function displayBackendError(message, conversationId) {
    const id = String(conversationId || message?.conversation_id || activeConversationId() || "");
    if (!id || id !== activeConversationId()) return false;
    if (message?.error_displayed) return false;
    const key = backendErrorScope(message, conversationId);
    if (!key) return false;
    const text = backendErrorText(message);
    const prior = backendErrorNotices.get(key);
    if (prior?.displayed && prior.text === text) return false;
    addErrorMsg(text);
    backendErrorNotices.set(key, {text, displayed: true});
    return true;
  }

  function renderBlockedResumeError(message, conversationId) {
    const id = String(conversationId || message?.conversation_id || activeConversationId() || "");
    if (!id || id !== activeConversationId()) return;
    const runId = String(message?.run_id || message?.resume_advice?.run_id || "");
    const advice = message?.resume_advice;
    if (!id || !runId || !advice || typeof advice !== "object") {
      displayBackendError(message, id);
      return;
    }
    if ((advice.run_id && String(advice.run_id) !== runId)
        || (advice.conversation_id && String(advice.conversation_id) !== id)) return;
    const snapshot = message.snapshot;
    if (snapshot && String(snapshot.conversation_id || id) === id
        && String(snapshot.run_id || runId) === runId) {
      renderExpertSnapshot(snapshot);
    }
    const saved = saveScopedResumeAdvice(advice, id, runId);
    const state = ensureExpertTimelineV2(runId);
    state.resumeAdvice = saved;
    const flow = pipelineStates.get(id)?.expertFlows?.get(runId);
    state.retryOwner = flow || state;
    if (flow) {
      flow.resumeAdvice = saved;
      flow.retryOwner = flow;
    }
    const status = ["paused", "blocked", "needs_attention", "completed", "terminated"].includes(state.status)
      ? state.status : "blocked";
    renderExpertControlsV2(status, runId, saved?.reason || message.text || "", state.attempt);

    const key = backendErrorScope(message, id);
    if (key) backendErrorNotices.set(key, {
      text: backendErrorText(message), displayed: true, blocked: true,
    });
  }

  function addAssistantMarkdown(text, destination = messagesEl, forceScroll = true) {
    if (!String(text || "").trim()) return null;
    hideEmptyState();
    const div = document.createElement("div");
    div.className = "msg assistant";
    div.innerHTML = "<div class='msg-bubble'><div class='md'>" +
      renderMarkdown(text || "") + "</div></div>";
    destination.appendChild(div);
    enhanceMath(div.querySelector(".md"));
    scrollBottom(forceScroll);
    return div;
  }

  const expertRoleLabels = {
    manager: "管理",
    planner: "规划顾问",
    executor: "执行",
    auditor: "审计",
  };

  Object.assign(expertRoleLabels, {
    manager: "管理专家", planner: "规划专家",
    executor: "执行专家", auditor: "审计专家",
  });

  function formatExpertSeconds(value) {
    return Number(value || 0).toFixed(2) + " 秒";
  }

  function ensureExpertTimelineV2(runId = "") {
    const normalizedRunId = String(runId || currentExpertTimeline?.runId || "");
    const existing = expertTimelines.get(normalizedRunId);
    if (existing) {
      currentExpertTimeline = existing;
      return existing;
    }
    hideEmptyState();
    const msg = document.createElement("div");
    msg.className = "msg assistant expert-message";
    const timeline = document.createElement("div");
    timeline.className = "expert-timeline";
    timeline.innerHTML = "<div class='expert-timeline-title'><strong>编排执行记录</strong>" +
      "<span class='expert-run-status'>等待状态</span><span class='expert-strategy-source'>策略待派生</span></div>" +
      "<p class='expert-run-reason hidden'></p>";
    // Compatibility state for checkpoint/replay helpers, never a second chat view.
    // Node bubbles and the stage board are the visible execution surfaces.
    msg.appendChild(timeline);
    currentExpertTimeline = {
      msg, timeline, cards: new Map(), text: new Map(), tools: new Map(), children: new Map(), waits: new Map(),
      textRenderTimers: new Map(),
      runId: normalizedRunId, status: "pending", attempt: 0, detailsLoaded: new Set(),
      nodes: new Map(), stageCursors: new Map(), attemptGroups: new Map(),
    };
    const state = currentExpertTimeline;
    expertTimelines.set(normalizedRunId, state);
    scrollBottom();
    return currentExpertTimeline;
  }

  function setExpertTimelineFlow(state, flow) {
    state.flow = flow;
    state.loopContracts = flow?.meta?.loops || [];
    for (const node of flow?.nodes || []) state.nodes.set(String(node.node_id), node);
    state.timeline.querySelector(".expert-timeline-title strong").textContent =
      flow?.label ? flow.label + " · 执行记录" : "编排执行记录";
  }

  function expertStageIdentity(state, event) {
    const nodeId = String(event.node_id || "");
    const member = String(event.member_id || "");
    const stem = JSON.stringify([nodeId || ("role:" + (event.role || "stage")), member]);
    const prior = state.stageCursors.get(stem);
    const attempt = Number(event.attempt ?? prior?.attempt ?? state.attempt ?? 0);
    const nodeAttempt = Number(event.node_attempt ?? (prior?.attempt === attempt ? prior.nodeAttempt : 0));
    return {key: JSON.stringify([stem, attempt, nodeAttempt]), stem, nodeId, member, attempt, nodeAttempt};
  }

  function ensureExpertStageV2(state, event) {
    const identity = expertStageIdentity(state, event);
    const {key, stem, nodeId, member, attempt, nodeAttempt} = identity;
    const prior = state.stageCursors.get(stem);
    if (!prior || attempt > prior.attempt || (attempt === prior.attempt && nodeAttempt >= prior.nodeAttempt)) {
      state.stageCursors.set(stem, identity);
    }
    let card = state.cards.get(key);
    if (!card) {
      let group = state.attemptGroups.get(attempt);
      if (!group) {
        group = document.createElement("details"); group.className = "expert-attempt-group";
        group.innerHTML = "<summary></summary><div class='expert-attempt-stages'></div>";
        group.querySelector("summary").textContent = attempt ? "执行轮次 " + attempt : "历史记录（轮次未记录）";
        group.open = attempt === state.attempt;
        state.attemptGroups.set(attempt, group); state.timeline.appendChild(group);
      }
      card = document.createElement("section"); card.className = "expert-stage is-pending";
      card.dataset.stageKey = key; card.dataset.nodeId = nodeId; card.dataset.memberId = member;
      card.dataset.attempt = String(attempt); card.dataset.nodeAttempt = String(nodeAttempt);
      card.innerHTML = "<header><span class='expert-stage-dot'></span><strong></strong>" +
        "<span class='expert-stage-role'></span><span class='expert-stage-status'>等待事件</span></header>" +
        "<div class='expert-stage-meta'></div><div class='expert-stage-usage'></div><div class='expert-stage-stats'></div>" +
        "<details class='expert-stage-output'><summary>正文与调用详情</summary>" +
        "<div class='expert-stage-body md'></div><div class='expert-event-list'></div></details>";
      group.querySelector(".expert-attempt-stages").appendChild(card);
      state.cards.set(key, card); state.text.set(key, "");
      card.querySelector(".expert-stage-output").addEventListener("toggle", (e) => {
        if (e.currentTarget.open) { flushExpertTextV2(state, key); loadExpertRoleDetails(state, key); }
      });
    }
    const node = state.nodes.get(nodeId);
    const role = String(event.role || node?.role || card.dataset.role || "");
    card.dataset.role = role;
    card.querySelector("header strong").textContent = (event.label || node?.label || nodeId || expertRoleLabels[role] || "历史阶段")
      + (member ? " · 成员 " + member : "");
    card.querySelector(".expert-stage-role").textContent = expertRoleLabels[role] || role;
    return {key, card, identity};
  }

  function renderExpertStageV2(event) {
    if (["running", "completed", "terminated", "failed", "stalled", "budget_exhausted"].includes(String(event.status || ""))
        && typeof saveScopedResumeAdvice === "function") {
      saveScopedResumeAdvice(null, event.conversation_id || activeConversationId(), event.run_id);
    }
    const state = ensureExpertTimelineV2(event.run_id || "");
    const {key, card, identity} = ensureExpertStageV2(state, event);
    const liveRunning = !event.history && !event.snapshot_aggregate && event.status === 'running';
    // Capture only this event's full text before merging status/model fields;
    // a later status-only event must not restore stale streamed output.
    if (typeof event.text === "string") {
      state.text.set(key, event.text);
      if (card.querySelector(".expert-stage-output").open) flushExpertTextV2(state, key);
    }
    // Completion/status-only events must not discard the call's timing/model.
    event = {...(card._expertEvent || {}), ...event};
    card._expertEvent = event;
    const incomingAttempt = identity.attempt;
    if (liveRunning && incomingAttempt >= state.attempt) {
      state.handling = {owner: 'system', handling_state: 'working', reason: '正在执行当前步骤。'};
      renderExpertControlsV2("running", state.runId, "", incomingAttempt);
    }
    if (event.strategy) {
      state.timeline.querySelector(".expert-strategy-source").textContent =
        event.strategy === "fast"
          ? "高成本快速 · 来自多 Agent 已开启"
          : "稳定成本 · 来自多 Agent 未开启";
    }
    const historicalPending = incomingAttempt < state.attempt && event.status === "running";
    card.className = "expert-stage is-" + (historicalPending ? "paused" : (event.status || "running"));
    card.querySelector(".expert-stage-status").textContent = ({
      running: "运行中", escalated: "主模型协助中", waiting_human: "等待用户", completed: "已完成", paused: "已暂停",
      skipped: event.reason === "checkpoint_completed" ? "已完成 · 本轮复用" : (event.reason === "branch_not_taken" ? "未走此分支" : "已跳过"),
      failed: "执行失败", terminated: "已终止", review_pending: "执行完成 · 待验收",
      stalled: "重复调用已停止", budget_exhausted: "预算已耗尽", needs_attention: "验收需处理",
    })[event.status] || event.status || "等待";
    if (historicalPending) card.querySelector(".expert-stage-status").textContent = "历史调用 · 未记录结束状态";
    if (
      event.status && event.status !== "running"
      && card.querySelector(".expert-stage-output").open
    ) flushExpertTextV2(state, key);
    const parts = [incomingAttempt ? "轮次 " + incomingAttempt : "历史",
      identity.nodeAttempt ? "节点调用 " + identity.nodeAttempt : "",
      event.provider, event.model, event.effort || "auto"].filter(Boolean);
    if (event.input_tokens != null) {
      parts.push(fmtInt(event.input_tokens) + " in / " + fmtInt(event.output_tokens) + " out");
    }
    if (event.fallback_reason) parts.push(event.fallback_reason);
    card.querySelector(".expert-stage-meta").textContent = parts.join(" · ");
    if (event.started_at) {
      if (!state.waits.has(key)) state.waits.set(key, []);
      const clock = document.createElement("span");
      clock.className = "expert-clock";
      const end = Number(event.completed_at || 0) || (event.duration_ms != null
        ? Number(event.started_at) + Number(event.duration_ms) / 1000 : 0);
      const live = event.status === "running" && !historicalPending;
      if (live || end) clock.dataset.start = event.started_at;
      else clock.textContent = "记录不完整";
      if (end) {
        clock.dataset.end = String(end);
        clock.textContent = formatExpertSeconds(Math.max(0,
          end - Number(event.started_at) - Number(event.paused_seconds || 0)));
      }
      clock.dataset.paused = event.paused_seconds || 0;
      clock.dataset.stageKey = key; clock.dataset.runId = state.runId;
      card.querySelector(".expert-stage-stats").replaceChildren(
        document.createTextNode("总时长 "), clock,
        document.createTextNode(" · 等待 " + formatExpertSeconds(event.wait_seconds) +
          " · 实际工作 " + formatExpertSeconds(event.active_seconds ??
            Math.max(0, (event.duration_ms || 0) / 1000 - Number(event.wait_seconds || 0))))
      );
    }
  }

  function renderExpertFlowStateV2(event) {
    if (!event || !event.phase) return;
    const state = ensureExpertTimelineV2(event.run_id || "");
    if (Number(event.attempt || 0) && Number(event.attempt) < state.attempt) return;
    if (event.resume_requires_retry !== undefined) state.resumeRequiresRetry = !!event.resume_requires_retry;
    const phase = String(event.phase || "");
    if (phase === "loop" && event.loop_state) {
      const title = state.timeline.querySelector(".expert-timeline-title");
      let badge = state.timeline.querySelector(".expert-loop-live");
      if (!badge) { badge = document.createElement("span"); badge.className = "expert-loop-live expert-flow-badge"; title?.appendChild(badge); }
      const loop = event.loop_state;
      const queue = event.loop_contract?.mode === "foreach";
      const progress = queue ? "队列 " + (loop.completed_items?.length || 0) + "/" + event.loop_contract.items.length + " · 本项第 " + (loop.item_round || 0) + " 轮" : "第 " + loop.round + " 轮";
      const blocked = ({provider_interrupted: "模型流中断，可继续", provider_error: "模型服务错误", budget_exhausted: "严格限额已到", token_window_no_progress: "Token 窗口连续无进展，需重规划", token_accounting_overrun: "实际 Token 超额，需对账", convergence_stalled: "修订无改进", acceptance_blocked: "验收证据待处理", node_failure: "执行节点待处理", no_progress: "无新增证据，等待方向", awaiting_input: "等待你的回答", recovery_needed: "节点待接续", handoff_format: "交付形式待核对", capability_missing: "节点能力与交付要求不匹配", external_dependency: "外部依赖待恢复"})[loop.error_kind] || "等待处理";
      badge.textContent = "↶ " + event.loop_id + " · " + progress + " · " + Number(loop.tokens || 0).toLocaleString()
        + " Token · " + ({completed: "验收通过", blocked, continue: "准备改进"}[loop.status] || "执行中");
      badge.title = loop.reason || "协作进度与验收证据";
      return; // Update one live marker, not a new paragraph on every usage event.
    }
    const debateRounds = Number(event.debate_rounds || 0);
    const reworkRounds = Number(event.rework_rounds || 0);
    const markerKey = [phase, debateRounds, reworkRounds, event.node_id || "", event.audit_protocol?.round || 0, event.passed ? 1 : 0].join(":");
    if (!state.flowMarkers) state.flowMarkers = new Set();
    if (state.flowMarkers.has(markerKey)) return;
    state.flowMarkers.add(markerKey);

    const title = state.timeline.querySelector(".expert-timeline-title");
    let badge = state.timeline.querySelector(".expert-flow-badge");
    if (!badge) {
      badge = document.createElement("span");
      badge.className = "expert-flow-badge";
      title.appendChild(badge);
    }
    const marker = document.createElement("p");
    marker.className = "expert-flow-marker";
    if (phase.startsWith("audit_")) {
      badge.textContent = "固定验收 · 第 " + Number(event.audit_protocol?.round || 0) + " 轮";
      marker.textContent = "◆ " + (event.reason || "逐项验收") + (event.decision ? "（缺陷值 " + event.decision.before + " → " + event.decision.after + "）" : "");
    } else if (phase === "debate" && debateRounds > 0) {
      badge.textContent = "辩论第 " + debateRounds + " 轮";
      marker.textContent = "◆ 辩论第 " + debateRounds + " 轮：规划专家回应审计问题，审计专家复审";
    } else if (phase === "rework" && reworkRounds > 0) {
      badge.textContent = "定向返工第 " + reworkRounds + " 轮";
      marker.textContent = "◆ 定向返工第 " + reworkRounds + " 轮：执行专家修复审计定位的问题";
    } else if (phase === "ruling") {
      badge.textContent = "管理终裁";
    } else if (phase === "done") {
      const complete = event.status === "completed" || event.passed === true;
      badge.textContent = complete ? "流程已完成" : "流程未完成 · 当前节点需处理";
      marker.textContent = complete ? "◆ 流程验收通过" : "◆ 当前节点受阻，后续节点尚未完成";
    } else if (phase === "recovery_revision_pending") {
      badge.textContent = "计划修订待批准";
      marker.textContent = "◆ " + (event.reason || "需要你批准一项计划修订");
      state.pendingRevision = true;
      workbenchPrompt(event, {pending_recovery_revision: event.pending_recovery_revision || event.audit_protocol?.pending_recovery_revision, run_id: event.run_id});
    } else if (phase === "audit_rubric_activated") {
      badge.textContent = "验收已改版";
      marker.textContent = "◆ " + (event.reason || "核验方法已自动升版");
    }
    if (marker.textContent) state.timeline.appendChild(marker);
    if (event.ruling_text && event.ruling_done && !state.rulingShown) {
      state.rulingShown = true;
      const ruling = document.createElement("p");
      ruling.className = "expert-flow-marker expert-flow-ruling";
      ruling.textContent = "◆ 管理终裁：" +
        String(event.ruling_text).trim().split(/\r?\n/)[0];
      state.timeline.appendChild(ruling);
    }
  }

  function expertRunStatusLabel(state) {
    const handlingLabel = ({deciding:'准备中', working:'系统处理中', needs_user:'等待你决定', blocked:'系统受阻', resolved:'已恢复'})[state.handling?.handling_state];
    const liveHumanWait = [...(state.waits || new Map()).values()].some(intervals =>
      intervals.some(item => item.active && ['ask_user','human_oncall','permission','case_answer'].includes(item.kind)));
    const userCases = pendingRecoveryCases(state.runId).some(event =>
      event.case.handling?.owner !== 'system' && Object.keys(event.case.pending || {}).length);
    if (handlingLabel && !['completed','terminated','failed'].includes(state.status)
        && !(state.handling?.owner === 'system' && (liveHumanWait || userCases))) return handlingLabel;
    const status = state.status;
    const labels = {running: '执行中', paused: '已暂停', blocked: '已受阻 · 未在执行',
      needs_attention: '需处理 · 未在执行', completed: '已完成', terminated: '已终止'};
    const advice = state.resumeAdvice || null;
    const resumable = ["paused", "blocked", "needs_attention"].includes(status);
    if (state.kernelMigrationRequired) {
      const migrate = document.createElement("button");
      migrate.textContent = "归档旧运行并迁移";
      migrate.addEventListener("click", async () => {
        migrate.disabled = true;
        try {
          const response = await fetch("/api/expert-runs/" + encodeURIComponent(state.runId) + "/migrate-kernel", {
            method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
              conversation_id: activeConversationId(), expected_updated_at: state.sourceUpdatedAt
            })
          });
          const result = await response.json();
          if (!response.ok) throw new Error(result.error || "迁移失败");
          renderExpertSnapshot(result.snapshot);
          notify("完整历史已归档，新运行保留可复用成果", "success");
        } catch (error) { notify(error.message, "error"); }
        finally { migrate.disabled = false; }
      });
      actions.appendChild(migrate);
      controls.appendChild(actions);
      return controls;
    }
    if (resumable && advice?.action === "retry_node") {
      return "上次自动修复已停止 · 待重试「" +
        String(advice.node_label || advice.node_id || "当前节点") + "」";
    }
    if (resumable && advice?.action === "answer_case" && !pendingRecoveryCases(state.runId).length) {
      return "需回答恢复案件 · 未在执行";
    }
    if (resumable && advice?.action === "review_cases") return "需逐项查看恢复案件 · 未在执行";
    if (resumable && advice?.action === "continue_checkpoint") return "可从检查点接续 · 未在执行";
    const cases = pendingRecoveryCases(state.runId);
    if (cases.length && ['running', 'paused', 'blocked', 'needs_attention'].includes(status)) {
      const waitingNodes = new Set(cases.map(event => event.node_id));
      const otherRunning = status === 'running' && [...(state.cards || new Map()).values()].some(card =>
        !waitingNodes.has(card.dataset.nodeId) && card.classList.contains('is-running')
        && Number(card.dataset.attempt || 0) === Number(state.attempt || 0));
      const label = cases.every(event => event.case.pending.kind === 'elevation') ? '等待你的授权' : '等待你的选择';
      return label + (otherRunning ? ' · 其他节点执行中' : ' · 自动修复已停止');
    }
    if (status !== 'running') return labels[status] || status;
    const waiting = new Set(); let permission = false;
    for (const [key, intervals] of state.waits || []) {
      for (const interval of intervals) {
        if (!interval.active || !['ask_user', 'human_oncall', 'permission'].includes(interval.kind)) continue;
        waiting.add(key); permission ||= interval.kind === 'permission';
      }
    }
    if (!waiting.size) return labels.running;
    const otherRunning = [...(state.cards || new Map())].some(([key, card]) => !waiting.has(key)
      && card.classList.contains('is-running') && Number(card.dataset.attempt || 0) === Number(state.attempt || 0));
    return (permission ? '等待授权' : '等待你的回答') + (otherRunning ? ' · 其他节点执行中' : '');
  }

  function syncExpertHumanWaits(state, snapshot) {
    const nodes = snapshot.node_states || snapshot.stages || {};
    for (const intervals of state.waits.values()) for (const interval of intervals) {
      const node = nodes[interval.nodeId] || {};
      if (!node.pending_interaction && !node.pending_permission) interval.active = false;
    }
    for (const [nodeId, node] of Object.entries(nodes)) {
      if (!Array.isArray(node.wait_intervals)) continue;
      const base = {run_id: snapshot.run_id, conversation_id: snapshot.conversation_id,
        node_id: nodeId, role: node.role, attempt: node.attempt || snapshot.attempt,
        node_attempt: node.node_attempt || snapshot.node_attempts?.[nodeId] || 0};
      const key = expertStageIdentity(state, base).key;
      const pending = node.pending_interaction || {};
      const permission = node.pending_permission || {};
      const intervals = node.wait_intervals.map(item => ({...item, nodeId, toolId: item.tool_id,
        key: String(item.tool_id || item.kind) + ":" + String(item.start || ""),
        active: snapshot.status === 'running' && node.status === 'running' && !item.end
          && ((item.tool_id === pending.request_id && ['ask_user', 'human_oncall'].includes(item.kind))
            || (item.kind === 'permission' && item.tool_id === (permission.tool_id || permission.request_id)))}));
      state.waits.set(key, intervals);
      for (const item of intervals.filter(item => item.active)) renderExpertRoleWaitV2({...base,
        kind: item.kind, wait_id: item.toolId, started_at: item.start, active: true, history: true});
    }
  }

  function expertToolWaitState(event) {
    if (!String(event.type || '').startsWith('expert_tool') || !(event.id || event.tool_id)) return event;
    const state = expertTimelines.get(String(event.run_id || ''));
    if (!state) return event;
    const key = expertStageIdentity(state, event).key;
    const waits = (state.waits.get(key) || []).filter(item => item.toolId === (event.id || event.tool_id)
      && ['ask_user', 'human_oncall'].includes(item.kind));
    if (!waits.length) return event;
    if (['expert_tool_start', 'expert_tool_result'].includes(event.type)
        || ['completed', 'failed'].includes(event.status)) {
      return {...event, wait_seconds: unionExpertWaitSeconds(waits.filter(item => item.end || item.active),
        event.started_at || event.completed_at || Date.now() / 1000)};
    }
    const current = waits.find(item => item.active);
    if (!current || ['running', 'outcome_unknown', 'interrupted'].includes(event.status)) return event;
    return {...event, type: 'expert_tool_wait', status: 'waiting_user', wait_started_at: current.start,
      wait_completed_at: null, wait_seconds: null,
      wait_seconds_prior: unionExpertWaitSeconds(waits.filter(item => item !== current && item.end), current.start)};
  }

  function renderExpertRoleWaitV2(event) {
    const state = ensureExpertTimelineV2(event.run_id || "");
    const {key: stageKey} = ensureExpertStageV2(state, event);
    const intervals = state.waits.get(stageKey) || [];
    const key = String(event.wait_id || event.kind || intervals.length) + ":" + String(event.started_at || "");
    let interval = intervals.find((item) => item.key === key);
    if (!interval) {
      interval = { key, start: Number(event.started_at || 0), end: null };
      intervals.push(interval);
    }
    if (event.started_at) interval.start = Number(event.started_at);
    if (!event.active && event.completed_at) interval.end = Number(event.completed_at);
    interval.active = !!event.active;
    interval.kind = event.kind;
    interval.nodeId = event.node_id;
    interval.toolId = event.wait_id;
    state.waits.set(stageKey, intervals);
    if (event.active && ['ask_user', 'human_oncall', 'permission', 'case_answer'].includes(event.kind)) {
      if (state.handling?.owner === 'system') state.handling = null;
    }
    const card = state.cards.get(stageKey);
    if (card) {
      let label = card.querySelector(".expert-human-wait");
      if (event.active) {
        if (!label) { label = document.createElement("div"); label.className = "expert-human-wait"; card.append(label); }
        label.textContent = `等待人工响应 · ${Math.floor(Number(event.elapsed || 0) / 60)} 分钟`;
      } else if (label) label.remove();
    }
    const statusLabel = state.timeline.querySelector('.expert-run-status');
    if (statusLabel) statusLabel.textContent = expertRunStatusLabel(state);
    if (event.active && ['ask_user', 'human_oncall'].includes(event.kind)) {
      const update = {...event, type: 'expert_tool_wait', id: event.wait_id,
        name: event.kind === 'human_oncall' ? 'human_oncall' : 'AskUserQuestion', status: 'waiting_user',
        started_at: undefined, completed_at: undefined, elapsed: undefined};
      // Host-generated questions need no invented tool row. Existing tool
      // calls remain unstarted internally while the user considers a reply.
      if (state.tools.has(stageKey + '::' + event.wait_id)) renderExpertToolV2(update, 'wait');
      const chatId = orchChatStem(event) + '::' + Number(event.node_attempt || 0) + '::' + event.wait_id;
      if (toolRows.has(chatId)) mirrorExpertChatEvent(update, {force: true});
    }
  }

  function unionExpertWaitSeconds(intervals, now) {
    const ranges = (intervals || []).map((item) => [
      Number(item.start || 0), Number(item.end || now),
    ]).filter((item) => item[0] && item[1] >= item[0]).sort((a, b) => a[0] - b[0]);
    let total = 0, start = null, end = 0;
    for (const range of ranges) {
      if (start === null) { start = range[0]; end = range[1]; }
      else if (range[0] <= end) end = Math.max(end, range[1]);
      else { total += end - start; start = range[0]; end = range[1]; }
    }
    return total + (start === null ? 0 : end - start);
  }

  function flushExpertTextV2(state, role) {
    const pendingRender = state.textRenderTimers.get(role);
    if (pendingRender) clearTimeout(pendingRender);
    state.textRenderTimers.delete(role);
    const card = state.cards.get(role);
    if (!card) return;
    const stageBody = card.querySelector(".expert-stage-body");
    stageBody.innerHTML = renderMarkdown(state.text.get(role) || "");
    enhanceMath(stageBody);
  }

  function appendExpertDeltaV2(event) {
    const state = ensureExpertTimelineV2(event.run_id || "");
    const {key, card} = ensureExpertStageV2(state, event);
    const value = (event.snapshot_aggregate ? "" : (state.text.get(key) || "")) + (event.text || "");
    state.text.set(key, value);
    if (!card.querySelector(".expert-stage-output").open) return;
    if (event.snapshot_aggregate) {
      flushExpertTextV2(state, key);
    } else if (!state.textRenderTimers.has(key)) {
      state.textRenderTimers.set(key, setTimeout(
        () => flushExpertTextV2(state, key), 80
      ));
    }
  }

  function renderExpertToolV2(event, phase = "declared") {
    event = expertToolWaitState(event);
    if (event.status === 'waiting_user') phase = 'wait';
    const state = ensureExpertTimelineV2(event.run_id || "");
    const {key, card} = ensureExpertStageV2(state, event);
    const id = key + "::" + String(event.id || event.tool_id || "");
    let row = state.tools.get(id);
    if (!row) {
      row = document.createElement("details"); row.className = "expert-tool-note";
      row.innerHTML = "<summary><strong>工具 · " + esc(event.name || "unknown") +
        "</strong><span class='expert-tool-state'>排队</span><span class='expert-tool-times expert-tool-clock'></span></summary>" +
        "<pre class='expert-tool-args'></pre><pre class='expert-tool-output'></pre>";
      card.querySelector(".expert-event-list").appendChild(row); state.tools.set(id, row);
    }
    if (event.args) row.querySelector(".expert-tool-args").textContent = JSON.stringify(event.args, null, 2);
    if (event.declared_at) row.dataset.declared = event.declared_at;
    if (event.started_at) row.dataset.started = event.started_at;
    if (event.wait_started_at) row.dataset.waitStarted = event.wait_started_at;
    if (event.status === 'waiting_user') {
      delete row.dataset.waitCompleted; delete row.dataset.waitSeconds;
      row.dataset.waitPrior = event.wait_seconds_prior || 0;
    }
    if (event.wait_completed_at) row.dataset.waitCompleted = event.wait_completed_at;
    if (phase === "start" && row.dataset.waitStarted && !row.dataset.waitCompleted) {
      row.dataset.waitCompleted = String(event.started_at || Date.now() / 1000);
    }
    if (event.completed_at) row.dataset.completed = event.completed_at;
    else if (phase === "result") row.dataset.completed = String(Date.now() / 1000);
    if (event.wait_seconds != null) row.dataset.waitSeconds = event.wait_seconds;
    if (event.elapsed != null) row.dataset.executionSeconds = event.elapsed;
    if (event.dispatch_seconds != null) row.dataset.dispatchSeconds = event.dispatch_seconds;
    if (phase === "result" || event.output) {
      row.querySelector(".expert-tool-output").textContent = (event.output || "").slice(0, 4000);
    } else if (phase === "wait" && event.reason) {
      row.querySelector(".expert-tool-output").textContent = String(event.reason);
    }
    row.querySelector(".expert-tool-state").textContent = phase === "start" ? "执行中" :
      phase === "wait" ? (event.status === 'waiting_user' ? '等待你的回答' : '等待授权') :
      phase === "result" ? (event.is_error ? "失败" : "完成") : ({
        queued: "排队", running: "执行中", waiting_permission: "等待授权", waiting_user: "等待你的回答",
        outcome_unknown: "结果未知 · 恢复前须检查", interrupted: "已中断",
        interrupted_before_execution: "执行前已中断",
      })[event.status] || event.status || "排队";
    updateExpertToolClock(row, Date.now() / 1000);
  }

  function updateExpertToolClock(row, now) {
    const declared = Number(row.dataset.declared || 0);
    if (!declared) return;
    const started = Number(row.dataset.started || 0);
    const completed = Number(row.dataset.completed || 0);
    const waitStarted = Number(row.dataset.waitStarted || 0);
    const waitCompleted = Number(row.dataset.waitCompleted || 0);
    const wallEnd = completed || now;
    const wait = row.dataset.waitSeconds !== undefined
      ? Number(row.dataset.waitSeconds || 0)
      : Number(row.dataset.waitPrior || 0) + (waitStarted ? Math.max(0, (waitCompleted || now) - waitStarted) : 0);
    const queueEnd = started || wallEnd;
    const queue = Math.max(0, queueEnd - declared - wait);
    const execution = row.dataset.executionSeconds !== undefined
      ? Math.max(0, Number(row.dataset.executionSeconds))
      : (started ? Math.max(0, wallEnd - started) : 0);
    row.querySelector(".expert-tool-times").textContent =
      "排队 " + formatExpertSeconds(queue) + " · 墙钟 " +
      formatExpertSeconds(Math.max(0, wallEnd - declared)) + " · 等待 " +
      formatExpertSeconds(wait) + " · 执行 " + formatExpertSeconds(execution) +
      (row.dataset.dispatchSeconds !== undefined
        ? " · 调度 " + formatExpertSeconds(Number(row.dataset.dispatchSeconds)) : "");
  }

  function appendExpertThinkingV2(event) {
    const {card} = ensureExpertStageV2(ensureExpertTimelineV2(event.run_id || ""), event);
    let block = card.querySelector(".expert-thinking");
    if (!block) {
      block = document.createElement("details"); block.className = "expert-thinking";
      block.innerHTML = "<summary>可展示 reasoning</summary><pre></pre>";
      card.querySelector(".expert-event-list").appendChild(block);
    }
    const body = block.querySelector("pre");
    body.textContent = (event.snapshot_aggregate ? "" : body.textContent) + (event.text || "");
  }

  function renderExpertUsageV2(event) {
    const {card} = ensureExpertStageV2(ensureExpertTimelineV2(event.run_id || ""), event);
    const cache = event.cache_usage_available
      ? fmtInt(event.cache_read_tokens || 0) + " 读 / " + fmtInt(event.cache_creation_tokens || 0) + " 写"
      : "未上报";
    const context = event.request_input_tokens && event.context_window
      ? fmtInt(event.request_input_tokens) + " / " + fmtInt(event.context_window) +
        "（剩余 " + fmtInt(event.context_remaining || 0) + "）" : "未上报";
    card.querySelector(".expert-stage-usage").textContent =
      "Token " + fmtInt(event.input_tokens || 0) + " in / " + fmtInt(event.output_tokens || 0) +
      " out · 缓存 " + cache + " · 上下文 " + context;
  }

  function renderExpertChildV2(event) {
    const state = ensureExpertTimelineV2(event.run_id || "");
    const {key, card} = ensureExpertStageV2(state, {...event, role: event.role || "executor"});
    const childId = key + "::" + String(event.agent_id || event.name || "");
    let child = state.children.get(childId);
    if (!child) {
      child = document.createElement("details"); child.className = "expert-child-note";
      card.querySelector(".expert-event-list").appendChild(child);
      state.children.set(childId, child);
    }
    const usage = event.usage || {};
    child.innerHTML = "<summary><strong>直属成员 · " + esc(event.name || event.agent_id) +
      "</strong><span>" + esc(event.status || "unknown") + "</span>" +
      (event.started_at ? "<span class='expert-clock' data-start='" + esc(event.started_at) + "'" +
        (event.completed_at ? " data-end='" + esc(event.completed_at) + "'" : "") + ">0.00 秒</span>" : "") +
      "</summary><p>模型用量：" + fmtInt(usage.input_tokens || 0) + " in / " +
      fmtInt(usage.output_tokens || 0) + " out · 缓存 " +
      (usage.cache_usage_available ? fmtInt(usage.cache_read_tokens || 0) + " 读 / " +
        fmtInt(usage.cache_creation_tokens || 0) + " 写" : "未上报") + "</p><code>" +
      esc(event.worktree || "") + "</code>";
  }

  function renderExpertChildEventV2(event) {
    const state = ensureExpertTimelineV2(event.run_id || "");
    const {key, card} = ensureExpertStageV2(state, {...event, role: event.role || "executor"});
    const childId = key + "::" + String(event.agent_id || "");
    let child = state.children.get(childId);
    if (!child) {
      child = document.createElement("details");
      child.className = "expert-child-note";
      child.innerHTML = "<summary><strong>直属成员 · " + esc(childId) +
        "</strong><span>运行中</span></summary><p></p><pre></pre>";
      card.querySelector(".expert-event-list").appendChild(child);
      state.children.set(childId, child);
    }
    const type = event.child_event_type || "event";
    if (type === "usage") {
      const usage = event.usage || {};
      child.querySelector("p").textContent =
        "Token " + fmtInt(usage.input_tokens || 0) + " in / " +
        fmtInt(usage.output_tokens || 0) + " out · 缓存 " +
        (usage.cache_usage_available
          ? fmtInt(usage.cache_read_tokens || 0) + " 读 / " +
            fmtInt(usage.cache_creation_tokens || 0) + " 写 · 命中 " +
            ((Number(usage.cache_hit_rate || 0) * 100).toFixed(1)) + "%"
          : "未上报");
      return;
    }
    const pre = child.querySelector("pre");
    if (pre) {
      const detail = type === "tool_use"
        ? "工具 " + (event.toolName || "unknown") + " " + JSON.stringify(event.args || {})
        : type === "tool_result"
          ? "结果 " + (event.toolName || "unknown") + " " + String(event.output || "").slice(0, 4000)
          : String(event.text || type);
      pre.textContent = (pre.textContent + "\n" + detail).trim().slice(-4000);
    }
  }

  function compactExpertStatusReason(value, limit = 220) {
    const text = String(value || "").replace(/\s+/g, " ").trim();
    if (text.length <= limit) return text;
    return text.slice(0, Math.max(1, limit - 1)).trimEnd() + "…";
  }

  function expertSnapshotStatusReason(snapshot, limit = 220) {
    if (snapshot?.status !== "blocked") {
      return compactExpertStatusReason(snapshot?.status_reason || "", limit);
    }
    const states = snapshot?.node_states || snapshot?.stages || {};
    const cursor = String(snapshot?.stage_cursor || "");
    const ordered = [];
    if (cursor && states[cursor]) ordered.push(states[cursor]);
    for (const [nodeId, state] of Object.entries(states)) {
      if (nodeId === cursor || !["blocked", "needs_attention", "failed", "budget_exhausted"].includes(state?.status)) continue;
      ordered.push(state);
    }
    for (const state of ordered) {
      const handoff = state?.handoff || {};
      if (handoff.question) return compactExpertStatusReason("等待用户输入：" + handoff.question, limit);
      const receipt = handoff.escalation_receipt || {};
      if (receipt.status === "needs_attention" && (receipt.text || receipt.error)) {
        return compactExpertStatusReason("执行器转派失败：" + (receipt.text || receipt.error), limit);
      }
      if (Array.isArray(handoff.blockers) && handoff.blockers.length) {
        return compactExpertStatusReason(handoff.blockers[0], limit);
      }
      const detail = state?.request_error || state?.status_reason || state?.reason;
      if (detail) return compactExpertStatusReason(detail, limit);
    }
    return compactExpertStatusReason(snapshot?.status_reason || "", limit);
  }

  function renderExpertControlsV2(status, runId, reason = "", attempt = 0) {
    const state = ensureExpertTimelineV2(runId || "");
    if (attempt && attempt < state.attempt) return;
    const liveOrTerminal = ["running", "completed", "terminated", "failed", "stalled", "budget_exhausted"].includes(status);
    if (liveOrTerminal) {
      if (typeof saveScopedResumeAdvice === "function") {
        saveScopedResumeAdvice(null, activeConversationId(), runId);
      }
      state.resumeRequiresRetry = false;
      state.resumeAdvice = null;
      if (state.retryOwner && state.retryOwner !== state) state.retryOwner.resumeAdvice = null;
    }
    if (attempt > state.attempt) {
      state.attempt = attempt; state.statusReason = ""; state.statusReasonFull = ""; state.statusReasonExpanded = false;
      state.flowMarkers?.clear(); state.rulingShown = false;
      state.timeline.querySelectorAll(".expert-flow-badge, .expert-flow-marker").forEach(el => el.remove());
      for (const [round, group] of state.attemptGroups) group.open = round === attempt;
      for (const card of state.cards.values()) {
        if (Number(card.dataset.attempt) >= attempt || !card.classList.contains("is-running")) continue;
        card.className = "expert-stage is-paused";
        card.querySelector(".expert-stage-status").textContent = "历史调用 · 未记录结束状态";
        card.querySelectorAll(".expert-clock:not([data-end])").forEach(clock => {delete clock.dataset.start; clock.textContent = "记录不完整";});
      }
    }
    const advice = state.resumeAdvice || null;
    const adviceReason = advice?.action === "retry_node"
      ? "上次自动修复已停止；「" + String(advice.node_label || advice.node_id || "当前节点") +
        "」尚未完成。"
      : String(advice?.reason || "");
    state.statusReasonFull = String(adviceReason || reason || (state.status === status
      ? state.statusReasonFull || state.statusReason || "" : ""));
    if (state.status !== status) state.statusReasonExpanded = false;
    state.statusReason = compactExpertStatusReason(state.statusReasonFull);
    state.status = status;
    state.msg.dataset.status = status;
    const label = expertRunStatusLabel(state);
    state.timeline.querySelector(".expert-run-status").textContent = label;
    const reasonEl = state.timeline.querySelector(".expert-run-reason");
    reasonEl.textContent = state.statusReason;
    reasonEl.classList.toggle("hidden", !state.statusReason);
    if (status !== "running") {
      for (const card of state.cards.values()) {
        if (!card.classList.contains("is-running") && !(status === "terminated"
          && card.classList.contains("is-paused") && Number(card.dataset.attempt) === state.attempt)) continue;
        card.className = "expert-stage is-" + (status === "completed" ? "needs_attention" : status);
        card.querySelector(".expert-stage-status").textContent = status === "terminated" ? "已终止" : "已停止 · 以节点检查点为准";
        card.querySelectorAll(".expert-clock:not([data-end])").forEach(clock => {clock.dataset.end = String(Date.now() / 1000);});
      }
    }

  }

  function renderExpertSnapshot(snapshot) {
    if (!snapshot) return;
    const conversationId = String(snapshot.conversation_id || activeConversationId() || "");
    if (conversationId && conversationId !== activeConversationId()) return;
    const state = ensureExpertTimelineV2(snapshot.run_id);
    state.kernelMigrationRequired = !!snapshot.migration_required;
    state.sourceUpdatedAt = snapshot.updated_at;
    if (Number(snapshot.attempt || 0) < state.attempt) return;
    state.attempt = Number(snapshot.attempt || 0);
    state.resumeRequiresRetry = !!snapshot.resume_requires_retry;
    state.loopControlMode = snapshot.loop_control_policy?.mode || "";
    state.handling = snapshot.handling || null;
    setExpertTimelineFlow(state, snapshot.locked_flow);
    expertSnapshotSeq.set(
      activeConversationId() + ":" + snapshot.run_id,
      Number(snapshot.last_seq || 0)
    );
    const currentNodes = snapshot.node_states || snapshot.stages || {};
    for (const [key, previous] of [...recoveryCases]) {
      if (previous.run_id !== snapshot.run_id
          || (previous.conversation_id && previous.conversation_id !== conversationId)) continue;
      const node = currentNodes[previous.node_id] || {};
      const current = node.recovery_case;
      if (!current || current.case_id !== previous.case.case_id
          || ['completed', 'skipped', 'branch_not_taken', 'terminated'].includes(node.status)
          || ['resolved', 'superseded'].includes(current.status) || current.closed_by_user
          || (node.epoch != null && String(current.epoch) !== String(node.epoch))
          || (node.input_revision != null && String(current.input_revision || '') !== String(node.input_revision || ''))) {
        recoveryCases.delete(key);
        applyRecoveryCase({...previous, case: current?.case_id === previous.case.case_id ? current : previous.case,
          history: true, archived: true, actionable: false});
      }
    }
    hydrateExpertTrajectory(snapshot);
    for (const rawEvent of snapshot.events || []) {
      const event = {...rawEvent, history: true};
      if (expertTrajectoryTypes.has(event.type)) {
        applyExpertTrajectoryEvent({
          conversation_id: snapshot.conversation_id || activeConversationId(),
          run_id: snapshot.run_id,
          ...event,
        }, false);
      }
      if (["expert_stage", "expert_node_started", "expert_node_completed"].includes(event.type)) renderExpertStageV2(event);
      else if (event.type === 'recovery_case' && !(snapshot.node_states || snapshot.stages || {})[event.node_id]?.recovery_case)
        applyRecoveryCase({...event, history: true, archived: true, actionable: false,
          run_id: snapshot.run_id, conversation_id: conversationId});
      else if (event.type === "expert_delta") appendExpertDeltaV2(event);
      else if (event.type === "expert_thinking") appendExpertThinkingV2(event);
      else if (event.type === "expert_tool") renderExpertToolV2(event);
      else if (event.type === "expert_tool_start") renderExpertToolV2(event, "start");
      else if (event.type === "expert_tool_wait") renderExpertToolV2(event, "wait");
      else if (event.type === "expert_tool_result") renderExpertToolV2(event, "result");
      else if (event.type === "expert_tool_state") renderExpertToolV2(event, "state");
      else if (event.type === "expert_role_wait") renderExpertRoleWaitV2(event);
      else if (event.type === "expert_usage") renderExpertUsageV2(event);
      else if (event.type === "expert_child") renderExpertChildV2(event);
      else if (event.type === "expert_child_event") renderExpertChildEventV2(event);
      if (
        String(event.type || "").startsWith("expert_")
        && event.type !== "expert_flow_defined"
      ) {
        mirrorExpertChatEvent({
          conversation_id: snapshot.conversation_id || activeConversationId(),
          run_id: snapshot.run_id,
          ...event,
        }, { force: true });
      }
    }
    // Also mirror completed node texts from node_states when event stream is thin.
    for (const [nodeId, nodeState] of Object.entries(snapshot.node_states || snapshot.stages || {})) {
      if (nodeState.recovery_case) applyRecoveryCase({type: 'recovery_case', history: true, run_id: snapshot.run_id,
        conversation_id: conversationId, node_id: nodeId, case: nodeState.recovery_case,
        archived: ['completed', 'skipped', 'branch_not_taken', 'terminated'].includes(nodeState.status)
          || ['resolved', 'superseded'].includes(nodeState.recovery_case.status)
          || !!nodeState.recovery_case.closed_by_user
          || (nodeState.epoch != null && String(nodeState.recovery_case.epoch) !== String(nodeState.epoch))
          || (nodeState.input_revision != null && String(nodeState.recovery_case.input_revision || '') !== String(nodeState.input_revision || '')),
        node_label: (snapshot.locked_flow?.nodes || []).find(node => node.node_id === nodeId)?.label || nodeState.label || nodeId,
        phase: nodeState.recovery_phase || ({awaiting_user: '待你决定', reported: '已汇报', resolved: '已解决',
          open: '统计', executing: '执行', superseded: '旧代际已保留'}[nodeState.recovery_case.status] || '')});
      const text = String(nodeState.text || nodeState.partial_text || "").trim();
      if (!text) continue;
      const flowNode = (snapshot.locked_flow?.nodes || []).find((item) => item.node_id === nodeId);
      mirrorExpertChatEvent({
        conversation_id: snapshot.conversation_id || activeConversationId(),
        run_id: snapshot.run_id,
        type: "expert_node_completed",
        node_id: nodeId,
        role: nodeState.role || flowNode?.role || "",
        attempt: Number(nodeState.attempt || 0), node_attempt: Number(nodeState.node_attempt || 0),
        label: nodeState.label || flowNode?.label || nodeId,
        model: nodeState.model || "",
        status: nodeState.status || "completed",
        history: true, snapshot_aggregate: true,
        started_at: nodeState.started_at,
        stream_activity: nodeState.stream_activity,
        text,
      }, { force: true });
    }
    // History remains visible after a restore, but it must never overwrite
    // the authoritative checkpoint progress with later historical events.
    hydrateExpertTrajectory(snapshot);
    // The advice is authoritative for this snapshot only.  Save it after
    // replaying recovery cases because replay intentionally invalidates live
    // advice when a newer case event arrives.
    const snapshotAdvice = Object.prototype.hasOwnProperty.call(snapshot, "resume_advice")
      ? snapshot.resume_advice
      : legacyResumeAdviceFromSnapshot(snapshot, conversationId);
    const advice = saveScopedResumeAdvice(snapshotAdvice, conversationId, snapshot.run_id);
    state.resumeAdvice = advice;
    const flowState = pipelineStates.get(conversationId)?.expertFlows?.get(String(snapshot.run_id || ""));
    if (flowState) {
      flowState.resumeAdvice = advice;
      flowState.retryOwner = flowState;
    }
    state.retryOwner = flowState || state;

    renderPipeline();
    if (snapshot.flow_state) {
      renderExpertFlowStateV2(Object.assign({ run_id: snapshot.run_id, attempt: snapshot.attempt }, snapshot.flow_state));
    }
    if (snapshot.pending_recovery_revision) {
      state.pendingRevision = true;
      workbenchPrompt({run_id: snapshot.run_id, conversation_id: snapshot.conversation_id}, snapshot);
    } else {
      state.pendingRevision = false;
    }
    state.savedPlanRevisionPending = !!snapshot.saved_plan_revision_pending;
    syncExpertHumanWaits(state, snapshot);
    mirrorExpertChatEvent({type: "expert_run_state", run_id: snapshot.run_id,
      conversation_id: conversationId, attempt: snapshot.attempt, status: snapshot.status, history: true});
    renderExpertControlsV2(snapshot.status, snapshot.run_id, expertSnapshotStatusReason(snapshot, Infinity), snapshot.attempt);
    state.timeline.querySelector(".expert-strategy-source").textContent = snapshot.strategy_source || "";
    const total = ((snapshot.usage || {}).total || {});
    if (total.source_count) {
      const summary = state.timeline.querySelector(".expert-run-usage") || document.createElement("span");
      summary.className = "expert-run-usage";
      summary.textContent = "总计 " + fmtInt(total.input_tokens || 0) + " in / " +
        fmtInt(total.output_tokens || 0) + " out · 缓存 " +
        (total.cache_usage_available
          ? fmtInt(total.cache_read_tokens || 0) + " 读 / " +
            fmtInt(total.cache_creation_tokens || 0) + " 写 · 命中 " +
            ((Number(total.cache_hit_rate || 0) * 100).toFixed(1)) + "%"
          : (total.cache_usage_partial ? "部分 Provider 未上报" : "未上报"));
      state.timeline.querySelector(".expert-timeline-title").appendChild(summary);
    }
    renderExpertControlsV2(snapshot.status, snapshot.run_id, expertSnapshotStatusReason(snapshot, Infinity), snapshot.attempt);
  }

  async function loadExpertRoleDetails(state, key, after = -1) {
    // Legacy function name; key is a node/member/attempt identity, never a role.
    if (!state || !state.runId || (after < 0 && state.detailsLoaded.has(key))) return;
    state.detailsLoaded.add(key);
    const card = state.cards.get(key);
    if (!card) {state.detailsLoaded.delete(key); return;}
    const role = card.dataset.role;
    const nodeId = card.dataset.nodeId;
    const list = card && card.querySelector(".expert-event-list");
    list?.querySelector(".expert-history-more")?.remove();
    const loading = document.createElement("p");
    loading.className = "expert-detail-loading";
    loading.textContent = "正在加载详细事件…";
    if (list) list.appendChild(loading);
    try {
      const response = await fetch(
        "/api/expert-runs/" + encodeURIComponent(state.runId) +
        "/history?limit=50&after=" + after + "&node_id=" + encodeURIComponent(nodeId) +
        "&attempt=" + card.dataset.attempt + "&node_attempt=" + card.dataset.nodeAttempt +
        "&member_id=" + encodeURIComponent(card.dataset.memberId)
      );
      const snapshot = await response.json();
      if (!response.ok || snapshot.error) throw new Error(snapshot.error || "加载失败");
      if (expertTimelines.get(state.runId) !== state || state.cards.get(key) !== card) return;
      for (const event of snapshot.items || []) {
        if (event.role && event.role !== role) continue;
        if (nodeId && event.node_id !== nodeId) continue;
        if (Number(event.attempt || 0) !== Number(card.dataset.attempt)) continue;
        if (Number(event.node_attempt || 0) !== Number(card.dataset.nodeAttempt)) continue;
        if (String(event.member_id || "") !== card.dataset.memberId) continue;
        if (["expert_tool", "expert_tool_start", "expert_tool_wait",
          "expert_tool_result", "expert_tool_state"].includes(event.type)) {
          renderExpertToolV2(event, event.type === "expert_tool_start" ? "start" :
            event.type === "expert_tool_wait" ? "wait" :
              event.type === "expert_tool_result" ? "result" : "state");
          mirrorExpertChatEvent({...event, conversation_id: snapshot.conversation_id}, {force: true});
        } else if (event.type === "expert_child") renderExpertChildV2(event);
      }
      if (snapshot.next_cursor !== null && snapshot.next_cursor !== undefined && list) {
        const more = document.createElement("button");
        more.className = "expert-history-more";
        more.textContent = "继续加载详细事件";
        more.addEventListener("click", () => loadExpertRoleDetails(state, key, snapshot.next_cursor), {once: true});
        list.appendChild(more);
      }
    } catch (error) {
      state.detailsLoaded.delete(key);
      if (list) {
        const failed = document.createElement("p");
        failed.className = "expert-detail-error";
        failed.textContent = "详细事件加载失败，折叠后可重试：" + (error.message || String(error));
        list.appendChild(failed);
      }
    } finally {
      loading.remove();
    }
  }

  let expertClockTimer = null;

  function tickExpertClocks() {
    const now = Date.now() / 1000;
    for (const entry of orchChatBubbles.values()) {
      if (entry.msg.isConnected && entry.activity.active) renderOrchChatActivity(entry, now);
    }
    document.querySelectorAll(".expert-clock[data-start]").forEach((node) => {
      const end = Number(node.dataset.end || 0) || now;
      const wall = Math.max(
        0, end - Number(node.dataset.start || end) - Number(node.dataset.paused || 0)
      );
      node.textContent = formatExpertSeconds(wall);
      const clockState = expertTimelines.get(node.dataset.runId);
      if (node.dataset.stageKey && clockState) {
        const card = clockState.cards.get(node.dataset.stageKey);
        const wait = unionExpertWaitSeconds(
          clockState.waits.get(node.dataset.stageKey) || [], now
        );
        const stats = card && card.querySelector(".expert-stage-stats");
        if (stats && !node.dataset.end) {
          stats.lastChild.textContent = " · 等待 " + formatExpertSeconds(wait) +
            " · 实际工作 " + formatExpertSeconds(Math.max(0, wall - wait));
        }
      }
    });
    document.querySelectorAll(".expert-tool-note").forEach((row) => {
      updateExpertToolClock(row, now);
    });
  }

  function syncExpertClockTimer() {
    const hasRunningNode = !!document.querySelector(
      ".expert-clock[data-start]:not([data-end]), .expert-tool-note:not([data-completed]), .orch-stream-activity[data-streaming='true']"
    );
    if (document.hidden || !hasRunningNode) {
      if (expertClockTimer) clearInterval(expertClockTimer);
      expertClockTimer = null;
      return;
    }
    if (!expertClockTimer) {
      tickExpertClocks();
      expertClockTimer = setInterval(tickExpertClocks, 1000);
    }
  }

  new MutationObserver(syncExpertClockTimer).observe(messagesEl, {
    childList: true, subtree: true, attributes: true,
    attributeFilter: ["data-start", "data-end", "data-completed", "data-streaming"]
  });
  document.addEventListener("visibilitychange", syncExpertClockTimer);

  function formatTurnSeconds(ms) {
    const total = Math.max(0, Math.round(Number(ms || 0) / 1000));
    if (total < 60) return total + " 秒";
    const minutes = Math.floor(total / 60);
    const seconds = total % 60;
    return minutes + " 分 " + seconds + " 秒";
  }

  function shortToolPath(value) {
    const text = String(value || "").replace(/\\/g, "/");
    if (!text) return "";
    const parts = text.split("/").filter(Boolean);
    if (parts.length <= 2) return text;
    return parts.slice(-2).join("/");
  }

  function toolArgObject(args) {
    if (args !== null && typeof args === "object") return args;
    if (typeof args === "string") {
      try {
        const parsed = JSON.parse(args);
        return parsed && typeof parsed === "object" ? parsed : {};
      } catch {
        return {};
      }
    }
    return {};
  }

  function toolValueText(value) {
    if (value === null || value === undefined) return "";
    if (typeof value === "string") return value;
    if (typeof value === "object") {
      try {
        const serialized = JSON.stringify(value, null, 2);
        return serialized === undefined ? String(value) : serialized;
      } catch {
        return String(value);
      }
    }
    return String(value);
  }

  function toolLineSummary(name, args) {
    const a = toolArgObject(args);
    if (String(name || "") === "LoadSkill" && typeof a.name === "string" && a.name.trim()) {
      return "LoadSkill " + a.name.trim();
    }
    const path = a.path || a.file_path || a.file || a.target || a.filename || "";
    const command = a.command || a.cmd || a.script || "";
    const pattern = a.pattern || a.query || a.grep || "";
    if (path) return String(name || "tool") + " " + shortToolPath(path);
    if (command) {
      const cmd = String(command).replace(/\s+/g, " ").trim();
      return String(name || "tool") + " " + (cmd.length > 64 ? cmd.slice(0, 64) + "…" : cmd);
    }
    if (pattern) return String(name || "tool") + " " + String(pattern).slice(0, 48);
    return String(name || "tool");
  }

  function stopTurnClock(finalElapsedSec) {
    if (turnClockTimer) {
      clearInterval(turnClockTimer);
      turnClockTimer = null;
    }
    if (!currentMsg) return;
    const label = currentMsg.querySelector(".turn-status-label");
    const clock = currentMsg.querySelector(".turn-status-clock");
    if (label) label.textContent = "已完成";
    if (clock) {
      const ms = finalElapsedSec != null
        ? Number(finalElapsedSec) * 1000
        : (turnStartedAt ? Date.now() - turnStartedAt : 0);
      clock.textContent = formatTurnSeconds(ms);
    }
  }

  function startTurnClock() {
    if (turnClockTimer) clearInterval(turnClockTimer);
    turnClockTimer = setInterval(() => {
      if (!currentMsg || !turnStartedAt) return;
      const clock = currentMsg.querySelector(".turn-status-clock");
      if (clock) clock.textContent = formatTurnSeconds(Date.now() - turnStartedAt);
    }, 1000);
  }

  function beginAssistant() {
    hideEmptyState();
    currentMsg = document.createElement("div");
    currentMsg.className = "msg assistant";
    const bubble = document.createElement("div");
    bubble.className = "msg-bubble";
    const status = document.createElement("div");
    status.className = "turn-status";
    status.innerHTML =
      "<span class='turn-status-label'>工作中</span>" +
      "<span class='turn-status-clock'>0 秒</span>";
    bubble.appendChild(status);
    currentMsg.appendChild(bubble);
    messagesEl.appendChild(currentMsg);
    currentText = "";
    currentTextSegment = "";
    currentMarkdownSegment = null;
    currentToolGroup = null;
    currentThinkingEl = null;
    currentThinkingStarted = 0;
    currentThinkingText = "";
    turnStartedAt = Date.now();
    startTurnClock();
    toolRows.clear();
    pendingTurnToolIds.clear();
  }

  function clearLiveMarkdownState(removeEmpty = false) {
    if (!currentMsg) return;
    for (const el of currentMsg.querySelectorAll(".md")) {
      el.classList.remove("cursor-blink");
      if (removeEmpty && !el.textContent.trim()) el.remove();
    }
  }

  function closeMarkdownSegment() {
    if (currentMarkdownSegment) {
      currentMarkdownSegment.classList.remove("cursor-blink");
      if (!currentTextSegment.trim() && !currentMarkdownSegment.textContent.trim()) {
        currentMarkdownSegment.remove();
      } else {
        enhanceMath(currentMarkdownSegment);
      }
    }
    currentMarkdownSegment = null;
    currentTextSegment = "";
  }

  function ensureMarkdownSegment() {
    if (!currentMsg) beginAssistant();
    finalizeThinking(true);
    if (currentMarkdownSegment) return currentMarkdownSegment;
    const bubble = currentMsg.querySelector(".msg-bubble");
    const md = document.createElement("div");
    md.className = "md cursor-blink";
    bubble.appendChild(md);
    currentMarkdownSegment = md;
    currentTextSegment = "";
    return md;
  }

  function appendDelta(text) {
    if (!currentMsg) beginAssistant();
    const chunk = text == null ? "" : String(text);
    if (!chunk) {
      scrollBottom();
      return;
    }
    // 文本开始输出后，后续工具开启新分组；新文本段始终追加在已有工具组之后。
    if (!currentMarkdownSegment) currentToolGroup = null;
    const el = ensureMarkdownSegment();
    currentText += chunk;
    currentTextSegment += chunk;
    el.classList.remove("hidden");
    el.innerHTML = renderMarkdown(currentTextSegment);
    el.dataset.draftText = currentTextSegment;
    scrollBottom();
  }

  function buildThinkingBlock(text, options = {}) {
    const done = !!options.done;
    const seconds = options.seconds;
    const det = document.createElement("details");
    det.className = "turn-thinking" + (done ? " is-done" : " is-running");
    const preview = String(text || "").replace(/\s+/g, " ").trim().slice(0, 96);
    const title = done
      ? (seconds != null ? "思考 · 持续了 " + seconds + " 秒" : "思考 · 已结束")
      : ("正在思考" + (preview ? " · " + preview : ""));
    det.innerHTML =
      "<summary><span class='th-mark'>思考</span><span class='th-title'></span></summary>" +
      "<pre class='th-body'></pre>";
    det.querySelector(".th-title").textContent = title;
    det.querySelector(".th-body").textContent = text || "";
    return det;
  }

  function finalizeThinking(done) {
    if (!currentThinkingEl) return;
    const started = currentThinkingStarted || Date.now();
    const seconds = Math.max(1, Math.round((Date.now() - started) / 1000));
    const title = currentThinkingEl.querySelector(".th-title");
    if (done) {
      currentThinkingEl.classList.remove("is-running");
      currentThinkingEl.classList.add("is-done");
      if (title) title.textContent = "思考 · 持续了 " + seconds + " 秒";
      currentThinkingEl = null;
      currentThinkingStarted = 0;
      currentThinkingText = "";
    } else if (title) {
      const preview = currentThinkingText.replace(/\s+/g, " ").trim().slice(0, 96);
      title.textContent = "正在思考" + (preview ? " · " + preview : "");
    }
  }

  function appendThinking(text) {
    if (!currentMsg) beginAssistant();
    const chunk = text == null ? "" : String(text);
    if (!chunk) {
      scrollBottom();
      return;
    }
    closeMarkdownSegment();
    currentToolGroup = null;
    if (!currentThinkingEl) {
      currentThinkingEl = buildThinkingBlock("", { done: false });
      currentMsg.querySelector(".msg-bubble").appendChild(currentThinkingEl);
      currentThinkingStarted = Date.now();
      currentThinkingText = "";
    }
    currentThinkingText += chunk;
    currentThinkingEl.querySelector(".th-body").textContent = currentThinkingText;
    finalizeThinking(false);
    scrollBottom();
  }

  // ---------- 工具聚合折叠 ----------

  function buildToolGroup(tools) {
    const det = document.createElement("details");
    det.className = "tool-group";
    det.innerHTML =
      "<summary><span class='tg-icon'>工</span><span class='tg-title'>工具调用</span><span class='tg-status'></span></summary>" +
      "<div class='tg-body'></div>";
    if (tools && tools.length) {
      const body = det.querySelector(".tg-body");
      for (const t of tools) body.appendChild(buildToolRow(t));
      updateGroupTitle(det);
    }
    return det;
  }

  function updateGroupTitle(det) {
    const rows = [...det.querySelectorAll(".tg-row")];
    const n = rows.length;
    const running = rows.some((row) => row.dataset.done !== "1");
    const errors = rows.filter((row) => row.classList.contains("is-error")).length;
    const title = det.querySelector(".tg-title");
    const status = det.querySelector(".tg-status");
    det.classList.toggle("is-running", running);
    det.classList.toggle("is-error", !running && errors > 0);
    if (n === 1) {
      const summary = rows[0].dataset.summary || rows[0].querySelector(".tool-name")?.textContent || "工具";
      title.textContent = running ? summary : summary;
    } else {
      title.textContent = running ? ("运行中 · " + n + " 个工具") : ("已运行 " + n + " 个工具");
    }
    if (status) {
      status.classList.toggle("is-error", errors > 0);
      if (running) status.textContent = "…";
      else if (errors) status.textContent = errors === n ? "失败" : (errors + " 失败");
      else status.textContent = "完成";
    }
  }

  function toolTimingLabel(t) {
    if (t.dispatch_seconds == null) return t.elapsed != null ? t.elapsed + "s" : "完成";
    return "执行 " + (t.elapsed != null ? t.elapsed + "s" : "未知") +
      " · 调度 " + t.dispatch_seconds + "s";
  }

  function buildToolRow(t) {
    const row = document.createElement("details");
    row.className = "tg-row" + (t.is_error ? " is-error" : "");
    row.dataset.done = t.done ? "1" : "0";
    const summary = toolLineSummary(t.name, t.args);
    row.dataset.summary = summary;
    const argsText = toolValueText(t.args);
    const resultText = toolValueText(t.result);
    const status = t.done
      ? t.is_error
        ? "<span class='tool-elapsed tool-error'>失败</span>"
        : "<span class='tool-elapsed'>" + esc(toolTimingLabel(t)) + "</span>"
      : "<span class='tool-elapsed'>执行中</span>";
    row.innerHTML =
      "<summary><span class='tool-name'>" + esc(summary) + "</span>" + status + "</summary>" +
      "<div class='tool-body'>" + esc(argsText.slice(0, 2000)) +
      (resultText ? "\n\n→ " + esc(resultText) : "") + "</div>";
    return row;
  }

  function renderTool(t, trackTurn = true) {
    if (!currentMsg) beginAssistant();
    finalizeThinking(true);
    closeMarkdownSegment();
    if (!currentToolGroup) {
      currentToolGroup = buildToolGroup();
      currentMsg.querySelector(".msg-bubble").appendChild(currentToolGroup);
    }
    const row = buildToolRow({ ...t, result: "", is_error: false, done: false });
    currentToolGroup.querySelector(".tg-body").appendChild(row);
    updateGroupTitle(currentToolGroup);
    toolRows.set(t.id, row);
    if (trackTurn) pendingTurnToolIds.add(t.id);
    scrollBottom();
  }

  function renderToolResult(r) {
    const row = toolRows.get(r.id);
    if (!row) return;
    pendingTurnToolIds.delete(r.id);
    row.dataset.done = "1";
    row.classList.toggle("is-error", !!r.is_error);
    const span = row.querySelector(".tool-elapsed");
    if (r.is_error) {
      span.textContent = "失败";
      span.classList.add("tool-error");
    } else {
      span.textContent = toolTimingLabel(r);
      span.classList.remove("tool-error");
    }
    const outputText = toolValueText(r.output);
    if (outputText) row.querySelector(".tool-body").textContent += "\n\n→ " + outputText;
    if (currentToolGroup) updateGroupTitle(currentToolGroup);
    scrollBottom();
  }

  function settlePendingToolRows(msg = {}) {
    const interrupted = !!msg.interrupted;
    const failed = !!msg.error;
    const label = interrupted ? "已中断" : failed ? "失败" : "结果未知";
    const detail = interrupted
      ? "工具调用随本轮任务中断，未返回结果。"
      : failed
        ? "工具调用因本轮任务异常结束，未返回结果。"
        : "本轮已结束，但工具未返回可确认的结果。";
    const groups = new Set();
    for (const toolId of pendingTurnToolIds) {
      const row = toolRows.get(toolId);
      if (!row || row.dataset.done === "1") continue;
      row.dataset.done = "1";
      row.classList.toggle("is-error", failed);
      const span = row.querySelector(".tool-elapsed");
      if (span) {
        span.textContent = label;
        span.classList.toggle("tool-error", failed);
      }
      const body = row.querySelector(".tool-body");
      if (body) body.textContent += "\n\n→ " + detail;
      const group = row.closest(".tool-group");
      if (group) groups.add(group);
    }
    for (const group of groups) {
      updateGroupTitle(group);
      const status = group.querySelector(".tg-status");
      if (status && !failed && !group.classList.contains("is-error")) {
        status.textContent = label;
      }
    }
    pendingTurnToolIds.clear();
  }

  function buildTurnChangesCard(changes) {
    const card = document.createElement("section");
    card.className = "turn-changes";
    const list = Array.isArray(changes) ? changes : [];
    const kindLabel = { add: "新增", modify: "修改", delete: "删除" };
    card.innerHTML =
      "<header><strong>本轮变更</strong><span>" + list.length + " 个文件</span></header>" +
      "<ul class='turn-changes-list'></ul>";
    const ul = card.querySelector(".turn-changes-list");
    for (const item of list) {
      const li = document.createElement("li");
      const kind = String(item.kind || "modify");
      li.className = "kind-" + kind;
      li.innerHTML =
        "<span class='change-kind'>" + esc(kindLabel[kind] || kind) + "</span>" +
        "<code class='change-path'>" + esc(item.path || "") + "</code>";
      ul.appendChild(li);
    }
    return card;
  }

  // 消息右下角：ctx · 模型 · effort（可点击修改）
  function attachMsgFooter(status) {
    if (!currentMsg || !status || !status.model) return;
    const footer = document.createElement("div");
    footer.className = "msg-footer";

    let ctxText = "ctx --";
    if (status.window > 0 && status.ctx > 0) {
      const pct = Math.min(Math.round((status.ctx / status.window) * 100), 100);
      ctxText = "ctx " + pct + "% (" + fmtInt(status.ctx) + "/" + fmtInt(status.window) + ")";
    } else if (status.window > 0) {
      ctxText = "ctx --/" + fmtInt(status.window);
    }
    footer.innerHTML =
      "<span class='mf-item'>" + ctxText + "</span>" +
      "<span class='mf-item'>·</span>" +
      "<span class='mf-item mf-click' data-kind='model' title='点击切换模型'>" +
      esc(status.model) + "</span>" +
      "<span class='mf-item'>·</span>" +
      "<span class='mf-item mf-click' data-kind='effort' title='点击调整 effort'>effort:" +
      esc(status.effort || "auto") + "</span>";
    currentMsg.appendChild(footer);

    footer.querySelector('[data-kind="model"]').addEventListener("click", (e) => {
      e.stopPropagation();
      showPopover(e.target, (pop) => {
        pop.appendChild(popTitle("切换模型"));
        if (!modelList.length) {
          pop.appendChild(popItem("（拉取列表中…）", false, () => {}));
          send({ type: "list_models" });
        }
        for (const m of modelList) {
          pop.appendChild(popItem(m, m === currentStatus.model, () =>
            send({ type: "switch_model", model: m })));
        }
      });
    });

    footer.querySelector('[data-kind="effort"]').addEventListener("click", (e) => {
      e.stopPropagation();
      const levels = currentEfforts.levels || [];
      if (!levels.length) {
        addSystemMsg("当前模型不支持 effort 调整");
        return;
      }
      showPopover(e.target, (pop) => {
        pop.appendChild(popTitle("调整 Effort"));
        pop.appendChild(popItem("auto", (currentEfforts.current || "auto") === "auto",
          () => send({ type: "set_effort", level: "" })));
        for (const lv of levels) {
          pop.appendChild(popItem(lv, lv === currentEfforts.current, () =>
            send({ type: "set_effort", level: lv })));
        }
      });
    });
  }

  function addSessionSummary(msg) {
    if (!msg.status) return;
    const st = msg.status;
    const div = document.createElement("div");
    div.className = "session-summary";
    div.innerHTML =
      "<span class='ss-card'>本轮 " + msg.elapsed + "s · Session " +
      esc(st.session) + " · " + fmtInt(st.total_in) + " in / " +
      fmtInt(st.total_out) + " out</span>";
    messagesEl.appendChild(div);
    scrollBottom();
  }

  // ---------- 状态渲染 ----------

  function updateState(msg) {
    const previousSlotSig = JSON.stringify(
      (currentSlots || []).map((slot) => [slot.key, slot.error || ""])
    );
    currentStatus = msg.status || {};
    currentRuntimeLoad = msg.runtime_load || currentRuntimeLoad;
    if (Array.isArray(msg.commands) && msg.commands.length) {
      slashCommands = msg.commands;
      renderSlashMenu();
    }
    if (Array.isArray(msg.running_conversations)) {
      const serverRunning = new Set(
        msg.running_conversations.map((id) => String(id || "")).filter(Boolean)
      );
      runningConversations.clear();
      const activeId = String((msg.status && msg.status.conversation_id) || currentStatus.conversation_id || "");
      for (const convId of serverRunning) {
        runningConversations.add(convId);
        if (pendingConversationStarts.has(convId)) {
          clearPendingConversationStart(convId);
        }
        if (convId && convId !== activeId) {
          const stub = { source: "mewcode", node_id: "local", id: convId, external_id: convId };
          const key = conversationStateKey(stub);
          if (!sidebarUnreadKeys.has(key)) {
            sidebarUnreadKeys.add(key);
            void patchSidebarConversationState("mark_unread", stub).catch(() => {});
          }
        }
      }
      for (const convId of Array.from(stoppingConversations)) {
        if (!serverRunning.has(convId)) {
          clearConversationStopTimer(convId);
          stoppingConversations.delete(convId);
        }
      }
    }
    if (msg.conversation_queues && typeof msg.conversation_queues === "object") {
      window.MewChatQueue?.syncAll?.(
        msg.conversation_queues,
        msg.conversation_queue_revisions
      );
    }
    currentEfforts = msg.efforts || { levels: [], current: "auto" };
    currentSlots = msg.slots || [];
    permissionModes = msg.permission_modes || permissionModes;
    if (
      pendingAttachments.length && currentStatus.conversation_id &&
      pendingAttachments.some((item) => item.conversation_id !== currentStatus.conversation_id)
    ) {
      discardPendingAttachments();
    }

    // 输入区按钮标签
    const modeChoice = permissionModes.find(
      (m) => m.value === (currentStatus.permission_mode === "plan" ? "plan" : (currentStatus.permission_level || currentStatus.permission_mode))
    );
    $("perm-mode-label").textContent = modeChoice ? modeChoice.label : "授权运行";
    updateModelIntelligenceLabels();

    // 多 Agent 模式开关状态
    $("multi-agent-btn").classList.toggle("ma-on", !!currentStatus.multi_agent);
    if (currentStatus.expert_mode) {
      orchestrationPlanning = false;
    } else if (typeof currentStatus.orchestration_planning === "boolean") {
      orchestrationPlanning = currentStatus.orchestration_planning;
    }
    window.MewWorkbench?.store.refresh();

    stopBtn.title = currentStatus.execution_kind === "orchestration" ? "暂停任务并保存检查点" : "打断";
    stopBtn.setAttribute("aria-label", stopBtn.title);
    updateShellStatus();

    updateContextBadge(currentStatus);
    updateStatusLine();
    syncActiveStreaming();
    if (msg.tasks) renderTasks(msg.tasks);
    const nextSlotSig = JSON.stringify(
      (currentSlots || []).map((slot) => [slot.key, slot.error || ""])
    );
    if (previousSlotSig !== nextSlotSig) {
      modelCatalog = null;
      modelCatalogAt = 0;
      loadModelCatalog(true).then(() => {
        if (runtimeSettingsView) renderRuntimeSettingsPanel(runtimeSettingsView);
      }).catch(() => {});
    }
    reconcileRuntimeSettingsState();
  }

  function updateShellStatus() {
    $("topbar-mode-normal").classList.toggle(
      "hidden",
      !!currentStatus.multi_agent || !!currentStatus.expert_mode || !!orchestrationPlanning
    );
    $("topbar-mode-multi").classList.toggle("hidden", !currentStatus.multi_agent || !!currentStatus.expert_mode);
    $("topbar-mode-orch-plan").classList.toggle(
      "hidden",
      !orchestrationPlanning || !!currentStatus.expert_mode
    );
    $("topbar-mode-expert").classList.toggle("hidden", !currentStatus.expert_mode);
    renderContinuousStatus();
    $("topbar-project").textContent = currentStatus.project_name || "MewCode";
    $("topbar-project").title = currentStatus.work_dir || "";
    $("topbar-conversation").textContent = currentStatus.conversation_title || "新对话";
  }

  function updateStatusLine() {
    const st = currentStatus;
    const cache = cacheUsage(st);
    sessionBadge.textContent =
      (st.session || "--") + " · " + fmtInt(st.total_in) + " in / " +
      fmtInt(st.total_out) + " out";

    if (cacheBadge) {
      const cacheKnown = cache.available;
      cacheBadge.textContent = cacheKnown
        ? "缓存命中 " + formatCacheRate(cache.rate)
        : "缓存命中 --";
      cacheBadge.textContent += cacheKnown
        ? " · hit " + fmtInt(cache.cumulativeRead) + " / miss " +
          fmtInt(cache.cumulativeMiss)
        : (cache.epoch > 0 ? " · API 未上报" : "");
      cacheBadge.title = cacheKnown
        ? "缓存读取 " + fmtInt(cache.cacheRead) + " / prompt " +
          fmtInt(cache.promptTokens) + " tokens" +
          (cache.cacheCreation > 0 ? " · 缓存写入 " + fmtInt(cache.cacheCreation) : "")
        : "等待模型返回缓存用量";
      cacheBadge.classList.toggle("is-pending", !cacheKnown);
      cacheBadge.classList.toggle("has-hit", cacheKnown && cache.rate > 0);
    }

    if (!runtimeCapsule || !runtimeCapsuleLabel) return;
    const activeSlot = currentSlots.find((slot) => slot.current);
    // Slot labels include both the configured model and source.  The capsule
    // renders those separately, so use the provider name to avoid misleading
    // output such as "Grok [grok-4.6] (desktop) · grok-4.6".
    const provider = String(st.provider || (activeSlot && activeSlot.label) || "MewCode")
      .replace(/^★\s*/, "");
    const model = st.model || "--";
    const context = ctxBadge.textContent || "ctx --";
    const summary = currentIqSummary();
    const intelligence = summary ? ` · ${iqText(summary)}` : "";
    runtimeCapsuleLabel.textContent = `${provider} · ${model}${intelligence} · ${context}`;
    runtimeCapsule.title = `Provider ${provider} · 模型 ${model} · 推理 ${currentEfforts.current || "auto"}${intelligence} · ${context}`;
    runtimeCapsule.classList.toggle("ready", !!activeSlot);
  }

  function updateUsage(u) {
    currentStatus.ctx = u.ctx;
    // The state/status payload owns the effective model window. Usage events
    // can arrive late from an older turn, so they must not replace its denominator.
    currentStatus.total_in = u.total_in;
    currentStatus.total_out = u.total_out;
    currentStatus.cache_read = u.cache_read;
    currentStatus.cache_creation = u.cache_creation;
    currentStatus.cache_usage_available = u.cache_usage_available === true;
    currentStatus.prompt_tokens = u.prompt_tokens != null
      ? u.prompt_tokens
      : (Number(u.total_in) || 0) + (Number(u.cache_read) || 0) +
        (Number(u.cache_creation) || 0);
    currentStatus.cache_hit_rate = u.cache_hit_rate != null
      ? u.cache_hit_rate
      : (currentStatus.cache_usage_available
        ? cacheUsage(currentStatus).rate
        : null);
    currentStatus.cache_miss = u.cache_miss;
    currentStatus.cache_request_read = u.cache_request_read;
    currentStatus.cache_request_miss = u.cache_request_miss;
    currentStatus.cache_request_hit_rate = u.cache_request_hit_rate;
    currentStatus.cache_request_usage_available =
      u.cache_request_usage_available === true;
    currentStatus.cache_cumulative_read = u.cache_cumulative_read;
    currentStatus.cache_cumulative_miss = u.cache_cumulative_miss;
    currentStatus.cache_cumulative_hit_rate = u.cache_cumulative_hit_rate;
    currentStatus.cache_cumulative_usage_available =
      u.cache_cumulative_usage_available === true;
    currentStatus.cache_epoch = u.cache_epoch;
    currentStatus.cache_invalidation_reason = u.cache_invalidation_reason;
    currentStatus.cache_request_cost_usd = u.cache_request_cost_usd;
    currentStatus.cache_cost_usd = u.cache_cost_usd;
    currentStatus.cache_system_hash = u.cache_system_hash;
    currentStatus.cache_tools_hash = u.cache_tools_hash;
    currentStatus.cache_lcp_tokens = u.cache_lcp_tokens;
    updateContextBadge(currentStatus);
    updateStatusLine();
  }

  // ---------- 输入区弹层：权限模式 / 模型·effort ----------

  function openProjectPermissionGrants(trigger) {
    openSettingsDialog("项目持久授权", trigger, (body) => {
      const status = document.createElement("p");
      status.className = "settings-intro";
      status.textContent = "正在读取当前项目的授权…";
      body.appendChild(status);
      (async () => {
        try {
          const response = await fetch("/api/permissions/grants");
          const data = await response.json();
          if (!response.ok) throw new Error(data.error || "授权列表读取失败");
          status.textContent = `当前项目：${data.project}。目录授权包含子目录，不包含同级目录。`;
          if (!data.grants.length) {
            const empty = document.createElement("p");
            empty.textContent = "当前没有有效的持久授权。";
            body.appendChild(empty);
            return;
          }
          for (const grant of data.grants) {
            const row = document.createElement("div");
            row.className = "settings-intro";
            const label = document.createElement("span");
            const scope = Array.isArray(grant.value) ? grant.value.join(" ") : String(grant.value || "");
            label.textContent = `${grant.kind}：${scope}（到期 ${new Date(grant.expires_at * 1000).toLocaleString()}） `;
            row.appendChild(label);
            const revoke = document.createElement("button");
            revoke.type = "button";
            revoke.className = "btn deny";
            revoke.textContent = "撤销";
            revoke.addEventListener("click", async () => {
              revoke.disabled = true;
              try {
                const result = await fetch("/api/permissions/grants/revoke", {
                  method: "POST",
                  headers: { "Content-Type": "application/json", "X-Mew-User-Grant": "1" },
                  body: JSON.stringify({ project: data.project, grant_id: grant.grant_id }),
                });
                const outcome = await result.json();
                if (!result.ok) throw new Error(outcome.error || "撤销失败");
                row.remove();
                notify("授权已撤销", "success");
              } catch (error) {
                notify(error.message || String(error), "error");
                revoke.disabled = false;
              }
            });
            row.appendChild(revoke);
            body.appendChild(row);
          }
        } catch (error) {
          status.textContent = error.message || String(error);
        }
      })();
    });
  }

  $("perm-mode-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    showPopover(e.currentTarget, (pop) => {
      pop.appendChild(popTitle("权限级别"));
      for (const m of permissionModes) {
        pop.appendChild(popItem(
          m.label, m.value === (currentStatus.permission_mode === "plan" ? "plan" : (currentStatus.permission_level || currentStatus.permission_mode)),
          () => send({ type: "set_permission_mode", mode: m.value }), m.desc
        ));
      }
      pop.appendChild(popItem("查看/撤销项目持久授权", false,
        () => openProjectPermissionGrants(e.currentTarget), "只显示当前项目的有效授权"));
    }, "up");
  });

  // 多 Agent 模式触发器：默认关闭（省 token），点击切换
  $("multi-agent-btn").addEventListener("click", () => {
    const next = !currentStatus.multi_agent;
    send({ type: "set_multi_agent", on: next });
    addSystemMsg(next
      ? "已开启多 Agent 模式：主 Agent 可派发子 Agent / 组建团队，token 消耗会显著增加，进度见右上角后台任务面板"
      : "已关闭多 Agent 模式：主 Agent 将独自完成任务，不再派发子 Agent");
  });

  function showRuntimeSettings(anchor, placement = "up") {
    const catalog = modelCatalog || { providers: [], models: [] };
    const families = runtimeProviderFamilies(catalog);
    const currentFamily = families.find((item) =>
      item.providerKeys.includes(currentStatus.provider_key)
    ) || families.find((item) => item.available) || families[0];
    const rememberedFamily = families.find((item) => item.key === rememberedFamilyKey);
    const initialFamily = rememberedFamily || currentFamily || families[0];
    const providerKey = (
      initialFamily &&
      initialFamily.providerKeys.includes(currentStatus.provider_key)
    )
      ? currentStatus.provider_key
      : (
        (initialFamily && initialFamily.providerKeys[0]) ||
        currentStatus.provider_key ||
        ""
      );
    const state = {
      anchor,
      placement,
      familyKey: (initialFamily && initialFamily.key) || "codex",
      providerKey,
      modelId: currentStatus.model || "",
      query: "",
      refreshing: false,
      feedback: "",
      feedbackError: false,
      pending: null,
      pendingFailed: false,
      advancedOpen: false,
      focusSearch: false,
      root: null,
    };
    runtimeSettingsView = state;
    showPopover(anchor, (pop) => {
      pop.classList.add("runtime-model-popover");
      pop.setAttribute("role", "dialog");
      pop.setAttribute("aria-label", "模型与推理设置");
      state.root = pop;
      renderRuntimeSettingsPanel(state);
    }, placement);
    // The picker must be usable while the remote IQ/catalog request is pending.
    // A slow or unavailable radar feed must never block the click from opening it.
    void refreshRuntimeSettings(state, false, true);
    if (catalogRefreshTimer) clearInterval(catalogRefreshTimer);
    catalogRefreshTimer = setInterval(async () => {
      if (popover.classList.contains("hidden") || runtimeSettingsView !== state) {
        clearInterval(catalogRefreshTimer);
        catalogRefreshTimer = null;
        return;
      }
      await refreshRuntimeSettings(state, false, true);
    }, (modelCatalog && modelCatalog.cc_switch && modelCatalog.cc_switch.available) ? 8000 : 60000);
  }

  $("model-effort-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    showRuntimeSettings(e.currentTarget);
  });
  if (runtimeCapsule) {
    runtimeCapsule.addEventListener("click", (e) => {
      e.stopPropagation();
      showRuntimeSettings(e.currentTarget, "down");
    });
  }

  // ---------- 任务面板 ----------

  function taskListDuration(task) {
    if (task.duration_basis !== "active_runtime") return "旧记录时长未知";
    if (task.duration_quality === "unknown") return "执行时长未知";
    if (typeof task.elapsed !== "number" || !Number.isFinite(task.elapsed) || task.elapsed < 0) return "执行时长未知";
    if (!task.runtime_started && task.elapsed === 0) return "未开始";
    const measured = fmtDur(task.elapsed);
    if (task.duration_quality === "lower_bound") return "至少 " + measured + "（下界）";
    return (task.status === "queued" ? "累计执行 " : "实际执行 ") + measured;
  }

  function renderTasks(tasks) {
    const cid = currentStatus.conversation_id || "default";
    try {
      sessionStorage.setItem("mewcode-tasks-" + cid, JSON.stringify(tasks));
    } catch { /* ignore */ }

    const list = $("task-list");
    const running = tasks.filter((t) => t.status === "running").length;
    const queued = tasks.filter((t) => t.status === "queued").length;
    $("task-count").textContent = [running ? running + " 运行中" : "", queued ? queued + " 排队中" : ""]
      .filter(Boolean).join(" · ") || tasks.length + " 个";
    list.innerHTML = "";
    if (!tasks.length) {
      list.innerHTML = "<li class='conv-empty'>暂无任务</li>";
      return;
    }
    for (const t of tasks) {
      const li = document.createElement("li");
      li.className = "task-item";
      const status = t.cleanup_failed ? "清理未确认" : t.cancellation_pending ? "正在停止…" : ({
        running: "运行中", queued: "排队中", completed: "已完成", failed: "失败", cancelled: "已取消",
      }[t.status] || "状态未知");
      const owner = t.owner_task_title || t.owner_task_id || "归属未记录";
      li.innerHTML =
        "<span class='task-status st-" + esc(t.status) + "'>" + esc(status) + "</span>" +
        "<span class='task-name'>" + esc(t.name) + "</span>" +
        "<div class='task-owner'>所属任务：" + esc(owner) + "</div>" +
        "<div style='color:var(--text-faint)'>" + esc(t.task) + " · " + esc(taskListDuration(t)) + "</div>";
      list.appendChild(li);
    }
  }

  function restoreTasksCache() {
    const cid = currentStatus.conversation_id || "default";
    try {
      const raw = sessionStorage.getItem("mewcode-tasks-" + cid);
      if (raw) renderTasks(JSON.parse(raw));
    } catch { /* ignore */ }
  }

  // ---------- 用量统计面板（右上角用户按钮） ----------

  const usageOverlay = $("usage-overlay");
  const usageTip = $("usage-tip");
  let usageDays = 0;   // 0=全部 / 30 / 7

  function fmtTokens(n) {
    n = n || 0;
    if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + "K";
    return String(n);
  }

  function fmtHour(h) {
    if (h < 0) return "--";
    return h < 12 ? (h === 0 ? 12 : h) + " AM" : (h === 12 ? 12 : h - 12) + " PM";
  }

  function shortModelName(id) {
    return String(id || "").split("/").pop().replace(/-\d{8}$/, "");
  }

  const usageSourceLabels = {
    official_api: "官方 API",
    provider_portal: "Provider 门户",
    local_ledger: "本地账本",
    configured: "本地配置",
    derived: "合法派生",
  };

  function formatMoneyValue(metric, currency) {
    if (!metric) return "不可用";
    const raw = String(metric.value);
    const compact = raw.includes(".")
      ? raw.replace(/(\.\d*?[1-9])0+$/, "$1").replace(/\.0+$/, "")
      : raw;
    return compact + " " + currency;
  }

  function usageMetric(label, metric, currency) {
    const source = metric ? (usageSourceLabels[metric.provenance] || metric.provenance) : "未提供";
    const quality = metric && metric.quality === "estimate" ? " · 估算" : "";
    return "<div class='usage-money'><span>" + esc(label) + "</span><strong>" +
      esc(formatMoneyValue(metric, currency)) + "</strong><small>" + esc(source + quality) + "</small></div>";
  }

  function renderUsageCards(s) {
    const cards = [
      ["会话数", fmtInt(s.sessions)],
      ["消息数", fmtInt(s.messages)],
      ["总 Token", fmtTokens(s.total_tokens)],
      ["活跃天数", fmtInt(s.active_days)],
      ["当前连击", s.current_streak + "d"],
      ["最长连击", s.longest_streak + "d"],
      ["高峰时段", fmtHour(s.peak_hour)],
      ["常用模型", shortModelName(s.favorite_model) || "--"],
    ];
    $("usage-cards").innerHTML = cards.map(([label, value]) =>
      "<div class='usage-card'><div class='uc-label'>" + esc(label) +
      "</div><div class='uc-value' title='" + esc(value) + "'>" + esc(value) + "</div></div>"
    ).join("");
  }

  function renderUsageGrid(data) {
    // 网格天数：7 / 30 跟随范围；全部固定展示最近 112 天
    const span = usageDays > 0 ? usageDays : 112;
    const days = data.days || {};
    const today = new Date(data.today + "T00:00:00");
    const cells = [];
    let maxTokens = 0;
    for (let i = span - 1; i >= 0; i--) {
      const d = new Date(today);
      d.setDate(today.getDate() - i);
      const key = d.getFullYear() + "-" +
        String(d.getMonth() + 1).padStart(2, "0") + "-" +
        String(d.getDate()).padStart(2, "0");
      const rec = days[key] || null;
      if (rec) maxTokens = Math.max(maxTokens, rec.tokens);
      cells.push({ key, rec });
    }
    const level = (t) => {
      if (!t || maxTokens <= 0) return 0;
      const r = t / maxTokens;
      return r > 0.75 ? 4 : r > 0.5 ? 3 : r > 0.25 ? 2 : 1;
    };
    $("usage-grid").innerHTML = cells.map((c) => {
      const lv = level(c.rec && c.rec.tokens);
      return "<span class='usage-cell" + (lv ? " l" + lv : "") +
        "' data-date='" + c.key + "'></span>";
    }).join("");

    const note = usageDays > 0
      ? "网格展示最近 " + span + " 天，每格一天，颜色越深 token 用量越多。"
      : "网格展示最近 112 天（统计卡含全部历史），颜色越深 token 用量越多。";
    $("usage-note").textContent = note;
    // 悬停查详情用：缓存当前 days 数据
    $("usage-grid").dataset.days = JSON.stringify(days);
  }

  function renderUsageFinancials(data) {
    const providers = (data && data.providers) || [];
    if (!providers.length) {
      $("usage-financials").innerHTML = "<div class='usage-empty'>尚未配置 Provider。配置官方或中转后，将按官方规则或额度探测显示计量。</div>";
      return;
    }
    const stateLabel = {
      ok: "已更新",
      error: "查询失败",
      missing_auth: "缺少凭据",
      unsupported: "暂不支持",
    };
    $("usage-financials").innerHTML = providers.map((card) => {
      const kind = card.kind === "official" ? "官方" : "中转";
      const state = stateLabel[card.status] || card.status || "";
      const lines = (card.lines || []).map((line) =>
        "<div class='usage-money'><span>" + esc(line.label || "") + "</span><strong>" +
        esc(line.value || "") + "</strong><small>" + esc(line.hint || "") + "</small></div>"
      ).join("");
      const body = lines
        ? "<div class='usage-money-grid usage-meter-grid'>" + lines + "</div>"
        : "<div class='usage-account-unavailable'>" + esc(card.message || card.headline || "暂无计量") + "</div>";
      return "<section class='usage-account-card'><header><span><strong>" + esc(card.name || card.provider_key) +
        "</strong><small>" + esc(kind + (card.vendor ? " · " + card.vendor : "") +
        (card.model ? " · " + card.model : "")) +
        "</small></span><em class='usage-state st-" + esc(card.status || "ok") + "'>" + esc(state) +
        "</em></header><div class='usage-meter-headline'>" + esc(card.headline || "") + "</div>" +
        body +
        (card.message && lines ? "<div class='usage-account-detail'>" + esc(card.message) + "</div>" : "") +
        (card.base_url ? "<div class='usage-account-detail'>" + esc(card.base_url) + "</div>" : "") +
        "</section>";
    }).join("");
  }

  async function loadUsage() {
    try {
      const [statsRes, metersRes] = await Promise.all([
        fetch("/api/usage/stats?days=" + usageDays),
        fetch("/api/usage/providers"),
      ]);
      if (!statsRes.ok) throw new Error("用量统计不可用");
      const data = await statsRes.json();
      renderUsageCards(data.summary || {});
      renderUsageGrid(data);
      if (metersRes.ok) {
        renderUsageFinancials(await metersRes.json());
      } else {
        let detail = "Provider 计量不可用";
        try {
          const err = await metersRes.json();
          detail = err.error || detail;
        } catch (_) {}
        $("usage-financials").innerHTML = "<div class='usage-empty'>" + esc(detail) + "</div>";
      }
    } catch (err) {
      $("usage-cards").innerHTML = "<div class='usage-card'><div class='uc-label'>加载失败</div><div class='uc-value'>--</div></div>";
      $("usage-grid").innerHTML = "";
      $("usage-financials").innerHTML = "<div class='usage-empty'>" + esc(err.message || "用量加载失败") + "</div>";
    }
  }

  function openUsage() {
    usageOverlay.classList.remove("hidden");
    loadUsage();
  }

  function closeUsage() {
    usageOverlay.classList.add("hidden");
    usageTip.classList.add("hidden");
  }

  async function usageJson(url, options) {
    const response = await fetch(url, options);
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "HTTP " + response.status);
    return payload;
  }

  $("user-btn").addEventListener("click", openUsage);
  const usageRefreshBtn = $("usage-refresh");
  if (usageRefreshBtn) {
    usageRefreshBtn.addEventListener("click", () => loadUsage());
  }
  $("usage-close").addEventListener("click", closeUsage);
  usageOverlay.addEventListener("click", (e) => {
    if (e.target === usageOverlay) closeUsage();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !usageOverlay.classList.contains("hidden")) closeUsage();
  });

  document.querySelectorAll(".usage-ranges button").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".usage-ranges button").forEach((b) => b.classList.remove("on"));
      btn.classList.add("on");
      usageDays = parseInt(btn.dataset.days, 10) || 0;
      loadUsage();
    });
  });

  // 悬停提示：事件委派到网格容器，tooltip 跟随鼠标
  $("usage-grid").addEventListener("mousemove", (e) => {
    const cell = e.target.closest(".usage-cell");
    if (!cell) { usageTip.classList.add("hidden"); return; }
    let days = {};
    try { days = JSON.parse($("usage-grid").dataset.days || "{}"); } catch { /* ignore */ }
    const rec = days[cell.dataset.date];
    const [, m, d] = cell.dataset.date.split("-");
    const dateLabel = parseInt(m, 10) + "月" + parseInt(d, 10) + "日";
    let html = "<strong>" + esc(dateLabel) + "</strong> · ";
    if (!rec) {
      html += "无用量";
    } else {
      html += fmtTokens(rec.tokens) + " tokens";
      if (rec.in || rec.out) {
        html += "<div class='tip-sub'>输入 " + fmtTokens(rec.in) + " · 输出 " + fmtTokens(rec.out) + " · " + fmtInt(rec.messages) + " 条消息</div>";
      } else {
        html += "<div class='tip-sub'>" + fmtInt(rec.messages) + " 条消息" + (rec.approx ? " · 历史估算" : "") + "</div>";
      }
      if (rec.approx && (rec.in || rec.out)) {
        html += "<div class='tip-sub'>含历史会话估算值</div>";
      }
    }
    usageTip.innerHTML = html;
    usageTip.classList.remove("hidden");
    const tw = usageTip.offsetWidth, th = usageTip.offsetHeight;
    let x = e.clientX + 12, y2 = e.clientY - th - 10;
    if (x + tw > window.innerWidth - 8) x = e.clientX - tw - 12;
    if (y2 < 8) y2 = e.clientY + 14;
    usageTip.style.left = x + "px";
    usageTip.style.top = y2 + "px";
  });
  $("usage-grid").addEventListener("mouseleave", () => usageTip.classList.add("hidden"));

  // ---------- 后台任务面板（右侧滑出：子 Agent / 团队实时遥测） ----------

  const btPanel = $("bgtasks-panel");
  const btBackdrop = $("bgtasks-backdrop");
  let btTimer = null;
  let btClockTimer = null;
  let btPollRevision = 0;
  let btCurrentConversationId = activeConversationId();
  let btLastData = null;
  const btClocks = new Map();
  const BT_CLOCK_MAX_AGE_MS = 5000;

  function btClockKey(row) {
    return String(row.id || row.task_id || row.name || "");
  }

  function btClockState(row) {
    return btClocks.get(btClockKey(row)) || null;
  }

  function btClockCanTick(row, state = btClockState(row)) {
    return !!state && state.canTick
      && (row.status === "running" || row.cleanup_failed === true);
  }

  function btElapsed(row, now = performance.now()) {
    const state = btClockState(row);
    if (!state) {
      return row.duration_basis === "legacy_wall" ? 0 : Math.max(Number(row.elapsed) || 0, 0);
    }
    let elapsed = state.baseElapsed;
    if (btClockCanTick(row, state) && state.anchor !== null) {
      const age = Math.min(Math.max(now - state.anchor, 0), BT_CLOCK_MAX_AGE_MS);
      elapsed += age * state.slots / 1000;
    }
    return Math.max(elapsed, 0);
  }

  function btDurationLabel(row, now = performance.now()) {
    const state = btClockState(row);
    if (row.duration_basis === "active_runtime" && row.duration_quality === "unknown") {
      return "执行时长未知";
    }
    const legacyUnknown = row.duration_basis === "legacy_wall"
      || (row.duration_quality === "unknown" && row.duration_basis !== "active_runtime");
    if (legacyUnknown) {
      return btClockCanTick(row, state)
        ? "本次观测 " + fmtDur(btElapsed(row, now)) + "（旧记录总时长未知）"
        : "旧记录时长未知";
    }
    if (["queued", "running"].includes(row.status) && !row.runtime_started
        && !btClockCanTick(row, state)) {
      return row.status === "queued" ? "未开始" : "尚未执行";
    }
    const elapsed = btElapsed(row, now);
    if (row.duration_quality === "lower_bound") return "至少 " + fmtDur(elapsed) + "（下界）";
    if (row.status === "queued") {
      return row.runtime_started ? fmtDur(elapsed) + "（已暂停）" : "未开始";
    }
    if (row.status === "running" && !btClockCanTick(row, state) && row.runtime_started) {
      return fmtDur(elapsed) + "（累计）";
    }
    if (!row.runtime_started && row.status !== "running") return "未开始";
    return fmtDur(elapsed);
  }

  function btGroupClock(group, now = performance.now()) {
    const running = group.rows.filter((row) => row.status === "running");
    const queued = group.rows.some((row) => row.status === "queued");
    if (running.length) {
      const active = running.filter((row) => btClockCanTick(row));
      const started = running.filter((row) => row.runtime_started);
      const rows = active.length ? active : started;
      if (rows.length) return fmtDur(Math.max(...rows.map((row) => btElapsed(row, now))));
      return "尚未执行";
    }
    const cleanupUnconfirmed = group.rows.filter((row) => row.cleanup_failed === true);
    if (cleanupUnconfirmed.length) {
      const active = cleanupUnconfirmed.filter((row) => btClockCanTick(row));
      return active.length
        ? fmtDur(Math.max(...active.map((row) => btElapsed(row, now)))) + "（清理未确认）"
        : "清理未确认";
    }
    return queued ? "排队中" : "已结束";
  }

  function stopBgClockTimer(resetAnchors = true) {
    if (btClockTimer !== null) clearInterval(btClockTimer);
    btClockTimer = null;
    if (resetAnchors) {
      const now = performance.now();
      const rows = (btLastData?.groups || []).flatMap((group) => group.rows || []);
      for (const row of rows) {
        const state = btClockState(row);
        if (state && state.anchor !== null) state.baseElapsed = btElapsed(row, now);
      }
      for (const state of btClocks.values()) state.anchor = null;
    }
  }

  function freezeBgClocks() {
    const now = performance.now();
    const rows = (btLastData?.groups || []).flatMap((group) => group.rows || []);
    for (const row of rows) {
      const state = btClockState(row);
      if (!state) continue;
      state.baseElapsed = btElapsed(row, now);
      state.anchor = null;
      state.slots = 0;
      state.canTick = false;
    }
    stopBgClockTimer(false);
    updateBgTaskClockLabels();
  }

  function markBgPollFailure() {
    freezeBgClocks();
    $("bgtasks-sub").textContent = "更新失败 · 状态待刷新";
  }

  function validBgTaskPayload(data) {
    return !!data && typeof data === "object" && !Array.isArray(data)
      && Array.isArray(data.groups)
      && data.groups.every((group) => !!group && typeof group === "object"
        && !Array.isArray(group) && Array.isArray(group.rows)
        && group.rows.every((row) => !!row && typeof row === "object" && !Array.isArray(row)));
  }

  function invalidateBgPolls(clearRows = false) {
    btPollRevision += 1;
    stopBgClockTimer();
    if (clearRows) {
      btLastData = null;
      btClocks.clear();
      $("bgtasks-body").innerHTML = "<div class='bt-empty'>正在读取当前对话的后台任务…</div>";
    }
  }

  function observeBgTaskClocks(data) {
    const now = performance.now();
    const rows = (data.groups || []).flatMap((group) => group.rows || []);
    btClocks.clear();
    for (const row of rows) {
      const slots = Number(row.runtime_active_slots);
      const canTick = (row.status === "running" || row.cleanup_failed === true)
        && row.runtime_active === true
        && Number.isInteger(slots) && slots > 0;
      const legacyUnknown = row.duration_basis === "legacy_wall"
        || (row.duration_quality === "unknown" && row.duration_basis !== "active_runtime");
      btClocks.set(btClockKey(row), {
        baseElapsed: legacyUnknown ? 0 : Math.max(Number(row.elapsed) || 0, 0),
        anchor: canTick ? now : null,
        slots: canTick ? slots : 0,
        canTick,
      });
    }
    btLastData = data;
  }

  function updateBgTaskClockLabels() {
    if (document.hidden || btPanel.classList.contains("collapsed")) {
      stopBgClockTimer();
      return;
    }
    const now = performance.now();
    const hasFreshClock = [...btClocks.values()].some((state) => state.canTick
      && state.anchor !== null && now - state.anchor < BT_CLOCK_MAX_AGE_MS);
    const body = $("bgtasks-body");
    body.querySelectorAll("[data-bg-row-clock]").forEach((element) => {
      const row = (btLastData?.groups || []).flatMap((group) => group.rows || [])
        .find((item) => btClockKey(item) === element.dataset.bgRowClock);
      if (row) element.textContent = btDurationLabel(row, now);
    });
    body.querySelectorAll("[data-bg-group-clock]").forEach((element) => {
      const group = (btLastData?.groups || []).find((item) => String(item.key || "") === element.dataset.bgGroupClock);
      if (group) element.textContent = btGroupClock(group, now);
    });
    if (!hasFreshClock) stopBgClockTimer(false);
  }

  function syncBgClockTimer() {
    if (document.hidden || btPanel.classList.contains("collapsed")) {
      stopBgClockTimer();
      return;
    }
    const now = performance.now();
    const hasFreshClock = [...btClocks.values()].some((state) => state.canTick
      && state.anchor !== null && now - state.anchor < BT_CLOCK_MAX_AGE_MS);
    if (hasFreshClock && btClockTimer === null) {
      btClockTimer = setInterval(updateBgTaskClockLabels, 1000);
    } else if (!hasFreshClock) {
      stopBgClockTimer(false);
    }
  }

  function fmtDur(s) {
    const value = Number(s);
    s = Math.max(Number.isFinite(value) ? Math.round(value) : 0, 0);
    if (s < 60) return s + "s";
    if (s < 3600) return Math.floor(s / 60) + "m" + String(s % 60).padStart(2, "0") + "s";
    return Math.floor(s / 3600) + "h" + String(Math.floor((s % 3600) / 60)).padStart(2, "0") + "m";
  }

  function btNativePhase(row) {
    const native = row.native_execution || {};
    if (native.phase === "running") {
      return native.process_started === true && Number.isInteger(native.child_pid)
        ? "原生进程已启动 · PID " + native.child_pid : "原生启动未确认";
    }
    return ({ queued: "等待原生执行名额", preparing: "准备原生执行",
      starting: "启动器已启动 · 子进程待确认", cleaning: "正在取消/清理原生进程",
      finished: native.cleanup_confirmed === true ? "原生结束 · 清理已确认" : "原生清理未确认",
      cleanup_unknown: "原生清理未确认", not_started: "原生命令未启动",
      interrupted: "历史原生状态 · 当前执行未确认" })[native.phase] || "";
  }

  function btStatusLabel(st) {
    return { queued: "排队中", running: "运行中", completed: "完成", failed: "失败", cancelled: "已取消" }[st] || st;
  }

  function renderBgTasks(data) {
    currentRuntimeLoad = data.runtime_load || currentRuntimeLoad || {};
    observeBgTaskClocks(data);
    const groups = data.groups || [];
    const badge = $("bgtasks-badge");
    const running = Number(data.running_total) || 0;
    const activeTotal = running + (Number(data.queued_total) || 0);
    if (activeTotal > 0) {
      badge.textContent = activeTotal;
      badge.classList.remove("hidden");
    } else {
      badge.classList.add("hidden");
    }
    if (btPanel.classList.contains("collapsed")) return;

    const queued = data.queued_total !== undefined && data.queued_total !== null
      ? (Number(data.queued_total) || 0) : (Number(currentRuntimeLoad?.queued) || 0);
    $("bgtasks-sub").textContent = queued
      ? queued + " 个排队中 · " + running + " 个运行中"
      : (running ? running + " 个运行中" : (groups.length ? "全部结束" : ""));

    const body = $("bgtasks-body");
    if (!groups.length) {
      body.innerHTML = "<div class='bt-empty'>暂无子 Agent 任务<br>主 Agent 通过 Agent 工具派发的子任务会在这里实时展示</div>";
      syncBgClockTimer();
      return;
    }

    body.innerHTML = groups.map((g) => {
      const dots = g.rows.map((r) => {
        const cls = ["running", "queued"].includes(r.status) ? "running"
          : r.status === "completed" ? "done" : "failed";
        const statusLabel = r.cleanup_failed ? "清理未确认"
          : r.cancellation_pending && ["queued", "running"].includes(r.status)
            ? "正在停止…" : btStatusLabel(r.status);
        return "<span class='bt-dot " + cls + "' title='" + esc(r.name + " · " + statusLabel) + "'></span>";
      }).join("");
      const rows = g.rows.map((r) =>
        "<tr class='st-" + esc(r.status) + (r.cancellation_pending && ["queued", "running"].includes(r.status)
          ? " bt-cancellation-pending" : "") + "' title='" + esc(r.task || "") + "'>" +
        "<td class='btr-name'>" + esc(r.name) +
        (r.maintenance_id ? "<div class='bt-native-phase'>" + esc(r.waiting_user ? "等待你的回答" : r.activity || btStatusLabel(r.status)) + "</div>" +
          "<button class='bt-maintenance-btn' data-maintenance-id='" + esc(r.maintenance_id) + "'>" +
          (r.waiting_user ? "回答问题" : "查看修复对话") + "</button>" : "") +
        (btNativePhase(r) ? "<div class='bt-native-phase'>" + esc(btNativePhase(r)) + "</div>" : "") +
        (r.parent_task_id && !r.maintenance_id ? "<small>父任务 " + esc(r.parent_task_id) + "</small>" : "") +
        (r.cleanup_failed ? " <small class='bt-cleanup-warning'>清理未确认</small>" : "") +
        (r.status === "queued" ? " <small>排队第 " + esc(r.queue_position || "?") + " 位</small>" : "") +
        (r.cancellation_pending && ["queued", "running"].includes(r.status)
          ? " <small class='bt-cancel-pending'>正在停止…</small>" : "") +
        (["queued", "running"].includes(r.status) && r.task_id
          ? " <button class='bt-cancel-btn' data-task-id='" + esc(r.task_id) +
            "'" + (r.cancellation_pending ? " disabled" : "") +
            " title='" + (r.cancellation_pending ? "正在停止…" : "停止此子 Agent") +
            "' aria-label='" + (r.cancellation_pending ? "正在停止…" : "停止此子 Agent") + "'>" +
            (r.cancellation_pending ? "…" : "×") + "</button>"
          : "") +
        (r.result_available && r.task_id
          ? " <button class='bt-result-btn' data-task-id='" + esc(r.task_id) +
            "' title='查看结果' aria-label='查看结果'>ⓘ</button>"
          : "") + "</td>" +
        "<td>" + fmtTokens(r.tokens) + "</td>" +
        "<td>" + fmtInt(r.tools) + "</td>" +
        "<td><span data-bg-row-clock='" + esc(btClockKey(r)) + "'>" +
          esc(btDurationLabel(r)) + "</span></td></tr>"
      ).join("");
      const kindLabel = g.kind === "maintenance" ? "Fix 子 Agent · Auto"
        : g.kind === "task" ? "任务下的 Agent"
        : g.kind === "team" ? "团队成员" : "旧任务 Agent";
      const cancellable = g.rows.filter((r) => ["queued", "running"].includes(r.status)
        && !r.cancellation_pending && r.task_id);
      return "<div class='bt-group st-" + esc(g.status) + "'>" +
        "<div class='btg-head'><span class='btg-title'>" + esc(g.title) + "</span>" +
        (cancellable.length ? "<button class='bt-stop-group' data-group-key='" + esc(g.key) +
          "' title='停止本任务的全部后台子 Agent'>停止本任务</button>" : "") +
        "<span class='btg-time' data-bg-group-clock='" + esc(g.key) + "'>" +
          esc(btGroupClock(g)) + "</span></div>" +
        "<div class='btg-meta'>" + kindLabel + " · " + fmtInt(g.agents) + " 个 Agent · " +
        fmtTokens(g.tokens) + " tokens · " + fmtInt(g.tools) + " tools</div>" +
        "<div class='bt-dots'>" + dots + "</div>" +
        "<table class='bt-table'><thead><tr><th>Agent</th><th>Tokens</th><th>Tools</th>" +
        "<th title='模型和工具实际占用执行槽的累计时间；排队不计入'>实际执行</th></tr></thead>" +
        "<tbody>" + rows + "</tbody></table>" +
        "<pre class='bt-result hidden'></pre></div>";
    }).join("");

    body.querySelectorAll(".bt-maintenance-btn").forEach((button) => {
      button.addEventListener("click", () => {
        const row = groups.flatMap(group => group.rows)
          .find(item => item.maintenance_id === button.dataset.maintenanceId);
        if (!row || row.conversation_id !== activeConversationId()) return;
        setBgTasksOpen(false);
        void window.MewWorkbench?.openMaintenance({project: row.project,
          conversation_id: row.conversation_id, mode: "fix", id: row.maintenance_id});
      });
    });
    body.querySelectorAll(".bt-result-btn").forEach((button) => {
      button.addEventListener("click", async (event) => {
        event.stopPropagation();
        const box = button.closest(".bt-group").querySelector(".bt-result");
        const taskId = button.dataset.taskId;
        if (!box.classList.contains("hidden") && box.dataset.taskId === taskId) {
          box.classList.add("hidden");
          return;
        }
        box.dataset.taskId = taskId;
        box.textContent = "加载中…";
        box.classList.remove("hidden");
        try {
          const res = await fetch("/api/tasks/" + encodeURIComponent(taskId));
          const task = await res.json();
          box.textContent = res.ok
            ? (task.result || "（任务没有返回文本）")
            : (task.error || "结果加载失败");
        } catch {
          box.textContent = "结果加载失败";
        }
      });
    });
    body.querySelectorAll(".bt-cancel-btn").forEach((button) => {
      button.addEventListener("click", async (event) => {
        event.stopPropagation();
        button.disabled = true;
        try {
          await usageJson(
            "/api/tasks/" + encodeURIComponent(button.dataset.taskId) + "/cancel",
            { method: "POST" }
          );
          await pollBgTasks();
        } catch (error) {
          notify(error.message || String(error), "error");
          button.disabled = false;
        }
      });
    });
    body.querySelectorAll(".bt-stop-group").forEach((button) => {
      button.addEventListener("click", async (event) => {
        event.stopPropagation();
        const group = groups.find((item) => item.key === button.dataset.groupKey);
        const taskIds = (group?.rows || [])
          .filter((row) => ["queued", "running"].includes(row.status)
            && !row.cancellation_pending && row.task_id)
          .map((row) => row.task_id);
        button.disabled = true;
        try {
          const outcomes = await Promise.allSettled(taskIds.map((taskId) =>
            usageJson("/api/tasks/" + encodeURIComponent(taskId) + "/cancel", { method: "POST" })
          ));
          const failures = outcomes.filter((result) => result.status === "rejected");
          if (failures.length) notify(failures.length + " 个子 Agent 未能停止，请刷新状态", "error");
          await pollBgTasks();
        } catch (error) {
          notify(error.message || String(error), "error");
        } finally {
          button.disabled = false;
        }
      });
    });
    syncBgClockTimer();
  }

  async function pollBgTasks() {
    if (document.hidden || btPanel.classList.contains("collapsed")) return;
    const revision = ++btPollRevision;
    const conversationId = activeConversationId();
    const stillCurrent = () => revision === btPollRevision
      && conversationId === activeConversationId()
      && !btPanel.classList.contains("collapsed") && !document.hidden;
    const clearIfConversationSwitched = () => {
      if (revision === btPollRevision && conversationId !== activeConversationId()) {
        invalidateBgPolls(true);
      }
    };
    try {
      const res = await fetch("/api/agent_tasks");
      if (!stillCurrent()) { clearIfConversationSwitched(); return; }
      if (!res || res.ok === false) {
        markBgPollFailure();
        return;
      }
      const data = await res.json();
      if (!stillCurrent()) { clearIfConversationSwitched(); return; }
      if (data && typeof data === "object" && !Array.isArray(data)
          && Object.prototype.hasOwnProperty.call(data, "conversation_id")
          && String(data.conversation_id || "") !== conversationId) {
        invalidateBgPolls(true);
        return;
      }
      if (!validBgTaskPayload(data)) {
        markBgPollFailure();
        return;
      }
      renderBgTasks(data);
      if (Array.isArray(data.tasks)) renderTasks(data.tasks);
    } catch {
      if (stillCurrent()) markBgPollFailure();
      else clearIfConversationSwitched();
    }
  }

  function scheduleBgPoll() {
    clearInterval(btTimer);
    btTimer = null;
    invalidateBgPolls(false);
    const open = !btPanel.classList.contains("collapsed");
    if (!open || document.hidden) return;
    // 子任务可能在模型槽之外工作；槽负载为 0 不代表任务已结束。
    btTimer = setInterval(pollBgTasks, 5000);
    pollBgTasks();
  }

  document.addEventListener("visibilitychange", scheduleBgPoll);
  window.addEventListener("mewcode:conversation-context", (event) => {
    const nextConversationId = String(event.detail?.conversationId || "");
    if (nextConversationId === btCurrentConversationId) return;
    btCurrentConversationId = nextConversationId;
    invalidateBgPolls(true);
    scheduleBgPoll();
  });
  function setBgTasksOpen(open, { restoreFocus = true } = {}) {
    btPanel.classList.toggle("collapsed", !open);
    btPanel.setAttribute("aria-hidden", open ? "false" : "true");
    btBackdrop.classList.toggle("hidden", !open);
    $("bgtasks-btn").setAttribute("aria-expanded", open ? "true" : "false");
    scheduleBgPoll();
    if (restoreFocus) {
      requestAnimationFrame(() => {
        $(open ? "bgtasks-close" : "bgtasks-btn").focus();
      });
    }
  }

  $("bgtasks-btn").addEventListener("click", () => {
    setBgTasksOpen(btPanel.classList.contains("collapsed"));
  });
  $("execution-members-btn")?.addEventListener("click", () => setBgTasksOpen(true));
  $("bgtasks-close").addEventListener("click", () => {
    setBgTasksOpen(false);
  });
  btBackdrop.addEventListener("click", () => {
    setBgTasksOpen(false);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !btPanel.classList.contains("collapsed")) {
      event.preventDefault();
      setBgTasksOpen(false);
    }
  });
  scheduleBgPoll();

  // ---------- Human on-call ----------

  function setHumanOnCallFeedback(text, kind = "error") {
    const feedback = $("human-oncall-feedback");
    if (!feedback) return;
    feedback.textContent = text || "";
    feedback.dataset.kind = text ? kind : "";
    feedback.classList.toggle("hidden", !text);
  }

  function humanOnCallDraftKey(card = currentHumanOnCall) {
    if (!card) return "";
    const requestId = String(card.requestId || "");
    const conversationId = String(card.conversationId || "");
    const version = String(card.version || "");
    const runId = String(card.runId || "");
    return requestId && conversationId
      ? conversationId + "\u001f" + requestId + "\u001f" + version + "\u001f" + runId : "";
  }

  function rememberHumanOnCallDraft(card = currentHumanOnCall) {
    const key = humanOnCallDraftKey(card);
    if (!key) return;
    humanOnCallDrafts.set(key, {
      answer: String(card.answer || ""),
      other: String($("human-oncall-other")?.value || ""),
      note: String($("human-oncall-note")?.value || ""),
    });
  }

  function forgetHumanOnCallDraft(card = currentHumanOnCall) {
    const key = humanOnCallDraftKey(card);
    if (key) humanOnCallDrafts.delete(key);
  }

  function savedHumanOnCallDraft(requestId, conversationId, version = "", runId = "") {
    const key = humanOnCallDraftKey({requestId, conversationId, version, runId});
    return key ? humanOnCallDrafts.get(key) || null : null;
  }

  function askUserDraftKey(card = currentAskUser) {
    if (!card) return "";
    const requestId = String(card.requestId || "");
    const conversationId = String(card.conversationId || "");
    const version = String(card.version || "");
    const runId = String(card.runId || "");
    return requestId && conversationId
      ? conversationId + "\u001f" + requestId + "\u001f" + version + "\u001f" + runId : "";
  }

  function rememberAskUserDraft(card = currentAskUser) {
    const key = askUserDraftKey(card);
    if (!key) return;
    const others = {};
    $("ask-user-questions").querySelectorAll(".ask-user-item").forEach((item) => {
      const name = item.dataset.name;
      const custom = item.querySelector(".ask-user-custom")?.value.trim() || "";
      if (name && custom) others[name] = custom;
    });
    askUserDrafts.set(key, {answers: collectAskUserAnswers(), notes: collectAskUserNotes(), others});
  }

  function forgetAskUserDraft(card = currentAskUser) {
    const key = askUserDraftKey(card);
    if (key) askUserDrafts.delete(key);
  }

  function savedAskUserDraft(requestId, conversationId, version = "", runId = "") {
    const key = askUserDraftKey({requestId, conversationId, version, runId});
    return key ? askUserDrafts.get(key) || null : null;
  }

  function preserveHumanOnCallDraft() {
    rememberHumanOnCallDraft();
  }

  function clearHumanOnCallAckTimer(card = currentHumanOnCall) {
    if (card?.ackTimer) {
      window.clearTimeout(card.ackTimer);
      card.ackTimer = null;
    }
  }

  function resetHumanOnCallControls() {
    const submit = $("human-oncall-submit");
    const cancel = $("human-oncall-cancel");
    submit.disabled = false;
    submit.setAttribute("aria-busy", "false");
    cancel.disabled = false;
    document.querySelectorAll("#human-oncall-options .human-oncall-option").forEach((button) => {
      button.disabled = false;
    });
    $("human-oncall-other").disabled = false;
    if ($("human-oncall-note")) $("human-oncall-note").disabled = false;
  }

  function hideHumanOnCallCard() {
    if (gateCountdownTimer) clearInterval(gateCountdownTimer);
    clearHumanOnCallAckTimer();
    resetHumanOnCallControls();
    forgetHumanOnCallDraft();
    currentHumanOnCall = null;
    humanOnCallPanel.classList.add("hidden");
    if ($("human-oncall-note")) $("human-oncall-note").value = "";
    setHumanOnCallFeedback("");
  }

  function setHumanOnCallSubmitting(submitting) {
    if (!currentHumanOnCall) return;
    const busy = Boolean(submitting);
    currentHumanOnCall.submitting = busy;
    const submit = $("human-oncall-submit");
    const cancel = $("human-oncall-cancel");
    const answer = currentHumanOnCall.answer || "";
    const otherEmpty = (answer === "__other__" || currentHumanOnCall.choice?.requires_input)
      && !$("human-oncall-other").value.trim();
    submit.disabled = busy || !answer || otherEmpty;
    submit.setAttribute("aria-busy", busy ? "true" : "false");
    cancel.disabled = busy;
    document.querySelectorAll("#human-oncall-options .human-oncall-option").forEach((button) => {
      button.disabled = busy;
    });
    $("human-oncall-other").disabled = busy;
    if ($("human-oncall-note")) $("human-oncall-note").disabled = busy;
  }

  function selectHumanOnCallChoice(value, prepared = null) {
    if (!currentHumanOnCall || currentHumanOnCall.submitting) return;
    currentHumanOnCall.answer = value;
    currentHumanOnCall.choice = prepared || null;
    document.querySelectorAll(".human-oncall-option").forEach((button) => {
      button.setAttribute("aria-checked", button.dataset.value === value ? "true" : "false");
    });
    const isOther = value === "__other__";
    const needsInput = isOther || Boolean(prepared?.requires_input);
    $("human-oncall-other-wrap").classList.toggle("hidden", !isOther);
    if (needsInput) {
      $("human-oncall-other-wrap").classList.remove("hidden");
      const label = $("human-oncall-other-wrap")?.querySelector("span");
      if (label) label.textContent = prepared?.input_label || (isOther ? "其他答案" : "选择后补充");
      $("human-oncall-other").placeholder = prepared?.input_label || (isOther ? "输入你的选择…" : "填写必要信息…");
    }
    $("human-oncall-submit").disabled = !value || (needsInput && !$("human-oncall-other").value.trim());
    rememberHumanOnCallDraft();
    if (needsInput) $("human-oncall-other").focus();
  }

  function setHumanOnCallPanelMode(mode) {
    const isAsk = mode === "ask_user";
    $("human-oncall-badge").textContent = isAsk ? "需要你的回答" : "需要你的决定";
    $("human-oncall-options").classList.toggle("hidden", isAsk);
    $("ask-user-questions").classList.toggle("hidden", !isAsk);
    $("human-oncall-other-wrap").classList.toggle(
      "hidden", isAsk || currentHumanOnCall?.answer !== "__other__"
    );
    $("human-oncall-note-wrap")?.classList.toggle(
      "hidden", isAsk || !currentHumanOnCall?.hasOptions
    );
    $("human-oncall-submit").textContent = isAsk ? "提交回答" : "确认选择";
  }

  function interactionChoiceBundle(msg) {
    const bundle = msg?.choice_bundle || msg?.choiceBundle || null;
    if (!bundle || typeof bundle !== "object") return null;
    const key = [msg?.conversation_id || activeConversationId(), msg?.run_id || "", msg?.request_id || "",
      bundle.id || "", bundle.version || ""].join("\u001f");
    if (key) interactionChoiceBundleCache.set(key, bundle);
    return bundle;
  }

  function preparedQuestionChoices(question, bundle) {
    const fromQuestion = Array.isArray(question?.prepared_choices) ? question.prepared_choices : null;
    const fromBundle = bundle?.questions?.find((item) => item?.name === question?.name)?.choices;
    const prepared = (fromQuestion || fromBundle || []).filter((item) => item && (item.id || item.value || item.label));
    return prepared.map((item, index) => ({...item,
      id: String(item.id ?? item.value ?? item.label ?? ("choice-" + (index + 1))),
      label: String(item.label ?? item.value ?? item.id ?? ""),
      description: String(item.description || ""),
      kind: item.kind === "action" ? "action" : "value",
      action: String(item.action || ""),
      requires_input: Boolean(item.requires_input),
      input_label: String(item.input_label || ""),
      evidence: Array.isArray(item.evidence) ? item.evidence.map((ref) => String(ref)) : [],
      verification: item.verification === "verified" ? "verified" : "suggested",
    }));
  }

  function preparedQuestionOptions(question, bundle) {
    const prepared = preparedQuestionChoices(question, bundle);
    const raw = Array.isArray(question?.options) ? question.options : [];
    const options = raw.map((option) => {
      const value = String(option?.value ?? option?.id ?? option?.label ?? option ?? "");
      const meta = prepared.find((choice) => choice.id === value) || null;
      return { ...(typeof option === "object" ? option : {value, label: value}), value,
        label: String(option?.label ?? meta?.label ?? value), description: String(option?.description || meta?.description || ""),
        recommended: Boolean(option?.recommended ?? meta?.recommended), prepared: meta };
    }).filter((option) => option.value);
    const missing = prepared.filter((choice) => !options.some((option) => option.value === choice.id));
    missing.forEach((choice) => options.push({value: choice.id, label: choice.label, description: choice.description,
      recommended: Boolean(choice.recommended), prepared: choice}));
    return {options: options.filter((option) => !(option.prepared?.kind === "action" && option.prepared?.action === "manual")),
      manualChoice: prepared.find((choice) => choice.kind === "action" && choice.action === "manual") || null,
      prepared};
  }

  function appendChoiceEvidence(parent, choice) {
    if (!choice) return;
    const verification = document.createElement("span"); verification.className = "human-oncall-verification";
    verification.textContent = choice.verification === "verified" ? "已核验" : "AI建议"; parent.appendChild(verification);
    if (choice.evidence?.length) {
      const details = document.createElement("details"); details.className = "human-oncall-evidence";
      const summary = document.createElement("summary"); summary.textContent = "展开来源 · " + choice.evidence.length; details.appendChild(summary);
      const list = document.createElement("ul"); choice.evidence.forEach((ref) => { const item = document.createElement("li"); item.textContent = ref; list.appendChild(item); });
      details.appendChild(list); parent.appendChild(details);
    }
    if (Array.isArray(choice.value) && choice.value.length) {
      const details = document.createElement("details"); details.className = "human-oncall-choice-values";
      const summary = document.createElement("summary"); summary.textContent = "查看候选列表 · " + choice.value.length + " 项"; details.appendChild(summary);
      const list = document.createElement("ul"); choice.value.forEach((value) => { const item = document.createElement("li"); item.textContent = String(value); list.appendChild(item); });
      details.appendChild(list); parent.appendChild(details);
    }
  }

  function choiceInputValue(value, question = null) {
    if (question?.type === "checkbox" || question?.input_type === "list") {
      return String(value || "").split(/\r?\n|,/).map((item) => item.trim()).filter(Boolean);
    }
    return String(value || "").trim();
  }

  let gateCountdownTimer = null;
  function showGateCountdown(msg) {
    if (gateCountdownTimer) clearInterval(gateCountdownTimer);
    let element = $("gate-countdown");
    if (!element) {
      element = document.createElement("p"); element.id = "gate-countdown";
      $("human-oncall-question").after(element);
    }
    const gate = msg.gate || {};
    const update = () => {
      if (!gate.kind) { element.textContent = ""; return; }
      const labels = {plan_approval: "计划审批", authorization_extension: "授权扩展", irreversible: "不可逆操作", business_choice: "业务选择", escalation_exhausted: "自动处理机会用完"};
      const seconds = gate.deadline ? Math.max(0, Math.ceil(gate.deadline - Date.now() / 1000)) : null;
      element.textContent = (labels[gate.kind] || gate.kind) + " · " +
        (seconds === null ? "等待你的明确选择" : seconds + " 秒后采用推荐项") + "。" + (gate.timeout_consequence || "");
    };
    update();
    if (gate.deadline) gateCountdownTimer = setInterval(update, 1000);
  }

  function showHumanOnCall(msg) {
    if (typeof workbenchPrompt === "function" && workbenchPrompt(msg)) return;
    showGateCountdown(msg);
    const question = (msg.questions || [])[0];
    if (!question) {
      addErrorMsg("human_oncall 没有提供可显示的问题");
      return;
    }

    const choiceBundle = typeof interactionChoiceBundle === "function"
      ? interactionChoiceBundle(msg) : (msg.choice_bundle || msg.choiceBundle || null);
    const preparedView = typeof preparedQuestionOptions === "function"
      ? preparedQuestionOptions(question, choiceBundle)
      : {options: (question.options || []).map((option) => ({...(typeof option === "object" ? option : {value: String(option), label: String(option)}),
        value: String(option?.value ?? option?.label ?? option ?? ""), prepared: null})), manualChoice: null, prepared: []};
    const appendPreparedEvidence = typeof appendChoiceEvidence === "function" ? appendChoiceEvidence : () => {};
    const requestId = String(msg.request_id || "");
    const conversationId = String(msg.conversation_id || activeConversationId());
    const runId = String(msg.run_id || "");
    const explicitVersion = msg.version ?? msg.request_version ?? msg.prompt_version
      ?? choiceBundle?.version ?? question.version ?? question.request_version;
    const version = explicitVersion != null && explicitVersion !== "" ? String(explicitVersion)
      : JSON.stringify([{name: question.name || "", type: question.type || "",
        title: question.title || "", question: question.question || "", context: question.context || "",
        allow_other: Boolean(question.allowOther ?? question.allow_other), options: (question.options || [])
          .map((option) => ({value: String(option?.value ?? option?.id ?? option?.label ?? option ?? ""),
            label: String(option?.label ?? option?.value ?? option?.id ?? option ?? "")}))}]);
    const existing = currentHumanOnCall
      && String(currentHumanOnCall.requestId || "") === requestId
      && String(currentHumanOnCall.conversationId || "") === conversationId
      && String(currentHumanOnCall.runId || "") === runId
      && String(currentHumanOnCall.version || "") === version
      ? currentHumanOnCall : null;
    // A reconnect may replay the same prompt while its response is still in
    // flight.  Keep the original object and ack timer so a duplicate prompt
    // cannot unlock a second submission.
    if (existing?.submitting) return;
    if (currentHumanOnCall && !existing) rememberHumanOnCallDraft();
    const savedDraft = savedHumanOnCallDraft(requestId, conversationId, version, runId);
    const previousAnswer = existing?.answer ?? savedDraft?.answer ?? "";
    const previousOther = existing ? $("human-oncall-other").value : (savedDraft?.other || "");
    clearHumanOnCallAckTimer(currentHumanOnCall);
    if (currentAskUser) rememberAskUserDraft();
    currentAskUser = null;
    currentHumanOnCall = {
      requestId,
      conversationId,
      runId,
      nodeId: String(msg.node_id || ""),
      caseId: String(msg.case_id || ""),
      message: msg,
      version,
      name: question.name || "choice",
      answer: previousAnswer,
      hasOptions: false,
      choiceBundle,
      choice: null,
      submitting: false,
      submittingCancel: false,
      expired: false,
    };
    resetHumanOnCallControls();
    setHumanOnCallPanelMode("human_oncall");
    setHumanOnCallFeedback("");
    $("human-oncall-question").textContent = question.title || question.question || "请选择下一步";
    const context = question.title ? [question.question, question.context].filter(Boolean).join("\n\n") : (question.context || "");
    $("human-oncall-context").textContent = context;
    $("human-oncall-context").classList.toggle("hidden", !context);
    $("human-oncall-other").value = previousOther;
    if ($("human-oncall-note")) $("human-oncall-note").value = existing ? $("human-oncall-note").value : (savedDraft?.note || "");

    const optionsEl = $("human-oncall-options");
    optionsEl.replaceChildren();
    const options = preparedView.options;
    currentHumanOnCall.hasOptions = options.length > 0 || Boolean(question.allowOther ?? question.allow_other) || Boolean(preparedView.manualChoice);
    for (const option of options) {
      const value = String(option.value ?? option.label ?? "");
      const optionItem = document.createElement("div");
      optionItem.className = "human-oncall-option-item";
      const button = document.createElement("button");
      button.type = "button";
      button.className = "human-oncall-option";
      button.dataset.value = value;
      button.dataset.choiceId = String(option.prepared?.id || value);
      button.dataset.choiceKind = String(option.prepared?.kind || "value");
      button.dataset.choiceAction = String(option.prepared?.action || "");
      button.setAttribute("role", "radio");
      button.setAttribute("aria-checked", "false");

      const label = document.createElement("span");
      label.className = "human-oncall-option-label";
      label.textContent = option.label || value;
      button.appendChild(label);

      if (option.recommended) {
        const recommended = document.createElement("span");
        recommended.className = "human-oncall-recommended";
        recommended.textContent = "推荐";
        button.appendChild(recommended);
      }
      if (option.description) {
        const description = document.createElement("span");
        description.className = "human-oncall-option-desc";
        description.textContent = option.description;
        button.appendChild(description);
      }
      appendPreparedEvidence(optionItem, option.prepared);

      button.addEventListener("click", () => selectHumanOnCallChoice(value, option.prepared));
      optionItem.prepend(button);
      optionsEl.appendChild(optionItem);
    }

    if (question.allowOther ?? question.allow_other ?? preparedView.manualChoice) {
      const otherButton = document.createElement("button");
      otherButton.type = "button";
      otherButton.className = "human-oncall-option";
      otherButton.dataset.value = "__other__";
      otherButton.dataset.choiceId = String(preparedView.manualChoice?.id || "__other__");
      otherButton.dataset.choiceKind = "action";
      otherButton.dataset.choiceAction = "manual";
      otherButton.setAttribute("role", "radio");
      otherButton.setAttribute("aria-checked", "false");
      const otherLabel = document.createElement("span");
      otherLabel.className = "human-oncall-option-label";
      otherLabel.textContent = "其他答案";
      otherButton.appendChild(otherLabel);
      otherButton.addEventListener("click", () => selectHumanOnCallChoice("__other__", preparedView.manualChoice));
      optionsEl.appendChild(otherButton);
    }

    setHumanOnCallPanelMode("human_oncall");
    humanOnCallPanel.classList.remove("hidden");
    $("human-oncall-submit").disabled = true;
    const optionValues = new Set(options.map((option) => String(option.value ?? option.label ?? "")));
    const canRestore = previousAnswer === "__other__"
      ? Boolean(question.allowOther ?? question.allow_other ?? preparedView.manualChoice)
      : optionValues.has(previousAnswer);
    if (canRestore) {
      selectHumanOnCallChoice(previousAnswer, options.find((option) => String(option.value) === previousAnswer)?.prepared || null);
    }
  }

  const preparedPromptActions = new Set(["research", "revise", "configure", "retry_prepare"]);
  function preparedPromptInstruction(action) {
    return ({
      research: "明确要求研究或补充当前问题的材料，再准备新的候选方案。",
      revise: "明确要求修改当前问题的约束，再准备新的候选方案。",
      configure: "明确要求补充处理所需的配置说明，再准备新的候选方案。",
      retry_prepare: "明确要求重新查找并准备当前问题的候选方案。",
    })[action] || "明确要求重新准备当前问题的候选方案。";
  }
  async function preparePromptChoices(card, action, actionInput, mode) {
    if (!card || card.preparing) return;
    const previous = card.message || {};
    card.preparing = true;
    if (mode === "human") setHumanOnCallSubmitting(true);
    else {
      setHumanOnCallFeedback("正在准备新的可选项…", "info");
      $("human-oncall-submit").disabled = true;
      document.querySelectorAll("#ask-user-questions .human-oncall-option").forEach((button) => { button.disabled = true; });
    }
    try {
      const response = await fetch("/api/orchestration/interaction-choices/prepare", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          conversation_id: card.conversationId,
          run_id: card.runId || "",
          node_id: card.nodeId || "",
          request_id: card.requestId,
          case_id: card.caseId || "",
          refresh: true,
          instruction: preparedPromptInstruction(action),
          action_input: Array.isArray(actionInput) ? actionInput.join("\n") : String(actionInput || ""),
        }),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.error || "准备选项失败");
      const next = {
        ...previous,
        ...result,
        conversation_id: result.conversation_id || card.conversationId,
        run_id: result.run_id || card.runId || "",
        node_id: result.node_id || card.nodeId || "",
        request_id: result.request_id || card.requestId,
        case_id: result.case_id || card.caseId || "",
        questions: Array.isArray(result.questions) ? result.questions : (previous.questions || []),
        choice_bundle: result.choice_bundle || result.bundle || null,
      };
      card.preparing = false;
      card.submitting = false;
      if (mode === "human") showHumanOnCall(next);
      else showAskUser(next);
    } catch (error) {
      card.preparing = false;
      if (mode === "human") {
        setHumanOnCallSubmitting(false);
        setHumanOnCallFeedback(error.message || String(error), "error");
      } else {
        document.querySelectorAll("#ask-user-questions .human-oncall-option").forEach((button) => { button.disabled = false; });
        updateAskUserSubmitState();
        setHumanOnCallFeedback(error.message || String(error), "error");
      }
    }
  }
  function answerHumanOnCall(cancelled) {
    if (!currentHumanOnCall || currentHumanOnCall.submitting) return;
    if (cancelled && currentHumanOnCall.expired) {
      // An expired card is local UI state only.  Closing it cannot approve or
      // resume anything, and avoids sending a response to a new window.
      hideHumanOnCallCard();
      return;
    }
    let answers = null;
    if (!cancelled) {
      const selectedAnswer = currentHumanOnCall.answer;
      const customAnswer = (selectedAnswer === "__other__" || currentHumanOnCall.choice?.requires_input)
        ? $("human-oncall-other").value.trim() : "";
      const preparedAction = currentHumanOnCall.choice?.kind === "action"
        ? String(currentHumanOnCall.choice.action || "") : "";
      if (preparedAction === "defer") {
        answerHumanOnCall(true);
        return;
      }
      if (preparedAction && typeof preparedPromptActions !== "undefined"
          && preparedPromptActions.has(preparedAction)) {
        if (currentHumanOnCall.choice?.requires_input && !customAnswer) {
          setHumanOnCallFeedback("请先填写此处理动作所需的补充说明。", "error");
          return;
        }
        void preparePromptChoices(currentHumanOnCall, preparedAction,
          currentHumanOnCall.choice?.requires_input ? choiceInputValue(customAnswer) : "", "human");
        return;
      }
      // Other is a user value, even when the bundle represents it with a
      // manual action choice. Never submit that action id as the answer.
      const answer = selectedAnswer === "__other__" ? customAnswer : selectedAnswer;
      if (!answer || (selectedAnswer === "__other__" && !customAnswer)) {
        setHumanOnCallFeedback("请先选择一个选项；其他答案不能为空。", "error");
        return;
      }
      answers = { [currentHumanOnCall.name]: currentHumanOnCall.choiceBundle && selectedAnswer === "__other__"
        ? {custom: answer} : answer };
    }
    currentHumanOnCall.submittingCancel = Boolean(cancelled);
    setHumanOnCallSubmitting(true);
    const response = {
      type: "human_oncall_response",
      request_id: currentHumanOnCall.requestId,
      conversation_id: currentHumanOnCall.conversationId,
      answers,
      cancelled,
    };
    if (!cancelled && currentHumanOnCall.choiceBundle?.id && currentHumanOnCall.choiceBundle?.version) {
      response.choice_bundle_id = String(currentHumanOnCall.choiceBundle.id);
      response.choice_version = String(currentHumanOnCall.choiceBundle.version);
      const input = currentHumanOnCall.answer !== "__other__"
        && currentHumanOnCall.choice?.requires_input ? $("human-oncall-other").value.trim() :
        currentHumanOnCall.answer === "__other__" ? $("human-oncall-other").value.trim() : "";
      if (input) response.choice_inputs = {[currentHumanOnCall.name]: choiceInputValue(input)};
    }
    const note = !cancelled && currentHumanOnCall.hasOptions ? $("human-oncall-note")?.value.trim() : "";
    if (note) response.notes = {[currentHumanOnCall.name]: note};
    const sent = send(response);
    if (!sent) {
      const canCloseExpired = cancelled && currentHumanOnCall.expired;
      clearHumanOnCallAckTimer();
      currentHumanOnCall.submittingCancel = false;
      setHumanOnCallSubmitting(false);
      if (canCloseExpired) {
        hideHumanOnCallCard();
      } else {
        setHumanOnCallFeedback(
          cancelled
            ? "连接已断开，取消未提交；卡片仍保留，重连后可以重试。"
            : "连接已断开，选择未提交；你的选择已保留，请重连后重试。",
          "error",
        );
      }
      return;
    }
    setHumanOnCallFeedback(cancelled ? "正在取消…" : "正在提交…", "info");
    const submittedCard = currentHumanOnCall;
    submittedCard.ackTimer = window.setTimeout(() => {
      if (currentHumanOnCall !== submittedCard || !submittedCard.submitting) return;
      submittedCard.submittingCancel = false;
      setHumanOnCallSubmitting(false);
      setHumanOnCallFeedback(
        "未收到确认回执，请检查连接或重启新版服务；选择已保留。",
        "error",
      );
    }, 12000);
  }

  function handleHumanOnCallResponseResult(msg) {
    const active = currentHumanOnCall;
    if (!active) return;
    if (String(msg.request_id || "") !== String(active.requestId || "")) return;
    if (String(msg.conversation_id || "") !== String(active.conversationId || "")) return;
    clearHumanOnCallAckTimer(active);
    if (msg.ok) {
      hideHumanOnCallCard();
      return;
    }
    const wasCancel = Boolean(active.submittingCancel);
    active.submittingCancel = false;
    active.expired = ["no_live_request", "stale_request"].includes(String(msg.error_type || ""));
    setHumanOnCallSubmitting(false);
    const error = String(msg.error || msg.text || "人工选择未提交，请检查连接后重试。");
    if (wasCancel && active.expired) {
      // A cancelled stale card is safe to dismiss, but this error never counts
      // as approval and never starts a task.
      hideHumanOnCallCard();
      addSystemMsg(error);
      return;
    }
    setHumanOnCallFeedback(error, "error");
  }

  function markHumanOnCallDisconnected() {
    if (!currentHumanOnCall) return;
    preserveHumanOnCallDraft();
    currentHumanOnCall.expired = true;
    currentHumanOnCall.submittingCancel = false;
    setHumanOnCallSubmitting(false);
    setHumanOnCallFeedback("连接已断开，选择已保留；重连后可以重新提交。", "error");
  }

  function askUserOptionLabel(option) {
    if (option && typeof option === "object") {
      return String(option.label || option.value || option.id || option.name || "");
    }
    return String(option ?? "");
  }

  function askUserOptionValue(option) {
    if (option && typeof option === "object") {
      return String(option.value || option.id || option.label || option.name || "");
    }
    return String(option ?? "");
  }

  function askUserPresentation(question) {
    let message = String(question.message || question.question || question.name || "");
    let context = String(question.context || ""), details = String(question.details || "");
    // Old durable prompts still carry diagnostics in the question itself.
    // Separate their display without changing the saved request or answer IDs.
    const marker = message.indexOf("已尝试：");
    if (marker >= 0 && !details) {
      details = message.slice(marker).trim(); message = message.slice(0, marker).trim();
    }
    const node = message.match(/^节点「([^」]+)」需要具体决定：[\s\S]*/);
    if (node) {
      if (!context) context = "节点：" + node[1];
      message = message.slice(message.indexOf("需要具体决定：") + "需要具体决定：".length).trim();
    }
    if (question.description && !context.includes(question.description)) {
      context = [context, String(question.description)].filter(Boolean).join("\n");
    }
    return {title: String(question.title || ""), message, context, details};
  }

  function updateAskUserSubmitState() {
    const answers = collectAskUserAnswers();
    const missingOther = [...$("ask-user-questions").querySelectorAll('.ask-user-item')].some((item) =>
      item.querySelector('[data-other="true"][aria-checked="true"]')
      && !item.querySelector('.ask-user-custom')?.value.trim());
    const missingRequiredInput = [...$("ask-user-questions").querySelectorAll('.ask-user-item')].some((item) => {
      const selected = item.querySelector('.human-oncall-option[aria-checked="true"]');
      return selected?.dataset.requiresInput === "true" && !item.querySelector('.ask-user-custom')?.value.trim();
    });
    $("human-oncall-submit").disabled = !currentAskUser
      || currentAskUser.questions.some((q, i) => !answers[q.name || ("q" + i)])
      || missingOther || missingRequiredInput;
  }

  function promptVersion(msg, question = null) {
    const explicit = msg?.version ?? msg?.request_version ?? msg?.prompt_version
      ?? msg?.choice_bundle?.version ?? msg?.choiceBundle?.version
      ?? question?.version ?? question?.request_version;
    if (explicit != null && explicit !== "") return String(explicit);
    const questions = Array.isArray(msg?.questions) ? msg.questions : (question ? [question] : []);
    return JSON.stringify(questions.map((item) => ({name: item?.name || "", type: item?.type || "",
      title: item?.title || "", message: item?.message || item?.question || "", context: item?.context || "",
      allow_other: Boolean(item?.allow_other), options: (Array.isArray(item?.options) ? item.options : [])
        .map((option) => ({value: askUserOptionValue(option), label: askUserOptionLabel(option)}))})));
  }

  function showAskUser(msg) {
    if (typeof workbenchPrompt === "function" && workbenchPrompt(msg)) return;
    showGateCountdown(msg);
    const questions = msg.questions || [];
    if (!questions.length) {
      addErrorMsg("AskUserQuestion 没有提供可显示的问题");
      return;
    }
    // The isolated renderer tests load this function without the whole app
    // helper set. Keep the legacy projection available in that small shell;
    // the full desktop bundle uses the richer prepared-choice helpers.
    const choiceBundle = typeof interactionChoiceBundle === "function"
      ? interactionChoiceBundle(msg) : (msg.choice_bundle || msg.choiceBundle || null);
    const projectPrepared = typeof preparedQuestionOptions === "function"
      ? preparedQuestionOptions : (question) => ({
        options: (Array.isArray(question?.options) ? question.options : []).map((option) => {
          const value = askUserOptionValue(option);
          return {...(typeof option === "object" ? option : {value, label: value}), value, prepared: null};
        }), manualChoice: null, prepared: [],
      });
    const appendPreparedEvidence = typeof appendChoiceEvidence === "function" ? appendChoiceEvidence : () => {};
    if (currentAskUser) rememberAskUserDraft();
    rememberHumanOnCallDraft();
    const requestId = String(msg.request_id || "");
    const conversationId = String(msg.conversation_id || activeConversationId());
    const runId = String(msg.run_id || "");
    const version = promptVersion(msg, questions[0]);
    const savedDraft = savedAskUserDraft(requestId, conversationId, version, runId);
    currentHumanOnCall = null;
    currentAskUser = {
      requestId,
      conversationId,
      runId,
      nodeId: String(msg.node_id || ""),
      caseId: String(msg.case_id || ""),
      message: msg,
      version,
      questions,
      choiceBundle,
    };
    resetHumanOnCallControls();
    setHumanOnCallPanelMode("ask_user");
    setHumanOnCallFeedback("");
    const single = questions.length === 1 ? askUserPresentation(questions[0]) : null;
    $("human-oncall-question").textContent = single?.title
      || (questions.length > 1 ? "请回答以下问题" : "需要你确认");
    $("human-oncall-context").textContent = "";
    $("human-oncall-context").classList.add("hidden");
    $("human-oncall-submit").disabled = false;

    const list = $("ask-user-questions");
    list.replaceChildren();
    questions.forEach((question, index) => {
      const name = question.name || ("q" + index);
      const preparedView = projectPrepared(question, choiceBundle);
      const options = preparedView.options;
      const type = question.input_mode === "text" ? "text"
        : options.length ? (question.type === "checkbox" ? "checkbox" : "radio") : "text";
      const presentation = askUserPresentation(question);
      const item = document.createElement("div");
      item.className = "ask-user-item";
      item.dataset.name = name;
      item.dataset.type = type;

      const label = document.createElement("div");
      label.className = "ask-user-item-label";
      label.textContent = presentation.message;
      if (presentation.context) {
        const context = document.createElement("p"); context.className = "ask-user-context";
        context.textContent = presentation.context; item.appendChild(context);
      }
      item.appendChild(label);

      if (type === "text") {
        const input = document.createElement("input");
        input.type = "text";
        input.className = "ask-user-text";
        input.maxLength = 500;
        input.autocomplete = "off";
        input.placeholder = "输入回答…";
        input.dataset.askName = name;
        input.setAttribute("aria-label", presentation.message);
        input.addEventListener("input", () => { updateAskUserSubmitState(); rememberAskUserDraft(); });
        item.appendChild(input);
      } else {
        const group = document.createElement("div");
        group.className = "human-oncall-options";
        group.setAttribute("role", type === "checkbox" ? "group" : "radiogroup");
        options.forEach((option) => {
          const value = askUserOptionValue(option);
          const optionItem = document.createElement("div");
          optionItem.className = "human-oncall-option-item";
          const button = document.createElement("button");
          button.type = "button";
          button.className = "human-oncall-option";
          button.dataset.value = value;
          button.dataset.choiceId = String(option.prepared?.id || value);
          button.dataset.choiceKind = String(option.prepared?.kind || "value");
          button.dataset.choiceAction = String(option.prepared?.action || "");
          button.dataset.requiresInput = option.prepared?.requires_input ? 'true' : 'false';
          button.setAttribute("role", type === "checkbox" ? "checkbox" : "radio");
          button.setAttribute("aria-checked", "false");
          const optionLabel = document.createElement("span");
          optionLabel.className = "human-oncall-option-label";
          optionLabel.textContent = askUserOptionLabel(option) || value;
          button.appendChild(optionLabel);
          if (option && typeof option === "object" && option.recommended) {
            const badge = document.createElement("span"); badge.className = "human-oncall-recommended";
            badge.textContent = "建议方案"; button.appendChild(badge);
          }
          if (option && typeof option === "object" && option.description) {
            const description = document.createElement("span"); description.className = "human-oncall-option-desc";
            description.textContent = option.description; button.appendChild(description);
          }
          appendPreparedEvidence(optionItem, option.prepared);
          button.addEventListener("click", () => {
            if (type === "checkbox") {
              const checked = button.getAttribute("aria-checked") !== "true";
              button.setAttribute("aria-checked", checked ? "true" : "false");
            } else {
              group.querySelectorAll(".human-oncall-option").forEach((el) => {
                el.setAttribute("aria-checked", el === button ? "true" : "false");
              });
            }
            const custom = item.querySelector('.ask-user-custom');
            const selected = group.querySelector('.human-oncall-option[aria-checked="true"]');
            const needsInput = selected?.dataset.other === 'true' || selected?.dataset.requiresInput === 'true';
            if (custom) custom.classList.toggle('hidden', !needsInput);
            updateAskUserSubmitState(); rememberAskUserDraft();
          });
          optionItem.prepend(button); group.appendChild(optionItem);
        });
        if ((question.allow_other ?? question.allowOther ?? Boolean(preparedView.manualChoice))
            || preparedView.prepared.some((choice) => choice.requires_input)) {
          const optionItem = document.createElement("div");
          optionItem.className = "human-oncall-option-item";
          const other = document.createElement('button'); other.type = 'button';
          other.className = 'human-oncall-option'; other.dataset.other = 'true';
          other.dataset.choiceId = preparedView.manualChoice?.id || '__other__';
          other.dataset.choiceKind = 'action'; other.dataset.choiceAction = 'manual';
          other.dataset.requiresInput = 'true';
          other.setAttribute('role', type === 'checkbox' ? 'checkbox' : 'radio');
          other.setAttribute('aria-checked', 'false'); other.textContent = '补充其他方案';
          const input = document.createElement('textarea'); input.className = 'ask-user-text ask-user-custom hidden';
          input.rows = 2; input.placeholder = '说明你的方案…'; input.setAttribute('aria-label', '其他方案');
          input.addEventListener('input', () => { updateAskUserSubmitState(); rememberAskUserDraft(); });
          other.addEventListener('click', () => {
            const checked = type !== 'checkbox' || other.getAttribute('aria-checked') !== 'true';
            if (type !== 'checkbox') group.querySelectorAll('.human-oncall-option').forEach(el => el.setAttribute('aria-checked', 'false'));
            other.setAttribute('aria-checked', String(checked)); input.classList.toggle('hidden', !checked);
            if (checked) input.focus(); updateAskUserSubmitState(); rememberAskUserDraft();
          });
          optionItem.append(other); group.appendChild(optionItem); item.appendChild(input);
        }
        item.appendChild(group);
        const custom = item.querySelector('.ask-user-custom');
        if (custom) item.appendChild(custom);
        if (options.length) {
          const noteLabel = document.createElement('label'); noteLabel.className = 'ask-user-note';
          noteLabel.textContent = '补充说明（可选）';
          const note = document.createElement('textarea'); note.className = 'ask-user-note-input';
          note.rows = 2; note.maxLength = 2000; note.placeholder = '可选：补充背景或核验线索。';
          note.dataset.askNote = name; note.setAttribute('aria-label', '补充说明（可选）');
          note.addEventListener('input', () => rememberAskUserDraft());
          const notes = document.createElement('details'); notes.className = 'ask-user-note-details';
          const summary = document.createElement('summary'); summary.textContent = '补充说明（可选）';
          noteLabel.appendChild(note); notes.append(summary, noteLabel); item.appendChild(notes);
        }
      }
      const savedAnswer = savedDraft?.answers?.[name] || '';
      if (type === 'text') {
        const textInput = item.querySelector('.ask-user-text');
        if (textInput) textInput.value = savedAnswer;
      } else if (savedAnswer) {
        const selectedValues = type === 'checkbox' ? savedAnswer.split(',').map((value) => value.trim()).filter(Boolean) : [savedAnswer];
        const known = new Set([...item.querySelectorAll('.human-oncall-option:not([data-other="true"])')].map((button) => button.dataset.value));
        const custom = item.querySelector('.ask-user-custom');
        const other = item.querySelector('[data-other="true"]');
        const customAnswer = selectedValues.filter((value) => !known.has(value));
        item.querySelectorAll('.human-oncall-option:not([data-other="true"])').forEach((button) => {
          button.setAttribute('aria-checked', selectedValues.includes(button.dataset.value) ? 'true' : 'false');
        });
        const selectedRequiresInput = [...item.querySelectorAll('.human-oncall-option:not([data-other="true"])')]
          .some((button) => selectedValues.includes(button.dataset.value) && button.dataset.requiresInput === 'true');
        if (selectedRequiresInput && custom) {
          custom.classList.remove('hidden'); custom.value = savedDraft?.others?.[name] || '';
        }
        if (customAnswer.length && other && custom) {
          other.setAttribute('aria-checked', 'true'); custom.classList.remove('hidden');
          custom.value = savedDraft?.others?.[name] || customAnswer.join(type === 'checkbox' ? ', ' : '');
        } else if (custom && savedDraft?.others?.[name]) {
          custom.value = savedDraft.others[name];
        }
      }
      const savedNote = savedDraft?.notes?.[name] || '';
      const noteInput = item.querySelector('.ask-user-note-input');
      if (noteInput) noteInput.value = savedNote;
      if (savedNote) item.querySelector('.ask-user-note-details')?.setAttribute('open', '');
      if (presentation.details) {
        const details = document.createElement('details'); details.className = 'ask-user-details';
        const summary = document.createElement('summary'); summary.textContent = '查看诊断与已尝试方法';
        const body = document.createElement('p'); body.textContent = presentation.details;
        details.append(summary, body); item.appendChild(details);
      }
      list.appendChild(item);
    });
    humanOnCallPanel.classList.remove("hidden");
    updateAskUserSubmitState();
  }

  function collectAskUserAnswers() {
    const answers = {};
    $("ask-user-questions").querySelectorAll(".ask-user-item").forEach((item) => {
      const name = item.dataset.name;
      const type = item.dataset.type;
      if (!name) return;
      if (type === "text") {
        const input = item.querySelector(".ask-user-text");
        answers[name] = (input && input.value.trim()) || "";
        return;
      }
      const selected = [...item.querySelectorAll('.human-oncall-option[aria-checked="true"]')]
        .map((button) => button.dataset.other === 'true'
          ? (item.querySelector('.ask-user-custom')?.value.trim() || '')
          : (button.dataset.value || ""))
        .filter(Boolean);
      answers[name] = type === "checkbox" ? selected.join(", ") : (selected[0] || "");
    });
    return answers;
  }

  function collectAskUserNotes() {
    const notes = {};
    $("ask-user-questions").querySelectorAll(".ask-user-item").forEach((item) => {
      const name = item.dataset.name;
      const note = item.querySelector(".ask-user-note-input")?.value.trim() || "";
      if (name && note && item.dataset.type !== "text") notes[name] = note;
    });
    return notes;
  }

  function collectAskUserChoiceInputs() {
    const inputs = {};
    $("ask-user-questions").querySelectorAll(".ask-user-item").forEach((item) => {
      const name = item.dataset.name;
      const selected = item.querySelector('.human-oncall-option[aria-checked="true"]');
      if (!name || selected?.dataset.requiresInput !== "true") return;
      const value = choiceInputValue(item.querySelector('.ask-user-custom')?.value || "", {input_type: item.dataset.type});
      if ((Array.isArray(value) && value.length) || (!Array.isArray(value) && value)) inputs[name] = value;
    });
    return inputs;
  }

  function selectedAskUserPreparedAction() {
    const selected = [...document.querySelectorAll(
      "#ask-user-questions .human-oncall-option[aria-checked=\"true\"]"
    )];
    for (const button of selected) {
      const action = String(button.dataset.choiceAction || "");
      if (!action || action === "manual") continue;
      const item = button.closest(".ask-user-item");
      const input = item?.querySelector(".ask-user-custom");
      const value = input ? choiceInputValue(input.value || "", {input_type: item?.dataset.type}) : "";
      return {action, requiresInput: button.dataset.requiresInput === "true", value};
    }
    return null;
  }

  function answerAskUser(cancelled) {
    if (!currentAskUser) return;
    updateAskUserSubmitState();
    if (!cancelled && $("human-oncall-submit").disabled) return;
    if (!cancelled) {
      const preparedAction = typeof selectedAskUserPreparedAction === "function"
        ? selectedAskUserPreparedAction() : null;
      if (preparedAction?.action === "defer") {
        answerAskUser(true);
        return;
      }
      if (preparedAction && typeof preparedPromptActions !== "undefined"
          && preparedPromptActions.has(preparedAction.action)) {
        const hasInput = Array.isArray(preparedAction.value)
          ? preparedAction.value.length > 0 : Boolean(preparedAction.value);
        if (preparedAction.requiresInput && !hasInput) {
          setHumanOnCallFeedback("请先填写此处理动作所需的补充说明。", "error");
          return;
        }
        void preparePromptChoices(currentAskUser, preparedAction.action, preparedAction.value, "ask");
        return;
      }
    }
    const askAnswers = cancelled ? {} : collectAskUserAnswers();
    if (!cancelled && currentAskUser.choiceBundle) {
      $("ask-user-questions").querySelectorAll(".ask-user-item").forEach((item) => {
        const name = item.dataset.name;
        const question = currentAskUser.questions.find(question => question.name === name);
        if (name && item.dataset.type === 'text' && question?.input_mode === 'text') {
          const value = choiceInputValue(item.querySelector('.ask-user-text')?.value || '', question);
          if (Array.isArray(value) ? value.length : value) askAnswers[name] = {custom: value};
          return;
        }
        const other = item.querySelector('.human-oncall-option[aria-checked="true"][data-other="true"]');
        if (!name || !other) return;
        const value = choiceInputValue(item.querySelector(".ask-user-custom")?.value || "",
          {input_type: item.dataset.type});
        const valid = Array.isArray(value) ? value.length > 0 : Boolean(value);
        if (valid) askAnswers[name] = {custom: value};
      });
    }
    const response = {
      type: "ask_user_response",
      request_id: currentAskUser.requestId,
      conversation_id: currentAskUser.conversationId,
      answers: askAnswers,
      cancelled: Boolean(cancelled),
    };
    if (!cancelled) {
      const notes = collectAskUserNotes();
      if (Object.keys(notes).length) response.notes = notes;
      const choiceInputs = typeof collectAskUserChoiceInputs === "function" ? collectAskUserChoiceInputs() : {};
      if (currentAskUser.choiceBundle?.id && currentAskUser.choiceBundle?.version) {
        response.choice_bundle_id = String(currentAskUser.choiceBundle.id);
        response.choice_version = String(currentAskUser.choiceBundle.version);
        if (Object.keys(choiceInputs).length) response.choice_inputs = choiceInputs;
      }
    }
    const submitted = send(response);
    if (submitted === false) {
      rememberAskUserDraft();
      setHumanOnCallFeedback(
        cancelled ? "连接已断开，取消未提交；回答仍保留。" : "连接已断开，回答未提交；选择和备注已保留。",
        "error",
      );
      return;
    }
    const submittedCard = currentAskUser;
    forgetAskUserDraft(submittedCard);
    currentAskUser = null;
    humanOnCallPanel.classList.add("hidden");
  }

  // ---------- WebSocket ----------

  function formatPermissionText(msg) {
    return [msg && msg.description, msg && msg.reason].filter(Boolean).join(" · ");
  }

  function showPermissionMessage(msg, conversationId) {
    if (typeof workbenchPrompt === "function" && workbenchPrompt(msg)) return;
    currentPermissionConversationId = conversationId;
    currentPermissionRequestId = msg.request_id || "";
    $("perm-tool").textContent = msg.tool;
    const directoryScope = String(msg.directory_scope || "");
    const directoryTtl = Number(msg.directory_ttl_seconds || 0);
    $("perm-desc").textContent = formatPermissionText(msg) + (directoryScope
      ? ` · 整个目录授权范围：${directoryScope} 及其子目录，不含同级目录；有效期 ${Math.round(directoryTtl / 86400)} 天。`
      : "");
    // Shell effects depend on changing scripts and state; a one-time decision
    // must not become a reusable command-prefix or same-argument permission.
    $("perm-always").hidden = msg.source === "review_missing_authorization"
      || ["Bash", "RunVerification", "TerminalStart", "TerminalInput"].includes(msg.tool);
    $("perm-directory").hidden = !directoryScope || directoryTtl <= 0;
    permBar.classList.remove("hidden");
  }

  function replayPendingConversationPrompt() {
    const id = activeConversationId();
    const msg = pendingConversationPrompts.get(id);
    if (!msg) return;
    pendingConversationPrompts.delete(id);
    if (msg.type === "permission") {
      showPermissionMessage(msg, id);
    } else if (msg.type === "human_oncall") {
      showHumanOnCall(msg);
    } else if (msg.type === "ask_user") {
      showAskUser(msg);
    }
  }

  async function refreshState(expectedNavigationToken = null) {
    try {
      const res = await fetch("/api/state");
      const data = await res.json();
      if (
        expectedNavigationToken !== null &&
        expectedNavigationToken !== conversationNavigationToken
      ) return;
      updateState(data);
      window.MewWorkbench?.store.refresh({tasks:true});
      restoreTasksCache();
    } catch { /* ignore */ }
  }

  function scheduleReconnect(immediate = false) {
    if (ws && [WebSocket.CONNECTING, WebSocket.OPEN].includes(ws.readyState)) return;
    if (reconnectTimer) {
      if (!immediate) return;
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    const delay = immediate ? 0 : Math.min(1000 * (2 ** reconnectAttempt), 15000);
    reconnectAttempt += 1;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connect();
    }, delay);
    const seconds = Math.max(1, Math.ceil(delay / 1000));
    $("conn-status-text").textContent = immediate
      ? "连接已断开，正在重连…"
      : "连接已断开，" + seconds + " 秒后自动重连…";
  }

  function connect() {
    if (ws && [WebSocket.CONNECTING, WebSocket.OPEN].includes(ws.readyState)) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const recovering = reconnectAttempt > 0;
    const socket = new WebSocket(proto + "://" + location.host + "/ws");
    ws = socket;
    sendBtn.disabled = true;

    socket.onopen = () => {
      if (ws !== socket) return;
      reconnectAttempt = 0;
      reconnectNeedsReplay = recovering;
      $("conn-status").classList.add("hidden");
      syncActiveStreaming();
    };

    socket.onclose = () => {
      if (ws !== socket) return;
      ws = null;
      $("conn-status").classList.remove("hidden");
      syncActiveStreaming();
      markHumanOnCallDisconnected();
      if (currentAskUser) rememberAskUserDraft();
      currentAskUser = null;
      if (!currentHumanOnCall) humanOnCallPanel.classList.add("hidden");
      scheduleReconnect();
    };

    socket.onerror = () => {
      if (socket.readyState !== WebSocket.CLOSED) socket.close();
    };

    socket.onmessage = (ev) => {
      if (ws !== socket) return;
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch {
        return;
      }
      if (msg.type === "usage_browser_command") {
        (async () => {
          let result = null;
          let error = "";
          try {
            if (!window.mewcodeDesktop) throw new Error("用量浏览器桥仅在 Electron 桌面端可用");
            result = await window.mewcodeDesktop.runUsageBrowserCommand(msg);
          } catch (failure) {
            error = failure && failure.message || String(failure);
          }
          try {
            await usageJson("/api/usage/browser/result", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ command_id: msg.command_id, result, error }),
            });
          } catch (failure) {
            notify("浏览器采集结果回传失败：" + (failure.message || String(failure)), "error");
          }
        })();
        return;
      }
      if (msg.type === "usage_run.updated") {
        const terminal = ["succeeded", "partial", "failed", "cancelled"].includes(msg.run && msg.run.status);
        if (terminal && !usageOverlay.classList.contains("hidden")) {
          clearTimeout(usageTerminalRefreshTimer);
          usageTerminalRefreshTimer = setTimeout(loadUsage, 150);
        }
        return;
      }
      if (msg.type === "runtime_load.updated") {
        currentRuntimeLoad = msg.runtime_load || currentRuntimeLoad;
        const badge = $("bgtasks-badge");
        const active = Number(currentRuntimeLoad.running || 0) + Number(currentRuntimeLoad.queued || 0);
        badge.textContent = active;
        badge.classList.toggle("hidden", active <= 0);
        if (!document.hidden && !btPanel.classList.contains("collapsed")) pollBgTasks();
        return;
      }
      if (msg.type === "pairing_request") {
        showPairingRequest(msg);
        notify("收到配对请求", "warning");
        return;
      }
      if (msg.type === "remote_conversation_focus") {
        const focusedId = String(msg.conversation_id || "");
        const scope = msg.scope === "user" || msg.scope === "normal" ? "normal" : "project";
        if (focusedId && focusedId !== activeConversationId() && msg.mode !== "view") {
          openConversation(scope, focusedId).catch(() => {});
        }
        if (msg.mode === "send") notify("手机正在这条对话里发言", "info");
        else if (msg.mode === "create") notify("手机新建了一条对话", "info");
        loadSidebar();
        return;
      }
      const scopedId = String(msg.conversation_id || "");
      if (['expert_run_state', 'expert_run_done', 'expert_cancelled', 'error'].includes(msg.type)) {
        window.dispatchEvent(new CustomEvent('mewcode:workbench-host-event', {detail: msg}));
      }
      if (msg.type === "turn_started") rememberBackendTurnScope(msg, scopedId || activeConversationId());
      if (msg.type === "chat_queue") {
        if (msg.continuing && scopedId) runningConversations.add(scopedId);
        window.MewChatQueue?.sync?.(msg);
        syncActiveStreaming();
        if (scopedId && scopedId !== activeConversationId()) loadSidebar();
        return;
      }
      if (msg.type === "done" && scopedId) conversationEventBuffers.delete(scopedId);
      else rememberConversationEvent(msg);
      if (msg.type === "pipeline_item") applyPipelineEvent(msg);
      if (expertTrajectoryTypes.has(msg.type)) applyExpertTrajectoryEvent(msg);
      if (scopedId && scopedId !== activeConversationId()) {
        if (msg.type === "turn_started") {
          clearPendingConversationStart(scopedId);
          runningConversations.add(scopedId);
          loadSidebar();
        } else if (msg.type === "done") {
          if (currentMsg) {
            const md = currentMsg.querySelector(".md");
            const plain = md ? (md.innerText || md.textContent || "") : "";
            attachAssistantCopyButton(currentMsg, plain);
          }
          releaseConversationActivity(scopedId);
          emitConversationUpdated(scopedId);
          pendingConversationPrompts.delete(scopedId);
          loadSidebar();
          notify(msg.error ? "后台对话执行失败" : "后台对话已完成", msg.error ? "error" : "success");
        } else if (["permission", "human_oncall", "ask_user", "plan_revision"].includes(msg.type)) {
          pendingConversationPrompts.set(scopedId, msg);
          notify("有一个后台对话正在等待你的确认", "warning");
          loadSidebar();
        } else if (msg.type === "error") {
          if (msg.error_code === "recovery_resume_blocked") {
            // A blocked resume belongs to its source task.  Do not append an
            // inactive conversation's task error to the active chat.
            notify("后台对话需要处理恢复事项", "warning");
          } else {
            notify("后台对话执行失败：" + backendErrorText(msg), "error");
          }
        } else if (msg.type === "command_result") {
          notify(msg.message || (msg.ok ? "后台对话命令已执行" : "后台对话命令失败"), msg.ok ? "success" : "error");
          loadSidebar();
        }
        return;
      }
      switch (msg.type) {
        case "workbench_changed":
          window.dispatchEvent(new CustomEvent("mewcode:workbench-changed", { detail: msg }));
          break;
        case "external_agent_update": {
          loadSidebar();
          break;
        }
        case "state":
          updateState(msg);
          window.dispatchEvent(new CustomEvent("mewcode:workbench-state"));

          if (reconnectNeedsReplay && activeConversationId()) {
            reconnectNeedsReplay = false;
            reconnectReplayPending = true;
            openConversation(currentStatus.scope || "project", activeConversationId())
              .catch(() => {})
              .finally(() => {
                reconnectReplayPending = false;
                replayPendingConversationPrompt();
              });
          }
          break;
        case "turn_started":
          currentStatus.execution_kind = msg.expert_mode ? "orchestration" : "chat";
          clearPendingConversationStart(scopedId || activeConversationId());
          stoppingConversations.delete(scopedId || activeConversationId());
          runningConversations.add(scopedId || activeConversationId());
          if (!msg.client_echoed && (msg.text || (msg.attachments || []).length)) {
            addUserMsg(msg.text || "", msg.attachments || []);
          }
          syncActiveStreaming();
          if (!msg.expert_mode && !currentMsg) beginAssistant();
          else currentExpertTimeline = null;
          break;
        case "delta":
          appendDelta(msg.text);
          break;
        case "thinking":
          appendThinking(msg.text);
          break;
        case "tool":
          renderTool(msg);
          break;
        case "tool_result":
          renderToolResult(msg);
          break;
        case "pipeline_item":
          break;
        case "usage":
          updateUsage(msg);
          emitConversationUpdated(scopedId || activeConversationId(), "mewcode:usage-updated");
          break;
        case "note":
          if (!isToolDenialNote(msg.text)) addSystemMsg(msg.text);
          break;
        case "workflow_recovery":
          if (msg.new && !msg.client_echoed) addUserMsg(msg.request || "", []);
          if (msg.new) addSystemMsg(msg.message || "");
          window.dispatchEvent(new CustomEvent('mewcode:workbench-host-event', {detail: msg}));
          break;
        case "continuation":
          currentStatus.continuation = msg.continuation || {};
          renderContinuousStatus();
          syncActiveStreaming();
          if (msg.continuation?.reason) addSystemMsg("持续任务：" + msg.continuation.reason);
          break;
        case "error":
          if (runtimeSettingsView && runtimeSettingsView.pending) {
            clearRuntimePendingTimer(runtimeSettingsView);
            runtimeSettingsView.pending = null;
            runtimeSettingsView.pendingFailed = true;
            runtimeSettingsView.feedback = msg.text;
            runtimeSettingsView.feedbackError = true;
            renderRuntimeSettingsPanel(runtimeSettingsView);
          }
          if (msg.error_code === "recovery_resume_blocked") {
            renderBlockedResumeError(msg, scopedId || activeConversationId());
          } else {
            displayBackendError(msg, scopedId || activeConversationId());
          }
          break;
        case "permission":
          if (reconnectReplayPending) {
            pendingConversationPrompts.set(scopedId || activeConversationId(), msg);
            break;
          }
          showPermissionMessage(msg, scopedId || activeConversationId());
          break;
        case "human_oncall":
          if (reconnectReplayPending) {
            pendingConversationPrompts.set(scopedId || activeConversationId(), msg);
            break;
          }
          showHumanOnCall(msg);
          break;
        case "human_oncall_response_result":
          handleHumanOnCallResponseResult(msg);
          break;
        case "ask_user":
          if (reconnectReplayPending) {
            pendingConversationPrompts.set(scopedId || activeConversationId(), msg);
            break;
          }
          showAskUser(msg);
          break;
        case "plan_revision":
          if (reconnectReplayPending) {
            pendingConversationPrompts.set(scopedId || activeConversationId(), msg);
            break;
          }
          workbenchPrompt(msg);
          break;
        case "models":
          modelList = msg.models || [];
          break;
        case "command_result":
          if (scopedId && !activeConversationId()) {
            currentStatus.conversation_id = scopedId;
            emitConversationContext();
          }
          addSystemMsg(msg.message || (msg.ok ? "命令已执行" : "命令执行失败"));
          if (msg.ok && msg.maintenance) {
            const owner = {project:currentStatus.work_dir,conversation_id:scopedId||activeConversationId()};
            if (owner.conversation_id === activeConversationId()) {
              void window.MewWorkbench?.openMaintenance({...owner,...msg.maintenance});
            }
          }
          if (msg.queued) addSystemMsg("附带任务已加入当前对话的下一回合队列。", "note");
          if (msg.ok && msg.prompt) {
            inputEl.value = msg.prompt;
            inputEl.dispatchEvent(new Event("input"));
            sendChat(true);
          }
          break;
        case "recovery_case":
          applyRecoveryCase(msg);
          break;
        case "expert_stage":
        case "expert_node_started":
        case "expert_node_completed":
          renderExpertStageV2(msg);
          break;
        case "expert_flow_state":
          renderExpertFlowStateV2(msg);
          break;
        case "expert_delta":
          appendExpertDeltaV2(msg);
          break;
        case "expert_thinking":
          appendExpertThinkingV2(msg);
          break;
        case "expert_tool":
          renderExpertToolV2(msg);
          break;
        case "expert_tool_start":
          renderExpertToolV2(msg, "start");
          break;
        case "expert_tool_wait":
          renderExpertToolV2(msg, "wait");
          break;
        case "expert_tool_result":
          renderExpertToolV2(msg, "result");
          break;
        case "expert_tool_state":
          renderExpertToolV2(msg, "state");
          break;
        case "expert_role_wait":
          renderExpertRoleWaitV2(msg);
          break;
        case "gate_timeout_selected":
          if (currentAskUser?.requestId === msg.wait_id || currentHumanOnCall?.requestId === msg.wait_id) {
            currentAskUser = null; hideHumanOnCallCard();
          }
          notify("已保存快照，按推荐项继续", "info");
          break;
        case "expert_usage":
          renderExpertUsageV2(msg);
          emitConversationUpdated(scopedId || activeConversationId(), "mewcode:usage-updated");
          break;
        case "expert_child":
          renderExpertChildV2(msg);
          break;
        case "expert_child_event":
          renderExpertChildEventV2(msg);
          if (msg.child_event_type === "usage") {
            emitConversationUpdated(scopedId || activeConversationId(), "mewcode:usage-updated");
          }
          break;
        case "expert_summary":
          addAssistantMarkdown(msg.text);
          break;
        case "expert_run_done":
          if (currentExpertTimeline) {
            currentExpertTimeline.timeline.classList.add("run-" + msg.status);
          }
          renderExpertControlsV2(msg.status, msg.run_id, msg.reason || msg.status_reason || "", msg.attempt);
          break;
        case "interrupt_ack":
          stoppingConversations.add(scopedId || activeConversationId());
          syncActiveStreaming();
          break;
        case "expert_run_state":
          renderExpertControlsV2(msg.status, msg.run_id, msg.reason || msg.status_reason || "", msg.attempt);
          break;
        case "expert_takeover_state":
          if (msg.plan_revision_required && msg.run_id) {
            const held = ensureExpertTimelineV2(msg.run_id);
            held.savedPlanRevisionPending = true;
            renderExpertControlsV2(held.status || "paused", msg.run_id);
          }
          if (msg.phase === "handling") {
            if (currentMsg) finishStream({elapsed: 0});
            permBar.classList.add("hidden");
            currentExpertTimeline = null;
            beginAssistant();
          } else if (msg.phase === "handled" || msg.phase === "held") {
            if (currentMsg) finishStream({elapsed: 0});
          }
          if (msg.text) addSystemMsg(msg.text);
          if (msg.handoff_review_progress) {
            addHandoffReviewCard(msg.run_id, msg.handoff_review_progress,
                                 scopedId || activeConversationId());
          }
          syncActiveStreaming();
          break;
        case "handoff_review_excerpt":
          addHandoffReviewExcerpt(msg, scopedId || activeConversationId());
          break;
        case "handoff_review_worklist":
          addHandoffReviewWorklist(msg, scopedId || activeConversationId());
          break;
        case "handoff_review_authorized":
        case "handoff_review_escalation_authorized":
          if (msg.text) addSystemMsg(msg.text);
          break;
        case "expert_cancelled": {
          const paths = msg.preserved_worktrees || [];
          notify(
            paths.length
              ? "专家运行已停止；有变更的 worktree 已保留：" + paths.join("、")
              : "专家运行及其直属 Agent 已停止",
            "warning"
          );
          break;
        }
        case "expert_checkpoint_restored":
          renderExpertSnapshot(msg.snapshot);
          break;
        case "done":
          if (msg.maintenance_only) {
            break;
          }
          releaseConversationActivity(scopedId || activeConversationId());
          emitConversationUpdated(scopedId || activeConversationId());
          finishStream(msg);

          break;
      }
      if (
        String(msg.type || "").startsWith("expert_")
        && msg.type !== "expert_flow_defined"
      ) {
        mirrorExpertChatEvent(msg);
      }
    };
  }

  function send(obj) {
    const conversationId = String(obj?.conversation_id || activeConversationId() || "");
    if (conversationId && ["chat", "expert_resume", "expert_retry"].includes(String(obj?.type || ""))
        && !obj?.turn_id && !obj?.turn_key && !obj?.request_id) {
      // Older servers do not echo a turn id.  Give each client submission a
      // fresh scope so identical text in two attempts is still visible once
      // per attempt; the following turn_started may replace this sequence.
      rememberBackendTurnScope({conversation_id: conversationId, run_id: obj?.run_id || ""}, conversationId);
    }
    if (ws && ws.readyState === WebSocket.OPEN) {
      try {
        ws.send(JSON.stringify(obj));
        return true;
      } catch {
        scheduleReconnect(true);
      }
    }
    return false;
  }

  function conversationHasActivity(conversationId) {
    const id = String(conversationId || "");
    return runningConversations.has(id) ||
      pendingConversationStarts.has(id) ||
      stoppingConversations.has(id);
  }

  function finalizeStoppedConversation(conversationId) {
    const id = String(conversationId || "");
    if (!conversationHasActivity(id)) return;
    releaseConversationActivity(id);
    if (id === activeConversationId()) {
      finishStream({ interrupted: true, already_stopped: true, elapsed: 0 });
    }
  }

  async function reconcileConversationStop(conversationId) {
    const id = String(conversationId || "");
    clearConversationStopTimer(id);
    try {
      const response = await fetch(
        "/api/chat/status?conversation_id=" + encodeURIComponent(id),
        { cache: "no-store" }
      );
      if (!response.ok) throw new Error("status " + response.status);
      const status = await response.json();
      if (!status.running) {
        finalizeStoppedConversation(id);
        return;
      }
      // The first cancellation is still unwinding.  Keep the real running
      // state but let the user issue another interrupt instead of trapping the
      // composer behind a permanently disabled "stopping" button.
      stoppingConversations.delete(id);
      syncActiveStreaming();
      if (id === activeConversationId()) {
        notify("停止请求已送达，但任务仍在收尾；可以再次点击打断", "warning");
      }
    } catch {
      stoppingConversations.delete(id);
      syncActiveStreaming();
      if (id === activeConversationId()) {
        notify("暂时无法确认停止状态，可以再次点击打断", "warning");
      }
    }
  }

  function scheduleConversationStopReconcile(conversationId) {
    const id = String(conversationId || "");
    clearConversationStopTimer(id);
    conversationStopTimers.set(
      id,
      window.setTimeout(() => void reconcileConversationStop(id), 3500)
    );
  }

  async function stopActiveConversation() {
    const conversationId = activeConversationId();
    if (!conversationId || !conversationHasActivity(conversationId)) return;
    if (stoppingConversations.has(conversationId)) return;
    stoppingConversations.add(conversationId);
    syncActiveStreaming();

    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 3000);
    try {
      const response = await fetch("/api/chat/interrupt", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          conversation_id: conversationId,
          action: "pause",
        }),
        signal: controller.signal,
      });
      if (!response.ok) throw new Error("interrupt " + response.status);
      const result = await response.json();
      if (["not_running", "cancelled_before_start"].includes(result.status)) {
        finalizeStoppedConversation(conversationId);
        return;
      }
      scheduleConversationStopReconcile(conversationId);
    } catch {
      // Older backends do not expose the HTTP control endpoint.  Keep the
      // WebSocket command as a compatibility fallback, then verify state.
      send({ type: "interrupt", conversation_id: conversationId });
      scheduleConversationStopReconcile(conversationId);
    } finally {
      window.clearTimeout(timeout);
    }
  }

  function finishStream(msg) {
    msg = msg || {};
    syncActiveStreaming();
    currentHumanOnCall = null;
    currentAskUser = null;
    humanOnCallPanel.classList.add("hidden");
    finalizeThinking(true);
    stopTurnClock(msg && msg.elapsed);
    closeMarkdownSegment();
    settlePendingToolRows(msg);
    if (currentMsg) {
      currentMsg.querySelectorAll(".md").forEach((el) => enhanceMath(el));
    }
    // 清理所有文本段的光标与空节点；工具组后的文本段不能只清理首个 .md。
    clearLiveMarkdownState(true);
    if (currentMsg && Array.isArray(msg.changes) && msg.changes.length) {
      const bubble = currentMsg.querySelector(".msg-bubble");
      if (bubble && !bubble.querySelector(".turn-changes")) {
        bubble.appendChild(buildTurnChangesCard(msg.changes));
      }
    }
    if (currentMsg) {
      const bubble = currentMsg.querySelector(".msg-bubble");
      if (bubble && !bubble.querySelector(".md, .tool-group, .turn-thinking, .turn-changes")) {
        currentMsg.remove();
        currentMsg = null;
      }
    }
    currentMarkdownSegment = null;
    currentTextSegment = "";
    currentToolGroup = null;
    currentThinkingEl = null;
    currentThinkingText = "";
    if (msg.status) {
      currentStatus = { ...currentStatus, ...msg.status };
      attachMsgFooter(msg.status);
      updateContextBadge(currentStatus);
      updateStatusLine();
      updateShellStatus();
    }
    if (msg.error) displayBackendError(msg, msg.conversation_id || activeConversationId());
    if (msg.interrupted) addSystemMsg(
      msg.expert_status === "paused" ? "（专家任务已暂停，可从时间线继续）" : "（已中断）"
    );
    // elapsed 为 0 的 done 是服务端拒绝请求时的复位消息，不显示统计卡片
    if (msg.elapsed > 0) addSessionSummary(msg);
    if (msg.tasks) renderTasks(msg.tasks);
    if (currentMsg) attachAssistantCopyButton(currentMsg, currentText);
    currentText = "";
    currentMsg = null;
    currentExpertTimeline = null;
    turnStartedAt = 0;
    backendTurnScopes.delete(String(msg.conversation_id || activeConversationId() || ""));
    loadSidebar();
  }

  // ---------- 输入与发送 ----------

  function renderAttachmentTray() {
    attachmentTray.innerHTML = "";
    attachmentTray.classList.toggle("hidden", !pendingAttachments.length && !attachmentUploadBusy);
    for (const attachment of pendingAttachments) {
      const chip = document.createElement("div");
      chip.className = "attachment-chip";
      if (attachment.kind === "image" && attachment.preview_url) {
        const image = document.createElement("img");
        image.src = attachment.preview_url; image.alt = ""; chip.appendChild(image);
      } else {
        const icon = document.createElement("span");
        icon.className = "attachment-kind-icon";
        icon.textContent = ({ pdf: "PDF", docx: "DOC", spreadsheet: "XLS", text: "TXT" })[attachment.kind] || "FILE";
        chip.appendChild(icon);
      }
      const meta = document.createElement("span");
      meta.innerHTML = "<strong>" + esc(attachment.name) + "</strong><small>" +
        esc(formatFileSize(attachment.size) + (attachment.truncated ? " · 正文已截断" : "")) + "</small>";
      const remove = document.createElement("button");
      remove.type = "button";
      remove.innerHTML = '<svg class="ui-icon" aria-hidden="true"><use href="/static/icons.svg#close"></use></svg>';
      remove.title = "移除附件";
      remove.addEventListener("click", async () => {
        remove.disabled = true;
        try { await fetch("/api/chat/attachments/" + encodeURIComponent(attachment.id), { method: "DELETE" }); }
        finally {
          pendingAttachments = pendingAttachments.filter((item) => item.id !== attachment.id);
          renderAttachmentTray();
        }
      });
      chip.append(meta, remove); attachmentTray.appendChild(chip);
    }
    if (attachmentUploadBusy) {
      const busy = document.createElement("div");
      busy.className = "attachment-chip is-uploading";
      busy.innerHTML = "<span class='attachment-spinner'></span><span><strong>正在解析附件</strong><small>图片校验与文档提取在本地完成</small></span>";
      attachmentTray.appendChild(busy);
    }
  }

  function discardPendingAttachments() {
    const stale = pendingAttachments;
    pendingAttachments = [];
    renderAttachmentTray();
    for (const attachment of stale) {
      fetch("/api/chat/attachments/" + encodeURIComponent(attachment.id), {
        method: "DELETE",
      }).catch(() => {});
    }
  }

  async function uploadAttachments(fileList) {
    if (chatQueueSubmissionPending) return;
    const files = [...(fileList || [])];
    if (!files.length) return;
    if (pendingAttachments.length + files.length > 10) {
      notify("每条消息最多添加 10 个附件", "error"); return;
    }
    const total = files.reduce((sum, file) => sum + file.size, 0) +
      pendingAttachments.reduce((sum, item) => sum + item.size, 0);
    if (files.some((file) => file.size > 20 * 1024 * 1024)) {
      notify("单个附件不能超过 20 MiB", "error"); return;
    }
    if (total > 50 * 1024 * 1024) {
      notify("单条消息的附件总大小不能超过 50 MiB", "error"); return;
    }
    attachmentUploadBusy = true; renderAttachmentTray();
    try {
      const form = new FormData();
      files.forEach((file, index) => {
        const name = file.name || (file.type.startsWith("image/") ? "pasted-image-" + (index + 1) + ".png" : "attachment-" + (index + 1));
        form.append("files", file, name);
      });
      const response = await fetch("/api/chat/attachments", { method: "POST", body: form });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "附件上传失败");
      pendingAttachments.push(...(result.attachments || []));
      await refreshState();
    } catch (error) {
      notify(error.message || String(error), "error");
    } finally {
      attachmentUploadBusy = false; renderAttachmentTray(); attachmentInput.value = "";
    }
  }

  async function queueCurrentComposer(text, attachments, conversationId, draftValue) {
    if (!window.MewChatQueue?.enqueue) {
      notify("等待区尚未就绪，请稍后重试", "error");
      return false;
    }
    chatQueueSubmissionPending = true;
    inputEl.disabled = true;
    syncActiveStreaming();
    try {
      const continuous = currentStatus.execution_kind !== "orchestration" && ["running", "reviewing", "intervening"].includes(currentStatus.continuation?.status);
      const intervention = (currentStatus.execution_kind === "orchestration" || continuous) && text.trim() && !attachments.length;
      if (intervention) {
        const response = await fetch(continuous ? "/api/chat/interventions" : "/api/orchestration/interventions", {
          method: "POST", headers: {"Content-Type": "application/json", "X-Mew-Chat-Queue": "1"},
          body: JSON.stringify({conversation_id: conversationId, queue_id: "input-" + crypto.randomUUID(), text, attachments: [],
            workbench_context: window.MewWorkbench?.chatContext?.() || null}),
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || "加入编排失败");
        if (activeConversationId() === conversationId) {
          if (result.new && !result.client_echoed && (!continuous || result.control)) addUserMsg(text, []);
          notify(result.message, "success");
        }
      } else {
        await window.MewChatQueue.enqueue({conversationId, text, attachments});
      }
      // The server binds the item to conversationId. Never clear a draft that
      // appeared after the user navigated to another conversation meanwhile.
      const attachmentIds = attachments.map((item) => String(item.id || ""));
      const currentAttachmentIds = pendingAttachments.map((item) => String(item.id || ""));
      const draftIsUnchanged = inputEl.value === draftValue;
      const attachmentsAreUnchanged = attachmentIds.length === currentAttachmentIds.length &&
        attachmentIds.every((id, index) => id === currentAttachmentIds[index]);
      if (
        activeConversationId() === conversationId &&
        draftIsUnchanged &&
        attachmentsAreUnchanged
      ) {
        inputEl.value = "";
        inputEl.style.height = "auto";
        pendingAttachments = [];
        renderAttachmentTray();
        hideSlashMenu();
        inputEl.focus();
      } else if (activeConversationId() === conversationId) {
        notify("消息已接收；检测到输入内容有变化，当前草稿已保留。", "success");
      }
      return true;
    } catch (error) {
      const message = error?.code === "not_running"
        ? "上一条回复刚刚结束，内容仍在输入框；再按一次回车即可发送。"
        : (error.message || "加入等待区失败，内容仍在输入框。");
      notify(message, error?.code === "not_running" ? "warning" : "error");
      return false;
    } finally {
      chatQueueSubmissionPending = false;
      inputEl.disabled = !!readonlyView;
      syncActiveStreaming();
    }
  }

  async function sendChat(skipSlashCommand = false, continuation = null) {
    if (conversationNavigationPending) return;
    if (continuation && (streaming || activeConversationId() !== continuation.conversation_id)) {
      notify("对话或执行状态已变化，请重新选择续接发送。", "error");
      return;
    }
    const draftValue = inputEl.value;
    const text = draftValue.trim();
    if ((!text && !pendingAttachments.length) || attachmentUploadBusy) return;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      $("conn-status").classList.remove("hidden");
      scheduleReconnect(true);
      return;
    }
    const slashCommand = skipSlashCommand ? null : parseSlashCommand(text);
    if (slashCommand) {
      inputEl.value = "";
      inputEl.style.height = "auto";
      hideSlashMenu();
      send({
        type: "command",
        name: slashCommand.name,
        args: slashCommand.args,
        conversation_id: activeConversationId(),
      });
      return;
    }
    if (readonlyView) {
      addSystemMsg("只读预览中不能发送消息，请新建或切换回当前项目的对话");
      return;
    }
    const attachments = pendingAttachments.map((item) => ({ ...item }));
    if (attachments.some((item) => item.kind === "image") && modelCatalog) {
      const selected = (modelCatalog.models || []).find((model) =>
        model.provider_key === currentStatus.provider_key && model.model_id === currentStatus.model
      );
      if (selected && !selected.vision) {
        notify("当前模型不支持图片输入，请先切换到带 Vision 标记的模型", "error");
        return;
      }
    }
    const conversationId = activeConversationId();
    if (!conversationId) {
      addErrorMsg("当前对话尚未就绪，请稍候重新发送。");
      void refreshState();
      return;
    }
    if (streaming) {
      await queueCurrentComposer(text, attachments, conversationId, draftValue);
      return;
    }
    continuation ||= window.MewWorkbench?.chatContinuation?.() || null;
    if(continuation && continuation.conversation_id!==conversationId)continuation=null;
    const userMessage = addUserMsg(text, attachments);
    inputEl.value = "";
    inputEl.style.height = "auto";
    pendingAttachments = [];
    renderAttachmentTray();
    conversationEventBuffers.set(conversationId, []);
    markConversationStartPending(conversationId);
    // Wait for turn_started to distinguish ordinary chat from an explicit resume.
    currentExpertTimeline = null;
    const sent = send({
      type: "chat",
      text,
      request_id: "input-" + crypto.randomUUID(),
      workbench_context: window.MewWorkbench?.chatContext?.() || null,
      attachments: attachments.map((item) => item.id),
      conversation_id: conversationId,
      ...(continuation ? { continuation_task_id: continuation.task_id } : {}),
    });
    if (!sent) {
      releaseConversationActivity(conversationId);
      finishStream({ interrupted: false, elapsed: 0 });
      if (userMessage && userMessage.isConnected) userMessage.remove();
      inputEl.value = text;
      inputEl.dispatchEvent(new Event("input"));
      pendingAttachments = attachments;
      renderAttachmentTray();
      addErrorMsg("消息未发送：连接已断开。内容已放回输入框，请重试。");
    }
  }

  // Explicit task identity is fetched only for this click; never carried to a
  // different conversation, queued turn or ordinary send. This grants nothing.
  $("continue-task-btn").addEventListener("click", async () => {
    const button = $("continue-task-btn");
    if (button.disabled || streaming || readonlyView || conversationNavigationPending) return;
    if (!inputEl.value.trim() && !pendingAttachments.length) {
      notify("请先输入本任务的补充说明，再点续接发送。", "info");
      return;
    }
    const conversationId = activeConversationId();
    const draft = inputEl.value;
    const attachmentIds = pendingAttachments.map(item => item.id).join("\n");
    button.disabled = true;
    try {
      const response = await fetch("/api/chat/task-authorization");
      const state = await response.json();
      if (!response.ok) throw new Error(state.error || "无法读取当前任务");
      if (!conversationId || state.conversation_id !== conversationId ||
          activeConversationId() !== conversationId || conversationNavigationPending ||
          inputEl.value !== draft || pendingAttachments.map(item => item.id).join("\n") !== attachmentIds || streaming) {
        throw new Error("对话、草稿或执行状态已变化；内容未发送，请重新选择续接发送。");
      }
      if (state.task_kind !== "chat" || !state.task_id) {
        throw new Error("没有可续接的普通对话任务；编排任务请使用对应任务卡的继续按钮。");
      }
      await sendChat(true, { conversation_id: conversationId, task_id: state.task_id });
    } catch (error) {
      notify(String(error.message || error), "error");
    } finally {
      button.disabled = false;
    }
  });

  sendBtn.addEventListener("click", () => sendChat());
  window.addEventListener("mewcode:chat-queue-error", (event) => {
    notify(event.detail?.message || "等待区操作失败", "error");
  });
  stopBtn.addEventListener("click", () => {
    void stopActiveConversation();
  });
  const updateStopLabel = () => {
    syncActiveStreaming();
  };
  updateStopLabel();

  window.MewSlashInput.bind(inputEl, () => ({
    value: inputEl.value,
    menuOpen: !$("slash-menu").classList.contains("hidden"),
    matches: slashMatches,
    selectedIndex: slashSelectedIndex,
    setSelectedIndex: (index) => { slashSelectedIndex = index; },
    render: renderSlashMenu,
    hide: hideSlashMenu,
    select: selectSlashCommand,
    submit: () => sendChat(),
  }));

  const COMPOSER_INPUT_MAX = 140;
  function syncComposerInputHeight() {
    inputEl.style.height = "auto";
    const next = Math.min(inputEl.scrollHeight, COMPOSER_INPUT_MAX);
    inputEl.style.height = next + "px";
    // Keep caret/end visible after autosize; avoid the post-IME bounce-to-top.
    inputEl.scrollTop = inputEl.scrollHeight;
  }
  inputEl.addEventListener("input", () => {
    syncComposerInputHeight();
    renderSlashMenu();
  });
  inputEl.addEventListener("keyup", syncComposerInputHeight);
  inputEl.addEventListener("blur", () => setTimeout(hideSlashMenu, 120));

  attachBtn.addEventListener("click", () => attachmentInput.click());
  attachmentInput.addEventListener("change", () => uploadAttachments(attachmentInput.files));
  const inputWrap = document.querySelector(".input-wrap");
  let dragDepth = 0;
  inputWrap.addEventListener("dragenter", (event) => {
    event.preventDefault(); dragDepth += 1; inputWrap.classList.add("is-dragging");
  });
  inputWrap.addEventListener("dragover", (event) => event.preventDefault());
  inputWrap.addEventListener("dragleave", (event) => {
    event.preventDefault(); dragDepth = Math.max(dragDepth - 1, 0);
    if (!dragDepth) inputWrap.classList.remove("is-dragging");
  });
  inputWrap.addEventListener("drop", (event) => {
    event.preventDefault(); dragDepth = 0; inputWrap.classList.remove("is-dragging");
    uploadAttachments(event.dataTransfer && event.dataTransfer.files);
  });
  inputEl.addEventListener("paste", (event) => {
    const files = [...(event.clipboardData && event.clipboardData.files || [])];
    if (!files.length) return;
    event.preventDefault(); uploadAttachments(files);
  });

  // ---------- 权限条（三档决策） ----------

  function loadApprovalMetrics(container) {
    container.textContent = "正在读取本次审批统计…";
    container.style.whiteSpace = "pre-wrap";
    fetch("/api/chat/approval-metrics", { cache: "no-store" })
      .then((response) => {
        if (!response.ok) throw new Error("approval metrics unavailable");
        return response.json();
      })
      .then((data) => { container.textContent = window.MewApprovalMetrics.format(data); })
      .catch(() => { container.textContent = "本次审批统计尚不可用"; });
  }
  $("approval-metrics-btn").addEventListener("click", (event) => {
    openSettingsDialog("当前会话权限统计（只读）", event.currentTarget, (body) => {
      const metrics = document.createElement("p");
      body.appendChild(metrics);
      loadApprovalMetrics(metrics);
    });
  });

  $("native-execution-btn").addEventListener("click", (event) => {
    openSettingsDialog("沙箱设置与执行恢复", event.currentTarget, (body, footer) => {
      globalThis.MewSandboxNetwork?.mount(body);
      const recoveryDetails = document.createElement('details');
      const recoveryHeading = document.createElement('summary');
      recoveryHeading.textContent = '执行状态与恢复';
      const recoveryBody = document.createElement('div');
      const recoveryFooter = document.createElement('div');
      recoveryDetails.append(recoveryHeading, recoveryBody, recoveryFooter);
      body.append(recoveryDetails);
      body = recoveryBody; footer = recoveryFooter;
      const summary = document.createElement("p");
      summary.style.whiteSpace = "pre-wrap";
      const note = document.createElement("p");
      note.textContent = "中断的隔离运行会自动检查收尾。若只剩命令清理未完成，仍可编辑项目文件；只有涉及项目文件的恢复才需要确认具体改动。";
      const diagnostics = document.createElement('details');
      const diagnosticsHeading = document.createElement('summary');
      diagnosticsHeading.textContent = '执行诊断（只读）';
      const diagnosticsText = document.createElement('p');
      diagnosticsText.style.whiteSpace = 'pre-wrap'; diagnosticsText.style.overflowWrap = 'anywhere';
      diagnostics.append(diagnosticsHeading, diagnosticsText);
      const operationResult = document.createElement('p');
      operationResult.style.whiteSpace = 'pre-wrap'; operationResult.hidden = true;
      const label = document.createElement("label");
      label.textContent = "当前卷的宿主暂存目录（绝对路径）";
      const directory = document.createElement("input");
      directory.type = "text"; directory.autocomplete = "off";
      label.appendChild(directory);
      body.append(summary, note, diagnostics, operationResult);
      const refresh = document.createElement("button");
      refresh.type = "button"; refresh.className = "btn"; refresh.textContent = "刷新状态";
      const save = document.createElement("button");
      save.type = "button"; save.className = "btn"; save.textContent = "确认设置当前卷暂存目录";
      const storageDetails = document.createElement("details");
      const storageHeading = document.createElement("summary");
      storageHeading.textContent = "高级：更改暂存位置";
      storageDetails.append(storageHeading, label, save);
      body.append(storageDetails);
      const recover = document.createElement("button");
      recover.type = "button"; recover.className = "btn"; recover.textContent = "检查并恢复";
      const transaction = document.createElement("select");
      const previewText = document.createElement("pre");
      previewText.style.maxHeight = "240px"; previewText.style.overflow = "auto";
      const preview = document.createElement("button");
      preview.type = "button"; preview.className = "btn"; preview.textContent = "查看需要恢复的文件";
      const confirmRecovery = document.createElement("button");
      confirmRecovery.type = "button"; confirmRecovery.className = "btn";
      confirmRecovery.textContent = "确认仅核验或回滚以上条目";
      body.append(transaction, previewText);
      footer.append(refresh, recover, preview, confirmRecovery);
      let state = null, recoveryPreview = null;
      const busy = () => {
        save.disabled = recover.disabled = refresh.disabled = preview.disabled = confirmRecovery.disabled = true;
        transaction.disabled = true;
      };
      const load = async () => {
        busy(); recoveryPreview = null; previewText.textContent = "";
        summary.textContent = "正在检查执行状态…";
        try {
          const response = await fetch("/api/chat/native-execution", { cache: "no-store" });
          const data = await response.json();
          if (!response.ok) throw new Error(data.error || "原生执行状态不可用");
          state = data;
          transaction.textContent = "";
          const fileRecoveries = data.transactions.filter(item => item.private_only !== true);
          data.transactions.forEach((item) => {
            const option = document.createElement("option");
            option.value = item.id; option.textContent = `${item.current_task ? "当前任务" : "之前任务"} · ${item.private_only ? "隔离命令收尾" : "文件恢复"}`;
            transaction.appendChild(option);
          });
          transaction.value = data.transactions[0]?.id || "";
          directory.value = data.storage.directory || "";
          const privatePending = data.transactions.length && !fileRecoveries.length;
          summary.textContent = `工作区：${data.workspace}\n` +
            (privatePending ? (data.execution_backend === 'host_reviewed' && data.commands_ready ?
              '旧隔离命令保持待核验；独立的新操作可经 Auto 审查继续，不能重放旧命令。' :
              '文件编辑可继续。上次隔离命令尚未确认结束，隔离命令暂不可用。') :
              data.transactions.length ? '上次执行有项目文件需要恢复，请先检查恢复结果。' : '没有待恢复的执行。') +
            [...new Set(data.transactions.map(item => item.message).filter(Boolean))].map(text => `\n${text}`).join('') +
            (data.execution_backend === 'host_reviewed' ? '\n当前 Auto 使用风险审查后的本机执行。' :
              data.gate.reason ? `\n执行环境：${data.gate.reason}` : '');
          diagnostics.hidden = !data.transactions.length;
          diagnosticsText.textContent = data.transactions.map(item =>
            `事务 ID：${item.id}\n状态：${item.state}\n` +
            (item.message ? `诊断：${item.message}\n` : '') +
            (item.cleanup_evidence ? `清理凭证：${item.cleanup_evidence}\n` : '') +
            (item.failure_type ? `原始异常类型：${item.failure_type}\n` : '') +
            (item.next_step || '')).join('\n\n');
          recoveryDetails.open = !!data.transactions.length;
          transaction.hidden = preview.hidden = confirmRecovery.hidden = previewText.hidden = !fileRecoveries.length;
          save.disabled = false;
          recover.disabled = !data.transactions.length;
          preview.disabled = transaction.disabled = !data.transactions.length;
        } catch (error) {
          state = null; summary.textContent = error.message || String(error);
        } finally { refresh.disabled = false; }
      };
      const submit = async (action, confirmation = null) => {
        if (!state) return;
        const captured = state;
        busy();
        operationResult.hidden = false; operationResult.textContent = '正在核验…';
        try {
          const response = await fetch(`/api/chat/native-execution/${action}`, {
            method: "POST", headers: { "Content-Type": "application/json", "X-Mew-Native-Control": "1" },
            body: JSON.stringify({ conversation_id: captured.conversation_id, task_id: captured.task_id,
              directory: directory.value.trim(), revision: captured.storage.revision,
              ...(confirmation ? { confirmation } : {}) }),
          });
          const result = await response.json();
          if (!response.ok) throw new Error(result.error || "宿主操作失败");
          await load();
          if (action === "reconcile" && !result.complete) {
            operationResult.textContent = result.outcomes.map(item =>
              (item.id ? `事务 ${item.id}：${item.state}\n` : '') +
              `仍需处理：${item.reason || item.state || '仍需宿主核验。'}`).join('\n\n');
          } else {
            operationResult.textContent = action === 'storage' ? '暂存目录已保存。' : '执行恢复检查已完成。';
            notify(action === "storage" ? "暂存目录已保存，权限范围未扩大" : "执行恢复检查已完成", "success");
          }
        } catch (error) {
          operationResult.textContent = error.message || String(error);
          notify(error.message || String(error), "error");
          await load();
        }
      };
      refresh.addEventListener("click", load);
      save.addEventListener("click", () => submit("storage"));
      recover.addEventListener("click", () => submit("reconcile"));
      transaction.addEventListener("change", () => {
        recoveryPreview = null; previewText.textContent = ""; confirmRecovery.disabled = true;
      });
      preview.addEventListener("click", async () => {
        if (!state || !transaction.value) return;
        busy(); recoveryPreview = null; previewText.textContent = "正在核对恢复范围…";
        try {
          const response = await fetch("/api/chat/native-execution/recovery-preview", {
            method: "POST", headers: { "Content-Type": "application/json", "X-Mew-Native-Control": "1" },
            body: JSON.stringify({ conversation_id: state.conversation_id, task_id: state.task_id,
              transaction_id: transaction.value }),
          });
          const result = await response.json();
          if (!response.ok) throw new Error(result.error || "恢复范围不可用");
          recoveryPreview = result.preview;
          previewText.textContent = `原任务：${result.preview.original_task_id}\n当前任务：${result.preview.current_task_id}\n` +
            `仅核验已提交结果或回滚未完成发布；不执行命令。条目数：${result.preview.targets.length}\n` +
            result.preview.targets.join("\n");
          confirmRecovery.disabled = false;
        } catch (error) { previewText.textContent = error.message || String(error); }
        finally {
          refresh.disabled = save.disabled = preview.disabled = transaction.disabled = false;
          recover.disabled = !state.transactions.length;
        }
      });
      confirmRecovery.addEventListener("click", () => {
        if (recoveryPreview) submit("reconcile", {
          transaction_id: recoveryPreview.transaction_id, token: recoveryPreview.token,
        });
      });
      load();
    });
  });

  $("task-auth-btn").addEventListener("click", async (event) => {
    try {
      const response = await fetch("/api/chat/task-authorization", { cache: "no-store" });
      const state = await response.json();
      if (!response.ok || !state.task_id) throw new Error(state.error || "请先开始一个对话任务");
      openSettingsDialog("当前任务授权", event.currentTarget, (body, footer) => {
        const el = (tag, className, text) => {
          const node = document.createElement(tag);
          if (className) node.className = className;
          if (text !== undefined) node.textContent = text;
          return node;
        };
        const form = el("div", "task-auth-form");
        body.appendChild(form);
        const intro = el("div", "task-auth-context");
        const contextHeading = el("div", "task-auth-context-heading");
        contextHeading.append(el("span", "task-auth-eyebrow", "授权对象"), el("span", "task-auth-badge", "仅当前任务"));
        const taskTitle = el("strong", "task-auth-task-title", state.task_title || "当前对话任务");
        taskTitle.title = state.task_title || "当前对话任务";
        intro.append(contextHeading, taskTitle,
          el("p", "task-auth-help", "只确认本次任务需要的范围。授权不会覆盖硬规则，也不会自动启用原生执行。"));
        form.appendChild(intro);

        const section = (title, description, target = form) => {
          const node = el("section", "task-auth-section");
          node.append(el("h3", "", title), el("p", "task-auth-help", description));
          target.appendChild(node);
          return node;
        };
        const behavior = (target, id, title, description, checked) => {
          const label = el("label", "task-auth-choice");
          const input = el("input", "task-auth-checkbox");
          input.type = "checkbox"; input.id = id; input.checked = checked;
          const copy = el("span", "task-auth-choice-copy");
          const help = el("span", "task-auth-help", description);
          help.id = id + "-help";
          input.setAttribute("aria-describedby", help.id);
          copy.append(el("strong", "", title), help);
          label.append(input, copy); target.appendChild(label);
          return input;
        };
        const permissions = section("执行权限", "按需开启；未勾选的行为不会随确认一并授权。");
        const gitWrite = behavior(permissions, "task-auth-git-write", "Git 暂存与本地提交",
          "允许当前任务写入 Git 对象、索引和引用。不允许修改 hooks、配置或仓库指针；仍须通过原生执行边界。",
          (state.authorization?.allowed_behaviors || []).includes("git_write"));

        const fields = {};
        const field = (target, key, title, placeholder, helpText) => {
          const wrap = el("div", "task-auth-field");
          const label = el("label", "task-auth-label", title);
          label.htmlFor = "task-auth-" + key;
          const input = el("textarea", "task-auth-input");
          input.id = label.htmlFor;
          input.placeholder = placeholder;
          input.value = (state.authorization?.[key] || []).join("\n");
          input.spellcheck = false;
          input.rows = key === "network_hosts" ? 2 : 3;
          const help = el("p", "task-auth-help", helpText);
          help.id = input.id + "-help";
          input.setAttribute("aria-describedby", help.id);
          wrap.append(label, input, help); target.appendChild(wrap);
          fields[key] = input;
        };
        const directories = section("文件范围", "填写已存在的绝对路径，每行一个。留空表示不新增该项任务授权，不会撤销既有权限。");
        const directoryGrid = el("div", "task-auth-grid");
        directories.appendChild(directoryGrid);
        field(directoryGrid, "read_directories", "可读目录", "例如 C:\\Projects\\my-app", "任务可读取的目录。");
        field(directoryGrid, "write_directories", "可写目录", "例如 C:\\Projects\\my-app\\output", "只填写需要保存改动的位置，避免授权过宽。");

        const advanced = el("details", "task-auth-details");
        advanced.open = Boolean(state.authorization?.allowed_tools?.length || state.authorization?.network_hosts?.length
          || (state.authorization?.allowed_behaviors || []).includes("isolated_command"));
        advanced.appendChild(el("summary", "", "更多选项 · 工具、网络与严格隔离"));
        const advancedBody = el("div", "task-auth-details-body");
        advanced.appendChild(advancedBody);
        const advancedGrid = el("div", "task-auth-grid");
        advancedBody.appendChild(advancedGrid);
        field(advancedGrid, "allowed_tools", "允许的结构化工具", "例如 ReadFile", "每行一个工具名，不接受任意 Shell 命令。");
        field(advancedGrid, "network_hosts", "可访问主机", "例如 docs.python.org", "精确主机名，不含通配符。仅只读结构化工具，不跟随重定向。");
        const isolatedCommand = behavior(advancedBody, "task-auth-isolated-command", "严格隔离档的容器执行",
          "仅允许在禁网容器中运行经审核的脚本与校验命令。不启用 AUTO，也不是 Git 授权的前置条件。",
          (state.authorization?.allowed_behaviors || []).includes("isolated_command"));
        form.appendChild(advanced);

        const metricsDetails = el("details", "task-auth-details task-auth-metrics");
        metricsDetails.appendChild(el("summary", "", "查看当前会话审批统计"));
        const metrics = el("div", "task-auth-details-body");
        metricsDetails.appendChild(metrics);
        let metricsLoaded = false;
        metricsDetails.addEventListener("toggle", () => {
          if (metricsDetails.open && !metricsLoaded) { metricsLoaded = true; loadApprovalMetrics(metrics); }
        });
        form.appendChild(metricsDetails);
        const errorBox = el("p", "task-auth-error");
        errorBox.hidden = true; errorBox.setAttribute("role", "alert");
        form.appendChild(errorBox);

        const actions = el("div", "task-auth-actions");
        const duration = el("div", "task-auth-duration");
        const expiryLabel = el("label", "task-auth-label", "有效期");
        expiryLabel.htmlFor = "task-auth-expiry";
        const expiry = el("select", "task-auth-select");
        expiry.id = expiryLabel.htmlFor;
        for (const [seconds, label] of [[3600, "1 小时"], [14400, "4 小时"], [86400, "24 小时"]]) {
          const option = document.createElement("option");
          option.value = String(seconds); option.textContent = label;
          expiry.appendChild(option);
        }
        duration.append(expiryLabel, expiry);
        const buttons = el("div", "task-auth-buttons");
        const cancel = el("button", "btn task-auth-cancel", "取消");
        cancel.type = "button"; cancel.addEventListener("click", closeSettingsDialog);
        const save = el("button", "btn task-auth-confirm", "确认授权");
        save.type = "button";
        buttons.append(cancel, save); actions.append(duration, buttons); footer.appendChild(actions);
        save.addEventListener("click", async () => {
          if (save.disabled) return;
          save.disabled = true; save.textContent = "正在保存…"; errorBox.hidden = true;
          const lines = (value) => value.split(/\r?\n/).map((part) => part.trim()).filter(Boolean);
          const payload = { conversation_id: state.conversation_id, task_id: state.task_id,
            ttl_seconds: Number(expiry.value),
            allowed_behaviors: [...(isolatedCommand.checked ? ["isolated_command"] : []),
              ...(gitWrite.checked ? ["git_write"] : [])] };
          for (const [key, input] of Object.entries(fields)) payload[key] = lines(input.value);
          try {
            const result = await fetch("/api/chat/task-authorization", {
              method: "POST", headers: { "Content-Type": "application/json", "X-Mew-Task-Authorization": "1" },
              body: JSON.stringify(payload),
            });
            const saved = await result.json();
            if (!result.ok) throw new Error(saved.error || "任务授权失败");
            notify("任务授权已记录，版本 " + saved.authorization.version + "。补充说明请用「续接发送」，保留当前任务身份。", "success");
            if (form.isConnected) closeSettingsDialog();
          } catch (error) {
            errorBox.textContent = error.message || String(error);
            errorBox.hidden = false;
            errorBox.scrollIntoView({ block: "nearest" });
            save.disabled = false; save.textContent = "确认授权";
          }
        });
      });
    } catch (error) {
      notify(error.message || String(error), "error");
    }
  });

  function answerPermission(decision) {
    send({
      type: "permission_response",
      request_id: currentPermissionRequestId,
      decision,
      conversation_id: currentPermissionConversationId || activeConversationId(),
    });
    currentPermissionConversationId = "";
    currentPermissionRequestId = "";
    permBar.classList.add("hidden");
  }
  $("perm-allow").addEventListener("click", () => answerPermission("allow"));
  $("perm-always").addEventListener("click", () => answerPermission("allow_always"));
  $("perm-directory").addEventListener("click", () => answerPermission("allow_directory"));
  $("perm-deny").addEventListener("click", () => answerPermission("deny"));
  $("pairing-allow-once").addEventListener("click", () => answerPairing("once"));
  $("pairing-allow-always").addEventListener("click", () => answerPairing("always"));
  $("pairing-deny").addEventListener("click", () => answerPairing("deny"));

  $("human-oncall-other").addEventListener("input", () => {
    if (currentHumanOnCall && !currentHumanOnCall.submitting) {
      const needsInput = currentHumanOnCall.answer === "__other__" || Boolean(currentHumanOnCall.choice?.requires_input);
      if (!needsInput) return;
      $("human-oncall-submit").disabled = !$("human-oncall-other").value.trim();
      rememberHumanOnCallDraft();
    }
  });
  $("human-oncall-note")?.addEventListener("input", () => {
    if (currentHumanOnCall) rememberHumanOnCallDraft();
  });
  $("human-oncall-submit").addEventListener("click", () => {
    if (currentAskUser) answerAskUser(false);
    else answerHumanOnCall(false);
  });
  $("human-oncall-cancel").addEventListener("click", () => {
    if (currentAskUser) answerAskUser(true);
    else answerHumanOnCall(true);
  });
  $("conn-retry").addEventListener("click", (event) => {
    event.preventDefault();
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = null;
    reconnectAttempt = 0;
    connect();
  });
  window.addEventListener("online", () => scheduleReconnect(true));

  // ---------- 启动 ----------

  async function bootstrapWorkspace() {
    connect();
    renderEmptyState();
    inputEl.focus();
    void loadSidebar();
    await refreshState();
    const conversationId = activeConversationId();
    // The status endpoint restores the active conversation id/title, but it
    // does not include history or expert checkpoints.  Hydrate that active
    // conversation on startup so the board does not remain at an empty 1/N
    // preview until the user clicks the already-selected sidebar row again.
    if (conversationId && !conversationNavigationPending) {
      await openConversation(
        currentStatus.scope === "normal" ? "normal" : "project",
        conversationId,
      );
    }
  }

  void bootstrapWorkspace();
  loadModelCatalog().catch(() => {});
})();
