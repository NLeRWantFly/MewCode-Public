from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "frontend" / "static"


def test_desktop_shell_uses_workspace_tree_and_phone_settings() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="workspace-settings"' in html
    assert "用 MewCode 继续" in html
    assert "external-agents-btn" not in html
    assert "external-agents-panel" not in html
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "/api/workspace-tree" in app
    assert "openPhoneSettings" in app
    assert "pairing-qr-wrap" not in app
    assert "pairing-allow-once" in (STATIC / "index.html").read_text(encoding="utf-8")
    assert "允许本次" in (STATIC / "index.html").read_text(encoding="utf-8")
    assert "mobile-pairing-input" not in (STATIC / "index.html").read_text(encoding="utf-8")
    assert "pairing-qr" not in app
    assert "pairing-link" not in app
    assert "qr_svg" not in app
    assert "/api/workspace/continue" in app
    assert "setExternalAgentsOpen" not in app
    assert "conv-time" in app
    assert "conv-count" in app
    assert "message.kind && message.kind !== \"message\"" in app
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert ".conv-item .conv-meta > span { display: none; }" not in css
    assert ".conv-item .conv-count" in css
    assert ".conv-item:hover .conv-count" in css


def test_composer_is_compact_with_context_ring() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'id="composer-ctx-ring"' in html
    assert "composer-shortcut" not in html
    assert "labeled-btn" in html
    assert ".ctx-ring" in css
    assert "min-height: 26px" in css
    assert "composer-ctx-ring" in app
    assert "collapseAllWorkspaceGroups" in app
    assert "folder-plus" in app
    assert "COMPOSER_INPUT_MAX" in app
    assert "softRefreshRuntimeSettings" in app
    assert "softUpdate" in app


def test_chat_composer_recovers_unacknowledged_and_stale_turns() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    server = (ROOT / "frontend" / "server.py").read_text(encoding="utf-8")
    assert "pendingConversationStarts" in app
    assert "markConversationStartPending" in app
    assert "reconcilePendingConversationStart" in app
    assert "failUnacknowledgedConversationStart" in app
    assert "/api/chat/status" in app
    assert "/api/chat/interrupt" in app
    assert "stopActiveConversation" in app
    assert "request_chat_interrupt" in server
    assert '"type": "interrupt_ack"' in server
    assert '"rejected": True' in server


def test_tauri_desktop_bridge_is_acl_allowed() -> None:
    capability = (ROOT / "src-tauri" / "capabilities" / "default.json").read_text(encoding="utf-8")
    permission = (ROOT / "src-tauri" / "permissions" / "desktop-bridge.toml").read_text(encoding="utf-8")
    assert "allow-desktop-bridge" in capability
    assert "127.0.0.1" in capability
    assert "provider_list" in permission
    assert "select_directory" in permission


def test_runtime_provider_column_exposes_grok_family() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    rust = (ROOT / "src-tauri" / "src" / "main.rs").read_text(encoding="utf-8")
    assert "function runtimeProviderFamilies(" in app
    assert 'name: "Codex"' in app
    assert 'name: "Claude Code"' in app
    assert 'name: "Grok"' in app
    assert 'return "grok"' in app
    assert "enrichGrokFamilyModels" not in app
    assert "const provider = String(st.provider" in app
    context_badge = app.split("function updateContextBadge(status) {", 1)[1].split(
        "// ---------- 主题", 1
    )[0]
    assert "const detectedWindow = Math.max(Number(status.detected_window) || 0, 0);" in context_badge
    assert "const manualCap = Number(status.context_tier) > 0 &&" in context_badge
    assert "Number(status.window) > 0 && detectedWindow > Number(status.window);" in context_badge
    assert 'parts.push("手动限额 · 模型上限 " + fmtInt(detectedWindow));' in context_badge
    assert r'"\n当前使用手动限额，模型完整窗口 " + fmtInt(detectedWindow)' in context_badge
    assert '{ value: 500000, label: "500k" }' in app
    assert "Codex、Claude Code、Grok、DeepSeek" in app
    assert 'name: "DeepSeek"' in app
    assert 'return "deepseek"' in app
    # openai-compat must not fall into the Codex family via \\bopenai\\b.
    assert 'protocol === "openai"' in app
    assert "openai-compat" in app
    # Custom openai-compat providers (e.g. Antigravity) each get their own row
    # named from the configured display name, not a shared "其他" bucket.
    assert "function customProviderFamilyKey(" in app
    assert "function customProviderFamilyName(" in app
    assert '("provider:" + key)' in app
    assert 'key: "custom",\n        name: "其他"' not in app
    assert '"xai": {"label":"desktop:example_provider_2"' in rust
    assert '"default_model":"grok-4.6"' in rust
    assert "mergeDesktopProviderPresets" in app
    assert "配置 desktop:example_provider_2 Provider" in app
    assert "runtime-cc-switch-compact" in app
    assert "cc-switch 当前 API" not in app
    assert "claude|anthropic" in app
    assert "enrichClaudeFamilyModels" in app
    assert "dedupeFamilyModels" in app
    assert "mewcode-ui-state-v1" in app
    assert "rememberWorkspaceOpen" in app
    assert "rememberRuntimeFamily" in app








