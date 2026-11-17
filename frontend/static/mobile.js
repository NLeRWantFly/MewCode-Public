(() => {
  "use strict";

  function resolveInvoke() {
    const internals = window.__TAURI_INTERNALS__;
    if (internals && typeof internals.invoke === "function") {
      return (command, args) => internals.invoke(command, args || {});
    }
    const core = window.__TAURI__ && window.__TAURI__.core;
    if (core && typeof core.invoke === "function") {
      return (command, args) => core.invoke(command, args);
    }
    return null;
  }

  function resolveEventApi() {
    return window.__TAURI__ && window.__TAURI__.event;
  }

  // Tauri v2 rejects a failed command with the raw error payload (usually the
  // string returned by `Err(String)`), not with an Error instance. Reading
  // `error.message` therefore yields undefined and the real cause gets buried
  // behind the fallback copy, so extract it from every shape Tauri may use.
  function errorMessage(error, fallback) {
    if (typeof error === "string" && error.trim()) return error;
    if (error && typeof error.message === "string" && error.message.trim()) return error.message;
    if (error && typeof error.error === "string" && error.error.trim()) return error.error;
    if (error != null && typeof error === "object") {
      try {
        const serialized = JSON.stringify(error);
        if (serialized && serialized !== "{}") return serialized;
      } catch (_) { /* fall through to the fallback */ }
    }
    return fallback;
  }

  function installBridge(invoke) {
    if (window.mewcodeDesktop) return window.mewcodeDesktop;
    resolveEventApi();
    window.mewcodeDesktop = Object.freeze({
      runtime: "tauri-mobile",
      mobileBackendUrl: () => invoke("mobile_backend_url"),
      mobilePairingState: () => invoke("mobile_pairing_state"),
      mobileDiscoverNodes: () => invoke("mobile_discover_nodes"),
      mobileProbeHost: (host) => invoke("mobile_probe_host", { host }),
      mobileRequestPairing: (endpoint, nodeId, label) => invoke("mobile_request_pairing", { endpoint, nodeId, label }),
      mobilePollPairing: (endpoint, requestId) => invoke("mobile_poll_pairing", { endpoint, requestId }),
      mobileSetActivePeer: (endpoint) => invoke("mobile_set_active_peer", { endpoint }),
      mobileClearPairing: (endpoint) => invoke("mobile_clear_pairing", { endpoint }),
      mobileRemoteRequest: (path, method = "GET", payload) => invoke("mobile_remote_request", { path, method, payload }),
    });
    return window.mewcodeDesktop;
  }

  function waitForInvoke(timeoutMs = 8000) {
    return new Promise((resolve, reject) => {
      const start = Date.now();
      const tick = () => {
        const invoke = resolveInvoke();
        if (invoke) {
          resolve(invoke);
          return;
        }
        if (Date.now() - start >= timeoutMs) {
          reject(new Error("Tauri IPC is not available"));
          return;
        }
        setTimeout(tick, 50);
      };
      tick();
    });
  }

  function initMobileRuntime(bridge) {
    if (window.__mewcodeMobileStarted) return;
    window.__mewcodeMobileStarted = true;
    document.body.classList.add("mobile-runtime");
    const pairingPanel = document.getElementById("mobile-pairing");
    const pairingState = document.getElementById("mobile-pairing-state");
    const pairingCopy = document.getElementById("mobile-pairing-copy");
    const pairingButton = document.getElementById("mobile-pairing-button");
    const unpairButton = document.getElementById("mobile-unpair");
    const nodeList = document.getElementById("mobile-node-list");
    const peerList = document.getElementById("mobile-peer-list");
    const manualHost = document.getElementById("mobile-manual-host");
    const hostInput = document.getElementById("mobile-host-input");
    const hostConnect = document.getElementById("mobile-host-connect");
    const connectionText = document.getElementById("mobile-connection-text");
    const connectionDot = document.getElementById("mobile-connection-dot");
    const historyEl = document.getElementById("mobile-history");
    const historyCount = document.getElementById("mobile-history-count");
    const detailSheet = document.getElementById("mobile-detail-sheet");
    const detailBody = document.getElementById("mobile-detail-body");
    const detailTitle = document.getElementById("mobile-detail-title");
    const detailMeta = document.getElementById("mobile-detail-meta");
    const detailBack = document.getElementById("mobile-detail-back");
    const syncChip = document.getElementById("mobile-sync-chip");
    const composer = document.getElementById("mobile-composer");
    const composerStatus = document.getElementById("mobile-send-status");
    const composerInput = document.getElementById("mobile-composer-input");
    const composerSend = document.getElementById("mobile-composer-send");
    const composerInterrupt = document.getElementById("mobile-composer-interrupt");
    const newChatButton = document.getElementById("mobile-new-chat");
    const orchBoard = document.getElementById("mobile-orch-board");
    const orchTitle = document.getElementById("mobile-orch-title");
    const orchStatus = document.getElementById("mobile-orch-status");
    const orchSteps = document.getElementById("mobile-orch-steps");
    const interruptButton = document.getElementById("mobile-interrupt");
    const resumeButton = document.getElementById("mobile-resume");
    const promptSheet = document.getElementById("mobile-prompt-sheet");
    const promptBadge = document.getElementById("mobile-prompt-badge");
    const promptQuestion = document.getElementById("mobile-prompt-question");
    const promptContext = document.getElementById("mobile-prompt-context");
    const promptOptions = document.getElementById("mobile-prompt-options");
    const promptOther = document.getElementById("mobile-prompt-other");
    const promptAllow = document.getElementById("mobile-prompt-allow");
    const promptAlways = document.getElementById("mobile-prompt-always");
    const promptDeny = document.getElementById("mobile-prompt-deny");
    const promptSubmit = document.getElementById("mobile-prompt-submit");
    const promptCancel = document.getElementById("mobile-prompt-cancel");
    let paired = false;
    let waiting = false;
    let connecting = false;
    let latestDiscovery = null;
    let activeConversationId = "";
    let openConversation = null;
    let sending = false;
    let pollTimer = 0;
    let operationId = "";
    let knownMessageCount = 0;
    let liveState = null;
    let currentPrompt = null;
    let promptChoice = "";
    let activeEndpoint = "";
    let historyRequestSeq = 0;
    let historyLoading = false;
    let detailRequestSeq = 0;
    const composerDrafts = new Map();
    const draftKeyFor = (conversation) => JSON.stringify([
      activeEndpoint,
      conversation?.projectId || "",
      conversation?.scope || "",
      conversation?.id || "",
    ]);
    const saveComposerDraft = () => {
      if (openConversation && composerInput) {
        composerDrafts.set(draftKeyFor(openConversation), composerInput.value);
      }
    };

    const sourceLabel = (source) => source === "claude-code" ? "Claude Code" : source === "codex" ? "Codex" : "MewCode";
    const HISTORY_SOURCE_ORDER = ["mewcode"];
    function historySourceRank(source) {
      const index = HISTORY_SOURCE_ORDER.indexOf(source);
      return index < 0 ? HISTORY_SOURCE_ORDER.length : index;
    }
    const escapeHtml = (value) => String(value ?? "").replace(/[&<>\"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[char]));
    const setConnection = (text, state = "idle") => {
      connectionText.textContent = text;
      connectionDot.dataset.state = state;
    };
    function setComposerStatus(text, state = "pending") {
      if (!composerStatus) return;
      composerStatus.textContent = text;
      composerStatus.dataset.state = state;
      composerStatus.classList.toggle("hidden", !text);
    }
    const nodeStatusText = (node) => {
      if (node.selectable) return node.url || "可连接";
      if (node.status === "offline") return "离线";
      if (node.status === "timeout") return "探测超时";
      return "不可达";
    };
    const isMewcode = (item) => String(item?.source || "mewcode") === "mewcode";

    function renderManualHost() {
      if (!manualHost) return;
      const show = !paired;
      manualHost.classList.toggle("hidden", !show);
      if (hostConnect) hostConnect.disabled = waiting || connecting;
      if (hostInput) hostInput.disabled = waiting || connecting;
    }

    function renderPeers(status) {
      const peers = status?.peers || [];
      if (!peerList) return;
      if (!peers.length) {
        peerList.innerHTML = "";
        return;
      }
      peerList.innerHTML = peers.map((item) =>
        `<button class="mobile-node-row" type="button" data-endpoint="${escapeHtml(item.endpoint || "")}">` +
        `<span class="mobile-history-main"><strong>${escapeHtml(item.label || item.endpoint || "已配对电脑")}</strong>` +
        `<small>${item.endpoint === status.endpoint ? "当前连接" : "点选切换"}</small></span></button>`
      ).join("");
      peerList.querySelectorAll(".mobile-node-row").forEach((row) => {
        row.addEventListener("click", async () => {
          closeDetail();
          paired = false;
          historyRequestSeq += 1;
          detailRequestSeq += 1;
          syncActionState();
          const next = await bridge.mobileSetActivePeer(row.dataset.endpoint);
          renderPairing(next);
          await loadHistory();
        });
      });
    }

    function renderPairing(status) {
      const endpoint = String(status?.endpoint || "");
      if ((status?.paired && endpoint && activeEndpoint && endpoint !== activeEndpoint) || (!status?.paired && activeEndpoint && "endpoint" in (status || {}))) {
        closeDetail();
        historyRequestSeq += 1;
        detailRequestSeq += 1;
        activeConversationId = "";
        historyEl.innerHTML = "<div class='mobile-empty'>配对后会显示电脑上的 MewCode 对话</div>";
        historyCount.textContent = "0";
      }
      if (endpoint) activeEndpoint = endpoint;
      else if (status && !status.paired && "endpoint" in status) activeEndpoint = "";
      paired = !!status?.paired;
      pairingState.textContent = paired ? "已连接" : (waiting ? "等待批准" : "未配对");
      pairingState.dataset.state = paired ? "connected" : (waiting ? "pending" : "idle");
      pairingButton.textContent = waiting ? "等待电脑批准…" : "刷新附近电脑";
      pairingButton.disabled = waiting;
      unpairButton.classList.toggle("hidden", !paired);
      pairingPanel?.classList.toggle("is-collapsed", paired && !waiting);
      pairingCopy.textContent = waiting
        ? "已向电脑发送配对请求，请在电脑上选择允许本次或始终允许。"
        : paired
          ? `已连接到 ${status.endpoint}。可在下方切换已配对电脑。`
          : "请填写电脑的 MagicDNS 域名（以 .ts.net 结尾）。可在电脑端「手机设备」设置里查看。";
      renderPeers(status);
      renderManualHost();
      if (newChatButton) newChatButton.disabled = !paired || waiting || connecting;
      syncActionState();
    }

    function renderNodes(discovery) {
      if (!nodeList) return;
      latestDiscovery = discovery || null;
      const error = discovery?.error || "";
      const nodes = discovery?.nodes || [];
      if (!discovery?.available) {
        nodeList.innerHTML = `<div class="mobile-empty">${escapeHtml(error || "Tailscale 不可用")}</div>`;
        renderManualHost();
        return;
      }
      if (!discovery.connected) {
        nodeList.innerHTML = `<div class="mobile-empty">${escapeHtml(error || "Tailscale 未连接")}</div>`;
        renderManualHost();
        return;
      }
      if (!nodes.length) {
        nodeList.innerHTML = `<div class="mobile-empty">${escapeHtml(error || "附近没有可列出的电脑")}</div>`;
        renderManualHost();
        return;
      }
      nodeList.innerHTML = nodes.map((node) =>
        `<button class="mobile-node-row" type="button" ${node.selectable ? "" : "disabled"} data-url="${escapeHtml(node.url || "")}" data-id="${escapeHtml(node.id || "")}" data-label="${escapeHtml(node.hostname || node.id || "")}">` +
        `<span class="mobile-history-main"><strong>${escapeHtml(node.hostname || node.id || "未命名节点")}</strong><small>${escapeHtml(nodeStatusText(node))}</small></span></button>`
      ).join("");
      nodeList.querySelectorAll(".mobile-node-row").forEach((row) => {
        row.addEventListener("click", () => requestPairing(row.dataset.url, row.dataset.id, row.dataset.label));
      });
      renderManualHost();
    }

    function renderHistory(items, meta = {}) {
      activeConversationId = String(meta.active_conversation_id || "");
      const openGroups = new Map();
      historyEl.querySelectorAll("details[data-group-key]").forEach((node) => {
        openGroups.set(node.dataset.groupKey, !!node.open);
      });
      const groups = new Map();
      const groupFor = (key, label, path) => {
        if (!groups.has(key)) groups.set(key, { key, label, path, rows: [] });
        return groups.get(key);
      };
      (meta.projects || []).forEach((project) => {
        const dir = String(project.dir || "");
        const key = `project:${String(project.id || dir)}`;
        groupFor(key, String(project.label || dir || "未指定项目"), dir);
      });
      const ranked = (items || []).filter(isMewcode).slice().sort((left, right) => {
        const rank = historySourceRank(String(left.source || "mewcode"))
          - historySourceRank(String(right.source || "mewcode"));
        if (rank) return rank;
        return String(right.updated_at || "").localeCompare(String(left.updated_at || ""));
      });
      ranked.forEach((item) => {
        const scope = String(item.scope || "project");
        const path = String(item.project_path || "");
        const projectId = String(item.project_id || "");
        if (scope === "user" || scope === "normal" || (!projectId && !path)) {
          groupFor("user:普通对话", "普通对话", "").rows.push(item);
          return;
        }
        const key = `project:${projectId || path || String(item.parent_folder || "")}`;
        const label = String(
          path.split(/[\\/]/).filter(Boolean).pop()
            || item.parent_folder || path || "未指定项目"
        );
        groupFor(key, label, path || String(item.parent_folder || "")).rows.push(item);
      });
      historyCount.textContent = String(ranked.length);
      if (!groups.size) {
        historyEl.innerHTML = "<div class='mobile-empty'>配对后会显示电脑上的 MewCode 对话</div>";
        return;
      }
      const folderHtml = (group) => {
        const open = openGroups.has(group.key) ? openGroups.get(group.key) : true;
        const rows = group.rows.map((item) => {
          const id = String(item.external_id || "");
          const itemProject = String(item.project_id || "");
          const selected = openConversation && openConversation.id === id
            && (!openConversation.projectId || openConversation.projectId === itemProject) ? " is-open" : "";
          const active = item.active ? " is-active" : "";
          const running = item.running ? " is-running" : "";
          const orch = item.execution_kind === "orchestration" ? " is-orch" : "";
          const resumable = item.resumable ? " is-resumable" : "";
          return `<button class="mobile-history-row${selected}${active}${running}${orch}${resumable}" type="button" data-source="mewcode" data-id="${escapeHtml(id)}" data-title="${escapeHtml(item.title || "未命名对话")}" data-scope="${escapeHtml(item.scope || "project")}" data-project-id="${escapeHtml(itemProject)}"><span class="mobile-history-main"><strong>${escapeHtml(item.title || "未命名对话")}</strong><small>${escapeHtml(item.updated_at || "")} · ${Number(item.message_count || 0)} 条消息</small></span><span class="mobile-chevron">›</span></button>`;
        }).join("");
        const body = group.rows.length
          ? `<div class="mobile-folder-rows">${rows}</div>`
          : `<div class="mobile-empty">此项目还没有对话</div>`;
        return `<details class="mobile-folder-group" data-group-key="${escapeHtml(group.key)}"${open ? " open" : ""}><summary class="mobile-folder-heading" title="${escapeHtml(group.path || group.label)}">${escapeHtml(group.label)}</summary>${body}</details>`;
      };
      const source = "mewcode";
      const sourceOpen = openGroups.has("source:mewcode") ? openGroups.get("source:mewcode") : true;
      const total = Array.from(groups.values()).flatMap((group) => group.rows).length;
      historyEl.innerHTML =
        `<details class="mobile-source-group" data-group-key="source:mewcode"${sourceOpen ? " open" : ""}><summary class="mobile-source-heading"><strong>${escapeHtml(sourceLabel(source))}</strong><span>${total} 条</span></summary>` +
        Array.from(groups.values()).map(folderHtml).join("") +
        `</details>`;
      historyEl.querySelectorAll(".mobile-history-row").forEach((row) => {
        row.addEventListener("click", () => showDetail(
          row.dataset.source,
          row.dataset.id,
          row.dataset.title || "",
          row.dataset.scope || "project",
          row.dataset.projectId || "",
        ));
      });
      syncOpenBadge();
    }

    function closeDetail() {
      saveComposerDraft();
      openConversation = null;
      operationId = "";
      liveState = null;
      setComposerStatus("");
      hidePromptSheet();
      orchBoard?.classList.add("hidden");
      detailSheet?.classList.add("hidden");
      if (composerInput) composerInput.value = "";
      syncOpenBadge();
      syncActionState();
    }

    function renderMobileMarkdown(text) {
      if (window.MewMarkdown?.renderMarkdown) return window.MewMarkdown.renderMarkdown(text);
      return `<pre style="white-space:pre-wrap">${escapeHtml(text || "")}</pre>`;
    }

    function speechMessages(detail) {
      return (detail.messages || []).filter((message) => !message.kind || message.kind === "message");
    }

    const STAGE_EVENT_TYPES = new Set([
      "expert_stage", "expert_step",
      "expert_node_started", "expert_node_completed", "expert_node_delta",
      "expert_node_thinking", "expert_member_started", "expert_member_completed",
      "expert_member_delta", "expert_pipeline_step_started",
      "expert_pipeline_step_completed", "expert_pipeline_step_output",
      "expert_delta",
    ]);
    const TOOL_EVENT_TYPES = new Set([
      "expert_tool", "expert_tool_start", "expert_tool_result", "expert_tool_wait",
      "expert_tool_state", "expert_node_tool", "expert_member_tool",
      "expert_pipeline_step_tool", "tool", "tool_result",
    ]);

    function toolArgObject(args) {
      if (args && typeof args === "object") return args;
      if (typeof args === "string") {
        try {
          const parsed = JSON.parse(args);
          return parsed && typeof parsed === "object" ? parsed : {};
        } catch (_) {
          return {};
        }
      }
      return {};
    }

    function toolLineSummary(name, args) {
      const parsed = toolArgObject(args);
      const path = parsed.path || parsed.file_path || parsed.file || parsed.target || parsed.filename || "";
      const command = parsed.command || parsed.cmd || parsed.script || "";
      const pattern = parsed.pattern || parsed.query || parsed.grep || "";
      if (path) return String(name || "tool") + " " + String(path).split(/[\\/]/).slice(-2).join("/");
      if (command) {
        const cmd = String(command).replace(/\s+/g, " ").trim();
        return String(name || "tool") + " " + (cmd.length > 64 ? cmd.slice(0, 64) + "…" : cmd);
      }
      if (pattern) return String(name || "tool") + " " + String(pattern).slice(0, 48);
      return String(name || "tool");
    }

    function upsertToolRow(tools, event, type) {
      const id = String(event.id || event.tool_id || event.name || tools.length);
      let row = tools.find((item) => String(item.id) === id);
      if (!row) {
        row = {
          id,
          name: event.name || event.tool || "tool",
          args: event.args || event.arguments || {},
          result: "",
          is_error: false,
          done: false,
        };
        tools.push(row);
      }
      if (event.name || event.tool) row.name = event.name || event.tool;
      if (event.args || event.arguments) row.args = event.args || event.arguments;
      const finished = type.includes("result")
        || event.output
        || event.result
        || event.status === "completed"
        || event.status === "failed";
      if (finished) {
        row.result = String(event.output || event.result || row.result || "").slice(0, 4000);
        row.is_error = !!event.is_error;
        row.done = true;
      }
      return row;
    }

    function renderToolsHtml(tools) {
      if (!tools || !tools.length) return "";
      const rows = tools.map((tool) => {
        const summary = toolLineSummary(tool.name, tool.args);
        const done = tool.done === true || !!tool.result;
        const status = tool.is_error ? "失败" : (done ? "完成" : "执行中");
        const argsObj = toolArgObject(tool.args);
        const argsText = Object.keys(argsObj).length
          ? JSON.stringify(argsObj, null, 2)
          : String(tool.args || "");
        const body = escapeHtml(argsText.slice(0, 2000))
          + (tool.result ? "\n\n→ " + escapeHtml(String(tool.result).slice(0, 2000)) : "");
        return `<details class="tg-row${tool.is_error ? " is-error" : ""}" data-done="${done ? "1" : "0"}">`
          + `<summary><span class="tool-name">${escapeHtml(summary)}</span>`
          + `<span class="tool-elapsed${tool.is_error ? " tool-error" : ""}">${status}</span></summary>`
          + `<div class="tool-body">${body}</div></details>`;
      }).join("");
      const running = tools.some((tool) => tool.done !== true && !tool.result);
      const count = tools.length;
      const title = count === 1
        ? toolLineSummary(tools[0].name, tools[0].args)
        : (running ? `运行中 · ${count} 个工具` : `已运行 ${count} 个工具`);
      return `<details class="tool-group${running ? " is-running" : ""}">`
        + `<summary><span class="tg-icon">工</span><span class="tg-title">${escapeHtml(title)}</span>`
        + `<span class="tg-status">${running ? "…" : "完成"}</span></summary>`
        + `<div class="tg-body">${rows}</div></details>`;
    }

    function stageBubbles(events) {
      const bubbles = [];
      const index = new Map();
      (events || []).forEach((event) => {
        const type = String(event?.type || "");
        const isTool = TOOL_EVENT_TYPES.has(type);
        if (!STAGE_EVENT_TYPES.has(type) && !isTool) return;
        if ((type === "tool" || type === "tool_result") && !event.node_id && !event.member_id && !event.role) return;
        const key = [event.run_id || "", event.node_id || event.role || event.label || "stage", event.member_id || ""].join("::");
        let bubble = index.get(key);
        if (!bubble) {
          bubble = {
            label: event.label || event.node_id || event.role || event.member_id || "阶段",
            model: event.model || "",
            status: event.status || "",
            text: "",
            tools: [],
            fromSnapshot: false,
          };
          index.set(key, bubble);
          bubbles.push(bubble);
        }
        if (event.label) bubble.label = event.label;
        if (event.model) bubble.model = event.model;
        if (event.status) bubble.status = event.status;
        if (isTool) upsertToolRow(bubble.tools, event, type);
        if (event.text && !isTool) {
          if (event.snapshot_aggregate) {
            bubble.text = event.text;
            bubble.fromSnapshot = true;
          } else if ((type.endsWith("_delta") || type.endsWith("_output")) && !bubble.fromSnapshot) {
            bubble.text = (bubble.text || "") + event.text;
          } else if (!bubble.fromSnapshot || !type.endsWith("_delta")) {
            bubble.text = event.text;
          }
        }
      });
      return bubbles.filter((item) => String(item.text || "").trim() || item.tools.length);
    }

    function chatToolsFromEvents(events) {
      const tools = [];
      (events || []).forEach((event) => {
        const type = String(event?.type || "");
        if (type !== "tool" && type !== "tool_result") return;
        if (event.node_id || event.member_id || event.role) return;
        upsertToolRow(tools, event, type);
      });
      return tools;
    }

    function pendingChatTools(speech, liveTools, extraTools) {
      const pending = [];
      const seen = new Set();
      (speech || []).forEach((message) => {
        (message.tools || []).forEach((tool) => {
          seen.add(`${tool.name}\0${JSON.stringify(tool.args || {})}`);
        });
      });
      [...(liveTools || []), ...(extraTools || [])].forEach((tool) => {
        const key = `${tool.name}\0${JSON.stringify(tool.args || {})}`;
        if (seen.has(key)) return;
        seen.add(key);
        pending.push(tool);
      });
      return pending;
    }

    function conversationRunning() {
      return !!(operationId || openConversation?.running || liveState?.running);
    }

    function canInterruptConversation() {
      return conversationRunning() || String(liveState?.expert_status || "") === "running";
    }

    function syncActionState() {
      const running = conversationRunning();
      const canInterrupt = canInterruptConversation();
      if (composerSend) {
        composerSend.disabled = sending || !paired || !openConversation;
        composerSend.classList.toggle("is-busy", sending);
        composerSend.textContent = sending ? "…" : "↑";
      }
      if (composerInput) composerInput.disabled = !paired || !openConversation;
      composerInterrupt?.classList.toggle("hidden", !canInterrupt);
      if (interruptButton) interruptButton.disabled = !canInterrupt || sending;
      if (composerInterrupt) composerInterrupt.disabled = !canInterrupt || sending;
      if (resumeButton) {
        const resumable = !!(liveState?.resumable) && !running;
        resumeButton.classList.toggle("hidden", !resumable);
        resumeButton.disabled = sending || !resumable;
      }
    }

    function renderOrchBoard(live) {
      const running = conversationRunning();
      const orch = !!(live && (live.execution_kind === "orchestration" || (live.steps || []).length || live.resumable));
      orchBoard?.classList.toggle("hidden", !orch && !running && !canInterruptConversation());
      if (orchTitle) orchTitle.textContent = orch ? "专家团" : "对话";
      const runningCount = (live?.steps || []).filter((step) => step.status === "running").length;
      const status = running
        ? (runningCount > 1 ? `${runningCount} 个并行进行中` : "进行中")
        : (live?.resumable ? "可继续" : (live?.expert_status || ""));
      if (orchStatus) orchStatus.textContent = status;
      const activeIds = new Set(live?.run?.active_node_ids || []);
      if (orchSteps) {
        orchSteps.classList.toggle("hidden", !(live?.steps || []).length);
        orchSteps.innerHTML = (live?.steps || []).map((step) => {
          const stepRunning = step.status === "running"
            || (running && (activeIds.has(step.id) || activeIds.has(String(step.id || "").replace(/^member:/, ""))));
          const state = stepRunning ? "running" : (step.status || "pending");
          const kind = step.kind === "member" ? " is-member" : "";
          return `<li class="is-${escapeHtml(state)}${kind}"><span>${escapeHtml(step.label || step.id || "步骤")}</span></li>`;
        }).join("");
      }
      syncActionState();
    }

    function hidePromptSheet() {
      currentPrompt = null;
      promptChoice = "";
      promptSheet?.classList.add("hidden");
    }

    function setPromptMode(kind) {
      const permission = kind === "permission";
      const choose = kind === "human_oncall" || kind === "plan_revision" || kind === "ask_user";
      promptAllow?.classList.toggle("hidden", !permission);
      promptAlways?.classList.toggle("hidden", !permission);
      promptDeny?.classList.toggle("hidden", !permission);
      promptSubmit?.classList.toggle("hidden", permission);
      promptCancel?.classList.toggle("hidden", permission);
      if (promptSubmit) promptSubmit.textContent = kind === "ask_user" ? "提交回答" : kind === "plan_revision" ? "批准修订" : "确认选择";
    }

    function renderPrompt(live) {
      const pending = (live?.pending || [])[0];
      if (!pending) {
        hidePromptSheet();
        return;
      }
      currentPrompt = pending;
      const kind = String(pending.type || "");
      setPromptMode(kind);
      promptSheet?.classList.remove("hidden");
      if (kind === "permission") {
        if (promptBadge) promptBadge.textContent = "需要你的权限";
        if (promptQuestion) promptQuestion.textContent = pending.description || pending.tool || "确认这次操作";
        if (promptContext) {
          promptContext.textContent = pending.reason || "";
          promptContext.classList.toggle("hidden", !pending.reason);
        }
        if (promptOptions) promptOptions.innerHTML = "";
        promptOther?.classList.add("hidden");
        return;
      }
      if (kind === "plan_revision") {
        const proposal = pending.proposal || {};
        if (promptBadge) promptBadge.textContent = "需要批准计划修订";
        if (promptQuestion) promptQuestion.textContent = proposal.summary || "需要你批准一项计划修订";
        if (promptContext) {
          promptContext.textContent = proposal.why || "";
          promptContext.classList.toggle("hidden", !proposal.why);
        }
        if (promptOptions) promptOptions.innerHTML = "";
        if (promptOther) {
          promptOther.classList.remove("hidden");
          promptOther.placeholder = "补充信息（可选）";
        }
        return;
      }
      const questions = pending.questions || [];
      const first = questions[0] || {};
      if (promptBadge) promptBadge.textContent = kind === "ask_user" ? "需要你的回答" : "需要你的决定";
      if (promptQuestion) promptQuestion.textContent = first.question || first.message || "请选择下一步";
      if (promptContext) {
        promptContext.textContent = first.context || "";
        promptContext.classList.toggle("hidden", !first.context);
      }
      const options = first.options || [];
      if (promptOptions) {
        promptOptions.innerHTML = options.map((option) => {
          const value = String(option.value ?? option.label ?? "");
          return `<button type="button" class="mobile-prompt-option" data-value="${escapeHtml(value)}" role="radio" aria-checked="false">${escapeHtml(option.label || value)}</button>`;
        }).join("");
        promptOptions.querySelectorAll("button").forEach((button) => {
          button.addEventListener("click", () => {
            promptChoice = button.dataset.value || "";
            promptOptions.querySelectorAll("button").forEach((item) => {
              item.setAttribute("aria-checked", item === button ? "true" : "false");
            });
            const isOther = promptChoice === "__other__";
            promptOther?.classList.toggle("hidden", !isOther && kind !== "ask_user");
          });
        });
      }
      const needText = kind === "ask_user" || !options.length;
      promptOther?.classList.toggle("hidden", !needText);
    }

    async function replyPrompt(payload) {
      const target = openConversation;
      if (!target || !currentPrompt || sending) return;
      setSending(true);
      try {
        await bridge.mobileRemoteRequest("/api/remote/prompts", "POST", {
          conversation_id: target.id,
          project_id: target.projectId || "",
          kind: currentPrompt.type,
          request_id: currentPrompt.request_id || "",
          run_id: currentPrompt.run_id || liveState?.run_id || "",
          ...payload,
        });
        if (openConversation !== target) return;
        hidePromptSheet();
      } catch (error) {
        if (openConversation !== target) return;
        const message = errorMessage(error, "提交失败");
        setComposerStatus(`发送失败：${message}`, "error");
        setConnection(message, "error");
        return;
      } finally {
        setSending(false);
      }
      await refreshOpenConversation();
    }

    async function interruptConversation() {
      const target = openConversation;
      if (!target || sending) return;
      setSending(true);
      try {
        await bridge.mobileRemoteRequest("/api/remote/conversations/interrupt", "POST", {
          conversation_id: target.id,
          project_id: target.projectId || "",
        });
        if (openConversation !== target) return;
        operationId = "";
        target.running = false;
      } catch (error) {
        if (openConversation !== target) return;
        const message = errorMessage(error, "无法暂停");
        setComposerStatus(`发送失败：${message}`, "error");
        setConnection(message, "error");
        return;
      } finally {
        setSending(false);
      }
      await refreshOpenConversation();
    }

    async function resumeConversation() {
      const target = openConversation;
      if (!target || sending) return;
      setSending(true);
      try {
        await bridge.mobileRemoteRequest("/api/remote/conversations/resume", "POST", {
          conversation_id: target.id,
          project_id: target.projectId || "",
          run_id: liveState?.run_id || "",
        });
        if (openConversation !== target) return;
      } catch (error) {
        if (openConversation !== target) return;
        const message = errorMessage(error, "无法继续专家团");
        setComposerStatus(`发送失败：${message}`, "error");
        setConnection(message, "error");
        return;
      } finally {
        setSending(false);
      }
      await refreshOpenConversation();
    }

    function renderSpeech(speech, extraAssistant = "", stages = [], extraTools = []) {
      const articles = [];
      (speech || []).forEach((message) => {
        const text = String(message.text || "").trim();
        const tools = message.tools || [];
        if (!text && !tools.length) return;
        const role = String(message.role || "").toLowerCase() === "user" ? "user" : "assistant";
        const md = text ? `<div class="md">${renderMobileMarkdown(message.text || "")}</div>` : "";
        articles.push(`<article class="msg ${role}"><div class="msg-bubble">${md}${renderToolsHtml(tools)}</div></article>`);
      });
      stages.forEach((stage) => {
        const text = String(stage.text || "").trim();
        const toolsHtml = renderToolsHtml(stage.tools || []);
        if (!text && !toolsHtml) return;
        const md = text ? `<div class="md">${renderMobileMarkdown(stage.text || "")}</div>` : "";
        articles.push(
          `<article class="msg assistant orch-stage-msg"${stage.status ? ` data-status="${escapeHtml(stage.status)}"` : ""}>` +
          `<div class="msg-bubble"><div class="orch-stage-msg-head"><strong>${escapeHtml(stage.label || "阶段")}</strong>` +
          `<span class="orch-stage-msg-model">${escapeHtml(stage.model || "")}</span></div>` +
          `${md}${toolsHtml}</div></article>`
        );
      });
      if (extraAssistant || extraTools.length) {
        const md = extraAssistant ? `<div class="md">${renderMobileMarkdown(extraAssistant)}</div>` : "";
        articles.push(`<article class="msg assistant is-live"><div class="msg-bubble">${md}${renderToolsHtml(extraTools)}</div></article>`);
      }
      if (!articles.length) {
        detailBody.innerHTML = "<div class='mobile-empty'>该对话还没有消息。在下方输入，电脑会接着跑。</div>";
        return;
      }
      detailBody.innerHTML = articles.join("");
      window.MewMarkdown?.enhanceMath?.(detailBody);
      detailBody.scrollTop = detailBody.scrollHeight;
    }

    function syncOpenBadge() {
      const liveRunning = !!(liveState && liveState.running);
      const live = !!(openConversation && (openConversation.active || openConversation.running || liveRunning));
      syncChip?.classList.toggle("is-on", live);
      if (syncChip) {
        syncChip.textContent = (openConversation?.running || liveRunning)
          ? (liveState?.execution_kind === "orchestration" ? "专家团进行中" : "电脑正在回复")
          : (liveState?.resumable ? "专家团可继续" : "电脑协同中");
      }
    }

    function setSending(on) {
      sending = !!on;
      syncActionState();
    }

    async function showDetail(source, externalId, titleHint, scope, projectId = "") {
      if (!detailSheet || !detailBody) return;
      saveComposerDraft();
      openConversation = {
        id: externalId,
        source: "mewcode",
        title: titleHint,
        scope: scope || "project",
        projectId: projectId || "",
        active: false,
        running: false,
      };
      detailRequestSeq += 1;
      operationId = "";
      liveState = null;
      knownMessageCount = 0;
      setComposerStatus("");
      if (composerInput) {
        composerInput.value = composerDrafts.get(draftKeyFor(openConversation)) || "";
      }
      detailSheet.classList.remove("hidden");
      if (detailTitle) detailTitle.textContent = titleHint || "对话";
      if (detailMeta) detailMeta.textContent = "MewCode";
      detailBody.innerHTML = "<div class='mobile-empty'>加载对话中…</div>";
      syncActionState();
      syncOpenBadge();
      await refreshOpenConversation();
    }

    async function refreshOpenConversation() {
      const target = openConversation;
      if (!target || !paired) return;
      const requestSeq = ++detailRequestSeq;
      const projectQuery = target.projectId ? `&project_id=${encodeURIComponent(target.projectId)}` : "";
      try {
        const query = `?source=mewcode&external_id=${encodeURIComponent(target.id)}&speech_only=true${projectQuery}`;
        const detail = await bridge.mobileRemoteRequest(`/api/remote/history/item${query}`);
        if (openConversation !== target || !paired || requestSeq !== detailRequestSeq) return;
        const speech = speechMessages(detail);
        knownMessageCount = speech.length;
        target.title = detail.title || target.title;
        target.scope = detail.scope || target.scope;
        target.projectId = target.projectId || String(detail.project_id || "");
        target.active = !!detail.active;
        target.running = !!detail.running;
        if (detailTitle) detailTitle.textContent = target.title || "对话";
        if (detailMeta) detailMeta.textContent = `${speech.length} 条 · ${target.active ? "电脑当前对话" : "可协同"}`;
        let extra = "";
        let extraTools = [];
        const polledOperationId = operationId;
        if (polledOperationId) {
          try {
            const operation = await bridge.mobileRemoteRequest(`/api/remote/operations/${encodeURIComponent(polledOperationId)}`);
            if (openConversation !== target || !paired || requestSeq !== detailRequestSeq) return;
            (operation.events || []).forEach((event) => {
              if (event && event.type === "delta" && event.text) extra += event.text;
              const type = String(event?.type || "");
              if (type === "tool" || type === "tool_result") upsertToolRow(extraTools, event, type);
            });
            const failed = [...(operation.events || [])].reverse().find((event) => event && event.type === "error");
            if (failed && !sending && operationId === polledOperationId) {
              setComposerStatus(`发送失败：${errorMessage(failed.text || failed.error || failed.message, "电脑端处理出错")}`, "error");
            }
            if (operation.status && operation.status !== "running") {
              if (operationId === polledOperationId) operationId = "";
              target.running = false;
            } else {
              target.running = true;
            }
          } catch (_) { /* keep last rendered speech */ }
        }
        if (openConversation !== target || !paired || requestSeq !== detailRequestSeq) return;
        let nextLive = null;
        try {
          nextLive = await bridge.mobileRemoteRequest(`/api/remote/live?conversation_id=${encodeURIComponent(target.id)}${projectQuery}`);
        } catch (_) {
          nextLive = null;
        }
        if (openConversation !== target || !paired || requestSeq !== detailRequestSeq) return;
        liveState = nextLive;
        if (liveState?.running) target.running = true;
        if (liveState && liveState.running === false && !operationId) target.running = false;
        const liveTools = chatToolsFromEvents(liveState?.events || []);
        const pendingTools = pendingChatTools(speech, liveTools, extraTools);
        renderOrchBoard(liveState);
        renderPrompt(liveState);
        renderSpeech(speech, extra, stageBubbles(liveState?.events || []), pendingTools);
        syncOpenBadge();
        syncActionState();
      } catch (error) {
        if (openConversation !== target || !paired || requestSeq !== detailRequestSeq) return;
        const detail = errorMessage(error, "");
        detailBody.innerHTML = `<div class="mobile-empty">${escapeHtml(detail || "远程请求失败，可重试")}</div>`;
        setConnection(detail || "远程请求失败，可重试", "error");
      }
    }

    async function sendPrompt() {
      const target = openConversation;
      const submitted = composerInput ? composerInput.value : "";
      const text = submitted.trim();
      if (!text || !target || sending || !paired) return;
      const draftKey = draftKeyFor(target);
      detailRequestSeq += 1;
      setSending(true);
      setComposerStatus("正在发送到电脑…", "pending");
      try {
        const result = await bridge.mobileRemoteRequest(
          `/api/remote/conversations/message?conversation_id=${encodeURIComponent(target.id)}`,
          "POST",
          { text, project_id: target.projectId || "" }
        );
        if (result?.error) throw new Error(result.error);
        if (!result?.queued && !result?.operation_id) {
          throw new Error("电脑未确认接收消息，请刷新对话核对后重试");
        }
        if (result.conversation_id && String(result.conversation_id) !== target.id) {
          throw new Error("电脑端当前对话已切换，请重试");
        }
        if (composerDrafts.get(draftKey) === submitted) composerDrafts.delete(draftKey);
        if (openConversation !== target) return;
        if (composerInput && composerInput.value === submitted) composerInput.value = "";
        if (result.queued) {
          setComposerStatus("已排队，当前回复结束后继续", "connected");
        } else {
          operationId = String(result.operation_id || "");
          target.running = true;
          setComposerStatus("已发送，等待电脑回复", "connected");
        }
        target.active = true;
        activeConversationId = target.id;
        syncOpenBadge();
      } catch (error) {
        if (openConversation !== target) return;
        const message = errorMessage(error, "发送失败");
        setComposerStatus(`发送失败：${message}`, "error");
        setConnection(message, "error");
        return;
      } finally {
        setSending(false);
      }
      await refreshOpenConversation();
    }

    async function createConversation() {
      if (!paired || sending) return;
      const endpoint = activeEndpoint;
      setSending(true);
      try {
        const created = await bridge.mobileRemoteRequest("/api/remote/conversations", "POST", { scope: "project" });
        if (!paired || endpoint !== activeEndpoint) return;
        const id = String(created.id || created.conversation_id || "");
        if (!id) throw new Error("电脑没有返回新对话");
        await loadHistory();
        if (!paired || endpoint !== activeEndpoint) return;
        await showDetail("mewcode", id, "新对话", created.scope || "project", created.project_id || "");
        composerInput?.focus();
      } catch (error) {
        if (!paired || endpoint !== activeEndpoint) return;
        setConnection(errorMessage(error, "无法新建对话"), "error");
      } finally {
        setSending(false);
      }
    }

    function closeDetailIfPhone() {
      if (window.matchMedia("(min-width: 840px)").matches) return;
      closeDetail();
    }

    async function loadHistory() {
      if (!paired || historyLoading) return;
      historyLoading = true;
      const seq = ++historyRequestSeq;
      setConnection("正在读取远程记录…", "pending");
      try {
        const result = await bridge.mobileRemoteRequest("/api/remote/history?source=mewcode");
        if (seq !== historyRequestSeq || !paired) return;
        renderHistory(result.items || [], result);
        setConnection(`已连接 · ${result.node_id || "远程节点"}`, "connected");
        if (openConversation) await refreshOpenConversation();
      } catch (error) {
        if (seq !== historyRequestSeq || !paired) return;
        setConnection(errorMessage(error, "连接已断开，可立即重试"), "error");
      } finally {
        historyLoading = false;
      }
    }

    async function pollRequest(endpoint, requestId) {
      const deadline = Date.now() + 300000;
      while (Date.now() < deadline) {
        const result = await bridge.mobilePollPairing(endpoint, requestId);
        if (result?.status === "approved") return result;
        if (result?.status === "rejected") throw new Error("电脑拒绝了本次配对");
        await new Promise((resolve) => setTimeout(resolve, 1500));
      }
      throw new Error("等待批准超时");
    }

    async function requestPairing(endpoint, nodeId, label) {
      if (!endpoint || connecting) return;
      connecting = true;
      waiting = true;
      renderPairing({ paired });
      setConnection("正在发送配对请求…", "pending");
      try {
        let result = await bridge.mobileRequestPairing(endpoint, nodeId, label);
        if (result?.status === "pending") {
          setConnection("等待电脑批准…", "pending");
          result = await pollRequest(endpoint, result.request_id);
        }
        if (result?.status !== "approved" && !result?.paired) throw new Error(result?.error || "配对未完成");
        waiting = false;
        const status = await bridge.mobilePairingState();
        renderPairing(status);
        await loadHistory();
      } catch (error) {
        waiting = false;
        renderPairing({ paired });
        setConnection(errorMessage(error, "配对失败，可重试"), "error");
      } finally {
        connecting = false;
        renderManualHost();
      }
    }

    async function connectManualHost() {
      const host = (hostInput?.value || "").trim();
      if (!host || connecting || waiting) return;
      if (typeof bridge.mobileProbeHost !== "function") {
        setConnection("当前客户端不支持手动连接", "error");
        return;
      }
      setConnection("正在探测电脑…", "pending");
      renderManualHost();
      try {
        const probed = await bridge.mobileProbeHost(host);
        if (!probed?.selectable || !probed?.url) {
          throw new Error(probed?.error || "未找到 MewCode 服务");
        }
        await requestPairing(probed.url, probed.id || "", probed.label || host);
      } catch (error) {
        setConnection(errorMessage(error, "连接失败，可重试"), "error");
        renderManualHost();
      }
    }

    async function refresh() {
      try {
        const status = await bridge.mobilePairingState();
        renderPairing(status);
        renderNodes(await bridge.mobileDiscoverNodes());
        if (status.paired) await loadHistory();
      } catch (error) {
        setConnection(errorMessage(error, "移动端状态不可用"), "error");
      }
    }

    function syncKeyboardInset() {
      if (!composer || !window.visualViewport) return;
      const viewport = window.visualViewport;
      const inset = Math.max(0, window.innerHeight - viewport.height - viewport.offsetTop);
      composer.style.paddingBottom = `calc(10px + env(safe-area-inset-bottom, 0px) + ${inset}px)`;
    }

    pairingButton.addEventListener("click", refresh);
    hostConnect?.addEventListener("click", connectManualHost);
    hostInput?.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        connectManualHost();
      }
    });
    unpairButton.addEventListener("click", async () => {
      closeDetail();
      paired = false;
      historyRequestSeq += 1;
      detailRequestSeq += 1;
      syncActionState();
      await bridge.mobileClearPairing();
      waiting = false;
      renderPairing({ paired: false, endpoint: "" });
      renderHistory([]);
      setConnection("已解除设备连接", "idle");
      refresh();
    });
    document.getElementById("mobile-refresh")?.addEventListener("click", refresh);
    document.getElementById("mobile-retry")?.addEventListener("click", refresh);
    detailBack?.addEventListener("click", closeDetailIfPhone);
    newChatButton?.addEventListener("click", createConversation);
    composerSend?.addEventListener("click", sendPrompt);
    interruptButton?.addEventListener("click", interruptConversation);
    composerInterrupt?.addEventListener("click", interruptConversation);
    resumeButton?.addEventListener("click", resumeConversation);
    promptAllow?.addEventListener("click", () => replyPrompt({ decision: "allow" }));
    promptAlways?.addEventListener("click", () => replyPrompt({ decision: "allow_always" }));
    promptDeny?.addEventListener("click", () => replyPrompt({ decision: "deny" }));
    promptSubmit?.addEventListener("click", () => {
      if (!currentPrompt) return;
      const kind = String(currentPrompt.type || "");
      if (kind === "plan_revision") {
        replyPrompt({ recovery_action: "approve", recovery_info: promptOther?.value || "" });
        return;
      }
      const name = (currentPrompt.questions || [])[0]?.name || "choice";
      let answer = promptChoice;
      if (answer === "__other__" || !answer) answer = (promptOther?.value || "").trim();
      if (!answer && kind !== "ask_user") return;
      replyPrompt({ answers: { [name]: answer } });
    });
    promptCancel?.addEventListener("click", () => {
      if (!currentPrompt) return;
      const kind = String(currentPrompt.type || "");
      if (kind === "plan_revision") {
        replyPrompt({ recovery_action: "reject" });
        return;
      }
      replyPrompt({ cancelled: true, answers: {} });
    });
    composer?.addEventListener("submit", (event) => {
      event.preventDefault();
      sendPrompt();
    });
    composerInput?.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
        event.preventDefault();
        sendPrompt();
      }
    });
    window.visualViewport?.addEventListener("resize", syncKeyboardInset);
    window.visualViewport?.addEventListener("scroll", syncKeyboardInset);
    pollTimer = window.setInterval(() => {
      if (!paired) return;
      if (openConversation || document.visibilityState === "visible") {
        loadHistory();
      }
    }, 2500);
    refresh();
  }

  function startMewcodeMobileRuntime(bridge) {
    const invoke = resolveInvoke();
    const resolved = bridge || window.mewcodeDesktop || (invoke ? installBridge(invoke) : null);
    if (!resolved) return false;
    initMobileRuntime(resolved);
    return true;
  }

  window.startMewcodeMobileRuntime = startMewcodeMobileRuntime;
  window.initMobileRuntime = initMobileRuntime;

  if (!document.getElementById("drawer")) {
    waitForInvoke(8000)
      .then((invoke) => startMewcodeMobileRuntime(installBridge(invoke)))
      .catch((error) => {
        const connectionText = document.getElementById("mobile-connection-text");
        const connectionDot = document.getElementById("mobile-connection-dot");
        if (connectionText) connectionText.textContent = errorMessage(error, "移动端状态不可用");
        if (connectionDot) connectionDot.dataset.state = "error";
      });
  }
})();