def test_assistant_copy_is_not_on_tool_rows() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "function attachAssistantCopyButton" in app
    assert "已复制助手回复" in app
    assert 'querySelectorAll(".msg-bubble > .md")' in app
    assert 'wrap.closest(".tool-group, .tg-row, .expert-tool-note")' in app
    assert 'insertAdjacentElement("afterend", actions)' in app
    assert "attachAssistantCopyButton(currentMsg, currentText)" in app
    assert "wrap.appendChild(actions)" not in app.split("function attachAssistantCopyButton")[1].split("async function openWorkspaceConversation")[0]
    assert ".msg.assistant:hover .msg-actions" not in css
    assert ".msg.assistant:not(:has(.tool-group)):hover .msg-actions" in css
    assert re.search(r"/static/style\.css\?v=[\w-]+", html)


def test_decision_panels_stay_inside_the_viewport() -> None:
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    # These panels render between the chat list and the composer, where nothing
    # scrolls the page — an unbounded card pushes 取消/确认 off-screen entirely.
    for panel in ("#chat-human-oncall",):
        block = css.split(panel + " {", 1)[1].split("}", 1)[0]
        assert "max-height: calc(100dvh - 170px)" in block
        assert "overflow-y: auto" in block
    for region in (".human-oncall-options", ".ask-user-questions"):
        block = css.split(region + " {", 1)[1].split("}", 1)[0]
        assert "max-height:" in block and "overflow-y: auto" in block


def test_tool_groups_use_compact_vertical_gap() -> None:
    css = (STATIC / "chat-messages.css").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "--chat-tool-gap:" in css
    assert "var(--chat-tool-gap)" in css.split("details.tool-group", 1)[1][:500]
    assert ".msg .md:empty" in css
    assert "orch-stage-msg-head:not(:has(strong:not(:empty)))" in css
    assert ".msg.assistant:has(.tool-group):not(:has(.md:not(:empty)))" in css
    assert re.search(r"/static/chat-messages\.css\?v=[\w-]+", html)


def test_permission_reason_surfaces_in_ui() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "function formatPermissionText(" in app
    assert "formatPermissionText(msg)" in app
    assert 'msg.reason' in app
    assert "AUTO_DENIED:" in (ROOT / "mewcode" / "agent.py").read_text(encoding="utf-8")
    assert "function isToolDenialNote(" in app
    assert "do not surface them as chat-level note banners" in (
        ROOT / "mewcode" / "expert_web.py"
    ).read_text(encoding="utf-8")
    assert '"reason": reason' in (ROOT / "mewcode" / "expert_web.py").read_text(encoding="utf-8")


def test_structure_has_a_direct_workbench_entry() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    workbench = (STATIC / "workbench" / "index.js").read_text(encoding="utf-8")
    assert 'data-open-workbench' in html
    assert "{value: 'structure', label: '结构'" in workbench
    assert "plan.openStep(id)" in workbench
    assert 'orch-canvas-overlay' not in html
    assert 'openCurrentOrchestrationCanvas' not in app


def test_sidebar_project_menu_has_bulk_actions() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert "全部标为已读" in app
    assert "全部归档" in app
    assert "从侧栏移除" in app
    assert 'action: "mark_all_read"' in app or 'bulkSidebarConversationState("mark_all_read"' in app
    assert 'bulkSidebarConversationState("archive_all"' in app
    assert "is-unread" in app
    assert ".conv-item.is-unread" in css


def test_added_project_is_revealed_in_sidebar() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "function revealAddedProjectInSidebar(dir)" in app
    assert 'workspaceOpenState["machine:local"] = true' in app
    assert 'workspaceOpenState["source:local:mewcode"] = true' in app
    assert 'projectOpenState[String(dir || "")] = true' in app
    assert 'if (search) search.value = ""' in app
    assert "revealAddedProjectInSidebar(data.dir);" in app
    add_flow = app.split("async function addProjectFromToolbar(trigger)", 1)[1].split(
        "function renderWorkspaceTree", 1
    )[0]
    assert "showProjectPicker(trigger);" in add_flow
    assert "selectDirectory" not in add_flow
    assert "添加已有项目" in app
    assert "选择已有项目文件夹" in app
    assert "添加此文件夹" in app




def test_expert_mirror_reuses_collapsible_tool_group() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "buildToolGroup" in app and "mirrorExpertChatEvent" in app
    assert "orch-stage-tool" not in app or "renderTool(" in app
    mirror = app.split("function mirrorExpertChatEvent(", 1)[1].split("\n  function ", 1)[0]
    assert "renderTool(" in mirror
    assert 'className = "orch-stage-tool"' not in mirror
    assert "currentMsg = entry.msg" in mirror
    bubble = app.split("function ensureOrchChatBubble(", 1)[1].split("\n  function ", 1)[0]
    assert "msg-bubble" in bubble
    assert "function toolLineSummary(" in app
    assert "a.command || a.cmd || a.script" in app
    assert "renderToolResult(" in mirror


def test_orch_chat_bubbles_isolate_conversation_and_node_attempts() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "chat-messages.css").read_text(encoding="utf-8")
    stem_fn = app.split("function orchChatStem(", 1)[1].split("\n  function ", 1)[0]
    key_fn = app.split("function orchChatKey(", 1)[1].split("\n  function ", 1)[0]
    render = app.split("function renderEntries(", 1)[1].split("\n  function ", 1)[0]
    activate = app.split("function activateConversation(", 1)[1].split("\n  const ", 1)[0]
    assert "conversation_id" in stem_fn
    assert "node_attempt" in key_fn
    assert "expert_node_started" in key_fn
    assert "orchChatLiveKeys" in key_fn
    assert "orchChatLiveKeys.clear()" in render
    assert "orchChatLiveKeys.clear()" in activate
    assert ".msg .md a" in css
    assert re.search(r"/static/app\.js\?v=[\w-]+", html)


def test_ask_user_websocket_hooks_exist() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    server = (ROOT / "frontend" / "server.py").read_text(encoding="utf-8")
    expert = (ROOT / "mewcode" / "expert_web.py").read_text(encoding="utf-8")
    assert "ask_user" in app and "ask_user_response" in app
    assert 'case "ask_user":' in app
    assert 'type: "ask_user_response"' in app
    replay_prompt = app.split("function replayPendingConversationPrompt(", 1)[1].split(
        "\n  async function ", 1
    )[0]
    assert 'msg.type === "ask_user"' in replay_prompt
    replay_events = app.split("function replayConversationEvents(", 1)[1].split(
        "\n  function ", 1
    )[0]
    assert "mirrorExpertChatEvent(message)" in replay_events
    assert "AskUserRequest" in server
    assert 'frontend.prompt_dispatch' in server and '"ask_user_response"' in server
    assert "AskUserRequest" in expert
    assert 'await receive_interaction(event, "ask_user")' in expert
    assert "HumanOnCallTool" in expert
    assert 'await receive_interaction(event, "human_oncall")' in expert




def test_expert_history_reopens_even_when_display_index_is_out_of_range() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    expert = (ROOT / "mewcode" / "expert_web.py").read_text(encoding="utf-8")
    render = app.split("function renderEntries(", 1)[1].split("\n  function ", 1)[0]
    snapshot = app.split("function renderExpertSnapshot(", 1)[1].split(
        "\n  async function ", 1
    )[0]
    mirror = app.split("function mirrorExpertChatEvent(", 1)[1].split("\n  function ", 1)[0]
    assert "index > entries.length" in render
    assert "index = entries.length" in render
    assert "orchChatBubbles.clear()" in render
    assert "{ force: true }" in snapshot
    assert "type: \"expert_node_completed\"" in snapshot
    assert "!options.force && !currentStatus.expert_mode" not in mirror
    assert "String(event.conversation_id) !== activeConversationId()" in mirror
    assert "type.endsWith(\"_completed\") || type === \"expert_stage\"" in mirror
    assert "def visible_chat_index(" in expert
    assert re.search(r"/static/app\.js\?v=[\w-]+", html)


def test_review_model_select_shows_provider_name_and_matches_provider() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    review = app.split("runtime-review-model", 1)[1].split("metaStrip.appendChild", 1)[0]
    assert "model.provider_name || model.provider_key" in review
    assert '" · " + model.provider_key' not in review
    assert "model.provider_key === review.provider_key" in review


def test_multiline_message_paragraphs_stay_visible() -> None:
    css = (STATIC / "chat-messages.css").read_text(encoding="utf-8")
    # :only-child ignores text nodes — `p:has(> br:only-child)` would also match
    # "文字<br>文字" and hide every multi-line paragraph. Blank <p><br></p>
    # artifacts are stripped in markdown-render.js instead.
    assert "br:only-child" not in css
    assert ".msg .md > p:empty" in css
    md = (STATIC / "markdown-render.js").read_text(encoding="utf-8")
    assert "<\\/p>" in md  # blank <p><br></p> strip present


def test_composer_stays_reachable_in_short_windows() -> None:
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert "@media (max-height: 560px)" in css
    assert "@media (max-height: 400px)" in css
    tall = css.split("@media (max-height: 560px)", 1)[1]
    assert "#chat-input { max-height: 80px" in tall
    assert ".chat-queue { max-height: 72px" in tall


def test_open_conversation_rehydrates_expert_flows_after_reset_pipeline() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "function rehydrateExpertRuns(" in app
    # Must call rehydrate AFTER resetPipeline in openConversation.
    open_idx = app.index("async function openConversation(")
    chunk = app[open_idx: open_idx + 2500]
    assert "resetPipeline(" in chunk
    assert "rehydrateExpertRuns(" in chunk
    assert chunk.index("resetPipeline(") < chunk.index("rehydrateExpertRuns(")


def test_startup_hydrates_active_conversation_and_expert_checkpoint() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    startup = app.split("// ---------- 启动 ----------", 1)[1]
    assert "async function bootstrapWorkspace()" in startup
    assert "void loadSidebar();" in startup
    assert "await refreshState();" in startup
    assert "const conversationId = activeConversationId()" in startup
    assert "await openConversation(" in startup
    assert "void bootstrapWorkspace()" in startup








def test_server_forwards_resume_after_node() -> None:
    server = (ROOT / "frontend" / "server.py").read_text(encoding="utf-8")
    web = (ROOT / "mewcode" / "expert_web.py").read_text(encoding="utf-8")
    assert "resume_after_node" in server
    assert "resume_after_node" in web
    assert "plan_store=_conversation_plan_store(runtime.conversation_id)" in server
    assert "saved_execution_flow(session.work_dir, flow_store=flow_store, plan_store=plan_store)" in web
    assert "flow=latest_flow" in web
    assert (
        "resume_after_node=resume_after_node" in web
        or "resume_after_node=after" in web
        or "resume_after_node=str(" in server
        or 'msg.get("resume_after_node")' in server
    )
